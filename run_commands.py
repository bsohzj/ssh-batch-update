#!/usr/bin/env python3
"""Run ordered exec/config command files against network devices over SSH."""

from __future__ import annotations

import argparse
import csv
import ipaddress
import os
import re
import sys
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence


class ConfigurationError(ValueError):
    """Raised when command, inventory, or runtime configuration is invalid."""


class CommandRejected(RuntimeError):
    """Raised when device output matches a configured failure pattern."""


class RunCancelled(RuntimeError):
    """Raised at a safe checkpoint after cancellation is requested."""


@dataclass(frozen=True)
class Settings:
    username: str
    password: str
    enable_secret: Optional[str]
    port: int
    timeout: int
    device_type: str


@dataclass(frozen=True)
class DeviceEntry:
    address: str
    error: str = ""


@dataclass(frozen=True)
class Command:
    section: str
    text: str
    line_number: int


@dataclass
class DeviceResult:
    address: str
    status: str
    transcript: str
    failed_section: str = ""
    failed_line: str = ""
    failed_command: str = ""
    transcript_file: str = ""
    error: str = ""


@dataclass(frozen=True)
class RunRequest:
    inventory: Optional[Path]
    command_file: Optional[Path]
    device_type: str
    output_dir: Path = Path("outputs")
    env_file: Optional[Path] = Path(".env")
    failure_patterns: tuple[str, ...] = ()
    apply: bool = False
    inventory_text: Optional[str] = None
    exec_commands_text: Optional[str] = None
    config_commands_text: Optional[str] = None


@dataclass(frozen=True)
class PreparedRun:
    request: RunRequest
    entries: tuple[DeviceEntry, ...]
    commands: tuple[Command, ...]
    failure_patterns: tuple[re.Pattern[str], ...]
    settings: Optional[Settings] = None

    @property
    def valid_entries(self) -> tuple[DeviceEntry, ...]:
        return tuple(entry for entry in self.entries if not entry.error)

    @property
    def invalid_entries(self) -> tuple[DeviceEntry, ...]:
        return tuple(entry for entry in self.entries if entry.error)


@dataclass(frozen=True)
class ProgressEvent:
    kind: str
    address: str = ""
    index: int = 0
    total: int = 0
    section: str = ""
    line_number: int = 0
    status: str = ""
    message: str = ""
    transcript_file: str = ""


@dataclass(frozen=True)
class BatchResult:
    results: tuple[DeviceResult, ...]
    run_directory: Path
    summary_path: Path

    @property
    def successes(self) -> int:
        return sum(result.status == "success" for result in self.results)

    @property
    def cancelled(self) -> int:
        return sum(result.status == "cancelled" for result in self.results)

    @property
    def failures(self) -> int:
        return len(self.results) - self.successes - self.cancelled


