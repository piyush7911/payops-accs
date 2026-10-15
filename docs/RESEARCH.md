# Verified long-running payout agent: gate soundness and consistency scoring

Companion to `docs/ACCS-spec.md`. Every number below is produced by code in this repo; commands to reproduce are in section 4.
Proofs here are paper proofs. Where a result is established by exhaustive enumeration of a finite abstraction, it says so.
Nothing here is mechanized in a proof assistant.

## 0. Setting

An LLM agent handles "my payout never arrived" incidents on Airwallex. The model is **untrusted**: it proposes an action
(`WAIT`, `REPLACE`, `ESCALATE`, `CLOSE`), the facts it relies on (claims), and a reply to the supplier. ACCS sits on the output path:

```
proposal -> consistency.score (claims vs. live records) -> policy.check (declarative spec) -> judge (restrict-only)
         -> execute | escalate        reply -> emitted only if corroborated and promise-free, else a code template
```

## 1. R1 - Coinductive soundness of the gate

### 1.1 Model
The system is a labelled transition system (LTS) `M = (S, s0, ->)`. A state `s` is the list of transfers of one obligation,
each `(status, failure_class)` with `status in {SCHEDULED, IN_APPROVAL, PROCESSING, OVERDUE, SENT, PAID, CANCELLED}` (the real
Airwallex statuses; a status the code has never seen is treated as live, see 1.7) and `failure_class in {R, N, none}`
(R = failure type the policy treats as safe to resend, N = not safe). Moves:

* **environment**: the full transition table in `model_check.ENV_MOVES`, applied to any transfer at any time: a new transfer may
  start `SCHEDULED`, `IN_APPROVAL` or `PROCESSING`; it can go `OVERDUE`; it can be cancelled before it is sent; and a transfer
  marked `PAID` can later be returned by the bank (`PAID -> CANCELLED`);
* **agent**: *any* proposal `a = (kind, reuse_id, cross_tenant, wallet_ok, score_ok, human_ok, big_amount)`. A proposal is applied
  only if `policy.check(a, ctx(s))` permits; a permitted `REPLACE` appends a `PROCESSING` transfer.

Because the proposer is untrusted, the agent moves quantify over **all** proposals, including ones backed by false claims
(modelled as `score_ok = false` / `wallet_ok = false`). The system never terminates (the agent keeps running), so the
property is about infinite traces.

### 1.2 Invariants
`Inv(s) = INV-1 and ... and INV-9` where on every *permitted decision* from `s`:

| | Statement | Clause that enforces it |
|---|---|---|
| INV-1 | at most one transfer per obligation is live (PROCESSING/SENT/PAID) | P1 (implied by P2) |
| INV-2 | a replacement is created only if the latest transfer is CANCELLED with a replaceable failure | P2 |
| INV-3 | funds never cross tenants | P4 |
| INV-4 | a `request_id` is never reused for a second payment | P3 |
| INV-5 | a replacement is created only if the wallet covers amount + fee | P5 |
| INV-6 | amounts above the autonomy limit are never replaced without human approval | P6 |
| INV-7 | an action backed by claims below the consistency threshold is never permitted | P7 |
| INV-8 | `WAIT` is permitted only while a transfer is in flight | P8 |
| INV-9 | at most `max_replacements` automatic replacements | P9 |

`audit_decision` re-derives each invariant from raw facts and is written independently of the gate's predicates.

### 1.3 Coinductive statement and proof
Safety on all infinite behaviours is the greatest fixed point

```
Safe = nu X.  { s | Inv(s)  and  for every move s -> s' : s' in X }
```

**Coinduction principle:** to show `s0 in Safe` it suffices to exhibit a set `I` with `s0 in I` and `I <= F(I)`, where
`F(X) = { s | Inv(s) and (s -> s' implies s' in X) }`; then `I <= nu F`.

**Proof.** Take `I = Reach(s0)`, the set of states reachable by any finite sequence of moves. `s0 in I` by definition and `I` is closed
under `->` by definition. It remains to show `Inv(s)` for every `s in I` and every permitted decision from `s`. `I` is finite
(20 states, because P9 bounds the number of transfers to `1 + max_replacements`), so this is a finite check: `model_check.explore`
runs a BFS to fixpoint (`capped = False`, i.e. the exploration was not cut off) and audits every permitted decision at every state:

> **20 reachable states, 5,120 agent moves, 2,629 permitted decisions audited, 0 violations** (policy digest `04f623e8a60f`).

