import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from PySide6.QtCore import QSettings
from keyring.errors import PasswordDeleteError

import batch_queue
import run_commands as runner


class TrackingSecretStore(batch_queue.MemorySecretStore):
    def __init__(self):
        super().__init__()
        self.delete_calls: list[str] = []
        self.failures_left: dict[str, int] = {}

    def delete(self, job_id: str) -> None:
        self.delete_calls.append(job_id)
        if self.failures_left.get(job_id, 0):
            self.failures_left[job_id] -= 1
            raise batch_queue.QueueStorageError(
                "Could not remove batch from macOS Keychain: (-25244, 'Unknown Error')"
            )
        super().delete(job_id)


class BatchQueueTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.settings = QSettings(
            str(self.base / "settings.ini"), QSettings.Format.IniFormat
        )
        self.secrets = TrackingSecretStore()
        self.now = datetime(2026, 10, 19, 3, 41, tzinfo=timezone.utc)
        self.controller = batch_queue.QueueController(
            batch_queue.QueueMetadataStore(self.settings),
            self.secrets,
            clock=lambda: self.now,
        )

    def tearDown(self):
        self.temporary.cleanup()

    def prepared(self, device_type="huawei", address="192.0.2.1"):
        request = runner.RunRequest(
            inventory=None,
            command_file=None,
            device_type=device_type,
            output_dir=self.base / "outputs",
            env_file=None,
            apply=True,
            inventory_text=address,
            exec_commands_text="show version",
            config_commands_text="interface loopback 1",
        )
        return runner.prepare_run(
            request,
            require_credentials=True,
            environment={
                "SSH_USERNAME": "queue-user",
                "SSH_PASSWORD": "queue-password",
                "ENABLE_SECRET": "queue-enable",
                "SSH_PORT": "2200",
                "SSH_TIMEOUT": "30",
            },
        )

    def add(self, device_type="huawei", scheduled=None):
        prepared = self.prepared(device_type)
        payload = batch_queue.BatchPayload.from_prepared(prepared)
        latest_start = scheduled + timedelta(hours=1) if scheduled else None
        return self.controller.add(payload, prepared, scheduled, latest_start)

    def test_payload_round_trip_reconstructs_complete_prepared_run(self):
        prepared = self.prepared("cisco_ios")
        payload = batch_queue.BatchPayload.from_prepared(prepared, "Operations")

        restored_payload = batch_queue.BatchPayload.from_json(payload.to_json())
        restored = restored_payload.prepare()

        self.assertEqual(restored.request.device_type, "cisco_ios")
        self.assertEqual(restored.settings.username, "queue-user")
        self.assertEqual(restored.settings.password, "queue-password")
        self.assertEqual(restored.settings.enable_secret, "queue-enable")
        self.assertEqual(restored.settings.port, 2200)
        self.assertEqual(restored.settings.timeout, 30)
        self.assertEqual(len(restored.commands), 2)
        self.assertEqual(restored_payload.profile_label, "Operations")
        self.assertFalse(restored.request.stop_on_first_failure)
        self.assertEqual(restored.request.output_mode, runner.OUTPUT_PER_DEVICE)

    def test_payload_round_trip_preserves_stop_on_first_failure(self):
        prepared = self.prepared()
        prepared = replace(
            prepared,
            request=replace(
                prepared.request,
                stop_on_first_failure=True,
                output_mode=runner.OUTPUT_SINGLE_FILE,
            ),
        )
        payload = batch_queue.BatchPayload.from_prepared(prepared)

        restored = batch_queue.BatchPayload.from_json(payload.to_json()).prepare()

        self.assertTrue(payload.stop_on_first_failure)
        self.assertTrue(restored.request.stop_on_first_failure)
        self.assertEqual(restored.request.output_mode, runner.OUTPUT_SINGLE_FILE)

    def test_version_one_payload_remains_readable_with_legacy_profile(self):
        payload = batch_queue.BatchPayload.from_prepared(self.prepared())
        data = json.loads(payload.to_json())
        data["version"] = 1
        data.pop("profile_label")

        restored = batch_queue.BatchPayload.from_json(json.dumps(data))

        self.assertEqual(restored.profile_label, "Unknown (legacy)")
        self.assertEqual(restored.username, "queue-user")
        self.assertFalse(restored.stop_on_first_failure)
        self.assertEqual(restored.output_mode, runner.OUTPUT_PER_DEVICE)

    def test_version_two_payload_keeps_profile_and_defaults_to_continue(self):
        data = json.loads(batch_queue.BatchPayload.from_prepared(self.prepared(), "Ops").to_json())
        data["version"] = 2
        data.pop("stop_on_first_failure")

        restored = batch_queue.BatchPayload.from_json(json.dumps(data))

        self.assertEqual(restored.profile_label, "Ops")
        self.assertFalse(restored.stop_on_first_failure)
        self.assertEqual(restored.output_mode, runner.OUTPUT_PER_DEVICE)

    def test_version_three_payload_defaults_to_per_device_output(self):
        data = json.loads(batch_queue.BatchPayload.from_prepared(self.prepared()).to_json())
        data["version"] = 3
        data.pop("output_mode")
        data["stop_on_first_failure"] = True

        restored = batch_queue.BatchPayload.from_json(json.dumps(data))

        self.assertTrue(restored.stop_on_first_failure)
        self.assertEqual(restored.output_mode, runner.OUTPUT_PER_DEVICE)

    def test_invalid_output_mode_payload_is_rejected(self):
        data = json.loads(batch_queue.BatchPayload.from_prepared(self.prepared()).to_json())
        data["output_mode"] = "unknown"

        with self.assertRaisesRegex(batch_queue.QueueStorageError, "output_mode"):
            batch_queue.BatchPayload.from_json(json.dumps(data))

    def test_settings_metadata_contains_no_payload_or_secrets(self):
        job = self.add()

        stored = "\n".join(
            f"{key}={self.settings.value(key)}" for key in self.settings.allKeys()
        )

        self.assertIn(job.job_id, stored)
        self.assertNotIn("192.0.2.1", stored)
        self.assertNotIn("show version", stored)
        self.assertNotIn("queue-user", stored)
        self.assertNotIn("queue-password", stored)
        self.assertNotIn("queue-enable", stored)
        self.assertIn(job.job_id, self.secrets.values)

    def test_future_job_does_not_block_ready_fifo_and_reordering(self):
        future = self.add("huawei", self.now + timedelta(hours=2))
        first_ready = self.add("cisco_ios")
        second_ready = self.add("huawei")

        self.assertEqual(self.controller.next_eligible().job_id, first_ready.job_id)
        self.assertTrue(self.controller.move(second_ready.job_id, -1))
        self.assertEqual(self.controller.next_eligible().job_id, second_ready.job_id)
        self.assertEqual(self.controller.job(future.job_id).status, "scheduled")

    def test_same_time_jobs_become_ready_in_visible_order(self):
        scheduled = self.now + timedelta(hours=1)
        first = self.add("huawei", scheduled)
        second = self.add("cisco_ios", scheduled)
        self.now = scheduled

        self.controller.refresh_due()

        self.assertEqual(self.controller.next_eligible().job_id, first.job_id)
        self.controller.mark_running(first.job_id)
        self.controller.mark_terminal(first.job_id, "succeeded", "ok")
        self.assertEqual(self.controller.next_eligible().job_id, second.job_id)

    def test_restored_jobs_are_paused_and_running_job_becomes_interrupted(self):
        job = self.add()
        self.controller.mark_running(job.job_id)

        restored = batch_queue.QueueController(
            batch_queue.QueueMetadataStore(self.settings),
            self.secrets,
            clock=lambda: self.now,
        )

        self.assertTrue(restored.paused)
        self.assertEqual(restored.job(job.job_id).status, "interrupted")
        self.assertIsNone(restored.next_eligible())

    def test_overdue_scheduled_job_restores_ready_but_paused(self):
        job = self.add(scheduled=self.now + timedelta(minutes=5))
        self.now += timedelta(minutes=10)

        restored = batch_queue.QueueController(
            batch_queue.QueueMetadataStore(self.settings),
            self.secrets,
            clock=lambda: self.now,
        )

        self.assertEqual(restored.job(job.job_id).status, "ready")
        self.assertTrue(restored.paused)

    def test_terminal_and_removed_jobs_delete_secret_payload(self):
        completed = self.add()
        removed = self.add("cisco_ios")

        self.controller.mark_running(completed.job_id)
        self.controller.mark_terminal(completed.job_id, "succeeded", "complete")
        self.controller.remove(removed.job_id)

        self.assertNotIn(completed.job_id, self.secrets.values)
        self.assertNotIn(removed.job_id, self.secrets.values)
        self.assertNotIn(completed.job_id, self.settings.value(batch_queue.QUEUE_ORDER_KEY, []))

    def test_cancelled_job_can_be_removed_from_visible_history(self):
        cancelled = self.add()
        self.controller.mark_running(cancelled.job_id)
        self.controller.mark_terminal(cancelled.job_id, "cancelled", "run cancelled")

        self.controller.remove(cancelled.job_id)

        self.assertIsNone(self.controller.job(cancelled.job_id))
        self.assertNotIn(cancelled.job_id, self.secrets.values)
        self.assertEqual(self.secrets.delete_calls.count(cancelled.job_id), 1)

    def test_expired_batch_removal_skips_confirmed_keychain_deletion(self):
        scheduled = self.now + timedelta(minutes=5)
        job = self.add(scheduled=scheduled)
        self.now = scheduled + timedelta(hours=1, seconds=1)

        self.assertEqual(self.controller.refresh_due(), [])
        self.assertEqual(self.secrets.delete_calls.count(job.job_id), 1)
        self.controller.remove(job.job_id)

        self.assertIsNone(self.controller.job(job.job_id))
        self.assertEqual(self.secrets.delete_calls.count(job.job_id), 1)
        self.assertEqual(self.controller.metadata_store.pending_cleanup_ids(), [])

    def test_clear_completed_skips_already_deleted_payloads(self):
        succeeded = self.add()
        expired = self.add(scheduled=self.now + timedelta(minutes=5))
        self.controller.mark_running(succeeded.job_id)
        self.controller.mark_terminal(succeeded.job_id, "succeeded", "complete")
        self.now += timedelta(hours=2)
        self.controller.refresh_due()

        self.assertEqual(
            self.controller.clear_completed(), [succeeded.job_id, expired.job_id]
        )
        self.assertEqual(self.secrets.delete_calls.count(succeeded.job_id), 1)
        self.assertEqual(self.secrets.delete_calls.count(expired.job_id), 1)

    def test_failed_expiry_cleanup_survives_relaunch_and_retries(self):
        scheduled = self.now + timedelta(minutes=5)
        job = self.add(scheduled=scheduled)
        self.secrets.failures_left[job.job_id] = 3
        self.now = scheduled + timedelta(hours=1, seconds=1)

        warnings = self.controller.refresh_due()
        self.assertIn("-25244", warnings[0])
        self.assertEqual(self.controller.job(job.job_id).status, "expired")
        self.assertIn(job.job_id, self.secrets.values)
        self.assertEqual(
            self.controller.metadata_store.pending_cleanup_ids(), [job.job_id]
        )
        stored_settings = "\n".join(
            f"{key}={self.settings.value(key)}" for key in self.settings.allKeys()
        )
        self.assertNotIn("queue-password", stored_settings)
        self.assertNotIn("show version", stored_settings)
        self.assertNotIn(job.job_id, self.settings.value(batch_queue.QUEUE_ORDER_KEY, []))
        with self.assertRaisesRegex(batch_queue.QueueStorageError, "-25244"):
            self.controller.remove(job.job_id)
        self.assertIsNotNone(self.controller.job(job.job_id))

        reloaded_settings = QSettings(
            str(self.base / "settings.ini"), QSettings.Format.IniFormat
        )
        restored = batch_queue.QueueController(
            batch_queue.QueueMetadataStore(reloaded_settings),
            self.secrets,
            clock=lambda: self.now,
        )

        self.assertIsNone(restored.job(job.job_id))
        self.assertEqual(len(restored.cleanup_warnings), 1)
        self.assertIn("-25244", restored.cleanup_warnings[0])
        self.assertEqual(self.secrets.delete_calls.count(job.job_id), 3)
        self.assertIn(job.job_id, self.secrets.values)
        self.assertEqual(restored.metadata_store.pending_cleanup_ids(), [job.job_id])

        next_launch_settings = QSettings(
            str(self.base / "settings.ini"), QSettings.Format.IniFormat
        )
        retried = batch_queue.QueueController(
            batch_queue.QueueMetadataStore(next_launch_settings),
            self.secrets,
            clock=lambda: self.now,
        )

        self.assertEqual(retried.cleanup_warnings, [])
        self.assertEqual(self.secrets.delete_calls.count(job.job_id), 4)
        self.assertNotIn(job.job_id, self.secrets.values)
        self.assertEqual(retried.metadata_store.pending_cleanup_ids(), [])

    def test_pending_batch_removal_failure_does_not_schedule_cleanup(self):
        job = self.add()
        self.secrets.failures_left[job.job_id] = 1

        with self.assertRaisesRegex(batch_queue.QueueStorageError, "-25244"):
            self.controller.remove(job.job_id)

        self.assertIsNotNone(self.controller.job(job.job_id))
        self.assertIn(job.job_id, self.secrets.values)
        self.assertEqual(self.controller.metadata_store.pending_cleanup_ids(), [])
        restored = batch_queue.QueueController(
            batch_queue.QueueMetadataStore(self.settings),
            self.secrets,
            clock=lambda: self.now,
        )
        self.assertEqual(restored.cleanup_warnings, [])
        self.assertIn(job.job_id, self.secrets.values)

    def test_startup_does_not_delete_payload_for_a_still_queued_job(self):
        job = self.add()
        self.controller.metadata_store.add_pending_cleanup(job.job_id)

        restored = batch_queue.QueueController(
            batch_queue.QueueMetadataStore(self.settings),
            self.secrets,
            clock=lambda: self.now,
        )

        self.assertIsNotNone(restored.job(job.job_id))
        self.assertIn(job.job_id, self.secrets.values)
        self.assertEqual(self.secrets.delete_calls.count(job.job_id), 0)
        self.assertEqual(restored.metadata_store.pending_cleanup_ids(), [job.job_id])
        restored.remove(job.job_id)
        self.assertEqual(restored.metadata_store.pending_cleanup_ids(), [])
        self.assertEqual(self.secrets.delete_calls.count(job.job_id), 1)

    def test_expiry_cleanup_failure_can_be_retried_by_removing_row(self):
        scheduled = self.now + timedelta(minutes=5)
        job = self.add(scheduled=scheduled)
        self.secrets.failures_left[job.job_id] = 1
        self.now = scheduled + timedelta(hours=1, seconds=1)
        self.assertEqual(len(self.controller.refresh_due()), 1)

        self.controller.remove(job.job_id)

        self.assertIsNone(self.controller.job(job.job_id))
        self.assertNotIn(job.job_id, self.secrets.values)
        self.assertEqual(self.secrets.delete_calls.count(job.job_id), 2)
        self.assertEqual(self.controller.metadata_store.pending_cleanup_ids(), [])


    def test_clear_completed_preserves_ready_and_scheduled_jobs(self):
        succeeded = self.add("huawei")
        cancelled = self.add("cisco_ios")
        ready = self.add("huawei")
        scheduled = self.add("cisco_ios", self.now + timedelta(hours=1))
        self.controller.mark_running(succeeded.job_id)
        self.controller.mark_terminal(succeeded.job_id, "succeeded", "complete")
        self.controller.mark_running(cancelled.job_id)
        self.controller.mark_terminal(cancelled.job_id, "cancelled", "cancelled")

        removed = self.controller.clear_completed()

        self.assertEqual(removed, [succeeded.job_id, cancelled.job_id])
        self.assertEqual(
            [job.job_id for job in self.controller.jobs],
            [ready.job_id, scheduled.job_id],
        )
        self.assertEqual(self.controller.job(ready.job_id).status, "ready")
        self.assertEqual(self.controller.job(scheduled.job_id).status, "scheduled")
        self.assertIn(ready.job_id, self.secrets.values)
        self.assertIn(scheduled.job_id, self.secrets.values)

    def test_blocked_job_is_skipped_and_retry_requires_explicit_action(self):
        blocked = self.add()
        ready = self.add("cisco_ios")
        self.controller.mark_blocked(blocked.job_id, "Keychain unavailable")

        self.assertEqual(self.controller.next_eligible().job_id, ready.job_id)
        self.controller.retry(blocked.job_id)
        self.assertEqual(self.controller.job(blocked.job_id).status, "ready")

    def test_scheduled_batch_requires_valid_latest_start(self):
        prepared = self.prepared()
        payload = batch_queue.BatchPayload.from_prepared(prepared)
        scheduled = self.now + timedelta(hours=1)

        with self.assertRaisesRegex(batch_queue.QueueStorageError, "expiry"):
            self.controller.add(payload, prepared, scheduled)
        with self.assertRaisesRegex(batch_queue.QueueStorageError, "later"):
            self.controller.add(payload, prepared, scheduled, scheduled)

    def test_latest_start_is_inclusive_then_expires_and_deletes_payload(self):
        scheduled = self.now + timedelta(minutes=5)
        latest = scheduled + timedelta(minutes=10)
        prepared = self.prepared()
        payload = batch_queue.BatchPayload.from_prepared(prepared)
        job = self.controller.add(payload, prepared, scheduled, latest)

        self.now = latest
        self.controller.refresh_due()
        self.assertEqual(self.controller.job(job.job_id).status, "ready")
        self.assertIn(job.job_id, self.secrets.values)

        self.now += timedelta(seconds=1)
        self.controller.refresh_due()
        expired = self.controller.job(job.job_id)
        self.assertEqual(expired.status, "expired")
        self.assertIn("Not started", expired.result)
        self.assertNotIn(job.job_id, self.secrets.values)
        self.assertIsNone(self.controller.next_eligible())

    def test_expired_restored_batch_remains_visible_for_session(self):
        scheduled = self.now + timedelta(minutes=5)
        prepared = self.prepared()
        payload = batch_queue.BatchPayload.from_prepared(prepared)
        job = self.controller.add(
            payload,
            prepared,
            scheduled,
            scheduled + timedelta(minutes=30),
        )
        self.now = scheduled + timedelta(minutes=31)

        restored = batch_queue.QueueController(
            batch_queue.QueueMetadataStore(self.settings),
            self.secrets,
            clock=lambda: self.now,
        )

        self.assertTrue(restored.paused)
        self.assertEqual(restored.job(job.job_id).status, "expired")
        self.assertNotIn(job.job_id, self.secrets.values)
        self.assertNotIn(job.job_id, self.settings.value(batch_queue.QUEUE_ORDER_KEY, []))

    def test_profile_and_deadline_restore_from_metadata(self):
        scheduled = self.now + timedelta(hours=2)
        prepared = self.prepared()
        payload = batch_queue.BatchPayload.from_prepared(prepared, "Night Ops")
        job = self.controller.add(
            payload,
            prepared,
            scheduled,
            scheduled + timedelta(hours=1),
        )

        restored = batch_queue.QueueController(
            batch_queue.QueueMetadataStore(self.settings),
            self.secrets,
            clock=lambda: self.now,
        ).job(job.job_id)

        self.assertIn("huawei", restored.name)
        self.assertEqual(restored.profile_label, "Night Ops")
        self.assertEqual(restored.latest_start_utc, scheduled + timedelta(hours=1))

    def test_legacy_schedule_without_deadline_remains_ready(self):
        scheduled = self.now + timedelta(minutes=5)
        job = self.add(scheduled=scheduled)
        self.settings.remove(
            f"{batch_queue.QUEUE_GROUP}/{job.job_id}/latest_start_utc"
        )
        self.settings.sync()
        self.now = scheduled + timedelta(hours=2)

        restored = batch_queue.QueueController(
            batch_queue.QueueMetadataStore(self.settings),
            self.secrets,
            clock=lambda: self.now,
        )

        legacy = restored.job(job.job_id)
        self.assertEqual(legacy.status, "ready")
        self.assertIsNone(legacy.latest_start_utc)
        self.assertIn(job.job_id, self.secrets.values)


class KeychainSecretStoreTests(unittest.TestCase):
    def test_missing_item_is_success_but_owner_error_is_not(self):
        keyring = Mock()
        keyring.delete_password.side_effect = [
            PasswordDeleteError("Can't delete password: (-25300, 'Item not found')"),
            PasswordDeleteError("Can't delete password: (-25244, 'Unknown Error')"),
        ]
        with patch.object(
            batch_queue.KeychainSecretStore, "_keyring", return_value=keyring
        ):
            store = batch_queue.KeychainSecretStore()
            store.delete("job-id")
            with self.assertRaisesRegex(batch_queue.QueueStorageError, "-25244"):
                store.delete("job-id")

        self.assertEqual(keyring.delete_password.call_count, 2)


if __name__ == "__main__":
    unittest.main()
