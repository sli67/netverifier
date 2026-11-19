"""Running a snapshot in a Containerlab lab, and comparing the live routers with Batfish.

The lab copy of each config is the snapshot's config with its physical
interfaces renamed to the lab image's ports (IOL calls them Ethernet0/1,
Ethernet0/2, ...; Ethernet0/0 is the lab's management port). Nothing else in a
config is changed. Links are the subnets two devices share, as found by
network.overview.

Live state is read over SSH and compared with Batfish's prediction for the same
configs: which BGP sessions are established, and which best BGP routes each
device has. A deployment passes when the live lab matches the prediction.
"""

import ipaddress
import json
import logging
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("worker")

# Interfaces that exist in software on any Cisco IOS device, so they need no lab port.
VIRTUAL = re.compile(r"^(loopback|tunnel|vlan|port-channel|null|bdi|nve|virtual)", re.I)
INTERFACE = re.compile(r"^interface\s+(\S+)", re.M)
# The snapshot formats each kind of lab router can run.
FORMATS = {"cisco_iol": ("CISCO_IOS",), "frr": ("CUMULUS_CONCATENATED",)}
# A pinned image for kinds that have a free one.
DEFAULT_IMAGES = {"frr": "quay.io/frrouting/frr:10.2.1"}
# FRR runs only the daemons listed here; bgpd is off in the image's own file.
FRR_DAEMONS = """zebra=yes
bgpd=yes
staticd=yes
vtysh_enable=yes
zebra_options="  -A 127.0.0.1 -s 90000000"
bgpd_options="   -A 127.0.0.1"
staticd_options="-A 127.0.0.1"
"""
# Without this file FRR's boot-time loader stops part way through frr.conf and
# silently drops later lines (seen: a route-map's "match community" missing).
FRR_VTYSH = "service integrated-vtysh-config\n"


class LabError(Exception):
    """The lab could not be set up or read; the message says why in terms a user can act on."""


# The lab's own management network. Containerlab's default (172.20.20.0/24)
# collides with networks Docker Compose projects often create.
DEFAULT_MGMT_SUBNET = "172.31.255.0/24"


def mgmt(subnet: str) -> dict[str, str]:
    """The topology's management network: a name and subnet of its own, and a matching IPv6 range."""
    net = ipaddress.ip_network(subnet, strict=False)
    a, b, c, _ = str(net.network_address).split(".")
    return {"network": "verifier-lab-mgmt", "ipv4-subnet": str(net), "ipv6-subnet": f"3fff:{a}:{b}:{c}::/64"}


# --- configs and topology ---------------------------------------------------


def iol_port(number: int) -> str:
    """The IOL name of Linux interface eth<number>: eth1 is Ethernet0/1, eth4 is Ethernet1/0."""
    return f"Ethernet{number // 4}/{number % 4}"


def interface_map(text: str) -> dict[str, tuple[str, str]]:
    """Physical interfaces in a config, in order, to (lab interface name, Linux interface)."""
    bases: list[str] = []
    for name in INTERFACE.findall(text):
        base = name.split(".")[0]
        if not VIRTUAL.match(base) and base not in bases:
            bases.append(base)
    return {base: (iol_port(n), f"eth{n}") for n, base in enumerate(bases, start=1)}


def translate(text: str, mapping: dict[str, tuple[str, str]]) -> str:
    """The config with each physical interface renamed to its lab port, subinterfaces included."""
    if not mapping:
        return text
    names = sorted(mapping, key=len, reverse=True)
    pattern = re.compile(r"(?<![\w/])(" + "|".join(re.escape(n) for n in names) + r")(?![\w/])")
    return pattern.sub(lambda m: mapping[m.group(1)][0], text)


def frr_parts(text: str) -> tuple[dict[str, list[str]], str]:
    """Split a Cumulus-layout file into its interface addresses and its frr.conf.

    The layout is: hostname, "# This file describes the network interfaces" and
    the interfaces file, "# ports.conf --" and ports.conf, then frr.conf from its
    "frr version" line.
    """
    addresses: dict[str, list[str]] = {}
    current = None
    marker = text.find("# This file describes the network interfaces")
    end = text.find("# ports.conf --")
    for line in (text[marker:end] if marker >= 0 else "").splitlines():
        words = line.split()
        if len(words) >= 2 and words[0] == "iface":
            current = words[1]
            addresses.setdefault(current, [])
        elif len(words) >= 2 and words[0] == "address" and current:
            addresses[current].append(words[1])
    start = re.search(r"^frr version", text, re.M)
    return addresses, text[start.start():] if start else ""


