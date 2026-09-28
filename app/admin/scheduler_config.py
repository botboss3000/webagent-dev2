"""Scheduler provider config + admin endpoints.

Persists the user's choice of scheduler backend in ``scheduler_config.json``
at the project root. Stores per-provider settings (API keys, service-account
JSON, region, etc.) under a ``providers`` map so the admin can flip between
providers without re-entering credentials.

File shape:

    {
      "provider": "local",
      "providers": {
        "local":           {},
        "google_cloud":    { "project_id": "...", "location": "...",
                             "service_account_json": "<json>", ... },
        "cronjob_org":     { "api_key": "..." },
        "generic_webhook": { "upstream_url": "...", "auth_header": "..." }
      }
    }
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.util.config_io import safe_write_json

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_FILE = PROJECT_ROOT / "data" / "config" / "scheduler_config.json"

DEFAULT_CONFIG: Dict[str, Any] = {
    "provider": "local",
    "providers": {},
}


router = APIRouter(prefix="/admin", tags=["admin"])


def _load_config() -> Dict[str, Any]:
    try:
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    merged = dict(DEFAULT_CONFIG)
                    merged.update(data)
                    # Migrate legacy "settings" -> "providers[<provider>]"
                    if "settings" in data and isinstance(data["settings"], dict):
                        merged.setdefault("providers", {})
                        merged["providers"][merged.get("provider", "local")] = data["settings"]
                    return merged
    except Exception as e:
        logger.warning("Failed to load scheduler_config.json: %s", e)
    return dict(DEFAULT_CONFIG)


def _save_config(data: Dict[str, Any]) -> None:
    try:
        safe_write_json(CONFIG_FILE, data)
    except Exception as e:
        logger.warning("Failed to save scheduler_config.json: %s", e)


def _valid_provider_ids() -> List[str]:
    from app.scheduler.providers import list_providers
    return ["local", *[p["id"] for p in list_providers()]]


def get_provider() -> str:
    cfg = _load_config()
    p = (cfg.get("provider") or "local").lower()
    return p if p in _valid_provider_ids() else "local"


def get_settings_for(provider_id: str) -> Dict[str, Any]:
    cfg = _load_config()
    providers = cfg.get("providers") or {}
    s = providers.get(provider_id) or {}
    return s if isinstance(s, dict) else {}


def get_settings() -> Dict[str, Any]:
    return get_settings_for(get_provider())


class SchedulerConfigBody(BaseModel):
    provider: str
    settings: Dict[str, Any] = {}


class TestConfigBody(BaseModel):
    provider: str
    settings: Dict[str, Any] = {}


@router.get("/settings/scheduler/providers")
async def list_scheduler_providers():
    """Provider metadata for the dynamic config form."""
    from app.scheduler.providers import list_providers
    providers = [{
        "id": "local",
        "display_name": "Local (in-process)",
        "description": "Run scheduled jobs inside this Python process. Best for single-instance / local-mode deployments.",
        "auth_mode": "none",
        "fields": [],
    }, *list_providers()]
    return {"providers": providers}


@router.get("/settings/scheduler")
async def get_scheduler_config():
    cfg = _load_config()
    return {
        "provider": cfg.get("provider", "local"),
        "providers": cfg.get("providers", {}),
        "available": _valid_provider_ids(),
    }


@router.post("/settings/scheduler")
async def set_scheduler_config(body: SchedulerConfigBody):
    provider = (body.provider or "").strip().lower()
    if provider not in _valid_provider_ids():
        raise HTTPException(status_code=400, detail=f"Unknown provider '{body.provider}'.")
    cfg = _load_config()
    cfg["provider"] = provider
    providers = cfg.get("providers") or {}
    providers[provider] = body.settings or {}
    cfg["providers"] = providers
    _save_config(cfg)

    # Hot-swap the running backend.
    try:
        from app.scheduler import get_scheduler, reset_scheduler, start_scheduler
        try:
            await get_scheduler().stop()
        except Exception:
            pass
        reset_scheduler()
        await start_scheduler()
    except Exception as e:
        logger.warning("Could not hot-swap scheduler: %s", e)

    return {"ok": True, "provider": provider, "settings": providers.get(provider, {})}


@router.post("/settings/scheduler/test")
async def test_scheduler_provider(body: TestConfigBody):
    """Validate the provided credentials without persisting them.

    Useful to check before clicking Save.
    """
    provider = (body.provider or "").strip().lower()
    if provider == "local":
        return {"ok": True, "message": "Local scheduler runs in-process — no external credentials to test."}
    from app.scheduler.providers import get_provider_class
    cls = get_provider_class(provider)
    if cls is None:
        raise HTTPException(status_code=400, detail=f"Unknown provider '{body.provider}'.")
    inst = cls(body.settings or {})  # type: ignore[call-arg]
    test = getattr(inst, "test_connection", None)
    if test is None:
        return {"ok": False, "error": "Provider does not expose test_connection."}
    try:
        return await test()
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.post("/settings/scheduler/sync")
async def force_sync_scheduler():
    """Re-push all enabled automations to the active remote provider."""
    try:
        from app.scheduler import get_scheduler
        sched = get_scheduler()
        await sched.sync_tasks()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.get("/scheduler/status")
async def scheduler_status():
    try:
        from app.scheduler import get_scheduler
        return await get_scheduler().get_status()
    except Exception as e:
        return {"provider": "unknown", "running": False, "error": str(e)}


@router.get("/automations/count")
async def automation_count():
    """Return the total number of automation rules across all agents."""
    from app.db import get_db
    db = get_db()
    try:
        rows = await db.list_automations()
        return {"count": len(rows)}
    except Exception as e:
        logger.warning("Failed to count automations: %s", e)
        return {"count": 0}
