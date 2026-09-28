"""Discovery + credential store for social sign-in providers.

The providers themselves are drop-in folders under `plugins/auth_providers/`
(one `<id>.json` descriptor + optional `<id>.py` hook). This module is the core
manager that:

  • scans that tree once and caches the descriptors (+ optional hook modules),
  • persists each provider's on/off flag and OAuth credentials in the encrypted
    vault (never in a plaintext config file — mirrors how integration platform
    creds are stored, `app/admin/integrations._provider_creds`),
  • builds the two views the app needs: an admin view (everything, secrets shown
    only as is-set flags) and a public view (enabled + configured providers, no
    secrets) for the sign-in buttons.

It holds NO web/OAuth mechanics — those live in `flow.py`. Adding a provider is
a pure drop-in: nothing here is edited.
"""

from __future__ import annotations

import importlib.util
import json
import logging
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_ADMIN_USER = "admin"
# Vault service key per provider — kept distinct from the integration OAuth keys
# (`*_oauth_config`) so a login app and a tool-connection app can be different.
_SERVICE_PREFIX = "social_login:"

# plugins/auth_providers lives at the repo root (sibling of app/).
_PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / "auth_providers"

# id -> {"descriptor": dict, "module": module|None}
_CACHE: dict[str, dict] = {}
_SCANNED = False


# ── Discovery ────────────────────────────────────────────────────────────────

def _load_module(provider_id: str, py_path: Path):
    """Import a provider's optional `<id>.py` hook module, guarded."""
    try:
        spec = importlib.util.spec_from_file_location(
            f"auth_provider_{provider_id}", py_path
        )
        if not spec or not spec.loader:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception as e:  # noqa: BLE001 — a broken provider must never break boot
        logger.warning("auth provider %s: hook module failed to load: %s", provider_id, e)
        return None