Hence `I <= F(I)` and every infinite trace stays in `I`, so `always Inv` holds on all infinite traces. qed (modulo 1.4).

**Up-to technique.** We prove the property of the finite abstraction, then lift it. The lifting is a *bisimulation up to the
abstraction function* `alpha`: it maps a concrete state (transfer ids, amounts, wallet balances) to the abstract tuple above.
It is sound because (i) the gate reads only abstract features (statuses, failure class, the six flags, the transfer count), so
`check(c) = check(alpha(c))`, and (ii) every concrete move maps to an abstract move (`alpha` commutes with `->`). A function that
commutes with the step relation is a functional simulation, and simulations preserve `always`-properties of the abstract system
to the concrete one. The two places where the abstraction could be unfaithful are in the "Limits" list (1.6).

### 1.4 Every clause is necessary (mutation analysis)
For each clause we delete it and re-run the exhaustive check; it must find a counterexample of the invariant the clause protects.
(`tests/test_model_check.py`)

| Clause removed | Invariant that then fails | Counterexample found |
|---|---|---|
| P2 latest-terminal | INV-2 | yes |
| P3 fresh-request-id | INV-4 | yes |
| P4 tenant-isolation | INV-3 | yes |
| P5 fee-budget | INV-5 | yes |
| P6 autonomy-limit | INV-6 | yes |
| P7 evidence | INV-7 | yes |
| P8 no-wait-when-terminal | INV-8 | yes |
| P9 max-replacements | INV-9 | yes |
| P1 single-live-payment | INV-1 | yes |

**P1 and human overrides.** In the first version P1 was redundant (P2 and sequential creation implied it). Once a human can
override P2 (the failure type is not on the auto-replace list but the person accepts the risk), P1 is the hard guard that stops
that approved action from creating a second live payment, and the check now finds a counterexample without it. With every
clause made non-overridable P1 is redundant again (`test_p1_is_necessary_exactly_because_p2_can_be_overridden_by_a_human`
shows both). Only P2 and P6 are overridable; the rest are hard, and a human approval never waives them.

### 1.5 Liveness: bounded detection
**Statement.** If the invariant is ever broken, it is detected in the step in which it breaks (lag 0).
**Argument.** For INV-1, `policy.violations` is a total function on a state, and the commander evaluates it on freshly read state after
every handled step (`Outcome.alerts`); a breach is therefore visible in the state that contains it. For INV-2..9 the breach is a
property of one permitted decision, which `audit_decision` evaluates at the moment of the decision. Empirically: with P1 and P2
removed the exhaustive search reaches a violating state and the auditor flags it on that same state
(`test_violation_is_detected_in_the_step_it_happens`), and an out-of-band second payment is flagged on the next step
(`test_auditor_runs_every_step_and_flags_an_out_of_band_double_payment`). **This is a bound of one step on an auditor that
runs every step; it does not bound how long after a step the commander runs again.**

### 1.7 What changed after reviewing the reference builds
The model originally knew four statuses. The reference builds' verification records (and our own probe) showed `SCHEDULED`,
`IN_APPROVAL`, `OVERDUE` and late returns, so the status model is now fail-safe: *only a definitively dead transfer
(`CANCELLED`/`FAILED`) is not live*, so a status we have never seen blocks a replacement instead of permitting a double payment.
The exhaustive check was extended to the real transition table and still finds no violation. Request ids are now minted by code
from (obligation, attempt) and a write-ahead row is committed before any call, so the "never pays twice" argument no longer rests
on process memory (`tests/test_durability_approvals.py`). See `docs/REFERENCE-REVIEW.md`.

### 1.6 Limits (R1)
* The proof is for the abstraction. Faithfulness of `alpha` for the *real* Airwallex API is argued, not proven (for example
  the sandbox reports every failure as `INCORRECT_ROUTING`; we recover the cause from `failure.code`, an assumption about the API).
* Coverage is one obligation at a time. Cross-obligation properties (INV-3 across many tenants) are enforced per action, and
  checked by random multi-tenant traces (200 seeds), not by exhaustive search over the product.
* No mechanized proof (Coq/Lean/TLA+). The exhaustive check *is* the finite-state model check.

## 2. R2 - Calibrated factual-consistency score

### 2.1 Definition
For a claim `c` and evidence set `E` (live `GET /transfers`, weight 4.0; secondary sources 0.5):

