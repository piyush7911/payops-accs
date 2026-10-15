# Sandbox verification

Recorded 2026-10-15 11:38:53Z by `python -m payops.smoke`. Environment: Airwallex sandbox, base URL `https://api.sandbox.airwallex.com`, API version `2026-08-21`. Regenerate any time; a FAIL means the API drifted from what the code assumes.

| ID | Claim | Result | Detail |
| --- | --- | --- | --- |
| V1 | login + pinned x-api-version | pass | bearer token, version header 2026-08-21 accepted on later calls |
| V2 | account funded, beneficiary present | pass | EUR available 10,023,331.47; setup reused existing |
| V3 | POST /beneficiaries/schema lists required fields | pass | 7 required leaf fields: city, country_code, account_currency, bank_country_code, bank_name, entity_type, payment_methods |
| V4 | POST /transfers/validate accepts a good transfer | pass | validate -> OK (free dry run) |
| V5 | validate rejects a bad beneficiary | pass | unknown beneficiary rejected before any fee: The request failed our schema validation. Refer to the errors object f |
| V6 | create transfer with swift_charge_option=PAYER | pass | transfer af44c6a9 SCHEDULED; fee 22.3 EUR with swift_charge_option=PAYER |
| V7 | reused request_id -> duplicate_request_id | pass | reused request_id -> 400 duplicate_request_id, client resolved to the SAME transfer |
| V8 | reused request_id, different amount -> conflict | pass | same id, different amount -> request_id_conflict |
| V9 | request_id length bounds | pass | request_id must be 10-50 chars (60 rejected, ours are 22) |
| V10 | SENT then FAILED(type) ends CANCELLED with a recoverable cause | pass | SENT then FAILED(CHANNEL_TIMEOUT) -> CANCELLED, cause recovered from failure.code = CHANNEL_TIMEOUT |
| V11 | list omits failure info, detail has it | pass | list omits the cause (failure_type=None); detail has failure.code=91402, details.type=INCORRECT_ROUTING |
| V12 | invalid simulation transition is not retried | pass | PAID before SENT -> 500 operation_failed (not retried); cleaned up |
| V13 | failed SWIFT payout keeps its fee | pass | a failed SWIFT payout refunds the amount but KEEPS the fee: balance fell by 22.30 EUR (fee 22.3) |
| V14 | full agent loop on the real sandbox | pass | agent end to end: WAIT -> REPLACE under deterministic id po-8831f142ff5b8031-r1 -> WAIT on the next ping; 2 transfers |
| V15 | webhook delivery | skip | webhook delivery needs a public tunnel and a secret registered in the web app; signature, dedupe, ordering and worker are covered by tests |
