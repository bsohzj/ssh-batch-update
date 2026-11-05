"""Interactive, single-device SSH session used by the GUI Terminal tab."""

from __future__ import annotations

import codecs
import ipaddress
import queue
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

from run_commands import Settings


class TerminalError(RuntimeError):
    """A terminal connection or transcript could not be started."""


class InteractiveSession(Protocol):
    address: str
    transcript_path: Optional[Path]

    @property
    def sensitive_values(self) -> tuple[str, ...]: ...
    def send(self, data: str) -> None: ...
    def resize(self, cols: int, rows: int) -> None: ...
    def stop(self) -> None: ...
    def run(
        self, on_connected: Callable[[], None], on_output: Callable[[str], None]
    ) -> Optional[Path]: ...


def terminal_transcript_path(folder: Path, address: str, now: datetime) -> Path:
    """Choose a unique, filesystem-safe name without replacing an earlier log."""

    safe_address = re.sub(r"[^A-Za-z0-9._-]", "_", address)
    stem = f"terminal_{now:%Y%m%d_%H%M%S}_{safe_address}"
    candidate = folder / f"{stem}.txt"
    suffix = 2
    while candidate.exists():
        candidate = folder / f"{stem}_{suffix}.txt"
        suffix += 1
    return candidate


class ReadableTranscript:
    """Turn common VT control sequences into a readable, streaming CLI log."""

    def __init__(self) -> None:
        self._state = "text"
        self._line = ""
        self._pending_cr = False

    def feed(self, chunk: str) -> str:
        completed: list[str] = []
        for char in chunk:
            if self._state == "escape":
                self._state = "csi" if char == "[" else "osc" if char == "]" else "text"
                continue
            if self._state == "csi":
                if "@" <= char <= "~":
                    self._state = "text"
                continue
            if self._state == "osc":
                if char == "\x07":
                    self._state = "text"
                elif char == "\x1b":
                    self._state = "osc_escape"
                continue
            if self._state == "osc_escape":
                self._state = "text" if char == "\\" else "osc"
                continue
            if char == "\x1b":
                self._state = "escape"
            elif char == "\r":
                if self._pending_cr:
                    completed.append(self._line + "\n")
                    self._line = ""
                self._pending_cr = True
            elif char == "\n":
                completed.append(self._line + "\n")
                self._line = ""
                self._pending_cr = False
            elif char in "\b\x7f":
                self._line = self._line[:-1]
            elif char.isprintable():
                if self._pending_cr:
                    completed.append(self._line + "\n")
                    self._line = ""
                    self._pending_cr = False
                self._line += char
        return "".join(completed)

    def finish(self) -> str:
        remaining = self._line + ("\n" if self._line else "")
        self._line = ""
        return remaining


class TerminalSession:
    """Own one Netmiko SSH channel; all channel operations run on one thread."""

    def __init__(
        self,
        address: str,
        settings: Settings,
        transcript_path: Optional[Path] = None,
        connect_handler: Optional[Callable[..., Any]] = None,
        disable_paging: bool = False,
    ) -> None:
        self.address = str(ipaddress.ip_address(address.strip()))
        if not settings.device_type.strip():
            raise TerminalError("Device type is required.")
        if not settings.username.strip() or not settings.password:
            raise TerminalError("Username and password are required.")
        self.settings = settings
        self.transcript_path = transcript_path
        self.connect_handler = connect_handler
        self.disable_paging = disable_paging
        self._requests: queue.SimpleQueue[tuple[str, Any]] = queue.SimpleQueue()
        self._stopping = threading.Event()

    @property
    def sensitive_values(self) -> tuple[str, ...]:
        return tuple(
            value for value in (
                self.settings.username, self.settings.password, self.settings.enable_secret
            ) if value
        )

    def send(self, data: str) -> None:
        if not self._stopping.is_set():
            if data == "\x7f" and self.settings.device_type.lower().startswith(
                ("cisco", "huawei")
            ):
                data = "\x08"
            self._requests.put(("input", data))

    def resize(self, cols: int, rows: int) -> None:
        if cols > 1 and rows > 0:
            self._requests.put(("resize", (cols, rows)))

    def stop(self) -> None:
        self._stopping.set()

    def run(
        self,
        on_connected: Callable[[], None],
        on_output: Callable[[str], None],
    ) -> Optional[Path]:
        import netmiko

        handler = self.connect_handler or netmiko.ConnectHandler
        connection = None
        transcript = None
        normalizer = ReadableTranscript()
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        try:
            if self._stopping.is_set():
                return None
            parameters: dict[str, Any] = {
                "device_type": self.settings.device_type,
                "host": self.address,
                "username": self.settings.username,
                "password": self.settings.password,
                "port": self.settings.port,
                "timeout": self.settings.timeout,
                "conn_timeout": self.settings.timeout,
            }
            if self.settings.enable_secret:
                parameters["secret"] = self.settings.enable_secret
            if not self.disable_paging:
                parameters["auto_connect"] = False
            connection = handler(**parameters)
            if not self.disable_paging:
                connection.establish_connection()
            channel = connection.remote_conn
            if channel is None:
                raise TerminalError("SSH channel was not created.")
            if self._stopping.is_set():
                return None
            if self.settings.enable_secret:
                connection.enable()
            if self._stopping.is_set():
                return None
            if self.transcript_path is not None:
                self.transcript_path.parent.mkdir(parents=True, exist_ok=True)
                transcript = self.transcript_path.open("x", encoding="utf-8")
            on_connected()
            if not self._stopping.is_set():
                channel.sendall(b"\r")
            while not self._stopping.is_set():
                while True:
                    try:
                        action, value = self._requests.get_nowait()
                    except queue.Empty:
                        break
                    if action == "input":
                        channel.sendall(value.encode("utf-8"))
                    elif action == "resize":
                        cols, rows = value
                        channel.resize_pty(width=cols, height=rows)
                if channel.recv_ready():
                    chunk = decoder.decode(channel.recv(65536))
                    if chunk:
                        on_output(chunk)
                        if transcript is not None:
                            transcript.write(normalizer.feed(chunk))
                elif channel.closed or channel.exit_status_ready():
                    break
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
                if connection is not None:
                    connection.disconnect()
            finally:
                if transcript is not None:
                    try:
                        transcript.write(normalizer.finish())
                    finally:
                        transcript.close()
