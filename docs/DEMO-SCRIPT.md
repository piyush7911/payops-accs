# Demo script (about 4.5 minutes)

Setup: `python run_ui.py` -> http://127.0.0.1:8765 . Simulator mode is deterministic; switch the model to "Offline heuristic" if the
network is flaky. Have `docs/VERIFICATION.md` open in a second tab.

**0:00 - the problem (20s).** "A supplier says a payout never arrived. The usual choices are both bad: wait too long and miss the
deadline, or resend and pay twice. We built an agent that decides, and a gate that makes paying twice impossible."

**0:20 - Timeout, auto-replace (60s).** Click it. Point at: *Goal* -> the agent *waits* while the transfer is in flight (ACCS gate:
evidence from live `GET /transfers`, score) -> *New information*: failed with `CHANNEL_TIMEOUT` -> the agent proposes REPLACE, the gate
PERMITS (policy clauses, safeguard-model second opinion) -> a *second* supplier ping creates no duplicate -> *Result*: original
CANCELLED, replacement PAID, **both fees counted**, "Live transfers 1 (max 1)".

**1:20 - Duplicate return -> human (50s).** Click it. "DUPLICATION_RETURN means the supplier may already hold the money. The
agent does not guess; it queues an approval." Show the approval card: exact amount, beneficiary, `request_id`, latest transfer.
Press Approve. "Bound to these terms: if anything had changed in the meantime it would expire. Double-clicking is safe."

**2:10 - Hallucinating model (40s).** Click it. "A misbehaving model claims a live transfer failed and pushes to resend." Show score
0.10, DENY, no second transfer, and the reply to the supplier replaced by a template built from verified state.

**2:50 - Crash mid-replacement (40s).** Click it. "The process dies after writing its intent and before calling Airwallex.
Restart: it recovers from the database and retries with the *same* request_id, once."

**3:30 - Webhooks x3 (25s).** Click it. "Three deliveries, one reaction; a forged signature gets 401."

**3:55 - Why you can trust it (35s).** Point at the header badge: "20 states - 2,629 decisions - 0 violations": an exhaustive check of
the gate against every possible proposal from an untrusted model, and every clause shown necessary by deleting it. Open
`docs/VERIFICATION.md`: "15 live checks against the real sandbox."

**Close (15s).** "The model reads messy human context; code guarantees nobody is paid twice."

Backup if live fails: Reset -> Simulator -> Offline heuristic; every scenario runs without any network.
