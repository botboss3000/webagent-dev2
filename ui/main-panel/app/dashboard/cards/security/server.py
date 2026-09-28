"""security — Security & Sign-ins card backend.

Contributes the ``security`` snapshot section: auth events recorded by app/auth
(category ``auth`` in the diagnostics store) — sign-ins, failed attempts, etc.

REMOVE-WHEN: the Dashboard tab is dropped from the Instances page.
"""

from __future__ import annotations

import time
from typing import Any, Dict

from dashboard_server_lib import logger, iso_from_epoch


async def build_section(ctx: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {"signins": 0, "failed": 0, "recent": []}
    window_s = float(ctx.get("window_s") or 3600.0)
    try:
        from app.db.logs_store import get_log_store
        rows = await get_log_store().query_diagnostics(
            categories=["auth"], since=iso_from_epoch(time.time() - window_s), limit=500)
        for r in rows:
            msg = (r.get("message") or "")
            lvl = (r.get("level") or "info").lower()
            if lvl in ("warning", "error", "critical"):
                out["failed"] += 1
            elif msg.lower().startswith(("sign-in", "social sign-in")):
                out["signins"] += 1
        out["recent"] = [{
            "ts": r.get("created_at") or r.get("ts"),
            "level": (r.get("level") or "info").lower(),
            "message": (r.get("message") or "")[:120],
        } for r in rows[:8]]
    except Exception as e:
        logger.debug("dashboard security section failed: %s", e)
    return out