class CancellationToken:
    """Thread-safe cooperative cancellation flag."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise RunCancelled("cancelled by user")


HOSTNAME_RE = re.compile(
    r"(?=.{1,253}\Z)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z"
)

DEFAULT_FAILURE_PATTERNS = {
    "cisco": (
        r"^\s*%\s*(?:Invalid input|Incomplete command|Ambiguous command|Error)",
    ),
    "huawei": (
        r"^\s*(?:Error:|%\s*(?:Invalid input|Incomplete command|Ambiguous command))",
    ),
}


# Read basic KEY=VALUE entries if python-dotenv is unavailable.
def _dotenv_values_fallback(env_path: Path) -> dict[str, str]:

    values: dict[str, str] = {}
    if not env_path.is_file():
        return values
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key:
            values[key] = value
    return values


# Read an env file without modifying the process environment.
def load_environment(env_path: Path) -> dict[str, str]:

    try:
        from dotenv import dotenv_values
    except ImportError:
        return _dotenv_values_fallback(env_path)
    return {
        key: value
        for key, value in dotenv_values(dotenv_path=env_path).items()
        if value is not None
    }


# Return a required environment value, or explain which value is missing.
def _required_environment(name: str, values: Mapping[str, str]) -> str:

    value = values.get(name, "").strip()
    if not value:
        raise ConfigurationError(f"Missing required environment variable: {name}")
    return value


# Read a positive integer setting from the environment, using a default when absent.
def _integer_environment(name: str, default: int, values: Mapping[str, str]) -> int:

    raw_value = values.get(name, str(default)).strip()
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if value <= 0:
        raise ConfigurationError(f"{name} must be greater than zero")
    return value


# Load SSH settings without retaining values from an earlier env file.
def load_settings(
    device_type: str,
    env_path: Optional[Path],
    environment: Optional[Mapping[str, str]] = None,
) -> Settings:

    values = load_environment(env_path) if env_path is not None else {}
    values.update(os.environ if environment is None else environment)
    return Settings(
        username=_required_environment("SSH_USERNAME", values),
        password=_required_environment("SSH_PASSWORD", values),
        enable_secret=values.get("ENABLE_SECRET", "").strip() or None,
        port=_integer_environment("SSH_PORT", 22, values),
        timeout=_integer_environment("SSH_TIMEOUT", 15, values),
        device_type=device_type,
    )


# Validate an IP address or hostname and return its canonical form.
def _normalise_address(value: str) -> str:

    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        if HOSTNAME_RE.fullmatch(value):
            return value.lower()
    raise ValueError("not an IP address or hostname")


# Parse and de-duplicate IP addresses or DNS hostnames from inventory text.
def parse_device_entries(text: str) -> list[DeviceEntry]:

    entries: list[DeviceEntry] = []
    seen: set[tuple[str, str]] = set()
    for line_number, raw_line in enumerate(text.splitlines(), 1):
        value = raw_line.strip()
        if not value or value.startswith("#"):
            continue
        try:
            address = _normalise_address(value)
        except ValueError:
            key = ("invalid", value)
            if key not in seen:
                seen.add(key)
                entries.append(
                    DeviceEntry(value, f"invalid IP address or hostname on line {line_number}")
                )
            continue
        key = ("valid", address)
        if key not in seen:
            seen.add(key)
            entries.append(DeviceEntry(address))
    if not entries:
        raise ConfigurationError("no device entries found in the inventory file")
    return entries


# Read and de-duplicate IP addresses or DNS hostnames from an inventory file.
def read_device_entries(path: Path) -> list[DeviceEntry]:

    return parse_device_entries(path.read_text(encoding="utf-8"))


# Parse ordered [exec]/[config] command text without interpolation.
def parse_commands(text: str) -> list[Command]:

    commands: list[Command] = []
    section: Optional[str] = None
    for line_number, raw_line in enumerate(text.splitlines(), 1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            candidate = stripped[1:-1].lower()
            if candidate not in {"exec", "config"}:
                raise ConfigurationError(f"unknown section on line {line_number}: {stripped}")
            section = candidate
            continue
        if section is None:
            raise ConfigurationError(f"command outside a section on line {line_number}")
        commands.append(Command(section, raw_line, line_number))
    if not commands:
        raise ConfigurationError("no commands found in the command file")
    return commands


# Parse an ordered [exec]/[config] command file without interpolation.
def read_commands(path: Path) -> list[Command]:

    return parse_commands(path.read_text(encoding="utf-8"))


# Parse separate GUI command boxes; exec commands always precede config commands.
def parse_inline_commands(exec_text: str, config_text: str) -> list[Command]:

    commands: list[Command] = []
    for section, text in (("exec", exec_text), ("config", config_text)):
        for line_number, raw_line in enumerate(text.splitlines(), 1):
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith("[") and stripped.endswith("]"):
                raise ConfigurationError(
                    f"section headers are not allowed in the {section} command box "
                    f"(line {line_number})"
                )
            commands.append(Command(section, raw_line, line_number))
    if not commands:
        raise ConfigurationError("no commands found in the command boxes")
    return commands


# Compile vendor defaults and site-specific command failure patterns.
def compile_failure_patterns(device_type: str, custom_patterns: Iterable[str]) -> list[re.Pattern[str]]:

    lower_device_type = device_type.lower()
    defaults: tuple[str, ...] = ()
    if lower_device_type.startswith("cisco"):
        defaults = DEFAULT_FAILURE_PATTERNS["cisco"]
    elif lower_device_type.startswith("huawei"):
        defaults = DEFAULT_FAILURE_PATTERNS["huawei"]
    patterns = list(defaults) + list(custom_patterns)
    compiled: list[re.Pattern[str]] = []
    for pattern in patterns:
        try:
            compiled.append(re.compile(pattern, re.MULTILINE))
        except re.error as exc:
            raise ConfigurationError(f"invalid failure pattern {pattern!r}: {exc}") from exc
    return compiled


# Format an exception for reports while removing known connection secrets.
def _safe_error(exc: Exception, settings: Settings) -> str:

    message = str(exc) or exc.__class__.__name__
    for secret in (settings.password, settings.enable_secret):
        if secret:
            message = message.replace(secret, "[redacted]")
    return f"{exc.__class__.__name__}: {message}"


# Add an operation label and its device response to a transcript buffer.
def _append_transcript(transcript: list[str], label: str, response: Any) -> None:

    transcript.append(label)
    text = str(response)
    if text:
        transcript.append(text if text.endswith("\n") else f"{text}\n")


# Return the first failure regex matching a device response, if any.
def _rejected(response: Any, patterns: Iterable[re.Pattern[str]]) -> Optional[str]:

    text = str(response)
    for pattern in patterns:
        if pattern.search(text):
            return pattern.pattern
    return None


# Execute commands for one device and return its complete transcript and result.
def execute_device(
    address: str,
    settings: Settings,
    commands: Sequence[Command],
    failure_patterns: Sequence[re.Pattern[str]],
    netmiko_module: Any,
    progress: Optional[Callable[[str], None]] = None,
    event_callback: Optional[Callable[[ProgressEvent], None]] = None,
    cancellation_token: Optional[CancellationToken] = None,
) -> DeviceResult:

    transcript: list[str] = [f"Device: {address}\n", f"Device type: {settings.device_type}\n\n"]
    connection = None
    in_config_mode = False
    current: Optional[Command] = None
    result: Optional[DeviceResult] = None
    try:
        if cancellation_token:
            cancellation_token.raise_if_cancelled()
        parameters: dict[str, Any] = {
            "device_type": settings.device_type,
            "host": address,
            "username": settings.username,
            "password": settings.password,
            "port": settings.port,
            "timeout": settings.timeout,
        }
        if settings.enable_secret:
            parameters["secret"] = settings.enable_secret
        if progress:
            progress(f"CONNECTING {address}")
        if event_callback:
            event_callback(ProgressEvent("device_status", address=address, status="connecting"))
        connection = netmiko_module.ConnectHandler(**parameters)
        if cancellation_token:
            cancellation_token.raise_if_cancelled()
        if settings.enable_secret:
            if progress:
                progress(f"ENABLING {address}")
            if event_callback:
                event_callback(ProgressEvent("device_status", address=address, status="enabling"))
            _append_transcript(transcript, "=== enable ===\n", connection.enable())
        if progress:
            progress(f"DISABLING PAGER {address}")
        if event_callback:
            event_callback(
                ProgressEvent("device_status", address=address, status="preparing session")
            )
        _append_transcript(transcript, "=== disable paging ===\n", connection.disable_paging())

        for next_command in commands:
            if cancellation_token:
                cancellation_token.raise_if_cancelled()
            current = next_command
            if current.section == "config" and not in_config_mode:
                if progress:
                    progress(f"ENTERING CONFIG MODE {address}")
                _append_transcript(
                    transcript, "=== enter configuration mode ===\n", connection.config_mode()
                )
                in_config_mode = True
            elif current.section == "exec" and in_config_mode:
                if progress:
                    progress(f"EXITING CONFIG MODE {address}")
                _append_transcript(
                    transcript, "=== exit configuration mode ===\n", connection.exit_config_mode()
                )
                in_config_mode = False

            label = f"=== [{current.section}] line {current.line_number} ===\n> {current.text}\n"
            if progress:
                progress(
                    f"RUNNING {address} [{current.section}] line {current.line_number}: "
                    f"{current.text}"
                )
            if event_callback:
                event_callback(
                    ProgressEvent(
                        "command_started",
                        address=address,
                        section=current.section,
                        line_number=current.line_number,
                        status="running",
                        message=f"[{current.section}] line {current.line_number}",
                    )
                )
            if current.section == "exec":
                response = connection.send_command(current.text)
            else:
                response = connection.send_command_timing(current.text, cmd_verify=False)
            _append_transcript(transcript, label, response)
            pattern = _rejected(response, failure_patterns)
            if pattern:
                raise CommandRejected(f"device response matched failure pattern: {pattern}")
            if cancellation_token:
                cancellation_token.raise_if_cancelled()

        result = DeviceResult(address=address, status="success", transcript="")
    except RunCancelled as exc:
        result = DeviceResult(
            address=address,
            status="cancelled",
            transcript="",
            error=str(exc),
        )
    except Exception as exc:
        result = DeviceResult(
            address=address,
            status="failed",
            transcript="",
            failed_section=current.section if current else "",
            failed_line=str(current.line_number) if current else "",
            failed_command=current.text if current else "",
            error=_safe_error(exc, settings),
        )
    finally:
        if connection is not None:
            if in_config_mode:
                try:
                    if progress:
                        progress(f"EXITING CONFIG MODE {address}")
                    _append_transcript(
                        transcript, "=== exit configuration mode ===\n", connection.exit_config_mode()
                    )
                except Exception:
                    pass
            try:
                if progress:
                    progress(f"DISCONNECTING {address}")
                if event_callback:
                    event_callback(
                        ProgressEvent("device_status", address=address, status="disconnecting")
                    )
                connection.disconnect()
            except Exception:
                pass
    assert result is not None
    result.transcript = "".join(transcript)
    return result


# Convert a device address into a filename that is safe on common filesystems.
def _safe_filename(address: str) -> str:

    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", address)
    return cleaned.strip("._") or "device"


# Write a private text file through a temporary file to avoid partial reports.
def _atomic_write(path: Path, content: str) -> None:

    temporary_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.",
            suffix=".tmp", delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            os.chmod(temporary_path, 0o600)
            temporary_file.write(content)
            if content and not content.endswith("\n"):
                temporary_file.write("\n")
        temporary_path.replace(path)
    finally:
        if temporary_path and temporary_path.exists():
            temporary_path.unlink()


# Create a unique, private timestamped directory for one apply run.
def create_run_directory(output_dir: Path) -> Path:

    output_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(output_dir, 0o700)
    stem = f"run_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}"
    for suffix in range(1000):
        candidate = output_dir / (stem if suffix == 0 else f"{stem}_{suffix}")
        try:
            candidate.mkdir(mode=0o700)
        except FileExistsError:
            continue
        os.chmod(candidate, 0o700)
        return candidate
    raise OSError("could not create a unique timestamped run directory")


# Write result metadata without transcript content.
def write_summary(path: Path, results: Iterable[DeviceResult]) -> None:

    rows = list(results)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", newline="", dir=path.parent,
        prefix=f".{path.name}.", suffix=".tmp", delete=False,
    ) as temporary_file:
        temp_path = Path(temporary_file.name)
        try:
            os.chmod(temp_path, 0o600)
            writer = csv.DictWriter(
                temporary_file,
                fieldnames=(
                    "device", "status", "failed_section", "failed_line", "failed_command",
                    "transcript_file", "error",
                ),
            )
            writer.writeheader()
            for result in rows:
                writer.writerow(
                    {
                        "device": result.address,
                        "status": result.status,
                        "failed_section": result.failed_section,
                        "failed_line": result.failed_line,
                        "failed_command": result.failed_command,
                        "transcript_file": result.transcript_file,
                        "error": result.error,
                    }
                )
            temporary_file.flush()
            temp_path.replace(path)
        finally:
            if temp_path.exists():
                temp_path.unlink()


# Import Netmiko only for apply runs and give a useful install error if absent.
def _load_netmiko() -> Any:

    try:
        import netmiko
    except ImportError as exc:
        raise ConfigurationError(
            "Netmiko is not installed. Run: python3 -m pip install -r requirements.txt"
        ) from exc
    return netmiko


# Validate a run request and prepare immutable inputs for execution.
def prepare_run(
    request: RunRequest,
    require_credentials: bool = False,
    environment: Optional[Mapping[str, str]] = None,
) -> PreparedRun:

    device_type = request.device_type.strip()
    if not device_type:
        raise ConfigurationError("device type is required")
    if request.inventory_text is not None:
        entries = tuple(parse_device_entries(request.inventory_text))
    elif request.inventory is not None:
        entries = tuple(read_device_entries(request.inventory))
    else:
        raise ConfigurationError("inventory file or inventory text is required")

    if request.exec_commands_text is not None or request.config_commands_text is not None:
        commands = tuple(
            parse_inline_commands(
                request.exec_commands_text or "",
                request.config_commands_text or "",
            )
        )
    elif request.command_file is not None:
        commands = tuple(read_commands(request.command_file))
    else:
        raise ConfigurationError("command file or command text is required")
    patterns = tuple(compile_failure_patterns(device_type, request.failure_patterns))
    settings = None
    if require_credentials:
        settings = load_settings(device_type, request.env_file, environment=environment)
    return PreparedRun(request, entries, commands, patterns, settings)


# Execute a prepared run sequentially, emitting structured progress events.
def run_batch(
    prepared: PreparedRun,
    progress_callback: Optional[Callable[[ProgressEvent], None]] = None,
    cancellation_token: Optional[CancellationToken] = None,
    netmiko_module: Any = None,
    message_callback: Optional[Callable[[str], None]] = None,
) -> BatchResult:

    if not prepared.request.apply:
        raise ConfigurationError("run_batch requires a request with apply enabled")
    if prepared.settings is None:
        raise ConfigurationError("connection settings were not prepared")

    token = cancellation_token or CancellationToken()
    run_directory = create_run_directory(prepared.request.output_dir)
    valid_entries = prepared.valid_entries
    if valid_entries and netmiko_module is None:
        netmiko_module = _load_netmiko()

    total = len(prepared.entries)
    results: list[DeviceResult] = []
    if progress_callback:
        progress_callback(ProgressEvent("run_started", total=total, status="running"))

    for index, entry in enumerate(prepared.entries, 1):
        if progress_callback:
            progress_callback(
                ProgressEvent(
                    "device_started",
                    address=entry.address,
                    index=index,
                    total=total,
                    status="starting",
                )
            )

        if token.cancelled:
            result = DeviceResult(
                entry.address,
                "cancelled",
                "",
                error="cancelled before device started",
            )
        elif entry.error:
            result = DeviceResult(entry.address, "failed", "", error=entry.error)
        else:
            result = execute_device(
                entry.address,
                prepared.settings,
                prepared.commands,
                prepared.failure_patterns,
                netmiko_module,
                progress=message_callback,
                event_callback=progress_callback,
                cancellation_token=token,
            )
            transcript_path = run_directory / f"{_safe_filename(entry.address)}.txt"
            _atomic_write(transcript_path, result.transcript)
            result.transcript_file = str(transcript_path)

        results.append(result)
        if progress_callback:
            progress_callback(
                ProgressEvent(
                    "device_finished",
                    address=entry.address,
                    index=index,
                    total=total,
                    status=result.status,
                    message=result.error,
                    transcript_file=result.transcript_file,
                    section=result.failed_section,
                    line_number=int(result.failed_line) if result.failed_line.isdigit() else 0,
                )
            )

    summary_path = run_directory / "summary.csv"
    write_summary(summary_path, results)
    batch_result = BatchResult(tuple(results), run_directory, summary_path)
    if progress_callback:
        progress_callback(
            ProgressEvent(
                "run_finished",
                total=total,
                status="cancelled" if batch_result.cancelled else "finished",
                message=(
                    f"{batch_result.successes} succeeded, {batch_result.failures} failed, "
                    f"{batch_result.cancelled} cancelled"
                ),
            )
        )
    return batch_result


# Create the command-line interface and its defaults.
def build_parser() -> argparse.ArgumentParser:

    parser = argparse.ArgumentParser(
        description="Run [exec] and [config] command files against devices over SSH."
    )
    parser.add_argument("inventory", type=Path, help="one IP address or hostname per line")
    parser.add_argument("command_file", type=Path, help="ordered [exec]/[config] command file")
    parser.add_argument("--device-type", required=True, help="Netmiko device type, e.g. cisco_ios or huawei")
    parser.add_argument("--apply", action="store_true", help="connect and run commands (otherwise dry-run)")
    parser.add_argument("--output-dir", type=Path, default=Path("outputs"))
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument(
        "--failure-pattern", action="append", default=[], metavar="REGEX",
        help="additional regex that marks a device command as failed; may be repeated",
    )
    return parser


# Validate inputs, optionally run commands, write reports, and return an exit code.
def main(argv: Optional[Sequence[str]] = None) -> int:

    args = build_parser().parse_args(argv)
    request = RunRequest(
        inventory=args.inventory,
        command_file=args.command_file,
        device_type=args.device_type,
        output_dir=args.output_dir,
        env_file=args.env_file,
        failure_patterns=tuple(args.failure_pattern),
        apply=args.apply,
    )
    try:
        prepared = prepare_run(request, require_credentials=args.apply)
    except (ConfigurationError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    valid_entries = prepared.valid_entries
    invalid_entries = prepared.invalid_entries
    if not args.apply:
        print(
            f"DRY RUN: {len(valid_entries)} valid device(s), {len(invalid_entries)} invalid "
            f"device entry/entries, {len(prepared.commands)} command(s); no SSH sessions opened."
        )
        for entry in invalid_entries:
            print(f"INVALID {entry.address}: {entry.error}", file=sys.stderr)
        return 1 if invalid_entries else 0

    invalid_addresses = {entry.address for entry in invalid_entries}

    def report_event(event: ProgressEvent) -> None:
        if event.kind == "device_started" and event.address not in invalid_addresses:
            print(f"STARTING {event.index}/{event.total} {event.address}", flush=True)
        elif event.kind == "device_finished":
            if event.status == "success":
                print(f"SUCCESS {event.address} -> {event.transcript_file}")
            elif event.status == "cancelled":
                print(f"CANCELLED {event.address}: {event.message}", file=sys.stderr)
            else:
                print(f"FAILED  {event.address}: {event.message}", file=sys.stderr)

    try:
        batch_result = run_batch(
            prepared,
            progress_callback=report_event,
            message_callback=lambda message: print(message, flush=True),
        )
    except (ConfigurationError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    print(
        f"Completed: {batch_result.successes} succeeded, "
        f"{batch_result.failures} failed"
    )
    print(f"Summary: {batch_result.summary_path}")
    return 0 if batch_result.failures == 0 and batch_result.cancelled == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
