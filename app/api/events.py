"""HTTP intake for push event sources.

Routes:
  - ``POST /api/v1/events/{source}`` — generic push endpoint. The source
    plugin verifies the request, normalizes the payload, and the router
    fans the events out to matching subscriptions.
  - ``GET  /api/v1/events/sources`` — manifest of registered sources +
    schemas (used by the automation parser + UI).
  - ``GET  /api/v1/events/subscriptions`` — list subscriptions (for the
    UI / debugging). Requires auth.

Pub/Sub push payloads (Gmail, Google Calendar, Google Drive) and Graph
notification payloads (Outlook, OneDrive) all flow through the same
endpoint; each source implements its own ``verify_webhook`` + ``handle_webhook``.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query, Request, Response

from app.events import get_manager
from app.events.router import route_event

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/events", tags=["events"])


@router.get("/sources")
async def list_sources():
    """Return the manifest of registered event sources."""
    return {"sources": get_manager().manifest()}


@router.get("/subscriptions")
async def list_subscriptions(
    agent_id: str | None = Query(default=None),
    owner_user_id: str | None = Query(default=None),
    source: str | None = Query(default=None),
):
    """List event subscriptions (filter by agent / owner / source)."""
    from app.db import get_db
    db = get_db()
    rows = await db.list_event_subscriptions(
        agent_id=agent_id, owner_user_id=owner_user_id, source=source,
    )
    return {"subscriptions": rows}


@router.post("/{source_name}")
async def event_intake(source_name: str, request: Request):
    """Push intake for one event source. The source plugin owns verification
    + normalization; this handler just dispatches."""
    mgr = get_manager()
    source = mgr.get(source_name)
    if source is None:
        logger.warning("Event intake for unknown source: %s", source_name)
        raise HTTPException(status_code=404, detail="unknown event source")
    if not source.enabled:
        raise HTTPException(status_code=503, detail="event source disabled")
    if not source.supports_push:
        raise HTTPException(status_code=405, detail="source does not accept push")

    if not await source.verify_webhook(request):
        logger.warning("Event intake verify failed for %s", source_name)
        raise HTTPException(status_code=403, detail="verification failed")

    try:
        events = await source.handle_webhook(request)
    except Exception as e:
        logger.exception("Source %s handle_webhook crashed: %s", source_name, e)
        raise HTTPException(status_code=500, detail="handler failure")

    # Some providers (Microsoft Graph) do a one-time validation handshake
    # whose reply body must echo a token. The source signals this by setting
    # ``request.state.events_short_circuit`` to the Response it wants returned.
    short_circuit = getattr(request.state, "events_short_circuit", None)
    if short_circuit is not None:
        return short_circuit

    results = []
    for evt in events or []:
        try:
            r = await route_event(evt)
            results.extend(r)
        except Exception as e:
            logger.exception("route_event after %s webhook failed: %s", source_name, e)

    # Most push providers expect 200 quickly; the actual fan-out has
    # already been scheduled on its own tasks inside the router.
    return Response(content='{"ok":true}', status_code=200, media_type="application/json")
