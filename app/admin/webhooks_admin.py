"""
Admin endpoints for generic inbound webhook management.

- GET  /admin/webhooks              — list all generic webhooks + base URL
- POST /admin/webhooks/{id}/toggle  — enable/disable a webhook
- GET  /admin/webhooks/{id}/logs    — get event logs for a webhook
"""

import json
import logging

from fastapi import APIRouter
from app.db import get_db
from app.communications.manager import get_plugin_manager

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/webhooks", tags=["admin"])


@router.get("")
async def list_webhooks_admin():
    """List all generic webhook registrations across all users, plus base URL."""
    db = get_db()
    # We need a way to list ALL webhooks. The backend methods are user-scoped.
    # Use the raw client to get all registrations.
    client = db.get_raw_client()
    try:
        rows = client.table("webhook_registrations").select("*").order("created_at", desc=True).execute()
        hooks = rows.data or []
    except Exception:
        # Fallback: iterate known users (limited approach — fine for local dev)
        hooks = []
        known_users = set()
        try:
            sessions = client.table("sessions").select("user_id").execute()
            for s in (sessions.data or []):
                uid = s.get("user_id")
                if uid:
                    known_users.add(uid)
        except Exception:
            pass
        for uid in known_users:
            user_hooks = await db.list_webhooks(uid)
            hooks.extend(user_hooks)
        hooks.sort(key=lambda h: h.get("created_at", ""), reverse=True)

    # Get base URL from plugin registry
    pm = get_plugin_manager()
    registry = getattr(pm, "_registry", {})
    base_url = registry.get("webhook_base_url", "")

    return {
        "webhooks": hooks,
        "webhook_base_url": base_url,
        "count": len(hooks),
    }


@router.post("/{webhook_id}/toggle")
async def toggle_webhook(webhook_id: str):
    """Enable or disable a webhook registration."""
    db = get_db()
    reg = await db.get_webhook(webhook_id)
    if not reg:
        return {"status": "error", "message": "Webhook not found"}

    new_active = not reg.get("active", True)
    client = db.get_raw_client()
    client.table("webhook_registrations").update({
        "active": 1 if new_active else 0,
    }).eq("id", webhook_id).execute()

    return {
        "status": "ok",
        "webhook_id": webhook_id,
        "active": new_active,
    }


@router.delete("/{webhook_id}/delete")
async def delete_webhook_admin(webhook_id: str):
    """Delete a webhook registration by id."""
    db = get_db()
    reg = await db.get_webhook(webhook_id)
    if not reg:
        return {"status": "error", "message": "Webhook not found"}
    client = db.get_raw_client()
    client.table("webhook_registrations").delete().eq("id", webhook_id).execute()
    return {"status": "ok", "webhook_id": webhook_id}


@router.get("/{webhook_id}/logs")
async def get_webhook_logs_admin(webhook_id: str, limit: int = 20):
    """Get recent event logs for a webhook registration."""
    db = get_db()
    logs = await db.get_webhook_logs(webhook_id, limit=limit)
    return {
        "status": "ok",
        "webhook_id": webhook_id,
        "events": logs,
        "count": len(logs),
    }
