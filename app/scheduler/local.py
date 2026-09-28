"""In-process scheduler.

A single asyncio task wakes every ``POLL_INTERVAL`` seconds, claims rows
whose ``next_run_at`` is past, and fires them via the executor. Each fire is
its own ``asyncio.create_task`` so a slow run does not stall the poll loop.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional, Set

from app.scheduler.base import SchedulerBackend
from app.scheduler.executor import execute_automation

logger = logging.getLogger(__name__)

POLL_INTERVAL = 30  # seconds


class LocalScheduler(SchedulerBackend):
    name = "local"

    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._running: bool = False
        self._in_flight: Set[str] = set()
        self._last_poll: Optional[str] = None
        self._last_error: Optional[str] = None

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="local_scheduler_loop")
        logger.info("LocalScheduler started (poll every %ss)", POLL_INTERVAL)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        logger.info("LocalScheduler stopped")

    async def sync_tasks(self, agent_id: Optional[str] = None) -> None:
        # Nothing to cache locally — the poll loop reads the DB each tick.
        # This is a hint that something changed; just kick the loop awake.
        return

    async def run_now(self, automation_id: str) -> dict:
        from app.db import get_db
        db = get_db()
        row = await db.get_automation(automation_id)
        if not row:
            return {"ok": False, "error": "automation not found"}
        if automation_id in self._in_flight:
            return {"ok": False, "error": "already running"}
        self._in_flight.add(automation_id)
        try:
            return await execute_automation(row)
        finally:
            self._in_flight.discard(automation_id)

    async def get_status(self) -> dict:
        from app.db import get_db
        try:
            db = get_db()
            rows = await db.list_automations()
            enabled = sum(1 for r in rows if r.get("enabled"))
            total = len(rows)
        except Exception:
            enabled = total = 0
        return {
            "provider": self.name,
            "running": bool(self._task and not self._task.done() and self._running),
            "last_poll": self._last_poll,
            "last_error": self._last_error,
            "in_flight": len(self._in_flight),
            "tasks_total": total,
            "tasks_enabled": enabled,
        }

    async def _loop(self) -> None:
        # First tick after a short delay so startup completes before we hit the DB.
        await asyncio.sleep(2)
        while self._running:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("LocalScheduler tick failed: %s", e)
                self._last_error = str(e)[:200]
            try:
                await asyncio.sleep(POLL_INTERVAL)
            except asyncio.CancelledError:
                raise

    async def _tick(self) -> None:
        # Kill switch: while engaged, the scheduler poll is a no-op (defense in
        # depth — the scheduler loop is normally stopped entirely).
        try:
            from app.kill_switch import is_engaged as _ks_engaged
            if _ks_engaged():
                return
        except Exception:
            pass
        from app.db import get_db
        db = get_db()
        now = datetime.now(timezone.utc).isoformat()
        self._last_poll = now
        try:
            due = await db.claim_due_automations(now_iso=now)
        except Exception as e:
            err_str = str(e)
            if "no such table" in err_str or "does not exist" in err_str:
                logger.warning("Scheduler tick skipped — automations table not ready yet: %s", err_str[:80])
                return
            raise
        for row in due:
            if row["id"] in self._in_flight:
                continue
            self._in_flight.add(row["id"])
            asyncio.create_task(self._fire(row), name=f"automation_{row['id']}")

    async def _fire(self, row: dict) -> None:
        try:
            await execute_automation(row)
        finally:
            self._in_flight.discard(row["id"])
