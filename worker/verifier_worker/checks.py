"""Checks that code runs on Batfish, with no model involved in the verdict.

A model writes each check's specification (which device, which filter, which
packets, what is expected). Everything after that is code: the specification is
validated against the parsed model, run, and judged. The same checks run on the
current configs and on a candidate, and `side_effects` compares the two models
to find what a candidate changed beyond what was asked.
"""

import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from . import spaces

MAX_ITEMS = 8

Expect = Literal["permit", "deny", "present", "absent", "established", "success", "failure"]

EXPECTATIONS: dict[str, tuple[str, ...]] = {
    "filter": ("permit", "deny"),
    "route_policy": ("permit", "deny"),
    "route": ("present", "absent"),
    "bgp_session": ("established",),
    "reachability": ("success", "failure"),
}


class Carveout(BaseModel):
    """Part of a check's space that the check does not cover. Code subtracts it exactly."""

    src_ips: str | None = Field(None, description="filter/reachability: source addresses")
    dst_ips: str | None = Field(None, description="filter/reachability: destination addresses")
    protocol: str | None = Field(None, description="filter/reachability: tcp, udp or icmp; omit for any")
    dst_ports: str | None = Field(None, description="filter/reachability: destination ports, e.g. 443 or 53")
    prefix: str | None = Field(None, description="route_policy: prefix range such as 198.51.100.0/24")
    communities: str | None = Field(None, description="route_policy: routes carrying this community, e.g. 65001:100")


class Check(BaseModel):
    """One property of the network that Batfish can decide."""

    requirement: str = Field(description="id of the requirement this check supports")
    kind: Literal["filter", "route_policy", "route", "bgp_session", "reachability"]
    description: str = Field(description="one sentence saying what must be true")
    node: str = Field(description="exact device name")
    expect: Expect
    filter: str | None = Field(None, description="filter kind: exact ACL or firewall filter name")
    policy: str | None = Field(None, description="route_policy kind: exact route-map or policy name")
    src_ips: str | None = Field(None, description="source addresses, e.g. 10.20.0.0/24")
    dst_ips: str | None = Field(None, description="destination addresses, e.g. 10.30.0.10")
    protocol: str | None = Field(None, description="tcp, udp or icmp; omit for any IP protocol")
    dst_ports: str | None = Field(None, description="destination ports, e.g. 443 or 1-1023")
    prefix: str | None = Field(
        None, description="route_policy: prefix space such as 10.0.0.0/8:8-32. route: the exact network"
    )
    communities: str | None = Field(
        None,
        description="route_policy: only routes carrying this community, e.g. 65001:100, or with a leading ! "
        "only routes NOT carrying it, e.g. !65001:100; omit for routes with any communities",
    )
    enter_interface: str | None = Field(None, description="reachability: interface the traffic enters the node on")
    remote_ip: str | None = Field(None, description="bgp_session: the neighbor's address")
    output_local_pref: int | None = Field(None, description="route_policy permit: every permitted route leaves with this local preference")
    output_med: int | None = Field(None, description="route_policy permit: every permitted route leaves with this MED")
    output_communities: str | None = Field(
        None,
        description="route_policy permit: every permitted route leaves carrying this community (65001:100), "
        "or with a leading ! not carrying it (!65001:100)",
    )
    output_as_path: str | None = Field(
        None,
        description="route_policy permit: every permitted route leaves with an AS path matching this regular "
        "expression over space-separated AS numbers, e.g. ^65001 65001 for a double prepend",
    )
    exceptions: list[Carveout] = Field(
        default_factory=list,
        description="filter, reachability, route_policy: parts of the space this check does not cover, for "
        "'everything except ...'. Code removes them exactly, including other protocols and ports",
    )


# One standard community, optionally negated: routes that do not carry it.
COMMUNITY = re.compile(r"!?\d{1,5}:\d{1,5}")

