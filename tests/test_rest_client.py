"""AirwallexREST reliability, with the network replaced by a scripted opener (no sockets)."""
import io
import json
import urllib.error

import pytest

from payops.airwallex import AirwallexError, AirwallexREST, RateLimiter, redact
from payops.models import Obligation


class Resp:
    def __init__(self, body): self.body = json.dumps(body).encode()
    def read(self): return self.body
    def __enter__(self): return self
    def __exit__(self, *a): return False


def http_error(code, body, headers=None):
    return urllib.error.HTTPError("u", code, "m", headers or {}, io.BytesIO(json.dumps(body).encode()))


class Script:
    """Pops one scripted outcome per request; records (method, path, headers, body)."""
    def __init__(self, *steps): self.steps, self.calls = list(steps), []

    def __call__(self, req, timeout):
        self.calls.append((req.get_method(), req.full_url.split(".com")[1], dict(req.header_items()), req.data))
        step = self.steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        return Resp(step)


LOGIN = {"token": "tok-1", "expires_at": "2099-01-01T00:00:00+0000"}


def client(*steps, **kw):
    s = Script(*steps)
    c = AirwallexREST("id", "key", opener=s, sleep=lambda x: None, rand=lambda: 0.0, limiter=RateLimiter(1000, 1000), **kw)
    return c, s


OB = Obligation("o1", "self", 100, "EUR", "SWIFT", "b")


def test_token_is_cached_and_versioned_header_sent():
    c, s = client(LOGIN, {"items": []}, {"items": []})
    c._call("GET", "/api/v1/transfers?page_size=1"); c._call("GET", "/api/v1/transfers?page_size=1")
    assert [x[1] for x in s.calls].count("/api/v1/authentication/login") == 1  # one login for two calls
    h = {k.lower(): v for k, v in s.calls[1][2].items()}
    assert h["authorization"] == "Bearer tok-1" and h["x-api-version"] == "2026-08-21"


def test_401_triggers_exactly_one_relogin_and_retry():
    c, s = client(LOGIN, http_error(401, {"code": "unauthorized"}), {"token": "tok-2", "expires_at": "2099-01-01T00:00:00+0000"}, {"ok": 1})
    assert c._call("GET", "/api/v1/x") == {"ok": 1}
    assert [x[1] for x in s.calls].count("/api/v1/authentication/login") == 2


def test_5xx_and_429_are_retried_with_the_same_body_so_the_same_request_id():
    c, s = client(LOGIN, http_error(429, {"code": "rate"}), http_error(503, {"code": "down"}), {"id": "t1", "status": "SCHEDULED", "source_amount": 100})
    c.create_transfer(OB, "po-abc1234567-r1")
    creates = [x for x in s.calls if x[1] == "/api/v1/transfers/create"]
    assert len(creates) == 3 and len({x[3] for x in creates}) == 1  # identical payload each time
    assert json.loads(creates[0][3])["request_id"] == "po-abc1234567-r1"


def test_network_error_is_retried_then_surfaces_as_typed_error():
    c, s = client(LOGIN, *[urllib.error.URLError("boom")] * 5)
    with pytest.raises(AirwallexError) as e:
        c._call("GET", "/api/v1/x")
    assert e.value.code == "network_error"


def test_4xx_is_not_retried_and_carries_code_and_trace():
    c, s = client(LOGIN, http_error(400, {"code": "validation_failed", "message": "bad"}, {"x-awx-traceid": "tr-9"}))
    with pytest.raises(AirwallexError) as e:
        c._call("POST", "/api/v1/transfers/create", {})
    assert e.value.code == "validation_failed" and e.value.trace_id == "tr-9" and len(s.calls) == 2  # login + 1 try


def test_simulation_transitions_are_never_retried():
    c, s = client(LOGIN, http_error(500, {"code": "operation_failed"}))
    with pytest.raises(AirwallexError):
        c.transition("self", "t1", "PAID")
    assert len(s.calls) == 2


