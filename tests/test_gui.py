import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QSettings
    from PySide6.QtGui import QCloseEvent
    from PySide6.QtWidgets import QApplication, QMessageBox

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
        inventory = self.base / "devices.txt"
        commands = self.base / "commands.txt"
        env_file = self.base / ".env"
        inventory.write_text("192.0.2.1\nbad host\n", encoding="utf-8")
        commands.write_text("[exec]\nshow version\n", encoding="utf-8")
        env_file.write_text("SSH_USERNAME=admin\nSSH_PASSWORD=secret\n", encoding="utf-8")
        self.window.inventory_edit.setText(str(inventory))
        self.window.command_edit.setText(str(commands))
        self.window.env_edit.setText(str(env_file))
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

        self.window.command_edit.setText(str(self.base / "different.txt"))

        self.assertFalse(self.window.run_button.isEnabled())
        self.assertIsNone(self.window._prepared)

    def test_validation_remembers_paths_but_not_credentials(self):
        self.populate_valid_inputs()
        self.assertTrue(self.window.validate_inputs(show_success=False))

        self.assertEqual(
            self.settings.value("paths/env"),
            str(self.base / ".env"),
        )
        all_keys = set(self.settings.allKeys())
        self.assertNotIn("SSH_PASSWORD", all_keys)
        self.assertNotIn("secret", [self.settings.value(key) for key in all_keys])

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


if __name__ == "__main__":
    unittest.main()