CHECK_GUIDE = """Check kinds, and what each one decides:
- filter: every packet matching src_ips, dst_ips, protocol and dst_ports is permitted (expect permit) or denied (expect deny) by the named ACL on node. Needs filter. This covers the whole packet space given, not one sample packet.
- route_policy: every BGP route whose prefix is in the prefix space is permitted or denied by the named route-map or policy on node. Needs policy and prefix. Set communities when the requirement is only about routes carrying a community (for example routes tagged as customer routes); without it the check covers routes with any communities, so a permit check on a policy that matches on a community will fail. For routes that do NOT carry a community, prefix it with ! (for example !65001:100 for routes not tagged as customer routes). A deny check with no communities set requires denying the prefix space even for routes that carry every community the policy matches on; write it only when the requirement really is that the prefix space is denied whatever the route carries.
- route: node's routing table has (expect present) or does not have (expect absent) a route for exactly this network. Needs prefix.
- bgp_session: node's BGP session with remote_ip is established. Needs remote_ip. Batfish can only establish sessions whose both ends are in the snapshot.
- reachability: every packet matching the headers that starts at node is delivered (expect success) or is not (expect failure). Set enter_interface so interface ACLs apply to it. Needs dst_ips.
Use one check per distinct property.

After the policy: a route_policy permit check can also say what every permitted route looks like when it leaves the policy: output_local_pref, output_med, output_communities (carries it, or with ! does not) and output_as_path (a regular expression over space-separated AS numbers). Use them for requirements about what a policy sets, adds, removes or prepends: "customer routes get local-pref 200 and community 65001:100" is one permit check with communities scoped to the customer routes, output_local_pref 200 and output_communities 65001:100. Batfish proves it for every route in the space.

Exceptions: for "everything except ...", write ONE check over the whole space and list the carve-outs in exceptions; code removes them exactly, so you do not split ports, protocols or prefixes yourself. Then write one permit check per exception if the exceptions must be permitted.
- filter or reachability: "guests may reach only TCP/443 on 10.30.0.10, all else to 10.0.0.0/8 is denied" is a deny check with src_ips 10.20.0.0/24, dst_ips 10.0.0.0/8, no protocol and no ports, and exceptions [{dst_ips: 10.30.0.10, protocol: tcp, dst_ports: 443}]. Leaving protocol empty covers every protocol (TCP, UDP, ICMP and the rest); a check with protocol tcp says nothing about UDP or ICMP.
- route_policy: "export only our aggregate and customer routes" is a deny check with prefix 0.0.0.0/0:0-32 and exceptions [{prefix: 198.51.100.0/24}, {communities: 65001:100}] (one of prefix or communities per exception). Do not narrow "customer routes" to the customer prefixes you happen to see: say routes carrying the customer community.
- A prefix is one range such as 10.0.0.0/8:8-32 or 192.0.2.0/24. Never a comma-separated list: use exceptions or separate checks."""


def inventory_text(inv: dict[str, dict[str, list[str]]]) -> str:
    lines = []
    for node, parts in sorted(inv.items()):
        lines.append(
            f"- {node}: filters [{', '.join(parts['filters'])}]; "
            f"routing policies [{', '.join(parts['policies'])}]; interfaces [{', '.join(parts['interfaces'])}]"
        )
    return "\n".join(lines)


