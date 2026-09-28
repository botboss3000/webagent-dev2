"""Email integration tools.

Coverage:
  - Gmail (read/send) via Google API
  - Outlook (read/send) via Microsoft Graph
  - Yahoo Mail: only `yahoo_userinfo` — Yahoo discontinued their public Mail
    REST API; reading/sending mail requires IMAP/SMTP, which is out of scope
    for an OAuth-bearer HTTP tool layer.

All HTTP goes through `oauth_helper.oauth_api_call` so token refresh / 401
retry stays consistent.
"""

import base64
import json
from email.message import EmailMessage

from app.integrations.oauth_helper import (
    oauth_api_call,
    get_oauth_token,
    not_connected_payload,
)


# ── Gmail (Google) ────────────────────────────────────────────────────────

_GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"


async def gmail_list_messages(
    user_id: str,
    agent_id: str,
    query: str = "",
    max_results: int = 10,
    label_ids: str = "",
) -> str:
    params: dict = {"maxResults": max(1, min(int(max_results or 10), 100))}
    if query:
        params["q"] = query
    if label_ids:
        params["labelIds"] = [s.strip() for s in label_ids.split(",") if s.strip()]
    result = await oauth_api_call(
        user_id, agent_id, "google", "GET", f"{_GMAIL_BASE}/messages",
        params=params, ability="google.gmail_read",
    )
    if result.get("status") == "not_connected":
        return not_connected_payload("google", ability=result.get("ability") or "google.gmail_read")
    return json.dumps(result)


async def gmail_get_message(user_id: str, agent_id: str, message_id: str, format: str = "metadata") -> str:
    if not message_id:
        return json.dumps({"status": "error", "message": "message_id required"})
    params = {"format": format or "metadata"}
    if (format or "metadata") == "metadata":
        params["metadataHeaders"] = ["From", "To", "Subject", "Date", "Cc"]
    result = await oauth_api_call(
        user_id, agent_id, "google", "GET",
        f"{_GMAIL_BASE}/messages/{message_id}",
        params=params, ability="google.gmail_read",
    )
    if result.get("status") == "not_connected":
        return not_connected_payload("google", ability=result.get("ability") or "google.gmail_read")
    if result.get("status") == "ok" and (format or "metadata") == "full":
        body = result.get("body")
        if isinstance(body, dict):
            decoded = _gmail_extract_plain_text(body.get("payload") or {})
            if decoded:
                body["_decoded_text"] = decoded
    return json.dumps(result)


async def gmail_send(
    user_id: str,
    agent_id: str,
    to: str,
    subject: str,
    body: str,
    cc: str = "",
    bcc: str = "",
    html: bool = False,
) -> str:
    tok = await get_oauth_token(user_id, agent_id, "google")
    if not tok:
        return not_connected_payload("google", ability="google.gmail_send")
    if "google.gmail_send" not in (tok.get("covered_abilities") or []):
        return not_connected_payload("google", ability="google.gmail_send")
    if not to or not subject:
        return json.dumps({"status": "error", "message": "to and subject required"})

    msg = EmailMessage()
    msg["To"] = to
    msg["Subject"] = subject
    if cc:
        msg["Cc"] = cc
    if bcc:
        msg["Bcc"] = bcc
    if tok.get("account"):
        msg["From"] = tok["account"]
    if html:
        msg.set_content(body, subtype="html")
    else:
        msg.set_content(body)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode().rstrip("=")

    result = await oauth_api_call(
        user_id, agent_id, "google", "POST",
        f"{_GMAIL_BASE}/messages/send",
        json_body={"raw": raw},
    )
    return json.dumps(result)


def _gmail_extract_plain_text(payload: dict) -> str:
    mime = (payload or {}).get("mimeType", "")
    if mime.startswith("text/"):
        data = (payload.get("body") or {}).get("data", "")
        if data:
            try:
                return base64.urlsafe_b64decode(data + "===").decode("utf-8", errors="replace")
            except Exception:
                return ""
    for part in (payload or {}).get("parts", []) or []:
        out = _gmail_extract_plain_text(part)
        if out:
            return out
    return ""


# ── Outlook (Microsoft Graph) ─────────────────────────────────────────────

_GRAPH = "https://graph.microsoft.com/v1.0"


