"""devices — Devices card backend.

Contributes the ``devices`` snapshot section: every computer running WebAgent
against the shared database (app/devices/), with online/offline + platform.

REMOVE-WHEN: the Dashboard tab is dropped from the Instances page.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List

from dashboard_server_lib import logger, to_epoch


async def build_section(ctx: Dict[str, Any]) -> List[Dict[str, Any]]:
    try:
        from app.db import get_db
        from app.db.offload import db_offload
        db = get_db()
        rows = await db_offload(lambda: db.list_devices(120)) or []
        out = []
        for r in rows:
            caps = r.get("capabilities")
            if isinstance(caps, str):
                try:
                    import json
                    caps = json.loads(caps)
                except Exception:
                    caps = {}
            caps = caps or {}
            last = to_epoch(r.get("last_seen"))
            out.append({
                "label": r.get("label") or r.get("instance_id") or "device",
                "online": (time.time() - last) < 90 if last else False,
                "platform": caps.get("platform") or caps.get("os") or "—",
                "last_seen_s": int(time.time() - last) if last else None,
            })
        return out
    except Exception as e:
        logger.debug("dashboard devices failed: %s", e)
        return []
