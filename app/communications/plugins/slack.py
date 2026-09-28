"""
Slack communication plugin.

Receives events from Slack Events API (message.im for DMs, slash commands).
Sends messages via Slack Web API (chat.postMessage).
Credentials: bot_token, signing_secret.
Env-var fallbacks: SLACK_BOT_TOKEN, SLACK_SIGNING_SECRET.

Plugin discovery: set `plugin_cls = SlackPlugin` at module level.
"""

import hashlib
import hmac
import json
import logging
import os
import time

import httpx
from fastapi import Request, Response

from app.communications.base import CommunicationPlugin

logger = logging.getLogger(__name__)

SLACK_API = "https://slack.com/api"


async def _post_message(bot_token: str, channel: str, text: str) -> dict:
    """Send a message via Slack Web API."""
    headers = {"Authorization": f"Bearer {bot_token}", "Content-Type": "application/json"}
    payload = {"channel": channel, "text": text}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(f"{SLACK_API}/chat.postMessage", headers=headers, json=payload)
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.error("Slack post_message failed: %s", e)
        return {"ok": False, "error": str(e)}


def _verify_slack_signature(signing_secret: str, signature: str, timestamp: str, body: bytes) -> bool:
    """Verify Slack's v0 request signature."""
    try:
        # Reject stale timestamps (> 5 minutes old)
        if abs(time.time() - int(timestamp)) > 300:
            return False
        base = f"v0:{timestamp}:".encode() + body
        expected = "v0=" + hmac.HMAC(
            signing_secret.encode("utf-8"), base, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, signature)
    except Exception:
        return False


class SlackPlugin(CommunicationPlugin):
    """Slack bot plugin — Events API receiver + chat.postMessage sender."""

    def __init__(self, registry: dict):
        self._registry = registry

    def _cfg(self) -> dict:
        return self._registry.get("plugins", {}).get("slack", {})

    def _cred(self, key: str, env_var: str) -> str:
        return self._cfg().get(key) or os.environ.get(env_var, "")

    @property
    def name(self) -> str:
        return "slack"

    @property
    def enabled(self) -> bool:
        if not self._cfg().get("enabled", False):
            return False
        return bool(self._cred("bot_token", "SLACK_BOT_TOKEN"))

    @property
    def user_id_prefix(self) -> str:
        return "slack:"

    @property
    def webhook_path(self) -> str:
        return "slack"

    def get_tools(self) -> list[dict]:
        return [
            {
                "name": "send_slack_message",
                "description": "Send a Slack message to a channel or user DM. Args: channel (channel ID or user ID for DMs, e.g. C01234 or U01234), text (str).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "channel": {"type": "string", "description": "Slack channel ID (C…) or user ID (U…) for DMs"},
                        "text": {"type": "string", "description": "Message text"},
                    },
                    "required": ["channel", "text"],
                },
            }
        ]

    async def verify_request(self, request: Request) -> bool:
        """Verify Slack signing secret. Falls back to accepting in dev."""
        body_bytes = await request.body()
        request.state._slack_body = body_bytes

        # Parse JSON or form body
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            try:
                request.state._slack_json = json.loads(body_bytes)
            except Exception:
                request.state._slack_json = {}
        else:
            import urllib.parse
            params = dict(urllib.parse.parse_qsl(body_bytes.decode("utf-8")))
            request.state._slack_json = params

        signing_secret = self._cred("signing_secret", "SLACK_SIGNING_SECRET")
        if not signing_secret:
            return True  # Dev mode — no verification

        signature = request.headers.get("X-Slack-Signature", "")
        timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
        return _verify_slack_signature(signing_secret, signature, timestamp, body_bytes)

    def extract_external_id(self, request: Request) -> str:
        body = getattr(request.state, "_slack_json", {})
        # Events API: body.event.user
        event = body.get("event", {})
        return event.get("user", "") or body.get("user_id", "")

    def extract_text(self, request: Request) -> str:
        body = getattr(request.state, "_slack_json", {})
        # Events API message: body.event.text
        # Slash commands: body.text
        event = body.get("event", {})
        return event.get("text", "") or body.get("text", "")

    async def handle_webhook(self, request: Request) -> Response:
        body = getattr(request.state, "_slack_json", {})

        # URL verification challenge (Slack Events API setup)
        if body.get("type") == "url_verification":
            return Response(
                content=json.dumps({"challenge": body.get("challenge", "")}),
                media_type="application/json",
                status_code=200,
            )

        # Acknowledge event immediately (Slack requires < 3s response)
        return Response(content="", status_code=200)

    async def send_message(self, recipient_id: str, text: str) -> str:
        bot_token = self._cred("bot_token", "SLACK_BOT_TOKEN")
        if not bot_token:
            raise RuntimeError("Slack bot token not configured")
        result = await _post_message(bot_token, recipient_id, text)
        return str(result)


FEATURE = {
    "id": "slack",
    "display_name": "Slack",
    "category": "channel",
    "status": "beta",
    "summary": "Send and receive Slack messages.",
    "requires": ["A Slack bot token / app credentials"],
}

plugin_cls = SlackPlugin
