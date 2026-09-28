"""user_mgmt — User Management card backend.

Contributes the ``users`` snapshot section: counts + the most recently active
accounts. Joins the credential store (app/auth/users.py) with user_profiles.

REMOVE-WHEN: the Dashboard tab is dropped from the Instances page.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict

from dashboard_server_lib import logger, raw_rows, to_epoch


async def build_section(ctx: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {"total": 0, "admins": 0, "pending": 0,
                           "new_in_window": 0, "recent": []}
    window_s = float(ctx.get("window_s") or 3600.0)
    try:
        from app.auth.users import list_users
        accounts = await list_users() or []
        profiles = await asyncio.to_thread(
            raw_rows, "user_profiles", "user_id,is_admin,created_at,last_login_at", 2000)
        prof = {p.get("user_id"): p for p in profiles}
        cutoff = time.time() - window_s
        now = time.time()
        rows = []
        for u in accounts:
            p = prof.get(u.user_id) or {}
            is_admin = bool(p.get("is_admin")) or u.user_id == "admin"
            approved = bool(getattr(u, "is_approved", True))
            last = to_epoch(p.get("last_login_at"))
            out["total"] += 1
            out["admins"] += 1 if is_admin else 0
            out["pending"] += 0 if approved else 1
            if to_epoch(p.get("created_at")) >= cutoff:
                out["new_in_window"] += 1
            rows.append({
                "user_id": u.user_id,
                "name": getattr(u, "display_name", None) or u.username,
                "username": u.username,
                "admin": is_admin,
                "approved": approved,
                "last_login_s": int(now - last) if last else None,
            })
        rows.sort(key=lambda r: (r["last_login_s"] is None, r["last_login_s"] or 0))
        out["recent"] = rows[:8]
    except Exception as e:
        logger.debug("dashboard users section failed: %s", e)
    return out
