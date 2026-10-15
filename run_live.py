"""Live run against the Airwallex sandbox with a live LLM. Needs .env (AWX_CLIENT_ID, AWX_API_KEY, OPENAI_*). Run: python run_live.py"""
import os
import sys
import time
import uuid

from payops.airwallex import AirwallexREST
from payops.commander import Commander
from payops.consistency import default_calibrator
from payops.judge import SafeguardJudge
from payops.llm import default_proposer, load_env
from payops.models import Obligation

load_env()
aw = AirwallexREST(os.environ["AWX_CLIENT_ID"], os.environ["AWX_API_KEY"])
from payops.bootstrap import ensure_setup

setup = ensure_setup(aw)  # global account + simulated deposit + SWIFT beneficiary, created only if missing
BEN = sys.argv[1] if len(sys.argv) > 1 else setup.beneficiary_id
print("setup:", setup.created or "reused existing", "| EUR balance", round(setup.balance, 2))
brain = default_proposer()
cmd = Commander(aw, brain, judge=SafeguardJudge(), calibrator=default_calibrator())
print("proposer:", type(brain).__name__, getattr(brain, "model", ""))


def show(out):
    v = out.verdict
    print(f"   [{out.decision}] gate={'PERMIT' if v.permit else 'DENY ' + str(v.clause)} score={out.score.value:.2f} | {out.note}")
    print(f"      {out.judge_note}")
    print(f"      reply[{out.reply_source}]: {out.emitted_reply}")


def scenario(label, failure_type):
    oid = f"inv-{label}-{uuid.uuid4().hex[:6]}"
    ob = Obligation(oid, "self", 250.0, "EUR", "SWIFT", BEN)
    orig = aw.create_transfer(ob, str(uuid.uuid4()))
    cmd.register(ob, orig)
    print(f"\n== {label}: {oid}, fee observed={ob.fee} EUR, wallet={aw.balance('self', 'EUR'):.2f}")
    print(" 1) supplier complains while original is in flight")
    show(cmd.handle(oid, "Supplier: our deadline passed and nothing arrived!"))
    aw.transition("self", orig.id, "SENT")
    aw.transition("self", orig.id, "FAILED", failure_type)
    time.sleep(1)
    print(f" 2) original fails ({failure_type})")
    show(cmd.handle(oid, "Supplier: still nothing, please resend today"))
    ts = aw.list_transfers("self", oid)
    if len(ts) > 1:
        print(" 3) supplier pings again; duplicate must be blocked")
        show(cmd.handle(oid, "Supplier: hello??"))
        aw.transition("self", ts[1].id, "SENT")
        aw.transition("self", ts[1].id, "PAID")
        time.sleep(1)
    print("  close report:", cmd.close_report(oid))
    print("  transfers on Airwallex:", [(t.id[:8], t.status, t.failure_type) for t in aw.list_transfers("self", oid)])


scenario("timeout", "CHANNEL_TIMEOUT")          # infra fault -> safe to replace
scenario("duplication", "DUPLICATION_RETURN")   # supplier may already hold the money -> human
print("\naudit rows:", len(cmd.audit))
