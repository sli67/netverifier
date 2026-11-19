"""The lab: renaming interfaces, the topology, reading IOS output, and comparing with Batfish."""

import json
from pathlib import Path

from ntc_templates.parse import parse_output

from verifier_worker import lab

EDGE = """hostname edge1
interface GigabitEthernet0/0
 ip address 203.0.113.2 255.255.255.252
interface GigabitEthernet0/1
 ip address 203.0.113.5 255.255.255.252
interface GigabitEthernet0/1.20
 encapsulation dot1Q 20
interface Loopback0
 ip address 10.255.0.1 255.255.255.255
router bgp 65001
 neighbor 10.255.0.2 update-source Loopback0
 neighbor 203.0.113.6 update-source GigabitEthernet0/1
"""


def test_physical_interfaces_get_lab_ports_and_are_renamed_everywhere():
    mapping = lab.interface_map(EDGE)
    assert mapping == {"GigabitEthernet0/0": ("Ethernet0/1", "eth1"), "GigabitEthernet0/1": ("Ethernet0/2", "eth2")}
    text = lab.translate(EDGE, mapping)
    assert "interface Ethernet0/1\n" in text and "interface Ethernet0/2.20" in text
    assert "update-source Ethernet0/2" in text and "update-source Loopback0" in text
    assert "GigabitEthernet" not in text
    assert lab.iol_port(4) == "Ethernet1/0" and lab.iol_port(3) == "Ethernet0/3"


def test_topology_links_the_ports_of_shared_subnets(tmp_path: Path):
    texts = {"edge1": EDGE, "isp-a": "hostname isp-a\ninterface GigabitEthernet0/0\n ip address 203.0.113.6 255.255.255.252\n"}
    links = [{"a": "edge1", "a_if": "GigabitEthernet0/1", "b": "isp-a", "b_if": "GigabitEthernet0/0", "subnet": "x"},
             {"a": "edge1", "a_if": "GigabitEthernet0/1.20", "b": "isp-a", "b_if": "GigabitEthernet0/0", "subnet": "y"},
             {"a": "edge1", "a_if": "Loopback0", "b": "core", "b_if": "Loopback0", "subnet": "z"}]  # fmt: skip
    topo_path, renamed = lab.write_lab(tmp_path, "verifier-abc", "cisco_iol", "img:1", texts, links, "baseline")
    topo = json.loads(topo_path.read_text())
    # Two subnets on the same pair of ports are one cable; a loopback is no cable at all.
    assert topo["topology"]["links"] == [{"endpoints": ["edge1:eth2", "isp-a:eth1"]}]
    node = topo["topology"]["nodes"]["edge1"]
    assert node["kind"] == "cisco_iol" and node["startup-config"].endswith("edge1.baseline.partial.cfg")
    assert "interface Ethernet0/2" in Path(node["startup-config"]).read_text()
    assert renamed["isp-a"] == {"GigabitEthernet0/0": "Ethernet0/1"}


SUMMARY = """BGP router identifier 10.255.0.1, local AS number 65001
BGP table version is 7, main routing table version 7

Neighbor        V           AS MsgRcvd MsgSent   TblVer  InQ OutQ Up/Down  State/PfxRcd
10.255.0.2      4        65001       9      11        7    0    0 00:05:12        0
203.0.113.6     4        64500       4       6        0    0    0 00:00:12 Active
"""
TABLE = """     Network          Next Hop            Metric LocPrf Weight Path
 *>   10.50.0.0/16     203.0.113.1              0    200      0 65010 i
 *    198.51.100.0     203.0.113.6              0             0 64500 65001 i
 *>                    0.0.0.0                  0         32768 i
 *>   192.0.2.0        203.0.113.1              0    200      0 65010 i
"""


def test_live_state_from_ios_output():
    summary = parse_output(platform="cisco_ios", command="show ip bgp summary", data=SUMMARY)
    table = parse_output(platform="cisco_ios", command="show ip bgp", data=TABLE)
    # Active is not established; classful networks get their mask; only best paths count.
    assert lab.parse_live(summary, table) == {
        "established": ["10.255.0.2"],
        "bgp_best": ["10.50.0.0/16", "192.0.2.0/24", "198.51.100.0/24"],
    }


def test_compare_names_every_difference():
    predicted = {"isp-a": {"established": ["203.0.113.5"], "bgp_best": ["192.0.2.0/24"]}}
    assert lab.compare(predicted, predicted)["ok"]
    live = {"isp-a": {"established": [], "bgp_best": ["10.50.0.0/16", "192.0.2.0/24"]}}
    result = lab.compare(predicted, live)
    assert not result["ok"]
    assert result["nodes"]["isp-a"] == {"sessions_missing": ["203.0.113.5"], "sessions_unexpected": [],
                                        "routes_missing": [], "routes_unexpected": ["10.50.0.0/16"]}  # fmt: skip