def spec_errors(check: Check, inv: dict[str, dict[str, list[str]]]) -> list[str]:
    """Problems that would make the check meaningless, such as a name that does not exist.

    A search over a filter that does not exist returns no rows, which would read
    as "holds", so names are confirmed before anything runs.
    """
    errors = []
    label = f"check {check.description!r}"
    if check.expect not in EXPECTATIONS[check.kind]:
        errors.append(f"{label}: expect must be one of {', '.join(EXPECTATIONS[check.kind])} for kind {check.kind}")
    node = inv.get(check.node)
    if node is None:
        errors.append(f"{label}: no device named {check.node!r}. Devices: {', '.join(sorted(inv))}")
        return errors
    if check.kind == "filter":
        if not check.filter or check.filter not in node["filters"]:
            errors.append(f"{label}: {check.node} has no filter {check.filter!r}. It has: {', '.join(node['filters'])}")
    elif check.kind == "route_policy":
        if not check.policy or check.policy not in node["policies"]:
            errors.append(
                f"{label}: {check.node} has no routing policy {check.policy!r}. It has: {', '.join(node['policies'])}"
            )
        if not check.prefix:
            errors.append(f"{label}: prefix is required")
        else:
            errors += _space_errors(label, lambda: spaces.parse_prefix_range(check.prefix))
        if check.communities and not COMMUNITY.fullmatch(check.communities):
            errors.append(f"{label}: communities must be one community such as 65001:100, or !65001:100 for routes without it")
        if has_output(check) and check.expect != "permit":
            errors.append(f"{label}: output_ conditions describe permitted routes, so expect must be permit")
        if check.output_communities and not COMMUNITY.fullmatch(check.output_communities):
            errors.append(f"{label}: output_communities must be one community such as 65001:100, or !65001:100")
        if check.output_as_path:
            try:
                re.compile(check.output_as_path)
            except re.error as error:
                errors.append(f"{label}: output_as_path is not a valid regular expression: {error}")
        for number, e in enumerate(check.exceptions, start=1):
            if bool(e.prefix) == bool(e.communities) or e.src_ips or e.dst_ips or e.protocol or e.dst_ports:
                errors.append(f"{label}: exception {number} must set exactly one of prefix or communities")
            elif e.prefix:
                errors += _space_errors(label, lambda e=e: spaces.parse_prefix_range(e.prefix))
            elif not COMMUNITY.fullmatch(e.communities) or e.communities.startswith("!"):
                errors.append(f"{label}: exception {number} communities must be one community such as 65001:100")
    if check.kind in ("filter", "reachability") and check.exceptions:
        errors += _space_errors(label, lambda: _packet_boxes(check))
    if check.kind not in ("filter", "reachability", "route_policy") and check.exceptions:
        errors.append(f"{label}: exceptions apply only to filter, reachability and route_policy checks")
    if check.kind != "route_policy" and has_output(check):
        errors.append(f"{label}: output_ conditions apply only to route_policy checks")
    if check.kind == "route":
        if not check.prefix:
            errors.append(f"{label}: prefix is required")
    if check.kind == "bgp_session":
        if not check.remote_ip:
            errors.append(f"{label}: remote_ip is required")
    if check.kind == "reachability":
        if not check.dst_ips:
            errors.append(f"{label}: dst_ips is required")
        if check.enter_interface and check.enter_interface not in node["interfaces"]:
            errors.append(
                f"{label}: {check.node} has no interface {check.enter_interface!r}. "
                f"It has: {', '.join(node['interfaces'])}"
            )
    return errors


def _space_errors(label: str, parse) -> list[str]:
    try:
        parse()
    except spaces.SpaceError as error:
        return [f"{label}: {error}"]
    return []


def _packet_boxes(check: Check) -> list[dict]:
    """The check's packet space minus its exceptions, as pieces to search one by one."""
    space = spaces.packet_box(check.src_ips, check.dst_ips, check.protocol, check.dst_ports)
    carveouts = [spaces.packet_box(e.src_ips, e.dst_ips, e.protocol, e.dst_ports) for e in check.exceptions]
    return spaces.packets_minus(space, carveouts)


def _header_spaces(check: Check) -> list[Any]:
    """HeaderConstraints covering exactly the check's space minus its exceptions."""
    from pybatfish.datamodel.flow import HeaderConstraints

    if not check.exceptions:
        return [_headers(check)]
    boxes = _packet_boxes(check)
    if not boxes:
        raise ValueError("the exceptions cover the whole space, so nothing is left to check")
    return [HeaderConstraints(**spaces.header_fields(box)) for box in boxes]


def has_output(check: Check) -> bool:
    """Whether a check says what permitted routes look like after the policy."""
    return any(v is not None for v in (check.output_local_pref, check.output_med, check.output_communities, check.output_as_path))


def _not_value(value: int) -> str:
    """Every 32-bit value except this one, in Batfish's integer-range syntax."""
    parts = ([f"0-{value - 1}"] if value > 0 else []) + ([f"{value + 1}-4294967295"] if value < 4294967295 else [])
    return ", ".join(parts)


def _output_violations(check: Check) -> list[tuple[dict[str, Any], str]]:
    """For each output condition: constraints matching an output route that breaks it, and the condition in words."""
    out = []
    if check.output_local_pref is not None:
        out.append(({"localPreference": _not_value(check.output_local_pref)}, f"with local-pref {check.output_local_pref}"))
    if check.output_med is not None:
        out.append(({"med": _not_value(check.output_med)}, f"with MED {check.output_med}"))
    if check.output_communities:
        community = check.output_communities.lstrip("!")
        absent = check.output_communities.startswith("!")
        out.append(({"communities": [community] if absent else [f"!{community}"]},
                    f"{'not carrying' if absent else 'carrying'} community {community}"))  # fmt: skip
    if check.output_as_path:
        out.append(({"asPath": [f"!/{check.output_as_path}/"]}, f"with an AS path matching {check.output_as_path}"))
    return out


