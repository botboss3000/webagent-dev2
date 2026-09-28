"""Generic webhook provider.

For any cron service that exposes an HTTP endpoint accepting a job spec.
WebAgent POSTs the following JSON to the configured ``upstream_url`` for
every job push, and a DELETE when the row goes away:

  {
    "automation_id": "...",
    "operation": "upsert" | "delete",
    "external_job_id": "<previous id, if any>",
    "fire_url": "https://your.app/api/v1/automations/fire/<id>?token=<...>",
    "schedule_cron": "* * * * *",
    "timezone": "UTC",
    "label": "Log current time",
    "owner_user_id": "..."
  }

The upstream service is expected to respond with ``{"job_id": "..."}`` on
success. WebAgent stores that id as ``external_job_id`` for the row.

This is the lowest-common-denominator integration — use it to wire any of:
  - Self-hosted cron services
  - n8n / Zapier scheduled triggers
  - AWS EventBridge / Lambda via a thin proxy
  - Internal scheduler microservices
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import httpx

from app.scheduler.remote_base import BaseRemoteScheduler, _build_fire_url

FEATURE = {
    "id": "generic_webhook",
    "display_name": "Generic webhook",
    "category": "scheduler",
    "status": "beta",
    "summary": "POST job specs to your own scheduler service, which calls back the fire webhook.",
    "requires": ["an upstream scheduler URL you control"],
}

logger = logging.getLogger(__name__)


class GenericWebhookScheduler(BaseRemoteScheduler):
    name = "generic_webhook"

    def _upstream(self) -> str:
        u = (self.settings.get("upstream_url") or "").strip()
        if not u:
            raise RuntimeError("Missing upstream_url.")
        return u.rstrip("/")

    def _headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json"}
        auth = (self.settings.get("auth_header") or "").strip()
        if auth:
            h["Authorization"] = auth
        return h

    def _fire_url(self, automation_id: str, token: str) -> str:
        override = (self.settings.get("fire_base_url") or "").strip().rstrip("/")
        if override:
            return f"{override}/api/v1/automations/fire/{automation_id}?token={token}"
        return _build_fire_url(automation_id, token)

    async def test_connection(self) -> Dict[str, Any]:
        url = self._upstream()
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    url,
                    headers=self._headers(),
                    json={"operation": "ping"},
                )
            if resp.status_code < 500:
                return {"ok": True, "message": f"Upstream responded HTTP {resp.status_code}."}
            return {"ok": False, "error": f"HTTP {resp.status_code}: {resp.text[:300]}"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    async def _push_job(self, row: Dict[str, Any], fire_url: str, fire_token: str) -> str:
        payload = {
            "automation_id": row["id"],
            "operation": "upsert",
            "external_job_id": row.get("external_job_id"),
            "fire_url": fire_url,
            "schedule_cron": row.get("schedule_cron"),
            "timezone": row.get("timezone") or "UTC",
            "label": row.get("task_label") or "WebAgent automation",
            "owner_user_id": row.get("owner_user_id"),
        }
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(self._upstream(), headers=self._headers(), json=payload)
        if resp.status_code >= 400:
            raise RuntimeError(f"Upstream upsert failed: {resp.status_code} {resp.text[:300]}")
        try:
            data = resp.json() or {}
            ext = data.get("job_id") or data.get("external_job_id") or row.get("external_job_id") or row["id"]
        except Exception:
            ext = row.get("external_job_id") or row["id"]
        return str(ext)

    async def _cancel_job(self, row: Dict[str, Any]) -> None:
        if not row.get("external_job_id"):
            return
        payload = {
            "automation_id": row["id"],
            "operation": "delete",
            "external_job_id": row.get("external_job_id"),
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(self._upstream(), headers=self._headers(), json=payload)
        if resp.status_code >= 500:
            raise RuntimeError(f"Upstream delete failed: {resp.status_code} {resp.text[:200]}")
