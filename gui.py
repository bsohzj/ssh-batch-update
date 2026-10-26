#!/usr/bin/env python3
"""PySide6 desktop interface for the SSH batch command runner."""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional
from uuid import uuid4

from PySide6.QtCore import (
    QDateTime,
    QObject,
    QProcess,
    QSettings,
    QThread,
    QTimer,
    QUrl,
    Qt,
    Signal,
    Slot,
)
from PySide6.QtGui import QCloseEvent, QColor, QDesktopServices, QPainter
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
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
    QTabWidget,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from batch_queue import (
    BatchPayload,
    EDITABLE_STATUSES,
    KeychainSecretStore,
    MemorySecretStore,
    QueueController,
    QueueMetadataStore,
    QueueStorageError,
    SecretStore,
)

from history import (
    HistoryCommandSnapshot,
    HistoryCommandStore,
    HistoryError,
    HistoryFilter,
    HistoryRecord,
    HistoryStore,
    KeychainHistoryCommandStore,
    MemoryHistoryCommandStore,
    default_history_path,
    delete_output_directory,
    read_summary,
    validate_output_deletion,
    write_output_marker,
)

from run_commands import (
    BatchResult,
    CancellationToken,
    ConfigurationError,
    PreparedRun,
    ProgressEvent,
    RunRequest,
    Settings,
    load_settings,
    parse_commands,
    parse_inline_commands,
    prepare_connection_test,
    prepare_run,
    run_batch,
    run_connection_test,
)


APP_NAME = "SSH Batch Update"
ORGANIZATION_NAME = "SG4291"
MANUAL_PROFILE_TEXT = "Manual"
PROFILE_GROUP = "credential_profiles/items"
LAST_PROFILE_KEY = "credential_profiles/last_selected"


@dataclass(frozen=True)
class CredentialProfile:
    """A saved display name and reference to a user-managed credentials file."""

    profile_id: str
    name: str
    env_path: Path


class CredentialProfileStore:
    """Persist profile metadata without storing any credential values."""

    def __init__(self, settings: QSettings) -> None:
        self.settings = settings

    def profiles(self) -> list[CredentialProfile]:
        profiles: list[CredentialProfile] = []
        self.settings.beginGroup(PROFILE_GROUP)
        try:
            for profile_id in self.settings.childGroups():
                self.settings.beginGroup(profile_id)
                try:
                    name = self.settings.value("name", "", str).strip()
                    env_path = self.settings.value("env_path", "", str).strip()
                finally:
                    self.settings.endGroup()
                if name and env_path:
                    profiles.append(CredentialProfile(profile_id, name, Path(env_path)))
        finally:
            self.settings.endGroup()
        return sorted(profiles, key=lambda profile: profile.name.casefold())

    def get(self, profile_id: str) -> Optional[CredentialProfile]:
        return next(
            (profile for profile in self.profiles() if profile.profile_id == profile_id),
            None,
        )

    def add(self, name: str, env_path: Path) -> CredentialProfile:
        clean_name = self._validate_name(name)
        profile = CredentialProfile(uuid4().hex, clean_name, self._absolute_path(env_path))
        self._write(profile)
        return profile

    def rename(self, profile_id: str, name: str) -> CredentialProfile:
        profile = self._required(profile_id)
        updated = CredentialProfile(
            profile.profile_id,
            self._validate_name(name, exclude_id=profile_id),
            profile.env_path,
        )
        self._write(updated)
        return updated

    def relink(self, profile_id: str, env_path: Path) -> CredentialProfile:
        profile = self._required(profile_id)
        updated = CredentialProfile(
            profile.profile_id,
            profile.name,
            self._absolute_path(env_path),
        )
        self._write(updated)
        return updated

    def remove(self, profile_id: str) -> None:
        self.settings.remove(f"{PROFILE_GROUP}/{profile_id}")
        if self.selected_id() == profile_id:
            self.set_selected_id(None)
        self.settings.sync()

    def selected_id(self) -> Optional[str]:
        value = self.settings.value(LAST_PROFILE_KEY, "", str).strip()
        return value or None

    def set_selected_id(self, profile_id: Optional[str]) -> None:
        if profile_id:
            self.settings.setValue(LAST_PROFILE_KEY, profile_id)
        else:
            self.settings.remove(LAST_PROFILE_KEY)
        self.settings.sync()

    def _required(self, profile_id: str) -> CredentialProfile:
        profile = self.get(profile_id)
        if profile is None:
            raise ConfigurationError("credential profile no longer exists")
        return profile

    def _validate_name(self, name: str, exclude_id: Optional[str] = None) -> str:
        clean_name = name.strip()
        if not clean_name:
            raise ConfigurationError("profile name is required")
        if len(clean_name) > 80:
            raise ConfigurationError("profile name must be 80 characters or fewer")
        if any(
            profile.profile_id != exclude_id
            and profile.name.casefold() == clean_name.casefold()
            for profile in self.profiles()
        ):
            raise ConfigurationError(f"a profile named {clean_name!r} already exists")
        return clean_name

    @staticmethod
    def _absolute_path(env_path: Path) -> Path:
        return Path(env_path).expanduser().absolute()

    def _write(self, profile: CredentialProfile) -> None:
        base = f"{PROFILE_GROUP}/{profile.profile_id}"
        self.settings.setValue(f"{base}/name", profile.name)
        self.settings.setValue(f"{base}/env_path", str(profile.env_path))
        self.settings.sync()


def read_credential_profile(env_path: Path) -> Settings:
    """Validate and read a profile without consulting global environment values."""

    path = Path(env_path).expanduser()
    if not path.is_file():
        raise ConfigurationError(f"credential file does not exist: {path}")
    try:
        return load_settings("profile", path, environment={})
    except (OSError, UnicodeError) as exc:
        raise ConfigurationError(f"could not read credential file: {exc}") from exc


