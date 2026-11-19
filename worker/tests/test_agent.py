"""The agent loop, driven by a scripted stand-in model so no model access is needed."""

from typing import Any

import re
import pandas as pd
import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from verifier_worker import agent

EDGE = """hostname edge1
router bgp 65009
 neighbor 10.0.0.10 remote-as 65010
 neighbor 10.0.0.10 route-map OUT out
!
route-map OUT permit 10
"""


class Scripted(BaseChatModel):
    """Replays a fixed list of responses and records what it was sent."""

    script: list[AIMessage]
    seen: list[list[Any]] = []

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append(list(messages))
        return ChatResult(generations=[ChatGeneration(message=self.script[len(self.seen) - 1])])


class FakeQuestion:
    def __init__(self, frame):
        self._frame = frame

    def answer(self):
        return self

    def frame(self):
        return self._frame


class FakeBatfish:
    """Stands in for a pybatfish Session: bf.q.<question>(**params).answer().frame()."""

    def __init__(self):
        self.calls = []
        self.q = self

    def bgpPeerConfiguration(self, **params):
        """Returns configuration settings for BGP peerings.

        :param nodes: Include nodes matching this specifier.
        :type nodes: nodeSpec
        :param properties: Include properties matching this regex.
        :type properties: bgpPeerPropertySpec
        """
        self.calls.append(("bgpPeerConfiguration", params))
        return FakeQuestion(pd.DataFrame([{"Node": "edge1", "Remote_AS": "65010", "Export_Policy": ["OUT"]}]))

    def testRoutePolicies(self, **params):
        """Evaluates the processing of a route by a given policy.

        :param policies: Only consider policies that match this specifier.
        :type policies: routingPolicySpec
        :param inputRoutes: *Required.* The BGP route announcements to test the policy on.
        :type inputRoutes: bgpRoutes
        :param direction: *Required.* The direction of the route (IN/OUT).
            Allowed values:

            * in: inbound
            * out: outbound
        :type direction: string
        """
        self.calls.append(("testRoutePolicies", params))
        if "direction" not in params:
            raise ValueError("Missing value for parameter direction")
        return FakeQuestion(pd.DataFrame([{"Node": "edge1", "Action": "PERMIT"}]))

    def routes(self, **params):
        raise ValueError("bad parameter")

    def nodeProperties(self, **params):
        return FakeQuestion(pd.DataFrame())


@pytest.fixture
def ctx():
    return agent.Context(
        snapshot_name="lab",
        files={"edge/edge1.cfg": EDGE, "r1/frr.conf": "router bgp 65001\n"},
        parsed_files=[
            {"path": "edge/edge1.cfg", "status": "PASSED", "format": "CISCO_IOS", "nodes": ["edge1"]},
            {"path": "r1/frr.conf", "status": "UNKNOWN", "format": "UNKNOWN", "nodes": []},
        ],
        intent="Keep customer routes away from ISP-A.",
        batfish=FakeBatfish(),
    )


def tools(ctx):
    return {t.name: t for t in agent.build_tools(ctx)}


def call(name, args, call_id):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


def test_search_finds_lines_with_path_and_line_number(ctx):
    out = tools(ctx)["search_configs"].invoke({"pattern": "route-map out"})
    assert out.splitlines() == [
        "edge/edge1.cfg:4: neighbor 10.0.0.10 route-map OUT out",
        "edge/edge1.cfg:6: route-map OUT permit 10",
    ]
    assert tools(ctx)["search_configs"].invoke({"pattern": "ospf"}) == "No matches."
    # An invalid regular expression is searched for literally instead of failing.
    assert tools(ctx)["search_configs"].invoke({"pattern": "neighbor ("}) == "No matches."


def test_read_config_numbers_lines_pages_and_rejects_unknown_paths(ctx):
    read = tools(ctx)["read_config"]
    out = read.invoke({"path": "edge/edge1.cfg", "start_line": 2, "max_lines": 2})
    assert out.splitlines() == ["2: router bgp 65009", "3:  neighbor 10.0.0.10 remote-as 65010", "[3 more lines; call again with start_line=4]"]
    assert "no file named" in read.invoke({"path": "../../etc/passwd"})


def test_batfish_question_is_limited_to_the_allowed_list_and_reports_errors(ctx):
    ask = tools(ctx)["batfish_question"]
    out = ask.invoke({"question": "bgpPeerConfiguration", "parameters": {"nodes": "edge1"}})
    assert "edge1,65010" in out
    assert ctx.batfish.calls == [("bgpPeerConfiguration", {"nodes": "edge1"})]
    assert "not available" in ask.invoke({"question": "init_snapshot"})
    assert "not available" in ask.invoke({"question": "__class__"})
    assert ask.invoke({"question": "routes"}).startswith("Error running routes: ValueError")
    assert ask.invoke({"question": "nodeProperties"}) == "No rows."


