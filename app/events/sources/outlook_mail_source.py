"""Outlook Mail event source via Microsoft Graph ``/subscriptions``.

Lifecycle:
  1. ``register_subscription`` creates a Graph subscription on
     ``me/mailFolders('Inbox')/messages`` with changeType=created. Graph
     posts notifications to ``POST /api/v1/events/outlook_mail`` with a
     ``validationToken`` query param on the first request — we echo it back.
  2. Each subsequent POST is a JSON envelope of zero+ notifications. We
     verify ``clientState`` against the stored value for each, look up the
     subscription row, fetch the new message metadata, apply the filter,
     and emit ``outlook_mail.message_received`` events.
  3. ``renew_subscription`` PATCHes the Graph subscription before its TTL
     (~3 days for mail) expires.

Required environment:
  - ``EVENTS_GRAPH_NOTIFICATION_BASE`` — public base URL the Graph
    notifications POST to, e.g. ``https://your.host``. Combined with
    ``/api/v1/events/outlook_mail`` to form ``notificationUrl``.
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
from app.integrations.oauth_helper import oauth_api_call, oauth_label

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"


def _notification_url() -> Optional[str]:
    base = (os.environ.get("EVENTS_GRAPH_NOTIFICATION_BASE", "") or "").rstrip("/")
    return f"{base}/api/v1/events/outlook_mail" if base else None


class OutlookMailSource(EventSource):
    @property
    def name(self) -> str:
        return "outlook_mail"

    @property
    def event_types(self) -> List[str]:
        return ["message_received"]

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
        return ("Outlook / Microsoft 365 inbox messages via Microsoft Graph "
                "subscriptions. Filter by sender substring or subject text.")

    def parser_schema(self) -> Dict[str, Any]:
        base = super().parser_schema()
        base["filter_fields"] = ["from_contains", "subject_contains"]
        base["examples"] = [
            'when an Outlook email arrives from anyone @airlines.com '
            '(filter: {"from_contains": "airlines.com"})',
        ]
        return base

    # ── Subscribe / renew / unsubscribe ──────────────────────────────────

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
            change_type="created",
            resource="me/mailFolders('Inbox')/messages",
            notification_url=notif_url,
            client_state=client_state,
            expiration=exp,
        )
        if resp.get("status") != "ok":
            raise RuntimeError(f"Graph subscription create failed: {resp}")
        body = resp.get("body") or {}
        sub_id = body.get("id") or ""
        if not sub_id:
            raise RuntimeError("Graph subscription returned no id")

        # Capture user email for downstream attribution.
        connected_email = ""
        try:
            from app.db import get_db
            db = get_db()
            elem = await db.auth_element_get(owner_user_id, "microsoft", oauth_label(agent_id))
            cfg = json.loads(elem.get("config") or "{}") if elem else {}
            connected_email = (cfg.get("email") or "").lower()
        except Exception:
            pass

        return SubscriptionRegistration(
            external_subscription_id=sub_id,
            external_expiration_at=body.get("expirationDateTime") or exp,
            external_metadata={
                "client_state": client_state,
                "email": connected_email,
                "notification_url": notif_url,
            },
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
            # Subscription may be gone — recreate.
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
            logger.warning("Graph delete subscription failed: %s", e)

    # ── Inbound ──────────────────────────────────────────────────────────

    async def verify_webhook(self, request: Request) -> bool:
        # Validation handshake responds with the token directly; signature
        # is via clientState which we check in handle_webhook.
        return True

    async def handle_webhook(self, request: Request) -> List[NormalizedEvent]:
        validation = await maybe_validation_response(request)
        if validation is not None:
            # FastAPI returns this directly via the intake; but the intake
            # always returns its own 200. We need to short-circuit: raise
            # a marker exception? Simpler: bypass via attaching to request.state.
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

        # Group notifications by subscription id so we batch DB lookups.
        for note in notes:
            sub_id = note.get("subscriptionId")
            client_state = note.get("clientState") or ""
            resource = note.get("resource") or ""
            change_type = note.get("changeType") or ""
            if not sub_id or change_type != "created":
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
            stored_cs = meta.get("client_state") or ""
            if stored_cs and stored_cs != client_state:
                logger.warning("Outlook clientState mismatch for sub %s", sub_id)
                continue

            msg = await self._fetch_message(row["owner_user_id"], row.get("agent_id", ""), resource)
            if not msg:
                continue
            filter_dict = row.get("filter") or {}
            if not self._matches(msg, filter_dict):
                continue
            events.append(self._build_event(row["owner_user_id"], meta.get("email", ""), msg))

        return events

    async def _fetch_message(self, user_id: str, agent_id: str, resource_path: str) -> Optional[dict]:
        url = f"{GRAPH_BASE}/{resource_path.lstrip('/')}"
        resp = await oauth_api_call(user_id, agent_id, "microsoft", "GET", url)
        if resp.get("status") != "ok":
            logger.debug("Outlook fetch failed for %s: %s", resource_path, resp)
            return None
        return resp.get("body") or None

    def _matches(self, msg: dict, f: Dict[str, Any]) -> bool:
        if not f:
            return True
        from_contains = (f.get("from_contains") or "").lower()
        if from_contains:
            sender = ((msg.get("from") or {}).get("emailAddress") or {}).get("address", "").lower()
            if from_contains not in sender:
                return False
        subj_contains = (f.get("subject_contains") or "").lower()
        if subj_contains and subj_contains not in (msg.get("subject") or "").lower():
            return False
        return True

    def _build_event(self, user_id: str, account_email: str, msg: dict) -> NormalizedEvent:
        sender = ((msg.get("from") or {}).get("emailAddress") or {}).get("address", "")
        return NormalizedEvent(
            source=self.name,
            event_type="message_received",
            owner_user_id=user_id,
            external_id=msg.get("id") or "",
            occurred_at=msg.get("receivedDateTime") or datetime.now(timezone.utc).isoformat(),
            payload={
                "account_email": account_email,
                "from": sender,
                "to": ", ".join(
                    (t.get("emailAddress") or {}).get("address", "")
                    for t in (msg.get("toRecipients") or [])
                ),
                "subject": msg.get("subject") or "",
                "snippet": (msg.get("bodyPreview") or "")[:500],
                "conversation_id": msg.get("conversationId") or "",
            },
            raw_ref={
                "message_id": msg.get("id"),
                "conversation_id": msg.get("conversationId"),
            },
        )

    def matches_filter(self, event, filter_dict: Dict[str, Any]) -> bool:
        return True  # Already enforced in handle_webhook


source_cls = OutlookMailSource
