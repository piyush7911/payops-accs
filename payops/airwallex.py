"""Airwallex access behind one interface.

FakeAirwallex: in-memory simulator (offline demo + tests), multi-tenant, mirrors the sandbox state machine.
AirwallexREST: real sandbox client (stdlib only). NOTE: payload fields follow the public hackathon guide but
have not been exercised against a live sandbox from this repo yet - check them against the docs MCP.
"""
from __future__ import annotations

import itertools
from decimal import Decimal
import json
import logging
import random
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import Dict, List, Optional

from .models import D, Obligation, TransferRec

BASE = "https://api.sandbox.airwallex.com"


class AirwallexError(Exception):
    """An Airwallex 4xx/5xx with its machine-readable code (e.g. duplicate_request_id, validation_failed)."""
    def __init__(self, status: int, code: str, message: str, trace_id: Optional[str] = None) -> None:
        super().__init__(f"{status} {code}: {message}" + (f" [trace {trace_id}]" if trace_id else ""))
        self.status, self.code, self.message, self.trace_id = status, code, message, trace_id


# DEMO-ONLY: an in-memory Airwallex used for the offline simulator and tests; it mirrors the sandbox state machine
# (PROCESSING -> SENT -> PAID | CANCELLED, late PAID -> CANCELLED returns) but is not Airwallex.
# PROD: use AirwallexREST against the live API and wait for webhooks.
class FakeAirwallex:
    def __init__(self) -> None:
        self.wallets: Dict[tuple, Decimal] = {}  # (tenant, currency) -> balance
        self.transfers: Dict[str, TransferRec] = {}
        self._by_request: Dict[str, str] = {}
        self._ids = itertools.count(1)
        self.invalid_beneficiaries: set = set()  # beneficiary ids that fail validation
        self.fail_next_create = False  # simulate an ambiguous timeout after the transfer was created

    def deposit(self, tenant: str, currency: str, amount) -> None:
        self.wallets[(tenant, currency)] = self.wallets.get((tenant, currency), Decimal(0)) + D(amount)

    def balance(self, tenant: str, currency: str) -> Decimal:
        return self.wallets.get((tenant, currency), Decimal(0))

    def create_transfer(self, ob: Obligation, request_id: str) -> TransferRec:
        if request_id in self._by_request:  # idempotent on request_id
            return self.transfers[self._by_request[request_id]]
        need = ob.amount + ob.fee
        if self.balance(ob.tenant, ob.currency) < need:
            raise RuntimeError("insufficient_funds")
        self.wallets[(ob.tenant, ob.currency)] -= need
        t = TransferRec(f"tr_{next(self._ids)}", ob.id, ob.tenant, request_id, "PROCESSING", amount=ob.amount,
                        fee=ob.fee, charge_option=ob.swift_charge_option if ob.method == "SWIFT" else None)
        self.transfers[t.id] = t
        self._by_request[request_id] = t.id
        if self.fail_next_create:
            self.fail_next_create = False
            raise TimeoutError("ambiguous: request may have been processed")
        return t

    def validate_transfer(self, ob: Obligation, request_id: str):
        """Mirror of POST /transfers/validate: free dry run."""
        if ob.beneficiary_id in self.invalid_beneficiaries or ob.amount <= 0:
            return False, "invalid beneficiary or amount"
        return True, ""

    def find_by_request_id(self, tenant: str, request_id: str) -> Optional[TransferRec]:
        tid = self._by_request.get(request_id)
        return self.transfers.get(tid) if tid else None

    def list_transfers(self, tenant: str, obligation_id: str) -> List[TransferRec]:
        return [t for t in self.transfers.values() if t.tenant == tenant and t.obligation_id == obligation_id]

    def transition(self, tenant: str, transfer_id: str, to: str, failure_type: Optional[str] = None) -> TransferRec:
        """Simulation endpoint. SENT must come before PAID; FAILED (with failure_type) only after SENT."""
        t = self.transfers[transfer_id]
        if t.tenant != tenant:
            raise PermissionError("transfer belongs to another tenant")
        if to == "SENT" and t.status == "PROCESSING":
            t.status = "SENT"
        elif to == "PAID" and t.status == "SENT":
            t.status = "PAID"
        elif to == "FAILED" and t.status in ("SENT", "PAID"):  # PAID -> CANCELLED is a late bank return (seen in the sandbox)
            t.status, t.failure_type = "CANCELLED", failure_type or "simulation"  # failed transfers end CANCELLED
        else:
            raise ValueError(f"invalid transition {t.status} -> {to}")
        return t


