import json
import threading
import urllib.request

import pytest

from payops import api


@pytest.fixture(scope="module")
def server():
    srv = api.serve(0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def post(base, path, body):
    req = urllib.request.Request(base + path, json.dumps(body).encode(), {"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


OB = {"id": "ob1", "tenant": "A", "amount": 100, "method": "SWIFT"}
CANCELLED = [{"id": "t0", "status": "CANCELLED", "failure_type": "CHANNEL_TIMEOUT", "request_id": "r0", "amount": 100}]


def test_policy_check_permits_and_denies_with_clause(server):
    base = {"obligation": OB, "transfers": CANCELLED, "wallet_balance": 1000, "used_request_ids": ["r0"]}
    code, d = post(server, "/policy.check", {**base, "action": {"kind": "REPLACE", "request_id": "new-1"}})
    assert code == 200 and d["permit"] is True
    code, d = post(server, "/policy.check", {**base, "action": {"kind": "REPLACE", "request_id": "r0"}})
    assert d["permit"] is False and d["clause"] == "P3-fresh-request-id"
    code, d = post(server, "/policy.check", {**base, "wallet_balance": 5, "action": {"kind": "REPLACE", "request_id": "new-2"}})
    assert d["clause"] == "P5-fee-budget"
    code, d = post(server, "/policy.check", {**base, "action": {"kind": "REPLACE", "request_id": "n", "funding_tenant": "B"}})
    assert d["clause"] == "P4-tenant-isolation"


def test_consistency_score_returns_contract_record(server):
    code, d = post(server, "/consistency.score", {"obligation_id": "ob1", "transfers": CANCELLED,
                   "claims": [{"subject": "t0", "field": "status", "value": "CANCELLED"}]})
    assert code == 200 and {"value", "provenance", "penalty"} <= set(d) and d["value"] > 0.8
    code, d = post(server, "/consistency.score", {"transfers": CANCELLED, "claims": [{"subject": "t0", "field": "status", "value": "PAID"}]})
    assert d["gate_value"] < 0.2 and d["penalty"] > 0.3


def test_bad_input_is_a_400_not_a_crash(server):
    code, d = post(server, "/policy.check", {"obligation": OB})
    assert code == 400 and "error" in d
    assert post(server, "/nope", {})[0] == 404