```
raw(c, E) = (S + 0.5) / (S + C + 1),      S = weight of evidence agreeing with c, C = weight contradicting
```
Provenance is the list of matching records with sign and weight. Fields: `status`, `failure_type` (absence counts: "no failure" is
evidence), `amount` (numeric tolerance), `transfer_count`. An action is scored by its weakest claim (`min`).

### 2.2 Properties
**Monotonicity.** `d raw / d S = (C + 0.5) / (S + C + 1)^2 >= 0` and `d raw / d C = -(S + 0.5) / (S + C + 1)^2 <= 0`.
Better-corroborated claims never score lower; more contradiction never raises the score. The calibrator is an isotonic
(non-decreasing) map, and a non-decreasing map composed with a monotone function is monotone, so monotonicity survives
calibration. `min` over claims is monotone in each argument. Bounded in (0,1); exactly 0.5 with no evidence.

**Calibration.** Non-circular protocol: ground truth is the true transfer state; the *claimant* sees a stale view; the *scorer* sees
a separately degraded read (25% stale, 15% missing). Label = claim matches the truth. Held-out half, 10-bin ECE:

| Claim source | n (test) | ECE raw -> calibrated | Brier raw -> calibrated | Constant-rate baseline Brier |
|---|---|---|---|---|
| synthetic claimant | 1,175 | 0.196 -> **0.015** | 0.205 -> **0.120** | 0.138 |
| live `gpt-oss-120b` | 89 | 0.208 -> **0.043** | 0.155 -> **0.064** | 0.063 |

Calibration removes the miscalibration in both settings. **Discrimination is real on the synthetic set (Brier beats the
constant baseline) but not demonstrated on live-LLM claims** (0.064 vs 0.063): the live model's claims are mostly true, so
the base rate already predicts them, and 89 test claims is a small sample. We do not claim more than this.

A bug found along the way: the first calibrator did not pool tied scores and produced a map no better than the baseline; the
fix (aggregate by distinct score before PAV) is covered by `test_pav_pools_ties_and_is_monotone`.

### 2.3 Safety consequence: calibration must not loosen the gate
After refitting, a claim with *no evidence* calibrates to 0.875 (claims like it are usually true), above the 0.8 gate. In a money
path that is wrong. The gate therefore uses `min(raw, calibrated)`: calibration can only tighten the rule
(`test_calibration_never_loosens_the_gate`). The calibrated value is reported as `P[claim true]` and as the penalty.

### 2.4 Penalty-signal interface
`penalty = 1 - value in [0,1]`, per claim; `batch_penalty = mean penalty over a batch`; both returned by `POST /consistency.score`
(`value`, `raw`, `gate_value`, `provenance`, `penalty`, `batch_penalty`, `per_claim`). The optimizer needs only the scalar; ACCS
does not know its objective.

## 3. Integrated system evidence
* **Live Airwallex sandbox + live LLM + live safeguard judge** (`python run_live.py`): a transfer that times out is replaced once and
  closed with both SWIFT fees recorded; a second ping while the replacement is in flight yields `WAIT`; a `DUPLICATION_RETURN`
  (supplier may already hold the money) is escalated, not replaced. The judge, given the full facts, approved the legitimate
  replacement; it once vetoed it when I had omitted the `request_id` from its input (a bug in my wiring, fixed). Being
  restrict-only, that failure cost a payment delay, not a double payment.
* **Misbehaving model** (`RecklessProposer`, tests): a false "it was cancelled" claim scores 0.1, the action is denied, the
  reply is replaced by a template built from verified state, and no second transfer is ever created.
* **Real API facts that changed the design** (verified live): the simulation field is `next_status`; failures surface as
  `failure.code` on the detail endpoint only; a failed SWIFT payout refunds the amount but **keeps the fee**; `request_id`s are
  returned upper-cased.

## 4. Reproduce
```
conda env create -f environment.yml && conda activate payops-accs
python -m pytest -q                      # 321 tests
python -m payops.model_check             # R1: states, mutation analysis
python -m payops.smoke                   # live sandbox verification -> docs/VERIFICATION.md
python run_ui.py                         # web UI (simulator; --live for the real sandbox)
python -m payops.calibration --live 80   # R2: needs .env (OPENAI_*), refits payops/calibrator.json
python demo.py                           # offline, three scenarios
python run_live.py                       # needs .env (AWX_*, OPENAI_*)
python -m payops.api                     # F1/F2 over HTTP on :8765
```

## 5. Not done
Connected accounts are simulated (Airwallex has not enabled them on this sandbox yet); everything else above ran live or exhaustively.
No Coq/Lean proof; calibration on production traffic.
