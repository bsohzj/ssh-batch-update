#!/usr/bin/env python3
"""PySide6 desktop interface for the SSH batch command runner."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QSettings, QThread, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QCloseEvent, QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
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

        self.setWindowTitle(APP_NAME)
        self.resize(1050, 720)
        self.setMinimumSize(820, 560)
        self._build_ui()
        self._restore_settings()
        self._connect_input_changes()
        self._loading_settings = False
        self._set_validated(False)

    def _build_ui(self) -> None:
        central = QWidget(self)
        root = QVBoxLayout(central)

        self.configuration_group = QGroupBox("Run configuration")
        form = QFormLayout(self.configuration_group)
        self.inventory_edit, inventory_row = self._path_row("Choose inventory…", self._choose_inventory)
        self.command_edit, command_row = self._path_row("Choose commands…", self._choose_commands)
        self.env_edit, env_row = self._path_row("Choose .env…", self._choose_env)
        self.output_edit, output_row = self._path_row("Choose output folder…", self._choose_output)
        form.addRow("Inventory", inventory_row)
        form.addRow("Commands", command_row)
        form.addRow("Credentials", env_row)
        form.addRow("Output folder", output_row)

        self.device_type_combo = QComboBox()
        self.device_type_combo.setEditable(True)
        self.device_type_combo.addItems(["huawei", "cisco_ios"])
        form.addRow("Device type", self.device_type_combo)

        self.failure_patterns_edit = QPlainTextEdit()
        self.failure_patterns_edit.setPlaceholderText("Optional: one additional failure regex per line")
        self.failure_patterns_edit.setMaximumHeight(76)
        form.addRow("Failure patterns", self.failure_patterns_edit)
        root.addWidget(self.configuration_group)

        button_row = QHBoxLayout()
        self.validate_button = QPushButton("Validate Files")
        self.run_button = QPushButton("Run Live")
        self.cancel_button = QPushButton("Cancel")
        self.validate_button.clicked.connect(self.validate_inputs)
        self.run_button.clicked.connect(self.start_run)
        self.cancel_button.clicked.connect(self.cancel_run)
        button_row.addWidget(self.validate_button)
        button_row.addWidget(self.run_button)
        button_row.addWidget(self.cancel_button)
        button_row.addStretch()
        self.validation_label = QLabel("Select the required files, then validate.")
        button_row.addWidget(self.validation_label)
        root.addLayout(button_row)

        self.device_table = QTableWidget(0, 5)
        self.device_table.setHorizontalHeaderLabels(
            ["Device", "Status", "Section / Line", "Result", "Transcript"]
        )
        self.device_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.device_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        header = self.device_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        root.addWidget(self.device_table, 1)

        progress_grid = QGridLayout()
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        self.status_label = QLabel("Idle")
        self.totals_label = QLabel("0 succeeded, 0 failed, 0 cancelled")
        progress_grid.addWidget(self.progress_bar, 0, 0, 1, 2)
        progress_grid.addWidget(self.status_label, 1, 0)
        progress_grid.addWidget(self.totals_label, 1, 1)
        root.addLayout(progress_grid)

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
        root.addLayout(result_buttons)

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
        for edit in (self.inventory_edit, self.command_edit, self.env_edit, self.output_edit):
            edit.textChanged.connect(self.invalidate_validation)
        self.device_type_combo.currentTextChanged.connect(self.invalidate_validation)
        self.failure_patterns_edit.textChanged.connect(self.invalidate_validation)

    def _restore_settings(self) -> None:
        default_output = Path.home() / "Documents" / APP_NAME / "outputs"
        self.inventory_edit.setText(self.settings.value("paths/inventory", "", str))
        self.command_edit.setText(self.settings.value("paths/commands", "", str))
        self.env_edit.setText(self.settings.value("paths/env", "", str))
        self.output_edit.setText(self.settings.value("paths/output", str(default_output), str))
        self.device_type_combo.setCurrentText(
            self.settings.value("run/device_type", "huawei", str)
        )
        self.failure_patterns_edit.setPlainText(
            self.settings.value("run/failure_patterns", "", str)
        )

    def _save_settings(self) -> None:
        self.settings.setValue("paths/inventory", self.inventory_edit.text().strip())
        self.settings.setValue("paths/commands", self.command_edit.text().strip())
        self.settings.setValue("paths/env", self.env_edit.text().strip())
        self.settings.setValue("paths/output", self.output_edit.text().strip())
        self.settings.setValue("run/device_type", self.device_type_combo.currentText().strip())
        self.settings.setValue("run/failure_patterns", self.failure_patterns_edit.toPlainText())
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
            "inventory file": self.inventory_edit.text().strip(),
            "command file": self.command_edit.text().strip(),
            ".env file": self.env_edit.text().strip(),
            "output folder": self.output_edit.text().strip(),
            "device type": self.device_type_combo.currentText().strip(),
        }
        missing = [label for label, value in required.items() if not value]
        if missing:
            raise ConfigurationError(f"Missing required selection: {', '.join(missing)}")
        for label in ("inventory file", "command file", ".env file"):
            if not Path(required[label]).expanduser().is_file():
                raise ConfigurationError(f"{label} does not exist: {required[label]}")
        patterns = tuple(
            line.strip()
            for line in self.failure_patterns_edit.toPlainText().splitlines()
            if line.strip()
        )
        return RunRequest(
            inventory=Path(required["inventory file"]).expanduser(),
            command_file=Path(required["command file"]).expanduser(),
            device_type=required["device type"],
            output_dir=Path(required["output folder"]).expanduser(),
            env_file=Path(required[".env file"]).expanduser(),
            failure_patterns=patterns,
            apply=True,
        )

    @Slot()
    def validate_inputs(self, show_success: bool = True) -> bool:
        try:
            request = self._request_from_inputs()
            prepared = prepare_run(request, require_credentials=True, environment={})
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
            "Literal passwords in the command file may appear in transcripts or summary.csv.",
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
    def _choose_inventory(self) -> None:
        self._choose_file(self.inventory_edit, "Choose inventory file", "Text files (*.txt);;All files (*)")

    @Slot()
    def _choose_commands(self) -> None:
        self._choose_file(self.command_edit, "Choose command file", "Text files (*.txt);;All files (*)")

    @Slot()
    def _choose_env(self) -> None:
        self._choose_file(self.env_edit, "Choose credentials file", "Environment files (*.env);;All files (*)")

    def _choose_file(self, edit: QLineEdit, title: str, file_filter: str) -> None:
        current = edit.text().strip()
        start = str(Path(current).parent) if current else str(Path.home())
        path, _ = QFileDialog.getOpenFileName(self, title, start, file_filter)
        if path:
            edit.setText(path)

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
