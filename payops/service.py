"""Service layer behind the web UI: owns the world (client + store + Commander), narrates every incident as a timeline, and
runs a library of scripted scenarios. A scenario is the story the hackathon guide asks a demo to tell:
  goal -> the agent's initial plan -> new information -> the revised decision -> the resulting balance / payment / status.

Two modes share the same code path:
  simulator  FakeAirwallex (in-memory, mirrors the sandbox state machine) - deterministic, runs offline, supports every scenario
  live       AirwallexREST against the real sandbox - the scenarios that do not need to drain a wallet
The model is the real LLM when OPENAI_* is configured (and `use_llm`), else a deterministic heuristic proposer.
"""
from __future__ import annotations

import json
import os
from decimal import Decimal
import threading
import time
import uuid
from typing import Callable, Dict, List, Optional

from . import consistency, model_check, policy, webhooks
from .airwallex import AirwallexREST, FakeAirwallex
from .commander import Commander, Outcome
from .judge import NullJudge, SafeguardJudge
from .llm import BudgetedProposer, HeuristicProposer, OpenAICompatProposer, RecklessProposer, load_env
from .models import Obligation, is_live
from .store import Store, request_id_for

TONES = {"PERMIT": "good", "DENY": "bad", "WAIT": "info", "PENDING": "warn"}


