"""Space arithmetic for checks with exceptions, and how checks use it."""

import ipaddress
from types import SimpleNamespace

import pandas as pd
import pytest

from verifier_worker import checks, spaces


def test_intervals():
    assert spaces.subtract([(0, 10)], [(3, 4)]) == [(0, 2), (5, 10)]
    assert spaces.intersect([(0, 10)], [(5, 20), (30, 40)]) == [(5, 10)]
    first, second = (int(ipaddress.IPv4Address(a)) for a in ("10.30.0.10", "10.30.0.11"))
    assert spaces.parse_ips("10.30.0.10, 10.30.0.11") == [(first, second)]


def test_all_but_tcp_443_covers_every_other_protocol_and_port():
    space = spaces.packet_box("10.20.0.0/24", "10.30.0.10", None, None)
    boxes = spaces.packets_minus(space, [spaces.packet_box(None, "10.30.0.10", "tcp", "443")])
    assert [spaces.header_fields(b) for b in boxes] == [
        {"srcIps": "10.20.0.0/24", "dstIps": "10.30.0.10/32", "ipProtocols": ["!TCP"]},
        {"srcIps": "10.20.0.0/24", "dstIps": "10.30.0.10/32", "ipProtocols": ["TCP"], "dstPorts": "0-442,444-65535"},
    ]


def test_ports_without_a_protocol_mean_tcp_and_udp():
    space = spaces.packet_box(None, "10.30.0.53", None, None)
    boxes = spaces.packets_minus(space, [spaces.packet_box(None, "10.30.0.53", None, "53")])
    fields = [spaces.header_fields(b) for b in boxes]
    assert fields[0] == {"dstIps": "10.30.0.53/32", "ipProtocols": ["!TCP", "!UDP"]}
    assert fields[1] == {"dstIps": "10.30.0.53/32", "ipProtocols": ["TCP", "UDP"], "dstPorts": "0-52,54-65535"}


def test_an_exception_elsewhere_changes_nothing_and_a_total_one_leaves_nothing():
    space = spaces.packet_box("10.20.0.0/24", "10.0.0.0/8", None, None)
    assert spaces.packets_minus(space, [spaces.packet_box(None, "192.0.2.1", None, None)]) == [space]
    assert spaces.packets_minus(space, [spaces.packet_box(None, None, None, None)]) == []


def test_prefix_ranges():
    full = spaces.parse_prefix_range("0.0.0.0/0:0-32")
    left = spaces.prefixes_minus(full, [spaces.parse_prefix_range("198.51.100.0/24")])
    # Every length but 24 stays whole; at /24 everything except the one prefix.
    assert left[:2] == ["0.0.0.0/0:0-23", "0.0.0.0/0:25-32"]
    assert "198.51.100.0/24:24-24" not in left and "198.51.101.0/24:24-24" in left
    assert len(left) == 2 + 24
    assert spaces.prefixes_minus(spaces.parse_prefix_range("10.0.0.0/8:16-32"), [spaces.parse_prefix_range("10.0.0.0/8:8-32")]) == []
    for bad in ("10.0.0.0/8:8-32,8.8.8.0/24", "10.0.0.0/8:4-32", "nope"):
        with pytest.raises(spaces.SpaceError):
            spaces.parse_prefix_range(bad)


class FakeBatfish:
    """Records each question's constraints; reports a counterexample for the first matching space."""

    def __init__(self, leak=lambda kwargs: False):
        self.calls, self.leak = [], leak
        self.q = self

    def __getattr__(self, name):
        def question(**kwargs):
            self.calls.append((name, kwargs))
            if name == "searchFilters" and self.leak(kwargs):
                rows = [{"Flow": SimpleNamespace(ipProtocol="UDP", srcIp="10.20.0.1", srcPort=1, dstIp="10.30.0.10", dstPort=33434),
                         "Line_Content": "45 permit udp any host 10.30.0.10", "Action": "PERMIT"}]  # fmt: skip
            else:
                rows = []
            return SimpleNamespace(answer=lambda **_: SimpleNamespace(frame=lambda: pd.DataFrame(rows)))

        return question


INVENTORY = {"edge": {"filters": ["GUEST-IN"], "policies": ["OUT"], "interfaces": []}}
ACL = {"requirement": "R1", "kind": "filter", "node": "edge", "filter": "GUEST-IN", "expect": "deny", "description": "d",
       "src_ips": "10.20.0.0/24", "dst_ips": "10.30.0.10", "exceptions": [{"dst_ips": "10.30.0.10", "protocol": "tcp", "dst_ports": "443"}]}  # fmt: skip


def run(bf, spec):
    check = checks.Check(**spec)
    assert checks.spec_errors(check, INVENTORY) == []
    return checks._run(bf, check, "snap")


def test_a_filter_check_with_exceptions_searches_every_remaining_piece():
    bf = FakeBatfish()
    holds, evidence = run(bf, ACL)
    assert holds and "outside the 1 exception(s), is permitted" in evidence
    protocols = [kwargs["headers"].ipProtocols for _, kwargs in bf.calls]
    assert protocols == [["!TCP"], ["TCP"]]

    leaky = FakeBatfish(leak=lambda kwargs: kwargs["headers"].ipProtocols == ["!TCP"])
    holds, evidence = run(leaky, ACL)
    assert not holds and evidence.startswith("UDP 10.20.0.1:1 -> 10.30.0.10:33434 is permitted by: 45 permit udp")


