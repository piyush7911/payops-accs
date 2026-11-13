"""One-call sandbox setup so a fresh account can run the demo: global account -> simulated deposit -> SWIFT beneficiary.
Idempotent: reuses what exists. Sandbox only (the deposit is simulated)."""
from __future__ import annotations

import uuid
from dataclasses import dataclass

BENEFICIARY_NICKNAME = "payops-accs supplier"


@dataclass
class Setup:
    global_account_id: str
    beneficiary_id: str
    balance: float
    created: list


def ensure_setup(aw, currency: str = "EUR", country: str = "NL", min_balance: float = 5000.0, deposit: float = 25000.0,
                 nickname: str = BENEFICIARY_NICKNAME, tenant: str = None) -> Setup:
    """tenant=None: our own account. tenant="acct_...": that connected account (every call sends x-on-behalf-of)."""
    created = []
    accts = [a for a in aw.list_global_accounts(tenant)
             if any(f.get("currency") == currency for f in a.get("required_features", [])) and a.get("status") == "ACTIVE"]
    if accts:
        ga = accts[0]
    else:
        ga = aw.create_global_account(country, currency, str(uuid.uuid4()), tenant)
        created.append(f"global_account {ga['id']}")
    bal = aw.balance(tenant or "self", currency)
    if bal < min_balance:
        # SANDBOX: simulated deposit. PROD: this account must already be funded; never auto-fund.
        aw.simulate_deposit(ga["id"], deposit, tenant=tenant)
        created.append(f"deposit {deposit} {currency}")
        bal = aw.balance(tenant or "self", currency)
    ben = next((b for b in aw.list_beneficiaries(tenant) if b.get("nickname") == nickname), None)
    if ben is None:
        ben = aw.create_beneficiary(nickname, str(uuid.uuid4()), tenant=tenant)
        created.append(f"beneficiary {ben['id']}")
    return Setup(ga["id"], ben["id"], bal, created)
