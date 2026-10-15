import random

from payops import calibration as K
from payops import consistency as C
from payops.models import Claim


def test_pav_pools_ties_and_is_monotone():
    cal = C.Calibrator.fit([0.1] * 10 + [0.5] * 10 + [0.9] * 10, [0] * 6 + [1] * 4 + [1] * 7 + [0] * 3 + [1] * 9 + [0])
    assert cal.xs == [0.1, 0.5, 0.9]
    assert cal.ys[0] < cal.ys[1] < cal.ys[2] or cal.ys == sorted(cal.ys)
    assert all(a <= b for a, b in zip(cal.ys, cal.ys[1:]))


def test_pav_repairs_inversions():
    cal = C.Calibrator.fit([0.2, 0.2, 0.8, 0.8], [1, 1, 0, 0])  # higher score, lower truth rate -> must pool
    assert len(set(cal.ys)) == 1


def test_calibration_improves_ece_on_noisy_evidence_channel():
    rows = K.dataset(600, 11)
    r = K.evaluate(rows, seed=3)
    assert r["calibrated"]["ece"] < 0.05 < r["raw"]["ece"]
    assert r["calibrated"]["brier"] < r["constant_baseline_brier"]  # the score carries real information


def test_calibrated_score_preserves_monotonicity():
    cal = C.default_calibrator()
    assert cal is not None
    c = Claim("t", "status", "CANCELLED")
    sup = C.Evidence("s", "t", "status", "CANCELLED")
    con = C.Evidence("s", "t", "status", "SENT")
    vals = [C.score(c, [sup] * n, cal).value for n in range(6)]
    assert vals == sorted(vals)
    vals = [C.score(c, [con] * n, cal).value for n in range(6)]
    assert vals == sorted(vals, reverse=True)


def test_penalty_interface():
    c = Claim("t", "status", "PAID")
    good = C.score(c, [C.Evidence("s", "t", "status", "PAID")])
    bad = C.score(c, [C.Evidence("s", "t", "status", "SENT")])
    assert C.batch_penalty([good]) < C.batch_penalty([good, bad]) < C.batch_penalty([bad])
    assert abs(good.penalty - (1 - good.value)) < 1e-12 and C.batch_penalty([]) == 0.0


def test_numeric_claims_use_tolerance_and_count_claims():
    ev = [C.Evidence("s", "t1", "amount", "100.00"), C.Evidence("s", "ob", "transfer_count", "2")]
    assert C.score(Claim("t1", "amount", "100"), ev).value > 0.85
    assert C.score(Claim("t1", "amount", "250"), ev).value < 0.2
    assert C.score(Claim("ob", "transfer_count", "2"), ev).value > 0.85


def test_unseen_claim_is_dont_know():
    assert C.score(Claim("zzz", "status", "PAID"), []).value == 0.5


def test_absence_claims_are_corroborated():
    from payops.models import TransferRec
    ts = [TransferRec("t1", "ob", "A", "r", "SENT", None, 100.0)]
    ev = C.evidence_from_transfers(ts, "ob")
    for v in ("null", "None", "", "n/a"):
        assert C.score(Claim("t1", "failure_type", v), ev).value > 0.85, v
    assert C.score(Claim("t1", "failure_type", "CHANNEL_TIMEOUT"), ev).value < 0.2  # claiming a failure that is not there
