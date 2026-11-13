# Sandbox verification

Recorded 2026-10-30 08:16:19Z by `python -m payops.smoke`. Environment: Airwallex sandbox, base URL `https://api.sandbox.airwallex.com`, API version `2026-08-21`. Regenerate any time; a FAIL means the API drifted from what the code assumes.

| ID | Claim | Result | Detail |
| --- | --- | --- | --- |
| V1 | login + pinned x-api-version | pass | bearer token, version header 2026-08-21 accepted on later calls |
| V2 | account funded, beneficiary present | pass | EUR available 10,022,017.42; setup reused existing |
| V3 | POST /beneficiaries/schema lists required fields | pass | 7 required leaf fields: city, country_code, account_currency, bank_country_code, bank_name, entity_type, payment_methods |
| V4 | POST /transfers/validate accepts a good transfer | pass | validate -> OK (free dry run) |
| V5 | validate rejects a bad beneficiary | pass | unknown beneficiary rejected before any fee: The request failed our schema validation. Refer to the errors object f |
| V6 | create transfer with swift_charge_option=PAYER | pass | transfer 3fbc7539 SCHEDULED; fee 22.34 EUR with swift_charge_option=PAYER |
| V7 | reused request_id -> duplicate_request_id | pass | reused request_id -> 400 duplicate_request_id, client resolved to the SAME transfer |
| V8 | reused request_id, different amount -> conflict | pass | same id, different amount -> request_id_conflict |
| V9 | request_id length bounds | pass | request_id must be 10-50 chars (60 rejected, ours are 22) |
| V10 | SENT then FAILED(type) ends CANCELLED with a recoverable cause | pass | SENT then FAILED(CHANNEL_TIMEOUT) -> CANCELLED, cause recovered from failure.code = CHANNEL_TIMEOUT |
| V11 | list omits failure info, detail has it | pass | list omits the cause (failure_type=None); detail has failure.code=91402, details.type=INCORRECT_ROUTING |
| V12 | invalid simulation transition is not retried | pass | PAID before SENT -> 500 operation_failed (not retried); cleaned up |
| V13 | failed SWIFT payout keeps its fee | pass | a failed SWIFT payout refunds the amount but KEEPS the fee: balance fell by 22.34 EUR (fee 22.34) |
| V14 | full agent loop on the real sandbox | pass | agent end to end: WAIT -> REPLACE under deterministic id po-406a2ad573c72a67-r1 -> WAIT on the next ping; 2 transfers |
| V15 | webhook delivery | pass | real `payout.transfer.*` events pushed by Airwallex through an ngrok tunnel to a webhook-only receiver; 14 failures decided by the agent (`python run_webhook_e2e.py`, record in `docs/live-proof/webhook-e2e.json`) |
| V16 | connected account: create, submit, activate, fund, beneficiary | pass | connected account acct_EbNswEPiP ACTIVE with EIN, global account + funded wallet + SWIFT beneficiary created on its behalf |
| V17 | x-on-behalf-of transfers belong to the tenant, not the platform | pass | x-on-behalf-of transfer 7593b3b0 visible to the tenant (1) and NOT to the platform's own account (0) |
| V18 | a tenant transfer is paid from the tenant wallet only | pass | tenant wallet paid 34.34 EUR; the platform wallet paid 0.00 (one tenant's money never funds another) |
| V19 | beneficiary validated server-side before create | pass | POST /beneficiaries/validate: good payload OK; bad IBAN rejected (ers. [beneficiary.bank_details.iban:083]); nothing created |

## Notes on behaviour found while verifying (all against the live sandbox)
* To submit a connected account: `business_details.account_usage.product_reference` (and `estimated_monthly_revenue` for businesses), an EIN in `business_identifiers`, and `business_person_details[].identifications.primary` are all required. A missing identification fails with the unhelpful `field_required: ensure is not null`.
* For a few minutes after activation, `transfers/create` on behalf of the account returns `400 fee_unknown`; `transfers/validate` passes during that window, so only a real create shows readiness.
* A transfer that ends CANCELLED has its principal returned and keeps its fee; one that sits at FAILED has NOT had its principal returned. Both mean "will not deliver", and which one a failure reaches is not predictable from timing or concurrency (found by reconciling wallets in `docs/LIVE-PROOF.md`).
* `GET /transfers` is newest-first and pages with a `page` cursor (the previous `page_after`). WITHOUT a `page` parameter it silently returns only the last 30 days; `page=0` is a full search. `reference` is not a filter; `request_id` is (case-insensitive).
* The simulator answers `500 operation_failed` if a status change arrives before a brand-new transfer has reached PROCESSING; retry briefly. It never needs a retry from the agent, which does not use the simulator.
