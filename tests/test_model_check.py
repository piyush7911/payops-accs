import pytest

from payops import model_check as mc
from payops import policy as P


def test_gate_is_safe_on_the_whole_reachable_state_space():
    r = mc.explore()
    assert r.ok, r.violations[:3]
    assert not r.capped  # a true fixpoint, not a bounded cut-off
    assert r.states >= 8 and r.decisions_checked > 1000


@pytest.mark.parametrize("cid", [c["id"] for c in P.DEFAULT.clauses])
def test_every_clause_is_necessary(cid):
    m = mc.mutation_analysis()[cid]
    assert m["violated_without_it"], f"{cid} did not matter: {m}"
    assert m["counterexample"]


def test_p1_is_necessary_exactly_because_p2_can_be_overridden_by_a_human():
    """With P2 overridable, a human-approved replacement could be created while the original is still live; P1 is the hard
    guard against that. If P2 were NOT overridable, P1 would be redundant (implied by P2)."""
    assert mc.mutation_analysis()["P1-single-live-payment"]["violated_without_it"]
    import copy
    spec = copy.deepcopy(P.DEFAULT.spec)
    for c in spec["clauses"]:
        c["overridable"] = False  # no human overrides at all
    strict = P.Policy(spec)
    assert not mc.mutation_analysis(strict)["P1-single-live-payment"]["violated_without_it"]
    assert mc.redundancy_analysis(strict)["P1-single-live-payment"]["violated_without_both"]


def test_overridable_set_is_exactly_p2_and_p6():
    assert P.DEFAULT.overridable() == {"P2-latest-terminal", "P6-autonomy-limit"}


def test_real_statuses_are_live_and_unknown_statuses_fail_safe():
    from payops.models import is_in_flight, is_live
    for st in ("SCHEDULED", "IN_APPROVAL", "PROCESSING", "OVERDUE", "SENT", "PAID", "SOMETHING_NEW"):
        assert is_live(st), st  # anything not definitively dead blocks a replacement
    for st in ("SCHEDULED", "IN_APPROVAL", "PROCESSING", "OVERDUE", "SENT", "SOMETHING_NEW"):
        assert is_in_flight(st), st
    assert not is_live("CANCELLED") and not is_live("FAILED") and not is_in_flight("PAID")


def test_policy_spec_validation_rejects_bad_specs():
    spec = {"params": {}, "clauses": [{"id": "x", "applies": ["REPLACE"], "check": "nope", "text": "t"}]}
    with pytest.raises(P.PolicyError):
        P.Policy(spec)
    dup = {"params": {}, "clauses": [{"id": "x", "applies": ["WAIT"], "check": "wait_only_in_flight", "text": "t"}] * 2}
    with pytest.raises(P.PolicyError):
        P.Policy(dup)
    with pytest.raises(P.PolicyError):
        P.Policy({"params": {}, "clauses": [{"id": "x", "applies": ["FLY"], "check": "wait_only_in_flight", "text": "t"}]})


def test_violation_is_detected_in_the_step_it_happens():
    """Bounded liveness: with P1 and P2 removed the first violating state is reached, and the independent auditor
    flags it on that very state (detection lag 0)."""
    pol = P.DEFAULT.without("P1-single-live-payment", "P2-latest-terminal")
    r = mc.explore(pol, stop_on_violation=True)
    assert r.violations and r.violations[0][0] in ("INV-1", "INV-2")
