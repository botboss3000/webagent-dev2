"""Visualizer ability — SELF-CONTAINED drop-in.

Everything this capability needs to expose its tools lives here: the FEATURE
descriptor, and a build_tools() that produces the page-authoring handlers. The
actual handler bodies still live in app/visualizer/ (register_tools), which
mutates a tools dict with ToolInfo objects rather than returning handlers.
build_tools() runs that registrar against a throwaway dict and adapts the result
into the (handlers, schemas, destructive) shape the generic drop-in loader wants,
populating module-level TOOL_SCHEMAS/DESTRUCTIVE in the process so they never
drift from the registrar.

Discovered generically by core via the three optional module hooks (see
app/abilities/__init__.py "Self-contained abilities"):
  • FEATURE        — catalog + UI + which tool names it gates
  • build_tools(...) — its tool handlers (app/tools/loader.py injects them)
  • TOOL_SCHEMAS / DESTRUCTIVE — read by the loader AFTER build_tools().
"""

from __future__ import annotations

# Populated INSIDE build_tools() from the ToolInfo objects the registrar emits,
# so they never drift from app/visualizer/register_tools. The loader reads these
# AFTER calling build_tools().
TOOL_SCHEMAS: dict = {}
DESTRUCTIVE: set = set()


def build_tools(*, user_id: str = "", session_id: str = "", agent_id: str = "",
                agent_template_id: str = "", enabled_providers=None, **_ctx):
    """Return {tool_name: handler} for the visualizer page tools.

    Lazily imports the registrar + adapter so scanning FEATURE stays cheap. Runs
    app/visualizer/register_tools against a throwaway dict, then extracts the
    handlers and (re)populates module-level TOOL_SCHEMAS/DESTRUCTIVE from each
    ToolInfo's parameters / destructive / requires_confirmation flags.
    """
    try:
        from app.visualizer import register_tools
        from plugins.admin.adapter import extract_injected
    except ImportError:
        # app/visualizer/ not available — visual rendering disabled.
        return {}

    # Pass agent_id through to the registrar so render_visual / create_genui /
    # edit_genui record this agent as the genui's owner (extract_injected calls
    # register_tools(staging, user_id, agent_id)).
    handlers, schemas, destructive = extract_injected(register_tools, user_id, agent_id)

    # Advanced recipes are retrieved only when needed. Keeping them out of the
    # loaded skill preserves working context for the design task itself.
    _references = {
        "contract": """Gen UI is a complete HTML document mounted in a shadow root. Use the documented mount(root, api) handshake (or WebagentGenui.register), query only through root, and keep listeners/timers/media cleanup scoped to the page. Do not depend on parent.postMessage, global document selectors, window-level input listeners, or 100vw/100vh layout.""",
        "style": """Consume inherited app tokens for every color, border, shadow, and font: var(--bg-*), --fg-*, --accent*, --border*, --shadow-*, --font-*. Do not define a local palette or literal status colors. Dark/light follows the inherited tokens. Use one Lucide-style inline SVG icon family for UI chrome; emoji is content only. Use hierarchy, an 8px-ish spacing rhythm, responsive layout, accessible labels, and visible focus states.""",
        "data": """Put editable or displayed records in the Gen UI data bag. Read with api.getData(), render from that state, and update server data with set_genui_data rather than hardcoding records into HTML. For immediate interaction, update local state, re-render, then ask the agent to persist through api.chat. After a data-only update, call refresh_genui when the visible page must reload.""",
        "verification": """After each build or material edit, inspect get_genui_logs for errors and use screenshot_genui with theme=both. For every promised interaction, pass a representative click selector to screenshot_genui and verify the resulting state; fix click misses, console errors, palette violations, overflow, and unreadable light-mode states before claiming completion.""",
        "credentials": """A page may render a credential form only through api.storeCredential(ability, values); secrets go directly to the encrypted vault and never through chat, page data, or an agent-visible value. Call check_credential first to learn whether a connection exists and which fields are allowed. Do not embed passwords or tokens in HTML, JavaScript, or diagnostics.""",
        "embedding": """Assume third-party sites may block raw iframes. Prefer an official embed URL or API-backed representation, give the user an external-open fallback, and never treat an iframe load as proof that an authenticated provider is usable. Use the Browser ability for real external-site interaction.""",
        "chat": """Use the documented Gen UI api chat helpers for in-page chat controls. Store chat configuration in the data bag, not inline scripts, and route user text through api.chat or api.action. Do not invent message-bridge APIs; load the contract reference first if a custom integration is needed.""",
    }

    async def _get_genui_reference(topic: str = "list"):
        """Return one concise Gen UI implementation reference by topic.

        Use this only for a needed advanced pattern; the main Visualizer skill
        carries the normal build contract. `list` returns available topics.
        """
        key = (topic or "list").strip().lower()
        if key == "list":
            return {"topics": sorted(_references), "note": "Request one topic at a time."}
        text = _references.get(key)
        if text is None:
            return {"error": f"Unknown Gen UI reference topic: {topic}",
                    "topics": sorted(_references)}
        return {"topic": key, "reference": text}

    handlers["get_genui_reference"] = _get_genui_reference
    schemas["get_genui_reference"] = {
        "type": "object",
        "properties": {
            "topic": {
                "type": "string",
                "enum": ["list", *sorted(_references)],
                "description": "Advanced Gen UI topic to retrieve only when needed.",
                "default": "list",
            },
        },
        "required": [],
    }

    # ── screenshot_genui ─────────────────────────────────────────────────────
    # Added HERE (not in the register_tools registrar) because it needs the live
    # session_id — the registrar only receives user_id, but the drop-in build_tools
    # contract hands us session_id too. Gives the agent *eyes*: renders a saved
    # genui headlessly the way the app mounts it, saves the shot as a session
    # attachment (user sees it; agent can read it via process_image), and returns
    # width/console signals so the agent can catch a not-full-width / broken build.
    async def _screenshot_genui(slug: str, theme: str = "dark", click=None):
        from app.visualizer.screenshot import screenshot_genui as _shot
        return await _shot(slug=slug, theme=theme, click=click,
                           user_id=user_id, session_id=session_id)

    handlers["screenshot_genui"] = _screenshot_genui
    schemas["screenshot_genui"] = {
        "type": "object",
        "properties": {
            "slug": {
                "type": "string",
                "description": ("Slug of the genui to render and screenshot (e.g. 'home', "
                                "'dashboard'). Use the slug you just rendered to."),
            },
            "theme": {
                "type": "string",
                "enum": ["dark", "light", "both"],
                "description": ("Which theme to render. 'both' returns a dark and a light shot "
                                "so you can confirm the genui is legible in each. Default 'dark'."),
            },
            "click": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional. CSS selector(s) to click INSIDE the genui before the shot, in "
                    "order — so the screenshot captures an INTERACTION's result (a name opening "
                    "a profile panel, a calendar day expanding, a tab switching, a popover). Use "
                    "this to VERIFY clickable features actually work, not just that the page looks "
                    "right at rest. Each selector is matched in the genui's shadow root (e.g. "
                    "'#student-sarah', '.day[data-date=\"2026-06-17\"]', '[data-tab=\"profile\"]'). "
                    "A selector that matches nothing is reported back as a CLICK MISS. Pass one "
                    "selector for a single click or several for a sequence."),
            },
        },
        "required": ["slug"],
    }

    TOOL_SCHEMAS.clear()
    TOOL_SCHEMAS.update(schemas)
    DESTRUCTIVE.clear()
    DESTRUCTIVE.update(destructive)
    # delete_genui removes a saved genui irreversibly → confirm in Ask/Plan mode
    # (matches the gated wiki_delete / cancel_automation). Create / edit / set-data
    # are reversible and run free, like memory.
    DESTRUCTIVE.add("delete_genui")

    return handlers