def test_reply_runs_tools_then_answers_and_records_the_steps(ctx):
    model = Scripted(
        script=[
            call("search_configs", {"pattern": "route-map"}, "c1"),
            call("batfish_question", {"question": "bgpPeerConfiguration"}, "c2"),
            AIMessage(content="edge1 exports to 10.0.0.10 through route-map OUT (edge/edge1.cfg:4)."),
        ],
        seen=[],
    )
    history = agent.to_history(
        [
            {"role": "user", "content": "hello", "failed": False},
            {"role": "agent", "content": "an earlier failure notice", "failed": True},
            {"role": "user", "content": "What is exported to 10.0.0.10?", "failed": False},
        ]
    )
    assert [type(m) for m in history] == [HumanMessage, HumanMessage]  # failed reply dropped

    progress = []
    answer, steps = agent.reply(model, ctx, history, progress.append)

    assert answer.startswith("edge1 exports to 10.0.0.10")
    # Progress names what is happening at each step, counting finished tool calls.
    assert {"step": 0, "doing": "searching configs for 'route-map'"} in progress
    assert {"step": 1, "doing": "Batfish bgpPeerConfiguration"} in progress
    assert progress[-1] == {"step": 2, "doing": "thinking"}
    assert [s["tool"] for s in steps] == ["search_configs", "batfish_question"]
    assert "edge/edge1.cfg:4" in steps[0]["summary"]
    # The prompt carries the snapshot's files and the user's intent; tool output reaches the model.
    system = model.seen[0][0].content
    assert "edge/edge1.cfg: CISCO_IOS, PASSED, device edge1" in system
    assert "r1/frr.conf: UNKNOWN, UNKNOWN, not usable by Batfish" in system
    assert "Keep customer routes away from ISP-A." in system
    assert isinstance(model.seen[1][-1], ToolMessage)


def test_reply_gives_up_when_the_model_never_stops_calling_tools(ctx):
    model = Scripted(script=[call("search_configs", {"pattern": "x"}, f"c{i}") for i in range(40)], seen=[])
    with pytest.raises(agent.AgentGaveUp):
        agent.reply(model, ctx, [HumanMessage("loop forever")])
    assert len(model.seen) <= agent.MAX_STEPS


