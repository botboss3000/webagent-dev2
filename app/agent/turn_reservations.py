"""Durable cross-process reservations for turns and side-effecting tools.

This is an at-most-once safety boundary, not an exactly-once claim. If a worker
dies after an external service accepts a request but before WebAgent records the
result, the call is marked ``uncertain`` and automatic replay is refused.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Optional

from app.db.browser_policy import load_browser_storage_policy


_ROOT = Path(__file__).resolve().parents[2]
_DB_PATH = _ROOT / "data" / "db" / "turn_reservations.sqlite"
_SCHEMA_READY: set[str] = set()
_SCHEMA_LOCK = threading.Lock()
ReservationState = Literal["acquired", "replay", "busy", "uncertain", "conflict"]


@dataclass(frozen=True)
class Reservation:
    state: ReservationState
    key: str
    owner_token: str = ""
    result: Optional[dict[str, Any]] = None
    detail: str = ""


def _connect() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    path_key = str(_DB_PATH)
    with _SCHEMA_LOCK:
        if path_key in _SCHEMA_READY:
            return conn
        schema_sql = """
        CREATE TABLE IF NOT EXISTS turn_reservations (
            reservation_key TEXT PRIMARY KEY,
            user_hash TEXT,
            request_hash TEXT NOT NULL,
            status TEXT NOT NULL,
            owner_token TEXT,
            lease_expires_at REAL,
            result_json TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tool_reservations (
            reservation_key TEXT PRIMARY KEY,
            turn_key TEXT NOT NULL,
            user_hash TEXT,
            request_hash TEXT NOT NULL,
            side_effecting INTEGER NOT NULL DEFAULT 0,
            provider_idempotent INTEGER NOT NULL DEFAULT 0,
            provider_name TEXT,
            provider_operation TEXT,
            provider_resource_id TEXT,
            status TEXT NOT NULL,
            owner_token TEXT,
            lease_expires_at REAL,
            result_json TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_tool_reservations_turn
            ON tool_reservations(turn_key);
        """
        last_error = None
        for attempt in range(5):
            try:
                try:
                    conn.execute("PRAGMA journal_mode=WAL")
                except sqlite3.OperationalError:
                    # Another cold-starting process may be switching journal
                    # mode. The schema transaction below is the real gate.
                    pass
                conn.executescript(schema_sql)
                existing = {
                    str(row[1])
                    for row in conn.execute(
                        "PRAGMA table_info(tool_reservations)"
                    ).fetchall()
                }
                for column, declaration in (
                    ("user_hash", "TEXT"),
                    ("provider_idempotent", "INTEGER NOT NULL DEFAULT 0"),
                    ("provider_name", "TEXT"),
                    ("provider_operation", "TEXT"),
                    ("provider_resource_id", "TEXT"),
                ):
                    if column not in existing:
                        conn.execute(
                            f"ALTER TABLE tool_reservations "
                            f"ADD COLUMN {column} {declaration}"
                        )
                turn_existing = {
                    str(row[1])
                    for row in conn.execute(
                        "PRAGMA table_info(turn_reservations)"
                    ).fetchall()
                }
                if "user_hash" not in turn_existing:
                    conn.execute(
                        "ALTER TABLE turn_reservations ADD COLUMN user_hash TEXT"
                    )
                _SCHEMA_READY.add(path_key)
                break
            except sqlite3.OperationalError as exc:
                last_error = exc
                if "locked" not in str(exc).lower() or attempt == 4:
                    conn.close()
                    raise
                time.sleep(0.05 * (attempt + 1))
        else:  # pragma: no cover - loop raises on final failure
            conn.close()
            raise last_error or sqlite3.OperationalError("schema initialization failed")
    return conn


def stable_key(*parts: Any) -> str:
    raw = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _reserve(
    table: str,
    key: str,
    request_hash: str,
    *,
    lease_seconds: int,
    side_effecting: bool = False,
    turn_key: str = "",
    user_hash: str = "",
) -> Reservation:
    now = time.time()
    owner = secrets.token_urlsafe(18)
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            f"SELECT * FROM {table} WHERE reservation_key=?", (key,)
        ).fetchone()
        if row is None:
            if table == "tool_reservations":
                conn.execute(
                    """INSERT INTO tool_reservations
                       (reservation_key,turn_key,user_hash,request_hash,side_effecting,status,
                        owner_token,lease_expires_at,created_at,updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (
                        key,
                        turn_key,
                        user_hash,
                        request_hash,
                        int(side_effecting),
                        "running",
                        owner,
                        now + lease_seconds,
                        now,
                        now,
                    ),
                )
            else:
                conn.execute(
                    """INSERT INTO turn_reservations
                       (reservation_key,user_hash,request_hash,status,owner_token,
                        lease_expires_at,created_at,updated_at)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (
                        key,
                        user_hash,
                        request_hash,
                        "running",
                        owner,
                        now + lease_seconds,
                        now,
                        now,
                    ),
                )
            conn.commit()
            return Reservation("acquired", key, owner)
        if row["request_hash"] != request_hash:
            conn.commit()
            return Reservation("conflict", key, detail="idempotency key reused with different input")
        if row["status"] == "complete":
            result = None
            try:
                result = json.loads(row["result_json"]) if row["result_json"] else None
            except (TypeError, json.JSONDecodeError):
                pass
            conn.commit()
            return Reservation("replay", key, result=result)
        if row["status"] in {"uncertain", "failed"}:
            conn.commit()
            return Reservation("uncertain", key, detail=row["status"])
        if float(row["lease_expires_at"] or 0) > now:
            conn.commit()
            return Reservation("busy", key)
        provider_retry_safe = (
            table == "tool_reservations"
            and bool(row["side_effecting"])
            and bool(row["provider_idempotent"])
        )
        if (
            table == "turn_reservations"
            or (
                table == "tool_reservations"
                and bool(row["side_effecting"])
                and not provider_retry_safe
            )
        ):
            conn.execute(
                f"UPDATE {table} SET status='uncertain',updated_at=? "
                "WHERE reservation_key=?",
                (now, key),
            )
            conn.commit()
            return Reservation(
                "uncertain",
                key,
                detail=(
                    "previous worker lost its lease before durable completion"
                    if table == "turn_reservations"
                    else "previous worker lost its lease during a side-effecting call"
                ),
            )
        conn.execute(
            f"UPDATE {table} SET status='running',owner_token=?,lease_expires_at=?,"
            "updated_at=? WHERE reservation_key=?",
            (owner, now + lease_seconds, now, key),
        )
        conn.commit()
        return Reservation("acquired", key, owner)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def reserve_turn(
    user_id: str,
    session_id: str,
    idempotency_key: str,
    request_hash: str,
    *,
    lease_seconds: int = 300,
) -> Reservation:
    key = stable_key("turn", user_id, session_id, idempotency_key)
    return _reserve(
        "turn_reservations",
        key,
        request_hash,
        lease_seconds=lease_seconds,
        user_hash=stable_key("user", user_id),
    )


def reserve_tool(
    turn_key: str,
    tool_call_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    *,
    side_effecting: bool,
    lease_seconds: int = 300,
) -> Reservation:
    request_hash = stable_key(tool_name, arguments)
    # The provider's tool_call_id can change when a crashed turn is rebuilt.
    # Key side effects by normalized intent within the durable turn instead.
    # Repeating an identical destructive action in one turn is conservatively
    # treated as a replay; callers can start a new user turn to repeat it.
    key = stable_key("tool", turn_key, tool_name, arguments)
    conn = _connect()
    try:
        parent = conn.execute(
            "SELECT user_hash FROM turn_reservations WHERE reservation_key=?",
            (turn_key,),
        ).fetchone()
        user_hash = str(parent[0] or "") if parent else ""
    finally:
        conn.close()
    return _reserve(
        "tool_reservations",
        key,
        request_hash,
        lease_seconds=lease_seconds,
        side_effecting=side_effecting,
        turn_key=turn_key,
        user_hash=user_hash,
    )


def tool_intent_status(turn_key: str, tool_name: str, arguments: dict) -> Optional[str]:
    """Return the durable state for one normalized side-effect intent."""
    if not turn_key or not tool_name:
        return None
    key = stable_key("tool", turn_key, tool_name, arguments)
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT status FROM tool_reservations WHERE reservation_key=?",
            (key,),
        ).fetchone()
        return str(row["status"]) if row else None
    finally:
        conn.close()


def reservation_status(reservation_key: str) -> Optional[str]:
    if not reservation_key:
        return None
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT status FROM tool_reservations WHERE reservation_key=?",
            (reservation_key,),
        ).fetchone()
        return str(row["status"]) if row else None
    finally:
        conn.close()


def complete(reservation: Reservation, result: Optional[dict[str, Any]] = None) -> bool:
    # Both tables use disjoint stable-key namespaces. Find the owning table
    # instead of trusting caller-provided metadata.
    conn = _connect()
    try:
        for candidate in ("tool_reservations", "turn_reservations"):
            cur = conn.execute(
                f"UPDATE {candidate} SET status='complete',result_json=?,"
                "lease_expires_at=NULL,updated_at=? WHERE reservation_key=? "
                "AND owner_token=? AND status='running'",
                (
                    json.dumps(result, separators=(",", ":"), default=str)
                    if result is not None
                    else None,
                    time.time(),
                    reservation.key,
                    reservation.owner_token,
                ),
            )
            if cur.rowcount:
                return True
        return False
    finally:
        conn.close()


def fail(reservation: Reservation, *, uncertain: bool = False) -> bool:
    status = "uncertain" if uncertain else "failed"
    conn = _connect()
    try:
        for table in ("tool_reservations", "turn_reservations"):
            cur = conn.execute(
                f"UPDATE {table} SET status=?,lease_expires_at=NULL,updated_at=? "
                "WHERE reservation_key=? AND owner_token=? AND status='running'",
                (status, time.time(), reservation.key, reservation.owner_token),
            )
            if cur.rowcount:
                return True
        return False
    finally:
        conn.close()


def register_provider_reconciliation(
    reservation_key: str,
    *,
    provider: str,
    operation: str,
    resource_id: str,
    provider_idempotent: bool,
) -> bool:
    """Persist a non-secret reconciliation hint before the provider call starts.

    A retry is allowed after a lost lease only when the adapter explicitly says
    the provider will deduplicate the same deterministic identifier.
    """
    if not reservation_key or not provider or not operation or not resource_id:
        return False
    conn = _connect()
    try:
        cur = conn.execute(
            """UPDATE tool_reservations
               SET provider_idempotent=?,provider_name=?,provider_operation=?,
                   provider_resource_id=?,updated_at=?
               WHERE reservation_key=? AND status='running'""",
            (
                int(provider_idempotent),
                provider,
                operation,
                resource_id,
                time.time(),
                reservation_key,
            ),
        )
        return bool(cur.rowcount)
    finally:
        conn.close()


def get_provider_reconciliation(reservation_key: str) -> Optional[dict[str, Any]]:
    """Return the stored non-secret provider lookup hint for recovery tooling."""
    if not reservation_key:
        return None
    conn = _connect()
    try:
        row = conn.execute(
            """SELECT status,provider_idempotent,provider_name,
                      provider_operation,provider_resource_id,updated_at
               FROM tool_reservations WHERE reservation_key=?""",
            (reservation_key,),
        ).fetchone()
        if not row or not row["provider_name"]:
            return None
        return {
            "status": str(row["status"]),
            "provider_idempotent": bool(row["provider_idempotent"]),
            "provider": str(row["provider_name"]),
            "operation": str(row["provider_operation"]),
            "resource_id": str(row["provider_resource_id"]),
            "updated_at": float(row["updated_at"]),
        }
    finally:
        conn.close()


def prune() -> int:
    cutoff = time.time() - (
        load_browser_storage_policy().turn_reservation_retention_hours * 3600
    )
    conn = _connect()
    try:
        total = 0
        for table in ("tool_reservations", "turn_reservations"):
            cur = conn.execute(
                f"DELETE FROM {table} WHERE updated_at < ? AND status != 'running'",
                (cutoff,),
            )
            total += int(cur.rowcount or 0)
        return total
    finally:
        conn.close()


def delete_user_reservations(user_id: str) -> int:
    """Erase durable turn/tool receipts without ever storing a raw user id."""
    user_hash = stable_key("user", user_id)
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        total = 0
        for table in ("tool_reservations", "turn_reservations"):
            cur = conn.execute(
                f"DELETE FROM {table} WHERE user_hash=?",
                (user_hash,),
            )
            total += int(cur.rowcount or 0)
        conn.commit()
        return total
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