def _route_text(route: Any) -> str:
    text = f"route {route.network}"
    if getattr(route, "communities", None):
        text += f" with communities {', '.join(route.communities)}"
    return text


def _output_text(route: Any) -> str:
    path = " ".join(str(a) for seg in (getattr(route, "asPath", None) or []) for a in seg.get("asns", []))
    return (f"local-pref {route.localPreference}, MED {route.metric}, "
            f"communities [{', '.join(route.communities or [])}], AS path [{path}]")  # fmt: skip


def route_covered(check: Check, network: str, communities: list[str]) -> bool:
    """Whether a route lies in a route_policy check's space: its prefix range and community condition, minus exceptions."""
    try:
        if not spaces.prefix_in(network, spaces.parse_prefix_range(check.prefix or "")):
            return False
        if check.communities:
            wanted, community = not check.communities.startswith("!"), check.communities.lstrip("!")
            if (community in communities) != wanted:
                return False
        for e in check.exceptions:
            if e.prefix and spaces.prefix_in(network, spaces.parse_prefix_range(e.prefix)):
                return False
            if e.communities and e.communities in communities:
                return False
    except (spaces.SpaceError, ValueError):
        return False
    return True


def covered_packets(checks: list[Check]) -> list[dict]:
    """The packet space the given filter checks cover, as boxes: each check's space minus its exceptions."""
    boxes = []
    for check in checks:
        try:
            boxes += _packet_boxes(check)
        except spaces.SpaceError:
            continue  # a check written in a form we cannot read covers nothing here
    return boxes


# Pieces of uncovered space searched per changed filter line, to keep validation quick.
MAX_UNCOVERED_SEARCHES = 24


def _uncovered_flow(bf: Any, base: str, candidate: str, row: Any, relevant: list[Check]) -> str | None:
    """A packet that a changed filter line now treats differently and that no check covers, described; else None.

    It searches the space no check with the line's new action covers, for a packet
    that hits the candidate's changed line, then tests that packet on the current
    filter. Finding one proves an unrequested change; not finding one within the
    search budget is not proof that none exists.
    """
    from pybatfish.datamodel.flow import HeaderConstraints

    action = str(row["Line_Action"]).lower()
    uncovered = spaces.packets_minus(
        spaces.packet_box(None, None, None, None), covered_packets([c for c in relevant if c.expect == action])
    )
    index = row["Line_Index"]
    target = f"matchLine {int(index)}" if index is not None and str(index).isdigit() else action
    for box in uncovered[:MAX_UNCOVERED_SEARCHES]:
        frame = (
            bf.q.searchFilters(
                nodes=row["Node"], filters=row["Filter_Name"], headers=HeaderConstraints(**spaces.header_fields(box)), action=target
            )
            .answer(snapshot=candidate)
            .frame()
        )
        if frame.empty:
            continue
        flow = frame.iloc[0]["Flow"]
        fields: dict[str, Any] = {"srcIps": flow.srcIp, "dstIps": flow.dstIp, "ipProtocols": [flow.ipProtocol]}
        if flow.ipProtocol in ("TCP", "UDP"):
            fields.update(srcPorts=str(flow.srcPort), dstPorts=str(flow.dstPort))
        elif getattr(flow, "icmpType", None) is not None:
            fields.update(icmpTypes=[flow.icmpType], icmpCodes=[flow.icmpCode])
        test = (
            bf.q.testFilters(nodes=row["Node"], filters=row["Filter_Name"], headers=HeaderConstraints(**fields))
            .answer(snapshot=base)
            .frame()
        )
        before = str(test.iloc[0]["Action"]).lower() if not test.empty else None
        if before and before != action:
            return f"for example {_flow(flow)} was {before} and is now {action}, and no check covers it"
    return None


def _excepting(check: Check) -> str:
    return f", outside the {len(check.exceptions)} exception(s)," if check.exceptions else ""


