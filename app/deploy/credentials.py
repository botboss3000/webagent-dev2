"""Vault-backed credential storage for deploy targets — with auto-forget.

The cloud keys a deploy target needs (a Google service-account JSON, AWS keys,
an SSH private key) are SECRETS, so they go into the SAME encrypted vault
(``auth_elements``) every other secret in the app uses — never a new store, never
the plaintext ``deploy.json``. This module is the one place that maps a deploy
provider id to its vault row and reads/writes it, mirroring
``app/abilities/credentials.py`` but keyed by provider (always admin-scoped:
deploying the app is an admin power).

Vault layout (admin scope): ``user_id="admin"``, ``service="deploy_cred:<id>"``,
``label="default"``. Secret fields land in the encrypted ``secret_ref`` (JSON
when more than one); non-secret cred fields land in plaintext ``config``.

EPHEMERAL BY DESIGN. ``forget()`` deletes the whole row — called automatically
after a successful deploy when the target's "Forget keys after deploy" option is
on, so the app borrows a powerful key for one deploy and then holds nothing.

The non-secret sign-in "id" fields (a Google Cloud project id) are handled by the
separate ``read_connect``/``save_connect``/``forget_connect`` trio at the bottom:
they live in their OWN persistent vault row (``deploy_connect:<id>``) so the whole
cloud connection travels with a shared vault DB and the id SURVIVES the key-forget.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_ADMIN_USER = "admin"


def _service(provider_id: str, profile: str = "") -> str:
    # A SAVED SERVER (a named SSH-target profile) gets its own vault row so the
    # panel can keep several servers' logins side by side; the bare
    # ``deploy_cred:<id>`` row stays the "currently-loaded" working slot the deploy
    # runtime reads. profile="" → the working slot (unchanged behaviour).
    base = f"deploy_cred:{provider_id}"
    return f"{base}:{profile}" if profile else base


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


async def read(provider_id: str, fields: List[dict], profile: str = "",
               github_fallback: bool = True) -> Dict[str, str]:
    """Full credential dict INCLUDING secret values, for the deploy runtime.

    ``profile`` selects a saved-server row (``deploy_cred:<id>:<profile>``); blank
    reads the working slot.

    ``github_fallback`` (default on) fills a BLANK ``github_token`` field from the
    reusable, vault-stored GitHub key (see ``get_github_token``), so a cloud target
    that declares a ``github_token`` credential automatically borrows the shared key
    when the admin didn't type a per-deploy one. Turned OFF for the save-preserve
    read (below) so the shared key is never copied into a provider's own row."""
    secret_keys = _secret_keys(fields)
    try:
        from app.db import get_db
        elem = await get_db().auth_element_get(_ADMIN_USER, _service(provider_id, profile), "default")
    except Exception as e:
        logger.debug("deploy cred read failed for %s: %s", provider_id, e)
        elem = None
    out: dict = {}
    if elem:
        out = _as_dict(elem.get("config"))
        out.update(_unpack_secrets(elem.get("secret_ref") or "", secret_keys))
    if github_fallback and any(f.get("key") == "github_token" for f in fields) \
            and not str(out.get("github_token") or "").strip():
        saved = await get_github_token()
        if saved:
            out["github_token"] = saved
    return out


async def save(provider_id: str, fields: List[dict], values: dict, profile: str = "") -> bool:
    """Persist cred field values. A BLANK secret means 'leave the stored secret
    unchanged' (so the UI never has to echo a secret back to edit it). ``profile``
    targets a saved-server row; blank targets the working slot."""
    secret_keys = _secret_keys(fields)
    values = values or {}
    # github_fallback OFF here: a blank github_token must mean "keep this provider's
    # own stored token" (or leave it empty), never "copy in the shared vault key".
    existing = await read(provider_id, fields, profile, github_fallback=False)

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
            user_id=_ADMIN_USER, service=_service(provider_id, profile),
            config=config, secret_ref=_pack_secrets(secrets, secret_keys),
            label="default",
        )
    except Exception as e:
        logger.warning("deploy cred save failed for %s: %s", provider_id, e)
        return False
    return True


async def forget(provider_id: str, profile: str = "") -> bool:
    """Delete the whole vault row (the auto-discard after a successful deploy, or a
    saved server being removed when ``profile`` is given)."""
    try:
        from app.db import get_db
        await get_db().auth_element_delete(_ADMIN_USER, _service(provider_id, profile), "default")
    except Exception as e:
        logger.warning("deploy cred forget failed for %s: %s", provider_id, e)
        return False
    return True


async def is_configured(provider_id: str, fields: List[dict], required: Optional[List[str]] = None,
                        profile: str = "") -> bool:
    req = required if required is not None else _secret_keys(fields)
    if not req:
        return False
    creds = await read(provider_id, fields, profile)
    return all(str(creds.get(k, "")).strip() for k in req)


async def public_view(provider_id: str, fields: List[dict], required: Optional[List[str]] = None,
                      profile: str = "") -> dict:
    """For the GET endpoint: the declared fields, non-secret values, and a
    {secret_key: bool} 'is set' map — NEVER the secret values themselves."""
    creds = await read(provider_id, fields, profile)
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


# ── Persistent connect config (the non-secret sign-in "id" fields) ────────────
# The cloud account IDENTITY — e.g. a Google Cloud project id — is NOT a secret,
# so it normally lives in the plaintext local ``deploy.json``. But to make a whole
# cloud sign-in TRAVEL with a shared vault DB (a second device signed into the same
# vault should get the project id, not just the key), we ALSO mirror those id fields
# into the vault. They get their OWN admin row (``deploy_connect:<id>``), separate
# from the ephemeral working-key row above, so the after-deploy key-forget never
# wipes the identity — only the key is discarded, the project id survives.
_CONNECT_LABEL = "default"


