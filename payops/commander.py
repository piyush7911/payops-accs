"""Incident Commander: LLM proposes -> consistency.score -> policy.check (+ judge) -> execute | queue for approval | escalate.

Emission guarantee: no action reaches Airwallex and no text reaches the supplier unless it passed the gates.
  * actions: claims corroborated AND policy.check permits AND the judge (if any) does not object
  * replies: every claim corroborated AND no money-movement promise the system did not actually perform; otherwise a
    code-built template from verified state is sent instead of the model's text
Never pays twice, even across crashes:
  * the request_id of a replacement is minted by CODE, deterministically, from (obligation, attempt number); the model never
    chooses it, and a retry of the same attempt reuses it, so Airwallex's own duplicate protection backs ours up
  * a write-ahead PENDING row is committed before the Airwallex call; after an ambiguous failure or a restart we reconcile by
    request_id (`recover`) and never invent a second payment
Humans: a denial by an *overridable* clause becomes an approval bound to the exact terms shown (amount, currency, beneficiary,
method, request_id, latest transfer state, policy version). If anything changed by approval time the approval expires. Hard
clauses (single live payment, tenant isolation, fee budget, ...) are never overridable.
"""
from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from . import consistency, policy
from .judge import NullJudge
from .models import Action, Claim, Obligation, Proposal, TransferRec
from .store import Store, digest, request_id_for

PROMISE = re.compile(r"\b(resent|re-sent|resend|replacement|refund|will arrive|arrive today|has been paid|was paid|transferred|credited)\b", re.I)


@dataclass
class Outcome:
    decision: str  # WAITING | REPLACED | ESCALATED | DENIED_ESCALATED | PENDING_APPROVAL | CLOSED
    verdict: policy.Verdict
    score: consistency.ScoreRecord
    proposal: Proposal
    note: str = ""
    emitted_reply: str = ""
    reply_source: str = ""  # "llm" | "template"
    judge_note: str = ""
    alerts: List[str] = field(default_factory=list)  # independent auditor findings after the step
    approval_id: Optional[str] = None
    request_id: Optional[str] = None


