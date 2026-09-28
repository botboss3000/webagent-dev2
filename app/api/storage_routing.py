"""
Storage routing config — readonly GET + admin POST to configure which
storage backend each data function uses.

Data resides at ``data/config/storage_routing.json``. If the file does not
exist, every function defaults to Server. Experimental browser authority and
browser cache choices require separate server-side feature flags.

Columns: browser (IndexedDB), server (SQLite), postgres.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from typing import Dict

from fastapi import APIRouter, HTTPException, Request

from app.auth.identity import request_user_id
from app.db.storage_router import storage_capabilities

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/storage", tags=["storage_routing"])

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_ROUTING_PATH = os.path.join(_PROJECT_ROOT, "data", "config", "storage_routing.json")

# ── Default routing (must match the rows rendered by the client) ───────────
_DEFAULT_ROUTING: Dict[str, str] = {
    "session_data":      "server",
    "session_tools":     "server",
    "session_cache":     "server",   # "browser" = browser caches in IndexedDB for instant renders
    "agent_data":        "server",
    "user_data":         "server",
    "vault":             "server",
    "genui_pages":       "server",
    "attachments":       "server",
    "local_instance":    "server",   # app-plane state pinned to this machine
    "app_shared_data":   "server",   # app-plane data eligible for remote sync
}


def _load_routing() -> Dict[str, str]:
    """Return the current routing dict (defaults if no config file)."""
    if not os.path.exists(_ROUTING_PATH):
        return dict(_DEFAULT_ROUTING)
    try:
        with open(_ROUTING_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.warning("Failed to read storage_routing.json: %s", e)
        return dict(_DEFAULT_ROUTING)

    # Merge with defaults to fill any missing keys
    merged = dict(_DEFAULT_ROUTING)
    if isinstance(data, dict):
        valid = {"browser", "server", "postgres"}
        merged.update({
            k: v for k, v in data.items()
            if k in _DEFAULT_ROUTING and v in valid
        })
    return merged


def _save_routing(data: Dict[str, str]) -> None:
    """Persist routing atomically so readers never observe partial JSON.

    Legacy cache-timing blocks are intentionally discarded because cached
    browser views no longer have a time-based expiry policy.
    """
    target_dir = os.path.dirname(_ROUTING_PATH)
    os.makedirs(target_dir, exist_ok=True)
    payload: Dict[str, object] = dict(data)
    fd, temp_path = tempfile.mkstemp(prefix=".storage-routing-", suffix=".json", dir=target_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, _ROUTING_PATH)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


async def _require_admin(request: Request) -> str:
    uid = request_user_id(request)
    try:
        from app.db import get_db
        is_admin = bool(uid) and await get_db().is_user_admin(uid)
    except Exception:
        is_admin = False
    if not is_admin:
        raise HTTPException(status_code=403, detail="Restricted to admin users only.")
    return uid


@router.get("/routing")
async def get_routing(request: Request):
    """Return the current storage routing config."""
    await _require_admin(request)
    return {
        "routing": _load_routing(),
        "defaults": _DEFAULT_ROUTING,
        "capabilities": storage_capabilities(),
    }


@router.post("/routing")
async def set_routing(body: dict, request: Request):
    """Save storage routing config.

    Body: ``{"routing": {"session_data": "browser", ...}}``.
    Only keys present in the defaults are accepted; values must be one of
    ``browser``, ``server``, or ``postgres``.
    """
    await _require_admin(request)
    incoming = body.get("routing", {})
    if not isinstance(incoming, dict):
        raise HTTPException(status_code=400, detail="Expected {routing: {...}}")

    VALID = {"browser", "server", "postgres"}

    merged = dict(_DEFAULT_ROUTING)
    for key, value in incoming.items():
        if key not in _DEFAULT_ROUTING:
            continue  # ignore unknown keys
        if value not in VALID:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid value for '{key}': '{value}'. Must be browser, server, or postgres.",
            )
        if value == "browser":
            caps = storage_capabilities()
            enabled = (
                key == "session_data" and caps["browser_authority"]
            ) or (
                key == "session_cache" and caps["browser_session_cache"]
            )
            if not enabled:
                raise HTTPException(
                    status_code=409,
                    detail=f"Browser storage is not enabled for '{key}'.",
                )
        merged[key] = value

    _save_routing(merged)
    # Reload the singleton so chat endpoints pick up the change immediately
    from app.db.storage_router import reload_routing
    reload_routing()
    return {"ok": True, "routing": merged}


@router.get("/browser-policy")
async def get_browser_policy(request: Request):
    """Return bounded cache, retention, export/delete, and telemetry policy."""
    await _require_admin(request)
    from app.db.browser_policy import browser_storage_policy_dict

    return {
        "policy": browser_storage_policy_dict(),
        "env_locked": os.environ.get("WEBAGENT_CONFIG_SOURCE", "").lower() == "env",
        "capabilities": storage_capabilities(),
    }


@router.post("/browser-policy")
async def set_browser_policy(body: dict, request: Request):
    """Atomically update browser storage policy without enabling either gate."""
    await _require_admin(request)
    incoming = body.get("policy")
    if not isinstance(incoming, dict):
        raise HTTPException(status_code=400, detail="Expected {policy: {...}}")
    from app.db.browser_policy import save_browser_storage_policy

    try:
        policy = save_browser_storage_policy(incoming)
    except PermissionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "ok": True,
        "policy": policy.__dict__,
        "capabilities": storage_capabilities(),
    }