def frr_lab_config(text: str) -> str:
    """frr.conf for a lab router: the snapshot's frr.conf, with the interface addresses zebra should set.

    On a Cumulus switch ifupdown sets addresses from /etc/network/interfaces; in
    the lab container nothing reads that file, so zebra sets them instead.
    """
    addresses, conf = frr_parts(text)
    stanzas = "".join(
        f"interface {name}\n" + "".join(f" ip address {a}\n" for a in addrs) + "!\n"
        for name, addrs in sorted(addresses.items())
        if addrs
    )
    return conf.rstrip("\n") + "\n!\n" + stanzas


def frr_ports(text: str) -> dict[str, tuple[str, str]]:
    """FRR lab routers use their Linux interface names directly, so ports map to themselves."""
    return {name: (name, name) for name in frr_parts(text)[0] if name != "lo"}


def lab_links(links: list[dict[str, str]], maps: dict[str, dict[str, tuple[str, str]]]) -> list[dict[str, Any]]:
    """Containerlab links for the subnets two devices share, one per pair of physical ports."""
    out, seen = [], set()
    for link in links:
        ends = []
        for node, names in ((link["a"], link["a_if"]), (link["b"], link["b_if"])):
            base = names.split(",")[0].strip().split(".")[0]
            port = maps.get(node, {}).get(base)
            if port is None:
                break
            ends.append(f"{node}:{port[1]}")
        if len(ends) == 2 and tuple(ends) not in seen:
            seen.add(tuple(ends))
            out.append({"endpoints": ends})
    return out


def lab_name(snapshot_id: str) -> str:
    return f"verifier-{snapshot_id[:8]}"


def topology(
    name: str, kind: str, image: str, configs: dict[str, str], links: list[dict[str, Any]], subnet: str = DEFAULT_MGMT_SUBNET
) -> dict:
    """A Containerlab topology. configs maps node name to its startup-config file path."""
    nodes = {node: {"kind": kind, "image": image, "startup-config": path} for node, path in sorted(configs.items())}
    return {"name": name, "mgmt": mgmt(subnet), "topology": {"nodes": nodes, "links": links}}


def write_lab(
    directory: Path,
    name: str,
    kind: str,
    image: str,
    texts: dict[str, str],
    links: list[dict[str, str]],
    tag: str,
    subnet: str = DEFAULT_MGMT_SUBNET,
) -> tuple[Path, dict[str, dict[str, str]]]:
    """Write the lab's configs and topology file. texts maps node name to its config text.

    Returns the topology path and, per node, how its interfaces were renamed.
    """
    directory.mkdir(parents=True, exist_ok=True)
    if kind == "frr":
        return _write_frr(directory, name, image, texts, links, tag, subnet)
    maps = {node: interface_map(text) for node, text in texts.items()}
    configs = {}
    for node, text in texts.items():
        # A ".partial" config is added to the image's own base config, which sets up management and SSH.
        path = directory / f"{node}.{tag}.partial.cfg"
        path.write_text(translate(text, maps[node]), encoding="utf-8")
        configs[node] = str(path)
    topo = topology(name, kind, image, configs, lab_links(links, maps), subnet)
    topo_path = directory / f"{name}.clab.yml"
    # JSON is valid YAML, so no YAML library is needed.
    topo_path.write_text(json.dumps(topo, indent=2), encoding="utf-8")
    renamed = {node: {orig: port[0] for orig, port in m.items()} for node, m in maps.items()}
    return topo_path, renamed


