"""Check specifications are validated in code before anything runs on Batfish."""

from verifier_worker import checks

INVENTORY = {"edge1": {"filters": [], "policies": ["ISP-A-OUT"], "interfaces": []}}


def policy_check(**fields):
    base = {"requirement": "R1", "kind": "route_policy", "description": "d", "node": "edge1",
            "expect": "deny", "policy": "ISP-A-OUT", "prefix": "8.8.8.0/24:24-32"}  # fmt: skip
    return checks.Check(**{**base, **fields})


def test_communities_may_be_negated_but_must_be_one_community():
    assert checks.spec_errors(policy_check(communities="65001:100"), INVENTORY) == []
    assert checks.spec_errors(policy_check(communities="!65001:100"), INVENTORY) == []
    for bad in ("65001:100,65001:200", "not 65001:100", "/^65001:/"):
        assert "communities must be one community" in checks.spec_errors(policy_check(communities=bad), INVENTORY)[0]
