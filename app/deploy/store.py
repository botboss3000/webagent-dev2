"""Persistence for the Deploy panel (App Config → App Settings → Deploy).

ONE runtime-only file (gitignored): ``data/config/deploy.json``. It holds, per
deploy target (``google_vm`` / ``aws`` / …), the NON-secret settings the admin
filled in (project, zone, machine size, repo URL, domain, the "forget keys after
deploy" flag) plus a record of the LAST deployment that target produced (its
server name, public address, state, timestamps).

NO cloud secrets live here — the service-account JSON / cloud keys go into the
encrypted vault (``app/deploy/credentials.py``), exactly like every other secret
in the app. A leak of this file reveals where a server was created, never a key
that could spend money.

All writes route through ``app/util/config_io.py`` (atomic, self-creates
``data/config/``) per docs/claude/deployment.md.
"""

from __future__ import annotations

import secrets as _secrets
import time
from pathlib import Path
from typing import Any, Dict

from app.util.config_io import read_json, safe_write_json

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_FILE = PROJECT_ROOT / "data" / "config" / "deploy.json"


def _load() -> Dict[str, Any]:
    data = read_json(CONFIG_FILE, {})
    return data if isinstance(data, dict) else {}


def _save(data: Dict[str, Any]) -> None:
    safe_write_json(CONFIG_FILE, data)


# ── Per-provider non-secret config ──────────────────────────────────────────

def get_config(provider_id: str) -> Dict[str, Any]:
    """The saved non-secret settings for one target (empty dict if none)."""
    block = _load().get("providers", {}).get(provider_id, {})
    cfg = block.get("config", {})
    return dict(cfg) if isinstance(cfg, dict) else {}


def save_config(provider_id: str, config: Dict[str, Any]) -> Dict[str, Any]:
    """Replace the saved settings for one target. Returns the stored config."""
    data = _load()
    providers = data.setdefault("providers", {})
    block = providers.setdefault(provider_id, {})
    block["config"] = dict(config or {})
    _save(data)
    return block["config"]


# ── Saved servers (named reusable profiles for a profile-aware target) ───────
#
# A "saved server" is a named set of NON-secret settings (address, user, port,
# repo, domain) the admin can pick from a dropdown instead of retyping — its
# secrets (SSH key / password) live in the vault under a per-server key
# (``app/deploy/credentials.py`` with ``profile=<server_id>``). Only the
# SSH target uses these today. They sit alongside the single "working" ``config``
# the deploy runtime actually reads: selecting a saved server copies it INTO the
# working slot (see manager.select_server), so deploy/test/tear-down are unchanged.

def list_servers(provider_id: str) -> Dict[str, Any]:
    """All saved-server profiles for a target, keyed by server id (may be empty)."""
    block = _load().get("providers", {}).get(provider_id, {})
    servers = block.get("servers", {})
    return dict(servers) if isinstance(servers, dict) else {}


def get_server(provider_id: str, server_id: str) -> Dict[str, Any]:
    rec = list_servers(provider_id).get(server_id, {})
    return dict(rec) if isinstance(rec, dict) else {}


def upsert_server(provider_id: str, server_id: str, profile: Dict[str, Any]) -> str:
    """Create or replace one saved server's NON-secret record. A blank
    ``server_id`` mints a new one. Returns the server id used."""
    data = _load()
    providers = data.setdefault("providers", {})
    block = providers.setdefault(provider_id, {})
    servers = block.setdefault("servers", {})
    if not isinstance(servers, dict):
        servers = {}
        block["servers"] = servers
    sid = (server_id or "").strip() or ("srv-" + _secrets.token_hex(4))
    rec = dict(profile or {})
    rec.setdefault("created_at", time.time())
    rec["updated_at"] = time.time()
    servers[sid] = rec
    _save(data)
    return sid


def delete_server(provider_id: str, server_id: str) -> None:
    data = _load()
    block = data.get("providers", {}).get(provider_id)
    if not isinstance(block, dict):
        return
    servers = block.get("servers")
    if isinstance(servers, dict) and server_id in servers:
        servers.pop(server_id, None)
        if block.get("active_server") == server_id:
            block["active_server"] = ""
        _save(data)


def get_active_server(provider_id: str) -> str:
    block = _load().get("providers", {}).get(provider_id, {})
    return str(block.get("active_server", "") or "")


def set_active_server(provider_id: str, server_id: str) -> None:
    data = _load()
    providers = data.setdefault("providers", {})
    block = providers.setdefault(provider_id, {})
    block["active_server"] = server_id or ""
    _save(data)


# ── Which target is currently selected in the panel ─────────────────────────

def get_active_provider() -> str:
    return str(_load().get("active_provider", "") or "")


def set_active_provider(provider_id: str) -> None:
    data = _load()
    data["active_provider"] = provider_id
    _save(data)


# ── Last-deployment record (what the status line + tear-down read) ───────────

def get_deployment(provider_id: str) -> Dict[str, Any]:
    block = _load().get("providers", {}).get(provider_id, {})
    rec = block.get("deployment", {})
    return dict(rec) if isinstance(rec, dict) else {}


def set_deployment(provider_id: str, record: Dict[str, Any]) -> None:
    """Stamp the result of a deploy (server name, IP/URL, state…)."""
    data = _load()
    providers = data.setdefault("providers", {})
    block = providers.setdefault(provider_id, {})
    rec = dict(record or {})
    rec.setdefault("updated_at", time.time())
    block["deployment"] = rec
    _save(data)


def clear_deployment(provider_id: str) -> None:
    data = _load()
    block = data.get("providers", {}).get(provider_id)
    if isinstance(block, dict) and "deployment" in block:
        block.pop("deployment", None)
        _save(data)
