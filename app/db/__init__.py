"""
Database backend factory for WebAgent.

Provides get_db() which returns the current StorageBackend instance
(SQLite local or remote Postgres) based on the persisted connection config.

The active backend is determined by ``app/db/connection_config.py`` — the
saved provider is the source of truth. There is no longer a separate
cloud/local mode toggle; the mode is derived from the provider.
"""

import logging
import os
import contextvars
from typing import Any, Optional
from app.db.interface import StorageBackend
from app.runtime_mode import performance_test_mode

logger = logging.getLogger(__name__)

class StorageLayoutInactiveError(RuntimeError):
    """Raised when no verified split layout or legacy SQLite authority exists."""



_app_db_instance: Optional[StorageBackend] = None
_user_db_instances: dict[str, StorageBackend] = {}
_agent_db_instances: dict[str, StorageBackend] = {}
_db_override: contextvars.ContextVar[Optional[Any]] = contextvars.ContextVar(
    "webagent_db_override", default=None
)


def get_mode() -> str:
    """
    Get the current database mode — derived from the saved provider config.

    Returns:
        "remote" when a Postgres-family provider is configured,
        "local" for SQLite.

    Env override: WEBAGENT_DB_MODE wins when WEBAGENT_CONFIG_SOURCE=env.
    """
    if performance_test_mode():
        return "local"
    if os.environ.get("WEBAGENT_CONFIG_SOURCE", "").lower() == "env":
        env_mode = os.environ.get("WEBAGENT_DB_MODE", "").strip().lower()
        if env_mode in ("remote", "local"):
            return env_mode
    try:
        from app.db.connection_config import load_config, is_postgres_provider
        prov = getattr(load_config(), "provider", "sqlite")
        return "remote" if is_postgres_provider(prov) else "local"
    except Exception:
        return "local"


def _maybe_wrap_encryption(backend: StorageBackend) -> StorageBackend:
    """
    If the active encryption level is non-trivial, wrap the backend in an
    EncryptedStorageBackend so sensitive fields are transparently encrypted
    on write and decrypted on read.
    """
    try:
        from app.encryption import get_level, get_encryption
        level = get_level()
        if level == "none":
            return backend
        from app.db.interface import EncryptedStorageBackend
        enc = get_encryption()
        wrapped = EncryptedStorageBackend(backend, enc)
        logger.info("Wrapped backend with EncryptedStorageBackend (level=%s)", level)
        return wrapped
    except Exception as e:
        logger.warning("Encryption wrap failed (%s); using backend unwrapped", e)
        return backend


def _maybe_wrap_hybrid(remote: StorageBackend) -> StorageBackend:
    """When the hybrid local-first layer is switched on, wrap the reachable remote
    (Postgres) authority in a HybridBackend that also holds a local SQLite hot
    store. Off by default and only ever applied to a live remote — a local-only
    install never reaches here, so it is a strict no-op there.

    Ordering: the encryption shim wraps the RESULT of this (Enc(Hybrid(local,
    remote))), so field-level secret encryption still sees a single backend and
    authoritative auth_element reads resolve against the remote."""
    try:
        from app.db.hybrid import hybrid_enabled, HybridBackend
        if not hybrid_enabled():
            return remote
        from app.db.local import LocalBackend
        local = LocalBackend()
        logger.info("Wrapped remote backend with HybridBackend (local-first hot store)")
        return HybridBackend(local, remote)
    except Exception as e:
        logger.warning("Hybrid wrap failed (%s); using remote unwrapped", e)
        return remote


def reset_db_instance() -> None:
    """Drop the cached backend so the next get_db() rebuilds it.

    Used after changing the encryption level so the wrapper is re-evaluated.
    """
    global _app_db_instance
    _app_db_instance = None
    _user_db_instances.clear()
    _agent_db_instances.clear()


_PG_PROVIDERS = ("postgres", "neon", "gcp_cloud_sql")


# ── Connection health ────────────────────────────────────────────────────────
# Records whether the LAST get_db() build actually reached the database the admin
# signed into, or silently fell back to THIS device's local SQLite copy. Surfaced
# to the Database & Devices page so a fallback shows a clear "couldn't reach the
# shared database" banner instead of failing quietly (the local copy always
# probes green, which is exactly what made the failure invisible before).
_conn_health: dict = {
    "ok": True, "degraded": False,
    "intended": None, "actual": None,
    "reason": None, "detail": None, "message": None,
}

_DEGRADED_MESSAGES = {
    "no_password": (
        "Couldn't reach the shared database — there's no saved password for it on "
        "this device, so the app is running on this device's local copy. Open the "
        "Database page and sign in (Activate) to reconnect."
    ),
    "connect_failed": (
        "Couldn't reach the shared database, so the app is running on this device's "
        "local copy. Check the connection and sign in again on the Database page."
    ),
}