def test_converge_waits_for_the_prediction_or_a_settled_lab():
    predicted = {"r1": {"established": ["10.0.0.2"], "bgp_best": []}}
    readings = iter([{"established": [], "bgp_best": []}] * 2 + [{"established": ["10.0.0.2"], "bgp_best": []}])
    now = [0.0]
    live = lab.converge({"r1": "h"}, predicted, "u", "p", timeout=600, read=lambda *a: next(readings),
                        clock=lambda: now[0], sleep=lambda s: now.__setitem__(0, now[0] + s))  # fmt: skip
    assert live["r1"]["established"] == ["10.0.0.2"] and now[0] == 30

    # A lab that never matches is reported once it has stopped changing, not after the full timeout.
    now = [0.0]
    stuck = lambda *a: {"established": [], "bgp_best": []}  # noqa: E731
    lab.converge({"r1": "h"}, predicted, "u", "p", timeout=600, read=stuck, settle=4,
                 clock=lambda: now[0], sleep=lambda s: now.__setitem__(0, now[0] + s))  # fmt: skip
    assert now[0] == 45

    # An unreachable device is retried until the timeout.
    now = [0.0]
    def down(*a):
        raise OSError("connection refused")
    live = lab.converge({"r1": "h"}, predicted, "u", "p", timeout=100, read=down,
                        clock=lambda: now[0], sleep=lambda s: now.__setitem__(0, now[0] + s))  # fmt: skip
    assert "connection refused" in live["r1"]["error"] and now[0] >= 100


FRR = """edge1
# This file describes the network interfaces
auto lo
iface lo inet loopback

auto eth1
iface eth1
    address 203.0.113.2/30

auto eth2
iface eth2
    address 203.0.113.5/30

# ports.conf --

frr version 10.2.1
hostname edge1
router bgp 65001
 neighbor 203.0.113.6 remote-as 64500
!
"""


def test_frr_layout_is_split_and_addresses_go_to_zebra(tmp_path: Path):
    addresses, conf = lab.frr_parts(FRR)
    assert addresses == {"lo": [], "eth1": ["203.0.113.2/30"], "eth2": ["203.0.113.5/30"]}
    assert conf.startswith("frr version 10.2.1") and "# ports.conf" not in conf
    lab_conf = lab.frr_lab_config(FRR)
    assert "interface eth1\n ip address 203.0.113.2/30\n!" in lab_conf and "router bgp 65001" in lab_conf

    isp = "isp-a\n# This file describes the network interfaces\nauto eth1\niface eth1\n    address 203.0.113.6/30\n# ports.conf --\nfrr version 10.2.1\n"
    links = [{"a": "edge1", "a_if": "eth2", "b": "isp-a", "b_if": "eth1", "subnet": "x"}]
    topo_path, renamed = lab.write_lab(tmp_path, "verifier-x", "frr", "quay.io/frrouting/frr:10.2.1", {"edge1": FRR, "isp-a": isp}, links, "baseline")
    topo = json.loads(topo_path.read_text())
    node = topo["topology"]["nodes"]["edge1"]
    assert node["kind"] == "linux" and node["binds"][0].endswith("edge1.baseline.frr.conf:/etc/frr/frr.conf")
    assert "bgpd=yes" in (tmp_path / "daemons").read_text()
    assert any(b.endswith(":/etc/frr/vtysh.conf") for b in node["binds"])
    assert topo["topology"]["links"] == [{"endpoints": ["edge1:eth2", "isp-a:eth1"]}] and renamed == {}


def test_frr_live_state_from_json():
    summary = {"peers": {"203.0.113.6": {"state": "Established"}, "10.0.0.9": {"state": "Active"}}}
    table = {"routes": {"8.8.8.0/24": [{"valid": True, "bestpath": True}], "9.9.9.0/24": [{"valid": True}]}}
    assert lab.parse_frr(summary, table) == {"established": ["203.0.113.6"], "bgp_best": ["8.8.8.0/24"]}
    assert lab.parse_frr({"ipv4Unicast": summary}, {"routes": {"1.0.0.0/8": [{"bestpath": {"overall": True}}]}})["bgp_best"] == ["1.0.0.0/8"]
    assert lab.addresses("verifier-x", ["edge1"], kind="frr") == {"edge1": "clab-verifier-x-edge1"}
