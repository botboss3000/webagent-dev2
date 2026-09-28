"""Gmail event source.

Push flow:
  1. ``register_subscription`` for a user calls Gmail ``users.watch`` on
     their Gmail account, asking Google to publish change notifications
     to a configured Cloud Pub/Sub topic. Saves the returned ``historyId``
     and ``expiration`` (max 7 days).
  2. Cloud Pub/Sub pushes notifications to
     ``POST /api/v1/events/gmail`` — the intake calls ``verify_webhook``
     (OIDC JWT verification), then ``handle_webhook``.
  3. ``handle_webhook`` decodes the Pub/Sub envelope, looks up the user by
     the ``emailAddress`` claim, calls ``users.history.list`` from the
     stored ``historyId`` forward to enumerate new ``messageAdded`` items,
     fetches each new message's metadata, applies the per-subscription
     filter (Gmail-style ``q``) at fetch time, and emits one
     ``gmail.message_received`` event per new matching message.
  4. The renewer re-runs ``register_subscription`` daily.

Required environment:
  - ``EVENTS_GMAIL_PUBSUB_TOPIC`` — full topic name,
    e.g. ``projects/webagent-495517/topics/webagent-gmail-watch``
  - ``EVENTS_PUBSUB_AUDIENCE`` — Pub/Sub push audience (also set on the
    Pub/Sub subscription). Typically the public webhook URL.
  - ``EVENTS_PUBSUB_SA_EMAIL`` (optional) — extra check on the push SA.

See ``docs/events-setup.md`` for one-time GCP setup.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import Request

from app.events.base import EventSource
from app.events.providers.google_pubsub import (
    decode_pubsub_envelope,
    verify_pubsub_jwt,
)
from app.events.types import NormalizedEvent, SubscriptionRegistration
from app.integrations.oauth_helper import oauth_api_call, oauth_label

logger = logging.getLogger(__name__)

_GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"


def _topic_name() -> Optional[str]:
    return os.environ.get("EVENTS_GMAIL_PUBSUB_TOPIC", "").strip() or None


def _expiration_iso_from_ms(expiration_ms_str: str) -> Optional[str]:
    try:
        ms = int(expiration_ms_str)
        return datetime.fromtimestamp(ms / 1000.0, timezone.utc).isoformat()
    except Exception:
        return None


def _header(headers: List[dict], name: str) -> str:
    name_l = name.lower()
    for h in headers or []:
        if (h.get("name") or "").lower() == name_l:
            return h.get("value") or ""
    return ""


async def _get_connected_email(db, owner_user_id: str, agent_id: str) -> str:
    """Read the connected Gmail address out of the user's OAuth element."""
    try:
        elem = await db.auth_element_get(owner_user_id, "google", oauth_label(agent_id))
        cfg = json.loads(elem.get("config") or "{}") if elem else {}
        return (cfg.get("email") or "").lower()
    except Exception:
        return ""


