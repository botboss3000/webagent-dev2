"""Vault-backed credential storage for DNS providers.

The API keys a DNS provider needs (a Cloudflare token, a Namecheap API key, AWS
keys, a Google service-account JSON) are SECRETS, so they go into the SAME
encrypted vault (``auth_elements``) every other secret in the app uses — never a
new store, never the plaintext ``dns.json``. This module maps a DNS provider id
to its vault row and reads/writes it, a near-exact twin of
``app/deploy/credentials.py`` but keyed by ``dns_cred:<id>`` (always admin-scoped:
pointing the app's domain is an admin power).

Vault layout (admin scope): ``user_id="admin"``, ``service="dns_cred:<id>"``,
``label="default"``. Secret fields land in the encrypted ``secret_ref`` (JSON
when more than one); non-secret cred fields land in plaintext ``config``.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_ADMIN_USER = "admin"


def _service(provider_id: str) -> str:
    return f"dns_cred:{provider_id}"


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


def _secret_keys(fields: List[dict]) -> List[str]:
    return [f["key"] for f in fields if f.get("secret")]


def _pack_secrets(secrets: Dict[str, str], secret_keys: List[str]) -> str:
    if not secret_keys:
        return ""
    if len(secret_keys) == 1:
        return secrets.get(secret_keys[0], "") or ""
    return json.dumps({k: secrets.get(k, "") for k in secret_keys})


def _unpack_secrets(secret_ref: str, secret_keys: List[str]) -> Dict[str, str]:
    if not secret_ref or not secret_keys:
        return {}
    if len(secret_keys) == 1:
        return {secret_keys[0]: secret_ref}
    try:
        parsed = json.loads(secret_ref)
        return {k: str(parsed.get(k, "")) for k in secret_keys} if isinstance(parsed, dict) else {}
    except Exception:
        return {}


async def read(provider_id: str, fields: List[dict]) -> Dict[str, str]:
    """Full credential dict INCLUDING secret values, for the runtime."""
    secret_keys = _secret_keys(fields)
    try:
        from app.db import get_db
        elem = await get_db().auth_element_get(_ADMIN_USER, _service(provider_id), "default")
    except Exception as e:
        logger.debug("dns cred read failed for %s: %s", provider_id, e)
        elem = None
    if not elem:
        return {}
    out = _as_dict(elem.get("config"))
    out.update(_unpack_secrets(elem.get("secret_ref") or "", secret_keys))
    return out


async def save(provider_id: str, fields: List[dict], values: dict) -> bool:
    """Persist cred field values. A BLANK secret means 'leave the stored secret
    unchanged' (so the UI never has to echo a secret back to edit it)."""
    secret_keys = _secret_keys(fields)
    values = values or {}
    existing = await read(provider_id, fields)

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
    try:
        from app.db import get_db
        await get_db().auth_element_set(
            user_id=_ADMIN_USER, service=_service(provider_id),
            config=config, secret_ref=_pack_secrets(secrets, secret_keys),
            label="default",
        )
    except Exception as e:
        logger.warning("dns cred save failed for %s: %s", provider_id, e)
        return False
    return True


async def forget(provider_id: str) -> bool:
    """Delete the whole vault row (disconnecting the account)."""
    try:
        from app.db import get_db
        await get_db().auth_element_delete(_ADMIN_USER, _service(provider_id), "default")
    except Exception as e:
        logger.warning("dns cred forget failed for %s: %s", provider_id, e)
        return False
    return True


async def is_configured(provider_id: str, fields: List[dict],
                        required: Optional[List[str]] = None) -> bool:
    req = required if required is not None else _secret_keys(fields)
    if not req:
        return False
    creds = await read(provider_id, fields)
    return all(str(creds.get(k, "")).strip() for k in req)


async def public_view(provider_id: str, fields: List[dict],
                      required: Optional[List[str]] = None) -> dict:
    """For the GET endpoint: the declared fields, non-secret values, and a
    {secret_key: bool} 'is set' map — NEVER the secret values themselves."""
    creds = await read(provider_id, fields)
    values: dict = {}
    secrets_set: dict = {}
    for f in fields:
        k = f["key"]
        if f.get("secret"):
            secrets_set[k] = bool(str(creds.get(k, "")).strip())
        else:
            values[k] = creds.get(k, "")
    req = required if required is not None else _secret_keys(fields)
    configured = bool(req) and all(str(creds.get(k, "")).strip() for k in req)
    return {"values": values, "secrets_set": secrets_set, "configured": configured}
