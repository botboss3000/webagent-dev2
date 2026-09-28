"""
Telegram communication plugin.

Uses python-telegram-bot for both sending messages and webhook reception.
Bot token from TELEGRAM_BOT_TOKEN env var.

Plugin discovery: set `plugin_cls = TelegramPlugin` at module level.
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import Request, Response

from app.communications.base import CommunicationPlugin

logger = logging.getLogger(__name__)

BOT_TOKEN_ENV = "TELEGRAM_BOT_TOKEN"

# ── Telegram API helpers (lightweight, no full SDK needed for basic ops) ──

TELEGRAM_API = "https://api.telegram.org/bot"


async def _send_message(bot_token: str, chat_id: int | str, text: str) -> dict:
    """Send a text message via Telegram Bot API."""
    url = f"{TELEGRAM_API}{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.error("Telegram send_message failed: %s", e)
        return {"ok": False, "error": str(e)}


async def set_webhook(bot_token: str, url: str) -> bool:
    """Register the webhook URL with Telegram."""
    api_url = f"{TELEGRAM_API}{bot_token}/setWebhook"
    payload = {"url": url, "allowed_updates": ["message"]}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(api_url, json=payload)
            data = resp.json()
            if data.get("ok"):
                logger.info("Telegram webhook set to %s", url)
                return True
            else:
                logger.error("Telegram setWebhook failed: %s", data)
                return False
    except Exception as e:
        logger.error("Telegram setWebhook error: %s", e)
        return False


async def delete_webhook(bot_token: str) -> bool:
    """Remove the webhook."""
    api_url = f"{TELEGRAM_API}{bot_token}/deleteWebhook"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(api_url)
            return resp.json().get("ok", False)
    except Exception as e:
        logger.error("Telegram deleteWebhook error: %s", e)
        return False


async def get_webhook_info(bot_token: str) -> dict:
    """Return current webhook info from Telegram (url, pending count, last error)."""
    api_url = f"{TELEGRAM_API}{bot_token}/getWebhookInfo"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(api_url)
            data = resp.json()
            if data.get("ok"):
                return data.get("result", {})
            return {}
    except Exception as e:
        logger.error("Telegram getWebhookInfo error: %s", e)
        return {}


async def get_me(bot_token: str) -> dict:
    """Return bot identity info (id, first_name, username) via getMe."""
    api_url = f"{TELEGRAM_API}{bot_token}/getMe"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(api_url)
            return resp.json()
    except Exception as e:
        logger.error("Telegram getMe error: %s", e)
        return {"ok": False, "error": str(e)}


# ── Plugin class ────────────────────────────────────────────────────────────


class TelegramPlugin(CommunicationPlugin):
    """Telegram bot plugin. Uses Bot API directly via HTTP (no full SDK).
    Supports both webhook mode (production) and long-polling mode (local dev)."""

    def __init__(self, registry: dict):
        self._registry = registry
        self._bot_token: Optional[str] = None
        self._agent_id: Optional[str] = None  # set by manager for per-agent pollers
        self._polling_task: Optional[asyncio.Task] = None
        self._last_update_id: int = 0
        self.conflict_detected: bool = False
        self.last_message_at: Optional[str] = None

    @property
    def is_polling(self) -> bool:
        """Whether the polling loop is currently running."""
        return self._polling_task is not None and not self._polling_task.done()

    @property
    def is_offline(self) -> bool:
        """No webhook base URL set — must use polling."""
        base_url = self._registry.get("webhook_base_url", "")
        return not base_url or "localhost" in base_url or "127.0.0.1" in base_url or "0.0.0.0" in base_url

    @property
    def name(self) -> str:
        return "telegram"

    @property
    def enabled(self) -> bool:
        plugin_config = self._registry.get("plugins", {}).get("telegram", {})
        if not plugin_config.get("enabled", False):
            return False
        # Check registry token first, then env var (so UI-set token takes priority)
        token = plugin_config.get("bot_token", "")
        if not token:
            token = os.environ.get(BOT_TOKEN_ENV, "")
        if not token:
            logger.warning("Telegram plugin enabled but no token set")
            return False
        self._bot_token = token
        return True

    @property
    def user_id_prefix(self) -> str:
        return "telegram:"

    @property
    def webhook_path(self) -> str:
        return "telegram"

    def get_tools(self) -> list[dict]:
        return [
            {
                "name": "send_telegram_message",
                "description": "Send a Telegram message to a chat. Use ONLY for registered contacts. Args: chat_id (int or str), text (str).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "chat_id": {
                            "type": ["integer", "string"],
                            "description": "Telegram chat ID (numeric, can be negative for groups)",
                        },
                        "text": {
                            "type": "string",
                            "description": "Message text (supports HTML formatting)",
                        },
                    },
                    "required": ["chat_id", "text"],
                },
            }
        ]

    def extract_external_id(self, request: Request) -> str:
        """Extract Telegram chat_id from the webhook payload."""
        # Telegram sends update as JSON body
        body = getattr(request.state, "_telegram_body", {})
        if not body:
            return ""
        message = body.get("message", {})
        chat = message.get("chat", {})
        return str(chat.get("id", ""))

    def extract_text(self, request: Request) -> str:
        """Extract message text from Telegram webhook payload."""
        body = getattr(request.state, "_telegram_body", {})
        if not body:
            return ""
        message = body.get("message", {})
        return message.get("text", "")

    async def verify_request(self, request: Request) -> bool:
        """
        Read and cache the JSON body. Telegram webhooks don't have a signature
        (unlike Twilio). For production, use a secret path or IP whitelist.
        """
        try:
            body = await request.json()
            request.state._telegram_body = body
            return True
        except Exception:
            return False

    async def handle_webhook(self, request: Request) -> Response:
        """
        Handle an incoming Telegram update.
        This is the main entry point — called from webhooks.py.
        It extracts the message, checks auth, and routes to agent loop.

        The actual agent routing happens in webhooks.py.
        This method just validates the request and returns parsed data
        for the webhook router to process.
        """
        body = getattr(request.state, "_telegram_body", {})
        if not body:
            return Response(content='{"error":"no body"}', status_code=400)

        message = body.get("message", {})
        if not message:
            # Non-message update (e.g. callback_query) — ignore
            return Response(content='{"ok":true}', status_code=200)

        return Response(content='{"ok":true}', status_code=200)

    async def send_message(self, recipient_id: str, text: str) -> str:
        """Send a message to a Telegram chat."""
        token = await self._resolve_token()
        if not token:
            raise RuntimeError("Telegram bot token not configured")
        result = await _send_message(token, recipient_id, text)
        return json.dumps(result)

    async def _resolve_token(self) -> str:
        """Resolve the bot token from registry or env var."""
        if self._bot_token:
            return self._bot_token
        plugin_config = self._registry.get("plugins", {}).get("telegram", {})
        token = plugin_config.get("bot_token", "") or os.environ.get(BOT_TOKEN_ENV, "")
        if token:
            self._bot_token = token
        return token

    async def start_polling(self, initial_update_id: int = 0) -> None:
        """Start the background long-polling loop.

        Args:
            initial_update_id: Resume from this offset instead of 0, so already-processed
                messages are not re-delivered when the poller is restarted.
        """
        if self.is_polling:
            logger.info("Telegram polling already running")
            return
        # Delete any existing webhook first (polling conflicts with webhook)
        token = await self._resolve_token()
        if token:
            try:
                await delete_webhook(token)
                logger.info("Deleted existing webhook before starting polling")
            except Exception:
                pass
        self._last_update_id = initial_update_id
        self._polling_task = asyncio.create_task(self._polling_loop())
        logger.info("Telegram polling started (offset=%d)", initial_update_id)

    async def stop_polling(self) -> None:
        """Stop the background polling loop."""
        if self._polling_task and not self._polling_task.done():
            self._polling_task.cancel()
            try:
                await self._polling_task
            except asyncio.CancelledError:
                pass
            self._polling_task = None
            logger.info("Telegram polling stopped")

    async def _polling_loop(self) -> None:
        """Continuously poll Telegram for new messages via getUpdates."""
        while True:
            try:
                token = await self._resolve_token()
                if not token:
                    await asyncio.sleep(5)
                    continue

                url = f"{TELEGRAM_API}{token}/getUpdates"
                params = {"offset": self._last_update_id + 1, "timeout": 10}
                async with httpx.AsyncClient(timeout=15) as client:
                    resp = await client.get(url, params=params)
                    data = resp.json()

                if resp.status_code == 409:
                    self.conflict_detected = True
                    logger.warning(
                        "Telegram polling 409 conflict — another instance may be connected to this bot"
                    )
                    await asyncio.sleep(10)
                    continue

                if data.get("ok"):
                    self.conflict_detected = False  # clear on successful poll
                    updates = data.get("result", [])
                    for update in updates:
                        update_id = update.get("update_id", 0)
                        message = update.get("message", {})
                        if not message:
                            self._last_update_id = update_id
                            continue

                        chat_id = str(message.get("chat", {}).get("id", ""))
                        text = message.get("text", "")
                        if chat_id and text:
                            self.last_message_at = datetime.now(timezone.utc).isoformat()
                            try:
                                from app.communications.processor import process_channel_message
                                await process_channel_message(
                                    self.name, chat_id, text, self,
                                    agent_id=self._agent_id,
                                )
                            except Exception as e:
                                logger.error("Polling message processing error: %s", e)

                        self._last_update_id = update_id

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Polling loop error: %s", e)
                await asyncio.sleep(5)

            await asyncio.sleep(2)

    async def set_webhook_url(self, base_url: str) -> bool:
        """Register the webhook with Telegram. Call after server starts."""
        # Stop polling if running
        await self.stop_polling()
        token = await self._resolve_token()
        if not token:
            return False
        webhook_url = f"{base_url.rstrip('/')}/api/v1/webhooks/telegram"
        return await set_webhook(token, webhook_url)

    async def delete_webhook_url(self) -> bool:
        """Remove the webhook registration."""
        token = await self._resolve_token()
        if not token:
            return False
        return await delete_webhook(token)

    async def get_webhook_info(self) -> dict:
        """Return Telegram's current webhook registration info for this bot."""
        token = await self._resolve_token()
        if not token:
            return {}
        return await get_webhook_info(token)

    async def get_me(self) -> dict:
        """Return bot identity via getMe — confirms token is valid."""
        token = await self._resolve_token()
        if not token:
            return {"ok": False, "error": "No token configured"}
        return await get_me(token)

# -- Plugin discovery hook --
FEATURE = {
    "id": "telegram",
    "display_name": "Telegram",
    "category": "channel",
    "status": "stable",
    "summary": "Send/receive Telegram messages; webhook or polling, per-agent bot tokens.",
    "requires": ["A Telegram bot token"],
}

plugin_cls = TelegramPlugin
