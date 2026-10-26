"""Immutable, release-scoped operational command dictionary.

The corpus intentionally contains only read-only EXEC/user-view commands.  It is
small enough to review as data and strict enough that accidental scope drift is
reported at import time.
"""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import IPv6Address, ip_address
import re
from typing import Iterable, Optional


CISCO_PLATFORM = "Catalyst 9300L"
CISCO_RELEASE = "IOS XE 17.3.x"
CISCO_TARGET = "17.3.3"
HUAWEI_PLATFORM = "CloudEngine S5755-S"
HUAWEI_RELEASE = "V600R024C10"
HUAWEI_TARGET = "V600R024C10SPC500 / YunShan OS 1.24.1.1"

CONCEPT_IDS = (
    "version",
    "inventory",
    "uptime",
    "cpu",
    "memory",
    "environmental-health",
    "power",
    "fans-temperature",
    "interface-summary",
    "interface-details",
    "errors-counters",
    "optics",
    "vlans",
    "mac-table",
    "spanning-tree",
    "link-aggregation",
    "lldp-neighbors",
    "routing-table",
    "arp-neighbor-table",
    "logs",
)

ALLOWED_CATEGORIES = frozenset(
    {"system", "hardware", "interfaces", "layer-2", "layer-3", "diagnostics"}
)
ALLOWED_MODES = frozenset({"exec"})
ALLOWED_RISKS = frozenset({"read-only"})


@dataclass(frozen=True)
class CommandEntry:
    """One immutable, release-scoped command recommendation.

    ``aliases`` and ``keywords`` are tuples so a frozen entry has no mutable
    child collections.  ``syntax`` is directly executable only when it has no
    ``<PLACEHOLDER>``; callers must substitute every placeholder first.
    """

    id: str
    vendor: str
    platform: str
    documented_release: str
    deployed_target: str
    concept_id: str
    title: str
    syntax: str
    category: str
    description: str
    aliases: tuple[str, ...]
    keywords: tuple[str, ...]
    mode: str
    risk: str
    applicability_notes: str


def _command(
    vendor: str,
    concept_id: str,
    title: str,
    syntax: str,
    category: str,
    description: str,
    aliases: tuple[str, ...],
    keywords: tuple[str, ...],
    applicability_notes: str = "Applies to the stated platform and release scope.",
) -> CommandEntry:
    if vendor == "Cisco":
        platform, release, target, slug = (
            CISCO_PLATFORM,
            CISCO_RELEASE,
            CISCO_TARGET,
            "cisco",
        )
    else:
        platform, release, target, slug = (
            HUAWEI_PLATFORM,
            HUAWEI_RELEASE,
            HUAWEI_TARGET,
            "huawei",
        )
    return CommandEntry(
        id=f"{slug}.{concept_id}",
        vendor=vendor,
        platform=platform,
        documented_release=release,
        deployed_target=target,
        concept_id=concept_id,
        title=title,
        syntax=syntax,
        category=category,
        description=description,
        aliases=aliases,
        keywords=keywords,
        mode="exec",
        risk="read-only",
        applicability_notes=applicability_notes,
    )


