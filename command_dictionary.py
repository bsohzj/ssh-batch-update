"""Immutable, release-scoped operational command dictionary.

The corpus contains reviewed read-only inspection commands and a small set of
configuration templates.  It is small enough to review as data and strict enough
that accidental scope drift is reported at import time.
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
HUAWEI_REFERENCE_RELEASE = "Generic VRP (V600R024C10 unverified)"

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
    "running-configuration",
    "interface-context",
    "interface-description",
    "port-link-type",
    "vlan-context",
    "vlan-name",
    "access-vlan",
    "trunk-vlan-list",
    "interface-admin-state",
    "ip-interface-state",
    "ping",
    "traceroute",
    "vlan-details",
    "mac-lookup",
    "arp-lookup",
    "stp-vlan",
    "lldp-interface",
    "trunk-native-vlan",
    "interface-mtu",
    "interface-description-summary",
    "trunk-membership",
    "interface-counters",
    "interface-current-configuration",
    "route-lookup",
    "filtered-logs",
    "clock",
    "ntp-status",
    "save-configuration",
)

_REFERENCE_CONCEPT_IDS = (
    "system-name",
    "lldp-enable",
    "dns-domain",
    "dns-server",
    "ntp-server",
    "syslog-server",
    "stp-edge-port",
    "interface-ipv4-address",
    "static-ipv4-route",
    "lag-member",
    "lacp-static-mode",
    "vlan-batch",
)
CONCEPT_IDS += _REFERENCE_CONCEPT_IDS

_HUAWEI_ONLY_CONCEPTS = frozenset({"lacp-static-mode", "vlan-batch"})

ALLOWED_CATEGORIES = frozenset(
    {"system", "hardware", "interfaces", "layer-2", "layer-3", "diagnostics"}
)
ALLOWED_MODES = frozenset(
    {
        "exec",
        "global configuration",
        "system view",
        "interface configuration",
        "interface view",
        "vlan configuration",
        "vlan view",
        "privileged exec",
        "user view",
    }
)
ALLOWED_RISKS = frozenset(
    {"read-only", "changes-state", "service-impacting", "active-diagnostic", "interactive"}
)


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
    rollback_syntax: str = ""


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
    *,
    mode: str = "exec",
    risk: str = "read-only",
    rollback_syntax: str = "",
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
        if concept_id in _REFERENCE_CONCEPT_IDS:
            release = HUAWEI_REFERENCE_RELEASE
            applicability_notes += (
                " Generic VRP converter reference; exact command availability on the stated"
                " deployed target is unverified. Check its command reference and feature support before use."
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
        mode=mode,
        risk=risk,
        applicability_notes=applicability_notes,
        rollback_syntax=rollback_syntax,
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
    _command(
        "Cisco", "running-configuration", "Running configuration", "show running-config", "system",
        "Shows the active running configuration on the switch.",
        ("running configuration", "active configuration", "show run"),
        ("running config", "current settings", "configuration review"),
        "Configuration output can contain protected operational data; handle it according to local policy.",
    ),
    _command(
        "Huawei", "running-configuration", "Current configuration", "display current-configuration", "system",
        "Shows the active current configuration on the switch.",
        ("current configuration", "active configuration", "display current"),
        ("current config", "running settings", "configuration review"),
        "Configuration output can contain protected operational data; handle it according to local policy.",
    ),
    _command(
        "Cisco", "interface-context", "Enter interface configuration", "interface <INTERFACE>", "interfaces",
        "Enters configuration context for the selected interface.",
        ("interface", "interface context", "interface configuration"),
        ("configure port", "select interface", "global configuration"),
        "Replace <INTERFACE> with a valid identifier. Run from global configuration mode. Physical ports, Vlan<VLAN_ID> SVIs and Port-channel<GROUP_ID> bundles use different interface types; substitute the complete identifier, verify support, and create the required VLAN or bundle deliberately.",
        mode="global configuration",
        risk="changes-state",
    ),
    _command(
        "Huawei", "interface-context", "Enter interface view", "interface <INTERFACE>", "interfaces",
        "Enters the view for the selected interface.",
        ("interface", "interface context", "interface configuration"),
        ("configure port", "select interface", "system view"),
        "Replace <INTERFACE> with a valid identifier. Run from system view. Physical ports, Vlanif<VLAN_ID> interfaces and Eth-Trunk<GROUP_ID> bundles use different interface types; substitute the complete identifier, verify support, and create the required VLAN or bundle deliberately.",
        mode="system view",
        risk="changes-state",
    ),
    _command(
        "Cisco", "interface-description", "Interface description", "description <DESCRIPTION>", "interfaces",
        "Sets the description text for the selected interface.",
        ("interface description", "description", "desc"),
        ("port label", "interface label", "documentation"),
        "Replace <DESCRIPTION> with the intended text. Run inside interface configuration context.",
        mode="interface configuration",
        risk="changes-state",
        rollback_syntax="no description",
    ),
    _command(
        "Huawei", "interface-description", "Interface description", "description <DESCRIPTION>", "interfaces",
        "Sets the description text for the selected interface.",
        ("interface description", "description", "desc"),
        ("port label", "interface label", "documentation"),
        "Replace <DESCRIPTION> with the intended text. Run inside interface view.",
        mode="interface view",
        risk="changes-state",
        rollback_syntax="undo description",
    ),
    _command(
        "Cisco", "port-link-type", "Layer 2 port mode", "switchport mode <MODE>", "layer-2",
        "Sets the Layer 2 switchport mode for the selected interface.",
        ("port link type", "switchport mode", "link mode"),
        ("access", "trunk", "layer 2", "port mode"),
        "Replace <MODE> with access or trunk. Run inside interface configuration context.",
        mode="interface configuration",
        risk="changes-state",
        rollback_syntax="no switchport mode",
    ),
    _command(
        "Huawei", "port-link-type", "Layer 2 link type", "port link-type <MODE>", "layer-2",
        "Sets the Layer 2 link type for the selected interface.",
        ("port link type", "port link-type", "link mode"),
        ("access", "trunk", "hybrid", "layer 2", "port mode"),
        "Replace <MODE> with access, trunk, or hybrid. Run inside interface view.",
        mode="interface view",
        risk="changes-state",
        rollback_syntax="undo port link-type",
    ),
    _command(
        "Cisco", "vlan-context", "Create or select VLAN", "vlan <VLAN_ID>", "layer-2",
        "Creates the VLAN when absent and enters its VLAN configuration context.",
        ("create vlan", "select vlan", "vlan configuration"),
        ("vlan id", "broadcast domain", "layer 2"),
        "Replace <VLAN_ID> with an approved VLAN. Run from global configuration mode. No automatic rollback is provided because deleting a pre-existing VLAN is unsafe.",
        mode="global configuration",
        risk="changes-state",
    ),
    _command(
        "Huawei", "vlan-context", "Create or select VLAN", "vlan <VLAN_ID>", "layer-2",
        "Creates the VLAN when absent and enters its VLAN view.",
        ("create vlan", "select vlan", "vlan configuration"),
        ("vlan id", "broadcast domain", "layer 2"),
        "Replace <VLAN_ID> with an approved VLAN. Run from system view. No automatic rollback is provided because deleting a pre-existing VLAN is unsafe.",
        mode="system view",
        risk="changes-state",
    ),
    _command(
        "Cisco", "vlan-name", "VLAN name", "name <VLAN_NAME>", "layer-2",
        "Sets the name of the selected VLAN.",
        ("vlan name", "name vlan", "label vlan"),
        ("vlan label", "vlan configuration", "identifier"),
        "Replace <VLAN_NAME> with the intended name. Run inside VLAN configuration context.",
        mode="vlan configuration",
        risk="changes-state",
        rollback_syntax="no name",
    ),
    _command(
        "Huawei", "vlan-name", "VLAN name", "name <VLAN_NAME>", "layer-2",
        "Sets the name of the selected VLAN.",
        ("vlan name", "name vlan", "label vlan"),
        ("vlan label", "vlan view", "identifier"),
        "Replace <VLAN_NAME> with the intended name. Run inside VLAN view; verify support on the deployed release.",
        mode="vlan view",
        risk="changes-state",
        rollback_syntax="undo name",
    ),
    _command(
        "Cisco", "access-vlan", "Access VLAN membership", "switchport access vlan <VLAN_ID>", "layer-2",
        "Assigns the selected access interface to a VLAN.",
        ("access vlan", "untagged vlan", "port vlan membership"),
        ("switchport", "access port", "vlan id"),
        "Replace <VLAN_ID> with an existing VLAN. Run inside interface configuration context after setting access mode; changing a live port can interrupt service.",
        mode="interface configuration",
        risk="service-impacting",
        rollback_syntax="no switchport access vlan",
    ),
    _command(
        "Huawei", "access-vlan", "Access VLAN membership", "port default vlan <VLAN_ID>", "layer-2",
        "Assigns the selected access interface to a VLAN.",
        ("access vlan", "untagged vlan", "port vlan membership"),
        ("default vlan", "access port", "vlan id"),
        "Replace <VLAN_ID> with an existing VLAN. Run inside interface view after setting access link type; changing a live port can interrupt service.",
        mode="interface view",
        risk="service-impacting",
        rollback_syntax="undo port default vlan",
    ),
    _command(
        "Cisco", "trunk-vlan-list", "Allowed trunk VLANs", "switchport trunk allowed vlan <VLAN_LIST>", "layer-2",
        "Sets the VLAN list allowed on the selected trunk interface.",
        ("trunk vlan list", "allowed vlans", "trunk membership"),
        ("switchport trunk", "tagged vlans", "vlan range"),
        "Replace <VLAN_LIST> with the complete approved list. Run inside interface configuration context after setting trunk mode. This form replaces the current list; no generic rollback is safe.",
        mode="interface configuration",
        risk="service-impacting",
    ),
    _command(
        "Huawei", "trunk-vlan-list", "Allowed trunk VLANs", "port trunk allow-pass vlan <VLAN_LIST>", "layer-2",
        "Allows the specified VLAN list on the selected trunk interface.",
        ("trunk vlan list", "allowed vlans", "trunk membership"),
        ("allow pass", "tagged vlans", "vlan range"),
        "Replace <VLAN_LIST> with the approved list. Run inside interface view after setting trunk link type. Addition/removal semantics differ from Cisco, so no generic rollback is provided.",
        mode="interface view",
        risk="service-impacting",
    ),
    _command(
        "Cisco", "interface-admin-state", "Administratively disable interface", "shutdown", "interfaces",
        "Administratively disables the selected interface.",
        ("interface admin state", "disable interface", "shutdown port"),
        ("administratively down", "port state", "service interruption"),
        "Run inside interface configuration context. This immediately interrupts service; use the rollback only after checking cabling and loop risk.",
        mode="interface configuration",
        risk="service-impacting",
        rollback_syntax="no shutdown",
    ),
    _command(
        "Huawei", "interface-admin-state", "Administratively disable interface", "shutdown", "interfaces",
        "Administratively disables the selected interface.",
        ("interface admin state", "disable interface", "shutdown port"),
        ("administratively down", "port state", "service interruption"),
        "Run inside interface view. This immediately interrupts service; use the rollback only after checking cabling and loop risk.",
        mode="interface view",
        risk="service-impacting",
        rollback_syntax="undo shutdown",
    ),
    _command(
        "Cisco", "ip-interface-state", "IP interface state", "show ip interface brief", "layer-3",
        "Shows interface IP addresses and concise protocol and administrative state.",
        ("ip interface state", "ip interface brief", "layer 3 interfaces"),
        ("protocol status", "administrative status", "ip address"),
        mode="privileged exec",
    ),
    _command(
        "Huawei", "ip-interface-state", "IP interface state", "display ip interface brief", "layer-3",
        "Shows interface IP addresses and concise physical and protocol state.",
        ("ip interface state", "ip interface brief", "layer 3 interfaces"),
        ("protocol status", "physical status", "ip address"),
        mode="user view",
    ),
    _command(
        "Cisco", "ping", "Reachability probe", "ping <IP_ADDRESS>", "diagnostics",
        "Sends active ICMP probes to test destination reachability.",
        ("ping", "reachability test", "icmp probe"),
        ("network test", "latency", "packet loss"),
        "Replace <IP_ADDRESS> with an approved destination. This active diagnostic generates network traffic.",
        mode="exec",
        risk="active-diagnostic",
    ),
    _command(
        "Huawei", "ping", "Reachability probe", "ping <IP_ADDRESS>", "diagnostics",
        "Sends active ICMP probes to test destination reachability.",
        ("ping", "reachability test", "icmp probe"),
        ("network test", "latency", "packet loss"),
        "Replace <IP_ADDRESS> with an approved destination. This active diagnostic generates network traffic.",
        mode="user view",
        risk="active-diagnostic",
    ),
    _command(
        "Cisco", "traceroute", "Path trace", "traceroute <IP_ADDRESS>", "diagnostics",
        "Sends active probes to discover the routed path toward a destination.",
        ("traceroute", "trace route", "path trace"),
        ("network path", "hops", "routing diagnostics"),
        "Replace <IP_ADDRESS> with an approved destination. This active diagnostic can send multiple probes and take time.",
        mode="exec",
        risk="active-diagnostic",
    ),
    _command(
        "Huawei", "traceroute", "Path trace", "tracert <IP_ADDRESS>", "diagnostics",
        "Sends active probes to discover the routed path toward a destination.",
        ("traceroute", "trace route", "path trace"),
        ("tracert", "network path", "hops", "routing diagnostics"),
        "Replace <IP_ADDRESS> with an approved destination. This active diagnostic can send multiple probes and take time.",
        mode="user view",
        risk="active-diagnostic",
    ),
    _command(
        "Cisco", "vlan-details", "VLAN details", "show vlan id <VLAN_ID>", "layer-2",
        "Shows status and membership details for one VLAN.",
        ("vlan details", "specific vlan", "vlan lookup"),
        ("vlan id", "access ports", "vlan status"),
        "Replace <VLAN_ID> with the VLAN to inspect.",
        mode="privileged exec",
    ),
    _command(
        "Huawei", "vlan-details", "VLAN details", "display vlan <VLAN_ID>", "layer-2",
        "Shows status and membership details for one VLAN.",
        ("vlan details", "specific vlan", "vlan lookup"),
        ("vlan id", "interfaces", "vlan status"),
        "Replace <VLAN_ID> with the VLAN to inspect.",
        mode="user view",
    ),
    _command(
        "Cisco", "mac-lookup", "MAC address lookup", "show mac address-table address <MAC_ADDRESS>", "layer-2",
        "Looks up one MAC address in the forwarding table.",
        ("mac lookup", "find mac address", "address table lookup"),
        ("forwarding database", "fdb", "port lookup"),
        "Replace <MAC_ADDRESS> with a vendor-accepted MAC address format.",
        mode="privileged exec",
    ),
    _command(
        "Huawei", "mac-lookup", "MAC address lookup", "display mac-address <MAC_ADDRESS>", "layer-2",
        "Looks up one MAC address in the forwarding table.",
        ("mac lookup", "find mac address", "address table lookup"),
        ("forwarding database", "fdb", "port lookup"),
        "Replace <MAC_ADDRESS> with a vendor-accepted MAC address format.",
        mode="user view",
    ),
    _command(
        "Cisco", "arp-lookup", "ARP entry lookup", "show ip arp <IP_ADDRESS>", "layer-3",
        "Looks up the ARP entry for one IPv4 address.",
        ("arp lookup", "find arp entry", "ip neighbor lookup"),
        ("ipv4", "mac mapping", "address resolution"),
        "Replace <IP_ADDRESS> with the IPv4 address to inspect.",
        mode="privileged exec",
    ),
    _command(
        "Huawei", "arp-lookup", "ARP entry lookup", "display arp <IP_ADDRESS>", "layer-3",
        "Looks up the ARP entry for one IPv4 address.",
        ("arp lookup", "find arp entry", "ip neighbor lookup"),
        ("ipv4", "mac mapping", "address resolution"),
        "Replace <IP_ADDRESS> with the IPv4 address to inspect; verify positional filtering on the deployed release.",
        mode="user view",
    ),
    _command(
        "Cisco", "stp-vlan", "Spanning tree for VLAN", "show spanning-tree vlan <VLAN_ID>", "layer-2",
        "Shows spanning-tree state and roles for one VLAN.",
        ("stp vlan", "spanning tree vlan", "vlan topology"),
        ("root bridge", "port role", "blocked port"),
        "Replace <VLAN_ID> with the VLAN to inspect.",
        mode="privileged exec",
    ),
    _command(
        "Huawei", "stp-vlan", "Spanning tree for VLAN", "display stp vlan <VLAN_ID>", "layer-2",
        "Shows spanning-tree state and roles for one VLAN.",
        ("stp vlan", "spanning tree vlan", "vlan topology"),
        ("root bridge", "port role", "blocked port"),
        "Replace <VLAN_ID> with the VLAN to inspect; verify MSTP or VBST behavior on the deployed release.",
        mode="user view",
    ),
    _command(
        "Cisco", "lldp-interface", "LLDP neighbor on interface", "show lldp neighbors interface <INTERFACE> detail", "layer-2",
        "Shows detailed LLDP neighbor information for one local interface.",
        ("lldp interface", "neighbor by interface", "lldp port detail"),
        ("topology", "remote port", "chassis id"),
        "Replace <INTERFACE> with a valid local interface identifier.",
        mode="privileged exec",
    ),
    _command(
        "Huawei", "lldp-interface", "LLDP neighbor on interface", "display lldp neighbor interface <INTERFACE> verbose", "layer-2",
        "Shows detailed LLDP neighbor information for one local interface.",
        ("lldp interface", "neighbor by interface", "lldp port detail"),
        ("topology", "remote port", "chassis id"),
        "Replace <INTERFACE> with a valid local interface identifier; verify argument order on the deployed release.",
        mode="user view",
    ),
    _command(
        "Cisco", "trunk-native-vlan", "Trunk native VLAN", "switchport trunk native vlan <VLAN_ID>", "layer-2",
        "Sets the native VLAN for untagged frames on the selected trunk.",
        ("native vlan", "trunk pvid", "untagged trunk vlan"),
        ("switchport trunk", "pvid", "vlan id"),
        "Replace <VLAN_ID> with the approved VLAN. Run inside interface configuration context after setting trunk mode; changing a live trunk can interrupt service.",
        mode="interface configuration",
        risk="service-impacting",
        rollback_syntax="no switchport trunk native vlan",
    ),
    _command(
        "Huawei", "trunk-native-vlan", "Trunk PVID", "port trunk pvid vlan <VLAN_ID>", "layer-2",
        "Sets the PVID used for untagged frames on the selected trunk.",
        ("native vlan", "trunk pvid", "untagged trunk vlan"),
        ("port trunk", "pvid", "vlan id"),
        "Replace <VLAN_ID> with the approved VLAN. Run inside interface view after setting trunk link type; changing a live trunk can interrupt service.",
        mode="interface view",
        risk="service-impacting",
        rollback_syntax="undo port trunk pvid vlan",
    ),
    _command(
        "Cisco", "interface-mtu", "Interface MTU", "mtu <BYTES>", "interfaces",
        "Sets the maximum transmission unit for the selected interface.",
        ("interface mtu", "maximum transmission unit", "jumbo frame size"),
        ("packet size", "bytes", "fragmentation"),
        "Replace <BYTES> with a value supported by the interface and release. Run inside interface configuration context; no rollback is supplied because the previous non-default value is unknown.",
        mode="interface configuration",
        risk="service-impacting",
    ),
    _command(
        "Huawei", "interface-mtu", "Interface MTU", "mtu <BYTES>", "interfaces",
        "Sets the maximum transmission unit for the selected interface.",
        ("interface mtu", "maximum transmission unit", "jumbo frame size"),
        ("packet size", "bytes", "fragmentation"),
        "Replace <BYTES> with a value supported by the interface and release. Run inside interface view; no rollback is supplied because the previous non-default value is unknown.",
        mode="interface view",
        risk="service-impacting",
    ),
    _command(
        "Cisco", "interface-description-summary", "Interface description summary", "show interfaces description", "interfaces",
        "Shows interface status together with configured descriptions.",
        ("interface descriptions", "port descriptions", "interface labels"),
        ("status summary", "documentation", "port label"),
        mode="privileged exec",
    ),
    _command(
        "Huawei", "interface-description-summary", "Interface description summary", "display interface description", "interfaces",
        "Shows interface status together with configured descriptions.",
        ("interface descriptions", "port descriptions", "interface labels"),
        ("status summary", "documentation", "port label"),
        mode="user view",
    ),
    _command(
        "Cisco", "trunk-membership", "Trunk membership state", "show interfaces trunk", "layer-2",
        "Shows active trunk interfaces, native VLANs, and allowed VLAN membership.",
        ("trunk membership", "trunk status", "allowed vlan state"),
        ("native vlan", "forwarding vlans", "switchport trunk"),
        mode="privileged exec",
    ),
    _command(
        "Huawei", "trunk-membership", "Port VLAN membership", "display port vlan", "layer-2",
        "Shows port link types, PVIDs, and VLAN membership.",
        ("trunk membership", "trunk status", "allowed vlan state"),
        ("pvid", "port vlan", "link type"),
        "Verify output coverage against the deployed release because it is not structurally identical to Cisco trunk output.",
        mode="user view",
    ),
    _command(
        "Cisco", "interface-counters", "Interface counters", "show interfaces <INTERFACE> counters", "interfaces",
        "Shows packet and byte counters for one interface.",
        ("interface counters", "port counters", "traffic counters"),
        ("packets", "bytes", "statistics"),
        "Replace <INTERFACE> with a valid interface identifier; verify counter subcommand support on the deployed release.",
        mode="privileged exec",
    ),
    _command(
        "Huawei", "interface-counters", "Interface counters", "display counters interface <INTERFACE>", "interfaces",
        "Shows packet and byte counters for one interface.",
        ("interface counters", "port counters", "traffic counters"),
        ("packets", "bytes", "statistics"),
        "Replace <INTERFACE> with a valid interface identifier; verify supported interface types on the deployed release.",
        mode="user view",
    ),
    _command(
        "Cisco", "interface-current-configuration", "Interface running configuration", "show running-config interface <INTERFACE>", "interfaces",
        "Shows the active configuration for one interface.",
        ("interface configuration", "show interface config", "port running config"),
        ("running configuration", "configuration review", "interface settings"),
        "Replace <INTERFACE> with a valid interface identifier. Output can contain protected operational data.",
        mode="privileged exec",
    ),
    _command(
        "Huawei", "interface-current-configuration", "Interface current configuration", "display current-configuration interface <INTERFACE>", "interfaces",
        "Shows the active configuration for one interface.",
        ("interface configuration", "show interface config", "port running config"),
        ("current configuration", "configuration review", "interface settings"),
        "Replace <INTERFACE> with a valid interface identifier. Verify grammar on the deployed release; output can contain protected operational data.",
        mode="user view",
    ),
    _command(
        "Cisco", "route-lookup", "Route lookup", "show ip route <PREFIX>", "layer-3",
        "Looks up the routing-table entry that matches one IPv4 prefix or address.",
        ("route lookup", "find route", "prefix lookup"),
        ("next hop", "longest match", "routing table"),
        "Replace <PREFIX> with the IPv4 prefix or address to inspect.",
        mode="privileged exec",
    ),
    _command(
        "Huawei", "route-lookup", "Route lookup", "display ip routing-table <PREFIX>", "layer-3",
        "Looks up routing-table information for one IPv4 prefix or address.",
        ("route lookup", "find route", "prefix lookup"),
        ("next hop", "longest match", "routing table"),
        "Replace <PREFIX> with the IPv4 prefix or address to inspect; verify prefix notation on the deployed release.",
        mode="user view",
    ),
    _command(
        "Cisco", "filtered-logs", "Filtered system logs", "show logging | include <TEXT>", "diagnostics",
        "Filters retained logging output using a supplied pattern.",
        ("filter logs", "search logs", "log include"),
        ("events", "messages", "pattern", "syslog"),
        "Replace <TEXT> with a carefully reviewed pattern. Pattern syntax is not necessarily literal text.",
        mode="privileged exec",
    ),
    _command(
        "Huawei", "filtered-logs", "Filtered system logs", "display logbuffer | include <TEXT>", "diagnostics",
        "Filters retained log-buffer output using a supplied pattern.",
        ("filter logs", "search logs", "log include"),
        ("events", "messages", "pattern", "syslog"),
        "Replace <TEXT> with a carefully reviewed pattern. Verify filter behavior on the deployed release.",
        mode="user view",
    ),
    _command(
        "Cisco", "clock", "Device clock", "show clock detail", "system",
        "Shows device time, time source, and timezone details.",
        ("device clock", "system time", "show time"),
        ("timezone", "time source", "log correlation"),
        mode="privileged exec",
    ),
    _command(
        "Huawei", "clock", "Device clock", "display clock", "system",
        "Shows the current device date, time, and timezone.",
        ("device clock", "system time", "show time"),
        ("timezone", "time source", "log correlation"),
        mode="user view",
    ),
    _command(
        "Cisco", "ntp-status", "NTP synchronization status", "show ntp status", "system",
        "Shows NTP synchronization state and reference-clock information.",
        ("ntp status", "time synchronization", "clock sync"),
        ("stratum", "reference clock", "time source"),
        "Verify command availability when NTP features are not configured on the deployed release.",
        mode="privileged exec",
    ),
    _command(
        "Huawei", "ntp-status", "NTP synchronization status", "display ntp status", "system",
        "Shows NTP synchronization state and reference-clock information.",
        ("ntp status", "time synchronization", "clock sync"),
        ("stratum", "reference clock", "time source"),
        "Verify command availability and exact output on the deployed release.",
        mode="user view",
    ),
    _command(
        "Cisco", "save-configuration", "Save running configuration", "copy running-config startup-config", "system",
        "Copies the active running configuration to startup storage.",
        ("save configuration", "save config", "write startup config"),
        ("startup configuration", "persistent configuration", "copy running config"),
        "Interactive command: confirmation or a destination prompt may appear. Review and answer prompts deliberately; no rollback is available.",
        mode="privileged exec",
        risk="interactive",
    ),
    _command(
        "Huawei", "save-configuration", "Save current configuration", "save", "system",
        "Saves the active current configuration to startup storage.",
        ("save configuration", "save config", "write startup config"),
        ("startup configuration", "persistent configuration", "save current config"),
        "Interactive command: confirmation may be required. Review and answer prompts deliberately; no rollback is available.",
        mode="user view",
        risk="interactive",
    ),
    _command(
        "Cisco", "system-name", "Set system name", "hostname <HOSTNAME>", "system",
        "Sets the device name used in its CLI prompt and management identity.",
        ("system name", "hostname", "sysname"), ("device name", "identity", "prompt"),
        "Replace <HOSTNAME> with the approved device name. Run from global configuration mode; coordinate monitoring and naming dependencies.",
        mode="global configuration", risk="changes-state",
    ),
    _command(
        "Huawei", "system-name", "Set system name", "sysname <HOSTNAME>", "system",
        "Sets the device name used in its CLI prompt and management identity.",
        ("system name", "hostname", "sysname"), ("device name", "identity", "prompt"),
        "Replace <HOSTNAME> with the approved device name. Run from system view; coordinate monitoring and naming dependencies.",
        mode="system view", risk="changes-state",
    ),
    _command(
        "Cisco", "lldp-enable", "Enable global LLDP", "lldp run", "layer-2",
        "Enables the LLDP process globally; interface transmit and receive settings still apply.",
        ("enable lldp", "global lldp"), ("discovery", "neighbors", "advertisements"),
        "Run from global configuration mode. Review interface LLDP policy and information advertised to adjacent devices.",
        mode="global configuration", risk="changes-state",
    ),
    _command(
        "Huawei", "lldp-enable", "Enable global LLDP", "lldp enable", "layer-2",
        "Enables LLDP globally; interface LLDP settings still apply.",
        ("enable lldp", "global lldp"), ("discovery", "neighbors", "advertisements"),
        "Run from system view, rather than interface view. Review interface LLDP policy and information advertised to adjacent devices.",
        mode="system view", risk="changes-state",
    ),
    _command(
        "Cisco", "dns-domain", "Set DNS domain", "ip domain name <DOMAIN_NAME>", "system",
        "Sets the default domain suffix used by the device resolver.",
        ("dns domain", "domain name"), ("resolver", "suffix", "name resolution"),
        "Replace <DOMAIN_NAME> with the approved DNS domain. Run from global configuration mode; review existing name resolution dependencies.",
        mode="global configuration", risk="changes-state",
    ),
    _command(
        "Huawei", "dns-domain", "Set DNS domain", "dns domain <DOMAIN_NAME>", "system",
        "Adds a domain suffix used by the device resolver.",
        ("dns domain", "domain name"), ("resolver", "suffix", "name resolution"),
        "Replace <DOMAIN_NAME> with the approved DNS domain. Run from system view. Resolver activation and existing domain ordering require separate review.",
        mode="system view", risk="changes-state",
    ),
    _command(
        "Cisco", "dns-server", "Configure DNS server", "ip name-server <IP_ADDRESS>", "system",
        "Adds a DNS resolver address for device name lookups.",
        ("dns server", "name server"), ("resolver", "lookup", "name resolution"),
        "Replace <IP_ADDRESS> with the approved resolver address. Run from global configuration mode. This template shows one server; the converter accepts multiple valid addresses and expands them separately. Review resolver activation, routing and VRF scope.",
        mode="global configuration", risk="changes-state",
    ),
    _command(
        "Huawei", "dns-server", "Configure DNS server", "dns server <IP_ADDRESS>", "system",
        "Adds a DNS resolver address for device name lookups.",
        ("dns server", "name server"), ("resolver", "lookup", "name resolution"),
        "Replace <IP_ADDRESS> with the approved resolver address. Run from system view. Resolver activation, routing and VPN-instance scope require separate review.",
        mode="system view", risk="changes-state",
    ),
    _command(
        "Cisco", "ntp-server", "Configure NTP server", "ntp server <IP_ADDRESS>", "system",
        "Configures a unicast NTP time source.",
        ("ntp server", "time server"), ("clock", "synchronization", "unicast"),
        "Replace <IP_ADDRESS> with the approved time-source address. Run from global configuration mode. Verify reachability and existing time policy before changing the source.",
        mode="global configuration", risk="changes-state",
    ),
    _command(
        "Huawei", "ntp-server", "Configure NTP server", "ntp-service unicast-server <IP_ADDRESS>", "system",
        "Configures a unicast NTP time source.",
        ("ntp server", "time server"), ("clock", "synchronization", "unicast"),
        "Replace <IP_ADDRESS> with the approved time-source address. Run from system view. Verify command availability, NTP activation, reachability and existing time policy on the deployed image.",
        mode="system view", risk="changes-state",
    ),
    _command(
        "Cisco", "syslog-server", "Configure remote syslog", "logging host <IP_ADDRESS>", "diagnostics",
        "Adds a destination for remote system-log messages.",
        ("syslog server", "logging host", "remote logging"), ("loghost", "events", "collector"),
        "Replace <IP_ADDRESS> with the approved collector address. Run from global configuration mode. Review logging activation, severity, transport and source-interface policy separately.",
        mode="global configuration", risk="changes-state",
    ),
    _command(
        "Huawei", "syslog-server", "Configure remote syslog", "info-center loghost <IP_ADDRESS>", "diagnostics",
        "Adds a destination for remote information-center log messages.",
        ("syslog server", "logging host", "remote logging"), ("loghost", "events", "collector"),
        "Replace <IP_ADDRESS> with the approved collector address. Run from system view. Review information-center activation, channel, severity, transport and source-interface policy separately.",
        mode="system view", risk="changes-state",
    ),
    _command(
        "Cisco", "stp-edge-port", "Enable STP edge behavior", "spanning-tree portfast", "layer-2",
        "Enables PortFast edge behavior on the selected access interface.",
        ("stp edge port", "portfast", "edge port"), ("spanning tree", "endpoint", "loop risk"),
        "Run inside interface configuration context on an approved endpoint-facing access port. Do not use on switch links; incorrect edge settings can create loops. Trunk, guard and topology policy require separate review.",
        mode="interface configuration", risk="service-impacting",
    ),
    _command(
        "Huawei", "stp-edge-port", "Enable STP edge behavior", "stp edged-port enable", "layer-2",
        "Enables spanning-tree edge behavior on the selected interface.",
        ("stp edge port", "portfast", "edge port"), ("spanning tree", "endpoint", "loop risk"),
        "Run inside interface view on an approved endpoint-facing port. Do not use on switch links; incorrect edge settings can create loops. Guard and topology policy require separate review.",
        mode="interface view", risk="service-impacting",
    ),
    _command(
        "Cisco", "interface-ipv4-address", "Set interface IPv4 address", "ip address <IP_ADDRESS> <SUBNET_MASK>", "layer-3",
        "Sets the primary IPv4 address and mask on the selected Layer 3 interface.",
        ("interface ip address", "svi address", "ipv4 address"), ("subnet", "netmask", "routed interface"),
        "Replace <IP_ADDRESS> and <SUBNET_MASK> with the approved IPv4 address and dotted-decimal mask. Run inside an SVI or supported routed-interface configuration context; VLAN existence, routing support and Layer 2 conversion must be reviewed separately. Changing a live address can interrupt management and traffic.",
        mode="interface configuration", risk="service-impacting",
    ),
    _command(
        "Huawei", "interface-ipv4-address", "Set interface IPv4 address", "ip address <IP_ADDRESS> <SUBNET_MASK>", "layer-3",
        "Sets the primary IPv4 address and mask on the selected Layer 3 interface.",
        ("interface ip address", "svi address", "ipv4 address"), ("subnet", "netmask", "vlanif"),
        "Replace <IP_ADDRESS> and <SUBNET_MASK> with the approved IPv4 address and dotted-decimal mask. Run inside Vlanif or another supported Layer 3 interface view; VLAN existence and routing support require separate review. Changing a live address can interrupt management and traffic.",
        mode="interface view", risk="service-impacting",
    ),
    _command(
        "Cisco", "static-ipv4-route", "Configure static IPv4 route", "ip route <NETWORK> <SUBNET_MASK> <NEXT_HOP>", "layer-3",
        "Adds a static IPv4 route using a next-hop address.",
        ("static route", "ipv4 route", "next hop route"), ("network", "netmask", "routing"),
        "Replace <NETWORK>, <SUBNET_MASK> and <NEXT_HOP> with the approved IPv4 destination network, dotted-decimal mask and next-hop address. Run from global configuration mode. Verify next-hop reachability, route preference, VRF and loop risk; an incorrect route can interrupt traffic.",
        mode="global configuration", risk="service-impacting",
    ),
    _command(
        "Huawei", "static-ipv4-route", "Configure static IPv4 route", "ip route-static <NETWORK> <SUBNET_MASK> <NEXT_HOP>", "layer-3",
        "Adds a static IPv4 route using a next-hop address.",
        ("static route", "ipv4 route", "next hop route"), ("network", "netmask", "routing"),
        "Replace <NETWORK>, <SUBNET_MASK> and <NEXT_HOP> with the approved IPv4 destination network, dotted-decimal mask and next-hop address. Run from system view. Verify next-hop reachability, route preference, VPN-instance and loop risk; an incorrect route can interrupt traffic.",
        mode="system view", risk="service-impacting",
    ),
    _command(
        "Cisco", "lag-member", "Add physical link to aggregation", "channel-group <GROUP_ID> mode <MODE>", "layer-2",
        "Assigns a physical member interface to a Port-channel with the chosen negotiation mode.",
        ("link aggregation member", "channel group", "eth trunk member"), ("lacp", "port-channel", "bundle"),
        "Replace <GROUP_ID> with the approved bundle number and <MODE> with active, passive or on. Run inside physical-member interface configuration context. Active and passive use LACP; on is static. Verify peer compatibility and matching member settings. The converter needs the complete bundle/member configuration to infer Huawei LACP mode; these commands are not a standalone one-to-one replacement. Changes can interrupt traffic.",
        mode="interface configuration", risk="service-impacting",
    ),
    _command(
        "Huawei", "lag-member", "Add physical link to aggregation", "eth-trunk <GROUP_ID>", "layer-2",
        "Assigns a physical member interface to an Eth-Trunk bundle.",
        ("link aggregation member", "channel group", "eth trunk member"), ("lacp", "port-channel", "bundle"),
        "Replace <GROUP_ID> with the approved existing Eth-Trunk number. Run inside physical-member interface view. Configure LACP mode on the Eth-Trunk itself and match peer/member settings; the converter uses that bundle context when choosing Cisco active versus on. Cisco passive is not preserved on a round trip. These commands are not a standalone one-to-one replacement. Changes can interrupt traffic.",
        mode="interface view", risk="service-impacting",
    ),
    _command(
        "Huawei", "lacp-static-mode", "Enable static LACP negotiation", "mode lacp-static", "layer-2",
        "Selects LACP negotiation for the Eth-Trunk; this differs from manual load-balancing without LACP.",
        ("lacp static", "eth trunk lacp mode"), ("aggregation", "bundle", "active", "passive"),
        "Run inside the approved Eth-Trunk interface view, not a physical-member view. Check release support, existing members and peer LACP settings before changing mode; member removal or service interruption may be required. Cisco represents LACP on member channel-group commands, so there is no standalone equivalent here.",
        mode="interface view", risk="service-impacting",
    ),
    _command(
        "Huawei", "vlan-batch", "Create multiple VLANs", "vlan batch <VLAN_LIST>", "layer-2",
        "Creates multiple VLANs without entering each VLAN view.",
        ("vlan batch", "create vlans", "multiple vlans"), ("bulk", "vlan declaration", "layer 2"),
        "Replace <VLAN_LIST> with approved VLAN IDs separated by spaces. Run from system view and verify reserved IDs and platform limits. Vendor syntax also supports ranges, but the converter accepts only explicit IDs and expands them to separate Cisco VLAN contexts. This does not configure member ports or Layer 3 interfaces.",
        mode="system view", risk="changes-state",
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
    {
        "show version | include uptime",
        "display version | include uptime",
        "show logging | include <text>",
        "display logbuffer | include <text>",
    }
)
_ALLOWED_PLACEHOLDERS = frozenset(
    {
        "<INTERFACE>",
        "<DESCRIPTION>",
        "<MODE>",
        "<VLAN_ID>",
        "<VLAN_NAME>",
        "<VLAN_LIST>",
        "<IP_ADDRESS>",
        "<MAC_ADDRESS>",
        "<PREFIX>",
        "<TEXT>",
        "<BYTES>",
        "<HOSTNAME>",
        "<DOMAIN_NAME>",
        "<SUBNET_MASK>",
        "<NETWORK>",
        "<NEXT_HOP>",
        "<GROUP_ID>",
    }
)
# New reference templates are approved by concept and vendor, not as prefixes
# accepted on unrelated existing entries.
_REFERENCE_CONFIG_SYNTAX = {
    "system-name": ("hostname <HOSTNAME>", "sysname <HOSTNAME>"),
    "lldp-enable": ("lldp run", "lldp enable"),
    "dns-domain": ("ip domain name <DOMAIN_NAME>", "dns domain <DOMAIN_NAME>"),
    "dns-server": ("ip name-server <IP_ADDRESS>", "dns server <IP_ADDRESS>"),
    "ntp-server": ("ntp server <IP_ADDRESS>", "ntp-service unicast-server <IP_ADDRESS>"),
    "syslog-server": ("logging host <IP_ADDRESS>", "info-center loghost <IP_ADDRESS>"),
    "stp-edge-port": ("spanning-tree portfast", "stp edged-port enable"),
    "interface-ipv4-address": ("ip address <IP_ADDRESS> <SUBNET_MASK>", "ip address <IP_ADDRESS> <SUBNET_MASK>"),
    "static-ipv4-route": ("ip route <NETWORK> <SUBNET_MASK> <NEXT_HOP>", "ip route-static <NETWORK> <SUBNET_MASK> <NEXT_HOP>"),
    "lag-member": ("channel-group <GROUP_ID> mode <MODE>", "eth-trunk <GROUP_ID>"),
    "lacp-static-mode": (None, "mode lacp-static"),
    "vlan-batch": (None, "vlan batch <VLAN_LIST>"),
}
_REFERENCE_INTERFACE_CONCEPTS = frozenset({
    "stp-edge-port", "interface-ipv4-address", "lag-member", "lacp-static-mode",
})
_REFERENCE_SERVICE_IMPACTING = _REFERENCE_INTERFACE_CONCEPTS | {"static-ipv4-route"}
_ALLOWED_CONFIG_PREFIXES = {
    "Cisco": (
        "interface ",
        "description ",
        "switchport mode ",
        "vlan ",
        "name ",
        "switchport access vlan ",
        "switchport trunk allowed vlan ",
        "shutdown",
        "switchport trunk native vlan ",
        "mtu ",
    ),
    "Huawei": (
        "interface ",
        "description ",
        "port link-type ",
        "vlan ",
        "name ",
        "port default vlan ",
        "port trunk allow-pass vlan ",
        "shutdown",
        "port trunk pvid vlan ",
        "mtu ",
    ),
}
_ALLOWED_ROLLBACK_SYNTAX = {
    "Cisco": frozenset(
        {
            "no description",
            "no switchport mode",
            "no name",
            "no switchport access vlan",
            "no shutdown",
            "no switchport trunk native vlan",
        }
    ),
    "Huawei": frozenset(
        {
            "undo description",
            "undo port link-type",
            "undo name",
            "undo port default vlan",
            "undo shutdown",
            "undo port trunk pvid vlan",
        }
    ),
}
_ALLOWED_ACTIVE_PREFIXES = {
    "Cisco": ("ping ", "traceroute "),
    "Huawei": ("ping ", "tracert "),
}
_ALLOWED_INTERACTIVE_SYNTAX = {
    "Cisco": "copy running-config startup-config",
    "Huawei": "save",
}
_ALLOWED_MUTATING_SYNTAX = frozenset(
    {
        "shutdown",
        "no shutdown",
        "undo shutdown",
        "copy running-config startup-config",
        "save",
    }
)


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
        entry.rollback_syntax,
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
    detected violations.  The checks enforce the reviewed 118-entry manifest,
    fixed platform/release metadata, identifiers, required fields, immutable
    tuple collections, reviewed mode/risk combinations, safe placeholders,
    control-char exclusion, and absence of credential/address-like sample data.
    """
    corpus = COMMAND_ENTRIES if entries is None else (
        entries if isinstance(entries, tuple) else tuple(entries)
    )
    errors: list[str] = []
    if len(corpus) != 118:
        errors.append(f"expected 118 entries, found {len(corpus)}")

    ids: set[str] = set()
    pair_counts: dict[tuple[str, str], int] = {}
    vendor_counts = {"Cisco": 0, "Huawei": 0}
    expected_scope = {
        "Cisco": (CISCO_PLATFORM, CISCO_RELEASE, CISCO_TARGET),
        "Huawei": (HUAWEI_PLATFORM, HUAWEI_RELEASE, HUAWEI_TARGET),
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
        rollback_valid = isinstance(entry.rollback_syntax, str)
        if not rollback_valid:
            errors.append(f"rollback_syntax must be a string for {label}")
        if any(not isinstance(value, str) or not value.strip() for value in scalar_values):
            errors.append(f"all scalar fields must be non-empty strings for {label}")
        if not aliases_valid or not keywords_valid or not rollback_valid or any(
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
            platform, release, target = expected_scope[entry.vendor]
            if entry.vendor == "Huawei" and entry.concept_id in _REFERENCE_CONCEPT_IDS:
                release = HUAWEI_REFERENCE_RELEASE
            vendor_counts[entry.vendor] += 1
            if (entry.platform, entry.documented_release, entry.deployed_target) != (
                platform,
                release,
                target,
            ):
                errors.append(f"scope metadata mismatch for {label}")
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
        reference_syntax = None
        if entry.concept_id in _REFERENCE_CONFIG_SYNTAX and entry.vendor in expected_scope:
            reference_syntax = _REFERENCE_CONFIG_SYNTAX[entry.concept_id][0 if entry.vendor == "Cisco" else 1]
            if entry.syntax != reference_syntax:
                errors.append(f"unreviewed reference syntax for {label}")
            interface = entry.concept_id in _REFERENCE_INTERFACE_CONCEPTS
            reference_mode = (
                ("interface configuration" if interface else "global configuration")
                if entry.vendor == "Cisco" else ("interface view" if interface else "system view")
            )
            reference_risk = "service-impacting" if entry.concept_id in _REFERENCE_SERVICE_IMPACTING else "changes-state"
            if entry.mode != reference_mode or entry.risk != reference_risk:
                errors.append(f"unreviewed reference mode/risk for {label}")
            if entry.rollback_syntax:
                errors.append(f"rollback is not reviewed for reference entry {label}")
        if entry.risk == "read-only":
            read_only_prefix = "show " if entry.vendor == "Cisco" else "display "
            if entry.mode not in {"exec", "privileged exec", "user view"}:
                errors.append(f"read-only entry must use an operational mode for {label}")
            if not entry.syntax.casefold().startswith(read_only_prefix):
                errors.append(f"non-read-only command prefix for {label}")
        elif entry.risk in {"changes-state", "service-impacting"}:
            if entry.mode in {"exec", "privileged exec", "user view"}:
                errors.append(f"state-changing entry must use configuration mode for {label}")
            config_prefixes = _ALLOWED_CONFIG_PREFIXES.get(entry.vendor, ())
            if not entry.syntax.casefold().startswith(config_prefixes) and entry.syntax != reference_syntax:
                errors.append(f"unreviewed configuration command prefix for {label}")
        elif entry.risk == "active-diagnostic":
            if not entry.syntax.casefold().startswith(
                _ALLOWED_ACTIVE_PREFIXES.get(entry.vendor, ())
            ):
                errors.append(f"unreviewed active diagnostic prefix for {label}")
        elif entry.risk == "interactive":
            if entry.syntax.casefold() != _ALLOWED_INTERACTIVE_SYNTAX.get(
                entry.vendor, ""
            ):
                errors.append(f"unreviewed interactive command for {label}")
            note = entry.applicability_notes.casefold()
            if "interactive" not in note or "confirmation" not in note:
                errors.append(f"interactive confirmation is not documented for {label}")
        if entry.rollback_syntax:
            if entry.risk not in {"changes-state", "service-impacting"}:
                errors.append(f"rollback is not allowed for {entry.risk} entry {label}")
            if entry.concept_id in {"interface-context", "save-configuration"}:
                errors.append(f"rollback is not allowed for {entry.concept_id}")
            if entry.rollback_syntax == entry.syntax:
                errors.append(f"rollback must differ from primary syntax for {label}")
            if entry.rollback_syntax.casefold() not in _ALLOWED_ROLLBACK_SYNTAX.get(
                entry.vendor, frozenset()
            ):
                errors.append(f"unreviewed rollback command for {label}")
        strings = _entry_strings(entry)
        if any(_CONTROL_RE.search(value) for value in strings):
            errors.append(f"control character found in {label}")
        placeholders = set(_PLACEHOLDER_RE.findall(entry.syntax))
        rollback_placeholders = set(_PLACEHOLDER_RE.findall(entry.rollback_syntax))
        for syntax_role, syntax in (
            ("primary", entry.syntax),
            ("rollback", entry.rollback_syntax),
        ):
            if not syntax:
                continue
            syntax_placeholders = set(_PLACEHOLDER_RE.findall(syntax))
            syntax_without_placeholders = _PLACEHOLDER_RE.sub("", syntax)
            if "<" in syntax_without_placeholders or ">" in syntax_without_placeholders:
                errors.append(f"malformed placeholder in {syntax_role} syntax for {label}")
            if not syntax_placeholders <= _ALLOWED_PLACEHOLDERS:
                errors.append(f"unsupported placeholder in {syntax_role} syntax for {label}")
            if any(token in syntax for token in (";", "&&", "||", "`")):
                errors.append(f"command chaining is not allowed in {syntax_role} syntax for {label}")
            normalized_pipeline = " ".join(syntax.casefold().split())
            if "|" in syntax and normalized_pipeline not in _ALLOWED_PIPELINES:
                errors.append(f"unapproved pipeline modifier in {syntax_role} syntax for {label}")
            if ">" in syntax_without_placeholders:
                errors.append(f"output redirection is not allowed in {syntax_role} syntax for {label}")
            if (
                _MUTATING_SYNTAX_RE.search(syntax)
                and normalized_pipeline not in _ALLOWED_MUTATING_SYNTAX
            ):
                errors.append(f"mutating or file-operation token found in {syntax_role} syntax for {label}")
        if not rollback_placeholders <= placeholders:
            errors.append(f"rollback introduces a new placeholder for {label}")
        if placeholders and (
            not placeholders <= set(_PLACEHOLDER_RE.findall(entry.applicability_notes))
            or "replace" not in entry.applicability_notes.casefold()
        ):
            errors.append(f"placeholder substitution is not documented for {label}")
        sample_fields = (
            entry.title,
            entry.syntax,
            entry.description,
            *entry.aliases,
            *entry.keywords,
            entry.applicability_notes,
            entry.rollback_syntax,
        )
        if any(_SENSITIVE_RE.search(value) for value in sample_fields):
            errors.append(f"sensitive sample term found in {label}")
        if any(
            pattern.search(value)
            for value in sample_fields
            for pattern in (_IPV4_RE, _MAC_RE, _EMAIL_RE)
        ) or any(_contains_ipv6(value) for value in sample_fields):
            errors.append(f"literal address-like sample found in {label}")

    if vendor_counts != {"Cisco": 58, "Huawei": 60}:
        errors.append(f"expected 58 Cisco and 60 Huawei entries, found {vendor_counts}")
    for concept_id in CONCEPT_IDS:
        for vendor in ("Cisco", "Huawei"):
            count = pair_counts.get((concept_id, vendor), 0)
            expected_count = 0 if vendor == "Cisco" and concept_id in _HUAWEI_ONLY_CONCEPTS else 1
            if count != expected_count:
                expected_label = "one" if expected_count else "no"
                errors.append(f"expected {expected_label} {vendor} entry for {concept_id}, found {count}")
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
    somewhere in an entry's searchable fields, including rollback syntax.
    Ranking is: exact primary syntax, exact rollback syntax, primary/rollback
    prefix, title/alias match, description/keyword match, then other metadata;
    original corpus order is the stable tie-breaker.  A blank query
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
        rollback = _normalize(entry.rollback_syntax)
        title_alias = _normalize(" ".join((entry.title, *entry.aliases)))
        description_keyword = _normalize(" ".join((entry.description, *entry.keywords)))
        if normalized_query and normalized_query == syntax:
            rank = 0
        elif normalized_query and rollback and normalized_query == rollback:
            rank = 1
        elif normalized_query and syntax.startswith(normalized_query):
            rank = 2
        elif normalized_query and rollback and rollback.startswith(normalized_query):
            rank = 3
        elif query_tokens and _matches_partial_tokens(query_tokens, title_alias):
            rank = 4
        elif query_tokens and _matches_partial_tokens(
            query_tokens, description_keyword
        ):
            rank = 5
        else:
            rank = 6
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
