"""The staged workflow, driven with a scripted model and fake Batfish results."""

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from verifier_worker import workflow
from verifier_worker.workflow import CheckPlan, Clarified, Edit, Proposal, Requirement

FILES = {"edge.cfg": "ip access-list extended GUEST\n 10 permit ip any any\n 20 deny ip any 10.0.0.0 0.255.255.255\n"}
PARSED = [{"path": "edge.cfg", "format": "CISCO_IOS", "status": "PASSED", "nodes": ["edge"]}]
INVENTORY = {"edge": {"filters": ["GUEST"], "policies": [], "interfaces": ["Gi0/0"]}}
CHECK = {"requirement": "R1", "kind": "filter", "description": "guests cannot reach 10/8", "node": "edge",
         "expect": "deny", "filter": "GUEST", "dst_ips": "10.0.0.0/8"}  # fmt: skip
GOOD_EDIT = Edit(path="edge.cfg", search=" 10 permit ip any any\n", replace="")
WRONG_EDIT = Edit(path="edge.cfg", search=" 20 deny", replace=" 25 deny")
CLEAN = [{"name": "Parsing", "status": "pass", "summary": "ok", "items": []}]


class Script:
    """Answers the workflow's model calls from queues, and records what was asked."""

    def __init__(self, **answers):
        self.answers = {name: list(queue) for name, queue in answers.items()}
        self.asked = []

    def ask(self, schema, system, user):
        self.asked.append((schema.__name__, system + "\n" + user))
        return self.answers[schema.__name__].pop(0)

    def prompts(self, name):
        return [user for asked, user in self.asked if asked == name]


def result(status):
    return [{"index": 0, "requirement": "R1", "description": CHECK["description"], "kind": "filter",
             "status": status, "evidence": "evidence"}]  # fmt: skip


def make(script, effects=CLEAN):
    """A graph whose check fails on the current configs, and passes on a candidate without the broad permit."""
    stored = []
    stages = []

    def run_checks(checks, candidate):
        status = "pass" if candidate and candidate["fixed"] else "fail"
        return [{**result(status)[0], "index": i, "description": c["description"]} for i, c in enumerate(checks)]

    def make_candidate(number, files, summary, diff):
        stored.append(files)
        fixed = "10 permit ip any any" not in files["edge.cfg"]
        return {"id": f"cand-{number}", "content_hash": f"hash-{number}", "changed": ["edge.cfg"], "fixed": fixed}

    deps = workflow.Deps(
        ask=script.ask,
        investigate=lambda task: ("GUEST line 10 permits everything first.", [{"tool": "read_config", "args": {}}]),
        files=FILES,
        parsed_files=PARSED,
        inventory=INVENTORY,
        run_checks=run_checks,
        side_effects=lambda candidate, checks: effects,
        make_candidate=make_candidate,
        progress=stages.append,
    )
    graph = workflow.build(deps, InMemorySaver())
    return graph, {"configurable": {"thread_id": "run"}}, stages, stored


REQS = Clarified(requirements=[Requirement(id="R1", text="Guests cannot reach 10.0.0.0/8")])
PLAN = CheckPlan(checks=[workflow.Check(**CHECK)])
CHANGE = {"mode": "change", "intent": "block guests from 10/8"}
VERIFY = {"mode": "verify", "intent": "block guests from 10/8"}


def proposal(*edits, summary="s"):
    return Proposal(summary=summary, edits=list(edits))


def waiting(graph, cfg):
    state = graph.get_state(cfg)
    return state.next, [i.value for task in state.tasks for i in task.interrupts]


def start_change(graph, cfg, **review):
    """Start a change run, which stops for the checks to be reviewed, and confirm them."""
    graph.invoke(CHANGE, cfg)
    assert waiting(graph, cfg) == (("review_checks",), [{"kind": "checks", "count": 1}])
    graph.invoke(Command(resume={"drop": [], "by": "owner", **review}), cfg)


