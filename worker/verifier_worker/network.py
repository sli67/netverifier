"""A compact map of the network, computed by code from Batfish's parsed model.

The agent gets this up front, so a question about the network as a whole does
not start with dozens of tool calls to rediscover devices and links. It uses
only questions that need the parsed configs, not the routing computation, so it
is available even when Batfish cannot compute routing for a snapshot.
"""

import ipaddress
import re
from collections import Counter, defaultdict
from typing import Any

MAX_DEVICES = 60
MAX_PAIRS = 80
LINK_EXAMPLES = 3
MAX_SEGMENTS = 20
MAX_EXTERNAL_ASES = 6
MAX_NAMES = 8
LOOPBACK = re.compile(r"^(lo|loopback)", re.I)
IGP_PATTERNS = {"isis": re.compile(r"\bisis\b", re.I), "ospf": re.compile(r"\bospf3?\b", re.I)}


def _rows(frame) -> list[dict[str, Any]]:
    return [row for _, row in frame.iterrows()]


def overview(bf: Any, files: dict[str, str] | None = None, parsed_files: list[dict[str, Any]] | None = None) -> dict:
    """Devices, the links between them and their BGP neighbors, from the current model of `bf`.

    files and parsed_files, when given, are used to note which IGPs each device's config mentions.
    """
    formats = {
        r["Node"]: r["Configuration_Format"]
        for r in _rows(bf.q.nodeProperties(properties="Configuration_Format").answer().frame())
    }
    devices: dict[str, dict[str, Any]] = {
        name: {"name": name, "format": fmt, "interfaces": 0, "addressed": 0, "vrfs": set(), "router_ids": set(),
               "as": set(), "ibgp": 0, "ebgp": 0, "internal_peers": set(), "rr_clients": 0, "dynamic_peers": 0,
               "external": Counter(), "igp": [], "loopbacks": [], "groups": Counter()}  # fmt: skip
        for name, fmt in formats.items()
    }

    owner: dict[str, str] = {}  # address -> device
    subnets: dict[Any, set[tuple[str, str]]] = defaultdict(set)
    descriptions: dict[tuple[str, str], str] = {}
    frame = bf.q.interfaceProperties(properties="Active,All_Prefixes,VRF,Description").answer().frame()
    for r in _rows(frame):
        node, name = r["Interface"].hostname, r["Interface"].interface
        device = devices.get(node)
        if device is None:
            continue
        device["interfaces"] += 1
        if r["VRF"] and r["VRF"] != "default":
            device["vrfs"].add(r["VRF"])
        prefixes = r["All_Prefixes"] or []
        if not r["Active"] or not prefixes:
            continue
        device["addressed"] += 1
        if r.get("Description"):
            descriptions[(node, name)] = str(r["Description"])
        for prefix in prefixes:
            try:
                address = ipaddress.ip_interface(prefix)
            except ValueError:
                continue
            owner[str(address.ip)] = node
            if address.network.num_addresses == 1 and LOOPBACK.match(name):
                device["loopbacks"].append(str(address.ip))
            if address.network.num_addresses > 1:
                subnets[address.network].add((node, name))

    for r in _rows(bf.q.bgpProcessConfiguration().answer().frame()):
        if r["Node"] in devices:
            devices[r["Node"]]["router_ids"].add(str(r["Router_ID"]))
    for r in _rows(bf.q.bgpPeerConfiguration().answer().frame()):
        device = devices.get(r["Node"])
        if device is None:
            continue
        local, remote, ip = str(r["Local_AS"]), str(r["Remote_AS"]), str(r["Remote_IP"] or "")
        device["as"].add(local)
        if r.get("Peer_Group"):
            device["groups"][str(r["Peer_Group"])] += 1
        if r.get("Route_Reflector_Client"):
            device["rr_clients"] += 1
        if "/" in ip or not ip:
            device["dynamic_peers"] += 1
            continue
        device["ibgp" if local == remote else "ebgp"] += 1
        peer = owner.get(ip)
        if peer and peer != r["Node"]:
            device["internal_peers"].add(peer)
        elif not peer:
            device["external"][remote] += 1

    if files and parsed_files:
        for entry in parsed_files:
            text = files.get(entry["path"], "")
            for node in entry.get("nodes") or []:
                if node in devices:
                    devices[node]["igp"] = [name for name, pattern in IGP_PATTERNS.items() if pattern.search(text)]

    links, segments = [], []
    ordered = sorted(subnets.items(), key=lambda item: (item[0].version, int(item[0].network_address)))
    for network, ends in ordered:
        nodes = sorted({node for node, _ in ends})
        if len(nodes) == 2:
            a = sorted(name for node, name in ends if node == nodes[0])
            b = sorted(name for node, name in ends if node == nodes[1])
            described = [descriptions[end] for end in sorted(ends) if end in descriptions]
            links.append(
                {"a": nodes[0], "a_if": ", ".join(a), "b": nodes[1], "b_if": ", ".join(b), "subnet": str(network),
                 "description": described[0] if described else ""}  # fmt: skip
            )
        elif len(nodes) > 2:
            segments.append({"subnet": str(network), "devices": nodes})

    neighbors: dict[str, set[str]] = defaultdict(set)
    for link in links:
        neighbors[link["a"]].add(link["b"])
        neighbors[link["b"]].add(link["a"])
    out = []
    for device in devices.values():
        out.append(
            {
                **device,
                "vrfs": sorted(device["vrfs"]),
                "loopbacks": sorted(device["loopbacks"]),
                "groups": dict(device["groups"].most_common()),
                "router_ids": sorted(device["router_ids"] - {"None", "0.0.0.0"}),
                "as": sorted(device["as"]),
                "internal_peers": sorted(device["internal_peers"]),
                "external": dict(device["external"].most_common()),
                "linked_to": sorted(neighbors[device["name"]]),
            }
        )
    return {"devices": sorted(out, key=lambda d: d["name"]), "links": links, "segments": segments}


