"""Process-local permission ceiling for isolated contract turns."""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Mapping


PROFILES = {"tool_free", "source_read_only"}
SOURCE_READ_TOOLS = frozenset({
    "read_source", "search_source", "search_comments", "read_directory",
})
_DISABLED_LOOP_NODES = frozenset({
    "memory_search", "memory_save", "copy_defaults",
    "attachment_describe", "contract_chk", "manager_chk", "delegation_chk",
    "skill_track",
})


def active_profile() -> str:
    if os.environ.get("WEBAGENT_CONTRACT_SUBPROCESS") != "1":
        return ""
    profile = os.environ.get("WEBAGENT_CONTRACT_PERMISSION_PROFILE", "").strip().lower()
    return profile if profile in PROFILES else ""


def filter_tools(tools: Mapping[str, Any]) -> Dict[str, Any]:
    profile = active_profile()
    if profile == "tool_free":
        return {}
    if profile == "source_read_only":
        return {name: info for name, info in tools.items() if name in SOURCE_READ_TOOLS}
    return dict(tools)


def clamp_runtime_agent(agent: Mapping[str, Any]) -> Dict[str, Any]:
    """Disable recursive/background lanes without mutating the clone record."""
    if not active_profile():
        return dict(agent)
    runtime = dict(agent)
    raw = runtime.get("loop_logic")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            raw = []
    by_node: Dict[str, Dict[str, Any]] = {}
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and item.get("node"):
                by_node[str(item["node"])] = dict(item)
    for node in _DISABLED_LOOP_NODES:
        by_node[node] = {"node": node, "enabled": False}
    runtime["loop_logic"] = list(by_node.values())

    metadata = runtime.get("metadata")
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (TypeError, ValueError):
            metadata = {}
    metadata = dict(metadata) if isinstance(metadata, dict) else {}
    manager = metadata.get("manager")
    manager = dict(manager) if isinstance(manager, dict) else {}
    manager["enabled"] = False
    manager["contracts"] = {"enabled": False}
    metadata["manager"] = manager
    runtime["metadata"] = metadata
    return runtime
