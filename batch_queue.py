"""Persistent, single-worker batch queue used by the desktop interface."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Protocol
from uuid import uuid4

from PySide6.QtCore import QSettings

from run_commands import (
    OUTPUT_MODES,
    OUTPUT_PER_DEVICE,
    PreparedRun,
    RunRequest,
    prepare_run,
)


KEYCHAIN_SERVICE = "com.sg4291.sshbatchupdate.queue"
QUEUE_GROUP = "batch_queue/jobs"
QUEUE_ORDER_KEY = "batch_queue/order"
PENDING_CLEANUP_KEY = "batch_queue/pending_keychain_cleanup"
PAYLOAD_VERSION = 4
EDITABLE_STATUSES = {"ready", "scheduled", "blocked", "interrupted"}
TERMINAL_STATUSES = {
    "succeeded",
    "completed_with_failures",
    "failed",
    "cancelled",
    "expired",
}


class QueueStorageError(RuntimeError):
    """Raised when a queued batch cannot be safely persisted or retrieved."""


class SecretStore(Protocol):
    def set(self, job_id: str, value: str) -> None: ...

    def get(self, job_id: str) -> str: ...

    def delete(self, job_id: str) -> None: ...


class KeychainSecretStore:
    """Store complete job payloads in the current user's macOS Keychain."""

    def __init__(self, service: str = KEYCHAIN_SERVICE) -> None:
        self.service = service

    @staticmethod
    def _keyring():
        try:
            import keyring
        except ImportError as exc:
            raise QueueStorageError(
                "The keyring dependency is unavailable; queued batches cannot be stored."
            ) from exc
        return keyring

    def set(self, job_id: str, value: str) -> None:
        try:
            self._keyring().set_password(self.service, job_id, value)
        except Exception as exc:
            raise QueueStorageError(f"Could not store batch in macOS Keychain: {exc}") from exc

    def get(self, job_id: str) -> str:
        try:
            value = self._keyring().get_password(self.service, job_id)
        except Exception as exc:
            raise QueueStorageError(f"Could not read batch from macOS Keychain: {exc}") from exc
        if value is None:
            raise QueueStorageError("The batch payload is missing from macOS Keychain.")
        return value

    def delete(self, job_id: str) -> None:
        try:
            self._keyring().delete_password(self.service, job_id)
        except Exception as exc:
            error_name = exc.__class__.__name__
            if error_name == "PasswordDeleteError" and "not found" in str(exc).lower():
                return
            raise QueueStorageError(f"Could not remove batch from macOS Keychain: {exc}") from exc


