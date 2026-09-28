"""Generic credential storage for credential-bearing abilities.

THE COMMON PROCESS. An ability that needs secrets (an API key, pasted cookies,
…) declares a ``credentials`` block in its ``<id>.json`` descriptor instead of
hand-rolling its own save endpoint + config card. Example::

    "credentials": {
      "scope": "admin",                 # admin | user | agent
      "requires": ["api_key"],          # keys that must be set to count configured
      "fields": [
        {"key": "provider", "label": "Provider", "type": "select",
         "options": ["apify", "rapidapi", "custom_http"]},
        {"key": "api_key",  "label": "API Key",  "type": "password", "secret": true},
        {"key": "endpoint", "label": "Endpoint", "type": "text"}
      ]
    }

This module is the ONE place that maps ``(ability, scope, user, agent)`` to a
vault row and reads/writes it, reusing the encrypted vault (``auth_elements``)
via ``db.auth_element_*``. Secret fields land in the encrypted ``secret_ref``
(JSON when more than one); non-secret fields land in plaintext ``config``.

Three scopes mirror how the app already stores secrets:
  • admin — one set per app   → user_id=admin, label="default"
  • user  — per end-user      → user_id=<caller>,       label="default"
  • agent — per agent         → user_id=<caller>,       label="agent:<agent_id>"

Endpoints (app/api/agents.py): GET/POST/DELETE /api/v1/abilities/{id}/credentials.
Gating: app/admin/integrations.get_admin_configured_providers() calls
``is_configured`` so an ability's tools only load once its creds are present.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Mirror of app.admin.integrations._ADMIN_USER — the synthetic owner of
# app-global secrets. Kept literal here to avoid importing that heavy module.
_ADMIN_USER = "admin"


# ── Back-compat: legacy vault rows from before the framework ─────────────────
# The two first consumers stored their creds under bespoke service keys. When a
# new-style row is absent we transparently read the legacy row so already-saved
# creds keep working with no migration. Field keys were chosen to MATCH the
# legacy config keys, so the only special handling is the single secret field.
#   ability_id -> (legacy_service, legacy_scope)
_LEGACY_SERVICE: Dict[str, tuple] = {
    "web_scraper":      ("scraper_config",  "admin"),
    "browser_cookies":  ("browser_session", "user"),
    # Browser Cookies folded into Browser Control — its optional pasted-cookie
    # fallback reads the same legacy row so cookies pasted before the merge work.
    "browser_control":  ("browser_session", "user"),
}


def _service(ability_id: str) -> str:
    return f"ability_cred:{ability_id}"


def ability_credentials_spec(ability_id: str) -> Optional[dict]:
    """The descriptor's ``credentials`` block (or None). Lazy import avoids a
    circular import with the package ``__init__``."""
    try:
        from app.abilities import ability_credentials_spec as _spec
        return _spec(ability_id)
    except Exception:
        return None


def _as_dict(raw: Any) -> dict:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return dict(parsed) if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _secret_keys(fields: list) -> list:
    return [f["key"] for f in fields if f.get("secret")]


def _pack_secrets(secrets: Dict[str, str], secret_keys: list) -> str:
    """Single secret → its raw value; multiple → a JSON object."""
    if not secret_keys:
        return ""
    if len(secret_keys) == 1:
        return secrets.get(secret_keys[0], "") or ""
    return json.dumps({k: secrets.get(k, "") for k in secret_keys})


def _unpack_secrets(secret_ref: str, secret_keys: list) -> Dict[str, str]:
    if not secret_ref or not secret_keys:
        return {}
    if len(secret_keys) == 1:
        return {secret_keys[0]: secret_ref}
    try:
        parsed = json.loads(secret_ref)
        return {k: str(parsed.get(k, "")) for k in secret_keys} if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _resolve_location(ability_id: str, scope: str, user_id: str, agent_id: str) -> tuple:
    """(vault_user_id, service, label) for the declared scope."""
    service = _service(ability_id)
    if scope == "admin":
        return _ADMIN_USER, service, "default"
    if scope == "agent":
        return user_id, service, f"agent:{agent_id}"
    return user_id, service, "default"  # user (default)


async def _read_legacy(ability_id: str, user_id: str, secret_keys: list) -> dict:
    """Read the pre-framework vault row for the two original consumers."""
    leg = _LEGACY_SERVICE.get(ability_id)
    if not leg:
        return {}
    legacy_service, legacy_scope = leg
    uid = _ADMIN_USER if legacy_scope == "admin" else user_id
    if not uid:
        return {}
    try:
        from app.db import get_db
        elem = await get_db().auth_element_get(uid, legacy_service, "default")
    except Exception as e:
        logger.debug("legacy cred read failed for %s: %s", ability_id, e)
        return {}
    if not elem:
        return {}
    out = _as_dict(elem.get("config"))
    if secret_keys:
        out[secret_keys[0]] = elem.get("secret_ref") or ""
    return out


async def read_credentials(ability_id: str, *, user_id: str = "", agent_id: str = "") -> dict:
    """Full credential dict (INCLUDING secret values) for the ability's runtime.
    Falls back to the legacy vault row when no new-style row exists."""
    spec = ability_credentials_spec(ability_id)
    if not spec:
        return {}
    scope = spec.get("scope", "user")
    fields = spec.get("fields", []) or []
    secret_keys = _secret_keys(fields)
    uid, service, label = _resolve_location(ability_id, scope, user_id, agent_id)
    try:
        from app.db import get_db
        elem = await get_db().auth_element_get(uid, service, label) if uid else None
    except Exception as e:
        logger.debug("cred read failed for %s: %s", ability_id, e)
        elem = None
    if not elem:
        return await _read_legacy(ability_id, user_id, secret_keys)
    out = _as_dict(elem.get("config"))
    out.update(_unpack_secrets(elem.get("secret_ref") or "", secret_keys))
    return out


async def save_credentials(ability_id: str, values: dict, *, user_id: str = "", agent_id: str = "") -> bool:
    """Persist credential field values. A BLANK secret field means 'leave the
    stored secret unchanged' (so the UI never has to echo it back to edit)."""
    spec = ability_credentials_spec(ability_id)
    if not spec:
        return False
    scope = spec.get("scope", "user")
    fields = spec.get("fields", []) or []
    secret_keys = _secret_keys(fields)
    values = values or {}

    # Preserve existing secrets when the incoming value for a secret is blank.
    existing = await read_credentials(ability_id, user_id=user_id, agent_id=agent_id)

    config: dict = {}
    secrets: dict = {}
    for f in fields:
        k = f["key"]
        v = values.get(k)
        if f.get("secret"):
            if v is None or str(v) == "":
                secrets[k] = existing.get(k, "")  # keep what's stored
            else:
                secrets[k] = str(v)
        else:
            config[k] = "" if v is None else v

    uid, service, label = _resolve_location(ability_id, scope, user_id, agent_id)
    if not uid:
        return False
    try:
        from app.db import get_db
        await get_db().auth_element_set(
            user_id=uid, service=service, config=config,
            secret_ref=_pack_secrets(secrets, secret_keys), label=label,
        )
    except Exception as e:
        logger.warning("cred save failed for %s: %s", ability_id, e)
        return False
    return True


