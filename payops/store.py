"""Durable state (SQLite, stdlib). Everything the agent must not forget across a crash or restart lives here:

  obligations   what we are paying
  attempts      write-ahead record of every payment attempt: (obligation, attempt_no, request_id, state, transfer_id)
                state PENDING (about to call Airwallex / outcome unknown) -> CREATED (we hold a transfer id)
  events        webhook events claimed atomically by id (INSERT OR IGNORE), with a processing status
  approvals     human-approval queue, bound to the exact terms shown to the approver (terms_digest)
  audit         append-only log of every decision

Why this matters: the "never pays twice" guarantee used to depend on an in-memory set, which a restart would forget.
Now (1) the write-ahead row exists before money can move, and (2) request ids are derived deterministically from
(obligation, attempt_no), so even if this database were lost, a retry would reuse the same id and Airwallex would answer
duplicate_request_id instead of creating a second transfer.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

from .models import D, Obligation

SCHEMA = """
CREATE TABLE IF NOT EXISTS obligations(
  id TEXT PRIMARY KEY, tenant TEXT, amount TEXT, currency TEXT, method TEXT, beneficiary_id TEXT, observed_fee TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS attempts(
  obligation_id TEXT, attempt_no INTEGER, request_id TEXT UNIQUE, state TEXT, transfer_id TEXT, created_at REAL, updated_at REAL,
  PRIMARY KEY(obligation_id, attempt_no));
CREATE TABLE IF NOT EXISTS events(
  event_id TEXT PRIMARY KEY, name TEXT, payload TEXT, status TEXT, received_at REAL, error TEXT);
CREATE TABLE IF NOT EXISTS approvals(
  id TEXT PRIMARY KEY, obligation_id TEXT, clause TEXT, terms TEXT, terms_digest TEXT, status TEXT,
  created_at REAL, resolved_at REAL, resolved_by TEXT, result TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS one_pending_approval ON approvals(obligation_id, clause, terms_digest) WHERE status='pending';
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, obligation_id TEXT, kind TEXT, body TEXT);
"""


def request_id_for(obligation_id: str, attempt_no: int) -> str:
    """Deterministic, <= 47 chars (Airwallex rejected 54+ chars in verification), unique per (obligation, attempt).
    A retry of the same attempt reuses the same id, which is exactly what makes retries safe."""
    return f"po-{hashlib.sha256(obligation_id.encode()).hexdigest()[:16]}-r{attempt_no}"


def digest(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:24]


class Store:
    def __init__(self, path: str = ":memory:") -> None:
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)  # autocommit; we use explicit txns
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._db.executescript(SCHEMA)

    def _q(self, sql: str, args: tuple = ()) -> List[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def _x(self, sql: str, args: tuple = ()) -> int:
        with self._lock:
            return self._db.execute(sql, args).rowcount

    # ---- obligations -------------------------------------------------------------------------
    def add_obligation(self, ob: Obligation) -> None:
        self._x("INSERT OR REPLACE INTO obligations VALUES(?,?,?,?,?,?,?,?)",
                (ob.id, ob.tenant, str(ob.amount), ob.currency, ob.method, ob.beneficiary_id,
                 None if ob.observed_fee is None else str(ob.observed_fee), time.time()))

    def obligation(self, oid: str) -> Optional[Obligation]:
        r = self._q("SELECT * FROM obligations WHERE id=?", (oid,))
        if not r:
            return None
        r = r[0]
        return Obligation(r["id"], r["tenant"], D(r["amount"]), r["currency"], r["method"], r["beneficiary_id"],
                          None if r["observed_fee"] is None else D(r["observed_fee"]))

    def obligations(self) -> List[Obligation]:
        return [self.obligation(r["id"]) for r in self._q("SELECT id FROM obligations ORDER BY created_at")]

    # ---- attempts (write-ahead) --------------------------------------------------------------
    def begin_attempt(self, oid: str, attempt_no: int, request_id: str) -> bool:
        """Record intent BEFORE calling Airwallex. Returns False if this exact attempt was already recorded."""
        now = time.time()
        return self._x("INSERT OR IGNORE INTO attempts VALUES(?,?,?,?,?,?,?)",
                       (oid, attempt_no, request_id.lower(), "PENDING", None, now, now)) == 1

    def finish_attempt(self, request_id: str, transfer_id: str) -> None:
        self._x("UPDATE attempts SET state='CREATED', transfer_id=?, updated_at=? WHERE request_id=?",
                (transfer_id, time.time(), request_id.lower()))

    def attempts(self, oid: str) -> List[dict]:
        return [dict(r) for r in self._q("SELECT * FROM attempts WHERE obligation_id=? ORDER BY attempt_no", (oid,))]

    def pending_attempts(self) -> List[dict]:
        return [dict(r) for r in self._q("SELECT * FROM attempts WHERE state='PENDING'")]

    def used_request_ids(self, retrying: Optional[str] = None) -> set:
        """All request ids we have ever used. `retrying` excludes one PENDING id: the retry of that very payment."""
        rows = self._q("SELECT request_id, state FROM attempts")
        return {r["request_id"] for r in rows if not (retrying and r["request_id"] == retrying.lower() and r["state"] == "PENDING")}

    # ---- webhook events ----------------------------------------------------------------------
    def claim_event(self, event_id: str, name: str, payload: str) -> bool:
        """Atomic dedupe: True only for the first delivery of an event id."""
        return self._x("INSERT OR IGNORE INTO events VALUES(?,?,?,?,?,?)", (event_id, name, payload, "queued", time.time(), None)) == 1

    def queued_events(self, limit: int = 50) -> List[dict]:
        return [dict(r) for r in self._q("SELECT * FROM events WHERE status='queued' ORDER BY received_at LIMIT ?", (limit,))]

    def mark_event(self, event_id: str, status: str, error: Optional[str] = None) -> None:
        self._x("UPDATE events SET status=?, error=? WHERE event_id=?", (status, error, event_id))

    # ---- approvals ---------------------------------------------------------------------------
    def add_approval(self, oid: str, clause: str, terms: dict) -> dict:
        """Create a pending approval, or return the existing identical pending one (dedupe)."""
        d = digest(terms)
        existing = self._q("SELECT * FROM approvals WHERE obligation_id=? AND clause=? AND terms_digest=? AND status='pending'", (oid, clause, d))
        if existing:
            return dict(existing[0])
        aid = "apr_" + uuid.uuid4().hex[:12]
        self._x("INSERT INTO approvals VALUES(?,?,?,?,?,?,?,?,?,?)", (aid, oid, clause, json.dumps(terms, default=str), d, "pending", time.time(), None, None, None))
        return dict(self._q("SELECT * FROM approvals WHERE id=?", (aid,))[0])

    def approval(self, aid: str) -> Optional[dict]:
        r = self._q("SELECT * FROM approvals WHERE id=?", (aid,))
        return dict(r[0]) if r else None

    def approvals(self, status: Optional[str] = None) -> List[dict]:
        rows = self._q("SELECT * FROM approvals WHERE status=? ORDER BY created_at DESC", (status,)) if status else \
            self._q("SELECT * FROM approvals ORDER BY created_at DESC")
        return [dict(r) for r in rows]

    def resolve_approval(self, aid: str, status: str, by: str, result: str = "") -> bool:
        """Only a pending approval can be resolved, once. Returns False if it was already resolved (idempotent)."""
        return self._x("UPDATE approvals SET status=?, resolved_at=?, resolved_by=?, result=? WHERE id=? AND status='pending'",
                       (status, time.time(), by, result, aid)) == 1

    # ---- audit -------------------------------------------------------------------------------
    def log(self, oid: str, kind: str, body: Dict[str, Any]) -> None:
        self._x("INSERT INTO audit(ts, obligation_id, kind, body) VALUES(?,?,?,?)", (time.time(), oid, kind, json.dumps(body, default=str)))

    def audit(self, oid: Optional[str] = None, limit: int = 200) -> List[dict]:
        rows = self._q("SELECT * FROM audit WHERE obligation_id=? ORDER BY id DESC LIMIT ?", (oid, limit)) if oid else \
            self._q("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))
        return [{**dict(r), "body": json.loads(r["body"])} for r in rows]
