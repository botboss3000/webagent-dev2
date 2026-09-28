"""failures — Recent Failures card backend.

Contributes the ``failures`` snapshot section: error/critical diagnostics rows
from the diagnostics store for the window.

REMOVE-WHEN: the Dashboard tab is dropped from the Instances page.
"""

from __future__ import annotations

from typing import Any, Dict

from dashboard_server_lib import logger, iso_since


async def build_section(ctx: Dict[str, Any]) -> Dict[str, Any]:
    try:
        from app.db import get_db
        from app.db.offload import db_offload
        db = get_db()
        rows = await db_offload(lambda: db.query_diagnostics(
            levels=["error", "critical"], since=iso_since(ctx.get("window_s") or 3600.0), limit=200)) or []
        recent = [{
            "ts": r.get("created_at"),
            "level": r.get("level"),
            "category": r.get("category"),
            "agent": r.get("agent_id"),
            "session": r.get("session_id"),
            "message": (r.get("message") or "")[:160],
        } for r in rows[:12]]
        return {"count": len(rows), "recent": recent}
    except Exception as e:
        logger.debug("dashboard failures failed: %s", e)
        return {"count": 0, "recent": []}