class Commander:
    def __init__(self, client, proposer, judge=None, calibrator=None, pol: Optional[policy.Policy] = None,
                 store: Optional[Store] = None) -> None:
        self.client, self.proposer = client, proposer
        self.judge = judge or NullJudge()
        self.calibrator, self.policy = calibrator, pol or policy.DEFAULT
        self.store = store or Store(":memory:")
        self.obligations: Dict[str, Obligation] = {o.id: o for o in self.store.obligations()}  # survives a restart
        self.audit: List[dict] = []
        self.event_outcomes: List[tuple] = []
        self._locks: Dict[str, threading.Lock] = {}

    # ---- state -------------------------------------------------------------------------------
    @property
    def used_request_ids(self) -> set:
        return self.store.used_request_ids()

    def register(self, ob: Obligation, original: TransferRec) -> None:
        self.obligations[ob.id] = ob
        self.store.add_obligation(ob)
        self.store.begin_attempt(ob.id, 0, original.request_id)
        self.store.finish_attempt(original.request_id, original.id)

    def _log(self, oid: str, kind: str = "decision", **kw) -> None:
        self.audit.append({"obligation": oid, **kw})
        self.store.log(oid, kind, kw)

    def recover(self) -> List[dict]:
        """After a crash/restart: settle every PENDING attempt by asking Airwallex for its request_id."""
        out = []
        for a in self.store.pending_attempts():
            ob = self.obligations.get(a["obligation_id"]) or self.store.obligation(a["obligation_id"])
            found = self.client.find_by_request_id(ob.tenant, a["request_id"]) if ob else None
            if found:
                self.store.finish_attempt(a["request_id"], found.id)
                out.append({"request_id": a["request_id"], "resolved": "CREATED", "transfer": found.id})
            else:
                out.append({"request_id": a["request_id"], "resolved": "STILL_PENDING",
                            "note": "no transfer found; the next run retries with the SAME request_id (safe: duplicate protection)"})
            self._log(a["obligation_id"], "recovery", **out[-1])
        return out

    # ---- main path ---------------------------------------------------------------------------
    def handle(self, obligation_id: str, supplier_message: str, human_approved: bool = False,
               overrides: frozenset = frozenset(), _forced: Optional[Proposal] = None) -> Outcome:
        ob = self.obligations[obligation_id]
        lock = self._locks.setdefault(obligation_id, threading.Lock())
        with lock:  # one incident handler per obligation at a time
            transfers = self.client.list_transfers(ob.tenant, ob.id)  # always re-read live state
            self._mirror_original(ob, transfers)
            attempt_no = len(transfers)
            rid = request_id_for(ob.id, attempt_no)  # minted by code, never by the model
            prop = _forced or self.proposer.propose(ob, transfers, supplier_message)
            if prop.action.kind == "REPLACE":
                prop.action.request_id = rid
            evidence = consistency.evidence_from_transfers(transfers, ob.id)
            sc = consistency.min_score(prop.claims, evidence, self.calibrator)
            # gate on min(raw, calibrated): calibration may tighten the rule but never loosen it. A claim with no evidence
            # has raw 0.5 and must not pass even if claims like it are usually true.
            gate_score = min(sc.raw, sc.value)
            ctx = policy.Context(ob, transfers, self.store.used_request_ids(retrying=rid), self.client.balance(ob.tenant, ob.currency),
                                 gate_score, human_approved, frozenset(overrides))
            verdict = policy.check(prop.action, ctx, self.policy)
            judge_note = ""
            if verdict.permit and prop.action.kind in ("REPLACE", "CLOSE") and _forced is None:  # second opinion; can only restrict
                claims_txt = "; ".join(f"{c.subject}.{c.field}={c.value}" for c in prop.claims)
                extra = {"request_id_previously_used": (prop.action.request_id or "").lower() in {x.lower() for x in ctx.used_request_ids},
                         "wallet_balance": str(ctx.wallet_balance), "needed_amount_plus_fee": str(ob.amount + ob.fee),
                         "human_approved": human_approved, "claims_consistency": round(sc.value, 3)}
                jv = self.judge.review(self.policy, prop.action, ob, transfers, claims_txt, extra)
                judge_note = f"judge:{'abstain' if jv.abstained else ('VIOLATES ' + str(jv.clause) if jv.violates else 'ok')} {jv.reason}"
                if jv.violates and not jv.abstained:
                    verdict = policy.Verdict(False, f"JUDGE:{jv.clause}", jv.reason)
            self._log(ob.id, proposed=prop.action.kind, permit=verdict.permit, clause=verdict.clause, detail=verdict.detail,
                      score=round(sc.value, 3), raw=round(sc.raw, 3), provenance=sc.provenance, rationale=prop.rationale,
                      policy=self.policy.digest, judge=judge_note, request_id=rid if prop.action.kind == "REPLACE" else None,
                      overrides=sorted(ctx.overrides))
            out = self._act(ob, prop, verdict, sc, transfers, rid, attempt_no)
            out.judge_note, out.request_id = judge_note, rid if prop.action.kind == "REPLACE" else None
            self._gate_reply(out, ob, transfers)
            # independent auditor on fresh state after EVERY step: an invariant breach is flagged in the step it occurs
            out.alerts = policy.violations({ob.id: self.client.list_transfers(ob.tenant, ob.id)})
            if out.alerts:
                self._log(ob.id, "alert", ALERT=out.alerts)
            return out

    @staticmethod
    def _mirror_original(ob: Obligation, transfers: List[TransferRec]) -> None:
        """A replacement is the same payment: use the original's fee for budgeting and its swift_charge_option for delivery
        terms (the fee differs by option: 13.39 SHARED vs 22.29 PAYER observed), instead of guessing."""
        if transfers and transfers[0].fee is not None and ob.observed_fee is None:
            ob.observed_fee = transfers[0].fee
        if transfers and transfers[0].charge_option:
            ob.swift_charge_option = transfers[0].charge_option

    def _act(self, ob, prop, verdict, sc, transfers, rid, attempt_no) -> Outcome:
        if not verdict.permit:
            clause = verdict.clause or ""
            if prop.action.kind == "REPLACE" and clause in self.policy.overridable():
                terms = self._terms(ob, rid, transfers, clause, prop.claims)
                apr = self.store.add_approval(ob.id, clause, terms)
                self._log(ob.id, "approval_requested", approval=apr["id"], clause=clause)
                return Outcome("PENDING_APPROVAL", verdict, sc, prop, f"{clause}: {verdict.detail}; waiting for human approval {apr['id']}",
                               approval_id=apr["id"])
            return Outcome("DENIED_ESCALATED", verdict, sc, prop, f"blocked by {verdict.clause}: {verdict.detail}; sent to human")
        kind = prop.action.kind
        if kind == "WAIT":
            return Outcome("WAITING", verdict, sc, prop, "transfer still in flight")
        if kind == "ESCALATE":
            return Outcome("ESCALATED", verdict, sc, prop, prop.rationale)
        if kind == "REPLACE":
            return self._replace(ob, prop, verdict, sc, rid, attempt_no)
        return Outcome("CLOSED", verdict, sc, prop)

    # ---- human approvals ---------------------------------------------------------------------
    def _terms(self, ob: Obligation, rid: str, transfers: List[TransferRec], clause: str, claims: List[Claim]) -> dict:
        last = transfers[-1] if transfers else None
        return {"obligation": ob.id, "tenant": ob.tenant, "amount": str(ob.amount), "currency": ob.currency, "method": ob.method,
                "beneficiary_id": ob.beneficiary_id, "request_id": rid, "clause": clause, "policy": self.policy.digest,
                "latest_transfer": {"id": last.id, "status": last.status, "failure_type": last.failure_type} if last else None,
                "claims": [[c.subject, c.field, c.value] for c in claims],
                "summary": f"Resend {ob.amount} {ob.currency} to beneficiary {ob.beneficiary_id} by {ob.method} "
                           f"(+{ob.fee} {ob.currency} fee) after {last.failure_type if last else 'n/a'} on {last.id[:8] if last else 'n/a'}."}

    def approve(self, approval_id: str, approver: str) -> dict:
        """Execute exactly what was approved, or refuse if the world changed. Idempotent: a second call changes nothing."""
        a = self.store.approval(approval_id)
        if a is None:
            raise KeyError(approval_id)
        if a["status"] != "pending":
            return {"status": a["status"], "idempotent": True, "result": a["result"]}
        terms = json.loads(a["terms"])
        ob = self.obligations[a["obligation_id"]]
        transfers = self.client.list_transfers(ob.tenant, ob.id)
        rid = request_id_for(ob.id, len(transfers))
        claims = [Claim(s, f, v) for s, f, v in terms["claims"]]
        fresh = self._terms(ob, rid, transfers, a["clause"], claims)
        if digest(fresh) != a["terms_digest"]:
            self.store.resolve_approval(approval_id, "expired", approver, "state changed since the approval was requested")
            self._log(ob.id, "approval_expired", approval=approval_id)
            return {"status": "expired", "reason": "state changed since the approval was requested; nothing was executed"}
        forced = Proposal(Action("REPLACE", ob.id, ob.tenant, rid, ob.tenant, f"approved by {approver}"), claims, f"human-approved ({approver})")
        out = self.handle(ob.id, f"[approved by {approver}]", overrides=frozenset({a["clause"]}), _forced=forced)
        status = "executed" if out.decision == "REPLACED" else "failed"
        self.store.resolve_approval(approval_id, status, approver, f"{out.decision}: {out.note}")
        self._log(ob.id, "approval_resolved", approval=approval_id, approver=approver, status=status)
        return {"status": status, "decision": out.decision, "note": out.note, "request_id": rid}

    def deny(self, approval_id: str, approver: str) -> dict:
        a = self.store.approval(approval_id)
        if a is None:
            raise KeyError(approval_id)
        ok = self.store.resolve_approval(approval_id, "denied", approver, "denied by human")
        if ok:
            self._log(a["obligation_id"], "approval_resolved", approval=approval_id, approver=approver, status="denied")
        return {"status": "denied" if ok else a["status"], "idempotent": not ok}

    def pending_approvals(self) -> List[dict]:
        return [{**a, "terms": json.loads(a["terms"])} for a in self.store.approvals("pending")]

    # ---- webhooks / replies / replace / close ------------------------------------------------
    def on_transfer_event(self, name: str, obj: dict) -> Optional[Outcome]:
        """Webhook entry point. Only a terminal state change on a transfer we track re-opens the incident; the agent then
        re-reads live state (it never trusts the webhook payload for money decisions) and runs the normal gated path."""
        ob_id = obj.get("reference")
        status = str(obj.get("status", "")).upper()
        if ob_id not in self.obligations or status not in ("FAILED", "CANCELLED", "PAID"):
            return None
        out = self.handle(ob_id, f"[webhook {name}] transfer {obj.get('id', '')[:8]} is now {status}")
        self.event_outcomes.append((name, obj.get("id"), out))
        return out

    def _gate_reply(self, out: Outcome, ob: Obligation, transfers: List[TransferRec]) -> None:
        """Emission path for text: model's reply only if corroborated and it promises nothing we did not do."""
        reply = out.proposal.reply.strip()
        promised = bool(PROMISE.search(reply))
        did_replace = out.decision == "REPLACED"
        thr = self.policy.params["min_consistency"]
        ok = bool(reply) and out.proposal.claims and min(out.score.raw, out.score.value) >= thr and (did_replace or not promised) and out.verdict.permit
        if ok:
            out.emitted_reply, out.reply_source = reply, "llm"
        else:
            out.emitted_reply, out.reply_source = self._template(out, ob, transfers), "template"
            if reply:
                self._log(ob.id, "reply_blocked", reply_blocked=reply, score=round(out.score.value, 3), promised=promised)

    @staticmethod
    def _template(out: Outcome, ob: Obligation, transfers: List[TransferRec]) -> str:
        last = transfers[-1] if transfers else None
        state = f"transfer {last.id[:8]} is {last.status}" if last else "no transfer found"
        tail = {"WAITING": "It is still in flight; we will update you when it settles.",
                "REPLACED": "A replacement payment has been created under a new reference.",
                "PENDING_APPROVAL": "A person is reviewing whether to send a replacement and will follow up.",
                "DENIED_ESCALATED": "We have escalated this to a person who will follow up.",
                "ESCALATED": "We have escalated this to a person who will follow up."}.get(out.decision, "")
        return f"Update on {ob.id}: {state}. {tail}".strip()

    def _replace(self, ob: Obligation, prop: Proposal, verdict, sc, rid: str, attempt_no: int) -> Outcome:
        validate = getattr(self.client, "validate_transfer", None)
        if validate:  # free dry run: a failed SWIFT payout still costs a fee, so never pay for a transfer that will be rejected
            ok, why = validate(ob, rid)
            if not ok:
                self._log(ob.id, "preflight_failed", request_id=rid, why=why)
                return Outcome("ESCALATED", verdict, sc, prop, f"preflight validation failed ({why}); nothing was sent; a person must fix the beneficiary")
        self.store.begin_attempt(ob.id, attempt_no, rid)  # write-ahead: committed BEFORE money can move
        try:
            t = self.client.create_transfer(ob, rid)
        except Exception as e:  # ambiguous: reconcile by request_id, do NOT mint a different id
            found = self.client.find_by_request_id(ob.tenant, rid)
            if found:
                self.store.finish_attempt(rid, found.id)
                self._log(ob.id, "reconciled", transfer=found.id, error=repr(e))
                return Outcome("REPLACED", verdict, sc, prop, f"reconciled {found.id} after {e!r}")
            self._log(ob.id, "unknown_outcome", request_id=rid, error=repr(e))
            return Outcome("ESCALATED", verdict, sc, prop, f"create failed ({e!r}); no transfer found; attempt stays PENDING, "
                                                           f"the next run retries with the same request_id")
        self.store.finish_attempt(rid, t.id)
        self.store.add_obligation(ob)  # persists the observed SWIFT fee
        return Outcome("REPLACED", verdict, sc, prop, f"replacement {t.id} created")

    def close_report(self, obligation_id: str) -> dict:
        """Incident is closed only when BOTH the original's fate and the replacement are recorded."""
        ob = self.obligations[obligation_id]
        ts = self.client.list_transfers(ob.tenant, ob.id)
        orig, repl = ts[0], (ts[1] if len(ts) > 1 else None)
        terminal = ("PAID", "CANCELLED")
        done = orig.status == "PAID" or (orig.status == "CANCELLED" and repl is not None and repl.status in terminal)
        return {"obligation": ob.id, "original": (orig.id, orig.status, orig.failure_type),
                "replacement": (repl.id, repl.status) if repl else None,
                "fees_paid": float(ob.fee * len(ts)) if ob.method == "SWIFT" else 0.0, "closable": done}
