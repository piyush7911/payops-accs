"""Proposers: turn an incident snapshot + supplier message into a Proposal. The LLM never executes anything.

ClaudeProposer calls the Anthropic Messages API (stdlib HTTP, needs ANTHROPIC_API_KEY). It sees a redacted snapshot,
no credentials. HeuristicProposer is the offline fallback. RecklessProposer simulates a misbehaving model for the
demo/tests: it always asks to replace, and may assert false facts. The gate + scorer must stop it.
"""
from __future__ import annotations

import json
import os
import urllib.request
import uuid
from typing import List

from .models import Action, Claim, Obligation, Proposal, TransferRec, is_in_flight

MODEL = os.environ.get("PAYOPS_MODEL", "claude-sonnet-5-5")

SYSTEM = """You are a payment-ops incident analyst. A supplier says a payout did not arrive.
Choose exactly one action: WAIT (transfer still in flight), REPLACE (original ended CANCELLED with a failure type),
or ESCALATE (unclear / risky). List every factual claim you rely on about the transfers as claims.
Reply with JSON only: {"action": "...", "claims": [{"subject": "<transfer id>", "field": "status|failure_type", "value": "..."}], "rationale": "...", "reply": "<2-sentence message to the supplier>"}.
Every fact stated in your reply must also appear in claims. Do not promise a payment date or confirm money moved unless it is in the data.
You cannot move money. A separate policy gate and evidence check will verify everything you say."""


def _snapshot(ob: Obligation, transfers: List[TransferRec]) -> dict:
    return {"obligation": {"id": ob.id, "amount": ob.amount, "currency": ob.currency, "method": ob.method},
            "transfers": [{"id": t.id, "status": t.status, "failure_type": t.failure_type} for t in transfers]}


def _to_proposal(d: dict, ob: Obligation) -> Proposal:
    kind = d.get("action", "ESCALATE")
    if kind not in ("WAIT", "REPLACE", "ESCALATE", "CLOSE"):
        kind = "ESCALATE"
    rid = str(uuid.uuid4()) if kind == "REPLACE" else None  # fresh id per new payment, minted by code not the model
    claims = [Claim(c["subject"], c["field"], str(c["value"])) for c in d.get("claims", []) if {"subject", "field", "value"} <= set(c)]
    return Proposal(Action(kind, ob.id, ob.tenant, rid, ob.tenant, d.get("rationale", "")), claims, d.get("rationale", ""), str(d.get("reply", "")))


class ClaudeProposer:
    def propose(self, ob: Obligation, transfers: List[TransferRec], message: str) -> Proposal:
        body = {"model": MODEL, "max_tokens": 600, "system": SYSTEM,
                "messages": [{"role": "user", "content": json.dumps({"supplier_message": message, **_snapshot(ob, transfers)}, default=str)}]}
        req = urllib.request.Request("https://api.anthropic.com/v1/messages", json.dumps(body).encode(), {
            "content-type": "application/json", "x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01"})
        with urllib.request.urlopen(req, timeout=60) as r:
            text = json.loads(r.read())["content"][0]["text"]
        try:
            return _to_proposal(json.loads(text[text.index("{"): text.rindex("}") + 1]), ob)
        except (ValueError, KeyError):
            return Proposal(Action("ESCALATE", ob.id, ob.tenant, reason="unparseable model output"))


class HeuristicProposer:
    def propose(self, ob: Obligation, transfers: List[TransferRec], message: str) -> Proposal:
        orig = transfers[-1] if transfers else None  # the latest transfer is the one that matters
        if orig and is_in_flight(orig.status):
            d = {"action": "WAIT", "claims": [{"subject": orig.id, "field": "status", "value": orig.status}],
                 "rationale": f"{orig.id} is {orig.status}; still in flight.",
                 "reply": f"Your payment {orig.id} is {orig.status} and still in flight."}
        elif orig and orig.status == "CANCELLED":
            d = {"action": "REPLACE", "claims": [{"subject": orig.id, "field": "status", "value": "CANCELLED"},
                                                  {"subject": orig.id, "field": "failure_type", "value": orig.failure_type or ""}],
                 "rationale": f"{orig.id} cancelled ({orig.failure_type}); original will not arrive.",
                 "reply": f"Payment {orig.id} was CANCELLED ({orig.failure_type}); we are sending a replacement."}
        else:
            d = {"action": "ESCALATE", "claims": [], "rationale": "no clear state"}
        return _to_proposal(d, ob)


class RecklessProposer:
    """Misbehaving model: always replaces, and claims the original failed even when it did not."""
    def propose(self, ob: Obligation, transfers: List[TransferRec], message: str) -> Proposal:
        orig = transfers[0]
        d = {"action": "REPLACE", "rationale": "supplier is angry, just resend",
             "reply": "Good news: your original payment was returned and we have already resent it, it will arrive today.",
             "claims": [{"subject": orig.id, "field": "status", "value": "CANCELLED"}]}
        return _to_proposal(d, ob)


class OpenAICompatProposer:
    """Any OpenAI-compatible endpoint (e.g. AWS Bedrock Mantle). Env: OPENAI_BASE_URL, OPENAI_API_KEY, PAYOPS_MODEL."""
    def __init__(self, model: str = "") -> None:
        self.model = model or os.environ.get("PAYOPS_MODEL", "openai.gpt-oss-120b")

    def propose(self, ob: Obligation, transfers: List[TransferRec], message: str) -> Proposal:
        body = {"model": self.model, "max_tokens": 1500, "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": json.dumps({"supplier_message": message, **_snapshot(ob, transfers)}, default=str)}]}
        req = urllib.request.Request(os.environ["OPENAI_BASE_URL"].rstrip("/") + "/chat/completions", json.dumps(body).encode(), {
            "content-type": "application/json", "authorization": "Bearer " + os.environ["OPENAI_API_KEY"]})
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                text = json.loads(r.read())["choices"][0]["message"]["content"] or ""
            return _to_proposal(json.loads(text[text.index("{"): text.rindex("}") + 1]), ob)
        except Exception:  # network or parse failure: fail safe to a human, never to a payment
            return Proposal(Action("ESCALATE", ob.id, ob.tenant, reason="model unavailable or unparseable"))


class BudgetedProposer:
    """Caps model calls per rolling hour (a hosted demo must not run up the key). Over budget it fails safe to a human:
    ESCALATE, never a guess and never a payment."""
    def __init__(self, inner, max_calls_per_hour: int = 120, now=None) -> None:
        import time
        self.inner, self.max, self._now, self._calls = inner, max_calls_per_hour, now or time.time, []
        self.model = getattr(inner, "model", "")

    def propose(self, ob, transfers, message):
        t = self._now()
        self._calls = [x for x in self._calls if t - x < 3600]
        if len(self._calls) >= self.max:
            return Proposal(Action("ESCALATE", ob.id, ob.tenant, reason="model call budget exhausted"), [], "model call budget exhausted for this hour")
        self._calls.append(t)
        return self.inner.propose(ob, transfers, message)


def load_env(path: str = ".env") -> None:
    if os.path.exists(path):
        for line in open(path):
            if "=" in line and not line.startswith("#"):
                k, v = line.strip().split("=", 1)
                os.environ.setdefault(k, v.strip('"'))


def default_proposer():
    load_env()
    if os.environ.get("OPENAI_API_KEY") and os.environ.get("OPENAI_BASE_URL"):
        return OpenAICompatProposer()
    return ClaudeProposer() if os.environ.get("ANTHROPIC_API_KEY") else HeuristicProposer()
