"""UI Admin ability — SELF-CONTAINED drop-in.

File editing confined to ``ui/`` (.css / .html only) — no shell, python, git, or
restart. Handlers live in the shared admin library (``plugins/admin/ui_tools.py``
via ``inject_ui_tools``, which wraps the source tools behind ``ui_guardrails``);
the shared ``plugins/admin/adapter.extract_injected`` runs the injector against a
throwaway dict and adapts its ToolInfo objects into the (handlers, schemas,
destructive) triple the generic drop-in loader contract wants, so schemas/flags
never drift.

Discovered generically by core (see app/tools/loader.py "Self-contained ability
tools"): build_tools() returns its handlers and the loader reads the
module-level TOOL_SCHEMAS / DESTRUCTIVE populated below AFTER the call.
"""

from __future__ import annotations

# Populated inside build_tools() from the injected ToolInfo objects (the loader
# reads these AFTER calling build_tools, so populating them there keeps them
# from drifting).
TOOL_SCHEMAS: dict = {}
DESTRUCTIVE: set = set()


def build_tools(*, user_id: str = "", session_id: str = "", agent_id: str = "",
                agent_template_id: str = "", enabled_providers=None, **_ctx):
    """Return {tool_name: handler} for the ui-confined file tools.

    The unrestricted Codebase Admin superset wins: when both abilities are on we
    return {} so the agent keeps full access rather than the guardrailed variant.
    The generic loader call does NOT yet pass enabled_providers into build_tools,
    so we accept enabled_providers=None and guard defensively (the Wave-B
    integrator will make the loader pass it).

    Imports stay LAZY so scanning FEATURE stays cheap.
    """
    if enabled_providers and "codebase_admin" in enabled_providers:
        return {}

    from plugins.admin.adapter import extract_injected
    from plugins.admin.ui_tools import inject_ui_tools

    handlers, schemas, destructive = extract_injected(inject_ui_tools, user_id)

    TOOL_SCHEMAS.clear()
    TOOL_SCHEMAS.update(schemas)
    DESTRUCTIVE.clear()
    DESTRUCTIVE.update(destructive)
    return handlers
