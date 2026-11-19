"""Sorting chat messages into questions and workflow suggestions."""

from verifier_worker import routing
from verifier_worker.routing import Route

CONVERSATION = [
    {"role": "user", "content": "What does GUEST do on edge?"},
    {"role": "agent", "content": "GUEST line 10 permits everything."},
    {"role": "user", "content": "Fix that ACL so guests can't reach 10/8."},
]


def asker(answer):
    asked = []

    def ask(schema, system, user):
        asked.append(user)
        if isinstance(answer, Exception):
            raise answer
        return answer

    return ask, asked


def test_a_change_request_becomes_a_suggestion():
    ask, asked = asker(Route(kind="change", intent="  On edge, GUEST denies guests to 10.0.0.0/8. "))
    assert routing.sort(ask, CONVERSATION) == (
        "change",
        {"mode": "change", "intent": "On edge, GUEST denies guests to 10.0.0.0/8."},
    )
    # The earlier turns are given so references like "that ACL" can be resolved.
    assert "GUEST line 10 permits everything." in asked[0] and asked[0].rstrip().endswith("reach 10/8.")


def test_a_check_maps_to_a_verify_run():
    ask, _ = asker(Route(kind="check", intent="Guests cannot reach 10.0.0.0/8."))
    assert routing.sort(ask, CONVERSATION)[1]["mode"] == "verify"


def test_questions_blank_intents_and_failures_suggest_nothing():
    for answer, kind in (
        (Route(kind="question"), "question"),
        (Route(kind="change", intent="  "), "change"),
        (RuntimeError("model down"), None),
    ):
        ask, _ = asker(answer)
        assert routing.sort(ask, CONVERSATION) == (kind, None)


def test_a_review_of_a_named_structure_becomes_an_audit():
    ask, _ = asker(Route(kind="audit", target=" ISP-A-OUT "))
    assert routing.sort(ask, CONVERSATION) == ("audit", {"mode": "audit", "target": "ISP-A-OUT", "intent": "Audit ISP-A-OUT"})
    ask, _ = asker(Route(kind="audit"))
    assert routing.sort(ask, CONVERSATION) == ("audit", None)
