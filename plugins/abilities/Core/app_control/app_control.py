"""App Control ability — SELF-CONTAINED drop-in. See app/abilities/__init__.py
for the contract.

Lets the agent change what the user sees: switch the main view (browser, pages,
agents, terminal, account, admin), show/hide the chat panel, and resize it.

Per-agent config (stored in the connection config.ability_settings, schema in
sibling app_control.json): allow_hide_chat, allow_resize_chat, max_chat_width,
min_chat_width, and allowed_views (a checklist of view names). The runtime
enforces all of these — a blocked view or a denied action returns an error
to the agent.

This file is fully self-contained: the ``set_app_view`` handler, its JSON
schema and the view-alias map all live here (previously in
``app/tools/app_control_tools.py``, now absorbed). ``build_tools`` builds the
core handler and wraps it with the per-agent config enforcement below. The
tool's JSON schema is published into the module-level ``TOOL_SCHEMAS`` inside
``build_tools`` so the loader (which reads it AFTER the call) always sees the
current value.

Non-destructive: it only moves panels around and writes no data, so
``DESTRUCTIVE`` is empty.
"""

from __future__ import annotations

import logging
from typing import Callable, Dict, Optional

logger = logging.getLogger(__name__)

# Populated inside build_tools() so the loader (which reads these AFTER
# build_tools) always sees current values.
TOOL_SCHEMAS: dict = {}
DESTRUCTIVE: set = set()  # set_app_view only rearranges panels — nothing destructive.

# Friendly view name (what the agent passes) → internal main-tab id used by
# ui/js/tabs.js. Both the friendly name and the raw tab id are accepted so the
# agent can't trip on the internal spelling.
_VIEW_ALIASES: Dict[str, str] = {
    "browser":     "web",
    "web":         "web",
    "genui":      "genui",
    "agents":      "agents",
    "terminal":    "terminal",
    "account":     "account",
    "admin":       "admin-tools",
    "admin-tools": "admin-tools",
}

# The values offered to the agent in the tool schema (friendly names only).
_VIEW_CHOICES = ["browser", "genui", "agents", "terminal", "account", "admin"]

SET_APP_VIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "view": {
            "type": "string",
            "enum": _VIEW_CHOICES,
            "description": (
                "Which main view to bring to the front of the user's screen. "
                "'browser' = the live in-app browser (mirrors your headless "
                "Chromium), 'genui' = the Gen UI workspace, 'agents' = the agent "
                "manager, 'terminal' = the terminal, 'account' = the account "
                "page, 'admin' = the admin tools. Omit to leave the current "
                "view unchanged."
            ),
        },
        "show_chat": {
            "type": "boolean",
            "description": (
                "Show (true) or hide (false) the chat panel. Hiding it gives "
                "the main view the full width — e.g. hide chat while showing "
                "the browser. Omit to leave chat visibility unchanged."
            ),
        },
        "chat_width": {
            "type": "integer",
            "description": (
                "Width of the chat panel in pixels (minimum 280). Use to widen "
                "or narrow chat, e.g. to give the browser more room. Omit to "
                "leave the width unchanged."
            ),
        },
    },
    "required": [],
}

# Default per-agent app-control limits, overridden by the config JSON stored
# on the app_control connection row (config.ability_settings).
_APP_CONTROL_DEFAULTS = {
    "allow_hide_chat": True,
    "allow_resize_chat": True,
    "max_chat_width": 0,      # 0 = unlimited
    "min_chat_width": 280,
    # Friendly view names — MUST match _VIEW_CHOICES / _VIEW_ALIASES keys above.
    # ("genui" is the current name for what used to be "pages"; using "pages"
    # here made the Gen UI view unreachable because _enforce_app_control compares
    # against this list while the schema/aliases only accept "genui".)
    "allowed_views": ["browser", "genui", "agents", "terminal", "account", "admin"],
}


def build_app_control_tools(user_id: str, session_id: str) -> Dict[str, Callable]:
    """Return the app-control tool handlers, closed over the caller's session.

    `session_id` targets the right screen — the front-end filters ui_command
    events to the session the viewer is currently chatting with.
    """

    async def set_app_view(
        view: Optional[str] = None,
        show_chat: Optional[bool] = None,
        chat_width: Optional[int] = None,
    ) -> str:
        """Change what the user is looking at in the app window.

        Switch the main view (browser, genui, agents, terminal, account,
        admin), show or hide the chat panel, and/or resize the chat panel —
        all on the user's live screen. Use this to follow your narration with
        the matching view: e.g. when you start browsing, bring the browser to
        the front and hide chat so the user watches the page. All arguments
        are optional; pass only what you want to change. The change is applied
        silently.
        """
        tab: Optional[str] = None
        friendly: Optional[str] = None
        if view is not None:
            friendly = str(view).strip().lower()
            tab = _VIEW_ALIASES.get(friendly)
            if tab is None:
                return (
                    f"Unknown view '{view}'. Choose one of: "
                    + ", ".join(_VIEW_CHOICES)
                )

        if tab is None and show_chat is None and chat_width is None:
            return "Nothing to change — pass a view, show_chat, and/or chat_width."

        if not session_id:
            return (
                "Can't change the view: this run has no live UI session attached "
                "(it may be an event-triggered or background run)."
            )

        width: Optional[int] = None
        if chat_width is not None:
            try:
                width = max(280, int(chat_width))
            except (TypeError, ValueError):
                return "chat_width must be a whole number of pixels (minimum 280)."

        event = {
            "type": "ui_command",
            "action": "set_view",
            "view": tab,            # internal tab id, or None to leave as-is
            "show_chat": show_chat,  # bool or None
            "chat_width": width,     # int or None
        }
        try:
            from app.api.chat import _emit_to_visualizers
            await _emit_to_visualizers(session_id, event, user_id=user_id)
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("set_app_view emit failed: %s", e)
            return f"Couldn't reach the user's screen to change the view: {e}"

        parts = []
        if friendly is not None:
            parts.append(f"showing the {friendly} view")
        if show_chat is not None:
            parts.append("showing the chat panel" if show_chat else "hiding the chat panel")
        if width is not None:
            parts.append(f"setting the chat panel width to {width}px")
        return "Done — " + ", ".join(parts) + "."

    return {"set_app_view": set_app_view}