async def outlook_list_messages(
    user_id: str,
    agent_id: str,
    query: str = "",
    unread_only: bool = False,
    max_results: int = 10,
    folder: str = "inbox",
) -> str:
    """List Outlook messages. folder = 'inbox', 'sentitems', 'drafts', or a folder id."""
    params: dict = {
        "$top": max(1, min(int(max_results or 10), 100)),
        "$select": "id,subject,from,toRecipients,receivedDateTime,isRead,bodyPreview",
        "$orderby": "receivedDateTime desc",
    }
    filters = []
    if unread_only:
        filters.append("isRead eq false")
    if filters:
        params["$filter"] = " and ".join(filters)
    if query:
        params["$search"] = f'"{query}"'
        params.pop("$orderby", None)  # Graph forbids $orderby with $search

    url = f"{_GRAPH}/me/mailFolders/{folder}/messages" if folder else f"{_GRAPH}/me/messages"
    result = await oauth_api_call(
        user_id, agent_id, "microsoft", "GET", url, params=params,
        ability="microsoft.mail_read",
    )
    if result.get("status") == "not_connected":
        return not_connected_payload("microsoft", ability=result.get("ability") or "microsoft.mail_read")
    return json.dumps(result)


async def outlook_get_message(user_id: str, agent_id: str, message_id: str, full_body: bool = False) -> str:
    if not message_id:
        return json.dumps({"status": "error", "message": "message_id required"})
    select = "id,subject,from,toRecipients,ccRecipients,receivedDateTime,isRead,bodyPreview"
    if full_body:
        select += ",body"
    result = await oauth_api_call(
        user_id, agent_id, "microsoft", "GET",
        f"{_GRAPH}/me/messages/{message_id}",
        params={"$select": select},
        ability="microsoft.mail_read",
    )
    if result.get("status") == "not_connected":
        return not_connected_payload("microsoft", ability=result.get("ability") or "microsoft.mail_read")
    return json.dumps(result)


async def outlook_send(
    user_id: str,
    agent_id: str,
    to: str,
    subject: str,
    body: str,
    cc: str = "",
    bcc: str = "",
    html: bool = False,
) -> str:
    if not to or not subject:
        return json.dumps({"status": "error", "message": "to and subject required"})

    def _addrs(s: str) -> list:
        return [{"emailAddress": {"address": a.strip()}} for a in s.split(",") if a.strip()]

    message = {
        "subject": subject,
        "body": {"contentType": "HTML" if html else "Text", "content": body},
        "toRecipients": _addrs(to),
    }
    if cc:
        message["ccRecipients"] = _addrs(cc)
    if bcc:
        message["bccRecipients"] = _addrs(bcc)

    result = await oauth_api_call(
        user_id, agent_id, "microsoft", "POST",
        f"{_GRAPH}/me/sendMail",
        json_body={"message": message, "saveToSentItems": True},
        ability="microsoft.mail_send",
    )
    if result.get("status") == "not_connected":
        return not_connected_payload("microsoft", ability=result.get("ability") or "microsoft.mail_send")
    return json.dumps(result)


# ── Yahoo Mail (identity only) ────────────────────────────────────────────

async def yahoo_userinfo(user_id: str, agent_id: str) -> str:
    """Return Yahoo profile info. Yahoo no longer offers a public REST Mail
    API; mailbox access requires IMAP/SMTP (outside this tool layer)."""
    result = await oauth_api_call(
        user_id, agent_id, "yahoo", "GET",
        "https://api.login.yahoo.com/openid/v1/userinfo",
    )
    if result.get("status") == "not_connected":
        return not_connected_payload("yahoo")
    return json.dumps(result)


# ── Tool registry ─────────────────────────────────────────────────────────

FEATURE = {
    "id": "email",
    "display_name": "Email (Gmail / Outlook / Yahoo)",
    "category": "integration",
    "status": "stable",
    "summary": "Read, search, and send email across Gmail and Outlook; Yahoo identity.",
    "requires": ["Google and/or Microsoft OAuth credentials"],
    # Ability-bundled skill: surfaced in the agent's # [SKILLS] catalog whenever
    # this ability is enabled; the body loads on demand via load_skill(handle).
    "skill_mode": "selectable",
    "skill_handle": "email_a1b2c3d4",
    "skill": (
        "## Working with email\n"
        "- Identify the provider first: Gmail tools are prefixed `gmail_`, Outlook `outlook_`.\n"
        "- To answer 'do I have any email about X', use the search tool with the\n"
        "  provider's query syntax (Gmail search operators / Graph $search) rather\n"
        "  than listing the whole inbox.\n"
        "- Always confirm the recipient and show the user a draft before sending;\n"
        "  send tools are destructive and will actually deliver.\n"
        "- Thread replies: reply in-thread (carry the thread/conversation id) rather\n"
        "  than starting a new message, unless the user asks for a fresh email."
    ),
}

