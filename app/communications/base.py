"""
Abstract base class for all communication plugins.

Each plugin = one channel (Telegram, WhatsApp, SMS, etc.).
Plugins are self-contained. Add/remove by dropping a file + toggling in registry.
"""

from abc import ABC, abstractmethod
from typing import Optional
from fastapi import Request, Response


class CommunicationPlugin(ABC):
    """One communication channel. Discovered by PluginManager."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique plugin identifier, e.g. 'telegram', 'whatsapp'."""
        ...

    @property
    @abstractmethod
    def enabled(self) -> bool:
        """Whether this plugin is active (from registry config)."""
        ...

    @abstractmethod
    def get_tools(self) -> list[dict]:
        """
        Return tool definitions for the LLM agent.
        Each tool dict follows the OpenAI tool schema format:
          {"name": str, "description": str, "parameters": {...}}
        """
        ...

    @abstractmethod
    async def handle_webhook(self, request: Request) -> Response:
        """
        Handle an incoming webhook from this channel.
        This is the inbound entry point for messages.

        Should:
          1. Verify the request signature (if applicable)
          2. Extract the sender's external ID
          3. Route to auth or agent loop
          4. Send the response back via the channel
        """
        ...

    @abstractmethod
    async def send_message(self, recipient_id: str, text: str) -> str:
        """
        Send a text message to a recipient on this channel.
        Used by agent tools and auth code sending.

        Args:
            recipient_id: External ID on this channel (phone, chat_id, etc.)
            text: Message body

        Returns:
            Channel-specific message ID or status string
        """
        ...

    @abstractmethod
    def extract_external_id(self, request: Request) -> str:
        """
        Pull the sender's channel-specific ID from a webhook payload.

        E.g. Telegram chat_id, WhatsApp phone number, SMS number.
        """
        ...

    @abstractmethod
    def extract_text(self, request: Request) -> str:
        """
        Pull the message text from a webhook payload.
        """
        ...

    @property
    @abstractmethod
    def user_id_prefix(self) -> str:
        """
        Prefix for internal user IDs, e.g. 'telegram:', 'whatsapp:', 'sms:'.
        This ensures unique user IDs across channels.
        """
        ...

    @property
    @abstractmethod
    def webhook_path(self) -> str:
        """
        URL path suffix for this plugin's webhook, e.g. 'telegram'.
        The full URL is /api/v1/webhooks/{webhook_path}.
        """
        ...

    async def verify_request(self, request: Request) -> bool:
        """
        Verify the authenticity of an incoming webhook request.
        Override for channel-specific signature verification.
        Default: accept all (override for production).
        """
        return True
