"""JSON Schema → MCP inputSchema conversion.

MCP's inputSchema is just JSON Schema with a few minor conventions.
This module maps ToolInfo.parameters (already JSON Schema) onto the shape
MCP expects, normalising a handful of edge cases.
"""
from __future__ import annotations

import copy
from typing import Any, Dict


def tool_to_mcp_schema(name: str, title: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a single tool's parameters dict to an MCP tool descriptor.

    ``params`` is the ``ToolInfo.parameters`` dict — a JSON Schema object with
    ``type: "object"`` at the top and a ``properties`` / ``required`` block.
    """
    schema: Dict[str, Any] = {"type": "object", "properties": {}, "required": []}

    if isinstance(params, dict):
        props = params.get("properties")
        if isinstance(props, dict):
            # Deep-copy so the original loader-side schema is never mutated.
            schema["properties"] = copy.deepcopy(props)
        req = params.get("required")
        if isinstance(req, list):
            schema["required"] = [str(r) for r in req]

    # Ensure every property has at least a placeholder description so the
    # CLI's model has something to read (some schemas omit it).
    for _, prop in schema["properties"].items():
        if isinstance(prop, dict) and "description" not in prop:
            prop["description"] = ""

    return {
        "name": name,
        "title": title or name,
        "description": title or "",
        "inputSchema": schema,
    }


def prune_schema_for_mode(
    schema_list: list, execution_mode: str, destructive_tools: set, ask_tools: set
) -> list:
    """Remove tools the current execution mode forbids.

    - ``auto``  — keep everything.
    - ``ask``   — keep all; destructive ones will be blocked at call time with a
                 clear error telling the model to ask the user for approval.
    - ``plan``  — drop write tools entirely (the model can't call them at all).
    """
    if execution_mode == "auto":
        return schema_list
    if execution_mode == "plan":
        return [s for s in schema_list if s["name"] not in destructive_tools]
    # 'ask' — keep all; the tools/call handler gates destructive calls.
    return schema_list
