"""Cross-process JWT revocation epochs and a minimal device registry."""

from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional


_ROOT = Path(__file__).resolve().parents[2]
_DB_PATH = _ROOT / "data" / "config" / "auth_revocation.sqlite"
_SCHEMA_READY: set[str] = set()
_SCHEMA_LOCK = threading.Lock()


def _connect() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), timeout=10, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=10000")
    path_key = str(_DB_PATH)
    with _SCHEMA_LOCK:
        if path_key in _SCHEMA_READY:
            return conn
        schema_sql = """
        CREATE TABLE IF NOT EXISTS user_revocation_epochs (
            user_hash TEXT PRIMARY KEY,
            epoch INTEGER NOT NULL DEFAULT 0,
            updated_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS auth_devices (
            user_hash TEXT NOT NULL,
            device_id TEXT NOT NULL,
            issued_epoch INTEGER NOT NULL,
            first_seen INTEGER NOT NULL,
            last_seen INTEGER NOT NULL,
            revoked_at INTEGER,
            purge_requested_at INTEGER,
            purge_acknowledged_at INTEGER,
            remember_token_hash TEXT,
            last_login_at INTEGER,
            last_ip TEXT,
            last_location TEXT,
            user_agent TEXT,
            last_origin TEXT,
            device_version INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (user_hash, device_id)
        );
        CREATE INDEX IF NOT EXISTS idx_auth_devices_user
            ON auth_devices(user_hash, revoked_at);
        """
        last_error = None
        for attempt in range(5):
            try:
                try:
                    conn.execute("PRAGMA journal_mode=WAL")
                except sqlite3.OperationalError:
                    pass
                conn.executescript(schema_sql)
                existing = {
                    str(row[1])
                    for row in conn.execute("PRAGMA table_info(auth_devices)").fetchall()
                }
                optional_columns = {
                    "purge_requested_at": "INTEGER",
                    "purge_acknowledged_at": "INTEGER",
                    "remember_token_hash": "TEXT",
                    "last_login_at": "INTEGER",
                    "last_ip": "TEXT",
                    "last_location": "TEXT",
                    "user_agent": "TEXT",
                    "last_origin": "TEXT",
                    "device_version": "INTEGER",
                }
                for column, column_type in optional_columns.items():
                    if column not in existing:
                        conn.execute(
                            f"ALTER TABLE auth_devices ADD COLUMN {column} {column_type}"
                        )
                conn.execute(
                    "UPDATE auth_devices SET device_version=1 WHERE device_version IS NULL"
                )
                conn.execute(
                    """CREATE UNIQUE INDEX IF NOT EXISTS idx_auth_devices_remember
                       ON auth_devices(remember_token_hash)
                       WHERE remember_token_hash IS NOT NULL"""
                )
                _SCHEMA_READY.add(path_key)
                break
            except sqlite3.OperationalError as exc:
                last_error = exc
                if "locked" not in str(exc).lower() or attempt == 4:
                    conn.close()
                    raise
                time.sleep(0.05 * (attempt + 1))
        else:  # pragma: no cover
            conn.close()
            raise last_error or sqlite3.OperationalError("schema initialization failed")
    return conn


def _user_hash(user_id: str) -> str:
    return hashlib.sha256(f"webagent-auth:{user_id}".encode("utf-8")).hexdigest()


def _remember_hash(token: str) -> str:
    return hashlib.sha256(
        f"webagent-remember:{token}".encode("utf-8")
    ).hexdigest()