def get_connection_health() -> dict:
    """Return a copy of the current connection-health record.

    ``degraded`` is True when the saved config points at a shared remote database
    but get_db() couldn't reach it and dropped to local SQLite. ``message`` is a
    ready-to-show one-liner; ``detail`` carries the underlying error."""
    return dict(_conn_health)


def is_remote_db() -> bool:
    """True only when the app is ACTUALLY connected to a remote Postgres database,
    False for local SQLite — including a degraded fallback to the local copy. Used
    to lengthen background-poller intervals on remote backends, where every
    round-trip is a costly network hop."""
    actual = _conn_health.get("actual")
    if actual:
        return actual != "local"
    # Health not yet recorded (pre-get_db) — infer from the saved provider.
    try:
        from app.db.connection_config import load_config, is_postgres_provider
        return is_postgres_provider(getattr(load_config(), "provider", "sqlite"))
    except Exception:
        return False


def _mark_healthy(actual: str) -> None:
    _conn_health.update(ok=True, degraded=False, intended=actual, actual=actual,
                        reason=None, detail=None, message=None)


def _mark_degraded(intended: str, reason: str, detail: str = "") -> None:
    msg = _DEGRADED_MESSAGES.get(reason, _DEGRADED_MESSAGES["connect_failed"])
    _conn_health.update(ok=False, degraded=True, intended=intended, actual="local",
                        reason=reason, detail=(detail or "")[:300], message=msg)
    logger.error("DB connection degraded: intended=%s reason=%s detail=%s",
                 intended, reason, (detail or "")[:200])


def _resolve_pg_password(cfg) -> str:
    """Resolve the Postgres password synchronously at cold start.

    Order:
      1. WEBAGENT_DB_PASSWORD env (the warm/activate path stashes it here).
      2. The database-independent credential store (OS keyring / file fallback).
         This is the durable source: it survives self-restarts and does NOT live
         inside the database we are trying to reach. We try the saved
         ``password_secret_key`` and, if that is unset (older configs), the
         conventional ``db_password_<provider>`` name so such configs self-heal.
      3. Best-effort read from the configurable secrets vault (legacy fallback).

    get_db() is synchronous, so steps 1-2 use no event loop at all; only the
    legacy step 3 needs the async vault and is guarded against a running loop."""
    pw = os.environ.get("WEBAGENT_DB_PASSWORD", "")
    if pw:
        return pw

    key = getattr(cfg, "password_secret_key", None)
    provider = getattr(cfg, "provider", "") or ""
    try:
        from app.db import cred_store
        for k in (key, f"db_password_{provider}" if provider else None):
            if not k:
                continue
            val = cred_store.get_secret(k)
            if val:
                return val
    except Exception as e:
        logger.debug("Could not resolve PG password from cred_store: %s", e)

    if key:
        try:
            import asyncio
            from app.secrets import get_secrets
            coro = get_secrets().get(key)
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                resolved = asyncio.run(coro)
                if resolved:
                    return resolved
        except Exception as e:
            logger.debug("Could not resolve PG password from secrets: %s", e)
    return pw


def active_postgres_conninfo() -> Optional[str]:
    """If the active backend is a Postgres-family provider, return a libpq
    conninfo string for it (host/port/db/user/password/ssl). Else None.

    Used by tools that need a direct Postgres connection (e.g. the admin DB
    viewer) without going through the storage interface."""
    try:
        from app.db.connection_config import load_config, is_postgres_provider
        cfg = load_config()
    except Exception:
        return None
    if not is_postgres_provider(getattr(cfg, "provider", "sqlite")):
        return None
    try:
        from app.db.postgres_backend import make_conninfo
        return make_conninfo(cfg, _resolve_pg_password(cfg))
    except Exception as e:
        logger.debug("Could not build active PG conninfo: %s", e)
        return None


