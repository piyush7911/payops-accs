# Payment Ops Incident Commander + ACCS

Airwallex Agentic Banking Hackathon, Kit 03. A supplier says a payout never arrived. An LLM agent decides **wait / replace / escalate**,
and **ACCS** (Agent Compliance & Consistency Suite, `docs/ACCS-spec.md`) makes paying twice impossible: the model proposes, code verifies
the facts and enforces the policy, a human decides the risky cases.

```
python run_ui.py          # http://127.0.0.1:8765  - simulator, no credentials needed
python run_ui.py --live   # the same scenarios against the real Airwallex sandbox (needs .env)
```

## How a decision is made
```
supplier message + live GET /transfers
  -> LLM proposes {action, claims, reply}                          untrusted; never holds credentials or chooses a request_id
  -> consistency.score   claims vs live records, provenance, calibrated P[true], penalty
  -> policy.check        declarative policy.json, clauses P1-P9 -> permit / deny + the clause
  -> judge (optional)    gpt-oss-safeguard second opinion; can only restrict
  -> PERMIT: preflight validate -> write-ahead attempt -> create transfer under a code-minted deterministic request_id
     DENY by an overridable clause (P2, P6): approval queue, bound to the exact terms, expires if the world changes
     DENY by a hard clause: blocked and escalated, never queued
  -> reply to the supplier is the model's text only if every claim is corroborated and it promises nothing we did not do
  -> independent auditor re-checks "at most one live transfer" on fresh state after every step
```

## What is guaranteed, and how it is checked
| Property | Check |
|---|---|
| One live payment per obligation, whatever the model says | exhaustive model check: **20 states, 2,629 permitted decisions audited, 0 violations**; every clause shown necessary by deleting it (`python -m payops.model_check`) |
| Unknown or new transfer statuses never permit a double payment | a transfer is live unless definitively CANCELLED/FAILED; real statuses (SCHEDULED, IN_APPROVAL, OVERDUE, late PAID->CANCELLED) are in the model |
| Crash or restart cannot cause a second payment | write-ahead attempt + deterministic request id (`po-<16 hex>-r<n>`) + `Commander.recover()`; tests kill the process at both bad moments |
| A hard rule cannot be waived by a human | only P2 and P6 are overridable; the approval re-runs the gate and expires if state changed |
| Facts the model states are true | claims scored against live records; a hallucinated claim scores ~0.1 and is blocked; calibration can only tighten the gate |
| Money is exact | `Decimal` end to end; never floats |
| Tenant funds never mix | clause P4, model-checked; a rich neighbour's wallet is never used |
| Webhooks: duplicates, disorder, forgery | HMAC-SHA256 over timestamp+raw body, 5-min replay window, atomic dedupe, ack-then-process, sticky terminal states |
| The real API behaves as the code assumes | `python -m payops.smoke` -> `docs/VERIFICATION.md` (14 live checks pass, 1 skipped) |

## ACCS components (accs.md)
| Spec | Where | Result |
|---|---|---|
| F1 `policy.check` | `policy.py`, `policy.json`, `POST /policy.check` | declarative, validated, fingerprinted |
| F2 `consistency.score` | `consistency.py`, `calibration.py`, `POST /consistency.score` | monotone; isotonic-calibrated: ECE 0.196 -> 0.015 (synthetic), 0.208 -> 0.043 (live LLM claims) |
| R1-R2 + proofs | `docs/RESEARCH.md` | paper proofs + exhaustive/empirical evidence, with limits stated |

## Run
```
conda env create -f environment.yml && conda activate payops-accs
python -m pytest -q       # 321 tests, no network
python demo.py            # terminal demo (offline)
python run_ui.py          # web UI with 7 scripted scenarios
python run_live.py        # terminal run against the real sandbox
python -m payops.smoke    # verify every live claim, rewrite docs/VERIFICATION.md
python -m payops.api      # ACCS HTTP API on :8765
python run_webhooks.py    # live webhook receiver (needs AWX_WEBHOOK_SECRET and a public tunnel)
```
`.env` (gitignored): `AWX_CLIENT_ID`, `AWX_API_KEY` (scoped key: Balances R, Global Accounts R/W, Simulations W, Beneficiaries R/W, Transfers R/W),
`OPENAI_BASE_URL`, `OPENAI_API_KEY` (any OpenAI-compatible endpoint, e.g. AWS Bedrock Mantle). Default model `openai.gpt-oss-120b`
(`PAYOPS_MODEL`), judge `openai.gpt-oss-safeguard-120b` (`PAYOPS_JUDGE_MODEL`), cap `PAYOPS_MAX_LLM_CALLS_PER_HOUR` (default 120),
optional `PAYOPS_APPROVER_TOKEN`. Without keys an offline heuristic proposer runs and the simulator needs nothing. A fresh sandbox is set up
for you (`payops/bootstrap.py`: EUR account, simulated deposit, beneficiary checked against `POST /beneficiaries/schema`).

## Docs
`docs/RESEARCH.md` proofs and measurements - `docs/VERIFICATION.md` live sandbox record - `docs/REFERENCE-REVIEW.md` what we learned from the nine
reference builds - `docs/DEMO-SCRIPT.md` 4.5-minute walkthrough - `docs/DEMO-VS-PRODUCTION.md` every shortcut - `docs/GOING-TO-PRODUCTION.md` what to add.

## Verified against the live sandbox
- Simulation body is `next_status`; failure causes appear only on `GET /transfers/{id}` as `failure.code` (type is always `INCORRECT_ROUTING`); the list omits them.
- A failed SWIFT payout refunds the amount but **keeps the fee**; the fee depends on `swift_charge_option` (13.39 SHARED vs 22.29 PAYER observed), so a replacement mirrors the original's option and budgets the observed fee.
- A reused `request_id` is rejected with `400 duplicate_request_id` (existing transfer id in the message), not replayed, even if the payload differs. `request_id` must be 10-50 characters. Ids come back upper-cased.
- `POST /transfers/validate` is a free dry run; `x-api-version: 2026-08-21` is accepted; PAID before SENT in simulation answers 500 and is never retried.
- Only infrastructure faults (`CHANNEL_TIMEOUT`, `SYSTEM_ERROR`, `UNABLE_TO_APPLY`) auto-replace; recalls, client requests, bad details and `DUPLICATION_RETURN` go to a human.

## Honest status
- **Live-verified:** Airwallex REST on our own account, the LLM proposer, the safeguard judge, the full agent loop and the UI's live mode.
- **Simulated:** connected accounts / `x-on-behalf-of` (not enabled on this sandbox; the tenant logic is model-checked and tested on the simulator); real webhook delivery (needs a tunnel; signature, dedupe, ordering and the worker are tested).
- **Not done:** no Coq/Lean mechanization (R1 is an exhaustive finite-state check plus a paper argument); the live-LLM calibration set is small (89 test claims) and shows calibration, not extra discrimination over the base rate; approver authentication is a shared token (see `docs/GOING-TO-PRODUCTION.md`).
