"""Twilio SMS as an event source (bridges the existing comms plugin)."""

from __future__ import annotations

from typing import Any, Dict, List

from app.events.base import EventSource
from app.events.types import SubscriptionRegistration


class TwilioSmsSource(EventSource):
    @property
    def name(self) -> str:
        return "sms"

    @property
    def event_types(self) -> List[str]:
        return ["message_received"]

    @property
    def supports_push(self) -> bool:
        return True

    @property
    def description(self) -> str:
        return ("Inbound SMS via the Twilio comms webhook.")

    def parser_schema(self) -> Dict[str, Any]:
        base = super().parser_schema()
        base["filter_fields"] = ["from_number", "contains_text"]
        return base

    async def register_subscription(
        self, *, owner_user_id: str, event_type: str, filter_dict: Dict[str, Any]
    ) -> SubscriptionRegistration:
        return SubscriptionRegistration()

    def matches_filter(self, event, filter_dict: Dict[str, Any]) -> bool:
        if not filter_dict:
            return True
        f = filter_dict.get("from_number")
        if f and (event.payload.get("from_number") or "") != f:
            return False
        contains = filter_dict.get("contains_text")
        if contains and contains.lower() not in (event.payload.get("text") or "").lower():
            return False
        return True


source_cls = TwilioSmsSource
