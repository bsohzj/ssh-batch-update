import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import history
from run_commands import Command, DeviceResult, FailureCategory, write_summary


class HistoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.now = datetime(2026, 10, 26, 10, 30, tzinfo=timezone.utc)
        self.store = history.HistoryStore(
            self.base / "private" / "history.sqlite3",
            clock=lambda: self.now,
        )

    def tearDown(self):
        self.temporary.cleanup()

    def record(self, job_id="job-1", **changes):
        values = dict(
            job_id=job_id,
            batch_name="cisco_ios — 2 devices — 2026-10-26 18:30:00",
            created_utc=self.now - timedelta(minutes=5),
            scheduled_utc=None,
            started_utc=self.now,
            completed_utc=None,
            source="manual",
            profile_label="Operations",
            device_type="cisco_ios",
            valid_devices=2,
            invalid_devices=0,
            command_count=3,
        )
        values.update(changes)
        return history.HistoryRecord(**values)

    def finish(
        self,
        record,
        status="succeeded",
        success=2,
        failure=0,
        cancelled=0,
        failure_counts=None,
    ):
        self.store.start(record)
        self.now += timedelta(minutes=2)
        self.store.finalize(
            record.job_id,
            status,
            success,
            failure,
            cancelled,
            f"{success} succeeded, {failure} failed, {cancelled} cancelled",
            failure_counts=failure_counts,
        )

    def test_database_is_private_and_round_trips_lifecycle(self):
        record = self.record()
        self.store.start(record)

        running = self.store.get(record.job_id)
        self.assertEqual(running.status, "running")
        self.assertIsNone(running.completed_utc)

        self.now += timedelta(seconds=95)
        self.store.finalize(
            record.job_id,
            "completed_with_failures",
            1,
            1,
            0,
            "1 succeeded, 1 failed, 0 cancelled",
            "/tmp/outputs/run_1",
            "/tmp/outputs/run_1/summary.csv",
        )
        completed = self.store.get(record.job_id)

        self.assertEqual(completed.status, "completed_with_failures")
        self.assertEqual(completed.success_count, 1)
        self.assertEqual(completed.failure_count, 1)
        self.assertEqual(completed.duration_seconds, 95)
        self.assertEqual(os.stat(self.store.path).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(self.store.path.parent).st_mode & 0o777, 0o700)

    def test_version_one_database_migrates_to_version_three_without_data_loss(self):
        legacy_path = self.base / "legacy" / "history.sqlite3"
        legacy_path.parent.mkdir(parents=True)
        with closing(sqlite3.connect(legacy_path)) as connection:
            connection.execute(
                """
                CREATE TABLE history (
                    job_id TEXT PRIMARY KEY, batch_name TEXT NOT NULL,
                    created_utc TEXT NOT NULL, scheduled_utc TEXT NOT NULL DEFAULT '',
                    started_utc TEXT NOT NULL, completed_utc TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL, profile_label TEXT NOT NULL,
                    device_type TEXT NOT NULL, valid_devices INTEGER NOT NULL,
                    invalid_devices INTEGER NOT NULL, command_count INTEGER NOT NULL,
                    status TEXT NOT NULL, success_count INTEGER NOT NULL DEFAULT 0,
                    failure_count INTEGER NOT NULL DEFAULT 0,
                    cancelled_count INTEGER NOT NULL DEFAULT 0,
                    result_text TEXT NOT NULL DEFAULT '', run_directory TEXT NOT NULL DEFAULT '',
                    summary_path TEXT NOT NULL DEFAULT ''
                )
                """
            )
            connection.execute(
                """
                INSERT INTO history (
                    job_id, batch_name, created_utc, scheduled_utc, started_utc,
                    source, profile_label, device_type, valid_devices,
                    invalid_devices, command_count, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "legacy-job", "Legacy batch", "2026-10-26T10:00:00Z", "",
                    "2026-10-26T10:05:00Z", "manual", "Operations", "huawei",
                    1, 0, 2, "succeeded",
                ),
            )
            connection.execute("PRAGMA user_version = 1")
            connection.commit()

        migrated = history.HistoryStore(legacy_path, clock=lambda: self.now)
        record = migrated.get("legacy-job")

        self.assertEqual(record.batch_name, "Legacy batch")
        self.assertIsNone(record.latest_start_utc)
        with closing(sqlite3.connect(legacy_path)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 3)
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(history)")
            }
            failure_table = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name = 'history_failure_counts'"
            ).fetchone()
        self.assertIn("latest_start_utc", columns)
        self.assertIsNotNone(failure_table)

    def test_version_two_database_adds_failure_counts_without_data_loss(self):
        legacy_path = self.base / "version-two" / "history.sqlite3"
        store = history.HistoryStore(legacy_path, clock=lambda: self.now)
        store.start(self.record("version-two-job"))
        with closing(sqlite3.connect(legacy_path)) as connection:
            connection.execute("DROP TABLE history_failure_counts")
            connection.execute("PRAGMA user_version = 2")
            connection.commit()

        migrated = history.HistoryStore(legacy_path, clock=lambda: self.now)

        self.assertEqual(
            migrated.get("version-two-job").batch_name,
            self.record().batch_name,
        )
        with closing(sqlite3.connect(legacy_path)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 3)
            failure_table = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name = 'history_failure_counts'"
            ).fetchone()
        self.assertIsNotNone(failure_table)

    def test_restart_marks_running_records_interrupted(self):
        self.store.start(self.record())
        self.now += timedelta(hours=1)

        changed = self.store.recover_interrupted()
        recovered = self.store.get("job-1")

        self.assertEqual(changed, 1)
        self.assertEqual(recovered.status, "interrupted")
        self.assertEqual(recovered.completed_utc, self.now)
        self.assertIn("closed", recovered.result_text)

    def test_filters_pagination_statistics_and_aggregates(self):
        first = self.record("first", profile_label="Operations")
        second = self.record(
            "second",
            batch_name="Huawei scheduled",
            source="scheduled",
            profile_label="Night Shift",
            device_type="huawei",
            started_utc=self.now + timedelta(days=8),
        )
        self.finish(first, "succeeded", 2, 0, 0)
        self.now += timedelta(days=8)
        self.finish(second, "completed_with_failures", 3, 1, 0)

        records, total = self.store.list(history.HistoryFilter(), limit=1)
        self.assertEqual(total, 2)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].job_id, "second")

        filtered = history.HistoryFilter(
            search="huawei",
            status="completed_with_failures",
            source="scheduled",
            device_type="huawei",
            profile_label="Night Shift",
        )
        records, total = self.store.list(filtered)
        stats = self.store.stats(filtered)
        timeline = self.store.timeline(history.HistoryFilter(), "weekly")
        statuses = self.store.status_counts(history.HistoryFilter())

        self.assertEqual(total, 1)
        self.assertEqual(records[0].job_id, "second")
        self.assertEqual(stats.runs, 1)
        self.assertEqual(stats.successes, 3)
        self.assertEqual(stats.failures, 1)
        self.assertEqual(stats.success_rate, 75.0)
        self.assertEqual(sum(item.successes for item in timeline), 5)
        self.assertEqual(statuses["succeeded"], 1)
        self.assertEqual(statuses["completed_with_failures"], 1)
        self.assertEqual(self.store.distinct("device_type"), ["cisco_ios", "huawei"])

    def test_failure_category_counts_follow_filters_and_include_legacy_residual(self):
        operations = self.record("operations", profile_label="Operations")
        night = self.record(
            "night",
            profile_label="Night Shift",
            started_utc=self.now + timedelta(hours=1),
        )
        self.finish(
            operations,
            "completed_with_failures",
            success=1,
            failure=3,
            failure_counts={
                FailureCategory.AUTHENTICATION: 1,
                FailureCategory.CONNECTIVITY: 1,
            },
        )
        self.finish(
            night,
            "completed_with_failures",
            success=0,
            failure=1,
            failure_counts={FailureCategory.COMMAND_TIMEOUT: 1},
        )

        all_counts = self.store.failure_category_counts(history.HistoryFilter())
        night_counts = self.store.failure_category_counts(
            history.HistoryFilter(profile_label="Night Shift")
        )

        self.assertEqual(all_counts[FailureCategory.AUTHENTICATION.value], 1)
        self.assertEqual(all_counts[FailureCategory.CONNECTIVITY.value], 1)
        self.assertEqual(all_counts[FailureCategory.COMMAND_TIMEOUT.value], 1)
        self.assertEqual(all_counts[history.UNCATEGORIZED_FAILURE], 1)
        self.assertEqual(night_counts[FailureCategory.COMMAND_TIMEOUT.value], 1)
        self.assertEqual(night_counts[history.UNCATEGORIZED_FAILURE], 0)
        self.assertEqual(
            set(all_counts),
            {category.value for category in FailureCategory}
            | {history.UNCATEGORIZED_FAILURE},
        )

    def test_failure_category_validation_and_cascade_cleanup(self):
        record = self.record("categorized")
        self.store.start(record)
        with self.assertRaises(history.HistoryError):
            self.store.finalize(
                record.job_id,
                "completed_with_failures",
                0,
                1,
                0,
                "invalid category",
                failure_counts={"unknown": 1},
            )
        with self.assertRaises(history.HistoryError):
            self.store.finalize(
                record.job_id,
                "completed_with_failures",
                0,
                1,
                0,
                "negative count",
                failure_counts={FailureCategory.CONNECTIVITY: -1},
            )
        with self.assertRaises(history.HistoryError):
            self.store.finalize(
                record.job_id,
                "completed_with_failures",
                0,
                1,
                0,
                "excess count",
                failure_counts={FailureCategory.CONNECTIVITY: 2},
            )

        self.store.finalize(
            record.job_id,
            "completed_with_failures",
            0,
            1,
            0,
            "categorized",
            failure_counts={FailureCategory.CONNECTIVITY: 1},
        )
        self.store.remove(record.job_id)
        with closing(sqlite3.connect(self.store.path)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM history_failure_counts"
                ).fetchone()[0],
                0,
            )

        second = self.record("second-categorized")
        self.finish(
            second,
            "completed_with_failures",
            success=0,
            failure=1,
            failure_counts={FailureCategory.UNEXPECTED: 1},
        )
        self.store.clear()
        with closing(sqlite3.connect(self.store.path)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM history_failure_counts"
                ).fetchone()[0],
                0,
            )

    def test_duration_timeline_groups_median_average_and_count(self):
        def add_duration(
            job_id,
            started,
            duration_seconds,
            status="succeeded",
            profile="Operations",
        ):
            record = self.record(
                job_id,
                batch_name=f"Duration {job_id}",
                started_utc=started,
                profile_label=profile,
            )
            self.store.start(record)
            self.store.finalize(
                job_id,
                status,
                1,
                int(status == "completed_with_failures"),
                0,
                "completed",
                completed_utc=started + timedelta(seconds=duration_seconds),
            )

        start = self.now
        add_duration("one-minute", start, 60)
        add_duration("three-minutes", start + timedelta(hours=1), 180)
        add_duration("five-minutes", start + timedelta(days=1), 300)
        add_duration("other-profile", start, 600, profile="Night Shift")
        filters = history.HistoryFilter(profile_label="Operations")

        daily = self.store.duration_timeline(filters, "daily")
        weekly = self.store.duration_timeline(filters, "weekly")
        monthly = self.store.duration_timeline(filters, "monthly")

        self.assertEqual(len(daily), 2)
        self.assertEqual(daily[0].median_seconds, 120.0)
        self.assertEqual(daily[0].average_seconds, 120.0)
        self.assertEqual(daily[0].run_count, 2)
        self.assertEqual(daily[1].median_seconds, 300.0)
        self.assertEqual(len(weekly), 1)
        self.assertEqual(weekly[0].median_seconds, 180.0)
        self.assertEqual(weekly[0].average_seconds, 180.0)
        self.assertEqual(weekly[0].run_count, 3)
        self.assertEqual(len(monthly), 1)
        self.assertEqual(monthly[0].median_seconds, 180.0)
        self.assertEqual(monthly[0].average_seconds, 180.0)
        self.assertEqual(monthly[0].run_count, 3)

    def test_duration_timeline_excludes_incomplete_outcomes_and_clamps_zero(self):
        def add_duration(job_id, status, duration_seconds):
            started = self.now
            record = self.record(job_id, started_utc=started)
            self.store.start(record)
            if status != "running":
                self.store.finalize(
                    job_id,
                    status,
                    int(status in {"succeeded", "completed_with_failures"}),
                    int(status == "failed"),
                    int(status == "cancelled"),
                    status,
                    completed_utc=started + timedelta(seconds=duration_seconds),
                )

        add_duration("negative", "succeeded", -30)
        add_duration("partial", "completed_with_failures", 30)
        add_duration("failed", "failed", 600)
        add_duration("cancelled", "cancelled", 600)
        add_duration("interrupted", "interrupted", 600)
        add_duration("running", "running", 600)

        buckets = self.store.duration_timeline(history.HistoryFilter(), "daily")

        self.assertEqual(len(buckets), 1)
        self.assertEqual(buckets[0].median_seconds, 15.0)
        self.assertEqual(buckets[0].average_seconds, 15.0)
        self.assertEqual(buckets[0].run_count, 2)
        self.assertEqual(
            self.store.duration_timeline(
                history.HistoryFilter(status="failed"),
                "daily",
            ),
            [],
        )

    def test_records_are_indefinite_until_removed_or_cleared(self):
        old = self.record("old", started_utc=self.now - timedelta(days=800))
        current = self.record("current")
        self.finish(old)
        self.finish(current)

        recent, recent_total = self.store.list(
            history.HistoryFilter(since_utc=self.now - timedelta(days=30))
        )
        all_records, all_total = self.store.list(history.HistoryFilter())

        self.assertEqual(recent_total, 1)
        self.assertEqual(recent[0].job_id, "current")
        self.assertEqual(all_total, 2)
        self.store.remove("old")
        self.assertIsNone(self.store.get("old"))
        self.assertEqual(self.store.clear(), ["current"])
        self.assertEqual(self.store.list(history.HistoryFilter())[1], 0)

    def test_sqlite_contains_no_commands_credentials_or_device_addresses(self):
        record = self.record(
            batch_name="Sanitized batch",
            result_text="sanitized result",
        )
        self.finish(record)
        contents = self.store.path.read_bytes()

        for forbidden in (
            b"show running-config",
            b"super-secret",
            b"network-user",
            b"192.0.2.44",
        ):
            self.assertNotIn(forbidden, contents)

    def test_missing_record_and_invalid_status_fail_safely(self):
        with self.assertRaises(history.HistoryError):
            self.store.finalize("missing", "succeeded", 0, 0, 0, "")
        self.store.start(self.record())
        with self.assertRaises(history.HistoryError):
            self.store.finalize("job-1", "running", 0, 0, 0, "")


class HistoryCommandAndOutputTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.now = datetime(2026, 10, 26, 10, 30, tzinfo=timezone.utc)

    def tearDown(self):
        self.temporary.cleanup()

    def record(self, run_directory="", summary_path=""):
        return history.HistoryRecord(
            job_id="job-commands",
            batch_name="Batch",
            created_utc=self.now,
            scheduled_utc=None,
            started_utc=self.now,
            completed_utc=self.now,
            source="manual",
            profile_label="Manual",
            device_type="cisco_ios",
            valid_devices=1,
            invalid_devices=0,
            command_count=2,
            status="succeeded",
            success_count=1,
            run_directory=str(run_directory),
            summary_path=str(summary_path),
        )

    def test_command_snapshot_round_trip_and_cleanup(self):
        store = history.MemoryHistoryCommandStore()
        snapshot = history.HistoryCommandSnapshot(
            (
                Command("exec", "show version", 1),
                Command("config", "password sensitive-value", 2),
            )
        )

        store.set("job", snapshot)
        restored = store.get("job")

        self.assertEqual(restored, snapshot)
        self.assertIn("password sensitive-value", store.values["job"])
        store.delete("job")
        with self.assertRaises(history.HistoryError):
            store.get("job")

    def test_invalid_command_snapshot_is_rejected(self):
        invalid = json.dumps(
            {
                "version": 1,
                "commands": [{"section": "unknown", "line_number": 0, "text": ""}],
            }
        )
        with self.assertRaises(history.HistoryError):
            history.HistoryCommandSnapshot.from_json(invalid)

    def test_summary_is_loaded_only_from_csv(self):
        summary = self.base / "summary.csv"
        summary.write_text(
            "device,status,failed_section,failed_line,failed_command,transcript_file,error\n"
            "192.0.2.1,success,,,,/tmp/192.0.2.1.txt,\n",
            encoding="utf-8",
        )

        results = history.read_summary(summary)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].address, "192.0.2.1")
        self.assertEqual(results[0].status, "success")
        self.assertIsNone(results[0].failure_category)
        malformed = self.base / "bad.csv"
        malformed.write_text("device,status\n192.0.2.1,success\n", encoding="utf-8")
        with self.assertRaises(history.HistoryError):
            history.read_summary(malformed)

        categorized = self.base / "categorized.csv"
        write_summary(
            categorized,
            [
                DeviceResult(
                    "192.0.2.2",
                    "failed",
                    "",
                    failure_category=FailureCategory.CONNECTIVITY,
                )
            ],
        )
        self.assertEqual(
            history.read_summary(categorized)[0].failure_category,
            FailureCategory.CONNECTIVITY,
        )

    def test_output_deletion_requires_matching_marker_and_summary(self):
        run_directory = self.base / "run_2026-10-26_18-30-00"
        run_directory.mkdir()
        summary = run_directory / "summary.csv"
        summary.write_text(
            "device,status,failed_section,failed_line,failed_command,transcript_file,error\n",
            encoding="utf-8",
        )
        transcript = run_directory / "device.txt"
        transcript.write_text("output", encoding="utf-8")
        record = self.record(run_directory, summary)

        with self.assertRaises(history.HistoryError):
            history.validate_output_deletion(record)
        history.write_output_marker(record)
        self.assertEqual(history.validate_output_deletion(record), run_directory.resolve())

        history.delete_output_directory(record)

        self.assertFalse(run_directory.exists())

    def test_output_deletion_rejects_mismatched_marker(self):
        run_directory = self.base / "run_test"
        run_directory.mkdir()
        summary = run_directory / "summary.csv"
        summary.write_text(
            "device,status,failed_section,failed_line,failed_command,transcript_file,error\n",
            encoding="utf-8",
        )
        (run_directory / history.HISTORY_MARKER).write_text(
            json.dumps({"job_id": "another-job"}), encoding="utf-8"
        )
        with self.assertRaises(history.HistoryError):
            history.validate_output_deletion(self.record(run_directory, summary))


if __name__ == "__main__":
    unittest.main()