def test_change_run_proposes_validates_and_waits_for_approval():
    script = Script(Clarified=[REQS], CheckPlan=[PLAN], Proposal=[proposal(GOOD_EDIT)])
    graph, cfg, stages, stored = make(script)
    start_change(graph, cfg)

    assert waiting(graph, cfg) == (("approve",), [{"kind": "approval", "candidate_hash": "hash-1"}])
    values = graph.get_state(cfg).values
    assert stages == [
        "clarify", "investigate", "plan_checks", "baseline", "review_checks", "review_checks", "propose", "validate", "report",
    ]  # fmt: skip (review_checks reports again when resumed)
    assert values["checks_confirmed_by"] == "owner"
    assert values["baseline"][0]["status"] == "fail"
    attempt = values["attempts"][0]
    assert attempt["passed"] and attempt["content_hash"] == "hash-1"
    assert "- 10 permit ip any any" in attempt["diff"]
    assert "10 permit" not in stored[0]["edge.cfg"]
    assert values["outcome"] is None and "waiting for approval" in values["headline"]

    graph.invoke(Command(resume={"decision": "approved", "candidate_hash": "hash-1", "by": "u1"}), cfg)
    state = graph.get_state(cfg)
    assert state.next == ()
    assert state.values["outcome"] == "approved" and state.values["decision"]["by"] == "u1"


def test_rejection_and_a_decision_on_another_candidate():
    script = Script(Clarified=[REQS], CheckPlan=[PLAN], Proposal=[proposal(GOOD_EDIT)])
    graph, cfg, _, _ = make(script)
    start_change(graph, cfg)
    graph.invoke(Command(resume={"decision": "rejected", "candidate_hash": "hash-1"}), cfg)
    assert graph.get_state(cfg).values["outcome"] == "rejected"

    # An approval that names other content approves nothing.
    script = Script(Clarified=[REQS], CheckPlan=[PLAN], Proposal=[proposal(GOOD_EDIT)])
    graph, cfg, _, _ = make(script)
    start_change(graph, cfg)
    graph.invoke(Command(resume={"decision": "approved", "candidate_hash": "other"}), cfg)
    values = graph.get_state(cfg).values
    assert values["outcome"] == "rejected" and "different candidate" in values["headline"]


def test_failed_validation_is_fed_back_and_repaired():
    script = Script(Clarified=[REQS], CheckPlan=[PLAN], Proposal=[proposal(WRONG_EDIT), proposal(GOOD_EDIT)])
    graph, cfg, stages, _ = make(script)
    start_change(graph, cfg)

    attempts = graph.get_state(cfg).values["attempts"]
    assert [a["passed"] for a in attempts] == [False, True]
    assert stages.count("propose") == 2 and stages.count("validate") == 2
    second_prompt = script.prompts("Proposal")[1]
    assert "Attempt 1 was rejected" in second_prompt and "Check not met" in second_prompt
    # Each attempt edits the current configs, not the previous attempt.
    assert "25 deny" not in attempts[1]["diff"]
    assert waiting(graph, cfg)[0] == ("approve",)


def test_gives_up_after_three_rounds_without_asking_for_approval():
    script = Script(Clarified=[REQS], CheckPlan=[PLAN], Proposal=[proposal(WRONG_EDIT)] * 3)
    graph, cfg, _, _ = make(script)
    start_change(graph, cfg)
    state = graph.get_state(cfg)
    assert state.next == ()
    assert len(state.values["attempts"]) == 3
    assert state.values["outcome"] == "not_validated"


def test_side_effect_fails_validation():
    effects = [{"name": "Routing tables", "status": "fail", "summary": "Routes change.", "items": ["removed x"]}]
    script = Script(Clarified=[REQS], CheckPlan=[PLAN], Proposal=[proposal(GOOD_EDIT)] * 3)
    graph, cfg, _, _ = make(script, effects=effects)
    start_change(graph, cfg)
    values = graph.get_state(cfg).values
    assert values["outcome"] == "not_validated"
    assert "Unwanted side effect (Routing tables)" in values["attempts"][0]["problem"]


