import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import Qt, QSettings
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

    def test_results_table_is_beside_the_settings_panel(self):
        splitter = self.window.main_splitter

        self.assertEqual(splitter.orientation(), Qt.Orientation.Horizontal)
        self.assertEqual(splitter.count(), 2)
        self.assertTrue(splitter.widget(0).isAncestorOf(self.window.configuration_group))
        self.assertTrue(splitter.widget(1).isAncestorOf(self.window.device_table))

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

    def test_profile_settings_store_only_name_id_and_path(self):
        credential_file = self.base / "private.env"
        credential_file.write_text(
            "SSH_USERNAME=profile-user\n"
            "SSH_PASSWORD=profile-password\n"
            "ENABLE_SECRET=profile-enable\n",
            encoding="utf-8",
        )
        profile = self.window.profile_store.add("Private Profile", credential_file)
        self.window.profile_store.set_selected_id(profile.profile_id)

        stored = "\n".join(
            f"{key}={self.settings.value(key)}" for key in self.settings.allKeys()
        )
        self.assertIn("Private Profile", stored)
        self.assertIn(str(credential_file.absolute()), stored)
        self.assertNotIn("profile-user", stored)
        self.assertNotIn("profile-password", stored)
        self.assertNotIn("profile-enable", stored)

    def test_profile_selection_loads_masked_credentials_and_optional_settings(self):
        credential_file = self.base / ".env"
        credential_file.write_text(
            "SSH_USERNAME=imported-admin\n"
            "SSH_PASSWORD=imported-secret\n"
            "ENABLE_SECRET=enable-secret\n"
            "SSH_PORT=2200\n",
            encoding="utf-8",
        )
        profile = self.window.profile_store.add("Lab Admin", credential_file)
        self.window._refresh_profile_combo(profile.profile_id)
        profile_index = self.window.profile_combo.findData(profile.profile_id)
        self.window._profile_activated(profile_index)

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
        self.assertEqual(self.window._prepared.settings.enable_secret, "enable-secret")

    def test_profiles_are_sorted_and_reject_invalid_names(self):
        first_file = self.base / "first.env"
        second_file = self.base / "second.env"
        first_file.write_text("SSH_USERNAME=one\nSSH_PASSWORD=first\n", encoding="utf-8")
        second_file.write_text("SSH_USERNAME=two\nSSH_PASSWORD=second\n", encoding="utf-8")

        self.window.profile_store.add("Zulu", first_file)
        self.window.profile_store.add("Alpha", second_file)

        self.assertEqual(
            [profile.name for profile in self.window.profile_store.profiles()],
            ["Alpha", "Zulu"],
        )
        with self.assertRaisesRegex(runner.ConfigurationError, "already exists"):
            self.window.profile_store.add("alpha", first_file)
        with self.assertRaisesRegex(runner.ConfigurationError, "name is required"):
            self.window.profile_store.add("  ", first_file)

    def test_profile_manager_adds_renames_relinks_and_removes_references(self):
        first_file = self.base / "first.env"
        second_file = self.base / "second.env"
        first_file.write_text("SSH_USERNAME=one\nSSH_PASSWORD=first\n", encoding="utf-8")
        second_file.write_text("SSH_USERNAME=two\nSSH_PASSWORD=second\n", encoding="utf-8")
        dialog = gui.CredentialProfileDialog(self.window.profile_store, parent=self.window)

        with patch.object(
            gui.QFileDialog,
            "getOpenFileName",
            return_value=(str(first_file), "Environment files (*.env)"),
        ), patch.object(gui.QInputDialog, "getText", return_value=("Lab Admin", True)):
            dialog.add_profile()

        profile = self.window.profile_store.profiles()[0]
        dialog.profile_table.selectRow(0)
        with patch.object(gui.QInputDialog, "getText", return_value=("Core Admin", True)):
            dialog.rename_profile()
        self.assertEqual(self.window.profile_store.get(profile.profile_id).name, "Core Admin")

        dialog.profile_table.selectRow(0)
        with patch.object(
            gui.QFileDialog,
            "getOpenFileName",
            return_value=(str(second_file), "Environment files (*.env)"),
        ):
            dialog.relink_profile()
        self.assertEqual(
            self.window.profile_store.get(profile.profile_id).env_path,
            second_file.absolute(),
        )

        dialog.profile_table.selectRow(0)
        with patch.object(
            gui.QMessageBox,
            "question",
            return_value=QMessageBox.StandardButton.Yes,
        ):
            dialog.remove_profile()
        self.assertEqual(self.window.profile_store.profiles(), [])
        self.assertTrue(first_file.exists())
        self.assertTrue(second_file.exists())

    def test_invalid_profile_file_is_rejected_by_manager(self):
        invalid_file = self.base / "invalid.env"
        invalid_file.write_text("SSH_USERNAME=admin\n", encoding="utf-8")
        dialog = gui.CredentialProfileDialog(self.window.profile_store, parent=self.window)

        with patch.object(
            gui.QFileDialog,
            "getOpenFileName",
            return_value=(str(invalid_file), "Environment files (*.env)"),
        ), patch.object(gui.QMessageBox, "critical") as critical, patch.object(
            gui.QInputDialog,
            "getText",
        ) as get_name:
            dialog.add_profile()

        critical.assert_called_once()
        get_name.assert_not_called()
        self.assertEqual(self.window.profile_store.profiles(), [])

    def test_manual_edit_detaches_loaded_profile_without_editing_file(self):
        credential_file = self.base / "saved.env"
        original = "SSH_USERNAME=saved-user\nSSH_PASSWORD=saved-password\nSSH_PORT=2200\n"
        credential_file.write_text(original, encoding="utf-8")
        profile = self.window.profile_store.add("Saved", credential_file)
        self.window._refresh_profile_combo(profile.profile_id)
        self.window._profile_activated(self.window.profile_combo.findData(profile.profile_id))

        self.window.username_edit.setText("one-off-user")

        self.assertIsNone(self.window.profile_combo.currentData())
        self.assertIsNone(self.window._credential_file)
        self.assertIsNone(self.window.profile_store.selected_id())
        self.assertEqual(credential_file.read_text(encoding="utf-8"), original)

    def test_reselecting_profile_reloads_changed_env_file(self):
        credential_file = self.base / "reload.env"
        credential_file.write_text(
            "SSH_USERNAME=first-user\nSSH_PASSWORD=first-password\n",
            encoding="utf-8",
        )
        profile = self.window.profile_store.add("Reload", credential_file)
        self.window._refresh_profile_combo(profile.profile_id)
        index = self.window.profile_combo.findData(profile.profile_id)
        self.window._profile_activated(index)
        credential_file.write_text(
            "SSH_USERNAME=second-user\nSSH_PASSWORD=second-password\n",
            encoding="utf-8",
        )

        self.window._profile_activated(index)

        self.assertEqual(self.window.username_edit.text(), "second-user")
        self.assertEqual(self.window.password_edit.text(), "second-password")

    def test_selecting_missing_profile_keeps_it_available_for_relinking(self):
        profile = self.window.profile_store.add("Unavailable", self.base / "gone.env")
        self.window._refresh_profile_combo(profile.profile_id)
        index = self.window.profile_combo.findData(profile.profile_id)

        self.window._profile_activated(index)

        self.assertEqual(self.window.profile_combo.currentData(), profile.profile_id)
        self.assertEqual(self.window.username_edit.text(), "")
        self.assertEqual(self.window.password_edit.text(), "")
        self.assertIn("Could not load profile", self.window.validation_label.text())
        self.assertIsNotNone(self.window.profile_store.get(profile.profile_id))

    def test_last_selected_profile_reloads_after_reopening(self):
        credential_file = self.base / "remembered.env"
        credential_file.write_text(
            "SSH_USERNAME=remembered-user\nSSH_PASSWORD=remembered-password\n",
            encoding="utf-8",
        )
        profile = self.window.profile_store.add("Remembered", credential_file)
        self.window._refresh_profile_combo(profile.profile_id)
        self.window._profile_activated(self.window.profile_combo.findData(profile.profile_id))

        reopened = gui.MainWindow(self.settings)
        try:
            self.assertEqual(reopened.profile_combo.currentData(), profile.profile_id)
            self.assertEqual(reopened.username_edit.text(), "remembered-user")
            self.assertEqual(reopened.password_edit.text(), "remembered-password")
        finally:
            reopened.close()

    def test_missing_last_profile_starts_manual_and_keeps_reference(self):
        missing_file = self.base / "missing.env"
        profile = self.window.profile_store.add("Missing", missing_file)
        self.window.profile_store.set_selected_id(profile.profile_id)

        reopened = gui.MainWindow(self.settings)
        try:
            self.assertIsNone(reopened.profile_combo.currentData())
            self.assertEqual(reopened.username_edit.text(), "")
            self.assertIn("Could not load", reopened.validation_label.text())
            self.assertIsNotNone(reopened.profile_store.get(profile.profile_id))
        finally:
            reopened.close()

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

    def test_double_clicking_result_shows_the_full_message(self):
        self.populate_valid_inputs()
        self.assertTrue(self.window.validate_inputs(show_success=False))
        row = self.window._rows["192.0.2.1"]
        full_error = (
            "NetmikoTimeoutException: TCP connection to device failed.\n"
            "Verify the hostname, port, and network reachability."
        )
        self.window._set_cell(row, 1, "Failed")
        self.window._set_cell(row, 3, full_error)

        with patch.object(gui.QDialog, "exec", autospec=True) as execute_dialog:
            self.window.handle_table_double_click(row, 3)

        execute_dialog.assert_called_once()
        dialogs = self.window.findChildren(gui.QDialog)
        self.assertEqual(len(dialogs), 1)
        dialog = dialogs[0]
        detail_boxes = dialog.findChildren(gui.QPlainTextEdit)
        self.assertEqual(len(detail_boxes), 1)
        self.assertEqual(detail_boxes[0].toPlainText(), full_error)
        self.assertIn("Double-click", self.window.device_table.item(row, 3).toolTip())

    def test_double_clicking_another_column_does_not_open_transcript(self):
        self.populate_valid_inputs()
        self.assertTrue(self.window.validate_inputs(show_success=False))
        row = self.window._rows["192.0.2.1"]

        with patch.object(gui.QDesktopServices, "openUrl") as open_url:
            self.window.open_transcript_at(row, 0)

        open_url.assert_not_called()


if __name__ == "__main__":
    unittest.main()