def test_a_route_policy_check_with_exceptions_asks_one_exact_question():
    bf = FakeBatfish()
    spec = {"requirement": "R1", "kind": "route_policy", "node": "edge", "policy": "OUT", "expect": "deny", "description": "d",
            "prefix": "0.0.0.0/0:0-32", "exceptions": [{"prefix": "198.51.100.0/24"}, {"communities": "65001:100"}]}  # fmt: skip
    holds, _ = run(bf, spec)
    constraints = bf.calls[0][1]["inputConstraints"]
    assert holds and constraints.communities == ["!65001:100"]
    assert "198.51.100.0/24:24-24" not in constraints.prefix and "0.0.0.0/0:0-23" in constraints.prefix


def test_malformed_spaces_and_exceptions_are_caught_before_running():
    errors = lambda **f: checks.spec_errors(checks.Check(**{**ACL, **f}), INVENTORY)  # noqa: E731
    assert "not an IPv4 address" in errors(exceptions=[{"dst_ips": "web-server"}])[0]
    assert "tcp, udp or icmp" in errors(exceptions=[{"protocol": "gre"}])[0]
    policy = {"kind": "route_policy", "policy": "OUT", "filter": None, "src_ips": None, "dst_ips": None}
    assert "not one prefix range" in errors(**policy, exceptions=[], prefix="10.0.0.0/8,8.8.8.0/24")[0]
    both = [{"prefix": "1.0.0.0/8", "communities": "1:1"}]
    assert "exactly one of prefix or communities" in errors(**policy, prefix="0.0.0.0/0:0-32", exceptions=both)[0]
    assert any("exceptions apply only" in e for e in errors(kind="route", expect="present", prefix="10.0.0.0/8"))


def test_route_coverage_follows_prefix_range_communities_and_exceptions():
    rfc = checks.Check(requirement="R1", kind="route_policy", node="edge", policy="OUT", expect="deny", description="d",
                       prefix="10.0.0.0/8:8-32")  # fmt: skip
    assert checks.route_covered(rfc, "10.50.0.0/16", ["65001:100"])
    assert not checks.route_covered(rfc, "8.8.8.0/24", ["65001:100"])
    tagged = rfc.model_copy(update={"prefix": "0.0.0.0/0:0-32", "communities": "!65001:100"})
    assert checks.route_covered(tagged, "8.8.8.0/24", []) and not checks.route_covered(tagged, "8.8.8.0/24", ["65001:100"])
    only = rfc.model_copy(update={"prefix": "0.0.0.0/0:0-32", "exceptions": [checks.Carveout(prefix="198.51.100.0/24")]})
    assert not checks.route_covered(only, "198.51.100.0/24", []) and checks.route_covered(only, "198.51.101.0/24", [])


def test_packet_coverage_minus_exceptions():
    check = checks.Check(**ACL)
    covered = checks.covered_packets([check])
    inside = lambda *point: any(spaces.box_contains(b, spaces.point_box(*point)) for b in covered)  # noqa: E731
    assert inside("10.20.0.5", "10.30.0.10", "UDP", 53)
    assert not inside("10.20.0.5", "10.30.0.10", "TCP", 443)  # the exception
    assert inside("10.20.0.5", "10.30.0.10", "TCP", 22)
    assert not inside("10.99.0.5", "10.30.0.10", "UDP", 53)  # another source


def test_after_the_policy_checks_search_for_a_permitted_route_that_leaves_wrong():
    from types import SimpleNamespace

    import pandas as pd

    calls = []
    out_route = SimpleNamespace(localPreference=100, metric=0, communities=[], asPath=[])
    in_route = SimpleNamespace(network="10.0.0.0/8", communities=["65001:100"])

    class Q:
        def searchRoutePolicies(self, **kw):
            calls.append(kw)
            # The first output search (local-pref) finds a route that leaves with local-pref 100.
            leak = kw["action"] == "permit" and kw.get("outputConstraints") is not None and len(calls) == 2
            rows = [{"Input_Route": in_route, "Output_Route": out_route}] if leak else []
            return SimpleNamespace(answer=lambda **_: SimpleNamespace(frame=lambda: pd.DataFrame(rows)))

    spec = {"requirement": "R1", "kind": "route_policy", "node": "edge", "policy": "OUT", "expect": "permit", "description": "d",
            "prefix": "0.0.0.0/0:0-32", "communities": "65001:100", "output_local_pref": 200, "output_communities": "!65001:999"}  # fmt: skip
    holds, evidence = run(SimpleNamespace(q=Q()), spec)
    assert not holds and "leaves with local-pref 100" in evidence and "expects with local-pref 200" in evidence
    assert calls[1]["outputConstraints"].localPreference == "0-199, 201-4294967295"
    errors = checks.spec_errors(checks.Check(**{**spec, "expect": "deny"}), INVENTORY)
    assert any("expect must be permit" in e for e in errors)
    assert checks._not_value(0) == "1-4294967295"
