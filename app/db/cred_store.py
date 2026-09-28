"""
Database-connection credential store — SYNCHRONOUS and DATABASE-INDEPENDENT.

Why this exists (and why it is separate from app.secrets):
  The general secrets vault (app.secrets) is configurable and, by default, the
  "inline_db" backend stores secrets INSIDE the application database. That is a
  chicken-and-egg for a *remote* database connection: the Postgres
  password would live inside the very database we need the password to reach.
  When that database is down (or on a cold start), the password is unreachable,
  so the app silently falls back to local SQLite — and a device configured for a
  shared database quietly stops sharing it.

  This module holds ONLY the few secrets needed to open the database connection
  (Postgres password). It is:
    * synchronous     — so the cold-start build path (get_db(), which is sync)
                        can resolve the password without an event loop or the
                        fragile asyncio.run() dance.
    * db-independent  — primary store is the OS keyring (Windows Credential
                        Manager / macOS Keychain / Linux Secret Service); never
                        the app database.

  Fallback: on a headless host with no usable keyring (e.g. a bare Termux /
  Linux box), we fall back to a 0600 JSON file under the user's home
  (~/.webagent/db_creds.json) — OUTSIDE the repo, like the per-machine
  device_id file. This is plaintext on disk and is logged as a downgrade; it is
  only reached when no OS keyring is available. The keys here mirror the names
  used elsewhere: "db_password_<provider>".
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# Distinct from the general vault's "webagent_vault" service so the two stores
# never collide in the OS credential manager.
_KEYRING_SERVICE = "webagent_db"

_HOME_DIR = os.path.join(os.path.expanduser("~"), ".webagent")
_FALLBACK_FILE = os.path.join(_HOME_DIR, "db_creds.json")

# Cache the keyring-usable decision per process (the probe touches the OS store).
_keyring_ok: Optional[bool] = None


def _keyring_usable() -> bool:
    """True only when `keyring` resolves to a real, secure backend (not the
    no-op 'fail' backend nor the insecure plaintext/alt fallbacks)."""
    global _keyring_ok
    if _keyring_ok is not None:
        return _keyring_ok
    try:
        import keyring
        import keyring.backends.fail as _fail
        kr = keyring.get_keyring()
        if isinstance(kr, _fail.Keyring):
            _keyring_ok = False
            return False
        mod = (type(kr).__module__ or "").lower()
        name = type(kr).__name__.lower()
        if "keyrings.alt" in mod or "plaintext" in name or "null" in name:
            _keyring_ok = False
            return False
        children = getattr(kr, "backends", None)
        if children is not None and len(list(children)) == 0:
            _keyring_ok = False
            return False
        _keyring_ok = True
        return True
    except Exception:
        _keyring_ok = False
        return False


def backend_name() -> str:
    """Which store is in effect — for diagnostics/status surfaces."""
    return "os_keyring" if _keyring_usable() else "local_file"


# ── File fallback (headless hosts only) ──────────────────────────────────────

def _read_fallback() -> dict:
    try:
        if os.path.exists(_FALLBACK_FILE):
            with open(_FALLBACK_FILE, "r", encoding="utf-8") as f:
                return json.load(f) or {}
    except Exception as e:
        logger.warning("Could not read db_creds fallback file: %s", e)
    return {}


def _write_fallback(data: dict) -> None:
    try:
        os.makedirs(_HOME_DIR, exist_ok=True)
        with open(_FALLBACK_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
        try:
            os.chmod(_FALLBACK_FILE, 0o600)  # best-effort; no-op-ish on Windows
        except Exception:
            pass
    except Exception as e:
        logger.error("Could not write db_creds fallback file: %s", e)
        raise


# ── Public API (all synchronous) ─────────────────────────────────────────────

def set_secret(key: str, value: str) -> None:
    """Store a DB-connection secret durably and database-independently."""
    if not key:
        return
    value = value or ""
    if _keyring_usable():
        try:
            import keyring
            keyring.set_password(_KEYRING_SERVICE, key, value)
            return
        except Exception as e:
            logger.warning("Keyring set failed for %s (%s); using file fallback", key, e)
    data = _read_fallback()
    data[key] = value
    _write_fallback(data)
    logger.warning(
        "Stored DB credential '%s' in the plaintext fallback file (%s) — no secure "
        "OS keyring is available on this host.", key, _FALLBACK_FILE,
    )


def get_secret(key: str) -> Optional[str]:
    """Resolve a DB-connection secret. Keyring first, then file fallback."""
    if not key:
        return None
    if _keyring_usable():
        try:
            import keyring
            val = keyring.get_password(_KEYRING_SERVICE, key)
            if val:
                return val
        except Exception as e:
            logger.debug("Keyring get failed for %s (%s); trying file fallback", key, e)
    return _read_fallback().get(key) or None


def delete_secret(key: str) -> bool:
    """Remove a stored secret from whichever store holds it."""
    removed = False
    if _keyring_usable():
        try:
            import keyring
            keyring.delete_password(_KEYRING_SERVICE, key)
            removed = True
        except Exception:
            pass
    data = _read_fallback()
    if key in data:
        del data[key]
        _write_fallback(data)
        removed = True
    return removed
