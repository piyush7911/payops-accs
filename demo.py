"""Offline demo of the Payment Ops Incident Commander + ACCS. Run: python demo.py"""
from payops.airwallex import FakeAirwallex
from payops.commander import Commander
from payops.llm import HeuristicProposer, RecklessProposer, default_proposer
from payops.models import Obligation


def show(title, out):
    v = out.verdict
    print(f"  [{out.decision}] gate={'PERMIT' if v.permit else 'DENY ' + str(v.clause)} score={out.score.value:.2f}  {out.note}")


def setup(aw, tenant, oid, amount, balance):
    aw.deposit(tenant, "EUR", balance)
    ob = Obligation(oid, tenant, amount, "EUR", "SWIFT", "ben_1")
    orig = aw.create_transfer(ob, f"req-{oid}-orig")
    return ob, orig


aw = FakeAirwallex()
brain = default_proposer()
print("proposer:", type(brain).__name__, getattr(brain, "model", ""))
cmd = Commander(aw, brain)

print("\n== Tenant A: normal incident ==")
a, a0 = setup(aw, "acct_A", "inv-A", 1000, 2100)
cmd.register(a, a0)
show("in flight", cmd.handle("inv-A", "Supplier: nothing arrived!"))
aw.transition("acct_A", a0.id, "SENT")
aw.transition("acct_A", a0.id, "FAILED", "CHANNEL_TIMEOUT")
out = cmd.handle("inv-A", "Supplier: deadline passed")
show("replace", out)
show("duplicate replace", cmd.handle("inv-A", "Supplier: still nothing"))  # live replacement exists
t = aw.list_transfers("acct_A", "inv-A")[1]
aw.transition("acct_A", t.id, "SENT"); aw.transition("acct_A", t.id, "PAID")
print("  close report:", cmd.close_report("inv-A"))

print("\n== Tenant B: wallet short, tenant A has plenty (no cross-tenant funding) ==")
b, b0 = setup(aw, "acct_B", "inv-B", 1000, 1500)  # enough for ONE swift payout only
cmd.register(b, b0)
aw.transition("acct_B", b0.id, "SENT"); aw.transition("acct_B", b0.id, "FAILED", "CHANNEL_TIMEOUT")
show("replace w/o funds", cmd.handle("inv-B", "Supplier: where is it?"))

print("\n== Misbehaving LLM: claims a transfer failed when it is only SENT ==")
aw2 = FakeAirwallex(); bad = Commander(aw2, RecklessProposer())
c, c0 = setup(aw2, "acct_C", "inv-C", 500, 5000)
bad.register(c, c0); aw2.transition("acct_C", c0.id, "SENT")
show("hallucinated failure", bad.handle("inv-C", "just resend it"))
print("  transfers created:", len(aw2.list_transfers("acct_C", "inv-C")), "(still 1: no double payment)")

print("\nAudit rows:", len(cmd.audit) + len(bad.audit))
