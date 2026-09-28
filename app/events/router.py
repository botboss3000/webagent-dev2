"""Route one normalized event to matching subscriptions.

This is the single funnel between "something happened" and "agent runs."
Both the HTTP webhook intake (push sources) and the poll loop (poll sources)
call ``route_event``. Subscriptions live in the ``agent_event_subscriptions``
table.

Per-event work:
  1. Look up all enabled subscriptions for (owner, source, event_type).
  2. Ask the source plugin to apply each subscription's filter.
  3. Dedup against ``event_deliveries`` (so re-sent webhooks / overlapping
     polls don't fire twice).
  4. For each match, invoke the executor (which creates a session + runs
     the agent loop with the event payload injected into the prompt).
"""

from __future__ import annotations

import asyncio
import logging
from typing import List

from app.events.types import NormalizedEvent

logger = logging.getLogger(__name__)


async def route_event(event: NormalizedEvent) -> List[dict]:
    """Route one event. Returns a list of fire-result dicts (one per matched sub)."""
    from app.db import get_db
    from app.events import get_manager
    from app.events.executor import execute_event_subscription

    db = get_db()
    mgr = get_manager()
    source = mgr.get(event.source)

    if source is None:
        logger.warning("route_event: unknown source %r", event.source)
        return []

    subs = await db.find_event_subscriptions_for_event(
        owner_user_id=event.owner_user_id,
        source=event.source,
        event_type=event.event_type,
    )
    if not subs:
        logger.debug("route_event: no subs for %s/%s owner=%s",
                     event.source, event.event_type, event.owner_user_id)
        return []

    fires: List[asyncio.Task] = []
    results: List[dict] = []

    for sub in subs:
        filter_dict = sub.get("filter") or {}
        try:
            if not source.matches_filter(event, filter_dict):
                continue
        except Exception as e:
            logger.warning("Filter eval failed for sub %s: %s", sub["id"], e)
            continue

        # Dedup: if this provider event has already been delivered to this
        # subscription, skip. Push providers retry; polls overlap.
        prior = await db.find_event_delivery(sub["id"], event.external_id)
        if prior and prior.get("status") in ("ok", "duplicate"):
            logger.debug("route_event: skipping duplicate %s/%s for sub %s",
                         event.source, event.external_id, sub["id"])
            results.append({"subscription_id": sub["id"], "status": "duplicate"})
            continue

        delivery_id = await db.insert_event_delivery(
            subscription_id=sub["id"],
            source=event.source,
            event_type=event.event_type,
            event_external_id=event.external_id,
            owner_user_id=event.owner_user_id,
            agent_id=sub["agent_id"],
            status="pending",
            payload_excerpt=event.payload_excerpt(),
        )

        fires.append(asyncio.create_task(
            _safe_execute(sub, event, delivery_id),
            name=f"event_fire_{sub['id'][:8]}",
        ))

    if fires:
        gathered = await asyncio.gather(*fires, return_exceptions=True)
        for r in gathered:
            if isinstance(r, Exception):
                logger.exception("Event fire crashed: %s", r)
                results.append({"status": "error", "error": str(r)})
            else:
                results.append(r)
    return results


async def _safe_execute(sub: dict, event: NormalizedEvent, delivery_id: str) -> dict:
    from app.db import get_db
    from app.events.executor import execute_event_subscription
    db = get_db()
    try:
        result = await execute_event_subscription(sub, event)
        await db.update_event_delivery(
            delivery_id,
            status="ok" if result.get("ok") else "error",
            error=result.get("error"),
            session_id=result.get("session_id"),
        )
        await db.update_event_subscription(
            sub["id"],
            last_event_at=event.received_at,
            last_event_external_id=event.external_id,
            last_status="ok" if result.get("ok") else "error",
            last_error=result.get("error"),
            last_session_id=result.get("session_id"),
            fire_count=(sub.get("fire_count") or 0) + 1,
        )
        return {"subscription_id": sub["id"], **result}
    except Exception as e:
        logger.exception("execute_event_subscription failed: %s", e)
        await db.update_event_delivery(delivery_id, status="error", error=str(e)[:500])
        await db.update_event_subscription(
            sub["id"],
            last_status="error",
            last_error=str(e)[:500],
        )
        return {"subscription_id": sub["id"], "ok": False, "error": str(e)}