async def delete_credentials(ability_id: str, *, user_id: str = "", agent_id: str = "") -> bool:
    """Clear both the new-style row and any legacy row."""
    spec = ability_credentials_spec(ability_id)
    if not spec:
        return False
    scope = spec.get("scope", "user")
    uid, service, label = _resolve_location(ability_id, scope, user_id, agent_id)
    try:
        from app.db import get_db
        db = get_db()
        if uid:
            await db.auth_element_delete(uid, service, label)
        leg = _LEGACY_SERVICE.get(ability_id)
        if leg:
            luid = _ADMIN_USER if leg[1] == "admin" else user_id
            if luid:
                try:
                    await db.auth_element_delete(luid, leg[0], "default")
                except Exception:
                    pass
    except Exception as e:
        logger.warning("cred delete failed for %s: %s", ability_id, e)
        return False
    return True


async def is_configured(ability_id: str, *, user_id: str = "", agent_id: str = "") -> bool:
    """True when every required field is present. Defaults the required set to
    the secret fields when the descriptor omits ``requires``."""
    spec = ability_credentials_spec(ability_id)
    if not spec:
        return False
    fields = spec.get("fields", []) or []
    required = spec.get("requires") or _secret_keys(fields)
    if not required:
        return False
    creds = await read_credentials(ability_id, user_id=user_id, agent_id=agent_id)
    return all(str(creds.get(k, "")).strip() for k in required)


async def public_view(ability_id: str, *, user_id: str = "", agent_id: str = "") -> Optional[dict]:
    """Render payload for the GET endpoint. NEVER returns secret values — only
    the declared fields, non-secret values, and a {secret_key: bool} 'is set' map."""
    spec = ability_credentials_spec(ability_id)
    if not spec:
        return None
    fields = spec.get("fields", []) or []
    creds = await read_credentials(ability_id, user_id=user_id, agent_id=agent_id)
    values: dict = {}
    secrets_set: dict = {}
    for f in fields:
        k = f["key"]
        if f.get("secret"):
            secrets_set[k] = bool(str(creds.get(k, "")).strip())
        else:
            values[k] = creds.get(k, "")
    required = spec.get("requires") or _secret_keys(fields)
    configured = bool(required) and all(str(creds.get(k, "")).strip() for k in required)
    return {
        "scope": spec.get("scope", "user"),
        "fields": fields,
        "values": values,
        "secrets_set": secrets_set,
        "configured": configured,
    }
