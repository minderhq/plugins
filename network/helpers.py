"""Pure helpers + SNMP OID constants for the network plugin.

Everything here is side-effect-free (no subprocess, no network) and unit-tested in
isolation. ``__init__`` imports these (and re-exposes them), so both the plugin and
the tests reference them via ``network``. Names keep their ``_`` prefix to
match their long-standing use as network-plugin internals.
"""

import ipaddress
import json
from typing import Dict, List, Tuple
import defusedxml.ElementTree as ElementTree

# ── SNMP OID constants ────────────────────────────────────────────────────────
_SNMP_SYSTEM_OIDS = {
    "sysDescr": "1.3.6.1.2.1.1.1.0",
    "sysObjectID": "1.3.6.1.2.1.1.2.0",
    "sysUpTime": "1.3.6.1.2.1.1.3.0",
    "sysContact": "1.3.6.1.2.1.1.4.0",
    "sysName": "1.3.6.1.2.1.1.5.0",
    "sysLocation": "1.3.6.1.2.1.1.6.0",
}
_IF_DESCR_OID = "1.3.6.1.2.1.2.2.1.2"
_IF_TYPE_OID = "1.3.6.1.2.1.2.2.1.3"
_IF_SPEED_OID = "1.3.6.1.2.1.2.2.1.5"
_IF_MAC_OID = "1.3.6.1.2.1.2.2.1.6"
_IF_OPERSTATUS_OID = "1.3.6.1.2.1.2.2.1.8"
_IF_ALIAS_OID = "1.3.6.1.2.1.31.1.1.1.18"  # ifXTable ifAlias
_IF_OPER = {"1": "up", "2": "down", "3": "testing"}

# Host Resources MIB (device health / inventory).
_HR_UPTIME_OID = "1.3.6.1.2.1.25.1.1.0"
_HR_MEMSIZE_OID = "1.3.6.1.2.1.25.2.2.0"
_HR_PROC_LOAD_OID = "1.3.6.1.2.1.25.3.3.1.2"  # per-processor load %
_HR_STOR_DESCR_OID = "1.3.6.1.2.1.25.2.3.1.3"
_HR_STOR_SIZE_OID = "1.3.6.1.2.1.25.2.3.1.5"  # in allocation units
_HR_STOR_USED_OID = "1.3.6.1.2.1.25.2.3.1.6"

# ARP / neighbour table: ipNetToMediaPhysAddress (index = ifIndex.a.b.c.d → MAC).
_ARP_MAC_OID = "1.3.6.1.2.1.4.22.1.2"


# ── discovery / parsing helpers ───────────────────────────────────────────────
def _expand_targets(spec: str, max_hosts: int) -> List[str]:
    """Expand a comma-separated hosts/CIDRs spec into individual host strings,
    capped at ``max_hosts``. A bare IP/hostname passes through; a CIDR expands."""
    hosts: List[str] = []
    for raw in spec.split(","):
        item = raw.strip()
        if not item:
            continue
        try:
            net = ipaddress.ip_network(item, strict=False)
        except ValueError:
            hosts.append(item)  # hostname (not an IP/CIDR)
            if len(hosts) >= max_hosts:
                return hosts[:max_hosts]
            continue
        if net.num_addresses == 1:
            hosts.append(str(net.network_address))
        else:
            for ip in net.hosts():
                hosts.append(str(ip))
                if len(hosts) >= max_hosts:
                    return hosts[:max_hosts]
        if len(hosts) >= max_hosts:
            return hosts[:max_hosts]
    return hosts[:max_hosts]


def _parse_nmap_xml(xml_text: str) -> List[Dict]:
    """Parse ``nmap -oX`` output into [{host, hostname, state, ports:[...]}]."""
    hosts: List[Dict] = []
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        return hosts
    for host_el in root.findall("host"):
        status = host_el.find("status")
        state = status.get("state") if status is not None else "unknown"
        addr = ""
        for a in host_el.findall("address"):
            if a.get("addrtype") in ("ipv4", "ipv6"):
                addr = a.get("addr", "")
                break
        hostname = ""
        hn = host_el.find("hostnames/hostname")
        if hn is not None:
            hostname = hn.get("name", "")
        ports = []
        for p in host_el.findall("ports/port"):
            pstate = p.find("state")
            if pstate is None or pstate.get("state") != "open":
                continue
            svc = p.find("service")
            ports.append(
                {
                    "port": int(p.get("portid", "0")),
                    "protocol": p.get("protocol", "tcp"),
                    "service": svc.get("name", "") if svc is not None else "",
                    "product": svc.get("product", "") if svc is not None else "",
                    "version": svc.get("version", "") if svc is not None else "",
                }
            )
        hosts.append(
            {"host": addr, "hostname": hostname, "state": state, "ports": ports}
        )
    return hosts


