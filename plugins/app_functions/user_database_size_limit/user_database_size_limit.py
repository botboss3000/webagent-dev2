"""Leader-only recurring maintenance for the per-user SQLite storage quota."""
from __future__ import annotations
import asyncio
import logging
from pathlib import Path
from typing import Optional
from app.api.db_viewer import _enforce_user_database_size_limit, _hybrid_backend_for
from app.db.local import reset_db_user_context, set_db_user_context
from app.db.user_store import _user_db_path
logger = logging.getLogger(__name__)
_STARTUP_DELAY_SECONDS = 300
_DEFAULT_INTERVAL_HOURS = 24
_MIN_INTERVAL_HOURS = 1
_MAX_INTERVAL_HOURS = 168
_sweep_task: Optional[asyncio.Task] = None

def _enabled():
    try:
        from app.abilities import app_function_enabled
        return app_function_enabled("user_database_size_limit")
    except Exception:
        return True

def _interval_seconds():
    try:
        from app.admin.ability_config import get_ability_config
        hours = int(float(get_ability_config("user_database_size_limit").get("maintenance_interval_hours", _DEFAULT_INTERVAL_HOURS)))
    except (TypeError, ValueError, OverflowError):
        hours = _DEFAULT_INTERVAL_HOURS
    except Exception:
        hours = _DEFAULT_INTERVAL_HOURS
    return max(_MIN_INTERVAL_HOURS, min(_MAX_INTERVAL_HOURS, hours)) * 3600

def _record(level, message):
    try:
        from app.agent.diagnostics import record
        record(level, "maintenance", message, source="user_database_quota_sweep")
    except Exception:
        pass

async def _sweep_once():
    result = {"users": 0, "checked": 0, "cleaned": 0, "skipped": 0, "failed": 0}
    if not _enabled(): return result
    try:
        from app.db import get_db
        accounts = await get_db().list_user_accounts()
    except Exception as exc:
        logger.warning("User database quota sweep could not enumerate users: %s", exc)
        result["failed"] = 1; return result
    for account in accounts:
        user_id = str(account.get("user_id") or "")
        if not user_id: continue
        result["users"] += 1
        token = set_db_user_context(user_id)
        try:
            path = Path(_user_db_path(user_id))
            if not path.exists(): result["skipped"] += 1; continue
            result["checked"] += 1
            cleanup = await _enforce_user_database_size_limit("user.db", path, user_id, [], _hybrid_backend_for("user.db"), None)
            if any(cleanup.get(key) for key in ("purged_sessions", "evicted_tool_outputs", "memory_cache_pages_reclaimed")):
                result["cleaned"] += 1
        except asyncio.CancelledError: raise
        except Exception as exc:
            result["failed"] += 1; logger.warning("User database quota sweep failed for %s: %s", user_id[:12], exc)
        finally: reset_db_user_context(token)
    _record("info", "User database quota sweep finished: " + " ".join(f"{key}={value}" for key, value in result.items()))
    return result

async def _sweep_loop():
    await asyncio.sleep(_STARTUP_DELAY_SECONDS)
    while True:
        try: await _sweep_once()
        except asyncio.CancelledError: raise
        except Exception as exc: logger.warning("User database quota sweep tick failed: %s", exc, exc_info=True)
        await asyncio.sleep(_interval_seconds())

async def start_background():
    global _sweep_task
    if not _enabled() or (_sweep_task is not None and not _sweep_task.done()): return
    _sweep_task = asyncio.create_task(_sweep_loop(), name="user_database_quota_sweep")
    hours = _interval_seconds() // 3600
    _record("info", f"User database quota sweep started (every {hours}h; first run after five minutes)")

async def stop_background():
    global _sweep_task
    task, _sweep_task = _sweep_task, None
    if task is not None:
        task.cancel()
        try: await task
        except asyncio.CancelledError: pass
