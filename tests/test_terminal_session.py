import queue
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from run_commands import Settings
from terminal_session import ReadableTranscript, TerminalSession, terminal_transcript_path
from terminal_ssh_settings import SSHAlgorithms


class FakeChannel:
    def __init__(self):
        self.chunks = queue.Queue()
        self.sent = []
        self.sizes = []
        self.closed = False

    def sendall(self, data):
        self.sent.append(data)
        if data == b"\r":
            self.chunks.put(b"switch# ")
        elif data == b"show version\r":
            self.chunks.put(b"show version\r\n\x1b[32mVersion 1\x1b[0m\r\nswitch# ")

    def resize_pty(self, width, height):
        self.sizes.append((width, height))

    def recv_ready(self):
        return not self.chunks.empty()

    def recv(self, size):
        return self.chunks.get_nowait()

    def exit_status_ready(self):
        return self.closed


class FakeConnection:
    def __init__(self, channel):
        self.remote_conn = channel
        self.disconnected = False
        self.enabled = False
        self.established = False

    def establish_connection(self):
        self.established = True

    def enable(self):
        self.enabled = True

    def disconnect(self):
        self.disconnected = True
        self.remote_conn.closed = True


class TerminalSessionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.settings = Settings("admin", "secret", None, 22, 15, "cisco_ios")

    def tearDown(self):
        self.temporary.cleanup()

    def run_fake_session(self, save):
        channel = FakeChannel()
        connection = FakeConnection(channel)
        parameters = []
        path = self.base / "session.txt" if save else None
        session = TerminalSession(
            "192.0.2.1",
            self.settings,
            path,
            connect_handler=lambda **kwargs: (parameters.append(kwargs), connection)[1],
        )
        connected = threading.Event()
        outputs = []
        result = []
        thread = threading.Thread(
            target=lambda: result.append(session.run(connected.set, outputs.append))
        )
        thread.start()
        self.assertTrue(connected.wait(2))
        session.send("show version\r")
        session.resize(100, 32)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not channel.sizes:
            time.sleep(0.01)
        self.assertEqual(channel.sizes, [(100, 32)])
        while time.monotonic() < deadline and not any("Version 1" in item for item in outputs):
            time.sleep(0.01)
        session.stop()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(parameters[0]["host"], "192.0.2.1")
        self.assertEqual(parameters[0]["device_type"], "cisco_ios")
        self.assertIs(parameters[0]["auto_connect"], False)
        self.assertTrue(connection.established)
        self.assertEqual(channel.sent, [b"\r", b"show version\r"])
        self.assertTrue(connection.disconnected)
        self.assertIn("Version 1", "".join(outputs))
        return result[0], path

    def test_saved_transcript_is_readable(self):
        result, path = self.run_fake_session(True)
        self.assertEqual(result, path)
        self.assertEqual(
            path.read_text(encoding="utf-8"),
            "switch# show version\nVersion 1\nswitch# \n",
        )

    def test_automatic_keeps_original_connection_parameters(self):
        connection = FakeConnection(FakeChannel())
        connection.remote_conn.closed = True
        with patch("netmiko.ConnectHandler", return_value=connection) as handler, \
             patch("terminal_session.restricted_connect_handler") as restricted:
            TerminalSession("192.0.2.1", self.settings).run(lambda: None, lambda text: None)
        restricted.assert_not_called()
        handler.assert_called_once_with(
            device_type="cisco_ios", host="192.0.2.1", username="admin", password="secret",
            port=22, timeout=15, conn_timeout=15, auto_connect=False,
        )

    def test_manual_connection_uses_scoped_handler_and_reports_no_fallback(self):
        choices = SSHAlgorithms(cipher="aes128-ctr")
        with patch("terminal_session.restricted_connect_handler", side_effect=ValueError("no compatible cipher")) as restricted, \
             patch("netmiko.ConnectHandler") as automatic:
            with self.assertRaisesRegex(RuntimeError, "cipher=aes128-ctr: no compatible cipher"):
                TerminalSession("192.0.2.1", self.settings, algorithms=choices).run(lambda: None, lambda text: None)
        self.assertEqual(restricted.call_args.args, (choices,))
        automatic.assert_not_called()

    def test_unchecked_session_creates_no_file(self):
        result, path = self.run_fake_session(False)
        self.assertIsNone(result)
        self.assertIsNone(path)
        self.assertEqual(list(self.base.iterdir()), [])

    def test_disable_paging_uses_netmiko_session_preparation(self):
        channel = FakeChannel()
        connection = FakeConnection(channel)
        parameters = []
        session = TerminalSession(
            "192.0.2.1",
            self.settings,
            connect_handler=lambda **kwargs: (parameters.append(kwargs), connection)[1],
            disable_paging=True,
        )

        def on_connected():
            session.stop()

        session.run(on_connected, lambda text: None)
        self.assertNotIn("auto_connect", parameters[0])
        self.assertFalse(connection.established)

    def test_connection_failure_creates_no_transcript(self):
        path = self.base / "failed.txt"
        session = TerminalSession(
            "192.0.2.1",
            self.settings,
            path,
            connect_handler=lambda **kwargs: (_ for _ in ()).throw(OSError("offline")),
        )
        with self.assertRaisesRegex(OSError, "offline"):
            session.run(lambda: None, lambda text: None)
        self.assertFalse(path.exists())

    def test_remote_closure_finishes_and_disconnects(self):
        channel = FakeChannel()
        connection = FakeConnection(channel)
        channel.chunks.put(b"goodbye\r\n")
        channel.closed = True
        outputs = []
        session = TerminalSession(
            "2001:db8::1",
            self.settings,
            connect_handler=lambda **kwargs: connection,
        )
        self.assertIsNone(session.run(lambda: None, outputs.append))
        self.assertIn("goodbye", "".join(outputs))
        self.assertTrue(connection.disconnected)

    def test_profile_enable_secret_enters_privileged_mode(self):
        channel = FakeChannel()
        connection = FakeConnection(channel)
        settings = Settings("admin", "secret", "enable-secret", 22, 15, "cisco_ios")
        session = TerminalSession(
            "192.0.2.1",
            settings,
            connect_handler=lambda **kwargs: connection,
        )

        def on_connected():
            session.stop()

        session.run(on_connected, lambda text: None)
        self.assertTrue(connection.enabled)

    def test_backspace_uses_control_h_for_supported_switches(self):
        channel = FakeChannel()
        connection = FakeConnection(channel)
        session = TerminalSession(
            "192.0.2.1",
            self.settings,
            connect_handler=lambda **kwargs: connection,
        )
        connected = threading.Event()
        thread = threading.Thread(
            target=lambda: session.run(connected.set, lambda text: None)
        )
        thread.start()
        self.assertTrue(connected.wait(2))
        session.send("\x7f")
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and len(channel.sent) < 2:
            time.sleep(0.01)
        session.stop()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(channel.sent, [b"\r", b"\x08"])

    def test_transcript_names_do_not_overwrite(self):
        now = datetime(2026, 11, 5, 10, 30, 0)
        first = terminal_transcript_path(self.base, "2001:db8::1", now)
        first.touch()
        second = terminal_transcript_path(self.base, "2001:db8::1", now)
        self.assertEqual(second.name, "terminal_20261105_103000_2001_db8__1_2.txt")

    def test_ansi_and_backspace_are_removed_from_readable_log(self):
        transcript = ReadableTranscript()
        text = transcript.feed("showx\b\r\n\x1b[31mred\x1b[0m")
        self.assertEqual(text + transcript.finish(), "show\nred\n")
