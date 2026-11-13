"""ACCS interface contract as a local HTTP API (stdlib only). Run: python -m payops.api [port]

POST /policy.check        {obligation, transfers, action, wallet_balance, consistency, used_request_ids, human_approved}
                          -> {permit, clause, detail, policy_digest}
POST /consistency.score   {claims:[{subject,field,value}], transfers:[...], obligation_id}
                          -> {value (calibrated P[true]), raw, gate_value=min(raw,value) (what policy P7 uses), provenance, penalty, per_claim:[...]}
POST /webhooks/airwallex  signed Airwallex events (x-timestamp, x-signature) -> verify, dedupe, order, dispatch to the Commander
GET  /health
"""
from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import consistency as C
from . import policy as P
from .models import Action, Claim, Obligation, TransferRec


def _transfers(items):
    return [TransferRec(t["id"], t.get("obligation_id", "ob"), t.get("tenant", "A"), t.get("request_id", t["id"]),
                        t["status"], t.get("failure_type"), float(t.get("amount", 0))) for t in items]


def policy_check(body: dict) -> dict:
    o = body["obligation"]
    ob = Obligation(o["id"], o["tenant"], float(o["amount"]), o.get("currency", "EUR"), o.get("method", "SWIFT"),
                    o.get("beneficiary_id", "ben"), o.get("observed_fee"))
    a = body["action"]
    action = Action(a["kind"], a.get("obligation_id", ob.id), a.get("tenant", ob.tenant), a.get("request_id"),
                    a.get("funding_tenant"), a.get("reason", ""))
    ctx = P.Context(ob, _transfers(body.get("transfers", [])), set(body.get("used_request_ids", [])),
                    float(body.get("wallet_balance", 0)), float(body.get("consistency", 1.0)), bool(body.get("human_approved")))
    v = P.check(action, ctx)
    return {"permit": v.permit, "clause": v.clause, "detail": v.detail, "policy_digest": P.DEFAULT.digest}


def consistency_score(body: dict) -> dict:
    ts = _transfers(body.get("transfers", []))
    ev = C.evidence_from_transfers(ts, body.get("obligation_id"))
    cal = C.default_calibrator()
    claims = [Claim(c["subject"], c["field"], str(c["value"])) for c in body.get("claims", [])]
    per = [C.score(c, ev, cal) for c in claims]
    worst = C.min_score(claims, ev, cal)
    return {"value": worst.value, "raw": worst.raw, "gate_value": min(worst.raw, worst.value), "penalty": worst.penalty, "provenance": worst.provenance,
            "batch_penalty": C.batch_penalty(per),
            "per_claim": [{"claim": vars(c), "value": r.value, "provenance": r.provenance, "penalty": r.penalty} for c, r in zip(claims, per)]}


SERVICE = {"svc": None}  # set by attach_service(); powers the web UI and /api/*
UI_PATH = __import__("os").path.join(__import__("os").path.dirname(__file__), "ui", "index.html")


def attach_service(svc) -> None:
    SERVICE["svc"] = svc


WEBHOOKS = {"receiver": None}  # set by configure_webhooks(); POST /webhooks/airwallex needs it


def configure_webhooks(receiver) -> None:
    WEBHOOKS["receiver"] = receiver


ROUTES = {"/policy.check": policy_check, "/consistency.score": consistency_score}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, obj: dict) -> None:
        data = json.dumps(obj, default=str).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _html(self) -> None:
        try:
            data = open(UI_PATH, "rb").read()
        except OSError:
            return self._send(404, {"error": "ui not built"})
        self.send_response(200)
        self.send_header("content-type", "text/html; charset=utf-8")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        svc = SERVICE["svc"]
        path = self.path.split("?", 1)[0]  # ignore the query string (e.g. /?present=1)
        if path == "/health":
            return self._send(200, {"ok": True, "policy_digest": P.DEFAULT.digest})
        if path in ("/", "/index.html") and svc:
            return self._html()
        if path == "/api/state" and svc:
            return self._send(200, svc.state())
        self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        if self.path == "/webhooks/airwallex":  # needs the RAW body for the signature, so it bypasses the JSON routes
            raw = self.rfile.read(int(self.headers.get("content-length", 0)))
            rcv = WEBHOOKS["receiver"]
            return self._send(*(rcv.handle(dict(self.headers.items()), raw) if rcv else (503, {"error": "webhooks not configured"})))
        svc = SERVICE["svc"]
        if svc and self.path.startswith("/api/"):
            return self._service_post(svc)
        fn = ROUTES.get(self.path)
        if not fn:
            return self._send(404, {"error": "not found"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
            self._send(200, fn(body))
        except (KeyError, ValueError, TypeError) as e:
            self._send(400, {"error": f"{type(e).__name__}: {e}"})

    def _service_post(self, svc) -> None:
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
            parts = self.path.strip("/").split("/")  # api/<verb>/...
            if parts[1] == "scenario" and len(parts) == 3:
                return self._send(200, svc.start(parts[2]))
            if parts[1] == "approvals" and len(parts) == 4 and parts[3] in ("approve", "deny"):
                # DEMO-ONLY: with PAYOPS_APPROVER_TOKEN unset anyone who can reach this port can approve a payment.
                # PROD: put this behind real authentication (SSO / signed per-user tokens) and take `by` from the identity, not the body.
                need = __import__("os").environ.get("PAYOPS_APPROVER_TOKEN")
                if need and not __import__("hmac").compare_digest(need, self.headers.get("x-approver-token", "")):
                    return self._send(403, {"error": "approver token required"})
                by = str(body.get("by", "operator"))[:60]
                return self._send(200, (svc.approve if parts[3] == "approve" else svc.deny)(parts[2], by))
            if parts[1] == "reset":
                svc.reset(mode=body.get("mode"), use_llm=body.get("use_llm"))
                return self._send(200, {"ok": True})
            self._send(404, {"error": "not found"})
        except KeyError as e:
            self._send(404, {"error": f"unknown id {e}"})
        except (ValueError, TypeError) as e:
            self._send(400, {"error": f"{type(e).__name__}: {e}"})

    def log_message(self, *a):  # quiet
        pass


def serve(port: int = 8765) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    import os
    if os.environ.get("AWX_WEBHOOK_SECRET"):
        print("webhooks: set AWX_WEBHOOK_SECRET present; wire a Commander with api.configure_webhooks(...) (see run_webhooks.py)")
    print(f"ACCS API on http://127.0.0.1:{port}")
    serve(port).serve_forever()
