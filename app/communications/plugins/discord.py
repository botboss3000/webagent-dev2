"""
Discord communication plugin.

Supports two modes:
  - Interactions webhook (slash commands) — Discord sends POST to your server URL.
  - Direct message sending via Discord REST API (bot token required).

Credentials: bot_token, application_id, public_key (for webhook verification).
Env-var fallbacks: DISCORD_BOT_TOKEN, DISCORD_APPLICATION_ID, DISCORD_PUBLIC_KEY.

Plugin discovery: set `plugin_cls = DiscordPlugin` at module level.
"""

import hashlib
import hmac
import json
import logging
import os
from typing import Optional

import httpx
from fastapi import Request, Response

from app.communications.base import CommunicationPlugin

logger = logging.getLogger(__name__)

DISCORD_API = "https://discord.com/api/v10"


async def _send_dm(bot_token: str, user_id: str, content: str) -> dict:
    """Open a DM channel then send a message to a Discord user."""
    headers = {"Authorization": f"Bot {bot_token}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            # Step 1: create DM channel
            dm_resp = await client.post(
                f"{DISCORD_API}/users/@me/channels",
                headers=headers,
                json={"recipient_id": user_id},
            )
            dm_resp.raise_for_status()
            channel_id = dm_resp.json().get("id")
            if not channel_id:
                return {"error": "Could not create DM channel"}

            # Step 2: send message
            msg_resp = await client.post(
                f"{DISCORD_API}/channels/{channel_id}/messages",
                headers=headers,
                json={"content": content},
            )
            msg_resp.raise_for_status()
            return msg_resp.json()
    except Exception as e:
        logger.error("Discord DM send failed: %s", e)
        return {"error": str(e)}


def _verify_discord_signature(public_key: str, signature: str, timestamp: str, body: bytes) -> bool:
    """Verify Discord's Ed25519 signature. Requires nacl or fallback."""
    try:
        from nacl.signing import VerifyKey
        from nacl.exceptions import BadSignatureError
        vk = VerifyKey(bytes.fromhex(public_key))
        vk.verify(timestamp.encode() + body, bytes.fromhex(signature))
        return True
    except ImportError:
        # nacl not installed — skip verification (dev mode)
        logger.warning("PyNaCl not installed; skipping Discord signature verification")
        return True
    except Exception:
        return False


class DiscordPlugin(CommunicationPlugin):
    """Discord bot plugin — interactions webhook + DM sending."""

    def __init__(self, registry: dict):
        self._registry = registry

    def _cfg(self) -> dict:
        return self._registry.get("plugins", {}).get("discord", {})

    def _cred(self, key: str, env_var: str) -> str:
        return self._cfg().get(key) or os.environ.get(env_var, "")

    @property
    def name(self) -> str:
        return "discord"

    @property
    def enabled(self) -> bool:
        if not self._cfg().get("enabled", False):
            return False
        return bool(self._cred("bot_token", "DISCORD_BOT_TOKEN"))

    @property
    def user_id_prefix(self) -> str:
        return "discord:"

    @property
    def webhook_path(self) -> str:
        return "discord"

    def get_tools(self) -> list[dict]:
        return [
            {
                "name": "send_discord_message",
                "description": "Send a Discord DM to a user by their Discord user ID. Args: user_id (Discord snowflake ID), text (str).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "user_id": {"type": "string", "description": "Discord user snowflake ID"},
                        "text": {"type": "string", "description": "Message content"},
                    },
                    "required": ["user_id", "text"],
                },
            }
        ]

    async def verify_request(self, request: Request) -> bool:
        """Verify Discord's Ed25519 interaction signature."""
        body_bytes = await request.body()
        request.state._discord_body = body_bytes
        signature = request.headers.get("X-Signature-Ed25519", "")
        timestamp = request.headers.get("X-Signature-Timestamp", "")
        public_key = self._cred("public_key", "DISCORD_PUBLIC_KEY")
        if not public_key or not signature or not timestamp:
            # Dev mode: parse body and accept
            try:
                request.state._discord_json = json.loads(body_bytes)
            except Exception:
                request.state._discord_json = {}
            return True
        ok = _verify_discord_signature(public_key, signature, timestamp, body_bytes)
        if ok:
            try:
                request.state._discord_json = json.loads(body_bytes)
            except Exception:
                request.state._discord_json = {}
        return ok

    def extract_external_id(self, request: Request) -> str:
        body = getattr(request.state, "_discord_json", {})
        # Slash command interaction: member.user.id or user.id
        return (
            body.get("member", {}).get("user", {}).get("id")
            or body.get("user", {}).get("id")
            or ""
        )

    def extract_text(self, request: Request) -> str:
        body = getattr(request.state, "_discord_json", {})
        # Slash command: data.options[0].value or data.name
        data = body.get("data", {})
        options = data.get("options", [])
        if options:
            return str(options[0].get("value", ""))
        return data.get("name", "")

    async def handle_webhook(self, request: Request) -> Response:
        body = getattr(request.state, "_discord_json", {})
        interaction_type = body.get("type", 0)

        # Type 1 = PING (Discord handshake)
        if interaction_type == 1:
            return Response(
                content=json.dumps({"type": 1}),
                media_type="application/json",
                status_code=200,
            )

        # Type 2 = APPLICATION_COMMAND (slash command)
        # Acknowledge immediately; actual response sent via send_message
        return Response(
            content=json.dumps({"type": 5}),  # DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE
            media_type="application/json",
            status_code=200,
        )

    async def send_message(self, recipient_id: str, text: str) -> str:
        bot_token = self._cred("bot_token", "DISCORD_BOT_TOKEN")
        if not bot_token:
            raise RuntimeError("Discord bot token not configured")
        result = await _send_dm(bot_token, recipient_id, text)
        return str(result)


FEATURE = {
    "id": "discord",
    "display_name": "Discord",
    "category": "channel",
    "status": "beta",
    "summary": "Send and receive Discord messages.",
    "requires": ["A Discord bot token"],
}

plugin_cls = DiscordPlugin
