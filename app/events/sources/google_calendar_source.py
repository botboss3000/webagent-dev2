"""Google Calendar event source.

Google Calendar's ``events.watch`` pushes change notifications to a generic
webhook URL (not Pub/Sub — Calendar uses the older "channels" mechanism).
Each watch is per-calendar and has a TTL of up to 1 week.

Push payload arrives as HTTP headers (``X-Goog-Resource-State``,
``X-Goog-Channel-Token``, etc.) with an empty body. On notification we call
``events.list`` with ``syncToken`` to enumerate what changed, fetch each
new/updated event, and emit normalized events.

Required environment:
  - ``EVENTS_GCAL_NOTIFICATION_BASE`` — public base URL Calendar will
    POST to, e.g. ``https://your.host``. Combined with
    ``/api/v1/events/google_calendar``.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import uuid as _uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import Request

from app.events.base import EventSource
from app.events.types import NormalizedEvent, SubscriptionRegistration
from app.integrations.oauth_helper import oauth_api_call

logger = logging.getLogger(__name__)

CAL_BASE = "https://www.googleapis.com/calendar/v3"


def _notification_url() -> Optional[str]:
    base = (os.environ.get("EVENTS_GCAL_NOTIFICATION_BASE", "") or "").rstrip("/")
    return f"{base}/api/v1/events/google_calendar" if base else None


class GoogleCalendarSource(EventSource):
    @property
    def name(self) -> str:
        return "google_calendar"

    @property
    def event_types(self) -> List[str]:
        return ["event_added", "event_updated", "event_cancelled"]

    @property
    def supports_push(self) -> bool:
        return True

    @property
    def required_provider(self) -> Optional[str]:
        return "google"

    @property
    def enabled(self) -> bool:
        return bool(_notification_url())

    @property
    def description(self) -> str:
        return ("Google Calendar event changes. Filter by calendar id or "
                "by substring in summary/description.")

    def parser_schema(self) -> Dict[str, Any]:
        base = super().parser_schema()
        base["filter_fields"] = ["calendar_id", "summary_contains", "attendee_contains"]
        base["examples"] = [
            'when a new event is added to my primary calendar, brief me  '
            '(filter: {"calendar_id": "primary"})',
        ]
        return base

    # ── Subscribe / renew / unsubscribe ──────────────────────────────────

    async def register_subscription(
        self, *, owner_user_id: str, agent_id: str, event_type: str, filter_dict: Dict[str, Any]
    ) -> SubscriptionRegistration:
        notif_url = _notification_url()
        if not notif_url:
            raise RuntimeError("EVENTS_GCAL_NOTIFICATION_BASE not configured")
        calendar_id = filter_dict.get("calendar_id") or "primary"

        channel_id = str(_uuid.uuid4())
        token = secrets.token_urlsafe(16)
        ttl_seconds = 7 * 24 * 3600 - 60  # 7 days minus a bit
        body = {
            "id": channel_id,
            "type": "web_hook",
            "address": notif_url,
            "token": token,
            "params": {"ttl": str(ttl_seconds)},
        }
        url = f"{CAL_BASE}/calendars/{calendar_id}/events/watch"
        resp = await oauth_api_call(owner_user_id, agent_id, "google", "POST", url, json_body=body)
        if resp.get("status") != "ok":
            raise RuntimeError(f"Calendar watch failed: {resp}")
        body_out = resp.get("body") or {}

        # Seed a syncToken so the very first notification has a baseline to diff from.
        sync_token = await self._fetch_initial_sync_token(owner_user_id, agent_id, calendar_id)

        exp_ms = body_out.get("expiration")
        try:
            exp_iso = datetime.fromtimestamp(int(exp_ms) / 1000.0, timezone.utc).isoformat() if exp_ms else None
        except Exception:
            exp_iso = None

        return SubscriptionRegistration(
            external_subscription_id=channel_id,
            external_resource_id=body_out.get("resourceId"),
            external_expiration_at=exp_iso,
            external_metadata={
                "channel_token": token,
                "calendar_id": calendar_id,
                "sync_token": sync_token or "",
            },
        )

    async def renew_subscription(self, subscription_row: Dict[str, Any]) -> SubscriptionRegistration:
        # Calendar channels can't be PATCHed in place — stop the old one + create a new one.
        try:
            await self.unregister_subscription(subscription_row)
        except Exception:
            pass
        return await self.register_subscription(
            owner_user_id=subscription_row["owner_user_id"],
            agent_id=subscription_row.get("agent_id", ""),
            event_type=subscription_row["event_type"],
            filter_dict=subscription_row.get("filter") or {},
        )

    async def unregister_subscription(self, subscription_row: Dict[str, Any]) -> None:
        channel_id = subscription_row.get("external_subscription_id")
        resource_id = subscription_row.get("external_resource_id")
        if not channel_id or not resource_id:
            return
        try:
            await oauth_api_call(
                subscription_row["owner_user_id"], subscription_row.get("agent_id", ""), "google", "POST",
                f"{CAL_BASE}/channels/stop",
                json_body={"id": channel_id, "resourceId": resource_id},
            )
        except Exception as e:
            logger.warning("Calendar channel stop failed: %s", e)

    async def _fetch_initial_sync_token(self, user_id: str, agent_id: str, calendar_id: str) -> Optional[str]:
        params = {"maxResults": 1, "showDeleted": "true", "singleEvents": "true"}
        page_token = None
        for _ in range(20):
            if page_token:
                params["pageToken"] = page_token
            url = f"{CAL_BASE}/calendars/{calendar_id}/events"
            resp = await oauth_api_call(user_id, agent_id, "google", "GET", url, params=params)
            if resp.get("status") != "ok":
                return None
            body = resp.get("body") or {}
            if body.get("nextSyncToken"):
                return body["nextSyncToken"]
            page_token = body.get("nextPageToken")
            if not page_token:
                return body.get("nextSyncToken")
        return None

    # ── Inbound ──────────────────────────────────────────────────────────

    async def verify_webhook(self, request: Request) -> bool:
        # Calendar pushes are unsigned; we authenticate via the per-channel
        # token + channel id, validated in handle_webhook.
        channel_id = request.headers.get("x-goog-channel-id") or ""
        token = request.headers.get("x-goog-channel-token") or ""
        if not channel_id:
            return False
        from app.db import get_db
        db = get_db()
        rows = await db.find_event_subscriptions_by_external(self.name, channel_id)
        if not rows:
            return False
        meta = rows[0].get("external_metadata") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        return (meta.get("channel_token") or "") == token

    async def handle_webhook(self, request: Request) -> List[NormalizedEvent]:
        channel_id = request.headers.get("x-goog-channel-id") or ""
        state = (request.headers.get("x-goog-resource-state") or "").lower()
        if state == "sync":
            return []  # initial handshake
        if not channel_id:
            return []
        from app.db import get_db
        db = get_db()
        rows = await db.find_event_subscriptions_by_external(self.name, channel_id)
        if not rows:
            return []
        sub = rows[0]
        meta = sub.get("external_metadata") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        calendar_id = meta.get("calendar_id") or "primary"
        sync_token = meta.get("sync_token") or ""
        user_id = sub["owner_user_id"]
        agent_id = sub.get("agent_id", "")

        new_events, new_sync_token = await self._list_changed_events(user_id, agent_id, calendar_id, sync_token)

        if new_sync_token and new_sync_token != sync_token:
            meta["sync_token"] = new_sync_token
            try:
                await db.update_event_subscription(sub["id"], external_metadata=meta)
            except Exception as e:
                logger.debug("Could not persist new sync token: %s", e)

        filter_dict = sub.get("filter") or {}
        emitted: List[NormalizedEvent] = []
        for ev in new_events:
            if not self._matches(ev, filter_dict):
                continue
            emitted.append(self._build_event(user_id, calendar_id, ev))
        return emitted

    async def _list_changed_events(
        self, user_id: str, agent_id: str, calendar_id: str, sync_token: str,
    ) -> tuple[List[dict], Optional[str]]:
        url = f"{CAL_BASE}/calendars/{calendar_id}/events"
        params: Dict[str, Any] = {"showDeleted": "true", "singleEvents": "true"}
        if sync_token:
            params["syncToken"] = sync_token
        else:
            # No baseline → take whatever's around now forward.
            params["timeMin"] = datetime.now(timezone.utc).isoformat()
        out: List[dict] = []
        next_sync = None
        page_token = None
        for _ in range(10):
            if page_token:
                params["pageToken"] = page_token
            resp = await oauth_api_call(user_id, agent_id, "google", "GET", url, params=params)
            if resp.get("status") != "ok":
                logger.warning("Calendar events.list failed: %s", resp)
                break
            body = resp.get("body") or {}
            for ev in body.get("items") or []:
                out.append(ev)
            next_sync = body.get("nextSyncToken") or next_sync
            page_token = body.get("nextPageToken")
            if not page_token:
                break
        return out, next_sync

    def _matches(self, ev: dict, f: Dict[str, Any]) -> bool:
        if not f:
            return True
        sc = (f.get("summary_contains") or "").lower()
        if sc and sc not in (ev.get("summary") or "").lower():
            return False
        ac = (f.get("attendee_contains") or "").lower()
        if ac:
            attendees = ev.get("attendees") or []
            hits = any(ac in (a.get("email") or "").lower() for a in attendees)
            if not hits:
                return False
        return True

    def _build_event(self, user_id: str, calendar_id: str, ev: dict) -> NormalizedEvent:
        status = (ev.get("status") or "").lower()
        if status == "cancelled":
            etype = "event_cancelled"
        elif ev.get("updated") and ev.get("created") and ev["updated"] != ev["created"]:
            etype = "event_updated"
        else:
            etype = "event_added"
        start = (ev.get("start") or {}).get("dateTime") or (ev.get("start") or {}).get("date") or ""
        end = (ev.get("end") or {}).get("dateTime") or (ev.get("end") or {}).get("date") or ""
        return NormalizedEvent(
            source=self.name,
            event_type=etype,
            owner_user_id=user_id,
            external_id=ev.get("id") or "",
            occurred_at=ev.get("updated") or datetime.now(timezone.utc).isoformat(),
            payload={
                "calendar_id": calendar_id,
                "summary": ev.get("summary") or "",
                "description": (ev.get("description") or "")[:1000],
                "location": ev.get("location") or "",
                "start": start,
                "end": end,
                "attendees": [
                    {"email": a.get("email"), "response": a.get("responseStatus")}
                    for a in (ev.get("attendees") or [])
                ],
                "html_link": ev.get("htmlLink") or "",
                "status": status,
            },
            raw_ref={"event_id": ev.get("id"), "calendar_id": calendar_id},
        )

    def matches_filter(self, event, filter_dict: Dict[str, Any]) -> bool:
        return True  # already enforced at fetch


source_cls = GoogleCalendarSource