COMMAND_ENTRIES: tuple[CommandEntry, ...] = (
    _command(
        "Cisco", "version", "Software and hardware version", "show version", "system",
        "Shows the running IOS XE image, hardware model, boot details, and uptime.",
        ("version", "software version", "os version"),
        ("release", "image", "boot", "model"),
    ),
    _command(
        "Huawei", "version", "Software and hardware version", "display version", "system",
        "Shows the running VRP/YunShan software version, hardware model, and uptime.",
        ("version", "software version", "os version"),
        ("release", "image", "boot", "model"),
    ),
    _command(
        "Cisco", "inventory", "Hardware inventory", "show inventory", "system",
        "Lists installed hardware components with product identifiers and serial data.",
        ("inventory", "hardware inventory", "components"),
        ("pid", "serial", "module", "chassis"),
    ),
    _command(
        "Huawei", "inventory", "Hardware inventory", "display device elabel", "system",
        "Displays electronic labels for the chassis and installed hardware components.",
        ("inventory", "hardware inventory", "components"),
        ("elabel", "serial", "module", "chassis"),
        "Electronic-label output can be lengthy on a device with many components.",
    ),
    _command(
        "Cisco", "uptime", "System uptime", "show version | include uptime", "system",
        "Filters version output to the line reporting how long the switch has been running.",
        ("uptime", "system uptime", "running time"),
        ("reload", "reboot", "availability"),
    ),
    _command(
        "Huawei", "uptime", "System uptime", "display version | include uptime", "system",
        "Filters version output to the line reporting how long the switch has been running.",
        ("uptime", "system uptime", "running time"),
        ("reload", "reboot", "availability"),
    ),
    _command(
        "Cisco", "cpu", "CPU utilization", "show processes cpu sorted", "system",
        "Shows aggregate CPU utilization and processes ordered by processor use.",
        ("cpu", "cpu utilization", "processor usage"),
        ("processes", "load", "performance"),
    ),
    _command(
        "Huawei", "cpu", "CPU utilization", "display cpu", "system",
        "Shows current and recent CPU utilization for the device.",
        ("cpu", "cpu utilization", "processor usage"),
        ("processes", "load", "performance"),
    ),
    _command(
        "Cisco", "memory", "Memory utilization", "show processes memory sorted", "system",
        "Shows processor memory usage and processes ordered by memory consumption.",
        ("memory", "memory utilization", "ram usage"),
        ("processes", "free memory", "used memory", "performance"),
    ),
    _command(
        "Huawei", "memory", "Memory utilization", "display memory", "system",
        "Shows total, used, and available device memory statistics.",
        ("memory", "memory utilization", "ram usage"),
        ("free memory", "used memory", "performance"),
    ),
    _command(
        "Cisco", "environmental-health", "Environmental health", "show environment all", "hardware",
        "Shows the combined fan, temperature, and internal power-supply status.",
        ("environment", "environmental health", "hardware health"),
        ("sensors", "temperature", "fan", "power supply"),
    ),
    _command(
        "Huawei", "environmental-health", "Environmental health", "display health", "hardware",
        "Shows combined voltage, temperature, fan, power, CPU, memory, and storage health.",
        ("environment", "environmental health", "hardware health"),
        ("sensors", "temperature", "fan", "power supply"),
    ),
    _command(
        "Cisco", "power", "Power-supply status", "show environment power", "hardware",
        "Shows installed internal power supplies and their operational state.",
        ("power", "power supply", "psu status"),
        ("watts", "internal power", "redundancy"),
    ),
    _command(
        "Huawei", "power", "Power-supply status", "display device power", "hardware",
        "Shows installed power modules and their supply state and electrical readings.",
        ("power", "power supply", "psu status"),
        ("watts", "voltage", "current", "redundancy"),
    ),
    _command(
        "Cisco", "fans-temperature", "Fan status", "show environment fan", "hardware",
        "Shows the operational status of the switch fans.",
        ("fan status", "cooling fans", "fan health"),
        ("fan", "cooling", "airflow"),
        "This entry covers fans within the paired cooling concept; temperature is included in environmental health.",
    ),
    _command(
        "Huawei", "fans-temperature", "Fan status", "display fan", "hardware",
        "Shows fan presence, registration, speed, mode, airflow, and operational status.",
        ("fan status", "cooling fans", "fan health"),
        ("fan", "cooling", "airflow", "speed"),
        "This entry covers fans within the paired cooling concept; temperature is included in environmental health.",
    ),
    _command(
        "Cisco", "interface-summary", "Interface status summary", "show interfaces status", "interfaces",
        "Shows a compact physical-port status, VLAN, duplex, speed, and media summary.",
        ("interface summary", "port summary", "interface status"),
        ("link", "speed", "duplex", "media"),
    ),
    _command(
        "Huawei", "interface-summary", "Interface status summary", "display interface brief", "interfaces",
        "Shows compact interface physical/protocol state, utilization, and error counts.",
        ("interface summary", "port summary", "interface status"),
        ("link", "physical", "protocol", "utilization"),
    ),
    _command(
        "Cisco", "interface-details", "Interface details", "show interfaces <INTERFACE>", "interfaces",
        "Shows detailed operational state, traffic statistics, and errors for one interface.",
        ("interface details", "port details", "interface statistics"),
        ("link", "traffic", "packets", "errors"),
        "Replace <INTERFACE> with a valid interface identifier from the target switch.",
    ),
    _command(
        "Huawei", "interface-details", "Interface details", "display interface <INTERFACE>", "interfaces",
        "Shows detailed operational state, traffic statistics, and errors for one interface.",
        ("interface details", "port details", "interface statistics"),
        ("link", "traffic", "packets", "errors"),
        "Replace <INTERFACE> with a valid interface identifier from the target switch.",
    ),
    _command(
        "Cisco", "errors-counters", "Interface error counters", "show interfaces counters errors", "interfaces",
        "Shows receive and transmit error counters across switch interfaces.",
        ("errors counters", "interface errors", "port errors"),
        ("crc", "collisions", "runts", "giants", "discards"),
    ),
    _command(
        "Huawei", "errors-counters", "Interface error counters", "display counters error", "interfaces",
        "Shows inbound and outbound error packet counters across interfaces.",
        ("errors counters", "interface errors", "port errors"),
        ("crc", "collisions", "runts", "giants", "packets"),
    ),
    _command(
        "Cisco", "optics", "Optical transceiver diagnostics", "show interfaces <INTERFACE> transceiver detail", "interfaces",
        "Shows detailed DOM readings and alarm thresholds for an optical interface.",
        ("optics", "transceiver diagnostics", "dom readings"),
        ("sfp", "optical power", "wavelength", "rx", "tx"),
        "Replace <INTERFACE> with an optical interface; readings depend on installed-module DOM support.",
    ),
    _command(
        "Huawei", "optics", "Optical transceiver diagnostics", "display interface <INTERFACE> transceiver verbose", "interfaces",
        "Shows detailed optical-module identity, diagnostics, readings, and alarms.",
        ("optics", "transceiver diagnostics", "dom readings"),
        ("sfp", "optical power", "wavelength", "rx", "tx"),
        "Replace <INTERFACE> with an optical interface; readings depend on installed-module DOM support.",
    ),
    _command(
        "Cisco", "vlans", "VLAN summary", "show vlan brief", "layer-2",
        "Shows configured VLAN identifiers, names, status, and assigned access ports.",
        ("vlans", "vlan summary", "virtual lans"),
        ("layer 2", "broadcast domain", "access ports"),
    ),
    _command(
        "Huawei", "vlans", "VLAN summary", "display vlan", "layer-2",
        "Shows configured VLAN identifiers, state, and associated interfaces.",
        ("vlans", "vlan summary", "virtual lans"),
        ("layer 2", "broadcast domain", "access ports"),
    ),
    _command(
        "Cisco", "mac-table", "MAC address table", "show mac address-table", "layer-2",
        "Shows learned and configured MAC addresses with VLAN and forwarding port.",
        ("mac table", "mac address table", "forwarding database"),
        ("fdb", "learned addresses", "layer 2"),
    ),
    _command(
        "Huawei", "mac-table", "MAC address table", "display mac-address", "layer-2",
        "Shows learned and configured MAC addresses with VLAN and forwarding interface.",
        ("mac table", "mac address table", "forwarding database"),
        ("fdb", "learned addresses", "layer 2"),
    ),
    _command(
        "Cisco", "spanning-tree", "Spanning-tree summary", "show spanning-tree summary", "layer-2",
        "Shows spanning-tree mode, root information, and aggregate port-state counts.",
        ("spanning tree", "stp summary", "loop prevention"),
        ("root bridge", "blocked ports", "topology", "layer 2"),
    ),
    _command(
        "Huawei", "spanning-tree", "Spanning-tree summary", "display stp brief", "layer-2",
        "Shows concise spanning-tree roles, states, and protection status by interface.",
        ("spanning tree", "stp summary", "loop prevention"),
        ("root bridge", "blocked ports", "topology", "layer 2"),
    ),
    _command(
        "Cisco", "link-aggregation", "Link aggregation summary", "show etherchannel summary", "layer-2",
        "Shows EtherChannel groups, protocols, port-channels, and member-port states.",
        ("link aggregation", "lag summary", "port channel"),
        ("etherchannel", "lacp", "bundled ports"),
    ),
    _command(
        "Huawei", "link-aggregation", "Link aggregation summary", "display eth-trunk", "layer-2",
        "Shows Eth-Trunk groups, operating mode, and member-interface states.",
        ("link aggregation", "lag summary", "port channel"),
        ("eth trunk", "lacp", "bundled ports"),
    ),
    _command(
        "Cisco", "lldp-neighbors", "LLDP neighbors", "show lldp neighbors detail", "layer-2",
        "Shows detailed LLDP-discovered neighbor identity, port, and capability data.",
        ("lldp neighbors", "neighbor discovery", "connected devices"),
        ("topology", "remote port", "chassis id", "discovery"),
        "Returns useful data only where LLDP is enabled and neighbors advertise it.",
    ),
    _command(
        "Huawei", "lldp-neighbors", "LLDP neighbors", "display lldp neighbor brief", "layer-2",
        "Shows a concise list of LLDP-discovered neighbors and their interfaces.",
        ("lldp neighbors", "neighbor discovery", "connected devices"),
        ("topology", "remote port", "chassis id", "discovery"),
        "Returns useful data only where LLDP is enabled and neighbors advertise it.",
    ),
    _command(
        "Cisco", "routing-table", "IPv4 routing table", "show ip route", "layer-3",
        "Shows the IPv4 routing table, route sources, metrics, and next hops.",
        ("routing table", "ip routes", "routes"),
        ("ipv4", "next hop", "prefix", "gateway"),
    ),
    _command(
        "Huawei", "routing-table", "IPv4 routing table", "display ip routing-table", "layer-3",
        "Shows the IPv4 routing table, route protocols, preferences, and next hops.",
        ("routing table", "ip routes", "routes"),
        ("ipv4", "next hop", "prefix", "gateway"),
    ),
    _command(
        "Cisco", "arp-neighbor-table", "ARP neighbor table", "show ip arp", "layer-3",
        "Shows IPv4 neighbor mappings between protocol and link-layer addresses.",
        ("arp neighbor table", "arp table", "ip neighbors"),
        ("ipv4", "address resolution", "mac mapping", "adjacency"),
    ),
    _command(
        "Huawei", "arp-neighbor-table", "ARP neighbor table", "display arp", "layer-3",
        "Shows IPv4 neighbor mappings between protocol and link-layer addresses.",
        ("arp neighbor table", "arp table", "ip neighbors"),
        ("ipv4", "address resolution", "mac mapping", "adjacency"),
    ),
    _command(
        "Cisco", "logs", "Buffered system logs", "show logging", "diagnostics",
        "Shows logging configuration and messages currently retained by the switch.",
        ("logs", "system logs", "log buffer"),
        ("events", "messages", "syslog", "diagnostics"),
    ),
    _command(
        "Huawei", "logs", "Buffered system logs", "display logbuffer", "diagnostics",
        "Shows messages currently retained in the device log buffer.",
        ("logs", "system logs", "log buffer"),
        ("events", "messages", "syslog", "diagnostics"),
    ),
)


