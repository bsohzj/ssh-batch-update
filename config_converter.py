"""Conservative, local Cisco IOS XE and Huawei VRP command conversion.

The converter intentionally supports a reviewed baseline rather than trying to
guess at vendor- or site-specific semantics.  Its result keeps executable
commands separate from rendered review comments so callers can safely decide
whether a result is suitable for the SSH command form.
"""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import re
from typing import Optional

from command_dictionary import COMMAND_ENTRIES, CommandEntry


VENDOR_CISCO = "cisco"
VENDOR_HUAWEI = "huawei"
VENDORS = (VENDOR_CISCO, VENDOR_HUAWEI)

SECTION_EXEC = "exec"
SECTION_CONFIG = "config"

DISPOSITION_REVIEW = "review"
DISPOSITION_UNMAPPED = "unmapped"
DISPOSITION_IGNORED = "ignored"


@dataclass(frozen=True)
class VendorDetection:
    """The deterministic evidence used to auto-detect an input vendor."""

    vendor: Optional[str]
    cisco_score: int
    huawei_score: int
    reasons: tuple[str, ...]

    @property
    def ambiguous(self) -> bool:
        return self.vendor is None


@dataclass(frozen=True)
class ConvertedCommand:
    """One executable target command and its source provenance."""

    source_line: int
    source_text: str
    target_text: str
    section: str


@dataclass(frozen=True)
class ConversionIssue:
    """One conversion or validation decision requiring user visibility."""

    line_number: int
    disposition: str
    section: str
    message: str
    source_text: str
    blocking: bool = False


@dataclass(frozen=True)
class ConversionResult:
    """Rendered target text, executable commands, and review information."""

    source_vendor: str
    target_vendor: str
    output_text: str
    commands: tuple[ConvertedCommand, ...]
    issues: tuple[ConversionIssue, ...]

    @property
    def exec_commands(self) -> tuple[str, ...]:
        return tuple(
            command.target_text
            for command in self.commands
            if command.section == SECTION_EXEC
        )

    @property
    def config_commands(self) -> tuple[str, ...]:
        return tuple(
            command.target_text
            for command in self.commands
            if command.section == SECTION_CONFIG
        )

    @property
    def blocking_issues(self) -> tuple[ConversionIssue, ...]:
        return tuple(issue for issue in self.issues if issue.blocking)


_PLACEHOLDER_RE = re.compile(r"<[A-Z][A-Z0-9_:.-]*>")
_SECTION_RE = re.compile(r"^\[(exec|config)\]$", re.IGNORECASE)

_CISCO_SIGNATURES = (
    re.compile(r"^show\s", re.IGNORECASE),
    re.compile(r"^hostname\s", re.IGNORECASE),
    re.compile(r"^switchport\s", re.IGNORECASE),
    re.compile(r"^no shutdown$", re.IGNORECASE),
    re.compile(r"^ip route\s", re.IGNORECASE),
    re.compile(r"^ip name-server\s", re.IGNORECASE),
    re.compile(r"^ip domain name\s", re.IGNORECASE),
    re.compile(r"^logging host\s", re.IGNORECASE),
    re.compile(r"^ntp server\s", re.IGNORECASE),
    re.compile(r"^channel-group\s", re.IGNORECASE),
    re.compile(r"^spanning-tree\s", re.IGNORECASE),
    re.compile(r"^configure terminal$", re.IGNORECASE),
    re.compile(
        r"^interface\s+(?:GigabitEthernet|TenGigabitEthernet|Vlan\d|Port-channel)",
        re.IGNORECASE,
    ),
)
_HUAWEI_SIGNATURES = (
    re.compile(r"^display\s", re.IGNORECASE),
    re.compile(r"^sysname\s", re.IGNORECASE),
    re.compile(r"^port (?:link-type|default vlan|trunk)\s", re.IGNORECASE),
    re.compile(r"^undo shutdown$", re.IGNORECASE),
    re.compile(r"^ip route-static\s", re.IGNORECASE),
    re.compile(r"^dns (?:server|domain)\s", re.IGNORECASE),
    re.compile(r"^info-center loghost\s", re.IGNORECASE),
    re.compile(r"^ntp-service\s", re.IGNORECASE),
    re.compile(r"^eth-trunk\s", re.IGNORECASE),
    re.compile(r"^stp\s", re.IGNORECASE),
    re.compile(r"^system-view$", re.IGNORECASE),
    re.compile(
        r"^interface\s+(?:GE|10GE|25GE|40GE|100GE|XGigabitEthernet|Vlanif|Eth-Trunk)",
        re.IGNORECASE,
    ),
)

