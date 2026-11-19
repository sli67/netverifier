"""Exact set arithmetic on packet and route spaces, for checks with exceptions.

A check such as "deny everything from the guests to 10.30.0.10 except TCP/443"
covers a space minus some carve-outs. The model only names the space and the
carve-outs; this module works out what is left, as pieces Batfish can search
one at a time. No piece is approximated: together they are exactly the space
minus the exceptions.
"""

import ipaddress
import re
from typing import Any

Intervals = list[tuple[int, int]]

FULL_IP: Intervals = [(0, 2**32 - 1)]
FULL_PORTS: Intervals = [(0, 65535)]
ALL_PROTOCOLS = frozenset(range(256))
PROTOCOLS = {"icmp": 1, "tcp": 6, "udp": 17}
NAMES = {number: name.upper() for name, number in PROTOCOLS.items()}
PORTED = frozenset({6, 17})
DIMENSIONS = ("src", "dst", "proto", "ports")
PREFIX_RANGE = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){3}/\d{1,2})(?::(\d{1,2})-(\d{1,2}))?$")


class SpaceError(ValueError):
    """A space is written in a form this module does not understand."""


# --- intervals --------------------------------------------------------------


def _normalise(items: Intervals) -> Intervals:
    out: Intervals = []
    for lo, hi in sorted(items):
        if out and lo <= out[-1][1] + 1:
            out[-1] = (out[-1][0], max(out[-1][1], hi))
        else:
            out.append((lo, hi))
    return out


def intersect(a: Intervals, b: Intervals) -> Intervals:
    out = [(max(x, y), min(p, q)) for x, p in a for y, q in b if max(x, y) <= min(p, q)]
    return _normalise(out)


def subtract(a: Intervals, b: Intervals) -> Intervals:
    out: Intervals = []
    for lo, hi in a:
        pieces = [(lo, hi)]
        for blo, bhi in b:
            pieces = [p for x, y in pieces for p in ((x, min(y, blo - 1)), (max(x, bhi + 1), y)) if p[0] <= p[1]]
        out.extend(pieces)
    return _normalise(out)


# --- parsing ----------------------------------------------------------------


def parse_ips(text: str | None) -> Intervals:
    """Addresses or prefixes, comma-separated, such as "10.20.0.0/24, 10.30.0.10". Empty means any."""
    if not text or text.strip().lower() in ("any", "0.0.0.0/0"):
        return FULL_IP
    out: Intervals = []
    for part in text.split(","):
        try:
            network = ipaddress.IPv4Network(part.strip(), strict=False)
        except ValueError as error:
            raise SpaceError(f"{part.strip()!r} is not an IPv4 address or prefix") from error
        out.append((int(network.network_address), int(network.broadcast_address)))
    return _normalise(out)


def parse_ports(text: str | None) -> Intervals:
    """Ports such as "443", "1-1023" or "53, 853". Empty means any."""
    if not text:
        return FULL_PORTS
    out: Intervals = []
    for part in text.split(","):
        bounds = part.strip().split("-")
        try:
            lo, hi = int(bounds[0]), int(bounds[-1])
        except ValueError as error:
            raise SpaceError(f"{part.strip()!r} is not a port or port range") from error
        if len(bounds) > 2 or not 0 <= lo <= hi <= 65535:
            raise SpaceError(f"{part.strip()!r} is not a port or port range")
        out.append((lo, hi))
    return _normalise(out)


def parse_protocol(protocol: str | None, ports: str | None) -> frozenset[int]:
    if protocol:
        number = PROTOCOLS.get(protocol.strip().lower())
        if number is None:
            raise SpaceError(f"protocol must be tcp, udp or icmp, not {protocol!r}")
        if ports and number not in PORTED:
            raise SpaceError("ports apply only to tcp and udp")
        return frozenset({number})
    # Ports without a protocol mean TCP and UDP: no other protocol has ports.
    return PORTED if ports else ALL_PROTOCOLS


def packet_box(src_ips: str | None, dst_ips: str | None, protocol: str | None, dst_ports: str | None) -> dict:
    return {
        "src": parse_ips(src_ips),
        "dst": parse_ips(dst_ips),
        "proto": parse_protocol(protocol, dst_ports),
        "ports": parse_ports(dst_ports),
    }


# --- packet spaces ----------------------------------------------------------


def _meet(dimension: str, a: Any, b: Any) -> Any:
    return a & b if dimension == "proto" else intersect(a, b)


def _minus(dimension: str, a: Any, b: Any) -> Any:
    return a - b if dimension == "proto" else subtract(a, b)


def box_minus(a: dict, b: dict) -> list[dict]:
    """a minus b, as disjoint boxes."""
    if any(not _meet(d, a[d], b[d]) for d in DIMENSIONS):
        return [a]
    out, current = [], dict(a)
    for d in DIMENSIONS:
        rest = _minus(d, current[d], b[d])
        if rest:
            out.append({**current, d: rest})
        current[d] = _meet(d, current[d], b[d])
    return out


def packets_minus(space: dict, exceptions: list[dict]) -> list[dict]:
    boxes = [space]
    for exception in exceptions:
        boxes = [piece for box in boxes for piece in box_minus(box, exception)]
    return boxes


