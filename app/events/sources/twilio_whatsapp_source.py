"""Twilio WhatsApp as an event source (bridges the existing comms plugin)."""

from __future__ import annotations

from typing import Any, Dict, List

from app.events.base import EventSource
from app.events.types import SubscriptionRegistration


class TwilioWhatsAppSource(EventSource):
    @property
    def name(self) -> str:
        return "whatsapp"

    @property
    def event_types(self) -> List[str]:
        return ["message_received"]

    @property
    def supports_push(self) -> bool:
        return True

    def parser_schema(self) -> Dict[str, Any]:
        base = super().parser_schema()
        base["filter_fields"] = ["from_number", "contains_text"]
        return base

    async def register_subscription(
        self, *, owner_user_id: str, event_type: str, filter_dict: Dict[str, Any]
    ) -> SubscriptionRegistration:
        return SubscriptionRegistration()


source_cls = TwilioWhatsAppSource
