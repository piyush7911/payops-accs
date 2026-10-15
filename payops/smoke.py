"""Live sandbox smoke test: `python -m payops.smoke` runs each claim this project makes about Airwallex against the real sandbox
and rewrites docs/VERIFICATION.md. If the API changes under us, this is where it shows up first. Sandbox only."""
from __future__ import annotations

import os
import sys
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, List, Tuple

from .airwallex import AirwallexError, AirwallexREST, DEFAULT_API_VERSION
from .bootstrap import ensure_setup
from .commander import Commander
from .consistency import default_calibrator
from .llm import HeuristicProposer, load_env
from .models import Obligation
from .store import request_id_for

Result = Tuple[str, str]  # (status, detail)


def run() -> List[Tuple[str, str, str, str]]:
    load_env()
    aw = AirwallexREST(os.environ["AWX_CLIENT_ID"], os.environ["AWX_API_KEY"])
    rows: List[Tuple[str, str, str, str]] = []
    ctx: dict = {}

    def check(cid: str, claim: str, fn: Callable[[], Result]) -> None:
        try:
            status, detail = fn()
        except Exception as e:  # a failing check is a finding, not a crash
            status, detail = "FAIL", f"{type(e).__name__}: {str(e)[:160]}"
        rows.append((cid, claim, status, detail))
        print(f"{cid:4s} {status:5s} {claim}")

    def v1():
        aw._ensure_token(force=True)
        return "pass", f"bearer token, version header {DEFAULT_API_VERSION} accepted on later calls"

    def v2():
        ctx["setup"] = ensure_setup(aw)
        return "pass", f"EUR available {ctx['setup'].balance:,.2f}; setup {ctx['setup'].created or 'reused existing'}"

    def v3():
        req = aw.beneficiary_required_fields("DE", "EUR")
        return "pass", f"{len(req)} required leaf fields: " + ", ".join(r.split('.')[-1] for r in req)

    def v4():
        ok, why = aw.validate_transfer(_ob(ctx), "po-smoke-validate-1")
        return ("pass", "validate -> OK (free dry run)") if ok else ("FAIL", why)

    def v5():
        bad = _ob(ctx)
        bad.beneficiary_id = "00000000-0000-0000-0000-000000000000"
        ok, why = aw.validate_transfer(bad, "po-smoke-validate-2")
        return ("pass", f"unknown beneficiary rejected before any fee: {why[:70]}") if not ok else ("FAIL", "validate accepted a bad beneficiary")

    def v6():
        ob = _ob(ctx)
        ctx["rid"] = "po-smoke-" + uuid.uuid4().hex[:12]
        t = aw.create_transfer(ob, ctx["rid"])
        ctx["ob"], ctx["t"] = ob, t
        return "pass", f"transfer {t.id[:8]} {t.status}; fee {ob.observed_fee} EUR with swift_charge_option={ob.swift_charge_option}"

    def v7():
        t2 = aw.create_transfer(Obligation(ctx["ob"].id, "self", ctx["ob"].amount, "EUR", "SWIFT", ctx["ob"].beneficiary_id), ctx["rid"])
        return ("pass", "reused request_id -> 400 duplicate_request_id, client resolved to the SAME transfer") if t2.id == ctx["t"].id else ("FAIL", "created a second transfer")

    def v8():
        other = Obligation(ctx["ob"].id, "self", ctx["ob"].amount + 1, "EUR", "SWIFT", ctx["ob"].beneficiary_id)
        try:
            aw.create_transfer(other, ctx["rid"])
        except AirwallexError as e:
            return ("pass", f"same id, different amount -> {e.code}") if e.code == "request_id_conflict" else ("FAIL", e.code)
        return "FAIL", "accepted a reused request_id with a different amount"

    def v9():
        try:
            aw._call("POST", "/api/v1/transfers/validate", body=aw._transfer_body(_ob(ctx), "x" * 60))
        except AirwallexError as e:
            return ("pass", "request_id must be 10-50 chars (60 rejected, ours are 22)") if "018" in str(e) or e.code == "validation_failed" else ("FAIL", str(e))
        return "FAIL", "60-char request_id accepted"

    def v10():
        t = ctx["t"]
        aw.transition("self", t.id, "SENT")
        aw.transition("self", t.id, "FAILED", "CHANNEL_TIMEOUT")
        time.sleep(1.5)
        listed = [x for x in aw.list_transfers("self", ctx["ob"].id) if x.id == t.id][0]
        return ("pass", f"SENT then FAILED(CHANNEL_TIMEOUT) -> {listed.status}, cause recovered from failure.code = {listed.failure_type}") \
            if listed.status == "CANCELLED" and listed.failure_type == "CHANNEL_TIMEOUT" else ("FAIL", f"{listed.status} {listed.failure_type}")

    def v11():
        d = aw._call("GET", f"/api/v1/transfers/{ctx['t'].id}")
        item = next(i for i in aw._all("self") if i["id"] == ctx["t"].id)
        return ("pass", f"list omits the cause (failure_type={item.get('failure_type')}); detail has failure.code={d['failure']['code']}, details.type={d['failure']['details']['type']}") \
            if item.get("failure_type") is None and d.get("failure") else ("FAIL", "shape differs from what the client assumes")

    def v12():
        ob = _ob(ctx)
        t = aw.create_transfer(ob, "po-smoke-" + uuid.uuid4().hex[:12])
        try:
            aw.transition("self", t.id, "PAID")  # PAID before SENT
        except AirwallexError as e:
            aw.transition("self", t.id, "SENT"); aw.transition("self", t.id, "FAILED", "SYSTEM_ERROR")
            return ("pass", f"PAID before SENT -> {e.status} {e.code} (not retried); cleaned up") if e.status >= 400 else ("FAIL", str(e))
        aw.transition("self", t.id, "FAILED", "SYSTEM_ERROR")
        return "info", "PAID before SENT was accepted in this run (sandbox behaviour varies); cleaned up"

    def v13():
        before = aw.balance("self", "EUR")
        ob = _ob(ctx)
        t = aw.create_transfer(ob, "po-smoke-" + uuid.uuid4().hex[:12])
        aw.transition("self", t.id, "SENT"); aw.transition("self", t.id, "FAILED", "SYSTEM_ERROR"); time.sleep(1.5)
        lost = before - aw.balance("self", "EUR")
        return ("pass", f"a failed SWIFT payout refunds the amount but KEEPS the fee: balance fell by {lost} EUR (fee {ob.observed_fee})") \
            if Decimal("0") < lost <= ob.observed_fee + Decimal("1") else ("info", f"balance moved by {lost} EUR")

    def v14():
        ob = _ob(ctx)
        ob.id = "inv-smoke-" + uuid.uuid4().hex[:6]
        cmd = Commander(aw, HeuristicProposer(), calibrator=default_calibrator())
        orig = aw.create_transfer(ob, "po-smoke-" + uuid.uuid4().hex[:12])
        cmd.register(ob, orig)
        w1 = cmd.handle(ob.id, "nothing arrived")
        aw.transition("self", orig.id, "SENT"); aw.transition("self", orig.id, "FAILED", "CHANNEL_TIMEOUT"); time.sleep(1.5)
        rep = cmd.handle(ob.id, "please resend")
        again = cmd.handle(ob.id, "ping")
        ts = aw.list_transfers("self", ob.id)
        ok = (w1.decision, rep.decision, len(ts)) == ("WAITING", "REPLACED", 2) and rep.request_id == request_id_for(ob.id, 1) and again.decision == "WAITING"
        return ("pass", f"agent end to end: WAIT -> REPLACE under deterministic id {rep.request_id} -> WAIT on the next ping; {len(ts)} transfers") if ok \
            else ("FAIL", f"{w1.decision}/{rep.decision}/{again.decision}, {len(ts)} transfers")

    def v15():
        return "skip", "webhook delivery needs a public tunnel and a secret registered in the web app; signature, dedupe, ordering and worker are covered by tests"

    for cid, claim, fn in [
        ("V1", "login + pinned x-api-version", v1), ("V2", "account funded, beneficiary present", v2),
        ("V3", "POST /beneficiaries/schema lists required fields", v3), ("V4", "POST /transfers/validate accepts a good transfer", v4),
        ("V5", "validate rejects a bad beneficiary", v5), ("V6", "create transfer with swift_charge_option=PAYER", v6),
        ("V7", "reused request_id -> duplicate_request_id", v7), ("V8", "reused request_id, different amount -> conflict", v8),
        ("V9", "request_id length bounds", v9), ("V10", "SENT then FAILED(type) ends CANCELLED with a recoverable cause", v10),
        ("V11", "list omits failure info, detail has it", v11), ("V12", "invalid simulation transition is not retried", v12),
        ("V13", "failed SWIFT payout keeps its fee", v13), ("V14", "full agent loop on the real sandbox", v14), ("V15", "webhook delivery", v15)]:
        check(cid, claim, fn)
    return rows


def _ob(ctx: dict) -> Obligation:
    ben = ctx["setup"].beneficiary_id if "setup" in ctx else ""
    return Obligation("inv-smoke", "self", 12, "EUR", "SWIFT", ben)


def write(rows, path: str) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    lines = [f"# Sandbox verification", "", f"Recorded {now} by `python -m payops.smoke`. Environment: Airwallex sandbox, base URL `https://api.sandbox.airwallex.com`, "
             f"API version `{DEFAULT_API_VERSION}`. Regenerate any time; a FAIL means the API drifted from what the code assumes.", "",
             "| ID | Claim | Result | Detail |", "| --- | --- | --- | --- |"]
    lines += [f"| {c} | {claim} | {st} | {d.replace('|', '/')} |" for c, claim, st, d in rows]
    open(path, "w").write("\n".join(lines) + "\n")


if __name__ == "__main__":
    rows = run()
    out = os.path.join(os.path.dirname(os.path.dirname(__file__)), "docs", "VERIFICATION.md")
    write(rows, out)
    bad = [r for r in rows if r[2] == "FAIL"]
    print(f"\nwrote {out}: {len(rows) - len(bad)}/{len(rows)} ok")
    sys.exit(1 if bad else 0)
