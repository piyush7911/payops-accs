"""R1 - exhaustive verification of the gate on a finite abstraction of the incident system.

System (labelled transition system, LTS). State = tuple of transfers (status, failure_class), oldest first.
  failure_class: "R" = replaceable (e.g. CHANNEL_TIMEOUT), "N" = not replaceable (e.g. DUPLICATION_RETURN), None.
Moves:
  environment: PROCESSING->SENT, SENT->PAID, SENT->CANCELLED(R|N)   (any transfer, any time)
  agent: ANY proposal - every action kind crossed with every combination of the adversarial flags
         (reuse_id, cross_tenant, wallet_ok, score_ok, human_ok, big_amount). The proposer (LLM) is untrusted, so the
         model quantifies over *all* proposals, true or false claims. A proposal is applied only if the real `policy.check`
         permits it; a permitted REPLACE appends a PROCESSING transfer.
We explore the reachable set by BFS to a fixpoint and check invariants INV-1..INV-9 on every state and on every permitted
decision. `audit_decision` is written independently of the gate's predicates (plain re-derivation from raw facts).

The reachable set R is the least fixed point of the step relation; the safety invariant I = (INV-1..9) is checked to
hold on all of R, i.e. R subset of I, and R is closed under every move (that is the coinductive proof obligation
I <= F(I) discharged by enumeration; see docs/RESEARCH.md for the argument and the abstraction's soundness).
"""
from __future__ import annotations

import itertools
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from . import policy as P
from .models import Action, Obligation, TransferRec, is_in_flight, is_live

State = Tuple[Tuple[str, Optional[str]], ...]
FT = {"R": "CHANNEL_TIMEOUT", "N": "DUPLICATION_RETURN", None: None}

# Environment transition table. Superset of what the sandbox simulator does: real transfers can also be cancelled before
# they are sent, go OVERDUE, and be returned by the bank AFTER being marked PAID (reference builds: PAID -> CANCELLED).
ENV_MOVES = {
    "SCHEDULED": (("PROCESSING", None, "PROCESSING"), ("CANCELLED", "N", "CANCEL_N")),
    "IN_APPROVAL": (("PROCESSING", None, "APPROVED"), ("CANCELLED", "N", "REJECTED")),
    "PROCESSING": (("SENT", None, "SENT"), ("OVERDUE", None, "OVERDUE"), ("CANCELLED", "R", "CANCEL_R"), ("CANCELLED", "N", "CANCEL_N")),
    "OVERDUE": (("SENT", None, "SENT"), ("PAID", None, "PAID"), ("CANCELLED", "R", "CANCEL_R"), ("CANCELLED", "N", "CANCEL_N")),
    "SENT": (("PAID", None, "PAID"), ("CANCELLED", "R", "CANCEL_R"), ("CANCELLED", "N", "CANCEL_N")),
    "PAID": (("CANCELLED", "R", "LATE_RETURN_R"), ("CANCELLED", "N", "LATE_RETURN_N")),
}
START_STATES = ("SCHEDULED", "IN_APPROVAL", "PROCESSING")  # a freshly created transfer may begin in any of these

FLAGS = list(itertools.product([False, True], repeat=6))  # reuse_id, cross_tenant, wallet_ok, score_ok, human_ok, big_amount

# which invariant each clause exists to protect (used for the mutation analysis)
CLAUSE_INVARIANT = {
    "P1-single-live-payment": "INV-1", "P2-latest-terminal": "INV-2", "P3-fresh-request-id": "INV-4",
    "P4-tenant-isolation": "INV-3", "P5-fee-budget": "INV-5", "P6-autonomy-limit": "INV-6",
    "P7-evidence": "INV-7", "P8-no-wait-when-terminal": "INV-8", "P9-max-replacements": "INV-9",
}


@dataclass
class Report:
    states: int = 0
    moves: int = 0
    decisions_checked: int = 0
    violations: List[Tuple[str, str]] = field(default_factory=list)  # (invariant, trace)
    capped: bool = False  # True if the exploration hit max_len (then the result is bounded, not a fixpoint)

    @property
    def ok(self) -> bool:
        return not self.violations

    def invariants_violated(self) -> set:
        return {v[0] for v in self.violations}


def _transfers(state: State) -> List[TransferRec]:
    return [TransferRec(f"t{i}", "ob", "A", f"r{i}", s, FT[f]) for i, (s, f) in enumerate(state)]


def _context(state: State, pol: P.Policy, flags) -> Tuple[Action, P.Context, dict]:
    reuse, cross, wallet_ok, score_ok, human_ok, big = flags
    ts = _transfers(state)
    ob = Obligation("ob", "A", 9000.0 if big else 100.0, "EUR", "SWIFT", "ben")
    used = {t.request_id for t in ts}
    rid = "r0" if reuse else "fresh"
    act = Action("REPLACE", "ob", "A", rid, funding_tenant="B" if cross else "A")
    ctx = P.Context(ob, ts, used, 1e9 if wallet_ok else 0.0, 1.0 if score_ok else 0.0,
                    overrides=frozenset(pol.overridable()) if human_ok else frozenset())  # a human approval waives overridable clauses only
    facts = dict(reuse=reuse, cross=cross, wallet_ok=wallet_ok, score_ok=score_ok, human_ok=human_ok, big=big)
    return act, ctx, facts


