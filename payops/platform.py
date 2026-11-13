"""Connected accounts (platform) lifecycle: create -> fill gaps -> submit -> activate -> fund -> beneficiary.

Verified live (Oct 2026) with these findings, all easy to trip over:
  * `account_details.business_details.account_usage.product_reference` is required to submit (and `estimated_monthly_revenue`
    is the business-account volume field, `expected_monthly_transaction_volume` belongs to individuals)
  * the business person needs `identifications.primary` (type, issuing country, number) or submit fails with the unhelpful
    `field_required: ensure is not null`
  * fields lock after submit, so everything is sent before it
  * activation is a sandbox simulation; wait ~2s after submit
Tenants are identified by a nickname we store in `business_name`, so ensure_tenant() is idempotent.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Optional

from .airwallex import AirwallexError
from .models import Obligation
from .bootstrap import Setup, ensure_setup

PRODUCT_REFERENCE = ["MAKE_TRANSFERS", "RECEIVE_TRANSFERS", "CONVERT_FUNDS", "USE_AWX_API"]


@dataclass
class Tenant:
    account_id: str
    name: str
    setup: Setup


def account_body(name: str, email: str) -> dict:
    addr = {"address_line1": "1 Market Street", "suburb": "San Francisco", "state": "CA", "postcode": "94105", "country_code": "US"}
    return {
        "request_id": str(uuid.uuid4()),
        "primary_contact": {"email": email},
        "account_details": {
            "legal_entity_type": "BUSINESS",
            "business_details": {
                "business_name": name, "business_structure": "LIMITED_LIABILITY_COMPANY", "business_start_date": "2020-01-15",
                "operating_country": ["US"], "url": "https://example.com",
                "business_identifiers": [{"country_code": "US", "number": "123456789", "type": "EIN"}],  # needed for on-behalf-of transfers
                "registration_address": addr, "industry_category_code": "ICCV3_0813XX",
                "description_of_goods_or_services": "Wholesale supply of office goods.",
                "account_usage": {"product_reference": PRODUCT_REFERENCE, "estimated_monthly_revenue": {"currency": "USD", "amount": "100000"}},
            },
            "business_person_details": [{  # must be in the create request; adding one later does not persist
                "first_name": "Jane", "last_name": "Doe", "date_of_birth": "1985-06-30", "nationality": "US", "job_title": "CEO",
                "roles": ["DIRECTOR", "CONTROLLING_PERSON"], "residential_address": addr,
                "identifications": {"primary": {"identification_type": "PASSPORT", "issuing_country_code": "US", "identification_number": "X1234567"}},
            }],
        },
        "customer_agreements": {"agreed_to_terms_and_conditions": True, "agreed_to_data_usage": True},
    }


def find_account(aw, name: str) -> Optional[dict]:
    for a in aw.list_accounts():
        if (a.get("account_details", {}).get("business_details", {}) or {}).get("business_name") == name:
            return a
    return None


def ensure_account(aw, name: str, sleep=time.sleep, with_fresh: bool = False):
    """Create, submit and activate a connected account named `name` (or reuse it). Returns the acct_ id, or (id, fresh) when
    with_fresh: fresh=True means it was not ACTIVE before this call, so its fee configuration may still be propagating."""
    acct = find_account(aw, name)
    if acct is None:
        acct = aw.create_account(account_body(name, f"ops+{uuid.uuid4().hex[:8]}@example.com"))
    aid, status = acct["id"], acct.get("status")
    fresh = status != "ACTIVE"
    if status == "CREATED":
        aw.submit_account(aid)
        sleep(2.5)
        status = "SUBMITTED"
    if status != "ACTIVE":
        try:
            aw.activate_account(aid)
        except AirwallexError:
            sleep(2.5)
            aw.activate_account(aid)
        for _ in range(10):
            if aw.get_account(aid).get("status") == "ACTIVE":
                break
            sleep(1)
        else:
            raise AirwallexError(0, "activation_timeout", f"{aid} did not become ACTIVE")
    return (aid, fresh) if with_fresh else aid


def wait_until_ready(aw, tenant: str, beneficiary_id: str, timeout: float = 600, sleep=time.sleep, now=time.time) -> float:
    """A freshly activated account answers `fee_unknown` to transfer creation for a few minutes (fee configuration propagates).
    Verified live: the free /transfers/validate passes during that window, so it is NOT a usable readiness check. Only a real
    create tells. We send one small SWIFT transfer (a sandbox fee, tagged `readiness-probe`) until it is accepted; any other
    error is real and raised. Returns the seconds waited."""
    probe = Obligation("readiness-probe", tenant, 5, "EUR", "SWIFT", beneficiary_id)
    rid = "po-ready-" + uuid.uuid4().hex[:12]  # one id for every retry of this probe: it can never create two transfers
    start = now()
    while True:
        try:
            aw.create_transfer(probe, rid)
            return now() - start
        except AirwallexError as e:
            if e.code != "fee_unknown" or now() - start > timeout:
                raise
        sleep(15)


def ensure_tenant(aw, name: str, deposit: float, min_balance: float = 0.0, sleep=time.sleep) -> Tenant:
    """Account + EUR global account + (simulated) funding + SWIFT beneficiary, all on behalf of the tenant. Idempotent."""
    aid, fresh = ensure_account(aw, name, sleep, with_fresh=True)
    setup = ensure_setup(aw, min_balance=min_balance, deposit=deposit, tenant=aid, nickname=f"supplier of {name}")
    if fresh:  # only a just-activated account needs the wait (and its probe fee); a reused one is already configured
        wait_until_ready(aw, aid, setup.beneficiary_id, sleep=sleep)
    return Tenant(aid, name, setup)
