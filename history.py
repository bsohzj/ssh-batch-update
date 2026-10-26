"""Persistent, non-secret audit history for GUI live batches."""

from __future__ import annotations

import csv
import json
import os
import shutil
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator, Optional, Protocol

from PySide6.QtCore import QStandardPaths

from run_commands import Command, DeviceResult


HISTORY_COMMAND_SERVICE = "com.sg4291.sshbatchupdate.history.commands"
HISTORY_COMMAND_VERSION = 1
HISTORY_MARKER = ".ssh-batch-update-history"
HISTORY_STATUSES = (
    "running",
    "succeeded",
    "completed_with_failures",
    "failed",
    "cancelled",
    "interrupted",
)


class HistoryError(RuntimeError):
    """Raised when audit history cannot be safely read or changed."""


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.astimezone()
    return value.astimezone(timezone.utc)


def _iso(value: Optional[datetime]) -> str:
    return _as_utc(value).isoformat().replace("+00:00", "Z") if value else ""


def _parse(value: str) -> Optional[datetime]:
    return (
        datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
        if value
        else None
    )


def default_history_path() -> Path:
    root = QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.AppDataLocation
    )
    if not root:
        root = str(Path.home() / "Library" / "Application Support" / "SSH Batch Update")
    return Path(root) / "history.sqlite3"


@dataclass(frozen=True)
class HistoryRecord:
    job_id: str
    batch_name: str
    created_utc: datetime
    scheduled_utc: Optional[datetime]
    started_utc: datetime
    completed_utc: Optional[datetime]
    source: str
    profile_label: str
    device_type: str
    valid_devices: int
    invalid_devices: int
    command_count: int
    status: str = "running"
    success_count: int = 0
    failure_count: int = 0
    cancelled_count: int = 0
    result_text: str = ""
    run_directory: str = ""
    summary_path: str = ""

    @property
    def duration_seconds(self) -> Optional[int]:
        if self.completed_utc is None:
            return None
        return max(0, int((self.completed_utc - self.started_utc).total_seconds()))


@dataclass(frozen=True)
class HistoryFilter:
    since_utc: Optional[datetime] = None
    search: str = ""
    status: str = ""
    source: str = ""
    device_type: str = ""
    profile_label: str = ""


@dataclass(frozen=True)
class HistoryStats:
    runs: int
    successes: int
    failures: int
    cancelled: int

    @property
    def success_rate(self) -> float:
        attempted = self.successes + self.failures
        return (self.successes / attempted * 100.0) if attempted else 0.0


@dataclass(frozen=True)
class HistoryBucket:
    label: str
    successes: int
    failures: int


@dataclass(frozen=True)
class HistoryCommandSnapshot:
    commands: tuple[Command, ...]

    def to_json(self) -> str:
        return json.dumps(
            {
                "version": HISTORY_COMMAND_VERSION,
                "commands": [
                    {
                        "section": command.section,
                        "line_number": command.line_number,
                        "text": command.text,
                    }
                    for command in self.commands
                ],
            },
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, value: str) -> "HistoryCommandSnapshot":
        try:
            data = json.loads(value)
            if data.get("version") != HISTORY_COMMAND_VERSION:
                raise ValueError("unsupported command snapshot version")
            commands = tuple(
                Command(
                    section=str(item["section"]),
                    text=str(item["text"]),
                    line_number=int(item["line_number"]),
                )
                for item in data["commands"]
            )
            if any(
                command.section not in {"exec", "config"}
                or command.line_number < 1
                or not command.text
                for command in commands
            ):
                raise ValueError("invalid command entry")
            return cls(commands)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise HistoryError(f"The stored command snapshot is invalid: {exc}") from exc


class HistoryCommandStore(Protocol):
    def set(self, job_id: str, snapshot: HistoryCommandSnapshot) -> None: ...

    def get(self, job_id: str) -> HistoryCommandSnapshot: ...

    def delete(self, job_id: str) -> None: ...


class KeychainHistoryCommandStore:
    def __init__(self, service: str = HISTORY_COMMAND_SERVICE) -> None:
        self.service = service

    @staticmethod
    def _keyring():
        try:
            import keyring
        except ImportError as exc:
            raise HistoryError("The keyring dependency is unavailable.") from exc
        return keyring

    def set(self, job_id: str, snapshot: HistoryCommandSnapshot) -> None:
        try:
            self._keyring().set_password(self.service, job_id, snapshot.to_json())
        except Exception as exc:
            raise HistoryError(f"Could not store commands in macOS Keychain: {exc}") from exc

    def get(self, job_id: str) -> HistoryCommandSnapshot:
        try:
            value = self._keyring().get_password(self.service, job_id)
        except Exception as exc:
            raise HistoryError(f"Could not read commands from macOS Keychain: {exc}") from exc
        if value is None:
            raise HistoryError("The command snapshot is missing from macOS Keychain.")
        return HistoryCommandSnapshot.from_json(value)

    def delete(self, job_id: str) -> None:
        try:
            self._keyring().delete_password(self.service, job_id)
        except Exception as exc:
            if exc.__class__.__name__ == "PasswordDeleteError" and "not found" in str(exc).lower():
                return
            raise HistoryError(f"Could not remove commands from macOS Keychain: {exc}") from exc