def _parse_snmpwalk(text: str, base_oid: str) -> Dict[str, str]:
    """Parse ``snmpbulkwalk -Oqn`` output (numeric-OID, quiet) into {index: value},
    where index is the OID suffix after ``base_oid``."""
    result: Dict[str, str] = {}
    prefix = base_oid if base_oid.startswith(".") else "." + base_oid
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(" ", 1)
        oid = parts[0]
        value = parts[1].strip().strip('"') if len(parts) > 1 else ""
        if oid.startswith(prefix + "."):
            result[oid[len(prefix) + 1 :]] = value
    return result


def _normalize_mac(mac: str) -> str:
    """Normalise a MAC to lowercase colon-separated (net-snmp renders it space- or
    colon-separated depending on flags/agent)."""
    return mac.replace(" ", ":").strip(":").lower()


def _parse_arp(raw: Dict[str, str]) -> Dict[str, str]:
    """Turn a parsed ipNetToMediaPhysAddress walk ({index: mac}) into {ip: mac}.
    The OID index is ``<ifIndex>.<a>.<b>.<c>.<d>``; value is the MAC."""
    out: Dict[str, str] = {}
    for index, mac in raw.items():
        parts = index.split(".")
        if len(parts) < 5:
            continue
        ip = ".".join(parts[-4:])
        out[ip] = _normalize_mac(mac)
    return out


# The platform's limits on a telegraf managed region (it refuses a larger one
# outright), with headroom: the region text in UTF-8 bytes and the number of
# TOML tables in it.
_REGION_MAX_BYTES = 60 * 1024
_REGION_MAX_TABLES = 480


def _toml_str(value: object) -> str:
    """A TOML basic string for ``value``. JSON string escaping is valid TOML
    (quotes, backslashes and control characters, incl. newlines, are escaped),
    so a scanned address, tenant id or community can never close the string or
    add keys/tables to the region."""
    return json.dumps(str(value))


def _host_blocks(h: Dict, snmp_community: str, tenant_id: str) -> List[Tuple[str, int]]:
    """``[(toml_block, table_count)]`` for one host: a net_response input per
    open TCP port and an snmp input when the host answered SNMP."""
    addr = str(h.get("host", ""))
    tenant = _toml_str(tenant_id)

    def tags(kind: str) -> str:
        return f"  [inputs.{kind}.tags]\n    tenant_id = {tenant}"

    out: List[Tuple[str, int]] = []
    for p in h.get("ports", []):
        if p.get("protocol", "tcp") != "tcp":
            continue
        address = f"{addr}:{p['port']}"
        out.append(
            (
                "[[inputs.net_response]]\n"
                '  protocol = "tcp"\n'
                f"  address = {_toml_str(address)}\n"
                '  timeout = "5s"\n' + tags("net_response"),
                2,  # the input table + its tags table
            )
        )
    if h.get("snmp"):
        agent = f"udp://{addr}:161"
        fields = "\n".join(
            "  [[inputs.snmp.field]]\n"
            f"    name = {_toml_str(name)}\n"
            f"    oid = {_toml_str(oid)}"
            for name, oid in _SNMP_SYSTEM_OIDS.items()
        )
        out.append(
            (
                "[[inputs.snmp]]\n"
                f"  agents = [{_toml_str(agent)}]\n"
                "  version = 2\n"
                f"  community = {_toml_str(snmp_community)}\n"
                + tags("snmp")
                + "\n"
                + fields,
                2 + len(_SNMP_SYSTEM_OIDS),  # input + tags + one per field
            )
        )
    return out


