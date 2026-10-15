# Demo vs production

Every deliberate shortcut is tagged in the code at the place it lives. Audit with:

```
rg -n "DEMO-ONLY:" payops        # unsafe or incorrect in production
rg -n "SANDBOX:"   payops        # relies on a sandbox-only Airwallex feature
rg -n "AIRWALLEX:" payops        # a non-obvious API behaviour the code depends on
rg -n "PROD:"      payops        # what to do instead
```

| Area | Demo / sandbox behaviour | Production approach |
| --- | --- | --- |
| Airwallex | `FakeAirwallex` in-memory simulator for offline runs and tests | `AirwallexREST` against the live API |
| Funding | `simulate_deposit` and `bootstrap.ensure_setup` fund the sandbox | accounts are funded for real; never auto-fund |
| Transfer state changes | `simulation/transfers/{id}/transition` forces SENT / PAID / FAILED | wait for `payout.transfer.*` webhooks |
| Tenants | scenario tenants are labels; live mode uses our own account because connected accounts are not enabled here | one connected account per customer, every call with `x-on-behalf-of` |
| Approvals | the UI's approve/deny has no authentication unless `PAYOPS_APPROVER_TOKEN` is set | real authentication (SSO or signed per-user tokens); take the approver from the identity |
| Webhooks | `allow_test_events` exists for the console's test events | leave it off; register the URL, keep the secret out of the repo |
| State | SQLite file (single process) | a managed database; one handler per obligation is enforced by an in-process lock, so run one worker or move the lock into the database |
| Model | `openai.gpt-oss-120b` via an OpenAI-compatible endpoint, capped per hour | a vetted model behind your own gateway; keep the gate regardless of model |
| Beneficiary | one fixture company beneficiary (`Acme Supplies GmbH`) | the supplier's real, verified bank details |
| Calibration | fitted on synthetic evidence noise plus 89 live-model claims | refit on real incident traffic before trusting the probabilities |