class MemoryHistoryCommandStore:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def set(self, job_id: str, snapshot: HistoryCommandSnapshot) -> None:
        self.values[job_id] = snapshot.to_json()

    def get(self, job_id: str) -> HistoryCommandSnapshot:
        try:
            return HistoryCommandSnapshot.from_json(self.values[job_id])
        except KeyError as exc:
            raise HistoryError("The command snapshot is missing.") from exc

    def delete(self, job_id: str) -> None:
        self.values.pop(job_id, None)


class HistoryStore:
    """SQLite-backed history containing no commands, credentials, or device addresses."""

    def __init__(
        self,
        path: Path,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.path = Path(path)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.path.parent, 0o700)
            self._initialize()
            os.chmod(self.path, 0o600)
        except (OSError, sqlite3.Error) as exc:
            raise HistoryError(f"Could not initialize audit history: {exc}") from exc

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS history (
                    job_id TEXT PRIMARY KEY,
                    batch_name TEXT NOT NULL,
                    created_utc TEXT NOT NULL,
                    scheduled_utc TEXT NOT NULL DEFAULT '',
                    started_utc TEXT NOT NULL,
                    completed_utc TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL,
                    profile_label TEXT NOT NULL,
                    device_type TEXT NOT NULL,
                    valid_devices INTEGER NOT NULL,
                    invalid_devices INTEGER NOT NULL,
                    command_count INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    success_count INTEGER NOT NULL DEFAULT 0,
                    failure_count INTEGER NOT NULL DEFAULT 0,
                    cancelled_count INTEGER NOT NULL DEFAULT 0,
                    result_text TEXT NOT NULL DEFAULT '',
                    run_directory TEXT NOT NULL DEFAULT '',
                    summary_path TEXT NOT NULL DEFAULT ''
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS history_started ON history(started_utc DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS history_status ON history(status)"
            )
            connection.execute("PRAGMA user_version = 1")

    def start(self, record: HistoryRecord) -> None:
        values = self._record_values(record)
        columns = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)
        try:
            with self._connect() as connection:
                connection.execute(
                    f"INSERT OR REPLACE INTO history ({columns}) VALUES ({placeholders})",
                    tuple(values.values()),
                )
        except sqlite3.Error as exc:
            raise HistoryError(f"Could not start audit record: {exc}") from exc

    def finalize(
        self,
        job_id: str,
        status: str,
        successes: int,
        failures: int,
        cancelled: int,
        result_text: str,
        run_directory: str = "",
        summary_path: str = "",
        completed_utc: Optional[datetime] = None,
    ) -> None:
        if status not in HISTORY_STATUSES or status == "running":
            raise HistoryError(f"Invalid final history status: {status}")
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    """
                    UPDATE history SET completed_utc = ?, status = ?, success_count = ?,
                        failure_count = ?, cancelled_count = ?, result_text = ?,
                        run_directory = ?, summary_path = ?
                    WHERE job_id = ?
                    """,
                    (
                        _iso(completed_utc or self.clock()),
                        status,
                        successes,
                        failures,
                        cancelled,
                        result_text,
                        run_directory,
                        summary_path,
                        job_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise HistoryError("The active audit record is missing.")
        except sqlite3.Error as exc:
            raise HistoryError(f"Could not finalize audit record: {exc}") from exc

    def recover_interrupted(self) -> int:
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    """
                    UPDATE history SET status = 'interrupted', completed_utc = ?,
                        result_text = 'Application closed before run completion'
                    WHERE status = 'running'
                    """,
                    (_iso(self.clock()),),
                )
                return cursor.rowcount
        except sqlite3.Error as exc:
            raise HistoryError(f"Could not recover interrupted audit records: {exc}") from exc

    def get(self, job_id: str) -> Optional[HistoryRecord]:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT * FROM history WHERE job_id = ?", (job_id,)
                ).fetchone()
        except sqlite3.Error as exc:
            raise HistoryError(f"Could not read audit record: {exc}") from exc
        return self._row_record(row) if row else None

    def list(
        self,
        filters: HistoryFilter,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[HistoryRecord], int]:
        where, parameters = self._where(filters)
        try:
            with self._connect() as connection:
                total = int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM history {where}", parameters
                    ).fetchone()[0]
                )
                rows = connection.execute(
                    f"SELECT * FROM history {where} ORDER BY started_utc DESC LIMIT ? OFFSET ?",
                    (*parameters, limit, offset),
                ).fetchall()
        except sqlite3.Error as exc:
            raise HistoryError(f"Could not list audit history: {exc}") from exc
        return [self._row_record(row) for row in rows], total

    def stats(self, filters: HistoryFilter) -> HistoryStats:
        where, parameters = self._where(filters)
        try:
            with self._connect() as connection:
                row = connection.execute(
                    f"""
                    SELECT COUNT(*), COALESCE(SUM(success_count), 0),
                        COALESCE(SUM(failure_count), 0), COALESCE(SUM(cancelled_count), 0)
                    FROM history {where}
                    """,
                    parameters,
                ).fetchone()
        except sqlite3.Error as exc:
            raise HistoryError(f"Could not calculate history statistics: {exc}") from exc
        return HistoryStats(*(int(value) for value in row))

    def timeline(self, filters: HistoryFilter, granularity: str) -> list[HistoryBucket]:
        where, parameters = self._where(filters)
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    f"SELECT started_utc, success_count, failure_count FROM history {where} "
                    "ORDER BY started_utc",
                    parameters,
                ).fetchall()
        except sqlite3.Error as exc:
            raise HistoryError(f"Could not calculate history timeline: {exc}") from exc
        buckets: dict[str, list[int]] = {}
        for row in rows:
            started = _parse(row["started_utc"])
            if started is None:
                continue
            local = started.astimezone()
            if granularity == "monthly":
                label = local.strftime("%Y-%m")
            elif granularity == "weekly":
                monday = local.date() - timedelta(days=local.weekday())
                label = monday.isoformat()
            else:
                label = local.date().isoformat()
            totals = buckets.setdefault(label, [0, 0])
            totals[0] += int(row["success_count"])
            totals[1] += int(row["failure_count"])
        return [HistoryBucket(label, *values) for label, values in buckets.items()]

    def status_counts(self, filters: HistoryFilter) -> dict[str, int]:
        where, parameters = self._where(filters)
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    f"SELECT status, COUNT(*) AS total FROM history {where} GROUP BY status",
                    parameters,
                ).fetchall()
        except sqlite3.Error as exc:
            raise HistoryError(f"Could not calculate status totals: {exc}") from exc
        return {str(row["status"]): int(row["total"]) for row in rows}

    def distinct(self, column: str) -> list[str]:
        if column not in {"device_type", "profile_label"}:
            raise ValueError("unsupported history field")
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    f"SELECT DISTINCT {column} FROM history WHERE {column} != '' ORDER BY {column}"
                ).fetchall()
        except sqlite3.Error as exc:
            raise HistoryError(f"Could not list history filters: {exc}") from exc
        return [str(row[0]) for row in rows]

    def remove(self, job_id: str) -> None:
        try:
            with self._connect() as connection:
                connection.execute("DELETE FROM history WHERE job_id = ?", (job_id,))
        except sqlite3.Error as exc:
            raise HistoryError(f"Could not remove audit record: {exc}") from exc

    def clear(self) -> list[str]:
        try:
            with self._connect() as connection:
                ids = [str(row[0]) for row in connection.execute("SELECT job_id FROM history")]
                connection.execute("DELETE FROM history")
        except sqlite3.Error as exc:
            raise HistoryError(f"Could not clear audit history: {exc}") from exc
        return ids

    def _where(self, filters: HistoryFilter) -> tuple[str, tuple[object, ...]]:
        clauses: list[str] = []
        parameters: list[object] = []
        if filters.since_utc:
            clauses.append("started_utc >= ?")
            parameters.append(_iso(filters.since_utc))
        if filters.search.strip():
            clauses.append("LOWER(batch_name) LIKE ?")
            parameters.append(f"%{filters.search.strip().lower()}%")
        for column, value in (
            ("status", filters.status),
            ("source", filters.source),
            ("device_type", filters.device_type),
            ("profile_label", filters.profile_label),
        ):
            if value:
                clauses.append(f"{column} = ?")
                parameters.append(value)
        return ("WHERE " + " AND ".join(clauses) if clauses else "", tuple(parameters))

    @staticmethod
    def _record_values(record: HistoryRecord) -> dict[str, object]:
        return {
            "job_id": record.job_id,
            "batch_name": record.batch_name,
            "created_utc": _iso(record.created_utc),
            "scheduled_utc": _iso(record.scheduled_utc),
            "started_utc": _iso(record.started_utc),
            "completed_utc": _iso(record.completed_utc),
            "source": record.source,
            "profile_label": record.profile_label,
            "device_type": record.device_type,
            "valid_devices": record.valid_devices,
            "invalid_devices": record.invalid_devices,
            "command_count": record.command_count,
            "status": record.status,
            "success_count": record.success_count,
            "failure_count": record.failure_count,
            "cancelled_count": record.cancelled_count,
            "result_text": record.result_text,
            "run_directory": record.run_directory,
            "summary_path": record.summary_path,
        }

    @staticmethod
    def _row_record(row: sqlite3.Row) -> HistoryRecord:
        return HistoryRecord(
            job_id=str(row["job_id"]),
            batch_name=str(row["batch_name"]),
            created_utc=_parse(row["created_utc"]),  # type: ignore[arg-type]
            scheduled_utc=_parse(row["scheduled_utc"]),
            started_utc=_parse(row["started_utc"]),  # type: ignore[arg-type]
            completed_utc=_parse(row["completed_utc"]),
            source=str(row["source"]),
            profile_label=str(row["profile_label"]),
            device_type=str(row["device_type"]),
            valid_devices=int(row["valid_devices"]),
            invalid_devices=int(row["invalid_devices"]),
            command_count=int(row["command_count"]),
            status=str(row["status"]),
            success_count=int(row["success_count"]),
            failure_count=int(row["failure_count"]),
            cancelled_count=int(row["cancelled_count"]),
            result_text=str(row["result_text"]),
            run_directory=str(row["run_directory"]),
            summary_path=str(row["summary_path"]),
        )


