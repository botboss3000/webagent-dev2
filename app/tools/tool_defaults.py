"""App-wide *global* per-tool defaults — the second source of truth (below the
per-agent override, above the built-in default) for two tool dimensions:

  - **permission**  — ``auto`` / ``ask`` / ``deny`` (how the runtime gates a call).
  - **visibility**  — ``always`` / ``discoverable`` (how the tool is exposed; this
                      mirrors ``tool_modes`` exposure modes).

These are admin-set DEFAULTS that every agent inherits *unless* that agent has
its own per-tool override. The blob is stored once, under the admin user, as a
single ``tool_defaults`` auth_element (see ``app/admin/integrations.py``) shaped:

    {"tools": {"<tool>": {"permission": "auto|ask|deny",
                          "visibility": "always|discoverable"}, ...}}

Each tool key may carry either or both dimensions; an absent dimension means "no
global default for that dimension", and an absent key means "no global override
at all".

Resolution order (effective value) for BOTH dimensions:

    agent-explicit override  ▸  global default  ▸  built-in default

Permission has **no per-agent override map** today — it is encoded across two
agent columns (``allowed_tools`` = the deny/block list, despite the name, and
``safety_policy.destructive_tools`` = the ask list). So the v1 semantics here are
**union / additive**: a global default can ADD a deny or ask for a tool the agent
never named, but an agent's own lists always win for tools it *does* name, and a
global can never relax (force ``auto`` on) a tool the agent already gated. The
default blob is EMPTY, so nothing changes until an admin sets a value.
This module is intentionally dependency-light (like ``tool_modes``) so it can
be used from the loop, the API layer, and the admin router without import cycles.

"""

from __future__ import annotations

from typing import Any, Dict, Optional

# Valid permission values for a global per-tool default.
PERMISSION_VALUES = ("auto", "ask", "deny")


def _agent_deny_set(agent_record: Optional[Dict[str, Any]]) -> set:
    """The tools the agent has explicitly DENIED (its ``allowed_tools`` block list)."""
    if not agent_record:
        return set()
    raw = agent_record.get("allowed_tools")
    if isinstance(raw, str):
        try:
            import json
            raw = json.loads(raw)
        except Exception:
            raw = []
    if isinstance(raw, list):
        return set(raw)
    return set()


def _agent_ask_set(agent_record: Optional[Dict[str, Any]]) -> set:
    """The tools the agent has explicitly marked ASK
    (``safety_policy.destructive_tools``)."""
    if not agent_record:
        return set()
    raw = agent_record.get("safety_policy") or "{}"
    if isinstance(raw, str):
        try:
            import json
            raw = json.loads(raw)
        except Exception:
            raw = {}
    if not isinstance(raw, dict):
        return set()
    extra = raw.get("destructive_tools") or []
    if isinstance(extra, list):
        return set(extra)
    return set()


def resolve_permission(
    name: str,
    agent_record: Optional[Dict[str, Any]],
    global_defaults: Optional[Dict[str, Dict[str, str]]],
) -> str:
    """Resolve a tool's effective PERMISSION for display.

    Mirrors how the runtime reads deny/ask (union, never subtract):

      - ``deny`` — the agent explicitly denied it (its block list), OR a global
        default denies it.
      - ``ask``  — the agent explicitly marked it ask, OR a global default marks
        it ask (and it isn't already denied above).
      - ``auto`` — otherwise (in neither list).

    The agent's own lists always win for tools it names; globals only ADD.
    """
    agent_deny = _agent_deny_set(agent_record)
    agent_ask = _agent_ask_set(agent_record)
    gd = global_defaults or {}
    g_perm = (gd.get(name) or {}).get("permission")

    # DENY: agent-explicit deny or global deny, without exceptions.
    if name in agent_deny or g_perm == "deny":
        return "deny"

    # ASK: agent-explicit ask, or a global ask.
    if name in agent_ask:
        return "ask"
    if g_perm == "ask":
        return "ask"

    return "auto"
