"""tool_usage — Tool & Model Usage card backend.

Contributes the ``tool_usage`` snapshot section: top tools by executions (with
failure rate + avg duration, from logs.db tool_executions) and per-model
calls/tokens/cost (from the shell's shared usage rows).

REMOVE-WHEN: the Dashboard tab is dropped from the Instances page.
"""

from __future__ import annotations

import time
from typing import Any, Dict

from dashboard_server_lib import logger, iso_from_epoch, to_epoch


async def build_section(ctx: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {"tools": [], "models": [], "tool_calls": 0, "tool_failures": 0}
    window_s = float(ctx.get("window_s") or 3600.0)
    usage_rows = ctx.get("rows") or []
    start = time.time() - window_s
    try:
        from app.db.logs_store import get_log_store
        rows = await get_log_store().query_tool_executions(
            since=iso_from_epoch(start), limit=2000)
        agg: Dict[str, Dict[str, float]] = {}
        for r in rows:
            name = r.get("tool_name") or "?"
            a = agg.setdefault(name, {"n": 0, "fail": 0, "ms": 0.0})
            a["n"] += 1
            a["fail"] += 0 if r.get("success") else 1
            a["ms"] += float(r.get("duration_ms") or 0.0)
        out["tool_calls"] = sum(int(a["n"]) for a in agg.values())
        out["tool_failures"] = sum(int(a["fail"]) for a in agg.values())
        top = sorted(agg.items(), key=lambda kv: kv[1]["n"], reverse=True)[:8]
        out["tools"] = [{
            "name": k, "calls": int(v["n"]), "failures": int(v["fail"]),
            "fail_pct": round(v["fail"] / v["n"] * 100, 1) if v["n"] else 0.0,
            "avg_ms": round(v["ms"] / v["n"], 0) if v["n"] else 0.0,
        } for k, v in top]
    except Exception as e:
        logger.debug("dashboard tools section failed: %s", e)
    try:
        cutoff = time.time() - window_s
        models: Dict[str, Dict[str, float]] = {}
        for r in usage_rows:
            if to_epoch(r.get("created_at")) < cutoff:
                continue
            m = models.setdefault(r.get("model") or "unknown", {"n": 0, "tok": 0, "cost": 0.0})
            m["n"] += 1
            m["tok"] += int(r.get("input_tokens") or 0) + int(r.get("output_tokens") or 0)
            m["cost"] += float(r.get("cost_usd") or 0.0)
        top = sorted(models.items(), key=lambda kv: kv[1]["n"], reverse=True)[:6]
        out["models"] = [{
            "model": k, "calls": int(v["n"]), "tokens": int(v["tok"]),
            "cost_usd": round(v["cost"], 4),
        } for k, v in top]
    except Exception as e:
        logger.debug("dashboard models aggregate failed: %s", e)
    return out