def read_summary(path: Path) -> tuple[DeviceResult, ...]:
    try:
        with Path(path).open(encoding="utf-8", newline="") as summary_file:
            reader = csv.DictReader(summary_file)
            rows = list(reader)
            fields = set(reader.fieldnames or ())
    except (OSError, csv.Error, UnicodeError) as exc:
        raise HistoryError(f"Could not read historical summary: {exc}") from exc
    required = {"device", "status", "transcript_file", "error"}
    if not required.issubset(fields):
        raise HistoryError("The historical summary has an unsupported format.")
    return tuple(
        DeviceResult(
            address=row.get("device", ""),
            status=row.get("status", ""),
            transcript="",
            failed_section=row.get("failed_section", ""),
            failed_line=row.get("failed_line", ""),
            failed_command=row.get("failed_command", ""),
            transcript_file=row.get("transcript_file", ""),
            error=row.get("error", ""),
        )
        for row in rows
    )


def write_output_marker(record: HistoryRecord) -> None:
    run_directory = Path(record.run_directory).expanduser().resolve()
    summary_path = Path(record.summary_path).expanduser().resolve()
    if (
        not run_directory.is_dir()
        or run_directory.is_symlink()
        or not summary_path.is_file()
        or summary_path.parent != run_directory
    ):
        return
    marker = run_directory / HISTORY_MARKER
    marker.write_text(json.dumps({"job_id": record.job_id}), encoding="utf-8")
    os.chmod(marker, 0o600)


