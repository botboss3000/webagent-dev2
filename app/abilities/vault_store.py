"""Agent-defined vault keys — the "Vault Capture" store behind the Visualizer.

WHY THIS EXISTS. app/abilities/credentials.py stores secrets for an ability that
DECLARES a ``credentials`` block in its descriptor. That's perfect for a fixed
ability (browser_control's login), but the design agent building a Gmail/Stripe/
whatever dashboard needs to invent a brand-new credential ON THE FLY — name it,
say where it's used, get a stable *key id* back — WITHOUT ever seeing the secret
the user types. This module is that generalized store.

THE CONTRACT (mirrors credentials.py so it's the same encrypted vault row shape):
  • The agent calls ``request_credential`` → we ``reserve`` a key: a row in the
    encrypted vault (``auth_elements``) under the SIGNED-IN USER, carrying only
    PUBLIC metadata (name, the fields a form should collect, and the *binding*
    that says where/how the secret attaches when used). No secret yet.
  • The user types the secret into the secure card → the browser POSTs it
    STRAIGHT to the server (``save_values``) → it lands in the encrypted
    ``secret_ref``. It never passes through the agent / the chat transcript / a
    log. The agent only ever holds the key id.
  • The dashboard later uses it by id through the server-side proxy
    (app/api/genui.py ``/vault/proxy``): ``read_secret`` hands the plaintext to
    the OUTBOUND call only, server-side, attached per the binding. The key is
    locked to its bound ``base_url`` so it can't be redirected elsewhere.

Vault row layout (one per key id, same table the Abilities → Credentials panel
uses, so encryption-at-rest is identical):
  user_id    = <signed-in user>            (user-scoped, the chosen vault)
  service    = "genui_vault"              (this store's namespace)
  label      = <key_id>                    (the stable handle the agent gets back)
  config     = {name, fields[], binding{}} (PLAINTEXT metadata — agent may read)
  secret_ref = "<value>" | {field: value}  (ENCRYPTED — agent may NEVER read)
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# This store's namespace in the shared auth_elements vault. One row per key id.
VAULT_SERVICE = "genui_vault"

# How a stored secret attaches to an outbound request. The agent picks one when
# it requests the credential; the proxy is the ONLY place that ever expands it.
#   bearer            → Authorization: Bearer <secret>
#   basic             → Authorization: Basic <secret>   (secret is the b64 blob)
#   header:<Name>     → <Name>: <secret>                (e.g. header:X-Api-Key)
#   query:<param>     → ?<param>=<secret>               (e.g. query:key)
_ATTACH_RE = re.compile(r"^(bearer|basic|header:[A-Za-z0-9\-_]+|query:[A-Za-z0-9\-_]+)$")


# ── slug / validation ────────────────────────────────────────────────────────

def slugify_key_id(name: str) -> str:
    """Turn a human name ("Gmail API Key") into a stable key id ("gmail_api_key")."""
    s = re.sub(r"[^a-z0-9]+", "_", (name or "").strip().lower()).strip("_")
    s = re.sub(r"_+", "_", s)
    return s[:48] or "key"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def normalize_fields(fields: Optional[list], name: str) -> List[dict]:
    """Coerce the agent-supplied field list into a clean [{key,label,secret}].
    Defaults to a single secret field when nothing usable is given."""
    out: List[dict] = []
    for f in (fields or []):
        if not isinstance(f, dict):
            continue
        key = str(f.get("key") or "").strip()
        if not key:
            continue
        out.append({
            "key": re.sub(r"[^A-Za-z0-9_]+", "_", key)[:40] or "value",
            "label": str(f.get("label") or key).strip()[:80],
            "secret": bool(f.get("secret", True)),
        })
    if not out:
        out = [{"key": "secret", "label": (name or "Secret").strip()[:80], "secret": True}]
    return out


def _secret_keys(fields: list) -> List[str]:
    return [f["key"] for f in fields if f.get("secret")]


def normalize_binding(binding: Optional[dict], fields: List[dict]) -> dict:
    """Validate/clean the {base_url, attach, secret_field} binding. The binding is
    what locks a key to one destination + one attach style, so the proxy can never
    be tricked into leaking the secret somewhere else."""
    b = binding if isinstance(binding, dict) else {}
    base_url = str(b.get("base_url") or "").strip()
    attach = str(b.get("attach") or "bearer").strip()
    if not _ATTACH_RE.match(attach):
        attach = "bearer"
    secret_fields = _secret_keys(fields)
    secret_field = str(b.get("secret_field") or "").strip()
    if secret_field not in secret_fields:
        secret_field = secret_fields[0] if secret_fields else "secret"
    return {"base_url": base_url, "attach": attach, "secret_field": secret_field}


def _pack_secrets(secrets: Dict[str, str], secret_keys: list) -> str:
    """Single secret → its raw value; multiple → a JSON object (mirrors credentials.py)."""
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


# ── read/write ───────────────────────────────────────────────────────────────

async def _get_row(user_id: str, key_id: str) -> Optional[dict]:
    if not user_id or not key_id:
        return None
    try:
        from app.db import get_db
        return await get_db().auth_element_get(user_id, VAULT_SERVICE, key_id)
    except Exception as e:
        logger.debug("vault row read failed for %s/%s: %s", user_id, key_id, e)
        return None


async def reserve_key(
    user_id: str,
    *,
    name: str,
    binding: Optional[dict] = None,
    fields: Optional[list] = None,
    key_id: Optional[str] = None,
) -> Optional[dict]:
    """Create (or re-describe) a vault key, storing ONLY public metadata. Never
    touches an already-stored secret — re-requesting the same id updates its
    name/fields/binding but keeps whatever the user previously saved.
    Returns the public metadata (incl. the resolved key id), or None on failure."""
    if not user_id:
        return None
    name = (name or "").strip() or "Credential"
    key_id = slugify_key_id(key_id or name)
    clean_fields = normalize_fields(fields, name)
    clean_binding = normalize_binding(binding, clean_fields)

    existing = await _get_row(user_id, key_id)
    existing_secret = existing.get("secret_ref") if existing else ""
    created_at = _as_dict(existing.get("config")).get("created_at") if existing else None

    config = {
        "name": name,
        "fields": clean_fields,
        "binding": clean_binding,
        "created_at": created_at or _now_iso(),
        "updated_at": _now_iso(),
    }
    try:
        from app.db import get_db
        await get_db().auth_element_set(
            user_id=user_id, service=VAULT_SERVICE, config=config,
            secret_ref=existing_secret or "", label=key_id,
        )
    except Exception as e:
        logger.warning("vault reserve failed for %s/%s: %s", user_id, key_id, e)
        return None
    return public_meta(key_id, config, bool(str(existing_secret or "").strip()))


async def save_values(user_id: str, key_id: str, values: dict) -> bool:
    """Persist secret values the user typed. A BLANK secret keeps the stored one
    (so the card never has to echo a secret back to edit). Browser→server only."""
    row = await _get_row(user_id, key_id)
    if not row:
        return False
    config = _as_dict(row.get("config"))
    fields = config.get("fields") or []
    secret_keys = _secret_keys(fields)
    existing = _unpack_secrets(row.get("secret_ref") or "", secret_keys)
    values = values or {}

    secrets: dict = {}
    for k in secret_keys:
        v = values.get(k)
        secrets[k] = existing.get(k, "") if (v is None or str(v) == "") else str(v)

    try:
        from app.db import get_db
        await get_db().auth_element_set(
            user_id=user_id, service=VAULT_SERVICE, config=config,
            secret_ref=_pack_secrets(secrets, secret_keys), label=key_id,
        )
    except Exception as e:
        logger.warning("vault save failed for %s/%s: %s", user_id, key_id, e)
        return False
    return True


async def delete_key(user_id: str, key_id: str) -> bool:
    try:
        from app.db import get_db
        return await get_db().auth_element_delete(user_id, VAULT_SERVICE, key_id)
    except Exception as e:
        logger.warning("vault delete failed for %s/%s: %s", user_id, key_id, e)
        return False


# ── views ────────────────────────────────────────────────────────────────────

def public_meta(key_id: str, config: dict, filled: bool) -> dict:
    """The agent-safe / card-safe shape: metadata + filled flag, NEVER a value."""
    binding = config.get("binding") or {}
    return {
        "key_id": key_id,
        "name": config.get("name") or key_id,
        "fields": config.get("fields") or [],
        "service": binding.get("base_url") or "",
        "attach": binding.get("attach") or "bearer",
        "filled": bool(filled),
        "created_at": config.get("created_at"),
    }


async def public_view(user_id: str, key_id: str) -> Optional[dict]:
    """Public metadata for one key (or None if it doesn't exist). No secrets."""
    row = await _get_row(user_id, key_id)
    if not row:
        return None
    config = _as_dict(row.get("config"))
    fields = config.get("fields") or []
    secret_keys = _secret_keys(fields)
    secrets = _unpack_secrets(row.get("secret_ref") or "", secret_keys)
    filled = all(str(secrets.get(k, "")).strip() for k in secret_keys) if secret_keys else False
    view = public_meta(key_id, config, filled)
    view["secrets_set"] = {k: bool(str(secrets.get(k, "")).strip()) for k in secret_keys}
    return view


async def list_keys(user_id: str) -> List[dict]:
    """Every vault key this user has, as agent-safe metadata. No secret values."""
    if not user_id:
        return []
    try:
        from app.db import get_db
        rows = await get_db().auth_element_list(user_id, VAULT_SERVICE)
    except Exception as e:
        logger.debug("vault list failed for %s: %s", user_id, e)
        return []
    out: List[dict] = []
    for row in rows or []:
        config = _as_dict(row.get("config"))
        fields = config.get("fields") or []
        secret_keys = _secret_keys(fields)
        secrets = _unpack_secrets(row.get("secret_ref") or "", secret_keys)
        filled = all(str(secrets.get(k, "")).strip() for k in secret_keys) if secret_keys else False
        out.append(public_meta(row.get("label") or "", config, filled))
    return out


async def read_secret(user_id: str, key_id: str) -> Optional[dict]:
    """SERVER-SIDE ONLY. The plaintext + binding, for the proxy to attach to an
    outbound call. Never return this through any agent-facing or browser-facing
    path — only the proxy endpoint calls it."""
    row = await _get_row(user_id, key_id)
    if not row:
        return None
    config = _as_dict(row.get("config"))
    fields = config.get("fields") or []
    secret_keys = _secret_keys(fields)
    secrets = _unpack_secrets(row.get("secret_ref") or "", secret_keys)
    return {
        "name": config.get("name") or key_id,
        "binding": config.get("binding") or {},
        "secrets": secrets,
    }
