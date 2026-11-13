"""Real webhook delivery, end to end on the live sandbox: a real transfer fails, Airwallex pushes payout.transfer.failed through an
ngrok tunnel that exposes only POST /webhooks/airwallex, and the ACCS agent (real LLM) decides. Needs NGROK_AUTHTOKEN and AWX_WEBHOOK_SECRET.
Run: python run_webhook_e2e.py [FAILURE_TYPE ...]    Writes docs/live-proof/webhook-e2e.json"""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from payops import platform, proof
from payops.airwallex import AirwallexREST
from payops.commander import Commander
from payops.consistency import default_calibrator
from payops.judge import SafeguardJudge
from payops.llm import default_proposer, load_env
from payops.webhooks import WebhookReceiver

load_env()
client = AirwallexREST(os.environ["AWX_CLIENT_ID"], os.environ["AWX_API_KEY"])
cmd = Commander(client, default_proposer(), judge=SafeguardJudge(), calibrator=default_calibrator())
rcv = WebhookReceiver(os.environ["AWX_WEBHOOK_SECRET"], cmd.on_transfer_event, store=cmd.store)
rcv.start()


class H(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/webhooks/airwallex":
            self.send_response(404); self.end_headers(); return
        raw = self.rfile.read(int(self.headers.get("content-length") or 0))
        code, body = rcv.handle(dict(self.headers.items()), raw)
        self.send_response(code); self.send_header("content-type", "application/json"); self.end_headers(); self.wfile.write(json.dumps(body).encode())

    def log_message(self, *a):
        pass


srv = ThreadingHTTPServer(("127.0.0.1", 8765), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
from pyngrok import conf, ngrok  # noqa: E402
conf.get_default().auth_token = os.environ["NGROK_AUTHTOKEN"]
print("tunnel", ngrok.connect(8765, "http").public_url, flush=True)

tenant = platform.ensure_tenant(client, "Proof Tenant 1", deposit=250_000, min_balance=120_000)
lab = proof.Lab(client, [tenant], None, None, None, 1)
types = sys.argv[1:] or ["SYSTEM_ERROR", "CHANNEL_TIMEOUT", "RECALL_REQUESTED", "DUPLICATION_RETURN"]
rows = []
for ft in types:
    ob = proof.Obligation(f"hook-{ft[:6].lower()}-{int(time.time())}", tenant.account_id, 100, "EUR", "SWIFT", tenant.setup.beneficiary_id)
    orig = client.create_transfer(ob, f"orig-{ob.id}")
    cmd.register(ob, orig)
    proof.transition_retry(lab, ob, orig.id, "SENT"); time.sleep(0.5)
    n0 = len(cmd.event_outcomes)
    proof.transition_retry(lab, ob, orig.id, "FAILED", ft)  # no message from us: only the pushed event can wake the agent
    for _ in range(90):
        if len(cmd.event_outcomes) > n0:
            break
        time.sleep(1)
    time.sleep(3)
    out = cmd.event_outcomes[-1][2] if len(cmd.event_outcomes) > n0 else None
    m = proof.measure(lab, ob)
    rows.append({"failure": ft, "event_received": out is not None, "decision": getattr(out, "decision", None), "note": getattr(out, "note", "")[:200], "transfers": m["transfers"], "live": m["live"]})
    print(rows[-1], flush=True)
os.makedirs("docs/live-proof", exist_ok=True)
json.dump(rows, open("docs/live-proof/webhook-e2e.json", "w"), indent=1)
os._exit(0)
