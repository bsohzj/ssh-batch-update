#!/usr/bin/env python3
"""PySide6 desktop interface for the SSH batch command runner."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QObject, QSettings, QThread, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QCloseEvent, QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from run_commands import (
    BatchResult,
    CancellationToken,
    ConfigurationError,
    PreparedRun,
    ProgressEvent,
    RunRequest,
    load_environment,
    parse_commands,
    prepare_run,
    run_batch,
)


APP_NAME = "SSH Batch Update"
ORGANIZATION_NAME = "SG4291"


class BatchWorker(QObject):
    """Run a prepared batch off the GUI thread."""

    event = Signal(object)
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, prepared: PreparedRun, cancellation_token: CancellationToken) -> None:
        super().__init__()
        self.prepared = prepared
        self.cancellation_token = cancellation_token

    @Slot()
    def run(self) -> None:
        try:
            result = run_batch(
                self.prepared,
                progress_callback=self.event.emit,
                cancellation_token=self.cancellation_token,
            )
        except Exception as exc:  # The GUI boundary must surface all worker failures.
            self.failed.emit(f"{exc.__class__.__name__}: {exc}")
        else:
            self.completed.emit(result)


class MainWindow(QMainWindow):
    """Main application window."""

    def __init__(self, settings: Optional[QSettings] = None) -> None:
        super().__init__()
        self.settings = settings or QSettings(ORGANIZATION_NAME, APP_NAME)
        self._prepared: Optional[PreparedRun] = None
        self._batch_result: Optional[BatchResult] = None
        self._token: Optional[CancellationToken] = None
        self._thread: Optional[QThread] = None
        self._worker: Optional[BatchWorker] = None
        self._running = False
        self._loading_settings = True
        self._close_after_run = False
        self._rows: dict[str, int] = {}
        self._last_inventory_import = ""
        self._last_command_import = ""
        self._last_credential_import = ""
        self._credential_file: Optional[Path] = None

        self.setWindowTitle(APP_NAME)
        self.resize(1400, 850)
        self.setMinimumSize(1050, 680)
        self._build_ui()
        self._restore_settings()
        self._connect_input_changes()
        self._loading_settings = False
        self._set_validated(False)

    def _build_ui(self) -> None:
        central = QWidget(self)
        root = QVBoxLayout(central)
        self.main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.main_splitter.setChildrenCollapsible(False)

        settings_panel = QWidget()
        settings_layout = QVBoxLayout(settings_panel)
        settings_layout.setContentsMargins(0, 0, 0, 0)
        settings_panel.setMinimumWidth(400)

        results_panel = QWidget()
        results_layout = QVBoxLayout(results_panel)
        results_layout.setContentsMargins(0, 0, 0, 0)
        results_panel.setMinimumWidth(600)

        self.configuration_group = QGroupBox()
        form = QFormLayout(self.configuration_group)

        self.inventory_text_edit = QPlainTextEdit()
        self.inventory_text_edit.setPlaceholderText(
            "One IP address or hostname per line. Blank lines and # comments are ignored."
        )
        self.inventory_text_edit.setMaximumHeight(100)
        inventory_button = QPushButton("Import Devices")
        inventory_button.clicked.connect(self._import_inventory)
        inventory_container = QWidget()
        inventory_layout = QHBoxLayout(inventory_container)
        inventory_layout.setContentsMargins(0, 0, 0, 0)
        inventory_layout.addWidget(self.inventory_text_edit, 1)
        inventory_layout.addWidget(inventory_button, 0)
        form.addRow("Devices", inventory_container)

        self.exec_commands_edit = QPlainTextEdit()
        self.exec_commands_edit.setPlaceholderText("One exec command per line")
        self.exec_commands_edit.setMaximumHeight(120)
        self.config_commands_edit = QPlainTextEdit()
        self.config_commands_edit.setPlaceholderText("One configuration command per line")
        self.config_commands_edit.setMaximumHeight(120)
        import_sectioned_button = QPushButton("Import Existing Commands")
        import_sectioned_button.clicked.connect(self._import_sectioned_commands)

        form.addRow("Exec Commands", self.exec_commands_edit)
        form.addRow("Config Commands", self.config_commands_edit)
        form.addRow("", import_sectioned_button)

        self.username_edit = QLineEdit()
        self.username_edit.setPlaceholderText("SSH Username")
        self.password_edit = QLineEdit()
        self.password_edit.setPlaceholderText("SSH Password")
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        import_credentials_button = QPushButton("Import Credentials")
        import_credentials_button.clicked.connect(self._import_credentials)
        self.output_edit, output_row = self._path_row("Choose output folder…", self._choose_output)
        form.insertRow(0, "Username", self.username_edit)
        form.insertRow(1, "Password", self.password_edit)
        form.insertRow(2, "", import_credentials_button)
        form.addRow("Output Folder", output_row)

        self.device_type_combo = QComboBox()
        self.device_type_combo.setEditable(True)
        self.device_type_combo.addItems(["huawei", "cisco_ios"])
        form.insertRow(3, "Device Type", self.device_type_combo)

        settings_layout.addWidget(self.configuration_group)

        button_row = QHBoxLayout()
        self.validate_button = QPushButton("Validate Files")
        self.run_button = QPushButton("Run Live")
        self.cancel_button = QPushButton("Stop Run")
        self.cancel_button.setStyleSheet("QPushButton { color: #c62828; }")
        self.validate_button.clicked.connect(self.validate_inputs)
        self.run_button.clicked.connect(self.start_run)
        self.cancel_button.clicked.connect(self.cancel_run)
        button_row.addWidget(self.validate_button)
        button_row.addWidget(self.run_button)
        button_row.addWidget(self.cancel_button)
        button_row.addStretch()
        self.validation_label = QLabel("Enter or import devices and commands, then validate.")
        self.validation_label.setWordWrap(True)
        settings_layout.addLayout(button_row)
        settings_layout.addWidget(self.validation_label)
        settings_layout.addStretch(1)

        self.device_table = QTableWidget(0, 5)
        self.device_table.setHorizontalHeaderLabels(
            ["Device", "Status", "Section / Line", "Result", "Output"]
        )
        self.device_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.device_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.device_table.cellDoubleClicked.connect(self.handle_table_double_click)
        header = self.device_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        results_layout.addWidget(self.device_table, 1)

        progress_grid = QGridLayout()
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        self.status_label = QLabel("Idle")
        self.totals_label = QLabel("0 succeeded, 0 failed, 0 cancelled")
        progress_grid.addWidget(self.progress_bar, 0, 0, 1, 2)
        progress_grid.addWidget(self.status_label, 1, 0)
        progress_grid.addWidget(self.totals_label, 1, 1)
        results_layout.addLayout(progress_grid)

        result_buttons = QHBoxLayout()
        self.open_output_button = QPushButton("Open Output Folder")
        self.open_summary_button = QPushButton("Open Summary")
        self.open_output_button.clicked.connect(self.open_output_folder)
        self.open_summary_button.clicked.connect(self.open_summary)
        self.open_output_button.setEnabled(False)
        self.open_summary_button.setEnabled(False)
        result_buttons.addWidget(self.open_output_button)
        result_buttons.addWidget(self.open_summary_button)
        result_buttons.addStretch()
        results_layout.addLayout(result_buttons)

        self.main_splitter.addWidget(settings_panel)
        self.main_splitter.addWidget(results_panel)
        self.main_splitter.setStretchFactor(0, 0)
        self.main_splitter.setStretchFactor(1, 1)
        self.main_splitter.setSizes([480, 900])
        root.addWidget(self.main_splitter)

        self.setCentralWidget(central)

    def _path_row(self, placeholder: str, callback) -> tuple[QLineEdit, QWidget]:
        edit = QLineEdit()
        edit.setPlaceholderText(placeholder)
        button = QPushButton("Browse…")
        button.clicked.connect(callback)
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(edit, 1)
        layout.addWidget(button)
        return edit, container

    def _connect_input_changes(self) -> None:
        for edit in (self.username_edit, self.password_edit, self.output_edit):
            edit.textChanged.connect(self.invalidate_validation)
        self.inventory_text_edit.textChanged.connect(self.invalidate_validation)
        self.exec_commands_edit.textChanged.connect(self.invalidate_validation)
        self.config_commands_edit.textChanged.connect(self.invalidate_validation)
        self.device_type_combo.currentTextChanged.connect(self.invalidate_validation)

    def _restore_settings(self) -> None:
        default_output = Path.home() / "Documents" / APP_NAME / "outputs"
        self._last_inventory_import = self.settings.value("paths/inventory_import", "", str)
        self._last_command_import = self.settings.value("paths/command_import", "", str)
        self._last_credential_import = self.settings.value(
            "paths/credential_import", "", str
        )
        self.output_edit.setText(self.settings.value("paths/output", str(default_output), str))
        self.device_type_combo.setCurrentText(
            self.settings.value("run/device_type", "huawei", str)
        )
        splitter_state = self.settings.value("window/main_splitter")
        if splitter_state is not None:
            self.main_splitter.restoreState(splitter_state)

    def _save_settings(self) -> None:
        self.settings.remove("paths/inventory")
        self.settings.remove("paths/commands")
        self.settings.setValue("paths/inventory_import", self._last_inventory_import)
        self.settings.setValue("paths/command_import", self._last_command_import)
        self.settings.remove("paths/env")
        self.settings.setValue("paths/credential_import", self._last_credential_import)
        self.settings.setValue("paths/output", self.output_edit.text().strip())
        self.settings.setValue("run/device_type", self.device_type_combo.currentText().strip())
        self.settings.setValue("window/main_splitter", self.main_splitter.saveState())
        self.settings.remove("run/failure_patterns")
        self.settings.sync()

    @Slot()
    def invalidate_validation(self) -> None:
        if self._loading_settings or self._running:
            return
        self._prepared = None
        self._set_validated(False)
        self.validation_label.setText("Inputs changed; validate again.")

    def _set_validated(self, validated: bool) -> None:
        self.run_button.setEnabled(validated and not self._running)
        self.cancel_button.setEnabled(self._running)

    def _request_from_inputs(self) -> RunRequest:
        required = {
            "username": self.username_edit.text().strip(),
            "password": self.password_edit.text(),
            "output folder": self.output_edit.text().strip(),
            "device type": self.device_type_combo.currentText().strip(),
        }
        missing = [label for label, value in required.items() if not value]
        if missing:
            raise ConfigurationError(f"Missing required selection: {', '.join(missing)}")
        return RunRequest(
            inventory=None,
            command_file=None,
            device_type=required["device type"],
            output_dir=Path(required["output folder"]).expanduser(),
            env_file=self._credential_file,
            failure_patterns=(),
            apply=True,
            inventory_text=self.inventory_text_edit.toPlainText(),
            exec_commands_text=self.exec_commands_edit.toPlainText(),
            config_commands_text=self.config_commands_edit.toPlainText(),
        )

    @Slot()
    def validate_inputs(self, show_success: bool = True) -> bool:
        try:
            request = self._request_from_inputs()
            prepared = prepare_run(
                request,
                require_credentials=True,
                environment={
                    "SSH_USERNAME": self.username_edit.text(),
                    "SSH_PASSWORD": self.password_edit.text(),
                },
            )
        except (ConfigurationError, OSError) as exc:
            self._prepared = None
            self._set_validated(False)
            self.validation_label.setText("Validation failed")
            if show_success:
                QMessageBox.critical(self, "Validation failed", str(exc))
            return False

        self._prepared = prepared
        self._populate_devices(prepared)
        self._save_settings()
        valid = len(prepared.valid_entries)
        invalid = len(prepared.invalid_entries)
        self.validation_label.setText(
            f"Validated: {valid} valid, {invalid} invalid, {len(prepared.commands)} commands"
        )
        self._set_validated(valid > 0)
        if show_success:
            QMessageBox.information(
                self,
                "Validation complete",
                f"{valid} valid device(s)\n"
                f"{invalid} invalid device entry/entries\n"
                f"{len(prepared.commands)} command(s)\n\nNo SSH sessions were opened.",
            )
        return valid > 0

    def _populate_devices(self, prepared: PreparedRun) -> None:
        self.device_table.setRowCount(0)
        self._rows.clear()
        for row, entry in enumerate(prepared.entries):
            self.device_table.insertRow(row)
            self._rows[entry.address] = row
            self._set_cell(row, 0, entry.address)
            self._set_cell(row, 1, "Invalid" if entry.error else "Ready")
            self._set_cell(row, 2, "")
            self._set_cell(row, 3, entry.error)
            self._set_cell(row, 4, "")
        self.progress_bar.setRange(0, max(1, len(prepared.entries)))
        self.progress_bar.setValue(0)
        self.status_label.setText("Validated")
        self.totals_label.setText("0 succeeded, 0 failed, 0 cancelled")
        self.open_output_button.setEnabled(False)
        self.open_summary_button.setEnabled(False)

    def _set_cell(self, row: int, column: int, value: str) -> None:
        item = self.device_table.item(row, column)
        if item is None:
            item = QTableWidgetItem()
            self.device_table.setItem(row, column, item)
        item.setText(value)
        if column == 3:
            item.setToolTip("Double-click to view the full result" if value else "")
        elif column == 4:
            item.setToolTip("Double-click to open this output file" if value else "")

    @Slot(int, int)
    def handle_table_double_click(self, row: int, column: int) -> None:
        if column == 3:
            self.show_result_details(row)
        elif column == 4:
            self.open_transcript_at(row, column)

    def show_result_details(self, row: int) -> None:
        result_item = self.device_table.item(row, 3)
        if result_item is None or not result_item.text().strip():
            return

        device_item = self.device_table.item(row, 0)
        status_item = self.device_table.item(row, 1)
        device = device_item.text() if device_item else "Device"
        status = status_item.text() if status_item else "Result"

        dialog = QDialog(self)
        dialog.setWindowTitle(f"Device Result — {device}")
        dialog.resize(720, 360)
        layout = QVBoxLayout(dialog)
        heading = QLabel(f"{device} — {status}")
        layout.addWidget(heading)
        details = QPlainTextEdit()
        details.setReadOnly(True)
        details.setPlainText(result_item.text())
        layout.addWidget(details, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()

    @Slot(int, int)
    def open_transcript_at(self, row: int, column: int) -> None:
        if column != 4:
            return
        item = self.device_table.item(row, column)
        if item is None or not item.text().strip():
            return
        transcript = Path(item.text().strip()).expanduser()
        if transcript.is_file():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(transcript)))

    @Slot()
    def start_run(self) -> None:
        if self._running:
            return
        if not self.validate_inputs(show_success=False) or self._prepared is None:
            QMessageBox.critical(self, "Cannot run", "Correct the validation errors first.")
            return
        prepared = self._prepared
        answer = QMessageBox.warning(
            self,
            "Confirm live run",
            f"Run {len(prepared.commands)} command(s) on "
            f"{len(prepared.valid_entries)} valid device(s)?\n\n"
            "This makes live changes and does not automatically roll them back.\n"
            "Literal passwords in the command boxes may appear in outputs or summary.csv.",
            QMessageBox.StandardButton.Apply | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Apply:
            return

        self._running = True
        self._batch_result = None
        self._token = CancellationToken()
        self._thread = QThread(self)
        self._worker = BatchWorker(prepared, self._token)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.event.connect(self.handle_progress)
        self._worker.completed.connect(self._run_completed)
        self._worker.failed.connect(self._run_failed)
        self._worker.completed.connect(self._thread.quit)
        self._worker.failed.connect(self._thread.quit)
        self._thread.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread_finished)
        self._set_running_ui(True)
        self._thread.start()

    def _set_running_ui(self, running: bool) -> None:
        self.validate_button.setEnabled(not running)
        self.run_button.setEnabled(not running and self._prepared is not None)
        self.cancel_button.setEnabled(running)
        self.configuration_group.setEnabled(not running)

    @Slot(object)
    def handle_progress(self, event: ProgressEvent) -> None:
        if event.kind == "run_started":
            self.progress_bar.setRange(0, max(1, event.total))
            self.progress_bar.setValue(0)
            self.status_label.setText("Running")
            return
        if event.kind == "run_finished":
            self.status_label.setText(event.message)
            return
        row = self._rows.get(event.address)
        if row is None:
            return
        if event.kind == "device_started":
            self._set_cell(row, 1, "Starting")
        elif event.kind == "device_status":
            self._set_cell(row, 1, event.status.title())
        elif event.kind == "command_started":
            self._set_cell(row, 1, "Running")
            self._set_cell(row, 2, f"{event.section} / {event.line_number}")
        elif event.kind == "device_finished":
            self._set_cell(row, 1, event.status.title())
            if event.section or event.line_number:
                self._set_cell(row, 2, f"{event.section} / {event.line_number}")
            self._set_cell(row, 3, event.message or event.status.title())
            self._set_cell(row, 4, event.transcript_file)
            self.progress_bar.setValue(event.index)

    @Slot()
    def cancel_run(self) -> None:
        if not self._running or self._token is None:
            return
        self._token.cancel()
        self.cancel_button.setEnabled(False)
        self.status_label.setText("Cancelling at the next safe checkpoint…")

    @Slot(object)
    def _run_completed(self, result: BatchResult) -> None:
        self._batch_result = result
        self.totals_label.setText(
            f"{result.successes} succeeded, {result.failures} failed, "
            f"{result.cancelled} cancelled"
        )
        self.status_label.setText("Run complete" if not result.cancelled else "Run cancelled")
        self.open_output_button.setEnabled(result.run_directory.is_dir())
        self.open_summary_button.setEnabled(result.summary_path.is_file())

    @Slot(str)
    def _run_failed(self, message: str) -> None:
        self.status_label.setText("Run failed")
        QMessageBox.critical(self, "Run failed", message)

    @Slot()
    def _thread_finished(self) -> None:
        self._running = False
        self._set_running_ui(False)
        self._token = None
        self._worker = None
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.deleteLater()
        if self._close_after_run:
            self._close_after_run = False
            QTimer.singleShot(0, self.close)

    @Slot()
    def open_output_folder(self) -> None:
        if self._batch_result and self._batch_result.run_directory.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._batch_result.run_directory)))

    @Slot()
    def open_summary(self) -> None:
        if self._batch_result and self._batch_result.summary_path.is_file():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._batch_result.summary_path)))

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._running:
            answer = QMessageBox.question(
                self,
                "Cancel active run?",
                "The app will stop at the next safe checkpoint and then close.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer == QMessageBox.StandardButton.Yes:
                self._close_after_run = True
                self.cancel_run()
            event.ignore()
            return
        self._save_settings()
        event.accept()

    @Slot()
    def _import_inventory(self) -> None:
        path = self._select_import_file("Import devices", self._last_inventory_import)
        if path is None:
            return
        text = self._read_import_file(path)
        if text is not None:
            self.inventory_text_edit.setPlainText(text)
            self._last_inventory_import = str(path)

    @Slot()
    def _import_sectioned_commands(self) -> None:
        path = self._select_import_file(
            "Import existing [exec]/[config] command file",
            self._last_command_import,
        )
        if path is None:
            return
        text = self._read_import_file(path)
        if text is None:
            return
        try:
            commands = parse_commands(text)
        except ConfigurationError as exc:
            QMessageBox.critical(self, "Command import failed", str(exc))
            return
        self.exec_commands_edit.setPlainText(
            "\n".join(command.text for command in commands if command.section == "exec")
        )
        self.config_commands_edit.setPlainText(
            "\n".join(command.text for command in commands if command.section == "config")
        )
        self._last_command_import = str(path)

    @Slot()
    def _import_credentials(self) -> None:
        start = (
            str(Path(self._last_credential_import).parent)
            if self._last_credential_import
            else str(Path.home())
        )
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "Import credentials",
            start,
            "Environment files (*.env);;All files (*)",
        )
        if not selected:
            return
        path = Path(selected)
        try:
            values = load_environment(path)
        except (OSError, UnicodeError) as exc:
            QMessageBox.critical(
                self,
                "Credential import failed",
                f"Could not read {path}:\n{exc}",
            )
            return
        username = values.get("SSH_USERNAME", "").strip()
        password = values.get("SSH_PASSWORD", "")
        missing = [
            label
            for label, value in (("SSH_USERNAME", username), ("SSH_PASSWORD", password))
            if not value
        ]
        if missing:
            QMessageBox.critical(
                self,
                "Credential import failed",
                f"Missing required value(s): {', '.join(missing)}",
            )
            return
        self._credential_file = path
        self._last_credential_import = str(path)
        self.username_edit.setText(username)
        self.password_edit.setText(password)

    def _select_import_file(self, title: str, previous: str) -> Optional[Path]:
        start = str(Path(previous).parent) if previous else str(Path.home())
        selected, _ = QFileDialog.getOpenFileName(
            self,
            title,
            start,
            "Text files (*.txt);;All files (*)",
        )
        return Path(selected) if selected else None

    def _read_import_file(self, path: Path) -> Optional[str]:
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            QMessageBox.critical(self, "Import failed", f"Could not read {path}:\n{exc}")
            return None

    @Slot()
    def _choose_output(self) -> None:
        current = self.output_edit.text().strip() or str(Path.home())
        path = QFileDialog.getExistingDirectory(self, "Choose output folder", current)
        if path:
            self.output_edit.setText(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SSH Batch Update desktop application")
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="construct the interface and exit without opening SSH sessions",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    app = QApplication([sys.argv[0]])
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(ORGANIZATION_NAME)
    window = MainWindow()
    if args.smoke_test:
        import netmiko  # noqa: F401 - verifies the packaged SSH dependency is present.

        window.show()
        app.processEvents()
        window.close()
        app.processEvents()
        return 0
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