def _telegraf_region(
    hosts: List[Dict],
    snmp_community: str,
    tenant_id: str = "",
    max_bytes: int = _REGION_MAX_BYTES,
    max_tables: int = _REGION_MAX_TABLES,
) -> Tuple[str, int]:
    """``(region, hosts_dropped)``: the telegraf inputs for ``hosts`` (see
    :func:`_telegraf_config`), trimmed to fit the platform's region limits.

    Hosts are taken whole, in address order, until the next one would exceed
    ``max_bytes`` or ``max_tables``; the rest are dropped (and counted), so a
    large scan still monitors a stable subset instead of being refused whole."""

    def _key(h: Dict) -> Tuple[int, int, str]:
        host = str(h.get("host", ""))
        try:
            ip = ipaddress.ip_address(host)
            return (0, ip.version * 2**128 + int(ip), "")
        except ValueError:
            return (1, 0, host)

    blocks: List[str] = []
    size, tables, dropped = 0, 0, 0
    for h in sorted(hosts, key=_key):
        host_blocks = _host_blocks(h, snmp_community, tenant_id)
        if not host_blocks:
            continue
        add_bytes = sum(len(b.encode("utf-8")) + 1 for b, _ in host_blocks)
        add_tables = sum(t for _, t in host_blocks)
        if dropped or size + add_bytes > max_bytes or tables + add_tables > max_tables:
            dropped += 1
            continue
        blocks.extend(b for b, _ in host_blocks)
        size += add_bytes
        tables += add_tables
    return "\n".join(blocks), dropped


def _telegraf_config(
    hosts: List[Dict], snmp_community: str, tenant_id: str = ""
) -> str:
    """Render telegraf inputs for discovered hosts: net_response per open TCP port,
    and an snmp input per SNMP-capable host.

    #984: each input carries a ``[inputs.X.tags] tenant_id`` so the net_response /
    snmp series telegraf writes to InfluxDB are tagged with the owning tenant --
    LAN-scan data is private infra data, not a public series, and must stay
    per-tenant queryable/isolated in InfluxDB.

    Every value is a TOML basic string (:func:`_toml_str`), and the region is
    trimmed to the platform's limits (:func:`_telegraf_region`).

    NOTE: consumed by the telegraf plugin's set_managed_region(), which the
    platform validates as TOML — keep the output valid TOML (see ../telegraf)."""
    return _telegraf_region(hosts, snmp_community, tenant_id)[0]


def _summarize(hosts: List[Dict]) -> Dict[str, Dict]:
    """{ip: {ports:[sorted], snmp:bool}} — the shape change detection compares."""
    out: Dict[str, Dict] = {}
    for h in hosts:
        ip = h.get("host", "")
        if not ip:
            continue
        out[ip] = {
            "ports": sorted(p["port"] for p in h.get("ports", [])),
            "snmp": bool(h.get("snmp")),
        }
    return out


def _diff_hosts(prev: Dict[str, Dict], curr: Dict[str, Dict]) -> Dict[str, List[str]]:
    """Change events between two summaries: new / down / changed host IPs."""
    new = [ip for ip in curr if ip not in prev]
    down = [ip for ip in prev if ip not in curr]
    changed = [ip for ip in curr if ip in prev and curr[ip] != prev[ip]]
    return {"new": new, "down": down, "changed": changed}


def _configured_cidrs(spec: str) -> List[str]:
    """The CIDR entries (num_addresses > 1) from a targets spec — the ranges the
    operator authorised, used to bound auto-expansion."""
    out: List[str] = []
    for raw in spec.split(","):
        item = raw.strip()
        if not item:
            continue
        try:
            net = ipaddress.ip_network(item, strict=False)
        except ValueError:
            continue
        if net.num_addresses > 1:
            out.append(item)
    return out


def _extract_neighbor_ips(hosts: List[Dict], cidrs: List[str]) -> List[str]:
    """ARP-neighbour IPs (from each host's snmp.arp) that fall INSIDE one of
    ``cidrs`` — safe auto-expansion candidates (never outside authorised ranges)."""
    nets = []
    for c in cidrs:
        try:
            nets.append(ipaddress.ip_network(c, strict=False))
        except ValueError:
            continue
    if not nets:
        return []
    found = set()
    for h in hosts:
        for ip in h.get("snmp", {}).get("arp", {}):
            try:
                addr = ipaddress.ip_address(ip)
            except ValueError:
                continue
            if any(addr in n for n in nets):
                found.add(ip)
    return sorted(found)