TOOLS = [
    # ── Gmail ──
    {
        "name": "gmail_list_messages",
        "provider": "google",
        "handler": gmail_list_messages,
        "stages": ["execute_tools"],
        "destructive": False,
        "parameters": {
            "type": "object",
            "properties": {
                "query":       {"type": "string",  "description": "Gmail search query — 'is:unread', 'from:alice newer_than:7d', 'has:attachment'. Empty = recent.", "default": ""},
                "max_results": {"type": "integer", "description": "Max messages (1-100).", "default": 10},
                "label_ids":   {"type": "string",  "description": "Optional comma-separated label IDs (e.g. 'INBOX,UNREAD').", "default": ""},
            },
            "required": [],
        },
    },
    {
        "name": "gmail_get_message",
        "provider": "google",
        "handler": gmail_get_message,
        "stages": ["execute_tools"],
        "destructive": False,
        "parameters": {
            "type": "object",
            "properties": {
                "message_id": {"type": "string", "description": "ID from gmail_list_messages."},
                "format":     {"type": "string", "enum": ["metadata", "full", "raw"],
                                "description": "metadata = headers + snippet; full = payload + decoded text body; raw = RFC 2822.",
                                "default": "metadata"},
            },
            "required": ["message_id"],
        },
    },
    {
        "name": "gmail_send",
        "provider": "google",
        "handler": gmail_send,
        "stages": ["execute_tools"],
        "destructive": True,
        "requires_confirmation": True,
        "parameters": {
            "type": "object",
            "properties": {
                "to":      {"type": "string",  "description": "Recipient(s), comma-separated."},
                "subject": {"type": "string",  "description": "Email subject."},
                "body":    {"type": "string",  "description": "Email body (plain text unless html=true)."},
                "cc":      {"type": "string",  "description": "Optional CC, comma-separated.", "default": ""},
                "bcc":     {"type": "string",  "description": "Optional BCC, comma-separated.", "default": ""},
                "html":    {"type": "boolean", "description": "Set true if body is HTML.", "default": False},
            },
            "required": ["to", "subject", "body"],
        },
    },

    # ── Outlook ──
    {
        "name": "outlook_list_messages",
        "provider": "microsoft",
        "handler": outlook_list_messages,
        "stages": ["execute_tools"],
        "destructive": False,
        "parameters": {
            "type": "object",
            "properties": {
                "query":       {"type": "string",  "description": "Optional free-text search (Graph $search). Mutually exclusive with sort order.", "default": ""},
                "unread_only": {"type": "boolean", "description": "When true, only unread messages.", "default": False},
                "max_results": {"type": "integer", "description": "Max messages (1-100).", "default": 10},
                "folder":      {"type": "string",  "description": "Well-known folder ('inbox', 'sentitems', 'drafts') or folder ID. Empty = all folders.", "default": "inbox"},
            },
            "required": [],
        },
    },
    {
        "name": "outlook_get_message",
        "provider": "microsoft",
        "handler": outlook_get_message,
        "stages": ["execute_tools"],
        "destructive": False,
        "parameters": {
            "type": "object",
            "properties": {
                "message_id": {"type": "string",  "description": "Outlook message id (from outlook_list_messages)."},
                "full_body":  {"type": "boolean", "description": "Include the full HTML/text body.", "default": False},
            },
            "required": ["message_id"],
        },
    },
    {
        "name": "outlook_send",
        "provider": "microsoft",
        "handler": outlook_send,
        "stages": ["execute_tools"],
        "destructive": True,
        "requires_confirmation": True,
        "parameters": {
            "type": "object",
            "properties": {
                "to":      {"type": "string",  "description": "Recipient(s), comma-separated."},
                "subject": {"type": "string",  "description": "Email subject."},
                "body":    {"type": "string",  "description": "Email body."},
                "cc":      {"type": "string",  "description": "Optional CC, comma-separated.", "default": ""},
                "bcc":     {"type": "string",  "description": "Optional BCC, comma-separated.", "default": ""},
                "html":    {"type": "boolean", "description": "Set true if body is HTML.", "default": False},
            },
            "required": ["to", "subject", "body"],
        },
    },

    # ── Yahoo ──
    {
        "name": "yahoo_userinfo",
        "provider": "yahoo",
        "handler": yahoo_userinfo,
        "stages": ["execute_tools"],
        "destructive": False,
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
]