async def _load_app_control_config(agent_id: str) -> dict:
    """Read per-agent app-control limits from the agent_orchestration connection
    row's config JSON. Returns defaults for any missing keys."""
    if not agent_id:
        return dict(_APP_CONTROL_DEFAULTS)
    limits = dict(_APP_CONTROL_DEFAULTS)
    try:
        from app.db import get_db
        db = get_db()
        conns = await db.get_agent_connections(agent_id)
        for c in conns:
            if c.get("connection_type") == "app_control" and c.get("section") == "ability":
                raw = c.get("config")
                if isinstance(raw, str):
                    import json
                    raw = json.loads(raw or "{}")
                if isinstance(raw, dict):
                    settings = raw.get("ability_settings") or {}
                    for k in _APP_CONTROL_DEFAULTS:
                        if k in settings:
                            v = settings[k]
                            if k == "allowed_views":
                                if isinstance(v, list):
                                    limits[k] = v
                            elif k in ("max_chat_width", "min_chat_width"):
                                try:
                                    limits[k] = int(v)
                                except (TypeError, ValueError):
                                    pass
                            elif k in ("allow_hide_chat", "allow_resize_chat"):
                                limits[k] = bool(v)
                break
    except Exception:
        pass
    return limits


def _enforce_app_control(cfg: dict, view=None, show_chat=None, chat_width=None):
    """Check per-agent app-control limits against a set_app_view call.
    Returns (error_string, clamped_width).  error is None when OK;
    clamped_width is None when no clamping was needed (or the call was blocked)."""
    clamped_w = None

    # 1. Allowed views
    if view is not None:
        friendly = str(view).strip().lower()
        allowed = cfg.get("allowed_views", _APP_CONTROL_DEFAULTS["allowed_views"])
        if allowed and friendly not in allowed:
            return f"View '{view}' is not in the allowed list for this agent. Allowed: {', '.join(allowed)}.", None

    # 2. Hide-chat permission
    if show_chat is not None and show_chat is False:
        if not cfg.get("allow_hide_chat", True):
            return "Hiding the chat panel is not allowed for this agent.", None

    # 3. Resize permission + clamp to min/max
    if chat_width is not None:
        if not cfg.get("allow_resize_chat", True):
            return "Resizing the chat panel is not allowed for this agent.", None
        max_w = cfg.get("max_chat_width", 0)
        min_w = cfg.get("min_chat_width", 280)
        clamped = max(280, int(chat_width))  # system hard floor
        if min_w > 280:
            clamped = max(min_w, clamped)
        if max_w > 0:
            clamped = min(max_w, clamped)
        clamped_w = clamped

    return None, clamped_w


def build_tools(*, user_id: str = "", session_id: str = "", agent_id: str = "",
                agent_template_id: str = "", enabled_providers=None, **_ctx):
    """Return {tool_name: handler} for the app-control tools.

    Wraps the core set_app_view handler with per-agent config enforcement:
    allowed views, hide-chat permission, resize permission, and min/max width
    clamping. Loads config from the app_control connection row
    (config.ability_settings) with defaults from _APP_CONTROL_DEFAULTS.
    """
    core_handlers = build_app_control_tools(user_id, session_id)
    core_set_view = core_handlers.get("set_app_view")

    TOOL_SCHEMAS.clear()
    TOOL_SCHEMAS.update({name: SET_APP_VIEW_SCHEMA for name in core_handlers})

    if core_set_view is None:
        return core_handlers

    async def set_app_view_wrapped(
        view: str = None,
        show_chat: bool = None,
        chat_width: int = None,
    ) -> str:
        """Change what the user sees, respecting per-agent app-control limits."""
        cfg = await _load_app_control_config(agent_id)
        error, clamped_w = _enforce_app_control(
            cfg, view=view, show_chat=show_chat, chat_width=chat_width)
        if error:
            return error
        # Pass the (possibly clamped) width to the core handler.
        return await core_set_view(
            view=view, show_chat=show_chat,
            chat_width=clamped_w if clamped_w is not None else chat_width)

    return {"set_app_view": set_app_view_wrapped}
