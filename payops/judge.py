"""Second opinion on the emission path: a policy-reasoning model (gpt-oss-safeguard) reviews each permitted action.

The judge can only RESTRICT. The code gate stays the authority: judge verdicts can turn a permit into a deny/escalation,
never a deny into a permit. If the judge is unavailable or returns garbage it abstains (the code gate still holds).
"""
from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass
from typing import List, Optional

from . import policy as P
from .models import Action, Obligation, TransferRec

SYSTEM = """You review a proposed payment-ops action against a written policy. Reply JSON only:
{"violates": true|false, "clause": "<clause id or null>", "reason": "<one sentence>"}.
Flag a violation only if the action clearly breaks a clause given ALL the facts provided (including request_id, wallet_balance, request_id_previously_used). If a fact needed for a clause is absent, do not flag. Policy:
"""


@dataclass
class JudgeVerdict:
    violates: bool
    clause: Optional[str] = None
    reason: str = ""
    abstained: bool = False


class NullJudge:
    def review(self, pol, action, ob, transfers, claims_summary="", extra=None) -> JudgeVerdict:
        return JudgeVerdict(False, abstained=True, reason="no judge configured")


class SafeguardJudge:
    def __init__(self, model: str = "") -> None:
        self.model = model or os.environ.get("PAYOPS_JUDGE_MODEL", "openai.gpt-oss-safeguard-120b")

    def review(self, pol: P.Policy, action: Action, ob: Obligation, transfers: List[TransferRec], claims_summary: str = "",
               extra: Optional[dict] = None) -> JudgeVerdict:
        facts = {"action": action.kind, "funding_tenant": action.funding_tenant, "obligation_tenant": ob.tenant,
                 "amount": ob.amount, "fee": ob.fee, "method": ob.method, "claims": claims_summary, "request_id": action.request_id, **(extra or {}),
                 "transfers": [{"id": t.id, "status": t.status, "failure_type": t.failure_type} for t in transfers]}
        body = {"model": self.model, "max_tokens": 1500, "messages": [
            {"role": "system", "content": SYSTEM + pol.text()}, {"role": "user", "content": json.dumps(facts, default=str)}]}
        try:
            req = urllib.request.Request(os.environ["OPENAI_BASE_URL"].rstrip("/") + "/chat/completions", json.dumps(body).encode(),
                                         {"content-type": "application/json", "authorization": "Bearer " + os.environ["OPENAI_API_KEY"]})
            with urllib.request.urlopen(req, timeout=90) as r:
                text = json.loads(r.read())["choices"][0]["message"]["content"] or ""
            d = json.loads(text[text.index("{"): text.rindex("}") + 1])
            return JudgeVerdict(bool(d.get("violates")), d.get("clause"), str(d.get("reason", "")))
        except Exception as e:  # abstain, never block or permit on a judge failure
            return JudgeVerdict(False, abstained=True, reason=f"judge unavailable: {type(e).__name__}")
