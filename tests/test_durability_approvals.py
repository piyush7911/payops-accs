"""Durability (restart/crash safety), deterministic request ids, and the approval queue."""
import pytest

from payops.airwallex import FakeAirwallex
from payops.commander import Commander
from payops.llm import HeuristicProposer
from payops.models import Obligation
from payops.store import Store, request_id_for


def build(path=":memory:", amount=100.0, funds=10_000.0, aw=None, store=None):
    aw = aw or FakeAirwallex()
    aw.deposit("A", "EUR", funds)
    ob = Obligation("inv-1", "A", amount, "EUR", "SWIFT", "ben")
    orig = aw.create_transfer(ob, "orig-1")
    cmd = Commander(aw, HeuristicProposer(), store=store or Store(path))
    cmd.register(ob, orig)
    return aw, cmd, ob, orig


def fail(aw, t, ft="CHANNEL_TIMEOUT"):
    aw.transition(t.tenant, t.id, "SENT")
    aw.transition(t.tenant, t.id, "FAILED", ft)


# ---- deterministic request ids ------------------------------------------------------------------
def test_request_ids_are_deterministic_short_and_unique():
    a, b = request_id_for("inv-1", 1), request_id_for("inv-1", 1)
    assert a == b and len(a) <= 47
    assert request_id_for("inv-1", 2) != a and request_id_for("inv-2", 1) != a


def test_model_never_chooses_the_request_id():
    aw, cmd, ob, orig = build()
    fail(aw, orig)
    out = cmd.handle(ob.id, "x")
    assert out.request_id == request_id_for(ob.id, 1)
    assert aw.list_transfers("A", ob.id)[1].request_id == out.request_id


# ---- restart safety -----------------------------------------------------------------------------
def test_state_survives_a_restart_and_nothing_is_paid_twice(tmp_path):
    db = str(tmp_path / "payops.db")
    aw, cmd, ob, orig = build(db)
    fail(aw, orig)
    assert cmd.handle(ob.id, "x").decision == "REPLACED"
    del cmd
    cmd2 = Commander(aw, HeuristicProposer(), store=Store(db))  # process restarted
    assert ob.id in cmd2.obligations and request_id_for(ob.id, 1) in cmd2.used_request_ids
    from tests.test_accs import AlwaysReplace
    cmd2.proposer = AlwaysReplace()
    assert cmd2.handle(ob.id, "ping").decision == "DENIED_ESCALATED"
    assert len(aw.list_transfers("A", ob.id)) == 2


def test_crash_after_create_before_bookkeeping_is_reconciled_on_recovery(tmp_path):
    db = str(tmp_path / "p.db")
    aw, cmd, ob, orig = build(db)
    fail(aw, orig)
    rid = request_id_for(ob.id, 1)
    cmd.store.begin_attempt(ob.id, 1, rid)  # write-ahead row committed ...
    aw.create_transfer(ob, rid)  # ... transfer created at Airwallex ... crash before finish_attempt()
    cmd2 = Commander(aw, HeuristicProposer(), store=Store(db))
    res = cmd2.recover()
    assert res[0]["resolved"] == "CREATED" and cmd2.store.pending_attempts() == []
    assert len(aw.list_transfers("A", ob.id)) == 2


def test_crash_before_create_retries_with_the_same_request_id_exactly_once(tmp_path):
    db = str(tmp_path / "p.db")
    aw, cmd, ob, orig = build(db)
    fail(aw, orig)
    rid = request_id_for(ob.id, 1)
    cmd.store.begin_attempt(ob.id, 1, rid)  # intent recorded, then the process died before calling Airwallex
    cmd2 = Commander(aw, HeuristicProposer(), store=Store(db))
    assert cmd2.recover()[0]["resolved"] == "STILL_PENDING"
    out = cmd2.handle(ob.id, "x")  # the retry must be allowed (P3 excludes this pending id) and reuse the same id
    assert out.decision == "REPLACED" and out.request_id == rid
    assert len(aw.list_transfers("A", ob.id)) == 2 and cmd2.store.pending_attempts() == []


def test_ambiguous_timeout_leaves_a_pending_attempt_that_recovery_settles():
    aw, cmd, ob, orig = build()
    fail(aw, orig)
    aw.fail_next_create = True
    out = cmd.handle(ob.id, "x")
    assert out.decision == "REPLACED" and "reconciled" in out.note and cmd.store.pending_attempts() == []


