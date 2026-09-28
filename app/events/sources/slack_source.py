"""Slack as an event source (bridges the existing comms plugin)."""

from __future__ import annotations

from typing import Any, Dict, List

from app.events.base import EventSource
from app.events.types import SubscriptionRegistration


class SlackSource(EventSource):
    @property
    def name(self) -> str:
        return "slack"

    @property
    def event_types(self) -> List[str]:
        return ["message_received", "mention"]

    @property
    def supports_push(self) -> bool:
        return True

    @property
    def description(self) -> str:
        return ("Slack inbound messages and @-mentions, delivered via the "
                "existing Slack Events API webhook.")

    def parser_schema(self) -> Dict[str, Any]:
        base = super().parser_schema()
        base["filter_fields"] = ["channel_id", "user_id", "contains_text"]
        base["examples"] = [
            'when someone @-mentions me in Slack, draft a reply',
            'when a Slack message in channel C0123 contains "deploy", alert me',
        ]
        return base

    async def register_subscription(
        self, *, owner_user_id: str, event_type: str, filter_dict: Dict[str, Any]
    ) -> SubscriptionRegistration:
        return SubscriptionRegistration()

    def matches_filter(self, event, filter_dict: Dict[str, Any]) -> bool:
        if not filter_dict:
            return True
        for k in ("channel_id", "user_id"):
            v = filter_dict.get(k)
            if v and str(event.payload.get(k) or "") != str(v):
                return False
        contains = filter_dict.get("contains_text")
        if contains:
            text = (event.payload.get("text") or "").lower()
            if contains.lower() not in text:
                return False
        return True


FEATURE = {
    "id": "slack_events",
    "display_name": "Slack (events)",
    "category": "event_source",
    "status": "beta",
    "summary": "Fire the agent on inbound Slack messages / mentions.",
    "requires": ["Slack app credentials"],
}

source_cls = SlackSource
