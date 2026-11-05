"""Local serial console transport for the interactive Terminal tab."""

from __future__ import annotations

import codecs
import queue
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import serial
from serial.tools import list_ports
from serial.tools.list_ports_common import ListPortInfo

from terminal_session import ReadableTranscript


def is_bluetooth_port(device: str, description: str = "") -> bool:
    return (
        "bluetooth" in f"{device} {description}".casefold()
        or Path(device).name.casefold() in ("cu.blth", "tty.blth")
    )


def available_console_ports() -> list[tuple[str, str]]:
    """Enumerate ports without opening them or writing to attached devices."""
    ports = list(list_ports.comports())

    def priority(port: ListPortInfo) -> tuple[int, str]:
        if is_bluetooth_port(port.device, port.description):
            rank = 2
        elif port.vid is not None or "usb" in port.device.casefold():
            rank = 0
        else:
            rank = 1
        return rank, port.device.casefold()

    return [(port.device, port.description) for port in sorted(ports, key=priority)]


@dataclass(frozen=True)
class ConsoleSettings:
    port: str
    baudrate: int = 9600
    bytesize: int = 8
    parity: str = "N"
    stopbits: float = 1
    flow_control: str = "none"

    def __post_init__(self) -> None:
        if not self.port.strip():
            raise ValueError("Select or enter a serial port.")
        if self.baudrate <= 0:
            raise ValueError("Baud rate must be greater than zero.")
        if self.bytesize not in (5, 6, 7, 8):
            raise ValueError("Unsupported data bits.")
        if self.parity not in ("N", "E", "O", "M", "S"):
            raise ValueError("Unsupported parity.")
        if self.stopbits not in (1, 1.5, 2):
            raise ValueError("Unsupported stop bits.")
        if self.flow_control not in ("none", "rtscts", "xonxoff"):
            raise ValueError("Unsupported flow control.")


class SerialConsoleSession:
    """Own a serial port on one worker thread; login is entirely interactive."""

    sensitive_values: tuple[str, ...] = ()

    def __init__(
        self,
        settings: ConsoleSettings,
        transcript_path: Optional[Path] = None,
        serial_factory: Callable[..., serial.Serial] = serial.Serial,
    ) -> None:
        self.settings = settings
        self.address = settings.port.strip()
        self.transcript_path = transcript_path
        self.serial_factory = serial_factory
        self._inputs: queue.SimpleQueue[bytes] = queue.SimpleQueue()
        self._stopping = threading.Event()

    def send(self, data: str) -> None:
        if not self._stopping.is_set():
            self._inputs.put(("\x08" if data == "\x7f" else data).encode("utf-8"))

    def resize(self, cols: int, rows: int) -> None:
        # A physical serial link has no SSH-style PTY resize operation.
        pass

    def stop(self) -> None:
        self._stopping.set()

    def run(
        self, on_connected: Callable[[], None], on_output: Callable[[str], None]
    ) -> Optional[Path]:
        port = None
        transcript = None
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        normalizer = ReadableTranscript()
        try:
            if self._stopping.is_set():
                return None
            port = self.serial_factory(
                port=self.address,
                baudrate=self.settings.baudrate,
                bytesize=self.settings.bytesize,
                parity=self.settings.parity,
                stopbits=self.settings.stopbits,
                xonxoff=self.settings.flow_control == "xonxoff",
                rtscts=self.settings.flow_control == "rtscts",
                timeout=0,
                write_timeout=1,
                exclusive=True,
            )
            if self._stopping.is_set():
                return None
            if self.transcript_path is not None:
                self.transcript_path.parent.mkdir(parents=True, exist_ok=True)
                transcript = self.transcript_path.open("x", encoding="utf-8")
            on_connected()
            while not self._stopping.is_set():
                while not self._stopping.is_set():
                    try:
                        data = self._inputs.get_nowait()
                    except queue.Empty:
                        break
                    while data and not self._stopping.is_set():
                        written = port.write(data)
                        if not written:
                            raise serial.SerialTimeoutException("Serial write did not complete.")
                        data = data[written:]
                waiting = port.in_waiting
                if waiting:
                    chunk = decoder.decode(port.read(min(waiting, 65536)))
                    if chunk:
                        on_output(chunk)
                        if transcript is not None:
                            transcript.write(normalizer.feed(chunk))
                else:
                    self._stopping.wait(0.03)
            tail = decoder.decode(b"", final=True)
            if tail:
                on_output(tail)
                if transcript is not None:
                    transcript.write(normalizer.feed(tail))
            return self.transcript_path
        finally:
            try:
                if port is not None:
                    port.close()
            finally:
                if transcript is not None:
                    try:
                        transcript.write(normalizer.finish())
                    finally:
                        transcript.close()
