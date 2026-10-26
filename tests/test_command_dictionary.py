from dataclasses import FrozenInstanceError, replace
import unittest

from command_dictionary import (
    COMMAND_ENTRIES,
    CONCEPT_IDS,
    CommandEntry,
    search_commands,
    validate_dictionary,
)


class CommandDictionaryCorpusTests(unittest.TestCase):
    def test_exact_size_vendor_counts_and_concept_pairs(self) -> None:
        self.assertEqual(len(COMMAND_ENTRIES), 40)
        self.assertEqual(sum(e.vendor == "Cisco" for e in COMMAND_ENTRIES), 20)
        self.assertEqual(sum(e.vendor == "Huawei" for e in COMMAND_ENTRIES), 20)
        self.assertEqual(len(CONCEPT_IDS), 20)
        for concept_id in CONCEPT_IDS:
            paired = [e for e in COMMAND_ENTRIES if e.concept_id == concept_id]
            self.assertEqual([e.vendor for e in paired], ["Cisco", "Huawei"])

    def test_entries_are_frozen_and_child_collections_are_tuples(self) -> None:
        entry = COMMAND_ENTRIES[0]
        with self.assertRaises(FrozenInstanceError):
            entry.title = "Changed"  # type: ignore[misc]
        self.assertIsInstance(entry.aliases, tuple)
        self.assertIsInstance(entry.keywords, tuple)
        with self.assertRaises(TypeError):
            entry.aliases[0] = "Changed"  # type: ignore[index]

    def test_scope_metadata_is_exact(self) -> None:
        for entry in COMMAND_ENTRIES:
            if entry.vendor == "Cisco":
                self.assertEqual(entry.platform, "Catalyst 9300L")
                self.assertEqual(entry.documented_release, "IOS XE 17.3.x")
                self.assertEqual(entry.deployed_target, "17.3.3")
                self.assertTrue(entry.syntax.startswith("show "))
            else:
                self.assertEqual(entry.platform, "CloudEngine S5755-S")
                self.assertEqual(entry.documented_release, "V600R024C10")
                self.assertEqual(
                    entry.deployed_target,
                    "V600R024C10SPC500 / YunShan OS 1.24.1.1",
                )
                self.assertTrue(entry.syntax.startswith("display "))
            self.assertEqual(entry.mode, "exec")
            self.assertEqual(entry.risk, "read-only")

    def test_expected_commands_are_present_in_concept_order(self) -> None:
        expected = (
            ("show version", "display version"),
            ("show inventory", "display device elabel"),
            ("show version | include uptime", "display version | include uptime"),
            ("show processes cpu sorted", "display cpu"),
            ("show processes memory sorted", "display memory"),
            ("show environment all", "display health"),
            ("show environment power", "display device power"),
            ("show environment fan", "display fan"),
            ("show interfaces status", "display interface brief"),
            ("show interfaces <INTERFACE>", "display interface <INTERFACE>"),
            ("show interfaces counters errors", "display counters error"),
            (
                "show interfaces <INTERFACE> transceiver detail",
                "display interface <INTERFACE> transceiver verbose",
            ),
            ("show vlan brief", "display vlan"),
            ("show mac address-table", "display mac-address"),
            ("show spanning-tree summary", "display stp brief"),
            ("show etherchannel summary", "display eth-trunk"),
            ("show lldp neighbors detail", "display lldp neighbor brief"),
            ("show ip route", "display ip routing-table"),
            ("show ip arp", "display arp"),
            ("show logging", "display logbuffer"),
        )
        actual = tuple(
            tuple(entry.syntax for entry in COMMAND_ENTRIES if entry.concept_id == concept)
            for concept in CONCEPT_IDS
        )
        self.assertEqual(actual, expected)

    def test_fan_entries_do_not_claim_temperature_output(self) -> None:
        entries = [e for e in COMMAND_ENTRIES if e.concept_id == "fans-temperature"]
        self.assertEqual([e.title for e in entries], ["Fan status", "Fan status"])
        for entry in entries:
            user_facing_claims = " ".join(
                (entry.title, entry.description, *entry.aliases, *entry.keywords)
            ).casefold()
            self.assertNotIn("temperature", user_facing_claims)

    def test_public_field_set_is_deliberately_stable(self) -> None:
        self.assertEqual(
            tuple(CommandEntry.__dataclass_fields__),
            (
                "id",
                "vendor",
                "platform",
                "documented_release",
                "deployed_target",
                "concept_id",
                "title",
                "syntax",
                "category",
                "description",
                "aliases",
                "keywords",
                "mode",
                "risk",
                "applicability_notes",
            ),
        )