class CredentialProfileDialog(QDialog):
    """Add and maintain references to existing credentials files."""

    def __init__(
        self,
        store: CredentialProfileStore,
        initial_path: str = "",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.store = store
        self.last_path = initial_path
        self.setWindowTitle("Manage Credential Profiles")
        self.resize(760, 360)

        layout = QVBoxLayout(self)
        explanation = QLabel(
            "Profiles reference existing .env files. Removing a profile does not delete its file."
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)

        self.profile_table = QTableWidget(0, 2)
        self.profile_table.setHorizontalHeaderLabels(["Profile", "Credentials File"])
        self.profile_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.profile_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.profile_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        profile_header = self.profile_table.horizontalHeader()
        profile_header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        profile_header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.profile_table.itemSelectionChanged.connect(self._update_button_states)
        layout.addWidget(self.profile_table, 1)

        action_row = QHBoxLayout()
        self.add_button = QPushButton("Add…")
        self.rename_button = QPushButton("Rename…")
        self.relink_button = QPushButton("Relink…")
        self.remove_button = QPushButton("Remove")
        self.add_button.clicked.connect(self.add_profile)
        self.rename_button.clicked.connect(self.rename_profile)
        self.relink_button.clicked.connect(self.relink_profile)
        self.remove_button.clicked.connect(self.remove_profile)
        action_row.addWidget(self.add_button)
        action_row.addWidget(self.rename_button)
        action_row.addWidget(self.relink_button)
        action_row.addWidget(self.remove_button)
        action_row.addStretch()
        close_buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close_buttons.rejected.connect(self.reject)
        action_row.addWidget(close_buttons)
        layout.addLayout(action_row)

        self.refresh()

    def refresh(self, selected_id: Optional[str] = None) -> None:
        self.profile_table.setRowCount(0)
        selected_row = -1
        for row, profile in enumerate(self.store.profiles()):
            self.profile_table.insertRow(row)
            name_item = QTableWidgetItem(profile.name)
            name_item.setData(Qt.ItemDataRole.UserRole, profile.profile_id)
            path_item = QTableWidgetItem(str(profile.env_path))
            self.profile_table.setItem(row, 0, name_item)
            self.profile_table.setItem(row, 1, path_item)
            if profile.profile_id == selected_id:
                selected_row = row
        if selected_row >= 0:
            self.profile_table.selectRow(selected_row)
        self._update_button_states()

    def selected_profile(self) -> Optional[CredentialProfile]:
        row = self.profile_table.currentRow()
        if row < 0:
            return None
        item = self.profile_table.item(row, 0)
        if item is None:
            return None
        profile_id = item.data(Qt.ItemDataRole.UserRole)
        return self.store.get(str(profile_id)) if profile_id else None

    @Slot()
    def add_profile(self) -> None:
        path = self._choose_credentials_file("Add credential profile")
        if path is None or not self._validate_file(path):
            return
        default_name = path.stem.lstrip(".") or path.parent.name or "Profile"
        name, accepted = QInputDialog.getText(
            self,
            "Add Credential Profile",
            "Profile name:",
            QLineEdit.EchoMode.Normal,
            default_name,
        )
        if not accepted:
            return
        try:
            profile = self.store.add(name, path)
        except ConfigurationError as exc:
            QMessageBox.critical(self, "Could not add profile", str(exc))
            return
        self.last_path = str(path)
        self.refresh(profile.profile_id)

    @Slot()
    def rename_profile(self) -> None:
        profile = self.selected_profile()
        if profile is None:
            return
        name, accepted = QInputDialog.getText(
            self,
            "Rename Credential Profile",
            "Profile name:",
            QLineEdit.EchoMode.Normal,
            profile.name,
        )
        if not accepted:
            return
        try:
            updated = self.store.rename(profile.profile_id, name)
        except ConfigurationError as exc:
            QMessageBox.critical(self, "Could not rename profile", str(exc))
            return
        self.refresh(updated.profile_id)

    @Slot()
    def relink_profile(self) -> None:
        profile = self.selected_profile()
        if profile is None:
            return
        path = self._choose_credentials_file(
            "Relink credential profile",
            str(profile.env_path),
        )
        if path is None or not self._validate_file(path):
            return
        updated = self.store.relink(profile.profile_id, path)
        self.last_path = str(path)
        self.refresh(updated.profile_id)

    @Slot()
    def remove_profile(self) -> None:
        profile = self.selected_profile()
        if profile is None:
            return
        answer = QMessageBox.question(
            self,
            "Remove credential profile?",
            f"Remove {profile.name!r} from the app?\n\n"
            "The credentials file will not be deleted.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.store.remove(profile.profile_id)
        self.refresh()

    def _choose_credentials_file(
        self,
        title: str,
        current: str = "",
    ) -> Optional[Path]:
        previous = current or self.last_path
        start = str(Path(previous).parent) if previous else str(Path.home())
        selected, _ = QFileDialog.getOpenFileName(
            self,
            title,
            start,
            "Environment files (*.env);;All files (*)",
        )
        return Path(selected).absolute() if selected else None

    def _validate_file(self, path: Path) -> bool:
        try:
            read_credential_profile(path)
        except ConfigurationError as exc:
            QMessageBox.critical(self, "Invalid credential profile", str(exc))
            return False
        return True

    @Slot()
    def _update_button_states(self) -> None:
        enabled = self.selected_profile() is not None
        self.rename_button.setEnabled(enabled)
        self.relink_button.setEnabled(enabled)
        self.remove_button.setEnabled(enabled)


class BatchWorker(QObject):
    """Run a prepared live batch or connection test off the GUI thread."""

    event = Signal(object)
    completed = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        prepared: PreparedRun,
        cancellation_token: CancellationToken,
        operation: str = "run",
    ) -> None:
        super().__init__()
        self.prepared = prepared
        self.cancellation_token = cancellation_token
        self.operation = operation

    @Slot()
    def run(self) -> None:
        try:
            if self.operation == "connection_test":
                result = run_connection_test(
                    self.prepared,
                    progress_callback=self.event.emit,
                    cancellation_token=self.cancellation_token,
                )
            else:
                result = run_batch(
                    self.prepared,
                    progress_callback=self.event.emit,
                    cancellation_token=self.cancellation_token,
                )
        except Exception as exc:  # The GUI boundary must surface all worker failures.
            self.failed.emit(f"{exc.__class__.__name__}: {exc}")
        else:
            self.completed.emit(result)


class ScheduleDialog(QDialog):
    """Select one future local date and time with minute precision."""

    def __init__(
        self,
        initial_utc: Optional[datetime] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Schedule Run")
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)
        label = QLabel("Run this batch at:")
        layout.addWidget(label)

        minimum = QDateTime.currentDateTime().addSecs(60)
        if initial_utc is not None:
            selected = QDateTime.fromSecsSinceEpoch(int(initial_utc.timestamp())).toLocalTime()
            selected = max(selected, minimum)
        else:
            selected = minimum
        if selected.time().second() or selected.time().msec():
            selected = selected.addSecs(60 - selected.time().second())
        selected.setTime(selected.time().addMSecs(-selected.time().msec()))

        fields = QFormLayout()
        self.date_edit = QDateEdit()
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDisplayFormat("ddd, d MMM yyyy")
        self.date_edit.setMinimumDate(QDateTime.currentDateTime().date())
        self.date_edit.setDate(selected.date())
        self.time_edit = QTimeEdit()
        self.time_edit.setDisplayFormat("h:mm AP")
        self.time_edit.setTime(selected.time())
        fields.addRow("Date", self.date_edit)
        fields.addRow("Time", self.time_edit)
        layout.addLayout(fields)

        self.preview_label = QLabel()
        self.preview_label.setWordWrap(True)
        self.date_edit.dateChanged.connect(self._update_preview)
        self.time_edit.timeChanged.connect(self._update_preview)
        layout.addWidget(self.preview_label)
        self._update_preview()

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        schedule_button = buttons.addButton(
            "Schedule", QDialogButtonBox.ButtonRole.AcceptRole
        )
        schedule_button.clicked.connect(self._accept_if_future)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _selected_local(self) -> QDateTime:
        return QDateTime(self.date_edit.date(), self.time_edit.time())

    @Slot()
    def _update_preview(self) -> None:
        display = self._selected_local().toString(
            "dddd, d MMMM yyyy 'at' h:mm AP"
        )
        self.preview_label.setText(f"Scheduled for {display}")

    def selected_utc(self) -> datetime:
        local = self._selected_local().toPython().replace(second=0, microsecond=0)
        if local.tzinfo is None:
            local = local.astimezone()
        return local.astimezone(timezone.utc)

    @Slot()
    def _accept_if_future(self) -> None:
        if self.selected_utc() <= datetime.now(timezone.utc):
            QMessageBox.critical(self, "Invalid schedule", "Choose a future date and time.")
            return
        self.accept()


class IdleSleepInhibitor(QObject):
    """Use macOS caffeinate while queued work needs the system to remain awake."""

    warning = Signal(str)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.process = QProcess(self)
        self.process.errorOccurred.connect(self._process_error)
        self._requested = False
        self._failed = False

    def set_required(self, required: bool) -> None:
        self._requested = required
        if sys.platform != "darwin" or not Path("/usr/bin/caffeinate").is_file():
            return
        if not required:
            self._failed = False
        if (
            required
            and not self._failed
            and self.process.state() == QProcess.ProcessState.NotRunning
        ):
            self.process.start("/usr/bin/caffeinate", ["-i", "-w", str(os.getpid())])
        elif not required and self.process.state() != QProcess.ProcessState.NotRunning:
            self.process.terminate()

    @Slot(QProcess.ProcessError)
    def _process_error(self, _error: QProcess.ProcessError) -> None:
        if self._requested:
            self._failed = True
            self.warning.emit(
                "macOS idle-sleep prevention could not start. Scheduled jobs remain queued, "
                "but the Mac must stay awake."
            )


class MainWindow(QMainWindow):
    """Main application window."""

    def __init__(
        self,
        settings: Optional[QSettings] = None,
        secret_store: Optional[SecretStore] = None,
        clock: Optional[Callable[[], datetime]] = None,
        history_store: Optional[HistoryStore] = None,
        history_command_store: Optional[HistoryCommandStore] = None,
    ) -> None:
        super().__init__()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.settings = settings or QSettings(ORGANIZATION_NAME, APP_NAME)
        self.profile_store = CredentialProfileStore(self.settings)
        self.queue_controller = QueueController(
            QueueMetadataStore(self.settings),
            secret_store or KeychainSecretStore(),
            clock=self._clock,
        )
        self._history_startup_warning = ""
        try:
            self.history_store: Optional[HistoryStore] = history_store or HistoryStore(
                default_history_path(), clock=self._clock
            )
            interrupted = self.history_store.recover_interrupted()
            if interrupted:
                self._history_startup_warning = (
                    f"Recovered {interrupted} interrupted audit history record(s)."
                )
        except HistoryError as exc:
            self.history_store = None
            self._history_startup_warning = str(exc)
        self.history_command_store = (
            history_command_store or KeychainHistoryCommandStore()
        )
        self._prepared: Optional[PreparedRun] = None
        self._batch_result: Optional[BatchResult] = None
        self._job_prepared: dict[str, PreparedRun] = {}
        self._job_results: dict[str, BatchResult] = {}
        self._job_rows: dict[str, list[list[str]]] = {}
        self._job_progress: dict[str, tuple[int, int, str, str]] = {}
        self._selected_job_id: Optional[str] = None
        self._active_job_id: Optional[str] = None
        self._editing_job_id: Optional[str] = None
        self._editing_connection_settings: Optional[Settings] = None
        self._token: Optional[CancellationToken] = None
        self._thread: Optional[QThread] = None
        self._worker: Optional[BatchWorker] = None
        self._operation: Optional[str] = None
        self._running = False
        self._loading_settings = True
        self._loading_profile = False
        self._close_after_run = False
        self._closing = False
        self._rows: dict[str, int] = {}
        self._last_inventory_import = ""
        self._last_command_import = ""
        self._last_credential_import = ""
        self._credential_file: Optional[Path] = None
        self._history_ui_built = False
        self._history_page = 0
        self._history_records: dict[str, HistoryRecord] = {}

        self.setWindowTitle(APP_NAME)
        self.resize(1400, 850)
        self.setMinimumSize(1050, 680)
        self._build_ui()
        self._restore_settings()
        self._connect_input_changes()
        self._loading_settings = False
        self._set_validated(False)
        self._refresh_queue_table()
        self.sleep_inhibitor = IdleSleepInhibitor(self)
        self.sleep_inhibitor.warning.connect(self._show_sleep_warning)
        self.scheduler_timer = QTimer(self)
        self.scheduler_timer.setInterval(1000)
        self.scheduler_timer.timeout.connect(self._queue_tick)
        self.scheduler_timer.start()
        self._update_queue_controls()
        if self._history_startup_warning:
            self.validation_label.setText(self._history_startup_warning)

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
            "One IPv4 or IPv6 address per line. Blank lines and # comments are ignored."
        )
        self.inventory_text_edit.setMaximumHeight(100)
        inventory_button = QPushButton("Import Devices")
        inventory_button.clicked.connect(self._import_inventory)
        inventory_export_button = QPushButton("Export Devices")
        inventory_export_button.clicked.connect(self._export_inventory)
        self.clear_devices_button = QPushButton("Clear Devices")
        self.clear_devices_button.setEnabled(False)
        self.clear_devices_button.clicked.connect(self._clear_devices)
        self.test_connections_button = QPushButton("Test Connections")
        self.test_connections_button.setToolTip(
            "Log in, detect the prompt, optionally test enable mode, and disconnect. "
            "Commands in the command boxes are not run."
        )
        self.test_connections_button.clicked.connect(self.start_connection_test)
        inventory_buttons = QWidget()
        inventory_buttons_layout = QVBoxLayout(inventory_buttons)
        inventory_buttons_layout.setContentsMargins(0, 0, 0, 0)
        inventory_buttons_layout.addWidget(inventory_button)
        inventory_buttons_layout.addWidget(inventory_export_button)
        inventory_buttons_layout.addWidget(self.clear_devices_button)
        inventory_buttons_layout.addWidget(self.test_connections_button)
        inventory_buttons_layout.addStretch()
        inventory_container = QWidget()
        inventory_layout = QHBoxLayout(inventory_container)
        inventory_layout.setContentsMargins(0, 0, 0, 0)
        inventory_layout.addWidget(self.inventory_text_edit, 1)
        inventory_layout.addWidget(inventory_buttons, 0)
        form.addRow("Devices", inventory_container)

        self.exec_commands_edit = QPlainTextEdit()
        self.exec_commands_edit.setPlaceholderText("One exec command per line")
        self.exec_commands_edit.setMaximumHeight(120)
        self.config_commands_edit = QPlainTextEdit()
        self.config_commands_edit.setPlaceholderText("One configuration command per line")
        self.config_commands_edit.setMaximumHeight(120)
        import_sectioned_button = QPushButton("Import Existing Commands")
        import_sectioned_button.clicked.connect(self._import_sectioned_commands)
        export_sectioned_button = QPushButton("Export Commands")
        export_sectioned_button.clicked.connect(self._export_sectioned_commands)
        self.clear_commands_button = QPushButton("Clear Commands")
        self.clear_commands_button.setEnabled(False)
        self.clear_commands_button.clicked.connect(self._clear_commands)
        command_file_buttons = QWidget()
        command_file_buttons_layout = QGridLayout(command_file_buttons)
        command_file_buttons_layout.setContentsMargins(0, 0, 0, 0)
        command_file_buttons_layout.addWidget(import_sectioned_button, 0, 0, 1, 2)
        command_file_buttons_layout.addWidget(export_sectioned_button, 1, 0)
        command_file_buttons_layout.addWidget(self.clear_commands_button, 1, 1)
        command_file_buttons_layout.setColumnStretch(0, 1)
        command_file_buttons_layout.setColumnStretch(1, 1)

        form.addRow("Exec Commands", self.exec_commands_edit)
        form.addRow("Config Commands", self.config_commands_edit)
        form.addRow("", command_file_buttons)

        self.username_edit = QLineEdit()
        self.username_edit.setPlaceholderText("SSH Username")
        self.password_edit = QLineEdit()
        self.password_edit.setPlaceholderText("SSH Password")
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.profile_combo = QComboBox()
        self.manage_profiles_button = QPushButton("Manage Profiles…")
        self.manage_profiles_button.clicked.connect(self.manage_profiles)
        profile_container = QWidget()
        profile_layout = QHBoxLayout(profile_container)
        profile_layout.setContentsMargins(0, 0, 0, 0)
        profile_layout.addWidget(self.profile_combo, 1)
        profile_layout.addWidget(self.manage_profiles_button)
        self.output_edit, output_row = self._path_row("Choose output folder…", self._choose_output)
        form.insertRow(0, "Profile", profile_container)
        form.insertRow(1, "Username", self.username_edit)
        form.insertRow(2, "Password", self.password_edit)
        form.addRow("Output Folder", output_row)

        self.device_type_combo = QComboBox()
        self.device_type_combo.setEditable(True)
        self.device_type_combo.addItems(["huawei", "cisco_ios"])
        form.insertRow(3, "Device Type", self.device_type_combo)

        settings_layout.addWidget(self.configuration_group)

        button_row = QGridLayout()
        self.validate_button = QPushButton("Validate Input")
        self.run_button = QPushButton("Run Live")
        self.schedule_button = QPushButton("Schedule Run")
        self.cancel_button = QPushButton("Stop Run")
        self.cancel_button.setStyleSheet("QPushButton { color: #c62828; }")
        self.validate_button.clicked.connect(self.validate_inputs)
        self.run_button.clicked.connect(self.start_run)
        self.schedule_button.clicked.connect(self.schedule_batch)
        self.cancel_button.clicked.connect(self.cancel_run)
        button_row.addWidget(self.validate_button, 0, 0)
        button_row.addWidget(self.run_button, 0, 1)
        button_row.addWidget(self.schedule_button, 1, 0)
        button_row.addWidget(self.cancel_button, 1, 1)
        edit_row = QHBoxLayout()
        self.save_edit_button = QPushButton("Save Changes")
        self.cancel_edit_button = QPushButton("Cancel Edit")
        self.save_edit_button.clicked.connect(self.save_batch_edit)
        self.cancel_edit_button.clicked.connect(self.cancel_batch_edit)
        self.save_edit_button.hide()
        self.cancel_edit_button.hide()
        edit_row.addWidget(self.save_edit_button)
        edit_row.addWidget(self.cancel_edit_button)
        edit_row.addStretch()
        self.validation_label = QLabel("Enter or import devices and commands, then validate.")
        self.validation_label.setWordWrap(True)
        settings_layout.addLayout(button_row)
        settings_layout.addLayout(edit_row)
        settings_layout.addWidget(self.validation_label)
        settings_layout.addStretch(1)

        self.results_tabs = QTabWidget()
        queue_tab = QWidget()
        queue_layout = QVBoxLayout(queue_tab)
        self.queue_table = QTableWidget(0, 8)
        self.queue_table.setHorizontalHeaderLabels(
            ["Order", "Batch", "Device Type", "Devices", "Commands", "Run At", "Status", "Result"]
        )
        self.queue_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.queue_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.queue_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.queue_table.verticalHeader().setVisible(False)
        self.queue_table.itemSelectionChanged.connect(self._queue_selection_changed)
        self.queue_table.cellDoubleClicked.connect(self._queue_double_clicked)
        queue_header = self.queue_table.horizontalHeader()
        for column in (0, 2, 3, 4, 5, 6):
            queue_header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        queue_header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        queue_header.setSectionResizeMode(7, QHeaderView.ResizeMode.Stretch)
        queue_layout.addWidget(self.queue_table, 1)

        queue_actions = QGridLayout()
        self.pause_queue_button = QPushButton("Pause Queue")
        self.move_up_button = QPushButton("Move Up")
        self.move_down_button = QPushButton("Move Down")
        self.edit_batch_button = QPushButton("Edit Batch")
        self.reschedule_button = QPushButton("Reschedule…")
        self.retry_button = QPushButton("Retry")
        self.remove_batch_button = QPushButton("Remove")
        self.clear_completed_button = QPushButton("Clear Completed")
        self.clear_completed_button.setStyleSheet("QPushButton { color: #c62828; }")
        self.pause_queue_button.clicked.connect(self.toggle_queue_pause)
        self.move_up_button.clicked.connect(lambda: self.move_selected_batch(-1))
        self.move_down_button.clicked.connect(lambda: self.move_selected_batch(1))
        self.edit_batch_button.clicked.connect(self.edit_selected_batch)
        self.reschedule_button.clicked.connect(self.reschedule_selected_batch)
        self.retry_button.clicked.connect(self.retry_selected_batch)
        self.remove_batch_button.clicked.connect(self.remove_selected_batch)
        self.clear_completed_button.clicked.connect(self.clear_completed_batches)
        for index, button in enumerate(
            (
                self.pause_queue_button,
                self.move_up_button,
                self.move_down_button,
                self.edit_batch_button,
                self.reschedule_button,
                self.retry_button,
                self.remove_batch_button,
                self.clear_completed_button,
            )
        ):
            queue_actions.addWidget(button, index // 4, index % 4)
        queue_layout.addLayout(queue_actions)

        device_results_tab = QWidget()
        device_results_layout = QVBoxLayout(device_results_tab)
        self.device_table = QTableWidget(0, 5)
        self.device_table.setHorizontalHeaderLabels(
            ["Device", "Status", "Stage / Line", "Result", "Output"]
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
        device_results_layout.addWidget(self.device_table, 1)

        progress_grid = QGridLayout()
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        self.status_label = QLabel("Idle")
        self.totals_label = QLabel("0 succeeded, 0 failed, 0 cancelled")
        progress_grid.addWidget(self.progress_bar, 0, 0, 1, 2)
        progress_grid.addWidget(self.status_label, 1, 0)
        progress_grid.addWidget(self.totals_label, 1, 1)
        device_results_layout.addLayout(progress_grid)

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
        device_results_layout.addLayout(result_buttons)

        self.results_tabs.addTab(queue_tab, "Queue")
        self.results_tabs.addTab(device_results_tab, "Device Results")
        self.history_tab = QWidget()
        self.history_layout = QVBoxLayout(self.history_tab)
        self.history_placeholder = QLabel(
            "Open this tab to load persistent audit history."
        )
        self.history_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.history_layout.addWidget(self.history_placeholder, 1)
        self.results_tabs.addTab(self.history_tab, "History")
        self.results_tabs.currentChanged.connect(self._results_tab_changed)
        results_layout.addWidget(self.results_tabs, 1)

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
        self.username_edit.textChanged.connect(self._credentials_edited)
        self.password_edit.textChanged.connect(self._credentials_edited)
        self.output_edit.textChanged.connect(self.invalidate_validation)
        self.inventory_text_edit.textChanged.connect(self.invalidate_validation)
        self.inventory_text_edit.textChanged.connect(self._update_clear_devices_button)
        self.exec_commands_edit.textChanged.connect(self.invalidate_validation)
        self.exec_commands_edit.textChanged.connect(self._update_clear_commands_button)
        self.config_commands_edit.textChanged.connect(self.invalidate_validation)
        self.config_commands_edit.textChanged.connect(self._update_clear_commands_button)
        self.device_type_combo.currentTextChanged.connect(self.invalidate_validation)
        self.profile_combo.activated.connect(self._profile_activated)

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
        selected_profile_id = self.profile_store.selected_id()
        self._refresh_profile_combo(selected_profile_id)
        if selected_profile_id:
            self._load_profile_by_id(selected_profile_id, startup=True)
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

    def _refresh_profile_combo(self, selected_id: Optional[str] = None) -> None:
        self.profile_combo.clear()
        self.profile_combo.addItem(MANUAL_PROFILE_TEXT, None)
        selected_index = 0
        for profile in self.profile_store.profiles():
            self.profile_combo.addItem(profile.name, profile.profile_id)
            if profile.profile_id == selected_id:
                selected_index = self.profile_combo.count() - 1
        self.profile_combo.setCurrentIndex(selected_index)

    @Slot(int)
    def _profile_activated(self, index: int) -> None:
        profile_id = self.profile_combo.itemData(index)
        if not profile_id:
            self._switch_to_manual(clear_credentials=True)
            self.validation_label.setText("Enter credentials manually, then validate.")
            return
        self._load_profile_by_id(str(profile_id), startup=False)

    def _load_profile_by_id(self, profile_id: str, startup: bool) -> bool:
        profile = self.profile_store.get(profile_id)
        if profile is None:
            self._switch_to_manual(clear_credentials=True)
            self.validation_label.setText("The previously selected profile no longer exists.")
            return False

        try:
            credentials = read_credential_profile(profile.env_path)
        except ConfigurationError as exc:
            self._loading_profile = True
            try:
                self.username_edit.clear()
                self.password_edit.clear()
            finally:
                self._loading_profile = False
            self._credential_file = None
            self._prepared = None
            self._set_validated(False)
            if startup:
                self.profile_store.set_selected_id(None)
                self.profile_combo.setCurrentIndex(0)
                self.validation_label.setText(
                    f"Could not load the previous profile {profile.name!r}: {exc}"
                )
            else:
                self.profile_store.set_selected_id(profile.profile_id)
                self.validation_label.setText(
                    f"Could not load profile {profile.name!r}: {exc}"
                )
            return False

        self._loading_profile = True
        try:
            self.username_edit.setText(credentials.username)
            self.password_edit.setText(credentials.password)
        finally:
            self._loading_profile = False
        self._credential_file = profile.env_path
        self.profile_store.set_selected_id(profile.profile_id)
        self._prepared = None
        self._set_validated(False)
        self.validation_label.setText(f"Loaded credential profile: {profile.name}")
        return True

    @Slot()
    def _credentials_edited(self) -> None:
        if self._loading_settings or self._loading_profile:
            return
        if self.profile_combo.currentData():
            self._switch_to_manual(clear_credentials=False)
        self.invalidate_validation()

    def _switch_to_manual(self, clear_credentials: bool) -> None:
        self._loading_profile = True
        try:
            self.profile_combo.setCurrentIndex(0)
            if clear_credentials:
                self.username_edit.clear()
                self.password_edit.clear()
        finally:
            self._loading_profile = False
        self._credential_file = None
        self.profile_store.set_selected_id(None)
        self._prepared = None
        self._set_validated(False)

    @Slot()
    def manage_profiles(self) -> None:
        selected_id = self.profile_combo.currentData()
        was_manual = not bool(selected_id)
        dialog = CredentialProfileDialog(
            self.profile_store,
            self._last_credential_import,
            self,
        )
        if selected_id:
            dialog.refresh(str(selected_id))
        dialog.exec()
        if dialog.last_path:
            self._last_credential_import = dialog.last_path

        selected_id = str(selected_id) if selected_id else None
        selected_profile = self.profile_store.get(selected_id) if selected_id else None
        self._refresh_profile_combo(selected_id if selected_profile else None)
        if selected_profile:
            self._load_profile_by_id(selected_profile.profile_id, startup=False)
        elif was_manual:
            self._credential_file = None
            self.profile_store.set_selected_id(None)
            self._prepared = None
            self._set_validated(False)
            self.validation_label.setText("Select a profile or enter credentials manually.")
        else:
            self._switch_to_manual(clear_credentials=True)
            self.validation_label.setText("Select a profile or enter credentials manually.")
        self._save_settings()

    @Slot()
    def invalidate_validation(self) -> None:
        if self._loading_settings:
            return
        self._prepared = None
        self._set_validated(False)
        self.validation_label.setText("Inputs changed; validate again.")

    def _set_validated(self, validated: bool) -> None:
        editable = self._editing_job_id is not None
        self.run_button.setEnabled(validated and not editable)
        self.schedule_button.setEnabled(validated and not editable)
        self.save_edit_button.setEnabled(validated and editable)
        self.test_connections_button.setEnabled(not self._running)
        self.cancel_button.setEnabled(self._running)

    @Slot()
    def _clear_devices(self) -> None:
        self.inventory_text_edit.clear()

    @Slot()
    def _clear_commands(self) -> None:
        self.exec_commands_edit.clear()
        self.config_commands_edit.clear()

    @Slot()
    def _update_clear_devices_button(self) -> None:
        self.clear_devices_button.setEnabled(
            bool(self.inventory_text_edit.toPlainText())
        )

    @Slot()
    def _update_clear_commands_button(self) -> None:
        self.clear_commands_button.setEnabled(
            bool(
                self.exec_commands_edit.toPlainText()
                or self.config_commands_edit.toPlainText()
            )
        )

    def _request_from_inputs(self, include_commands: bool = True) -> RunRequest:
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
            exec_commands_text=(
                self.exec_commands_edit.toPlainText() if include_commands else None
            ),
            config_commands_text=(
                self.config_commands_edit.toPlainText() if include_commands else None
            ),
        )

    def _credential_environment(self) -> dict[str, str]:
        values = {
            "SSH_USERNAME": self.username_edit.text(),
            "SSH_PASSWORD": self.password_edit.text(),
        }
        if self._editing_connection_settings is not None:
            values.update(
                {
                    "ENABLE_SECRET": self._editing_connection_settings.enable_secret or "",
                    "SSH_PORT": str(self._editing_connection_settings.port),
                    "SSH_TIMEOUT": str(self._editing_connection_settings.timeout),
                }
            )
        return values

    @Slot()
    def validate_inputs(self, show_success: bool = True) -> bool:
        try:
            request = self._request_from_inputs()
            prepared = prepare_run(
                request,
                require_credentials=True,
                environment=self._credential_environment(),
            )
        except (ConfigurationError, OSError) as exc:
            self._prepared = None
            self._set_validated(False)
            self.validation_label.setText(f"Validation failed; {exc}")
            if show_success:
                QMessageBox.critical(self, "Validation failed", str(exc))
            return False

        self._prepared = prepared
        if not self._running and self._selected_job_id is None:
            self._populate_devices(prepared)
        self._save_settings()
        valid = len(prepared.valid_entries)
        invalid = len(prepared.invalid_entries)
        if valid == 0:
            entry_word = "entry" if invalid == 1 else "entries"
            reason = f"no valid IP addresses found ({invalid} invalid device {entry_word})"
            self.validation_label.setText(f"Validation failed; {reason}")
            self._set_validated(False)
            if show_success:
                QMessageBox.critical(self, "Validation failed", reason)
            return False
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

    def _populate_devices(
        self,
        prepared: PreparedRun,
        operation: str = "run",
    ) -> None:
        self._selected_job_id = None
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
        self.totals_label.setText(
            "0 passed, 0 failed, 0 cancelled"
            if operation == "connection_test"
            else "0 succeeded, 0 failed, 0 cancelled"
        )
        self.open_output_button.setEnabled(False)
        self.open_summary_button.setEnabled(False)

    def _initialize_job_rows(self, job_id: str, prepared: PreparedRun) -> None:
        self._job_rows[job_id] = [
            [entry.address, "Invalid" if entry.error else "Ready", "", entry.error, ""]
            for entry in prepared.entries
        ]
        self._job_progress[job_id] = (
            0,
            max(1, len(prepared.entries)),
            "Queued",
            "0 succeeded, 0 failed, 0 cancelled",
        )

    def _show_job_results(self, job_id: str) -> None:
        job = self.queue_controller.job(job_id)
        rows = self._job_rows.get(job_id)
        if rows is None and job is not None:
            try:
                prepared = self._job_prepared.get(job_id)
                if prepared is None:
                    prepared = self.queue_controller.payload(job_id).prepare()
            except (QueueStorageError, ConfigurationError, OSError):
                prepared = None
            if prepared is not None:
                self._job_prepared[job_id] = prepared
                self._initialize_job_rows(job_id, prepared)
                rows = self._job_rows[job_id]
        self._selected_job_id = job_id
        self.device_table.setRowCount(0)
        self._rows.clear()
        for row_index, values in enumerate(rows or []):
            self.device_table.insertRow(row_index)
            self._rows[values[0]] = row_index
            for column, value in enumerate(values):
                self._set_cell(row_index, column, value)
        value, maximum, status, totals = self._job_progress.get(
            job_id,
            (0, max(1, len(rows or [])), job.status.title() if job else "Queued", ""),
        )
        self.progress_bar.setRange(0, maximum)
        self.progress_bar.setValue(value)
        self.status_label.setText(status)
        self.totals_label.setText(totals)
        result = self._job_results.get(job_id)
        self._batch_result = result
        self.open_output_button.setEnabled(bool(result and result.run_directory.is_dir()))
        self.open_summary_button.setEnabled(bool(result and result.summary_path.is_file()))

    def _set_job_cell(self, job_id: str, address: str, column: int, value: str) -> None:
        rows = self._job_rows.get(job_id, [])
        for row_index, row in enumerate(rows):
            if row[0] == address:
                row[column] = value
                if self._selected_job_id == job_id:
                    self._set_cell(row_index, column, value)
                return

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
    def start_connection_test(self) -> None:
        if self._running:
            return
        try:
            request = self._request_from_inputs(include_commands=False)
            prepared = prepare_connection_test(
                request,
                environment=self._credential_environment(),
            )
        except (ConfigurationError, OSError) as exc:
            QMessageBox.critical(self, "Cannot test connections", str(exc))
            return

        if not prepared.valid_entries:
            self._populate_devices(prepared, operation="connection_test")
            QMessageBox.critical(
                self,
                "Cannot test connections",
                "At least one valid IP address is required.",
            )
            return

        self._populate_devices(prepared, operation="connection_test")
        self._save_settings()
        self._start_worker(prepared, operation="connection_test")

    @Slot()
    def start_run(self) -> None:
        self._submit_batch(scheduled_utc=None, request_dispatch=True)

    @Slot()
    def schedule_batch(self) -> None:
        dialog = ScheduleDialog(parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self._submit_batch(scheduled_utc=dialog.selected_utc(), request_dispatch=False)

    def _submit_batch(
        self,
        scheduled_utc: Optional[datetime],
        request_dispatch: bool,
    ) -> None:
        if not self.validate_inputs(show_success=False) or self._prepared is None:
            QMessageBox.critical(self, "Cannot run", "Correct the validation errors first.")
            return
        prepared = self._prepared
        schedule_text = ""
        if scheduled_utc is not None:
            schedule_text = (
                "\nScheduled for "
                f"{scheduled_utc.astimezone().strftime('%Y-%m-%d %H:%M')}.\n"
            )
        answer = QMessageBox.warning(
            self,
            "Confirm live batch",
            f"Submit {len(prepared.commands)} command(s) for "
            f"{len(prepared.valid_entries)} valid device(s)?\n\n"
            f"{schedule_text}"
            "This makes live changes and does not automatically roll them back.\n"
            "Literal passwords in the command boxes may appear in outputs or summary.csv.\n"
            "Until the batch finishes or is removed, its complete snapshot is protected "
            "by macOS Keychain.",
            QMessageBox.StandardButton.Apply | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Apply:
            return

        try:
            payload = BatchPayload.from_prepared(
                prepared,
                self.profile_combo.currentText() or MANUAL_PROFILE_TEXT,
            )
            job = self.queue_controller.add(payload, prepared, scheduled_utc)
        except (QueueStorageError, OSError) as exc:
            QMessageBox.critical(self, "Could not queue batch", str(exc))
            return
        self._job_prepared[job.job_id] = prepared
        self._initialize_job_rows(job.job_id, prepared)
        self._selected_job_id = job.job_id
        if request_dispatch:
            self.queue_controller.paused = False
        self._refresh_queue_table(select_job_id=job.job_id)
        self._show_job_results(job.job_id)
        self._save_settings()
        self._update_queue_controls()
        QTimer.singleShot(0, self._queue_tick)

    @Slot()
    def _queue_tick(self) -> None:
        if self._closing:
            return
        self.queue_controller.refresh_due()
        self._refresh_queue_table(select_job_id=self._selected_job_id)
        future_armed = any(
            job.status == "scheduled" for job in self.queue_controller.pending_jobs()
        ) and not self.queue_controller.paused
        self.sleep_inhibitor.set_required(self._running or future_armed)
        if self._running:
            return
        job = self.queue_controller.next_eligible()
        if job is None:
            self._update_queue_controls()
            return
        try:
            payload = self.queue_controller.payload(job.job_id)
            prepared = self._job_prepared.get(job.job_id)
            if prepared is None:
                prepared = payload.prepare()
        except (QueueStorageError, ConfigurationError, OSError) as exc:
            self.queue_controller.mark_blocked(job.job_id, str(exc))
            self._refresh_queue_table(select_job_id=job.job_id)
            QTimer.singleShot(0, self._queue_tick)
            return

        self._job_prepared[job.job_id] = prepared
        if job.job_id not in self._job_rows:
            self._initialize_job_rows(job.job_id, prepared)
        self.queue_controller.mark_running(job.job_id)
        self._start_audit_record(job, payload, prepared)
        self._active_job_id = job.job_id
        self._selected_job_id = job.job_id
        self._refresh_queue_table(select_job_id=job.job_id)
        self._show_job_results(job.job_id)
        self.results_tabs.setCurrentIndex(1)
        self._start_worker(prepared, operation="run", job_id=job.job_id)

    def _start_audit_record(
        self,
        job,
        payload: BatchPayload,
        prepared: PreparedRun,
    ) -> None:
        if self.history_store is None:
            if self._history_startup_warning:
                self.validation_label.setText(self._history_startup_warning)
            return
        record = HistoryRecord(
            job_id=job.job_id,
            batch_name=job.name,
            created_utc=job.created_utc,
            scheduled_utc=job.scheduled_utc,
            started_utc=self._clock(),
            completed_utc=None,
            source="scheduled" if job.scheduled_utc else "manual",
            profile_label=payload.profile_label,
            device_type=job.device_type,
            valid_devices=job.valid_devices,
            invalid_devices=job.invalid_devices,
            command_count=job.command_count,
        )
        warnings: list[str] = []
        try:
            self.history_store.start(record)
        except HistoryError as exc:
            warnings.append(str(exc))
        else:
            try:
                self.history_command_store.set(
                    job.job_id, HistoryCommandSnapshot(tuple(prepared.commands))
                )
            except HistoryError as exc:
                warnings.append(str(exc))
        if warnings:
            self.validation_label.setText("Audit history warning: " + " ".join(warnings))
        self._refresh_history_if_loaded()

    def _safe_history_message(self, job_id: str, message: str) -> str:
        prepared = self._job_prepared.get(job_id)
        safe = str(message)
        if prepared is None:
            return safe
        if prepared.settings is not None:
            for secret in (
                prepared.settings.username,
                prepared.settings.password,
                prepared.settings.enable_secret,
            ):
                if secret:
                    safe = safe.replace(secret, "[redacted]")
        for entry in prepared.entries:
            if entry.address:
                safe = safe.replace(entry.address, "[device]")
        return safe

    def _finalize_audit_record(
        self,
        job_id: str,
        status: str,
        result_text: str,
        result: Optional[BatchResult] = None,
    ) -> None:
        if self.history_store is None:
            return
        try:
            self.history_store.finalize(
                job_id,
                status,
                result.successes if result else 0,
                result.failures if result else 0,
                result.cancelled if result else 0,
                self._safe_history_message(job_id, result_text),
                str(result.run_directory) if result else "",
                str(result.summary_path) if result else "",
                completed_utc=self._clock(),
            )
            record = self.history_store.get(job_id)
            if record and record.run_directory:
                write_output_marker(record)
        except (HistoryError, OSError) as exc:
            self.validation_label.setText(f"Audit history warning: {exc}")
        self._refresh_history_if_loaded()

    def _start_worker(
        self,
        prepared: PreparedRun,
        operation: str,
        job_id: Optional[str] = None,
    ) -> None:
        self._running = True
        self._operation = operation
        if operation == "run":
            self._active_job_id = job_id
        else:
            self._active_job_id = None
            self._batch_result = None
        self._token = CancellationToken()
        self._thread = QThread(self)
        self._worker = BatchWorker(prepared, self._token, operation=operation)
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
        self.validate_button.setEnabled(True)
        self.test_connections_button.setEnabled(not running)
        self.cancel_button.setEnabled(running)
        self.configuration_group.setEnabled(True)
        self._set_validated(self._prepared is not None)
        self._update_queue_controls()

    @Slot(object)
    def handle_progress(self, event: ProgressEvent) -> None:
        if self._operation == "run" and self._active_job_id:
            self._handle_job_progress(self._active_job_id, event)
            return
        if event.kind == "run_started":
            self.progress_bar.setRange(0, max(1, event.total))
            self.progress_bar.setValue(0)
            self.status_label.setText(
                "Testing connections"
                if self._operation == "connection_test"
                else "Running"
            )
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
            if event.section:
                self._set_cell(row, 2, event.section)
        elif event.kind == "command_started":
            self._set_cell(row, 1, "Running")
            self._set_cell(row, 2, f"{event.section} / {event.line_number}")
        elif event.kind == "device_finished":
            status = event.status.title()
            if self._operation == "connection_test" and event.status == "success":
                status = "Passed"
            self._set_cell(row, 1, status)
            if event.line_number:
                self._set_cell(row, 2, f"{event.section} / {event.line_number}")
            elif event.section:
                self._set_cell(row, 2, event.section)
            self._set_cell(row, 3, event.message or status)
            self._set_cell(row, 4, event.transcript_file)
            self.progress_bar.setValue(event.index)

    def _handle_job_progress(self, job_id: str, event: ProgressEvent) -> None:
        value, maximum, status_text, totals = self._job_progress.get(
            job_id, (0, max(1, event.total), "Queued", "")
        )
        if event.kind == "run_started":
            value, maximum, status_text = 0, max(1, event.total), "Running"
        elif event.kind == "run_finished":
            status_text, totals = event.message, event.message
        elif event.kind == "device_started":
            self._set_job_cell(job_id, event.address, 1, "Starting")
        elif event.kind == "device_status":
            self._set_job_cell(job_id, event.address, 1, event.status.title())
            if event.section:
                self._set_job_cell(job_id, event.address, 2, event.section)
        elif event.kind == "command_started":
            self._set_job_cell(job_id, event.address, 1, "Running")
            self._set_job_cell(
                job_id,
                event.address,
                2,
                f"{event.section} / {event.line_number}",
            )
        elif event.kind == "device_finished":
            shown_status = event.status.title()
            self._set_job_cell(job_id, event.address, 1, shown_status)
            if event.line_number:
                self._set_job_cell(
                    job_id,
                    event.address,
                    2,
                    f"{event.section} / {event.line_number}",
                )
            elif event.section:
                self._set_job_cell(job_id, event.address, 2, event.section)
            self._set_job_cell(job_id, event.address, 3, event.message or shown_status)
            self._set_job_cell(job_id, event.address, 4, event.transcript_file)
            value = event.index
        self._job_progress[job_id] = (value, maximum, status_text, totals)
        if self._selected_job_id == job_id:
            self.progress_bar.setRange(0, maximum)
            self.progress_bar.setValue(value)
            self.status_label.setText(status_text)
            self.totals_label.setText(totals)

    @Slot()
    def cancel_run(self) -> None:
        if not self._running or self._token is None:
            return
        self.queue_controller.paused = True
        self._refresh_queue_table(select_job_id=self._active_job_id)
        self._token.cancel()
        self.cancel_button.setEnabled(False)
        self.status_label.setText("Cancelling at the next safe checkpoint…")
        self._update_queue_controls()

    @Slot(object)
    def _run_completed(self, result: BatchResult) -> None:
        if self._operation == "run" and self._active_job_id:
            job_id = self._active_job_id
            self._job_results[job_id] = result
            terminal_status = (
                "cancelled"
                if result.cancelled
                else "completed_with_failures"
                if result.failures
                else "succeeded"
            )
            result_text = (
                f"{result.successes} succeeded, {result.failures} failed, "
                f"{result.cancelled} cancelled"
            )
            self._finalize_audit_record(
                job_id, terminal_status, result_text, result
            )
            warning = self.queue_controller.mark_terminal(
                job_id, terminal_status, result_text
            )
            current = self._job_progress.get(job_id, (0, 1, "", ""))
            self._job_progress[job_id] = (
                current[1],
                current[1],
                "Run cancelled" if result.cancelled else "Run complete",
                result_text,
            )
            if self._selected_job_id == job_id:
                self._batch_result = result
                self._show_job_results(job_id)
            self._refresh_queue_table(select_job_id=job_id)
            if warning:
                self.validation_label.setText(f"Keychain cleanup warning: {warning}")
            return
        self._batch_result = result
        if self._operation == "connection_test":
            self.totals_label.setText(
                f"{result.successes} passed, {result.failures} failed, "
                f"{result.cancelled} cancelled"
            )
            self.status_label.setText(
                "Connection test complete"
                if not result.cancelled
                else "Connection test cancelled"
            )
        else:
            self.totals_label.setText(
                f"{result.successes} succeeded, {result.failures} failed, "
                f"{result.cancelled} cancelled"
            )
            self.status_label.setText(
                "Run complete" if not result.cancelled else "Run cancelled"
            )
        self.open_output_button.setEnabled(result.run_directory.is_dir())
        self.open_summary_button.setEnabled(result.summary_path.is_file())

    @Slot(str)
    def _run_failed(self, message: str) -> None:
        if self._operation == "run" and self._active_job_id:
            job_id = self._active_job_id
            safe_message = self._safe_history_message(job_id, message)
            self._finalize_audit_record(job_id, "failed", safe_message)
            warning = self.queue_controller.mark_terminal(job_id, "failed", message)
            current = self._job_progress.get(job_id, (0, 1, "", ""))
            self._job_progress[job_id] = (
                current[0], current[1], "Run failed", message
            )
            self._refresh_queue_table(select_job_id=job_id)
            if self._selected_job_id == job_id:
                self.status_label.setText("Run failed")
                self.totals_label.setText(message)
            if warning:
                self.validation_label.setText(f"Keychain cleanup warning: {warning}")
            return
        title = (
            "Connection test failed"
            if self._operation == "connection_test"
            else "Run failed"
        )
        self.status_label.setText(title)
        QMessageBox.critical(self, title, message)

    @Slot()
    def _thread_finished(self) -> None:
        finished_operation = self._operation
        self._running = False
        self._set_running_ui(False)
        self._token = None
        self._worker = None
        self._operation = None
        self._active_job_id = None
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.deleteLater()
        if self._close_after_run:
            self._close_after_run = False
            self._closing = True
            QTimer.singleShot(0, self.close)
        elif finished_operation == "run":
            QTimer.singleShot(0, self._queue_tick)

    def _selected_queue_job_id(self) -> Optional[str]:
        row = self.queue_table.currentRow()
        if row < 0:
            return None
        item = self.queue_table.item(row, 0)
        value = item.data(Qt.ItemDataRole.UserRole) if item else None
        return str(value) if value else None

    def _refresh_queue_table(self, select_job_id: Optional[str] = None) -> None:
        if not hasattr(self, "queue_table"):
            return
        selected = select_job_id or self._selected_queue_job_id()
        self.queue_table.blockSignals(True)
        try:
            self.queue_table.setRowCount(0)
            selected_row = -1
            for row, job in enumerate(self.queue_controller.jobs):
                self.queue_table.insertRow(row)
                order_item = QTableWidgetItem(str(row + 1))
                order_item.setData(Qt.ItemDataRole.UserRole, job.job_id)
                scheduled = (
                    job.scheduled_utc.astimezone().strftime("%Y-%m-%d %H:%M")
                    if job.scheduled_utc
                    else "Ready now"
                )
                status = job.status.replace("_", " ").title()
                values = (
                    order_item,
                    QTableWidgetItem(job.name),
                    QTableWidgetItem(job.device_type),
                    QTableWidgetItem(
                        f"{job.valid_devices} valid"
                        + (f", {job.invalid_devices} invalid" if job.invalid_devices else "")
                    ),
                    QTableWidgetItem(str(job.command_count)),
                    QTableWidgetItem(scheduled),
                    QTableWidgetItem(status),
                    QTableWidgetItem(job.result),
                )
                for column, item in enumerate(values):
                    self.queue_table.setItem(row, column, item)
                if job.job_id == selected:
                    selected_row = row
            if selected_row >= 0:
                self.queue_table.selectRow(selected_row)
        finally:
            self.queue_table.blockSignals(False)
        self._update_queue_controls()

    @Slot()
    def _queue_selection_changed(self) -> None:
        job_id = self._selected_queue_job_id()
        if job_id:
            self._show_job_results(job_id)
        self._update_queue_controls()

    @Slot(int, int)
    def _queue_double_clicked(self, row: int, _column: int) -> None:
        item = self.queue_table.item(row, 0)
        job_id = item.data(Qt.ItemDataRole.UserRole) if item else None
        if job_id:
            self._show_job_results(str(job_id))
            self.results_tabs.setCurrentIndex(1)

    def _update_queue_controls(self) -> None:
        if not hasattr(self, "pause_queue_button"):
            return
        self.pause_queue_button.setText(
            "Resume Queue" if self.queue_controller.paused else "Pause Queue"
        )
        job = self.queue_controller.job(self._selected_queue_job_id())
        editable = bool(job and job.status in EDITABLE_STATUSES)
        self.move_up_button.setEnabled(editable)
        self.move_down_button.setEnabled(editable)
        self.edit_batch_button.setEnabled(editable and self._editing_job_id is None)
        self.reschedule_button.setEnabled(editable)
        self.retry_button.setEnabled(bool(job and job.status in {"blocked", "interrupted"}))
        self.remove_batch_button.setEnabled(bool(job and job.status != "running"))
        self.clear_completed_button.setEnabled(
            bool(self.queue_controller.completed_jobs())
        )

    @Slot()
    def toggle_queue_pause(self) -> None:
        self.queue_controller.paused = not self.queue_controller.paused
        self._update_queue_controls()
        self._refresh_queue_table()
        if not self.queue_controller.paused:
            QTimer.singleShot(0, self._queue_tick)

    def move_selected_batch(self, direction: int) -> None:
        job_id = self._selected_queue_job_id()
        if not job_id:
            return
        try:
            self.queue_controller.move(job_id, direction)
        except QueueStorageError as exc:
            QMessageBox.critical(self, "Could not move batch", str(exc))
            return
        self._refresh_queue_table(select_job_id=job_id)

    @Slot()
    def edit_selected_batch(self) -> None:
        job_id = self._selected_queue_job_id()
        if not job_id:
            return
        if any(
            (
                self.inventory_text_edit.toPlainText().strip(),
                self.exec_commands_edit.toPlainText().strip(),
                self.config_commands_edit.toPlainText().strip(),
                self.username_edit.text(),
                self.password_edit.text(),
            )
        ):
            answer = QMessageBox.question(
                self,
                "Replace current form?",
                "Editing this queued batch will replace the current form contents.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        try:
            payload = self.queue_controller.payload(job_id)
        except QueueStorageError as exc:
            QMessageBox.critical(self, "Could not edit batch", str(exc))
            return
        self._editing_connection_settings = Settings(
            username=payload.username,
            password=payload.password,
            enable_secret=payload.enable_secret,
            port=payload.port,
            timeout=payload.timeout,
            device_type=payload.device_type,
        )
        self._loading_settings = True
        try:
            self._switch_to_manual(clear_credentials=False)
            self.username_edit.setText(payload.username)
            self.password_edit.setText(payload.password)
            self.device_type_combo.setCurrentText(payload.device_type)
            self.inventory_text_edit.setPlainText(payload.inventory_text)
            self.exec_commands_edit.setPlainText(payload.exec_commands_text)
            self.config_commands_edit.setPlainText(payload.config_commands_text)
            self.output_edit.setText(payload.output_dir)
        finally:
            self._loading_settings = False
        self._credential_file = None
        self._editing_job_id = job_id
        self._prepared = None
        self.save_edit_button.show()
        self.cancel_edit_button.show()
        self.validation_label.setText("Editing queued batch; validate before saving changes.")
        self._set_validated(False)
        self._update_queue_controls()

    @Slot()
    def save_batch_edit(self) -> None:
        job_id = self._editing_job_id
        if not job_id:
            return
        if not self.validate_inputs(show_success=False) or self._prepared is None:
            QMessageBox.critical(self, "Cannot save batch", "Correct the validation errors first.")
            return
        answer = QMessageBox.warning(
            self,
            "Confirm batch changes",
            f"Replace the queued snapshot with {len(self._prepared.commands)} command(s) "
            f"for {len(self._prepared.valid_entries)} valid device(s)?\n\n"
            "The live changes have no automatic rollback.",
            QMessageBox.StandardButton.Apply | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Apply:
            return
        try:
            payload = BatchPayload.from_prepared(
                self._prepared,
                self.profile_combo.currentText() or MANUAL_PROFILE_TEXT,
            )
            self.queue_controller.replace_payload(job_id, payload, self._prepared)
        except QueueStorageError as exc:
            QMessageBox.critical(self, "Could not save batch", str(exc))
            return
        self._job_prepared[job_id] = self._prepared
        self._initialize_job_rows(job_id, self._prepared)
        self._finish_batch_edit()
        self._refresh_queue_table(select_job_id=job_id)
        self._show_job_results(job_id)

    @Slot()
    def cancel_batch_edit(self) -> None:
        self._finish_batch_edit()
        self.validation_label.setText("Queued batch was not changed.")

    def _finish_batch_edit(self) -> None:
        self._editing_job_id = None
        self._editing_connection_settings = None
        self.save_edit_button.hide()
        self.cancel_edit_button.hide()
        self._set_validated(self._prepared is not None)
        self._update_queue_controls()

    @Slot()
    def reschedule_selected_batch(self) -> None:
        job_id = self._selected_queue_job_id()
        job = self.queue_controller.job(job_id)
        if job is None:
            return
        dialog = ScheduleDialog(job.scheduled_utc, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            self.queue_controller.reschedule(job.job_id, dialog.selected_utc())
        except QueueStorageError as exc:
            QMessageBox.critical(self, "Could not reschedule batch", str(exc))
            return
        self._refresh_queue_table(select_job_id=job.job_id)

    @Slot()
    def retry_selected_batch(self) -> None:
        job_id = self._selected_queue_job_id()
        job = self.queue_controller.job(job_id)
        if job is None:
            return
        warning = (
            "This batch may already have applied some commands. Retrying can repeat changes."
            if job.status == "interrupted"
            else "Retry this blocked batch?"
        )
        answer = QMessageBox.warning(
            self,
            "Retry batch?",
            warning,
            QMessageBox.StandardButton.Apply | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Apply:
            return
        try:
            self.queue_controller.payload(job.job_id)
            self.queue_controller.retry(job.job_id)
        except QueueStorageError as exc:
            QMessageBox.critical(self, "Could not retry batch", str(exc))
            return
        self._refresh_queue_table(select_job_id=job.job_id)
        QTimer.singleShot(0, self._queue_tick)

    @Slot()
    def remove_selected_batch(self) -> None:
        job_id = self._selected_queue_job_id()
        job = self.queue_controller.job(job_id)
        if job is None:
            return
        answer = QMessageBox.question(
            self,
            "Remove queued batch?",
            "Remove this batch from the queue? Output files and credential profile files "
            "will not be deleted.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.queue_controller.remove(job.job_id)
        except QueueStorageError as exc:
            QMessageBox.critical(self, "Could not remove batch", str(exc))
            return
        self._job_prepared.pop(job.job_id, None)
        self._job_results.pop(job.job_id, None)
        self._job_rows.pop(job.job_id, None)
        self._job_progress.pop(job.job_id, None)
        self._selected_job_id = None
        self._refresh_queue_table()
        self._update_queue_controls()

    @Slot()
    def clear_completed_batches(self) -> None:
        completed = self.queue_controller.completed_jobs()
        if not completed:
            return
        answer = QMessageBox.question(
            self,
            "Clear completed runs?",
            f"Remove {len(completed)} completed run(s) from the queue history?\n\n"
            "Running, queued, and scheduled batches will remain. Output files, "
            "transcripts, and summaries will not be deleted.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            removed_ids = self.queue_controller.clear_completed()
        except QueueStorageError as exc:
            QMessageBox.critical(self, "Could not clear completed runs", str(exc))
            return
        for job_id in removed_ids:
            self._job_prepared.pop(job_id, None)
            self._job_results.pop(job_id, None)
            self._job_rows.pop(job_id, None)
            self._job_progress.pop(job_id, None)
        if self._selected_job_id in removed_ids:
            self._selected_job_id = None
            self._batch_result = None
            self.device_table.setRowCount(0)
            self._rows.clear()
            self.progress_bar.setValue(0)
            self.status_label.setText("No batch selected")
            self.totals_label.setText("0 succeeded, 0 failed, 0 cancelled")
            self.open_output_button.setEnabled(False)
            self.open_summary_button.setEnabled(False)
        self._refresh_queue_table(select_job_id=self._selected_job_id)
        self._update_queue_controls()

    @Slot(int)
    def _results_tab_changed(self, index: int) -> None:
        if index == self.results_tabs.indexOf(self.history_tab):
            self._ensure_history_ui()

    def _ensure_history_ui(self) -> None:
        if self._history_ui_built:
            return
        self._history_ui_built = True
        self.history_layout.removeWidget(self.history_placeholder)
        self.history_placeholder.deleteLater()
        if self.history_store is None:
            unavailable = QLabel(
                "Audit history is unavailable.\n\n" + self._history_startup_warning
            )
            unavailable.setWordWrap(True)
            unavailable.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.history_layout.addWidget(unavailable, 1)
            return

        from PySide6.QtCharts import QChartView

        filters = QGridLayout()
        self.history_search_edit = QLineEdit()
        self.history_search_edit.setPlaceholderText("Search batch names…")
        self.history_range_combo = QComboBox()
        for text, days in (
            ("Last 7 days", 7),
            ("Last 30 days", 30),
            ("Last 90 days", 90),
            ("Last year", 365),
            ("All time", None),
        ):
            self.history_range_combo.addItem(text, days)
        self.history_range_combo.setCurrentIndex(1)
        self.history_status_combo = QComboBox()
        self.history_status_combo.addItem("All statuses", "")
        for status in (
            "succeeded",
            "completed_with_failures",
            "failed",
            "cancelled",
            "interrupted",
            "running",
        ):
            self.history_status_combo.addItem(status.replace("_", " ").title(), status)
        self.history_source_combo = QComboBox()
        self.history_source_combo.addItem("Manual and scheduled", "")
        self.history_source_combo.addItem("Manual", "manual")
        self.history_source_combo.addItem("Scheduled", "scheduled")
        self.history_device_combo = QComboBox()
        self.history_profile_combo = QComboBox()
        filters.addWidget(QLabel("Range"), 0, 0)
        filters.addWidget(self.history_range_combo, 0, 1)
        filters.addWidget(QLabel("Status"), 0, 2)
        filters.addWidget(self.history_status_combo, 0, 3)
        filters.addWidget(QLabel("Source"), 0, 4)
        filters.addWidget(self.history_source_combo, 0, 5)
        filters.addWidget(QLabel("Device Type"), 1, 0)
        filters.addWidget(self.history_device_combo, 1, 1)
        filters.addWidget(QLabel("Profile"), 1, 2)
        filters.addWidget(self.history_profile_combo, 1, 3)
        filters.addWidget(self.history_search_edit, 1, 4, 1, 2)
        self.history_layout.addLayout(filters)

        cards = QHBoxLayout()
        self.history_runs_value = QLabel("0")
        self.history_success_value = QLabel("0")
        self.history_failure_value = QLabel("0")
        self.history_rate_value = QLabel("0.0%")
        for title, value in (
            ("Total Runs", self.history_runs_value),
            ("Successful Devices", self.history_success_value),
            ("Failed Devices", self.history_failure_value),
            ("Device Success Rate", self.history_rate_value),
        ):
            group = QGroupBox(title)
            card_layout = QVBoxLayout(group)
            value.setAlignment(Qt.AlignmentFlag.AlignCenter)
            value.setStyleSheet("font-size: 20px; font-weight: 600;")
            card_layout.addWidget(value)
            cards.addWidget(group)
        self.history_layout.addLayout(cards)

        charts = QHBoxLayout()
        self.history_line_view = QChartView()
        self.history_pie_view = QChartView()
        self.history_line_view.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.history_pie_view.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.history_line_view.setMinimumHeight(220)
        self.history_pie_view.setMinimumHeight(220)
        charts.addWidget(self.history_line_view, 3)
        charts.addWidget(self.history_pie_view, 2)
        self.history_layout.addLayout(charts)

        self.history_empty_label = QLabel("No audit history matches these filters.")
        self.history_empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.history_layout.addWidget(self.history_empty_label)
        self.history_table = QTableWidget(0, 9)
        self.history_table.setHorizontalHeaderLabels(
            [
                "Started",
                "Batch",
                "Source",
                "Profile",
                "Device Type",
                "Devices",
                "Duration",
                "Status",
                "Result",
            ]
        )
        self.history_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.history_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.history_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.history_table.verticalHeader().setVisible(False)
        history_header = self.history_table.horizontalHeader()
        for column in (0, 2, 3, 4, 5, 6, 7):
            history_header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        history_header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        history_header.setSectionResizeMode(8, QHeaderView.ResizeMode.Stretch)
        self.history_table.itemSelectionChanged.connect(self._history_selection_changed)
        self.history_table.cellDoubleClicked.connect(
            lambda _row, _column: self.view_history_results()
        )
        self.history_layout.addWidget(self.history_table, 1)

        paging = QHBoxLayout()
        self.history_previous_button = QPushButton("Previous")
        self.history_next_button = QPushButton("Next")
        self.history_page_label = QLabel()
        self.history_previous_button.clicked.connect(lambda: self._change_history_page(-1))
        self.history_next_button.clicked.connect(lambda: self._change_history_page(1))
        paging.addWidget(self.history_previous_button)
        paging.addWidget(self.history_next_button)
        paging.addWidget(self.history_page_label)
        paging.addStretch()
        self.history_layout.addLayout(paging)

        actions = QGridLayout()
        self.history_results_button = QPushButton("View Device Results")
        self.history_commands_button = QPushButton("View Commands")
        self.history_output_button = QPushButton("Open Output Folder")
        self.history_summary_button = QPushButton("Open Summary")
        self.history_remove_button = QPushButton("Remove from History")
        self.history_delete_outputs_button = QPushButton("Delete History && Outputs")
        self.history_clear_button = QPushButton("Clear History")
        self.history_remove_button.setStyleSheet("QPushButton { color: #c62828; }")
        self.history_delete_outputs_button.setStyleSheet(
            "QPushButton { color: #c62828; }"
        )
        self.history_clear_button.setStyleSheet("QPushButton { color: #c62828; }")
        self.history_results_button.clicked.connect(self.view_history_results)
        self.history_commands_button.clicked.connect(self.view_history_commands)
        self.history_output_button.clicked.connect(self.open_history_output)
        self.history_summary_button.clicked.connect(self.open_history_summary)
        self.history_remove_button.clicked.connect(self.remove_history_record)
        self.history_delete_outputs_button.clicked.connect(
            self.delete_history_outputs
        )
        self.history_clear_button.clicked.connect(self.clear_history)
        for index, button in enumerate(
            (
                self.history_results_button,
                self.history_commands_button,
                self.history_output_button,
                self.history_summary_button,
                self.history_remove_button,
                self.history_delete_outputs_button,
                self.history_clear_button,
            )
        ):
            actions.addWidget(button, index // 4, index % 4)
        self.history_layout.addLayout(actions)

        self.history_search_edit.textChanged.connect(self._history_filters_changed)
        for combo in (
            self.history_range_combo,
            self.history_status_combo,
            self.history_source_combo,
            self.history_device_combo,
            self.history_profile_combo,
        ):
            combo.currentIndexChanged.connect(self._history_filters_changed)
        self._refresh_history_filter_values()
        self._refresh_history()

    def _refresh_history_filter_values(self) -> None:
        if not self._history_ui_built or self.history_store is None or not hasattr(self, "history_device_combo"):
            return
        for combo, field, all_text in (
            (self.history_device_combo, "device_type", "All device types"),
            (self.history_profile_combo, "profile_label", "All profiles"),
        ):
            selected = combo.currentData() if combo.count() else ""
            combo.blockSignals(True)
            combo.clear()
            combo.addItem(all_text, "")
            try:
                values = self.history_store.distinct(field)
            except HistoryError as exc:
                self.validation_label.setText(f"Audit history warning: {exc}")
                values = []
            for value in values:
                combo.addItem(value, value)
            index = combo.findData(selected)
            combo.setCurrentIndex(index if index >= 0 else 0)
            combo.blockSignals(False)

    @Slot()
    def _history_filters_changed(self) -> None:
        self._history_page = 0
        self._refresh_history()

    def _history_filter(self) -> HistoryFilter:
        days = self.history_range_combo.currentData()
        since = self._clock() - timedelta(days=int(days)) if days else None
        return HistoryFilter(
            since_utc=since,
            search=self.history_search_edit.text(),
            status=str(self.history_status_combo.currentData() or ""),
            source=str(self.history_source_combo.currentData() or ""),
            device_type=str(self.history_device_combo.currentData() or ""),
            profile_label=str(self.history_profile_combo.currentData() or ""),
        )

    def _history_granularity(self) -> str:
        days = self.history_range_combo.currentData()
        if days is None:
            return "monthly"
        return "weekly" if int(days) >= 365 else "daily"

    def _refresh_history_if_loaded(self) -> None:
        if self._history_ui_built and hasattr(self, "history_table"):
            self._refresh_history_filter_values()
            self._refresh_history()

    def _refresh_history(self) -> None:
        if self.history_store is None or not hasattr(self, "history_table"):
            return
        try:
            filters = self._history_filter()
            records, total = self.history_store.list(
                filters, limit=100, offset=self._history_page * 100
            )
            stats = self.history_store.stats(filters)
            timeline = self.history_store.timeline(
                filters, self._history_granularity()
            )
            statuses = self.history_store.status_counts(filters)
        except HistoryError as exc:
            self.validation_label.setText(f"Audit history warning: {exc}")
            return
        self._history_records = {record.job_id: record for record in records}
        self.history_runs_value.setText(str(stats.runs))
        self.history_success_value.setText(str(stats.successes))
        self.history_failure_value.setText(str(stats.failures))
        self.history_rate_value.setText(f"{stats.success_rate:.1f}%")
        self._update_history_charts(timeline, statuses)

        self.history_table.blockSignals(True)
        self.history_table.setRowCount(0)
        for row, record in enumerate(records):
            self.history_table.insertRow(row)
            started_item = QTableWidgetItem(
                record.started_utc.astimezone().strftime("%Y-%m-%d %H:%M")
            )
            started_item.setData(Qt.ItemDataRole.UserRole, record.job_id)
            duration = self._format_duration(record.duration_seconds)
            result_devices = (
                record.success_count + record.failure_count + record.cancelled_count
            )
            devices = result_devices or record.valid_devices + record.invalid_devices
            result_text = record.result_text
            if record.summary_path and not Path(record.summary_path).is_file():
                result_text = (result_text + " — Output unavailable").strip(" —")
            values = (
                started_item,
                QTableWidgetItem(record.batch_name),
                QTableWidgetItem(record.source.title()),
                QTableWidgetItem(record.profile_label),
                QTableWidgetItem(record.device_type),
                QTableWidgetItem(str(devices)),
                QTableWidgetItem(duration),
                QTableWidgetItem(record.status.replace("_", " ").title()),
                QTableWidgetItem(result_text),
            )
            for column, item in enumerate(values):
                self.history_table.setItem(row, column, item)
        self.history_table.blockSignals(False)
        self.history_empty_label.setVisible(total == 0)
        pages = max(1, (total + 99) // 100)
        if self._history_page >= pages:
            self._history_page = pages - 1
            self._refresh_history()
            return
        self.history_page_label.setText(
            f"Page {self._history_page + 1} of {pages} — {total} run(s)"
        )
        self.history_previous_button.setEnabled(self._history_page > 0)
        self.history_next_button.setEnabled(self._history_page + 1 < pages)
        try:
            all_history_count = self.history_store.stats(HistoryFilter()).runs
        except HistoryError:
            all_history_count = total
        self.history_clear_button.setEnabled(all_history_count > 0)
        self._history_selection_changed()

    @staticmethod
    def _format_duration(seconds: Optional[int]) -> str:
        if seconds is None:
            return "In progress"
        minutes, remaining = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours}h {minutes}m"
        if minutes:
            return f"{minutes}m {remaining}s"
        return f"{remaining}s"

    def _update_history_charts(self, timeline, statuses: dict[str, int]) -> None:
        from PySide6.QtCharts import (
            QBarCategoryAxis,
            QChart,
            QLineSeries,
            QPieSeries,
            QValueAxis,
        )

        chart = QChart()
        chart.setTitle("Successful and failed devices over time")
        success_series = QLineSeries()
        success_series.setName("Succeeded")
        success_series.setColor(QColor("#2e7d32"))
        failure_series = QLineSeries()
        failure_series.setName("Failed")
        failure_series.setColor(QColor("#c62828"))
        labels: list[str] = []
        maximum = 1
        for index, bucket in enumerate(timeline):
            labels.append(bucket.label)
            success_series.append(index, bucket.successes)
            failure_series.append(index, bucket.failures)
            maximum = max(maximum, bucket.successes, bucket.failures)
        chart.addSeries(success_series)
        chart.addSeries(failure_series)
        x_axis = QBarCategoryAxis()
        x_axis.append(labels or ["No data"])
        y_axis = QValueAxis()
        y_axis.setLabelFormat("%d")
        y_axis.setRange(0, maximum)
        y_axis.setTickCount(min(6, maximum + 1))
        chart.addAxis(x_axis, Qt.AlignmentFlag.AlignBottom)
        chart.addAxis(y_axis, Qt.AlignmentFlag.AlignLeft)
        success_series.attachAxis(x_axis)
        success_series.attachAxis(y_axis)
        failure_series.attachAxis(x_axis)
        failure_series.attachAxis(y_axis)
        self.history_line_view.setChart(chart)

        pie_chart = QChart()
        pie_chart.setTitle("Run status")
        pie = QPieSeries()
        pie.setHoleSize(0.45)
        colors = {
            "succeeded": "#2e7d32",
            "completed_with_failures": "#f9a825",
            "failed": "#c62828",
            "cancelled": "#616161",
            "interrupted": "#6a1b9a",
            "running": "#1565c0",
        }
        for status, count in statuses.items():
            if count:
                piece = pie.append(status.replace("_", " ").title(), count)
                piece.setColor(QColor(colors.get(status, "#546e7a")))
        pie_chart.addSeries(pie)
        self.history_pie_view.setChart(pie_chart)

    def _change_history_page(self, direction: int) -> None:
        self._history_page = max(0, self._history_page + direction)
        self._refresh_history()

    def _selected_history_record(self) -> Optional[HistoryRecord]:
        if not hasattr(self, "history_table"):
            return None
        row = self.history_table.currentRow()
        item = self.history_table.item(row, 0) if row >= 0 else None
        job_id = item.data(Qt.ItemDataRole.UserRole) if item else None
        return self._history_records.get(str(job_id)) if job_id else None

    @Slot()
    def _history_selection_changed(self) -> None:
        if not hasattr(self, "history_results_button"):
            return
        record = self._selected_history_record()
        has_record = record is not None
        summary_exists = bool(record and Path(record.summary_path).is_file())
        output_exists = bool(record and Path(record.run_directory).is_dir())
        self.history_results_button.setEnabled(summary_exists)
        self.history_commands_button.setEnabled(has_record)
        self.history_output_button.setEnabled(output_exists)
        self.history_summary_button.setEnabled(summary_exists)
        self.history_remove_button.setEnabled(has_record)
        can_delete = False
        if record:
            try:
                validate_output_deletion(record)
                can_delete = True
            except HistoryError:
                pass
        self.history_delete_outputs_button.setEnabled(can_delete)

    @Slot()
    def view_history_results(self) -> None:
        record = self._selected_history_record()
        if record is None:
            return
        try:
            results = read_summary(Path(record.summary_path))
        except HistoryError as exc:
            QMessageBox.critical(self, "Output unavailable", str(exc))
            return
        self._selected_job_id = None
        self.device_table.setRowCount(0)
        self._rows.clear()
        for row, result in enumerate(results):
            self.device_table.insertRow(row)
            self._rows[result.address] = row
            self._set_cell(row, 0, result.address)
            self._set_cell(row, 1, result.status.title())
            stage = result.failed_section
            if result.failed_line:
                stage = f"{stage} / {result.failed_line}" if stage else result.failed_line
            self._set_cell(row, 2, stage)
            self._set_cell(row, 3, result.error or result.status.title())
            self._set_cell(row, 4, result.transcript_file)
        self._batch_result = BatchResult(
            results, Path(record.run_directory), Path(record.summary_path)
        )
        self.progress_bar.setRange(0, max(1, len(results)))
        self.progress_bar.setValue(len(results))
        self.status_label.setText(record.status.replace("_", " ").title())
        self.totals_label.setText(record.result_text)
        self.open_output_button.setEnabled(Path(record.run_directory).is_dir())
        self.open_summary_button.setEnabled(Path(record.summary_path).is_file())
        self.results_tabs.setCurrentIndex(1)

    @Slot()
    def view_history_commands(self) -> None:
        record = self._selected_history_record()
        if record is None:
            return
        answer = QMessageBox.warning(
            self,
            "View sensitive command snapshot?",
            "Commands may contain literal passwords or other sensitive values.",
            QMessageBox.StandardButton.Open | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Open:
            return
        try:
            snapshot = self.history_command_store.get(record.job_id)
        except HistoryError as exc:
            QMessageBox.critical(self, "Commands unavailable", str(exc))
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Commands — {record.batch_name}")
        dialog.resize(720, 520)
        layout = QVBoxLayout(dialog)
        for section, title in (("exec", "Exec Commands"), ("config", "Config Commands")):
            layout.addWidget(QLabel(title))
            editor = QPlainTextEdit()
            editor.setReadOnly(True)
            editor.setPlainText(
                "\n".join(
                    command.text
                    for command in snapshot.commands
                    if command.section == section
                )
            )
            layout.addWidget(editor, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()

    @Slot()
    def open_history_output(self) -> None:
        record = self._selected_history_record()
        if record and Path(record.run_directory).is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(record.run_directory))

    @Slot()
    def open_history_summary(self) -> None:
        record = self._selected_history_record()
        if record and Path(record.summary_path).is_file():
            QDesktopServices.openUrl(QUrl.fromLocalFile(record.summary_path))

    @Slot()
    def remove_history_record(self) -> None:
        record = self._selected_history_record()
        if record is None or self.history_store is None:
            return
        answer = QMessageBox.question(
            self,
            "Remove audit history record?",
            "Remove this record and its encrypted command snapshot?\n\n"
            "Output files will not be deleted.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.history_command_store.delete(record.job_id)
            self.history_store.remove(record.job_id)
        except HistoryError as exc:
            QMessageBox.critical(self, "Could not remove history", str(exc))
            return
        self._refresh_history_filter_values()
        self._refresh_history()

    @Slot()
    def delete_history_outputs(self) -> None:
        record = self._selected_history_record()
        if record is None or self.history_store is None:
            return
        try:
            run_directory = validate_output_deletion(record)
        except HistoryError as exc:
            QMessageBox.critical(self, "Cannot delete output", str(exc))
            return
        answer = QMessageBox.warning(
            self,
            "Delete history and output files?",
            f"Permanently delete this audit record and all files in:\n\n{run_directory}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            delete_output_directory(record)
            self.history_command_store.delete(record.job_id)
            self.history_store.remove(record.job_id)
        except HistoryError as exc:
            self._refresh_history()
            QMessageBox.critical(self, "Could not delete history output", str(exc))
            return
        self._refresh_history_filter_values()
        self._refresh_history()

    @Slot()
    def clear_history(self) -> None:
        if self.history_store is None:
            return
        try:
            records, total = self.history_store.list(
                HistoryFilter(), limit=1_000_000
            )
        except HistoryError as exc:
            QMessageBox.critical(self, "Could not read history", str(exc))
            return
        if not total:
            return
        answer = QMessageBox.question(
            self,
            "Clear audit history?",
            f"Remove all {total} history records and encrypted command snapshots?\n\n"
            "Output files will not be deleted.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            for record in records:
                self.history_command_store.delete(record.job_id)
            self.history_store.clear()
        except HistoryError as exc:
            QMessageBox.critical(self, "Could not clear history", str(exc))
            return
        self._refresh_history_filter_values()
        self._refresh_history()

    @Slot(str)
    def _show_sleep_warning(self, message: str) -> None:
        self.validation_label.setText(message)

    @Slot()
    def open_output_folder(self) -> None:
        if self._batch_result and self._batch_result.run_directory.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._batch_result.run_directory)))

    @Slot()
    def open_summary(self) -> None:
        if self._batch_result and self._batch_result.summary_path.is_file():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._batch_result.summary_path)))

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._closing:
            self._save_settings()
            if hasattr(self, "scheduler_timer"):
                self.scheduler_timer.stop()
            if hasattr(self, "sleep_inhibitor"):
                self.sleep_inhibitor.set_required(False)
            event.accept()
            return
        if self._running:
            answer = QMessageBox.question(
                self,
                "Stop active operation?",
                "The app will stop at the next safe checkpoint and then close. "
                "Any remaining queued batches will be restored paused next time.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer == QMessageBox.StandardButton.Yes:
                self._close_after_run = True
                self.cancel_run()
            event.ignore()
            return
        if self.queue_controller.pending_jobs():
            answer = QMessageBox.question(
                self,
                "Close with queued batches?",
                "Queued and scheduled batches will not run while the application is closed. "
                "They will be restored in a paused state next time.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        self._save_settings()
        self._closing = True
        if hasattr(self, "scheduler_timer"):
            self.scheduler_timer.stop()
        if hasattr(self, "sleep_inhibitor"):
            self.sleep_inhibitor.set_required(False)
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
    def _export_inventory(self) -> None:
        text = self.inventory_text_edit.toPlainText()
        if not text.strip():
            QMessageBox.critical(self, "Device export failed", "There are no devices to export.")
            return
        path = self._select_export_file(
            "Export devices",
            self._last_inventory_import,
            "devices.txt",
        )
        if path is not None and self._write_export_file(path, text):
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
    def _export_sectioned_commands(self) -> None:
        exec_text = self.exec_commands_edit.toPlainText()
        config_text = self.config_commands_edit.toPlainText()
        try:
            parse_inline_commands(exec_text, config_text)
        except ConfigurationError as exc:
            QMessageBox.critical(self, "Command export failed", str(exc))
            return

        contents = f"[exec]\n{exec_text.rstrip()}\n\n[config]\n{config_text.rstrip()}\n"
        path = self._select_export_file(
            "Export [exec]/[config] command file",
            self._last_command_import,
            "commands.txt",
        )
        if path is not None and self._write_export_file(path, contents):
            self._last_command_import = str(path)

    def _select_import_file(self, title: str, previous: str) -> Optional[Path]:
        start = str(Path(previous).parent) if previous else str(Path.home())
        selected, _ = QFileDialog.getOpenFileName(
            self,
            title,
            start,
            "Text files (*.txt);;All files (*)",
        )
        return Path(selected) if selected else None

    def _select_export_file(
        self,
        title: str,
        previous: str,
        default_name: str,
    ) -> Optional[Path]:
        start = Path(previous).parent if previous else Path.home()
        selected, _ = QFileDialog.getSaveFileName(
            self,
            title,
            str(start / default_name),
            "Text files (*.txt);;All files (*)",
        )
        return Path(selected) if selected else None

    def _write_export_file(self, path: Path, text: str) -> bool:
        try:
            path.write_text(text, encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            QMessageBox.critical(self, "Export failed", f"Could not write {path}:\n{exc}")
            return False
        return True

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
    if args.smoke_test:
        import netmiko  # noqa: F401 - verifies the packaged SSH dependency is present.
        import keyring.backends.macOS  # noqa: F401 - verifies packaged Keychain support.
        import PySide6.QtCharts  # noqa: F401 - verifies packaged chart support.

        with tempfile.TemporaryDirectory() as temporary:
            smoke_settings = QSettings(
                str(Path(temporary) / "smoke-settings.ini"),
                QSettings.Format.IniFormat,
            )
            window = MainWindow(
                smoke_settings,
                secret_store=MemorySecretStore(),
                history_store=HistoryStore(Path(temporary) / "history.sqlite3"),
                history_command_store=MemoryHistoryCommandStore(),
            )
            window.show()
            app.processEvents()
            window._ensure_history_ui()
            app.processEvents()
            window.close()
            app.processEvents()
        return 0
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
