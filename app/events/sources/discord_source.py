"""Discord as an event source (bridges the existing comms plugin)."""

from __future__ import annotations

from typing import Any, Dict, List

from app.events.base import EventSource
from app.events.types import SubscriptionRegistration


class DiscordSource(EventSource):
    @property
    def name(self) -> str:
        return "discord"

    @property
    def event_types(self) -> List[str]:
        return ["message_received", "command_received"]

    @property
    def supports_push(self) -> bool:
        return True

    @property
    def description(self) -> str:
        return ("Discord slash commands and (when gateway is enabled) inbound messages.")

    def parser_schema(self) -> Dict[str, Any]:
        base = super().parser_schema()
        base["filter_fields"] = ["channel_id", "guild_id", "user_id", "contains_text"]
        return base

    async def register_subscription(
        self, *, owner_user_id: str, event_type: str, filter_dict: Dict[str, Any]
    ) -> SubscriptionRegistration:
        return SubscriptionRegistration()


source_cls = DiscordSource