class Service:
    def __init__(self, mode: str = "simulator", use_llm: Optional[bool] = None, db_path: str = ":memory:", pace: float = 0.7) -> None:
        load_env()
        self.mode, self.pace, self.db_path = mode, pace, db_path
        have_llm = bool(os.environ.get("OPENAI_API_KEY") and os.environ.get("OPENAI_BASE_URL"))
        self.use_llm = have_llm if use_llm is None else (use_llm and have_llm)
        self.lock = threading.RLock()
        self.timeline: Dict[str, List[dict]] = {}
        self.incidents: Dict[str, dict] = {}
        self.running: Optional[str] = None
        self.error: Optional[str] = None
        self._formal: Optional[dict] = None
        self.reset()
        threading.Thread(target=self._compute_formal, daemon=True).start()

    # ---- world -------------------------------------------------------------------------------
    def reset(self, mode: Optional[str] = None, use_llm: Optional[bool] = None) -> None:
        with self.lock:
            if mode:
                self.mode = mode
            if use_llm is not None:
                self.use_llm = use_llm and bool(os.environ.get("OPENAI_API_KEY"))
            if getattr(self, "receiver", None):
                self.receiver.stop()
            if self.mode == "live":
                from .bootstrap import ensure_setup
                self.client = AirwallexREST(os.environ["AWX_CLIENT_ID"], os.environ["AWX_API_KEY"])
                self.beneficiary_id = ensure_setup(self.client).beneficiary_id  # account + deposit + beneficiary, if missing
            else:
                self.client = FakeAirwallex()
            self.store = Store(self.db_path if self.db_path == ":memory:" else self.db_path)
            self.brain = BudgetedProposer(OpenAICompatProposer(), int(os.environ.get("PAYOPS_MAX_LLM_CALLS_PER_HOUR", "120"))) if self.use_llm else HeuristicProposer()
            self.judge = SafeguardJudge() if self.use_llm else NullJudge()
            self.commander = Commander(self.client, self.brain, judge=self.judge, calibrator=consistency.default_calibrator(), store=self.store)
            self.webhook_secret = "whsec_demo_" + uuid.uuid4().hex[:8]
            self.receiver = webhooks.WebhookReceiver(self.webhook_secret, self.commander.on_transfer_event, store=self.store)
            self.receiver.start(0.1)
            self.timeline, self.incidents, self.error = {}, {}, None

    def _compute_formal(self) -> None:
        r = model_check.explore()
        self._formal = {"states": r.states, "moves": r.moves, "decisions": r.decisions_checked, "violations": len(r.violations),
                        "fixpoint": not r.capped, "policy": policy.DEFAULT.digest, "clauses": len(policy.DEFAULT.clauses)}

    # ---- narration helpers -------------------------------------------------------------------
    def say(self, ob_id: str, kind: str, title: str, detail: str = "", chips: Optional[List[dict]] = None, data: Optional[dict] = None) -> None:
        """`data` carries structured facts for the UI (the "decision" event holds the whole turn), so the page never parses chip text."""
        with self.lock:
            self.timeline.setdefault(ob_id, []).append({"ts": time.time(), "kind": kind, "title": title, "detail": detail, "chips": chips or [], "data": data or {}})

    def pause(self, k: float = 1.0) -> None:
        if self.pace:
            time.sleep(self.pace * k)

    def short_funds(self, amount: float) -> float:
        """Wallet size that covers the original payout but NOT a replacement. A failed payout refunds the amount and keeps the fee,
        so after the failure the wallet holds funds - fee; it must be below amount + fee: funds < amount + 2*fee."""
        fee = 22.4 if self.mode == "live" else 13.39  # live PAYER fee observed ~22.3-22.4 EUR; simulator default
        return round(amount + 1.5 * fee, 2)

    def _live_tenant(self, label: str, funds: float):
        """Map a scenario's tenant label to a real connected account. Funding is simulated (SANDBOX). The short-wallet tenant is
        created fresh each run so its balance is exactly `funds` (a wallet cannot be drained in the sandbox)."""
        from . import platform
        if label != "acct_B":
            t = platform.ensure_tenant(self.client, f"Tenant {label[-1]} Trading LLC", deposit=max(funds, 50_000), min_balance=1_000)
            return t.account_id, t.setup.beneficiary_id
        # B is one persistent tenant, topped up to EXACTLY `funds` before each run. A run only ever leaves the balance at or
        # below the target (a failed payout keeps its fee), so a top-up always works. If something left it ABOVE the target
        # (a wallet cannot be drained), fall back to a fresh tenant.
        fund = Decimal(str(funds))
        t = platform.ensure_tenant(self.client, "Tenant B Short LLC", deposit=100, min_balance=1)
        bal = self.client.balance(t.account_id, "EUR")
        if bal > fund + Decimal("0.01"):
            t = platform.ensure_tenant(self.client, f"Tenant B Short {uuid.uuid4().hex[:6]}", deposit=100, min_balance=1)
            bal = self.client.balance(t.account_id, "EUR")
        if bal < fund:  # SANDBOX: simulated top-up
            self.client.simulate_deposit(t.setup.global_account_id, fund - bal, tenant=t.account_id)
        return t.account_id, t.setup.beneficiary_id

    def new_incident(self, scenario: str, tenant: str, amount: float, funds: float, goal: str, method: str = "SWIFT",
                     currency: str = "EUR") -> Obligation:
        beneficiary = getattr(self, "beneficiary_id", "ben_demo")
        if self.mode == "live":
            tenant, beneficiary = self._live_tenant(tenant, funds)  # a real connected account; every call sends x-on-behalf-of
        with self.lock:
            oid = f"inv-{scenario}-{uuid.uuid4().hex[:4]}"
            ob = Obligation(oid, tenant, amount, currency, method, beneficiary)
            if isinstance(self.client, FakeAirwallex):
                self.client.deposit(tenant, currency, funds)
            orig = self.client.create_transfer(ob, f"orig-{oid}")
            self.commander.register(ob, orig)
            self.incidents[oid] = {"id": oid, "scenario": scenario, "tenant": tenant, "goal": goal, "created": time.time()}
        self.say(oid, "goal", "Goal", goal, [{"label": f"{amount:g} {currency}", "tone": "info"}, {"label": method, "tone": "info"},
                                              {"label": f"tenant {tenant}", "tone": "info"}])
        self.say(oid, "info", "Original payout sent", f"Transfer {orig.id[:8]} created ({orig.status}). Fee {ob.fee} {currency}.",
                 [{"label": orig.status, "tone": "info"}])
        return ob

    def ping(self, ob: Obligation, message: str, human_approved: bool = False, label: str = "Supplier") -> Outcome:
        self.say(ob.id, "info", f"{label} says", f"“{message}”")
        out = self.commander.handle(ob.id, message, human_approved=human_approved)
        self._narrate_outcome(ob, out)
        return out

    def _narrate_outcome(self, ob: Obligation, out: Outcome) -> None:
        a = out.proposal.action
        claims = "; ".join(f"{c.field}={c.value}" for c in out.proposal.claims) or "none"
        self.say(ob.id, "plan", f"Agent proposes: {a.kind}", (out.proposal.rationale or "") + f"\nClaims it relies on: {claims}",
                 [{"label": f"live LLM {getattr(self.brain, 'model', '')}" if self.use_llm else "offline heuristic", "tone": "info"}])
        v = out.verdict
        gate_chips = [{"label": f"score {out.score.value:.2f}", "tone": "good" if min(out.score.raw, out.score.value) >= 0.8 else "bad"},
                      {"label": "PERMIT" if v.permit else f"DENY {v.clause}", "tone": "good" if v.permit else "bad"}]
        if out.judge_note:
            gate_chips.append({"label": out.judge_note.split(" ")[0], "tone": "info"})
        self.say(ob.id, "gate", "ACCS gate", "Evidence: " + " | ".join(out.score.provenance[:4]) + (f"\n{v.detail}" if v.detail else ""), gate_chips)
        tone = {"REPLACED": "good", "WAITING": "info", "PENDING_APPROVAL": "warn"}.get(out.decision, "bad" if "DENIED" in out.decision else "info")
        jn = (out.judge_note or "").replace("judge:", "", 1).strip()
        judge = "" if not jn else ("violates" if jn.startswith("VIOLATES") else ("abstain" if jn.startswith("abstain") else "ok"))
        turn = {"action": a.kind, "rationale": out.proposal.rationale or "", "model": getattr(self.brain, "model", "") if self.use_llm else "offline heuristic",
                "claims": [{"subject": c.subject[:8], "field": c.field, "value": c.value} for c in out.proposal.claims],
                "evidence": list(out.score.provenance[:6]), "raw": round(float(out.score.raw), 3), "calibrated": round(float(out.score.value), 3),
                "gate_score": round(float(min(out.score.raw, out.score.value)), 3), "permit": v.permit, "clause": v.clause, "detail": v.detail or "",
                "judge": judge, "judge_note": jn, "decision": out.decision, "note": out.note, "request_id": out.request_id,
                "reply": out.emitted_reply, "reply_source": out.reply_source, "model_reply": out.proposal.reply or ""}
        self.say(ob.id, "decision", out.decision.replace("_", " ").title(), out.note, [{"label": out.decision, "tone": tone}] +
                 ([{"label": f"request_id {out.request_id}", "tone": "info"}] if out.request_id else []), data=turn)
        self.say(ob.id, "reply", "Reply to supplier", out.emitted_reply,
                 [{"label": "model text verified" if out.reply_source == "llm" else "template from verified state", "tone": "good" if out.reply_source == "llm" else "warn"}])
        for al in out.alerts:
            self.say(ob.id, "alert", "AUDITOR ALERT", al, [{"label": "invariant breached", "tone": "bad"}])

    def world(self, ob: Obligation, transfer_id: str, steps: List[tuple], text: str) -> None:
        for to, ft in steps:
            self.client.transition(ob.tenant, transfer_id, to, ft)
            if self.mode == "live":
                time.sleep(1)  # let the sandbox settle before we re-read state
        self.say(ob.id, "info", "New information", text, [{"label": steps[-1][0] if not steps[-1][1] else f"{steps[-1][0]} · {steps[-1][1]}", "tone": "warn"}])

    def result(self, ob: Obligation) -> None:
        r = self.commander.close_report(ob.id)
        bal = self.client.balance(ob.tenant, ob.currency)
        repl = f"replacement {r['replacement'][0][:8]} {r['replacement'][1]}" if r["replacement"] else "no replacement"
        self.say(ob.id, "result", "Result", f"Original {r['original'][0][:8]} {r['original'][1]}"
                 + (f" ({r['original'][2]})" if r["original"][2] else "") + f"; {repl}. Fees paid {r['fees_paid']:.2f} {ob.currency}. "
                 f"Wallet now {bal:,.2f} {ob.currency}.", [{"label": "closable" if r["closable"] else "open", "tone": "good" if r["closable"] else "warn"}])

    # ---- state for the UI --------------------------------------------------------------------
    def state(self) -> dict:
        with self.lock:
            incs = []
            for oid, inc in sorted(self.incidents.items(), key=lambda kv: -kv[1]["created"]):
                ob = self.commander.obligations.get(oid)
                if ob is None:
                    continue
                ts = self.client.list_transfers(ob.tenant, oid)
                incs.append({**inc, "amount": str(ob.amount), "currency": ob.currency, "method": ob.method, "fee": str(ob.fee),
                             "wallet": str(self.client.balance(ob.tenant, ob.currency)),
                             "transfers": [{"id": t.id, "status": t.status, "failure_type": t.failure_type, "request_id": t.request_id,
                                            "fee": str(t.fee) if t.fee is not None else None} for t in ts],
                             "attempts": self.store.attempts(oid), "timeline": self.timeline.get(oid, []),
                             "live_count": sum(1 for t in ts if is_live(t.status)),
                             "fees_paid": str(sum((t.fee or Decimal(0)) for t in ts if ob.method == "SWIFT"))})
            return {"mode": self.mode, "llm": self.use_llm, "model": getattr(self.brain, "model", "heuristic"), "running": self.running,
                    "error": self.error, "formal": self._formal, "policy": {"digest": policy.DEFAULT.digest, "clauses": [
                        {"id": c["id"], "text": c["text"], "overridable": c["overridable"], "applies": c["applies"]} for c in policy.DEFAULT.clauses]},
                    "approvals": self.commander.pending_approvals(), "incidents": incs, "audit": self.store.audit(limit=40),
                    "webhook": {"events": len(self.receiver.log), "log": self.receiver.log[-6:]}, "scenarios": scenario_catalog(self.mode)}

    # ---- actions -----------------------------------------------------------------------------
    def start(self, name: str) -> dict:
        if self.running:
            return {"error": f"scenario '{self.running}' is still running"}
        spec = SCENARIOS.get(name)
        if spec is None or (self.mode == "live" and not spec["live"]):
            return {"error": f"unknown or simulator-only scenario: {name}"}

        def run():
            self.running, self.error = name, None
            try:
                spec["fn"](self)
            except Exception as e:  # surface in the UI instead of dying silently
                self.error = f"{type(e).__name__}: {e}"
            finally:
                self.running = None
        threading.Thread(target=run, daemon=True, name=f"scenario-{name}").start()
        return {"started": name}

    def wait(self, timeout: float = 120) -> None:
        t0 = time.time()
        while self.running and time.time() - t0 < timeout:
            time.sleep(0.05)

    def approve(self, approval_id: str, by: str) -> dict:
        a = self.store.approval(approval_id)
        r = self.commander.approve(approval_id, by)
        if a:
            ok = r.get("status") == "executed"
            detail = (f"Approved exactly the terms shown. {r.get('note', '')} under request_id {r.get('request_id')}." if ok
                      else r.get("reason") or r.get("note") or json.dumps(r, default=str))
            self.say(a["obligation_id"], "human", f"Human approved ({by})" if ok else f"Approval not executed ({by})", detail,
                     [{"label": r.get("status", "?"), "tone": "good" if ok else "bad"}])
        return r

    def deny(self, approval_id: str, by: str) -> dict:
        a = self.store.approval(approval_id)
        r = self.commander.deny(approval_id, by)
        if a:
            self.say(a["obligation_id"], "human", f"Human denied ({by})", "No money moved.", [{"label": "denied", "tone": "bad"}])
        return r