class CommandDictionaryValidationTests(unittest.TestCase):
    def assert_invalid(self, entries: tuple[CommandEntry, ...], text: str) -> None:
        with self.assertRaisesRegex(ValueError, text):
            validate_dictionary(entries)

    def with_replacement(self, index: int, replacement: CommandEntry) -> tuple[CommandEntry, ...]:
        entries = list(COMMAND_ENTRIES)
        entries[index] = replacement
        return tuple(entries)

    def test_valid_dictionary_returns_same_tuple(self) -> None:
        self.assertIs(validate_dictionary(), COMMAND_ENTRIES)
        self.assertIs(validate_dictionary(COMMAND_ENTRIES), COMMAND_ENTRIES)
        self.assertEqual(validate_dictionary(iter(COMMAND_ENTRIES)), COMMAND_ENTRIES)

    def test_rejects_wrong_corpus_size(self) -> None:
        self.assert_invalid(COMMAND_ENTRIES[:-1], "expected 40 entries")

    def test_rejects_duplicate_id_and_missing_pair(self) -> None:
        replacement = replace(
            COMMAND_ENTRIES[1],
            id=COMMAND_ENTRIES[0].id,
            vendor="Cisco",
            platform="Catalyst 9300L",
            documented_release="IOS XE 17.3.x",
            deployed_target="17.3.3",
            syntax="show version",
        )
        entries = self.with_replacement(1, replacement)
        self.assert_invalid(entries, "duplicate id")
        self.assert_invalid(entries, "expected one Huawei entry for version")

    def test_rejects_bad_id_scope_mode_risk_and_category(self) -> None:
        base = COMMAND_ENTRIES[0]
        cases = (
            (replace(base, id="Cisco Version"), "invalid id"),
            (replace(base, platform="Other"), "scope metadata mismatch"),
            (replace(base, mode="configuration"), "unsupported mode"),
            (replace(base, risk="unknown"), "unsupported risk"),
            (replace(base, category="other"), "unsupported category"),
        )
        for replacement, message in cases:
            with self.subTest(message=message):
                self.assert_invalid(self.with_replacement(0, replacement), message)

    def test_rejects_blank_required_field_and_mutable_collections(self) -> None:
        self.assert_invalid(
            self.with_replacement(0, replace(COMMAND_ENTRIES[0], title=" ")),
            "non-empty strings",
        )
        mutable_aliases = replace(
            COMMAND_ENTRIES[0], aliases=["version"]  # type: ignore[arg-type]
        )
        self.assert_invalid(
            self.with_replacement(0, mutable_aliases),
            "aliases must be a non-empty tuple",
        )

    def test_malformed_field_types_are_reported_as_validation_errors(self) -> None:
        bad_vendor = replace(
            COMMAND_ENTRIES[0], vendor=None  # type: ignore[arg-type]
        )
        self.assert_invalid(
            self.with_replacement(0, bad_vendor),
            "all scalar fields must be non-empty strings",
        )
        bad_aliases = replace(
            COMMAND_ENTRIES[0], aliases=None  # type: ignore[arg-type]
        )
        self.assert_invalid(
            self.with_replacement(0, bad_aliases),
            "aliases must be a non-empty tuple of strings",
        )

    def test_rejects_control_characters_and_command_chaining(self) -> None:
        self.assert_invalid(
            self.with_replacement(
                0, replace(COMMAND_ENTRIES[0], description="Bad\nvalue")
            ),
            "control character",
        )
        self.assert_invalid(
            self.with_replacement(
                0, replace(COMMAND_ENTRIES[0], syntax="show version; reload")
            ),
            "command chaining",
        )

    def test_rejects_output_modifiers_and_file_operations(self) -> None:
        for syntax in (
            "show version | redirect flash:version.txt",
            "show version | append flash:version.txt",
            "show version | tee version.txt",
        ):
            with self.subTest(syntax=syntax):
                self.assert_invalid(
                    self.with_replacement(
                        0, replace(COMMAND_ENTRIES[0], syntax=syntax)
                    ),
                    "unapproved pipeline modifier",
                )
        self.assert_invalid(
            self.with_replacement(
                0, replace(COMMAND_ENTRIES[0], syntax="show version > version.txt")
            ),
            "output redirection is not allowed",
        )
        for operation in ("copy", "delete", "erase", "write", "save", "reset"):
            with self.subTest(operation=operation):
                self.assert_invalid(
                    self.with_replacement(
                        0,
                        replace(
                            COMMAND_ENTRIES[0],
                            syntax=f"show version {operation} flash:file",
                        ),
                    ),
                    "mutating or file-operation token",
                )

    def test_only_reviewed_read_only_pipelines_are_allowed(self) -> None:
        uptime_entries = tuple(
            entry for entry in COMMAND_ENTRIES if entry.concept_id == "uptime"
        )
        self.assertEqual(
            [entry.syntax for entry in uptime_entries],
            ["show version | include uptime", "display version | include uptime"],
        )
        self.assertIs(validate_dictionary(), COMMAND_ENTRIES)

    def test_rejects_unsupported_or_undocumented_placeholders(self) -> None:
        self.assert_invalid(
            self.with_replacement(
                18,
                replace(
                    COMMAND_ENTRIES[18],
                    syntax="show interfaces <PORT>",
                    applicability_notes="Replace <PORT> with a port.",
                ),
            ),
            "unsupported placeholder",
        )
        self.assert_invalid(
            self.with_replacement(
                18,
                replace(
                    COMMAND_ENTRIES[18], applicability_notes="Use a valid interface."
                ),
            ),
            "placeholder substitution is not documented",
        )
        self.assert_invalid(
            self.with_replacement(
                18, replace(COMMAND_ENTRIES[18], syntax="show interfaces <INTERFACE")
            ),
            "malformed placeholder",
        )

    def test_rejects_sensitive_and_literal_address_samples(self) -> None:
        self.assert_invalid(
            self.with_replacement(
                0, replace(COMMAND_ENTRIES[0], description="password: example")
            ),
            "sensitive sample term",
        )
        self.assert_invalid(
            self.with_replacement(
                0, replace(COMMAND_ENTRIES[0], description="Query host 192.0.2.1")
            ),
            "literal address-like sample",
        )
        self.assert_invalid(
            self.with_replacement(
                0, replace(COMMAND_ENTRIES[0], description="Query host 2001:db8::1")
            ),
            "literal address-like sample",
        )

    def test_rejects_pair_without_shared_alias(self) -> None:
        replacement = replace(COMMAND_ENTRIES[1], aliases=("firmware facts",))
        self.assert_invalid(
            self.with_replacement(1, replacement),
            "paired entries have no shared alias for version",
        )