class MemorySecretStore:
    """In-memory secret store for tests and dependency injection."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def set(self, job_id: str, value: str) -> None:
        self.values[job_id] = value

    def get(self, job_id: str) -> str:
        try:
            return self.values[job_id]
        except KeyError as exc:
            raise QueueStorageError("The batch payload is missing from the secret store.") from exc

    def delete(self, job_id: str) -> None:
        self.values.pop(job_id, None)


@dataclass(frozen=True)
class BatchPayload:
    """Sensitive, immutable inputs required to reconstruct a prepared live run."""

    inventory_text: str
    exec_commands_text: str
    config_commands_text: str
    output_dir: str
    device_type: str
    username: str
    password: str
    enable_secret: Optional[str]
    port: int
    timeout: int
    failure_patterns: tuple[str, ...] = ()
    profile_label: str = "Manual"
    stop_on_first_failure: bool = False
    output_mode: str = OUTPUT_PER_DEVICE

    @classmethod
    def from_prepared(
        cls,
        prepared: PreparedRun,
        profile_label: str = "Manual",
    ) -> "BatchPayload":
        settings = prepared.settings
        if settings is None:
            raise QueueStorageError("The validated batch does not contain connection settings.")
        request = prepared.request
        return cls(
            inventory_text=request.inventory_text or "",
            exec_commands_text=request.exec_commands_text or "",
            config_commands_text=request.config_commands_text or "",
            output_dir=str(request.output_dir),
            device_type=request.device_type,
            username=settings.username,
            password=settings.password,
            enable_secret=settings.enable_secret,
            port=settings.port,
            timeout=settings.timeout,
            failure_patterns=tuple(request.failure_patterns),
            profile_label=profile_label or "Manual",
            stop_on_first_failure=request.stop_on_first_failure,
            output_mode=request.output_mode,
        )

    def to_json(self) -> str:
        return json.dumps(
            {
                "version": PAYLOAD_VERSION,
                "inventory_text": self.inventory_text,
                "exec_commands_text": self.exec_commands_text,
                "config_commands_text": self.config_commands_text,
                "output_dir": self.output_dir,
                "device_type": self.device_type,
                "username": self.username,
                "password": self.password,
                "enable_secret": self.enable_secret,
                "port": self.port,
                "timeout": self.timeout,
                "failure_patterns": list(self.failure_patterns),
                "profile_label": self.profile_label,
                "stop_on_first_failure": self.stop_on_first_failure,
                "output_mode": self.output_mode,
            },
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, value: str) -> "BatchPayload":
        try:
            data = json.loads(value)
            version = int(data.get("version", 0))
            if version not in {1, 2, 3, PAYLOAD_VERSION}:
                raise ValueError("unsupported payload version")
            stop_on_first_failure = data.get("stop_on_first_failure", False)
            if not isinstance(stop_on_first_failure, bool):
                raise ValueError("invalid stop_on_first_failure value")
            output_mode = data.get("output_mode", OUTPUT_PER_DEVICE)
            if output_mode not in OUTPUT_MODES:
                raise ValueError("invalid output_mode value")
            return cls(
                inventory_text=str(data["inventory_text"]),
                exec_commands_text=str(data["exec_commands_text"]),
                config_commands_text=str(data["config_commands_text"]),
                output_dir=str(data["output_dir"]),
                device_type=str(data["device_type"]),
                username=str(data["username"]),
                password=str(data["password"]),
                enable_secret=(
                    str(data["enable_secret"]) if data.get("enable_secret") else None
                ),
                port=int(data["port"]),
                timeout=int(data["timeout"]),
                failure_patterns=tuple(str(item) for item in data.get("failure_patterns", [])),
                profile_label=(
                    str(data.get("profile_label") or "Manual")
                    if version >= 2
                    else "Unknown (legacy)"
                ),
                stop_on_first_failure=stop_on_first_failure,
                output_mode=output_mode,
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise QueueStorageError(f"The stored batch payload is invalid: {exc}") from exc

    def prepare(self) -> PreparedRun:
        request = RunRequest(
            inventory=None,
            command_file=None,
            device_type=self.device_type,
            output_dir=Path(self.output_dir).expanduser(),
            env_file=None,
            failure_patterns=self.failure_patterns,
            apply=True,
            inventory_text=self.inventory_text,
            exec_commands_text=self.exec_commands_text,
            config_commands_text=self.config_commands_text,
            stop_on_first_failure=self.stop_on_first_failure,
            output_mode=self.output_mode,
        )
        return prepare_run(
            request,
            require_credentials=True,
            environment={
                "SSH_USERNAME": self.username,
                "SSH_PASSWORD": self.password,
                "ENABLE_SECRET": self.enable_secret or "",
                "SSH_PORT": str(self.port),
                "SSH_TIMEOUT": str(self.timeout),
            },
        )


@dataclass(frozen=True)
class QueuedBatch:
    job_id: str
    name: str
    created_utc: datetime
    scheduled_utc: Optional[datetime]
    latest_start_utc: Optional[datetime]
    device_type: str
    profile_label: str
    valid_devices: int
    invalid_devices: int
    command_count: int
    status: str
    result: str = ""

    def is_due(self, now_utc: datetime) -> bool:
        return self.scheduled_utc is None or self.scheduled_utc <= now_utc

    def is_expired(self, now_utc: datetime) -> bool:
        return self.latest_start_utc is not None and now_utc > self.latest_start_utc


class QueueMetadataStore:
    """Persist non-sensitive job metadata and ordering in QSettings."""

    def __init__(self, settings: QSettings) -> None:
        self.settings = settings

    def load(self) -> list[QueuedBatch]:
        raw_order = self.settings.value(QUEUE_ORDER_KEY, [])
        if isinstance(raw_order, str):
            order = [raw_order] if raw_order else []
        else:
            order = [str(item) for item in (raw_order or [])]
        jobs: list[QueuedBatch] = []
        for job_id in order:
            base = f"{QUEUE_GROUP}/{job_id}"
            name = self.settings.value(f"{base}/name", "", str)
            created = self.settings.value(f"{base}/created_utc", "", str)
            if not name or not created:
                continue
            scheduled = self.settings.value(f"{base}/scheduled_utc", "", str)
            latest_start = self.settings.value(f"{base}/latest_start_utc", "", str)
            status = self.settings.value(f"{base}/status", "ready", str)
            if status == "running":
                status = "interrupted"
            try:
                jobs.append(
                    QueuedBatch(
                        job_id=job_id,
                        name=name,
                        created_utc=_parse_utc(created),
                        scheduled_utc=_parse_utc(scheduled) if scheduled else None,
                        latest_start_utc=(
                            _parse_utc(latest_start) if latest_start else None
                        ),
                        device_type=self.settings.value(f"{base}/device_type", "", str),
                        profile_label=self.settings.value(
                            f"{base}/profile_label", "Unknown (legacy)", str
                        ),
                        valid_devices=int(
                            self.settings.value(f"{base}/valid_devices", 0)
                        ),
                        invalid_devices=int(
                            self.settings.value(f"{base}/invalid_devices", 0)
                        ),
                        command_count=int(
                            self.settings.value(f"{base}/command_count", 0)
                        ),
                        status=status,
                        result="",
                    )
                )
            except (TypeError, ValueError):
                continue
        if any(job.status == "interrupted" for job in jobs):
            self.save_all(jobs)
        return jobs

    def save_all(self, jobs: list[QueuedBatch]) -> None:
        persisted = [job for job in jobs if job.status not in TERMINAL_STATUSES]
        self.settings.setValue(QUEUE_ORDER_KEY, [job.job_id for job in persisted])
        known = set(self.settings.childGroups())
        self.settings.beginGroup(QUEUE_GROUP)
        try:
            known = set(self.settings.childGroups())
        finally:
            self.settings.endGroup()
        current = {job.job_id for job in persisted}
        for removed in known - current:
            self.settings.remove(f"{QUEUE_GROUP}/{removed}")
        for job in persisted:
            self._save_job(job)
        self.settings.sync()

    def pending_cleanup_ids(self) -> list[str]:
        raw = self.settings.value(PENDING_CLEANUP_KEY, [])
        if isinstance(raw, str):
            return [raw] if raw else []
        return list(dict.fromkeys(str(job_id) for job_id in (raw or []) if job_id))

    def add_pending_cleanup(self, job_id: str) -> None:
        pending = self.pending_cleanup_ids()
        if job_id not in pending:
            self.settings.setValue(PENDING_CLEANUP_KEY, pending + [job_id])
            self.settings.sync()

    def remove_pending_cleanup(self, job_id: str) -> None:
        pending = self.pending_cleanup_ids()
        if job_id not in pending:
            return
        remaining = [item for item in pending if item != job_id]
        if remaining:
            self.settings.setValue(PENDING_CLEANUP_KEY, remaining)
        else:
            self.settings.remove(PENDING_CLEANUP_KEY)
        self.settings.sync()

    def _save_job(self, job: QueuedBatch) -> None:
        base = f"{QUEUE_GROUP}/{job.job_id}"
        self.settings.setValue(f"{base}/name", job.name)
        self.settings.setValue(f"{base}/created_utc", _iso_utc(job.created_utc))
        self.settings.setValue(
            f"{base}/scheduled_utc",
            _iso_utc(job.scheduled_utc) if job.scheduled_utc else "",
        )
        self.settings.setValue(
            f"{base}/latest_start_utc",
            _iso_utc(job.latest_start_utc) if job.latest_start_utc else "",
        )
        self.settings.setValue(f"{base}/device_type", job.device_type)
        self.settings.setValue(f"{base}/profile_label", job.profile_label)
        self.settings.setValue(f"{base}/valid_devices", job.valid_devices)
        self.settings.setValue(f"{base}/invalid_devices", job.invalid_devices)
        self.settings.setValue(f"{base}/command_count", job.command_count)
        self.settings.setValue(f"{base}/status", job.status)
        self.settings.remove(f"{base}/result")


class QueueController:
    """Own queue ordering and state while allowing exactly one active job."""

    def __init__(
        self,
        metadata_store: QueueMetadataStore,
        secret_store: SecretStore,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.metadata_store = metadata_store
        self.secret_store = secret_store
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.jobs = metadata_store.load()
        self.restored = bool(self.jobs)
        self.paused = self.restored
        self.active_job_id: Optional[str] = None
        self._deleted_payload_ids: set[str] = set()
        self.cleanup_warnings = self._retry_pending_cleanup() + self.refresh_due()

    def _delete_payload(self, job_id: str, *, persist_failure: bool) -> None:
        if job_id in self._deleted_payload_ids:
            return
        try:
            self.secret_store.delete(job_id)
        except QueueStorageError:
            if persist_failure:
                self.metadata_store.add_pending_cleanup(job_id)
            raise
        self._deleted_payload_ids.add(job_id)
        self.metadata_store.remove_pending_cleanup(job_id)

    def _retry_pending_cleanup(self) -> list[str]:
        warnings: list[str] = []
        for job_id in self.metadata_store.pending_cleanup_ids():
            if self.job(job_id) is not None:
                # A crash can leave a live queue row alongside a cleanup record.
                # The row still owns its payload until it becomes terminal.
                continue
            try:
                self._delete_payload(job_id, persist_failure=True)
            except QueueStorageError as exc:
                warnings.append(str(exc))
        return warnings

    def add(
        self,
        payload: BatchPayload,
        prepared: PreparedRun,
        scheduled_utc: Optional[datetime] = None,
        latest_start_utc: Optional[datetime] = None,
    ) -> QueuedBatch:
        now = _as_utc(self.clock())
        scheduled = _as_utc(scheduled_utc) if scheduled_utc else None
        latest_start = _as_utc(latest_start_utc) if latest_start_utc else None
        if scheduled is not None:
            if latest_start is None:
                raise QueueStorageError("A scheduled batch requires an expiry time.")
            if latest_start <= scheduled:
                raise QueueStorageError("Expiry must be later than the scheduled time.")
        elif latest_start is not None:
            raise QueueStorageError("A Run Live batch cannot have an expiry time.")
        job_id = uuid4().hex
        job = QueuedBatch(
            job_id=job_id,
            name=(
                f"{prepared.request.device_type} — {len(prepared.valid_entries)} devices — "
                f"{now.astimezone().strftime('%Y-%m-%d %H:%M:%S')}"
            ),
            created_utc=now,
            scheduled_utc=scheduled,
            latest_start_utc=latest_start,
            device_type=prepared.request.device_type,
            profile_label=payload.profile_label,
            valid_devices=len(prepared.valid_entries),
            invalid_devices=len(prepared.invalid_entries),
            command_count=len(prepared.commands),
            status="scheduled" if scheduled and scheduled > now else "ready",
        )
        self.secret_store.set(job_id, payload.to_json())
        try:
            self.jobs.append(job)
            self.metadata_store.save_all(self.jobs)
        except Exception:
            self.jobs = [item for item in self.jobs if item.job_id != job_id]
            self.secret_store.delete(job_id)
            raise
        return job

    def job(self, job_id: Optional[str]) -> Optional[QueuedBatch]:
        return next((job for job in self.jobs if job.job_id == job_id), None)

    def payload(self, job_id: str) -> BatchPayload:
        return BatchPayload.from_json(self.secret_store.get(job_id))

    def replace_payload(
        self,
        job_id: str,
        payload: BatchPayload,
        prepared: PreparedRun,
    ) -> QueuedBatch:
        job = self._editable(job_id)
        self.secret_store.set(job_id, payload.to_json())
        status = (
            "scheduled"
            if job.scheduled_utc and not job.is_due(_as_utc(self.clock()))
            else "ready"
        )
        updated = replace(
            job,
            device_type=prepared.request.device_type,
            profile_label=payload.profile_label,
            valid_devices=len(prepared.valid_entries),
            invalid_devices=len(prepared.invalid_entries),
            command_count=len(prepared.commands),
            status=status,
            result="",
        )
        self._replace(updated)
        return updated

    def refresh_due(self) -> list[str]:
        now = _as_utc(self.clock())
        changed = False
        warnings: list[str] = []
        updated: list[QueuedBatch] = []
        for job in self.jobs:
            if job.status in {"scheduled", "ready"} and job.is_expired(now):
                local_deadline = job.latest_start_utc.astimezone().strftime(
                    "%Y-%m-%d %H:%M"
                )
                job = replace(
                    job,
                    status="expired",
                    result=f"Not started: scheduled batch timeout reached at {local_deadline}",
                )
                try:
                    self._delete_payload(job.job_id, persist_failure=True)
                except QueueStorageError as exc:
                    warnings.append(str(exc))
                changed = True
            elif job.status == "scheduled" and job.is_due(now):
                job = replace(job, status="ready")
                changed = True
            updated.append(job)
        self.jobs = updated
        if changed:
            self.metadata_store.save_all(self.jobs)
        return warnings

    def next_eligible(self) -> Optional[QueuedBatch]:
        self.refresh_due()
        if self.paused or self.active_job_id:
            return None
        return next((job for job in self.jobs if job.status == "ready"), None)

    def mark_running(self, job_id: str) -> QueuedBatch:
        job = self._required(job_id)
        if job.status != "ready":
            raise QueueStorageError("Only a ready batch can start.")
        updated = replace(job, status="running", result="")
        self.active_job_id = job_id
        self._replace(updated)
        return updated

    def mark_blocked(self, job_id: str, message: str) -> QueuedBatch:
        updated = replace(self._required(job_id), status="blocked", result=message)
        if self.active_job_id == job_id:
            self.active_job_id = None
        self._replace(updated)
        return updated

    def mark_terminal(self, job_id: str, status: str, result: str) -> Optional[str]:
        if status not in TERMINAL_STATUSES:
            raise ValueError(f"invalid terminal status: {status}")
        updated = replace(self._required(job_id), status=status, result=result)
        if self.active_job_id == job_id:
            self.active_job_id = None
        self.jobs = [updated if job.job_id == job_id else job for job in self.jobs]
        warning = None
        try:
            self._delete_payload(job_id, persist_failure=True)
        except QueueStorageError as exc:
            warning = str(exc)
        self.metadata_store.save_all(self.jobs)
        return warning

    def move(self, job_id: str, direction: int) -> bool:
        job = self._editable(job_id)
        index = self.jobs.index(job)
        candidate = index + direction
        while 0 <= candidate < len(self.jobs):
            other = self.jobs[candidate]
            if other.status in EDITABLE_STATUSES:
                self.jobs[index], self.jobs[candidate] = self.jobs[candidate], self.jobs[index]
                self.metadata_store.save_all(self.jobs)
                return True
            candidate += direction
        return False

    def reschedule(
        self,
        job_id: str,
        scheduled_utc: datetime,
        latest_start_utc: datetime,
    ) -> QueuedBatch:
        job = self._editable(job_id)
        scheduled = _as_utc(scheduled_utc)
        latest_start = _as_utc(latest_start_utc)
        if latest_start <= scheduled:
            raise QueueStorageError("Expiry must be later than the scheduled time.")
        now = _as_utc(self.clock())
        updated = replace(
            job,
            scheduled_utc=scheduled,
            latest_start_utc=latest_start,
            status="scheduled" if scheduled and scheduled > now else "ready",
            result="",
        )
        self._replace(updated)
        return updated

    def retry(self, job_id: str) -> QueuedBatch:
        job = self._required(job_id)
        if job.status not in {"interrupted", "blocked"}:
            raise QueueStorageError("Only interrupted or blocked batches can be retried.")
        status = (
            "scheduled"
            if job.scheduled_utc and not job.is_due(_as_utc(self.clock()))
            else "ready"
        )
        updated = replace(job, status=status, result="")
        self._replace(updated)
        return updated

    def remove(self, job_id: str) -> None:
        job = self._required(job_id)
        if job.status == "running":
            raise QueueStorageError("A running batch cannot be removed.")
        # A successful terminal cleanup is known for this session; failed
        # cleanup remains retryable without losing a live batch payload.
        self._delete_payload(
            job_id, persist_failure=job.status in TERMINAL_STATUSES
        )
        self.jobs = [job for job in self.jobs if job.job_id != job_id]
        self.metadata_store.save_all(self.jobs)

    def completed_jobs(self) -> list[QueuedBatch]:
        """Return terminal history entries that are safe to clear."""
        return [job for job in self.jobs if job.status in TERMINAL_STATUSES]

    def clear_completed(self) -> list[str]:
        """Remove terminal history without affecting pending or active work."""
        completed = self.completed_jobs()
        completed_ids = [job.job_id for job in completed]
        for job_id in completed_ids:
            self._delete_payload(job_id, persist_failure=True)
        completed_set = set(completed_ids)
        self.jobs = [job for job in self.jobs if job.job_id not in completed_set]
        self.metadata_store.save_all(self.jobs)
        return completed_ids

    def pending_jobs(self) -> list[QueuedBatch]:
        return [job for job in self.jobs if job.status not in TERMINAL_STATUSES]

    def _required(self, job_id: str) -> QueuedBatch:
        job = self.job(job_id)
        if job is None:
            raise QueueStorageError("The queued batch no longer exists.")
        return job

    def _editable(self, job_id: str) -> QueuedBatch:
        job = self._required(job_id)
        if job.status not in EDITABLE_STATUSES:
            raise QueueStorageError("This batch can no longer be edited.")
        return job

    def _replace(self, updated: QueuedBatch) -> None:
        self.jobs = [updated if job.job_id == updated.job_id else job for job in self.jobs]
        self.metadata_store.save_all(self.jobs)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.astimezone()
    return value.astimezone(timezone.utc)


def _iso_utc(value: datetime) -> str:
    return _as_utc(value).isoformat().replace("+00:00", "Z")


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
