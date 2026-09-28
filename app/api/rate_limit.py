"""Anonymous/public-chat abuse controls.

Purpose: put a floor under the abuse surface opened by anonymous/public chat
(the website embed widget). Without it, anyone can mint unlimited guest tokens
and spam `/chat/stream`, burning the agent owner's LLM budget (see the security
audit — this was the CRITICAL finding).

Design: short-lived fixed-window counters use a process-local dict. Anonymous
identity and chat counters are also persisted in SQLite so workers and restarts
on one host share enforcement. A multi-host deployment still needs a shared
edge limiter (Caddy/Cloudflare/Redis) in front of every instance.

Limits are env-overridable so an operator can tighten/loosen without a redeploy.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Dict, Tuple

from fastapi import HTTPException, Request

# key -> (window_start_epoch, count)
_BUCKETS: Dict[str, Tuple[float, int]] = {}
_LOCK = Lock()
# Prune when the table grows past this many keys (windows are short, so stale
# entries are common under churn — keeps memory bounded without a background task).
_MAX_KEYS = 20000

# Durable, process-shared anonymous abuse state. Runtime databases live under
# data/db (git-ignored); SQLite WAL + BEGIN IMMEDIATE makes the counters atomic
# across uvicorn workers on one host. A multi-host deployment still needs a
# shared edge limiter (Cloudflare/Caddy/Redis) in front of every instance.
_ABUSE_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "db" / "anonymous_abuse.sqlite"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except Exception:
        return default


def client_ip(request: Request) -> str:
    """Validated client IP with forwarding trusted only from known proxies.

    Loopback is trusted for the bundled local Caddy/nginx path. Public/private
    proxy peers must be listed in ``TRUSTED_PROXY_IPS``; setting
    ``TRUST_PRIVATE_PROXIES=1`` restores broad private-network trust for legacy
    container platforms. Direct clients cannot choose their limiter bucket by
    sending a fake X-Forwarded-For header.
    """
    peer_raw = request.client.host if request.client else ""
    try:
        peer = ipaddress.ip_address(peer_raw)
    except ValueError:
        return "unknown"

    trusted = peer.is_loopback
    trusted_env = os.environ.get("TRUSTED_PROXY_IPS", "").strip()
    if trusted_env:
        try:
            trusted = trusted or any(
                peer in ipaddress.ip_network(item.strip(), strict=False)
                for item in trusted_env.split(",") if item.strip()
            )
        except ValueError:
            pass
    if os.environ.get("TRUST_PRIVATE_PROXIES", "").strip().lower() in {"1", "true", "yes", "on"}:
        trusted = trusted or peer.is_private or peer.is_link_local

    candidate = peer_raw
    if trusted:
        candidate = (
            request.headers.get("cf-connecting-ip", "").strip()
            or request.headers.get("x-forwarded-for", "").split(",")[0].strip()
            or request.headers.get("x-real-ip", "").strip()
            or peer_raw
        )
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return str(peer)


def _keyed_digest(label: str, value: str) -> str:
    from app.auth.jwt import get_secret

    return hmac.new(
        get_secret().encode("utf-8"),
        f"anonymous-abuse:v1:{label}:{value}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def client_source_hash(request: Request) -> str:
    """Pseudonymous network source used only for abuse aggregation.

    The stored value is an HMAC; the raw address never enters the abuse
    database. A /24 IPv4 or /64 IPv6 prefix catches modest IP rotation. Browser
    hints deliberately do not split this strict network bucket because an
    attacker can rotate them. This is a rate-limit signal, not a claim of
    real-world identity.
    """
    ip_text = client_ip(request)
    try:
        ip = ipaddress.ip_address(ip_text)
        network = ipaddress.ip_network(
            f"{ip}/{24 if ip.version == 4 else 64}", strict=False,
        ).with_prefixlen
    except ValueError:
        network = "unknown"
    return _keyed_digest("network-source", network)


def _connect_abuse_db() -> sqlite3.Connection:
    _ABUSE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_ABUSE_DB_PATH), timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS anonymous_source_identities (
               source_hash TEXT NOT NULL,
               browser_hash TEXT NOT NULL,
               user_id_hash TEXT NOT NULL DEFAULT '',
               first_seen REAL NOT NULL,
               last_seen REAL NOT NULL,
               PRIMARY KEY (source_hash, browser_hash)
           )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS anonymous_user_sources (
               user_id_hash TEXT PRIMARY KEY,
               source_hash TEXT NOT NULL,
               updated_at REAL NOT NULL
           )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS anonymous_rate_buckets (
               bucket_hash TEXT PRIMARY KEY,
               window_start REAL NOT NULL,
               count INTEGER NOT NULL
           )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS anonymous_usage_buckets (
               bucket_hash TEXT PRIMARY KEY,
               window_start REAL NOT NULL,
               units INTEGER NOT NULL
           )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS anonymous_risk_events (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               created_at REAL NOT NULL,
               user_hash TEXT NOT NULL,
               source_hash TEXT NOT NULL,
               signal_kind TEXT NOT NULL,
               signal_hash TEXT NOT NULL
           )"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_anon_risk_recent
           ON anonymous_risk_events(created_at, signal_kind, signal_hash)"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS anonymous_cooldowns (
               subject_hash TEXT PRIMARY KEY,
               until_at REAL NOT NULL,
               score INTEGER NOT NULL,
               reason TEXT NOT NULL,
               created_at REAL NOT NULL
           )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS anonymous_run_leases (
               lease_id TEXT PRIMARY KEY,
               user_hash TEXT NOT NULL,
               session_hash TEXT NOT NULL,
               pool_key TEXT NOT NULL DEFAULT 'platform',
               started_at REAL NOT NULL,
               expires_at REAL NOT NULL
           )"""
    )
    lease_columns = {row[1] for row in conn.execute("PRAGMA table_info(anonymous_run_leases)").fetchall()}
    if "pool_key" not in lease_columns:
        conn.execute(
            "ALTER TABLE anonymous_run_leases ADD COLUMN pool_key TEXT NOT NULL DEFAULT 'platform'"
        )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS anonymous_control_state (
               state_key TEXT PRIMARY KEY,
               value_json TEXT NOT NULL,
               updated_at REAL NOT NULL
           )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS anonymous_control_events (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               created_at REAL NOT NULL,
               event_type TEXT NOT NULL,
               user_hash TEXT NOT NULL DEFAULT '',
               source_hash TEXT NOT NULL DEFAULT '',
               score INTEGER NOT NULL DEFAULT 0,
               detail TEXT NOT NULL DEFAULT ''
           )"""
    )
    # Admin-only roster of opaque anonymous subjects that have reached a real
    # control point. This intentionally stores no source/network information.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS anonymous_control_identities (
               user_id TEXT PRIMARY KEY,
               first_seen REAL NOT NULL,
               last_seen REAL NOT NULL
           )"""
    )
    return conn


def _persistent_consume(key: str, units: int, limit: int, window_sec: int) -> Dict[str, Any]:
    """Atomically add units to a durable budget; ``window_sec <= 0`` is lifetime."""
    now = time.time()
    units = max(0, int(units))
    bucket_hash = _keyed_digest("usage-bucket", key)
    conn = _connect_abuse_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT window_start, units FROM anonymous_usage_buckets WHERE bucket_hash = ?",
            (bucket_hash,),
        ).fetchone()
        lifetime = window_sec <= 0
        if not row or (not lifetime and now - float(row[0]) >= window_sec):
            window_start, used_before = now, 0
        else:
            window_start, used_before = float(row[0]), int(row[1])
        used = used_before + units
        conn.execute(
            """INSERT INTO anonymous_usage_buckets(bucket_hash, window_start, units)
               VALUES (?, ?, ?)
               ON CONFLICT(bucket_hash) DO UPDATE SET
                   window_start=excluded.window_start, units=excluded.units""",
            (bucket_hash, window_start, used),
        )
        conn.commit()
        return {
            "allowed": limit <= 0 or used <= limit,
            "used": used,
            "used_before": used_before,
            "limit": limit,
            "resets_in": None if lifetime else max(0, int(window_sec - (now - window_start))),
        }
    finally:
        conn.close()


