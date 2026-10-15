import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from payops import api, webhooks as W
from payops.airwallex import FakeAirwallex
from payops.commander import Commander
from payops.llm import HeuristicProposer
from payops.models import Obligation

SECRET = "whsec_test"


def event(eid, name, tid, status, created, ref="inv-1"):
    return json.dumps({"id": eid, "name": name, "created_at": created,
                       "data": {"object": {"id": tid, "status": status, "reference": ref}}}).encode()


def signed(body, ts=None):
    ts = ts or str(int(time.time() * 1000))
    return {"x-timestamp": ts, "x-signature": W.sign(SECRET, ts, body)}


def test_signature_roundtrip_tamper_and_replay():
    body, ts = b'{"a":1}', "1700000000000"
    sig = W.sign(SECRET, ts, body)
    assert W.verify(SECRET, ts, body, sig, now_ms=1700000000000 + 1000)
    assert not W.verify(SECRET, ts, body + b" ", sig, now_ms=1700000000000)  # tampered body
    assert not W.verify("other", ts, body, sig, now_ms=1700000000000)  # wrong secret
    assert not W.verify(SECRET, ts, body, sig, now_ms=1700000000000 + 10 * 60 * 1000)  # replay outside tolerance
    assert not W.verify(SECRET, "", body, sig) and not W.verify(SECRET, ts, body, "")


def test_bad_signature_is_401_and_nothing_dispatches():
    got = []
    r = W.WebhookReceiver(SECRET, lambda n, o: got.append(o))
    body = event("e1", "payout.transfer.failed", "t1", "FAILED", "2026-10-15T10:00:00+0000")
    assert r.handle({"x-timestamp": "1", "x-signature": "bad"}, body)[0] == 401 and got == []


def test_duplicates_ignored_and_out_of_order_events_dropped():
    got = []
    r = W.WebhookReceiver(SECRET, lambda n, o: got.append((n, o["status"])))
    newer = event("e2", "payout.transfer.paid", "t1", "PAID", "2026-10-15T10:00:05+0000")
    older = event("e1", "payout.transfer.sent", "t1", "SENT", "2026-10-15T10:00:01+0000")
    assert r.handle(signed(newer), newer) == (200, {"status": "queued"})  # acknowledged immediately, processed by the worker
    r.process()
    assert r.handle(signed(newer), newer) == (200, {"status": "duplicate"})  # redelivery
    assert r.handle(signed(older), older) == (200, {"status": "queued"}); r.process()  # arrives late
    assert got == [("payout.transfer.paid", "PAID")]  # applied once; the late SENT was dropped as stale
    assert ("stale", "e1", "payout.transfer.sent") in r.log
    other = event("e3", "payment_attempt.authorized", "p1", "AUTHORIZED", "2026-10-15T10:00:06+0000")
    r.handle(signed(other), other); r.process()
    assert ("ignored", "e3", "payment_attempt.authorized") in r.log
    assert r.handle(signed(b"not json"), b"not json")[0] == 400


def _wired():
    aw = FakeAirwallex(); aw.deposit("self", "EUR", 10_000)
    ob = Obligation("inv-1", "self", 100.0, "EUR", "SWIFT", "b")
    orig = aw.create_transfer(ob, "req-1")
    cmd = Commander(aw, HeuristicProposer()); cmd.register(ob, orig)
    return aw, cmd, orig, W.WebhookReceiver(SECRET, cmd.on_transfer_event)


def test_failed_event_triggers_exactly_one_replacement_even_if_delivered_twice():
    aw, cmd, orig, rcv = _wired()
    aw.transition("self", orig.id, "SENT")
    aw.transition("self", orig.id, "FAILED", "CHANNEL_TIMEOUT")  # the world changes; Airwallex then pushes the event
    body = event("e1", "payout.transfer.failed", orig.id, "CANCELLED", "2026-10-15T10:00:00+0000")
    for _ in range(3):  # at-least-once delivery
        assert rcv.handle(signed(body), body)[0] == 200
    rcv.process()
    assert len(aw.list_transfers("self", "inv-1")) == 2  # one replacement, not three
    assert cmd.event_outcomes[0][2].decision == "REPLACED"


def test_webhook_payload_is_not_trusted_for_money_decisions():
    """A forged-but-signed 'FAILED' event while the transfer is really in flight must not cause a replacement."""
    aw, cmd, orig, rcv = _wired()
    aw.transition("self", orig.id, "SENT")  # truly in flight
    body = event("e9", "payout.transfer.failed", orig.id, "FAILED", "2026-10-15T10:00:00+0000")
    rcv.handle(signed(body), body); rcv.process()
    assert len(aw.list_transfers("self", "inv-1")) == 1
    assert cmd.event_outcomes[0][2].decision == "WAITING"


