"""Shared adapter for the admin abilities (codebase_admin / git_control / ui_admin).

These three abilities are self-contained drop-ins whose handlers live in this
shared library (plugins/admin/). Each one's build_tools() calls an `inject_*`
function that mutates a tools dict with ToolInfo objects (the historical shape).
`extract_injected` runs such an injector against a throwaway dict and adapts the
result into the (handlers, schemas, destructive) triple the generic drop-in
loader contract wants — so the schemas/flags come straight from the ToolInfo
objects and never drift.
"""

from __future__ import annotations

_DEFAULT_SCHEMA = {"type": "object", "properties": {}, "required": []}


def extract_injected(injector, *args):
    """Run `injector(staging, *args)` and return (handlers, schemas, destructive).

    - handlers:   {tool_name: callable}
    - schemas:    {tool_name: json-schema} (from each ToolInfo's parameters)
    - destructive: set of tool names that are destructive OR require confirmation
                   (the generic loader ties requires_confirmation to destructive,
                   so we fold both in to preserve the prior guardrail behavior).
    """
    staging: dict = {}
    injector(staging, *args)

    handlers = {}
    schemas = {}
    destructive = set()
    for name, info in staging.items():
        handlers[name] = getattr(info, "handler", info)
        schemas[name] = getattr(info, "parameters", None) or dict(_DEFAULT_SCHEMA)
        if getattr(info, "destructive", False) or getattr(info, "requires_confirmation", False):
            destructive.add(name)
    return handlers, schemas, destructive