FAILURE_CODES = {  # Airwallex failure.code -> the simulator failure_type that produced it (sandbox always reports type INCORRECT_ROUTING)
    "90802": "BENEFICIARY_BANK_RETURNED", "91402": "CHANNEL_TIMEOUT", "91401": "SYSTEM_ERROR", "99901": "UNABLE_TO_APPLY",
    "91301": "DUPLICATION_RETURN", "91001": "RECALL_REQUESTED", "99902": "OTHER", "90701": "ACCOUNT_CLOSED",
    "91002": "CLIENT_REQUESTED", "90101": "INVALID_ACCOUNT_NAME_OR_NUMBER",
}
DEFAULT_API_VERSION = "2026-08-21"  # accepted by the sandbox; pinned so response shapes do not drift under us
log = logging.getLogger("payops.airwallex")
_SECRET = re.compile(r"(Bearer\s+[\w.\-]+|x-api-key['\"]?\s*[:=]\s*['\"]?[\w\-]+|\b[A-Z]{2}\d{2}[A-Z0-9]{12,30}\b)", re.I)


def redact(text: str) -> str:
    """Strip bearer tokens, API keys and IBAN-looking strings before anything is logged or raised."""
    return _SECRET.sub("[REDACTED]", text)


class RateLimiter:
    """Sliding 1-second window: 15 requests/s overall, 8/s per path (Airwallex's documented limits)."""
    def __init__(self, global_rps: int = 15, path_rps: int = 8, now=time.monotonic, sleep=time.sleep) -> None:
        self.global_rps, self.path_rps, self.now, self.sleep = global_rps, path_rps, now, sleep
        self._all: List[float] = []
        self._by: Dict[str, List[float]] = {}
        self._lock = threading.Lock()

    def acquire(self, path: str) -> None:
        while True:
            with self._lock:
                t = self.now()
                self._all = [x for x in self._all if t - x < 1.0]
                mine = self._by.setdefault(path, [])
                mine[:] = [x for x in mine if t - x < 1.0]
                if len(self._all) < self.global_rps and len(mine) < self.path_rps:
                    self._all.append(t)
                    mine.append(t)
                    return
                wait = 1.0 - (t - min(self._all[0] if len(self._all) >= self.global_rps else t, mine[0] if len(mine) >= self.path_rps else t))
            self.sleep(max(wait, 0.01))