def _usage_status(key: str, limit: int, window_sec: int) -> Dict[str, Any]:
    now = time.time()
    bucket_hash = _keyed_digest("usage-bucket", key)
    conn = _connect_abuse_db()
    try:
        row = conn.execute(
            "SELECT window_start, units FROM anonymous_usage_buckets WHERE bucket_hash = ?",
            (bucket_hash,),
        ).fetchone()
        lifetime = window_sec <= 0
        expired = bool(row) and not lifetime and now - float(row[0]) >= window_sec
        used = 0 if not row or expired else int(row[1])
        resets = None if lifetime else (
            0 if not row or not used else max(0, int(window_sec - (now - float(row[0]))))
        )
        return {
            "used": used, "limit": limit,
            "remaining": None if limit <= 0 else max(0, limit - used),
            "percent": 0 if limit <= 0 else min(100, round(100 * used / limit)),
            "resets_in": resets,
        }
    finally:
        conn.close()


def _persistent_consume_many(
    items: list[tuple], window_sec: int,
) -> Dict[str, Any]:
    """Reserve budgets atomically; optional item window ``0`` means lifetime."""
    now = time.time()
    conn = _connect_abuse_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        staged = []
        for item in items:
            key, raw_units, raw_limit = item[:3]
            item_window = int(item[3]) if len(item) > 3 else int(window_sec)
            lifetime = item_window <= 0
            units, limit = max(0, int(raw_units)), max(0, int(raw_limit))
            bucket_hash = _keyed_digest("usage-bucket", key)
            row = conn.execute(
                "SELECT window_start, units FROM anonymous_usage_buckets WHERE bucket_hash=?",
                (bucket_hash,),
            ).fetchone()
            if not row or (not lifetime and now - float(row[0]) >= item_window):
                window_start, used_before = now, 0
            else:
                window_start, used_before = float(row[0]), int(row[1])
            used = used_before + units
            result = {
                "allowed": limit <= 0 or used <= limit,
                "used": used, "used_before": used_before, "limit": limit,
                "resets_in": None if lifetime else max(0, int(item_window - (now - window_start))),
            }
            if not result["allowed"]:
                conn.rollback()
                return {"allowed": False, "key": key, **result}
            staged.append((bucket_hash, window_start, used, key, result))
        for bucket_hash, window_start, used, _key, _result in staged:
            conn.execute(
                """INSERT INTO anonymous_usage_buckets(bucket_hash,window_start,units)
                   VALUES (?,?,?) ON CONFLICT(bucket_hash) DO UPDATE SET
                   window_start=excluded.window_start, units=excluded.units""",
                (bucket_hash, window_start, used),
            )
        conn.commit()
        return {"allowed": True, "results": {key: result for *_x, key, result in staged}}
    finally:
        conn.close()


def _persistent_hit(key: str, limit: int, window_sec: int) -> bool:
    if limit <= 0:
        return True
    now = time.time()
    bucket_hash = _keyed_digest("bucket", key)
    conn = _connect_abuse_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT window_start, count FROM anonymous_rate_buckets WHERE bucket_hash = ?",
            (bucket_hash,),
        ).fetchone()
        if not row or now - float(row[0]) >= window_sec:
            window_start, count = now, 1
        else:
            window_start, count = float(row[0]), int(row[1]) + 1
        conn.execute(
            """INSERT INTO anonymous_rate_buckets(bucket_hash, window_start, count)
               VALUES (?, ?, ?)
               ON CONFLICT(bucket_hash) DO UPDATE SET
                   window_start = excluded.window_start,
                   count = excluded.count""",
            (bucket_hash, window_start, count),
        )
        conn.commit()
        return count <= limit
    finally:
        conn.close()


def _persistent_bucket_status(key: str, window_sec: int) -> Dict[str, Any]:
    """Read a durable bucket without incrementing it."""
    now = time.time()
    bucket_hash = _keyed_digest("bucket", key)
    conn = _connect_abuse_db()
    try:
        row = conn.execute(
            "SELECT window_start, count FROM anonymous_rate_buckets WHERE bucket_hash = ?",
            (bucket_hash,),
        ).fetchone()
        if not row or now - float(row[0]) >= window_sec:
            return {"count": 0, "window_start": None, "resets_in": 0}
        return {
            "count": int(row[1]),
            "window_start": float(row[0]),
            "resets_in": max(0, int(window_sec - (now - float(row[0])))),
        }
    finally:
        conn.close()


def _persistent_reset(key: str) -> None:
    bucket_hash = _keyed_digest("bucket", key)
    conn = _connect_abuse_db()
    try:
        conn.execute(
            "DELETE FROM anonymous_rate_buckets WHERE bucket_hash = ?",
            (bucket_hash,),
        )
        conn.commit()
    finally:
        conn.close()


async def begin_login_attempt(request: Request, account: str) -> str:
    """Reserve a bounded login attempt and return its opaque failure-bucket key."""
    window = _env_int("WEBAGENT_LOGIN_WINDOW", 300)
    account_max = _env_int("WEBAGENT_LOGIN_ACCOUNT_SOURCE_MAX", 5)
    source_max = _env_int("WEBAGENT_LOGIN_SOURCE_MAX", 20)
    global_max = _env_int("WEBAGENT_LOGIN_GLOBAL_MAX", 100)
    source_hash = client_source_hash(request)
    account_hash = _keyed_digest("login-account", (account or "").strip().casefold())
    failure_key = f"login-failure:{source_hash}:{account_hash}"

    status = await asyncio.to_thread(_persistent_bucket_status, failure_key, window)
    if account_max > 0 and status["count"] >= account_max:
        raise HTTPException(
            status_code=429,
            detail="Too many sign-in attempts. Please try again later.",
            headers={"Retry-After": str(max(1, status["resets_in"]))},
        )
    source_ok = await asyncio.to_thread(
        _persistent_hit, f"login-source:{source_hash}", source_max, window,
    )
    global_ok = await asyncio.to_thread(
        _persistent_hit, "login-global", global_max, window,
    )
    if not source_ok or not global_ok:
        raise HTTPException(
            status_code=429,
            detail="Too many sign-in attempts. Please try again later.",
            headers={"Retry-After": str(window)},
        )
    return failure_key


async def record_login_failure(failure_key: str) -> None:
    window = _env_int("WEBAGENT_LOGIN_WINDOW", 300)
    account_max = _env_int("WEBAGENT_LOGIN_ACCOUNT_SOURCE_MAX", 5)
    await asyncio.to_thread(_persistent_hit, failure_key, account_max, window)


async def clear_login_failures(failure_key: str) -> None:
    await asyncio.to_thread(_persistent_reset, failure_key)


def _reserve_source_identity(
    source_hash: str, browser_hash: str, limit: int, window_sec: int,
) -> Dict[str, Any]:
    if limit <= 0:
        return {"allowed": True, "count": 0, "is_new": False}
    now = time.time()
    cutoff = now - window_sec
    conn = _connect_abuse_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        # Expired identities must not become permanent allow-list entries, and
        # opportunistic pruning keeps hostile browser-id churn disk-bounded.
        conn.execute(
            "DELETE FROM anonymous_source_identities WHERE last_seen < ?",
            (cutoff,),
        )
        existing = conn.execute(
            """SELECT 1 FROM anonymous_source_identities
               WHERE source_hash = ? AND browser_hash = ?""",
            (source_hash, browser_hash),
        ).fetchone()
        count = int(conn.execute(
            """SELECT COUNT(*) FROM anonymous_source_identities
               WHERE source_hash = ? AND last_seen >= ?""",
            (source_hash, cutoff),
        ).fetchone()[0])
        if existing:
            conn.execute(
                """UPDATE anonymous_source_identities SET last_seen = ?
                   WHERE source_hash = ? AND browser_hash = ?""",
                (now, source_hash, browser_hash),
            )
            conn.commit()
            return {"allowed": True, "count": max(1, count), "is_new": False}
        if count >= limit:
            conn.rollback()
            return {"allowed": False, "count": count, "is_new": True}
        conn.execute(
            """INSERT INTO anonymous_source_identities
               (source_hash, browser_hash, first_seen, last_seen)
               VALUES (?, ?, ?, ?)""",
            (source_hash, browser_hash, now, now),
        )
        conn.commit()
        return {"allowed": True, "count": count + 1, "is_new": True}
    finally:
        conn.close()