_MODE_TRANSITIONS = {
    VENDOR_CISCO: frozenset({"configure terminal", "conf t", "end", "exit"}),
    VENDOR_HUAWEI: frozenset({"system-view", "return", "quit"}),
}

_UNSUPPORTED_PREFIXES = (
    "aaa ",
    "authentication ",
    "authorization ",
    "accounting ",
    "dot1x ",
    "mab ",
    "qos ",
    "traffic ",
    "policy-map ",
    "class-map ",
    "snmp-server ",
    "snmp-agent ",
    "crypto ",
    "pki ",
    "stack ",
    "stack-port ",
    "device board ",
    "transceiver ",
    "redundancy ",
    "event manager ",
    "track ",
    "ip sla ",
)

_SENSITIVE_PATTERNS = (
    re.compile(r"\b(?:password|secret|community|shared-key|cipher)\b", re.IGNORECASE),
    re.compile(r"^username\s+.*\s(?:password|secret)\s", re.IGNORECASE),
    re.compile(r"^enable\s+(?:password|secret)\s", re.IGNORECASE),
    re.compile(r"^password\s", re.IGNORECASE),
    re.compile(r"^local-user\s+.*\spassword\s", re.IGNORECASE),
    re.compile(r"^set authentication password\s", re.IGNORECASE),
    re.compile(r"^(?:snmp-server|snmp-agent)\s+community\s", re.IGNORECASE),
    re.compile(r"^(?:radius-server|tacacs-server).*\bkey\b", re.IGNORECASE),
    re.compile(r"\bshared-key\b", re.IGNORECASE),
    re.compile(r"\bkey\s+(?:cipher|simple|\d+)\b", re.IGNORECASE),
)

_INTERFACE_CONFIG_CONCEPTS = frozenset(
    {
        "interface-description",
        "port-link-type",
        "access-vlan",
        "trunk-vlan-list",
        "interface-admin-state",
        "trunk-native-vlan",
        "interface-mtu",
    }
)


@dataclass(frozen=True)
class _TemplatePair:
    source: CommandEntry
    target: CommandEntry
    regex: re.Pattern[str]
    placeholders: tuple[str, ...]


def _template_regex(template: str) -> tuple[re.Pattern[str], tuple[str, ...]]:
    pieces: list[str] = []
    placeholders: list[str] = []
    position = 0
    for index, match in enumerate(_PLACEHOLDER_RE.finditer(template)):
        pieces.append(re.escape(template[position : match.start()]))
        pieces.append(f"(?P<p{index}>.+?)")
        placeholders.append(match.group(0))
        position = match.end()
    pieces.append(re.escape(template[position:]))
    return re.compile("^" + "".join(pieces) + "$", re.IGNORECASE), tuple(placeholders)


