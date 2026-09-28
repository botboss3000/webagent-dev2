"""Live browser events — interactive-shared-browser broadcast plumbing.

When the agent drives the shared Playwright page (clicks, types, navigates), the
in-app Browser panel wants to paint *attributed* feedback: a cursor gliding to
the target, a ripple on click, a highlight box, plus an action ticker. To do that
the frontend needs, per action: WHAT is happening (kind + plain-English label),
WHERE on the page (the element's viewport box / a point), and WHEN (timestamp).

This module is the **emitter** for those events. It is invisible plumbing — it
gates no tools and carries no FEATURE / TOOL_SCHEMAS. The browser_control wrapper
calls it around each real tool action; it broadcasts a structured `browser_live`
event to every WebSocket the user has open via the core `notify_user` transport.

Design rules (WHY):
  • EVERYTHING is best-effort and NON-FATAL. A broadcast or a bounding-box probe
    must NEVER raise or change the agent's tool result — visual flair is strictly
    secondary to the action succeeding. Hence every public coroutine wraps its
    whole body in try/except and degrades to None / a no-op.
  • Heavy / core imports stay LAZY (inside the functions) so the ability scan that
    imports this package's siblings stays cheap and circular-import-safe — the
    core chat module imports plenty, and we must not pull it in at module load.
  • Coordinates are page-viewport CSS pixels (1280x720), scroll-aware — exactly
    what Playwright's `bounding_box()` returns and what the frontend overlay maps.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Allowed event kinds (documented for the integrator; not enforced — we pass the
# caller's kind through verbatim so new kinds need no edit here).
#   'telegraph' — pre-action: show the cursor moving to / highlighting the target
#   'click'     — a click landed
#   'type'      — text was typed into a field
#   'nav'       — a navigation occurred
#   'scroll'    — the page scrolled
#   'backend'   — the session swapped browser backend (in-app <-> on-device)
#   'busy'/'idle' — coarse activity hints for the ticker


async def publish_browser_event(
    user_id: str,
    bs_id: str,
    kind: str,
    label: str,
    *,
    box: Optional[Dict[str, Any]] = None,
    point: Optional[Dict[str, Any]] = None,
    text: Optional[str] = None,
    url: Optional[str] = None,
    selector: Optional[str] = None,
) -> None:
    """Build a `browser_live` event and fire-and-forget it to the user's sockets.

    The event shape is a fixed contract the frontend overlay matches against (see
    the dict below). `notify_user` is imported lazily — the core chat module is
    heavy and importing it at module top risks a circular import during ability
    discovery. Never raises: a delivery failure must not break the tool call.
    """
    try:
        # Lazy import — keep ability scan cheap + avoid the circular import that a
        # top-level `from app.api.chat import notify_user` would create.
        from app.api.chat import notify_user

        event: Dict[str, Any] = {
            "type": "browser_live",
            "bs_id": bs_id,
            "actor": "agent",
            "kind": kind,
            "label": label,
            "box": box,
            "point": point,
            "text": text,
            "url": url,
            "selector": selector,
            "ts": int(time.time() * 1000),
        }
        await notify_user(user_id, event)
    except Exception:  # noqa: BLE001 — visual flair is never allowed to fail the action
        logger.debug("publish_browser_event failed (non-fatal)", exc_info=True)


async def compute_box(bs_id: str, selector: str) -> Optional[Dict[str, Any]]:
    """Return the target element's viewport box `{x,y,width,height}` or None.

    Used to position the telegraphed cursor / highlight before an action lands.
    Resolved against the SAME shared page the agent drives, so coordinates line up
    with what the human sees. Kept fast (short bounding-box timeout) and fully
    guarded — a bad selector, missing element or timeout just yields None and the
    overlay falls back to a non-positioned hint.
    """
    try:
        # Lazy import of the core page accessor (Playwright is heavy).
        from app.tools.browser import get_or_create_page

        page = await get_or_create_page(bs_id)
        locator = page.locator(selector).first
        try:
            # Prefer a short timeout so a stubborn selector can't stall the action.
            return await locator.bounding_box(timeout=1500)
        except TypeError:
            # Older Playwright without the timeout kwarg — fall back gracefully.
            return await locator.bounding_box()
    except Exception:  # noqa: BLE001 — non-fatal probe
        logger.debug("compute_box failed for selector=%r (non-fatal)", selector, exc_info=True)
        return None


async def describe_element(bs_id: str, selector: str) -> Optional[str]:
    """Best-effort short human label for an element (its trimmed inner_text).

    Lets the wrapper build friendly labels like 'Clicking “Sign in”' instead of a
    raw CSS selector. Capped at ~40 chars; returns None on any failure or when the
    element has no usable text (e.g. an icon-only button).
    """
    try:
        from app.tools.browser import get_or_create_page

        page = await get_or_create_page(bs_id)
        locator = page.locator(selector).first
        try:
            raw = await locator.inner_text(timeout=1500)
        except TypeError:
            raw = await locator.inner_text()
        if not raw:
            return None
        # Collapse whitespace + trim to a tidy, single-line label.
        text = " ".join(raw.split()).strip()
        if not text:
            return None
        return text[:40]
    except Exception:  # noqa: BLE001 — non-fatal probe
        logger.debug("describe_element failed for selector=%r (non-fatal)", selector, exc_info=True)
        return None