def test_edits_that_do_not_apply_are_retried_then_recorded():
    missing = Edit(path="edge.cfg", search="no such text", replace="x")
    script = Script(Clarified=[REQS], CheckPlan=[PLAN], Proposal=[proposal(missing), proposal(GOOD_EDIT)])
    graph, cfg, _, _ = make(script)
    start_change(graph, cfg)
    # The bad edit was corrected within the same round.
    attempts = graph.get_state(cfg).values["attempts"]
    assert len(attempts) == 1 and attempts[0]["passed"]
    assert "occurs 0 times" in script.prompts("Proposal")[1]

    script = Script(Clarified=[REQS], CheckPlan=[PLAN], Proposal=[proposal(missing)] * 9)
    graph, cfg, _, stored = make(script)
    start_change(graph, cfg)
    values = graph.get_state(cfg).values
    assert stored == [] and values["outcome"] == "not_validated"
    assert "could not be applied" in values["attempts"][0]["problem"]


def test_verify_run_reports_and_stops():
    script = Script(Clarified=[REQS], CheckPlan=[PLAN])
    graph, cfg, stages, _ = make(script)
    graph.invoke(VERIFY, cfg)
    state = graph.get_state(cfg)
    assert state.next == ()
    assert state.values["outcome"] == "violated"
    assert stages == ["clarify", "investigate", "plan_checks", "baseline", "report"]


def test_clarify_asks_then_resumes_with_the_answer():
    script = Script(Clarified=[Clarified(question="Which upstream do you mean?"), REQS], CheckPlan=[PLAN])
    graph, cfg, _, _ = make(script)
    graph.invoke(VERIFY, cfg)
    assert waiting(graph, cfg) == (("ask_user",), [{"kind": "question", "question": "Which upstream do you mean?"}])
    assert graph.get_state(cfg).values["pending_question"] == "Which upstream do you mean?"

    graph.invoke(Command(resume="The one on edge."), cfg)
    values = graph.get_state(cfg).values
    assert values["qa"] == [{"question": "Which upstream do you mean?", "answer": "The one on edge."}]
    assert "A: The one on edge." in script.prompts("Clarified")[1]
    # The model was asked exactly twice: resuming did not repeat the first call.
    assert len(script.prompts("Clarified")) == 2
    assert values["outcome"] == "violated"


def test_stops_asking_after_the_question_limit():
    script = Script(Clarified=[Clarified(question="Which?")] * 4)
    graph, cfg, _, _ = make(script)
    graph.invoke(VERIFY, cfg)
    for _ in range(2):
        graph.invoke(Command(resume="that one"), cfg)
    # A fourth question is not put to the user; with no requirements the run fails.
    with pytest.raises(workflow.WorkflowError):
        graph.invoke(Command(resume="that one"), cfg)
    assert "may not ask further questions" in script.prompts("Clarified")[3]


def test_bad_checks_are_corrected_or_dropped_never_run():
    bad = workflow.Check(**{**CHECK, "filter": "NOPE"})
    script = Script(Clarified=[REQS], CheckPlan=[CheckPlan(checks=[bad]), PLAN])
    graph, cfg, _, _ = make(script)
    graph.invoke(VERIFY, cfg)
    assert graph.get_state(cfg).values["checks"][0]["filter"] == "GUEST"
    assert "has no filter 'NOPE'" in script.prompts("CheckPlan")[1]

    script = Script(Clarified=[REQS], CheckPlan=[CheckPlan(checks=[bad])] * 3)
    graph, cfg, _, _ = make(script)
    graph.invoke(VERIFY, cfg)
    values = graph.get_state(cfg).values
    assert values["checks"] == [] and values["outcome"] == "inconclusive"
    assert values["uncheckable"][0]["requirement"] == "R1"


