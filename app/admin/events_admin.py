"""Admin-level event-trigger infrastructure.

Operator-facing endpoints under ``/admin/events/*``. Used by the App Config
"Event Sources" panel to:
  - inspect which sources are enabled (env var / dependency status)
  - count subscriptions per source across all users
  - bulk re-register or stop-all watches for a source (e.g. after recreating
    a Pub/Sub topic)
  - browse recent delivery rows for debugging

All endpoints are admin-only; callers pass ``requesting_user_id`` and we
verify with ``db.is_user_admin``.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query

from app.db import get_db
from app.events import get_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/events", tags=["admin-events"])


async def _require_admin(db, user_id: str) -> None:
    # Routes through the shared chokepoint so 'open' access mode grants the
    # bootstrap admin to a tokenless tunnel caller; non-open modes still require
    # a real DB admin id. See app.auth.identity.resolve_admin_uid. (db arg kept
    # for signature compatibility with existing call sites.)
    from app.auth.identity import resolve_admin_uid
    if not await resolve_admin_uid(user_id):
        raise HTTPException(status_code=403, detail="Admin access required.")


_ENV_VAR_BY_SOURCE: Dict[str, List[str]] = {
    "gmail": ["EVENTS_GMAIL_PUBSUB_TOPIC", "EVENTS_PUBSUB_AUDIENCE"],
    "outlook_mail": ["EVENTS_GRAPH_NOTIFICATION_BASE"],
    "outlook_calendar": ["EVENTS_GRAPH_NOTIFICATION_BASE"],
    "onedrive": ["EVENTS_GRAPH_NOTIFICATION_BASE"],
    "google_calendar": ["EVENTS_GCAL_NOTIFICATION_BASE"],
    "google_drive": ["EVENTS_GDRIVE_NOTIFICATION_BASE"],
    "dropbox": ["EVENTS_DROPBOX_WEBHOOK_BASE"],
    "shopify": ["EVENTS_SHOPIFY_WEBHOOK_BASE"],
}


@router.get("/sources")
async def list_sources_admin(requesting_user_id: str = Query(...)):
    """Per-source overview: enable status + env-var presence + sub counts."""
    db = get_db()
    await _require_admin(db, requesting_user_id)

    mgr = get_manager()
    all_subs = await db.list_event_subscriptions()
    by_source: Dict[str, List[dict]] = {}
    for s in all_subs:
        by_source.setdefault(s["source"], []).append(s)

    out: List[Dict[str, Any]] = []
    for source in mgr.all():
        subs = by_source.get(source.name, [])
        enabled = sum(1 for s in subs if s.get("enabled"))
        errored = sum(1 for s in subs if (s.get("last_status") == "error"))
        env_vars = _ENV_VAR_BY_SOURCE.get(source.name, [])
        env_status = [
            {"name": v, "set": bool(os.environ.get(v))} for v in env_vars
        ]
        out.append({
            "name": source.name,
            "event_types": list(source.event_types),
            "supports_push": source.supports_push,
            "supports_poll": source.supports_poll,
            "required_provider": source.required_provider,
            "enabled": source.enabled,
            "description": source.description,
            "env_vars": env_status,
            "subscription_total": len(subs),
            "subscription_enabled": enabled,
            "subscription_errored": errored,
        })
    out.sort(key=lambda r: (not r["enabled"], r["name"]))
    return {"sources": out}


@router.get("/subscriptions")
async def list_all_subscriptions_admin(
    requesting_user_id: str = Query(...),
    source: Optional[str] = Query(default=None),
    owner_user_id: Optional[str] = Query(default=None),
    limit: int = Query(default=200, le=2000),
):
    """Cross-user subscription list (admin only)."""
    db = get_db()
    await _require_admin(db, requesting_user_id)
    rows = await db.list_event_subscriptions(
        source=source, owner_user_id=owner_user_id,
    )
    return {"subscriptions": rows[:limit], "total": len(rows)}


@router.get("/deliveries")
async def list_recent_deliveries(
    requesting_user_id: str = Query(...),
    subscription_id: Optional[str] = Query(default=None),
    source: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    limit: int = Query(default=100, le=1000),
):
    """Recent ``event_deliveries`` rows. Useful for debugging."""
    db = get_db()
    await _require_admin(db, requesting_user_id)
    conn = db._get_conn()
    try:
        clauses, params = [], []
        if subscription_id:
            clauses.append("subscription_id = ?"); params.append(subscription_id)
        if source:
            clauses.append("source = ?"); params.append(source)
        if status:
            clauses.append("status = ?"); params.append(status)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = conn.execute(
            f"SELECT * FROM event_deliveries {where} ORDER BY created_at DESC LIMIT ?",
            [*params, int(limit)],
        ).fetchall()
        return {"deliveries": [dict(r) for r in rows]}
    finally:
        conn.close()


@router.post("/sources/{source_name}/re-register-all")
async def re_register_all(
    source_name: str,
    requesting_user_id: str = Query(...),
):
    """Re-run ``register_subscription`` for every enabled row on this source.

    Used after recreating the Pub/Sub topic, switching Pub/Sub audiences, or
    rotating the Graph notification base URL. Best-effort: failures are
    recorded on each row's ``last_status``/``last_error`` and reported
    individually in the response.
    """
    db = get_db()
    await _require_admin(db, requesting_user_id)
    mgr = get_manager()
    source = mgr.get(source_name)
    if not source:
        raise HTTPException(status_code=404, detail="Unknown event source")
    if not source.enabled:
        raise HTTPException(status_code=400, detail="Source not enabled (missing env vars?)")

    rows = await db.list_event_subscriptions(source=source_name, enabled_only=True)
    succeeded, failed = [], []
    for row in rows:
        try:
            if row.get("external_subscription_id"):
                try:
                    await source.unregister_subscription(row)
                except Exception as e:
                    logger.debug("unregister-before-rereg failed: %s", e)
            reg = await source.register_subscription(
                owner_user_id=row["owner_user_id"],
                event_type=row["event_type"],
                filter_dict=row.get("filter") or {},
            )
            await db.update_event_subscription(
                row["id"],
                external_subscription_id=reg.external_subscription_id,
                external_resource_id=reg.external_resource_id,
                external_expiration_at=reg.external_expiration_at,
                external_metadata=reg.external_metadata or {},
                poll_cursor=reg.poll_cursor,
                last_status="ok",
                last_error=None,
            )
            succeeded.append(row["id"])
        except Exception as e:
            logger.warning("Bulk re-register failed for sub %s: %s", row["id"], e)
            try:
                await db.update_event_subscription(
                    row["id"], last_status="error", last_error=str(e)[:400],
                )
            except Exception:
                pass
            failed.append({"id": row["id"], "error": str(e)[:400]})

    return {
        "source": source_name,
        "succeeded": len(succeeded),
        "failed": len(failed),
        "errors": failed,
    }


@router.post("/sources/{source_name}/stop-all")
async def stop_all(
    source_name: str,
    requesting_user_id: str = Query(...),
    disable_rows: bool = Query(default=False),
):
    """Call ``unregister_subscription`` for every row on this source.

    Use before deleting infrastructure (Pub/Sub topic, Graph app, etc.).
    Rows stay in the DB so they can be re-registered later; pass
    ``disable_rows=true`` to also flip them to ``enabled=false`` so they
    don't get picked up by the renewer until you re-enable them.
    """
    db = get_db()
    await _require_admin(db, requesting_user_id)
    mgr = get_manager()
    source = mgr.get(source_name)
    if not source:
        raise HTTPException(status_code=404, detail="Unknown event source")

    rows = await db.list_event_subscriptions(source=source_name)
    stopped, failed = [], []
    for row in rows:
        try:
            await source.unregister_subscription(row)
            if disable_rows:
                await db.update_event_subscription(
                    row["id"], enabled=False,
                    external_subscription_id=None,
                    external_resource_id=None,
                    external_expiration_at=None,
                    last_status="stopped",
                    last_error=None,
                )
            else:
                await db.update_event_subscription(
                    row["id"],
                    external_subscription_id=None,
                    external_resource_id=None,
                    external_expiration_at=None,
                    last_status="stopped",
                )
            stopped.append(row["id"])
        except Exception as e:
            logger.warning("stop-all unregister failed for sub %s: %s", row["id"], e)
            failed.append({"id": row["id"], "error": str(e)[:400]})

    return {
        "source": source_name,
        "stopped": len(stopped),
        "failed": len(failed),
        "errors": failed,
        "disabled_rows": disable_rows,
    }


@router.get("/runtime-status")
async def runtime_status(requesting_user_id: str = Query(...)):
    """Quick health view of the in-process poller + renewer loops."""
    db = get_db()
    await _require_admin(db, requesting_user_id)
    mgr = get_manager()
    poller_running = bool(mgr._poller_task and not mgr._poller_task.done())
    renewer_running = bool(mgr._renewer_task and not mgr._renewer_task.done())

    # Expiring-soon count for an at-a-glance dashboard.
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
    expiring = await db.list_event_subscriptions_needing_renewal(cutoff)

    return {
        "poller_running": poller_running,
        "renewer_running": renewer_running,
        "subscriptions_expiring_within_24h": len(expiring),
    }