_ID_RE = re.compile(r"^(cisco|huawei)\.[a-z0-9]+(?:-[a-z0-9]+)*$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_PLACEHOLDER_RE = re.compile(r"<[A-Z][A-Z0-9_]*>")
_SENSITIVE_RE = re.compile(
    r"\b(?:password|passphrase|secret|credential|community\s+string|api[- ]?token|private\s+key)\b",
    re.IGNORECASE,
)
_IPV4_RE = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
_MAC_RE = re.compile(
    r"(?<![0-9a-f])(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}(?![0-9a-f])"
    r"|(?<![0-9a-f])(?:[0-9a-f]{4}\.){2}[0-9a-f]{4}(?![0-9a-f])",
    re.IGNORECASE,
)
_EMAIL_RE = re.compile(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b")
_IPV6_CANDIDATE_RE = re.compile(r"[0-9a-f]*:[0-9a-f:.%]+", re.IGNORECASE)
_MUTATING_SYNTAX_RE = re.compile(
    r"\b(?:append|redirect|tee|copy|delete|erase|format|mkdir|rmdir|rename|move|"
    r"write|save|reset|clear|reload|reboot|shutdown|install|upgrade|download|upload)\b",
    re.IGNORECASE,
)
_ALLOWED_PIPELINES = frozenset(
    {"show version | include uptime", "display version | include uptime"}
)
_ALLOWED_PLACEHOLDERS = frozenset({"<INTERFACE>"})


def _entry_strings(entry: CommandEntry) -> tuple[str, ...]:
    return (
        entry.id,
        entry.vendor,
        entry.platform,
        entry.documented_release,
        entry.deployed_target,
        entry.concept_id,
        entry.title,
        entry.syntax,
        entry.category,
        entry.description,
        *entry.aliases,
        *entry.keywords,
        entry.mode,
        entry.risk,
        entry.applicability_notes,
    )


def _normalize(value: str) -> str:
    """Case-fold text and collapse every punctuation/whitespace run to one space."""
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def _contains_ipv6(value: str) -> bool:
    """Return whether text contains a syntactically valid literal IPv6 address."""
    for match in _IPV6_CANDIDATE_RE.finditer(value):
        candidate = match.group(0).split("%", 1)[0].strip(".,;()[]{}")
        try:
            if isinstance(ip_address(candidate), IPv6Address):
                return True
        except ValueError:
            pass
    return False


def validate_dictionary(
    entries: Optional[Iterable[CommandEntry]] = None,
) -> tuple[CommandEntry, ...]:
    """Validate and return an immutable command corpus.

    With no argument, validate :data:`COMMAND_ENTRIES`.  Any iterable is
    materialized as a tuple.  Success returns that tuple (the original object
    when a tuple was supplied); failure raises ``ValueError`` containing all
    detected violations.  The checks enforce the exact 40-entry, 20-pair scope,
    fixed platform/release metadata, identifiers, required fields, immutable
    tuple collections, read-only command shape, safe placeholders, control-char
    exclusion, and absence of credential/address-like sample data.
    """
    corpus = COMMAND_ENTRIES if entries is None else (
        entries if isinstance(entries, tuple) else tuple(entries)
    )
    errors: list[str] = []
    if len(corpus) != 40:
        errors.append(f"expected 40 entries, found {len(corpus)}")

    ids: set[str] = set()
    pair_counts: dict[tuple[str, str], int] = {}
    vendor_counts = {"Cisco": 0, "Huawei": 0}
    expected_scope = {
        "Cisco": (CISCO_PLATFORM, CISCO_RELEASE, CISCO_TARGET, "show "),
        "Huawei": (HUAWEI_PLATFORM, HUAWEI_RELEASE, HUAWEI_TARGET, "display "),
    }

    for index, entry in enumerate(corpus):
        if not isinstance(entry, CommandEntry):
            errors.append(f"entry[{index}] is not a CommandEntry")
            continue
        label = entry.id if isinstance(entry.id, str) and entry.id else f"entry[{index}]"
        scalar_values = (
            entry.id,
            entry.vendor,
            entry.platform,
            entry.documented_release,
            entry.deployed_target,
            entry.concept_id,
            entry.title,
            entry.syntax,
            entry.category,
            entry.description,
            entry.mode,
            entry.risk,
            entry.applicability_notes,
        )
        aliases_valid = (
            isinstance(entry.aliases, tuple)
            and bool(entry.aliases)
            and all(isinstance(value, str) and value.strip() for value in entry.aliases)
        )
        keywords_valid = (
            isinstance(entry.keywords, tuple)
            and bool(entry.keywords)
            and all(isinstance(value, str) and value.strip() for value in entry.keywords)
        )
        if not aliases_valid:
            errors.append(f"aliases must be a non-empty tuple of strings for {label}")
        if not keywords_valid:
            errors.append(f"keywords must be a non-empty tuple of strings for {label}")
        if any(not isinstance(value, str) or not value.strip() for value in scalar_values):
            errors.append(f"all scalar fields must be non-empty strings for {label}")
        if not aliases_valid or not keywords_valid or any(
            not isinstance(value, str) or not value.strip() for value in scalar_values
        ):
            continue
        if entry.id in ids:
            errors.append(f"duplicate id: {entry.id}")
        ids.add(entry.id)
        if not _ID_RE.fullmatch(entry.id):
            errors.append(f"invalid id: {entry.id!r}")
        if entry.id != f"{entry.vendor.casefold()}.{entry.concept_id}":
            errors.append(f"id does not match vendor/concept for {label}")
        if entry.vendor not in expected_scope:
            errors.append(f"unsupported vendor for {label}: {entry.vendor!r}")
        else:
            platform, release, target, command_prefix = expected_scope[entry.vendor]
            vendor_counts[entry.vendor] += 1
            if (entry.platform, entry.documented_release, entry.deployed_target) != (
                platform,
                release,
                target,
            ):
                errors.append(f"scope metadata mismatch for {label}")
            if not entry.syntax.casefold().startswith(command_prefix):
                errors.append(f"non-read-only command prefix for {label}")
        if entry.concept_id not in CONCEPT_IDS:
            errors.append(f"unsupported concept for {label}: {entry.concept_id!r}")
        pair = (entry.concept_id, entry.vendor)
        pair_counts[pair] = pair_counts.get(pair, 0) + 1
        if entry.category not in ALLOWED_CATEGORIES:
            errors.append(f"unsupported category for {label}: {entry.category!r}")
        if entry.mode not in ALLOWED_MODES:
            errors.append(f"unsupported mode for {label}: {entry.mode!r}")
        if entry.risk not in ALLOWED_RISKS:
            errors.append(f"unsupported risk for {label}: {entry.risk!r}")
        strings = _entry_strings(entry)
        if any(_CONTROL_RE.search(value) for value in strings):
            errors.append(f"control character found in {label}")
        placeholders = set(_PLACEHOLDER_RE.findall(entry.syntax))
        syntax_without_placeholders = _PLACEHOLDER_RE.sub("", entry.syntax)
        if "<" in syntax_without_placeholders or ">" in syntax_without_placeholders:
            errors.append(f"malformed placeholder in {label}")
        if not placeholders <= _ALLOWED_PLACEHOLDERS:
            errors.append(f"unsupported placeholder in {label}")
        if placeholders and (
            not placeholders <= set(_PLACEHOLDER_RE.findall(entry.applicability_notes))
            or "replace" not in entry.applicability_notes.casefold()
        ):
            errors.append(f"placeholder substitution is not documented for {label}")
        if any(token in entry.syntax for token in (";", "&&", "||", "`")):
            errors.append(f"command chaining is not allowed for {label}")
        normalized_pipeline = " ".join(entry.syntax.casefold().split())
        if "|" in entry.syntax and normalized_pipeline not in _ALLOWED_PIPELINES:
            errors.append(f"unapproved pipeline modifier for {label}")
        syntax_without_placeholders = _PLACEHOLDER_RE.sub("", entry.syntax)
        if ">" in syntax_without_placeholders:
            errors.append(f"output redirection is not allowed for {label}")
        if _MUTATING_SYNTAX_RE.search(entry.syntax):
            errors.append(f"mutating or file-operation token found in {label}")
        sample_fields = (
            entry.title,
            entry.syntax,
            entry.description,
            *entry.aliases,
            *entry.keywords,
            entry.applicability_notes,
        )
        if any(_SENSITIVE_RE.search(value) for value in sample_fields):
            errors.append(f"sensitive sample term found in {label}")
        if any(
            pattern.search(value)
            for value in sample_fields
            for pattern in (_IPV4_RE, _MAC_RE, _EMAIL_RE)
        ) or any(_contains_ipv6(value) for value in sample_fields):
            errors.append(f"literal address-like sample found in {label}")

    if vendor_counts != {"Cisco": 20, "Huawei": 20}:
        errors.append(f"expected 20 entries per vendor, found {vendor_counts}")
    for concept_id in CONCEPT_IDS:
        for vendor in ("Cisco", "Huawei"):
            count = pair_counts.get((concept_id, vendor), 0)
            if count != 1:
                errors.append(f"expected one {vendor} entry for {concept_id}, found {count}")
        paired = [entry for entry in corpus if isinstance(entry, CommandEntry) and entry.concept_id == concept_id]
        if len(paired) == 2 and all(
            isinstance(entry.aliases, tuple)
            and all(isinstance(alias, str) for alias in entry.aliases)
            for entry in paired
        ):
            shared_aliases = set(map(_normalize, paired[0].aliases)) & set(
                map(_normalize, paired[1].aliases)
            )
            if not shared_aliases:
                errors.append(f"paired entries have no shared alias for {concept_id}")

    if errors:
        raise ValueError("Invalid command dictionary: " + "; ".join(errors))
    return corpus


def _searchable_text(entry: CommandEntry) -> str:
    return _normalize(" ".join(_entry_strings(entry)))


def _matches_partial_tokens(query_tokens: list[str], text: str) -> bool:
    """Return whether each query token is contained in a token from ``text``."""
    searchable_tokens = text.split()
    return all(
        any(query_token in searchable_token for searchable_token in searchable_tokens)
        for query_token in query_tokens
    )


def search_commands(
    query: str = "",
    platform: Optional[str] = None,
    category: Optional[str] = None,
) -> tuple[CommandEntry, ...]:
    """Return ranked immutable matches from :data:`COMMAND_ENTRIES`.

    Search is case-insensitive and treats punctuation and whitespace alike.
    Every normalized query token must occur as a case-insensitive partial token
    somewhere in an entry's searchable fields.  Ranking is: exact syntax,
    syntax prefix, title/alias match, description/keyword match, then other
    metadata; original corpus order is the stable tie-breaker.  A blank query
    returns all filtered entries in corpus order.  ``platform`` and ``category``
    are independent optional exact filters after the same normalization;
    ``platform`` may be either the platform label or vendor name.  Results are
    always a tuple.  Non-string arguments raise ``TypeError`` and unknown
    filters simply produce an empty tuple.
    """
    if not isinstance(query, str):
        raise TypeError("query must be a string")
    if platform is not None and not isinstance(platform, str):
        raise TypeError("platform must be a string or None")
    if category is not None and not isinstance(category, str):
        raise TypeError("category must be a string or None")

    normalized_query = _normalize(query)
    query_tokens = normalized_query.split()
    normalized_platform = _normalize(platform or "")
    normalized_category = _normalize(category or "")
    if platform is not None and platform.strip() and not normalized_platform:
        return ()
    if category is not None and category.strip() and not normalized_category:
        return ()
    ranked: list[tuple[int, int, CommandEntry]] = []

    for order, entry in enumerate(COMMAND_ENTRIES):
        if normalized_platform and normalized_platform not in {
            _normalize(entry.platform),
            _normalize(entry.vendor),
        }:
            continue
        if normalized_category and normalized_category != _normalize(entry.category):
            continue
        all_text = _searchable_text(entry)
        if query_tokens and not _matches_partial_tokens(query_tokens, all_text):
            continue

        syntax = _normalize(entry.syntax)
        title_alias = _normalize(" ".join((entry.title, *entry.aliases)))
        description_keyword = _normalize(" ".join((entry.description, *entry.keywords)))
        if normalized_query and normalized_query == syntax:
            rank = 0
        elif normalized_query and syntax.startswith(normalized_query):
            rank = 1
        elif query_tokens and _matches_partial_tokens(query_tokens, title_alias):
            rank = 2
        elif query_tokens and _matches_partial_tokens(
            query_tokens, description_keyword
        ):
            rank = 3
        else:
            rank = 4
        ranked.append((rank, order, entry))

    ranked.sort(key=lambda item: (item[0], item[1]))
    return tuple(item[2] for item in ranked)


# Fail fast if an edit weakens the reviewed corpus invariants.
validate_dictionary()


__all__ = (
    "CommandEntry",
    "COMMAND_ENTRIES",
    "CONCEPT_IDS",
    "search_commands",
    "validate_dictionary",
)
