"""Short-lived, single-use credentials for browser WebSocket URLs."""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time
from pathlib import Path


_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "db" / "connection_tickets.sqlite"
_ALLOWED_PATHS = frozenset({
    "/api/v1/browser/proxy",
    "/api/v1/browser/ws",
    "/api/v1/browser/screencast",
    "/api/v1/control/desktop",
    "/api/v1/terminal/ws",
})


def validate_path(path: str) -> str:
    normalized = str(path or "").strip()
    if normalized not in _ALLOWED_PATHS:
        raise ValueError("unsupported connection path")
    return normalized


def _connect() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), timeout=5.0)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS connection_tickets (
               ticket_hash TEXT PRIMARY KEY,
               user_id TEXT NOT NULL,
               path TEXT NOT NULL,
               expires_at REAL NOT NULL
           )"""
    )
    return conn


def _digest(ticket: str) -> str:
    return hashlib.sha256(ticket.encode("utf-8")).hexdigest()


def issue(user_id: str, path: str, *, ttl_seconds: int = 60) -> str:
    if not user_id:
        raise ValueError("user identity is required")
    path = validate_path(path)
    ticket = secrets.token_urlsafe(32)
    now = time.time()
    conn = _connect()
    try:
        conn.execute("DELETE FROM connection_tickets WHERE expires_at <= ?", (now,))
        conn.execute(
            "INSERT INTO connection_tickets(ticket_hash,user_id,path,expires_at) VALUES (?,?,?,?)",
            (_digest(ticket), user_id, path, now + max(1, min(int(ttl_seconds), 60))),
        )
        conn.commit()
    finally:
        conn.close()
    return ticket


def consume(ticket: str, path: str) -> str:
    if not ticket:
        return ""
    try:
        path = validate_path(path)
    except ValueError:
        return ""
    now = time.time()
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT user_id,path,expires_at FROM connection_tickets WHERE ticket_hash=?",
            (_digest(ticket),),
        ).fetchone()
        conn.execute("DELETE FROM connection_tickets WHERE ticket_hash=?", (_digest(ticket),))
        conn.commit()
        if not row or row[1] != path or float(row[2]) <= now:
            return ""
        return str(row[0])
    finally:
        conn.close()