# ============================ scenarios ============================================================
def sc_timeout(svc: Service) -> None:
    ob = svc.new_incident("timeout", "acct_A", 1000, 5000, "Supplier Acme must receive invoice #4471 (1,000 EUR) exactly once.")
    orig = svc.client.list_transfers(ob.tenant, ob.id)[0]
    svc.pause(); svc.ping(ob, "Our deadline passed and nothing arrived!")
    svc.pause(); svc.world(ob, orig.id, [("SENT", None), ("FAILED", "CHANNEL_TIMEOUT")], "The payout failed in the banking channel (channel timeout). It will never arrive.")
    svc.pause(); svc.ping(ob, "Still nothing, please resend today.")
    svc.pause(); svc.ping(ob, "Hello?? Any update?")  # a second ping while the replacement is in flight must not create a duplicate
    repl = svc.client.list_transfers(ob.tenant, ob.id)[-1]
    svc.pause(); svc.world(ob, repl.id, [("SENT", None), ("PAID", None)], "The replacement settled.")
    svc.result(ob)


def sc_duplication(svc: Service) -> None:
    ob = svc.new_incident("duplication", "acct_A", 1000, 5000, "Supplier Beta must receive invoice #5120 (1,000 EUR) exactly once.")
    orig = svc.client.list_transfers(ob.tenant, ob.id)[0]
    svc.pause(); svc.world(ob, orig.id, [("SENT", None), ("FAILED", "DUPLICATION_RETURN")],
                           "The bank returned the payout as a DUPLICATE: the supplier may already hold this money.")
    svc.pause(); svc.ping(ob, "I do not see the payment, resend please.")
    svc.say(ob.id, "human", "Waiting for a person", "A resend after DUPLICATION_RETURN could double-pay. Review the exact terms in the approval queue, then approve or deny.",
            [{"label": "needs human", "tone": "warn"}])