def _bind_source_identity(source_hash: str, browser_hash: str, user_id: str) -> None:
    conn = _connect_abuse_db()
    try:
        now = time.time()
        user_hash = _keyed_digest("user", user_id)
        conn.execute(
            """UPDATE anonymous_source_identities SET user_id_hash = ?, last_seen = ?
               WHERE source_hash = ? AND browser_hash = ?""",
            (user_hash, now, source_hash, browser_hash),
        )
        # Identity-farming rows expire on their short rate window. Lifetime
        # network-credit attribution needs a separate durable pseudonymous
        # association; neither table stores the raw IP/network prefix.
        conn.execute(
            """INSERT INTO anonymous_user_sources(user_id_hash,source_hash,updated_at)
               VALUES (?,?,?) ON CONFLICT(user_id_hash) DO UPDATE SET
               source_hash=excluded.source_hash, updated_at=excluded.updated_at""",
            (user_hash, source_hash, now),
        )
        conn.commit()
    finally:
        conn.close()


def _release_source_identity(source_hash: str, browser_hash: str) -> None:
    """Roll back a just-created reservation when a later global gate refuses it."""
    conn = _connect_abuse_db()
    try:
        conn.execute(
            """DELETE FROM anonymous_source_identities
               WHERE source_hash=? AND browser_hash=? AND user_id_hash=''""",
            (source_hash, browser_hash),
        )
        conn.commit()
    finally:
        conn.close()


def _record_source_event(level: str, message: str) -> None:
    try:
        from app.agent.diagnostics import record
        record(level, "auth", message, persist=True)
    except Exception:
        pass


def hit(key: str, limit: int, window_sec: int) -> bool:
    """Register one event for ``key``. Returns True if it is WITHIN the limit,
    False if the limit is now exceeded for the current window."""
    if limit <= 0:
        return True  # 0/negative disables the check
    now = time.monotonic()
    with _LOCK:
        if len(_BUCKETS) > _MAX_KEYS:
            cutoff = now - window_sec
            for k in [k for k, (ws, _) in _BUCKETS.items() if ws < cutoff]:
                _BUCKETS.pop(k, None)
        ws, count = _BUCKETS.get(key, (now, 0))
        if now - ws >= window_sec:
            ws, count = now, 0        # window elapsed → reset
        count += 1
        _BUCKETS[key] = (ws, count)
        return count <= limit


def enforce(key: str, limit: int, window_sec: int, detail: str = "Too many requests. Please slow down.") -> None:
    """Raise HTTP 429 when ``key`` exceeds ``limit`` events per ``window_sec``."""
    if not hit(key, limit, window_sec):
        raise HTTPException(status_code=429, detail=detail,
                            headers={"Retry-After": str(window_sec)})


# ── Public-chat abuse limits (env-overridable, app-settings.json-backed) ─────
def anon_session_limits() -> Tuple[int, int]:
    """(max new anonymous sessions, per window seconds) per client IP.
    Reads from app-settings.json via app.admin.settings, with env-var override."""
    try:
        from app.admin.settings import get_anon_session_max, get_anon_session_window
        return get_anon_session_max(), get_anon_session_window()
    except Exception:
        return _env_int("WEBAGENT_ANON_SESSION_MAX", 10), _env_int("WEBAGENT_ANON_SESSION_WINDOW", 60)


def anon_identity_limits() -> Tuple[int, int]:
    """(max distinct browser identities, per window) per source fingerprint."""
    try:
        from app.admin.settings import get_anon_identity_max, get_anon_identity_window
        return get_anon_identity_max(), get_anon_identity_window()
    except Exception:
        return _env_int("WEBAGENT_ANON_IDENTITY_MAX", 5), _env_int("WEBAGENT_ANON_IDENTITY_WINDOW", 86400)


def anon_global_session_limits() -> Tuple[int, int]:
    """App-wide anonymous identity-mint circuit breaker."""
    try:
        from app.admin.settings import (
            get_anon_global_session_max,
            get_anon_global_session_window,
        )
        return get_anon_global_session_max(), get_anon_global_session_window()
    except Exception:
        return _env_int("WEBAGENT_ANON_GLOBAL_SESSION_MAX", 25), _env_int("WEBAGENT_ANON_GLOBAL_SESSION_WINDOW", 3600)


def public_registration_limits() -> Tuple[int, int, int]:
    """(per-IP max, app-wide max, window) for public account creation."""
    try:
        from app.admin.settings import (
            get_public_registration_global_max,
            get_public_registration_ip_max,
            get_public_registration_window,
        )
        return (
            get_public_registration_ip_max(),
            get_public_registration_global_max(),
            get_public_registration_window(),
        )
    except Exception:
        return (
            _env_int("WEBAGENT_PUBLIC_REGISTRATION_IP_MAX", 5),
            _env_int("WEBAGENT_PUBLIC_REGISTRATION_GLOBAL_MAX", 100),
            _env_int("WEBAGENT_PUBLIC_REGISTRATION_WINDOW", 3600),
        )


async def enforce_public_registration(request: Request) -> None:
    """Protect open registration from local and distributed account farming."""
    ip_max, global_max, window = public_registration_limits()
    ip = client_ip(request)
    if not await asyncio.to_thread(
        _persistent_hit, f"public-registration-ip:{ip}", ip_max, window,
    ):
        raise HTTPException(
            status_code=429,
            detail={
                "code": "rate_limited",
                "scope": "public_registration_network",
                "message": "Too many accounts were created from this network. Please try later.",
                "limit": ip_max,
                "window_seconds": window,
            },
            headers={"Retry-After": str(window)},
        )
    if not await asyncio.to_thread(
        _persistent_hit, "public-registration-global", global_max, window,
    ):
        _record_source_event(
            "warning", "App-wide public registration circuit breaker activated.",
        )
        raise HTTPException(
            status_code=429,
            detail={
                "code": "rate_limited",
                "scope": "public_registration_global",
                "message": "Account creation is temporarily at capacity. Please try later.",
                "limit": global_max,
                "window_seconds": window,
            },
            headers={"Retry-After": str(window)},
        )


async def enforce_anon_session_creation(request: Request, browser_id: str) -> Dict[str, Any]:
    """Gate every endpoint that can mint an anonymous JWT.

    Two independent controls apply: a fast request bucket for the exact client
    IP and a durable distinct-browser count for the pseudonymous source. The
    latter detects local-storage/browser-id rotation across workers and restarts.
    """
    if not anonymous_chat_enabled():
        raise HTTPException(
            status_code=429,
            detail=_anonymous_unavailable_detail("anonymous_chat_disabled"),
            headers={"Retry-After": "3600"},
        )
    session_max, session_window = anon_session_limits()
    ip = client_ip(request)
    source_hash = client_source_hash(request)
    browser_hash = _keyed_digest("browser", browser_id)

    enforce(
        f"anon-session-ip:{ip}", session_max, session_window,
        detail={
            "code": "rate_limited",
            "scope": "anonymous_session_network",
            "message": "Too many anonymous sessions started from your network.",
            "limit": session_max,
            "window_seconds": session_window,
        },
    )
    if not await asyncio.to_thread(
        _persistent_hit, f"anon-session-ip:{ip}", session_max, session_window,
    ):
        raise HTTPException(
            status_code=429,
            detail={
                "code": "rate_limited",
                "scope": "anonymous_session_network",
                "message": "Too many anonymous sessions started from your network.",
                "limit": session_max,
                "window_seconds": session_window,
            },
            headers={"Retry-After": str(session_window)},
        )

    identity_max, identity_window = anon_identity_limits()
    reservation = await asyncio.to_thread(
        _reserve_source_identity,
        source_hash,
        browser_hash,
        identity_max,
        identity_window,
    )
    if not reservation["allowed"]:
        _record_source_event(
            "warning",
            f"Blocked anonymous identity farming from source {source_hash[:12]} "
            f"after {reservation['count']} distinct browser identities.",
        )
        raise HTTPException(
            status_code=429,
            detail={
                "code": "rate_limited",
                "scope": "anonymous_identity_source",
                "message": "Too many anonymous identities were created from this source.",
                "limit": identity_max,
                "window_seconds": identity_window,
            },
            headers={"Retry-After": str(identity_window)},
        )
    # A refresh of an existing browser identity must not consume the global
    # identity-mint breaker. This matters because admission JWTs are short-lived.
    if reservation["is_new"]:
        global_max, global_window = anon_global_session_limits()
        if not await asyncio.to_thread(
            _persistent_hit, "anon-session-global", global_max, global_window,
        ):
            await asyncio.to_thread(
                _release_source_identity, source_hash, browser_hash,
            )
            _record_source_event(
                "warning",
                "App-wide anonymous identity-mint circuit breaker activated.",
            )
            raise HTTPException(
                status_code=429,
                detail={
                    "code": "rate_limited",
                    "scope": "anonymous_global_session",
                    "message": "Anonymous access is temporarily at capacity. Register and sign in to continue.",
                    "limit": global_max,
                    "window_seconds": global_window,
                },
                headers={"Retry-After": str(global_window)},
            )
    return {
        "source_hash": source_hash,
        "browser_hash": browser_hash,
        "count": reservation["count"],
        "is_new": reservation["is_new"],
    }


