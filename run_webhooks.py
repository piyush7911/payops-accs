"""Webhook-driven incident handling against the live sandbox. Run: AWX_WEBHOOK_SECRET=... python run_webhooks.py [port]
Register https://<your-tunnel>/webhooks/airwallex in Settings > Developer > Webhooks (sandbox web app) and copy the secret.
The agent only reacts when Airwallex pushes a terminal transfer event; it re-reads live state before deciding."""
import os
import sys

from payops import api
from payops.airwallex import AirwallexREST
from payops.commander import Commander
from payops.consistency import default_calibrator
from payops.judge import SafeguardJudge
from payops.llm import default_proposer, load_env
from payops.webhooks import WebhookReceiver

load_env()
aw = AirwallexREST(os.environ["AWX_CLIENT_ID"], os.environ["AWX_API_KEY"])
cmd = Commander(aw, default_proposer(), judge=SafeguardJudge(), calibrator=default_calibrator())
# obligations must be registered by whatever created the original transfer (see run_live.py: cmd.register(ob, orig))
rcv = WebhookReceiver(os.environ["AWX_WEBHOOK_SECRET"], cmd.on_transfer_event, store=cmd.store)
rcv.start()  # worker thread: the HTTP handler only verifies, persists and acknowledges
api.configure_webhooks(rcv)
port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
print(f"listening on :{port}  POST /webhooks/airwallex")
api.serve(port).serve_forever()
