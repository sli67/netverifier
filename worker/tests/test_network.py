"""The network overview is computed by code from Batfish's tables."""

from types import SimpleNamespace

import pandas as pd

from verifier_worker import network


def iface(node, name):
    return SimpleNamespace(hostname=node, interface=name)


class FakeQuestions:
    tables = {
        "nodeProperties": [
            {"Node": "core", "Configuration_Format": "CISCO_IOS"},
            {"Node": "edge", "Configuration_Format": "CISCO_IOS"},
            {"Node": "leaf", "Configuration_Format": "JUNIPER"},
        ],
        "interfaceProperties": [
            {"Interface": iface("core", "Gi0/0"), "Active": True, "All_Prefixes": ["10.0.0.1/31"], "VRF": "default",
             "Description": "BACKBONE to edge"},
            {"Interface": iface("core", "Gi0/1"), "Active": True, "All_Prefixes": ["10.0.0.3/31"], "VRF": "default"},
            {"Interface": iface("core", "Lo0"), "Active": True, "All_Prefixes": ["10.255.0.1/32"], "VRF": "default"},
            {"Interface": iface("edge", "Gi0/0"), "Active": True, "All_Prefixes": ["10.0.0.0/31"], "VRF": "default"},
            {"Interface": iface("edge", "Gi0/9"), "Active": True, "All_Prefixes": ["203.0.113.1/30"], "VRF": "cust"},
            {"Interface": iface("leaf", "xe-0/0/0"), "Active": True, "All_Prefixes": ["10.0.0.2/31"], "VRF": "default"},
            {"Interface": iface("leaf", "xe-0/0/1"), "Active": False, "All_Prefixes": ["10.9.9.1/31"], "VRF": "default"},
        ],
        "bgpProcessConfiguration": [
            {"Node": "core", "Router_ID": "10.255.0.1"},
            {"Node": "edge", "Router_ID": "0.0.0.0"},
        ],
        "bgpPeerConfiguration": [
            {"Node": "core", "Local_AS": 65001, "Remote_AS": "65001", "Remote_IP": "10.0.0.0", "Route_Reflector_Client": True,
             "Peer_Group": "IBGP"},
            {"Node": "edge", "Local_AS": 65001, "Remote_AS": "65001", "Remote_IP": "10.0.0.1", "Route_Reflector_Client": False},
            {"Node": "edge", "Local_AS": 65001, "Remote_AS": "64500", "Remote_IP": "203.0.113.2", "Route_Reflector_Client": False},
            {"Node": "edge", "Local_AS": 65001, "Remote_AS": "64500", "Remote_IP": "198.51.100.0/24", "Route_Reflector_Client": False},
        ],
    }

    def __getattr__(self, name):
        rows = self.tables[name]
        return lambda **_: SimpleNamespace(answer=lambda: SimpleNamespace(frame=lambda: pd.DataFrame(rows)))


BF = SimpleNamespace(q=FakeQuestions())


def test_overview_finds_links_peers_and_the_most_connected_device():
    files = {"core.cfg": "router isis\n", "edge.cfg": "router bgp 65001\n"}
    parsed = [{"path": "core.cfg", "nodes": ["core"]}, {"path": "edge.cfg", "nodes": ["edge"]}]
    view = network.overview(BF, files, parsed)
    devices = {d["name"]: d for d in view["devices"]}

    # Links come from shared subnets of active interfaces; /32s and inactive interfaces are not links.
    assert [(l["a"], l["b"], l["subnet"]) for l in view["links"]] == [
        ("core", "edge", "10.0.0.0/31"),
        ("core", "leaf", "10.0.0.2/31"),
    ]
    assert devices["core"]["linked_to"] == ["edge", "leaf"]
    # A neighbor is internal when its address is on another device in the snapshot.
    assert devices["edge"]["internal_peers"] == ["core"]
    assert devices["edge"]["external"] == {"64500": 1}
    assert devices["edge"]["dynamic_peers"] == 1 and devices["edge"]["ebgp"] == 1 and devices["edge"]["ibgp"] == 1
    assert devices["core"]["rr_clients"] == 1
    assert devices["edge"]["router_ids"] == [] and devices["edge"]["vrfs"] == ["cust"]
    assert devices["core"]["loopbacks"] == ["10.255.0.1"] and devices["core"]["groups"] == {"IBGP": 1}
    assert view["links"][0]["description"] == "BACKBONE to edge"
    assert devices["core"]["igp"] == ["isis"] and devices["edge"]["igp"] == []

    text = network.render(view)
    assert "- core: 2 (edge, leaf)" in text.split("most connected first:")[1].splitlines()[1]
    assert '- core <-> edge: 1 (Gi0/0 <-> Gi0/0 10.0.0.0/31) described as "BACKBONE to edge"' in text
    assert "loopbacks 10.255.0.1" in text and "BGP groups: IBGP (1)" in text
    assert "not on any device in this snapshot: AS 64500 (1)" in text


def test_render_handles_an_empty_view():
    assert network.render(None) == "" and network.render({"devices": []}) == ""
