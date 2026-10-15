# Notes for coding agents working in this repo

Payment Ops Incident Commander (Airwallex hackathon, Kit 03) + ACCS. Stdlib Python 3.11.
`conda env create -f environment.yml && conda activate payops-accs`, then `python -m pytest -q` (321 tests, ~9s, no network).

## Invariants you must not weaken
* The model is untrusted. It proposes; `payops/policy.py` (declarative `policy.json`) decides; money moves only in `Commander._replace`.
* `request_id`s are minted by code (`store.request_id_for`), never by the model. A write-ahead `PENDING` attempt is committed before
  any create call. Retries reuse the same id.
* A transfer is live unless it is definitively `CANCELLED`/`FAILED` (`models.is_live`). Do not enumerate "live" statuses.
* Money is `Decimal` (`models.D`). Never do money arithmetic on floats; serialise with `default=str`.
* Only clauses marked `overridable` in `policy.json` (P2, P6) can be waived by a human approval; hard clauses never.
* After any change to `policy.json`, `policy.py`, or `model_check.py`, run `python -m payops.model_check` and the tests; every
  clause must still be shown necessary (P1 is necessary because P2 is overridable).
* Calibration may only tighten the gate: the gate uses `min(raw, calibrated)`.

## Conventions
* Tag shortcuts: `DEMO-ONLY:` (+ `PROD:`), `SANDBOX:`, `AIRWALLEX:` (non-obvious API behaviour). Keep `docs/DEMO-VS-PRODUCTION.md` in sync.
* Verified-live facts live in `docs/VERIFICATION.md` (regenerate with `python -m payops.smoke`; needs `.env` with AWX_* keys, sandbox only).
* Secrets: `.env` is gitignored. Never print keys, never commit them, never put them in a prompt.
* UI text from the model is untrusted: render through `esc()` only (a test enforces the single `innerHTML` sink).
