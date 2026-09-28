"""
WhatsApp via Twilio communication plugin.

Uses the same Twilio REST API as twilio_sms but with whatsapp: number prefixes.
Receives inbound WhatsApp messages via Twilio webhook.
Credentials: account_sid, auth_token, from_number (whatsapp:+14155238886 format).
Env-var fallbacks: TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_FROM.

The shared Twilio implementation lives in `_twilio_base.py`; this file just sets
the WhatsApp channel constants (notably the whatsapp: prefix toggle). Plugin
discovery: `plugin_cls = TwilioWhatsAppPlugin`.
"""

from app.communications.plugins._twilio_base import TwilioBasePlugin


class TwilioWhatsAppPlugin(TwilioBasePlugin):
    """WhatsApp via Twilio plugin — same REST API as SMS, whatsapp: prefixed numbers."""

    _PLUGIN_KEY = "twilio_whatsapp"
    _USER_ID_PREFIX = "whatsapp:"
    _FROM_ENV = "TWILIO_WHATSAPP_FROM"
    _WHATSAPP = True
    _VERIFY_SIGNATURE = False
    _DISPLAY = "Twilio WhatsApp"
    _TOOL_NAME = "send_whatsapp_message"
    _TOOL_DESC = (
        "Send a WhatsApp message via Twilio to a phone number. "
        "Args: to (E.164 phone number, e.g. +15551234567), text (str)."
    )
    _TOOL_TO_DESC = (
        "Recipient phone number in E.164 format "
        "(e.g. +15551234567, whatsapp: prefix optional)"
    )


FEATURE = {
    "id": "whatsapp",
    "display_name": "WhatsApp (Twilio)",
    "category": "channel",
    "status": "beta",
    "summary": "Send and receive WhatsApp messages via Twilio.",
    "requires": ["Twilio account SID + auth token + a WhatsApp sender"],
}

plugin_cls = TwilioWhatsAppPlugin