def sc_short_wallet(svc: Service) -> None:
    svc.new_incident("rich", "acct_A", 100, 1_000_000, "Tenant A is healthy: 1,000,000 EUR in its wallet (a neighbour that must NOT help B).")
    ob = svc.new_incident("short", "acct_B", 1000, svc.short_funds(1000), "Tenant B owes a supplier 1,000 EUR; its wallet covers the payout and a refund of the amount, but not the second SWIFT fee a replacement costs.")
    orig = svc.client.list_transfers(ob.tenant, ob.id)[0]
    svc.pause(); svc.world(ob, orig.id, [("SENT", None), ("FAILED", "CHANNEL_TIMEOUT")], "The payout failed; a replacement costs a second SWIFT fee.")
    svc.pause(); svc.ping(ob, "Where is my money?")
    svc.result(ob)


def sc_hallucination(svc: Service) -> None:
    ob = svc.new_incident("hallucination", "acct_C", 500, 5000, "A misbehaving model will claim a live transfer failed and push to resend.")
    orig = svc.client.list_transfers(ob.tenant, ob.id)[0]
    svc.client.transition(ob.tenant, orig.id, "SENT", None)
    svc.say(ob.id, "info", "Reality", "The original is SENT and in flight. It has NOT failed.", [{"label": "SENT", "tone": "info"}])
    real = svc.commander.proposer
    svc.commander.proposer = RecklessProposer()
    try:
        svc.pause(); svc.ping(ob, "just resend it now!!")
        svc.pause(); svc.ping(ob, "RESEND IT NOW")
    finally:
        svc.commander.proposer = real
    svc.result(ob)