def _headers(check: Check) -> Any:
    from pybatfish.datamodel.flow import HeaderConstraints

    fields: dict[str, Any] = {}
    if check.src_ips:
        fields["srcIps"] = check.src_ips
    if check.dst_ips:
        fields["dstIps"] = check.dst_ips
    if check.protocol:
        fields["ipProtocols"] = [check.protocol]
    if check.dst_ports:
        fields["dstPorts"] = check.dst_ports
    return HeaderConstraints(**fields)


def _flow(flow: Any) -> str:
    port = lambda p: f":{p}" if p is not None and flow.ipProtocol in ("TCP", "UDP") else ""  # noqa: E731
    return f"{flow.ipProtocol} {flow.srcIp}{port(flow.srcPort)} -> {flow.dstIp}{port(flow.dstPort)}"


def _run(bf: Any, check: Check, snapshot: str) -> tuple[bool, str]:
    """Returns (holds, evidence). Evidence is a counterexample when the check does not hold."""
    if check.kind == "filter":
        opposite = "deny" if check.expect == "permit" else "permit"
        for headers in _header_spaces(check):
            frame = (
                bf.q.searchFilters(nodes=check.node, filters=check.filter, headers=headers, action=opposite)
                .answer(snapshot=snapshot)
                .frame()
            )
            if not frame.empty:
                break
        else:
            return True, f"No packet in this space{_excepting(check)} is {'denied' if opposite == 'deny' else 'permitted'}."
        row = frame.iloc[0]
        line = row["Line_Content"] or "the implicit default"
        verdict = "permitted" if row["Action"] == "PERMIT" else "denied"
        return False, f"{_flow(row['Flow'])} is {verdict} by: {line}"

    if check.kind == "route_policy":
        from pybatfish.datamodel.route import BgpRouteConstraints

        opposite = "deny" if check.expect == "permit" else "permit"
        space = spaces.parse_prefix_range(check.prefix)
        prefixes = spaces.prefixes_minus(space, [spaces.parse_prefix_range(e.prefix) for e in check.exceptions if e.prefix])
        if not prefixes:
            raise ValueError("the exceptions cover the whole prefix space, so nothing is left to check")
        # Batfish reads a list of communities as: carries every plain one, and none of the !-prefixed ones.
        communities = ([check.communities] if check.communities else []) + [
            f"!{e.communities}" for e in check.exceptions if e.communities
        ]
        inputs = BgpRouteConstraints(prefix=prefixes, communities=communities or None)
        frame = (
            bf.q.searchRoutePolicies(nodes=check.node, policies=check.policy, inputConstraints=inputs, action=opposite)
            .answer(snapshot=snapshot)
            .frame()
        )
        if not frame.empty:
            return False, f"{_route_text(frame.iloc[0]['Input_Route'])} is {'denied' if opposite == 'deny' else 'permitted'}."
        holds = f"No route in this prefix space{_excepting(check)} is {'denied' if opposite == 'deny' else 'permitted'}"
        # After the policy: look for a permitted route that leaves breaking each condition.
        wanted = []
        for constraints, words in _output_violations(check):
            frame = (
                bf.q.searchRoutePolicies(
                    nodes=check.node, policies=check.policy, inputConstraints=inputs,
                    outputConstraints=BgpRouteConstraints(**constraints), action="permit",
                )  # fmt: skip
                .answer(snapshot=snapshot)
                .frame()
            )
            if not frame.empty:
                row = frame.iloc[0]
                return False, (f"{_route_text(row['Input_Route'])} is permitted but leaves with "
                               f"{_output_text(row['Output_Route'])}; the check expects {words}.")  # fmt: skip
            wanted.append(words)
        return True, holds + (f"; every permitted route leaves {'; '.join(wanted)}." if wanted else ".")

    if check.kind == "route":
        frame = bf.q.routes(nodes=check.node, network=check.prefix).answer(snapshot=snapshot).frame()
        if frame.empty:
            return check.expect == "absent", f"{check.node} has no route for {check.prefix}."
        row = frame.iloc[0]
        return check.expect == "present", f"{check.node} has {row['Protocol']} route {row['Network']} via {row['Next_Hop']}."

    if check.kind == "bgp_session":
        frame = bf.q.bgpSessionStatus(nodes=check.node).answer(snapshot=snapshot).frame()
        rows = frame[frame["Remote_IP"].astype(str) == check.remote_ip]
        if rows.empty:
            return False, f"{check.node} has no BGP session configured with {check.remote_ip}."
        status = rows.iloc[0]["Established_Status"]
        return status == "ESTABLISHED", f"Session with {check.remote_ip} is {status}."

    if check.kind == "reachability":
        from pybatfish.datamodel.flow import PathConstraints

        start = f"@enter({check.node}[{check.enter_interface}])" if check.enter_interface else check.node
        opposite = "FAILURE" if check.expect == "success" else "SUCCESS"
        for headers in _header_spaces(check):
            frame = (
                bf.q.reachability(pathConstraints=PathConstraints(startLocation=start), headers=headers, actions=opposite)
                .answer(snapshot=snapshot)
                .frame()
            )
            if not frame.empty:
                break
        else:
            return True, f"No packet in this space{_excepting(check)} {'fails' if opposite == 'FAILURE' else 'is delivered'}."
        row = frame.iloc[0]
        return False, f"{_flow(row['Flow'])} ends as {row['Traces'][0].disposition}."

    raise ValueError(f"unknown check kind {check.kind!r}")