def _build_template_pairs(source_vendor: str) -> tuple[_TemplatePair, ...]:
    source_name = "Cisco" if source_vendor == VENDOR_CISCO else "Huawei"
    target_name = "Huawei" if source_vendor == VENDOR_CISCO else "Cisco"
    by_concept: dict[str, dict[str, CommandEntry]] = {}
    for entry in COMMAND_ENTRIES:
        by_concept.setdefault(entry.concept_id, {})[entry.vendor] = entry
    pairs: list[_TemplatePair] = []
    for entries in by_concept.values():
        source = entries[source_name]
        target = entries[target_name]
        regex, placeholders = _template_regex(source.syntax)
        pairs.append(_TemplatePair(source, target, regex, placeholders))
    return tuple(
        sorted(
            pairs,
            key=lambda pair: len(_PLACEHOLDER_RE.sub("", pair.source.syntax)),
            reverse=True,
        )
    )


_TEMPLATE_PAIRS = {
    vendor: _build_template_pairs(vendor) for vendor in VENDORS
}


def opposite_vendor(vendor: str) -> str:
    if vendor == VENDOR_CISCO:
        return VENDOR_HUAWEI
    if vendor == VENDOR_HUAWEI:
        return VENDOR_CISCO
    raise ValueError(f"unsupported vendor: {vendor}")


def vendor_label(vendor: str) -> str:
    if vendor == VENDOR_CISCO:
        return "Cisco IOS XE"
    if vendor == VENDOR_HUAWEI:
        return "Huawei VRP"
    raise ValueError(f"unsupported vendor: {vendor}")


def detect_vendor(text: str) -> VendorDetection:
    """Detect a source vendor from unambiguous, vendor-specific syntax."""

    cisco_score = 0
    huawei_score = 0
    reasons: list[str] = []
    for line_number, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith(("!", "#")) or _SECTION_RE.fullmatch(line):
            continue
        if any(pattern.search(line) for pattern in _CISCO_SIGNATURES):
            cisco_score += 1
            reasons.append(f"line {line_number}: Cisco syntax")
        if any(pattern.search(line) for pattern in _HUAWEI_SIGNATURES):
            huawei_score += 1
            reasons.append(f"line {line_number}: Huawei syntax")

    vendor: Optional[str] = None
    if cisco_score and not huawei_score:
        vendor = VENDOR_CISCO
    elif huawei_score and not cisco_score:
        vendor = VENDOR_HUAWEI
    elif cisco_score >= huawei_score + 2:
        vendor = VENDOR_CISCO
    elif huawei_score >= cisco_score + 2:
        vendor = VENDOR_HUAWEI
    return VendorDetection(vendor, cisco_score, huawei_score, tuple(reasons))


def _is_sensitive(line: str) -> bool:
    if line.lower().startswith("description "):
        return False
    return any(pattern.search(line) for pattern in _SENSITIVE_PATTERNS)


def _comment_prefix(vendor: str) -> str:
    return "!" if vendor == VENDOR_CISCO else "#"


def _review_comment(vendor: str, line_number: int, message: str) -> str:
    return f"{_comment_prefix(vendor)} REVIEW line {line_number}: {message}"


def _normalise_vlan_list(value: str, source_vendor: str) -> str:
    stripped = value.strip()
    if stripped.lower() == "all":
        return "all"
    if source_vendor == VENDOR_CISCO:
        return " ".join(part.strip() for part in stripped.split(",") if part.strip())
    return ",".join(stripped.split())


def _map_interface(value: str, source_vendor: str) -> tuple[str, bool]:
    if _PLACEHOLDER_RE.fullmatch(value):
        return value, False
    if source_vendor == VENDOR_CISCO:
        mappings = (
            (r"GigabitEthernet(.+)", r"GE\1"),
            (r"Vlan(\d+)", r"Vlanif\1"),
            (r"Port-channel(\d+)", r"Eth-Trunk\1"),
        )
    else:
        mappings = (
            (r"GE(.+)", r"GigabitEthernet\1"),
            (r"Vlanif(\d+)", r"Vlan\1"),
            (r"Eth-Trunk(\d+)", r"Port-channel\1"),
        )
    for pattern, replacement in mappings:
        if re.fullmatch(pattern, value, re.IGNORECASE):
            return re.sub(pattern, replacement, value, flags=re.IGNORECASE), True
    return "<REVIEW_INTERFACE>", False


