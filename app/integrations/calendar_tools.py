"""Calendar integration tools — Google Calendar + Outlook Calendar."""

import json
from datetime import datetime, timezone

from app.integrations.oauth_helper import oauth_api_call, not_connected_payload
from app.tools.provider_idempotency import (
    google_calendar_create,
    microsoft_calendar_create,
)


# ── Google Calendar ───────────────────────────────────────────────────────

_GCAL_BASE = "https://www.googleapis.com/calendar/v3"


async def gcal_list_events(
    user_id: str,
    agent_id: str,
    calendar_id: str = "primary",
    query: str = "",
    time_min: str = "",
    time_max: str = "",
    max_results: int = 10,
) -> str:
    params: dict = {
        "maxResults": max(1, min(int(max_results or 10), 100)),
        "singleEvents": "true",
        "orderBy": "startTime",
    }
    params["timeMin"] = time_min or datetime.now(timezone.utc).isoformat()
    if time_max:
        params["timeMax"] = time_max
    if query:
        params["q"] = query
    result = await oauth_api_call(
        user_id, agent_id, "google", "GET",
        f"{_GCAL_BASE}/calendars/{calendar_id}/events",
        params=params, ability="google.calendar_read",
    )
    if result.get("status") == "not_connected":
        return not_connected_payload("google", ability=result.get("ability") or "google.calendar_read")
    return json.dumps(result)


async def gcal_create_event(
    user_id: str,
    agent_id: str,
    summary: str,
    start: str,
    end: str,
    calendar_id: str = "primary",
    description: str = "",
    location: str = "",
    attendees: str = "",
    timezone_str: str = "UTC",
) -> str:
    if not summary or not start or not end:
        return json.dumps({"status": "error", "message": "summary, start, end required"})

    def _time_field(value: str) -> dict:
        if "T" in value:
            return {"dateTime": value, "timeZone": timezone_str}
        return {"date": value}

    body: dict = {
        "summary": summary,
        "start": _time_field(start),
        "end":   _time_field(end),
    }
    provider_op = google_calendar_create()
    if provider_op:
        # Google documents client-supplied event IDs as the duplicate-prevention
        # primitive for a timed-out events.insert request.
        body["id"] = provider_op.resource_id
    if description:
        body["description"] = description
    if location:
        body["location"] = location
    if attendees:
        body["attendees"] = [{"email": a.strip()} for a in attendees.split(",") if a.strip()]

    result = await oauth_api_call(
        user_id, agent_id, "google", "POST",
        f"{_GCAL_BASE}/calendars/{calendar_id}/events",
        json_body=body, ability="google.calendar_write",
    )
    if (
        provider_op
        and result.get("status") == "error"
        and int(result.get("http_status") or 0) == 409
    ):
        # A previous worker may have completed the deterministic insert before
        # losing its lease. Google reports the repeated insert as a conflict, so
        # reconcile by reading the exact provider resource rather than turning a
        # successfully-created event into a durable tool error.
        existing = await oauth_api_call(
            user_id,
            agent_id,
            "google",
            "GET",
            (
                f"{_GCAL_BASE}/calendars/{calendar_id}/events/"
                f"{provider_op.resource_id}"
            ),
            ability="google.calendar_write",
        )
        if existing.get("status") == "ok":
            result = {**existing, "reconciled": True}
    if result.get("status") == "not_connected":
        return not_connected_payload("google", ability=result.get("ability") or "google.calendar_write")
    return json.dumps(result)


# ── Outlook Calendar (Microsoft Graph) ────────────────────────────────────

_GRAPH = "https://graph.microsoft.com/v1.0"


async def outlook_calendar_list_events(
    user_id: str,
    agent_id: str,
    time_min: str = "",
    time_max: str = "",
    max_results: int = 10,
    query: str = "",
) -> str:
    """List Outlook calendar events. Uses calendarView for time-range queries."""
    start = time_min or datetime.now(timezone.utc).isoformat()
    end = time_max or ""
    if end:
        url = f"{_GRAPH}/me/calendarView"
        params = {
            "startDateTime": start,
            "endDateTime": end,
            "$top": max(1, min(int(max_results or 10), 100)),
            "$select": "id,subject,start,end,location,attendees,bodyPreview,organizer",
            "$orderby": "start/dateTime",
        }
    else:
        url = f"{_GRAPH}/me/events"
        params = {
            "$top": max(1, min(int(max_results or 10), 100)),
            "$filter": f"start/dateTime ge '{start}'",
            "$select": "id,subject,start,end,location,attendees,bodyPreview,organizer",
            "$orderby": "start/dateTime",
        }
    if query:
        params["$search"] = f'"{query}"'
        params.pop("$orderby", None)
        params.pop("$filter", None)

    result = await oauth_api_call(
        user_id, agent_id, "microsoft", "GET", url, params=params,
        ability="microsoft.calendar_write",  # MS only has one calendar scope
    )
    if result.get("status") == "not_connected":
        return not_connected_payload("microsoft", ability=result.get("ability") or "microsoft.calendar_write")
    return json.dumps(result)


