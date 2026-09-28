"""Full-database encryption at rest (SQLCipher) — opt-in, per-database.

OFF by default. When a database is flagged on (``app/db_encryption.json``) and
the SQLCipher engine is installed, that database's file is encrypted at rest
under a per-file key held in the keyring vault (see :mod:`app.db.db_keys`). The
vault/KEK is the single root of trust: it holds the keys for every database.

How it fits together:
  * :func:`connect` is a near drop-in for ``sqlite3.connect`` used by every
    SQLite store (local+vault, logs+recordings, wiki). It picks the driver
    (SQLCipher when a file is/should-be encrypted, else stdlib ``sqlite3``) and
    applies the key. **When nothing is encrypted it is exactly stdlib sqlite3** —
    zero behaviour change for an install that never turns this on.
  * :func:`reconcile` runs once at startup (before any store opens its files) and
    migrates each file to match the configured state — plaintext→encrypted or
    encrypted→plaintext — backup-first, atomically, with rollback and no
    plaintext residue left behind.

Key application is driven by the file's ACTUAL state (detected from its header),
not just config, so a config/file mismatch (e.g. enabled but not yet migrated)
degrades safely instead of failing to open.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from typing import Dict, List, Optional, Sequence, Tuple

from app.db import db_keys

logger = logging.getLogger(__name__)

# app/db/db_crypto.py → app/db → app
_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CONFIG_FILE = os.path.join(_APP_DIR, "db_encryption.json")

# The databases that can be encrypted, in a stable order. Each is a separate
# SQLite file with its own connection site.
DB_IDS: Tuple[str, ...] = ("app", "local", "vault", "app_secrets", "agent_secrets", "user_secrets",
                            "logs", "recordings", "wiki")

DB_LABELS: Dict[str, str] = {
    "app": "App control plane (accounts, catalog, billing, coordination)",
    "local": "Main database (chat, sessions, memories, agents)",
    "vault": "Legacy credentials vault (migrated to app/agent/user secrets)",
    "app_secrets": "App secrets (deploy keys, LLM fallback, OAuth app creds, DNS keys)",
    "agent_secrets": "Agent secrets (OAuth tokens, ability creds, per-user LLM overrides)",
    "user_secrets": "User secrets (genui vault keys, browser cookies, user ability creds)",
    "logs": "Diagnostics & tool-metrics logs",
    "recordings": "Browser render recordings",
    "wiki": "Wiki articles (note: served as public pages)",
}

_SQLITE_MAGIC = b"SQLite format 3\x00"  # first 16 bytes of a plaintext SQLite file

# ── SQLCipher engine availability ────────────────────────────────────────────
_cipher_mod = None  # None=unprobed, False=unavailable, else the dbapi2 module


def sqlcipher_available() -> bool:
    """True if the SQLCipher Python driver is importable on this host."""
    global _cipher_mod
    if _cipher_mod is None:
        try:
            from sqlcipher3 import dbapi2 as _dbapi  # type: ignore
            _cipher_mod = _dbapi
        except Exception:
            _cipher_mod = False
    return _cipher_mod is not False


def _driver(use_cipher: bool):
    return _cipher_mod if use_cipher else sqlite3


# ── Per-database config (which files are encrypted) ──────────────────────────
_config_cache: Optional[Dict[str, bool]] = None


def _env_locked() -> bool:
    return os.environ.get("WEBAGENT_CONFIG_SOURCE", "").lower() == "env"


def load_config() -> Dict[str, bool]:
    """Return {db_id: enabled}. Absent file = everything off (the default)."""
    global _config_cache
    if _config_cache is None:
        raw: Dict = {}
        try:
            if os.path.exists(_CONFIG_FILE):
                with open(_CONFIG_FILE, "r") as f:
                    raw = (json.load(f) or {}).get("databases", {}) or {}
        except Exception as e:
            logger.warning("Failed to read db_encryption.json: %s", e)
        _config_cache = {k: bool(raw.get(k, False)) for k in DB_IDS}
    return dict(_config_cache)


def is_enabled(db_id: str) -> bool:
    return load_config().get(db_id, False)


def any_enabled() -> bool:
    return any(load_config().values())


def set_enabled(db_id: str, on: bool) -> None:
    """Persist the desired encrypted-state for a database. Applied on next reconcile."""
    global _config_cache
    if db_id not in DB_IDS:
        raise ValueError(f"Unknown database id '{db_id}'")
    cfg = load_config()
    cfg[db_id] = bool(on)
    _config_cache = cfg
    if _env_locked():
        return
    try:
        tmp = _CONFIG_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"databases": cfg}, f, indent=2)
        os.replace(tmp, _CONFIG_FILE)
    except Exception as e:
        logger.warning("Failed to write db_encryption.json: %s", e)


# ── File paths + state ───────────────────────────────────────────────────────
def db_path(db_id: str) -> Optional[str]:
    """Resolve the on-disk file for a database id (lazy imports avoid cycles)."""
    try:
        if db_id == "app":
            from app.db.storage_layout import APP_DB_PATH
            return str(APP_DB_PATH)
        if db_id == "local":
            from app.db.local import DEFAULT_DB_PATH
            return DEFAULT_DB_PATH
        if db_id == "vault":
            from app.db.local import DEFAULT_DB_PATH, _vault_path_for
            return _vault_path_for(DEFAULT_DB_PATH)
        if db_id == "app_secrets":
            from app.db.local import DEFAULT_DB_PATH, _app_vault_path
            return _app_vault_path(DEFAULT_DB_PATH)
        if db_id == "agent_secrets":
            from app.db.local import DEFAULT_DB_PATH, _agent_vault_path
            return _agent_vault_path(DEFAULT_DB_PATH)
        if db_id == "user_secrets":
            from app.db.local import DEFAULT_DB_PATH, _user_vault_path
            return _user_vault_path(DEFAULT_DB_PATH)
        if db_id == "logs":
            from app.db.logs_store import LOGS_DB_PATH
            return LOGS_DB_PATH
        if db_id == "recordings":
            from app.db.logs_store import RECORDINGS_DB_PATH
            return RECORDINGS_DB_PATH
        if db_id == "wiki":
            from app.wiki.db import WIKI_DB_PATH
            return WIKI_DB_PATH
    except Exception as e:
        logger.debug("db_path(%s) failed: %s", db_id, e)
    return None


_state_cache: Dict[str, bool] = {}  # path -> is_encrypted


def file_is_encrypted(path: str) -> bool:
    """True if the file exists, is non-empty, and is NOT a plaintext SQLite file."""
    if path in _state_cache:
        return _state_cache[path]
    enc = False
    try:
        if os.path.exists(path) and os.path.getsize(path) > 0:
            with open(path, "rb") as f:
                head = f.read(16)
            enc = head != _SQLITE_MAGIC
    except Exception:
        enc = False
    _state_cache[path] = enc
    return enc


def _absent_or_empty(path: str) -> bool:
    try:
        return (not os.path.exists(path)) or os.path.getsize(path) == 0
    except Exception:
        return True


def _pragma_key_sql(hexkey: str) -> str:
    # 32-byte raw key as 64 hex chars → SQLCipher raw-key form (no KDF).
    return f"PRAGMA key=\"x'{hexkey}'\""


def _resolve_key(db_id: str, path: str) -> Optional[str]:
    """Decide whether to apply a key for this file, and return the hex key or None.

    Driven by ACTUAL file state so it never fails on config/file drift:
      * existing encrypted file → return its key (must unlock it);
      * absent/empty file the config wants encrypted → mint a key (create encrypted);
      * plaintext file → None (open as plaintext; reconcile will migrate it).
    """
    if not sqlcipher_available():
        return None
    if file_is_encrypted(path):
        key = db_keys.get_raw_key(db_id)
        if key is None:
            logger.error(
                "Database '%s' file is encrypted but no key is available in the vault "
                "(%s) — cannot open.", db_id, path)
        return key
    if is_enabled(db_id) and _absent_or_empty(path):
        return db_keys.get_or_create_raw_key(db_id)
    return None


# ── The connection entry point (drop-in for sqlite3.connect) ─────────────────
def connect(
    path: str,
    db_id: str,
    *,
    attaches: Sequence[Tuple[str, str, str]] = (),
    **connect_kwargs,
):
    """Open ``path`` (as database ``db_id``), applying SQLCipher keys as needed.

    ``attaches`` is a sequence of ``(alias, file_path, attach_db_id)`` to ATTACH
    onto this connection (e.g. the local backend attaches the vault). Each attach
    is keyed independently based on its own file state.

    When neither the main file nor any attach is encrypted, this returns a plain
    ``sqlite3`` connection identical to calling ``sqlite3.connect`` directly.
    """
    main_key = _resolve_key(db_id, path)
    attach_keys: List[Tuple[str, str, Optional[str]]] = []
    for alias, apath, adb_id in attaches:
        attach_keys.append((alias, apath, _resolve_key(adb_id, apath)))

    use_cipher = main_key is not None or any(k is not None for _, _, k in attach_keys)
    drv = _driver(use_cipher)
    conn = drv.connect(path, **connect_kwargs)
    # Match the row factory to the driver: stdlib sqlite3.Row rejects a SQLCipher
    # cursor, so under the cipher driver use ITS Row (both are dict-like). Callers
    # therefore must NOT re-assign row_factory after calling connect().
    conn.row_factory = drv.Row

    # PRAGMA key must be the FIRST statement on the connection.
    if main_key is not None:
        conn.execute(_pragma_key_sql(main_key))

    for alias, apath, akey in attach_keys:
        try:
            if not use_cipher:
                conn.execute(f"ATTACH DATABASE ? AS {alias}", (apath,))
            elif akey is not None:
                conn.execute(f"ATTACH DATABASE ? AS {alias} KEY \"x'{akey}'\"", (apath,))
            else:
                # Plaintext attach onto a SQLCipher connection needs an empty key.
                conn.execute(f"ATTACH DATABASE ? AS {alias} KEY ''", (apath,))
        except Exception as e:
            logger.debug("ATTACH %s (%s) failed: %s", alias, apath, e)
    return conn


# ── Migration / reconcile ────────────────────────────────────────────────────
def _checkpoint_plain(path: str) -> None:
    """Fold any WAL sidecar back into the main plaintext file, then drop sidecars."""
    try:
        c = sqlite3.connect(path)
        try:
            c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            c.close()
    except Exception as e:
        logger.debug("checkpoint(plain) %s: %s", path, e)
    _drop_sidecars(path)


def _checkpoint_encrypted(path: str, hexkey: str) -> None:
    try:
        c = _cipher_mod.connect(path)
        try:
            c.execute(_pragma_key_sql(hexkey))
            c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            c.close()
    except Exception as e:
        logger.debug("checkpoint(enc) %s: %s", path, e)
    _drop_sidecars(path)


def _drop_sidecars(path: str) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        try:
            sp = path + suffix
            if os.path.exists(sp):
                os.remove(sp)
        except Exception:
            pass


def _schema_count(conn) -> int:
    try:
        return conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type IN ('table','index','trigger','view')"
        ).fetchone()[0]
    except Exception:
        return -1


def _encrypt_file(db_id: str, path: str) -> dict:
    """Convert a plaintext SQLite file to an encrypted one in place (atomic)."""
    if not sqlcipher_available():
        return {"db": db_id, "ok": False, "error": "sqlcipher-unavailable"}
    if _absent_or_empty(path):
        # Nothing to migrate; a fresh file will be created encrypted on first open.
        return {"db": db_id, "ok": True, "action": "none-empty"}
    hexkey = db_keys.get_or_create_raw_key(db_id)
    _checkpoint_plain(path)
    tmp = path + ".enc-tmp"
    try:
        if os.path.exists(tmp):
            os.remove(tmp)
    except Exception:
        pass
    try:
        src = _cipher_mod.connect(path)  # opened plaintext (no key)
        try:
            expected = _schema_count(src)
            src.execute(f"ATTACH DATABASE ? AS enc KEY \"x'{hexkey}'\"", (tmp,))
            src.execute("SELECT sqlcipher_export('enc')")
            src.execute("DETACH DATABASE enc")
        finally:
            src.close()
        # Sanity: the new file must open WITH the key, pass integrity, and match schema.
        chk = _cipher_mod.connect(tmp)
        try:
            chk.execute(_pragma_key_sql(hexkey))
            ok_integrity = chk.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            got = _schema_count(chk)
        finally:
            chk.close()
        if not ok_integrity or (expected >= 0 and got != expected):
            os.remove(tmp)
            return {"db": db_id, "ok": False, "error": f"verify-failed integrity={ok_integrity} schema {got}/{expected}"}
    except Exception as e:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return {"db": db_id, "ok": False, "error": str(e)}

    # Atomic swap: move plaintext aside, put encrypted in place, then shred the plaintext.
    backup = path + ".plain-premigrate"
    try:
        os.replace(path, backup)
        os.replace(tmp, path)
    except Exception as e:
        # Roll back if possible.
        try:
            if not os.path.exists(path) and os.path.exists(backup):
                os.replace(backup, path)
        except Exception:
            pass
        return {"db": db_id, "ok": False, "error": f"swap-failed: {e}"}
    _drop_sidecars(path)
    _secure_delete(backup)
    _state_cache[path] = True
    logger.info("Encrypted database '%s' at rest (%s)", db_id, path)
    return {"db": db_id, "ok": True, "action": "encrypted"}


def _decrypt_file(db_id: str, path: str) -> dict:
    """Convert an encrypted SQLite file back to plaintext in place (atomic)."""
    if not sqlcipher_available():
        return {"db": db_id, "ok": False, "error": "sqlcipher-unavailable"}
    if not file_is_encrypted(path):
        return {"db": db_id, "ok": True, "action": "already-plain"}
    hexkey = db_keys.get_raw_key(db_id)
    if hexkey is None:
        return {"db": db_id, "ok": False, "error": "no-key (cannot decrypt)"}
    _checkpoint_encrypted(path, hexkey)
    tmp = path + ".plain-tmp"
    try:
        if os.path.exists(tmp):
            os.remove(tmp)
    except Exception:
        pass
    try:
        src = _cipher_mod.connect(path)
        try:
            src.execute(_pragma_key_sql(hexkey))
            expected = _schema_count(src)
            src.execute(f"ATTACH DATABASE ? AS plain KEY ''", (tmp,))
            src.execute("SELECT sqlcipher_export('plain')")
            src.execute("DETACH DATABASE plain")
        finally:
            src.close()
        chk = sqlite3.connect(tmp)
        try:
            ok_integrity = chk.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            got = _schema_count(chk)
        finally:
            chk.close()
        if not ok_integrity or (expected >= 0 and got != expected):
            os.remove(tmp)
            return {"db": db_id, "ok": False, "error": f"verify-failed integrity={ok_integrity} schema {got}/{expected}"}
    except Exception as e:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return {"db": db_id, "ok": False, "error": str(e)}

    backup = path + ".enc-premigrate"
    try:
        os.replace(path, backup)
        os.replace(tmp, path)
    except Exception as e:
        try:
            if not os.path.exists(path) and os.path.exists(backup):
                os.replace(backup, path)
        except Exception:
            pass
        return {"db": db_id, "ok": False, "error": f"swap-failed: {e}"}
    _drop_sidecars(path)
    try:
        os.remove(backup)  # encrypted backup — safe to just remove
    except Exception:
        pass
    _state_cache[path] = False
    logger.info("Decrypted database '%s' back to plaintext (%s)", db_id, path)
    return {"db": db_id, "ok": True, "action": "decrypted"}


def _secure_delete(path: str) -> None:
    """Best-effort overwrite-then-delete of a plaintext file we're discarding."""
    try:
        if os.path.exists(path):
            size = os.path.getsize(path)
            with open(path, "r+b", buffering=0) as f:
                f.write(os.urandom(min(size, 1 << 20)))
                f.flush()
                os.fsync(f.fileno())
            os.remove(path)
    except Exception:
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass


