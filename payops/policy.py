"""F1 policy.check - declarative policy spec (policy.json) + gate.

The spec is data: params and clauses (id, applies-to action kinds, named check, human text). `Policy.load` validates it
(unique ids, known checks, known action kinds) and fingerprints it, so every verdict can name the exact policy version.
Clauses marked "overridable" can be waived by a human approval bound to one specific action; hard clauses (single live
payment, fresh request_id, tenant isolation, fee budget, evidence, wait-only-in-flight, max replacements) never can.
The gate is a pure function of (policy, action, context), which is what lets us verify it exhaustively (model_check.py).

Safety invariants over every reachable system state:
  INV-1  at most one transfer per obligation is live (any status except CANCELLED/FAILED)
  INV-2  a replacement is only ever created when the latest transfer is CANCELLED with a replaceable failure type
  INV-3  funds never cross tenants
  INV-4  a request_id is never reused for a second payment
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from .models import Action, D, Obligation, TransferRec, Verdict, is_in_flight, is_live

ACTION_KINDS = ("WAIT", "REPLACE", "ESCALATE", "CLOSE")


@dataclass
class Context:
    obligation: Obligation
    transfers: List[TransferRec]  # all transfers for this obligation, oldest first
    used_request_ids: set
    wallet_balance: float  # balance of the *tenant's own* wallet in obligation currency
    consistency: float  # min score over claims backing the action (1.0 if none needed)
    human_approved: bool = False  # legacy shorthand: approves the autonomy-limit override only
    overrides: frozenset = frozenset()  # clause ids a human has approved FOR THIS ACTION (only 'overridable' clauses honour it)


Check = Callable[[Action, Context, dict], Optional[str]]


def _single_live_payment(a, c, p):
    live = [t for t in c.transfers if is_live(t.status)]
    return f"{len(live)} live transfer(s): {[t.id for t in live]}" if live else None


def _latest_terminal_replaceable(a, c, p):
    if not c.transfers:
        return "no transfer on record"
    last = c.transfers[-1]
    if last.status != "CANCELLED":
        return f"latest transfer is {last.status}, not CANCELLED"
    if last.failure_type not in p["replaceable_failure_types"]:
        return f"failure_type {last.failure_type!r} is not known-replaceable"
    return None


def _fresh_request_id(a, c, p):
    if not a.request_id:
        return "missing request_id"
    return "request_id already used" if a.request_id.lower() in {x.lower() for x in c.used_request_ids} else None


def _tenant_isolation(a, c, p):
    if a.tenant != c.obligation.tenant or (a.funding_tenant or a.tenant) != c.obligation.tenant:
        return f"funding from {a.funding_tenant or a.tenant} for tenant {c.obligation.tenant}"
    return None


def _fee_budget(a, c, p):
    need, have = c.obligation.amount + c.obligation.fee, D(c.wallet_balance)
    return f"need {need:.2f}, wallet has {have:.2f}" if have < need else None


def _autonomy_limit(a, c, p):
    if c.obligation.amount > D(p["autonomy_limit"]):
        return f"amount {c.obligation.amount:.2f} exceeds autonomy limit"
    return None


def _evidence_threshold(a, c, p):
    return f"consistency {c.consistency:.2f} < {p['min_consistency']}" if c.consistency < p["min_consistency"] else None


def _wait_only_in_flight(a, c, p):
    return None if any(is_in_flight(t.status) for t in c.transfers) else "no transfer in flight"


def _max_replacements(a, c, p):
    n = max(len(c.transfers) - 1, 0)  # transfers beyond the original
    return f"{n} replacement(s) already made (max {p['max_replacements']})" if n >= p["max_replacements"] else None


CHECKS: Dict[str, Check] = {
    "single_live_payment": _single_live_payment, "latest_terminal_replaceable": _latest_terminal_replaceable,
    "fresh_request_id": _fresh_request_id, "tenant_isolation": _tenant_isolation, "fee_budget": _fee_budget,
    "autonomy_limit": _autonomy_limit, "evidence_threshold": _evidence_threshold,
    "wait_only_in_flight": _wait_only_in_flight, "max_replacements": _max_replacements,
}


class PolicyError(ValueError):
    pass


class Policy:
    def __init__(self, spec: dict, checks: Optional[Dict[str, Check]] = None) -> None:
        self.checks = checks or CHECKS
        self._validate(spec)
        self.spec = spec
        self.params = spec["params"]
        self.clauses = spec["clauses"]
        self.digest = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:12]

    def _validate(self, spec: dict) -> None:
        for key in ("params", "clauses"):
            if key not in spec:
                raise PolicyError(f"policy missing '{key}'")
        seen = set()
        for c in spec["clauses"]:
            for f in ("id", "applies", "check", "text", "overridable"):
                if f not in c:
                    raise PolicyError(f"clause missing '{f}': {c}")
            if c["id"] in seen:
                raise PolicyError(f"duplicate clause id {c['id']}")
            seen.add(c["id"])
            if c["check"] not in self.checks:
                raise PolicyError(f"clause {c['id']}: unknown check {c['check']!r}")
            bad = [k for k in c["applies"] if k not in ACTION_KINDS]
            if bad:
                raise PolicyError(f"clause {c['id']}: unknown action kinds {bad}")

    @classmethod
    def load(cls, path: Optional[str] = None) -> "Policy":
        with open(path or os.path.join(os.path.dirname(__file__), "policy.json")) as f:
            return cls(json.load(f))

    def without(self, *clause_ids: str) -> "Policy":
        """Mutant policy with clauses removed (used to show which clauses are necessary)."""
        spec = json.loads(json.dumps(self.spec))
        spec["clauses"] = [c for c in spec["clauses"] if c["id"] not in clause_ids]
        return Policy(spec, self.checks)

    def overridable(self) -> set:
        return {c["id"] for c in self.clauses if c["overridable"]}

    def text(self) -> str:
        return "\n".join(f"{c['id']} (applies to {', '.join(c['applies'])}): {c['text']}" for c in self.clauses)


DEFAULT = Policy.load()


def check(action: Action, ctx: Context, policy: Optional[Policy] = None) -> Verdict:
    """policy.check: permit/deny plus the clause responsible. ESCALATE is always permitted (it moves no money)."""
    pol = policy or DEFAULT
    if action.kind == "ESCALATE":
        return Verdict(True)
    if action.obligation_id != ctx.obligation.id:
        return Verdict(False, "P4-tenant-isolation", "action targets a different obligation")
    approved = set(ctx.overrides) | ({"P6-autonomy-limit"} if ctx.human_approved else set())
    for clause in pol.clauses:
        if action.kind in clause["applies"]:
            if clause["overridable"] and clause["id"] in approved:  # a human accepted this specific risk
                continue
            problem = pol.checks[clause["check"]](action, ctx, pol.params)
            if problem:
                return Verdict(False, clause["id"], problem)
    return Verdict(True)


def violations(transfers_by_obligation: Dict[str, List[TransferRec]]) -> List[str]:
    """Independent auditor for INV-1. Run after every step: a violation is detected within one step."""
    out = []
    for oid, ts in transfers_by_obligation.items():
        live = [t.id for t in ts if is_live(t.status)]
        if len(live) > 1:
            out.append(f"INV-1 violated for {oid}: live transfers {live}")
    return out