def _cidrs(items: Intervals) -> str:
    networks = [
        str(net)
        for lo, hi in items
        for net in ipaddress.summarize_address_range(ipaddress.IPv4Address(lo), ipaddress.IPv4Address(hi))
    ]
    return ", ".join(networks)


def header_fields(box: dict) -> dict[str, Any]:
    """Batfish HeaderConstraints fields for one box."""
    fields: dict[str, Any] = {}
    if box["src"] != FULL_IP:
        fields["srcIps"] = _cidrs(box["src"])
    if box["dst"] != FULL_IP:
        fields["dstIps"] = _cidrs(box["dst"])
    proto = box["proto"]
    if proto != ALL_PROTOCOLS:
        missing = ALL_PROTOCOLS - proto
        if len(missing) < len(proto):
            fields["ipProtocols"] = [f"!{NAMES.get(p, p)}" for p in sorted(missing)]
        else:
            fields["ipProtocols"] = [NAMES.get(p, str(p)) for p in sorted(proto)]
    if box["ports"] != FULL_PORTS:
        fields["dstPorts"] = ",".join(f"{lo}" if lo == hi else f"{lo}-{hi}" for lo, hi in box["ports"])
    return fields


# --- route prefix spaces ----------------------------------------------------

PrefixRange = tuple[ipaddress.IPv4Network, int, int]


def parse_prefix_range(text: str) -> PrefixRange:
    """One prefix range in Batfish's form: 10.0.0.0/8 (exactly that prefix) or 10.0.0.0/8:8-32."""
    match = PREFIX_RANGE.match(text.strip())
    if not match:
        raise SpaceError(
            f"{text!r} is not one prefix range such as 10.0.0.0/8 or 10.0.0.0/8:8-32 (use one check per range)"
        )
    try:
        network = ipaddress.IPv4Network(match.group(1), strict=False)
    except ValueError as error:
        raise SpaceError(f"{text!r} is not a valid prefix") from error
    lo = int(match.group(2)) if match.group(2) else network.prefixlen
    hi = int(match.group(3)) if match.group(3) else network.prefixlen
    if not network.prefixlen <= lo <= hi <= 32:
        raise SpaceError(f"{text!r}: lengths must be between the prefix length and 32, low to high")
    return network, lo, hi


def _range(network: ipaddress.IPv4Network, lo: int, hi: int) -> list[PrefixRange]:
    lo = max(lo, network.prefixlen)
    return [(network, lo, hi)] if lo <= hi else []


def range_minus(r: PrefixRange, e: PrefixRange) -> list[PrefixRange]:
    """The prefixes in r that are not in e, as prefix ranges."""
    (n, lo, hi), (m, elo, ehi) = r, e
    if not n.overlaps(m):
        return [r]
    if n.subnet_of(m):
        # Every prefix in r lies inside m, so only its length decides.
        return _range(n, lo, min(hi, elo - 1)) + _range(n, max(lo, ehi + 1), hi)
    out = _range(n, lo, min(hi, elo - 1)) + _range(n, max(lo, ehi + 1), hi)
    a, b = max(lo, elo), min(hi, ehi)
    if a <= b:
        # Shorter than m: such prefixes contain m or miss it, never lie inside it.
        out += _range(n, a, min(b, m.prefixlen - 1))
        # As long as m or longer: inside n but outside m means inside one of m's siblings.
        start = max(a, m.prefixlen)
        if start <= b:
            for sibling in n.address_exclude(m):
                out += _range(sibling, start, b)
    return out


def prefixes_minus(space: PrefixRange, exceptions: list[PrefixRange]) -> list[str]:
    ranges = [space]
    for exception in exceptions:
        ranges = [piece for r in ranges for piece in range_minus(r, exception)]
    return [f"{net}:{lo}-{hi}" for net, lo, hi in ranges]


# --- membership: is one route or packet inside a space? -----------------------


def prefix_in(network: str, space: PrefixRange) -> bool:
    net = ipaddress.IPv4Network(network, strict=False)
    outer, lo, hi = space
    return lo <= net.prefixlen <= hi and net.subnet_of(outer)


def point_box(src_ip: str, dst_ip: str, protocol: str | int, dst_port: int | None) -> dict:
    """A single packet as a box, for testing whether a space contains it."""
    name = str(protocol).lower()
    number = PROTOCOLS.get(name, int(name) if name.isdigit() else -1)
    src, dst = int(ipaddress.IPv4Address(src_ip)), int(ipaddress.IPv4Address(dst_ip))
    port = dst_port if number in PORTED and dst_port is not None else 0
    return {"src": [(src, src)], "dst": [(dst, dst)], "proto": frozenset({number}), "ports": [(port, port)]}


def box_contains(outer: dict, point: dict) -> bool:
    """Whether every packet in point lies in outer. Ports count only for TCP and UDP."""
    if not point["proto"] <= outer["proto"]:
        return False
    if subtract(point["src"], outer["src"]) or subtract(point["dst"], outer["dst"]):
        return False
    return not (point["proto"] <= PORTED and subtract(point["ports"], outer["ports"]))
