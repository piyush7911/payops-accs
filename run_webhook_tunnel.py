"""Live webhook delivery over an ngrok tunnel that exposes ONLY POST /webhooks/airwallex (no UI, no approve endpoints).
Run: python run_webhook_tunnel.py   (needs NGROK_AUTHTOKEN in .env; AWX_WEBHOOK_SECRET once the webhook is registered)
Prints the URL to register in the sandbox web app (Settings > Developer > Webhooks). Every delivery is verified, logged to
docs/live-proof/webhook-events.jsonl (names and ids only) and handed to the Commander."""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from payops.llm import load_env
from payops.webhooks import WebhookReceiver

load_env()
secret = os.environ.get("AWX_WEBHOOK_SECRET", "")
LOG = os.path.join("docs", "live-proof", "webhook-events.jsonl")


def on_transfer(name: str, obj: dict) -> None:
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a") as f:
        f.write(json.dumps({"name": name, "transfer": obj.get("id"), "status": obj.get("status")}) + "\n")
    print("event", name, obj.get("id"), obj.get("status"), flush=True)


rcv = WebhookReceiver(secret or "unset", on_transfer)
rcv.start()


class H(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/webhooks/airwallex":
            self.send_response(404); self.end_headers(); return
        raw = self.rfile.read(int(self.headers.get("content-length") or 0))
        code, body = rcv.handle(dict(self.headers.items()), raw)
        data = json.dumps(body).encode()
        self.send_response(code); self.send_header("content-type", "application/json"); self.end_headers(); self.wfile.write(data)

    def log_message(self, *a):
        pass


port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
srv = ThreadingHTTPServer(("127.0.0.1", port), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
from pyngrok import conf, ngrok  # noqa: E402
conf.get_default().auth_token = os.environ["NGROK_AUTHTOKEN"]
url = ngrok.connect(port, "http").public_url
print(f"register this URL: {url}/webhooks/airwallex", flush=True)
threading.Event().wait()
