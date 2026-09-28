"""Persistence for the Domains panel (App Config → Data Settings → Domains).

ONE runtime-only file (gitignored): ``data/config/dns.json``. It holds, per DNS
provider (``cloudflare`` / ``namecheap`` / …), the NON-secret settings the admin
filled in (an account username, a region) plus a record of the LAST domain that
provider POINTED (the domain, the record name, the IP it points at, the public
URL, timestamps) so the panel can re-show it and re-verify on reload.

NO API keys live here — the tokens go into the encrypted vault
(``app/dns/credentials.py``), exactly like every other secret in the app. A leak
of this file reveals which domain points where, never a key that could edit DNS.

All writes route through ``app/util/config_io.py`` (atomic, self-creates
``data/config/``) per docs/claude/deployment.md — mirrors ``app/deploy/store.py``.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict

from app.util.config_io import read_json, safe_write_json

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_FILE = PROJECT_ROOT / "data" / "config" / "dns.json"


def _load() -> Dict[str, Any]:
    data = read_json(CONFIG_FILE, {})
    return data if isinstance(data, dict) else {}


def _save(data: Dict[str, Any]) -> None:
    safe_write_json(CONFIG_FILE, data)


# ── Per-provider non-secret config ──────────────────────────────────────────

def get_config(provider_id: str) -> Dict[str, Any]:
    """The saved non-secret settings for one provider (empty dict if none)."""
    block = _load().get("providers", {}).get(provider_id, {})
    cfg = block.get("config", {})
    return dict(cfg) if isinstance(cfg, dict) else {}


def save_config(provider_id: str, config: Dict[str, Any]) -> Dict[str, Any]:
    """Replace the saved settings for one provider. Returns the stored config."""
    data = _load()
    providers = data.setdefault("providers", {})
    block = providers.setdefault(provider_id, {})
    block["config"] = dict(config or {})
    _save(data)
    return block["config"]


# ── Which provider is currently selected in the panel ───────────────────────

def get_active_provider() -> str:
    return str(_load().get("active_provider", "") or "")


def set_active_provider(provider_id: str) -> None:
    data = _load()
    data["active_provider"] = provider_id
    _save(data)


# ── Last-pointed record (what the status line + re-verify read) ──────────────

def get_pointing(provider_id: str) -> Dict[str, Any]:
    block = _load().get("providers", {}).get(provider_id, {})
    rec = block.get("pointing", {})
    return dict(rec) if isinstance(rec, dict) else {}


def set_pointing(provider_id: str, record: Dict[str, Any]) -> None:
    """Stamp the result of a point (domain, record name, ip, public_url, state…)."""
    data = _load()
    providers = data.setdefault("providers", {})
    block = providers.setdefault(provider_id, {})
    rec = dict(record or {})
    rec.setdefault("updated_at", time.time())
    block["pointing"] = rec
    _save(data)


def clear_pointing(provider_id: str) -> None:
    data = _load()
    block = data.get("providers", {}).get(provider_id)
    if isinstance(block, dict) and "pointing" in block:
        block.pop("pointing", None)
        _save(data)