def run_checks(bf: Any, checks: list[dict[str, Any]], snapshot: str) -> list[dict[str, Any]]:
    """Run each check on one parsed model. Status is pass, fail, or error when it could not be decided."""
    inv = inventory_of(bf, snapshot)
    results = []
    for index, raw in enumerate(checks):
        check = Check(**raw)
        problems = spec_errors(check, inv)
        if problems:
            status, evidence = "error", " ".join(problems)
        else:
            try:
                holds, evidence = _run(bf, check, snapshot)
                status = "pass" if holds else "fail"
            except Exception as error:  # noqa: BLE001 - one undecidable check must not hide the others
                status, evidence = "error", f"{type(error).__name__}: {error}"[:500]
        results.append(
            {
                "index": index,
                "requirement": check.requirement,
                "description": check.description,
                "kind": check.kind,
                "status": status,
                "evidence": evidence,
            }
        )
    return results


def unreachable_filter_lines(bf: Any, snapshot: str, name: str) -> list[dict[str, str]]:
    """ACL or filter lines named name that no packet can ever reach, found by Batfish, not by a model."""
    try:
        frame = bf.q.filterLineReachability(filters=name).answer(snapshot=snapshot).frame()
    except Exception:  # noqa: BLE001 - not a filter, or Batfish cannot answer: no findings
        return []
    findings = []
    for _, row in frame.iterrows():
        blocking = "; ".join(str(b) for b in (row.get("Blocking_Lines") or [])) or "no single earlier line"
        where = ", ".join(str(s) for s in (row.get("Sources") or [])) or name
        findings.append({
            "description": f"{where}: line \"{row['Unreachable_Line']}\" can never match a packet",
            "evidence": f"Earlier lines catch everything it would match ({blocking})."
                        + (" Its action differs from theirs, so it was probably meant to apply." if row.get("Different_Action") else ""),
        })  # fmt: skip
    return findings


def inventory_of(bf: Any, snapshot: str) -> dict[str, dict[str, list[str]]]:
    frame = (
        bf.q.nodeProperties(properties="IP_Access_Lists,Routing_Policies,Interfaces").answer(snapshot=snapshot).frame()
    )
    return {
        row["Node"]: {
            "filters": sorted(row["IP_Access_Lists"] or []),
            "policies": sorted(row["Routing_Policies"] or []),
            "interfaces": sorted(row["Interfaces"] or []),
        }
        for _, row in frame.iterrows()
    }


# --- what a candidate changed beyond the requirements ------------------------


def _effect(name: str, status: str, summary: str, items: list[str] | None = None) -> dict[str, Any]:
    items = items or []
    shown = items[:MAX_ITEMS]
    if len(items) > len(shown):
        shown.append(f"and {len(items) - len(shown)} more")
    return {"name": name, "status": status, "summary": summary, "items": shown}


def _guard(name: str, compare) -> dict[str, Any]:
    """A comparison that cannot run is shown for review, never counted as clean."""
    try:
        return compare()
    except Exception as error:  # noqa: BLE001
        return _effect(name, "review", f"Could not be compared: {type(error).__name__}: {error}"[:400])