def _connect_service(provider_id: str) -> str:
    return f"deploy_connect:{provider_id}"


async def read_connect(provider_id: str, connect_keys: List[str]) -> Dict[str, str]:
    """The stored connect 'id' fields from the vault (empty for any not set)."""
    if not connect_keys:
        return {}
    try:
        from app.db import get_db
        elem = await get_db().auth_element_get(_ADMIN_USER, _connect_service(provider_id), _CONNECT_LABEL)
    except Exception as e:  # noqa: BLE001
        logger.debug("deploy connect read failed for %s: %s", provider_id, e)
        elem = None
    cfg = _as_dict((elem or {}).get("config"))
    return {k: str(cfg.get(k, "")) for k in connect_keys if str(cfg.get(k, "")).strip()}


async def save_connect(provider_id: str, connect_keys: List[str], values: dict) -> bool:
    """Mirror the non-secret sign-in 'id' fields into the vault so the cloud
    connection travels with a shared vault DB. Merges over whatever is stored (a
    blank/absent value keeps the existing one, so a partial save never wipes it)."""
    if not connect_keys:
        return False
    merged = dict(await read_connect(provider_id, connect_keys))
    for k in connect_keys:
        v = (values or {}).get(k)
        if v is not None and str(v).strip():
            merged[k] = str(v)
    if not merged:
        return False
    try:
        from app.db import get_db
        await get_db().auth_element_set(
            user_id=_ADMIN_USER, service=_connect_service(provider_id),
            config=merged, secret_ref="", label=_CONNECT_LABEL,
        )
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("deploy connect save failed for %s: %s", provider_id, e)
        return False


async def forget_connect(provider_id: str) -> bool:
    """Delete the stored connect 'id' row — the 'Remove account' companion to
    ``forget`` (which drops the key)."""
    try:
        from app.db import get_db
        await get_db().auth_element_delete(_ADMIN_USER, _connect_service(provider_id), _CONNECT_LABEL)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("deploy connect forget failed for %s: %s", provider_id, e)
        return False


# ── Reusable GitHub key (shared across every deploy target) ───────────────────
# One GitHub personal access token, stored ENCRYPTED in the same vault as every
# other secret (never plaintext), that the deploy section reuses for cloning any
# private repo — the access check, the manual install commands, and the cloud
# deploy all fall back to it when no per-deploy token is typed. Admin-scoped, its
# own vault row so it is independent of any single provider or saved server.
_GH_USER = "admin"
_GH_SERVICE = "deploy_github_token"
_GH_LABEL = "default"


async def _gh_read_row() -> tuple:
    """The stored (token, config-dict) for the shared GitHub row, ('' , {}) if none.
    The token is the secret; the config holds the non-secret ``repo_url`` (the clean
    address of the repo the Git Control page tracks) so a new deployment sharing
    the vault gets BOTH the URL and the key — separately, never a tokenized URL."""
    try:
        from app.db import get_db
        elem = await get_db().auth_element_get(_GH_USER, _GH_SERVICE, _GH_LABEL)
    except Exception as e:  # noqa: BLE001
        logger.debug("github deploy key read failed: %s", e)
        return "", {}
    return str((elem or {}).get("secret_ref") or "").strip(), _as_dict((elem or {}).get("config"))


async def get_github_token() -> str:
    """The reusable GitHub key from the vault, or '' when none is stored."""
    tok, _ = await _gh_read_row()
    return tok


async def get_github_repo_url() -> str:
    """The clean repo URL the Git Control page tracks, from the vault (or '')."""
    _, cfg = await _gh_read_row()
    return str(cfg.get("repo_url") or "").strip()


async def save_github_token(token: str) -> bool:
    """Store (or replace) the reusable GitHub key. A blank token is rejected here —
    clearing is an explicit action (``clear_github_token``), so a stray blank save
    never silently wipes the stored key. The stored ``repo_url`` is PRESERVED."""
    token = (token or "").strip()
    if not token:
        return False
    _, cfg = await _gh_read_row()
    try:
        from app.db import get_db
        await get_db().auth_element_set(
            user_id=_GH_USER, service=_GH_SERVICE,
            config=cfg, secret_ref=token, label=_GH_LABEL,
        )
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("github deploy key save failed: %s", e)
        return False


async def save_github_repo_url(url: str) -> bool:
    """Store the clean repo URL the Git Control page tracks, PRESERVING the token.
    A blank URL is ignored (never wipes a stored one)."""
    url = (url or "").strip()
    if not url:
        return False
    token, cfg = await _gh_read_row()
    if cfg.get("repo_url") == url:
        return True   # no change — skip the write
    cfg = dict(cfg)
    cfg["repo_url"] = url
    try:
        from app.db import get_db
        await get_db().auth_element_set(
            user_id=_GH_USER, service=_GH_SERVICE,
            config=cfg, secret_ref=token, label=_GH_LABEL,
        )
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("github repo url save failed: %s", e)
        return False


async def clear_github_token() -> bool:
    """Forget the reusable GitHub key entirely."""
    try:
        from app.db import get_db
        await get_db().auth_element_delete(_GH_USER, _GH_SERVICE, _GH_LABEL)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("github deploy key clear failed: %s", e)
        return False


async def github_token_status() -> dict:
    """For the GET endpoint: whether a key is stored + a masked hint, NEVER the key."""
    token = await get_github_token()
    return {
        "configured": bool(token),
        "masked": (token[:4] + "…" + token[-4:]) if len(token) > 8 else ("••••" if token else ""),
    }
