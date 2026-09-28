"""
Shared base for the Twilio communication plugins (SMS + WhatsApp).

Both channels speak the exact same Twilio REST API and differ only in a handful
of constants plus a WhatsApp `whatsapp:` number-prefix toggle. This module holds
the common implementation; `twilio_sms.py` and `twilio_whatsapp.py` each stay a
thin, separately-discoverable plugin (their own `FEATURE` header + `plugin_cls`)
that sets the channel constants below.

Leading-underscore filename → skipped by the plugin discovery scan
(`manager._discover_plugins`), so this base is never loaded as a plugin itself.
"""

import hashlib
import hmac
import logging
import os
import urllib.parse

import httpx
from fastapi import Request, Response

from app.communications.base import CommunicationPlugin

logger = logging.getLogger(__name__)

TWILIO_API = "https://api.twilio.com/2010-04-01"


async def _twilio_send(account_sid: str, auth_token: str, from_num: str,
                       to_num: str, body: str, *, whatsapp: bool = False) -> dict:
    """Send a message via the Twilio REST API. For WhatsApp, both the sender and
    recipient numbers get a `whatsapp:` prefix (added only if not already set)."""
    url = f"{TWILIO_API}/Accounts/{account_sid}/Messages.json"
    if whatsapp:
        if not from_num.startswith("whatsapp:"):
            from_num = f"whatsapp:{from_num}"
        if not to_num.startswith("whatsapp:"):
            to_num = f"whatsapp:{to_num}"
    payload = {"From": from_num, "To": to_num, "Body": body}
    try:
        async with httpx.AsyncClient(timeout=15, auth=(account_sid, auth_token)) as client:
            resp = await client.post(url, data=payload)
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.error("Twilio %s send failed: %s", "WhatsApp" if whatsapp else "SMS", e)
        return {"error": str(e)}


def _verify_twilio_signature(auth_token: str, signature: str, url: str, params: dict) -> bool:
    """Verify Twilio's X-Twilio-Signature HMAC-SHA1."""
    try:
        sorted_params = "".join(f"{k}{v}" for k, v in sorted(params.items()))
        expected = hmac.HMAC(
            auth_token.encode("utf-8"),
            (url + sorted_params).encode("utf-8"),
            hashlib.sha1,
        ).digest()
        import base64
        return hmac.compare_digest(
            base64.b64encode(expected).decode(),
            signature,
        )
    except Exception:
        return False


class TwilioBasePlugin(CommunicationPlugin):
    """Common Twilio channel implementation. Subclasses set the constants below."""

    # ── Channel constants (overridden per subclass) ──────────────────────────
    _PLUGIN_KEY: str = ""        # registry.json plugins.<key> + plugin name + webhook path
    _USER_ID_PREFIX: str = ""    # e.g. "sms:" / "whatsapp:"
    _FROM_ENV: str = ""          # env-var fallback for the sender number
    _WHATSAPP: bool = False      # prepend/strip the "whatsapp:" number prefix
    _VERIFY_SIGNATURE: bool = False  # check X-Twilio-Signature on inbound webhooks
    _DISPLAY: str = "Twilio"     # used in the "credentials not configured" error
    # Tool metadata
    _TOOL_NAME: str = ""
    _TOOL_DESC: str = ""
    _TOOL_TO_DESC: str = ""

    def __init__(self, registry: dict):
        self._registry = registry

    def _cfg(self) -> dict:
        return self._registry.get("plugins", {}).get(self._PLUGIN_KEY, {})

    def _cred(self, key: str, env_var: str) -> str:
        return self._cfg().get(key) or os.environ.get(env_var, "")

    @property
    def name(self) -> str:
        return self._PLUGIN_KEY

    @property
    def enabled(self) -> bool:
        if not self._cfg().get("enabled", False):
            return False
        return bool(self._cred("account_sid", "TWILIO_ACCOUNT_SID") and
                    self._cred("auth_token", "TWILIO_AUTH_TOKEN"))

    @property
    def user_id_prefix(self) -> str:
        return self._USER_ID_PREFIX

    @property
    def webhook_path(self) -> str:
        return self._PLUGIN_KEY

    def get_tools(self) -> list[dict]:
        return [
            {
                "name": self._TOOL_NAME,
                "description": self._TOOL_DESC,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "to": {"type": "string", "description": self._TOOL_TO_DESC},
                        "text": {"type": "string", "description": "Message body"},
                    },
                    "required": ["to", "text"],
                },
            }
        ]

    async def verify_request(self, request: Request) -> bool:
        """Parse the Twilio form body (stashing params for the extractors). When
        signature checking is on (SMS), verify X-Twilio-Signature once both a
        signature header and an auth_token are present; otherwise accept (dev)."""
        body_bytes = await request.body()
        params = dict(urllib.parse.parse_qsl(body_bytes.decode("utf-8")))
        request.state._twilio_params = params
        if not self._VERIFY_SIGNATURE:
            return True
        auth_token = self._cred("auth_token", "TWILIO_AUTH_TOKEN")
        sig = request.headers.get("X-Twilio-Signature", "")
        if not sig or not auth_token:
            return True  # no signature / no token → accept (dev mode)
        return _verify_twilio_signature(auth_token, sig, str(request.url), params)

    def extract_external_id(self, request: Request) -> str:
        params = getattr(request.state, "_twilio_params", {})
        raw = params.get("From", "")
        # WhatsApp stores the bare number (the whatsapp: prefix is re-added on send).
        return raw.replace("whatsapp:", "") if self._WHATSAPP else raw

    def extract_text(self, request: Request) -> str:
        params = getattr(request.state, "_twilio_params", {})
        return params.get("Body", "")

    async def handle_webhook(self, request: Request) -> Response:
        # Webhook verification + body parsing happens in verify_request; the
        # actual routing is done by the webhooks router.
        return Response(
            content='<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
            media_type="application/xml",
            status_code=200,
        )

    async def send_message(self, recipient_id: str, text: str) -> str:
        account_sid = self._cred("account_sid", "TWILIO_ACCOUNT_SID")
        auth_token = self._cred("auth_token", "TWILIO_AUTH_TOKEN")
        from_num = self._cred("from_number", self._FROM_ENV)
        if not account_sid or not auth_token or not from_num:
            raise RuntimeError(f"{self._DISPLAY} credentials not configured")
        result = await _twilio_send(
            account_sid, auth_token, from_num, recipient_id, text, whatsapp=self._WHATSAPP,
        )
        return str(result)