def side_effects(
    bf: Any,
    base: str,
    candidate: str,
    base_report: dict[str, Any],
    candidate_report: dict[str, Any],
    checks: list[dict[str, Any]],
    changed: list[str],
) -> list[dict[str, Any]]:
    """Compare the candidate's model with the current one. `changed` lists the file paths that differ.

    Each entry has status pass (nothing changed), fail (a regression, or a change
    outside what the requirements cover) or review (a change the requirements do
    cover, listed so the approver sees exactly what moved).
    """
    named_filters = {(c["node"], c["filter"]) for c in checks if c["kind"] == "filter"}
    parsed = [Check(**c) for c in checks]
    named_policies = {(c["node"], c["policy"]) for c in checks if c["kind"] == "route_policy"}
    changed_nodes = {node for f in candidate_report["files"] if f["path"] in changed for node in f["nodes"]}
    routing_in_scope = any(c["kind"] in ("route", "route_policy", "bgp_session") for c in checks)

    def parsing() -> dict[str, Any]:
        before = {f["path"]: f["status"] for f in base_report["files"]}
        worse = [
            f"{f['path']}: {before.get(f['path'], 'new')} -> {f['status']}"
            for f in candidate_report["files"]
            if f["status"] != before.get(f["path"]) and f["status"] != "PASSED"
        ]
        key = lambda i: (i["type"], i["details"], i.get("lineText"))  # noqa: E731
        known = {key(i) for i in base_report["issues"]}
        new = [f"{i['details']}" + (f" ({i['lineText']})" if i.get("lineText") else "") for i in candidate_report["issues"] if key(i) not in known]
        if worse or new:
            return _effect("Parsing", "fail", "The candidate parses less cleanly than the current configs.", worse + new)
        return _effect("Parsing", "pass", "The candidate parses as cleanly as the current configs.")

    def references() -> dict[str, Any]:
        def rows(snapshot: str) -> set[tuple[str, str, str]]:
            frame = bf.q.undefinedReferences().answer(snapshot=snapshot).frame()
            return {(str(r["File_Name"]), str(r["Struct_Type"]), str(r["Ref_Name"])) for _, r in frame.iterrows()}

        new = sorted(rows(candidate) - rows(base))
        if new:
            return _effect(
                "References", "fail", "The candidate refers to names that are not defined.",
                [f"{kind} {name} in {path}" for path, kind, name in new],
            )
        return _effect("References", "pass", "No new undefined references.")

    def sessions() -> dict[str, Any]:
        def established(snapshot: str) -> set[tuple[str, str]]:
            frame = bf.q.bgpSessionStatus().answer(snapshot=snapshot).frame()
            return {
                (str(r["Node"]), str(r["Remote_IP"]))
                for _, r in frame.iterrows()
                if r["Established_Status"] == "ESTABLISHED"
            }

        lost = sorted(established(base) - established(candidate))
        if lost:
            return _effect(
                "BGP sessions", "fail", "Sessions that are established now would not be.",
                [f"{node} with {remote}" for node, remote in lost],
            )
        return _effect("BGP sessions", "pass", "Every established BGP session stays established.")

    def filters() -> dict[str, Any]:
        frame = bf.q.compareFilters().answer(snapshot=candidate, reference_snapshot=base).frame()
        if frame.empty:
            return _effect("Filters", "pass", "No ACL or firewall filter behaves differently.")
        outside, unrequested, inside = [], [], []
        for _, r in frame.iterrows():
            line = lambda text: text or "the implicit deny"  # noqa: E731
            text = (
                f"{r['Node']} {r['Filter_Name']}: packets that matched \"{line(r['Reference_Line_Content'])}\" "
                f"now match \"{line(r['Line_Content'])}\" ({str(r['Line_Action']).lower()})"
            )
            key = (r["Node"], r["Filter_Name"])
            if key not in named_filters:
                outside.append(text)
                continue
            relevant = [c for c in parsed if c.kind == "filter" and (c.node, c.filter) == key]
            leak = _uncovered_flow(bf, base, candidate, r, relevant)
            (unrequested if leak else inside).append(f"{text}; {leak}" if leak else text)
        if outside or unrequested:
            summary = (
                "Filters change in ways no check asked for."
                if unrequested
                else "Filters that no requirement covers behave differently."
            )
            return _effect("Filters", "fail", summary, outside + unrequested + inside)
        return _effect("Filters", "review", "Filters change only where the checks asked for it.", inside)

    def policies() -> dict[str, Any]:
        # Each policy is compared with the one of the same name. Asking Batfish about
        # several at once compares every pair of them, which reports differences
        # between unrelated policies. Only devices whose files changed are compared.
        before, after = inventory_of(bf, base), inventory_of(bf, candidate)
        outside, unrequested, inside = [], [], []
        for node in sorted(changed_nodes):
            was = {p for p in before.get(node, {}).get("policies", []) if not p.startswith("~")}
            now = {p for p in after.get(node, {}).get("policies", []) if not p.startswith("~")}
            for policy in sorted(was | now):
                if policy in was and policy in now:
                    frame = (
                        bf.q.compareRoutePolicies(nodes=node, policy=policy)
                        .answer(snapshot=candidate, reference_snapshot=base)
                        .frame()
                    )
                    # One row per kind of difference: each must be one a check asked for.
                    relevant = [c for c in parsed if c.kind == "route_policy" and (c.node, c.policy) == (node, policy)]
                    for _, row in frame.iterrows():
                        route = row["Input_Route"]
                        communities = list(getattr(route, "communities", None) or [])
                        now = str(row["Snapshot_Action"]).lower()
                        # Both permit: the action is the same and only what the policy sets changed.
                        attributes = now == str(row["Reference_Action"]).lower()
                        text = (
                            f"{node} {policy}: for example route {route.network}"
                            + (f" with communities {', '.join(communities)}" if communities else "")
                            + (f" is still {now} but leaves differently ({str(row.get('Difference', ''))[:120]})"
                               if attributes else f" was {str(row['Reference_Action']).lower()} and is now {now}")
                        )  # fmt: skip
                        if (node, policy) not in named_policies:
                            outside.append(text)
                        elif any(
                            c.expect == now and route_covered(c, route.network, communities) and (not attributes or has_output(c))
                            for c in relevant
                        ):
                            inside.append(text)
                        else:
                            unrequested.append(text + " (no check asked for this)")
                    continue
                else:
                    text = f"{node} {policy}: {'removed' if policy in was else 'added'}"
                (inside if (node, policy) in named_policies else outside).append(text)
        if outside or unrequested:
            summary = (
                "Routing policies change in ways no check asked for."
                if unrequested
                else "Routing policies that no requirement covers behave differently."
            )
            return _effect("Routing policies", "fail", summary, outside + unrequested + inside)
        if inside:
            return _effect("Routing policies", "review", "Routing policies change only where the checks asked for it.", inside)
        return _effect("Routing policies", "pass", "No routing policy treats any route differently.")

    def routes() -> dict[str, Any]:
        def table(snapshot: str) -> set[tuple[str, str, str, str, str]]:
            frame = bf.q.routes().answer(snapshot=snapshot).frame()
            return {
                (str(r["Node"]), str(r["VRF"]), str(r["Network"]), str(r["Next_Hop"]), str(r["Protocol"]))
                for _, r in frame.iterrows()
            }

        before, after = table(base), table(candidate)
        show = lambda sign, r: f"{sign} {r[0]}: {r[2]} via {r[3]} ({r[4]})"  # noqa: E731
        changes = [show("removed", r) for r in sorted(before - after)] + [show("added", r) for r in sorted(after - before)]
        if not changes:
            return _effect("Routing tables", "pass", "Every device's routing table is unchanged.")
        if routing_in_scope:
            return _effect("Routing tables", "review", "Routes change, and the requirements are about routing.", changes)
        return _effect("Routing tables", "fail", "Routes change, but no requirement is about routing.", changes)

    def forwarding() -> dict[str, Any]:
        frame = bf.q.differentialReachability().answer(snapshot=candidate, reference_snapshot=base).frame()
        if frame.empty:
            return _effect("Forwarding", "pass", "No packet is delivered in one model and dropped in the other.")
        samples = [_flow(r["Flow"]) for _, r in frame.head(MAX_ITEMS).iterrows()]
        return _effect(
            "Forwarding", "review",
            "Some traffic is delivered in one model and not the other. Expected when the change is meant to "
            "block or allow traffic; shown so the reviewer can confirm it is the intended traffic.",
            samples,
        )

    return [
        _guard("Parsing", parsing),
        _guard("References", references),
        _guard("BGP sessions", sessions),
        _guard("Filters", filters),
        _guard("Routing policies", policies),
        _guard("Routing tables", routes),
        _guard("Forwarding", forwarding),
    ]
