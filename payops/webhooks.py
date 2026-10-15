"""Airwallex webhook receiver: verify -> durable dedupe -> acknowledge -> process out of band.

Signature (documented): x-signature = hex HMAC-SHA256(secret, x-timestamp + raw_body); x-timestamp is Unix ms; the body is the
raw bytes (never re-serialised). Verify BEFORE parsing; reject timestamps outside a tolerance (replay protection).
Delivery is at-least-once and unordered, and the sender retries for days on any non-200, so:
  * answer 200 IMMEDIATELY after persisting the event (the Commander may call an LLM, which is slow) - processing happens in a
    worker (`start()`), not in the request
  * dedupe by event `id` with an atomic INSERT OR IGNORE in SQLite (survives restarts)
  * apply an event to a transfer only if no newer event (by `created_at`) was already applied to it; terminal states are sticky
    (a FAILED/CANCELLED/PAID transfer is never moved back by a late event)
  * unknown events/ids are acknowledged and ignored
Event names: `payout.transfer.*` and `payout.batch_transfers.*` (confirmed by the reference builds); the resource is
`data.object` (or `data`). Test events from the Airwallex console are signed with the key in the payload's
`client-secret-key` header; accepting that is only safe in a sandbox, so it is OFF unless `allow_test_events=True`.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from typing import Callable, Dict, Optional, Tuple

from .store import Store

TERMINAL = ("PAID", "FAILED", "CANCELLED")


def sign(secret: str, timestamp: str, body: bytes) -> str:
    return hmac.new(secret.encode(), timestamp.encode() + body, hashlib.sha256).hexdigest()


def verify(secret: str, timestamp: str, body: bytes, signature: str, tolerance_ms: int = 5 * 60 * 1000,
           now_ms: Optional[int] = None) -> bool:
    if not (secret and timestamp and signature):
        return False
    if not hmac.compare_digest(sign(secret, timestamp, body), signature):
        return False
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    try:
        return abs(now - int(timestamp)) <= tolerance_ms
    except ValueError:
        return False


def _resource(ev: dict) -> dict:
    data = ev.get("data") or {}
    obj = data.get("object")
    return obj if isinstance(obj, dict) else data


class WebhookReceiver:
    def __init__(self, secret: str, on_transfer: Callable[[str, dict], None], tolerance_ms: int = 5 * 60 * 1000,
                 store: Optional[Store] = None, inline: bool = False, allow_test_events: bool = False) -> None:
        self.secret, self.on_transfer, self.tolerance_ms = secret, on_transfer, tolerance_ms
        self.store = store or Store(":memory:")
        self.inline, self.allow_test_events = inline, allow_test_events
        self._last_applied: Dict[str, str] = {}  # transfer id -> created_at of the newest event applied
        self._terminal: Dict[str, str] = {}  # transfer id -> terminal status already applied (sticky)
        self.log: list = []
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._plock = threading.Lock()

    # ---- request path: verify, claim, ack ----------------------------------------------------
    def handle(self, headers: Dict[str, str], raw_body: bytes, now_ms: Optional[int] = None) -> Tuple[int, dict]:
        h = {k.lower(): v for k, v in headers.items()}
        # DEMO-ONLY: allow_test_events trusts a key supplied BY THE SENDER (the console test-event path). PROD: leave it False.
        secrets = [self.secret] + ([h["client-secret-key"]] if self.allow_test_events and h.get("client-secret-key") else [])
        if not any(verify(s, h.get("x-timestamp", ""), raw_body, h.get("x-signature", ""), self.tolerance_ms, now_ms) for s in secrets):
            return 401, {"error": "bad signature or stale timestamp"}
        try:
            ev = json.loads(raw_body)
            eid, name = ev["id"], ev["name"]
        except (ValueError, KeyError, TypeError):
            return 400, {"error": "malformed event"}
        if not self.store.claim_event(eid, name, raw_body.decode()):  # atomic, durable dedupe
            return 200, {"status": "duplicate"}
        if self.inline:
            self.process()
        return 200, {"status": "queued"}

    # ---- worker path: order, dispatch --------------------------------------------------------
    def process(self, limit: int = 100) -> int:
        """Apply queued events oldest-created first. Safe to call from a worker or from tests."""
        with self._plock:
            rows = self.store.queued_events(limit)
            parsed = []
            for r in rows:
                ev = json.loads(r["payload"])
                parsed.append((ev.get("created_at", ""), r["event_id"], r["name"], ev))
            parsed.sort(key=lambda x: x[0])  # out-of-order delivery: oldest first within the batch
            for created, eid, name, ev in parsed:
                obj = _resource(ev)
                if not (name.startswith("payout.transfer.") and "id" in obj):
                    self.store.mark_event(eid, "done")
                    self.log.append(("ignored", eid, name))
                    continue
                tid = obj["id"]
                if created < self._last_applied.get(tid, ""):  # a newer event for this transfer already applied
                    self.store.mark_event(eid, "done")
                    self.log.append(("stale", eid, name))
                    continue
                if tid in self._terminal and str(obj.get("status", "")).upper() != self._terminal[tid]:
                    self.store.mark_event(eid, "done")  # terminal states are sticky: never regress
                    self.log.append(("sticky", eid, name))
                    continue
                self._last_applied[tid] = created
                if str(obj.get("status", "")).upper() in TERMINAL:
                    self._terminal[tid] = str(obj["status"]).upper()
                try:
                    self.on_transfer(name, obj)
                    self.store.mark_event(eid, "done")
                    self.log.append(("applied", eid, name))
                except Exception as e:  # leave a record; do not lose the event or crash the worker
                    self.store.mark_event(eid, "failed", repr(e)[:300])
                    self.log.append(("failed", eid, name))
            return len(parsed)

    def start(self, interval: float = 0.5) -> None:
        def loop():
            while not self._stop.is_set():
                self.process()
                self._stop.wait(interval)
        self._thread = threading.Thread(target=loop, daemon=True, name="webhook-worker")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
