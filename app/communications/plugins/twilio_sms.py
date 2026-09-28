"""
Twilio SMS communication plugin.

Receives inbound SMS via Twilio webhook, sends outbound SMS via Twilio REST API.
Credentials: account_sid, auth_token, from_number (stored in registry.json).
Env-var fallbacks: TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER.

The shared Twilio implementation lives in `_twilio_base.py`; this file just sets
the SMS channel constants. Plugin discovery: `plugin_cls = TwilioSmsPlugin`.
"""

from app.communications.plugins._twilio_base import TwilioBasePlugin


class TwilioSmsPlugin(TwilioBasePlugin):
    """Twilio SMS plugin — receives inbound SMS, sends outbound SMS."""

    _PLUGIN_KEY = "twilio_sms"
    _USER_ID_PREFIX = "sms:"
    _FROM_ENV = "TWILIO_FROM_NUMBER"
    _WHATSAPP = False
    _VERIFY_SIGNATURE = True
    _DISPLAY = "Twilio SMS"
    _TOOL_NAME = "send_sms"
    _TOOL_DESC = (
        "Send an SMS message via Twilio to a phone number. "
        "Args: to (E.164 phone number e.g. +15551234567), text (str)."
    )
    _TOOL_TO_DESC = "Recipient phone number in E.164 format (e.g. +15551234567)"


FEATURE = {
    "id": "sms",
    "display_name": "SMS (Twilio)",
    "category": "channel",
    "status": "beta",
    "summary": "Send and receive SMS via Twilio.",
    "requires": ["Twilio account SID + auth token + a phone number"],
}

plugin_cls = TwilioSmsPlugin
