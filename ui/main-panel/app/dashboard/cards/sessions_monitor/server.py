"""sessions_monitor — Sessions & Runs card backend.

Contributes the ``sessions`` snapshot section: running sessions first (with live
duration), then the most recently touched, each with its window cost.

REMOVE-WHEN: the Dashboard tab is dropped from the Instances page.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict

from dashboard_server_lib import logger, raw_rows, to_epoch


async def build_section(ctx: Dict[str, Any]) -> Dict[str, Any]:
    usage_rows = ctx.get("rows") or []
    run_rows = ctx.get("run_rows") or []
    out: Dict[str, Any] = {"active": len(run_rows), "list": []}
    try:
        rows = await asyncio.to_thread(
            raw_rows, "sessions", "id,user_id,title,agent_id,status,updated_at", 40, "updated_at")
        cost: Dict[str, Dict[str, float]] = {}
        for r in usage_rows:
            sid = r.get("session_id")
            if not sid:
                continue
            c = cost.setdefault(sid, {"cost": 0.0, "calls": 0})
            c["cost"] += float(r.get("cost_usd") or 0.0)
            c["calls"] += 1
        runs = {rr.get("session_id"): rr for rr in run_rows}
        now = time.time()
        seen = set()
        items = []

        def _item(sid, title, user_id, agent_id, updated) -> Dict[str, Any]:
            rr = runs.get(sid) or {}
            started = to_epoch(rr.get("started_at"))
            hb = to_epoch(rr.get("heartbeat_at") or rr.get("updated_at"))
            running = bool(rr)
            c = cost.get(sid, {})
            return {
                "id": sid,
                "title": (title or "Untitled session")[:60],
                "user_id": user_id,
                "agent_id": agent_id or rr.get("agent_id"),
                "running": running,
                "running_s": int(now - started) if running and started else None,
                "stale": bool(running and hb and (now - hb) > 120),
                "updated_s": int(now - to_epoch(updated)) if updated else None,
                "cost_usd": round(c.get("cost", 0.0), 4),
                "calls": int(c.get("calls", 0)),
            }

        by_recent = {r.get("id"): r for r in rows}
        for sid, rr in runs.items():
            r = by_recent.get(sid) or {}
            items.append(_item(sid, r.get("title"), r.get("user_id") or rr.get("user_id"),
                               r.get("agent_id"), r.get("updated_at")))
            seen.add(sid)
        for r in rows:
            sid = r.get("id")
            if sid in seen or (r.get("status") or "active") != "active":
                continue
            items.append(_item(sid, r.get("title"), r.get("user_id"), r.get("agent_id"),
                               r.get("updated_at")))
            if len(items) >= 9:
                break
        out["list"] = items[:9]
    except Exception as e:
        logger.debug("dashboard sessions section failed: %s", e)
    return out
