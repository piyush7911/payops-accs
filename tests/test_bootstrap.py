from payops.airwallex import AirwallexREST
from payops.bootstrap import ensure_setup


class Fake(AirwallexREST):
    """AirwallexREST with the network replaced by a tiny in-memory Airwallex."""
    def __init__(self, accounts=None, bens=None, bal=0.0):
        super().__init__("id", "key")
        self.accounts, self.bens, self.bal, self.calls = accounts or [], bens or [], bal, []

    def list_global_accounts(self, tenant=None): return self.accounts
    def create_global_account(self, country, currency, rid, tenant=None):
        a = {"id": "ga1", "status": "ACTIVE", "required_features": [{"currency": currency}]}
        self.accounts.append(a); self.calls.append("create_ga"); return a
    def balance(self, tenant, currency): return self.bal
    def simulate_deposit(self, gid, amount, payer="x", tenant=None): self.bal += amount; self.calls.append("deposit"); return {}
    def list_beneficiaries(self, tenant=None): return self.bens
    def create_beneficiary(self, nickname, rid, tenant=None, **k):
        b = {"id": "b1", "nickname": nickname}; self.bens.append(b); self.calls.append("create_ben"); return b


def test_fresh_account_creates_everything_once():
    f = Fake()
    s = ensure_setup(f)
    assert f.calls == ["create_ga", "deposit", "create_ben"] and s.beneficiary_id == "b1" and s.balance >= 5000
    f.calls.clear()
    ensure_setup(f)  # second run reuses everything
    assert f.calls == []


def test_existing_funded_account_is_left_alone():
    f = Fake([{"id": "g", "status": "ACTIVE", "required_features": [{"currency": "EUR"}]}], [{"id": "b9", "nickname": "payops-accs supplier"}], 10_000_000)
    s = ensure_setup(f)
    assert f.calls == [] and s.global_account_id == "g" and s.beneficiary_id == "b9"


def test_processing_account_is_not_reused():
    f = Fake([{"id": "g", "status": "PROCESSING", "required_features": [{"currency": "EUR"}]}], None, 10_000_000)
    ensure_setup(f)
    assert "create_ga" in f.calls


# ---- request_id semantics verified live: a reused id yields 400 duplicate_request_id, not the original transfer ----
import pytest

from payops.airwallex import AirwallexError
from payops.models import Obligation

DUP = AirwallexError(400, "duplicate_request_id", "The request ID X has been used for a transfer with the transfer ID t1 before.")


class RestWith(AirwallexREST):
    def __init__(self, existing_amount=100.0, raise_other=None):
        super().__init__("i", "k")
        self.existing_amount, self.raise_other = existing_amount, raise_other

    def _call(self, method, path, body=None, tenant=None, auth=True):
        if path == "/api/v1/transfers/create":
            raise self.raise_other or DUP
        if path.startswith("/api/v1/transfers?"):
            return {"items": [{"id": "t1", "status": "SCHEDULED", "request_id": "ABC-1", "reference": "o1",
                               "source_amount": self.existing_amount, "created_at": "2026-10-15T00:00:00"}]}
        raise AssertionError(path)


OB = Obligation("o1", "self", 100.0, "EUR", "SWIFT", "b")


def test_duplicate_request_id_returns_the_existing_transfer():
    t = RestWith().create_transfer(OB, "abc-1")  # Airwallex upper-cases ids; match is case-insensitive
    assert t.id == "t1"


def test_duplicate_request_id_with_different_amount_is_a_conflict_not_silently_accepted():
    with pytest.raises(AirwallexError) as e:
        RestWith(existing_amount=250.0).create_transfer(OB, "abc-1")
    assert e.value.code == "request_id_conflict"


def test_other_airwallex_errors_propagate():
    with pytest.raises(AirwallexError) as e:
        RestWith(raise_other=AirwallexError(400, "validation_failed", "bad")).create_transfer(OB, "abc-1")
    assert e.value.code == "validation_failed"


# ---- connected accounts lifecycle -----------------------------------------------------------------
from payops import platform


class FakePlatform(Fake):
    """Adds the account endpoints. Accounts are kept in a dict keyed by id."""
    def __init__(self):
        super().__init__(bal=10_000)
        self.accts, self.log = {}, []

    def list_accounts(self): return list(self.accts.values())
    def create_account(self, body):
        a = {"id": f"acct_{len(self.accts)}", "status": "CREATED", "account_details": body["account_details"]}
        self.accts[a["id"]] = a; self.log.append("create"); return a
    def submit_account(self, aid): self.accts[aid]["status"] = "SUBMITTED"; self.log.append("submit"); return {}
    def activate_account(self, aid): self.accts[aid]["status"] = "ACTIVE"; self.log.append("activate"); return {}
    def get_account(self, aid): return self.accts[aid]
    fee_unknown_polls = 0
    def create_transfer(self, ob, rid):
        if self.fee_unknown_polls > 0:
            self.fee_unknown_polls -= 1
            raise AirwallexError(400, "fee_unknown", "fee configuration")
        return None


def test_account_body_has_what_the_sandbox_demanded():
    b = platform.account_body("X LLC", "a@b.co")["account_details"]
    assert b["business_details"]["business_identifiers"][0]["type"] == "EIN"  # needed for on-behalf-of transfers
    assert b["business_details"]["account_usage"]["product_reference"] and "estimated_monthly_revenue" in b["business_details"]["account_usage"]
    assert b["business_person_details"][0]["identifications"]["primary"]["identification_type"]  # else submit: "ensure is not null"


def test_tenant_lifecycle_is_idempotent():
    f = FakePlatform()
    t1 = platform.ensure_tenant(f, "T LLC", deposit=1000, sleep=lambda x: None)
    assert f.log == ["create", "submit", "activate"] and f.accts[t1.account_id]["status"] == "ACTIVE"
    f.log.clear()
    t2 = platform.ensure_tenant(f, "T LLC", deposit=1000, sleep=lambda x: None)
    assert f.log == [] and t2.account_id == t1.account_id


def test_new_tenant_waits_for_fee_configuration_before_being_used():
    f = FakePlatform(); f.fee_unknown_polls = 3
    sleeps = []
    platform.ensure_tenant(f, "T LLC", deposit=1000, sleep=sleeps.append)
    assert sleeps.count(15) == 3 and f.fee_unknown_polls == 0


def test_other_errors_are_not_swallowed_by_the_readiness_wait():
    f = FakePlatform()
    f.create_transfer = lambda ob, rid: (_ for _ in ()).throw(AirwallexError(403, "forbidden", "x"))
    with pytest.raises(AirwallexError) as e:
        platform.ensure_tenant(f, "T LLC", deposit=1000, sleep=lambda x: None)
    assert e.value.code == "forbidden"


def test_readiness_probe_only_for_freshly_activated_accounts():
    f = FakePlatform(); probes = []
    f.create_transfer = lambda ob, rid: probes.append(rid)
    platform.ensure_tenant(f, "T LLC", deposit=1000, sleep=lambda x: None)
    assert len(probes) == 1  # new account: one probe (a sandbox fee)
    platform.ensure_tenant(f, "T LLC", deposit=1000, sleep=lambda x: None)
    assert len(probes) == 1  # reused account: no probe, no fee