class GmailSource(EventSource):
    """Gmail event source.

    Two modes, picked automatically based on whether `EVENTS_GMAIL_PUBSUB_TOPIC`
    is configured:
      * **Push** (preferred) — Gmail `users.watch` → Cloud Pub/Sub → our webhook.
        True real-time. Requires GCP setup (see docs/events-setup.md).
      * **Poll fallback** — when the topic env var is unset. The background
        poller calls `users.history.list` on a cadence (default 60s) and emits
        the same `message_received` events. Quota-cheap; ~60s delivery lag.

    Either way the subscription row + per-event payload look identical to the
    rest of the system, so the router / executor / filters work unchanged.
    """

    @property
    def name(self) -> str:
        return "gmail"

    @property
    def event_types(self) -> List[str]:
        return ["message_received"]

    @property
    def supports_push(self) -> bool:
        # Real push only when Pub/Sub is configured.
        return bool(_topic_name())

    @property
    def supports_poll(self) -> bool:
        # Poll fallback whenever push is NOT configured. Mutually exclusive
        # so each subscription is wired one way.
        return not bool(_topic_name())

    @property
    def default_poll_interval_seconds(self) -> int:
        # Gmail history.list quota is cheap; 60s is a comfortable cadence.
        return 60

    @property
    def required_provider(self) -> Optional[str]:
        return "google"

    @property
    def enabled(self) -> bool:
        # Either path works as long as the user has Google OAuth connected;
        # provider-side wiring is handled per-subscription.
        return True

    @property
    def description(self) -> str:
        if _topic_name():
            return ("Gmail inbox changes pushed via Google Cloud Pub/Sub (real-time). "
                    "Filter with Gmail search syntax in the `query` field "
                    "(e.g. `from:airlines.com`, `subject:flight`).")
        return ("Gmail inbox changes via poll fallback "
                "(EVENTS_GMAIL_PUBSUB_TOPIC not configured, so we poll "
                f"users.history.list every {self.default_poll_interval_seconds}s "
                "instead of receiving Pub/Sub push). Filter with Gmail search "
                "syntax in the `query` field. Set the env var + restart for "
                "true real-time push.")

    def required_scopes(self, event_type: str, filter_dict: Dict[str, Any]) -> List[List[str]]:
        # Push (`users.watch` / `users.stop`) is gated by Gmail's modify or
        # metadata scope (or full mail.google.com). The read-only scope is
        # NOT enough — `watch` returns 403 with insufficient scopes.
        if self.supports_push:
            return [[
                "https://www.googleapis.com/auth/gmail.modify",
                "https://www.googleapis.com/auth/gmail.metadata",
                "https://mail.google.com/",
            ]]
        # Poll fallback uses `users.history.list` + `messages.get` (metadata
        # format) + `users.getProfile`. `gmail.readonly` is sufficient.
        return [[
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.modify",
            "https://www.googleapis.com/auth/gmail.metadata",
            "https://mail.google.com/",
        ]]

    def parser_schema(self) -> Dict[str, Any]:
        base = super().parser_schema()
        base["filter_fields"] = ["query", "label_ids"]
        base["examples"] = [
            'when an email arrives from any airline, summarize it  '
            '(filter: {"query": "from:airlines"})',
            'when an email arrives in INBOX with "invoice" in the subject  '
            '(filter: {"query": "in:inbox subject:invoice"})',
        ]
        return base

    # ── Subscribe / renew / unsubscribe ──────────────────────────────────

    async def register_subscription(
        self, *, owner_user_id: str, agent_id: str, event_type: str, filter_dict: Dict[str, Any]
    ) -> SubscriptionRegistration:
        topic = _topic_name()
        if topic:
            return await self._register_push(
                owner_user_id=owner_user_id, agent_id=agent_id,
                filter_dict=filter_dict, topic=topic,
            )
        return await self._register_poll(
            owner_user_id=owner_user_id, agent_id=agent_id,
            filter_dict=filter_dict,
        )

    async def _register_push(
        self, *, owner_user_id: str, agent_id: str, filter_dict: Dict[str, Any], topic: str,
    ) -> SubscriptionRegistration:
        labels = filter_dict.get("label_ids") or ["INBOX"]
        if isinstance(labels, str):
            labels = [s.strip() for s in labels.split(",") if s.strip()]

        body = {
            "topicName": topic,
            "labelIds": labels,
            "labelFilterAction": "include",
        }
        resp = await oauth_api_call(
            owner_user_id, agent_id, "google", "POST",
            f"{_GMAIL_BASE}/watch", json_body=body,
        )
        if resp.get("status") != "ok":
            raise RuntimeError(f"Gmail watch failed: {resp}")
        body_out = resp.get("body") or {}
        history_id = str(body_out.get("historyId") or "")
        expiration_iso = _expiration_iso_from_ms(str(body_out.get("expiration") or ""))

        from app.db import get_db
        connected_email = await _get_connected_email(get_db(), owner_user_id, agent_id)

        return SubscriptionRegistration(
            external_subscription_id=connected_email or owner_user_id,  # used as lookup key
            external_resource_id=None,
            external_expiration_at=expiration_iso,
            external_metadata={
                "history_id": history_id,
                "email": connected_email,
                "labels": labels,
                "topic": topic,
                "mode": "push",
            },
        )

    async def _register_poll(
        self, *, owner_user_id: str, agent_id: str, filter_dict: Dict[str, Any],
    ) -> SubscriptionRegistration:
        """Seed a poll-mode subscription with the current historyId so the
        first poll only sees messages that arrive *after* subscription time."""
        from app.db import get_db
        db = get_db()
        connected_email = await _get_connected_email(db, owner_user_id, agent_id)
        current_history_id = await self._fetch_current_history_id(owner_user_id, agent_id)

        labels = filter_dict.get("label_ids") or ["INBOX"]
        if isinstance(labels, str):
            labels = [s.strip() for s in labels.split(",") if s.strip()]

        return SubscriptionRegistration(
            external_subscription_id=connected_email or owner_user_id,
            external_resource_id=None,
            external_expiration_at=None,  # poll mode has no provider-side TTL
            external_metadata={
                "history_id": current_history_id,
                "email": connected_email,
                "labels": labels,
                "mode": "poll",
            },
            poll_cursor=current_history_id,
        )

    async def _fetch_current_history_id(self, user_id: str, agent_id: str) -> str:
        resp = await oauth_api_call(
            user_id, agent_id, "google", "GET",
            f"{_GMAIL_BASE}/profile",
        )
        if resp.get("status") != "ok":
            raise RuntimeError(f"Gmail profile fetch failed: {resp}")
        return str((resp.get("body") or {}).get("historyId") or "")

    async def unregister_subscription(self, subscription_row: Dict[str, Any]) -> None:
        # Only push-mode subs have a provider-side handle to stop.
        meta = subscription_row.get("external_metadata") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        if meta.get("mode") == "poll":
            return
        owner_user_id = subscription_row["owner_user_id"]
        agent_id = subscription_row.get("agent_id", "")
        try:
            await oauth_api_call(
                owner_user_id, agent_id, "google", "POST",
                f"{_GMAIL_BASE}/stop",
            )
        except Exception as e:
            logger.warning("Gmail stop failed for user %s: %s", owner_user_id[:12], e)

    # ── Poll fallback ────────────────────────────────────────────────────

    async def poll(self, subscription_row: Dict[str, Any]) -> Tuple[List[NormalizedEvent], Optional[str]]:
        user_id = subscription_row["owner_user_id"]
        agent_id = subscription_row.get("agent_id", "")
        meta = subscription_row.get("external_metadata") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        since = (subscription_row.get("poll_cursor")
                 or str(meta.get("history_id") or "")).strip()
        email = (meta.get("email") or "").lower()
        if not email:
            from app.db import get_db
            email = await _get_connected_email(get_db(), user_id, agent_id)

        if not since:
            # No cursor yet — seed and emit nothing this tick.
            current = await self._fetch_current_history_id(user_id, agent_id)
            return [], current or None

        new_msg_ids = await self._list_new_message_ids(user_id, agent_id, since)
        if not new_msg_ids:
            current = await self._fetch_current_history_id(user_id, agent_id)
            return [], current or since

        filter_dict = subscription_row.get("filter") or {}
        query = (filter_dict.get("query") or "").strip()

        events: List[NormalizedEvent] = []
        for msg_id in new_msg_ids:
            if query:
                matched = await self._message_matches_query(user_id, agent_id, msg_id, query)
                if not matched:
                    continue
            msg = await self._fetch_message_metadata(user_id, agent_id, msg_id)
            if msg is None:
                continue
            events.append(self._build_event(user_id, email, msg))

        new_cursor = await self._fetch_current_history_id(user_id, agent_id)
        return events, new_cursor or since

    # ── Pub/Sub intake ───────────────────────────────────────────────────

    async def verify_webhook(self, request: Request) -> bool:
        return await verify_pubsub_jwt(request)

    async def handle_webhook(self, request: Request) -> List[NormalizedEvent]:
        attrs, data = await decode_pubsub_envelope(request)
        try:
            payload = json.loads(data or b"{}")
        except Exception:
            payload = {}
        email = (payload.get("emailAddress") or "").lower()
        new_history_id = str(payload.get("historyId") or "")
        if not email or not new_history_id:
            return []

        from app.db import get_db
        db = get_db()

        # Find the user this email belongs to.
        user_id = await db.find_user_by_oauth_account("google", email)
        if not user_id:
            logger.warning("Gmail Pub/Sub: no user mapped to %s", email)
            return []

        # Find this user's Gmail event subscriptions to determine the lowest
        # historyId to reconcile from, and to apply filters.
        subs = await db.find_event_subscriptions_for_user_source(user_id, self.name)
        if not subs:
            return []

        # We reconcile from each sub's stored historyId. Group by historyId
        # to minimize history.list calls across overlapping subs.
        events: List[NormalizedEvent] = []
        per_sub_meta_updates: List[tuple] = []  # (sub_id, new_history_id)

        for sub in subs:
            agent_id = sub.get("agent_id", "")
            meta = sub.get("external_metadata") or {}
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except Exception:
                    meta = {}
            since = str(meta.get("history_id") or "")
            if not since:
                # First-time arrival: just record the current historyId; we
                # have nothing to compare from yet.
                meta["history_id"] = new_history_id
                per_sub_meta_updates.append((sub["id"], meta))
                continue

            new_msgs = await self._list_new_message_ids(user_id, agent_id, since)
            if not new_msgs:
                meta["history_id"] = new_history_id
                per_sub_meta_updates.append((sub["id"], meta))
                continue

            filter_dict = sub.get("filter") or {}
            query = (filter_dict.get("query") or "").strip()

            for msg_id in new_msgs:
                # Apply Gmail query at fetch time if specified.
                if query:
                    matched = await self._message_matches_query(user_id, agent_id, msg_id, query)
                    if not matched:
                        continue
                msg = await self._fetch_message_metadata(user_id, agent_id, msg_id)
                if msg is None:
                    continue
                evt = self._build_event(user_id, email, msg)
                events.append(evt)

            meta["history_id"] = new_history_id
            per_sub_meta_updates.append((sub["id"], meta))

        for sub_id, meta in per_sub_meta_updates:
            try:
                await db.update_event_subscription(sub_id, external_metadata=meta)
            except Exception as e:
                logger.debug("Could not persist new history_id for %s: %s", sub_id, e)

        return events

    # ── History list / message fetch ─────────────────────────────────────

    async def _list_new_message_ids(self, user_id: str, agent_id: str, since_history_id: str) -> List[str]:
        params = {
            "startHistoryId": since_history_id,
            "historyTypes": "messageAdded",
        }
        ids: List[str] = []
        page_token = None
        for _ in range(5):  # cap pagination
            if page_token:
                params["pageToken"] = page_token
            resp = await oauth_api_call(
                user_id, agent_id, "google", "GET",
                f"{_GMAIL_BASE}/history", params=params,
            )
            if resp.get("status") != "ok":
                logger.warning("Gmail history.list failed: %s", resp)
                break
            body = resp.get("body") or {}
            for h in body.get("history") or []:
                for ma in h.get("messagesAdded") or []:
                    m = ma.get("message") or {}
                    mid = m.get("id")
                    if mid:
                        ids.append(mid)
            page_token = body.get("nextPageToken")
            if not page_token:
                break
        # Dedup while preserving order
        seen, out = set(), []
        for m in ids:
            if m not in seen:
                seen.add(m); out.append(m)
        return out

    async def _message_matches_query(self, user_id: str, agent_id: str, message_id: str, query: str) -> bool:
        # Cheapest match check: use list with q + ids parameter limited to this id.
        params = {"q": f"{query} rfc822msgid:* ", "maxResults": 1}
        # Simpler: get the message with format=minimal and let query filter via list
        # — we use list with q AND filter by id manually.
        params = {"q": query, "maxResults": 50}
        resp = await oauth_api_call(
            user_id, agent_id, "google", "GET",
            f"{_GMAIL_BASE}/messages", params=params,
        )
        if resp.get("status") != "ok":
            return False
        body = resp.get("body") or {}
        for m in body.get("messages") or []:
            if m.get("id") == message_id:
                return True
        return False

    async def _fetch_message_metadata(self, user_id: str, agent_id: str, message_id: str) -> Optional[dict]:
        params = {
            "format": "metadata",
            "metadataHeaders": ["From", "To", "Subject", "Date", "Message-ID"],
        }
        resp = await oauth_api_call(
            user_id, agent_id, "google", "GET",
            f"{_GMAIL_BASE}/messages/{message_id}", params=params,
        )
        if resp.get("status") != "ok":
            return None
        return resp.get("body") or None

    def _build_event(self, user_id: str, email: str, msg: dict) -> NormalizedEvent:
        payload_meta = msg.get("payload") or {}
        headers = payload_meta.get("headers") or []
        snippet = msg.get("snippet") or ""
        date_hdr = _header(headers, "Date")
        try:
            from email.utils import parsedate_to_datetime
            occurred = parsedate_to_datetime(date_hdr).astimezone(timezone.utc).isoformat()
        except Exception:
            occurred = datetime.now(timezone.utc).isoformat()
        return NormalizedEvent(
            source=self.name,
            event_type="message_received",
            owner_user_id=user_id,
            external_id=str(msg.get("id") or ""),
            occurred_at=occurred,
            payload={
                "account_email": email,
                "from": _header(headers, "From"),
                "to": _header(headers, "To"),
                "subject": _header(headers, "Subject"),
                "snippet": snippet,
                "label_ids": msg.get("labelIds") or [],
                "thread_id": msg.get("threadId") or "",
                "internal_date": msg.get("internalDate") or "",
            },
            raw_ref={
                "message_id": msg.get("id"),
                "thread_id": msg.get("threadId"),
            },
        )

    # Gmail's filter is enforced at fetch time (above); per-event matches_filter
    # always returns True so the router doesn't re-evaluate the query.
    def matches_filter(self, event, filter_dict: Dict[str, Any]) -> bool:
        return True


FEATURE = {
    "id": "gmail_events",
    "display_name": "Gmail (events)",
    "category": "event_source",
    "status": "stable",
    "summary": "Fire the agent when matching email arrives (Gmail push via Pub/Sub).",
    "requires": ["Google OAuth credentials", "Cloud Pub/Sub setup (see docs/events-setup.md)"],
}

source_cls = GmailSource
