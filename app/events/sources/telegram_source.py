"""Telegram as an event source.

The Telegram comms plugin already owns the inbound webhook (``/api/v1/webhooks/telegram``)
and the polling fallback. This source plugin's job is just to expose Telegram
as a subscribable event type so the automation parser can route lines like
*"when someone messages me on Telegram, summarize and forward to Slack"*
through the same machinery as Gmail/Calendar/etc.

Events are *emitted* (i.e. handed to the router) by
``app.communications.processor.process_channel_message`` after each inbound
message — see the ``emit_channel_event`` helper there.
"""

from __future__ import annotations

from typing import Any, Dict, List

from app.events.base import EventSource
from app.events.types import SubscriptionRegistration


class TelegramSource(EventSource):
    @property
    def name(self) -> str:
        return "telegram"

    @property
    def event_types(self) -> List[str]:
        return ["message_received"]

    @property
    def supports_push(self) -> bool:
        return True

    @property
    def description(self) -> str:
        return ("Telegram inbound messages. Filter by sender chat_id or "
                "substring in the message text.")

    def parser_schema(self) -> Dict[str, Any]:
        base = super().parser_schema()
        base["filter_fields"] = ["chat_id", "from_username", "contains_text"]
        base["examples"] = [
            'when a Telegram message arrives from chat 12345, summarize it',
            'when any Telegram message mentions "urgent", message me on SMS',
        ]
        return base

    async def register_subscription(
        self, *, owner_user_id: str, event_type: str, filter_dict: Dict[str, Any]
    ) -> SubscriptionRegistration:
        # Provider side is already wired by the Telegram comms plugin.
        # Nothing to register per-user.
        return SubscriptionRegistration()

    def matches_filter(self, event, filter_dict: Dict[str, Any]) -> bool:
        if not filter_dict:
            return True
        chat_id = filter_dict.get("chat_id")
        if chat_id and str(event.payload.get("chat_id") or "") != str(chat_id):
            return False
        from_user = filter_dict.get("from_username")
        if from_user and (event.payload.get("from_username") or "") != from_user:
            return False
        contains = filter_dict.get("contains_text")
        if contains:
            text = (event.payload.get("text") or "").lower()
            if contains.lower() not in text:
                return False
        return True


FEATURE = {
    "id": "telegram_events",
    "display_name": "Telegram (events)",
    "category": "event_source",
    "status": "beta",
    "summary": "Fire the agent on inbound Telegram messages.",
    "requires": ["A Telegram bot token"],
}

source_cls = TelegramSource
