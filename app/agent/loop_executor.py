"""
Loop Logic Executor.

Reads `loop_logic` JSON from the agents/agent_templates table and exposes
per-node enable/disable configuration so stream_agent_events can gate
individual steps at runtime.

Two JSON formats are supported for full backward compatibility:

  Flat array (legacy):
    ["user_input", "slash_cmd", ..., "fire_optimizer"]
    → Treated as a UI ordering hint only.  All nodes run.  This is the
      format stored today — existing agents are completely unaffected.

  Object array (new):
    [{"node": "interrupt_chk", "enabled": false},
     {"node": "skill_track",   "enabled": true},
     ...]
    → Per-node config is applied.  Only nodes with enabled=false are
      skipped; everything else continues to run as before.

Nodes that would break the loop if disabled (llm_call, check_continue, …)
are listed in LOCKED_NODES and are always treated as enabled regardless of
what the DB says.
"""

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# ── Canonical node order — mirrors LOOP_NODES in ui/js/loop-diagram.js ────────
DEFAULT_NODE_ORDER: List[str] = [
    # INPUT
    "user_input",
    # PRE-LOOP (chat.py)
    "slash_cmd", "ensure_session", "agent_resolve", "participants", "save_user_msg",
    # CONTEXT (chat.py)
    "load_context", "copy_defaults", "skip_gate", "memory_search", "resolve_attach",
    "attachment_describe", "build_prompt", "build_history",
    # LOOP INIT (stream_agent_events, before while-loop)
    "load_provider", "load_tools", "integration_status", "assemble_msgs",
    # INFERENCE (per-turn while-loop)
    "interrupt_chk", "turn_counter", "permission_chk", "build_tool_defs",
    "llm_call",
    # ROUTING (validate + guard, per tool call)
    "db_persist_asst", "validate_tools", "destructive_chk", "guardrails",
    "contract_chk", "manager_chk", "post_val_chk",
    # EXECUTION (per tool result)
    "execute_tools", "db_persist_tool", "delegation_chk", "skill_track",
    # CONTINUE?
    "check_continue",
    # OUTPUT
    "final_response", "db_persist_final", "memory_save",
]

# Nodes that are structurally required — disabling them would break the loop.
# Even if loop_logic says enabled=false for these, we ignore it.
LOCKED_NODES: frozenset = frozenset({
    "user_input",
    "load_tools",
    "llm_call",
    "execute_tools",
    "check_continue",
    "final_response",
})

# Nodes that have runtime gating implemented in loop.py and/or chat.py.
# UI uses this set to decide which nodes get toggle switches.
GATED_NODES: frozenset = frozenset({
    "interrupt_chk",   # Skip interrupt checks (batch / automated agents)
    "permission_chk",  # Skip turn-limit gate (long-running / headless agents)
    "guardrails",      # Skip destructive-tool confirmation (admin agents)
    "contract_chk",    # Skip the per-agent run contract (mechanical rules)
    "manager_chk",     # Derived mirror of the Manager Loop master switch (not a gate)
    "delegation_chk",  # Skip agent-delegation detection
    "skill_track",     # Skip skill-execution DB writes (lightweight agents)
    "memory_search",   # Skip brain context lookup (lightweight / tool-only agents)
    "memory_save",     # Skip post-chat memory upsert (ephemeral agents)

    "copy_defaults",   # Skip copying default context docs on first use
    "attachment_describe",  # Skip describing images for non-multimodal models
})


# ── NodeConfig ─────────────────────────────────────────────────────────────────

@dataclass
class NodeConfig:
    """Resolved configuration for a single loop node."""
    node: str
    enabled: bool = True
    extra: Dict[str, Any] = field(default_factory=dict)


# ── LoopConfig ─────────────────────────────────────────────────────────────────

class LoopConfig:
    """
    Parsed loop_logic configuration for one agent.

    Usage::

        # Build from a DB agent dict
        cfg = LoopConfig.from_agent(agent)

        # Check whether a step should run
        if cfg.is_enabled("skill_track"):
            await track_skill(...)

    Thread-safe: instances are read-only after construction.
    """

    def __init__(self, loop_logic_raw: Any = None):
        self._nodes: Dict[str, NodeConfig] = {}
        self._parse(loop_logic_raw)

    # ── Parsing ────────────────────────────────────────────────────────────────

    def _parse(self, raw: Any) -> None:
        # Seed defaults: all nodes enabled
        for node in DEFAULT_NODE_ORDER:
            self._nodes[node] = NodeConfig(node=node, enabled=True)

        # Decode if stored as JSON string
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                return  # Keep all-enabled defaults

        if not isinstance(raw, list) or not raw:
            return  # Empty or null → keep defaults (all nodes run)

        # Detect format by inspecting the first element
        if isinstance(raw[0], str):
            # ── Legacy flat-string array ──────────────────────────────────────
            # This is the format stored today.  We treat it as a UI ordering
            # hint only and leave all nodes enabled (preserving existing behavior
            # exactly).
            return

        # ── New object array ──────────────────────────────────────────────────
        for item in raw:
            if not isinstance(item, dict):
                continue
            node_name = item.get("node")
            if not node_name:
                continue

            enabled = bool(item.get("enabled", True))

            # Locked nodes can never be disabled
            if node_name in LOCKED_NODES:
                enabled = True

            self._nodes[node_name] = NodeConfig(
                node=node_name,
                enabled=enabled,
                extra={k: v for k, v in item.items()
                       if k not in ("node", "enabled")},
            )

    # ── Public API ─────────────────────────────────────────────────────────────

    def is_enabled(self, node: str) -> bool:
        """
        Return True if this node should execute.

        Locked nodes always return True.  Unknown nodes (e.g. future nodes not
        yet in DEFAULT_NODE_ORDER) default to True so forward-compat is safe.
        """
        if node in LOCKED_NODES:
            return True
        cfg = self._nodes.get(node)
        if cfg is None:
            return True
        return cfg.enabled

    def disabled_nodes(self) -> List[str]:
        """Return sorted list of node IDs that are currently disabled."""
        return sorted(
            n for n, cfg in self._nodes.items()
            if not cfg.enabled and n not in LOCKED_NODES
        )

    # ── Factories ──────────────────────────────────────────────────────────────

    @classmethod
    def default(cls) -> "LoopConfig":
        """All nodes enabled — equivalent to an agent with no loop_logic set."""
        return cls(None)

    @classmethod
    def from_agent(cls, agent: Optional[Dict[str, Any]]) -> "LoopConfig":
        """
        Create from an agent dict as returned by get_agent_by_id() or
        list_agents_for_user().  Returns a default (all-enabled) config if
        agent is None or has no loop_logic key.

        ``manager_chk`` is a *derived* node: the Manager Loop master switch
        (``metadata['manager'].enabled``) is authoritative, so a disabled master
        switch reports the node as disabled instead of echoing a stale stored
        value.
        """
        if not agent:
            return cls.default()
        cfg = cls(agent.get("loop_logic", "[]"))
        try:
            from app.agent.manager_config import manager_loop_for_agent
            if not manager_loop_for_agent(agent).get("enabled"):
                node = cfg._nodes.get("manager_chk")
                if node is not None:
                    node.enabled = False
        except Exception:
            pass
        return cfg

    # ── Repr ───────────────────────────────────────────────────────────────────

    def __repr__(self) -> str:
        disabled = self.disabled_nodes()
        if not disabled:
            return "LoopConfig(all_enabled=True)"
        return f"LoopConfig(disabled={disabled!r})"
