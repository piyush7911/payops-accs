"""F2 consistency.score - factual-consistency score of an LLM claim against Airwallex records, with provenance.

raw = (S + a) / (S + C + a + b),  a = b = 0.5
  S = weight of supporting evidence, C = weight of contradicting evidence.
Authoritative records (live GET /transfers) weigh 4.0; secondary sources (supplier email, the LLM itself) 0.5.

Properties (tested in tests/test_consistency.py):
  - monotone: more support never lowers the score; more contradiction never raises it (by construction, and by test).
  - bounded in (0,1); 0.5 with no evidence ("don't know").
  - calibrated: `Calibrator` (isotonic regression / PAV) maps raw -> empirical P(claim true), fitted on labelled claims
    (calibration.py). Monotone maps preserve the ordering, so monotonicity survives calibration.
Penalty interface for an external optimizer: penalty = 1 - value in [0,1]; `batch_penalty` = mean over a set of claims.
It is differentiable-free by design: the optimizer only needs the scalar and may treat ACCS as a black box.
Supported claim fields: status, failure_type, amount (subject = transfer id); transfer_count (subject = obligation id).
"""
from __future__ import annotations

import bisect
import json
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from .models import Claim, TransferRec

AUTHORITATIVE_W = 4.0
SECONDARY_W = 0.5
A = B = 0.5


@dataclass
class Evidence:
    source: str  # e.g. "airwallex:GET /transfers"
    ref: str  # record id
    field: str
    value: str
    authoritative: bool = True


@dataclass
class ScoreRecord:
    value: float  # calibrated if a calibrator was supplied, else raw
    provenance: List[str] = field(default_factory=list)
    penalty: float = 0.0
    raw: float = 0.0


class Calibrator:
    """Isotonic regression (pool-adjacent-violators). Monotone non-decreasing map raw score -> P(claim is true)."""

    def __init__(self, xs: Sequence[float], ys: Sequence[float]) -> None:
        self.xs, self.ys = list(xs), list(ys)

    @classmethod
    def fit(cls, scores: Sequence[float], labels: Sequence[int]) -> "Calibrator":
        agg: dict = {}  # pool ties first: one point per distinct score, weighted by count (required for correct PAV)
        for x, y in zip(scores, labels):
            sy, n = agg.get(x, (0.0, 0))
            agg[x] = (sy + float(y), n + 1)
        blocks = []  # [sum_y, count, x_max]
        for x in sorted(agg):
            sy, n = agg[x]
            blocks.append([sy, n, x])
            while len(blocks) > 1 and blocks[-2][0] / blocks[-2][1] > blocks[-1][0] / blocks[-1][1]:
                b = blocks.pop()
                blocks[-1][0] += b[0]
                blocks[-1][1] += b[1]
                blocks[-1][2] = b[2]
        xs = [b[2] for b in blocks]
        ys = [min(max(b[0] / b[1], 0.01), 0.99) for b in blocks]  # keep strictly inside (0,1)
        return cls(xs, ys)

    def __call__(self, x: float) -> float:
        i = bisect.bisect_left(self.xs, x)
        return self.ys[min(i, len(self.ys) - 1)]

    def to_json(self) -> str:
        return json.dumps({"xs": self.xs, "ys": self.ys})

    @classmethod
    def from_json(cls, s: str) -> "Calibrator":
        d = json.loads(s)
        return cls(d["xs"], d["ys"])


def evidence_from_transfers(transfers: List[TransferRec], obligation_id: Optional[str] = None,
                            source: str = "airwallex:GET /transfers") -> List[Evidence]:
    ev = []
    for t in transfers:
        ev.append(Evidence(source, t.id, "status", t.status))
        ev.append(Evidence(source, t.id, "amount", f"{t.amount:.2f}"))
        # absence is evidence too: "no failure" is recorded as "" so a claim of failure_type=null can be corroborated
        ev.append(Evidence(source, t.id, "failure_type", t.failure_type or ""))
    if obligation_id:
        ev.append(Evidence(source, obligation_id, "transfer_count", str(len(transfers))))
    return ev


def _same(field_: str, a: str, b: str) -> bool:
    if field_ in ("amount", "transfer_count"):
        try:
            return abs(float(a) - float(b)) < 0.005
        except ValueError:
            return False
    norm = lambda v: "" if v.strip().lower() in ("none", "null", "n/a", "") else v.strip().upper()
    return norm(a) == norm(b) if field_ == "failure_type" else a.strip().upper() == b.strip().upper()


def score(claim: Claim, evidence: List[Evidence], calibrator: Optional[Calibrator] = None) -> ScoreRecord:
    s = c = 0.0
    prov: List[str] = []
    for e in evidence:
        if e.ref != claim.subject or e.field != claim.field:
            continue
        w = AUTHORITATIVE_W if e.authoritative else SECONDARY_W
        if _same(claim.field, e.value, claim.value):
            s += w
            prov.append(f"+{w} {e.source}#{e.ref}.{e.field}={e.value}")
        else:
            c += w
            prov.append(f"-{w} {e.source}#{e.ref}.{e.field}={e.value} (claimed {claim.value})")
    if not prov:
        prov.append("no evidence found")
    raw = (s + A) / (s + c + A + B)
    v = calibrator(raw) if calibrator else raw
    return ScoreRecord(v, prov, 1.0 - v, raw)


def min_score(claims: List[Claim], evidence: List[Evidence], calibrator: Optional[Calibrator] = None) -> ScoreRecord:
    """An action is only as trustworthy as its weakest supporting claim."""
    if not claims:
        return ScoreRecord(1.0, ["no claims relied on"], 0.0, 1.0)
    recs = [score(c, evidence, calibrator) for c in claims]
    worst = min(recs, key=lambda r: r.value)
    return ScoreRecord(worst.value, [p for r in recs for p in r.provenance], worst.penalty, worst.raw)


def batch_penalty(records: Sequence[ScoreRecord]) -> float:
    """Penalty signal for an external optimizer: mean (1 - score) over a batch of claims. 0 = fully corroborated."""
    return sum(r.penalty for r in records) / len(records) if records else 0.0


def default_calibrator() -> Optional[Calibrator]:
    """Calibrator fitted by `python -m payops.calibration` (shipped as calibrator.json), or None if absent."""
    import os
    path = os.path.join(os.path.dirname(__file__), "calibrator.json")
    try:
        with open(path) as f:
            return Calibrator.from_json(f.read())
    except OSError:
        return None
