#!/usr/bin/env python3
"""PySide6 desktop interface for the SSH batch command runner."""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional
from uuid import uuid4

from PySide6.QtCore import (
    QDateTime,
    QEvent,
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
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QBrush,
    QCloseEvent,
    QColor,
    QCursor,
    QDesktopServices,
    QPainter,
    QPalette,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
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
    QScrollArea,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTimeEdit,
    QToolTip,
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

from command_dictionary import COMMAND_ENTRIES, CommandEntry, search_commands

from config_converter import (
    DISPOSITION_IGNORED,
    DISPOSITION_REVIEW,
    DISPOSITION_UNMAPPED,
    VENDOR_CISCO,
    VENDOR_HUAWEI,
    ConversionIssue,
    ConversionResult,
    analyze_target_text,
    convert_text,
    detect_vendor,
    opposite_vendor,
    render_sectioned_commands,
    vendor_label,
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
THEME_SYSTEM = "system"
THEME_LIGHT = "light"
THEME_DARK = "dark"
THEME_MODES = (THEME_SYSTEM, THEME_LIGHT, THEME_DARK)
RESULTS_ATTENTION_INTERVAL_MS = 500
RESULTS_ATTENTION_TICKS = 6
HISTORY_TREND_DEVICES = "devices"
HISTORY_TREND_DURATION = "duration"


def normalize_theme_mode(value: object) -> str:
    """Return a supported persisted theme name, defaulting safely to System."""
    mode = str(value or "").strip().lower()
    return mode if mode in THEME_MODES else THEME_SYSTEM


def _forced_theme_palette(mode: str) -> QPalette:
    """Build a complete application palette without replacing the native style."""
    dark = mode == THEME_DARK
    colors = {
        QPalette.ColorRole.Window: "#202124" if dark else "#f5f5f5",
        QPalette.ColorRole.WindowText: "#f1f3f4" if dark else "#202124",
        QPalette.ColorRole.Base: "#151515" if dark else "#ffffff",
        QPalette.ColorRole.AlternateBase: "#292a2d" if dark else "#eeeeee",
        QPalette.ColorRole.ToolTipBase: "#303134" if dark else "#ffffdc",
        QPalette.ColorRole.ToolTipText: "#f1f3f4" if dark else "#202124",
        QPalette.ColorRole.Text: "#f1f3f4" if dark else "#202124",
        QPalette.ColorRole.Button: "#303134" if dark else "#eeeeee",
        QPalette.ColorRole.ButtonText: "#f1f3f4" if dark else "#202124",
        QPalette.ColorRole.BrightText: "#ffffff",
        QPalette.ColorRole.Link: "#8ab4f8" if dark else "#0067c0",
        QPalette.ColorRole.Highlight: "#0a84ff" if dark else "#0067c0",
        QPalette.ColorRole.HighlightedText: "#ffffff",
        QPalette.ColorRole.PlaceholderText: "#9aa0a6" if dark else "#767676",
        QPalette.ColorRole.Light: "#5f6368" if dark else "#ffffff",
        QPalette.ColorRole.Midlight: "#3c4043" if dark else "#dadce0",
        QPalette.ColorRole.Mid: "#5f6368" if dark else "#9aa0a6",
        QPalette.ColorRole.Dark: "#121212" if dark else "#5f6368",
        QPalette.ColorRole.Shadow: "#000000",
    }
    palette = QPalette()
    for role, color in colors.items():
        palette.setColor(role, QColor(color))
    disabled = QColor("#80868b" if dark else "#777777")
    for role in (
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.Text,
        QPalette.ColorRole.ButtonText,
        QPalette.ColorRole.PlaceholderText,
    ):
        palette.setColor(QPalette.ColorGroup.Disabled, role, disabled)
    return palette


def apply_application_theme(app: QApplication, mode: object) -> str:
    """Apply a theme to every Qt window while retaining the platform widget style."""
    normalized = normalize_theme_mode(mode)
    scheme = {
        THEME_SYSTEM: Qt.ColorScheme.Unknown,
        THEME_LIGHT: Qt.ColorScheme.Light,
        THEME_DARK: Qt.ColorScheme.Dark,
    }[normalized]
    app.styleHints().setColorScheme(scheme)
    if normalized == THEME_SYSTEM:
        # An unresolved palette hands color ownership back to the platform, so future
        # macOS appearance changes continue to reach the application.
        app.setPalette(QPalette())
    else:
        app.setPalette(_forced_theme_palette(normalized))
    return normalized


def _repolish_application_widgets(app: QApplication) -> None:
    """Refresh native widget styling after an application palette change."""
    for widget in app.allWidgets():
        style = widget.style()
        style.unpolish(widget)
        style.polish(widget)
        widget.update()


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
        self._theme_mode = normalize_theme_mode(
            self.settings.value("appearance/theme", THEME_SYSTEM, str)
        )
        app = QApplication.instance()
        if app is not None:
            apply_application_theme(app, self._theme_mode)
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
        self._dictionary_ui_built = False
        self.dictionary_dialog: Optional[QDialog] = None
        self._converter_ui_built = False
        self.converter_dialog: Optional[QDialog] = None
        self._converter_result: Optional[ConversionResult] = None
        self._converter_target_vendor: Optional[str] = None
        self._converter_source_stale = False
        self._converter_output_dirty = False
        self._converter_setting_output = False
        self._converter_result_is_analysis = False
        self._last_converter_import = ""
        self._last_converter_export = ""
        self._history_page = 0
        self._history_records: dict[str, HistoryRecord] = {}
        self._results_attention_pending = False
        self._results_attention_tick = 0
        self._results_attention_highlighted = False

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
        self._build_menu()
        central = QWidget(self)
        root = QVBoxLayout(central)
        self.main_tabs = QTabWidget()
        self.main_tabs.tabBar().setExpanding(True)
        self.main_tabs.tabBar().setMinimumHeight(44)

        ssh_tab = QWidget()
        ssh_layout = QVBoxLayout(ssh_tab)
        ssh_layout.setContentsMargins(0, 0, 0, 0)
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
        self.command_dictionary_button = QPushButton("Command Dictionary")
        self.command_dictionary_button.setToolTip(
            "Open the offline command dictionary in a separate window."
        )
        self.command_dictionary_button.clicked.connect(
            self._open_command_dictionary
        )
        self.config_converter_button = QPushButton("Config Converter")
        self.config_converter_button.setToolTip(
            "Convert reviewed Cisco IOS XE and Huawei VRP commands locally."
        )
        self.config_converter_button.clicked.connect(self._open_config_converter)
        command_file_buttons = QWidget()
        self.command_file_buttons = command_file_buttons
        command_file_buttons_layout = QGridLayout(command_file_buttons)
        command_file_buttons_layout.setContentsMargins(0, 0, 0, 0)
        command_file_buttons_layout.addWidget(import_sectioned_button, 0, 0, 1, 2)
        command_file_buttons_layout.addWidget(export_sectioned_button, 1, 0)
        command_file_buttons_layout.addWidget(self.clear_commands_button, 1, 1)
        command_file_buttons_layout.setColumnStretch(0, 1)
        command_file_buttons_layout.setColumnStretch(1, 1)

        self.command_inputs_container = QWidget()
        command_inputs_layout = QGridLayout(self.command_inputs_container)
        command_inputs_layout.setContentsMargins(0, 0, 0, 0)
        devices_label = QLabel("Devices")
        devices_label.setBuddy(self.inventory_text_edit)
        exec_commands_label = QLabel("Exec Commands")
        exec_commands_label.setBuddy(self.exec_commands_edit)
        config_commands_label = QLabel("Config Commands")
        config_commands_label.setBuddy(self.config_commands_edit)
        command_inputs_layout.addWidget(devices_label, 4, 0)
        command_inputs_layout.addWidget(self.inventory_text_edit, 4, 1)
        command_inputs_layout.addWidget(inventory_buttons, 4, 2)
        command_inputs_layout.addWidget(exec_commands_label, 5, 0)
        command_inputs_layout.addWidget(self.exec_commands_edit, 5, 1)
        command_inputs_layout.addWidget(config_commands_label, 6, 0)
        command_inputs_layout.addWidget(self.config_commands_edit, 6, 1)
        command_tools = QWidget()
        command_tools_layout = QVBoxLayout(command_tools)
        command_tools_layout.setContentsMargins(0, 0, 0, 0)
        command_tools_layout.addWidget(self.command_dictionary_button)
        command_tools_layout.addWidget(self.config_converter_button)
        command_inputs_layout.addWidget(
            command_tools, 5, 2, 2, 1, Qt.AlignmentFlag.AlignVCenter
        )
        self.command_tools_container = command_tools
        command_inputs_layout.addWidget(command_file_buttons, 7, 1, 1, 2)
        command_inputs_layout.setColumnStretch(1, 1)

        self.username_edit = QLineEdit()
        self.username_edit.setPlaceholderText("SSH Username")
        self.password_edit = QLineEdit()
        self.password_edit.setPlaceholderText("SSH Password")
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.profile_combo = QComboBox()
        self.manage_profiles_button = QPushButton("Manage Profiles…")
        self.manage_profiles_button.clicked.connect(self.manage_profiles)
        self.output_edit, output_row = self._path_row("Choose output folder…", self._choose_output)

        self.device_type_combo = QComboBox()
        self.device_type_combo.setEditable(True)
        self.device_type_combo.addItems(["huawei", "cisco_ios"])

        profile_label = QLabel("Profile")
        profile_label.setBuddy(self.profile_combo)
        username_label = QLabel("Username")
        username_label.setBuddy(self.username_edit)
        password_label = QLabel("Password")
        password_label.setBuddy(self.password_edit)
        device_type_label = QLabel("Device Type")
        device_type_label.setBuddy(self.device_type_combo)
        output_label = QLabel("Output Folder")
        output_label.setBuddy(self.output_edit)
        command_inputs_layout.addWidget(profile_label, 0, 0)
        command_inputs_layout.addWidget(self.profile_combo, 0, 1)
        command_inputs_layout.addWidget(self.manage_profiles_button, 0, 2)
        command_inputs_layout.addWidget(username_label, 1, 0)
        command_inputs_layout.addWidget(self.username_edit, 1, 1)
        command_inputs_layout.addWidget(password_label, 2, 0)
        command_inputs_layout.addWidget(self.password_edit, 2, 1)
        command_inputs_layout.addWidget(device_type_label, 3, 0)
        command_inputs_layout.addWidget(self.device_type_combo, 3, 1)
        command_inputs_layout.addWidget(output_label, 8, 0)
        command_inputs_layout.addWidget(output_row, 8, 1)

        form.addRow(self.command_inputs_container)

        settings_layout.addWidget(self.configuration_group)

        button_row = QGridLayout()
        self.validate_button = QPushButton("Validate Input")
        self.run_button = QPushButton("Run Live")
        self.schedule_button = QPushButton("Schedule Run")
        self.cancel_button = QPushButton("Stop Run")
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
        self.results_tabs.currentChanged.connect(self._results_tab_changed)
        results_layout.addWidget(self.results_tabs, 1)

        self.history_tab = QWidget()
        self.history_layout = QVBoxLayout(self.history_tab)
        self.history_placeholder = QLabel(
            "Open History to load persistent audit history."
        )
        self.history_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.history_layout.addWidget(self.history_placeholder, 1)

        self.main_splitter.addWidget(settings_panel)
        self.main_splitter.addWidget(results_panel)
        self.main_splitter.setStretchFactor(0, 0)
        self.main_splitter.setStretchFactor(1, 1)
        self.main_splitter.setSizes([480, 900])
        ssh_layout.addWidget(self.main_splitter)

        self.main_tabs.addTab(ssh_tab, "SSH")
        self.main_tabs.addTab(self.history_tab, "History")
        self.main_tabs.currentChanged.connect(self._main_tab_changed)
        self.main_tabs.setCurrentWidget(ssh_tab)
        self.ssh_tab = ssh_tab
        root.addWidget(self.main_tabs)

        self.results_attention_timer = QTimer(self)
        self.results_attention_timer.setInterval(RESULTS_ATTENTION_INTERVAL_MS)
        self.results_attention_timer.timeout.connect(
            self._advance_results_attention_pulse
        )

        self.setCentralWidget(central)
        self._update_destructive_button_styles()

    def _build_menu(self) -> None:
        self.view_menu = self.menuBar().addMenu("&View")
        self.theme_menu = self.view_menu.addMenu("&Theme")
        self.theme_action_group = QActionGroup(self)
        self.theme_action_group.setExclusive(True)
        self.theme_actions: dict[str, QAction] = {}
        for mode, label in (
            (THEME_SYSTEM, "System"),
            (THEME_LIGHT, "Light"),
            (THEME_DARK, "Dark"),
        ):
            action = QAction(label, self)
            action.setCheckable(True)
            action.setData(mode)
            action.setChecked(mode == self._theme_mode)
            action.triggered.connect(
                lambda checked=False, selected=mode: self._select_theme(
                    selected, checked
                )
            )
            self.theme_action_group.addAction(action)
            self.theme_menu.addAction(action)
            self.theme_actions[mode] = action
        self.theme_system_action = self.theme_actions[THEME_SYSTEM]
        self.theme_light_action = self.theme_actions[THEME_LIGHT]
        self.theme_dark_action = self.theme_actions[THEME_DARK]

    @Slot(str, bool)
    def _select_theme(self, mode: str, checked: bool = True) -> None:
        if not checked:
            return
        app = QApplication.instance()
        if app is None:
            return
        self._theme_mode = apply_application_theme(app, mode)
        _repolish_application_widgets(app)
        QTimer.singleShot(0, lambda: _repolish_application_widgets(app))
        self.settings.setValue("appearance/theme", self._theme_mode)
        self.settings.sync()
        self._update_destructive_button_styles()
        self._refresh_results_attention_color()
        if self._history_ui_built and hasattr(self, "history_table"):
            self._refresh_history()

    def _update_destructive_button_styles(self) -> None:
        app = QApplication.instance()
        palette = app.palette() if app is not None else self.palette()
        dark = palette.color(QPalette.ColorRole.Window).lightness() < 128
        color = "#ff6b6b" if dark else "#c62828"
        style = f"QPushButton {{ color: {color}; }}"
        for name in (
            "cancel_button",
            "clear_completed_button",
            "history_remove_button",
            "history_delete_outputs_button",
            "history_clear_button",
        ):
            button = getattr(self, name, None)
            if button is not None:
                button.setStyleSheet(style)

    @Slot()
    def _open_config_converter(self) -> None:
        self._ensure_converter_ui()
        if self.converter_dialog is None:
            return
        if self.converter_dialog.isMinimized():
            self.converter_dialog.showNormal()
        else:
            self.converter_dialog.show()
        self.converter_dialog.raise_()
        self.converter_dialog.activateWindow()
        QTimer.singleShot(0, self.converter_source_edit.setFocus)

    def _ensure_converter_ui(self) -> None:
        if self._converter_ui_built:
            return
        self._converter_ui_built = True
        self.converter_dialog = QDialog(self)
        self.converter_dialog.setWindowTitle("Config Converter")
        self.converter_dialog.setAccessibleName("Config Converter")
        self.converter_dialog.setModal(False)
        self.converter_dialog.setWindowModality(Qt.WindowModality.NonModal)
        self.converter_dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.converter_dialog.setMinimumSize(900, 650)
        self.converter_dialog.resize(1180, 780)
        layout = QVBoxLayout(self.converter_dialog)

        scope_label = QLabel(
            "Generic Cisco IOS XE / Huawei VRP candidate conversion. Verify every "
            "result against the target model, software release, and site policy."
        )
        scope_label.setWordWrap(True)
        scope_label.setAccessibleName("Converter scope warning")
        layout.addWidget(scope_label)
        self.converter_scope_label = scope_label

        direction_row = QHBoxLayout()
        self.converter_source_vendor_combo = QComboBox()
        self.converter_source_vendor_combo.addItem("Auto-detect", None)
        self.converter_source_vendor_combo.addItem("Cisco IOS XE", VENDOR_CISCO)
        self.converter_source_vendor_combo.addItem("Huawei VRP", VENDOR_HUAWEI)
        self.converter_swap_button = QPushButton("Swap Direction")
        self.converter_target_label = QLabel("Target: detected on Convert")
        self.converter_target_label.setWordWrap(True)
        direction_row.addWidget(QLabel("Source"))
        direction_row.addWidget(self.converter_source_vendor_combo)
        direction_row.addWidget(self.converter_swap_button)
        direction_row.addWidget(self.converter_target_label, 1)
        layout.addLayout(direction_row)

        editor_splitter = QSplitter(Qt.Orientation.Horizontal)
        editor_splitter.setChildrenCollapsible(False)

        source_panel = QWidget()
        source_layout = QVBoxLayout(source_panel)
        source_layout.setContentsMargins(0, 0, 0, 0)
        source_heading = QLabel("Source configuration or commands")
        source_layout.addWidget(source_heading)
        self.converter_source_edit = QPlainTextEdit()
        self.converter_source_edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.converter_source_edit.setPlaceholderText(
            "Paste a configuration, command list, or [exec]/[config] command file."
        )
        source_layout.addWidget(self.converter_source_edit, 1)
        source_actions = QHBoxLayout()
        self.converter_import_button = QPushButton("Import Configuration")
        self.converter_clear_button = QPushButton("Clear")
        self.converter_clear_button.setEnabled(False)
        self.converter_convert_button = QPushButton("Convert")
        self.converter_convert_button.setEnabled(False)
        source_actions.addWidget(self.converter_import_button)
        source_actions.addWidget(self.converter_clear_button)
        source_actions.addStretch()
        source_actions.addWidget(self.converter_convert_button)
        source_layout.addLayout(source_actions)
        editor_splitter.addWidget(source_panel)

        output_panel = QWidget()
        output_layout = QVBoxLayout(output_panel)
        output_layout.setContentsMargins(0, 0, 0, 0)
        output_heading = QLabel("Converted target text")
        output_layout.addWidget(output_heading)
        self.converter_output_edit = QPlainTextEdit()
        self.converter_output_edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.converter_output_edit.setPlaceholderText(
            "Converted candidate output appears here and remains editable for review."
        )
        output_layout.addWidget(self.converter_output_edit, 1)
        output_actions = QHBoxLayout()
        self.converter_recheck_button = QPushButton("Recheck")
        self.converter_export_button = QPushButton("Export…")
        self.converter_import_to_ssh_button = QPushButton("Import to SSH Commands")
        for button in (
            self.converter_recheck_button,
            self.converter_export_button,
            self.converter_import_to_ssh_button,
        ):
            button.setEnabled(False)
        output_actions.addWidget(self.converter_recheck_button)
        output_actions.addStretch()
        output_actions.addWidget(self.converter_export_button)
        output_actions.addWidget(self.converter_import_to_ssh_button)
        output_layout.addLayout(output_actions)
        editor_splitter.addWidget(output_panel)
        editor_splitter.setStretchFactor(0, 1)
        editor_splitter.setStretchFactor(1, 1)
        editor_splitter.setSizes([580, 580])
        self.converter_editor_splitter = editor_splitter
        layout.addWidget(editor_splitter, 2)

        self.converter_status_label = QLabel(
            "Enter or import source text, select its vendor or use auto-detect, then Convert."
        )
        self.converter_status_label.setWordWrap(True)
        layout.addWidget(self.converter_status_label)

        self.converter_issue_table = QTableWidget(0, 4)
        self.converter_issue_table.setHorizontalHeaderLabels(
            ["Source line", "Disposition", "Section", "Explanation"]
        )
        self.converter_issue_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers
        )
        self.converter_issue_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self.converter_issue_table.setSelectionMode(
            QTableWidget.SelectionMode.SingleSelection
        )
        self.converter_issue_table.setAlternatingRowColors(True)
        self.converter_issue_table.verticalHeader().setVisible(False)
        issue_header = self.converter_issue_table.horizontalHeader()
        for column in (0, 1, 2):
            issue_header.setSectionResizeMode(
                column, QHeaderView.ResizeMode.ResizeToContents
            )
        issue_header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.converter_issue_table.setMinimumHeight(150)
        layout.addWidget(self.converter_issue_table, 1)

        self.converter_source_edit.textChanged.connect(
            self._converter_source_changed
        )
        self.converter_output_edit.textChanged.connect(
            self._converter_output_changed
        )
        self.converter_source_vendor_combo.currentIndexChanged.connect(
            self._converter_direction_changed
        )
        self.converter_swap_button.clicked.connect(self._swap_converter_direction)
        self.converter_import_button.clicked.connect(self._import_converter_source)
        self.converter_clear_button.clicked.connect(self._clear_converter)
        self.converter_convert_button.clicked.connect(self._convert_converter_source)
        self.converter_recheck_button.clicked.connect(self._recheck_converter_output)
        self.converter_export_button.clicked.connect(self._export_converter_output)
        self.converter_import_to_ssh_button.clicked.connect(
            self._import_converter_to_ssh
        )
        self.converter_issue_table.itemSelectionChanged.connect(
            self._converter_issue_selected
        )

    @Slot()
    def _converter_source_changed(self) -> None:
        if not self._converter_ui_built:
            return
        has_source = bool(self.converter_source_edit.toPlainText().strip())
        self.converter_convert_button.setEnabled(has_source)
        if self._converter_result is not None:
            self._converter_source_stale = True
            self.converter_status_label.setText(
                "Source changed. Click Convert to replace the stale result."
            )
        self._update_converter_actions()

    @Slot()
    def _converter_output_changed(self) -> None:
        if not self._converter_ui_built or self._converter_setting_output:
            return
        self._converter_output_dirty = True
        self.converter_status_label.setText(
            "Output edited. Recheck before SSH import or sectioned export."
        )
        self._update_converter_actions()

    @Slot()
    def _converter_direction_changed(self) -> None:
        if not self._converter_ui_built:
            return
        selected = self.converter_source_vendor_combo.currentData()
        if selected:
            self.converter_target_label.setText(
                f"Target: {vendor_label(opposite_vendor(str(selected)))}"
            )
        else:
            self.converter_target_label.setText("Target: detected on Convert")
        if self._converter_result is not None:
            self._converter_source_stale = True
            self.converter_status_label.setText(
                "Direction changed. Click Convert to replace the stale result."
            )
        self._update_converter_actions()

    @Slot()
    def _swap_converter_direction(self) -> None:
        selected = self.converter_source_vendor_combo.currentData()
        if selected is None:
            detection = detect_vendor(self.converter_source_edit.toPlainText())
            if detection.vendor is None:
                QMessageBox.critical(
                    self.converter_dialog,
                    "Cannot swap direction",
                    "Select a source vendor or enter text with recognizable vendor syntax.",
                )
                return
            selected = detection.vendor
        target = opposite_vendor(str(selected))
        index = self.converter_source_vendor_combo.findData(target)
        self.converter_source_vendor_combo.setCurrentIndex(index)

    def _resolved_converter_source(self, show_error: bool = True) -> Optional[str]:
        selected = self.converter_source_vendor_combo.currentData()
        if selected:
            return str(selected)
        detection = detect_vendor(self.converter_source_edit.toPlainText())
        if detection.vendor:
            return detection.vendor
        if show_error:
            QMessageBox.critical(
                self.converter_dialog,
                "Source vendor required",
                "The source is unknown or contains mixed Cisco and Huawei syntax. "
                "Choose Cisco IOS XE or Huawei VRP explicitly.",
            )
        return None

    @Slot()
    def _convert_converter_source(self) -> None:
        source_vendor = self._resolved_converter_source()
        if source_vendor is None:
            return
        try:
            result = convert_text(
                self.converter_source_edit.toPlainText(), source_vendor
            )
        except ValueError as exc:
            QMessageBox.critical(
                self.converter_dialog, "Conversion failed", str(exc)
            )
            return
        self._converter_target_vendor = result.target_vendor
        self.converter_target_label.setText(
            f"Target: {vendor_label(result.target_vendor)}"
        )
        self._converter_setting_output = True
        try:
            self.converter_output_edit.setPlainText(result.output_text)
        finally:
            self._converter_setting_output = False
        self._converter_source_stale = False
        self._converter_output_dirty = False
        self._set_converter_result(result, is_analysis=False)

    @Slot()
    def _recheck_converter_output(self) -> None:
        if self._converter_target_vendor is None:
            source_vendor = self._resolved_converter_source()
            if source_vendor is None:
                return
            self._converter_target_vendor = opposite_vendor(source_vendor)
            self.converter_target_label.setText(
                f"Target: {vendor_label(self._converter_target_vendor)}"
            )
        result = analyze_target_text(
            self.converter_output_edit.toPlainText(),
            self._converter_target_vendor,
        )
        self._converter_output_dirty = False
        self._set_converter_result(result, is_analysis=True)

    def _set_converter_result(
        self, result: ConversionResult, *, is_analysis: bool
    ) -> None:
        self._converter_result = result
        self._converter_result_is_analysis = is_analysis
        table = self.converter_issue_table
        table.blockSignals(True)
        table.setRowCount(0)
        for row, issue in enumerate(result.issues):
            table.insertRow(row)
            values = (
                str(issue.line_number),
                issue.disposition.title(),
                issue.section.title(),
                issue.message,
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, issue)
                table.setItem(row, column, item)
        table.blockSignals(False)
        dispositions = [issue.disposition for issue in result.issues]
        review_count = dispositions.count(DISPOSITION_REVIEW)
        unmapped_count = dispositions.count(DISPOSITION_UNMAPPED)
        ignored_count = dispositions.count(DISPOSITION_IGNORED)
        blocking_count = len(result.blocking_issues)
        self.converter_status_label.setText(
            f"{len(result.commands)} converted | {review_count} review | "
            f"{unmapped_count} unmapped | {ignored_count} ignored | "
            f"{blocking_count} blocking"
        )
        self._update_converter_actions()

    def _update_converter_actions(self) -> None:
        if not self._converter_ui_built:
            return
        has_source = bool(self.converter_source_edit.toPlainText())
        has_output = bool(self.converter_output_edit.toPlainText().strip())
        self.converter_clear_button.setEnabled(has_source or has_output)
        self.converter_recheck_button.setEnabled(has_output)
        self.converter_export_button.setEnabled(has_output)
        self.converter_import_to_ssh_button.setEnabled(has_output)

    @Slot()
    def _clear_converter(self) -> None:
        self._converter_result = None
        self._converter_target_vendor = None
        self._converter_source_stale = False
        self._converter_output_dirty = False
        self._converter_result_is_analysis = False
        self._converter_setting_output = True
        try:
            self.converter_output_edit.clear()
        finally:
            self._converter_setting_output = False
        self.converter_source_edit.clear()
        self.converter_issue_table.clearContents()
        self.converter_issue_table.setRowCount(0)
        selected = self.converter_source_vendor_combo.currentData()
        if selected:
            self.converter_target_label.setText(
                f"Target: {vendor_label(opposite_vendor(str(selected)))}"
            )
        else:
            self.converter_target_label.setText("Target: detected on Convert")
        self.converter_status_label.setText(
            "Enter or import source text, select its vendor or use auto-detect, then Convert."
        )
        self._update_converter_actions()

    @Slot()
    def _converter_issue_selected(self) -> None:
        selected = self.converter_issue_table.selectedItems()
        if not selected:
            return
        row = selected[0].row()
        item = self.converter_issue_table.item(row, 0)
        issue = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        if not isinstance(issue, ConversionIssue):
            return
        editor = (
            self.converter_output_edit
            if self._converter_result_is_analysis
            else self.converter_source_edit
        )
        block = editor.document().findBlockByLineNumber(max(0, issue.line_number - 1))
        if not block.isValid():
            return
        cursor = QTextCursor(block)
        cursor.select(QTextCursor.SelectionType.LineUnderCursor)
        editor.setTextCursor(cursor)
        editor.setFocus()

    @Slot()
    def _import_converter_source(self) -> None:
        start = (
            str(Path(self._last_converter_import).parent)
            if self._last_converter_import
            else str(Path.home())
        )
        converter_was_visible = bool(
            self.converter_dialog is not None and self.converter_dialog.isVisible()
        )
        selected, _ = QFileDialog.getOpenFileName(
            self.converter_dialog,
            "Import configuration or commands",
            start,
            "Configuration files (*.cfg *.txt);;All files (*)",
        )
        self._restore_converter_after_file_dialog(converter_was_visible)
        if not selected:
            return
        path = Path(selected)
        text = self._read_import_file(path)
        if text is None:
            return
        self.converter_source_edit.setPlainText(text)
        self._last_converter_import = str(path)

    def _choose_converter_export_format(self) -> Optional[str]:
        message = QMessageBox(self.converter_dialog)
        message.setWindowTitle("Export converted output")
        message.setText("Choose the export format.")
        plain_button = message.addButton(
            "Plain Target Configuration", QMessageBox.ButtonRole.AcceptRole
        )
        ssh_button = message.addButton(
            "SSH [exec]/[config] File", QMessageBox.ButtonRole.ActionRole
        )
        message.addButton(QMessageBox.StandardButton.Cancel)
        message.exec()
        clicked = message.clickedButton()
        if clicked is plain_button:
            return "plain"
        if clicked is ssh_button:
            return "ssh"
        return None

    def _select_converter_export_file(
        self, title: str, default_name: str, file_filter: str
    ) -> Optional[Path]:
        start = (
            Path(self._last_converter_export).parent
            if self._last_converter_export
            else Path.home()
        )
        converter_was_visible = bool(
            self.converter_dialog is not None and self.converter_dialog.isVisible()
        )
        selected, _ = QFileDialog.getSaveFileName(
            self.converter_dialog,
            title,
            str(start / default_name),
            file_filter,
        )
        self._restore_converter_after_file_dialog(converter_was_visible)
        return Path(selected) if selected else None

    def _restore_converter_after_file_dialog(self, was_visible: bool) -> None:
        if not was_visible or self.converter_dialog is None or self._closing:
            return

        def restore() -> None:
            if self.converter_dialog is None or self._closing:
                return
            if self.converter_dialog.isMinimized():
                self.converter_dialog.showNormal()
            else:
                self.converter_dialog.show()
            self.converter_dialog.raise_()
            self.converter_dialog.activateWindow()

        # Native macOS file panels can deliver their Cancel close event to the
        # non-modal parent. Restore after that event has drained from the queue.
        restore()
        QTimer.singleShot(0, restore)

    @Slot()
    def _export_converter_output(self) -> None:
        export_format = self._choose_converter_export_format()
        if export_format is None:
            return
        if export_format == "plain":
            has_review_risk = bool(
                self._converter_source_stale
                or self._converter_output_dirty
                or (
                    self._converter_result is not None
                    and self._converter_result.blocking_issues
                )
            )
            if has_review_risk:
                answer = QMessageBox.warning(
                    self.converter_dialog,
                    "Export review-only output?",
                    "This output is stale, modified, or contains blocking review items. "
                    "Export it only as a review candidate.",
                    QMessageBox.StandardButton.Yes
                    | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Cancel,
                )
                if answer != QMessageBox.StandardButton.Yes:
                    return
            path = self._select_converter_export_file(
                "Export target configuration",
                "converted-config.cfg",
                "Configuration files (*.cfg *.txt);;All files (*)",
            )
            text = self.converter_output_edit.toPlainText()
        else:
            result = self._converter_handoff_result()
            if result is None:
                return
            try:
                text = render_sectioned_commands(result)
            except ValueError as exc:
                QMessageBox.critical(
                    self.converter_dialog, "SSH export blocked", str(exc)
                )
                return
            path = self._select_converter_export_file(
                "Export SSH command file",
                "converted-commands.txt",
                "Text files (*.txt);;All files (*)",
            )
        if path is not None and self._write_export_file(path, text):
            self._last_converter_export = str(path)

    def _converter_handoff_result(self) -> Optional[ConversionResult]:
        if self._converter_source_stale:
            QMessageBox.critical(
                self.converter_dialog,
                "Stale conversion",
                "The source or conversion direction changed. Click Convert again first.",
            )
            return None
        if self._converter_output_dirty or self._converter_result is None:
            self._recheck_converter_output()
        result = self._converter_result
        if result is None:
            return None
        if result.blocking_issues:
            QMessageBox.critical(
                self.converter_dialog,
                "Import blocked",
                f"Resolve all {len(result.blocking_issues)} blocking review item(s) "
                "before executable export or SSH import.",
            )
            return None
        return result

    def _choose_converter_import_mode(
        self, exec_count: int, config_count: int
    ) -> Optional[str]:
        message = QMessageBox(self.converter_dialog)
        message.setWindowTitle("Import converted commands")
        message.setText(
            f"The result contains {exec_count} Exec and {config_count} Config commands. "
            "The SSH command boxes already contain text."
        )
        replace_button = message.addButton(
            "Replace", QMessageBox.ButtonRole.AcceptRole
        )
        append_button = message.addButton(
            "Append", QMessageBox.ButtonRole.ActionRole
        )
        message.addButton(QMessageBox.StandardButton.Cancel)
        message.exec()
        clicked = message.clickedButton()
        if clicked is replace_button:
            return "replace"
        if clicked is append_button:
            return "append"
        return None

    @staticmethod
    def _append_command_text(existing: str, additional: str) -> str:
        if not additional:
            return existing
        if not existing.strip():
            return additional
        return f"{existing.rstrip()}\n{additional}"

    @Slot()
    def _import_converter_to_ssh(self) -> None:
        result = self._converter_handoff_result()
        if result is None:
            return
        exec_text = "\n".join(result.exec_commands)
        config_text = "\n".join(result.config_commands)
        existing_exec = self.exec_commands_edit.toPlainText()
        existing_config = self.config_commands_edit.toPlainText()
        mode = "replace"
        if existing_exec.strip() or existing_config.strip():
            selected = self._choose_converter_import_mode(
                len(result.exec_commands), len(result.config_commands)
            )
            if selected is None:
                return
            mode = selected
        if mode == "replace":
            self.exec_commands_edit.setPlainText(exec_text)
            self.config_commands_edit.setPlainText(config_text)
        else:
            self.exec_commands_edit.setPlainText(
                self._append_command_text(existing_exec, exec_text)
            )
            self.config_commands_edit.setPlainText(
                self._append_command_text(existing_config, config_text)
            )
        device_type = (
            "cisco_ios"
            if result.target_vendor == VENDOR_CISCO
            else "huawei"
        )
        self.device_type_combo.setCurrentText(device_type)
        self._prepared = None
        self._set_validated(False)
        self.validation_label.setText(
            f"Imported {len(result.exec_commands)} Exec and "
            f"{len(result.config_commands)} Config commands from Config Converter. "
            "Validate before running."
        )

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
        self._request_results_attention()
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
        del index
        self._acknowledge_results_attention_if_visible()

    @Slot(int)
    def _main_tab_changed(self, index: int) -> None:
        if index == self.main_tabs.indexOf(self.history_tab):
            self._ensure_history_ui()
        self._acknowledge_results_attention_if_visible()

    def _device_results_visible(self) -> bool:
        return (
            self.main_tabs.currentWidget() is self.ssh_tab
            and self.results_tabs.currentIndex() == 1
        )

    def _request_results_attention(self) -> None:
        if self._device_results_visible():
            self._clear_results_attention()
            return
        self._results_attention_pending = True
        self._results_attention_tick = 0
        self._set_results_attention_labels(True)
        self._set_results_attention_highlighted(True)
        self.results_attention_timer.start()

    def _advance_results_attention_pulse(self) -> None:
        if not self._results_attention_pending:
            self.results_attention_timer.stop()
            return
        self._results_attention_tick += 1
        if self._results_attention_tick >= RESULTS_ATTENTION_TICKS:
            self.results_attention_timer.stop()
            self._set_results_attention_highlighted(False)
            return
        self._set_results_attention_highlighted(
            self._results_attention_tick % 2 == 0
        )

    def _acknowledge_results_attention_if_visible(self) -> None:
        if self._results_attention_pending and self._device_results_visible():
            self._clear_results_attention()

    def _clear_results_attention(self) -> None:
        self.results_attention_timer.stop()
        self._results_attention_pending = False
        self._results_attention_tick = 0
        self._set_results_attention_labels(False)
        self._set_results_attention_highlighted(False)

    def _set_results_attention_labels(self, pending: bool) -> None:
        self.main_tabs.setTabText(
            self.main_tabs.indexOf(self.ssh_tab), "SSH •" if pending else "SSH"
        )
        self.results_tabs.setTabText(
            1, "Device Results •" if pending else "Device Results"
        )

    def _set_results_attention_highlighted(self, highlighted: bool) -> None:
        self._results_attention_highlighted = highlighted
        color = QColor()
        if highlighted:
            app = QApplication.instance()
            palette = app.palette() if app is not None else self.palette()
            color = palette.color(QPalette.ColorRole.Highlight)
        self.main_tabs.tabBar().setTabTextColor(
            self.main_tabs.indexOf(self.ssh_tab), color
        )
        self.results_tabs.tabBar().setTabTextColor(1, color)

    def _refresh_results_attention_color(self) -> None:
        if self._results_attention_pending:
            self._set_results_attention_highlighted(
                self._results_attention_highlighted
            )

    @Slot()
    def _open_command_dictionary(self) -> None:
        self._ensure_dictionary_ui()
        if self.dictionary_dialog is None:
            return
        if self.dictionary_dialog.isMinimized():
            self.dictionary_dialog.showNormal()
        else:
            self.dictionary_dialog.show()
        self.dictionary_dialog.raise_()
        self.dictionary_dialog.activateWindow()
        QTimer.singleShot(0, self.dictionary_search_edit.setFocus)

    def _ensure_dictionary_ui(self) -> None:
        if self._dictionary_ui_built:
            return
        self._dictionary_ui_built = True
        self.dictionary_dialog = QDialog(self)
        self.dictionary_dialog.setWindowTitle("Command Dictionary")
        self.dictionary_dialog.setAccessibleName("Command Dictionary")
        self.dictionary_dialog.setModal(False)
        self.dictionary_dialog.setWindowModality(Qt.WindowModality.NonModal)
        self.dictionary_dialog.setAttribute(
            Qt.WidgetAttribute.WA_DeleteOnClose, False
        )
        self.dictionary_dialog.setMinimumSize(900, 600)
        self.dictionary_dialog.resize(1000, 680)
        self.dictionary_layout = QVBoxLayout(self.dictionary_dialog)

        filters = QHBoxLayout()
        self.dictionary_search_edit = QLineEdit()
        self.dictionary_search_edit.setPlaceholderText(
            "Search commands, purposes, aliases, and keywords…"
        )
        self.dictionary_search_edit.setClearButtonEnabled(True)
        self.dictionary_platform_combo = QComboBox()
        self.dictionary_platform_combo.addItem("All", None)
        self.dictionary_platform_combo.addItem("Catalyst 9300L", "Catalyst 9300L")
        self.dictionary_platform_combo.addItem(
            "CloudEngine S5755-S", "CloudEngine S5755-S"
        )
        self.dictionary_category_combo = QComboBox()
        self.dictionary_category_combo.addItem("All", None)
        for category in sorted(
            {entry.category for entry in COMMAND_ENTRIES}, key=str.casefold
        ):
            self.dictionary_category_combo.addItem(category, category)
        self.dictionary_count_label = QLabel()
        filters.addWidget(QLabel("Search"))
        filters.addWidget(self.dictionary_search_edit, 1)
        filters.addWidget(QLabel("Platform"))
        filters.addWidget(self.dictionary_platform_combo)
        filters.addWidget(QLabel("Category"))
        filters.addWidget(self.dictionary_category_combo)
        filters.addWidget(self.dictionary_count_label)
        self.dictionary_layout.addLayout(filters)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        self.dictionary_table = QTableWidget(0, 3)
        self.dictionary_table.setHorizontalHeaderLabels(
            ["Platform", "Category", "Command"]
        )
        self.dictionary_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers
        )
        self.dictionary_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self.dictionary_table.setSelectionMode(
            QTableWidget.SelectionMode.SingleSelection
        )
        self.dictionary_table.setAlternatingRowColors(True)
        self.dictionary_table.verticalHeader().setVisible(False)
        dictionary_header = self.dictionary_table.horizontalHeader()
        dictionary_header.setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        dictionary_header.setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        dictionary_header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        splitter.addWidget(self.dictionary_table)

        self.dictionary_details_panel = QWidget()
        self.dictionary_details_panel.setAccessibleName("Command details")
        details_panel_layout = QVBoxLayout(self.dictionary_details_panel)
        self.dictionary_details_scroll = QScrollArea()
        self.dictionary_details_scroll.setWidgetResizable(True)
        self.dictionary_details_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.dictionary_details_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        details_content = QWidget()
        details_layout = QVBoxLayout(details_content)
        self.dictionary_title_label = QLabel("Select a command")
        self.dictionary_title_label.setWordWrap(True)
        self.dictionary_title_label.setStyleSheet("font-size: 18px; font-weight: 600;")
        details_layout.addWidget(self.dictionary_title_label)

        self.dictionary_purpose_heading_label = QLabel("Purpose")
        details_layout.addWidget(self.dictionary_purpose_heading_label)
        self.dictionary_description_label = QLabel()
        self.dictionary_description_label.setWordWrap(True)
        self.dictionary_description_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        details_layout.addWidget(self.dictionary_description_label)

        self.dictionary_syntax_heading_label = QLabel("Syntax")
        details_layout.addWidget(self.dictionary_syntax_heading_label)
        self.dictionary_syntax_edit = QPlainTextEdit()
        self.dictionary_syntax_edit.setReadOnly(True)
        self.dictionary_syntax_edit.setMaximumHeight(100)
        details_layout.addWidget(self.dictionary_syntax_edit)

        primary_copy_row = QHBoxLayout()
        self.dictionary_copy_button = QPushButton("Copy")
        self.dictionary_copy_button.setAccessibleName("Copy primary syntax")
        self.dictionary_copy_button.setAccessibleDescription(
            "Copies the selected primary command or template syntax."
        )
        self.dictionary_copy_button.setEnabled(False)
        self.dictionary_copy_confirmation = QLabel()
        primary_copy_row.addWidget(self.dictionary_copy_button)
        primary_copy_row.addWidget(self.dictionary_copy_confirmation)
        primary_copy_row.addStretch()
        details_layout.addLayout(primary_copy_row)
        self.dictionary_primary_copy_row = primary_copy_row

        self.dictionary_placeholder_warning = QLabel(
            "This is a template. Replace every <PLACEHOLDER> before use."
        )
        self.dictionary_placeholder_warning.setWordWrap(True)
        self.dictionary_placeholder_warning.setStyleSheet("font-weight: 600;")
        self.dictionary_placeholder_warning.hide()
        details_layout.addWidget(self.dictionary_placeholder_warning)

        self.dictionary_rollback_heading_label = QLabel("Rollback syntax")
        self.dictionary_rollback_heading_label.hide()
        details_layout.addWidget(self.dictionary_rollback_heading_label)
        self.dictionary_rollback_edit = QPlainTextEdit()
        self.dictionary_rollback_edit.setReadOnly(True)
        self.dictionary_rollback_edit.setMaximumHeight(100)
        self.dictionary_rollback_edit.hide()
        details_layout.addWidget(self.dictionary_rollback_edit)

        rollback_copy_row = QHBoxLayout()
        self.dictionary_copy_rollback_button = QPushButton("Copy")
        self.dictionary_copy_rollback_button.setAccessibleName(
            "Copy rollback syntax"
        )
        self.dictionary_copy_rollback_button.setAccessibleDescription(
            "Copies the selected rollback command syntax."
        )
        self.dictionary_copy_rollback_button.setEnabled(False)
        self.dictionary_copy_rollback_button.hide()
        self.dictionary_copy_rollback_confirmation = QLabel()
        self.dictionary_copy_rollback_confirmation.hide()
        rollback_copy_row.addWidget(self.dictionary_copy_rollback_button)
        rollback_copy_row.addWidget(self.dictionary_copy_rollback_confirmation)
        rollback_copy_row.addStretch()
        details_layout.addLayout(rollback_copy_row)
        self.dictionary_rollback_copy_row = rollback_copy_row

        self.dictionary_notes_heading_label = QLabel("Notes")
        details_layout.addWidget(self.dictionary_notes_heading_label)
        self.dictionary_notes_label = QLabel()
        self.dictionary_notes_label.setWordWrap(True)
        self.dictionary_notes_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        details_layout.addWidget(self.dictionary_notes_label)

        self.dictionary_additional_info_button = QPushButton("Additional info")
        self.dictionary_additional_info_button.setFocusPolicy(
            Qt.FocusPolicy.StrongFocus
        )
        self.dictionary_additional_info_button.setAccessibleName("Additional info")
        self.dictionary_additional_info_button.setAccessibleDescription(
            "Shows applicability, deployed target, mode, and risk."
        )
        details_layout.addWidget(
            self.dictionary_additional_info_button,
            0,
            Qt.AlignmentFlag.AlignLeft,
        )

        self.dictionary_additional_info_widget = QWidget()
        self.dictionary_additional_info_widget.setAccessibleName(
            "Additional command information"
        )
        metadata = QFormLayout(self.dictionary_additional_info_widget)
        self.dictionary_applicability_label = QLabel()
        self.dictionary_target_label = QLabel()
        self.dictionary_mode_label = QLabel()
        self.dictionary_risk_label = QLabel()
        for label in (
            self.dictionary_applicability_label,
            self.dictionary_target_label,
            self.dictionary_mode_label,
            self.dictionary_risk_label,
        ):
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        metadata.addRow("Applicability", self.dictionary_applicability_label)
        metadata.addRow("Deployed target", self.dictionary_target_label)
        metadata.addRow("Mode", self.dictionary_mode_label)
        metadata.addRow("Risk", self.dictionary_risk_label)
        self._dictionary_additional_info_expanded = False
        self.dictionary_additional_info_widget.hide()
        details_layout.addWidget(self.dictionary_additional_info_widget)
        details_layout.addStretch()
        self.dictionary_details_scroll.setWidget(details_content)
        details_panel_layout.addWidget(self.dictionary_details_scroll)
        splitter.addWidget(self.dictionary_details_panel)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([820, 520])
        self.dictionary_splitter = splitter
        self.dictionary_layout.addWidget(splitter, 1)

        self.dictionary_empty_label = QLabel(
            "No commands match the current search and filters."
        )
        self.dictionary_empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.dictionary_empty_label.hide()
        self.dictionary_layout.addWidget(self.dictionary_empty_label)

        self.dictionary_search_edit.textChanged.connect(
            self._refresh_dictionary_table
        )
        self.dictionary_platform_combo.currentIndexChanged.connect(
            self._refresh_dictionary_table
        )
        self.dictionary_category_combo.currentIndexChanged.connect(
            self._refresh_dictionary_table
        )
        self.dictionary_table.itemSelectionChanged.connect(
            self._dictionary_selection_changed
        )
        self.dictionary_copy_button.clicked.connect(self._copy_dictionary_command)
        self.dictionary_copy_rollback_button.clicked.connect(
            self._copy_dictionary_rollback
        )
        self.dictionary_additional_info_button.clicked.connect(
            self._toggle_dictionary_additional_info
        )
        self._refresh_dictionary_table()

    @Slot()
    def _refresh_dictionary_table(self) -> None:
        if not self._dictionary_ui_built:
            return
        query = self.dictionary_search_edit.text()
        platform = self.dictionary_platform_combo.currentData()
        category = self.dictionary_category_combo.currentData()
        entries = tuple(search_commands(query, platform, category))
        self.dictionary_table.blockSignals(True)
        self.dictionary_table.setRowCount(0)
        for row, entry in enumerate(entries):
            self.dictionary_table.insertRow(row)
            values = (entry.platform, entry.category, entry.syntax)
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, entry)
                self.dictionary_table.setItem(row, column, item)
        self.dictionary_table.blockSignals(False)
        count = len(entries)
        self.dictionary_count_label.setText(
            f"{count} command" if count == 1 else f"{count} commands"
        )
        self.dictionary_empty_label.setVisible(count == 0)
        self.dictionary_copy_confirmation.clear()
        self.dictionary_copy_rollback_confirmation.clear()
        if entries:
            self.dictionary_table.selectRow(0)
        else:
            self._show_dictionary_entry(None)

    @Slot()
    def _dictionary_selection_changed(self) -> None:
        row = self.dictionary_table.currentRow()
        item = self.dictionary_table.item(row, 0) if row >= 0 else None
        entry = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        self._show_dictionary_entry(entry if isinstance(entry, CommandEntry) else None)

    @Slot()
    def _toggle_dictionary_additional_info(self) -> None:
        self._set_dictionary_additional_info_expanded(
            not self._dictionary_additional_info_expanded
        )

    def _set_dictionary_additional_info_expanded(self, expanded: bool) -> None:
        self._dictionary_additional_info_expanded = expanded
        self.dictionary_additional_info_button.setText(
            "Hide additional info" if expanded else "Additional info"
        )
        self.dictionary_additional_info_widget.setVisible(
            expanded and self._selected_dictionary_entry is not None
        )

    def _show_dictionary_entry(self, entry: Optional[CommandEntry]) -> None:
        self._selected_dictionary_entry = entry
        if entry is None:
            self.dictionary_additional_info_button.hide()
            self.dictionary_additional_info_widget.hide()
            self.dictionary_title_label.setText("No command selected")
            self.dictionary_description_label.clear()
            self.dictionary_applicability_label.clear()
            self.dictionary_target_label.clear()
            self.dictionary_mode_label.clear()
            self.dictionary_risk_label.clear()
            self.dictionary_notes_label.clear()
            self.dictionary_notes_label.setMinimumHeight(0)
            self.dictionary_syntax_edit.clear()
            self.dictionary_rollback_edit.clear()
            self.dictionary_rollback_heading_label.hide()
            self.dictionary_rollback_edit.hide()
            self.dictionary_placeholder_warning.hide()
            self.dictionary_copy_button.setEnabled(False)
            self.dictionary_copy_confirmation.clear()
            self.dictionary_copy_rollback_button.setEnabled(False)
            self.dictionary_copy_rollback_button.hide()
            self.dictionary_copy_rollback_confirmation.clear()
            self.dictionary_copy_rollback_confirmation.hide()
            return
        self.dictionary_additional_info_button.show()
        self._set_dictionary_additional_info_expanded(
            self._dictionary_additional_info_expanded
        )
        applicability = f"{entry.vendor} {entry.platform}"
        if entry.documented_release:
            applicability += f" — documented for {entry.documented_release}"
        self.dictionary_title_label.setText(entry.title)
        self.dictionary_description_label.setText(entry.description)
        self.dictionary_applicability_label.setText(applicability)
        self.dictionary_target_label.setText(entry.deployed_target)
        self.dictionary_mode_label.setText(entry.mode)
        self.dictionary_risk_label.setText(entry.risk)
        self.dictionary_notes_label.setText(entry.applicability_notes)
        wrapped_notes_height = self.dictionary_notes_label.heightForWidth(280)
        self.dictionary_notes_label.setMinimumHeight(max(0, wrapped_notes_height))
        self.dictionary_syntax_edit.setPlainText(entry.syntax)
        has_rollback = bool(entry.rollback_syntax)
        self.dictionary_rollback_heading_label.setVisible(has_rollback)
        self.dictionary_rollback_edit.setVisible(has_rollback)
        self.dictionary_rollback_edit.setPlainText(entry.rollback_syntax)
        self.dictionary_copy_rollback_button.setVisible(has_rollback)
        self.dictionary_copy_rollback_button.setEnabled(has_rollback)
        self.dictionary_copy_rollback_confirmation.setVisible(has_rollback)
        is_template = re.search(r"<[^<>\n]+>", entry.syntax) is not None
        self.dictionary_placeholder_warning.setVisible(is_template)
        self.dictionary_copy_button.setEnabled(True)
        self.dictionary_copy_confirmation.clear()
        self.dictionary_copy_rollback_confirmation.clear()

    @Slot()
    def _copy_dictionary_command(self) -> None:
        entry = getattr(self, "_selected_dictionary_entry", None)
        if entry is None:
            return
        clipboard = QApplication.clipboard()
        if clipboard is None:
            return
        clipboard.setText(entry.syntax)
        is_template = re.search(r"<[^<>\n]+>", entry.syntax) is not None
        self.dictionary_copy_confirmation.setText(
            "Template copied." if is_template else "Command copied."
        )
        self.dictionary_copy_rollback_confirmation.clear()

    @Slot()
    def _copy_dictionary_rollback(self) -> None:
        entry = getattr(self, "_selected_dictionary_entry", None)
        if entry is None or not entry.rollback_syntax:
            return
        clipboard = QApplication.clipboard()
        if clipboard is None:
            return
        clipboard.setText(entry.rollback_syntax)
        self.dictionary_copy_confirmation.clear()
        self.dictionary_copy_rollback_confirmation.setText("Rollback copied.")

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

        trend_controls = QHBoxLayout()
        trend_label = QLabel("Trend")
        self.history_trend_combo = QComboBox()
        self.history_trend_combo.addItem("Device Results", HISTORY_TREND_DEVICES)
        self.history_trend_combo.addItem("Run Duration", HISTORY_TREND_DURATION)
        trend_label.setBuddy(self.history_trend_combo)
        trend_controls.addWidget(trend_label)
        trend_controls.addWidget(self.history_trend_combo)
        trend_controls.addStretch()
        self.history_layout.addLayout(trend_controls)

        charts = QHBoxLayout()
        self.history_line_view = QChartView()
        self.history_pie_view = QChartView()
        self.history_line_view.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.history_pie_view.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.history_line_view.setFrameShape(QFrame.Shape.NoFrame)
        self.history_pie_view.setFrameShape(QFrame.Shape.NoFrame)
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
        self._update_destructive_button_styles()

        self.history_search_edit.textChanged.connect(self._history_filters_changed)
        for combo in (
            self.history_range_combo,
            self.history_status_combo,
            self.history_source_combo,
            self.history_device_combo,
            self.history_profile_combo,
        ):
            combo.currentIndexChanged.connect(self._history_filters_changed)
        self.history_trend_combo.currentIndexChanged.connect(
            self._history_trend_changed
        )
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

    @Slot()
    def _history_trend_changed(self) -> None:
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
            granularity = self._history_granularity()
            timeline = self.history_store.timeline(filters, granularity)
            duration_timeline = (
                self.history_store.duration_timeline(filters, granularity)
                if self.history_trend_combo.currentData() == HISTORY_TREND_DURATION
                else []
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
        self._update_history_charts(
            timeline,
            duration_timeline,
            statuses,
            granularity,
        )

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
        self.history_line_view.setVisible(total > 0)
        self.history_pie_view.setVisible(total > 0)
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

    @staticmethod
    def _history_bucket_heading(label: str, granularity: str) -> str:
        if granularity == "weekly":
            return f"Week of {label}"
        if granularity == "monthly":
            return f"Month {label}"
        return label

    @classmethod
    def _history_line_detail(
        cls,
        label: str,
        granularity: str,
        series_name: str,
        count: int,
    ) -> str:
        noun = "device" if count == 1 else "devices"
        return (
            f"{cls._history_bucket_heading(label, granularity)}\n"
            f"{series_name}: {count} {noun}"
        )

    @staticmethod
    def _history_status_detail(label: str, count: int, total: int) -> str:
        noun = "run" if count == 1 else "runs"
        detail = f"{label}\n{count} {noun}"
        if total > 0:
            detail += f" ({count / total:.1%})"
        return detail

    @classmethod
    def _history_duration_detail(
        cls,
        label: str,
        granularity: str,
        median_seconds: float,
        average_seconds: float,
        run_count: int,
    ) -> str:
        return (
            f"{cls._history_bucket_heading(label, granularity)}\n"
            f"Median: {cls._format_duration(round(median_seconds))}\n"
            f"Average: {cls._format_duration(round(average_seconds))}\n"
            f"Runs: {run_count}"
        )

    @staticmethod
    def _show_history_chart_tooltip(view: QWidget, detail: str) -> None:
        if detail:
            QToolTip.showText(QCursor.pos(), detail, view)
        else:
            QToolTip.hideText()

    def _update_history_charts(
        self,
        timeline,
        duration_timeline,
        statuses: dict[str, int],
        granularity: str = "daily",
    ) -> None:
        from PySide6.QtCharts import (
            QBarCategoryAxis,
            QChart,
            QLineSeries,
            QPieSeries,
            QValueAxis,
        )

        QToolTip.hideText()
        app = QApplication.instance()
        palette = app.palette() if app is not None else self.palette()
        dark_mode = palette.color(QPalette.ColorRole.Window).lightness() < 128
        success_color = QColor("#66bb6a" if dark_mode else "#2e7d32")
        failure_color = QColor("#ef5350" if dark_mode else "#c62828")

        chart = QChart()
        labels: list[str] = []
        x_axis = QBarCategoryAxis()
        y_axis = QValueAxis()
        if self.history_trend_combo.currentData() == HISTORY_TREND_DURATION:
            duration_color = QColor("#64b5f6" if dark_mode else "#1565c0")
            duration_series = QLineSeries()
            duration_series.setName("Median Duration")
            duration_series.setColor(duration_color)
            duration_series.setPointsVisible(True)
            duration_series.setMarkerSize(8.0)
            duration_values = tuple(
                (
                    bucket.label,
                    float(bucket.median_seconds),
                    float(bucket.average_seconds),
                    int(bucket.run_count),
                )
                for bucket in duration_timeline
            )
            chart.setTitle(
                "Median run duration over time"
                if duration_values
                else "No completed run duration data"
            )
            maximum_minutes = 1.0
            for index, (label, median_seconds, _average, _count) in enumerate(
                duration_values
            ):
                labels.append(label)
                median_minutes = median_seconds / 60.0
                duration_series.append(index, median_minutes)
                maximum_minutes = max(maximum_minutes, median_minutes)

            def duration_detail(point) -> str:
                try:
                    index = int(round(point.x()))
                except (OverflowError, TypeError, ValueError):
                    return ""
                if index < 0 or index >= len(duration_values):
                    return ""
                label, median_seconds, average_seconds, run_count = (
                    duration_values[index]
                )
                return self._history_duration_detail(
                    label,
                    granularity,
                    median_seconds,
                    average_seconds,
                    run_count,
                )

            duration_series.hovered.connect(
                lambda point, state: self._show_history_chart_tooltip(
                    self.history_line_view,
                    duration_detail(point) if state else "",
                )
            )
            duration_series.clicked.connect(
                lambda point: self._show_history_chart_tooltip(
                    self.history_line_view,
                    duration_detail(point),
                )
            )
            chart.addSeries(duration_series)
            y_axis.setTitleText("Duration (minutes)")
            y_axis.setLabelFormat("%.1f")
            y_axis.setRange(0.0, maximum_minutes)
            y_axis.setTickCount(6)
            self.history_line_view.setAccessibleName(
                "Median run duration over time"
            )
            duration_accessible = [
                self._history_duration_detail(
                    label,
                    granularity,
                    median_seconds,
                    average_seconds,
                    run_count,
                ).replace("\n", ": ", 1).replace("\n", ", ")
                for label, median_seconds, average_seconds, run_count in (
                    duration_values
                )
            ]
            self.history_line_view.setAccessibleDescription(
                "; ".join(duration_accessible)
                or "No completed run duration data"
            )
            line_series = (duration_series,)
        else:
            chart.setTitle("Successful and failed devices over time")
            success_series = QLineSeries()
            success_series.setName("Succeeded")
            success_series.setColor(success_color)
            success_series.setPointsVisible(True)
            success_series.setMarkerSize(8.0)
            failure_series = QLineSeries()
            failure_series.setName("Failed")
            failure_series.setColor(failure_color)
            failure_series.setPointsVisible(True)
            failure_series.setMarkerSize(8.0)
            maximum = 1
            for index, bucket in enumerate(timeline):
                labels.append(bucket.label)
                success_series.append(index, bucket.successes)
                failure_series.append(index, bucket.failures)
                maximum = max(maximum, bucket.successes, bucket.failures)

            timeline_values = tuple(
                (bucket.label, int(bucket.successes), int(bucket.failures))
                for bucket in timeline
            )

            def line_detail(point, series_name: str, value_index: int) -> str:
                try:
                    index = int(round(point.x()))
                except (OverflowError, TypeError, ValueError):
                    return ""
                if index < 0 or index >= len(timeline_values):
                    return ""
                label, successes, failures = timeline_values[index]
                count = (successes, failures)[value_index]
                return self._history_line_detail(
                    label, granularity, series_name, count
                )

            for series, series_name, value_index in (
                (success_series, "Succeeded", 0),
                (failure_series, "Failed", 1),
            ):
                series.hovered.connect(
                    lambda point, state, name=series_name, index=value_index: (
                        self._show_history_chart_tooltip(
                            self.history_line_view,
                            line_detail(point, name, index) if state else "",
                        )
                    )
                )
                series.clicked.connect(
                    lambda point, name=series_name, index=value_index: (
                        self._show_history_chart_tooltip(
                            self.history_line_view,
                            line_detail(point, name, index),
                        )
                    )
                )
            chart.addSeries(success_series)
            chart.addSeries(failure_series)
            y_axis.setLabelFormat("%d")
            y_axis.setRange(0, maximum)
            y_axis.setTickCount(min(6, maximum + 1))
            self.history_line_view.setAccessibleName(
                "Successful and failed devices over time"
            )
            line_accessible = []
            for label, successes, failures in timeline_values:
                heading = self._history_bucket_heading(label, granularity)
                success_noun = "device" if successes == 1 else "devices"
                failure_noun = "device" if failures == 1 else "devices"
                line_accessible.append(
                    f"{heading}: {successes} succeeded {success_noun}, "
                    f"{failures} failed {failure_noun}"
                )
            self.history_line_view.setAccessibleDescription(
                "; ".join(line_accessible) or "No timeline data"
            )
            line_series = (success_series, failure_series)

        x_axis.append(labels or ["No data"])
        chart.addAxis(x_axis, Qt.AlignmentFlag.AlignBottom)
        chart.addAxis(y_axis, Qt.AlignmentFlag.AlignLeft)
        for series in line_series:
            series.attachAxis(x_axis)
            series.attachAxis(y_axis)
        self._style_history_chart(chart, (x_axis, y_axis))
        self.history_line_view.setBackgroundBrush(
            QBrush(palette.color(QPalette.ColorRole.Window))
        )
        self.history_line_view.setChart(chart)

        pie_chart = QChart()
        pie_chart.setTitle("Run status")
        pie = QPieSeries()
        pie.setHoleSize(0.45)
        colors = (
            {
                "succeeded": "#66bb6a",
                "completed_with_failures": "#ffca28",
                "failed": "#ef5350",
                "cancelled": "#b0bec5",
                "interrupted": "#ba68c8",
                "running": "#64b5f6",
            }
            if dark_mode
            else {
                "succeeded": "#2e7d32",
                "completed_with_failures": "#f9a825",
                "failed": "#c62828",
                "cancelled": "#616161",
                "interrupted": "#6a1b9a",
                "running": "#1565c0",
            }
        )
        status_total = sum(count for count in statuses.values() if count > 0)
        status_accessible = []
        for status, count in statuses.items():
            if count:
                label = status.replace("_", " ").title()
                piece = pie.append(label, count)
                piece.setColor(QColor(colors.get(status, "#546e7a")))
                piece.setBorderColor(palette.color(QPalette.ColorRole.Base))
                piece.setLabelColor(palette.color(QPalette.ColorRole.WindowText))
                detail = self._history_status_detail(label, count, status_total)
                status_accessible.append(detail.replace("\n", ": "))
                piece.hovered.connect(
                    lambda state, text=detail: self._show_history_chart_tooltip(
                        self.history_pie_view, text if state else ""
                    )
                )
                piece.clicked.connect(
                    lambda text=detail: self._show_history_chart_tooltip(
                        self.history_pie_view, text
                    )
                )
        pie_chart.addSeries(pie)
        self._style_history_chart(pie_chart)
        self.history_pie_view.setBackgroundBrush(
            QBrush(palette.color(QPalette.ColorRole.Window))
        )
        self.history_pie_view.setAccessibleName("Run status")
        self.history_pie_view.setAccessibleDescription(
            "; ".join(status_accessible) or "No run status data"
        )
        self.history_pie_view.setChart(pie_chart)

    def _style_history_chart(self, chart, axes=()) -> None:
        """Match Qt Charts, which does not inherit QWidget dark-mode colors."""
        app = QApplication.instance()
        palette = app.palette() if app is not None else self.palette()
        window = palette.color(QPalette.ColorRole.Window)
        base = palette.color(QPalette.ColorRole.Base)
        text = palette.color(QPalette.ColorRole.WindowText)
        mid = palette.color(QPalette.ColorRole.Mid)
        chart.setBackgroundVisible(True)
        chart.setBackgroundBrush(QBrush(window))
        chart.setBackgroundRoundness(4)
        chart.setDropShadowEnabled(False)
        chart.setPlotAreaBackgroundVisible(True)
        chart.setPlotAreaBackgroundBrush(QBrush(base))
        chart.setTitleBrush(QBrush(text))
        chart.legend().setLabelColor(text)
        chart.legend().setBackgroundVisible(False)
        for axis in axes:
            axis.setLabelsColor(text)
            axis.setTitleBrush(QBrush(text))
            axis.setLinePenColor(mid)
            axis.setGridLineColor(mid)

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() in {
            QEvent.Type.PaletteChange,
            QEvent.Type.ApplicationPaletteChange,
            QEvent.Type.ThemeChange,
        }:
            if hasattr(self, "cancel_button"):
                self._update_destructive_button_styles()
            if hasattr(self, "results_attention_timer"):
                self._refresh_results_attention_color()
            if self._history_ui_built and hasattr(self, "history_table"):
                QTimer.singleShot(0, self._refresh_history)

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
        self.main_tabs.setCurrentWidget(self.ssh_tab)
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
            if self.dictionary_dialog is not None:
                self.dictionary_dialog.hide()
            if self.converter_dialog is not None:
                self.converter_dialog.hide()
            if hasattr(self, "scheduler_timer"):
                self.scheduler_timer.stop()
            if hasattr(self, "results_attention_timer"):
                self.results_attention_timer.stop()
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
        if self.dictionary_dialog is not None:
            self.dictionary_dialog.hide()
        if self.converter_dialog is not None:
            self.converter_dialog.hide()
        if hasattr(self, "scheduler_timer"):
            self.scheduler_timer.stop()
        if hasattr(self, "results_attention_timer"):
            self.results_attention_timer.stop()
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
            apply_application_theme(
                app,
                smoke_settings.value("appearance/theme", THEME_SYSTEM, str),
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
            window._open_command_dictionary()
            window.dictionary_search_edit.setText("interface")
            window._open_config_converter()
            window.converter_source_edit.setPlainText("show version")
            window._convert_converter_source()
            app.processEvents()
            window.close()
            app.processEvents()
        return 0
    settings = QSettings(ORGANIZATION_NAME, APP_NAME)
    apply_application_theme(
        app,
        settings.value("appearance/theme", THEME_SYSTEM, str),
    )
    window = MainWindow(settings)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