def _render_template_target(
    pair: _TemplatePair,
    match: re.Match[str],
    source_vendor: str,
) -> tuple[str, bool]:
    target = pair.target.syntax
    interface_was_mapped = True
    for index, placeholder in enumerate(pair.placeholders):
        value = match.group(f"p{index}")
        if placeholder == "<INTERFACE>":
            value, interface_was_mapped = _map_interface(value, source_vendor)
        elif placeholder == "<VLAN_LIST>":
            value = _normalise_vlan_list(value, source_vendor)
        target = target.replace(placeholder, value, 1)
    return target, interface_was_mapped


def _entry_section(entry: CommandEntry) -> str:
    if entry.mode in {"exec", "privileged exec", "user view"}:
        return SECTION_EXEC
    return SECTION_CONFIG


def _context_for_line(line: str, vendor: str) -> tuple[str, Optional[str]]:
    match = re.fullmatch(r"interface\s+(.+)", line, re.IGNORECASE)
    if match:
        return "interface", match.group(1)
    if re.fullmatch(r"vlan\s+\d+", line, re.IGNORECASE):
        return "vlan", None
    if vendor == VENDOR_HUAWEI and re.fullmatch(
        r"vlan batch\s+.+", line, re.IGNORECASE
    ):
        return "global", None
    return "global", None


def _lag_modes(text: str, vendor: str) -> set[str]:
    modes: set[str] = set()
    current_group: Optional[str] = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if vendor == VENDOR_CISCO:
            match = re.fullmatch(
                r"channel-group\s+(\d+)\s+mode\s+(active|passive)",
                line,
                re.IGNORECASE,
            )
            if match:
                modes.add(match.group(1))
        else:
            match = re.fullmatch(r"interface\s+Eth-Trunk(\d+)", line, re.IGNORECASE)
            if match:
                current_group = match.group(1)
                continue
            if line.startswith("interface ") or line in {"#", "quit", "return"}:
                current_group = None
            if current_group and line.lower() == "mode lacp-static":
                modes.add(current_group)
    return modes


def _append_command(
    commands: list[ConvertedCommand],
    output_lines: list[str],
    source_line: int,
    source_text: str,
    target_text: str,
    section: str,
    *,
    indent: bool = False,
) -> None:
    command_text = target_text.strip()
    commands.append(
        ConvertedCommand(source_line, source_text, command_text, section)
    )
    output_lines.append(f" {command_text}" if indent else command_text)


def _add_placeholder_issue(
    issues: list[ConversionIssue],
    line_number: int,
    section: str,
    source_text: str,
    target_text: str,
) -> None:
    if _PLACEHOLDER_RE.search(target_text):
        issues.append(
            ConversionIssue(
                line_number,
                DISPOSITION_REVIEW,
                section,
                "Replace every review placeholder before executable export or SSH import.",
                source_text,
                True,
            )
        )