async def outlook_calendar_create_event(
    user_id: str,
    agent_id: str,
    subject: str,
    start: str,
    end: str,
    description: str = "",
    location: str = "",
    attendees: str = "",
    timezone_str: str = "UTC",
    html_body: bool = False,
) -> str:
    if not subject or not start or not end:
        return json.dumps({"status": "error", "message": "subject, start, end required"})

    body: dict = {
        "subject": subject,
        "start": {"dateTime": start, "timeZone": timezone_str},
        "end":   {"dateTime": end,   "timeZone": timezone_str},
    }
    provider_op = microsoft_calendar_create()
    if provider_op:
        # Graph's transactionId tells the service to avoid a redundant POST.
        body["transactionId"] = provider_op.resource_id
    if description:
        body["body"] = {"contentType": "HTML" if html_body else "Text", "content": description}
    if location:
        body["location"] = {"displayName": location}
    if attendees:
        body["attendees"] = [
            {"emailAddress": {"address": a.strip()}, "type": "required"}
            for a in attendees.split(",") if a.strip()
        ]

    result = await oauth_api_call(
        user_id, agent_id, "microsoft", "POST",
        f"{_GRAPH}/me/events",
        json_body=body, ability="microsoft.calendar_write",
    )
    if result.get("status") == "not_connected":
        return not_connected_payload("microsoft", ability=result.get("ability") or "microsoft.calendar_write")
    return json.dumps(result)


# ── Tool registry ─────────────────────────────────────────────────────────

FEATURE = {
    "id": "calendar",
    "display_name": "Calendar (Google / Outlook)",
    "category": "integration",
    "status": "stable",
    "summary": "List, create, and manage events on Google Calendar and Outlook Calendar.",
    "requires": ["Google and/or Microsoft OAuth credentials"],
}

TOOLS = [
    # ── Google Calendar ──
    {
        "name": "gcal_list_events",
        "provider": "google",
        "handler": gcal_list_events,
        "stages": ["execute_tools"],
        "destructive": False,
        "parameters": {
            "type": "object",
            "properties": {
                "calendar_id": {"type": "string",  "description": "Calendar to query. 'primary' = main calendar.", "default": "primary"},
                "query":       {"type": "string",  "description": "Optional free-text search.", "default": ""},
                "time_min":    {"type": "string",  "description": "RFC 3339 lower bound. Blank = now.", "default": ""},
                "time_max":    {"type": "string",  "description": "Optional RFC 3339 upper bound.", "default": ""},
                "max_results": {"type": "integer", "description": "Max events (1-100).", "default": 10},
            },
            "required": [],
        },
    },
    {
        "name": "gcal_create_event",
        "provider": "google",
        "handler": gcal_create_event,
        "stages": ["execute_tools"],
        "destructive": True,
        "requires_confirmation": True,
        "parameters": {
            "type": "object",
            "properties": {
                "summary":      {"type": "string", "description": "Event title."},
                "start":        {"type": "string", "description": "RFC 3339 datetime or YYYY-MM-DD for all-day."},
                "end":          {"type": "string", "description": "RFC 3339 datetime or YYYY-MM-DD (exclusive) for all-day."},
                "calendar_id":  {"type": "string", "description": "Calendar to write to.", "default": "primary"},
                "description":  {"type": "string", "description": "Optional event description.", "default": ""},
                "location":     {"type": "string", "description": "Optional location.", "default": ""},
                "attendees":    {"type": "string", "description": "Comma-separated attendee emails.", "default": ""},
                "timezone_str": {"type": "string", "description": "IANA timezone for timed events.", "default": "UTC"},
            },
            "required": ["summary", "start", "end"],
        },
    },

    # ── Outlook Calendar ──
    {
        "name": "outlook_calendar_list_events",
        "provider": "microsoft",
        "handler": outlook_calendar_list_events,
        "stages": ["execute_tools"],
        "destructive": False,
        "parameters": {
            "type": "object",
            "properties": {
                "time_min":    {"type": "string",  "description": "RFC 3339 lower bound. Blank = now.", "default": ""},
                "time_max":    {"type": "string",  "description": "Optional RFC 3339 upper bound (if set, uses calendarView).", "default": ""},
                "max_results": {"type": "integer", "description": "Max events (1-100).", "default": 10},
                "query":       {"type": "string",  "description": "Optional free-text search.", "default": ""},
            },
            "required": [],
        },
    },
    {
        "name": "outlook_calendar_create_event",
        "provider": "microsoft",
        "handler": outlook_calendar_create_event,
        "stages": ["execute_tools"],
        "destructive": True,
        "requires_confirmation": True,
        "parameters": {
            "type": "object",
            "properties": {
                "subject":      {"type": "string",  "description": "Event title."},
                "start":        {"type": "string",  "description": "RFC 3339 datetime."},
                "end":          {"type": "string",  "description": "RFC 3339 datetime."},
                "description":  {"type": "string",  "description": "Optional event body.", "default": ""},
                "location":     {"type": "string",  "description": "Optional location.", "default": ""},
                "attendees":    {"type": "string",  "description": "Comma-separated attendee emails.", "default": ""},
                "timezone_str": {"type": "string",  "description": "IANA timezone (e.g. 'America/Los_Angeles').", "default": "UTC"},
                "html_body":    {"type": "boolean", "description": "Set true if description is HTML.", "default": False},
            },
            "required": ["subject", "start", "end"],
        },
    },
]