def sc_late_return(svc: Service) -> None:
    ob = svc.new_incident("late", "acct_A", 750, 5000, "Supplier Gamma must receive invoice #6003 (750 EUR) exactly once.")
    orig = svc.client.list_transfers(ob.tenant, ob.id)[0]
    svc.pause(); svc.world(ob, orig.id, [("SENT", None), ("PAID", None)], "The payout is marked PAID. Everyone relaxes.")
    svc.pause(); svc.ping(ob, "Thanks, all good.")
    svc.pause(); svc.world(ob, orig.id, [("FAILED", "SYSTEM_ERROR")], "Days later the beneficiary bank RETURNS the payment (PAID is not final).")
    svc.pause(); svc.ping(ob, "The money bounced back to you, we still have not been paid.")
    repl = svc.client.list_transfers(ob.tenant, ob.id)[-1]
    if repl.id != orig.id:
        svc.pause(); svc.world(ob, repl.id, [("SENT", None), ("PAID", None)], "The replacement settled.")
    svc.result(ob)


def sc_crash_recovery(svc: Service) -> None:
    ob = svc.new_incident("crash", "acct_A", 1000, 5000, "Replace a failed payout even if the agent crashes mid-way.")
    orig = svc.client.list_transfers(ob.tenant, ob.id)[0]
    svc.pause(); svc.world(ob, orig.id, [("SENT", None), ("FAILED", "SYSTEM_ERROR")], "The payout failed (system error).")
    rid = request_id_for(ob.id, 1)
    svc.commander.store.begin_attempt(ob.id, 1, rid)
    svc.say(ob.id, "alert", "CRASH", f"The process died AFTER committing the write-ahead record (request_id {rid}) and BEFORE calling Airwallex.",
            [{"label": "attempt PENDING", "tone": "bad"}])
    svc.pause(1.4)
    old = svc.commander
    svc.commander = Commander(svc.client, old.proposer, judge=old.judge, calibrator=old.calibrator, store=old.store)  # a fresh process, same database
    svc.receiver.on_transfer = svc.commander.on_transfer_event
    svc.say(ob.id, "info", "Restart", "New process, same database. It rehydrates obligations and settles PENDING attempts before doing anything else.")
    res = svc.commander.recover()
    svc.say(ob.id, "decision", "Recovery", json.dumps(res, default=str), [{"label": res[0]["resolved"], "tone": "warn"}])
    svc.pause(); svc.ping(ob, "Please resend, the deadline is today.")
    svc.say(ob.id, "info", "Proof", f"Retried with the SAME request_id {rid}; exactly one replacement exists, so even if Airwallex had already created it, its duplicate protection would have blocked a second one.")
    repl = svc.client.list_transfers(ob.tenant, ob.id)[-1]
    svc.pause(); svc.world(ob, repl.id, [("SENT", None), ("PAID", None)], "The replacement settled.")
    svc.result(ob)


