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
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence


class ConfigurationError(ValueError):
    """Raised when command, inventory, or runtime configuration is invalid."""


class CommandRejected(RuntimeError):
    """Raised when device output matches a configured failure pattern."""


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


def _load_dotenv_fallback(env_path: Path) -> None:
    """Load basic KEY=VALUE entries if python-dotenv is unavailable."""

    if not env_path.is_file():
        return
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
            os.environ.setdefault(key, value)


def load_environment(env_path: Path) -> None:
    """Load an env file without replacing values already in the environment."""

    try:
        from dotenv import load_dotenv
    except ImportError:
        _load_dotenv_fallback(env_path)
    else:
        load_dotenv(dotenv_path=env_path, override=False)


def _required_environment(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigurationError(f"Missing required environment variable: {name}")
    return value


def _integer_environment(name: str, default: int) -> int:
    raw_value = os.getenv(name, str(default)).strip()
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if value <= 0:
        raise ConfigurationError(f"{name} must be greater than zero")
    return value


def load_settings(device_type: str, env_path: Path) -> Settings:
    load_environment(env_path)
    return Settings(
        username=_required_environment("SSH_USERNAME"),
        password=_required_environment("SSH_PASSWORD"),
        enable_secret=os.getenv("ENABLE_SECRET", "").strip() or None,
        port=_integer_environment("SSH_PORT", 22),
        timeout=_integer_environment("SSH_TIMEOUT", 15),
        device_type=device_type,
    )


def _normalise_address(value: str) -> str:
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        if HOSTNAME_RE.fullmatch(value):
            return value.lower()
    raise ValueError("not an IP address or hostname")


def read_device_entries(path: Path) -> list[DeviceEntry]:
    """Read and de-duplicate IP addresses or DNS hostnames from an inventory."""

    entries: list[DeviceEntry] = []
    seen: set[tuple[str, str]] = set()
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
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


def read_commands(path: Path) -> list[Command]:
    """Parse an ordered [exec]/[config] command file without interpolation."""

    commands: list[Command] = []
    section: Optional[str] = None
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
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


def compile_failure_patterns(device_type: str, custom_patterns: Iterable[str]) -> list[re.Pattern[str]]:
    """Compile vendor defaults and site-specific command failure patterns."""

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


def _safe_error(exc: Exception, settings: Settings) -> str:
    message = str(exc) or exc.__class__.__name__
    for secret in (settings.password, settings.enable_secret):
        if secret:
            message = message.replace(secret, "[redacted]")
    return f"{exc.__class__.__name__}: {message}"


def _append_transcript(transcript: list[str], label: str, response: Any) -> None:
    transcript.append(label)
    text = str(response)
    if text:
        transcript.append(text if text.endswith("\n") else f"{text}\n")


def _rejected(response: Any, patterns: Iterable[re.Pattern[str]]) -> Optional[str]:
    text = str(response)
    for pattern in patterns:
        if pattern.search(text):
            return pattern.pattern
    return None


def execute_device(
    address: str,
    settings: Settings,
    commands: Sequence[Command],
    failure_patterns: Sequence[re.Pattern[str]],
    netmiko_module: Any,
    progress: Optional[Callable[[str], None]] = None,
) -> DeviceResult:
    """Execute commands for one device and return its full transcript/result."""

    transcript: list[str] = [f"Device: {address}\n", f"Device type: {settings.device_type}\n\n"]
    connection = None
    in_config_mode = False
    current: Optional[Command] = None
    result: Optional[DeviceResult] = None
    try:
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
        connection = netmiko_module.ConnectHandler(**parameters)
        if settings.enable_secret:
            if progress:
                progress(f"ENABLING {address}")
            _append_transcript(transcript, "=== enable ===\n", connection.enable())
        if progress:
            progress(f"DISABLING PAGER {address}")
        _append_transcript(transcript, "=== disable paging ===\n", connection.disable_paging())

        for current in commands:
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
            if current.section == "exec":
                response = connection.send_command(current.text)
            else:
                response = connection.send_command_timing(current.text, cmd_verify=False)
            _append_transcript(transcript, label, response)
            pattern = _rejected(response, failure_patterns)
            if pattern:
                raise CommandRejected(f"device response matched failure pattern: {pattern}")

        result = DeviceResult(address=address, status="success", transcript="")
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
                connection.disconnect()
            except Exception:
                pass
    assert result is not None
    result.transcript = "".join(transcript)
    return result


def _safe_filename(address: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", address)
    return cleaned.strip("._") or "device"


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


def create_run_directory(output_dir: Path) -> Path:
    """Create a unique, private timestamped directory for one apply run."""

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


def write_summary(path: Path, results: Iterable[DeviceResult]) -> None:
    """Write result metadata without transcript content."""

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


def _load_netmiko() -> Any:
    try:
        import netmiko
    except ImportError as exc:
        raise ConfigurationError(
            "Netmiko is not installed. Run: python3 -m pip install -r requirements.txt"
        ) from exc
    return netmiko


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


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        entries = read_device_entries(args.inventory)
        commands = read_commands(args.command_file)
        patterns = compile_failure_patterns(args.device_type, args.failure_pattern)
    except (ConfigurationError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    valid_entries = [entry for entry in entries if not entry.error]
    invalid_entries = [entry for entry in entries if entry.error]
    if not args.apply:
        print(
            f"DRY RUN: {len(valid_entries)} valid device(s), {len(invalid_entries)} invalid "
            f"device entry/entries, {len(commands)} command(s); no SSH sessions opened."
        )
        for entry in invalid_entries:
            print(f"INVALID {entry.address}: {entry.error}", file=sys.stderr)
        return 1 if invalid_entries else 0

    try:
        settings = load_settings(args.device_type, args.env_file)
        run_directory = create_run_directory(args.output_dir)
        netmiko_module = _load_netmiko() if valid_entries else None
    except (ConfigurationError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    results: list[DeviceResult] = []
    try:
        for index, entry in enumerate(entries, 1):
            if entry.error:
                result = DeviceResult(entry.address, "failed", "", error=entry.error)
            else:
                print(f"STARTING {index}/{len(entries)} {entry.address}", flush=True)
                result = execute_device(
                    entry.address,
                    settings,
                    commands,
                    patterns,
                    netmiko_module,
                    progress=lambda message: print(message, flush=True),
                )
                transcript_path = run_directory / f"{_safe_filename(entry.address)}.txt"
                _atomic_write(transcript_path, result.transcript)
                result.transcript_file = str(transcript_path)
            results.append(result)
            if result.status == "success":
                print(f"SUCCESS {entry.address} -> {result.transcript_file}")
            else:
                print(f"FAILED  {entry.address}: {result.error}", file=sys.stderr)
        write_summary(run_directory / "summary.csv", results)
    except OSError as exc:
        print(f"ERROR: could not write report: {exc}", file=sys.stderr)
        return 2

    successes = sum(result.status == "success" for result in results)
    failures = len(results) - successes
    print(f"Completed: {successes} succeeded, {failures} failed")
    print(f"Summary: {run_directory / 'summary.csv'}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
