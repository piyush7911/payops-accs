"""Scenario library + web API. Every scenario runs end to end offline and is checked against the safety invariant."""
import json
import threading
import urllib.request

import pytest

from payops import api
from payops.models import is_live
from payops.service import SCENARIOS, Service


@pytest.fixture()
def svc():
    s = Service(mode="simulator", use_llm=False, pace=0)
    yield s
    s.receiver.stop()


def run(svc, name):
    assert svc.start(name) == {"started": name}
    svc.wait(60)
    assert svc.error is None, svc.error
    return svc.state()


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_every_scenario_completes_and_never_has_two_live_transfers(svc, name):
    st = run(svc, name)
    assert st["incidents"], name
    for inc in st["incidents"]:
        assert inc["live_count"] <= 1, f"{name}: {inc['id']} has {inc['live_count']} live transfers"
        kinds = [e["kind"] for e in inc["timeline"]]
        assert kinds[0] == "goal"  # every story starts with the user's goal
        assert not any(e["kind"] == "alert" and "AUDITOR" in e["title"] for e in inc["timeline"])  # auditor never fired


def test_timeout_replaces_exactly_once_and_closes_with_both_fees(svc):
    inc = run(svc, "timeout")["incidents"][0]
    assert [t["status"] for t in inc["transfers"]] == ["CANCELLED", "PAID"]
    assert inc["timeline"][-1]["kind"] == "result" and "Fees paid 26.78" in inc["timeline"][-1]["detail"]


def test_duplication_queues_one_approval_and_moves_no_money_until_approved(svc):
    st = run(svc, "duplication")
    assert len(st["approvals"]) == 1 and len(st["incidents"][0]["transfers"]) == 1
    r = svc.approve(st["approvals"][0]["id"], "ops@x")
    assert r["status"] == "executed"
    st2 = svc.state()
    assert not st2["approvals"] and len(st2["incidents"][0]["transfers"]) == 2


def test_short_wallet_blocks_p5_and_leaves_the_rich_tenant_untouched(svc):
    st = run(svc, "short_wallet")
    by = {i["tenant"]: i for i in st["incidents"]}
    assert len(by["acct_B"]["transfers"]) == 1  # no replacement
    assert any("P5-fee-budget" in c["label"] for e in by["acct_B"]["timeline"] for c in e["chips"])
    assert by["acct_A"]["wallet"].startswith("1000000") or float(by["acct_A"]["wallet"]) > 999_000  # A's money never used


def test_hallucination_creates_no_second_transfer_and_replies_with_the_template(svc):
    inc = run(svc, "hallucination")["incidents"][0]
    assert len(inc["transfers"]) == 1
    replies = [e for e in inc["timeline"] if e["kind"] == "reply"]
    assert replies and all("template" in e["chips"][0]["label"] for e in replies)


def test_webhook_triple_delivery_causes_one_replacement_and_forgery_is_rejected(svc):
    inc = run(svc, "webhook")["incidents"][0]
    assert len(inc["transfers"]) == 2
    assert any("401" in e["detail"] for e in inc["timeline"] if e["title"] == "Forged delivery")
    assert sum(1 for e in svc.receiver.log if e[0] == "applied") == 1


def test_crash_recovery_reuses_the_same_request_id(svc):
    inc = run(svc, "crash_recovery")["incidents"][0]
    rids = [a["request_id"] for a in inc["attempts"]]
    assert len(rids) == len(set(rids)) == 2 and inc["attempts"][1]["state"] == "CREATED"
    assert inc["transfers"][1]["request_id"] == inc["attempts"][1]["request_id"]


def test_late_return_reopens_after_paid(svc):
    inc = run(svc, "late_return")["incidents"][0]
    assert [t["status"] for t in inc["transfers"]] == ["CANCELLED", "PAID"]


def test_one_scenario_at_a_time_and_unknown_names(svc):
    svc.pace = 0.05
    svc.start("timeout")
    assert "error" in svc.start("duplication")
    svc.wait(60)
    assert "error" in svc.start("nope")


# ---- HTTP surface --------------------------------------------------------------------------------
@pytest.fixture()
def server(svc):
    api.attach_service(svc)
    srv = api.serve(0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    api.attach_service(None)


def call(base, path, body=None):
    req = urllib.request.Request(base + path, None if body is None else json.dumps(body).encode(), {"content-type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return r.status, r.read()


def test_ui_and_state_endpoints_and_approval_flow_over_http(server, svc):
    code, html = call(server, "/")
    assert code == 200 and b"Incident Commander" in html
    assert json.loads(call(server, "/api/state")[1])["mode"] == "simulator"
    assert json.loads(call(server, "/api/scenario/duplication", {})[1]) == {"started": "duplication"}
    svc.wait(60)
    apr = json.loads(call(server, "/api/state")[1])["approvals"][0]["id"]
    assert json.loads(call(server, f"/api/approvals/{apr}/approve", {"by": "ops"})[1])["status"] == "executed"
    assert json.loads(call(server, f"/api/approvals/{apr}/approve", {"by": "ops"})[1])["idempotent"] is True  # double click is safe
    with pytest.raises(urllib.error.HTTPError) as e:
        call(server, "/api/approvals/apr_nope/approve", {})
    assert e.value.code == 404


def test_ui_escapes_untrusted_text():
    """LLM output is untrusted: the page must never write raw data into HTML."""
    html = open(api.UI_PATH).read()
    assert "const esc =" in html and html.count("innerHTML") == 1  # the single sink is the cached put()
    for field in ("e.detail", "e.title", "t.request_id", "a.terms.summary", "c.text"):
        assert f"esc({field}" in html, field


def test_model_call_budget_fails_safe_to_escalation():
    from payops.llm import BudgetedProposer, HeuristicProposer
    from payops.models import Obligation, TransferRec
    t = [0.0]
    bp = BudgetedProposer(HeuristicProposer(), 2, now=lambda: t[0])
    ob = Obligation("o", "A", 10, "EUR", "SWIFT", "b")
    ts = [TransferRec("t1", "o", "A", "r", "SENT")]
    assert bp.propose(ob, ts, "x").action.kind == "WAIT" and bp.propose(ob, ts, "x").action.kind == "WAIT"
    assert bp.propose(ob, ts, "x").action.kind == "ESCALATE"  # budget gone: a human, not a guess
    t[0] = 3601
    assert bp.propose(ob, ts, "x").action.kind == "WAIT"  # window rolled


def test_approver_token_gates_approvals_when_configured(server, svc, monkeypatch):
    monkeypatch.setenv("PAYOPS_APPROVER_TOKEN", "s3cret")
    call(server, "/api/scenario/duplication", {})
    svc.wait(60)
    apr = json.loads(call(server, "/api/state")[1])["approvals"][0]["id"]

    def post(headers):
        req = urllib.request.Request(server + f"/api/approvals/{apr}/approve", json.dumps({"by": "x"}).encode(), {"content-type": "application/json", **headers})
        return urllib.request.urlopen(req)
    for h in ({}, {"x-approver-token": "wrong"}):
        with pytest.raises(urllib.error.HTTPError) as e:
            post(h)
        assert e.value.code == 403
    assert len(json.loads(call(server, "/api/state")[1])["incidents"][0]["transfers"]) == 1  # nothing moved without the token
    assert json.loads(post({"x-approver-token": "s3cret"}).read())["status"] == "executed"