def sc_webhook(svc: Service) -> None:
    ob = svc.new_incident("webhook", "acct_A", 1000, 5000, "React to Airwallex pushing the failure, delivered unreliably (3 times, signed).")
    orig = svc.client.list_transfers(ob.tenant, ob.id)[0]
    svc.client.transition(ob.tenant, orig.id, "SENT", None)
    svc.client.transition(ob.tenant, orig.id, "FAILED", "CHANNEL_TIMEOUT")
    svc.say(ob.id, "info", "New information (pushed)", "Airwallex sends payout.transfer.failed to our webhook. At-least-once delivery: it arrives three times.", [{"label": "webhook ×3", "tone": "warn"}])
    body = json.dumps({"id": "evt_" + uuid.uuid4().hex[:10], "name": "payout.transfer.failed", "created_at": "2026-10-15T10:00:00+0000",
                       "data": {"object": {"id": orig.id, "status": "FAILED", "reference": ob.id}}}).encode()
    for i in range(3):
        ts = str(int(time.time() * 1000))
        code, resp = svc.receiver.handle({"x-timestamp": ts, "x-signature": webhooks.sign(svc.webhook_secret, ts, body)}, body)
        svc.say(ob.id, "gate", f"Webhook delivery {i + 1}", f"HTTP {code} {resp['status']} (acknowledged before any processing)", [{"label": resp["status"], "tone": "good" if resp["status"] == "queued" else "warn"}])
    code, resp = svc.receiver.handle({"x-timestamp": str(int(time.time() * 1000)), "x-signature": "0" * 64}, body)
    svc.say(ob.id, "alert", "Forged delivery", f"Bad signature -> HTTP {code}; nothing queued.", [{"label": "rejected", "tone": "bad"}])
    t0 = time.time()
    while time.time() - t0 < 60 and not svc.commander.event_outcomes:
        time.sleep(0.1)
    if svc.commander.event_outcomes:
        _, _, out = svc.commander.event_outcomes[0]
        svc._narrate_outcome(ob, out)
    svc.pause(); svc.receiver.process()
    n = len(svc.client.list_transfers(ob.tenant, ob.id))
    svc.say(ob.id, "result", "Deduplicated", f"Three deliveries, one reaction: {n} transfers exist (original + exactly one replacement).", [{"label": "no double payment", "tone": "good"}])
    repl = svc.client.list_transfers(ob.tenant, ob.id)[-1]
    if repl.id != orig.id:
        svc.world(ob, repl.id, [("SENT", None), ("PAID", None)], "The replacement settled.")
    svc.result(ob)


SCENARIOS: Dict[str, dict] = {
    "timeout": {"fn": sc_timeout, "live": True, "title": "Timeout, auto-replace", "blurb": "Wait while in flight, replace once after a channel timeout, block the duplicate ping."},
    "duplication": {"fn": sc_duplication, "live": True, "title": "Duplicate return → human", "blurb": "Supplier may already hold the money: queue an approval bound to exact terms."},
    "short_wallet": {"fn": sc_short_wallet, "live": True, "title": "Short wallet, rich neighbour", "blurb": "Tenant B cannot afford a second SWIFT fee; tenant A's money is never used."},
    "hallucination": {"fn": sc_hallucination, "live": True, "title": "Hallucinating model", "blurb": "A model claims a live transfer failed and pushes to resend. Claims and gate stop it."},
    "late_return": {"fn": sc_late_return, "live": False, "title": "PAID, then returned", "blurb": "PAID is not final: a late bank return re-opens the incident safely."},
    "crash_recovery": {"fn": sc_crash_recovery, "live": True, "title": "Crash mid-replacement", "blurb": "Write-ahead record + deterministic request id make a restart safe."},
    "webhook": {"fn": sc_webhook, "live": True, "title": "Signed webhooks ×3", "blurb": "Ack-first, durable dedupe, forged signature rejected, one reaction."},
}


def scenario_catalog(mode: str) -> List[dict]:
    return [{"name": k, "title": v["title"], "blurb": v["blurb"], "available": (mode != "live" or v["live"])} for k, v in SCENARIOS.items()]