def _write_frr(
    directory: Path, name: str, image: str, texts: dict[str, str], links: list[dict[str, str]], tag: str, subnet: str
) -> tuple[Path, dict[str, dict[str, str]]]:
    daemons, vtysh = directory / "daemons", directory / "vtysh.conf"
    daemons.write_text(FRR_DAEMONS, encoding="utf-8")
    vtysh.write_text(FRR_VTYSH, encoding="utf-8")
    nodes = {}
    for node, text in sorted(texts.items()):
        path = directory / f"{node}.{tag}.frr.conf"
        path.write_text(frr_lab_config(text), encoding="utf-8")
        binds = [f"{path}:/etc/frr/frr.conf", f"{daemons}:/etc/frr/daemons", f"{vtysh}:/etc/frr/vtysh.conf"]
        nodes[node] = {"kind": "linux", "image": image, "binds": binds}
    maps = {node: frr_ports(text) for node, text in texts.items()}
    topo = {"name": name, "mgmt": mgmt(subnet), "topology": {"nodes": nodes, "links": lab_links(links, maps)}}
    topo_path = directory / f"{name}.clab.yml"
    topo_path.write_text(json.dumps(topo, indent=2), encoding="utf-8")
    return topo_path, {}


# --- running the lab --------------------------------------------------------

Runner = Callable[[list[str]], subprocess.CompletedProcess]


