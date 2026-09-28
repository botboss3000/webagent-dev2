"""
Per-user data-backend resolver (multi-tenant / bring-your-own-database).

Given a user id, returns the StorageBackend that holds THAT user's interaction
data (their own Postgres). This is the "data plane" half of the split; the
control plane (accounts, agent catalog, billing) stays on the central backend
returned by `app.db.get_control_db()`.

Only used when App Settings → User BYOD is ON. The tenant router
(app/db/router.py) calls `resolve_data_backend()` with the current caller's id.

Design notes
------------
* Each personal Postgres backend owns its own psycopg connection pool
  (min 4 / max 16). We therefore keep a small LRU of warm tenant backends and
  close a pool when its tenant is evicted (idle TTL or over the cap) so a busy
  server can't accumulate unbounded connections.
* Tenant backends are wrapped with the field-level encryption shim
  (`_maybe_wrap_encryption`) so a user's own LLM keys / vault (auth_elements) are
  encrypted at rest in their DB — but NOT with the hybrid local-first wrapper:
  hybrid uses one shared local SQLite hot store, which would cross-contaminate
  tenants. Personal DBs go straight to Postgres.
* No-personal-DB fallback: when user BYOD is ON but a user has no registry
  record, we fall back to the central control backend (a shared "trial" DB) and
  flag it so the UI can show a "connect your own database" banner. A user who DOES
  have a personal DB that is currently unreachable is NOT silently sent to central
  (that would split their transcript across two databases) — we raise instead, and
  their chat surfaces a clear "your database is unreachable" error.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from typing import Optional

logger = logging.getLogger(__name__)

# Warm tenant-backend LRU. Keyed by user_id → {"backend", "at"}.
_CACHE: "OrderedDict[str, dict]" = OrderedDict()
_CACHE_LOCK = threading.RLock()
_MAX_TENANTS = 24          # cap on simultaneously-open personal pools
_IDLE_TTL_SECONDS = 900.0  # close a tenant pool after this long idle

# Per-user connection health, for the "your database is unreachable" banner and
# the "you're on the shared trial DB" banner. user_id → dict.
_HEALTH: dict = {}


class TenantDBUnavailable(RuntimeError):
    """Raised when a user HAS a personal database configured but it can't be
    reached. Deliberately not swallowed into a central fallback — that would split
    the user's data across two databases."""


def _set_health(user_id: str, **fields) -> None:
    rec = _HEALTH.setdefault(user_id, {})
    rec.update(fields)


def tenant_health(user_id: str) -> dict:
    """Current data-plane health for a user (personal/trial/unreachable + message)."""
    return dict(_HEALTH.get(user_id) or {"state": "unknown"})


def _close_backend(backend) -> None:
    """Best-effort close of a tenant backend's pool (through any wrappers)."""
    try:
        inner = getattr(backend, "_inner", backend)  # unwrap Encryption shim
        close = getattr(inner, "close", None)
        if callable(close):
            close()
    except Exception as e:  # noqa: BLE001
        logger.debug("Error closing tenant backend: %s", e)


def _evict_locked(user_id: str) -> None:
    entry = _CACHE.pop(user_id, None)
    if entry:
        _close_backend(entry.get("backend"))


def _prune_locked(now: float) -> None:
    # Drop idle tenants, then trim to the cap (oldest first).
    stale = [uid for uid, e in _CACHE.items() if (now - e["at"]) > _IDLE_TTL_SECONDS]
    for uid in stale:
        _evict_locked(uid)
    while len(_CACHE) > _MAX_TENANTS:
        old_uid, old_entry = _CACHE.popitem(last=False)
        _close_backend(old_entry.get("backend"))


def _build_tenant_backend(user_id: str):
    """Build a fresh personal Postgres backend for a user (opens a pool + runs the
    fingerprint-gated schema bootstrap on first connect). Raises on failure."""
    from app.db import _maybe_wrap_encryption
    from app.db.postgres_backend import build_postgres_backend
    from app.db.tenant_registry import load_tenant_config, resolve_tenant_password

    cfg = load_tenant_config(user_id)
    if cfg is None:
        raise KeyError("no tenant record")

    # Resolve the tenant password from the credential store DIRECTLY — do NOT use
    # the global _resolve_pg_password(), which checks WEBAGENT_DB_PASSWORD (the
    # CENTRAL db's password) first and would hand this tenant the wrong password.
    password = resolve_tenant_password(user_id)
    if not password:
        raise TenantDBUnavailable(
            f"no stored password for {user_id}'s personal database"
        )
    # build_postgres_backend opens the pool and runs _init_db() (idempotent,
    # fingerprint-gated) so an empty personal DB is provisioned on first connect.
    backend = build_postgres_backend(cfg, password=password, seed=True)
    return _maybe_wrap_encryption(backend)


def get_tenant_backend(user_id: str):
    """Return the cached-or-freshly-built personal backend for a user. Raises
    TenantDBUnavailable if the user has a personal DB configured but it can't be
    reached; raises KeyError if the user has no personal DB record."""
    now = time.monotonic()
    with _CACHE_LOCK:
        entry = _CACHE.get(user_id)
        if entry is not None:
            entry["at"] = now
            _CACHE.move_to_end(user_id)
            return entry["backend"]

    # Build outside the cache lock is tempting, but two concurrent callers would
    # then open two pools. Keep it simple + correct: build under the lock. Pool
    # open is a few seconds only on a cold tenant; warm hits never reach here.
    with _CACHE_LOCK:
        entry = _CACHE.get(user_id)
        if entry is not None:
            entry["at"] = now
            _CACHE.move_to_end(user_id)
            return entry["backend"]
        try:
            backend = _build_tenant_backend(user_id)
        except TenantDBUnavailable as e:
            _set_health(user_id, state="unreachable", message=str(e))
            raise
        except KeyError:
            raise
        except Exception as e:  # noqa: BLE001
            _set_health(user_id, state="unreachable", message=str(e)[:300])
            raise TenantDBUnavailable(
                f"could not reach {user_id}'s personal database: {e}"
            ) from e
        _CACHE[user_id] = {"backend": backend, "at": now}
        _set_health(user_id, state="personal", message="")
        _prune_locked(now)
        return backend


def resolve_data_backend(user_id: Optional[str]):
    """Resolve the DATA-plane backend for the current caller.

    * No user id (background job without identity) → central control backend.
    * User has a personal DB → their backend (raises TenantDBUnavailable if down).
    * User has no personal DB → central control backend (shared trial), flagged so
      the UI can prompt them to connect their own.
    """
    from app.db import get_control_db
    from app.db.tenant_registry import has_tenant_db

    if not user_id:
        return get_control_db()

    if has_tenant_db(user_id):
        return get_tenant_backend(user_id)

    # No personal DB configured → shared central fallback (trial).
    _set_health(user_id, state="trial", message="Using the shared database — "
                "connect your own database to keep your data separate.")
    return get_control_db()


def evict(user_id: str) -> None:
    """Drop and close a user's cached backend (e.g. after they change their DB)."""
    with _CACHE_LOCK:
        _evict_locked(user_id)
    _HEALTH.pop(user_id, None)


def evict_all() -> None:
    """Close every cached tenant pool (e.g. when user BYOD is switched off)."""
    with _CACHE_LOCK:
        for uid in list(_CACHE.keys()):
            _evict_locked(uid)
    _HEALTH.clear()
