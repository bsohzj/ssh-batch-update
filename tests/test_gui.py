import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QSettings
    from PySide6.QtGui import QCloseEvent
    from PySide6.QtWidgets import QApplication, QLineEdit, QMessageBox

    import gui
    import run_commands as runner
except ImportError:
    GUI_AVAILABLE = False
else:
    GUI_AVAILABLE = True


@unittest.skipUnless(GUI_AVAILABLE, "PySide6 is not installed")
class MainWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.settings = QSettings(
            str(self.base / "settings.ini"),
            QSettings.Format.IniFormat,
        )
        self.window = gui.MainWindow(self.settings)

    def tearDown(self):
        self.window.close()
        self.app.processEvents()
        self.temporary.cleanup()

    def populate_valid_inputs(self):
        self.window.inventory_text_edit.setPlainText("192.0.2.1\nbad host\n")
        self.window.exec_commands_edit.setPlainText("show version\n")
        self.window.config_commands_edit.setPlainText("interface loopback 1\n")
        self.window.username_edit.setText("admin")
        self.window.password_edit.setText("secret")
        self.window.output_edit.setText(str(self.base / "outputs"))
        self.window.device_type_combo.setCurrentText("cisco_ios")

    def test_validation_populates_devices_and_enables_live_run(self):
        self.populate_valid_inputs()

        valid = self.window.validate_inputs(show_success=False)

        self.assertTrue(valid)
        self.assertTrue(self.window.run_button.isEnabled())
        self.assertEqual(self.window.device_table.rowCount(), 2)
        self.assertEqual(self.window.device_table.item(0, 1).text(), "Ready")
        self.assertEqual(self.window.device_table.item(1, 1).text(), "Invalid")

    def test_input_change_invalidates_a_successful_validation(self):
        self.populate_valid_inputs()
        self.assertTrue(self.window.validate_inputs(show_success=False))

        self.window.exec_commands_edit.setPlainText("display version")

        self.assertFalse(self.window.run_button.isEnabled())
        self.assertIsNone(self.window._prepared)

    def test_validation_remembers_paths_but_not_credentials(self):
        self.populate_valid_inputs()
        self.assertTrue(self.window.validate_inputs(show_success=False))

        all_keys = set(self.settings.allKeys())
        self.assertNotIn("paths/env", all_keys)
        self.assertNotIn("SSH_PASSWORD", all_keys)
        self.assertNotIn("admin", [self.settings.value(key) for key in all_keys])
        self.assertNotIn("secret", [self.settings.value(key) for key in all_keys])
        self.assertNotIn("show version", [self.settings.value(key) for key in all_keys])

    def test_password_is_hidden_and_credentials_can_be_imported(self):
        credential_file = self.base / ".env"
        credential_file.write_text(
            "SSH_USERNAME=imported-admin\n"
            "SSH_PASSWORD=imported-secret\n"
            "SSH_PORT=2200\n",
            encoding="utf-8",
        )

        with patch.object(
            gui.QFileDialog,
            "getOpenFileName",
            return_value=(str(credential_file), "Environment files (*.env)"),
        ):
            self.window._import_credentials()

        self.assertEqual(self.window.username_edit.text(), "imported-admin")
        self.assertEqual(self.window.password_edit.text(), "imported-secret")
        self.assertEqual(
            self.window.password_edit.echoMode(),
            QLineEdit.EchoMode.Password,
        )
        self.window.inventory_text_edit.setPlainText("192.0.2.1")
        self.window.exec_commands_edit.setPlainText("show version")
        self.window.output_edit.setText(str(self.base / "outputs"))
        self.assertTrue(self.window.validate_inputs(show_success=False))
        self.assertEqual(self.window._prepared.settings.port, 2200)

    def test_importing_sectioned_command_file_splits_the_command_boxes(self):
        command_file = self.base / "commands.txt"
        command_file.write_text(
            "[exec]\nshow version\n[config]\ninterface loopback 1\n",
            encoding="utf-8",
        )

        with patch.object(
            gui.QFileDialog,
            "getOpenFileName",
            return_value=(str(command_file), "Text files (*.txt)"),
        ):
            self.window._import_sectioned_commands()

        self.assertEqual(self.window.exec_commands_edit.toPlainText(), "show version")
        self.assertEqual(
            self.window.config_commands_edit.toPlainText(),
            "interface loopback 1",
        )

    def test_declining_confirmation_does_not_start_worker_thread(self):
        self.populate_valid_inputs()
        self.assertTrue(self.window.validate_inputs(show_success=False))

        with patch.object(
            gui.QMessageBox,
            "warning",
            return_value=QMessageBox.StandardButton.Cancel,
        ):
            self.window.start_run()

        self.assertFalse(self.window._running)
        self.assertIsNone(self.window._thread)

    def test_progress_event_never_requires_command_text(self):
        self.populate_valid_inputs()
        self.assertTrue(self.window.validate_inputs(show_success=False))

        self.window.handle_progress(
            runner.ProgressEvent(
                "command_started",
                address="192.0.2.1",
                section="exec",
                line_number=2,
                status="running",
            )
        )

        row = self.window._rows["192.0.2.1"]
        self.assertEqual(self.window.device_table.item(row, 1).text(), "Running")
        self.assertEqual(self.window.device_table.item(row, 2).text(), "exec / 2")

    def test_cancel_requests_cooperative_stop(self):
        self.assertEqual(self.window.cancel_button.text(), "Stop Run")
        self.assertIn("#c62828", self.window.cancel_button.styleSheet())
        token = runner.CancellationToken()
        self.window._running = True
        self.window._token = token

        self.window.cancel_run()

        self.assertTrue(token.cancelled)
        self.assertIn("Cancelling", self.window.status_label.text())
        self.window._running = False
        self.window._token = None

    def test_closing_during_run_requests_the_same_safe_cancellation(self):
        token = runner.CancellationToken()
        self.window._running = True
        self.window._token = token
        event = QCloseEvent()

        with patch.object(
            gui.QMessageBox,
            "question",
            return_value=QMessageBox.StandardButton.Yes,
        ):
            self.window.closeEvent(event)

        self.assertTrue(token.cancelled)
        self.assertTrue(self.window._close_after_run)
        self.assertFalse(event.isAccepted())
        self.window._running = False
        self.window._token = None
        self.window._close_after_run = False

    def test_completed_run_enables_existing_result_actions(self):
        run_directory = self.base / "outputs" / "run_test"
        run_directory.mkdir(parents=True)
        summary = run_directory / "summary.csv"
        summary.write_text("device,status\n", encoding="utf-8")
        result = runner.BatchResult((), run_directory, summary)

        self.window._run_completed(result)

        self.assertTrue(self.window.open_output_button.isEnabled())
        self.assertTrue(self.window.open_summary_button.isEnabled())

    def test_double_clicking_transcript_column_opens_existing_file(self):
        self.populate_valid_inputs()
        self.assertTrue(self.window.validate_inputs(show_success=False))
        self.assertEqual(self.window.device_table.horizontalHeaderItem(4).text(), "Output")
        transcript = self.base / "device-transcript.txt"
        transcript.write_text("show version output\n", encoding="utf-8")
        row = self.window._rows["192.0.2.1"]
        self.window._set_cell(row, 4, str(transcript))

        with patch.object(gui.QDesktopServices, "openUrl") as open_url:
            self.window.open_transcript_at(row, 4)

        open_url.assert_called_once()
        self.assertEqual(
            open_url.call_args.args[0].toLocalFile(),
            str(transcript),
        )

    def test_double_clicking_another_column_does_not_open_transcript(self):
        self.populate_valid_inputs()
        self.assertTrue(self.window.validate_inputs(show_success=False))
        row = self.window._rows["192.0.2.1"]

        with patch.object(gui.QDesktopServices, "openUrl") as open_url:
            self.window.open_transcript_at(row, 0)

        open_url.assert_not_called()


if __name__ == "__main__":
    unittest.main()
