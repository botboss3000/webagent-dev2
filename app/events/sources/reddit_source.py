"""Reddit poll-based event source for inbox / mentions."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.events.base import EventSource
from app.events.types import NormalizedEvent, SubscriptionRegistration
from app.integrations.oauth_helper import oauth_api_call


class RedditSource(EventSource):
    @property
    def name(self) -> str:
        return "reddit"

    @property
    def event_types(self) -> List[str]:
        return ["inbox_message", "mention"]

    @property
    def supports_push(self) -> bool:
        return False

    @property
    def required_provider(self) -> Optional[str]:
        return "reddit"

    @property
    def default_poll_interval_seconds(self) -> int:
        return 120

    def parser_schema(self) -> Dict[str, Any]:
        base = super().parser_schema()
        base["filter_fields"] = ["subreddit", "contains_text"]
        return base

    async def register_subscription(
        self, *, owner_user_id: str, agent_id: str, event_type: str, filter_dict: Dict[str, Any]
    ) -> SubscriptionRegistration:
        return SubscriptionRegistration(poll_cursor=None)

    async def poll(
        self, subscription_row: Dict[str, Any],
    ) -> Tuple[List[NormalizedEvent], Optional[str]]:
        event_type = subscription_row.get("event_type") or "inbox_message"
        path = "/message/inbox" if event_type == "inbox_message" else "/message/mentions"
        cursor = subscription_row.get("poll_cursor") or ""
        params: Dict[str, Any] = {"limit": 25}
        if cursor:
            params["before"] = cursor
        resp = await oauth_api_call(
            subscription_row["owner_user_id"], subscription_row.get("agent_id", ""), "reddit", "GET",
            f"https://oauth.reddit.com{path}",
            params=params,
            headers={"User-Agent": "webagent-events/1.0"},
        )
        if resp.get("status") != "ok":
            return [], cursor
        body = resp.get("body") or {}
        items = ((body.get("data") or {}).get("children")) or []
        out: List[NormalizedEvent] = []
        newest = cursor
        for it in items:
            d = (it.get("data") or {})
            full_id = d.get("name") or ""
            if not newest:
                newest = full_id
            out.append(NormalizedEvent(
                source=self.name,
                event_type=event_type,
                owner_user_id=subscription_row["owner_user_id"],
                external_id=full_id,
                occurred_at=datetime.fromtimestamp(int(d.get("created_utc") or 0), timezone.utc).isoformat(),
                payload={
                    "subject": d.get("subject") or "",
                    "body": (d.get("body") or "")[:1500],
                    "author": d.get("author") or "",
                    "subreddit": d.get("subreddit") or "",
                    "permalink": d.get("permalink") or "",
                },
                raw_ref={"id": full_id},
            ))
        return out, newest

    def matches_filter(self, event, filter_dict: Dict[str, Any]) -> bool:
        if not filter_dict:
            return True
        s = (filter_dict.get("subreddit") or "").lower()
        if s and s != (event.payload.get("subreddit") or "").lower():
            return False
        c = (filter_dict.get("contains_text") or "").lower()
        if c and c not in (event.payload.get("body") or "").lower():
            return False
        return True


source_cls = RedditSource
