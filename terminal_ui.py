"""Interactive Terminal tab, isolated from queued SSH batch work."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, QThread, QUrl, Qt, Signal, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from run_commands import ConfigurationError, Settings
from terminal_session import TerminalSession, terminal_transcript_path


class TerminalBridge(QObject):
    """Send terminal data through Qt WebChannel without injecting JavaScript."""

    output = Signal(str)
    clear = Signal()
    enabled = Signal(bool)
    loaded = Signal()
    input_received = Signal(str)
    size_received = Signal(int, int)

    @Slot()
    def ready(self) -> None:
        self.loaded.emit()

    @Slot(str)
    def sendInput(self, data: str) -> None:
        self.input_received.emit(data)

    @Slot(int, int)
    def resizeTerminal(self, cols: int, rows: int) -> None:
        self.size_received.emit(cols, rows)


class TerminalThread(QThread):
    connected = Signal()
    output = Signal(str)
    done = Signal(object, str)

    def __init__(self, session: TerminalSession, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.session = session

    def run(self) -> None:
        try:
            path = self.session.run(self.connected.emit, self.output.emit)
        except Exception as exc:  # Worker errors must reach the GUI thread.
            settings = self.session.settings
            message = f"{exc.__class__.__name__}: {exc}"
            for secret in (settings.username, settings.password, settings.enable_secret):
                if secret:
                    message = message.replace(secret, "[redacted]")
            path = self.session.transcript_path
            self.done.emit(path if path is not None and path.is_file() else None, message)
        else:
            self.done.emit(path, "")


class TerminalPane(QWidget):
    """Connection form and full keyboard-driven terminal screen."""

    def __init__(
        self,
        profile_store: Any,
        read_profile: Callable[[Path], Settings],
        output_folder: Callable[[], str],
        initial_profile_id: Optional[str] = None,
        initial_username: str = "",
        initial_password: str = "",
        initial_device_type: str = "huawei",
        manage_profiles: Optional[Callable[[], None]] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.profile_store = profile_store
        self.read_profile = read_profile
        self.output_folder = output_folder
        self._session: Optional[TerminalSession] = None
        self._thread: Optional[TerminalThread] = None
        self._ready = False
        self._terminal_size = (80, 24)
        self._loading_profile = False
        self._close_when_finished = False
        self._transcript_path: Optional[Path] = None

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        connection_group = QGroupBox()
        connection_group.setMinimumWidth(350)
        form = QFormLayout(connection_group)
        self.address_edit = QLineEdit()
        self.address_edit.setPlaceholderText("One IPv4 or IPv6 address")
        self.profile_combo = QComboBox()
        profile_row = QWidget()
        profile_layout = QHBoxLayout(profile_row)
        profile_layout.setContentsMargins(0, 0, 0, 0)
        profile_layout.addWidget(self.profile_combo, 1)
        if manage_profiles is not None:
            manage_button = QPushButton("Manage Profiles…")
            manage_button.clicked.connect(manage_profiles)
            profile_layout.addWidget(manage_button)
        self.username_edit = QLineEdit()
        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.device_type_combo = QComboBox()
        self.device_type_combo.setEditable(True)
        self.device_type_combo.addItems(["huawei", "cisco_ios"])
        self.device_type_combo.setCurrentText(initial_device_type)
        self.save_transcript_check = QCheckBox("Save transcript")
        self.save_transcript_check.setToolTip(
            "Save a readable session log in the SSH tab's Output Folder. "
            "Device output may contain passwords."
        )
        self.disable_paging_check = QCheckBox("Disable paging")
        self.disable_paging_check.setToolTip(
            "Show long command output without --More-- prompts. "
            "Applies only to this SSH session."
        )
        form.addRow("Device", self.address_edit)
        form.addRow("Profile", profile_row)
        form.addRow("Username", self.username_edit)
        form.addRow("Password", self.password_edit)
        form.addRow("Device Type", self.device_type_combo)
        form.addRow("", self.disable_paging_check)
        form.addRow("", self.save_transcript_check)
        buttons = QWidget()
        button_layout = QHBoxLayout(buttons)
        button_layout.setContentsMargins(0, 0, 0, 0)
        self.connect_button = QPushButton("Connect")
        self.disconnect_button = QPushButton("Disconnect")
        self.disconnect_button.setEnabled(False)
        button_layout.addWidget(self.connect_button)
        button_layout.addWidget(self.disconnect_button)
        form.addRow("", buttons)
        self.status_label = QLabel("Loading terminal…")
        self.status_label.setWordWrap(True)
        form.addRow("Status", self.status_label)
        self.open_transcript_button = QPushButton("Open Transcript")
        self.open_transcript_button.setEnabled(False)
        form.addRow("", self.open_transcript_button)
        self.connect_button.setEnabled(False)

        self.web_view = QWebEngineView()
        self.bridge = TerminalBridge(self)
        self.web_channel = QWebChannel(self.web_view.page())
        self.web_channel.registerObject("terminalBridge", self.bridge)
        self.web_view.page().setWebChannel(self.web_channel)
        self.web_view.load(
            QUrl.fromLocalFile(
                str(Path(__file__).resolve().parent / "terminal_assets" / "terminal.html")
            )
        )
        splitter.addWidget(connection_group)
        splitter.addWidget(self.web_view)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([390, 950])
        root.addWidget(splitter)

        self.refresh_profiles(initial_profile_id)
        if not initial_profile_id or not self.profile_combo.currentData():
            self.username_edit.setText(initial_username)
            self.password_edit.setText(initial_password)
        self.profile_combo.activated.connect(self._profile_activated)
        self.username_edit.textEdited.connect(self._credentials_edited)
        self.password_edit.textEdited.connect(self._credentials_edited)
        self.connect_button.clicked.connect(self.connect_session)
        self.disconnect_button.clicked.connect(self.disconnect_session)
        self.open_transcript_button.clicked.connect(self.open_transcript)
        self.bridge.loaded.connect(self._terminal_loaded)
        self.bridge.input_received.connect(self._send_input)
        self.bridge.size_received.connect(self._resize_terminal)
        self.web_view.loadFinished.connect(self._web_load_finished)

    def refresh_profiles(self, selected_id: Optional[str] = None) -> None:
        if selected_id is None:
            selected_id = self.profile_combo.currentData()
        self.profile_combo.clear()
        self.profile_combo.addItem("Manual", None)
        for profile in self.profile_store.profiles():
            self.profile_combo.addItem(profile.name, profile.profile_id)
        index = self.profile_combo.findData(selected_id) if selected_id else 0
        self.profile_combo.setCurrentIndex(max(0, index))
        if selected_id and index < 0:
            self.username_edit.clear()
            self.password_edit.clear()
            self.status_label.setText("The selected profile was removed.")
        elif selected_id:
            self._load_profile(str(selected_id))

    @Slot(int)
    def _profile_activated(self, index: int) -> None:
        profile_id = self.profile_combo.itemData(index)
        if profile_id:
            self._load_profile(str(profile_id))
        else:
            self.username_edit.clear()
            self.password_edit.clear()

    def _load_profile(self, profile_id: str) -> None:
        profile = self.profile_store.get(profile_id)
        if profile is None:
            self.status_label.setText("The selected profile no longer exists.")
            return
        try:
            credentials = self.read_profile(profile.env_path)
        except ConfigurationError as exc:
            self.username_edit.clear()
            self.password_edit.clear()
            self.status_label.setText(f"Could not load profile: {exc}")
            return
        self._loading_profile = True
        try:
            self.username_edit.setText(credentials.username)
            self.password_edit.setText(credentials.password)
        finally:
            self._loading_profile = False

    @Slot()
    def _credentials_edited(self) -> None:
        if not self._loading_profile and self.profile_combo.currentData():
            self.profile_combo.setCurrentIndex(0)

    @Slot()
    def _terminal_loaded(self) -> None:
        self._ready = True
        self.connect_button.setEnabled(True)
        self.status_label.setText("Disconnected")

    @Slot(bool)
    def _web_load_finished(self, success: bool) -> None:
        if not success:
            self.status_label.setText("Could not load the bundled terminal view.")

    def _connection_settings(self) -> Settings:
        profile_id = self.profile_combo.currentData()
        if profile_id:
            profile = self.profile_store.get(str(profile_id))
            if profile is None:
                raise ValueError("The selected profile no longer exists.")
            settings = self.read_profile(profile.env_path)
            return replace(
                settings,
                username=self.username_edit.text().strip(),
                password=self.password_edit.text(),
                device_type=self.device_type_combo.currentText().strip(),
            )
        return Settings(
            username=self.username_edit.text().strip(),
            password=self.password_edit.text(),
            enable_secret=None,
            port=22,
            timeout=15,
            device_type=self.device_type_combo.currentText().strip(),
        )

    @Slot()
    def connect_session(self) -> None:
        if self._thread is not None or not self._ready:
            return
        try:
            settings = self._connection_settings()
            address = self.address_edit.text().strip()
            path = None
            if self.save_transcript_check.isChecked():
                folder_text = self.output_folder().strip()
                if not folder_text:
                    raise ValueError("Set an Output Folder on the SSH tab first.")
                path = terminal_transcript_path(Path(folder_text).expanduser(), address, datetime.now())
            session = TerminalSession(
                address,
                settings,
                path,
                disable_paging=self.disable_paging_check.isChecked(),
            )
        except (OSError, ValueError) as exc:
            self.status_label.setText(f"Cannot connect: {exc}")
            return
        self._session = session
        session.resize(*self._terminal_size)
        self._transcript_path = None
        self.open_transcript_button.setEnabled(False)
        self.bridge.clear.emit()
        self.bridge.enabled.emit(False)
        self.status_label.setText(f"Connecting to {session.address}…")
        self.connect_button.setEnabled(False)
        self.disconnect_button.setEnabled(True)
        self.save_transcript_check.setEnabled(False)
        self.disable_paging_check.setEnabled(False)
        self._thread = TerminalThread(session, self)
        self._thread.connected.connect(self._connected)
        self._thread.output.connect(self.bridge.output)
        self._thread.done.connect(self._session_finished)
        self._thread.finished.connect(self._thread_finished)
        self._thread.start()

    @Slot()
    def _connected(self) -> None:
        if self._session is not None:
            self.status_label.setText(f"Connected to {self._session.address}")
            self.bridge.enabled.emit(True)

    @Slot(str)
    def _send_input(self, data: str) -> None:
        if self._session is not None:
            self._session.send(data)

    @Slot(int, int)
    def _resize_terminal(self, cols: int, rows: int) -> None:
        if cols < 2 or rows < 1:
            return
        self._terminal_size = (cols, rows)
        if self._session is not None:
            self._session.resize(cols, rows)

    @Slot()
    def disconnect_session(self) -> None:
        if self._session is not None:
            self._session.stop()
            self.status_label.setText("Disconnecting…")
            self.disconnect_button.setEnabled(False)
            self.bridge.enabled.emit(False)

    @Slot(object, str)
    def _session_finished(self, path: Optional[Path], error: str) -> None:
        self.bridge.enabled.emit(False)
        self.status_label.setText(f"Terminal error: {error}" if error else "Disconnected")
        if path is not None and path.is_file():
            self._transcript_path = path
            self.open_transcript_button.setEnabled(True)
            self.open_transcript_button.setToolTip(str(path))

    @Slot()
    def _thread_finished(self) -> None:
        thread = self._thread
        self._thread = None
        self._session = None
        if thread is not None:
            thread.deleteLater()
        self.connect_button.setEnabled(self._ready)
        self.disconnect_button.setEnabled(False)
        self.save_transcript_check.setChecked(False)
        self.save_transcript_check.setEnabled(True)
        self.disable_paging_check.setEnabled(True)
        if self._close_when_finished:
            self._close_when_finished = False
            self.window().close()

    @Slot()
    def open_transcript(self) -> None:
        if self._transcript_path is not None and self._transcript_path.is_file():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._transcript_path)))

    def is_active(self) -> bool:
        return self._thread is not None

    def stop_for_close(self) -> None:
        self._close_when_finished = True
        self.disconnect_session()
