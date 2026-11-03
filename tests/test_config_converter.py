import unittest

from config_converter import (
    VENDOR_CISCO,
    VENDOR_HUAWEI,
    analyze_target_text,
    convert_text,
    detect_vendor,
    render_sectioned_commands,
)


class VendorDetectionTests(unittest.TestCase):
    def test_detects_one_strong_vendor_signature(self) -> None:
        self.assertEqual(detect_vendor("show version\n").vendor, VENDOR_CISCO)
        self.assertEqual(detect_vendor("display version\n").vendor, VENDOR_HUAWEI)

    def test_mixed_or_unknown_input_is_ambiguous(self) -> None:
        self.assertTrue(detect_vendor("description desk\n").ambiguous)
        self.assertTrue(
            detect_vendor("show version\ndisplay version\n").ambiguous
        )


class ConversionTests(unittest.TestCase):
    def test_operational_dictionary_pair_converts_both_directions(self) -> None:
        cisco = convert_text("show interfaces GigabitEthernet1/0/1\n", VENDOR_CISCO)
        self.assertEqual(cisco.exec_commands, ("display interface GE1/0/1",))
        self.assertFalse(cisco.blocking_issues)

        huawei = convert_text("display interface GE1/0/1\n", VENDOR_HUAWEI)
        self.assertEqual(
            huawei.exec_commands,
            ("show interfaces GigabitEthernet1/0/1",),
        )
        self.assertFalse(huawei.blocking_issues)

    def test_common_configuration_block_converts_both_directions(self) -> None:
        source = (
            "hostname EDGE-01\n"
            "vlan 20\n"
            " name VOICE\n"
            "interface GigabitEthernet1/0/2\n"
            " description Phone\n"
            " switchport mode access\n"
            " switchport access vlan 20\n"
            " no shutdown\n"
        )
        result = convert_text(source, VENDOR_CISCO)

        self.assertIn("sysname EDGE-01", result.output_text)
        self.assertIn("interface GE1/0/2", result.output_text)
        self.assertIn("port default vlan 20", result.output_text)
        self.assertIn("undo shutdown", result.output_text)
        self.assertFalse(result.blocking_issues)

        reverse = convert_text(result.output_text, VENDOR_HUAWEI)
        self.assertIn("hostname EDGE-01", reverse.output_text)
        self.assertIn("interface GigabitEthernet1/0/2", reverse.output_text)
        self.assertIn("switchport access vlan 20", reverse.output_text)
        self.assertFalse(reverse.blocking_issues)

    def test_sectioned_input_is_accepted_and_reclassified(self) -> None:
        result = convert_text(
            "[exec]\nshow version\n[config]\nhostname EDGE-01\n",
            VENDOR_CISCO,
        )

        self.assertEqual(result.exec_commands, ("display version",))
        self.assertEqual(result.config_commands, ("sysname EDGE-01",))

    def test_system_services_and_routes_convert(self) -> None:
        result = convert_text(
            "ip name-server 192.0.2.10 192.0.2.11\n"
            "ip domain name example.test\n"
            "ntp server 192.0.2.20\n"
            "logging host 192.0.2.30\n"
            "ip route 0.0.0.0 0.0.0.0 192.0.2.1\n",
            VENDOR_CISCO,
        )

        self.assertEqual(
            result.config_commands,
            (
                "dns server 192.0.2.10",
                "dns server 192.0.2.11",
                "dns domain example.test",
                "ntp-service unicast-server 192.0.2.20",
                "info-center loghost 192.0.2.30",
                "ip route-static 0.0.0.0 0.0.0.0 192.0.2.1",
            ),
        )

    def test_lacp_membership_uses_target_context(self) -> None:
        result = convert_text(
            "interface Port-channel1\n"
            " description Uplink\n"
            "interface GigabitEthernet1/0/1\n"
            " channel-group 1 mode active\n",
            VENDOR_CISCO,
        )

        self.assertIn("interface Eth-Trunk1\n mode lacp-static", result.output_text)
        self.assertIn("eth-trunk 1", result.config_commands)

    def test_mode_transitions_are_ignored(self) -> None:
        result = convert_text(
            "configure terminal\nhostname EDGE-01\nend\n",
            VENDOR_CISCO,
        )

        self.assertEqual(result.config_commands, ("sysname EDGE-01",))
        self.assertEqual(
            [issue.disposition for issue in result.issues],
            ["ignored", "ignored"],
        )

    def test_sensitive_values_are_never_copied_to_result(self) -> None:
        secret = "super-secret-value"
        result = convert_text(
            f"username admin privilege 15 secret 9 {secret}\n",
            VENDOR_CISCO,
        )

        combined = result.output_text + repr(result.issues)
        self.assertNotIn(secret, combined)
        self.assertIn("sensitive command redacted", combined)
        self.assertTrue(result.blocking_issues)

    def test_snmp_community_on_host_line_is_redacted(self) -> None:
        secret = "legacy-community"
        result = convert_text(
            f"snmp-server host 192.0.2.10 version 2c {secret}\n",
            VENDOR_CISCO,
        )

        self.assertNotIn(secret, result.output_text + repr(result.issues))
        self.assertTrue(result.blocking_issues)

    def test_unsupported_commands_become_target_review_comments(self) -> None:
        result = convert_text("aaa new-model\n", VENDOR_CISCO)

        self.assertTrue(result.output_text.startswith("# REVIEW line 1:"))
        self.assertTrue(result.blocking_issues)
        self.assertFalse(result.commands)

    def test_unknown_interface_family_is_blocking(self) -> None:
        result = convert_text("interface TenGigabitEthernet1/0/1\n", VENDOR_CISCO)

        self.assertIn("<REVIEW_INTERFACE>", result.output_text)
        self.assertTrue(result.blocking_issues)


class TargetAnalysisTests(unittest.TestCase):
    def test_rechecks_and_classifies_edited_output(self) -> None:
        result = analyze_target_text(
            "display version\nsysname EDGE-01\ninterface GE1/0/1\n description Desk\n",
            VENDOR_HUAWEI,
        )

        self.assertEqual(result.exec_commands, ("display version",))
        self.assertEqual(
            result.config_commands,
            ("sysname EDGE-01", "interface GE1/0/1", "description Desk"),
        )
        self.assertFalse(result.blocking_issues)

    def test_unknown_line_review_marker_and_placeholder_block_handoff(self) -> None:
        result = analyze_target_text(
            "display version\n# REVIEW line 2: inspect\nfoo bar\ninterface <REVIEW_INTERFACE>\n",
            VENDOR_HUAWEI,
        )

        self.assertEqual(len(result.blocking_issues), 3)
        with self.assertRaises(ValueError):
            render_sectioned_commands(result)

    def test_sectioned_export_contains_only_clean_executable_commands(self) -> None:
        result = analyze_target_text(
            "display version\nsysname EDGE-01\n",
            VENDOR_HUAWEI,
        )

        self.assertEqual(
            render_sectioned_commands(result),
            "[exec]\ndisplay version\n\n[config]\nsysname EDGE-01\n",
        )


if __name__ == "__main__":
    unittest.main()
