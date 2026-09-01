import csv
import os
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, call, patch

import run_commands as runner


class CommandRunnerTests(unittest.TestCase):
    def settings(self, device_type="huawei", enable_secret=None):
        return runner.Settings(
            username="admin",
            password="ssh-secret",
            enable_secret=enable_secret,
            port=22,
            timeout=15,
            device_type=device_type,
        )

    def test_inventory_accepts_hostnames_normalises_ips_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            inventory = Path(directory) / "devices.txt"
            inventory.write_text(
                "# comment\n192.0.2.1\n192.0.2.1\n2001:0db8::1\n"
                "Switch-A.Example.net\nswitch-a.example.net\nbad host\n",
                encoding="utf-8",
            )
            entries = runner.read_device_entries(inventory)

        self.assertEqual(
            [(entry.address, bool(entry.error)) for entry in entries],
            [
                ("192.0.2.1", False),
                ("2001:db8::1", False),
                ("switch-a.example.net", False),
                ("bad host", True),
            ],
        )

    def test_command_sections_preserve_literal_passwords_and_reject_malformed_file(self):
        with tempfile.TemporaryDirectory() as directory:
            command_file = Path(directory) / "commands.txt"
            command_file.write_text(
                "[exec]\nshow version\n[config]\nset password\n secret value \n",
                encoding="utf-8",
            )
            commands = runner.read_commands(command_file)
            malformed = Path(directory) / "malformed.txt"
            malformed.write_text("show version\n", encoding="utf-8")

            with self.assertRaisesRegex(runner.ConfigurationError, "outside a section"):
                runner.read_commands(malformed)

        self.assertEqual(
            [(item.section, item.text) for item in commands],
            [("exec", "show version"), ("config", "set password"), ("config", " secret value ")],
        )

    def test_execute_runs_exec_and_config_in_order_with_enable(self):
        connection = Mock()
        connection.enable.return_value = "enabled"
        connection.disable_paging.return_value = "paging off"
        connection.send_command.return_value = "version output"
        connection.config_mode.return_value = "Enter system view"
        connection.send_command_timing.side_effect = ["configured", "password accepted"]
        connection.exit_config_mode.return_value = "quit"
        netmiko = types.SimpleNamespace(ConnectHandler=Mock(return_value=connection))
        commands = [
            runner.Command("exec", "display version", 2),
            runner.Command("config", "set authentication", 5),
            runner.Command("config", "the-password", 6),
        ]

        result = runner.execute_device(
            "192.0.2.1",
            self.settings(enable_secret="enable-secret"),
            commands,
            [],
            netmiko,
        )

        self.assertEqual(result.status, "success")
        self.assertIn("> the-password\npassword accepted", result.transcript)
        self.assertEqual(
            connection.method_calls,
            [
                call.enable(),
                call.disable_paging(),
                call.send_command("display version"),
                call.config_mode(),
                call.send_command_timing("set authentication", cmd_verify=False),
                call.send_command_timing("the-password", cmd_verify=False),
                call.exit_config_mode(),
                call.disconnect(),
            ],
        )

    def test_failure_pattern_stops_one_device_after_partial_transcript(self):
        connection = Mock()
        connection.disable_paging.return_value = ""
        connection.send_command.side_effect = ["good output", "% Invalid input detected at '^' marker."]
        netmiko = types.SimpleNamespace(ConnectHandler=Mock(return_value=connection))
        commands = [
            runner.Command("exec", "show version", 2),
            runner.Command("exec", "bad command", 3),
            runner.Command("exec", "must not run", 4),
        ]

        result = runner.execute_device(
            "192.0.2.1",
            self.settings("cisco_ios"),
            commands,
            runner.compile_failure_patterns("cisco_ios", []),
            netmiko,
        )

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.failed_line, "3")
        self.assertEqual(result.failed_command, "bad command")
        self.assertNotIn("must not run", result.transcript)
        self.assertEqual(connection.send_command.call_count, 2)
        self.assertIn("failure pattern", result.error)

    def test_custom_failure_pattern_is_used_for_other_netmiko_platforms(self):
        connection = Mock()
        connection.disable_paging.return_value = ""
        connection.send_command.return_value = "Access policy denied this request"
        netmiko = types.SimpleNamespace(ConnectHandler=Mock(return_value=connection))

        result = runner.execute_device(
            "router.example.net",
            self.settings("generic_termserver"),
            [runner.Command("exec", "run restricted task", 2)],
            runner.compile_failure_patterns("generic_termserver", ["policy denied"]),
            netmiko,
        )

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.failed_command, "run restricted task")

    def test_progress_reports_connection_mode_and_current_command(self):
        connection = Mock()
        connection.disable_paging.return_value = ""
        connection.config_mode.return_value = ""
        connection.send_command_timing.return_value = ""
        connection.exit_config_mode.return_value = ""
        progress = []

        runner.execute_device(
            "192.0.2.1",
            self.settings(),
            [runner.Command("config", "snmp-agent sys-info version v3", 4)],
            [],
            types.SimpleNamespace(ConnectHandler=Mock(return_value=connection)),
            progress=progress.append,
        )

        self.assertEqual(
            progress,
            [
                "CONNECTING 192.0.2.1",
                "DISABLING PAGER 192.0.2.1",
                "ENTERING CONFIG MODE 192.0.2.1",
                "RUNNING 192.0.2.1 [config] line 4: snmp-agent sys-info version v3",
                "EXITING CONFIG MODE 192.0.2.1",
                "DISCONNECTING 192.0.2.1",
            ],
        )

    def test_dry_run_needs_no_credentials_or_netmiko(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            devices = base / "devices.txt"
            commands = base / "commands.txt"
            devices.write_text("router.example.net\n", encoding="utf-8")
            commands.write_text("[exec]\nshow version\n", encoding="utf-8")
            with patch.object(runner, "_load_netmiko", side_effect=AssertionError("not loaded")), patch.dict(
                os.environ, {}, clear=True
            ):
                exit_code = runner.main(
                    [str(devices), str(commands), "--device-type", "cisco_ios"]
                )

        self.assertEqual(exit_code, 0)

    def test_run_directory_uses_a_human_readable_timestamp(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            runner, "datetime"
        ) as mock_datetime:
            mock_datetime.now.return_value = datetime(2026, 9, 1, 17, 4, 30)
            run_directory = runner.create_run_directory(Path(directory))

        self.assertEqual(run_directory.name, "run_2026-09-01_17-04-30")

    def test_apply_continues_after_connection_failure_and_writes_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            devices = base / "devices.txt"
            command_file = base / "commands.txt"
            output_dir = base / "outputs"
            devices.write_text("192.0.2.10\n192.0.2.11\n", encoding="utf-8")
            command_file.write_text("[exec]\nshow version\n", encoding="utf-8")
            successful_connection = Mock()
            successful_connection.disable_paging.return_value = ""
            successful_connection.send_command.return_value = "version"
            netmiko = types.SimpleNamespace(
                ConnectHandler=Mock(
                    side_effect=[RuntimeError("login ssh-secret rejected"), successful_connection]
                )
            )
            with patch.dict(
                os.environ,
                {"SSH_USERNAME": "admin", "SSH_PASSWORD": "ssh-secret"},
                clear=True,
            ), patch.object(runner, "_load_netmiko", return_value=netmiko):
                exit_code = runner.main(
                    [
                        str(devices), str(command_file), "--device-type", "cisco_ios", "--apply",
                        "--output-dir", str(output_dir),
                    ]
                )

            run_directory = next(output_dir.iterdir())
            with (run_directory / "summary.csv").open(encoding="utf-8", newline="") as summary:
                rows = list(csv.DictReader(summary))
            success_transcript_exists = Path(rows[1]["transcript_file"]).exists()

        self.assertEqual(exit_code, 1)
        self.assertEqual([row["status"] for row in rows], ["failed", "success"])
        self.assertNotIn("ssh-secret", rows[0]["error"])
        self.assertTrue(success_transcript_exists)


if __name__ == "__main__":
    unittest.main()