def _maybe_build_postgres():
    """Return a live PostgresBackend if the saved connection config selects a
    Postgres-family provider; otherwise None (fall through to local SQLite)."""
    try:
        from app.db.connection_config import load_config, is_postgres_provider
        cfg = load_config()
    except Exception as e:
        logger.debug("Could not load connection config: %s", e)
        return None
    if not is_postgres_provider(getattr(cfg, "provider", "sqlite")):
        return None
    from app.db.postgres_backend import build_postgres_backend
    password = _resolve_pg_password(cfg)
    if not password:
        # No durable password on this device (the exact cause of the silent
        # local fallback) — record it so the page can say so plainly.
        _mark_degraded("postgres", "no_password",
                       "no saved password for the Postgres connection on this device")
        return None
    # Retry a TRANSIENT connection failure before giving up. Opening the pool at
    # boot briefly bursts several connections at the remote pooler; a momentary
    # reset ("server closed the connection unexpectedly" / WinError 10054), common
    # right after a fast restart while the pooler still counts the old connections,
    # would otherwise strand the whole server on an EMPTY local SQLite copy for its
    # entire life — the worst failure mode (silent wrong data), not a slow one. A
    # short backing-off retry lets the pooler drain and accept us. A genuinely
    # unreachable DB still falls through to the local copy after the attempts, with
    # the degraded banner, exactly as before.
    import time as _time
    last_err: Exception | None = None
    for _attempt in range(4):
        try:
            backend = build_postgres_backend(cfg, password=password)
            logger.info("Initialized PostgresBackend (provider=%s%s)", cfg.provider,
                        f", after {_attempt} retr{'y' if _attempt == 1 else 'ies'}" if _attempt else "")
            return backend
        except Exception as e:  # noqa: BLE001
            last_err = e
            if _attempt < 3:
                _delay = 1.5 * (2 ** _attempt)  # 1.5s, 3s, 6s
                logger.warning("PostgresBackend init attempt %d failed (%s); retrying in %.1fs",
                               _attempt + 1, e, _delay)
                _time.sleep(_delay)
    logger.error("PostgresBackend init failed after retries (%s); falling back to local", last_err)
    _mark_degraded("postgres", "connect_failed", str(last_err))
    return None


def _build_local_control_backend() -> StorageBackend:
    """Select a safe local authority without routing user data into app.db."""
    from app.db.local import LocalBackend
    from app.db.storage_layout import (
        APP_DB_PATH,
        is_layout_active,
    )

    if is_layout_active():
        logger.info("Initialized app-plane LocalBackend (SQLite v2)")
        return LocalBackend(str(APP_DB_PATH), seed=False, plane="app")

    raise StorageLayoutInactiveError(
        "Split storage layout is not activated. Initialize and verify the v2 "
        "storage layout before serving database-backed requests."
    )


def get_control_db() -> StorageBackend:
    """
    Get the central/control storage backend instance — the one database the admin
    signed into. This is the single shared database in single-tenant mode, and the
    account/agent-catalog/billing plane in user-BYOD mode.

    Lazily creates the backend based on the saved connection config. Cached in
    _app_db_instance.

    Returns:
        StorageBackend implementation (PostgresBackend or LocalBackend), possibly
        wrapped by EncryptedStorageBackend when encryption is enabled.
    """
    global _app_db_instance

    if _app_db_instance is None:
        from app.db.connection_config import load_config, is_postgres_provider
        if performance_test_mode():
            _provider = "sqlite"
        else:
            try:
                _provider = (getattr(load_config(), "provider", "") or "").strip()
            except Exception:
                _provider = ""

        # 1) Postgres-family — the universal asyncpg/psycopg backend.
        #    is_postgres_provider matches every provider whose dialect is "postgres"
        #    (postgres, aws_rds, gcp_cloud_sql, azure_postgres, neon).
        if is_postgres_provider(_provider):
            pg_backend = _maybe_build_postgres()  # marks health degraded on failure
            if pg_backend is not None:
                _mark_healthy("postgres")
                _app_db_instance = _maybe_wrap_encryption(_maybe_wrap_hybrid(pg_backend))
                return _app_db_instance
            # Signed into a shared Postgres DB but couldn't reach it → local copy.
            logger.error("Falling back to LocalBackend (SQLite) — the shared Postgres "
                         "database is unreachable; see the Database page")
            _app_db_instance = _maybe_wrap_encryption(_build_local_control_backend())
            return _app_db_instance

        # 2) SQLite (or unrecognized legacy provider) — always local.
        _mark_healthy("local")
        _app_db_instance = _maybe_wrap_encryption(_build_local_control_backend())

    return _app_db_instance


def set_db_override(db: Any):
    """Install a task-local DB facade and return its reset token."""
    return _db_override.set(db)


def reset_db_override(token) -> None:
    """Restore the DB facade active before ``set_db_override``."""
    _db_override.reset(token)


def get_db() -> StorageBackend:
    """
    The storage backend for the current request/turn.

    Single-tenant (default): returns the central control backend unchanged — every
    caller shares the one admin-configured database, exactly as before.

    User BYOD ON (App Settings → User BYOD): returns a TenantRouterBackend
    that keeps the account/admin plane on the central database but routes each
    caller's interaction data (sessions, chats, memories, agents, secrets) to THAT
    user's own database. The ~90 existing call sites are unchanged: the router
    duck-types StorageBackend (see app/db/router.py).
    """
    override = _db_override.get()
    if override is not None:
        return override
    from app.db.storage_layout import is_layout_active
    if is_layout_active() and not is_remote_db():
        from app.db.router import PlaneRouterBackend
        return PlaneRouterBackend(get_app_db())
    control = get_control_db()
    try:
        from app.admin.settings import get_user_byod_enabled
        if not get_user_byod_enabled():
            try:
                from app.db.storage_layout import is_layout_active
                if is_layout_active():
                    from app.db.router import PlaneRouterBackend
                    return PlaneRouterBackend(get_app_db())
            except Exception as exc:
                logger.warning("Active layout routing unavailable: %s", exc)
            return control
    except Exception:  # noqa: BLE001 — never let a settings read break DB access
        return control
    from app.db.router import TenantRouterBackend
    return TenantRouterBackend(get_app_db())