def _names(items: list[str]) -> str:
    more = len(items) - MAX_NAMES
    return ", ".join(items[:MAX_NAMES]) + (f" and {more} more" if more > 0 else "")


def render(view: dict | None) -> str:
    """The overview as text for a prompt, bounded in size."""
    if not view or not view.get("devices"):
        return ""
    lines = [f"Devices ({len(view['devices'])}):"]
    for d in view["devices"][:MAX_DEVICES]:
        parts = [d["format"], f"{d['interfaces']} interfaces ({d['addressed']} active with an address)"]
        if d["vrfs"]:
            parts.append(f"VRFs besides default: {_names(d['vrfs'])}")
        if d["loopbacks"]:
            parts.append(f"loopbacks {_names(d['loopbacks'])}")
        if d["as"]:
            parts.append(f"BGP AS {'/'.join(d['as'])}" + (f", router-id {', '.join(d['router_ids'])}" if d["router_ids"] else ""))
            bgp = f"{d['ibgp']} iBGP and {d['ebgp']} eBGP neighbor entries"
            if d["rr_clients"]:
                bgp += f", {d['rr_clients']} of them route-reflector clients"
            if d["dynamic_peers"]:
                bgp += f", {d['dynamic_peers']} dynamic peer ranges"
            parts.append(bgp)
        if d["groups"]:
            parts.append("BGP groups: " + _names([f"{g} ({n})" for g, n in d["groups"].items()]))
        if d["internal_peers"]:
            parts.append(f"BGP with devices in this snapshot: {', '.join(d['internal_peers'])}")
        if d["external"]:
            ases = ", ".join(f"AS {asn} ({n})" for asn, n in list(d["external"].items())[:MAX_EXTERNAL_ASES])
            more = len(d["external"]) - MAX_EXTERNAL_ASES
            parts.append(
                f"BGP neighbors at addresses not on any device in this snapshot: {ases}"
                + (f" and {more} more ASes" if more > 0 else "")
            )
        if d["igp"]:
            parts.append(f"config mentions {', '.join(d['igp'])}")
        lines.append(f"- {d['name']}: " + "; ".join(parts))
    if len(view["devices"]) > MAX_DEVICES:
        lines.append(f"[{len(view['devices']) - MAX_DEVICES} more devices not shown]")

    ranked = sorted(view["devices"], key=lambda d: (-len(d["linked_to"]), -len(d["internal_peers"]), d["name"]))
    lines.append("\nDirectly linked devices per device (shared point-to-point subnets), most connected first:")
    lines += [f"- {d['name']}: {len(d['linked_to'])} ({', '.join(d['linked_to']) or 'none'})" for d in ranked[:MAX_DEVICES]]

    pairs: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for link in view["links"]:
        pairs[(link["a"], link["b"])].append(link)
    lines.append(f"\nLinks ({len(view['links'])} shared subnets between {len(pairs)} device pairs):")
    for (a, b), group in list(pairs.items())[:MAX_PAIRS]:
        examples = "; ".join(f"{l['a_if']} <-> {l['b_if']} {l['subnet']}" for l in group[:LINK_EXAMPLES])
        more = f"; and {len(group) - LINK_EXAMPLES} more" if len(group) > LINK_EXAMPLES else ""
        described = sorted({l["description"] for l in group if l["description"]})
        label = f' described as "{described[0][:60]}"' + (f" and {len(described) - 1} other ways" if len(described) > 1 else "")
        lines.append(f"- {a} <-> {b}: {len(group)} ({examples}{more})" + (label if described else ""))
    if len(pairs) > MAX_PAIRS:
        lines.append(f"[{len(pairs) - MAX_PAIRS} more device pairs not shown]")
    if view["segments"]:
        lines.append("\nSubnets shared by more than two devices:")
        lines += [f"- {s['subnet']}: {', '.join(s['devices'])}" for s in view["segments"][:MAX_SEGMENTS]]
    return "\n".join(lines)