class AirwallexREST:
    """Verified against the live sandbox (Oct 2026): login, balances, beneficiaries, transfers, validate, simulation.

    Reliability: single-flight token refresh 60s before expiry; one re-login + retry on 401; exponential backoff with jitter
    on 429 / 5xx / network errors, always re-sending the SAME body (so the same request_id); rate limiting; amounts parsed
    losslessly as Decimal; every error is a typed AirwallexError carrying the machine-readable code and a trace id, with
    secrets redacted. Simulation POSTs are never retried (the sandbox answers 500 operation_failed for invalid transitions)."""

    def __init__(self, client_id: str, api_key: str, api_version: Optional[str] = DEFAULT_API_VERSION, base: str = BASE,
                 opener=None, sleep=time.sleep, now=time.time, rand=random.random, max_attempts: int = 5,
                 limiter: Optional[RateLimiter] = None) -> None:
        self.client_id, self.api_key, self.api_version, self.base = client_id, api_key, api_version, base.rstrip("/")
        self._open = opener or (lambda req, timeout: urllib.request.urlopen(req, timeout=timeout))
        self._sleep, self._now, self._rand, self.max_attempts = sleep, now, rand, max_attempts
        self.limiter = limiter or RateLimiter()
        self._token, self._exp = "", 0.0
        self._login_lock = threading.Lock()

    # ---- transport ---------------------------------------------------------------------------
    def _ensure_token(self, force: bool = False) -> str:
        with self._login_lock:  # single-flight: concurrent callers share one login
            if force or not self._token or self._now() >= self._exp - 60:
                r = self._send("POST", "/api/v1/authentication/login", None, None, use_key=True, retry=True)
                self._token = r["token"]
                try:
                    self._exp = datetime.strptime(r["expires_at"], "%Y-%m-%dT%H:%M:%S%z").timestamp()
                except (KeyError, ValueError):
                    self._exp = self._now() + 25 * 60
            return self._token

    def _call(self, method: str, path: str, body: Optional[dict] = None, tenant: Optional[str] = None, retry: bool = True):
        return self._send(method, path, body, tenant, use_key=False, retry=retry)

    def _send(self, method: str, path: str, body: Optional[dict], tenant: Optional[str], use_key: bool, retry: bool):
        refreshed = False
        for attempt in range(self.max_attempts if retry else 1):
            h = {"Content-Type": "application/json"}
            if use_key:
                h.update({"x-client-id": self.client_id, "x-api-key": self.api_key})
            else:
                h["Authorization"] = "Bearer " + self._ensure_token()
                if self.api_version:
                    h["x-api-version"] = self.api_version
            if tenant and tenant != "self":  # "self" = our own account, no x-on-behalf-of
                h["x-on-behalf-of"] = tenant
            self.limiter.acquire(path.split("?")[0])
            req = urllib.request.Request(self.base + path, json.dumps(body).encode() if body is not None else None, h, method=method)
            try:
                with self._open(req, 30) as r:
                    raw = r.read()
                return json.loads(raw, parse_float=Decimal) if raw else {}
            except urllib.error.HTTPError as e:
                try:
                    d = json.loads(e.read() or b"{}")
                except ValueError:
                    d = {}
                err = AirwallexError(e.code, str(d.get("code", "http_error")), redact(str(d.get("message", ""))[:300]),
                                     trace_id=e.headers.get("x-awx-traceid") if e.headers else None)
                if e.code == 401 and not use_key and not refreshed:
                    refreshed = True
                    self._ensure_token(force=True)
                    continue
                if retry and (e.code == 429 or e.code >= 500) and attempt < self.max_attempts - 1 and err.code != "operation_failed":
                    log.warning("retrying %s %s after %s (attempt %d)", method, path, e.code, attempt + 1)
                    self._backoff(attempt)
                    continue
                raise err from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:  # network failure: outcome unknown
                if retry and attempt < self.max_attempts - 1:
                    log.warning("retrying %s %s after network error (attempt %d)", method, path, attempt + 1)
                    self._backoff(attempt)
                    continue
                raise AirwallexError(0, "network_error", redact(repr(e))[:200]) from None
        raise AirwallexError(0, "network_error", "exhausted retries")

    def _backoff(self, attempt: int) -> None:
        self._sleep(min(0.5 * (2 ** attempt), 8.0) * (0.5 + self._rand() / 2))  # exponential with jitter

    # ---- mapping -----------------------------------------------------------------------------
    @staticmethod
    def _status(s: str) -> str:
        return "CANCELLED" if s in ("FAILED", "CANCELLED") else s  # failed transfers end CANCELLED

    def _rec(self, d: dict, ob_id: str, tenant: str, detail: bool = False) -> TransferRec:
        status, ft = self._status(d["status"]), None
        if status == "CANCELLED":
            if not detail:  # the list endpoint omits failure info; the detail endpoint has failure.code
                d = self._call("GET", f"/api/v1/transfers/{d['id']}", tenant=tenant)
            code = str((d.get("failure") or {}).get("code") or "")
            ft = FAILURE_CODES.get(code, f"CODE_{code}" if code else None)
        return TransferRec(d["id"], ob_id or d.get("reference", ""), tenant, d.get("request_id", ""), status, ft,
                           D(d.get("source_amount") or 0),
                           None if d.get("fee_amount") is None else D(d["fee_amount"]), d.get("swift_charge_option"))

    # ---- money endpoints ---------------------------------------------------------------------
    def balance(self, tenant: str, currency: str) -> Decimal:
        for b in self._call("GET", "/api/v1/balances/current", tenant=tenant):
            if b.get("currency") == currency:
                return D(b.get("available_amount", 0))
        return Decimal(0)

    def _transfer_body(self, ob: Obligation, request_id: str) -> dict:
        body = {"request_id": request_id, "beneficiary_id": ob.beneficiary_id,
                "source_currency": ob.currency, "source_amount": str(ob.amount), "transfer_currency": ob.currency,
                "transfer_method": ob.method, "reason": "payment_to_supplier", "reference": ob.id, "fee_paid_by": "PAYER"}
        if ob.method == "SWIFT":
            body["swift_charge_option"] = ob.swift_charge_option  # the supplier must receive the invoiced amount in full
        return body

    def validate_transfer(self, ob: Obligation, request_id: str):
        """Free dry run (POST /transfers/validate). A failed SWIFT payout still costs a fee, so check before paying."""
        try:
            self._call("POST", "/api/v1/transfers/validate", body=self._transfer_body(ob, request_id), tenant=ob.tenant)
            return True, ""
        except AirwallexError as e:
            if e.code == "validation_failed":
                return False, e.message
            raise

    def create_transfer(self, ob: Obligation, request_id: str) -> TransferRec:
        try:
            d = self._call("POST", "/api/v1/transfers/create", tenant=ob.tenant, body=self._transfer_body(ob, request_id))
        except AirwallexError as e:
            # AIRWALLEX: a retry with a used request_id is rejected, not replayed. Verified live: a retry with a used request_id is NOT idempotent-return; Airwallex answers 400
            # duplicate_request_id (even if the payload differs). Treat it as "already created": fetch that transfer.
            if e.code != "duplicate_request_id":
                raise
            found = self.find_by_request_id(ob.tenant, request_id)
            if found is None:
                raise
            if abs(found.amount - ob.amount) > Decimal("0.005"):  # same id, different payment: refuse to pretend it is ours
                raise AirwallexError(409, "request_id_conflict", f"request_id reused with a different amount ({found.amount} vs {ob.amount})") from None
            return found
        if ob.observed_fee is None and d.get("fee_amount") is not None:
            ob.observed_fee = D(d["fee_amount"])  # use the real fee for budgeting from now on
        return self._rec(d, ob.id, ob.tenant)

    def _all(self, tenant: str) -> List[dict]:
        items = self._call("GET", "/api/v1/transfers?page_size=100", tenant=tenant).get("items", [])
        return sorted(items, key=lambda i: i.get("created_at", ""))  # original first

    def list_transfers(self, tenant: str, obligation_id: str) -> List[TransferRec]:
        return [self._rec(i, obligation_id, tenant) for i in self._all(tenant) if i.get("reference") == obligation_id]

    def find_by_request_id(self, tenant: str, request_id: str) -> Optional[TransferRec]:
        for i in self._all(tenant):  # Airwallex upper-cases request_id
            if i.get("request_id", "").lower() == request_id.lower():
                return self._rec(i, i.get("reference", ""), tenant)
        return None

    def transition(self, tenant: str, transfer_id: str, to: str, failure_type: Optional[str] = None) -> TransferRec:
        body = {"next_status": to}
        if failure_type:
            body["failure_type"] = failure_type
        # SANDBOX: the simulation endpoints do not exist in production. PROD: wait for payout.transfer.* webhooks instead.
        # AIRWALLEX: SENT must come before PAID/FAILED; a bad transition answers 500 operation_failed, so it is never retried.
        d = self._call("POST", f"/api/v1/simulation/transfers/{transfer_id}/transition", body=body, tenant=tenant, retry=False)
        return self._rec(d, d.get("reference", ""), tenant)

    # ---- setup helpers (used by bootstrap.py) -------------------------------------------------
    def list_global_accounts(self) -> List[dict]:
        return self._call("GET", "/api/v1/global_accounts").get("items", [])

    def create_global_account(self, country_code: str, currency: str, request_id: str) -> dict:
        return self._call("POST", "/api/v1/global_accounts/create", body={
            "request_id": request_id, "country_code": country_code,
            "required_features": [{"currency": currency, "transfer_method": "LOCAL"}]})

    def simulate_deposit(self, global_account_id: str, amount, payer_name: str = "Seed funding") -> dict:
        # SANDBOX: simulated inbound deposit; there is no top-up button and no such endpoint in production. PROD: real funding.
        return self._call("POST", "/api/v1/simulation/deposit/create", retry=False, body={
            "global_account_id": global_account_id, "amount": float(amount), "payer_name": payer_name})

    def list_beneficiaries(self) -> List[dict]:
        return self._call("GET", "/api/v1/beneficiaries?page_size=100").get("items", [])

    # fields the schema marks required that Airwallex derives itself or names differently in the create call (verified live)
    SCHEMA_ALIASES = {"bank_name": "derived from swift_code/iban", "payment_methods": "sent as transfer_methods"}

    def beneficiary_required_fields(self, country: str, currency: str, entity_type: str = "COMPANY", method: str = "SWIFT") -> List[str]:
        """POST /beneficiaries/schema -> the leaf fields that are required and relevant for this corridor."""
        d = self._call("POST", "/api/v1/beneficiaries/schema", body={"bank_country_code": country, "account_currency": currency,
                                                                       "entity_type": entity_type, "transfer_method": method})["schema"]
        return sorted(k.replace("create_beneficiary_request.", "") for k, v in d.items()
                      if v.get("required") and v.get("relevant") and v.get("type") != "object")

    @staticmethod
    def missing_fields(payload: dict, required: List[str], aliases: Optional[dict] = None) -> List[str]:
        """Required schema paths absent from a create payload (aliases are not counted as missing)."""
        flat = {}
        def walk(prefix, node):
            for k, v in node.items():
                path = f"{prefix}.{k}" if prefix else k
                if isinstance(v, dict):
                    walk(path, v)
                else:
                    flat[path] = v
        walk("", {k: v for k, v in payload.items() if k not in ("request_id", "nickname")})
        aliases = aliases or {}
        return [r for r in required if r.split(".")[-1] not in aliases and r not in aliases and not flat.get(r)]

    def create_beneficiary(self, nickname: str, request_id: str, company_name: str = "Acme Supplies GmbH",
                           iban: str = "DE89370400440532013000", swift_code: str = "COBADEFF", country: str = "DE",
                           currency: str = "EUR", method: str = "SWIFT") -> dict:
        """Company beneficiary (field set verified against the live sandbox). The payload is checked against the corridor's
        schema first, so a missing field is reported by name instead of as an opaque validation_failed."""
        body = self._beneficiary_body(nickname, request_id, company_name, iban, swift_code, country, currency, method)
        missing = self.missing_fields(body, self.beneficiary_required_fields(country, currency, "COMPANY", method), self.SCHEMA_ALIASES)
        if missing:
            raise AirwallexError(400, "beneficiary_schema_missing", f"required by the {country}/{currency}/{method} schema: {', '.join(missing)}")
        return self._call("POST", "/api/v1/beneficiaries/create", body=body)

    @staticmethod
    def _beneficiary_body(nickname, request_id, company_name, iban, swift_code, country, currency, method) -> dict:
        return {
            "request_id": request_id, "nickname": nickname, "transfer_methods": [method],
            "beneficiary": {"entity_type": "COMPANY", "company_name": company_name,
                            "bank_details": {"account_currency": currency, "account_name": company_name,
                                             "account_number": iban, "iban": iban, "swift_code": swift_code,
                                             "bank_country_code": country},
                            "address": {"country_code": country, "city": "Berlin", "street_address": "1 Test Strasse",
                                        "postcode": "10115"}}}