def convert_text(
    text: str,
    source_vendor: Optional[str] = None,
) -> ConversionResult:
    """Convert supported source text to the opposite vendor conservatively."""

    if source_vendor is None:
        detection = detect_vendor(text)
        if detection.vendor is None:
            raise ValueError(
                "Could not confidently detect Cisco or Huawei syntax; select a source vendor."
            )
        source_vendor = detection.vendor
    if source_vendor not in VENDORS:
        raise ValueError(f"unsupported vendor: {source_vendor}")
    if not text.strip():
        raise ValueError("Enter or import configuration or command text first.")

    target_vendor = opposite_vendor(source_vendor)
    commands: list[ConvertedCommand] = []
    issues: list[ConversionIssue] = []
    output_lines: list[str] = []
    declared_section: Optional[str] = None
    context = "global"
    context_name: Optional[str] = None
    lacp_groups = _lag_modes(text, source_vendor)

    for line_number, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line:
            output_lines.append("")
            continue
        section_match = _SECTION_RE.fullmatch(line)
        if section_match:
            declared_section = section_match.group(1).lower()
            continue
        if line.startswith(("!", "#")):
            context = "global"
            context_name = None
            if line in {"!", "#"}:
                output_lines.append(_comment_prefix(target_vendor))
            continue

        lowered = line.casefold()
        if lowered in _MODE_TRANSITIONS[source_vendor]:
            issues.append(
                ConversionIssue(
                    line_number,
                    DISPOSITION_IGNORED,
                    declared_section or SECTION_CONFIG,
                    "Configuration-mode transition is managed by the SSH runner.",
                    line,
                )
            )
            continue

        if _is_sensitive(line):
            message = "sensitive credential or key material was omitted"
            output_lines.append(_review_comment(target_vendor, line_number, message))
            issues.append(
                ConversionIssue(
                    line_number,
                    DISPOSITION_REVIEW,
                    declared_section or SECTION_CONFIG,
                    "Sensitive values are not portable and were redacted.",
                    "[sensitive command redacted]",
                    True,
                )
            )
            continue

        if any(lowered.startswith(prefix) for prefix in _UNSUPPORTED_PREFIXES):
            message = "unsupported site-, security-, or hardware-specific command"
            output_lines.append(_review_comment(target_vendor, line_number, message))
            issues.append(
                ConversionIssue(
                    line_number,
                    DISPOSITION_UNMAPPED,
                    declared_section or SECTION_CONFIG,
                    "This command family has no reviewed generic mapping.",
                    line,
                    True,
                )
            )
            continue

        matched = False

        # Directly compatible generic system services.
        custom_patterns: tuple[tuple[str, str, str], ...]
        if source_vendor == VENDOR_CISCO:
            custom_patterns = (
                (r"hostname\s+(.+)", r"sysname \1", SECTION_CONFIG),
                (r"lldp run", "lldp enable", SECTION_CONFIG),
                (r"ip domain name\s+(.+)", r"dns domain \1", SECTION_CONFIG),
                (r"ntp server\s+(\S+)", r"ntp-service unicast-server \1", SECTION_CONFIG),
                (r"logging host\s+(\S+)", r"info-center loghost \1", SECTION_CONFIG),
                (r"no shutdown", "undo shutdown", SECTION_CONFIG),
                (r"spanning-tree portfast", "stp edged-port enable", SECTION_CONFIG),
                (r"ip address\s+(\S+)\s+(\S+)", r"ip address \1 \2", SECTION_CONFIG),
                (r"ip route\s+(\S+)\s+(\S+)\s+(\S+)", r"ip route-static \1 \2 \3", SECTION_CONFIG),
            )
        else:
            custom_patterns = (
                (r"sysname\s+(.+)", r"hostname \1", SECTION_CONFIG),
                (r"lldp enable", "lldp run", SECTION_CONFIG),
                (r"dns domain\s+(.+)", r"ip domain name \1", SECTION_CONFIG),
                (r"ntp-service unicast-server\s+(\S+)", r"ntp server \1", SECTION_CONFIG),
                (r"info-center loghost\s+(\S+)", r"logging host \1", SECTION_CONFIG),
                (r"undo shutdown", "no shutdown", SECTION_CONFIG),
                (r"stp edged-port enable", "spanning-tree portfast", SECTION_CONFIG),
                (r"ip address\s+(\S+)\s+(\S+)", r"ip address \1 \2", SECTION_CONFIG),
                (r"ip route-static\s+(\S+)\s+(\S+)\s+(\S+)", r"ip route \1 \2 \3", SECTION_CONFIG),
            )
        for pattern, replacement, section in custom_patterns:
            match = re.fullmatch(pattern, line, re.IGNORECASE)
            if not match:
                continue
            target = match.expand(replacement)
            if line.lower().startswith(("ip address ", "spanning-tree portfast", "stp edged-port")) and context != "interface":
                break
            _append_command(
                commands,
                output_lines,
                line_number,
                line,
                target,
                section,
                indent=raw_line[:1].isspace(),
            )
            _add_placeholder_issue(issues, line_number, section, line, target)
            matched = True
            break
        if matched:
            continue

        # One Cisco name-server line may become several Huawei commands.
        if source_vendor == VENDOR_CISCO:
            match = re.fullmatch(r"ip name-server\s+(.+)", line, re.IGNORECASE)
            if match:
                servers = match.group(1).split()
                if servers and all(_valid_ip(server) for server in servers):
                    for server in servers:
                        _append_command(
                            commands,
                            output_lines,
                            line_number,
                            line,
                            f"dns server {server}",
                            SECTION_CONFIG,
                        )
                    continue
        else:
            match = re.fullmatch(r"dns server\s+(\S+)", line, re.IGNORECASE)
            if match and _valid_ip(match.group(1)):
                _append_command(
                    commands,
                    output_lines,
                    line_number,
                    line,
                    f"ip name-server {match.group(1)}",
                    SECTION_CONFIG,
                )
                continue

        # Huawei's compact VLAN declaration expands to target VLAN contexts.
        if source_vendor == VENDOR_HUAWEI:
            match = re.fullmatch(r"vlan batch\s+(.+)", line, re.IGNORECASE)
            if match:
                vlan_ids = match.group(1).split()
                if vlan_ids and all(_valid_vlan_id(value) for value in vlan_ids):
                    for vlan_id in vlan_ids:
                        _append_command(
                            commands,
                            output_lines,
                            line_number,
                            line,
                            f"vlan {vlan_id}",
                            SECTION_CONFIG,
                        )
                    context = "global"
                    continue

        # Link aggregation needs context from the whole source configuration.
        if source_vendor == VENDOR_CISCO:
            match = re.fullmatch(
                r"channel-group\s+(\d+)\s+mode\s+(active|passive|on)",
                line,
                re.IGNORECASE,
            )
            if match and context == "interface":
                group = match.group(1)
                _append_command(
                    commands,
                    output_lines,
                    line_number,
                    line,
                    f"eth-trunk {group}",
                    SECTION_CONFIG,
                    indent=True,
                )
                continue
        else:
            match = re.fullmatch(r"eth-trunk\s+(\d+)", line, re.IGNORECASE)
            if match and context == "interface":
                group = match.group(1)
                mode = "active" if group in lacp_groups else "on"
                _append_command(
                    commands,
                    output_lines,
                    line_number,
                    line,
                    f"channel-group {group} mode {mode}",
                    SECTION_CONFIG,
                    indent=True,
                )
                continue
            if lowered == "mode lacp-static" and context == "interface":
                issues.append(
                    ConversionIssue(
                        line_number,
                        DISPOSITION_IGNORED,
                        SECTION_CONFIG,
                        "LACP mode is represented on Cisco member channel-group commands.",
                        line,
                    )
                )
                continue

        for pair in _TEMPLATE_PAIRS[source_vendor]:
            match = pair.regex.fullmatch(line)
            if not match:
                continue
            if pair.source.concept_id in _INTERFACE_CONFIG_CONCEPTS and context != "interface":
                continue
            if pair.source.concept_id == "vlan-name" and context != "vlan":
                continue
            target, interface_mapped = _render_template_target(
                pair, match, source_vendor
            )
            section = _entry_section(pair.target)
            _append_command(
                commands,
                output_lines,
                line_number,
                line,
                target,
                section,
                indent=raw_line[:1].isspace() and section == SECTION_CONFIG,
            )
            _add_placeholder_issue(issues, line_number, section, line, target)
            if "<INTERFACE>" in pair.placeholders:
                if interface_mapped:
                    issues.append(
                        ConversionIssue(
                            line_number,
                            DISPOSITION_REVIEW,
                            section,
                            "Verify the translated interface identifier on the target hardware.",
                            line,
                        )
                    )
                elif "<REVIEW_INTERFACE>" in target:
                    issues.append(
                        ConversionIssue(
                            line_number,
                            DISPOSITION_REVIEW,
                            section,
                            "This interface family cannot be mapped generically.",
                            line,
                            True,
                        )
                    )
            if pair.source.concept_id == "interface-context":
                context, context_name = _context_for_line(line, source_vendor)
                group_match = re.fullmatch(
                    r"(?:Port-channel|Eth-Trunk)(\d+)",
                    context_name or "",
                    re.IGNORECASE,
                )
                if (
                    source_vendor == VENDOR_CISCO
                    and group_match
                    and group_match.group(1) in lacp_groups
                ):
                    _append_command(
                        commands,
                        output_lines,
                        line_number,
                        line,
                        "mode lacp-static",
                        SECTION_CONFIG,
                        indent=True,
                    )
            elif pair.source.concept_id == "vlan-context":
                context = "vlan"
                context_name = None
            matched = True
            break
        if matched:
            continue

        if lowered.startswith(("spanning-tree ", "stp ")):
            message = "STP mode, guard, root, or per-VLAN semantics require topology review"
        elif lowered.startswith(
            ("interface 10", "interface 25", "interface 40", "interface 100")
        ) or lowered.startswith("interface tengigabit"):
            message = "high-speed interface naming depends on target hardware"
        else:
            message = "unsupported source command"
        output_lines.append(_review_comment(target_vendor, line_number, message))
        issues.append(
            ConversionIssue(
                line_number,
                DISPOSITION_UNMAPPED,
                declared_section or SECTION_CONFIG,
                message.capitalize() + ".",
                line,
                True,
            )
        )

    output_text = "\n".join(output_lines).rstrip()
    return ConversionResult(
        source_vendor,
        target_vendor,
        output_text,
        tuple(commands),
        tuple(issues),
    )


