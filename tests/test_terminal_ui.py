import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QWidget

import terminal_ui
from run_commands import Settings


class FakeProfileStore:
    def __init__(self, path):
        self.profile = SimpleNamespace(
            profile_id="profile-1", name="Lab", env_path=path
        )

    def profiles(self):
        return [self.profile]

    def get(self, profile_id):
        return self.profile if profile_id == self.profile.profile_id else None


class FakeTerminalSession:
    instances = []
    sensitive_values = ()

    def __init__(self, address, settings, transcript_path, disable_paging=False, algorithms=None):
        self.address = address
        self.settings = settings
        self.transcript_path = transcript_path
        self.disable_paging = disable_paging
        self.algorithms = algorithms
        self.stopped = threading.Event()
        self.sent = []
        self.sizes = []
        self.instances.append(self)

    def run(self, on_connected, on_output):
        on_connected()
        on_output("switch# ")
        self.stopped.wait(3)
        return self.transcript_path

    def send(self, value):
        self.sent.append(value)

    def resize(self, cols, rows):
        self.sizes.append((cols, rows))

    def stop(self):
        self.stopped.set()


class FakePage(QObject):
    def setWebChannel(self, channel):
        self.channel = channel


class FakeWebView(QWidget):
    loadFinished = Signal(bool)

    def __init__(self):
        super().__init__()
        self._page = FakePage(self)

    def page(self):
        return self._page

    def load(self, url):
        self.url = url


class TerminalPaneTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.web_patch = patch.object(terminal_ui, "QWebEngineView", FakeWebView)
        self.web_patch.start()
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.store = FakeProfileStore(self.base / "lab.env")
        self.pane = terminal_ui.TerminalPane(
            self.store,
            lambda path: Settings("operator", "profile-secret", None, 2222, 20, "profile"),
            lambda: str(self.base),
        )
        self.pane.show()
        self.pane._terminal_loaded()

    def tearDown(self):
        if self.pane.is_active():
            self.pane.disconnect_session()
            self.wait_for(lambda: not self.pane.is_active())
        self.pane.close()
        self.app.processEvents()
        self.temporary.cleanup()
        self.web_patch.stop()

    def wait_for(self, condition):
        for _ in range(100):
            self.app.processEvents()
            if condition():
                return
            QTest.qWait(30)
        self.fail("Timed out waiting for Terminal UI")

    def test_profile_selection_and_manual_override(self):
        self.pane.profile_combo.setCurrentIndex(1)
        self.pane._profile_activated(1)
        self.assertEqual(self.pane.username_edit.text(), "operator")
        self.assertEqual(self.pane.password_edit.text(), "profile-secret")
        self.assertEqual(self.pane._connection_settings().port, 2222)
        self.pane.username_edit.setText("other")
        self.pane.username_edit.textEdited.emit("other")
        self.assertIsNone(self.pane.profile_combo.currentData())
        self.assertEqual(self.pane._connection_settings().port, 2222)

    def test_advanced_defaults_and_port_are_independent_of_console(self):
        self.assertTrue(self.pane.advanced_settings.isHidden())
        self.assertFalse(self.pane.port_spin.isHidden())
        self.assertEqual(self.pane.port_spin.value(), 22)
        self.assertTrue(all(combo.currentData() is None for combo in self.pane.algorithm_combos.values()))
        self.pane.advanced_button.click()
        self.assertFalse(self.pane.advanced_settings.isHidden())
        self.pane.port_spin.setValue(2200)
        self.pane.connection_tabs.setCurrentIndex(1)
        self.assertTrue(self.pane.advanced_settings.isHidden())
        self.pane.connection_tabs.setCurrentIndex(0)
        self.assertFalse(self.pane.advanced_settings.isHidden())
        self.assertEqual(self.pane.port_spin.value(), 2200)

    def test_connection_locks_ssh_fields_and_passes_selected_algorithms(self):
        with patch.object(terminal_ui, "TerminalSession", FakeTerminalSession):
            self.pane.address_edit.setText("192.0.2.1")
            self.pane.port_spin.setValue(2200)
            combo = self.pane.algorithm_combos["cipher"]
            combo.setCurrentIndex(1)
            self.pane.connect_session()
            self.wait_for(lambda: self.pane._session is not None)
            self.assertEqual(self.pane._session.settings.port, 2200)
            self.assertEqual(self.pane._session.algorithms.cipher, combo.currentData())
            for field in (self.pane.address_edit, self.pane.port_spin, self.pane.profile_combo,
                          self.pane.username_edit, self.pane.password_edit, self.pane.device_type_combo,
                          self.pane.connection_tabs, self.pane.disable_paging_check, combo,
                          self.pane.test_tcp_button):
                self.assertFalse(field.isEnabled())
            self.pane.refresh_profiles("profile-1")
            self.assertEqual(self.pane.username_edit.text(), "")
            self.pane.disconnect_session()
            self.wait_for(lambda: not self.pane.is_active())
            self.assertEqual(self.pane.username_edit.text(), "operator")
            self.assertTrue(self.pane.address_edit.isEnabled())

    def test_tcp_test_without_credentials_serializes_connection_and_waits_on_close(self):
        release = threading.Event()
        started = threading.Event()
        calls = []

        def check(address, port):
            calls.append((address, port, threading.current_thread()))
            started.set()
            release.wait(2)
            return terminal_ui.TCPPortResult("success", 0.25)

        with patch.object(terminal_ui, "test_tcp_port", side_effect=check), \
             patch.object(terminal_ui, "TerminalSession") as session_factory:
            self.pane.address_edit.setText("::1")
            self.pane.port_spin.setValue(2222)
            self.pane.test_tcp_connection()
            self.wait_for(started.is_set)
            self.assertTrue(self.pane.is_active())
            self.assertFalse(self.pane.connect_button.isEnabled())
            self.assertFalse(self.pane.port_spin.isEnabled())
            self.pane.connect_session()
            session_factory.assert_not_called()
            self.pane.test_tcp_connection()
            self.pane.stop_for_close()
            self.assertTrue(self.pane.is_active())
            release.set()
            self.wait_for(lambda: not self.pane.is_active())
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][:2], ("::1", 2222))
            self.assertIsNot(calls[0][2], threading.current_thread())
            self.assertIn("succeeded (0.25 s)", self.pane.tcp_result_label.text())

    def test_tcp_invalid_address_and_failure_results(self):
        with patch.object(terminal_ui, "test_tcp_port") as check:
            self.pane.address_edit.setText("hostname.example")
            self.pane.test_tcp_connection()
            check.assert_not_called()
            self.assertIn("valid IPv4 or IPv6", self.pane.tcp_result_label.text())
        for status in ("refused", "timeout", "error"):
            self.pane._tcp_test_result(terminal_ui.TCPPortResult(status, 1.0, "detail"))
            self.assertIn("detail", self.pane.tcp_result_label.text())

    def test_console_mode_shows_serial_fields_and_preserves_manual_port(self):
        with patch.object(terminal_ui, "available_console_ports", return_value=[
            ("/dev/cu.usbserial", "USB adapter")
        ]):
            self.pane.connection_tabs.setCurrentIndex(1)
            self.assertFalse(self.pane.serial_port_combo.isHidden())
            self.assertTrue(self.pane.username_edit.isHidden())
            self.assertTrue(self.pane.device_type_combo.isHidden())
            self.assertTrue(self.pane.disable_paging_check.isHidden())
            self.assertEqual(self.pane.serial_port_combo.currentText(), "/dev/cu.usbserial")
            self.assertEqual(self.pane._console_settings().baudrate, 9600)
            self.pane.serial_port_combo.setCurrentText("/dev/cu.manual")
            self.pane.refresh_serial_ports()
            self.assertEqual(self.pane.serial_port_combo.currentText(), "/dev/cu.manual")
            self.pane.connection_tabs.setCurrentIndex(0)
            self.assertFalse(self.pane.username_edit.isHidden())
            self.assertTrue(self.pane.serial_port_combo.isHidden())

    def test_console_connection_requires_no_ssh_credentials(self):
        class FakeConsoleSession(FakeTerminalSession):
            def __init__(self, settings, transcript_path):
                super().__init__(settings.port, settings, transcript_path)

        with patch.object(terminal_ui, "available_console_ports", return_value=[]), \
             patch.object(terminal_ui, "SerialConsoleSession", FakeConsoleSession):
            self.pane.connection_tabs.setCurrentIndex(1)
            self.pane.serial_port_combo.setCurrentText("/dev/cu.usbserial")
            self.pane.baud_rate_combo.setCurrentText("115200")
            self.pane.save_transcript_check.setChecked(True)
            self.pane.connect_session()
            self.wait_for(lambda: self.pane.is_active())
            self.assertEqual(self.pane._session.settings.baudrate, 115200)
            self.assertEqual(self.pane._session.transcript_path.parent, self.base)
            self.assertFalse(self.pane.connection_tabs.isEnabled())
            self.assertFalse(self.pane.refresh_ports_button.isEnabled())
            self.assertFalse(self.pane.serial_scan_timer.isActive())
            self.pane.bridge.sendInput("\r")
            self.assertEqual(self.pane._session.sent, ["\r"])
            self.pane.disconnect_session()
            self.wait_for(lambda: not self.pane.is_active())
            self.assertTrue(self.pane.connection_tabs.isEnabled())
            self.assertTrue(self.pane.refresh_ports_button.isEnabled())
            self.assertTrue(self.pane.serial_scan_timer.isActive())

    def test_serial_polling_is_limited_to_visible_disconnected_console(self):
        with patch.object(terminal_ui, "available_console_ports", return_value=[]) as scan:
            self.assertFalse(self.pane.serial_scan_timer.isActive())
            self.pane.connection_tabs.setCurrentIndex(1)
            self.assertTrue(self.pane.serial_scan_timer.isActive())
            self.assertEqual(self.pane.serial_scan_timer.interval(), 1000)
            self.pane.hide()
            self.assertFalse(self.pane.serial_scan_timer.isActive())
            scan.reset_mock()
            self.pane.serial_scan_timer.timeout.emit()
            scan.assert_not_called()
            self.pane.show()
            self.assertTrue(self.pane.serial_scan_timer.isActive())
            self.pane.connection_tabs.setCurrentIndex(0)
            self.assertFalse(self.pane.serial_scan_timer.isActive())
            scan.reset_mock()
            self.pane.serial_scan_timer.timeout.emit()
            scan.assert_not_called()

    def test_polling_detects_port_changes_without_rebuilding_unchanged_list(self):
        usb = [("/dev/cu.usbserial-AQ00KNX0", "USB adapter")]
        with patch.object(terminal_ui, "available_console_ports", side_effect=[[], usb, usb, []]):
            self.pane.connection_tabs.setCurrentIndex(1)
            self.pane.connect_button.setFocus()
            self.pane.serial_scan_timer.timeout.emit()
            self.assertEqual(self.pane.serial_port_combo.currentText(), usb[0][0])
            removed = QSignalSpy(self.pane.serial_port_combo.model().rowsRemoved)
            inserted = QSignalSpy(self.pane.serial_port_combo.model().rowsInserted)
            self.pane.serial_scan_timer.timeout.emit()
            self.assertEqual(removed.count(), 0)
            self.assertEqual(inserted.count(), 0)
            self.pane.serial_scan_timer.timeout.emit()
            self.assertEqual(self.pane.serial_port_combo.currentText(), "")

    def test_polling_does_not_interrupt_port_editing(self):
        with patch.object(terminal_ui, "available_console_ports", return_value=[]) as scan:
            self.pane.connection_tabs.setCurrentIndex(1)
            self.pane.serial_port_combo.lineEdit().setFocus()
            self.app.processEvents()
            self.pane.serial_port_combo.lineEdit().setText("/dev/tty.usbserial-manual")
            scan.reset_mock()
            self.pane.serial_scan_timer.timeout.emit()
            scan.assert_not_called()
            self.assertEqual(self.pane.serial_port_combo.currentText(), "/dev/tty.usbserial-manual")

    def test_bluetooth_is_not_auto_selected_and_usb_is_selected_after_refresh(self):
        bluetooth = [("/dev/cu.BLTH", "n/a"), ("/dev/cu.Bluetooth-Incoming-Port", "n/a")]
        with patch.object(terminal_ui, "available_console_ports", return_value=bluetooth):
            self.pane.connection_tabs.setCurrentIndex(1)
            self.assertEqual(self.pane.serial_port_combo.currentText(), "")
        with patch.object(terminal_ui, "available_console_ports", return_value=[
            ("/dev/cu.usbserial-AQ00KNX0", "FT232R USB UART"), *bluetooth
        ]):
            self.pane.refresh_serial_ports()
            self.assertEqual(self.pane.serial_port_combo.currentText(), "/dev/cu.usbserial-AQ00KNX0")
            self.pane.serial_port_combo.setCurrentIndex(1)
            self.pane.serial_port_combo.activated.emit(1)
            self.pane.refresh_serial_ports()
            self.assertEqual(self.pane.serial_port_combo.currentText(), "/dev/cu.BLTH")

    def test_console_invalid_baud_rate_is_shown_before_connecting(self):
        with patch.object(terminal_ui, "available_console_ports", return_value=[]):
            self.pane.connection_tabs.setCurrentIndex(1)
        self.pane.serial_port_combo.setCurrentText("/dev/cu.test")
        self.pane.baud_rate_combo.setCurrentText("bad")
        self.pane.connect_session()
        self.assertFalse(self.pane.is_active())
        self.assertIn("Baud rate must be a whole number", self.pane.status_label.text())

    def test_transcript_checkbox_is_per_session_and_starts_off(self):
        FakeTerminalSession.instances.clear()
        self.pane.address_edit.setText("192.0.2.1")
        self.pane.username_edit.setText("admin")
        self.pane.password_edit.setText("secret")
        self.assertFalse(self.pane.save_transcript_check.isChecked())
        self.assertFalse(self.pane.disable_paging_check.isChecked())
        with patch.object(terminal_ui, "TerminalSession", FakeTerminalSession):
            self.pane.connect_session()
            self.wait_for(lambda: bool(FakeTerminalSession.instances))
            self.assertIsNone(FakeTerminalSession.instances[-1].transcript_path)
            self.assertFalse(FakeTerminalSession.instances[-1].disable_paging)
            self.assertFalse(self.pane.save_transcript_check.isEnabled())
            self.assertFalse(self.pane.disable_paging_check.isEnabled())
            self.pane.bridge.sendInput("show version\r")
            self.pane.bridge.resizeTerminal(100, 35)
            self.assertEqual(FakeTerminalSession.instances[-1].sent, ["show version\r"])
            self.assertIn((100, 35), FakeTerminalSession.instances[-1].sizes)
            self.pane.hide()
            self.assertTrue(self.pane.is_active())
            self.pane.show()
            self.pane.disconnect_session()
            self.wait_for(lambda: not self.pane.is_active())
            self.assertFalse(self.pane.save_transcript_check.isChecked())

            self.pane.save_transcript_check.setChecked(True)
            self.pane.disable_paging_check.setChecked(True)
            self.pane.connect_session()
            self.wait_for(lambda: len(FakeTerminalSession.instances) == 2)
            self.assertTrue(FakeTerminalSession.instances[-1].disable_paging)
            path = FakeTerminalSession.instances[-1].transcript_path
            self.assertEqual(path.parent, self.base)
            self.assertIn("192.0.2.1", path.name)
            self.pane.disconnect_session()
            self.wait_for(lambda: not self.pane.is_active())
            self.assertFalse(self.pane.save_transcript_check.isChecked())
            self.assertTrue(self.pane.disable_paging_check.isEnabled())
