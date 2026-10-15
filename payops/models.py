"""Core domain types. Airwallex transfer states: PROCESSING -> SENT -> PAID, or -> CANCELLED (with failure_type)."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import List, Optional

# Real Airwallex transfer statuses (verified in the sandbox and in the reference builds' verification docs):
#   SCHEDULED, IN_APPROVAL, PROCESSING, OVERDUE, SENT, PAID, FAILED, CANCELLED   (FAILED is reported as CANCELLED by list/detail)
# Fail-safe classification: anything that is not definitively dead counts as live, so a status we have never seen is
# treated as "money may be moving" and blocks a replacement rather than permitting a double payment.
DEAD = ("CANCELLED", "FAILED")
SETTLED = ("PAID",)
KNOWN_STATUSES = ("SCHEDULED", "IN_APPROVAL", "PROCESSING", "OVERDUE", "SENT", "PAID", "CANCELLED")


def is_live(status: str) -> bool:
    """Money is in flight or delivered: everything except a definitively dead transfer."""
    return status.upper() not in DEAD


def is_in_flight(status: str) -> bool:
    """Still moving (not delivered, not dead): waiting is a valid response."""
    return status.upper() not in DEAD + SETTLED


LIVE_STATUSES = tuple(x for x in KNOWN_STATUSES if is_live(x))  # kept for readability/tests
IN_FLIGHT = tuple(x for x in KNOWN_STATUSES if is_in_flight(x))
def D(x) -> Decimal:
    """Exact money. Floats are converted through str() so 0.1 stays 0.1; never do money arithmetic on floats."""
    return x if isinstance(x, Decimal) else Decimal(str(x))


SWIFT_FEE = Decimal("13.39")  # last-resort default; the real fee varies by amount and charge option (13.39 SHARED vs 22.29 PAYER observed)  # default SWIFT fee (EUR, observed live in sandbox; the guide says 12.85). Overridden by the fee Airwallex returns. LOCAL has none


@dataclass
class Obligation:
    id: str
    tenant: str  # connected account id (sent as x-on-behalf-of)
    amount: Decimal  # major units
    currency: str
    method: str  # "SWIFT" | "LOCAL"
    beneficiary_id: str
    observed_fee: Optional[Decimal] = None  # fee_amount returned by Airwallex on the first transfer
    swift_charge_option: str = "PAYER"  # PAYER: supplier receives the full amount (default SHARED lets banks deduct from it)

    def __post_init__(self) -> None:
        self.amount = D(self.amount)
        if self.observed_fee is not None:
            self.observed_fee = D(self.observed_fee)

    @property
    def fee(self) -> Decimal:
        if self.method != "SWIFT":
            return Decimal(0)
        return self.observed_fee if self.observed_fee is not None else SWIFT_FEE


@dataclass
class TransferRec:
    id: str
    obligation_id: str
    tenant: str
    request_id: str
    status: str  # PROCESSING | SENT | PAID | CANCELLED
    failure_type: Optional[str] = None
    amount: Decimal = Decimal(0)
    fee: Optional[Decimal] = None  # fee_amount Airwallex reported for this transfer
    charge_option: Optional[str] = None  # swift_charge_option it was created with

    def __post_init__(self) -> None:
        self.amount = D(self.amount)
        if self.fee is not None:
            self.fee = D(self.fee)


@dataclass
class Action:
    kind: str  # WAIT | REPLACE | ESCALATE | CLOSE
    obligation_id: str
    tenant: str
    request_id: Optional[str] = None  # required for REPLACE
    funding_tenant: Optional[str] = None  # wallet the money would come from
    reason: str = ""


@dataclass
class Claim:
    """A factual statement the LLM makes about the incident, to be checked against evidence."""
    subject: str  # transfer id
    field: str  # "status" | "failure_type"
    value: str
    source: str = "llm"  # "llm" | "supplier"


@dataclass
class Proposal:
    action: Action
    claims: List[Claim] = field(default_factory=list)
    rationale: str = ""
    reply: str = ""  # draft message to the supplier; only emitted if every claim in it is corroborated


@dataclass
class Verdict:
    permit: bool
    clause: Optional[str] = None  # policy clause responsible for a denial
    detail: str = ""
