import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PySide6.QtCore import QSettings

import batch_queue
import run_commands as runner


class BatchQueueTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.settings = QSettings(
            str(self.base / "settings.ini"), QSettings.Format.IniFormat
        )
        self.secrets = batch_queue.MemorySecretStore()
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
        return self.controller.add(payload, prepared, scheduled)

    def test_payload_round_trip_reconstructs_complete_prepared_run(self):
        prepared = self.prepared("cisco_ios")
        payload = batch_queue.BatchPayload.from_prepared(prepared)

        restored = batch_queue.BatchPayload.from_json(payload.to_json()).prepare()

        self.assertEqual(restored.request.device_type, "cisco_ios")
        self.assertEqual(restored.settings.username, "queue-user")
        self.assertEqual(restored.settings.password, "queue-password")
        self.assertEqual(restored.settings.enable_secret, "queue-enable")
        self.assertEqual(restored.settings.port, 2200)
        self.assertEqual(restored.settings.timeout, 30)
        self.assertEqual(len(restored.commands), 2)

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


if __name__ == "__main__":
    unittest.main()