async def bind_anon_session_identity(reservation: Dict[str, Any], user_id: str) -> None:
    """Attach the minted anon id to its privacy-safe source record and alert."""
    await asyncio.to_thread(
        _bind_source_identity,
        reservation["source_hash"],
        reservation["browser_hash"],
        user_id,
    )
    if reservation.get("is_new") and int(reservation.get("count") or 0) >= 2:
        _record_source_event(
            "warning",
            f"Anonymous source {reservation['source_hash'][:12]} is now linked to "
            f"{reservation['count']} distinct browser identities.",
        )


def anon_chat_limits() -> Tuple[int, int]:
    """(max messages, per window seconds) for one anonymous identity.
    Reads from app-settings.json via app.admin.settings, with env-var override."""
    try:
        from app.admin.settings import get_anon_chat_max, get_anon_chat_window
        return get_anon_chat_max(), get_anon_chat_window()
    except Exception:
        return _env_int("WEBAGENT_ANON_CHAT_MAX", 5), _env_int("WEBAGENT_ANON_CHAT_WINDOW", 300)


def anon_chat_ip_limits() -> Tuple[int, int]:
    """(max messages, per window seconds) across all anon chat from one IP —
    catches a single IP cycling through many minted tokens.
    Reads from app-settings.json via app.admin.settings, with env-var override."""
    try:
        from app.admin.settings import get_anon_chat_ip_max, get_anon_chat_ip_window
        return get_anon_chat_ip_max(), get_anon_chat_ip_window()
    except Exception:
        return _env_int("WEBAGENT_ANON_CHAT_IP_MAX", 10), _env_int("WEBAGENT_ANON_CHAT_IP_WINDOW", 300)


def anon_daily_chat_limits() -> Tuple[int, int]:
    """Per-identity daily allowance that transitions heavy guests to signup."""
    try:
        from app.admin.settings import get_anon_daily_chat_max, get_anon_daily_chat_window
        return get_anon_daily_chat_max(), get_anon_daily_chat_window()
    except Exception:
        return _env_int("WEBAGENT_ANON_DAILY_CHAT_MAX", 10), _env_int("WEBAGENT_ANON_DAILY_CHAT_WINDOW", 86400)


def anon_global_chat_limits() -> Tuple[int, int]:
    """App-wide anonymous chat-admission circuit breaker."""
    try:
        from app.admin.settings import get_anon_global_chat_max, get_anon_global_chat_window
        return get_anon_global_chat_max(), get_anon_global_chat_window()
    except Exception:
        return _env_int("WEBAGENT_ANON_GLOBAL_CHAT_MAX", 50), _env_int("WEBAGENT_ANON_GLOBAL_CHAT_WINDOW", 300)


def anonymous_chat_enabled() -> bool:
    try:
        from app.admin.settings import get_anonymous_chat_enabled
        return get_anonymous_chat_enabled()
    except Exception:
        return os.environ.get("WEBAGENT_ANONYMOUS_CHAT_ENABLED", "true").strip().lower() in {
            "1", "true", "yes", "on",
        }


def anon_budget_limits() -> Tuple[int, int]:
    """Hard app-wide admitted-turn budget for anonymous chat."""
    try:
        from app.admin.settings import get_anon_budget_max, get_anon_budget_window
        return get_anon_budget_max(), get_anon_budget_window()
    except Exception:
        return _env_int("WEBAGENT_ANON_BUDGET_MAX", 100), _env_int("WEBAGENT_ANON_BUDGET_WINDOW", 86400)


def _anonymous_unavailable_detail(scope: str, *, limit: int = 0, window: int = 0) -> Dict[str, Any]:
    detail = {
        "code": "registration_required",
        "scope": scope,
        "message": "Anonymous chat is unavailable. Register and sign in to continue.",
    }
    if limit > 0:
        detail["limit"] = limit
    if window > 0:
        detail["window_seconds"] = window
    return detail


def anonymous_budget_status() -> Dict[str, Any]:
    """Current operator-facing status for the anonymous chat policy."""
    maximum, window = anon_budget_limits()
    bucket = _persistent_bucket_status("anon-chat-budget", window)
    used = min(maximum, int(bucket["count"])) if maximum > 0 else int(bucket["count"])
    return {
        "enabled": anonymous_chat_enabled(),
        "budget_max": maximum,
        "budget_window": window,
        "budget_used": used,
        "budget_remaining": max(0, maximum - used) if maximum > 0 else None,
        "budget_resets_in": int(bucket["resets_in"]),
        "budget_exhausted": maximum > 0 and used >= maximum,
    }


def anon_native_controls() -> Dict[str, Any]:
    try:
        from app.admin.settings import get_anon_native_controls
        return get_anon_native_controls()
    except Exception:
        return {
            "estimated_output_tokens": 4096,
            "estimated_cost_per_1k_microusd": 10000,
            "token_user_max": 100000,
            "token_source_max": 100000,
            "token_global_max": 1000000,
            "cost_user_microusd_max": 250000,
            "cost_source_microusd_max": 250000,
            "cost_global_microusd_max": 2500000,
            "spend_window": 86400,
            "max_concurrent_runs": 2,
            "run_lease_seconds": 900,
            "risk_delay_score": 2,
            "risk_cooldown_score": 4,
            "risk_delay_ms": 500,
            "risk_cooldown_seconds": 900,
            "auto_close_enabled": True,
            "auto_close_seconds": 900,
            "error_max": 10,
            "error_window": 300,
        }


def _source_for_user(user_id: str) -> str:
    user_hash = _keyed_digest("user", user_id)
    conn = _connect_abuse_db()
    try:
        row = conn.execute(
            """SELECT source_hash FROM anonymous_user_sources
               WHERE user_id_hash = ?""",
            (user_hash,),
        ).fetchone()
        if row:
            return str(row[0])
        # Compatibility for guests minted before the durable accounting link
        # existed. A later credential refresh/backfill binds them permanently.
        row = conn.execute(
            """SELECT source_hash FROM anonymous_source_identities
               WHERE user_id_hash = ? ORDER BY last_seen DESC LIMIT 1""",
            (user_hash,),
        ).fetchone()
        return str(row[0]) if row else "unknown"
    finally:
        conn.close()


def _control_event(
    event_type: str, *, user_id: str = "", source_hash: str = "",
    score: int = 0, detail: str = "",
) -> None:
    conn = _connect_abuse_db()
    try:
        conn.execute(
            """INSERT INTO anonymous_control_events
               (created_at,event_type,user_hash,source_hash,score,detail)
               VALUES (?,?,?,?,?,?)""",
            (
                time.time(), event_type,
                _keyed_digest("user", user_id) if user_id else "",
                source_hash, int(score), str(detail)[:500],
            ),
        )
        conn.execute(
            "DELETE FROM anonymous_control_events WHERE created_at < ?",
            (time.time() - 7 * 86400,),
        )
        conn.commit()
    finally:
        conn.close()


