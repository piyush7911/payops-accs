import random

import pytest

from payops import consistency, policy
from payops.airwallex import FakeAirwallex
from payops.commander import Commander
from payops.llm import HeuristicProposer, RecklessProposer
from payops.models import SWIFT_FEE, Action, Claim, Obligation, Proposal, TransferRec


def make(tenant="acct_A", oid="inv-1", amount=100.0, funds=10_000.0, method="SWIFT", proposer=None, aw=None):
    aw = aw or FakeAirwallex()
    aw.deposit(tenant, "EUR", funds)
    ob = Obligation(oid, tenant, amount, "EUR", method, "ben")
    orig = aw.create_transfer(ob, f"orig-{oid}")
    cmd = Commander(aw, proposer or HeuristicProposer())
    cmd.register(ob, orig)
    return aw, cmd, ob, orig


def fail(aw, t, ft="CHANNEL_TIMEOUT"):
    aw.transition(t.tenant, t.id, "SENT")
    aw.transition(t.tenant, t.id, "FAILED", ft)


# ---- F1: policy.check -------------------------------------------------------------------------
def test_wait_while_in_flight_and_no_replacement():
    aw, cmd, ob, orig = make()
    assert cmd.handle(ob.id, "nothing arrived").decision == "WAITING"
    assert len(aw.list_transfers(ob.tenant, ob.id)) == 1


class AlwaysReplace:
    """A pushy model: asks to replace on every ping, with truthful claims about the latest transfer."""
    def propose(self, ob, transfers, message):
        from payops.llm import _to_proposal
        last = transfers[-1]
        return _to_proposal({"action": "REPLACE", "claims": [{"subject": last.id, "field": "status", "value": last.status}]}, ob)


def test_replace_after_failure_then_duplicate_denied_with_clause():
    aw, cmd, ob, orig = make()
    fail(aw, orig)
    assert cmd.handle(ob.id, "x").decision == "REPLACED"
    assert cmd.handle(ob.id, "still nothing").decision == "WAITING"  # a sensible model waits for the replacement
    cmd.proposer = AlwaysReplace()  # a pushy one does not; the gate holds
    out = cmd.handle(ob.id, "still nothing")
    assert out.decision == "DENIED_ESCALATED" and out.verdict.clause == "P1-single-live-payment"
    assert len(aw.list_transfers(ob.tenant, ob.id)) == 2


def test_fee_budget_two_swift_fees():
    aw, cmd, ob, orig = make(amount=1000, funds=1020)
    fail(aw, orig)
    assert cmd.handle(ob.id, "x").verdict.clause == "P5-fee-budget"


def test_tenant_isolation_never_uses_other_wallet():
    aw, cmd, ob, orig = make(tenant="acct_B", amount=1000, funds=1020)
    aw.deposit("acct_A", "EUR", 1_000_000)  # rich neighbour must not help
    fail(aw, orig)
    out = cmd.handle(ob.id, "x")
    assert not out.verdict.permit
    bad = Action("REPLACE", ob.id, "acct_B", "new-id", funding_tenant="acct_A")
    ctx = policy.Context(ob, aw.list_transfers("acct_B", ob.id), cmd.used_request_ids, 1e9, 1.0)
    assert policy.check(bad, ctx).clause == "P4-tenant-isolation"


def test_autonomy_limit_requires_human():
    aw, cmd, ob, orig = make(amount=9000, funds=50_000)
    fail(aw, orig)
    assert cmd.handle(ob.id, "x").verdict.clause == "P6-autonomy-limit"
    assert cmd.handle(ob.id, "x", human_approved=True).decision == "REPLACED"


def test_unknown_failure_type_escalates():
    aw, cmd, ob, orig = make()
    fail(aw, orig, "weird_unknown")
    assert cmd.handle(ob.id, "x").verdict.clause == "P2-latest-terminal"


def test_request_id_reuse_denied():
    aw, cmd, ob, orig = make()
    fail(aw, orig)
    ctx = policy.Context(ob, aw.list_transfers(ob.tenant, ob.id), cmd.used_request_ids, 1e6, 1.0)
    assert policy.check(Action("REPLACE", ob.id, ob.tenant, "orig-inv-1", ob.tenant), ctx).clause == "P3-fresh-request-id"


# ---- F2: consistency.score --------------------------------------------------------------------
def test_hallucinated_claim_scores_low_and_blocks():
    aw, cmd, ob, orig = make(proposer=RecklessProposer())
    aw.transition(ob.tenant, orig.id, "SENT")  # still in flight, LLM claims CANCELLED
    out = cmd.handle(ob.id, "just resend")
    assert out.score.value < 0.2 and not out.verdict.permit
    assert len(aw.list_transfers(ob.tenant, ob.id)) == 1


def test_score_monotone_in_support_and_contradiction():
    c = Claim("t", "status", "CANCELLED")
    sup = consistency.Evidence("s", "t", "status", "CANCELLED")
    con = consistency.Evidence("s", "t", "status", "SENT")
    prev = -1.0
    for n in range(6):  # more support never lowers the score
        v = consistency.score(c, [sup] * n).value
        assert v >= prev
        prev = v
    prev = 2.0
    for n in range(6):  # more contradiction never raises it
        v = consistency.score(c, [con] * n).value
        assert v <= prev
        prev = v