def test_unknown_obligation_and_non_terminal_events_do_nothing():
    aw, cmd, orig, rcv = _wired()
    b1 = event("a", "payout.transfer.failed", "x", "FAILED", "2026-10-15T10:00:00+0000", ref="not-ours")
    b2 = event("b", "payout.transfer.sent", orig.id, "SENT", "2026-10-15T10:00:01+0000")
    rcv.handle(signed(b1), b1); rcv.handle(signed(b2), b2); rcv.process()
    assert cmd.event_outcomes == []


def test_http_endpoint_end_to_end():
    aw, cmd, orig, rcv = _wired()
    api.configure_webhooks(rcv)
    srv = api.serve(0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        aw.transition("self", orig.id, "SENT"); aw.transition("self", orig.id, "FAILED", "CHANNEL_TIMEOUT")
        body = event("h1", "payout.transfer.failed", orig.id, "CANCELLED", "2026-10-15T10:00:00+0000")
        req = urllib.request.Request(base + "/webhooks/airwallex", body, {**signed(body), "content-type": "application/json"})
        assert urllib.request.urlopen(req).status == 200
        rcv.process()
        bad = urllib.request.Request(base + "/webhooks/airwallex", body, {"x-timestamp": "1", "x-signature": "no"})
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(bad)
        assert e.value.code == 401
        assert len(aw.list_transfers("self", "inv-1")) == 2
    finally:
        srv.shutdown()
        api.configure_webhooks(None)


def test_ack_is_immediate_and_slow_processing_does_not_block_the_request():
    import time as _t
    calls = []
    def slow(name, obj):
        _t.sleep(0.3)  # e.g. an LLM call inside the Commander
        calls.append(obj["id"])
    r = W.WebhookReceiver(SECRET, slow)
    body = event("s1", "payout.transfer.failed", "t1", "FAILED", "2026-10-15T10:00:00+0000")
    t0 = _t.time()
    assert r.handle(signed(body), body)[0] == 200
    assert _t.time() - t0 < 0.2 and calls == []  # answered before the work ran
    r.start(0.05)
    try:
        for _ in range(40):
            if calls:
                break
            _t.sleep(0.05)
        assert calls == ["t1"]
    finally:
        r.stop()


def test_terminal_states_are_sticky_and_data_id_fallback_works():
    got = []
    r = W.WebhookReceiver(SECRET, lambda n, o: got.append(o["status"]))
    failed = json.dumps({"id": "a", "name": "payout.transfer.failed", "created_at": "2026-10-15T10:00:00+0000",
                         "data": {"id": "t1", "status": "FAILED"}}).encode()  # resource is data, not data.object
    paid = json.dumps({"id": "b", "name": "payout.transfer.paid", "created_at": "2026-10-15T10:00:09+0000",
                       "data": {"id": "t1", "status": "PAID"}}).encode()  # newer, but contradicts a terminal state
    r.handle(signed(failed), failed); r.process()
    r.handle(signed(paid), paid); r.process()
    assert got == ["FAILED"] and ("sticky", "b", "payout.transfer.paid") in r.log


def test_event_dedupe_survives_a_restart(tmp_path):
    from payops.store import Store
    db = str(tmp_path / "w.db")
    body = event("p1", "payout.transfer.failed", "t1", "FAILED", "2026-10-15T10:00:00+0000")
    W.WebhookReceiver(SECRET, lambda n, o: None, store=Store(db)).handle(signed(body), body)
    again = W.WebhookReceiver(SECRET, lambda n, o: None, store=Store(db))  # process restarted
    assert again.handle(signed(body), body) == (200, {"status": "duplicate"})


def test_test_event_secret_header_is_rejected_unless_explicitly_allowed():
    body = event("t1", "payout.transfer.failed", "t1", "FAILED", "2026-10-15T10:00:00+0000")
    ts = str(int(time.time() * 1000))
    hdr = {"x-timestamp": ts, "x-signature": W.sign("attacker-key", ts, body), "client-secret-key": "attacker-key"}
    assert W.WebhookReceiver(SECRET, lambda n, o: None).handle(hdr, body)[0] == 401  # default: forged header gives no access
    assert W.WebhookReceiver(SECRET, lambda n, o: None, allow_test_events=True).handle(hdr, body)[0] == 200  # sandbox opt-in