def current_epoch(user_id: str) -> int:
    if not user_id:
        return 0
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT epoch FROM user_revocation_epochs WHERE user_hash=?",
            (_user_hash(user_id),),
        ).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def register_device(
    user_id: str,
    device_id: str,
    epoch: int,
    *,
    reauthenticated: bool = False,
) -> int:
    if not user_id or not device_id:
        return 0
    now = int(time.time())
    conn = _connect()
    try:
        conn.execute(
            """DELETE FROM auth_devices
               WHERE last_seen < ?
                 AND NOT (
                     purge_requested_at IS NOT NULL
                     AND purge_acknowledged_at IS NULL
                 )""",
            (now - 90 * 24 * 3600,),
        )
        conn.execute(
            """INSERT INTO auth_devices
                   (user_hash,device_id,issued_epoch,first_seen,last_seen,
                    revoked_at,last_login_at,device_version)
               VALUES (?,?,?,?,?,NULL,?,1)
               ON CONFLICT(user_hash,device_id) DO UPDATE SET
                   issued_epoch=excluded.issued_epoch,
                   last_seen=excluded.last_seen,
                   last_login_at=excluded.last_login_at,
                   device_version=CASE WHEN ?
                       THEN COALESCE(auth_devices.device_version, 1) + 1
                       ELSE COALESCE(auth_devices.device_version, 1) END,
                   revoked_at=CASE WHEN ? THEN NULL ELSE auth_devices.revoked_at END,
                   purge_requested_at=CASE WHEN ? THEN NULL ELSE auth_devices.purge_requested_at END,
                   purge_acknowledged_at=CASE WHEN ? THEN NULL ELSE auth_devices.purge_acknowledged_at END
               WHERE ? OR (
                   auth_devices.revoked_at IS NULL
                   AND auth_devices.purge_requested_at IS NULL
               )""",
            (
                _user_hash(user_id), device_id, int(epoch), now, now, now,
                reauthenticated, reauthenticated, reauthenticated,
                reauthenticated, reauthenticated,
            ),
        )
        row = conn.execute(
            "SELECT device_version FROM auth_devices WHERE user_hash=? AND device_id=?",
            (_user_hash(user_id), device_id),
        ).fetchone()
        return int(row[0] or 1) if row else 0
    finally:
        conn.close()


def update_device_metadata(
    user_id: str,
    device_id: str,
    *,
    ip_address: str = "",
    location: str = "",
    user_agent: str = "",
    origin: str = "",
) -> bool:
    """Attach display-only login context to an existing device record."""
    if not user_id or not device_id:
        return False
    conn = _connect()
    try:
        cur = conn.execute(
            """UPDATE auth_devices
               SET last_ip=?,last_location=?,user_agent=?,last_origin=?
               WHERE user_hash=? AND device_id=?""",
            (
                ip_address[:64], location[:160], user_agent[:512], origin[:255],
                _user_hash(user_id), device_id,
            ),
        )
        return bool(cur.rowcount)
    finally:
        conn.close()


def bind_remember_token(user_id: str, device_id: str, token: str) -> bool:
    """Bind the current remember token to one active device, storing only a hash."""
    if not user_id or not device_id or not token:
        return False
    key = _user_hash(user_id)
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        # Remember-me is historically single-session. Rotation must invalidate
        # any older device binding as well as the account-store token.
        conn.execute(
            "UPDATE auth_devices SET remember_token_hash=NULL WHERE user_hash=?",
            (key,),
        )
        cur = conn.execute(
            """UPDATE auth_devices SET remember_token_hash=?
               WHERE user_hash=? AND device_id=? AND revoked_at IS NULL
                 AND issued_epoch=COALESCE(
                     (SELECT epoch FROM user_revocation_epochs WHERE user_hash=?),
                     0
                 )""",
            (_remember_hash(token), key, device_id, key),
        )
        conn.commit()
        return bool(cur.rowcount)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def clear_remember_tokens(user_id: str) -> int:
    """Remove all device bindings after account-token rotation or logout."""
    if not user_id:
        return 0
    conn = _connect()
    try:
        cur = conn.execute(
            "UPDATE auth_devices SET remember_token_hash=NULL "
            "WHERE user_hash=? AND remember_token_hash IS NOT NULL",
            (_user_hash(user_id),),
        )
        return int(cur.rowcount or 0)
    finally:
        conn.close()