def test_calibration_on_labelled_set():
    rng = random.Random(7)
    rows = []  # (score, label)
    for _ in range(400):
        true_status = rng.choice(["PROCESSING", "SENT", "PAID", "CANCELLED"])
        claimed = true_status if rng.random() < 0.5 else rng.choice(["PROCESSING", "SENT", "PAID", "CANCELLED"])
        ev = [consistency.Evidence("airwallex", "t1", "status", true_status)]
        rows.append((consistency.score(Claim("t1", "status", claimed), ev).value, claimed == true_status))
    true_mean = sum(s for s, y in rows if y) / sum(1 for _, y in rows if y)
    false_mean = sum(s for s, y in rows if not y) / sum(1 for _, y in rows if not y)
    assert true_mean > 0.85 and false_mean < 0.15
    brier = sum((s - y) ** 2 for s, y in rows) / len(rows)
    assert brier < 0.05


# ---- R1 analogue: safety invariant over random traces ----------------------------------------
def _random_trace(seed, steps=60, gated=True):
    rng = random.Random(seed)
    aw = FakeAirwallex()
    cmd = Commander(aw, HeuristicProposer())
    obs = []
    for i, tenant in enumerate(["acct_A", "acct_B", "acct_C"]):
        aw.deposit(tenant, "EUR", 50_000)
        ob = Obligation(f"inv-{i}", tenant, 100.0 + i, "EUR", rng.choice(["SWIFT", "LOCAL"]), "ben")
        cmd.register(ob, aw.create_transfer(ob, f"orig-{i}"))
        obs.append(ob)
    for _ in range(steps):
        ob = rng.choice(obs)
        ts = aw.list_transfers(ob.tenant, ob.id)
        t = rng.choice(ts)
        try:  # world events: random simulated transitions
            to = rng.choice(["SENT", "PAID", "FAILED"])
            aw.transition(ob.tenant, t.id, to, rng.choice(["CHANNEL_TIMEOUT", "recall", "weird"]))
        except ValueError:
            pass
        if gated:
            cmd.handle(ob.id, "supplier nagging")
        elif rng.random() < 0.7:  # no gate: a naive agent that resends whenever pinged
            aw.create_transfer(ob, f"naive-{rng.random()}")
        bad = policy.violations({o.id: aw.list_transfers(o.tenant, o.id) for o in obs})
        if bad:
            return bad
    return []


@pytest.mark.parametrize("seed", range(200))
def test_inv1_holds_on_every_random_trace_with_gate(seed):
    assert _random_trace(seed) == []


def test_ungated_agent_violates_inv1_and_auditor_detects_it():
    """Mutation check: without the gate the invariant breaks, and the auditor flags it at the step it happens."""
    assert any(_random_trace(s, gated=False) for s in range(20))


# ---- idempotency / ambiguous failures ---------------------------------------------------------
def test_ambiguous_timeout_reconciles_without_second_payment():
    aw, cmd, ob, orig = make()
    fail(aw, orig)
    aw.fail_next_create = True  # transfer is created server-side but the client sees a timeout
    out = cmd.handle(ob.id, "x")
    assert out.decision == "REPLACED" and "reconciled" in out.note
    assert len(aw.list_transfers(ob.tenant, ob.id)) == 2


def test_close_report_records_original_and_two_fees():
    aw, cmd, ob, orig = make()
    fail(aw, orig)
    cmd.handle(ob.id, "x")
    repl = aw.list_transfers(ob.tenant, ob.id)[1]
    aw.transition(ob.tenant, repl.id, "SENT")
    aw.transition(ob.tenant, repl.id, "PAID")
    r = cmd.close_report(ob.id)
    assert r["closable"] and r["original"][1] == "CANCELLED" and r["fees_paid"] == pytest.approx(float(2 * SWIFT_FEE))


def test_escalated_incident_is_not_closable():
    aw, cmd, ob, orig = make()
    fail(aw, orig, "DUPLICATION_RETURN")
    assert cmd.handle(ob.id, "x").decision == "PENDING_APPROVAL"  # overridable clause -> a human can decide, nothing auto-sent
    assert cmd.close_report(ob.id)["closable"] is False


def test_live_failure_types_policy():
    for ft in ("RECALL_REQUESTED", "CLIENT_REQUESTED", "ACCOUNT_CLOSED", "BENEFICIARY_BANK_RETURNED", "DUPLICATION_RETURN"):
        aw, cmd, ob, orig = make()
        fail(aw, orig, ft)
        assert cmd.handle(ob.id, "x").verdict.clause == "P2-latest-terminal", ft


# ---- emission path for replies ----------------------------------------------------------------
def test_false_reply_is_replaced_by_verified_template():
    aw, cmd, ob, orig = make(proposer=RecklessProposer())
    aw.transition(ob.tenant, orig.id, "SENT")
    out = cmd.handle(ob.id, "just resend")
    assert out.reply_source == "template" and "resent" not in out.emitted_reply.lower()
    assert orig.id[:8] in out.emitted_reply