def test_apply_edits_and_public_state():
    files, problems = workflow.apply_edits({"a": "x\nx\n"}, [Edit(path="a", search="x", replace="y")])
    assert files == {"a": "x\nx\n"} and "occurs 2 times" in problems[0]
    _, problems = workflow.apply_edits({"a": "x"}, [Edit(path="a", search="x", replace="x")])
    assert "unchanged" in problems[0]
    assert workflow.public({"mode": "verify", "candidate": {"report": 1}, "findings": "f"}) == {"findings": "f"}


def test_reviewer_drops_a_check():
    second = {**CHECK, "requirement": "R1", "description": "guests cannot reach 10.1/16", "dst_ips": "10.1.0.0/16"}
    plan = CheckPlan(checks=[workflow.Check(**CHECK), workflow.Check(**second)])
    script = Script(Clarified=[REQS], CheckPlan=[plan], Proposal=[proposal(GOOD_EDIT)])
    graph, cfg, _, _ = make(script)
    graph.invoke(CHANGE, cfg)
    graph.invoke(Command(resume={"drop": [0, 99], "by": "owner"}), cfg)
    values = graph.get_state(cfg).values
    # The dropped check is gone from the checks and the baseline, and is not used to validate.
    assert [c["description"] for c in values["checks"]] == ["guests cannot reach 10.1/16"]
    assert [r["index"] for r in values["baseline"]] == [0]
    assert values["dropped_checks"][0]["description"] == CHECK["description"]
    assert values["dropped_checks"][0]["by"] == "owner"
    assert len(values["attempts"][0]["validation"]) == 1
    assert "1 check(s) were removed by the reviewer" in values["headline"]


def test_dropping_every_failing_check_proposes_nothing():
    script = Script(Clarified=[REQS], CheckPlan=[PLAN])
    graph, cfg, stages, _ = make(script)
    graph.invoke(CHANGE, cfg)
    graph.invoke(Command(resume={"drop": [0], "by": "owner"}), cfg)
    state = graph.get_state(cfg)
    assert state.next == () and "propose" not in stages
    assert state.values["outcome"] == "inconclusive"


def test_reviewer_correction_rewrites_the_checks():
    script = Script(Clarified=[REQS], CheckPlan=[PLAN, PLAN], Proposal=[proposal(GOOD_EDIT)])
    graph, cfg, stages, _ = make(script)
    graph.invoke(CHANGE, cfg)
    graph.invoke(Command(resume={"drop": [0], "note": "Only check the 10.1/16 part.", "by": "owner"}), cfg)
    # Back to planning with the note, then baseline again, then waiting for review again.
    assert waiting(graph, cfg)[0] == ("review_checks",)
    assert stages.count("plan_checks") == 2 and stages.count("baseline") == 2
    second = script.prompts("CheckPlan")[1]
    assert "Only check the 10.1/16 part." in second and "Your latest checks:" in second
    values = graph.get_state(cfg).values
    # A correction ignores the ticks sent with it: the checks are rewritten instead.
    assert len(values["checks"]) == 1 and not values.get("dropped_checks")
    assert values["check_notes"] == [{"note": "Only check the 10.1/16 part.", "by": "owner"}]

    graph.invoke(Command(resume={"drop": [], "by": "owner"}), cfg)
    assert waiting(graph, cfg)[0] == ("approve",)


def test_corrections_stop_at_the_limit():
    script = Script(Clarified=[REQS], CheckPlan=[PLAN] * 4, Proposal=[proposal(GOOD_EDIT)])
    graph, cfg, _, _ = make(script)
    graph.invoke(CHANGE, cfg)
    for _ in range(workflow.MAX_CHECK_REVISIONS):
        graph.invoke(Command(resume={"note": "again", "by": "owner"}), cfg)
    # One more correction is not taken: the checks are confirmed as they are.
    graph.invoke(Command(resume={"note": "again", "by": "owner"}), cfg)
    assert len(script.prompts("CheckPlan")) == 1 + workflow.MAX_CHECK_REVISIONS
    assert waiting(graph, cfg)[0] == ("approve",)


