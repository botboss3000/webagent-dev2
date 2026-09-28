"""
Browser-side error intake — JS errors, console warnings from the page.

The browser catches its own JS errors and unhandled promise rejections (in
``ui/js/main.js`` via ``window.onerror`` / ``unhandledrejection``), shows them
as a red banner in the page, and now also pushes them here — so they land in
the server's diagnostic flight-recorder alongside server-side errors.

    POST /api/v1/log/browser-error

The TUI's ``read_diagnostics(category="browser")`` picks them up immediately.
"""

import logging

from fastapi import APIRouter

from app.agent.diagnostics import get_recorder

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/log", tags=["log"])


@router.post("/browser-error")
async def ingest_browser_error(body: dict):
    """Accept a JS error or console warning from the browser UI.

    Body fields (all optional):
      - message: str   — the error text
      - source: str    — filename or context label
      - stack: str     — stack trace
      - level: str     — "error" or "warning" (defaults to "error")
      - url: str       — the page URL at time of error
      - user_id / session_id / agent_id — correlation coordinates (set by the page)
    """
    rec = get_recorder()
    if not rec:
        return {"ok": False, "reason": "recorder not available"}

    level = (body.get("level") or "error").lower()
    if level not in ("error", "warning"):
        level = "error"

    message = str(body.get("message") or "(no message)").strip()[:4000]
    source = str(body.get("source") or "").strip()[:200]

    detail = {}
    if body.get("stack"):
        detail["stack"] = str(body["stack"])[:8000]
    if body.get("url"):
        detail["url"] = str(body["url"])[:500]

    rec.record(
        level=level,
        category="browser",
        message=message,
        source=source or "browser",
        detail=detail or None,
        session_id=body.get("session_id"),
        agent_id=body.get("agent_id"),
        user_id=body.get("user_id"),
        persist=True,
    )

    return {"ok": True}