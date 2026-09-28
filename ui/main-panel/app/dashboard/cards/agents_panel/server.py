"""agents_panel — Agents card backend.

Contributes the ``agents`` snapshot section: every non-hidden active agent with
its live run count + window usage, busiest first. Uses the shell's shared
usage_rows / run_rows (ctx) so no extra 20k-row scan.

REMOVE-WHEN: the Dashboard tab is dropped from the Instances page.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List

from dashboard_server_lib import logger, raw_rows, to_epoch


async def build_section(ctx: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {"total": 0, "list": []}
    window_s = float(ctx.get("window_s") or 3600.0)
    usage_rows = ctx.get("rows") or []
    run_rows = ctx.get("run_rows") or []
    try:
        rows = await asyncio.to_thread(raw_rows, "agents", "id,name,status,model,metadata,updated_at", 300)
        cutoff = time.time() - window_s
        usage: Dict[str, Dict[str, float]] = {}
        for r in usage_rows:
            if to_epoch(r.get("created_at")) < cutoff:
                continue
            a = usage.setdefault(r.get("agent_id") or "", {"cost": 0.0, "calls": 0, "tok": 0})
            a["cost"] += float(r.get("cost_usd") or 0.0)
            a["calls"] += 1
            a["tok"] += int(r.get("input_tokens") or 0) + int(r.get("output_tokens") or 0)
        running: Dict[str, int] = {}
        for rr in run_rows:
            aid = rr.get("agent_id") or ""
            running[aid] = running.get(aid, 0) + 1
        agents = []
        for r in rows:
            if (r.get("status") or "active") != "active":
                continue
            meta = r.get("metadata")
            if isinstance(meta, str):
                try:
                    import json
                    meta = json.loads(meta)
                except Exception:
                    meta = {}
            meta = meta or {}
            if meta.get("hidden_from_user") or meta.get("system_agent"):
                continue
            aid = r.get("id")
            u = usage.get(aid, {})
            agents.append({
                "id": aid,
                "name": r.get("name") or aid,
                "icon": meta.get("icon") or "bot",
                "model": r.get("model"),
                "engine": meta.get("engine"),
                "running": running.get(aid, 0),
                "cost_usd": round(u.get("cost", 0.0), 4),
                "calls": int(u.get("calls", 0)),
                "tokens": int(u.get("tok", 0)),
            })
        out["total"] = len(agents)
        agents.sort(key=lambda a: (-a["running"], -a["tokens"], a["name"].lower()))
        out["list"] = agents[:10]
    except Exception as e:
        logger.debug("dashboard agents section failed: %s", e)
    return out