def validate_output_deletion(record: HistoryRecord) -> Path:
    run_directory = Path(record.run_directory).expanduser().resolve()
    summary_path = Path(record.summary_path).expanduser().resolve()
    if not record.run_directory or not run_directory.is_dir():
        raise HistoryError("The output directory is unavailable.")
    if run_directory.is_symlink():
        raise HistoryError("Refusing to delete a symbolic-link output directory.")
    if run_directory in {Path("/").resolve(), Path.home().resolve()}:
        raise HistoryError("Refusing to delete a broad filesystem location.")
    if not run_directory.name.startswith("run_"):
        raise HistoryError("The output directory does not have the expected run name.")
    if (
        not summary_path.is_file()
        or summary_path.name != "summary.csv"
        or summary_path.parent != run_directory
    ):
        raise HistoryError("The summary does not belong to the recorded run directory.")
    marker = run_directory / HISTORY_MARKER
    try:
        marker_data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise HistoryError("The protected history marker is missing or invalid.") from exc
    if marker_data.get("job_id") != record.job_id:
        raise HistoryError("The output marker does not match this history record.")
    return run_directory


def delete_output_directory(record: HistoryRecord) -> None:
    run_directory = validate_output_deletion(record)
    try:
        shutil.rmtree(run_directory)
    except OSError as exc:
        raise HistoryError(f"Could not delete output directory: {exc}") from exc