def reconcile() -> List[dict]:
    """Make every database file match its configured encrypted-state.

    Run ONCE at startup, before any store opens its files. Returns a list of
    per-database action results. Never raises (a logging/seed step must not block
    boot); a failed migration leaves that file untouched.
    """
    actions: List[dict] = []
    if not sqlcipher_available():
        if any_enabled():
            logger.warning(
                "Full-DB encryption is configured on for %s but the SQLCipher engine "
                "is not installed — databases left as-is.",
                [k for k, v in load_config().items() if v],
            )
        return actions
    _state_cache.clear()
    for db_id in DB_IDS:
        path = db_path(db_id)
        if not path:
            continue
        desired = is_enabled(db_id)
        actual = file_is_encrypted(path)
        try:
            if desired and not actual:
                actions.append(_encrypt_file(db_id, path))
            elif not desired and actual:
                actions.append(_decrypt_file(db_id, path))
        except Exception as e:  # pragma: no cover - defensive
            actions.append({"db": db_id, "ok": False, "error": str(e)})
            logger.warning("reconcile(%s) failed: %s", db_id, e)
    _state_cache.clear()
    # Drop any cached backend so the next open uses the new on-disk state.
    try:
        from app.db import reset_db_instance
        reset_db_instance()
    except Exception:
        pass
    return actions


