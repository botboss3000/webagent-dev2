"""health_board — System Health card backend.

Contributes the ``health`` snapshot section: one status row per subsystem
(db / vault / scheduler / tunnel / devices / disk / build). Depends on the
``db_health``, ``devices`` and ``storage`` sections — read from ctx["snapshot"]
(the shell runs plugin sections in card.json ``order`` so devices lands first).

REMOVE-WHEN: the Dashboard tab is dropped from the Instances page.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Dict, List

from dashboard_server_lib import logger
from dashboard_server_lib import raw_rows  # noqa: F401  (kept for parity)


def _sw_version(project_root) -> str | None:
    """The de-facto release marker: the CACHE name in sw.js (webagent-vNNN)."""
    try:
        head = (project_root / "sw.js").read_text(encoding="utf-8", errors="ignore")[:2000]
        m = re.search(r'CACHE\s*=\s*"([^"]+)"', head)
        return m.group(1) if m else None
    except Exception:
        return None


async def build_section(ctx: Dict[str, Any]) -> List[Dict[str, Any]]:
    snap = ctx.get("snapshot") or {}
    db_health = snap.get("db_health") or {}
    devices = snap.get("devices") or []
    storage = snap.get("storage") or {}
    checks: List[Dict[str, Any]] = []

    def add(cid, label, state, value, detail=None):
        checks.append({"id": cid, "label": label, "state": state,
                       "value": value, "detail": detail})

    st = "err" if db_health.get("degraded") else ("ok" if db_health.get("ok", True) else "warn")
    val = (db_health.get("actual") or "—") + (" · hybrid" if db_health.get("hybrid") else "")
    add("db", "Database", st, val, db_health.get("host") or "local file")

    try:
        from app.secrets import get_secrets_status
        s = await asyncio.to_thread(get_secrets_status)
        add("vault", "Secrets vault", "warn" if s.get("restart_recommended") else "ok",
            s.get("provider") or "—",
            "restart recommended" if s.get("restart_recommended") else None)
    except Exception as e:
        add("vault", "Secrets vault", "err", "unavailable", str(e)[:80])

    try:
        from app.scheduler import get_scheduler
        st_ = await get_scheduler().get_status()
        run = bool(st_.get("running"))
        add("scheduler", "Automations", "ok" if run else "off",
            f"{st_.get('tasks_enabled', 0)}/{st_.get('tasks_total', 0)} enabled",
            (st_.get("last_error") or None) if run else "scheduler idle")
    except Exception as e:
        add("scheduler", "Automations", "err", "unavailable", str(e)[:80])

    try:
        from app.tunnel_link.store import load_config as _tl
        cfg = _tl()
        url = cfg.get("tunnel_url")
        add("tunnel", "Tunnel", "ok" if (cfg.get("registered_relay") and url) else "off",
            (url or "not linked").replace("https://", ""), None)
    except Exception:
        add("tunnel", "Tunnel", "off", "not installed", None)

    online = sum(1 for d in devices if d.get("online"))
    add("devices", "Devices", "ok" if online else "off",
        f"{online}/{len(devices)} online" if devices else "none linked", None)

    free = storage.get("disk_free_gb")
    if free is not None:
        add("disk", "Disk space", "err" if free < 2 else ("warn" if free < 10 else "ok"),
            f"{free} GB free", f"of {storage.get('disk_total_gb')} GB")

    ver = _sw_version(ctx.get("project_root"))
    if ver:
        add("version", "Build", "ok", ver, None)
    return checks