# ---- approvals ----------------------------------------------------------------------------------
def test_overridable_denial_becomes_one_deduplicated_approval_bound_to_terms():
    aw, cmd, ob, orig = build()
    fail(aw, orig, "DUPLICATION_RETURN")
    o1, o2 = cmd.handle(ob.id, "x"), cmd.handle(ob.id, "again")
    assert o1.decision == "PENDING_APPROVAL" and o1.approval_id == o2.approval_id  # deduped
    (apr,) = cmd.pending_approvals()
    t = apr["terms"]
    assert t["amount"] == "100.0" and t["currency"] == "EUR" and t["beneficiary_id"] == "ben" and t["request_id"] == request_id_for(ob.id, 1)
    assert "DUPLICATION_RETURN" in t["summary"]
    assert len(aw.list_transfers("A", ob.id)) == 1  # nothing moved yet


def test_approve_executes_exactly_once_and_is_idempotent():
    aw, cmd, ob, orig = build()
    fail(aw, orig, "DUPLICATION_RETURN")
    apr = cmd.handle(ob.id, "x").approval_id
    r = cmd.approve(apr, "ops@example.com")
    assert r["status"] == "executed" and r["decision"] == "REPLACED"
    assert cmd.approve(apr, "ops@example.com")["idempotent"] is True
    assert len(aw.list_transfers("A", ob.id)) == 2


def test_approval_expires_if_the_world_changed():
    aw, cmd, ob, orig = build()
    fail(aw, orig, "DUPLICATION_RETURN")
    apr = cmd.handle(ob.id, "x").approval_id
    aw.create_transfer(ob, "someone-else-paid")  # supplier paid by another route while the approval sat in the queue
    r = cmd.approve(apr, "ops@example.com")
    assert r["status"] == "expired"
    assert len(aw.list_transfers("A", ob.id)) == 2  # only the out-of-band one; the agent added nothing


def test_hard_clauses_still_block_an_approved_action():
    aw, cmd, ob, orig = build(amount=1000, funds=1020)  # cannot afford amount + fee for a second payout
    fail(aw, orig, "DUPLICATION_RETURN")
    apr = cmd.handle(ob.id, "x").approval_id
    r = cmd.approve(apr, "ops@example.com")
    assert r["status"] == "failed" and "P5" in r["note"]  # a human cannot approve money that is not there
    assert len(aw.list_transfers("A", ob.id)) == 1


def test_deny_and_big_amount_approval():
    aw, cmd, ob, orig = build(amount=9000, funds=50_000)
    fail(aw, orig)
    out = cmd.handle(ob.id, "x")
    assert out.decision == "PENDING_APPROVAL" and out.verdict.clause == "P6-autonomy-limit"
    assert cmd.deny(out.approval_id, "ops")["status"] == "denied"
    assert len(aw.list_transfers("A", ob.id)) == 1
    out2 = cmd.handle(ob.id, "x")  # a fresh identical request creates a fresh pending approval
    assert cmd.approve(out2.approval_id, "ops")["status"] == "executed"


@pytest.mark.parametrize("clause_hard", ["P1-single-live-payment", "P3-fresh-request-id", "P4-tenant-isolation", "P5-fee-budget"])
def test_hard_clauses_are_never_queued_for_approval(clause_hard):
    from payops.policy import DEFAULT
    assert clause_hard not in DEFAULT.overridable()


# ---- preflight + mirroring the original's terms -------------------------------------------------
def test_preflight_failure_sends_nothing_and_escalates():
    aw, cmd, ob, orig = build()
    fail(aw, orig)
    aw.invalid_beneficiaries.add("ben")  # Airwallex's validate endpoint would reject this beneficiary
    out = cmd.handle(ob.id, "x")
    assert out.decision == "ESCALATED" and "preflight" in out.note
    assert len(aw.list_transfers("A", ob.id)) == 1 and cmd.store.pending_attempts() == []  # no fee burned, nothing written ahead


def test_replacement_mirrors_original_fee_and_charge_option():
    aw = FakeAirwallex(); aw.deposit("A", "EUR", 10_000)
    ob = Obligation("inv-9", "A", 100, "EUR", "SWIFT", "ben", swift_charge_option="SHARED")
    orig = aw.create_transfer(ob, "orig-9")
    ob2 = Obligation("inv-9", "A", 100, "EUR", "SWIFT", "ben")  # a fresh process that only knows the obligation
    cmd = Commander(aw, HeuristicProposer())
    cmd.register(ob2, orig)
    fail(aw, orig)
    cmd.handle("inv-9", "x")
    assert ob2.swift_charge_option == "SHARED"
    assert aw.list_transfers("A", "inv-9")[1].charge_option == "SHARED"