def _valid_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def _valid_vlan_id(value: str) -> bool:
    try:
        vlan_id = int(value)
    except ValueError:
        return False
    return 1 <= vlan_id <= 4094


def _recognize_target_line(
    line: str,
    vendor: str,
    context: str,
) -> tuple[Optional[str], str, Optional[str]]:
    lowered = line.casefold()
    if lowered in _MODE_TRANSITIONS[vendor]:
        return None, context, "Configuration-mode transition is managed by the SSH runner."
    for pair in _TEMPLATE_PAIRS[vendor]:
        match = pair.regex.fullmatch(line)
        if not match:
            continue
        concept = pair.source.concept_id
        if concept in _INTERFACE_CONFIG_CONCEPTS and context != "interface":
            continue
        if concept == "vlan-name" and context != "vlan":
            continue
        new_context = context
        if concept == "interface-context":
            new_context = "interface"
        elif concept == "vlan-context":
            new_context = "vlan"
        return _entry_section(pair.source), new_context, None

    config_patterns = {
        VENDOR_CISCO: (
            r"hostname\s+.+",
            r"lldp run",
            r"ip name-server\s+.+",
            r"ip domain name\s+.+",
            r"ntp server\s+\S+",
            r"logging host\s+\S+",
            r"no shutdown",
            r"spanning-tree portfast",
            r"ip address\s+\S+\s+\S+",
            r"ip route\s+\S+\s+\S+\s+\S+",
            r"channel-group\s+\d+\s+mode\s+(?:active|passive|on)",
        ),
        VENDOR_HUAWEI: (
            r"sysname\s+.+",
            r"lldp enable",
            r"dns server\s+\S+",
            r"dns domain\s+.+",
            r"ntp-service unicast-server\s+\S+",
            r"info-center loghost\s+\S+",
            r"undo shutdown",
            r"stp edged-port enable",
            r"ip address\s+\S+\s+\S+",
            r"ip route-static\s+\S+\s+\S+\s+\S+",
            r"eth-trunk\s+\d+",
            r"mode lacp-static",
            r"vlan batch\s+.+",
        ),
    }
    if any(re.fullmatch(pattern, line, re.IGNORECASE) for pattern in config_patterns[vendor]):
        if re.fullmatch(r"(?:no|undo) shutdown|spanning-tree portfast|stp edged-port enable|channel-group .+|eth-trunk .+|mode lacp-static|ip address .+", line, re.IGNORECASE) and context != "interface":
            return None, context, None
        return SECTION_CONFIG, context, None
    return None, context, None