def audit_decision(kind: str, state: State, facts: dict, max_replacements: int, limit_ok_amount: bool = True) -> List[str]:
    """Independent oracle: given a PERMITTED action, which invariants does it break? Re-derived from raw facts only."""
    broken = []
    live = [s for s, _ in state if is_live(s)]
    if kind == "REPLACE":
        if live:
            broken.append("INV-1")  # replacing while something live => would create a second live payment
        last = state[-1] if state else None
        if (last is None or last[0] != "CANCELLED" or last[1] != "R") and not facts["human_ok"]:
            broken.append("INV-2")  # hard unless a human approved this specific replacement
        if facts["cross"]:
            broken.append("INV-3")
        if facts["reuse"]:
            broken.append("INV-4")
        if not facts["wallet_ok"]:
            broken.append("INV-5")
        if facts["big"] and not facts["human_ok"]:  # (overridable: INV-6 holds modulo human approval)
            broken.append("INV-6")
        if not facts["score_ok"]:
            broken.append("INV-7")
        if len(state) - 1 >= max_replacements:
            broken.append("INV-9")
    if kind == "WAIT" and not any(is_in_flight(s) for s, _ in state):
        broken.append("INV-8")
    if kind == "CLOSE" and not facts["score_ok"]:
        broken.append("INV-7")
    return broken


def explore(pol: Optional[P.Policy] = None, max_len: int = 4, stop_on_violation: bool = False) -> Report:
    pol = pol or P.DEFAULT
    rep = Report()
    starts = [((st0, None),) for st0 in START_STATES]
    seen: Dict[State, str] = {x: "init" for x in starts}
    q = deque(starts)
    while q:
        st = q.popleft()
        trace = seen[st]
        rep.states += 1
        if len(P.violations({"ob": _transfers(st)})):  # INV-1 on the state itself
            rep.violations.append(("INV-1", trace))
            if stop_on_violation:
                return rep
        succ: List[Tuple[State, str]] = []
        for i, (s, f) in enumerate(st):  # environment moves (real Airwallex statuses, incl. late bank returns)
            def put(ns, nf, label):
                new = list(st)
                new[i] = (ns, nf)
                succ.append((tuple(new), f"{trace} > env:t{i}:{label}"))
            for ns, nf, label in ENV_MOVES.get(s, ()):
                put(ns, nf, label)
        for kind in ("WAIT", "ESCALATE", "CLOSE"):  # agent moves that never create transfers
            for flags in FLAGS:
                act, ctx, facts = _context(st, pol, flags)
                act.kind = kind
                v = P.check(act, ctx, pol)
                rep.moves += 1
                if v.permit:
                    rep.decisions_checked += 1
                    for inv in audit_decision(kind, st, facts, pol.params.get("max_replacements", 1)):
                        rep.violations.append((inv, f"{trace} > agent:{kind}{facts}"))
        for flags in FLAGS:  # REPLACE under every adversarial flag combination
            act, ctx, facts = _context(st, pol, flags)
            v = P.check(act, ctx, pol)
            rep.moves += 1
            if v.permit:
                rep.decisions_checked += 1
                for inv in audit_decision("REPLACE", st, facts, pol.params.get("max_replacements", 1)):
                    rep.violations.append((inv, f"{trace} > agent:REPLACE{facts}"))
                if len(st) >= max_len:
                    rep.capped = True
                else:
                    succ.append((st + (("PROCESSING", None),), f"{trace} > agent:REPLACE"))
        if stop_on_violation and rep.violations:
            return rep
        for ns, label in succ:
            if ns not in seen:
                seen[ns] = label
                q.append(ns)
    return rep


def mutation_analysis(pol: Optional[P.Policy] = None, max_len: int = 4) -> Dict[str, dict]:
    """Delete each clause in turn; the exhaustive check must then find a violation of the invariant it protects."""
    pol = pol or P.DEFAULT
    out = {}
    for clause in pol.clauses:
        m = explore(pol.without(clause["id"]), max_len=max_len)
        want = CLAUSE_INVARIANT[clause["id"]]
        first = next((tr for inv, tr in m.violations if inv == want), None)
        out[clause["id"]] = {"protects": want, "violated_without_it": want in m.invariants_violated(),
                             "counterexample": first, "all_violated": sorted(m.invariants_violated())}
    return out


# P1 is implied by P2 (+ sequential creation): deleting it alone changes nothing, deleting both breaks INV-1.
REDUNDANT_PAIRS = {"P1-single-live-payment": "P2-latest-terminal"}


def redundancy_analysis(pol: Optional[P.Policy] = None, max_len: int = 4) -> Dict[str, dict]:
    pol = pol or P.DEFAULT
    res = {}
    for a, b in REDUNDANT_PAIRS.items():
        m = explore(pol.without(a, b), max_len=max_len)
        res[a] = {"implied_by": b, "violated_without_both": CLAUSE_INVARIANT[a] in m.invariants_violated()}
    return res


if __name__ == "__main__":
    r = explore()
    print(f"policy digest {P.DEFAULT.digest}: {r.states} reachable states, {r.moves} agent moves, "
          f"{r.decisions_checked} permitted decisions audited, violations={len(r.violations)}, capped={r.capped}")
    for cid, m in mutation_analysis().items():
        note = "" if m["violated_without_it"] else "  (redundant given " + REDUNDANT_PAIRS.get(cid, "?") + ": defense in depth)"
        print(f"  without {cid:28s} -> {m['protects']} violated: {m['violated_without_it']}{note}")
    for cid, m in redundancy_analysis().items():
        print(f"  without {cid} AND {m['implied_by']} -> violated: {m['violated_without_both']}")
