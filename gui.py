#!/usr/bin/env python3
"""PySide6 desktop interface for the SSH batch command runner."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from uuid import uuid4

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
    Settings,
    load_settings,
    parse_commands,
    prepare_run,
    run_batch,
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
        self.profile_store = CredentialProfileStore(self.settings)
        self._prepared: Optional[PreparedRun] = None
        self._batch_result: Optional[BatchResult] = None
        self._token: Optional[CancellationToken] = None
        self._thread: Optional[QThread] = None
        self._worker: Optional[BatchWorker] = None
        self._running = False
        self._loading_settings = True
        self._loading_profile = False
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
        self.username_edit.textChanged.connect(self._credentials_edited)
        self.password_edit.textChanged.connect(self._credentials_edited)
        self.output_edit.textChanged.connect(self.invalidate_validation)
        self.inventory_text_edit.textChanged.connect(self.invalidate_validation)
        self.exec_commands_edit.textChanged.connect(self.invalidate_validation)
        self.config_commands_edit.textChanged.connect(self.invalidate_validation)
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
        if self._loading_settings or self._loading_profile or self._running:
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