def _auto_close(reason: str) -> None:
    controls = anon_native_controls()
    if not controls.get("auto_close_enabled"):
        return
    until_at = time.time() + max(60, int(controls["auto_close_seconds"]))
    conn = _connect_abuse_db()
    try:
        conn.execute(
            """INSERT INTO anonymous_control_state(state_key,value_json,updated_at)
               VALUES ('auto_close',?,?)
               ON CONFLICT(state_key) DO UPDATE SET
                   value_json=excluded.value_json, updated_at=excluded.updated_at""",
            (json.dumps({"until_at": until_at, "reason": reason}), time.time()),
        )
        conn.commit()
    finally:
        conn.close()
    _control_event("auto_close", detail=reason)
    _record_source_event("warning", f"Anonymous chat auto-closed: {reason}")


def anonymous_auto_close_status() -> Dict[str, Any]:
    conn = _connect_abuse_db()
    try:
        row = conn.execute(
            "SELECT value_json FROM anonymous_control_state WHERE state_key='auto_close'",
        ).fetchone()
    finally:
        conn.close()
    try:
        value = json.loads(row[0]) if row else {}
    except Exception:
        value = {}
    until_at = float(value.get("until_at") or 0)
    return {
        "active": until_at > time.time(),
        "until_at": until_at or None,
        "remaining_seconds": max(0, int(until_at - time.time())),
        "reason": str(value.get("reason") or ""),
    }


def clear_anonymous_auto_close() -> None:
    conn = _connect_abuse_db()
    try:
        conn.execute("DELETE FROM anonymous_control_state WHERE state_key='auto_close'")
        conn.commit()
    finally:
        conn.close()
    _control_event("auto_close_cleared")


def _cooldown_status(user_id: str, source_hash: str) -> Dict[str, Any]:
    subjects = [_keyed_digest("cooldown-user", user_id)]
    if source_hash != "unknown":
        subjects.append(_keyed_digest("cooldown-source", source_hash))
    conn = _connect_abuse_db()
    try:
        placeholders = ",".join("?" for _ in subjects)
        rows = conn.execute(
            f"SELECT until_at, score, reason FROM anonymous_cooldowns WHERE subject_hash IN ({placeholders}) ORDER BY until_at DESC",
            subjects,
        ).fetchall()
    finally:
        conn.close()
    active = next((row for row in rows if float(row[0]) > time.time()), None)
    return {
        "active": bool(active),
        "until_at": float(active[0]) if active else None,
        "remaining_seconds": max(0, int(float(active[0]) - time.time())) if active else 0,
        "score": int(active[1]) if active else 0,
        "reason": str(active[2]) if active else "",
    }


def set_anonymous_cooldown(user_id: str, seconds: int, reason: str, score: int = 10) -> None:
    source_hash = _source_for_user(user_id)
    now = time.time()
    until = now + max(1, int(seconds))
    conn = _connect_abuse_db()
    try:
        subjects = [_keyed_digest("cooldown-user", user_id)]
        if source_hash != "unknown":
            subjects.append(_keyed_digest("cooldown-source", source_hash))
        for subject in subjects:
            conn.execute(
                """INSERT INTO anonymous_cooldowns(subject_hash,until_at,score,reason,created_at)
                   VALUES (?,?,?,?,?) ON CONFLICT(subject_hash) DO UPDATE SET
                   until_at=excluded.until_at, score=excluded.score,
                   reason=excluded.reason, created_at=excluded.created_at""",
                (subject, until, int(score), str(reason)[:200], now),
            )
        conn.commit()
    finally:
        conn.close()
    _control_event("cooldown", user_id=user_id, source_hash=source_hash, score=score, detail=reason)


def clear_anonymous_cooldown(user_id: str) -> None:
    source_hash = _source_for_user(user_id)
    subjects = [_keyed_digest("cooldown-user", user_id)]
    if source_hash != "unknown":
        subjects.append(_keyed_digest("cooldown-source", source_hash))
    conn = _connect_abuse_db()
    try:
        placeholders = ",".join("?" for _ in subjects)
        conn.execute(
            f"DELETE FROM anonymous_cooldowns WHERE subject_hash IN ({placeholders})",
            subjects,
        )
        conn.commit()
    finally:
        conn.close()
    _control_event("cooldown_cleared", user_id=user_id, source_hash=source_hash)


def _record_risk(user_id: str, source_hash: str, prompt: str, admission_id: str, user_agent: str) -> Dict[str, Any]:
    """Record privacy-safe coordination signals and return a bounded score."""
    now = time.time()
    user_hash = _keyed_digest("risk-user", user_id)
    prompt_hash = _keyed_digest("prompt", " ".join(prompt.lower().split())[:4000])
    admission_hash = _keyed_digest("admission", admission_id or "missing")
    ua_hash = _keyed_digest("user-agent", user_agent[:500] or "missing")
    conn = _connect_abuse_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cutoff = now - 300
        conn.execute("DELETE FROM anonymous_risk_events WHERE created_at < ?", (now - 86400,))
        def distinct(kind: str, signal: str, field: str = "user_hash") -> int:
            return int(conn.execute(
                f"SELECT COUNT(DISTINCT {field}) FROM anonymous_risk_events "
                "WHERE created_at >= ? AND signal_kind=? AND signal_hash=?",
                (cutoff, kind, signal),
            ).fetchone()[0])
        prompt_users = distinct("prompt", prompt_hash)
        admission_other_sources = int(conn.execute(
            """SELECT COUNT(DISTINCT source_hash) FROM anonymous_risk_events
               WHERE created_at >= ? AND signal_kind='admission'
               AND signal_hash=? AND source_hash<>?""",
            (cutoff, admission_hash, source_hash),
        ).fetchone()[0])
        ua_sources = distinct("user_agent", ua_hash, "source_hash")
        burst = int(conn.execute(
            "SELECT COUNT(*) FROM anonymous_risk_events WHERE created_at >= ? AND source_hash=? AND signal_kind='timing'",
            (now - 10, source_hash),
        ).fetchone()[0])
        for kind, signal in (
            ("prompt", prompt_hash), ("admission", admission_hash),
            ("user_agent", ua_hash), ("timing", _keyed_digest("timing", source_hash)),
        ):
            conn.execute(
                """INSERT INTO anonymous_risk_events
                   (created_at,user_hash,source_hash,signal_kind,signal_hash)
                   VALUES (?,?,?,?,?)""",
                (now, user_hash, source_hash, kind, signal),
            )
        conn.commit()
    finally:
        conn.close()
    score, reasons = 0, []
    if prompt_users >= 3:
        score += 3; reasons.append("repeated_prompt")
    if admission_other_sources >= 1:
        score += 4; reasons.append("admission_reuse")
    if ua_sources >= 9:
        score += 1; reasons.append("distributed_user_agent")
    if burst >= 4:
        score += 2; reasons.append("timing_burst")
    return {"score": score, "reasons": reasons}


