"""Outlook Calendar event source via Microsoft Graph subscriptions.

Subscribes to ``me/events`` with changeType=``created,updated,deleted``.
Notification handling mirrors ``outlook_mail_source`` — fetch each new
event by resource path, apply the filter, emit normalized events.

Required environment:
  - ``EVENTS_GRAPH_NOTIFICATION_BASE`` (shared with outlook_mail).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import Request

from app.events.base import EventSource
from app.events.providers.graph_subscription import (
    create_subscription,
    delete_subscription,
    expiration_iso,
    maybe_validation_response,
    new_client_state,
    renew_subscription as graph_renew,
)
from app.events.types import NormalizedEvent, SubscriptionRegistration
from app.integrations.oauth_helper import oauth_api_call

logger = logging.getLogger(__name__)
GRAPH_BASE = "https://graph.microsoft.com/v1.0"


def _notification_url() -> Optional[str]:
    base = (os.environ.get("EVENTS_GRAPH_NOTIFICATION_BASE", "") or "").rstrip("/")
    return f"{base}/api/v1/events/outlook_calendar" if base else None


class OutlookCalendarSource(EventSource):
    @property
    def name(self) -> str:
        return "outlook_calendar"

    @property
    def event_types(self) -> List[str]:
        return ["event_added", "event_updated", "event_cancelled"]

    @property
    def supports_push(self) -> bool:
        return True

    @property
    def required_provider(self) -> Optional[str]:
        return "microsoft"

    @property
    def enabled(self) -> bool:
        return bool(_notification_url())

    @property
    def description(self) -> str:
        return ("Outlook / M365 calendar changes via Graph subscriptions.")

    def parser_schema(self) -> Dict[str, Any]:
        base = super().parser_schema()
        base["filter_fields"] = ["subject_contains", "attendee_contains"]
        return base

    async def register_subscription(
        self, *, owner_user_id: str, agent_id: str, event_type: str, filter_dict: Dict[str, Any]
    ) -> SubscriptionRegistration:
        notif_url = _notification_url()
        if not notif_url:
            raise RuntimeError("EVENTS_GRAPH_NOTIFICATION_BASE not configured")
        client_state = new_client_state()
        exp = expiration_iso()
        resp = await create_subscription(
            user_id=owner_user_id,
            agent_id=agent_id,
            change_type="created,updated,deleted",
            resource="me/events",
            notification_url=notif_url,
            client_state=client_state,
            expiration=exp,
        )
        if resp.get("status") != "ok":
            raise RuntimeError(f"Graph calendar subscription failed: {resp}")
        body = resp.get("body") or {}
        sub_id = body.get("id") or ""
        if not sub_id:
            raise RuntimeError("Graph subscription returned no id")
        return SubscriptionRegistration(
            external_subscription_id=sub_id,
            external_expiration_at=body.get("expirationDateTime") or exp,
            external_metadata={"client_state": client_state, "notification_url": notif_url},
        )

    async def renew_subscription(self, subscription_row: Dict[str, Any]) -> SubscriptionRegistration:
        sub_id = subscription_row.get("external_subscription_id")
        agent_id = subscription_row.get("agent_id", "")
        if not sub_id:
            return await self.register_subscription(
                owner_user_id=subscription_row["owner_user_id"],
                agent_id=agent_id,
                event_type=subscription_row["event_type"],
                filter_dict=subscription_row.get("filter") or {},
            )
        exp = expiration_iso()
        resp = await graph_renew(
            user_id=subscription_row["owner_user_id"],
            agent_id=agent_id,
            subscription_id=sub_id,
            expiration=exp,
        )
        if resp.get("status") != "ok":
            return await self.register_subscription(
                owner_user_id=subscription_row["owner_user_id"],
                agent_id=agent_id,
                event_type=subscription_row["event_type"],
                filter_dict=subscription_row.get("filter") or {},
            )
        body = resp.get("body") or {}
        meta = subscription_row.get("external_metadata") or {}
        return SubscriptionRegistration(
            external_subscription_id=sub_id,
            external_expiration_at=body.get("expirationDateTime") or exp,
            external_metadata=meta if isinstance(meta, dict) else {},
        )

    async def unregister_subscription(self, subscription_row: Dict[str, Any]) -> None:
        sub_id = subscription_row.get("external_subscription_id")
        if not sub_id:
            return
        try:
            await delete_subscription(
                user_id=subscription_row["owner_user_id"],
                agent_id=subscription_row.get("agent_id", ""),
                subscription_id=sub_id,
            )
        except Exception as e:
            logger.warning("Outlook calendar unsubscribe failed: %s", e)

    async def handle_webhook(self, request: Request) -> List[NormalizedEvent]:
        validation = await maybe_validation_response(request)
        if validation is not None:
            request.state.events_short_circuit = validation
            return []
        try:
            body = await request.json()
        except Exception:
            return []
        notes = body.get("value") or []
        if not notes:
            return []
        from app.db import get_db
        db = get_db()
        events: List[NormalizedEvent] = []
        for note in notes:
            sub_id = note.get("subscriptionId")
            client_state = note.get("clientState") or ""
            resource = note.get("resource") or ""
            change_type = (note.get("changeType") or "").lower()
            if not sub_id or change_type not in ("created", "updated", "deleted"):
                continue
            rows = await db.find_event_subscriptions_by_external(self.name, sub_id)
            if not rows:
                continue
            row = rows[0]
            meta = row.get("external_metadata") or {}
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except Exception:
                    meta = {}
            if (meta.get("client_state") or "") != client_state:
                logger.warning("Outlook calendar clientState mismatch for sub %s", sub_id)
                continue

            if change_type == "deleted":
                # Graph deleted notifications carry no body — emit a cancelled event with the resource id.
                events.append(NormalizedEvent(
                    source=self.name,
                    event_type="event_cancelled",
                    owner_user_id=row["owner_user_id"],
                    external_id=resource.split("/")[-1] if resource else "",
                    occurred_at=datetime.now(timezone.utc).isoformat(),
                    payload={"resource": resource, "deleted": True},
                    raw_ref={"resource": resource},
                ))
                continue

            url = f"{GRAPH_BASE}/{resource.lstrip('/')}"
            resp = await oauth_api_call(row["owner_user_id"], row.get("agent_id", ""), "microsoft", "GET", url)
            if resp.get("status") != "ok":
                continue
            ev = resp.get("body") or {}
            filter_dict = row.get("filter") or {}
            if not self._matches(ev, filter_dict):
                continue
            etype = "event_added" if change_type == "created" else "event_updated"
            events.append(self._build_event(row["owner_user_id"], ev, etype))
        return events

    def _matches(self, ev: dict, f: Dict[str, Any]) -> bool:
        if not f:
            return True
        sc = (f.get("subject_contains") or "").lower()
        if sc and sc not in (ev.get("subject") or "").lower():
            return False
        ac = (f.get("attendee_contains") or "").lower()
        if ac:
            attendees = ev.get("attendees") or []
            hits = any(ac in (((a.get("emailAddress") or {}).get("address") or "").lower()) for a in attendees)
            if not hits:
                return False
        return True

    def _build_event(self, user_id: str, ev: dict, etype: str) -> NormalizedEvent:
        start = ((ev.get("start") or {}).get("dateTime") or "")
        end = ((ev.get("end") or {}).get("dateTime") or "")
        return NormalizedEvent(
            source=self.name,
            event_type=etype,
            owner_user_id=user_id,
            external_id=ev.get("id") or "",
            occurred_at=ev.get("lastModifiedDateTime") or datetime.now(timezone.utc).isoformat(),
            payload={
                "subject": ev.get("subject") or "",
                "body_preview": (ev.get("bodyPreview") or "")[:500],
                "start": start,
                "end": end,
                "location": (ev.get("location") or {}).get("displayName") or "",
                "organizer": ((ev.get("organizer") or {}).get("emailAddress") or {}).get("address", ""),
                "web_link": ev.get("webLink") or "",
            },
            raw_ref={"event_id": ev.get("id")},
        )

    def matches_filter(self, event, filter_dict: Dict[str, Any]) -> bool:
        return True


source_cls = OutlookCalendarSource