def remember_token_device(user_id: str, token: str) -> Optional[str]:
    """Return the current, non-revoked device bound to a remember token."""
    if not user_id or not token:
        return None
    key = _user_hash(user_id)
    conn = _connect()
    try:
        row = conn.execute(
            """SELECT d.device_id,d.revoked_at,d.issued_epoch,
                      COALESCE(e.epoch, 0)
               FROM auth_devices d
               LEFT JOIN user_revocation_epochs e ON e.user_hash=d.user_hash
               WHERE d.user_hash=? AND d.remember_token_hash=?""",
            (key, _remember_hash(token)),
        ).fetchone()
        if not row or row[1] is not None or int(row[2]) != int(row[3]):
            return None
        return str(row[0])
    finally:
        conn.close()


def claim_legacy_remember_token(user_id: str, token: str) -> Optional[str]:
    """Migrate a pre-binding token only for one unambiguous active device."""
    if not user_id or not token:
        return None
    key = _user_hash(user_id)
    conn = _connect()
    try:
        rows = conn.execute(
            """SELECT d.device_id,d.issued_epoch,d.revoked_at,
                      d.purge_requested_at,COALESCE(e.epoch, 0)
               FROM auth_devices d
               LEFT JOIN user_revocation_epochs e ON e.user_hash=d.user_hash
               WHERE d.user_hash=?""",
            (key,),
        ).fetchall()
    finally:
        conn.close()
    eligible = [
        str(row[0])
        for row in rows
        if row[2] is None and row[3] is None and int(row[1]) == int(row[4])
    ]
    if len(rows) != 1 or len(eligible) != 1:
        return None
    return eligible[0] if bind_remember_token(user_id, eligible[0], token) else None


def token_is_current(
    user_id: str,
    token_epoch: int,
    device_id: str = "",
    device_version: int = 0,
) -> bool:
    epoch = current_epoch(user_id)
    if int(token_epoch or 0) != epoch:
        return False
    if not device_id:
        return epoch == 0  # legacy tokens remain valid only until first revocation
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT revoked_at,last_seen,device_version FROM auth_devices "
            "WHERE user_hash=? AND device_id=?",
            (_user_hash(user_id), device_id),
        ).fetchone()
        if row is None or row[0] is not None:
            return False
        stored_version = int(row[2] or 1)
        if (int(device_version or 0) or 1) != stored_version:
            return False
        now = int(time.time())
        if int(row[1] or 0) < now - 300:
            conn.execute(
                "UPDATE auth_devices SET last_seen=? "
                "WHERE user_hash=? AND device_id=? AND revoked_at IS NULL",
                (now, _user_hash(user_id), device_id),
            )
        return True
    finally:
        conn.close()


def revoke_user(user_id: str) -> int:
    """Atomically invalidate every token/device currently issued to a user."""
    now = int(time.time())
    key = _user_hash(user_id)
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT epoch FROM user_revocation_epochs WHERE user_hash=?", (key,)
        ).fetchone()
        next_epoch = (int(row[0]) if row else 0) + 1
        conn.execute(
            """INSERT INTO user_revocation_epochs(user_hash,epoch,updated_at)
               VALUES (?,?,?)
               ON CONFLICT(user_hash) DO UPDATE SET
                   epoch=excluded.epoch, updated_at=excluded.updated_at""",
            (key, next_epoch, now),
        )
        conn.execute(
            """UPDATE auth_devices
               SET revoked_at=?,
                   purge_requested_at=COALESCE(purge_requested_at, ?),
                   purge_acknowledged_at=NULL
               WHERE user_hash=? AND revoked_at IS NULL""",
            (now, now, key),
        )
        conn.commit()
        return next_epoch
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def revoke_device(user_id: str, device_id: str) -> bool:
    return bool(revoke_devices(user_id, [device_id]))


