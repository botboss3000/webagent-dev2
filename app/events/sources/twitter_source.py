"""Twitter / X poll-based event source.

X's streaming endpoints require paid tier. For the free tier we poll
``users/me/mentions`` on a cadence and emit a ``mention`` event for any
tweet whose ``id`` is greater than the saved cursor (Twitter ids are
ascending). One subscription = one cursor.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.events.base import EventSource
from app.events.types import NormalizedEvent, SubscriptionRegistration
from app.integrations.oauth_helper import oauth_api_call

logger = logging.getLogger(__name__)


class TwitterSource(EventSource):
    @property
    def name(self) -> str:
        return "twitter"

    @property
    def event_types(self) -> List[str]:
        return ["mention"]

    @property
    def supports_push(self) -> bool:
        return False

    @property
    def required_provider(self) -> Optional[str]:
        return "twitter"

    @property
    def default_poll_interval_seconds(self) -> int:
        return 180  # be polite — X rate limits are tight

    @property
    def description(self) -> str:
        return ("Poll for @-mentions of the authenticated X account.")

    def parser_schema(self) -> Dict[str, Any]:
        base = super().parser_schema()
        base["filter_fields"] = ["contains_text"]
        return base

    async def register_subscription(
        self, *, owner_user_id: str, agent_id: str, event_type: str, filter_dict: Dict[str, Any]
    ) -> SubscriptionRegistration:
        # Get the user's X user_id so we can call /2/users/{id}/mentions later.
        resp = await oauth_api_call(
            owner_user_id, agent_id, "twitter", "GET",
            "https://api.twitter.com/2/users/me",
        )
        x_user_id = ""
        if resp.get("status") == "ok":
            x_user_id = (((resp.get("body") or {}).get("data")) or {}).get("id") or ""
        return SubscriptionRegistration(
            external_metadata={"x_user_id": x_user_id},
            poll_cursor=None,
        )

    async def poll(
        self, subscription_row: Dict[str, Any],
    ) -> Tuple[List[NormalizedEvent], Optional[str]]:
        meta = subscription_row.get("external_metadata") or {}
        x_user_id = meta.get("x_user_id")
        if not x_user_id:
            return [], subscription_row.get("poll_cursor")
        cursor = subscription_row.get("poll_cursor") or ""
        params: Dict[str, Any] = {
            "max_results": 20,
            "tweet.fields": "created_at,author_id,text,referenced_tweets",
        }
        if cursor:
            params["since_id"] = cursor
        url = f"https://api.twitter.com/2/users/{x_user_id}/mentions"
        resp = await oauth_api_call(subscription_row["owner_user_id"], subscription_row.get("agent_id", ""), "twitter", "GET", url, params=params)
        if resp.get("status") != "ok":
            return [], cursor
        body = resp.get("body") or {}
        items = body.get("data") or []
        meta_info = body.get("meta") or {}
        newest_id = meta_info.get("newest_id") or cursor

        out: List[NormalizedEvent] = []
        for t in items:
            payload = {
                "text": t.get("text") or "",
                "author_id": t.get("author_id") or "",
                "id": t.get("id") or "",
                "created_at": t.get("created_at") or "",
            }
            out.append(NormalizedEvent(
                source=self.name,
                event_type="mention",
                owner_user_id=subscription_row["owner_user_id"],
                external_id=t.get("id") or "",
                occurred_at=t.get("created_at") or datetime.now(timezone.utc).isoformat(),
                payload=payload,
                raw_ref={"tweet_id": t.get("id")},
            ))
        return out, newest_id

    def matches_filter(self, event, filter_dict: Dict[str, Any]) -> bool:
        if not filter_dict:
            return True
        c = (filter_dict.get("contains_text") or "").lower()
        if c and c not in (event.payload.get("text") or "").lower():
            return False
        return True


source_cls = TwitterSource
