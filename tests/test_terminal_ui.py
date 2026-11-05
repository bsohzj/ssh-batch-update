import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtTest import QTest
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

    def __init__(self, address, settings, transcript_path, disable_paging=False):
        self.address = address
        self.settings = settings
        self.transcript_path = transcript_path
        self.disable_paging = disable_paging
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
        self.assertEqual(self.pane._connection_settings().port, 22)

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
