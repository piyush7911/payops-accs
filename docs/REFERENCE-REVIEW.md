# What we learned from the nine Airwallex reference builds

Source: the `awx-devx` GitHub organisation, read (not run) in October 2026. This records what changed in our design because of
them, and what we deliberately did not take. Where a claim is about their code I read it; where I only saw a directory listing
it says so.

## What each one is
| Repo | What it does | Most useful to us |
| --- | --- | --- |
| global-payout | cheapest-rail contractor batches | live verification record; client reliability rules; deterministic request ids; real status map |
| fx-target-payment | pays FX bills at a target rate | `verification-results.md` (failure object, `swift_charge_option`, request-id limits, PAID -> CANCELLED), validate-before-pay, LLM call cap |
| allocard | rule-derived card limits, reconciler, webhooks | webhook contract (ack first, process out of band), desired-vs-applied reconciliation, 241 test files |
| agent-cards | per-agent card policy + approval queue + MCP | approval queue with dedupe and idempotent resolve; going-to-production doc |
| finance-manager | Telegram agent on AgentOS + compute MCP | confirms where we differ: its controls are skill text (instructions to the model), ours are code |
| freelance-marketplace | milestone release on connected accounts | `DEMO-ONLY / PROD / SANDBOX / AIRWALLEX` comment tags and a shortcut inventory |
| slack-bot | thread -> approved invoice | draft-then-approve; a preflight that names what is missing |
| drop-in-checkout | embedded checkout sample | atomic webhook event claims |
| sticker-pack | swag | none |

## What we changed because of them
| Finding | Where it came from | What we did |
| --- | --- | --- |
| Real transfer statuses include `SCHEDULED`, `IN_APPROVAL`, `OVERDUE`; our first policy only knew `PROCESSING/SENT/PAID`, so a `SCHEDULED` original would not have blocked a replacement | global-payout status map; we also saw `SCHEDULED` in our own probe | everything not definitively dead counts as live (fail-safe, unknown statuses too); formal model extended with these statuses |
| A `PAID` transfer can later become `CANCELLED` (late bank return) | fx-target verification | modelled in the exhaustive check; scenario "PAID, then returned" |
| `swift_charge_option` defaults to SHARED (banks may deduct from what the supplier gets); the fee differs by option (13.39 vs 22.29 EUR measured) | fx-target decisions D-18 | replacement mirrors the original's option; fee budget uses the observed fee |
| `POST /transfers/validate` is a free dry run; a failed SWIFT payout still costs a fee | fx-target (arming) + our live check | preflight before any replacement |
| Request ids: rejected at 54+ chars there; we measured the real bounds 10-50 | fx-target; our live check | deterministic ids `po-<16 hex>-r<n>` (22 chars) |
| Deterministic ids make retries safe even if local state is lost | global-payout id builders | the replacement id is derived from (obligation, attempt) by code; the model never chooses it |
| Client rules: single-flight token refresh, one re-login on 401, backoff with jitter on 429/5xx/timeouts re-sending the same body, 15 rps / 8 per path, lossless amounts, typed errors with trace id, redaction | global-payout client | implemented in `AirwallexREST`, covered by scripted-network tests; money is `Decimal` end to end |
| Simulation `PAID` before `SENT` answers 500 `operation_failed` | global-payout / fx-target | simulation calls are never retried |
| Webhooks: answer 200 immediately, persist, process out of band; dedupe atomically; never regress a terminal state; `data.object` or `data`; test events use the payload's `client-secret-key` | allocard, drop-in, global-payout | durable receiver + worker; sticky terminals; test-event secret is OFF by default because trusting a key the sender supplies authenticates nothing |
| Approval queue: dedupe pending requests, resolve once | agent-cards | approvals bound to exact terms; expire if state changed; hard clauses never queued |
| LLM calls capped per hour | fx-target D-28 | `BudgetedProposer`, fails safe to a human |
| Tag every shortcut | freelance-marketplace | `DEMO-ONLY:` / `PROD:` / `SANDBOX:` / `AIRWALLEX:` comments + `docs/DEMO-VS-PRODUCTION.md` |
| Smoke script that regenerates a verification record | global-payout, fx-target | `python -m payops.smoke` -> `docs/VERIFICATION.md` |
| A reusable `request_id` returns 400 `duplicate_request_id` with the existing id in the message | global-payout V11; we then verified for transfers | client resolves a duplicate to the existing transfer and refuses if the amount differs |

## A result that came out of it
Adding human-overridable clauses changed what the exhaustive check says about P1 (single live payment): it was redundant when P2
(latest transfer cancelled with a replaceable failure) implied it, and becomes **necessary** once a human can override P2. The
check proves both statements; see `tests/test_model_check.py`.

## What we did not take
* Their UIs, stacks (Next.js, Mongo, Redis, BullMQ) and design systems: we kept stdlib Python and one self-contained page.
* Connected-account flows (freelance-marketplace, payroll-style kits): not enabled on our sandbox; simulated.
* Bill extraction, charts, FX targeting: out of scope for payout recovery.
* Their MCP servers: ours is not an MCP server; see the README note on how our REST calls map to the developer MCP.
