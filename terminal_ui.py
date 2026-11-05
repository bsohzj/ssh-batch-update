"""Interactive Terminal tab, isolated from queued SSH batch work."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, QThread, QTimer, QUrl, Qt, Signal, Slot
from PySide6.QtGui import QDesktopServices, QHideEvent, QShowEvent
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QGridLayout,
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
from serial_console import (
    ConsoleSettings, SerialConsoleSession, available_console_ports, is_bluetooth_port,
)
from terminal_session import InteractiveSession, TerminalError, TerminalSession, terminal_transcript_path


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

    def __init__(self, session: InteractiveSession, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.session = session

    def run(self) -> None:
        try:
            path = self.session.run(self.connected.emit, self.output.emit)
        except Exception as exc:  # Worker errors must reach the GUI thread.
            message = f"{exc.__class__.__name__}: {exc}"
            for secret in self.session.sensitive_values:
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
        self._session: Optional[InteractiveSession] = None
        self._thread: Optional[TerminalThread] = None
        self._ready = False
        self._terminal_size = (80, 24)
        self._loading_profile = False
        self._close_when_finished = False
        self._transcript_path: Optional[Path] = None
        self._serial_port_explicit = False
        self._serial_ports: Optional[list[tuple[str, str]]] = None
        self.serial_scan_timer = QTimer(self)
        self.serial_scan_timer.setInterval(1000)
        self.serial_scan_timer.timeout.connect(self._poll_serial_ports)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        connection_group = QGroupBox()
        connection_group.setMinimumWidth(400)
        form = QGridLayout(connection_group)
        form.setAlignment(Qt.AlignmentFlag.AlignTop)
        form.setColumnStretch(1, 1)
        self.connection_type_combo = QComboBox()
        self.connection_type_combo.addItem("SSH", "ssh")
        self.connection_type_combo.addItem("Console", "console")
        form.addWidget(QLabel("Connection"), 0, 0)
        form.addWidget(self.connection_type_combo, 0, 1)
        self._ssh_widgets: list[QWidget] = []
        self._console_widgets: list[QWidget] = []
        self.address_edit = QLineEdit()
        self.address_edit.setPlaceholderText("One IPv4 or IPv6 address")
        self.profile_combo = QComboBox()
        if manage_profiles is not None:
            manage_button = QPushButton("Manage Profiles…")
            manage_button.clicked.connect(manage_profiles)
            form.addWidget(manage_button, 1, 2)
            self._ssh_widgets.append(manage_button)
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
        for row, (text, field) in enumerate(
            (
                ("Profile", self.profile_combo),
                ("Username", self.username_edit),
                ("Password", self.password_edit),
                ("Device Type", self.device_type_combo),
                ("Device", self.address_edit),
            ), start=1
        ):
            label = QLabel(text)
            label.setBuddy(field)
            form.addWidget(label, row, 0)
            form.addWidget(field, row, 1)
            self._ssh_widgets.extend((label, field))
        form.addWidget(self.disable_paging_check, 6, 1)
        self._ssh_widgets.append(self.disable_paging_check)

        self.serial_port_combo = QComboBox()
        self.serial_port_combo.setEditable(True)
        self.serial_port_combo.lineEdit().setPlaceholderText("/dev/cu.usbserial-…")
        self.refresh_ports_button = QPushButton("Refresh Ports")
        form.addWidget(self.refresh_ports_button, 1, 2)
        self._console_widgets.append(self.refresh_ports_button)
        self.baud_rate_combo = QComboBox()
        self.baud_rate_combo.setEditable(True)
        self.baud_rate_combo.addItems(["9600", "19200", "38400", "57600", "115200"])
        self.data_bits_combo = QComboBox()
        for value in (8, 7, 6, 5):
            self.data_bits_combo.addItem(str(value), value)
        self.parity_combo = QComboBox()
        for label, value in (("None", "N"), ("Even", "E"), ("Odd", "O"), ("Mark", "M"), ("Space", "S")):
            self.parity_combo.addItem(label, value)
        self.stop_bits_combo = QComboBox()
        for value in (1, 1.5, 2):
            self.stop_bits_combo.addItem(str(value), value)
        self.flow_control_combo = QComboBox()
        for label, value in (("None", "none"), ("RTS/CTS", "rtscts"), ("XON/XOFF", "xonxoff")):
            self.flow_control_combo.addItem(label, value)
        for row, (text, field) in enumerate(
            (
                ("Serial Port", self.serial_port_combo),
                ("Baud Rate", self.baud_rate_combo),
                ("Data Bits", self.data_bits_combo),
                ("Parity", self.parity_combo),
                ("Stop Bits", self.stop_bits_combo),
                ("Flow Control", self.flow_control_combo),
            ), start=1
        ):
            label = QLabel(text)
            label.setBuddy(field)
            form.addWidget(label, row, 0)
            form.addWidget(field, row, 1)
            self._console_widgets.extend((label, field))
        console_hint = QLabel("Press Enter to show the console prompt. Log in directly in the terminal.")
        console_hint.setWordWrap(True)
        form.addWidget(console_hint, 7, 1, 1, 2)
        self._console_widgets.append(console_hint)
        form.addWidget(self.save_transcript_check, 8, 1)
        buttons = QWidget()
        button_layout = QHBoxLayout(buttons)
        button_layout.setContentsMargins(0, 0, 0, 0)
        self.connect_button = QPushButton("Connect")
        self.disconnect_button = QPushButton("Disconnect")
        self.disconnect_button.setEnabled(False)
        button_layout.addWidget(self.connect_button)
        button_layout.addWidget(self.disconnect_button)
        form.addWidget(buttons, 9, 1, 1, 2)
        self.status_label = QLabel("Loading terminal…")
        self.status_label.setWordWrap(True)
        form.addWidget(QLabel("Status"), 10, 0, Qt.AlignmentFlag.AlignTop)
        form.addWidget(self.status_label, 10, 1, 1, 2)
        self.open_transcript_button = QPushButton("Open Transcript")
        self.open_transcript_button.setEnabled(False)
        form.addWidget(self.open_transcript_button, 11, 1, 1, 2)
        self.connect_button.setEnabled(False)

        self.web_view = QWebEngineView()
        self.web_view.setMinimumWidth(600)
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
        splitter.setSizes([480, 900])
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
        self.connection_type_combo.currentIndexChanged.connect(self._connection_type_changed)
        self.refresh_ports_button.clicked.connect(self.refresh_serial_ports)
        self.serial_port_combo.activated.connect(self._serial_port_edited)
        self.serial_port_combo.lineEdit().textEdited.connect(self._serial_port_edited)
        self._connection_type_changed(0)

    @Slot()
    def _serial_port_edited(self) -> None:
        self._serial_port_explicit = True

    @Slot(int)
    def _connection_type_changed(self, index: int) -> None:
        console = self.connection_type_combo.itemData(index) == "console"
        for widget in self._ssh_widgets:
            widget.setVisible(not console)
        for widget in self._console_widgets:
            widget.setVisible(console)
        if console:
            self.refresh_serial_ports()
        self._update_serial_scan_timer()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self._update_serial_scan_timer()

    def hideEvent(self, event: QHideEvent) -> None:
        self.serial_scan_timer.stop()
        super().hideEvent(event)

    def _can_scan_serial_ports(self) -> bool:
        return (
            self.isVisible()
            and self.connection_type_combo.currentData() == "console"
            and self._thread is None
        )

    def _update_serial_scan_timer(self) -> None:
        if self._can_scan_serial_ports():
            if not self.serial_scan_timer.isActive():
                self.serial_scan_timer.start()
        else:
            self.serial_scan_timer.stop()

    @Slot()
    def _poll_serial_ports(self) -> None:
        if not self._can_scan_serial_ports():
            return
        # Avoid rebuilding the model while a user is choosing or typing a port.
        if self.serial_port_combo.view().isVisible() or self.serial_port_combo.lineEdit().hasFocus():
            return
        self.refresh_serial_ports()

    @Slot()
    def refresh_serial_ports(self) -> None:
        selected = self.serial_port_combo.currentText()
        preserve_selection = selected and (
            self._serial_port_explicit or self.serial_port_combo.findText(selected) < 0
        )
        try:
            ports = available_console_ports()
        except OSError as exc:
            self.status_label.setText(f"Could not list serial ports: {exc}")
            return
        if ports == self._serial_ports:
            return
        self._serial_ports = ports
        self.serial_port_combo.clear()
        for device, description in ports:
            self.serial_port_combo.addItem(device)
            self.serial_port_combo.setItemData(
                self.serial_port_combo.count() - 1, description, Qt.ItemDataRole.ToolTipRole
            )
        if preserve_selection:
            self.serial_port_combo.setCurrentText(selected)
        else:
            preferred = next(
                (device for device, description in ports if not is_bluetooth_port(device, description)),
                "",
            )
            if preferred:
                self.serial_port_combo.setCurrentText(preferred)
            else:
                self.serial_port_combo.setCurrentIndex(-1)

    def _console_settings(self) -> ConsoleSettings:
        try:
            baudrate = int(self.baud_rate_combo.currentText().strip())
        except ValueError as exc:
            raise ValueError("Baud rate must be a whole number.") from exc
        return ConsoleSettings(
            port=self.serial_port_combo.currentText().strip(),
            baudrate=baudrate,
            bytesize=self.data_bits_combo.currentData(),
            parity=self.parity_combo.currentData(),
            stopbits=self.stop_bits_combo.currentData(),
            flow_control=self.flow_control_combo.currentData(),
        )

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
            console = self.connection_type_combo.currentData() == "console"
            address = (
                self.serial_port_combo.currentText().strip()
                if console else self.address_edit.text().strip()
            )
            path = None
            if self.save_transcript_check.isChecked():
                folder_text = self.output_folder().strip()
                if not folder_text:
                    raise ValueError("Set an Output Folder on the SSH tab first.")
                path = terminal_transcript_path(
                    Path(folder_text).expanduser(), Path(address).name if console else address, datetime.now()
                )
            session: InteractiveSession
            if console:
                session = SerialConsoleSession(self._console_settings(), path)
            else:
                session = TerminalSession(
                    address, self._connection_settings(), path,
                    disable_paging=self.disable_paging_check.isChecked(),
                )
        except (OSError, ValueError, TerminalError) as exc:
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
        self.connection_type_combo.setEnabled(False)
        for widget in self._console_widgets:
            widget.setEnabled(False)
        self._thread = TerminalThread(session, self)
        self._update_serial_scan_timer()
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
        self.connection_type_combo.setEnabled(True)
        for widget in self._console_widgets:
            widget.setEnabled(True)
        if self._close_when_finished:
            self._close_when_finished = False
            self.window().close()
        else:
            self._update_serial_scan_timer()

    @Slot()
    def open_transcript(self) -> None:
        if self._transcript_path is not None and self._transcript_path.is_file():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._transcript_path)))

    def is_active(self) -> bool:
        return self._thread is not None

    def stop_for_close(self) -> None:
        self._close_when_finished = True
        self.disconnect_session()
