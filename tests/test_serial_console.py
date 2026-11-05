import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import serial

from serial_console import ConsoleSettings, SerialConsoleSession, available_console_ports


class SerialConsoleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_loopback_input_output_and_optional_transcript(self):
        for save in (False, True):
            with self.subTest(save=save):
                port = serial.serial_for_url("loop://", timeout=0, write_timeout=1)
                path = self.base / "console.txt" if save else None
                session = SerialConsoleSession(
                    ConsoleSettings("/dev/cu.test"), path,
                    serial_factory=lambda **kwargs: port,
                )
                output = []
                result = []

                def connected():
                    session.send("switch# showx")
                    session.send("\x7f")
                    session.send("\r\nOK\r\n")
                    session.send("\x03")

                def received(text):
                    output.append(text)
                    if "\x03" in text:
                        session.stop()

                thread = threading.Thread(
                    target=lambda: result.append(session.run(connected, received))
                )
                thread.start()
                thread.join(2)
                if thread.is_alive():
                    session.stop()
                    thread.join(2)
                    self.fail("Serial loopback did not finish")
                self.assertFalse(port.is_open)
                self.assertIn("switch# showx\x08\r\nOK\r\n\x03", "".join(output))
                self.assertEqual(result, [path])
                if save:
                    self.assertEqual(path.read_text(), "switch# show\nOK\n")
                else:
                    self.assertEqual(list(self.base.iterdir()), [])

    def test_serial_settings_are_applied_without_automatic_input(self):
        parameters = []
        port = serial.serial_for_url("loop://", timeout=0)

        def factory(**kwargs):
            parameters.append(kwargs)
            return port

        session = SerialConsoleSession(
            ConsoleSettings("/dev/cu.usbserial", 115200, 7, "E", 2, "rtscts"),
            serial_factory=factory,
        )
        session.run(session.stop, lambda text: None)
        self.assertEqual(parameters[0], {
            "port": "/dev/cu.usbserial", "baudrate": 115200, "bytesize": 7,
            "parity": "E", "stopbits": 2, "xonxoff": False, "rtscts": True,
            "timeout": 0, "write_timeout": 1, "exclusive": True,
        })
        self.assertFalse(port.is_open)

    def test_open_failure_does_not_create_log(self):
        path = self.base / "failed.txt"

        def factory(**kwargs):
            raise serial.SerialException("Port is busy")

        session = SerialConsoleSession(ConsoleSettings("/dev/cu.busy"), path, factory)
        with self.assertRaisesRegex(serial.SerialException, "busy"):
            session.run(lambda: None, lambda text: None)
        self.assertFalse(path.exists())

    def test_unplug_error_closes_port_and_preserves_partial_log(self):
        class UnpluggedPort:
            closed = False
            remaining = b"switch# "

            @property
            def in_waiting(self):
                if self.remaining:
                    return len(self.remaining)
                raise serial.SerialException("Device disconnected")

            def read(self, size):
                data, self.remaining = self.remaining, b""
                return data

            def close(self):
                self.closed = True

        port = UnpluggedPort()
        path = self.base / "partial.txt"
        session = SerialConsoleSession(
            ConsoleSettings("/dev/cu.removed"), path, lambda **kwargs: port
        )
        with self.assertRaisesRegex(serial.SerialException, "disconnected"):
            session.run(lambda: None, lambda text: None)
        self.assertTrue(port.closed)
        self.assertEqual(path.read_text(), "switch# \n")

    def test_cancel_before_start_does_not_open_port(self):
        session = SerialConsoleSession(
            ConsoleSettings("/dev/cu.test"),
            serial_factory=lambda **kwargs: self.fail("Port opened after cancellation"),
        )
        session.stop()
        self.assertIsNone(session.run(lambda: None, lambda text: None))

    def test_port_discovery_returns_sorted_paths(self):
        with patch("serial_console.list_ports.comports", return_value=[
            SimpleNamespace(device="/dev/cu.b", description="Adapter B", vid=None),
            SimpleNamespace(device="/dev/cu.a", description="Adapter A", vid=None),
        ]):
            self.assertEqual(available_console_ports(), [
                ("/dev/cu.a", "Adapter A"), ("/dev/cu.b", "Adapter B")
            ])

    def test_usb_adapters_precede_bluetooth_and_other_ports(self):
        with patch("serial_console.list_ports.comports", return_value=[
            SimpleNamespace(device="/dev/cu.BLTH", description="n/a", vid=None),
            SimpleNamespace(device="/dev/cu.other", description="Other", vid=None),
            SimpleNamespace(device="/dev/cu.usbserial-AQ00KNX0", description="FT232R USB UART", vid=0x403),
        ]):
            self.assertEqual([device for device, _ in available_console_ports()], [
                "/dev/cu.usbserial-AQ00KNX0", "/dev/cu.other", "/dev/cu.BLTH"
            ])

    def test_invalid_console_settings_are_rejected(self):
        for kwargs in ({"port": ""}, {"port": "x", "baudrate": 0},
                       {"port": "x", "parity": "invalid"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                ConsoleSettings(**kwargs)
