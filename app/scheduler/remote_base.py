"""Base class for remote scheduler providers.

Remote providers don't run a poll loop. Instead they push each enabled
automation row to an external cron service whose target is our webhook
endpoint ``POST /api/v1/automations/fire/{automation_id}?token=<fire_token>``.

Per-row state:
  - ``fire_token``    — opaque secret; generated on first push.
  - ``external_job_id`` — id assigned by the remote service.
  - ``external_provider`` — provider name that owns ``external_job_id``.

Subclass contract:
  - ``test_connection() -> dict``
  - ``_push_job(row) -> str``   returns external_job_id
  - ``_cancel_job(row) -> None``
"""

from __future__ import annotations

import logging
import secrets
from abc import abstractmethod
from typing import Any, Dict, List, Optional

from app.scheduler.base import SchedulerBackend
from app.scheduler.executor import execute_automation

logger = logging.getLogger(__name__)


class BaseRemoteScheduler(SchedulerBackend):
    """Common ``sync_tasks`` / ``run_now`` for remote providers."""

    name: str = "remote_base"

    def __init__(self, settings: Optional[Dict[str, Any]] = None) -> None:
        self.settings = dict(settings or {})

    # --- abstract bits -------------------------------------------------

    @abstractmethod
    async def test_connection(self) -> Dict[str, Any]:
        """Validate auth + reachability. Returns ``{ok, message, ...}``."""

    @abstractmethod
    async def _push_job(self, row: Dict[str, Any], fire_url: str, fire_token: str) -> str:
        """Create or update one job at the remote service. Return external_job_id."""

    @abstractmethod
    async def _cancel_job(self, row: Dict[str, Any]) -> None:
        """Best-effort delete of a previously-pushed job."""

    # --- shared lifecycle ----------------------------------------------

    async def start(self) -> None:
        await self.sync_tasks()

    async def stop(self) -> None:
        return

    async def sync_tasks(self, agent_id: Optional[str] = None) -> None:
        from app.db import get_db
        db = get_db()
        rows = await db.list_automations(agent_id=agent_id)
        for row in rows:
            try:
                if row.get("enabled"):
                    await self._sync_one(db, row)
                else:
                    if row.get("external_job_id"):
                        await self._cancel_safe(row)
                        await db.update_automation(
                            row["id"],
                            external_job_id=None,
                            external_provider=None,
                        )
            except Exception as e:
                logger.warning("Remote sync failed for %s: %s", row.get("id"), e)
                await db.update_automation(row["id"], last_status="error", last_error=str(e)[:300])

    async def _sync_one(self, db, row: Dict[str, Any]) -> None:
        token = row.get("fire_token") or secrets.token_urlsafe(24)
        fire_url = _build_fire_url(row["id"], token)
        # If row was pushed by a different provider, cancel there first.
        if row.get("external_job_id") and row.get("external_provider") and row["external_provider"] != self.name:
            try:
                from app.scheduler.providers import get_provider_class
                prev_cls = get_provider_class(row["external_provider"])
                if prev_cls is not None:
                    prev = prev_cls(self._settings_for_other(row["external_provider"]))
                    await prev._cancel_safe(row)
            except Exception:
                pass
        external_job_id = await self._push_job(row, fire_url, token)
        await db.update_automation(
            row["id"],
            fire_token=token,
            external_job_id=external_job_id,
            external_provider=self.name,
        )

    async def _cancel_safe(self, row: Dict[str, Any]) -> None:
        try:
            await self._cancel_job(row)
        except Exception as e:
            logger.debug("Cancel job failed: %s", e)

    def _settings_for_other(self, provider_name: str) -> Dict[str, Any]:
        try:
            from app.admin.scheduler_config import get_settings_for
            return get_settings_for(provider_name)
        except Exception:
            return {}

    async def run_now(self, automation_id: str) -> dict:
        from app.db import get_db
        db = get_db()
        row = await db.get_automation(automation_id)
        if not row:
            return {"ok": False, "error": "automation not found"}
        return await execute_automation(row)

    async def get_status(self) -> dict:
        from app.db import get_db
        try:
            db = get_db()
            rows = await db.list_automations()
            pushed = sum(1 for r in rows if r.get("external_provider") == self.name)
        except Exception:
            pushed = 0
        return {
            "provider": self.name,
            "running": True,  # remote services don't have an in-process loop
            "pushed_jobs": pushed,
        }


def _build_fire_url(automation_id: str, token: str) -> str:
    """Build the public webhook URL the remote service should hit."""
    base = ""
    # Use the communications registry value (auto-detected from request traffic).
    try:
        from app.communications.manager import get_plugin_manager
        base = (getattr(get_plugin_manager(), "_registry", {}) or {}).get("webhook_base_url", "").rstrip("/")
    except Exception:
        base = ""
    if not base:
        base = "http://localhost:8080"
    return f"{base}/api/v1/automations/fire/{automation_id}?token={token}"