def test_honest_reply_is_emitted():
    aw, cmd, ob, orig = make()
    out = cmd.handle(ob.id, "nothing arrived")
    assert out.reply_source == "llm" and out.emitted_reply


def test_judge_can_only_restrict():
    from payops.judge import JudgeVerdict

    class Blocking:
        def review(self, *a, **k):
            return JudgeVerdict(True, "P5-fee-budget", "looks underfunded")

    class Permissive:
        def review(self, *a, **k):
            return JudgeVerdict(False, reason="fine")

    aw, cmd, ob, orig = make()
    fail(aw, orig)
    cmd.judge = Blocking()
    out = cmd.handle(ob.id, "x")
    assert out.decision == "DENIED_ESCALATED" and out.verdict.clause.startswith("JUDGE:")
    assert len(aw.list_transfers(ob.tenant, ob.id)) == 1
    # a permissive judge can never override a code-gate denial
    aw2, cmd2, ob2, orig2 = make(amount=1000, funds=1020)
    fail(aw2, orig2)
    cmd2.judge = Permissive()
    assert cmd2.handle(ob2.id, "x").verdict.clause == "P5-fee-budget"


def test_judge_abstain_keeps_code_gate_authoritative():
    from payops.judge import NullJudge
    aw, cmd, ob, orig = make()
    fail(aw, orig)
    cmd.judge = NullJudge()
    assert cmd.handle(ob.id, "x").decision == "REPLACED"


def test_max_replacements_blocks_a_second_replacement():
    aw, cmd, ob, orig = make()
    fail(aw, orig)
    assert cmd.handle(ob.id, "x").decision == "REPLACED"
    repl = aw.list_transfers(ob.tenant, ob.id)[1]
    fail(aw, repl)  # the replacement also fails with a replaceable type
    out = cmd.handle(ob.id, "x")
    assert out.verdict.clause == "P9-max-replacements"
    assert len(aw.list_transfers(ob.tenant, ob.id)) == 2


def test_calibration_never_loosens_the_gate():
    """A claim with no evidence calibrates to a high probability on honest claimants, but must still be blocked."""
    from payops.consistency import Calibrator
    aw, cmd, ob, orig = make(proposer=HeuristicProposer())
    fail(aw, orig)
    cmd.calibrator = Calibrator([0.1, 0.5, 0.9], [0.6, 0.9, 0.95])  # generous: 0.5 -> 0.9

    class Unseen:
        def propose(self, ob_, ts, msg):
            from payops.llm import _to_proposal
            return _to_proposal({"action": "REPLACE", "claims": [{"subject": "ghost", "field": "status", "value": "CANCELLED"}]}, ob_)

    cmd.proposer = Unseen()
    out = cmd.handle(ob.id, "x")
    assert out.verdict.clause == "P7-evidence" and len(aw.list_transfers(ob.tenant, ob.id)) == 1


# ---- real Airwallex statuses beyond PROCESSING/SENT/PAID (SCHEDULED seen in the live sandbox) ----------------
@pytest.mark.parametrize("status", ["SCHEDULED", "IN_APPROVAL", "OVERDUE"])
def test_no_replacement_while_original_is_scheduled_in_approval_or_overdue(status):
    aw, cmd, ob, orig = make()
    aw.transfers[orig.id].status = status  # the world reports a status our first version did not know
    out = cmd.handle(ob.id, "nothing arrived")
    assert out.decision == "WAITING"  # in flight: waiting is the valid response
    assert len(aw.list_transfers(ob.tenant, ob.id)) == 1


def test_human_override_waives_p2_but_never_p1():
    aw, cmd, ob, orig = make()
    fail(aw, orig, "DUPLICATION_RETURN")
    ctx = policy.Context(ob, aw.list_transfers(ob.tenant, ob.id), cmd.used_request_ids, 1e6, 1.0,
                         overrides=frozenset({"P2-latest-terminal"}))
    act = Action("REPLACE", ob.id, ob.tenant, "fresh-1", ob.tenant)
    assert policy.check(act, ctx).permit  # a human accepted the risk of resending after DUPLICATION_RETURN
    live = [TransferRec("t9", ob.id, ob.tenant, "r9", "IN_APPROVAL", None, 100.0)]
    ctx2 = policy.Context(ob, aw.list_transfers(ob.tenant, ob.id) + live, cmd.used_request_ids, 1e6, 1.0,
                          overrides=frozenset({"P1-single-live-payment", "P2-latest-terminal"}))
    assert policy.check(act, ctx2).clause == "P1-single-live-payment"  # hard clause ignores the override


def test_exact_decimal_amounts_survive_prompt_and_judge_serialisation():
    """Regression: Decimal money must serialise into the LLM/judge prompts (found by the live run)."""
    import json
    from payops.llm import _snapshot
    ob = Obligation("o", "A", "100.10", "EUR", "SWIFT", "b")
    assert json.loads(json.dumps(_snapshot(ob, []), default=str))["obligation"]["amount"] == "100.10"
