"""Durable, rotating credentials for anonymous browser identities.

Access JWTs stay short-lived.  This opaque credential is the durable browser
proof used to recover the same ``anon_*`` identity and mint a replacement JWT.
Only a SHA-256 digest is stored; successful use rotates the credential.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional


DEVICE_TTL_DAYS = 365


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _ensure_table(conn: Any) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS anonymous_guest_credentials (
               credential_hash TEXT PRIMARY KEY,
               user_id TEXT NOT NULL,
               channel TEXT NOT NULL,
               external_id TEXT NOT NULL,
               agent_id TEXT NOT NULL DEFAULT '',
               expires_at TEXT NOT NULL,
               last_used_at TEXT NOT NULL,
               created_at TEXT NOT NULL
           )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_guest_credentials_user "
        "ON anonymous_guest_credentials(user_id)"
    )


def recover_and_rotate(
    db: Any,
    credential: Optional[str],
    *,
    channel: str,
    agent_id: str = "",
) -> Optional[dict[str, str]]:
    """Consume a valid credential and return its identity plus a replacement."""
    if not credential:
        return None
    conn = db._get_conn()
    try:
        _ensure_table(conn)
        row = conn.execute(
            "SELECT * FROM anonymous_guest_credentials WHERE credential_hash=?",
            (_digest(credential),),
        ).fetchone()
        if not row:
            return None
        row = dict(row)
        if row.get("channel") != channel or str(row.get("agent_id") or "") != str(agent_id or ""):
            return None
        try:
            expiry = datetime.fromisoformat(str(row["expires_at"]).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        if expiry <= _now():
            conn.execute(
                "DELETE FROM anonymous_guest_credentials WHERE credential_hash=?",
                (_digest(credential),),
            )
            conn.commit()
            return None
        replacement = secrets.token_urlsafe(48)
        now = _now()
        updated = conn.execute(
            """UPDATE anonymous_guest_credentials
               SET credential_hash=?, expires_at=?, last_used_at=?
               WHERE credential_hash=?""",
            (
                _digest(replacement), _iso(now + timedelta(days=DEVICE_TTL_DAYS)),
                _iso(now), _digest(credential),
            ),
        )
        if updated.rowcount != 1:
            conn.rollback()
            return None
        conn.commit()
        return {
            "user_id": str(row["user_id"]),
            "external_id": str(row["external_id"]),
            "guest_credential": replacement,
        }
    finally:
        conn.close()


def issue(
    db: Any,
    *,
    user_id: str,
    channel: str,
    external_id: str,
    agent_id: str = "",
) -> str:
    """Create a durable device credential for a newly admitted guest."""
    raw = secrets.token_urlsafe(48)
    now = _now()
    conn = db._get_conn()
    try:
        _ensure_table(conn)
        conn.execute(
            """INSERT INTO anonymous_guest_credentials
               (credential_hash,user_id,channel,external_id,agent_id,
                expires_at,last_used_at,created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                _digest(raw), user_id, channel, external_id, agent_id or "",
                _iso(now + timedelta(days=DEVICE_TTL_DAYS)), _iso(now), _iso(now),
            ),
        )
        conn.commit()
        return raw
    finally:
        conn.close()