def test_swift_charge_option_and_exact_amount_in_the_payload():
    c, s = client(LOGIN, {"id": "t1", "status": "SCHEDULED", "source_amount": "100.10", "fee_amount": "22.29"})
    ob = Obligation("o1", "self", "100.10", "EUR", "SWIFT", "b")
    c.create_transfer(ob, "po-abc1234567-r1")
    body = json.loads(s.calls[1][3])
    assert body["swift_charge_option"] == "PAYER" and body["source_amount"] == "100.10" and body["fee_paid_by"] == "PAYER"
    assert str(ob.observed_fee) == "22.29"  # real fee captured for budgeting


def test_validate_transfer_ok_and_failure():
    c, s = client(LOGIN, "OK", http_error(400, {"code": "validation_failed", "message": "beneficiary_id 045"}))
    assert c.validate_transfer(OB, "po-abc1234567-r1") == (True, "")
    ok, why = c.validate_transfer(OB, "po-abc1234567-r2")
    assert not ok and "045" in why


def test_rate_limiter_blocks_the_ninth_call_in_a_second_per_path():
    t = [0.0]; sleeps = []
    def sleep(x): sleeps.append(x); t[0] += x
    rl = RateLimiter(15, 8, now=lambda: t[0], sleep=sleep)
    for _ in range(8): rl.acquire("/p")
    assert not sleeps
    rl.acquire("/p")
    assert sleeps  # the 9th call within the window had to wait


def test_secrets_are_redacted():
    assert "tok" not in redact("Authorization: Bearer tok.abc-123 failed") and "[REDACTED]" in redact("pay DE89370400440532013000 now")


# ---- beneficiary schema check (POST /beneficiaries/schema) ----------------------------------------
SCHEMA = {"schema": {
    "create_beneficiary_request": {"required": True, "relevant": True, "type": "object"},
    "create_beneficiary_request.beneficiary.address.city": {"required": True, "relevant": True, "type": "string"},
    "create_beneficiary_request.beneficiary.bank_details.bank_name": {"required": True, "relevant": True, "type": "string"},
    "create_beneficiary_request.beneficiary.bank_details.iban": {"required": True, "relevant": True, "type": "string"},
    "create_beneficiary_request.payment_methods": {"required": True, "relevant": True, "type": "array"},
    "create_beneficiary_request.beneficiary.additional_info.x": {"required": False, "relevant": True, "type": "string"}}}


def test_required_fields_come_from_the_schema_endpoint():
    c, s = client(LOGIN, SCHEMA)
    req = c.beneficiary_required_fields("DE", "EUR")
    assert "beneficiary.address.city" in req and "payment_methods" in req and "beneficiary.additional_info.x" not in req
    assert json.loads(s.calls[1][3]) == {"bank_country_code": "DE", "account_currency": "EUR", "entity_type": "COMPANY", "transfer_method": "SWIFT"}


def test_missing_fields_ignores_aliases_and_reports_real_gaps():
    body = AirwallexREST._beneficiary_body("n", "r", "Acme", "DE89370400440532013000", "COBADEFF", "DE", "EUR", "SWIFT")
    req = ["beneficiary.address.city", "beneficiary.bank_details.bank_name", "beneficiary.bank_details.iban", "payment_methods"]
    assert AirwallexREST.missing_fields(body, req, AirwallexREST.SCHEMA_ALIASES) == []  # bank_name/payment_methods are aliases
    del body["beneficiary"]["address"]["city"]
    assert AirwallexREST.missing_fields(body, req, AirwallexREST.SCHEMA_ALIASES) == ["beneficiary.address.city"]


def test_create_beneficiary_fails_early_naming_the_missing_field():
    schema = json.loads(json.dumps(SCHEMA))
    schema["schema"]["create_beneficiary_request.beneficiary.bank_details.routing_number"] = {"required": True, "relevant": True, "type": "string"}
    c, s = client(LOGIN, schema)
    with pytest.raises(AirwallexError) as e:
        c.create_beneficiary("n", "po-abc1234567-r1")
    assert e.value.code == "beneficiary_schema_missing" and "routing_number" in e.value.message
    assert not any(x[1] == "/api/v1/beneficiaries/create" for x in s.calls)  # nothing was sent