def _run(command: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(command, capture_output=True, text=True, timeout=900)


def deploy(containerlab: str, topo_path: Path, run: Runner = _run) -> None:
    """Boot the whole lab from its topology, replacing whatever ran before."""
    if shutil.which(containerlab) is None and run is _run:
        raise LabError(f"Containerlab ({containerlab}) is not installed on the deploy worker's machine.")
    result = run([containerlab, "deploy", "-t", str(topo_path), "--reconfigure"])
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip().splitlines()[-6:]
        raise LabError("Containerlab could not deploy the lab: " + " | ".join(tail))


def addresses(name: str, nodes: list[str], run: Runner = _run, kind: str = "cisco_iol") -> dict[str, str]:
    """How to reach each node: its management address, or for FRR its container name (read with docker exec)."""
    if kind == "frr":
        return {node: f"clab-{name}-{node}" for node in nodes}
    out = {}
    for node in nodes:
        container = f"clab-{name}-{node}"
        result = run(["docker", "inspect", "-f", "{{json .NetworkSettings.Networks}}", container])
        if result.returncode != 0:
            raise LabError(f"lab device {container} is not running")
        networks = json.loads(result.stdout or "{}")
        ip = next((n.get("IPAddress") for n in networks.values() if n.get("IPAddress")), None)
        if not ip:
            raise LabError(f"lab device {container} has no management address")
        out[node] = ip
    return out


# --- reading live state -----------------------------------------------------


def classful(network: str) -> str:
    """IOS shows a classful network without its mask (192.0.2.0 meaning /24)."""
    if "/" in network:
        return str(ipaddress.ip_network(network, strict=False))
    first = int(network.split(".")[0])
    length = 8 if first < 128 else 16 if first < 192 else 24
    return f"{network}/{length}"


def parse_live(summary: list[dict[str, str]], table: list[dict[str, str]]) -> dict[str, list[str]]:
    """Established sessions and best BGP routes from parsed 'show ip bgp summary' and 'show ip bgp'."""
    sessions = sorted(r["bgp_neighbor"] for r in summary if r.get("state_or_prefixes_received", "").isdigit())
    best = sorted({classful(r["network"]) for r in table if r.get("path_selection") == ">" and r.get("network")})
    return {"established": sessions, "bgp_best": best}


def read_device(host: str, username: str, password: str) -> dict[str, list[str]]:
    from netmiko import ConnectHandler

    device = {"device_type": "cisco_ios", "host": host, "username": username, "password": password,
              "conn_timeout": 20, "auth_timeout": 20}  # fmt: skip
    with ConnectHandler(**device) as connection:
        summary = connection.send_command("show ip bgp summary", use_textfsm=True)
        table = connection.send_command("show ip bgp", use_textfsm=True)
    # With no BGP output the parser returns the raw text rather than rows.
    return parse_live(summary if isinstance(summary, list) else [], table if isinstance(table, list) else [])


def parse_frr(summary: dict, table: dict) -> dict[str, list[str]]:
    """Established sessions and best BGP routes from FRR's 'show bgp ipv4 unicast summary json' and '... json'."""
    peers = summary.get("peers") or summary.get("ipv4Unicast", {}).get("peers", {})
    sessions = sorted(ip for ip, peer in peers.items() if peer.get("state") == "Established")

    def best(path: dict) -> bool:
        flag = path.get("bestpath")
        return flag is True or (isinstance(flag, dict) and bool(flag.get("overall")))

    routes = table.get("routes", {})
    return {"established": sessions, "bgp_best": sorted(p for p, paths in routes.items() if any(best(x) for x in paths))}


def read_frr(container: str, username: str = "", password: str = "", run: Runner = _run) -> dict[str, list[str]]:
    """Read an FRR lab router through docker exec; it needs no login."""
    out = []
    for command in ("show bgp ipv4 unicast summary json", "show bgp ipv4 unicast json"):
        result = run(["docker", "exec", container, "vtysh", "-c", command])
        if result.returncode != 0:
            raise LabError(f"could not read {container}: {(result.stderr or result.stdout).strip()[:200]}")
        out.append(json.loads(result.stdout or "{}"))
    return parse_frr(out[0], out[1])


def observe(hosts: dict[str, str], username: str, password: str, read: Callable = read_device) -> dict[str, dict]:
    out = {}
    for node, host in sorted(hosts.items()):
        try:
            out[node] = read(host, username, password)
        except Exception as error:  # noqa: BLE001 - one unreachable device is reported, not fatal
            out[node] = {"error": f"{type(error).__name__}: {error}"[:300], "established": [], "bgp_best": []}
    return out


# --- Batfish's prediction, and the comparison --------------------------------


def predict(bf: Any, snapshot: str) -> dict[str, dict[str, list[str]]]:
    """Established sessions and best BGP routes per device, as Batfish computes them."""
    out: dict[str, dict[str, list[str]]] = {}
    sessions = bf.q.bgpSessionStatus().answer(snapshot=snapshot).frame()
    for _, row in sessions.iterrows():
        entry = out.setdefault(row["Node"], {"established": [], "bgp_best": []})
        if row["Established_Status"] == "ESTABLISHED":
            entry["established"].append(str(row["Remote_IP"]))
    rib = bf.q.bgpRib(vrfs="default").answer(snapshot=snapshot).frame()
    for _, row in rib.iterrows():
        if "BEST" in (row["Status"] or []):
            out.setdefault(row["Node"], {"established": [], "bgp_best": []})["bgp_best"].append(str(row["Network"]))
    return {node: {k: sorted(set(v)) for k, v in entry.items()} for node, entry in out.items()}


def compare(predicted: dict[str, dict], live: dict[str, dict]) -> dict[str, Any]:
    """Where the live lab differs from Batfish's prediction. ok is True when it does not."""
    nodes = {}
    for node in sorted(set(predicted) | set(live)):
        want = predicted.get(node, {"established": [], "bgp_best": []})
        have = live.get(node, {"established": [], "bgp_best": []})
        diff = {
            "sessions_missing": sorted(set(want["established"]) - set(have["established"])),
            "sessions_unexpected": sorted(set(have["established"]) - set(want["established"])),
            "routes_missing": sorted(set(want["bgp_best"]) - set(have["bgp_best"])),
            "routes_unexpected": sorted(set(have["bgp_best"]) - set(want["bgp_best"])),
        }
        if have.get("error"):
            diff["error"] = have["error"]
        nodes[node] = diff
    ok = all(not any(d.values()) for d in nodes.values())
    return {"ok": ok, "nodes": nodes}


def converge(
    hosts: dict[str, str],
    predicted: dict[str, dict],
    username: str,
    password: str,
    timeout: float,
    interval: float = 15,
    settle: int = 4,
    read: Callable = read_device,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, dict]:
    """Read the lab until it matches the prediction, or has stopped changing, or time runs out.

    Routers take a while to boot and BGP a while to settle, so a single early
    reading would report differences that are only delays. "Stopped changing"
    means `settle` readings in a row were identical, about a minute by default.
    """
    deadline = clock() + timeout
    previous, same = None, 0
    while True:
        live = observe(hosts, username, password, read)
        if compare(predicted, live)["ok"]:
            return live
        booted = not any(d.get("error") for d in live.values())
        same = same + 1 if booted and live == previous else 0
        if same + 1 >= settle or clock() >= deadline:
            return live
        previous = live
        sleep(interval)
