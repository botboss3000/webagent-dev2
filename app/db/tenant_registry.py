"""
Tenant registry — the map from a user to THEIR OWN data database.

Part of the per-user bring-your-own-database (multi-tenant) feature. This is the
CONTROL-PLANE lookup that `app/db/tenant.py` consults to decide which Postgres a
given user's interaction data lives in. It is only consulted when App Settings →
User BYOD is ON (see `app.admin.settings.get_user_byod_enabled`).

Storage mirrors the established per-user auth_elements pattern
(app/admin/settings.py): the non-secret connection details live in a JSON file
keyed by user_id, and the DATABASE PASSWORD is kept OUT of that file — stored in
the database-independent credential store (OS keyring / file fallback,
app/db/cred_store.py). The password MUST live outside every application database:
it is the very thing needed to REACH the user's database, so it cannot depend on
reaching a database first (chicken-and-egg). cred_store is exactly that
DB-independent store, and `_resolve_pg_password()` in app/db/__init__.py already
resolves a password from it by `password_secret_key`, so a record written here
plugs straight into the existing Postgres backend builder.

    data/config/tenant_dbs.json
    {
      "<user_id>": {
        "provider": "postgres", "host": "...", "port": 5432,
        "database": "...", "username": "...", "ssl_mode": "require",
        "schema": "public", "options": {},
        "password_secret_key": "tenant_db_pw_<user_id>",
        "status": "active", "last_test_message": "", "last_ok_at": "..."
      },
      ...
    }
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from app.db import cred_store
from app.db.connection_config import DBConnectionConfig
from app.util.config_io import safe_write_json

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
TENANT_DBS_FILE = PROJECT_ROOT / "data" / "config" / "tenant_dbs.json"

# Non-secret fields persisted to tenant_dbs.json (password is NEVER among them).
_RECORD_FIELDS = (
    "provider", "host", "port", "database", "username", "ssl_mode",
    "schema", "options", "password_secret_key",
    "status", "last_test_message", "last_ok_at",
)


def _pw_key(user_id: str) -> str:
    """Credential-store key holding this user's data-database password."""
    return f"tenant_db_pw_{user_id}"


def _load_all() -> dict:
    """Load the whole tenant registry (map of user_id → record)."""
    try:
        if TENANT_DBS_FILE.exists():
            with open(TENANT_DBS_FILE) as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
    except Exception as e:  # noqa: BLE001
        logger.warning("Failed to load tenant_dbs.json: %s", e)
    return {}


def _save_all(data: dict) -> None:
    """Persist the whole tenant registry (creates data/config/ on demand)."""
    try:
        safe_write_json(TENANT_DBS_FILE, data)
    except Exception as e:  # noqa: BLE001
        logger.warning("Failed to save tenant_dbs.json: %s", e)


def get_tenant_record(user_id: str) -> Optional[dict]:
    """The non-secret registry record for one user, or None if they have no
    personal database configured. Never contains the password."""
    if not user_id:
        return None
    rec = _load_all().get(user_id)
    return dict(rec) if isinstance(rec, dict) else None


def list_tenant_records() -> dict:
    """Every user's registry record (non-secret), keyed by user_id. For the admin
    view of who has a personal database configured."""
    return _load_all()


def has_tenant_db(user_id: str) -> bool:
    """True when this user has a personal data database configured."""
    return get_tenant_record(user_id) is not None


def set_tenant_db(user_id: str, cfg: DBConnectionConfig, password: Optional[str]) -> dict:
    """Create/update a user's personal-database record.

    Writes the non-secret connection details to tenant_dbs.json and stashes the
    password (when provided) in the DB-independent credential store. Returns the
    stored (non-secret) record. Passing ``password=None`` keeps any existing
    stored password (e.g. an edit that doesn't change it)."""
    if not user_id:
        raise ValueError("user_id is required")

    pw_key = _pw_key(user_id)
    if password:
        cred_store.set_secret(pw_key, password)

    record = {
        "provider": cfg.provider or "postgres",
        "host": cfg.host,
        "port": cfg.port,
        "database": cfg.database or "",
        "username": cfg.username,
        "ssl_mode": cfg.ssl_mode or "require",
        "schema": getattr(cfg, "schema", "public") or "public",
        "options": cfg.options or {},
        "password_secret_key": pw_key,
        "status": "unverified",
        "last_test_message": "",
        "last_ok_at": None,
    }
    all_recs = _load_all()
    # Preserve status/last_ok fields on update unless the caller resets them.
    prev = all_recs.get(user_id)
    if isinstance(prev, dict):
        for k in ("status", "last_test_message", "last_ok_at"):
            if k in prev and record.get(k) in (None, "", "unverified"):
                record[k] = prev[k]
    all_recs[user_id] = record
    _save_all(all_recs)
    return record


def update_status(user_id: str, status: str, message: str = "", last_ok_at: Optional[str] = None) -> None:
    """Record the outcome of a connection test / live probe for a user's DB."""
    all_recs = _load_all()
    rec = all_recs.get(user_id)
    if not isinstance(rec, dict):
        return
    rec["status"] = status
    rec["last_test_message"] = (message or "")[:300]
    if last_ok_at is not None:
        rec["last_ok_at"] = last_ok_at
    _save_all(all_recs)


def delete_tenant_db(user_id: str) -> bool:
    """Remove a user's personal-database record and its stored password. Returns
    True if a record existed. (Does NOT touch the data inside their database.)"""
    all_recs = _load_all()
    existed = user_id in all_recs
    if existed:
        del all_recs[user_id]
        _save_all(all_recs)
    try:
        cred_store.delete_secret(_pw_key(user_id))
    except Exception as e:  # noqa: BLE001
        logger.debug("Could not delete tenant password for %s: %s", user_id, e)
    return existed


def load_tenant_config(user_id: str) -> Optional[DBConnectionConfig]:
    """Build a DBConnectionConfig for a user's personal database, or None when
    they have no record. The returned config's ``password_secret_key`` points at
    the credential-store entry, so `_resolve_pg_password()` (app/db/__init__.py)
    resolves the actual password the same way it does for the global DB."""
    rec = get_tenant_record(user_id)
    if not rec:
        return None
    return DBConnectionConfig(
        provider=rec.get("provider") or "postgres",
        host=rec.get("host"),
        port=rec.get("port"),
        database=rec.get("database") or "",
        username=rec.get("username"),
        password_secret_key=rec.get("password_secret_key") or _pw_key(user_id),
        ssl_mode=rec.get("ssl_mode") or "require",
        schema=rec.get("schema") or "public",
        options=rec.get("options") or {},
    )


def resolve_tenant_password(user_id: str) -> Optional[str]:
    """Fetch a user's data-database password from the credential store."""
    try:
        return cred_store.get_secret(_pw_key(user_id))
    except Exception as e:  # noqa: BLE001
        logger.debug("Could not resolve tenant password for %s: %s", user_id, e)
        return None