def test_reply_answers_from_what_it_found_when_it_runs_out_of_steps(ctx):
    loop = [call("search_configs", {"pattern": "bgp"}, f"c{i}") for i in range(agent.MAX_STEPS)]
    model = Scripted(script=[*loop, AIMessage("Found the BGP config. I did not check the route-maps.")], seen=[])
    model.script = model.script[: agent.MAX_STEPS // 2] + [model.script[-1]]
    answer, steps = agent.reply(model, ctx, [HumanMessage("check everything")])
    assert answer == "Found the BGP config. I did not check the route-maps."
    assert steps and all(step["tool"] == "search_configs" and step["summary"] for step in steps)
    # The last request is the plain instruction to answer, after only completed tool calls.
    final = model.seen[-1]
    assert final[-1].content == agent.OUT_OF_STEPS
    assert isinstance(final[-2], ToolMessage)


def test_search_docs_is_offered_only_when_an_index_is_available(ctx):
    assert "search_docs" not in tools(ctx)

    asked = []
    ctx.search_docs = lambda query, vendor: asked.append((query, vendor)) or "[1] FRR User Guide: Route Maps ..."
    out = tools(ctx)["search_docs"].invoke({"query": "route-map continue", "vendor": "frr"})
    assert out.startswith("[1] FRR User Guide")
    assert asked == [("route-map continue", "frr")]

    def broken(query, vendor):
        raise RuntimeError("database is down")

    ctx.search_docs = broken
    assert "Error searching documentation: RuntimeError" in tools(ctx)["search_docs"].invoke({"query": "x"})


def test_question_parameters_come_from_batfishs_own_documentation(ctx):
    params = agent.question_parameters(ctx.batfish, "testRoutePolicies")
    assert [(p.name, p.type, p.required) for p in params] == [
        ("policies", "routingPolicySpec", False),
        ("inputRoutes", "bgpRoutes", True),
        ("direction", "string", True),
    ]
    assert params[2].description.startswith("The direction of the route (IN/OUT). Allowed values:")
    assert agent.question_parameters(ctx.batfish, "layer3Edges") is None  # not offered by this stand-in

    reference = agent.question_reference(ctx.batfish)
    assert "- testRoutePolicies(policies: routingPolicySpec, inputRoutes: bgpRoutes (required), direction: string (required))" in reference
    assert "- bgpPeerConfiguration(nodes: nodeSpec, properties: bgpPeerPropertySpec)" in reference
    assert reference in agent.system_prompt(ctx)


def test_route_objects_are_built_from_the_json_the_agent_sends(ctx):
    from pybatfish.datamodel.route import BgpRoute

    route = {"network": "192.0.2.0/24", "originatorIp": "10.0.0.9", "originType": "igp", "protocol": "bgp"}
    out = tools(ctx)["batfish_question"].invoke(
        {"question": "testRoutePolicies", "parameters": {"policies": "OUT", "direction": "out", "inputRoutes": [route]}}
    )
    assert "edge1,PERMIT" in out
    sent = ctx.batfish.calls[-1][1]
    assert sent["policies"] == "OUT"
    assert isinstance(sent["inputRoutes"][0], BgpRoute) and sent["inputRoutes"][0].network == "192.0.2.0/24"


def test_a_failed_question_explains_what_it_accepts(ctx):
    out = tools(ctx)["batfish_question"].invoke({"question": "testRoutePolicies", "parameters": {"policies": "OUT"}})
    assert out.startswith("Error running testRoutePolicies: ValueError: Missing value for parameter direction")
    assert "- inputRoutes (bgpRoutes, required): The BGP route announcements to test the policy on." in out
    assert "- direction (string, required)" in out
    assert "bgpRoutes: a list of objects with required keys network" in out

    # A malformed route is reported the same way instead of raising.
    bad = tools(ctx)["batfish_question"].invoke(
        {"question": "testRoutePolicies", "parameters": {"direction": "out", "inputRoutes": [{"prefix": "x"}]}}
    )
    assert bad.startswith("Error running testRoutePolicies: TypeError")


def test_show_structure_groups_identical_definitions_and_lists_references():
    from types import SimpleNamespace

    import pandas as pd

    files = {"a.conf": "x\npolicy FEDNET\n then accept\n", "b.conf": "policy FEDNET\n then accept\ny\n", "c.conf": "policy FEDNET\n then reject\n"}
    lines = lambda f, ns: SimpleNamespace(filename=f"configs/{f}", lines=ns)  # noqa: E731
    defined = pd.DataFrame([
        {"Structure_Type": "policy-statement", "Structure_Name": "FEDNET", "Source_Lines": lines("a.conf", [2, 3])},
        {"Structure_Type": "policy-statement", "Structure_Name": "FEDNET", "Source_Lines": lines("b.conf", [1, 2])},
        {"Structure_Type": "policy-statement", "Structure_Name": "FEDNET", "Source_Lines": lines("c.conf", [1, 2])},
    ])
    referenced = pd.DataFrame([
        {"Structure_Type": "policy-statement", "Structure_Name": "FEDNET", "Context": "bgp export", "Source_Lines": lines("a.conf", [1])},
    ])
    asked = []

    class Questions:
        def __getattr__(self, name):
            frame = {"definedStructures": defined, "referencedStructures": referenced}[name]
            return lambda **kw: asked.append((name, kw)) or SimpleNamespace(answer=lambda: SimpleNamespace(frame=lambda: frame))

    parsed = [{"path": p, "format": "JUNIPER", "status": "PASSED", "nodes": [p[0] + "-re0"]} for p in files]
    ctx = agent.Context(snapshot_name="s", files=files, parsed_files=parsed, intent=None, batfish=SimpleNamespace(q=Questions()))
    show = next(t for t in agent.build_tools(ctx) if t.name == "show_structure")
    out = show.invoke({"name": "FEDNET", "kind": "policy-statement"})
    # One call shows every device; identical text is shown once with the devices that share it.
    assert "identical on 2 devices: a-re0 (a.conf:2), b-re0 (b.conf:1)" in out
    assert "on c-re0 (c.conf:1)" in out and "then reject" in out
    assert "- a-re0 a.conf:1 (bgp export): x" in out
    assert asked[0] == ("definedStructures", {"names": "FEDNET", "types": "policy-statement"})


def test_read_config_takes_several_ranges_and_search_shows_context(ctx):
    tools = {t.name: t for t in agent.build_tools(ctx)}
    path = next(iter(ctx.files))
    out = tools["read_config"].invoke({"ranges": [{"path": path, "start_line": 1, "max_lines": 1}, {"path": "nope", "start_line": 1, "max_lines": 1}]})
    assert f"=== {path} from line 1 ===\n1: " in out and "no file named 'nope'" in out
    first_word = ctx.files[path].splitlines()[1].split()[0]
    found = tools["search_configs"].invoke({"pattern": re.escape(first_word), "context": 1, "path": path})
    assert f"{path}:1-" in found and found.rstrip().endswith("--")