# ── Status (for the admin UI) ────────────────────────────────────────────────
def can_enable() -> Tuple[bool, Optional[str]]:
    """Whether full-DB encryption can be turned on here, and why not if not."""
    if _env_locked():
        return False, "Configuration is env-locked on this deployment."
    if not sqlcipher_available():
        return False, "The SQLCipher engine is not installed on this host."
    # Full-DB encryption holds its per-file keys in the OS keyring DIRECTLY (see
    # app.db.db_keys — it reads the keyring, not the async secrets vault API). So
    # the only real requirement is a secure OS keyring to hold those keys; the
    # *secrets-vault* backend mode (inline_db vs os_keyring, used for integration
    # tokens) is a separate subsystem and must NOT gate this. Gating on it wrongly
    # disabled the whole panel on installs whose vault runs in inline_db mode even
    # though their databases were already keyring-encrypted and fully readable.
    try:
        from app.secrets import keyring_is_secure
        if not keyring_is_secure():
            return False, "Full-DB encryption requires a secure OS keyring to hold the database keys."
    except Exception as e:
        return False, f"Keyring unavailable: {e}"
    return True, None


def status() -> dict:
    """Per-database + global status for the Data Management panel."""
    ok, reason = can_enable()
    dbs = []
    for db_id in DB_IDS:
        path = db_path(db_id)
        dbs.append({
            "id": db_id,
            "label": DB_LABELS.get(db_id, db_id),
            "enabled": is_enabled(db_id),
            "encrypted_on_disk": bool(path) and file_is_encrypted(path),
            "exists": bool(path) and os.path.exists(path),
        })
    return {
        "sqlcipher_available": sqlcipher_available(),
        "can_enable": ok,
        "reason": reason,
        "env_locked": _env_locked(),
        "databases": dbs,
    }
