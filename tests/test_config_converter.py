import unittest
from unittest.mock import patch

import config_converter
from command_dictionary import COMMAND_ENTRIES, CONCEPT_IDS

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
    def test_dictionary_growth_keeps_original_template_inventory(self) -> None:
        expected = frozenset(CONCEPT_IDS[:48])
        self.assertIsInstance(config_converter.CONVERTER_TEMPLATE_CONCEPT_IDS, frozenset)
        self.assertEqual(config_converter.CONVERTER_TEMPLATE_CONCEPT_IDS, expected)
        for vendor in (VENDOR_CISCO, VENDOR_HUAWEI):
            pairs = config_converter._TEMPLATE_PAIRS[vendor]
            self.assertEqual(len(pairs), 48)
            self.assertEqual({pair.source.concept_id for pair in pairs}, expected)
            self.assertTrue(all(pair.source in COMMAND_ENTRIES[:96] and pair.target in COMMAND_ENTRIES[:96] for pair in pairs))
            with patch.object(config_converter, "COMMAND_ENTRIES", COMMAND_ENTRIES[:96]):
                self.assertEqual(config_converter._build_template_pairs(vendor), pairs)

    def test_added_reference_templates_do_not_bypass_custom_guards(self) -> None:
        cases = (
            (VENDOR_CISCO, "ip name-server invalid"),
            (VENDOR_HUAWEI, "dns server invalid"),
            (VENDOR_CISCO, "ip address 192.0.2.1 255.255.255.0"),
            (VENDOR_HUAWEI, "ip address 192.0.2.1 255.255.255.0"),
            (VENDOR_CISCO, "spanning-tree portfast"),
            (VENDOR_HUAWEI, "stp edged-port enable"),
            (VENDOR_CISCO, "channel-group 1 mode active"),
            (VENDOR_HUAWEI, "eth-trunk 1"),
            (VENDOR_HUAWEI, "mode lacp-static"),
            (VENDOR_CISCO, "interface GigabitEthernet1/0/1\n channel-group 1 mode invalid"),
            (VENDOR_HUAWEI, "interface GE1/0/1\n eth-trunk invalid"),
        )
        for vendor, text in cases:
            with self.subTest(vendor=vendor, text=text):
                result = convert_text(text, vendor)
                self.assertTrue(result.blocking_issues)
                self.assertEqual(result.config_commands, () if "\n" not in text else ("interface GE1/0/1" if vendor == VENDOR_CISCO else "interface GigabitEthernet1/0/1",))

    def test_custom_vlan_expansion_preserves_global_context(self) -> None:
        result = convert_text("vlan batch 10 20\nname VOICE", VENDOR_HUAWEI)
        self.assertEqual(result.config_commands, ("vlan 10", "vlan 20"))
        self.assertTrue(result.blocking_issues)

    def test_invalid_vlan_batch_cannot_fall_through_generic_vlan_template(self) -> None:
        for values in ("4095", "0", "10 to 20", "10,20", "invalid", ""):
            text = f"vlan batch {values}".strip()
            with self.subTest(text=text):
                result = convert_text(text, VENDOR_HUAWEI)
                self.assertTrue(result.blocking_issues)
                self.assertEqual(result.config_commands, ())
                self.assertIn("REVIEW", result.output_text)
                rechecked = analyze_target_text(result.output_text, VENDOR_CISCO)
                self.assertTrue(rechecked.blocking_issues)
                with self.assertRaises(ValueError):
                    render_sectioned_commands(rechecked)

    def test_cisco_source_rejects_huawei_only_vlan_batch(self) -> None:
        for text in ("vlan batch 10 20", "vlan batch", "vlan batch invalid"):
            with self.subTest(text=text):
                result = convert_text(text, VENDOR_CISCO)
                self.assertEqual(result.commands, ())
                self.assertTrue(result.blocking_issues)
                self.assertIn("Huawei-only", result.issues[0].message)
                self.assertIn("REVIEW", result.output_text)
                rechecked = analyze_target_text(result.output_text, VENDOR_HUAWEI)
                self.assertEqual(rechecked.commands, ())
                self.assertTrue(rechecked.blocking_issues)
                with self.assertRaises(ValueError):
                    render_sectioned_commands(rechecked)

    def test_lacp_bundle_mode_is_ignored_and_members_use_complete_context(self) -> None:
        for mode_line, expected_mode in ((" mode lacp-static\n", "active"), ("", "on")):
            result = convert_text(
                f"interface Eth-Trunk1\n{mode_line}interface GE1/0/1\n eth-trunk 1\n",
                VENDOR_HUAWEI,
            )
            self.assertFalse(result.blocking_issues)
            self.assertEqual(result.config_commands, (
                "interface Port-channel1", "interface GigabitEthernet1/0/1",
                f"channel-group 1 mode {expected_mode}",
            ))
            self.assertEqual(sum(issue.disposition == "ignored" for issue in result.issues), bool(mode_line))

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
    def test_vlan_batch_target_validation_is_vendor_specific_and_keeps_global_context(self) -> None:
        valid = analyze_target_text("vlan batch 10 20", VENDOR_HUAWEI)
        self.assertFalse(valid.blocking_issues)
        self.assertEqual(valid.config_commands, ("vlan batch 10 20",))
        with_name = analyze_target_text("vlan batch 10 20\nname VOICE", VENDOR_HUAWEI)
        self.assertEqual(with_name.config_commands, ("vlan batch 10 20",))
        self.assertTrue(with_name.blocking_issues)
        for vendor in (VENDOR_CISCO, VENDOR_HUAWEI):
            for values in ("4095", "0", "10 to 20", "10,20", "invalid", ""):
                with self.subTest(vendor=vendor, values=values):
                    result = analyze_target_text(f"vlan batch {values}".strip(), vendor)
                    self.assertEqual(result.config_commands, ())
                    self.assertTrue(result.blocking_issues)
                    with self.assertRaises(ValueError):
                        render_sectioned_commands(result)
        cisco = analyze_target_text("vlan batch 10 20", VENDOR_CISCO)
        self.assertTrue(cisco.blocking_issues)
        self.assertEqual(cisco.config_commands, ())

    def test_new_reference_templates_do_not_relax_target_context_guards(self) -> None:
        for vendor, lines in (
            (VENDOR_CISCO, ("ip address 192.0.2.1 255.255.255.0", "spanning-tree portfast", "channel-group 1 mode active")),
            (VENDOR_HUAWEI, ("ip address 192.0.2.1 255.255.255.0", "stp edged-port enable", "eth-trunk 1", "mode lacp-static")),
        ):
            for text in lines:
                with self.subTest(vendor=vendor, text=text):
                    result = analyze_target_text(text, vendor)
                    self.assertTrue(result.blocking_issues)
                    self.assertEqual(result.config_commands, ())

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