def revoke_devices(user_id: str, device_ids: list[str]) -> list[str]:
    """Atomically revoke the requested devices and require local cache erasure."""
    unique_ids = list(dict.fromkeys(str(value).strip() for value in device_ids))
    unique_ids = [value for value in unique_ids if value]
    if not user_id or not unique_ids:
        return []
    if len(unique_ids) > 250:
        raise ValueError("Too many device sessions selected")

    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        placeholders = ",".join("?" for _ in unique_ids)
        key = _user_hash(user_id)
        rows = conn.execute(
            f"""SELECT device_id FROM auth_devices
                 WHERE user_hash=? AND device_id IN ({placeholders})
                   AND revoked_at IS NULL""",
            (key, *unique_ids),
        ).fetchall()
        found = {str(row[0]) for row in rows}
        revoked = [device_id for device_id in unique_ids if device_id in found]
        if revoked:
            revoked_placeholders = ",".join("?" for _ in revoked)
            now = int(time.time())
            conn.execute(
                f"""UPDATE auth_devices
                    SET revoked_at=?,
                        purge_requested_at=COALESCE(purge_requested_at, ?),
                        purge_acknowledged_at=NULL,
                        remember_token_hash=NULL
                    WHERE user_hash=? AND device_id IN ({revoked_placeholders})
                      AND revoked_at IS NULL""",
                (now, now, key, *revoked),
            )
        conn.commit()
        return revoked
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_devices(user_id: str) -> list[dict]:
    """List sessions that can still authenticate; revoked purge jobs stay hidden."""
    conn = _connect()
    try:
        rows = conn.execute(
            """SELECT device_id,issued_epoch,first_seen,last_seen,revoked_at,
                      purge_requested_at,purge_acknowledged_at,last_login_at,
                      last_ip,last_location,user_agent,last_origin
               FROM auth_devices
               WHERE user_hash=? AND revoked_at IS NULL
                 AND purge_requested_at IS NULL
                 AND issued_epoch=COALESCE(
                     (SELECT epoch FROM user_revocation_epochs WHERE user_hash=?), 0
                 )
               ORDER BY last_seen DESC""",
            (_user_hash(user_id), _user_hash(user_id)),
        ).fetchall()
        return [
            {
                "device_id": row[0],
                "issued_epoch": int(row[1]),
                "first_seen": int(row[2]),
                "last_seen": int(row[3]),
                "revoked": row[4] is not None,
                "purge_requested_at": int(row[5]) if row[5] is not None else None,
                "purge_acknowledged_at": int(row[6]) if row[6] is not None else None,
                "purge_acknowledged": row[6] is not None,
                "last_login_at": int(row[7] or row[2]),
                "ip_address": str(row[8] or ""),
                "location": str(row[9] or "Unknown"),
                "user_agent": str(row[10] or ""),
                "origin": str(row[11] or ""),
            }
            for row in rows
        ]
    finally:
        conn.close()


def device_purge_status(user_id: str, device_id: str) -> dict:
    """Return only the purge state for one cryptographically identified device."""
    conn = _connect()
    try:
        row = conn.execute(
            """SELECT purge_requested_at,purge_acknowledged_at,revoked_at
               FROM auth_devices WHERE user_hash=? AND device_id=?""",
            (_user_hash(user_id), device_id),
        ).fetchone()
        if row is None:
            return {
                "purge_required": False,
                "purge_acknowledged": False,
                "revoked": True,
            }
        return {
            "purge_required": row[0] is not None and row[1] is None,
            "purge_requested_at": int(row[0]) if row[0] is not None else None,
            "purge_acknowledged": row[1] is not None,
            "purge_acknowledged_at": int(row[1]) if row[1] is not None else None,
            "revoked": row[2] is not None,
        }
    finally:
        conn.close()


def acknowledge_device_purge(user_id: str, device_id: str) -> bool:
    """Acknowledge physical local-cache deletion for a requested device."""
    now = int(time.time())
    conn = _connect()
    try:
        cur = conn.execute(
            """UPDATE auth_devices SET purge_acknowledged_at=?
               WHERE user_hash=? AND device_id=?
                 AND purge_requested_at IS NOT NULL
                 AND purge_acknowledged_at IS NULL""",
            (now, _user_hash(user_id), device_id),
        )
        return bool(cur.rowcount)
    finally:
        conn.close()


def token_device_id(payload: Optional[dict]) -> str:
    return str((payload or {}).get("device_id") or "")
