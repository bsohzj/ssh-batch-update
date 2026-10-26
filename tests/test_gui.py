import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QEvent, Qt, QSettings, QTime
    from PySide6.QtGui import QCloseEvent, QColor, QPalette
    from PySide6.QtWidgets import QApplication, QDialog, QLineEdit, QMessageBox

    import batch_queue
    import gui
    import history
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
        self.secret_store = batch_queue.MemorySecretStore()
        self.history_store = history.HistoryStore(self.base / "history.sqlite3")
        self.history_commands = history.MemoryHistoryCommandStore()
        self.window = gui.MainWindow(
            self.settings,
            secret_store=self.secret_store,
            history_store=self.history_store,
            history_command_store=self.history_commands,
        )

    def tearDown(self):
        with patch.object(
            gui.QMessageBox,
            "question",
            return_value=QMessageBox.StandardButton.Yes,
        ):
            self.window.close()
        self.app.processEvents()
        self.temporary.cleanup()

    def populate_valid_inputs(self):
        self.window.inventory_text_edit.setPlainText("192.0.2.1\n10.2.0.44a\n")
        self.window.exec_commands_edit.setPlainText("show version\n")
        self.window.config_commands_edit.setPlainText("interface loopback 1\n")
        self.window.username_edit.setText("admin")
        self.window.password_edit.setText("secret")
        self.window.output_edit.setText(str(self.base / "outputs"))
        self.window.device_type_combo.setCurrentText("cisco_ios")

    def add_history_record(
        self,
        job_id="history-job",
        status="succeeded",
        successes=1,
        failures=0,
        cancelled=0,
        source="manual",
        profile="Manual",
        run_directory="",
        summary_path="",
    ):
        now = datetime.now(timezone.utc)
        record = history.HistoryRecord(
            job_id=job_id,
            batch_name=f"cisco_ios — 1 devices — {job_id}",
            created_utc=now - timedelta(minutes=2),
            scheduled_utc=now - timedelta(minutes=1) if source == "scheduled" else None,
            started_utc=now - timedelta(minutes=1),
            completed_utc=None,
            source=source,
            profile_label=profile,
            device_type="cisco_ios",
            valid_devices=1,
            invalid_devices=0,
            command_count=2,
        )
        self.history_store.start(record)
        if status != "running":
            result_text = (
                f"{successes} succeeded, {failures} failed, {cancelled} cancelled"
            )
            self.history_store.finalize(
                job_id,
                status,
                successes,
                failures,
                cancelled,
                result_text,
                run_directory,
                summary_path,
                completed_utc=now,
            )
        return self.history_store.get(job_id)

    def test_validation_populates_devices_and_enables_live_run(self):
        self.populate_valid_inputs()

        valid = self.window.validate_inputs(show_success=False)

        self.assertTrue(valid)
        self.assertTrue(self.window.run_button.isEnabled())
        self.assertEqual(self.window.device_table.rowCount(), 2)
        self.assertEqual(self.window.device_table.item(0, 1).text(), "Ready")
        self.assertEqual(self.window.device_table.item(1, 1).text(), "Invalid")

    def test_validation_label_includes_failure_reason(self):
        self.window.inventory_text_edit.setPlainText("192.0.2.1")
        self.window.exec_commands_edit.setPlainText("show version")
        self.window.output_edit.setText(str(self.base / "outputs"))

        valid = self.window.validate_inputs(show_success=False)

        self.assertFalse(valid)
        self.assertIn(
            "Validation failed; Missing required selection: username, password",
            self.window.validation_label.text(),
        )

    def test_validation_reports_when_all_ip_addresses_are_invalid(self):
        self.populate_valid_inputs()
        self.window.inventory_text_edit.setPlainText("10.2.0.44a")

        valid = self.window.validate_inputs(show_success=False)

        self.assertFalse(valid)
        self.assertEqual(
            self.window.validation_label.text(),
            "Validation failed; no valid IP addresses found "
            "(1 invalid device entry)",
        )
        self.assertFalse(self.window.run_button.isEnabled())

    def test_clear_buttons_follow_their_text_content(self):
        self.assertFalse(self.window.clear_devices_button.isEnabled())
        self.assertFalse(self.window.clear_commands_button.isEnabled())
        self.assertEqual(self.window.clear_devices_button.styleSheet(), "")
        self.assertEqual(self.window.clear_commands_button.styleSheet(), "")

        self.window.inventory_text_edit.setPlainText("192.0.2.1")
        self.assertTrue(self.window.clear_devices_button.isEnabled())
        self.window.inventory_text_edit.clear()
        self.assertFalse(self.window.clear_devices_button.isEnabled())

        self.window.config_commands_edit.setPlainText("interface loopback 1")
        self.assertTrue(self.window.clear_commands_button.isEnabled())
        self.window.config_commands_edit.clear()
        self.assertFalse(self.window.clear_commands_button.isEnabled())

        self.window.exec_commands_edit.setPlainText("show version")
        self.assertTrue(self.window.clear_commands_button.isEnabled())

    def test_clear_devices_preserves_other_inputs_and_results(self):
        self.populate_valid_inputs()
        self.assertTrue(self.window.validate_inputs(show_success=False))
        row_count = self.window.device_table.rowCount()
        self.window._set_cell(0, 4, "/tmp/existing-device-output.txt")
        result_marker = object()
        self.window._batch_result = result_marker
        username = self.window.username_edit.text()
        password = self.window.password_edit.text()
        device_type = self.window.device_type_combo.currentText()
        output_folder = self.window.output_edit.text()

        self.window.clear_devices_button.click()

        self.assertEqual(self.window.inventory_text_edit.toPlainText(), "")
        self.assertFalse(self.window.clear_devices_button.isEnabled())
        self.assertIsNone(self.window._prepared)
        self.assertFalse(self.window.run_button.isEnabled())
        self.assertEqual(self.window.device_table.rowCount(), row_count)
        self.assertEqual(
            self.window.device_table.item(0, 4).text(),
            "/tmp/existing-device-output.txt",
        )
        self.assertIs(self.window._batch_result, result_marker)
        self.assertEqual(self.window.username_edit.text(), username)
        self.assertEqual(self.window.password_edit.text(), password)
        self.assertEqual(self.window.device_type_combo.currentText(), device_type)
        self.assertEqual(self.window.output_edit.text(), output_folder)

    def test_clear_commands_clears_both_and_preserves_devices_and_results(self):
        self.populate_valid_inputs()
        self.assertTrue(self.window.validate_inputs(show_success=False))
        devices = self.window.inventory_text_edit.toPlainText()
        row_count = self.window.device_table.rowCount()
        self.window._set_cell(0, 4, "/tmp/existing-command-output.txt")
        result_marker = object()
        self.window._batch_result = result_marker

        self.window.clear_commands_button.click()

        self.assertEqual(self.window.exec_commands_edit.toPlainText(), "")
        self.assertEqual(self.window.config_commands_edit.toPlainText(), "")
        self.assertFalse(self.window.clear_commands_button.isEnabled())
        self.assertEqual(self.window.inventory_text_edit.toPlainText(), devices)
        self.assertIsNone(self.window._prepared)
        self.assertFalse(self.window.run_button.isEnabled())
        self.assertEqual(self.window.device_table.rowCount(), row_count)
        self.assertEqual(
            self.window.device_table.item(0, 4).text(),
            "/tmp/existing-command-output.txt",
        )
        self.assertIs(self.window._batch_result, result_marker)

    def test_results_table_is_beside_the_settings_panel(self):
        splitter = self.window.main_splitter

        self.assertEqual(splitter.orientation(), Qt.Orientation.Horizontal)
        self.assertEqual(splitter.count(), 2)
        self.assertTrue(splitter.widget(0).isAncestorOf(self.window.configuration_group))
        self.assertTrue(splitter.widget(1).isAncestorOf(self.window.device_table))

    def test_queue_and_device_results_tabs_and_controls_are_available(self):
        self.assertEqual(self.window.main_tabs.count(), 2)
        self.assertEqual(self.window.main_tabs.tabText(0), "SSH")
        self.assertEqual(self.window.main_tabs.tabText(1), "History")
        self.assertIs(self.window.main_tabs.currentWidget(), self.window.ssh_tab)
        self.assertTrue(self.window.main_tabs.tabBar().expanding())
        self.assertGreaterEqual(self.window.main_tabs.tabBar().minimumHeight(), 44)
        self.assertEqual(self.window.results_tabs.count(), 2)
        self.assertEqual(self.window.results_tabs.tabText(0), "Queue")
        self.assertEqual(self.window.results_tabs.tabText(1), "Device Results")
        self.assertEqual(self.window.queue_table.columnCount(), 8)
        self.assertEqual(self.window.schedule_button.text(), "Schedule Run")
        self.assertEqual(self.window.pause_queue_button.text(), "Pause Queue")
        self.assertFalse(hasattr(self.window, "queue_button"))
        self.assertEqual(self.window.clear_completed_button.text(), "Clear Completed")
        self.assertIn("#c62828", self.window.clear_completed_button.styleSheet())
        self.assertFalse(self.window.clear_completed_button.isEnabled())

    def test_theme_menu_defaults_to_system_and_actions_are_exclusive(self):
        self.assertEqual(self.window.view_menu.title().replace("&", ""), "View")
        self.assertEqual(self.window.theme_menu.title().replace("&", ""), "Theme")
        self.assertTrue(self.window.theme_action_group.isExclusive())
        self.assertTrue(self.window.theme_system_action.isChecked())
        self.assertFalse(self.window.theme_light_action.isChecked())
        self.assertFalse(self.window.theme_dark_action.isChecked())

        self.window.theme_dark_action.trigger()

        self.assertFalse(self.window.theme_system_action.isChecked())
        self.assertFalse(self.window.theme_light_action.isChecked())
        self.assertTrue(self.window.theme_dark_action.isChecked())
        self.assertEqual(self.settings.value("appearance/theme"), "dark")
        self.settings.beginGroup("appearance")
        self.assertEqual(self.settings.childKeys(), ["theme"])
        self.settings.endGroup()

    def test_runtime_theme_is_application_wide_and_system_restores_native_palette(self):
        self.window.theme_dark_action.trigger()
        self.app.processEvents()

        app_palette = self.app.palette()
        self.assertEqual(
            app_palette.color(QPalette.ColorRole.Window), QColor("#202124")
        )
        self.assertEqual(
            self.window.palette().color(QPalette.ColorRole.Window), QColor("#202124")
        )
        dialog = QDialog(self.window)
        self.assertEqual(
            dialog.palette().color(QPalette.ColorRole.Window), QColor("#202124")
        )
        self.assertGreater(self.app.palette().resolveMask(), 0)

        self.window.theme_light_action.trigger()
        self.app.processEvents()

        self.assertTrue(self.window.theme_light_action.isChecked())
        self.assertEqual(
            self.app.palette().color(QPalette.ColorRole.Window), QColor("#f5f5f5")
        )
        self.assertEqual(self.settings.value("appearance/theme"), "light")

        self.window.theme_system_action.trigger()
        self.app.processEvents()

        self.assertEqual(self.window._theme_mode, gui.THEME_SYSTEM)
        self.assertTrue(self.window.theme_system_action.isChecked())
        self.assertEqual(self.settings.value("appearance/theme"), "system")
        self.assertEqual(self.app.palette().resolveMask(), 0)

    def test_stored_theme_is_applied_when_a_window_is_reopened(self):
        self.window.theme_dark_action.trigger()
        self.settings.sync()
        self.window.close()
        self.app.processEvents()

        reopened_settings = QSettings(
            str(self.base / "settings.ini"),
            QSettings.Format.IniFormat,
        )
        self.window = gui.MainWindow(
            reopened_settings,
            secret_store=batch_queue.MemorySecretStore(),
            history_store=history.HistoryStore(self.base / "reopened-history.sqlite3"),
            history_command_store=history.MemoryHistoryCommandStore(),
        )

        self.assertEqual(self.window._theme_mode, gui.THEME_DARK)
        self.assertTrue(self.window.theme_dark_action.isChecked())
        self.assertEqual(
            self.app.palette().color(QPalette.ColorRole.Window), QColor("#202124")
        )

    def test_theme_switch_preserves_lazy_history_and_styles_it_when_created(self):
        self.assertFalse(self.window._history_ui_built)

        self.window.theme_dark_action.trigger()

        self.assertFalse(self.window._history_ui_built)
        self.add_history_record("lazy-dark")
        self.window._ensure_history_ui()
        line_chart = self.window.history_line_view.chart()
        self.assertEqual(
            line_chart.backgroundBrush().color(), QColor("#202124")
        )
        self.assertEqual(
            [series.color().name() for series in line_chart.series()],
            ["#66bb6a", "#ef5350"],
        )

    def test_runtime_theme_refreshes_existing_history_and_destructive_colors(self):
        self.add_history_record("existing-theme")
        self.window._ensure_history_ui()
        self.assertIn("#c62828", self.window.cancel_button.styleSheet())
        self.assertIn("#c62828", self.window.clear_completed_button.styleSheet())
        self.assertIn("#c62828", self.window.history_clear_button.styleSheet())

        self.window.theme_dark_action.trigger()
        self.app.processEvents()

        line_chart = self.window.history_line_view.chart()
        self.assertEqual(
            line_chart.backgroundBrush().color(), QColor("#202124")
        )
        self.assertEqual(
            [series.color().name() for series in line_chart.series()],
            ["#66bb6a", "#ef5350"],
        )
        for button in (
            self.window.cancel_button,
            self.window.clear_completed_button,
            self.window.history_remove_button,
            self.window.history_delete_outputs_button,
            self.window.history_clear_button,
        ):
            self.assertIn("#ff6b6b", button.styleSheet())

    def test_history_tab_is_lazy_and_builds_cards_charts_and_table(self):
        self.assertFalse(self.window._history_ui_built)

        self.window.main_tabs.setCurrentWidget(self.window.history_tab)
        self.app.processEvents()

        self.assertTrue(self.window._history_ui_built)
        self.assertIs(self.window.main_tabs.currentWidget(), self.window.history_tab)
        self.assertEqual(self.window.main_tabs.indexOf(self.window.history_tab), 1)
        self.assertEqual(self.window.results_tabs.indexOf(self.window.history_tab), -1)
        self.assertEqual(self.window.history_table.columnCount(), 9)
        self.assertEqual(self.window.history_runs_value.text(), "0")
        self.assertEqual(len(self.window.history_line_view.chart().series()), 2)
        self.assertEqual(len(self.window.history_pie_view.chart().series()), 1)
        self.assertFalse(self.window.history_empty_label.isHidden())
        self.assertTrue(self.window.history_line_view.isHidden())
        self.assertTrue(self.window.history_pie_view.isHidden())

    def test_reopened_window_defaults_to_ssh_instead_of_last_main_tab(self):
        self.window.main_tabs.setCurrentWidget(self.window.history_tab)
        self.window._save_settings()
        self.window.close()
        self.app.processEvents()

        reopened_settings = QSettings(
            str(self.base / "settings.ini"),
            QSettings.Format.IniFormat,
        )
        self.window = gui.MainWindow(
            reopened_settings,
            secret_store=batch_queue.MemorySecretStore(),
            history_store=history.HistoryStore(self.base / "reopened-history.sqlite3"),
            history_command_store=history.MemoryHistoryCommandStore(),
        )

        self.assertIs(self.window.main_tabs.currentWidget(), self.window.ssh_tab)
        self.assertFalse(self.window._history_ui_built)

    def test_history_dashboard_filters_and_aggregates_records(self):
        self.add_history_record("good", "succeeded", successes=3, profile="Ops")
        self.add_history_record(
            "partial",
            "completed_with_failures",
            successes=1,
            failures=2,
            source="scheduled",
            profile="Night",
        )
        self.window._ensure_history_ui()

        self.assertEqual(self.window.history_runs_value.text(), "2")
        self.assertEqual(self.window.history_success_value.text(), "4")
        self.assertEqual(self.window.history_failure_value.text(), "2")
        self.assertEqual(self.window.history_rate_value.text(), "66.7%")
        self.assertEqual(self.window.history_table.rowCount(), 2)
        self.assertFalse(self.window.history_line_view.isHidden())
        self.assertFalse(self.window.history_pie_view.isHidden())
        line_series = self.window.history_line_view.chart().series()
        self.assertEqual([series.count() for series in line_series], [1, 1])
        self.assertTrue(all(series.pointsVisible() for series in line_series))
        self.assertEqual([series.markerSize() for series in line_series], [8.0, 8.0])
        self.assertEqual(
            [
                [(point.x(), point.y()) for point in series.points()]
                for series in line_series
            ],
            [[(0.0, 4.0)], [(0.0, 2.0)]],
        )

        index = self.window.history_status_combo.findData("completed_with_failures")
        self.window.history_status_combo.setCurrentIndex(index)
        self.app.processEvents()

        self.assertEqual(self.window.history_table.rowCount(), 1)
        self.assertEqual(
            self.window.history_table.item(0, 7).text(),
            "Completed With Failures",
        )

    def test_history_charts_use_dark_palette_and_accessible_colors(self):
        palette = QPalette(self.window.palette())
        palette.setColor(QPalette.ColorRole.Window, QColor("#202124"))
        palette.setColor(QPalette.ColorRole.Base, QColor("#151515"))
        palette.setColor(QPalette.ColorRole.WindowText, QColor("#f1f3f4"))
        palette.setColor(QPalette.ColorRole.Mid, QColor("#5f6368"))
        self.app.setPalette(palette)
        self.add_history_record("dark", "completed_with_failures", 2, 1)

        self.window._ensure_history_ui()

        line_chart = self.window.history_line_view.chart()
        self.assertEqual(
            line_chart.backgroundBrush().color(), palette.color(QPalette.ColorRole.Window)
        )
        self.assertEqual(
            line_chart.plotAreaBackgroundBrush().color(),
            palette.color(QPalette.ColorRole.Base),
        )
        self.assertEqual(
            line_chart.titleBrush().color(), palette.color(QPalette.ColorRole.WindowText)
        )
        for axis in line_chart.axes():
            self.assertEqual(
                axis.labelsColor(), palette.color(QPalette.ColorRole.WindowText)
            )
            self.assertEqual(axis.gridLineColor(), palette.color(QPalette.ColorRole.Mid))
        line_colors = [series.color().name() for series in line_chart.series()]
        self.assertEqual(line_colors, ["#66bb6a", "#ef5350"])

        pie_chart = self.window.history_pie_view.chart()
        self.assertEqual(
            pie_chart.backgroundBrush().color(), palette.color(QPalette.ColorRole.Window)
        )
        pie_colors = [piece.color().name() for piece in pie_chart.series()[0].slices()]
        self.assertEqual(pie_colors, ["#ffca28"])

    def test_palette_change_refreshes_already_created_history_charts(self):
        self.add_history_record("palette")
        self.window._ensure_history_ui()

        with patch.object(self.window, "_refresh_history") as refresh:
            self.window.changeEvent(QEvent(QEvent.Type.PaletteChange))
            self.app.processEvents()

        refresh.assert_called_once_with()

    def test_scheduled_audit_start_snapshots_profile_and_encrypted_commands(self):
        prepared = runner.prepare_run(
            self._valid_request("cisco_ios", "192.0.2.1"),
            require_credentials=True,
            environment={"SSH_USERNAME": "admin", "SSH_PASSWORD": "secret"},
        )
        payload = batch_queue.BatchPayload.from_prepared(prepared, "Night Ops")
        scheduled = datetime.now(timezone.utc) + timedelta(hours=1)
        job = self.window.queue_controller.add(payload, prepared, scheduled)

        self.window._start_audit_record(job, payload, prepared)

        record = self.history_store.get(job.job_id)
        self.assertEqual(record.source, "scheduled")
        self.assertEqual(record.profile_label, "Night Ops")
        snapshot = self.history_commands.get(job.job_id)
        self.assertEqual(
            [(command.section, command.text) for command in snapshot.commands],
            [("exec", "show version"), ("config", "interface loopback 1")],
        )

    def test_completed_run_finalizes_history_and_writes_protected_marker(self):
        prepared = runner.prepare_run(
            self._valid_request("cisco_ios", "192.0.2.1"),
            require_credentials=True,
            environment={"SSH_USERNAME": "admin", "SSH_PASSWORD": "secret"},
        )
        payload = batch_queue.BatchPayload.from_prepared(prepared, "Ops")
        job = self.window.queue_controller.add(payload, prepared)
        self.window.queue_controller.mark_running(job.job_id)
        self.window._job_prepared[job.job_id] = prepared
        self.window._start_audit_record(job, payload, prepared)
        self.window._active_job_id = job.job_id
        self.window._operation = "run"
        run_directory = self.base / "outputs" / "run_history"
        run_directory.mkdir(parents=True)
        transcript = run_directory / "192.0.2.1.txt"
        transcript.write_text("ok", encoding="utf-8")
        summary = run_directory / "summary.csv"
        device = runner.DeviceResult(
            "192.0.2.1", "success", "", transcript_file=str(transcript)
        )
        runner.write_summary(summary, [device])

        self.window._run_completed(
            runner.BatchResult((device,), run_directory, summary)
        )

        record = self.history_store.get(job.job_id)
        self.assertEqual(record.status, "succeeded")
        self.assertEqual(record.success_count, 1)
        self.assertEqual(Path(record.summary_path), summary)
        self.assertTrue((run_directory / history.HISTORY_MARKER).is_file())

    def test_cancelled_run_is_finalized_in_audit_history(self):
        prepared = runner.prepare_run(
            self._valid_request("cisco_ios", "192.0.2.1"),
            require_credentials=True,
            environment={"SSH_USERNAME": "admin", "SSH_PASSWORD": "secret"},
        )
        payload = batch_queue.BatchPayload.from_prepared(prepared)
        job = self.window.queue_controller.add(payload, prepared)
        self.window.queue_controller.mark_running(job.job_id)
        self.window._job_prepared[job.job_id] = prepared
        self.window._start_audit_record(job, payload, prepared)
        self.window._active_job_id = job.job_id
        self.window._operation = "run"
        run_directory = self.base / "outputs" / "run_cancelled"
        run_directory.mkdir(parents=True)
        summary = run_directory / "summary.csv"
        device = runner.DeviceResult(
            "192.0.2.1", "cancelled", "", error="cancelled by user"
        )
        runner.write_summary(summary, [device])

        self.window._run_completed(
            runner.BatchResult((device,), run_directory, summary)
        )

        record = self.history_store.get(job.job_id)
        self.assertEqual(record.status, "cancelled")
        self.assertEqual(record.cancelled_count, 1)

    def test_worker_failure_history_redacts_credentials_and_addresses(self):
        prepared = runner.prepare_run(
            self._valid_request("cisco_ios", "192.0.2.1"),
            require_credentials=True,
            environment={"SSH_USERNAME": "admin", "SSH_PASSWORD": "secret"},
        )
        payload = batch_queue.BatchPayload.from_prepared(prepared, "Manual")
        job = self.window.queue_controller.add(payload, prepared)
        self.window.queue_controller.mark_running(job.job_id)
        self.window._job_prepared[job.job_id] = prepared
        self.window._start_audit_record(job, payload, prepared)
        self.window._active_job_id = job.job_id
        self.window._operation = "run"

        self.window._run_failed("secret rejected by 192.0.2.1 for admin")

        result = self.history_store.get(job.job_id).result_text
        self.assertNotIn("secret", result)
        self.assertNotIn("192.0.2.1", result)
        self.assertNotIn("admin", result)
        self.assertIn("[redacted]", result)
        self.assertIn("[device]", result)

    def test_schedule_dialog_displays_separate_date_and_time_fields(self):
        dialog = gui.ScheduleDialog()
        dialog.show()
        self.app.processEvents()

        self.assertTrue(dialog.date_edit.isVisible())
        self.assertTrue(dialog.time_edit.isVisible())
        self.assertTrue(dialog.date_edit.calendarPopup())
        self.assertEqual(dialog.date_edit.displayFormat(), "ddd, d MMM yyyy")
        self.assertEqual(dialog.time_edit.displayFormat(), "h:mm AP")
        self.assertTrue(dialog.preview_label.text().startswith("Scheduled for "))

        dialog.date_edit.setDate(dialog.date_edit.date().addDays(1))
        dialog.time_edit.setTime(QTime(18, 30))
        self.app.processEvents()

        self.assertIn("6:30 PM", dialog.preview_label.text())
        self.assertGreater(dialog.selected_utc(), datetime.now(timezone.utc))

        dialog.close()

    def test_queue_submission_snapshots_sensitive_inputs_outside_qsettings(self):
        self.populate_valid_inputs()

        with patch.object(
            gui.QMessageBox,
            "warning",
            return_value=QMessageBox.StandardButton.Apply,
        ):
            self.window.start_run()

        self.assertEqual(len(self.window.queue_controller.jobs), 1)
        job = self.window.queue_controller.jobs[0]
        payload = self.window.queue_controller.payload(job.job_id)
        self.assertEqual(payload.inventory_text, "192.0.2.1\n10.2.0.44a\n")
        self.assertEqual(payload.password, "secret")
        self.assertEqual(self.window.queue_table.rowCount(), 1)
        stored = "\n".join(
            f"{key}={self.settings.value(key)}" for key in self.settings.allKeys()
        )
        self.assertNotIn("192.0.2.1", stored)
        self.assertNotIn("show version", stored)
        self.assertNotIn("secret", stored)

    def test_queued_batch_can_be_fully_edited_without_changing_active_snapshot(self):
        self.populate_valid_inputs()
        with patch.object(
            gui.QMessageBox,
            "warning",
            return_value=QMessageBox.StandardButton.Apply,
        ):
            self.window.start_run()
        job_id = self.window.queue_controller.jobs[0].job_id
        original = self.window.queue_controller.payload(job_id)
        self.secret_store.set(
            job_id,
            replace(
                original,
                enable_secret="queued-enable",
                port=2200,
                timeout=30,
            ).to_json(),
        )

        with patch.object(
            gui.QMessageBox,
            "question",
            return_value=QMessageBox.StandardButton.Yes,
        ):
            self.window.edit_selected_batch()
        self.window.exec_commands_edit.setPlainText("show clock")
        with patch.object(
            gui.QMessageBox,
            "warning",
            return_value=QMessageBox.StandardButton.Apply,
        ):
            self.window.save_batch_edit()

        self.assertIsNone(self.window._editing_job_id)
        saved = self.window.queue_controller.payload(job_id)
        self.assertEqual(saved.exec_commands_text, "show clock")
        self.assertEqual(saved.enable_secret, "queued-enable")
        self.assertEqual(saved.port, 2200)
        self.assertEqual(saved.timeout, 30)
        self.assertFalse(self.window.save_edit_button.isVisible())

    def test_dispatch_starts_only_one_ready_job(self):
        first_prepared = runner.prepare_run(
            self._valid_request("huawei", "192.0.2.1"),
            require_credentials=True,
            environment={"SSH_USERNAME": "admin", "SSH_PASSWORD": "secret"},
        )
        second_prepared = runner.prepare_run(
            self._valid_request("cisco_ios", "192.0.2.2"),
            require_credentials=True,
            environment={"SSH_USERNAME": "admin", "SSH_PASSWORD": "secret"},
        )
        first = self.window.queue_controller.add(
            batch_queue.BatchPayload.from_prepared(first_prepared), first_prepared
        )
        self.window.queue_controller.add(
            batch_queue.BatchPayload.from_prepared(second_prepared), second_prepared
        )
        self.window.queue_controller.paused = False

        with patch.object(self.window, "_start_worker") as start_worker:
            self.window._queue_tick()
            self.window._queue_tick()

        start_worker.assert_called_once()
        self.assertEqual(self.window.queue_controller.active_job_id, first.job_id)

    def test_hidden_dispatch_keeps_history_visible_and_marks_results(self):
        prepared = runner.prepare_run(
            self._valid_request("huawei", "192.0.2.1"),
            require_credentials=True,
            environment={"SSH_USERNAME": "admin", "SSH_PASSWORD": "secret"},
        )
        self.window.queue_controller.add(
            batch_queue.BatchPayload.from_prepared(prepared), prepared
        )
        self.window.queue_controller.paused = False
        self.window.main_tabs.setCurrentWidget(self.window.history_tab)

        with patch.object(self.window, "_start_worker") as start_worker:
            self.window._queue_tick()

        start_worker.assert_called_once()
        self.assertIs(self.window.main_tabs.currentWidget(), self.window.history_tab)
        self.assertTrue(self.window._results_attention_pending)
        self.assertTrue(self.window.results_attention_timer.isActive())
        self.assertEqual(self.window.main_tabs.tabText(0), "SSH •")
        self.assertEqual(self.window.results_tabs.tabText(1), "Device Results •")

        self.window.main_tabs.setCurrentWidget(self.window.ssh_tab)

        self.assertTrue(self.window._results_attention_pending)
        self.assertEqual(self.window.results_tabs.currentIndex(), 0)

        self.window.results_tabs.setCurrentIndex(1)

        self.assertFalse(self.window._results_attention_pending)
        self.assertFalse(self.window.results_attention_timer.isActive())
        self.assertEqual(self.window.main_tabs.tabText(0), "SSH")
        self.assertEqual(self.window.results_tabs.tabText(1), "Device Results")

    def test_results_attention_pulses_three_times_then_leaves_dots(self):
        self.window._request_results_attention()
        timer = self.window.results_attention_timer
        highlight = self.app.palette().color(QPalette.ColorRole.Highlight)

        self.assertEqual(timer.interval(), 500)
        self.assertEqual(gui.RESULTS_ATTENTION_TICKS, 6)
        self.assertEqual(
            self.window.main_tabs.tabBar().tabTextColor(0), highlight
        )
        self.assertEqual(
            self.window.results_tabs.tabBar().tabTextColor(1), highlight
        )

        self.window._advance_results_attention_pulse()
        self.window._request_results_attention()

        self.assertIs(self.window.results_attention_timer, timer)
        self.assertEqual(self.window._results_attention_tick, 0)
        for _ in range(gui.RESULTS_ATTENTION_TICKS):
            self.window._advance_results_attention_pulse()

        self.assertFalse(timer.isActive())
        self.assertTrue(self.window._results_attention_pending)
        self.assertEqual(self.window.main_tabs.tabText(0), "SSH •")
        self.assertEqual(self.window.results_tabs.tabText(1), "Device Results •")
        self.assertFalse(
            self.window.main_tabs.tabBar().tabTextColor(0).isValid()
        )
        self.assertFalse(
            self.window.results_tabs.tabBar().tabTextColor(1).isValid()
        )

    def test_visible_results_do_not_request_attention(self):
        self.window.results_tabs.setCurrentIndex(1)

        self.window._request_results_attention()

        self.assertFalse(self.window._results_attention_pending)
        self.assertFalse(self.window.results_attention_timer.isActive())
        self.assertEqual(self.window.main_tabs.tabText(0), "SSH")
        self.assertEqual(self.window.results_tabs.tabText(1), "Device Results")

    def test_theme_change_updates_active_results_attention_color(self):
        self.window._request_results_attention()

        self.window.theme_dark_action.trigger()
        self.app.processEvents()

        highlight = QColor("#0a84ff")
        self.assertEqual(
            self.window.main_tabs.tabBar().tabTextColor(0), highlight
        )
        self.assertEqual(
            self.window.results_tabs.tabBar().tabTextColor(1), highlight
        )

    def test_close_stops_results_attention_timer(self):
        self.window._request_results_attention()
        self.assertTrue(self.window.results_attention_timer.isActive())

        self.window.close()
        self.app.processEvents()

        self.assertFalse(self.window.results_attention_timer.isActive())

    def test_future_schedule_arms_idle_sleep_prevention(self):
        prepared = runner.prepare_run(
            self._valid_request("huawei", "192.0.2.1"),
            require_credentials=True,
            environment={"SSH_USERNAME": "admin", "SSH_PASSWORD": "secret"},
        )
        scheduled = datetime.now(timezone.utc) + timedelta(hours=1)
        self.window.queue_controller.add(
            batch_queue.BatchPayload.from_prepared(prepared), prepared, scheduled
        )
        self.window.queue_controller.paused = False

        with patch.object(self.window.sleep_inhibitor, "set_required") as set_required:
            self.window._queue_tick()

        set_required.assert_called_with(True)

    def test_queued_completion_updates_status_and_deletes_secret(self):
        prepared = runner.prepare_run(
            self._valid_request("huawei", "192.0.2.1"),
            require_credentials=True,
            environment={"SSH_USERNAME": "admin", "SSH_PASSWORD": "secret"},
        )
        job = self.window.queue_controller.add(
            batch_queue.BatchPayload.from_prepared(prepared), prepared
        )
        self.window.queue_controller.mark_running(job.job_id)
        self.window._active_job_id = job.job_id
        self.window._operation = "run"
        run_directory = self.base / "outputs" / "run_test"
        run_directory.mkdir(parents=True)
        summary = run_directory / "summary.csv"
        summary.write_text("device,status\n", encoding="utf-8")

        self.window._run_completed(runner.BatchResult((), run_directory, summary))

        self.assertEqual(
            self.window.queue_controller.job(job.job_id).status, "succeeded"
        )
        self.assertNotIn(job.job_id, self.secret_store.values)
        self.assertEqual(self.window._job_results[job.job_id].summary_path, summary)

    def test_queued_worker_failure_does_not_open_blocking_dialog(self):
        prepared = runner.prepare_run(
            self._valid_request("huawei", "192.0.2.1"),
            require_credentials=True,
            environment={"SSH_USERNAME": "admin", "SSH_PASSWORD": "secret"},
        )
        job = self.window.queue_controller.add(
            batch_queue.BatchPayload.from_prepared(prepared), prepared
        )
        self.window.queue_controller.mark_running(job.job_id)
        self.window._active_job_id = job.job_id
        self.window._operation = "run"

        with patch.object(gui.QMessageBox, "critical") as critical:
            self.window._run_failed("Unexpected worker failure")

        critical.assert_not_called()
        self.assertEqual(self.window.queue_controller.job(job.job_id).status, "failed")

    def test_configuration_remains_editable_while_live_worker_runs(self):
        self.populate_valid_inputs()
        self.assertTrue(self.window.validate_inputs(show_success=False))

        self.window._set_running_ui(True)

        self.assertTrue(self.window.configuration_group.isEnabled())
        self.assertTrue(self.window.validate_button.isEnabled())
        self.assertTrue(self.window.run_button.isEnabled())

    def test_run_live_queues_behind_an_active_batch_without_starting_twice(self):
        active_prepared = runner.prepare_run(
            self._valid_request("huawei", "192.0.2.1"),
            require_credentials=True,
            environment={"SSH_USERNAME": "admin", "SSH_PASSWORD": "secret"},
        )
        active = self.window.queue_controller.add(
            batch_queue.BatchPayload.from_prepared(active_prepared), active_prepared
        )
        self.window.queue_controller.mark_running(active.job_id)
        self.window._running = True
        self.window._active_job_id = active.job_id
        self.populate_valid_inputs()

        with (
            patch.object(
                gui.QMessageBox,
                "warning",
                return_value=QMessageBox.StandardButton.Apply,
            ),
            patch.object(self.window, "_start_worker") as start_worker,
        ):
            self.window.start_run()
            self.window._queue_tick()

        self.assertEqual(len(self.window.queue_controller.jobs), 2)
        self.assertEqual(self.window.queue_controller.jobs[0].status, "running")
        self.assertEqual(self.window.queue_controller.jobs[1].status, "ready")
        start_worker.assert_not_called()
        self.window._running = False
        self.window._active_job_id = None
        self.window.queue_controller.active_job_id = None

    def test_cancelled_queue_entry_can_be_removed(self):
        prepared = runner.prepare_run(
            self._valid_request("huawei", "192.0.2.1"),
            require_credentials=True,
            environment={"SSH_USERNAME": "admin", "SSH_PASSWORD": "secret"},
        )
        job = self.window.queue_controller.add(
            batch_queue.BatchPayload.from_prepared(prepared), prepared
        )
        self.window.queue_controller.mark_running(job.job_id)
        self.window.queue_controller.mark_terminal(
            job.job_id, "cancelled", "0 succeeded, 0 failed, 1 cancelled"
        )
        self.window._refresh_queue_table(select_job_id=job.job_id)

        self.assertTrue(self.window.remove_batch_button.isEnabled())
        with patch.object(
            gui.QMessageBox,
            "question",
            return_value=QMessageBox.StandardButton.Yes,
        ):
            self.window.remove_selected_batch()

        self.assertIsNone(self.window.queue_controller.job(job.job_id))
        self.assertEqual(self.window.queue_table.rowCount(), 0)

    def test_clear_completed_button_leaves_future_schedule_untouched(self):
        prepared = runner.prepare_run(
            self._valid_request("huawei", "192.0.2.1"),
            require_credentials=True,
            environment={"SSH_USERNAME": "admin", "SSH_PASSWORD": "secret"},
        )
        completed = self.window.queue_controller.add(
            batch_queue.BatchPayload.from_prepared(prepared), prepared
        )
        scheduled = self.window.queue_controller.add(
            batch_queue.BatchPayload.from_prepared(prepared),
            prepared,
            datetime.now(timezone.utc) + timedelta(hours=1),
        )
        self.window.queue_controller.mark_running(completed.job_id)
        self.window.queue_controller.mark_terminal(
            completed.job_id, "succeeded", "1 succeeded"
        )
        self.window._refresh_queue_table(select_job_id=completed.job_id)

        self.assertTrue(self.window.clear_completed_button.isEnabled())
        with patch.object(
            gui.QMessageBox,
            "question",
            return_value=QMessageBox.StandardButton.Yes,
        ):
            self.window.clear_completed_button.click()

        self.assertIsNone(self.window.queue_controller.job(completed.job_id))
        self.assertIsNotNone(self.window.queue_controller.job(scheduled.job_id))
        self.assertEqual(
            self.window.queue_controller.job(scheduled.job_id).status, "scheduled"
        )
        self.assertEqual(self.window.queue_table.rowCount(), 1)

    def test_clear_completed_queue_entries_does_not_remove_audit_history(self):
        prepared = runner.prepare_run(
            self._valid_request("huawei", "192.0.2.1"),
            require_credentials=True,
            environment={"SSH_USERNAME": "admin", "SSH_PASSWORD": "secret"},
        )
        job = self.window.queue_controller.add(
            batch_queue.BatchPayload.from_prepared(prepared), prepared
        )
        self.window.queue_controller.mark_running(job.job_id)
        self.window.queue_controller.mark_terminal(job.job_id, "succeeded", "complete")
        self.add_history_record(job.job_id)
        self.window._refresh_queue_table(select_job_id=job.job_id)

        with patch.object(
            gui.QMessageBox,
            "question",
            return_value=QMessageBox.StandardButton.Yes,
        ):
            self.window.clear_completed_batches()

        self.assertIsNone(self.window.queue_controller.job(job.job_id))
        self.assertIsNotNone(self.history_store.get(job.job_id))

    def test_history_device_results_load_from_summary_on_demand(self):
        run_directory = self.base / "outputs" / "run_results"
        run_directory.mkdir(parents=True)
        transcript = run_directory / "192.0.2.5.txt"
        transcript.write_text("device output", encoding="utf-8")
        summary = run_directory / "summary.csv"
        device = runner.DeviceResult(
            "192.0.2.5",
            "failed",
            "",
            failed_section="config",
            failed_line="2",
            transcript_file=str(transcript),
            error="configuration rejected",
        )
        runner.write_summary(summary, [device])
        self.add_history_record(
            "results",
            "completed_with_failures",
            failures=1,
            run_directory=str(run_directory),
            summary_path=str(summary),
        )
        self.window._ensure_history_ui()
        self.window.history_table.selectRow(0)
        self.window.main_tabs.setCurrentWidget(self.window.history_tab)
        self.window._request_results_attention()

        self.window.view_history_results()

        self.assertIs(self.window.main_tabs.currentWidget(), self.window.ssh_tab)
        self.assertEqual(self.window.results_tabs.currentIndex(), 1)
        self.assertFalse(self.window._results_attention_pending)
        self.assertEqual(self.window.device_table.rowCount(), 1)
        self.assertEqual(self.window.device_table.item(0, 0).text(), "192.0.2.5")
        self.assertEqual(self.window.device_table.item(0, 2).text(), "config / 2")
        self.assertEqual(
            self.window.device_table.item(0, 3).text(), "configuration rejected"
        )
        self.assertEqual(self.window.device_table.item(0, 4).text(), str(transcript))

    def test_missing_history_summary_is_shown_as_output_unavailable(self):
        self.add_history_record(
            "missing-output",
            run_directory=str(self.base / "gone"),
            summary_path=str(self.base / "gone" / "summary.csv"),
        )
        self.window._ensure_history_ui()

        self.assertIn("Output unavailable", self.window.history_table.item(0, 8).text())
        self.window.history_table.selectRow(0)
        self.assertFalse(self.window.history_results_button.isEnabled())
        self.assertFalse(self.window.history_output_button.isEnabled())

    def test_view_history_commands_reads_encrypted_snapshot_after_warning(self):
        record = self.add_history_record("commands")
        snapshot = history.HistoryCommandSnapshot(
            (
                runner.Command("exec", "show clock", 1),
                runner.Command("config", "hostname access-1", 2),
            )
        )
        self.history_commands.set(record.job_id, snapshot)
        self.window._ensure_history_ui()
        self.window.history_table.selectRow(0)

        with (
            patch.object(
                gui.QMessageBox,
                "warning",
                return_value=QMessageBox.StandardButton.Open,
            ),
            patch.object(self.history_commands, "get", wraps=self.history_commands.get) as get,
            patch.object(gui.QDialog, "exec", return_value=QDialog.DialogCode.Rejected),
        ):
            self.window.view_history_commands()

        get.assert_called_once_with(record.job_id)

    def test_remove_history_preserves_output_and_deletes_command_snapshot(self):
        run_directory = self.base / "outputs" / "run_preserved"
        run_directory.mkdir(parents=True)
        summary = run_directory / "summary.csv"
        runner.write_summary(summary, [])
        record = self.add_history_record(
            "remove",
            run_directory=str(run_directory),
            summary_path=str(summary),
        )
        self.history_commands.set(record.job_id, history.HistoryCommandSnapshot(()))
        self.window._ensure_history_ui()
        self.window.history_table.selectRow(0)

        with patch.object(
            gui.QMessageBox,
            "question",
            return_value=QMessageBox.StandardButton.Yes,
        ):
            self.window.remove_history_record()

        self.assertIsNone(self.history_store.get(record.job_id))
        self.assertTrue(run_directory.is_dir())
        with self.assertRaises(history.HistoryError):
            self.history_commands.get(record.job_id)

    def test_delete_history_outputs_requires_marker_and_deletes_exact_run(self):
        run_directory = self.base / "outputs" / "run_delete"
        run_directory.mkdir(parents=True)
        summary = run_directory / "summary.csv"
        runner.write_summary(summary, [])
        record = self.add_history_record(
            "delete",
            run_directory=str(run_directory),
            summary_path=str(summary),
        )
        history.write_output_marker(record)
        self.history_commands.set(record.job_id, history.HistoryCommandSnapshot(()))
        self.window._ensure_history_ui()
        self.window.history_table.selectRow(0)
        self.assertTrue(self.window.history_delete_outputs_button.isEnabled())

        with patch.object(
            gui.QMessageBox,
            "warning",
            return_value=QMessageBox.StandardButton.Yes,
        ):
            self.window.delete_history_outputs()

        self.assertFalse(run_directory.exists())
        self.assertIsNone(self.history_store.get(record.job_id))

    def _valid_request(self, device_type, address):
        return runner.RunRequest(
            inventory=None,
            command_file=None,
            device_type=device_type,
            output_dir=self.base / "outputs",
            env_file=None,
            apply=True,
            inventory_text=address,
            exec_commands_text="show version",
            config_commands_text="interface loopback 1",
        )

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

        reopened = gui.MainWindow(
            self.settings,
            secret_store=batch_queue.MemorySecretStore(),
            history_store=self.history_store,
            history_command_store=self.history_commands,
        )
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

        reopened = gui.MainWindow(
            self.settings,
            secret_store=batch_queue.MemorySecretStore(),
            history_store=self.history_store,
            history_command_store=self.history_commands,
        )
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
        self.assertTrue(self.window.clear_commands_button.isEnabled())

    def test_importing_devices_enables_clear_devices(self):
        device_file = self.base / "devices.txt"
        device_file.write_text("192.0.2.1\n192.0.2.2\n", encoding="utf-8")

        with patch.object(
            gui.QFileDialog,
            "getOpenFileName",
            return_value=(str(device_file), "Text files (*.txt)"),
        ):
            self.window._import_inventory()

        self.assertEqual(
            self.window.inventory_text_edit.toPlainText(),
            "192.0.2.1\n192.0.2.2\n",
        )
        self.assertTrue(self.window.clear_devices_button.isEnabled())

    def test_exporting_devices_writes_the_devices_text(self):
        export_file = self.base / "exported-devices.txt"
        self.window.inventory_text_edit.setPlainText(
            "# Core switches\n192.0.2.1\n192.0.2.2"
        )

        with patch.object(
            gui.QFileDialog,
            "getSaveFileName",
            return_value=(str(export_file), "Text files (*.txt)"),
        ):
            self.window._export_inventory()

        self.assertEqual(
            export_file.read_text(encoding="utf-8"),
            "# Core switches\n192.0.2.1\n192.0.2.2",
        )
        self.assertEqual(self.window._last_inventory_import, str(export_file))

    def test_exporting_commands_writes_both_sections(self):
        export_file = self.base / "exported-commands.txt"
        self.window.exec_commands_edit.setPlainText("show version\nshow clock")
        self.window.config_commands_edit.setPlainText("interface loopback 1\ndescription Test")

        with patch.object(
            gui.QFileDialog,
            "getSaveFileName",
            return_value=(str(export_file), "Text files (*.txt)"),
        ):
            self.window._export_sectioned_commands()

        self.assertEqual(
            export_file.read_text(encoding="utf-8"),
            "[exec]\nshow version\nshow clock\n\n"
            "[config]\ninterface loopback 1\ndescription Test\n",
        )
        commands = runner.read_commands(export_file)
        self.assertEqual(
            [(command.section, command.text) for command in commands],
            [
                ("exec", "show version"),
                ("exec", "show clock"),
                ("config", "interface loopback 1"),
                ("config", "description Test"),
            ],
        )
        self.assertEqual(self.window._last_command_import, str(export_file))

    def test_empty_commands_are_not_exported(self):
        with (
            patch.object(gui.QFileDialog, "getSaveFileName") as save_dialog,
            patch.object(gui.QMessageBox, "critical") as error_dialog,
        ):
            self.window._export_sectioned_commands()

        save_dialog.assert_not_called()
        error_dialog.assert_called_once()

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

    def test_connection_test_does_not_require_command_text(self):
        self.window.inventory_text_edit.setPlainText("192.0.2.1")
        self.window.username_edit.setText("admin")
        self.window.password_edit.setText("secret")
        self.window.output_edit.setText(str(self.base / "outputs"))
        self.window.device_type_combo.setCurrentText("cisco_ios")

        with patch.object(self.window, "_start_worker") as start_worker:
            self.window.start_connection_test()

        start_worker.assert_called_once()
        prepared = start_worker.call_args.args[0]
        self.assertEqual(prepared.commands, ())
        self.assertEqual(start_worker.call_args.kwargs["operation"], "connection_test")
        self.assertEqual(self.window.device_table.rowCount(), 1)
        self.assertEqual(
            self.window.device_table.horizontalHeaderItem(2).text(),
            "Stage / Line",
        )

    def test_connection_progress_updates_stage_result_and_output(self):
        self.window.inventory_text_edit.setPlainText("192.0.2.1")
        self.window.username_edit.setText("admin")
        self.window.password_edit.setText("secret")
        self.window.output_edit.setText(str(self.base / "outputs"))
        request = self.window._request_from_inputs(include_commands=False)
        prepared = runner.prepare_connection_test(
            request,
            environment={"SSH_USERNAME": "admin", "SSH_PASSWORD": "secret"},
        )
        self.window._populate_devices(prepared, operation="connection_test")
        self.window._operation = "connection_test"
        diagnostic = self.base / "192.0.2.1.txt"
        diagnostic.write_text("connection test", encoding="utf-8")

        self.window.handle_progress(
            runner.ProgressEvent(
                "device_status",
                address="192.0.2.1",
                status="detecting prompt",
                section="Prompt detection",
            )
        )
        self.window.handle_progress(
            runner.ProgressEvent(
                "device_finished",
                address="192.0.2.1",
                index=1,
                total=1,
                status="success",
                section="Complete",
                message="SSH connection test passed",
                transcript_file=str(diagnostic),
            )
        )

        row = self.window._rows["192.0.2.1"]
        self.assertEqual(self.window.device_table.item(row, 1).text(), "Passed")
        self.assertEqual(self.window.device_table.item(row, 2).text(), "Complete")
        self.assertEqual(
            self.window.device_table.item(row, 3).text(),
            "SSH connection test passed",
        )
        self.assertEqual(self.window.device_table.item(row, 4).text(), str(diagnostic))

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
        self.assertTrue(self.window.queue_controller.paused)
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
            "Verify the IP address, port, and network reachability."
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
