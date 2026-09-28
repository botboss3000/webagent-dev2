"""Background polling loop for poll-only event sources.

Wakes every ``TICK_INTERVAL`` seconds, asks each poll source for its due
subscriptions, calls ``source.poll`` per row, routes any new events,
persists the new cursor.

Rate limiting:
  - Per-subscription: cadence is ``poll_interval_seconds`` (override) or
    the source's ``default_poll_interval_seconds``.
  - Per-source: at most one in-flight poll per (source, subscription_id)
    so a slow API call doesn't pile up on the next tick.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Set, Tuple

logger = logging.getLogger(__name__)

TICK_INTERVAL = 15  # seconds between manager ticks


class EventPoller:
    def __init__(self, manager) -> None:
        self._mgr = manager
        self._in_flight: Set[Tuple[str, str]] = set()  # (source, subscription_id)
        self._last_tick_at = None
        self._last_error = None

    async def run(self) -> None:
        # Short warm-up so app startup completes first.
        await asyncio.sleep(3)
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("Event poller tick failed: %s", e)
                self._last_error = str(e)[:200]
            try:
                await asyncio.sleep(TICK_INTERVAL)
            except asyncio.CancelledError:
                raise

    async def _tick(self) -> None:
        # Kill switch: while engaged, polling is a no-op (defense in depth — the
        # event runtime is normally stopped entirely).
        try:
            from app.kill_switch import is_engaged as _ks_engaged
            if _ks_engaged():
                return
        except Exception:
            pass
        from app.db import get_db
        db = get_db()
        self._last_tick_at = datetime.now(timezone.utc).isoformat()

        for source in self._mgr.poll_sources():
            cadence = max(5, int(source.default_poll_interval_seconds or 60))
            cutoff = (datetime.now(timezone.utc) - timedelta(seconds=cadence)).isoformat()
            try:
                due = await db.list_event_subscriptions_for_poll(source.name, cutoff)
            except Exception as e:
                logger.warning("Could not list poll subs for %s: %s", source.name, e)
                continue
            for sub in due:
                # Honor per-sub override.
                override = sub.get("poll_interval_seconds")
                if override and sub.get("last_polled_at"):
                    try:
                        last = datetime.fromisoformat(sub["last_polled_at"])
                        if (datetime.now(timezone.utc) - last) < timedelta(seconds=int(override)):
                            continue
                    except Exception:
                        pass
                key = (source.name, sub["id"])
                if key in self._in_flight:
                    continue
                self._in_flight.add(key)
                asyncio.create_task(
                    self._poll_one(source, sub),
                    name=f"poll_{source.name}_{sub['id'][:8]}",
                )

    async def _poll_one(self, source, sub: dict) -> None:
        from app.db import get_db
        from app.events.router import route_event
        db = get_db()
        try:
            events, new_cursor = await source.poll(sub)
            for evt in events or []:
                try:
                    await route_event(evt)
                except Exception as e:
                    logger.exception("route_event during poll failed: %s", e)
            await db.update_event_subscription(
                sub["id"],
                poll_cursor=new_cursor if new_cursor is not None else sub.get("poll_cursor"),
                last_polled_at=datetime.now(timezone.utc).isoformat(),
                last_status="ok",
                last_error=None,
            )
        except Exception as e:
            logger.exception("Poll failed for sub %s on source %s: %s",
                             sub["id"], source.name, e)
            try:
                await db.update_event_subscription(
                    sub["id"],
                    last_polled_at=datetime.now(timezone.utc).isoformat(),
                    last_status="error",
                    last_error=str(e)[:500],
                )
            except Exception:
                pass
        finally:
            self._in_flight.discard((source.name, sub["id"]))
