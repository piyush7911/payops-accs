"""Web UI for the Incident Commander. Run: python run_ui.py [port] [--live]   then open http://127.0.0.1:8765
Simulator mode needs no credentials; the model is the real LLM if OPENAI_* is set in .env, else an offline heuristic.
--live runs the timeout/duplication scenarios against the real Airwallex sandbox (needs AWX_* in .env)."""
import sys

from payops import api
from payops.service import Service

port = int(next((a for a in sys.argv[1:] if a.isdigit()), 8765))
svc = Service(mode="live" if "--live" in sys.argv else "simulator", db_path=":memory:")
api.attach_service(svc)
api.configure_webhooks(svc.receiver)
print(f"Incident Commander UI on http://127.0.0.1:{port}  (mode={svc.mode}, llm={'live' if svc.use_llm else 'offline heuristic'})")
api.serve(port).serve_forever()