def _scan(force: bool = False) -> None:
    global _SCANNED
    if _SCANNED and not force:
        return
    _CACHE.clear()
    try:
        folders = sorted(p for p in _PLUGIN_DIR.iterdir() if p.is_dir())
    except Exception:
        folders = []
    for folder in folders:
        if folder.name.startswith("_"):
            continue  # _TEMPLATE and friends
        desc_file = folder / f"{folder.name}.json"
        if not desc_file.is_file():
            continue
        try:
            descriptor = json.loads(desc_file.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            logger.warning("auth provider %s: bad descriptor: %s", folder.name, e)
            continue
        pid = descriptor.get("id") or folder.name
        py_file = folder / f"{folder.name}.py"
        module = _load_module(pid, py_file) if py_file.is_file() else None
        _CACHE[pid] = {"descriptor": descriptor, "module": module}
    _SCANNED = True
    logger.info("Discovered %d social sign-in provider(s): %s",
                len(_CACHE), ", ".join(_CACHE.keys()) or "none")


def refresh() -> None:
    """Force a re-scan (e.g. after a provider folder is dropped in at runtime)."""
    _scan(force=True)


def list_descriptors() -> list[dict]:
    _scan()
    return [c["descriptor"] for c in _CACHE.values()]


def get_descriptor(provider_id: str) -> Optional[dict]:
    _scan()
    c = _CACHE.get(provider_id)
    return c["descriptor"] if c else None


def get_module(provider_id: str):
    _scan()
    c = _CACHE.get(provider_id)
    return c["module"] if c else None


def credential_fields(provider_id: str) -> list[dict]:
    d = get_descriptor(provider_id) or {}
    return d.get("credential_fields") or []


def client_id_field(provider_id: str) -> str:
    d = get_descriptor(provider_id) or {}
    return d.get("client_id_field") or "client_id"


# ── Vault-backed enable + credential store ──────────────────────────────────

def _service(provider_id: str) -> str:
    return f"{_SERVICE_PREFIX}{provider_id}"


async def _read_row(provider_id: str) -> tuple[dict, dict]:
    """Return (config, secrets) for a provider — both dicts, possibly empty."""
    try:
        from app.db import get_db
        db = get_db()
        elem = await db.auth_element_get(_ADMIN_USER, _service(provider_id), "default")
    except Exception as e:  # noqa: BLE001
        logger.debug("social_login read failed for %s: %s", provider_id, e)
        return {}, {}
    if not elem:
        return {}, {}
    config = elem.get("config") or {}
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except Exception:
            config = {}
    secrets: dict = {}
    raw = elem.get("secret_ref") or ""
    if raw:
        try:
            secrets = json.loads(raw)
        except Exception:
            secrets = {}
    return (config if isinstance(config, dict) else {},
            secrets if isinstance(secrets, dict) else {})


async def get_credentials(provider_id: str) -> dict:
    """All credential field values (secret + non-secret) for the OAuth flow."""
    config, secrets = await _read_row(provider_id)
    out: dict[str, str] = {}
    for f in credential_fields(provider_id):
        key = f.get("key")
        if not key:
            continue
        if f.get("secret"):
            out[key] = secrets.get(key, "")
        else:
            out[key] = config.get(key, "")
    return out


async def is_enabled(provider_id: str) -> bool:
    config, _ = await _read_row(provider_id)
    return bool(config.get("enabled"))


async def is_configured(provider_id: str) -> bool:
    """A provider is 'configured' when every declared credential field is set."""
    creds = await get_credentials(provider_id)
    fields = credential_fields(provider_id)
    if not fields:
        return False
    return all((creds.get(f.get("key", "")) or "").strip() for f in fields)


async def save_config(provider_id: str, *, enabled: bool, field_values: dict) -> None:
    """Persist the on/off flag + credentials. A blank secret field is left as-is."""
    if get_descriptor(provider_id) is None:
        raise KeyError(provider_id)
    config, secrets = await _read_row(provider_id)
    new_config: dict[str, Any] = {**config, "enabled": bool(enabled)}
    new_secrets: dict[str, Any] = {**secrets}
    for f in credential_fields(provider_id):
        key = f.get("key")
        if not key or key not in field_values:
            continue
        val = (field_values.get(key) or "").strip()
        if f.get("secret"):
            if val:  # blank = keep the stored secret unchanged
                new_secrets[key] = val
        else:
            new_config[key] = val
    from app.db import get_db
    db = get_db()
    await db.auth_element_set(
        user_id=_ADMIN_USER,
        service=_service(provider_id),
        config=new_config,
        secret_ref=json.dumps(new_secrets),
        label="default",
    )


# ── Views ────────────────────────────────────────────────────────────────────

def _display(descriptor: dict) -> dict:
    return {
        "id": descriptor.get("id"),
        "display_name": descriptor.get("display_name") or descriptor.get("id"),
        "status": descriptor.get("status") or "unknown",
        "icon": descriptor.get("icon") or "log-in",
        "brand_color": descriptor.get("brand_color") or "#ffffff",
        "text_color": descriptor.get("text_color") or "#1f1f1f",
        "border": bool(descriptor.get("border")),
    }


async def public_view() -> list[dict]:
    """Enabled + configured providers, display-only (for the sign-in buttons)."""
    _scan()
    out = []
    for pid, c in _CACHE.items():
        if await is_enabled(pid) and await is_configured(pid):
            out.append(_display(c["descriptor"]))
    return out


async def admin_view() -> list[dict]:
    """Every installed provider with enable/configured state + credential schema.

    Secret values are NEVER returned — each field carries an `is_set` flag and,
    for non-secret fields, its current value so the admin form can prefill.
    """
    _scan()
    rows = []
    for pid, c in _CACHE.items():
        d = c["descriptor"]
        config, secrets = await _read_row(pid)
        fields = []
        for f in (d.get("credential_fields") or []):
            key = f.get("key", "")
            secret = bool(f.get("secret"))
            fields.append({
                "key": key,
                "label": f.get("label") or key,
                "secret": secret,
                # Optional per-field help ("where to get this") — surfaced by the
                # admin App Access → Social sign-in panel as a "?" tooltip beside
                # the label, mirroring the cloud-deploy field hints.
                "tip": f.get("tip") or "",
                "is_set": bool((secrets.get(key) if secret else config.get(key)) or ""),
                "value": "" if secret else (config.get(key, "") or ""),
            })
        row = _display(d)
        row.update({
            "enabled": bool(config.get("enabled")),
            "configured": await is_configured(pid),
            "credential_fields": fields,
            "requires": d.get("requires") or [],
            "setup_url": d.get("setup_url") or "",
            "setup_hint": d.get("setup_hint") or "",
        })
        rows.append(row)
    return rows