def analyze_target_text(text: str, target_vendor: str) -> ConversionResult:
    """Reclassify edited target text and report unsupported executable lines."""

    if target_vendor not in VENDORS:
        raise ValueError(f"unsupported vendor: {target_vendor}")
    commands: list[ConvertedCommand] = []
    issues: list[ConversionIssue] = []
    declared_section: Optional[str] = None
    context = "global"

    for line_number, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line:
            continue
        section_match = _SECTION_RE.fullmatch(line)
        if section_match:
            declared_section = section_match.group(1).lower()
            context = "global"
            continue
        if line.startswith(("!", "#")):
            if "REVIEW" in line.upper():
                issues.append(
                    ConversionIssue(
                        line_number,
                        DISPOSITION_REVIEW,
                        declared_section or SECTION_CONFIG,
                        "Resolve or remove this generated review marker.",
                        line,
                        True,
                    )
                )
            if line in {"!", "#"}:
                context = "global"
            continue
        if _PLACEHOLDER_RE.search(line):
            issues.append(
                ConversionIssue(
                    line_number,
                    DISPOSITION_REVIEW,
                    declared_section or SECTION_CONFIG,
                    "Replace every review placeholder before executable export or SSH import.",
                    line,
                    True,
                )
            )
            continue
        if _is_sensitive(line):
            issues.append(
                ConversionIssue(
                    line_number,
                    DISPOSITION_REVIEW,
                    declared_section or SECTION_CONFIG,
                    "Sensitive target commands are not accepted by the converter handoff.",
                    "[sensitive command redacted]",
                    True,
                )
            )
            continue

        section, new_context, ignored_reason = _recognize_target_line(
            line, target_vendor, context
        )
        context = new_context
        if ignored_reason:
            issues.append(
                ConversionIssue(
                    line_number,
                    DISPOSITION_IGNORED,
                    declared_section or SECTION_CONFIG,
                    ignored_reason,
                    line,
                )
            )
            continue
        if section is None:
            issues.append(
                ConversionIssue(
                    line_number,
                    DISPOSITION_UNMAPPED,
                    declared_section or SECTION_CONFIG,
                    "Edited line is not in the reviewed target command corpus.",
                    line,
                    True,
                )
            )
            continue
        if declared_section and declared_section != section:
            issues.append(
                ConversionIssue(
                    line_number,
                    DISPOSITION_REVIEW,
                    declared_section,
                    f"Command belongs in [{section}], not [{declared_section}].",
                    line,
                    True,
                )
            )
            continue
        commands.append(
            ConvertedCommand(line_number, line, line, section)
        )

    return ConversionResult(
        target_vendor,
        target_vendor,
        text,
        tuple(commands),
        tuple(issues),
    )


def render_sectioned_commands(result: ConversionResult) -> str:
    """Render a clean SSH command file from a fully reviewed result."""

    if result.blocking_issues:
        raise ValueError(
            "Resolve all blocking review items before exporting SSH commands."
        )
    exec_text = "\n".join(result.exec_commands)
    config_text = "\n".join(result.config_commands)
    return f"[exec]\n{exec_text}\n\n[config]\n{config_text}\n"