def test_a_report_request_stops_before_investigating():
    script = Script(Clarified=[Clarified(not_checkable="It asks for a list of where FEDNET is defined.")])
    graph, cfg, stages, _ = make(script)
    with pytest.raises(workflow.WorkflowError) as error:
        graph.invoke(VERIFY, cfg)
    assert str(error.value).startswith(workflow.NOT_A_CHECK) and "list of where FEDNET" in str(error.value)
    assert stages == ["clarify"]
    # The model is told what Batfish can decide, and not to write report-style requirements.
    assert workflow.CHECKABLE in script.asked[0][1] and "never one about the verification itself" in script.asked[0][1]


def test_with_no_checks_the_findings_are_the_answer():
    script = Script(Clarified=[REQS], CheckPlan=[CheckPlan(uncheckable=[{"requirement": "R1", "reason": "no kind fits"}])])
    graph, cfg, _, _ = make(script)
    graph.invoke(VERIFY, cfg)
    values = graph.get_state(cfg).values
    assert values["outcome"] == "inconclusive" and "findings are below" in values["headline"]


def test_audit_infers_intent_waits_for_confirmation_then_reports():
    from verifier_worker.workflow import Inferred

    inferred = Inferred(purpose="GUEST keeps guests out of 10/8.", requirements=[Requirement(id="R1", text="GUEST denies guests to 10.0.0.0/8")])
    script = Script(Inferred=[inferred], CheckPlan=[PLAN])
    stages = []
    deps = workflow.Deps(
        ask=script.ask, investigate=lambda task: ("unused", []), files=FILES, parsed_files=PARSED, inventory=INVENTORY,
        run_checks=lambda checks, candidate: result("fail") if checks else [], side_effects=lambda c, k: CLEAN,
        make_candidate=lambda *a: {}, progress=stages.append,
        describe=lambda name: "=== extended ipv4 access-list GUEST, on edge ===\n10 permit ip any any",
        code_findings=lambda name: [{"description": "edge: line 20 can never match", "evidence": "line 10 catches it"}],
    )  # fmt: skip
    graph = workflow.build(deps, InMemorySaver())
    cfg = {"configurable": {"thread_id": "audit"}}
    graph.invoke({"mode": "audit", "intent": "Audit GUEST", "target": "GUEST"}, cfg)
    # No clarify and no investigation: the structure itself is the evidence. It always stops for confirmation.
    assert stages == ["infer", "plan_checks", "baseline", "review_checks"]
    values = graph.get_state(cfg).values
    assert values["purpose"] == "GUEST keeps guests out of 10/8." and "10 permit ip any any" in values["findings"]
    assert "Structure to audit: GUEST" in script.prompts("Inferred")[0]

    graph.invoke(Command(resume={"drop": [], "by": "owner"}), cfg)
    values = graph.get_state(cfg).values
    assert graph.get_state(cfg).next == () and values["outcome"] == "violated"
    assert "GUEST: 0 of 1 check(s)" in values["headline"] and "found 1 problem(s) in it directly" in values["headline"]
    assert values["code_findings"][0]["description"] == "edge: line 20 can never match"


def test_audit_of_an_unknown_name_stops_at_once():
    deps = workflow.Deps(ask=Script().ask, investigate=lambda t: ("", []), files=FILES, parsed_files=PARSED, inventory=INVENTORY,
                         run_checks=lambda *a: [], side_effects=lambda *a: [], make_candidate=lambda *a: {},
                         describe=lambda name: "No structure named 'NOPE' is defined or referenced.")  # fmt: skip
    graph = workflow.build(deps, InMemorySaver())
    with pytest.raises(workflow.WorkflowError) as error:
        graph.invoke({"mode": "audit", "intent": "Audit NOPE", "target": "NOPE"}, {"configurable": {"thread_id": "x"}})
    assert "Nothing named 'NOPE'" in str(error.value)
