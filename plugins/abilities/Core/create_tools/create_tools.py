"""Create Tools ability — SELF-CONTAINED drop-in. See app/abilities/__init__.py for the contract.

TIER-1 ability: the actual handler (`create_tool`) lives in core
(app/tools/registry.py). This file declares the ability, lazily wraps that core
handler in build_tools(), and carries the tool's JSON schema verbatim so the
generic loader block can register it without any core wiring.
"""

from __future__ import annotations

from typing import Optional

# Populated inside build_tools() (the schema's `stages` description is built from
# the core VALID_NODE_IDS, so we mirror it from core there to avoid drift). The
# loader reads these AFTER calling build_tools().
TOOL_SCHEMAS: dict = {}
# create_tool MINTS + registers brand-new executable Python at runtime — a
# write/exec action — so it confirm-gates in Ask/Plan mode (populated in
# build_tools below). The core handler's own `destructive` PARAM is about the
# tool being *created*, separate from create_tool itself needing confirmation.
DESTRUCTIVE: set = set()


def build_tools(*, user_id: str = "", session_id: str = "", agent_id: str = "",
                agent_template_id: Optional[str] = None, enabled_providers=None,
                **_ctx) -> dict:
    """Return {tool_name: handler} for the create_tools ability.

    Lazily imports the core handler + valid node ids, closes the wrapper over
    user_id (exactly as the old loader block did), and populates the module-level
    TOOL_SCHEMAS so the generic loader can register the tool.
    """
    from app.tools.registry import create_tool as _builtin_create_tool, VALID_NODE_IDS as _VALID_NODE_IDS

    async def _create_tool_wrapper(name, description, parameters, code, stages, destructive=False, agent_types=None):
        return await _builtin_create_tool(
            name=name,
            description=description,
            parameters=parameters,
            code=code,
            stages=stages,
            destructive=destructive,
            agent_types=agent_types,
            user_id=user_id,
        )

    TOOL_SCHEMAS.clear()
    TOOL_SCHEMAS.update({
        "create_tool": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Tool identifier (e.g. 'check_email')"},
                "description": {"type": "string", "description": "What the tool does (shown to model)"},
                "parameters": {"type": "object", "description": "JSON Schema describing tool inputs"},
                "code": {"type": "string", "description": "Full Python async function code. Must contain an async function with the same name as the tool."},
                "stages": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "REQUIRED. List of loop node IDs where this tool operates. "
                        "Most tools: ['execute_tools']. Memory tools: ['memory_search', 'memory_save']. "
                        f"Valid values: {', '.join(sorted(_VALID_NODE_IDS))}."
                    ),
                },
                "destructive": {
                    "type": "boolean",
                    "description": "True if this tool writes, deletes, or has irreversible side-effects.",
                    "default": False,
                },
                "agent_types": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Agent type names that may use this tool. Empty = all agent types.",
                    "default": [],
                },
            },
            "required": ["name", "description", "parameters", "code", "stages"],
        },
    })
    DESTRUCTIVE.clear()
    DESTRUCTIVE.add("create_tool")  # minting executable code → confirm in Ask/Plan

    return {"create_tool": _create_tool_wrapper}
