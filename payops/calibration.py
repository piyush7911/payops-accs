"""Non-circular calibration of consistency.score.

Ground truth = true transfer state. Two independent noisy channels:
  * the CLAIMANT (synthetic, or the live LLM) sees a possibly-stale view of the truth and states claims;
  * the SCORER sees evidence from a *separate* degraded read (stale or missing records).
Label = claim matches the true state. The score only relates to the label through the noisy evidence channel, so
calibration is a real test: a score of 0.9 should mean ~90% of such claims are true.
Run: python -m payops.calibration [--live N]
"""
from __future__ import annotations

import json
import os
import random
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import List, Tuple

from . import consistency as C
from .llm import OpenAICompatProposer, load_env
from .models import Claim, Obligation, TransferRec

STATUS = ["PROCESSING", "SENT", "PAID", "CANCELLED"]
FTS = ["CHANNEL_TIMEOUT", "SYSTEM_ERROR", "DUPLICATION_RETURN", "RECALL_REQUESTED", "ACCOUNT_CLOSED"]
STALE = {"SENT": "PROCESSING", "PAID": "SENT", "CANCELLED": "SENT", "PROCESSING": "PROCESSING"}


def true_state(rng: random.Random, i: int) -> List[TransferRec]:
    n = rng.choice([1, 1, 2])
    out = []
    for k in range(n):
        st = rng.choice(STATUS)
        out.append(TransferRec(f"tr_{i}_{k}", f"ob{i}", "A", f"rq{i}{k}", st, rng.choice(FTS) if st == "CANCELLED" else None,
                               rng.choice([100.0, 250.0, 1000.0])))
    return out


def degrade(ts: List[TransferRec], rng: random.Random, p_stale=0.25, p_missing=0.15) -> List[TransferRec]:
    out = []
    for t in ts:
        r = rng.random()
        if r < p_missing:
            continue
        if r < p_missing + p_stale:
            out.append(TransferRec(t.id, t.obligation_id, t.tenant, t.request_id, STALE[t.status], None, t.amount))
        else:
            out.append(t)
    return out


def claim_true(c: Claim, truth: List[TransferRec]) -> bool:
    for t in truth:
        if t.id == c.subject:
            actual = {"status": t.status, "failure_type": t.failure_type or "", "amount": f"{t.amount:.2f}"}.get(c.field)
            return actual is not None and C._same(c.field, actual, c.value)
    return False


def synthetic_claims(truth: List[TransferRec], view: List[TransferRec], rng: random.Random) -> List[Claim]:
    """Claimant states what it believes from its (possibly stale) view, sometimes confidently wrong."""
    claims = []
    for t in view:
        claims.append(Claim(t.id, "status", t.status))
        if t.failure_type:
            claims.append(Claim(t.id, "failure_type", t.failure_type))
        if rng.random() < 0.15:  # careless/hallucinated claim
            claims.append(Claim(t.id, "status", rng.choice(STATUS)))
    return claims


def live_claims(view: List[TransferRec], brain: OpenAICompatProposer, msg: str) -> List[Claim]:
    ob = Obligation(view[0].obligation_id if view else "ob", "A", 250.0, "EUR", "SWIFT", "b")
    return brain.propose(ob, view, msg).claims


def dataset(n: int, seed: int, live: bool = False) -> List[Tuple[float, int]]:
    rng = random.Random(seed)
    cases = []
    for i in range(n):
        truth = true_state(rng, i)
        claim_view = degrade(truth, rng, 0.25, 0.0) or truth  # claimant never sees nothing
        cases.append((truth, claim_view, degrade(truth, rng), f"Supplier msg #{i}: nothing arrived"))
    brain = OpenAICompatProposer() if live else None

    def one(case):
        truth, claim_view, ev_view, msg = case
        claims = live_claims(claim_view, brain, msg) if live else synthetic_claims(truth, claim_view, random.Random(len(msg)))
        ev = C.evidence_from_transfers(ev_view)
        return [(C.score(c, ev).raw, int(claim_true(c, truth))) for c in claims if c.field in ("status", "failure_type")]

    rows: List[Tuple[float, int]] = []
    with ThreadPoolExecutor(8 if live else 1) as ex:
        for r in ex.map(one, cases):
            rows.extend(r)
    return rows


def ece(pairs: List[Tuple[float, int]], bins: int = 10) -> float:
    if not pairs:
        return 0.0
    tot = 0.0
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        sel = [(p, y) for p, y in pairs if lo <= p < hi or (b == bins - 1 and p == 1.0)]
        if sel:
            tot += len(sel) / len(pairs) * abs(sum(p for p, _ in sel) / len(sel) - sum(y for _, y in sel) / len(sel))
    return tot


def brier(pairs: List[Tuple[float, int]]) -> float:
    return sum((p - y) ** 2 for p, y in pairs) / len(pairs) if pairs else 0.0


def evaluate(rows: List[Tuple[float, int]], seed: int = 0) -> dict:
    rng = random.Random(seed)
    rows = rows[:]
    rng.shuffle(rows)
    k = len(rows) // 2
    train, test = rows[:k], rows[k:]
    cal = C.Calibrator.fit([s for s, _ in train], [y for _, y in train])
    cal_test = [(cal(s), y) for s, y in test]
    base = sum(y for _, y in test) / len(test)
    return {"n_train": len(train), "n_test": len(test), "base_rate_true": round(base, 3),
            "raw": {"ece": round(ece(test), 4), "brier": round(brier(test), 4)},
            "calibrated": {"ece": round(ece(cal_test), 4), "brier": round(brier(cal_test), 4)},
            "constant_baseline_brier": round(brier([(base, y) for _, y in test]), 4), "calibrator": cal}


if __name__ == "__main__":
    load_env()
    n_live = int(sys.argv[sys.argv.index("--live") + 1]) if "--live" in sys.argv else 0
    out = {}
    syn = dataset(1500, 1)
    r = evaluate(syn)
    out["synthetic"] = {k: v for k, v in r.items() if k != "calibrator"}
    pooled = syn
    if n_live:
        live = dataset(n_live, 2, live=True)
        rl = evaluate(live)
        out["live_llm"] = {k: v for k, v in rl.items() if k != "calibrator"}
        pooled = syn + live
    final = C.Calibrator.fit([s for s, _ in pooled], [y for _, y in pooled])
    here = os.path.dirname(__file__)
    open(os.path.join(here, "calibrator.json"), "w").write(final.to_json())
    print(json.dumps(out, indent=2))