def get_data_db(user_id: Optional[str]) -> StorageBackend:
    """Explicit data-plane backend for a specific user — the user's own database
    when user BYOD is on and they have one, else the central backend. Used by
    background jobs / raw-SQL paths that must target a KNOWN user rather than the
    ambient request caller. Safe in single-tenant mode (returns central)."""
    from app.db.storage_layout import is_layout_active
    if is_layout_active() and not is_remote_db():
        return get_user_db(user_id or "admin")
    try:
        from app.admin.settings import get_user_byod_enabled
        if not get_user_byod_enabled():
            return get_control_db()
    except Exception:  # noqa: BLE001
        return get_control_db()
    from app.db.tenant import resolve_data_backend
    return resolve_data_backend(user_id)


def get_app_db() -> StorageBackend:
    """Explicit installation/control-plane backend.

    Local SQLite uses a plane-scoped LocalBackend over app.db; remote deployments
    keep their configured control database.
    """
    return get_control_db()


def get_user_db(user_id: str):
    """Explicit encrypted handle for one user's local authority store.

    Plane-scoped SQLite stores still attach the centralized secret vaults for
    credential operations.  They therefore must carry the same encryption
    decorator as the app/control handle; otherwise a direct auth-element read
    can surface ciphertext to callers as though it were a usable secret.
    """
    from app.db.local import LocalBackend
    from app.db.user_store import _user_db_path

    key = str(user_id)
    backend = _user_db_instances.get(key)
    if backend is None:
        backend = _maybe_wrap_encryption(
            LocalBackend(_user_db_path(key), seed=False, plane="user")
        )
        _user_db_instances[key] = backend
    return backend


def get_agent_db(agent_id: str, *, parent_id: Optional[str] = None):
    """Explicit encrypted handle for one agent's authority store."""
    from pathlib import Path
    from app.agent_workspace import agent_db_path, subagent_db_path
    from app.db.local import LocalBackend
    from app.db.storage_layout import PROJECT_ROOT, get_app_store

    path = subagent_db_path(parent_id, agent_id) if parent_id else None
    if path is None:
        row = get_app_store().fetchone(
            "SELECT storage_ref FROM agent_catalog WHERE agent_id=?", (agent_id,)
        )
        ref = str((row or {}).get("storage_ref") or "")
        path = (PROJECT_ROOT / ref).resolve() if ref else agent_db_path(agent_id)
    authority_root = (PROJECT_ROOT / "data" / "agent_data").resolve()
    resolved = Path(path).resolve()
    if resolved != authority_root and authority_root not in resolved.parents:
        raise RuntimeError(f"Agent storage_ref escapes authority root: {resolved}")
    key = str(resolved)
    backend = _agent_db_instances.get(key)
    if backend is None:
        backend = _maybe_wrap_encryption(
            LocalBackend(key, seed=False, plane="agent")
        )
        _agent_db_instances[key] = backend
    return backend


async def get_db_stats() -> dict:
    """
    Get statistics about the current database.
    
    Returns:
        dict with mode and table row counts
    """
    db = get_db()
    raw = db.get_raw_client()
    
    tables = [
        "sessions", "interactions", "session_summaries", "agent_prompts",
        "memories", "memory_chunks",
        "tools", "agent_credentials", "skills",
    ]
    
    stats = {}
    for table in tables:
        try:
            result = raw.table(table).select("id", count="exact").limit(1).execute()
            # Remote Postgres may work differently from SQLite; fallback
            # to a separate count query for backends that don't set .count.
            if hasattr(result, 'count') and result.count is not None:
                stats[table] = result.count
            else:
                # Count all rows for local mode
                all_rows = raw.table(table).select("id").execute()
                stats[table] = len(all_rows.data) if all_rows.data else 0
        except Exception:
            stats[table] = -1  # Table doesn't exist or error
    
    # Try to get the actual file size for SQLite backends
    db_path = ""
    if not is_remote_db():
        try:
            from app.db.local import DEFAULT_DB_PATH
            db_path = DEFAULT_DB_PATH
            if os.path.exists(db_path):
                stats["db_size_bytes"] = os.path.getsize(db_path)
        except Exception:
            pass
    
    return {
        "mode": get_mode(),
        "backend": type(db).__name__,
        "tables": stats,
        "db_path": db_path,
    }