def _estimate_admission(message: str, controls: Dict[str, Any]) -> Tuple[int, int]:
    prompt_tokens = max(1, (len(message or "") + 3) // 4)
    estimated_tokens = prompt_tokens + int(controls["estimated_output_tokens"])
    cost = (estimated_tokens * int(controls["estimated_cost_per_1k_microusd"]) + 999) // 1000
    return estimated_tokens, cost


def _consume_native_budgets(user_id: str, source_hash: str, message: str) -> Dict[str, Any]:
    controls = anon_native_controls()
    tokens, cost = _estimate_admission(message, controls)
    window = int(controls["spend_window"])
    # Provider-reported cost is authoritative once it exists. Stop admitting
    # more work after the guest, shared network, or global actual-cost pool has
    # reached its allowance, even if conservative admission reservations have
    # not yet consumed their parallel estimated-cost bucket.
    actual_checks = [
        ("cost_user", f"actual-cost-user:{user_id}", controls["cost_user_microusd_max"], window),
        # A network gets one anonymous credit grant. It never replenishes.
        ("cost_source", f"actual-cost-source:{source_hash}", controls["cost_source_microusd_max"], 0),
        ("cost_global", "actual-cost-global", controls["cost_global_microusd_max"], window),
    ]
    for scope, key, limit, item_window in actual_checks:
        status = _usage_status(key, int(limit), item_window)
        if int(limit) > 0 and int(status["used"]) >= int(limit):
            if scope.endswith("global"):
                _auto_close(f"actual {scope} budget exhausted")
            _control_event(
                "budget_block", user_id=user_id, source_hash=source_hash,
                detail=f"actual_{scope}",
            )
            return {
                "allowed": False, "scope": scope, "tokens": tokens,
                "cost_microusd": cost, **status,
            }
    checks = [
        ("tokens_user", f"estimated-token-user:{user_id}", tokens, controls["token_user_max"], window),
        ("tokens_source", f"estimated-token-source:{source_hash}", tokens, controls["token_source_max"], window),
        ("tokens_global", "estimated-token-global", tokens, controls["token_global_max"], window),
        ("cost_user", f"estimated-cost-user:{user_id}", cost, controls["cost_user_microusd_max"], window),
        ("cost_source", f"estimated-cost-source:{source_hash}", cost, controls["cost_source_microusd_max"], 0),
        ("cost_global", "estimated-cost-global", cost, controls["cost_global_microusd_max"], window),
    ]
    reservation = _persistent_consume_many(
        [(key, units, int(limit), item_window) for _scope, key, units, limit, item_window in checks], window,
    )
    if not reservation["allowed"]:
        failed_key = reservation.pop("key")
        scope = next(scope for scope, key, _units, _limit, _window in checks if key == failed_key)
        if scope.endswith("global"):
            _auto_close(f"{scope} budget exhausted")
        _control_event("budget_block", user_id=user_id, source_hash=source_hash, detail=scope)
        return {
            "allowed": False, "scope": scope, "tokens": tokens,
            "cost_microusd": cost, **reservation,
        }
    return {
        "allowed": True, "tokens": tokens, "cost_microusd": cost,
        "checks": reservation["results"],
    }


def begin_anonymous_run(
    user_id: str, session_id: str, pool_key: str = "platform",
    maximum: int | None = None,
) -> str | None:
    if not str(user_id or "").startswith("anon_"):
        return None
    controls = anon_native_controls()
    maximum = int(controls["max_concurrent_runs"] if maximum is None else maximum)
    if maximum <= 0:
        return None
    now = time.time()
    lease_id = secrets.token_urlsafe(18)
    conn = _connect_abuse_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM anonymous_run_leases WHERE expires_at <= ?", (now,))
        active = int(conn.execute(
            "SELECT COUNT(*) FROM anonymous_run_leases WHERE pool_key=?", (pool_key,),
        ).fetchone()[0])
        if active >= maximum:
            conn.rollback()
            _control_event("concurrency_block", user_id=user_id, detail=f"active={active}")
            raise RuntimeError("anonymous_concurrency_limit")
        conn.execute(
            """INSERT INTO anonymous_run_leases
               (lease_id,user_hash,session_hash,pool_key,started_at,expires_at)
               VALUES (?,?,?,?,?,?)""",
            (
                lease_id, _keyed_digest("run-user", user_id),
                _keyed_digest("run-session", session_id), pool_key, now,
                now + int(controls["run_lease_seconds"]),
            ),
        )
        conn.commit()
        return lease_id
    finally:
        conn.close()


def end_anonymous_run(lease_id: str | None) -> None:
    if not lease_id:
        return
    conn = _connect_abuse_db()
    try:
        conn.execute("DELETE FROM anonymous_run_leases WHERE lease_id=?", (lease_id,))
        conn.commit()
    finally:
        conn.close()


def is_anonymous_control_subject(user_id: str) -> bool:
    """Whether an opaque subject is governed by anonymous native controls."""
    if str(user_id or "").startswith("anon_"):
        return True
    try:
        from app.agent.member_workspace import is_agent_member_subject
        return is_agent_member_subject(str(user_id or ""))
    except Exception:
        return False


def _register_anonymous_control_identity(user_id: str) -> None:
    """Make a real anonymous control subject available to admin telemetry."""
    if not is_anonymous_control_subject(user_id):
        return
    now = time.time()
    conn = _connect_abuse_db()
    try:
        conn.execute(
            """INSERT INTO anonymous_control_identities(user_id,first_seen,last_seen)
               VALUES (?,?,?) ON CONFLICT(user_id) DO UPDATE SET last_seen=excluded.last_seen""",
            (str(user_id), now, now),
        )
        conn.commit()
    finally:
        conn.close()


def anonymous_control_identity_ids() -> list[str]:
    """Opaque anonymous subjects known to the native-control system."""
    conn = _connect_abuse_db()
    try:
        return [str(row[0]) for row in conn.execute(
            "SELECT user_id FROM anonymous_control_identities ORDER BY last_seen DESC"
        ).fetchall()]
    finally:
        conn.close()


def anonymous_control_identity_metadata() -> Dict[str, Dict[str, str]]:
    """Admin-only first/last activity timestamps for observed anonymous subjects."""
    conn = _connect_abuse_db()
    try:
        rows = conn.execute(
            "SELECT user_id,first_seen,last_seen FROM anonymous_control_identities"
        ).fetchall()
        return {
            str(user_id): {
                "first_activity_at": datetime.fromtimestamp(float(first_seen), timezone.utc).isoformat(),
                "last_activity_at": datetime.fromtimestamp(float(last_seen), timezone.utc).isoformat(),
            }
            for user_id, first_seen, last_seen in rows
        }
    finally:
        conn.close()


def record_anonymous_actual_usage(
    user_id: str, input_tokens: int, output_tokens: int, cost_usd: float,
) -> Dict[str, Any]:
    if not is_anonymous_control_subject(user_id):
        return {"exhausted": False}
    _register_anonymous_control_identity(user_id)
    controls = anon_native_controls()
    window = int(controls["spend_window"])
    source = _source_for_user(user_id)
    tokens = max(0, int(input_tokens or 0) + int(output_tokens or 0))
    cost = max(0, int(round(float(cost_usd or 0) * 1_000_000)))
    for key, units, item_window in (
        (f"actual-token-user:{user_id}", tokens, window),
        (f"actual-token-source:{source}", tokens, window),
        ("actual-token-global", tokens, window),
        (f"actual-cost-user:{user_id}", cost, window),
        (f"actual-cost-source:{source}", cost, 0),
        ("actual-cost-global", cost, window),
    ):
        _persistent_consume(key, units, 0, item_window)
    checks = [
        ("cost_global", "actual-cost-global", controls["cost_global_microusd_max"], window),
        ("cost_source", f"actual-cost-source:{source}", controls["cost_source_microusd_max"], 0),
        ("cost_user", f"actual-cost-user:{user_id}", controls["cost_user_microusd_max"], window),
    ]
    for scope, key, limit, item_window in checks:
        status = _usage_status(key, int(limit), item_window)
        if int(limit) > 0 and int(status["used"]) >= int(limit):
            if scope == "cost_global":
                _auto_close("actual cost_global budget exhausted")
            _control_event(
                "budget_exhausted", user_id=user_id, source_hash=source,
                detail=f"actual_{scope}",
            )
            return {"exhausted": True, "scope": scope, **status}
    return {"exhausted": False}


def record_anonymous_run_error(user_id: str, detail: str = "") -> None:
    if not is_anonymous_control_subject(user_id):
        return
    _register_anonymous_control_identity(user_id)
    controls = anon_native_controls()
    maximum = int(controls["error_max"])
    window = int(controls["error_window"])
    allowed = _persistent_hit("anon-run-errors-global", maximum, window)
    _control_event("run_error", user_id=user_id, detail=detail[:200])
    if not allowed:
        _auto_close("anonymous model error threshold exceeded")


def anonymous_control_snapshot(user_ids: list[str] | None = None) -> Dict[str, Any]:
    controls = anon_native_controls()
    window = int(controls["spend_window"])
    conn = _connect_abuse_db()
    try:
        conn.execute("DELETE FROM anonymous_run_leases WHERE expires_at <= ?", (time.time(),))
        active_runs = int(conn.execute("SELECT COUNT(*) FROM anonymous_run_leases").fetchone()[0])
        events = [
            {
                "created_at": row[0], "event_type": row[1],
                "score": row[2], "detail": row[3],
            }
            for row in conn.execute(
            """SELECT created_at,event_type,score,detail FROM anonymous_control_events
               ORDER BY id DESC LIMIT 30"""
            ).fetchall()
        ]
        conn.commit()
    finally:
        conn.close()
    global_usage = {
        "estimated_tokens": _usage_status("estimated-token-global", controls["token_global_max"], window),
        "estimated_cost_microusd": _usage_status("estimated-cost-global", controls["cost_global_microusd_max"], window),
        "actual_tokens": _usage_status("actual-token-global", controls["token_global_max"], window),
        "actual_cost_microusd": _usage_status("actual-cost-global", controls["cost_global_microusd_max"], window),
    }
    users = {}
    identity_metadata = anonymous_control_identity_metadata()
    subject_ids = list(dict.fromkeys([
        *(str(user_id) for user_id in (user_ids or []) if is_anonymous_control_subject(user_id)),
        *identity_metadata,
    ]))
    for user_id in subject_ids:
        source = _source_for_user(user_id)
        users[user_id] = {
            **identity_metadata.get(user_id, {}),
            "cooldown": _cooldown_status(user_id, source),
            "estimated_tokens": _usage_status(f"estimated-token-user:{user_id}", controls["token_user_max"], window),
            "estimated_cost_microusd": _usage_status(f"estimated-cost-user:{user_id}", controls["cost_user_microusd_max"], window),
            "actual_tokens": _usage_status(f"actual-token-user:{user_id}", controls["token_user_max"], window),
            "actual_cost_microusd": _usage_status(f"actual-cost-user:{user_id}", controls["cost_user_microusd_max"], window),
            "network_estimated_tokens": _usage_status(f"estimated-token-source:{source}", controls["token_source_max"], window),
            "network_estimated_cost_microusd": _usage_status(f"estimated-cost-source:{source}", controls["cost_source_microusd_max"], 0),
            "network_actual_tokens": _usage_status(f"actual-token-source:{source}", controls["token_source_max"], window),
            "network_actual_cost_microusd": _usage_status(f"actual-cost-source:{source}", controls["cost_source_microusd_max"], 0),
        }
    return {
        "settings": controls,
        "policy": anonymous_budget_status(),
        "auto_close": anonymous_auto_close_status(),
        "active_runs": active_runs,
        "global_usage": global_usage,
        "users": users,
        "recent_events": events,
    }


def enforce_anon_chat(user_id: str, request: Request) -> None:
    """Rate-limit ONE public (anonymous) chat turn — no-op for registered users.

    Anonymous identities are ``anon_*`` (app/communications/auth.py). Caps both
    per-identity and per-IP so a minted guest token can't be looped to burn the
    agent owner's LLM budget. Called from every send lane (/chat, /send, /stream)
    right after the caller id is verified. See the security audit CRITICAL find."""
    if not is_anonymous_control_subject(user_id):
        return
    cmax, cwin = anon_chat_limits()
    enforce(f"anon-chat-uid:{user_id}", cmax, cwin,
            detail="You're sending messages too quickly. Please wait a moment.")
    ipmax, ipwin = anon_chat_ip_limits()
    enforce(f"anon-chat-ip:{client_ip(request)}", ipmax, ipwin,
            detail="Too many messages from your network. Please wait a moment.")


async def enforce_tier_chat(
    user_id: str, request: Request, *, db=None, message: str = "", agent_id: str = "",
) -> None:
    """Apply the effective tier's per-user message window to every chat lane.

    Anonymous callers keep the additional IP bucket so cycling guest tokens
    does not evade the limit. Capability lookup failures use the restrictive
    anonymous defaults and never silently disable enforcement.
    """
    anonymous = is_anonymous_control_subject(user_id) or not user_id
    if anonymous and len(message or "") > 32_000:
        raise HTTPException(
            status_code=413,
            detail={"code": "prompt_too_large", "maximum": 32_000},
        )
    platform_sponsored = True
    if anonymous and agent_id:
        try:
            if db is None:
                from app.db import get_db
                db = get_db()
            agent = await db.get_agent_by_id(agent_id)
            from app.agent.public_policy import normalize_public_access
            platform_sponsored = (
                normalize_public_access(agent or {})["funding"]["mode"] == "platform_showcase"
            )
        except Exception:
            platform_sponsored = True
    if anonymous and not anonymous_chat_enabled():
        raise HTTPException(
            status_code=429,
            detail=_anonymous_unavailable_detail("anonymous_chat_disabled"),
            headers={"Retry-After": "3600"},
        )
    if anonymous:
        auto_close = await asyncio.to_thread(anonymous_auto_close_status)
        if auto_close["active"]:
            raise HTTPException(
                status_code=429,
                detail={
                    "code": "registration_required",
                    "scope": "anonymous_auto_closed",
                    "message": "Anonymous chat is temporarily paused by its safety budget. Register and sign in to continue.",
                    "reason": auto_close["reason"],
                },
                headers={"Retry-After": str(max(1, auto_close["remaining_seconds"]))},
            )

        # Anonymous access tokens are deliberately short-lived and bound to
        # the coarse source that minted them. A copied token cannot be replayed
        # from an unrelated network to bypass admission controls.
        from app.auth.jwt import decode_token
        auth = request.headers.get("authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        payload = decode_token(token) if token else None
        source = client_source_hash(request)
        if (
            not payload
            or payload.get("anon_admission") is not True
            or payload.get("anon_source") != source
            or str(payload.get("user_id") or "") != str(user_id or "")
        ):
            raise HTTPException(
                status_code=401,
                detail={
                    "code": "anonymous_admission_required",
                    "scope": "anonymous_admission",
                    "message": "Refresh this guest session or register and sign in to continue.",
                },
            )

        cooldown = await asyncio.to_thread(_cooldown_status, str(user_id), source)
        if cooldown["active"]:
            raise HTTPException(
                status_code=429,
                detail={
                    "code": "registration_required",
                    "scope": "anonymous_cooldown",
                    "message": "Anonymous access is temporarily limited for this traffic pattern. Register and sign in to continue.",
                    "reason": cooldown["reason"],
                },
                headers={"Retry-After": str(max(1, cooldown["remaining_seconds"]))},
            )

        risk = await asyncio.to_thread(
            _record_risk,
            str(user_id), source, str(message or ""),
            str(payload.get("anon_admission_id") or ""),
            request.headers.get("user-agent", ""),
        )
        controls = anon_native_controls()
        if risk["score"] >= int(controls["risk_cooldown_score"]):
            reason = ",".join(risk["reasons"]) or "correlated_traffic"
            await asyncio.to_thread(
                set_anonymous_cooldown,
                str(user_id), int(controls["risk_cooldown_seconds"]), reason, risk["score"],
            )
            raise HTTPException(
                status_code=429,
                detail={
                    "code": "registration_required",
                    "scope": "anonymous_risk_cooldown",
                    "message": "This anonymous traffic pattern requires registration to continue.",
                    "risk_score": risk["score"],
                },
                headers={"Retry-After": str(max(1, int(controls["risk_cooldown_seconds"])))},
            )
        if risk["score"] >= int(controls["risk_delay_score"]):
            await asyncio.sleep(min(5.0, int(controls["risk_delay_ms"]) / 1000))
            _control_event(
                "progressive_delay", user_id=str(user_id), source_hash=source,
                score=risk["score"], detail=",".join(risk["reasons"]),
            )

    try:
        from app.entitlements.service import resolve_capabilities
        capabilities = await resolve_capabilities(user_id, db=db)
        limits = capabilities.get("limits") or {}
        anonymous_limits = capabilities.get("anonymous") or {}
        maximum = int(limits.get("messages_per_window") or 0)
        window = int(limits.get("window_seconds") or 0)
    except Exception:
        anonymous_limits = {}
        maximum, window = 30, 300
    if maximum > 0 and window > 0:
        tier_detail = {
            "code": "rate_limited",
            "scope": "user_tier",
            "message": "You're sending messages too quickly. Please wait a moment.",
            "limit": maximum,
            "window_seconds": window,
        }
        enforce(
            f"tier-chat-uid:{user_id or '__anonymous__'}", maximum, window,
            detail=tier_detail,
        )
        if anonymous and not await asyncio.to_thread(
            _persistent_hit,
            f"tier-chat-uid:{user_id or '__anonymous__'}",
            maximum,
            window,
        ):
            raise HTTPException(
                status_code=429, detail=tier_detail,
                headers={"Retry-After": str(window)},
            )
    if anonymous:
        # Published Anonymous-tier values govern the visitor allowance. Missing
        # legacy values retain the established native defaults; global controls
        # below remain independent ceilings and cannot be loosened by a tier.
        identity_default, identity_window_default = anon_chat_limits()
        identity_max = max(0, int(anonymous_limits.get("identity_messages", identity_default) or 0))
        identity_window = max(1, int(anonymous_limits.get("identity_window_seconds", identity_window_default) or identity_window_default))
        identity_detail = {
            "code": "rate_limited", "scope": "anonymous_tier_identity",
            "message": "You're sending messages too quickly. Please wait a moment.",
            "limit": identity_max, "window_seconds": identity_window,
        }
        if identity_max > 0 and not await asyncio.to_thread(
            _persistent_hit, f"anonymous-tier-identity:{user_id or '__anonymous__'}",
            identity_max, identity_window,
        ):
            raise HTTPException(status_code=429, detail=identity_detail,
                                headers={"Retry-After": str(identity_window)})
        # Retain the pre-existing app-wide identity ceiling as a second gate.
        if identity_default > 0:
            global_identity_detail = dict(identity_detail, scope="anonymous_identity_global",
                                          limit=identity_default,
                                          window_seconds=identity_window_default)
            enforce(f"anon-chat-uid:{user_id or '__anonymous__'}", identity_default,
                    identity_window_default, detail=global_identity_detail)
            if not await asyncio.to_thread(
                _persistent_hit, f"anon-chat-uid:{user_id or '__anonymous__'}",
                identity_default, identity_window_default,
            ):
                raise HTTPException(status_code=429, detail=global_identity_detail,
                                    headers={"Retry-After": str(identity_window_default)})

        daily_default, daily_window_default = anon_daily_chat_limits()
        daily_max = max(0, int(anonymous_limits.get("daily_messages", daily_default) or 0))
        daily_window = max(1, int(anonymous_limits.get("daily_window_seconds", daily_window_default) or daily_window_default))
        daily_detail = {
            "code": "registration_required",
            "scope": "anonymous_daily_allowance",
            "message": "You've reached today's anonymous chat allowance. Register and sign in to continue.",
            "limit": daily_max,
            "window_seconds": daily_window,
        }
        if daily_max > 0 and not await asyncio.to_thread(
            _persistent_hit,
            f"anonymous-tier-daily:{user_id or '__anonymous__'}",
            daily_max,
            daily_window,
        ):
            raise HTTPException(
                status_code=429, detail=daily_detail,
                headers={"Retry-After": str(daily_window)},
            )
        if daily_default > 0:
            global_daily_detail = dict(daily_detail, scope="anonymous_daily_global",
                                       limit=daily_default,
                                       window_seconds=daily_window_default)
            if not await asyncio.to_thread(
                _persistent_hit, f"anon-chat-daily:{user_id or '__anonymous__'}",
                daily_default, daily_window_default,
            ):
                raise HTTPException(status_code=429, detail=global_daily_detail,
                                    headers={"Retry-After": str(daily_window_default)})

        ip_default, ip_window_default = anon_chat_ip_limits()
        ipmax = max(0, int(anonymous_limits.get("network_messages", ip_default) or 0))
        ipwin = max(1, int(anonymous_limits.get("network_window_seconds", ip_window_default) or ip_window_default))
        network_detail = {
            "code": "rate_limited",
            "scope": "anonymous_network",
            "message": "Too many messages from your network. Please wait a moment.",
            "limit": ipmax,
            "window_seconds": ipwin,
        }
        ip = client_ip(request)
        enforce(
            f"anonymous-tier-network-ip:{ip}", ipmax, ipwin, detail=network_detail,
        )
        if not await asyncio.to_thread(
            _persistent_hit, f"anonymous-tier-network-ip:{ip}", ipmax, ipwin,
        ):
            raise HTTPException(
                status_code=429, detail=network_detail,
                headers={"Retry-After": str(ipwin)},
            )

        # A second durable aggregate uses a coarse network prefix. Browser hints
        # cannot split this bucket, so changing User-Agent does not evade it.
        source_detail = dict(network_detail, scope="anonymous_source")
        source = client_source_hash(request)
        enforce(
            f"anonymous-tier-network-source:{source}", ipmax, ipwin, detail=source_detail,
        )
        if not await asyncio.to_thread(
            _persistent_hit, f"anonymous-tier-network-source:{source}", ipmax, ipwin,
        ):
            raise HTTPException(
                status_code=429, detail=source_detail,
                headers={"Retry-After": str(ipwin)},
            )
        if ip_default > 0:
            global_network_detail = dict(network_detail, scope="anonymous_network_global",
                                         limit=ip_default,
                                         window_seconds=ip_window_default)
            enforce(f"anon-chat-ip:{ip}", ip_default, ip_window_default,
                    detail=global_network_detail)
            if not await asyncio.to_thread(
                _persistent_hit, f"anon-chat-ip:{ip}", ip_default, ip_window_default,
            ):
                raise HTTPException(status_code=429, detail=global_network_detail,
                                    headers={"Retry-After": str(ip_window_default)})
            global_source_detail = dict(global_network_detail, scope="anonymous_source_global")
            enforce(f"anon-chat-source:{source}", ip_default, ip_window_default,
                    detail=global_source_detail)
            if not await asyncio.to_thread(
                _persistent_hit, f"anon-chat-source:{source}", ip_default, ip_window_default,
            ):
                raise HTTPException(status_code=429, detail=global_source_detail,
                                    headers={"Retry-After": str(ip_window_default)})

        global_max, global_window = anon_global_chat_limits()
        global_detail = {
            "code": "rate_limited",
            "scope": "anonymous_global_chat",
            "message": "Anonymous chat is temporarily at capacity. Register and sign in to continue.",
            "limit": global_max,
            "window_seconds": global_window,
        }
        if not await asyncio.to_thread(
            _persistent_hit, "anon-chat-global", global_max, global_window,
        ):
            _record_source_event(
                "warning",
                "App-wide anonymous chat circuit breaker activated.",
            )
            raise HTTPException(
                status_code=429, detail=global_detail,
                headers={"Retry-After": str(global_window)},
            )

        # Reserve estimated tokens and cost only after every cheaper rate/risk
        # gate has accepted the turn, so rejected spam cannot consume a real
        # guest's spend allowance as a denial-of-service technique.
        if platform_sponsored:
            budget = await asyncio.to_thread(
                _consume_native_budgets, str(user_id), source, str(message or ""),
            )
            if not budget["allowed"]:
                raise HTTPException(
                    status_code=429,
                    detail={
                        "code": "registration_required",
                        "scope": f"anonymous_{budget['scope']}_budget",
                        "message": "This anonymous usage budget is exhausted. Register and sign in to continue.",
                        "limit": budget["limit"],
                        "used": budget["used"],
                    },
                    headers=(
                        {"Retry-After": str(max(1, budget["resets_in"]))}
                        if budget.get("resets_in") is not None else None
                    ),
                )

        # Last admission gate: rejected traffic above cannot consume the hard
        # budget, and every successful reservation happens atomically before
        # model work. Exactly ``budget_max`` anonymous turns can pass per period.
        if platform_sponsored:
            budget_max, budget_window = anon_budget_limits()
            if not await asyncio.to_thread(
                _persistent_hit, "anon-chat-budget", budget_max, budget_window,
            ):
                _record_source_event(
                    "warning", "App-wide anonymous chat budget exhausted.",
                )
                raise HTTPException(
                    status_code=429,
                    detail=_anonymous_unavailable_detail(
                        "anonymous_budget_exhausted",
                        limit=budget_max,
                        window=budget_window,
                    ),
                    headers={"Retry-After": str(budget_window)},
                )
