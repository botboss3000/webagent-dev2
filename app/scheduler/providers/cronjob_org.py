"""cron-job.org REST provider.

Docs: https://docs.cron-job.org/rest-api.html

Endpoints used:
  PUT     /jobs                — create a job, returns ``{jobId}``
  PATCH   /jobs/{id}           — update existing
  DELETE  /jobs/{id}           — remove

Auth: ``Authorization: Bearer <api_key>``.

The cron-job.org schedule format isn't a 5-field cron string — it's a JSON
struct with discrete minute/hour/mday/month/wday integer arrays. We parse the
stored 5-field cron expression into those arrays.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

import httpx

from app.scheduler.remote_base import BaseRemoteScheduler, _build_fire_url

FEATURE = {
    "id": "cronjob_org",
    "display_name": "cron-job.org",
    "category": "scheduler",
    "status": "beta",
    "summary": "Push scheduled jobs to the free cron-job.org hosted cron service.",
    "requires": ["a cron-job.org API key"],
}

logger = logging.getLogger(__name__)

_BASE = "https://api.cron-job.org"


def _parse_cron_field(field: str, wildcard_value: int = -1) -> List[int]:
    """Parse a single cron field into a list of ints; ``[-1]`` means wildcard."""
    field = (field or "").strip()
    if field == "*" or field == "":
        return [wildcard_value]
    out: List[int] = []
    for chunk in field.split(","):
        m = re.match(r"^\*/(\d+)$", chunk)
        if m:
            # Step over wildcard — leave as wildcard, caller flattens (cron-job.org
            # has no native step support; document the limitation and fall back
            # to a wildcard, which approximates upward).
            return [wildcard_value]
        m = re.match(r"^(\d+)-(\d+)(?:/(\d+))?$", chunk)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            step = int(m.group(3)) if m.group(3) else 1
            out.extend(range(lo, hi + 1, step))
            continue
        if chunk.isdigit():
            out.append(int(chunk))
            continue
        # Unsupported (e.g. 1L, JAN). Fall back to wildcard.
        return [wildcard_value]
    return sorted(set(out)) if out else [wildcard_value]


def cron_to_cronjob_schedule(expr: str, timezone: str = "UTC") -> Dict[str, Any]:
    parts = (expr or "").split()
    if len(parts) != 5:
        raise RuntimeError(f"Cannot push cron expression {expr!r}: cron-job.org needs 5 fields.")
    minute, hour, mday, month, wday = parts
    return {
        "timezone": timezone or "UTC",
        "minutes": _parse_cron_field(minute),
        "hours": _parse_cron_field(hour),
        "mdays": _parse_cron_field(mday),
        "months": _parse_cron_field(month),
        "wdays": _parse_cron_field(wday),
    }


class CronJobOrgScheduler(BaseRemoteScheduler):
    name = "cronjob_org"

    def _api_key(self) -> str:
        k = (self.settings.get("api_key") or "").strip()
        if not k:
            raise RuntimeError("Missing cron-job.org API key.")
        return k

    def _fire_url(self, automation_id: str, token: str) -> str:
        override = (self.settings.get("fire_base_url") or "").strip().rstrip("/")
        if override:
            return f"{override}/api/v1/automations/fire/{automation_id}?token={token}"
        return _build_fire_url(automation_id, token)

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key()}", "Content-Type": "application/json"}

    def _build_body(self, row: Dict[str, Any], fire_url: str) -> Dict[str, Any]:
        return {
            "job": {
                "url": fire_url,
                "enabled": True,
                "saveResponses": False,
                "title": (row.get("task_label") or "WebAgent automation")[:200],
                "requestMethod": 1,  # 1 = POST per docs
                "schedule": cron_to_cronjob_schedule(row.get("schedule_cron", ""), row.get("timezone", "UTC")),
            }
        }

    # ---- abstract impl --------------------------------------------------

    async def test_connection(self) -> Dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(f"{_BASE}/jobs", headers=self._headers())
            if resp.status_code == 200:
                count = len((resp.json() or {}).get("jobs", []))
                return {"ok": True, "message": f"Authenticated; account has {count} job(s)."}
            if resp.status_code in (401, 403):
                return {"ok": False, "error": "API key rejected (HTTP %d)." % resp.status_code}
            return {"ok": False, "error": f"HTTP {resp.status_code}: {resp.text[:300]}"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    async def _push_job(self, row: Dict[str, Any], fire_url: str, fire_token: str) -> str:
        body = self._build_body(row, fire_url)
        existing = row.get("external_job_id")
        async with httpx.AsyncClient(timeout=20.0) as client:
            if existing:
                resp = await client.patch(f"{_BASE}/jobs/{existing}", headers=self._headers(), json=body)
                if resp.status_code in (200, 204):
                    return str(existing)
                if resp.status_code != 404:
                    raise RuntimeError(f"PATCH failed: {resp.status_code} {resp.text[:300]}")
            resp = await client.put(f"{_BASE}/jobs", headers=self._headers(), json=body)
            if resp.status_code not in (200, 201):
                raise RuntimeError(f"CREATE failed: {resp.status_code} {resp.text[:300]}")
            data = resp.json() or {}
            job_id = data.get("jobId") or data.get("jobID")
            if job_id is None:
                raise RuntimeError(f"Unexpected response: {data}")
            return str(job_id)

    async def _cancel_job(self, row: Dict[str, Any]) -> None:
        ext = row.get("external_job_id")
        if not ext:
            return
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.delete(f"{_BASE}/jobs/{ext}", headers=self._headers())
            if resp.status_code not in (200, 204, 404):
                raise RuntimeError(f"DELETE failed: {resp.status_code} {resp.text[:200]}")
