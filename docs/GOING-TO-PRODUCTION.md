# Going to production

What this repo already guarantees, and what you must add around it.

## Already enforced in code (and tested)
* One live payment per obligation; deterministic, write-ahead request ids; restart recovery (`Commander.recover`).
* Hard clauses can never be overridden by a human or a model; overridable ones need an approval bound to the exact terms.
* The model never holds credentials, never chooses a request id, and its text only reaches a supplier if every claim is corroborated.
* Exact decimal money; typed Airwallex errors with redaction; retries re-send the same `request_id`.

## You must add
1. **Authentication for approvers.** Today `PAYOPS_APPROVER_TOKEN` is a shared secret. Use SSO or signed per-user tokens, record the
   approver identity from the token, and consider requiring two people above a threshold.
2. **Per-agent identity.** If several agents or tenants call this service, give each its own key or short-lived token and scope what it
   may propose; the gate is per obligation but does not know who is asking.
3. **Connected accounts.** Create and activate one per customer, then send `x-on-behalf-of` on every call. The code paths exist
   (tenant isolation is clause P4 and is model-checked); they have only been exercised on the simulator.
4. **Durable infrastructure.** Move SQLite to a managed database, add backups, and either run a single worker or take the
   per-obligation lock in the database. Alert on `ALERT` audit rows and on attempts stuck in `PENDING`.
5. **Webhooks.** Register the URL, store the secret in a secrets manager, allow Airwallex's published IP ranges, and confirm the exact
   payout event names in your account (matched by the `payout.transfer.` prefix here).
6. **Secrets and keys.** Scoped API keys only (Balances R, Global Accounts R/W, Beneficiaries R/W, Transfers R/W, plus Connected Accounts
   for platforms), IP allowlist if your egress is stable, rotation, nothing in the repo or in chat.
7. **Rate and cost limits.** Keep `PAYOPS_MAX_LLM_CALLS_PER_HOUR`; put the HTTP API behind a gateway with its own limits.
8. **Calibration and policy review.** Refit the scorer on real incidents; have finance review `policy.json` (autonomy limit,
   replaceable failure types, max replacements) and re-run `python -m payops.model_check` after every policy change.
9. **Observability.** Ship the audit log; every decision already records policy digest, score, provenance and request id.