class CommandDictionarySearchTests(unittest.TestCase):
    def test_blank_query_returns_all_in_stable_order(self) -> None:
        self.assertEqual(search_commands(), COMMAND_ENTRIES)
        self.assertEqual(search_commands(" \t--- "), COMMAND_ENTRIES)

    def test_case_punctuation_and_whitespace_are_normalized(self) -> None:
        results = search_commands("  FORWARDING---database\t!! ")
        self.assertEqual(
            [(entry.vendor, entry.concept_id) for entry in results],
            [("Cisco", "mac-table"), ("Huawei", "mac-table")],
        )

    def test_all_query_tokens_are_required_across_searchable_fields(self) -> None:
        results = search_commands("Cisco CRC")
        self.assertEqual([entry.id for entry in results], ["cisco.errors-counters"])
        self.assertEqual(search_commands("Cisco CRC Huawei"), ())

    def test_partial_tokens_match_syntax_and_other_searchable_fields(self) -> None:
        display_results = search_commands("dis", platform="Huawei")
        self.assertEqual(len(display_results), 20)
        self.assertEqual(display_results[0].syntax, "display version")
        self.assertIn(
            "display interface brief",
            [entry.syntax for entry in display_results],
        )
        self.assertEqual(
            [entry.id for entry in search_commands("neigh disc")],
            ["cisco.lldp-neighbors", "huawei.lldp-neighbors"],
        )
        self.assertEqual(
            [entry.id for entry in search_commands("colli")],
            ["cisco.errors-counters", "huawei.errors-counters"],
        )

    def test_partial_token_search_keeps_and_semantics_filters_and_ranking(self) -> None:
        self.assertEqual(search_commands("dis vers missing"), ())
        results = search_commands(
            "DIS INT", platform="HUAWEI", category="INTERFACES"
        )
        self.assertEqual(
            [entry.id for entry in results],
            [
                "huawei.interface-summary",
                "huawei.interface-details",
                "huawei.errors-counters",
                "huawei.optics",
            ],
        )
        self.assertEqual(search_commands("---"), COMMAND_ENTRIES)
        self.assertIs(validate_dictionary(), COMMAND_ENTRIES)

    def test_exact_syntax_and_prefix_ranking(self) -> None:
        exact = search_commands("show environment all")
        self.assertEqual(exact[0].id, "cisco.environmental-health")
        prefix = search_commands("show environment")
        self.assertEqual(
            [entry.id for entry in prefix[:3]],
            [
                "cisco.environmental-health",
                "cisco.power",
                "cisco.fans-temperature",
            ],
        )

    def test_title_alias_matches_rank_above_description_keywords(self) -> None:
        results = search_commands("power")
        self.assertEqual(
            [entry.concept_id for entry in results[:2]],
            ["power", "power"],
        )
        self.assertEqual([entry.vendor for entry in results[:2]], ["Cisco", "Huawei"])

    def test_shared_alias_finds_cross_vendor_pair_in_stable_order(self) -> None:
        results = search_commands("neighbor discovery")
        self.assertEqual(
            [entry.id for entry in results],
            ["cisco.lldp-neighbors", "huawei.lldp-neighbors"],
        )

    def test_platform_and_category_filters_are_independent(self) -> None:
        self.assertEqual(len(search_commands(platform="Cisco")), 20)
        self.assertEqual(len(search_commands(platform="Catalyst-9300L")), 20)
        self.assertEqual(len(search_commands(category="Layer 2")), 10)
        combined = search_commands(platform="HUAWEI", category="layer-3")
        self.assertEqual(
            [entry.concept_id for entry in combined],
            ["routing-table", "arp-neighbor-table"],
        )
        self.assertEqual(search_commands(platform="unknown"), ())
        self.assertEqual(search_commands(category="unknown"), ())

    def test_punctuation_only_non_empty_filters_match_nothing(self) -> None:
        self.assertEqual(search_commands(platform="---"), ())
        self.assertEqual(search_commands(category=" !!! "), ())
        self.assertEqual(search_commands(platform="", category=""), COMMAND_ENTRIES)

    def test_query_and_filters_compose(self) -> None:
        self.assertEqual(
            [entry.id for entry in search_commands("routes", platform="Huawei")],
            ["huawei.routing-table"],
        )
        self.assertEqual(search_commands("routes", category="interfaces"), ())

    def test_non_string_arguments_raise_type_error(self) -> None:
        with self.assertRaises(TypeError):
            search_commands(1)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            search_commands(platform=1)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            search_commands(category=1)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
