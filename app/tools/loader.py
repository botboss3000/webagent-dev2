"""
Tool loader for dynamic tool loading from database.

================================================================================
 THIS IS CORE — do NOT add new integrations / providers / capabilities here.
================================================================================
New capabilities are DROP-IN FILES in a plugin folder, auto-discovered at
runtime — never wired into this loader or a central list. See CLAUDE.md
("Core vs. plugins") and docs/claude/production-editions.md.

  • A new OAuth/API integration → a new file in app/integrations/ exposing a
    TOOLS list (copy app/integrations/_TEMPLATE.py). It is gathered by
    inject_integration_tools() automatically.
  • A new event source / channel / connector / secrets vault / encryption
    method / payment processor / scheduler provider → its own file in the
    matching plugin folder with a FEATURE header.

Only edit this file to change CORE loop machinery (the irreducible built-in
tools, ability gating, tool-mode handling) — not to register a capability.
================================================================================
"""
import json
import logging
import os
from dataclasses import dataclass
from typing import Dict, Any, Callable, List, Optional
from app.db import get_app_db, get_db

logger = logging.getLogger(__name__)


# ── Legacy Tier-1 compatibility symbol ────────────────────────────────────────
# Explicit denial is universal: no tool may bypass it in the loader, prompt
# index, provider schema, or execution path.  Keep this empty export temporarily
# for callers compiled against older releases; it is not a permission exception.
TIER_1_ALWAYS_ON = frozenset()

# Anonymous web visitors are deliberately plain-chat callers. Discovery,
# self-editing, attachments, memory/session search, and every ability-backed
# tool are omitted entirely so the model cannot enumerate or activate them.
ANONYMOUS_CHAT_TOOLS = frozenset({"get_time", "get_date", "calculate", "request_agent_login"})


def _is_anonymous_user_id(user_id: Optional[str]) -> bool:
    return bool(user_id) and str(user_id).startswith("anon_")


# ── Built-in tool metadata ────────────────────────────────────────────────────
# Consumed by /admin/tools to provide a merged view of all tools (built-ins +
# user skills) with loop-stage annotations for the visualizer.
#
# stages      — which loop node(s) this tool can fire in
# destructive — writes, deletes, or has irreversible side-effects
# agent_types — which agent type names may use it; [] means all
BUILTIN_TOOL_METADATA: Dict[str, Dict[str, Any]] = {
    # ── Core discovery (always sent, locked) ──
    "load_tool":                     {"stages": ["execute_tools"],                                "destructive": False, "agent_types": []},
    "load_ability":                  {"stages": ["execute_tools"],                                "destructive": False, "agent_types": []},
    "set_execution_mode":            {"stages": ["execute_tools"],                                "destructive": False, "agent_types": []},
    # Plan workspace + chat-component presenters — CORE (Core ▸ Base), not the
    # Visualizer ability: present_plan is required by Plan mode's mode contract.
    "present_plan":                  {"stages": ["execute_tools"],                                "destructive": False, "agent_types": []},
    "present_chat_component":        {"stages": ["execute_tools"],                                "destructive": False, "agent_types": []},
    # Task Progress checkpoint tools (Core ▸ Base). They are non-destructive
    # bookkeeping tools, but still honour explicit per-agent and global deny
    # permissions before they reach the model.
    "update_task_checkpoint":        {"stages": ["execute_tools"],                                "destructive": False, "agent_types": []},
    "get_task_checkpoint":           {"stages": ["execute_tools"],                                "destructive": False, "agent_types": []},
    "run_soft_ability":              {"stages": ["guardrails", "execute_tools"],                 "destructive": True, "requires_confirmation": True, "agent_types": []},
    # ── Web & browser ──
    "web_search":                    {"stages": ["execute_tools"],                                "destructive": False, "agent_types": []},
    "browser_action":                {"stages": ["execute_tools"],                                "destructive": False, "agent_types": []},
    "http_request":                  {"stages": ["execute_tools"],                                "destructive": False, "agent_types": []},
    "vault_login":                   {"stages": ["execute_tools"],                                "destructive": False, "agent_types": []},
    "maps_geocode":                  {"stages": ["execute_tools"],                                "destructive": False, "agent_types": []},
    # ── Image generation (gated by image_generation) ──
    "generate_image":                {"stages": ["execute_tools"],                                "destructive": False, "agent_types": []},
    # ── DB & context (gated by codebase_admin) ──
    "db_query":                      {"stages": ["execute_tools"],                                "destructive": True,  "agent_types": []},
    "checkout_files":                 {"stages": ["execute_tools"],                                "destructive": False, "agent_types": []},
    "checkin_files":                  {"stages": ["execute_tools"],                                "destructive": False, "agent_types": []},
    "abandon_file_checkouts":         {"stages": ["execute_tools"],                                "destructive": False, "agent_types": []},
    "file_checkout_status":           {"stages": ["execute_tools"],                                "destructive": False, "agent_types": []},
    # ── Diagnostics (gated by the diagnostics ability) ──
    "read_diagnostics":              {"stages": ["execute_tools"],                                "destructive": False, "agent_types": []},
    # ── Agent management (gated by the agent_management ability) — in-process,
    #    user-scoped agent CRUD. Reads are non-destructive; writes mutate the
    #    user's own agents only (ownership enforced in the tool/DB layer). ──
    "list_agent_templates":          {"stages": ["execute_tools"],                                "destructive": False, "agent_types": []},
    "list_my_agents":                {"stages": ["execute_tools"],                                "destructive": False, "agent_types": []},
    "get_agent":                     {"stages": ["execute_tools"],                                "destructive": False, "agent_types": []},
    "list_agent_tools":              {"stages": ["execute_tools"],                                "destructive": False, "agent_types": []},
    "create_agent":                  {"stages": ["execute_tools"],                                "destructive": True,  "agent_types": []},
    "update_agent":                  {"stages": ["execute_tools"],                                "destructive": True,  "agent_types": []},
    "set_agent_tool":                {"stages": ["execute_tools"],                                "destructive": True,  "agent_types": []},
    "edit_agent_prompt":             {"stages": ["execute_tools"],                                "destructive": True,  "agent_types": []},
    "set_agent_ability":             {"stages": ["execute_tools"],                                "destructive": True,  "agent_types": []},
    "manage_agent_skills":           {"stages": ["execute_tools"],                                "destructive": True,  "agent_types": []},
    # ── Memory ──
    "memory":                        {"stages": ["memory_search", "memory_save", "execute_tools"], "destructive": False, "agent_types": []},
    "session_search":                {"stages": ["load_context", "execute_tools"],               "destructive": False, "agent_types": []},
    "similar_sessions":              {"stages": ["load_context", "execute_tools"],               "destructive": False, "agent_types": []},
    # ── Self-prompt (Core ▸ Base — an agent reading/improving its OWN prompt) ──
    "read_own_prompt":               {"stages": ["execute_tools"],                               "destructive": False, "agent_types": []},
    "edit_own_prompt":               {"stages": ["execute_tools"],                               "destructive": True,  "agent_types": []},
    # ── Self-skills (Core ▸ Base — an agent teaching itself reusable how-to) ──
    "save_own_skill":                {"stages": ["execute_tools"],                               "destructive": True,  "agent_types": []},
    "remove_own_skill":              {"stages": ["execute_tools"],                               "destructive": True,  "agent_types": []},
    # ── Utilities ──
    "get_time":                      {"stages": ["execute_tools"],                               "destructive": False, "agent_types": []},
    "get_date":                      {"stages": ["execute_tools"],                               "destructive": False, "agent_types": []},
    "get_weather":                   {"stages": ["execute_tools"],                               "destructive": False, "agent_types": []},
    "calculate":                     {"stages": ["execute_tools"],                               "destructive": False, "agent_types": []},
    "read_attachment":               {"stages": ["load_context", "execute_tools"],               "destructive": False, "agent_types": []},
    # ── Tool creation ──
    "create_tool":                   {"stages": ["execute_tools"],                               "destructive": True,  "agent_types": []},
    "rate_skill":                    {"stages": ["execute_tools"],                               "destructive": False, "agent_types": []},
    # ── Webhooks ──
    "register_webhook":              {"stages": ["execute_tools"],                               "destructive": False, "agent_types": []},
    "list_webhooks":                 {"stages": ["execute_tools"],                               "destructive": False, "agent_types": []},
    "delete_webhook":                {"stages": ["execute_tools"],                               "destructive": True,  "agent_types": []},
    "get_webhook_log":               {"stages": ["execute_tools"],                               "destructive": False, "agent_types": []},
    # ── Optimizer ──
    "run_optimizer":                 {"stages": ["user_input", "execute_tools"],                 "destructive": False, "agent_types": ["default"]},
    "run_worker_trials":             {"stages": ["opt_validate"],                                "destructive": False, "agent_types": ["optimizer"]},
    "deploy_optimization":           {"stages": ["opt_apply"],                                   "destructive": True,  "agent_types": ["optimizer"]},
    # ── Delegation ──
    "delegate_to_agent":             {"stages": ["execute_tools"],                                "destructive": False, "agent_types": []},
    "list_delegatable_agents":       {"stages": ["execute_tools"],                                "destructive": False, "agent_types": []},
    # ── Terminal control (gated by terminal_control) — open/drive interactive
    #    terminal programs; open/send/close write, so they pass guardrails ──
    "terminal_open":                 {"stages": ["guardrails", "execute_tools"],                 "destructive": True,  "requires_confirmation": False, "agent_types": []},
    "terminal_read":                 {"stages": ["execute_tools"],                                "destructive": False, "agent_types": []},
    "terminal_send":                 {"stages": ["guardrails", "execute_tools"],                 "destructive": True,  "requires_confirmation": False, "agent_types": []},
    "terminal_wait":                 {"stages": ["execute_tools"],                                "destructive": False, "agent_types": []},
    "terminal_list":                 {"stages": ["execute_tools"],                                "destructive": False, "agent_types": []},
    "terminal_close":                {"stages": ["guardrails", "execute_tools"],                 "destructive": True,  "requires_confirmation": False, "agent_types": []},
    # ── App control (gated by app_control) — rearrange the viewer's own screen,
    #    writes no data ──
    "set_app_view":                  {"stages": ["execute_tools"],                                "destructive": False, "agent_types": []},
    # ── Admin/source (privileged) — write/exec tools pass through guardrails ──
    "read_source":                   {"stages": ["execute_tools"],                               "destructive": False, "requires_confirmation": False, "agent_types": ["admin"]},
    "write_source":                  {"stages": ["guardrails", "execute_tools"],                 "destructive": True,  "requires_confirmation": False, "agent_types": ["admin"]},
    "edit_source":                   {"stages": ["guardrails", "execute_tools"],                 "destructive": True,  "requires_confirmation": False, "agent_types": ["admin"]},
    "delete_source":                 {"stages": ["guardrails", "execute_tools"],                 "destructive": True,  "requires_confirmation": False, "agent_types": ["admin"]},
    "resolve_conflict":              {"stages": ["guardrails", "execute_tools"],                 "destructive": True,  "requires_confirmation": False, "agent_types": ["admin"]},
    "commit_and_push":               {"stages": ["guardrails", "execute_tools"],                 "destructive": True,  "requires_confirmation": True,  "agent_types": ["admin"]},
    "run_command":                   {"stages": ["guardrails", "execute_tools"],                 "destructive": True,  "requires_confirmation": True,  "agent_types": ["admin"]},
    "restart_server":                {"stages": ["guardrails", "execute_tools"],                 "destructive": True,  "requires_confirmation": False, "agent_types": ["admin"]},
    # ── Auth / comms ──
    "register_user":                 {"stages": ["execute_tools"],                               "destructive": False, "agent_types": []},
    # ── OAuth integration ──
    "check_oauth_connection":        {"stages": ["execute_tools"],                               "destructive": False, "agent_types": []},
}


# ── Ability → built-in tools map ──────────────────────────────────────────────
# The authoritative reverse of the per-ability injection blocks in
# _inject_builtin_tools(). Drives (a) seeding "discoverable" tool modes the
# moment an ability is toggled on for an agent, and (b) labelling each tool with
# its ability in the agent Tools panel. Tools NOT listed here (always-on
# utilities, DB/custom tools, OAuth-integration tools) carry no ability and
# default to the "always" mode.
#
# DROP-IN — this map is now BUILT from the ability files in plugins/abilities/
# (each declares the tool names it gates). Do NOT add an ability here: drop a
# file in plugins/abilities/ instead. The literal below is only a fail-safe used
# if that scan is unavailable. See app/abilities/__init__.py and CLAUDE.md.
_FALLBACK_ABILITY_TOOLS: Dict[str, List[str]] = {
    "web_access":          ["web_search", "maps_geocode", "get_weather"],
    "diagnostics":         ["read_diagnostics"],
    "agent_management":    ["list_agent_templates", "list_my_agents", "get_agent",
                            "list_agent_tools", "create_agent", "update_agent",
                            "set_agent_tool", "edit_agent_prompt",
                            "set_agent_ability", "manage_agent_skills"],
    "browser_control":     ["browser_action", "http_request"],
    "image_generation":    ["generate_image"],
    "codebase_admin":      ["db_query", "read_source", "checkout_files", "checkin_files",
                            "abandon_file_checkouts", "file_checkout_status",
                            "write_source", "edit_source",
                            "delete_source", "resolve_conflict", "commit_and_push",
                            "run_command", "restart_server"],
    "create_tools":        ["create_tool"],
    "agent_orchestration": ["run_optimizer", "delegate_to_agent",
                            "list_delegatable_agents"],
    "automation":          ["list_event_sources", "list_delivery_channels",
                            "event_subscribe"],
    "terminal_control":    ["terminal_open", "terminal_read", "terminal_send",
                            "terminal_wait", "terminal_list", "terminal_close"],
    "app_control":         ["set_app_view"],
    "wiki_control":        ["wiki_search", "wiki_list", "wiki_get",
                            "wiki_create", "wiki_update", "wiki_set_status",
                            "wiki_delete", "wiki_history", "wiki_get_revision",
                            "wiki_restore", "wiki_backlinks"],
}

def _build_ability_tools() -> Dict[str, List[str]]:
    """Build the ability→tools map from plugins/abilities/, with a fail-safe."""
    merged: Dict[str, List[str]] = {}
    try:
        from app.abilities import tools_map
        merged.update(tools_map())
    except Exception as e:
        logger.warning("Ability scan unavailable (%s); using fallback ability map", e)
        merged.update(_FALLBACK_ABILITY_TOOLS)
    if not merged:  # scan present but empty — don't strand every ability
        merged.update(_FALLBACK_ABILITY_TOOLS)
    return merged


ABILITY_TOOLS: Dict[str, List[str]] = _build_ability_tools()


def _merge_integration_metadata() -> None:
    """Pull tool metadata from `app/integrations/*` subpackages into BUILTIN_TOOL_METADATA.

    Runs at import time so /admin/tools and the loop diagram see Gmail / Calendar /
    Drive tools alongside the rest. Deleting `app/integrations/` makes this no-op.
    """
    try:
        from app.integrations import integration_tool_metadata
        BUILTIN_TOOL_METADATA.update(integration_tool_metadata())
    except Exception as e:
        logger.warning("could not merge integration tool metadata: %s", e)


_merge_integration_metadata()


def _merge_ability_tool_metadata() -> None:
    """Pull per-tool loop metadata from `plugins/abilities/*` descriptors into
    BUILTIN_TOOL_METADATA, so a dropped-in ability's tools appear in /admin/tools
    and the loop visualizer with no edit here. Explicit `tool_metadata` blocks in
    an ability's .json override the legacy literals above; tools with no entry
    anywhere get safe defaults (execute_tools stage, non-destructive)."""
    try:
        from app.abilities import tool_metadata
        for name, meta in tool_metadata().items():
            if meta.pop("_explicit", False) or name not in BUILTIN_TOOL_METADATA:
                BUILTIN_TOOL_METADATA[name] = meta
    except Exception as e:
        logger.warning("could not merge ability tool metadata: %s", e)


_merge_ability_tool_metadata()


def _get_webhook_base_url() -> str:
    """Get the configured public webhook base URL from the plugin registry."""
    try:
        from app.communications.manager import get_plugin_manager
        pm = get_plugin_manager()
        registry = getattr(pm, "_registry", {})
        return registry.get("webhook_base_url", "http://localhost:8080")
    except Exception:
        return "http://localhost:8080"


@dataclass
class ToolInfo:
    """Enriched tool descriptor returned by load_tools()."""
    name: str
    handler: Callable
    parameters: dict
    tool_id: str = ''
    requires_confirmation: bool = False  # True → treated as destructive (guardrail check)
    destructive: bool = False  # True → tool writes, deletes, or has side-effects


class ToolLoader:
    """Load tools dynamically from the database and compile them into Python functions."""

    def __init__(self):
        # Resolve the scoped database at call time. Import-time resolution used
        # to silently bind the process-global loader to the admin user store.
        self._client = None

    async def load_tools(self, user_id: str, agent_id: str = "", agent_template_id: Optional[str] = None, session_id: str = "", gate_caller_access: bool = False, turn_id: str = "") -> Dict[str, 'ToolInfo']:
        """
        Load all active tools for a user from the tools table.
        Each tool's `code` field contains the full async function to execute.

        Args:
            user_id: The user ID to load tools for
            agent_template_id: Active agent template id.

        Returns:
            Dictionary mapping tool names to ToolInfo objects
        """
        tools: Dict[str, ToolInfo] = {}

        # Load all active tools for this user
        rows = await self._fetch_user_tools(user_id)
        for row in rows:
            name = row['name']
            handler = self._make_handler(row, user_id)
            params = row.get('parameters', {"type": "object", "properties": {}, "required": []})
            if isinstance(params, str):
                try:
                    params = json.loads(params)
                except (json.JSONDecodeError, TypeError):
                    params = {"type": "object", "properties": {}, "required": []}
            # Ensure params is a proper JSON Schema object
            if not isinstance(params, dict) or params.get("type") != "object":
                params = {"type": "object", "properties": {}, "required": []}
            tools[name] = ToolInfo(
                name=name,
                handler=handler,
                parameters=params,
                tool_id=row.get('id', ''),
                requires_confirmation=bool(row.get('requires_confirmation', 0)),
            )
            logger.debug(f"Loaded tool {name} for user {user_id}")

        # ── Resolve enabled+admin-configured integrations for this agent ──
        # Computed once so the same set gates both the integration tools and
        # the built-in OAuth helpers (check_oauth_connection).
        enabled_providers: set = set()
        try:
            from app.integrations import gather_enabled_providers
            enabled_providers = await gather_enabled_providers(agent_id, user_id) if agent_id else set()
        except ImportError:
            pass
        except Exception as e:
            logger.warning("Could not gather enabled providers for %s: %s", agent_id, e)

        # A discoverable ability becomes callable on the next loop iteration
        # after load_ability persists it on the session. Include that durable
        # activation here, not only the agent's static provider list; otherwise
        # the prompt advertises the ability while its handlers are absent.
        if session_id:
            try:
                from app.db import get_db as _active_db
                enabled_providers |= set(
                    await _active_db().get_session_active_abilities(session_id)
                )
            except Exception as e:
                logger.debug("Could not read active abilities for %s: %s", session_id[:12], e)

        # ── Edition gate ──
        # In a non-`full` build, drop abilities the active edition excludes (by
        # maturity). This is the single chokepoint that gates every built-in
        # ability's tools (web_access, browser_control, …). No-op for `full`;
        # unknown keys (integration provider connection-types) are kept as-is.
        try:
            from app.features.gating import ability_enabled
            enabled_providers = {p for p in enabled_providers if ability_enabled(p)}
        except Exception:
            pass

        # ── Caller-access gate (runtime only) ──
        # Drop any ability whose per-agent "Available to" level (everyone /
        # registered / admin) the LIVE caller (`user_id`) doesn't meet, so its
        # tools are never materialized for that caller — a real security boundary,
        # not a prompt hint. Default everyone = no-op; fail-open. The matching
        # ability skills are stripped from the prompt in append_skills_section.
        # OPT-IN (`gate_caller_access`): the real chat/run paths pass True so
        # `user_id` is the live chatter; the config/preview endpoints (Tools panel,
        # schema preview) leave it False so they always show the agent's full
        # configured set regardless of who is viewing.
        caller_entitlement_groups: set[str] | None = None
        if gate_caller_access:
            try:
                from app.agent.ability_access import filter_abilities_for_caller
                enabled_providers = await filter_abilities_for_caller(agent_id, enabled_providers, user_id)
                from app.entitlements.service import resolve_capabilities
                _caps = await resolve_capabilities(user_id)
                caller_entitlement_groups = set(_caps.get("ability_groups") or [])
            except Exception as exc:
                # Entitlement resolution is an authorization boundary. Preserve
                # only the reviewed minimal core if the shared filter fails.
                from app.entitlements.abilities import filter_abilities_by_groups
                logger.warning("Ability entitlement filter failed; restricting to chat_core: %s", exc)
                enabled_providers = filter_abilities_by_groups(
                    set(enabled_providers), {"chat_core"},
                )
                caller_entitlement_groups = {"chat_core"}

            # Database-authored executable tools are the output of the
            # tool-creation capability. They are not attached to a plugin
            # descriptor, so enforce that group explicitly at their shared
            # materialization boundary.
            if "tool_creation" not in caller_entitlement_groups:
                tools = {name: info for name, info in tools.items() if not info.tool_id}

        # ── Inject built-in tools (override any DB versions) ──
        self._inject_builtin_tools(
            tools, user_id,
            agent_id=agent_id,
            agent_template_id=agent_template_id,
            enabled_providers=enabled_providers,
            session_id=session_id,
        )

        # ── Inject OAuth-integration tools (Gmail, Calendar, Drive, …) ──
        try:
            from app.integrations import inject_integration_tools
            inject_integration_tools(tools, user_id, agent_id, enabled_providers=enabled_providers, tool_info_cls=ToolInfo)
        except ImportError:
            pass  # app/integrations/ deleted — feature off.
        except Exception as e:
            logger.warning("integration tool injection failed for agent %s: %s", agent_id, e)

        return tools

    def _inject_builtin_tools(self, tools: Dict[str, ToolInfo], user_id: str, agent_id: str = "", agent_template_id: Optional[str] = None, enabled_providers: Optional[set] = None, session_id: str = "") -> None:
        """Inject built-in tools that are always available regardless of DB state.

        `enabled_providers` is the set of integration connection_types enabled
        for this agent AND admin-configured. It gates the OAuth helper tools
        (check_oauth_connection) so the LLM never sees provider names it has
        no permission to use.

        ⚠ DROP-IN POLICY — do NOT add a new `if "<ability>" in enabled_providers:`
        block here to wire a new ability's tools. The per-ability blocks below
        (terminal_control, app_control, wiki_control, …) are LEGACY core-wired
        abilities. A NEW tool-bearing ability ships its own handlers in its plugin
        file via a `build_tools()` hook and is injected automatically by the ONE
        generic block further down (search "Self-contained ability tools"). Drop a
        file in plugins/abilities/ — wire nothing here. See CLAUDE.md "Core vs.
        plugins" and plugins/abilities/_TEMPLATE.py ("TWO FLAVOURS OF ABILITY").
        """
        if enabled_providers is None:
            enabled_providers = set()

        # ── Orchestration gating ─────────────────────────────────────────────
        # `_is_opt_agent`   → this run IS the Optimizer agent.
        #                     Only the Optimizer gets the internal optimizer-pipeline
        #                     tools (run_worker_trials / deploy_optimization).
        #                     No normal agent ever sees them — otherwise agents
        #                     (e.g. admin) discover them via list_tools/search_tools
        #                     and loop on them.
        _is_opt_agent = agent_template_id == "optimizer"

        # create_tool (create_tools ability) → moved to plugins/abilities/create_tools.py build_tools.

        # ── rate_skill (record user feedback on tool executions) ──
        async def _run_soft_ability_wrapper(ability_id: str, inputs: Optional[dict] = None):
            from app.abilities.soft_workflows import execute_soft_ability
            return await execute_soft_ability(
                ability_id=ability_id, inputs=inputs or {}, user_id=user_id,
                agent_id=agent_id, session_id=session_id,
            )

        tools["run_soft_ability"] = ToolInfo(
            name="run_soft_ability",
            handler=_run_soft_ability_wrapper,
            requires_confirmation=True,
            destructive=True,
            parameters={
                "type": "object",
                "properties": {
                    "ability_id": {"type": "string", "description": "Custom ability id or slug to execute."},
                    "inputs": {"type": "object", "description": "Named inputs referenced as ${inputs.name}."},
                },
                "required": ["ability_id"],
            },
        )

        async def _rate_skill_wrapper(skill_name: str, feedback_type: str, message: Optional[str] = None):
            db = get_db()
            skill_id = await db.skill_get_id_by_name(user_id, skill_name)
            if not skill_id:
                return json.dumps({"status": "error", "message": f"Skill '{skill_name}' not found"})
            fid = await db.skill_add_feedback(
                skill_id=skill_id, user_id=user_id,
                feedback_type=feedback_type, message=message,
            )
            return json.dumps({"status": "ok", "feedback_id": fid})

        tools["rate_skill"] = ToolInfo(
            name="rate_skill",
            handler=_rate_skill_wrapper,
            parameters={
                "type": "object",
                "properties": {
                    "skill_name": {"type": "string", "description": "Name of the skill/tool to rate"},
                    "feedback_type": {
                        "type": "string",
                        "enum": ["positive", "negative", "correction"],
                        "description": "positive = user liked it, negative = user disliked it, correction = user provided a fix"
                    },
                    "message": {"type": "string", "description": "Optional user comment or correction"},
                },
                "required": ["skill_name", "feedback_type"],
            },
        )

        # run_optimizer (trigger interactive optimizer session) → moved to
        # plugins/abilities/Core/agent_orchestration/agent_orchestration.py
        # build_tools (gated by the agent_orchestration ability like the rest
        # of the orchestration toolset).

        # ── read_attachment (always available) ──
        from app.tools.read_attachment import read_attachment as _builtin_read_attachment, TOOL_DEFINITION as _ATTACH_TOOL_DEF
        tools["read_attachment"] = ToolInfo(
            name="read_attachment",
            handler=_builtin_read_attachment,
            parameters=_ATTACH_TOOL_DEF["parameters"],
        )

        async def _request_agent_login(mode: str = "login"):
            """Ask the client to render a non-transcript authentication card."""
            from app.agent.profiles import auth_policy
            policy = await auth_policy(agent_id)
            return json.dumps({
                "status": "ok", "ui": "agent_auth", "agent_id": agent_id,
                "mode": mode if mode in {"login", "register", "app"} else "login",
                "app_login_enabled": policy["app_login_enabled"],
                "local_signup_mode": policy["local_signup_mode"],
            })

        tools["request_agent_login"] = ToolInfo(
            name="request_agent_login",
            handler=_request_agent_login,
            parameters={"type": "object", "properties": {
                "mode": {"type": "string", "enum": ["login", "register", "app"],
                         "description": "Authentication view to show in a secure chat card."}
            }},
        )

        async def _manage_agent_profiles(action: str = "list", profile: Optional[dict] = None,
                                           member_id: str = "", profile_slug: str = "",
                                           enabled: Optional[bool] = None,
                                           auth_updates: Optional[dict] = None,
                                           max_uses: int = 1, expires_at: str = ""):
            from app.agent import profiles as agent_profiles
            principal = await agent_profiles.resolve_member(agent_id, user_id, auto_link_app=False)
            if not principal or not principal.get("is_agent_admin"):
                return json.dumps({"status": "error", "message": "Agent administrator access required"})
            if action == "list":
                return json.dumps({
                    "status": "ok", "ui": "agent_profile_admin", "agent_id": agent_id,
                    "profiles": await agent_profiles.list_profiles(agent_id),
                    "members": [agent_profiles.safe_principal(m) for m in await agent_profiles.list_members(agent_id)],
                    "auth_policy": await agent_profiles.auth_policy(agent_id),
                })
            if action == "save_profile" and profile:
                row = await agent_profiles.upsert_profile(
                    agent_id, slug=str(profile.get("slug") or ""),
                    name=str(profile.get("name") or ""),
                    description=str(profile.get("description") or ""),
                    policy=profile.get("policy") if isinstance(profile.get("policy"), dict) else {},
                )
                return json.dumps({"status": "ok", "profile": row})
            if action == "assign_profile":
                row = await agent_profiles.assign_profile(agent_id, member_id, profile_slug)
                return json.dumps({"status": "ok", "principal": agent_profiles.safe_principal(row)})
            if action == "set_administrator" and enabled is not None:
                row = await agent_profiles.set_member_admin(agent_id, member_id, enabled)
                return json.dumps({"status": "ok", "principal": agent_profiles.safe_principal(row)})
            if action == "auth_policy" and auth_updates is not None:
                row = await agent_profiles.update_auth_policy(agent_id, auth_updates)
                return json.dumps({"status": "ok", "auth_policy": row})
            if action == "create_invite":
                row = await agent_profiles.create_invite(
                    agent_id, profile_slug or agent_profiles.MEMBER, max_uses, expires_at or None,
                )
                return json.dumps({"status": "ok", **row})
            return json.dumps({"status": "error", "message": "Invalid profile administration action"})

        tools["manage_agent_profiles"] = ToolInfo(
            name="manage_agent_profiles", handler=_manage_agent_profiles,
            requires_confirmation=True, destructive=True,
            parameters={"type": "object", "properties": {
                "action": {"type": "string", "enum": ["list", "save_profile", "assign_profile", "set_administrator", "auth_policy", "create_invite"]},
                "profile": {"type": "object"}, "member_id": {"type": "string"},
                "profile_slug": {"type": "string"}, "enabled": {"type": "boolean"},
                "auth_updates": {"type": "object"},
                "max_uses": {"type": "integer", "minimum": 1},
                "expires_at": {"type": "string"},
            }, "required": ["action"]},
        )

        # ── Communication plugin tools (Telegram, WhatsApp, etc.) ──
        # Only inject a channel's outbound tools when that channel is actually
        # active for THIS agent — `enabled_providers` already encodes both gates
        # (app admin enabled the channel ∩ the agent has the connection on). A
        # disabled/coming-soon channel therefore injects nothing, so a stray bot
        # token can't expose send_telegram_message and have a call raise
        # "bot token not configured" — the source of the spurious Telegram errors.
        try:
            from app.communications.manager import get_plugin_manager
            pm = get_plugin_manager()
            for tool_def in pm.get_all_tools():
                _name = tool_def["name"]
                if _name in tools:
                    continue
                _plugin = next(
                    (p for p in pm.get_enabled_plugins()
                     if any(t["name"] == _name for t in p.get_tools())),
                    None
                )
                if _plugin is None:
                    continue
                # Channel must be active for this agent (admin-enabled + connected).
                if _plugin.name not in enabled_providers:
                    continue
                # Closure capture by value via default args
                def _build_handler(_p, _n):
                    async def _handler(**kw):
                        text = kw.get("text", "")
                        recipient = kw.get("chat_id") or kw.get("to") or kw.get("phone")
                        if not recipient:
                            return json.dumps({"error": f"missing recipient for {_n}"})
                        return await _p.send_message(str(recipient), text)
                    return _handler
                tools[_name] = ToolInfo(
                    name=_name,
                    handler=_build_handler(_plugin, _name),
                    parameters=tool_def["parameters"],
                )
        except Exception as e:
            logger.warning("Failed to inject communication plugin tools: %s", e)

        # ── register_user (auth tool, always available) ──
        async def _register_user_wrapper(name: str, source_channel: str = ""):
            """Register a user who just verified their channel identity."""
            import json
            from app.communications.auth import (
                get_identity, upgrade_to_verified,
                find_user_by_display_name, migrate_anonymous_to_user,
            )

            chan, ext_id = None, None
            if ":" in user_id:
                chan, ext_id = user_id.split(":", 1)
            else:
                try:
                    _raw = get_db().get_raw_client()
                    resp = (
                        _raw.table("channel_identities")
                        .select("channel, external_id")
                        .eq("user_id", user_id)
                        .limit(1)
                        .execute()
                    )
                    rows = resp.data if hasattr(resp, 'data') else (resp or [])
                    if rows:
                        chan, ext_id = rows[0]["channel"], rows[0]["external_id"]
                except Exception:
                    pass

            if not chan or not ext_id:
                return json.dumps({"error": "cannot determine channel identity"})

            identity = await get_identity(chan, ext_id)
            if identity is None:
                return json.dumps({"error": "identity not found"})

            if identity.user_tier == "full":
                return json.dumps({"status": "ok", "message": "Already fully registered."})

            existing_uid = await find_user_by_display_name(name)
            if existing_uid and existing_uid != user_id:
                moved = await migrate_anonymous_to_user(user_id, existing_uid)
                identity = await upgrade_to_verified(identity, display_name=name)
                return json.dumps({
                    "status": "ok",
                    "user_tier": identity.user_tier,
                    "display_name": identity.display_name,
                    "migrated_to": existing_uid,
                    "interactions_moved": moved,
                    "message": f"Welcome back, {name}! Your messages have been linked to your existing account.",
                })

            identity = await upgrade_to_verified(identity, display_name=name)
            return json.dumps({
                "status": "ok",
                "user_tier": identity.user_tier,
                "display_name": identity.display_name,
                "message": f"User registered as {name} on {chan}.",
            })

        tools["register_user"] = ToolInfo(
            name="register_user",
            handler=_register_user_wrapper,
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "User's display name"},
                    "source_channel": {"type": "string", "description": "Which channel they came from (telegram, whatsapp, etc.)"},
                },
                "required": ["name"],
            },
        )

        # codebase_admin source suite → moved to plugins/abilities/codebase_admin.py build_tools.
        # git_control → moved to plugins/abilities/git_control.py build_tools.
        # ui_admin → moved to plugins/abilities/ui_admin.py build_tools (uses enabled_providers).
        # visualizer → moved to plugins/abilities/visualizer.py build_tools.

        # delegation tools (delegate_to_agent / list_delegatable_agents) →
        #   folded into plugins/abilities/Core/agent_orchestration.py build_tools
        #   (former app/tools/delegation.py). They are now injected by the generic
        #   self-contained discovery block below when the "agent_orchestration"
        #   ability is enabled — no special wiring here anymore.
        # terminal_control → moved to plugins/abilities/terminal_control.py build_tools.
        # app_control → moved to plugins/abilities/app_control.py build_tools.
        # wiki_control → self-contained in plugins/abilities/Memory/wiki_context.py build_tools.

        # ── Self-contained ability tools (generic drop-in discovery) ──────────
        # The blocks above wire specific abilities whose handlers live in core.
        # This ONE generic block covers the newer contract: an ability that ships
        # its OWN handlers in its plugin file (plugins/abilities/<id>.py) via a
        # module-level build_tools() hook — exactly like an integration carries
        # its TOOLS. For every ENABLED such ability we call its build_tools and
        # inject what it returns. Adding a tool-bearing ability therefore needs NO
        # edit here: drop the plugin file in and it is discovered. The ability
        # owns its own gating (it may return {} to inject nothing this call).
        try:
            from app import abilities as _abilities_mgr
            # A locked-on ability (e.g. context_control) is "always active" by
            # definition, so its tools must load even when the agent has no
            # explicit agent_connections row enabling it — mirroring how
            # turn_hooks_for_agent / context_strategy_for_agent union locked-on
            # abilities. Without this, compact_context (the only context_control
            # tool) silently never loads for agents that rely on the locked-on
            # default, and the agent correctly reports it cannot self-compact.
            _locked_on_abilities: set = set()
            try:
                _cat = _abilities_mgr._load()
                _locked_on_abilities = {
                    _aid for _aid, _feat in (_cat or {}).items()
                    if isinstance(_feat, dict) and _feat.get("locked_on")
                }
            except Exception:
                _locked_on_abilities = set()
            for _ab_id in (set(enabled_providers or ()) | _locked_on_abilities):
                _ab_mod = _abilities_mgr.ability_module(_ab_id)
                _build = getattr(_ab_mod, "build_tools", None) if _ab_mod else None
                if not callable(_build):
                    continue
                try:
                    _built = _build(
                        user_id=user_id, session_id=session_id,
                        agent_id=agent_id or "", agent_template_id=agent_template_id,
                        enabled_providers=enabled_providers,
                    ) or {}
                except Exception as _abe:
                    logger.warning("Ability %s build_tools failed: %s", _ab_id, _abe)
                    continue
                _ab_schemas = getattr(_ab_mod, "TOOL_SCHEMAS", {}) or {}
                _ab_destr = set(getattr(_ab_mod, "DESTRUCTIVE", ()) or ())
                for _abname, _abhandler in _built.items():
                    _abd = _abname in _ab_destr
                    # Drop-in abilities may keep a tool destructive (so Ask/Plan
                    # mode gates it) without hard-locking its UI permission to Ask.
                    # Existing abilities retain the original destructive => locked
                    # confirmation behavior unless they explicitly opt out.
                    _ab_confirm = getattr(_ab_mod, "REQUIRES_CONFIRMATION", None)
                    _abr = _abd if _ab_confirm is None else _abname in set(_ab_confirm)
                    tools[_abname] = ToolInfo(
                        name=_abname,
                        handler=_abhandler,
                        parameters=_ab_schemas.get(
                            _abname, {"type": "object", "properties": {}, "required": []}
                        ),
                        destructive=_abd,
                        requires_confirmation=_abr,
                    )
        except Exception as _abme:
            logger.warning("Drop-in ability tools unavailable: %s", _abme)

        # ═══════════════════════════════════════════════════════════════
        # Bootstrap core tools — always available from turn 1
        # These are the agent's discovery and essential utilities.
        # All other tools are discovered via list_tools / search_tools.
        # ═══════════════════════════════════════════════════════════════

        # web_search / db_query / get_weather / http_request used to be imported
        # here too; they now ship with their owning abilities' build_tools
        # (web_access, codebase_admin, browser_control) and are no longer wired
        # in core.
        from app.tools.core_tools import (
            present_chat_component as _core_present_chat_component,
            present_plan as _core_present_plan,
            get_task_checkpoint as _core_get_task_checkpoint,
            update_task_checkpoint as _core_update_task_checkpoint,
            memory as _core_memory,
            session_search as _core_session_search,
            similar_sessions as _core_similar_sessions,
            get_time as _core_get_time,
            get_date as _core_get_date,
            calculate as _core_calculate,
            read_own_prompt as _core_read_own_prompt,
            edit_own_prompt as _core_edit_own_prompt,
            save_own_skill as _core_save_own_skill,
            remove_own_skill as _core_remove_own_skill,
        )

        # ── load_tool (core) — activate a discoverable tool ──────────────────
        # Discoverable tools appear by name + one-line description in the
        # generated # [TOOLS] index, but their full JSON schema is withheld to
        # keep context lean. load_tool returns the tool's full input schema AND
        # records it in the session's active-tools list, so the loop starts
        # sending its real schema on subsequent turns and it becomes callable.
        # Mirrors load_skill. Every loaded tool lives in `tools` regardless of
        # mode (mode only decides whether the schema is sent), so this closure —
        # capturing the fully-populated dict — can resolve any of them.
        async def _load_tool_wrapper(name: str):
            """Activate a tool listed under "Load on demand" in the [TOOLS] section. Returns its full input schema and keeps it callable for the rest of the conversation."""
            info = tools.get(name)
            if info is None:
                return json.dumps({
                    "status": "error",
                    "message": (
                        f"No tool named '{name}' is available to you. Only load "
                        f"tools listed in the [TOOLS] section of your prompt."
                    ),
                })
            already = False
            if session_id:
                try:
                    from app.db import get_db as _gd
                    already = name in await _gd().get_session_active_tools(session_id)
                    await _gd().set_session_active_tool(session_id, name, True)
                except Exception as e:
                    logger.debug("set_session_active_tool failed: %s", e)
            desc = (info.handler.__doc__ or "").strip()
            payload = {
                "status": "ok",
                "tool": {
                    "name": name,
                    "description": desc,
                    "parameters": info.parameters,
                },
            }
            # Per-tool mini-skill: read the owning ability's exact
            # `tools/<tool_name>.skill.md` document (plugins/abilities/README.md)
            # HERE — the moment the tool becomes callable — so its specialized
            # how-to arrives with the tool instead of sitting in every prompt.
            # A tool whose ability is turned off, or without a matching file,
            # simply gets no extra prose.
            # Skipped on a REPEAT load, mirroring load_ability's dedup: the section
            # is already in the transcript above, and re-injecting it is a
            # context-blowup tic, not a fix.
            section = ""
            if not already:
                try:
                    from app.tools.tool_modes import ability_for_tool as _aft
                    from app.abilities import ability_tool_skill_section as _atss
                    _owner = _aft(name)
                    if _owner:
                        section = _atss(_owner, name)
                except Exception as e:
                    logger.debug("tool skill section resolve failed for %s: %s", name, e)
            if section:
                payload["skill"] = section
                payload["message"] = (
                    f"Tool '{name}' is now active and its mini-skill is included "
                    f"above. You can call it directly for the rest of this "
                    f"conversation."
                )
            else:
                payload["message"] = (
                    f"Tool '{name}' is now active. You can call it directly for "
                    f"the rest of this conversation."
                )
            return json.dumps(payload)

        tools["load_tool"] = ToolInfo(
            name="load_tool",
            handler=_load_tool_wrapper,
            parameters={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Exact name of the tool to activate (from the [TOOLS] section).",
                    },
                },
                "required": ["name"],
            },
        )

        # ── load_ability (core) — activate a discoverable ability in one call ──
        # A "discoverable" ability appears as a single # [ABILITIES] entry; its
        # tools and how-to skill are withheld. load_ability returns the skill body
        # plus the ability's tools — full input schema for the agent's VISIBLE
        # tools, name + description for its discoverable ones — and marks the
        # ability (and its bundled skill) active so its visible tools start being
        # sent on the next turn. Mirrors load_tool / load_skill.
        async def _load_ability_wrapper(ability_id: str):
            """Activate an ability listed under the [ABILITIES] section. Returns its how-to skill plus its tools (full schema for visible tools, name+description for discoverable ones) and keeps it active for the rest of the conversation."""
            from app.tools.tool_modes import resolve_mode as _rm, VISIBLE as _VIS
            # Tolerate the common mix-up where the model passes the ability's SKILL
            # HANDLE (e.g. "visualizer_7a842b54") instead of the ability id. The
            # handle is "<ability_id>_<8 hex>"; if the raw id resolves to nothing,
            # strip a trailing _<8hex> and retry so the load still succeeds.
            if ability_id not in ABILITY_TOOLS:
                import re as _re_h
                _m = _re_h.match(r"^(.*)_[0-9a-f]{8}$", ability_id or "")
                if _m and _m.group(1) in ABILITY_TOOLS:
                    ability_id = _m.group(1)

            # ── Dedup: don't re-inject an ability that's already active ──────────
            # The skill body returned below is large (often ~10k tokens). It is
            # meant to be loaded ONCE and then "stays in context for the rest of
            # this conversation". Re-calling load_ability for an already-active
            # ability is a common model tic — especially after a `continue`
            # restarts the loop and the model forgets it already loaded it — and
            # each redundant call duplicated the whole skill into the transcript,
            # a major driver of runaway context. If it's already active, return a
            # tiny acknowledgement (tool names only, NO skill body) instead.
            if session_id:
                try:
                    from app.db import get_db as _gd
                    if ability_id in await _gd().get_session_active_abilities(session_id):
                        _names = list(ABILITY_TOOLS.get(ability_id, []))
                        return json.dumps({
                            "status": "already_active",
                            "ability": {"id": ability_id},
                            "tools": _names,
                            "message": (
                                f"Ability '{ability_id}' is already active — its skill is "
                                f"already in the context above and its tools "
                                f"({', '.join(_names) or 'none'}) are callable now. Do not "
                                f"load it again; just call the tool you need."
                            ),
                        })
                except Exception as e:
                    logger.debug("load_ability dedup check failed for %s: %s", ability_id, e)

            names = list(ABILITY_TOOLS.get(ability_id, []))

            # Resolve the agent's per-tool modes FIRST: they decide which of this
            # ability's tools are already being SENT (visible), which is what
            # selects the per-tool mini-skills delivered below — a visible tool
            # never gets a load_tool call, so this is its only delivery point.
            agent_modes = {}
            try:
                from app.db import get_db as _gd
                if agent_id:
                    agent_modes = await _gd().get_agent_tool_modes(agent_id)
            except Exception:
                agent_modes = {}

            skill_body = skill_summary = ""
            skill_handle = None
            skill_for_visible = ""
            try:
                from app.agent.ability_skills import ability_skill_payload as _asp
                _pl = _asp(ability_id,
                           [n for n in names if _rm(n, agent_modes) == _VIS],
                           await _gd().get_agent_skill_modes(agent_id) if agent_id else {})
                skill_body = _pl.get("body") or ""
                skill_handle = _pl.get("handle")
                skill_summary = _pl.get("summary") or ""
                skill_for_visible = _pl.get("deferred") or ""
            except Exception as e:
                logger.debug("load_ability skill resolve failed for %s: %s", ability_id, e)

            if not names and not skill_body:
                return json.dumps({
                    "status": "error",
                    "message": (
                        f"No ability named '{ability_id}' is available to you. Only "
                        f"load abilities listed in the [ABILITIES] section."
                    ),
                })

            tools_out = []
            for n in names:
                info = tools.get(n)
                if info is None:
                    continue
                desc = ((info.handler.__doc__ or "").strip().split("\n")[0]
                        if hasattr(info, "handler") else "")
                entry = {"name": n, "description": desc}
                if _rm(n, agent_modes) == _VIS:
                    entry["parameters"] = info.parameters if hasattr(info, "parameters") else {}
                else:
                    entry["note"] = "discoverable — call load_tool to get its parameters"
                tools_out.append(entry)

            if session_id:
                try:
                    from app.db import get_db as _gd
                    _db = _gd()
                    await _db.set_session_active_ability(session_id, ability_id, True)
                    if skill_handle:
                        await _db.set_session_active_skill(session_id, skill_handle, True)
                except Exception as e:
                    logger.debug("load_ability activation failed: %s", e)

            _payload = {
                "status": "ok",
                "ability": {"id": ability_id, "skill_summary": skill_summary},
                "skill": skill_body,
                "tools": tools_out,
            }
            if skill_for_visible:
                # Mini-skills for the ability's ALREADY-visible tools. Discoverable
                # ones are NOT included — theirs arrive from load_tool when the model
                # activates them — and tools turned off for this agent contribute
                # nothing at all.
                _payload["skills_for_visible_tools"] = skill_for_visible
            _payload["message"] = (
                f"Ability '{ability_id}' is now active. Its visible tools are "
                f"callable now; call load_tool for any discoverable ones — each "
                f"tool carries its own mini-skill, delivered when you load it. The "
                f"skill above stays in context for the rest of this conversation."
            )
            return json.dumps(_payload)

        tools["load_ability"] = ToolInfo(
            name="load_ability",
            handler=_load_ability_wrapper,
            parameters={
                "type": "object",
                "properties": {
                    "ability_id": {
                        "type": "string",
                        "description": "Exact id of the ability to activate (from the [ABILITIES] section).",
                    },
                },
                "required": ["ability_id"],
            },
        )

        # ── On-demand skills (list_skills / load_skill) ──
        # Always available (mirrors the tool-discovery bootstrap). The agent
        # learns which skills exist from the `# [SKILLS]` prompt section and
        # uses load_skill to pull a selectable skill's full body into context.
        from app.tools.core_tools import (
            list_skills as _core_list_skills,
            load_skill as _core_load_skill,
        )

        async def _list_skills_wrapper():
            return await _core_list_skills(agent_id=agent_id, session_id=session_id)
        _list_skills_wrapper.__doc__ = _core_list_skills.__doc__

        tools["list_skills"] = ToolInfo(
            name="list_skills",
            handler=_list_skills_wrapper,
            parameters={"type": "object", "properties": {}, "required": []},
        )

        async def _load_skill_wrapper(name: str):
            return await _core_load_skill(name=name, agent_id=agent_id, session_id=session_id)
        _load_skill_wrapper.__doc__ = _core_load_skill.__doc__

        tools["load_skill"] = ToolInfo(
            name="load_skill",
            handler=_load_skill_wrapper,
            parameters={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Exact name of the skill to load (from the [SKILLS] section).",
                    },
                },
                "required": ["name"],
            },
        )

        # ── set_execution_mode (core) — switch Ask/Plan/Auto mid-conversation ──
        # The chat pill (Ask/Plan/Auto) is the user's per-message control. This
        # tool lets the agent CHANGE the live mode when the user authorises it —
        # the canonical case is flipping Plan→Auto the moment the user approves a
        # plan ("yes, go ahead"), so the agent can carry it out without every
        # write being gated. It records the choice on the session; the loop reads
        # the tool's result, applies the new mode for the rest of the turn, and
        # broadcasts an `execution_mode` event so the UI pill visibly switches.
        # Only call it when the user has clearly authorised the change.
        async def _set_execution_mode_wrapper(mode: str, reason: str = ""):
            """Switch the conversation's execution mode (ask/plan/auto). Call this when the user authorises a change — e.g. flip to 'auto' once they approve your plan so you can act without each step being gated, or back to 'plan'/'ask' when they want to slow down. The pill below the chat updates to match."""
            m = str(mode or "").strip().lower()
            _aliases = {"read": "plan", "write": "ask", "plan": "plan", "ask": "ask", "auto": "auto"}
            m = _aliases.get(m, "")
            if not m:
                return json.dumps({
                    "status": "error",
                    "message": "mode must be one of: ask, plan, auto.",
                })
            if session_id:
                try:
                    from app.db import get_db as _gd
                    _dbm = _gd()
                    # Mode in force BEFORE this flip — the durable notice records
                    # it so the UI nets a group's changes (a flip back to the
                    # group's starting mode cancels the notice).
                    try:
                        _from_mode = await _dbm.get_session_execution_mode(session_id) or ""
                    except Exception:
                        _from_mode = ""
                    await _dbm.set_session_execution_mode(session_id, m, reason or "")
                    # Durable twin of the live WS notice: persist a system:mode
                    # row so the flip shows on cold reload (and in the IndexedDB
                    # cache). initiator='agent' — the row carries its own
                    # provenance even though the set_execution_mode tool call is
                    # visible in the transcript. Best-effort — the switch itself
                    # already succeeded.
                    try:
                        await _dbm.add_execution_mode_notice(
                            user_id, session_id, m, reason or "",
                            turn_id=turn_id or None,
                            initiator="agent", tool="set_execution_mode",
                            from_mode=_from_mode)
                    except Exception as _pe:
                        logger.debug("persist execution-mode notice failed: %s", _pe)
                except Exception as e:
                    logger.debug("set_session_execution_mode failed: %s", e)
            _label = {"ask": "ASK", "plan": "PLAN", "auto": "AUTO"}[m]
            _posture = {
                "ask": "Read/research freely and deliver a proposal; destructive or write actions are unavailable.",
                "plan": "Research with read-only tools; do not make changes — produce a plan and get approval.",
                "auto": "You may now act autonomously — tools run without per-step confirmation. Proceed and report what you did.",
            }[m]
            return json.dumps({
                "status": "ok",
                "execution_mode": m,
                "message": f"Execution mode is now {_label}. {_posture}",
            })

        tools["set_execution_mode"] = ToolInfo(
            name="set_execution_mode",
            handler=_set_execution_mode_wrapper,
            parameters={
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": ["ask", "plan", "auto"],
                        "description": "The mode to switch to. 'auto' = write-capable execution (use after approval); 'plan' = read-only planning; 'ask' = read-only proposal.",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Optional one-line note on why you're switching (e.g. 'user approved the plan').",
                    },
                },
                "required": ["mode"],
            },
        )

        # ── Plan workspace + chat-component presenters (core) ─────────────────
        # present_plan is REQUIRED by Plan mode's mode contract
        # (app/agent/execution_modes.py "plan-document" + the loop's
        # require_plan_document completion gate); present_chat_component is its
        # sibling chat presenter. Both are plain chat-component/harness tools with
        # no Gen UI coupling, so they are wired HERE with the always-on core and
        # listed under the virtual Core ▸ Base row — not under the OPTIONAL
        # Visualizer ability they used to ship in, where switching that ability off
        # (agent_connections.enabled = 0) silently turned Plan mode into an
        # un-completable dead end ("plan_document_tool_unavailable").
        tools["present_chat_component"] = ToolInfo(
            name="present_chat_component",
            handler=_core_present_chat_component,
            parameters={"type": "object", "properties": {
                "type": {"type": "string", "enum": ["todo_list", "status", "choice", "approval", "form"]},
                "title": {"type": "string",
                          "description": "Short display name — 1-2 words, max 3 (e.g. 'Checklist', 'Plan Overview'). Long names are capped server-side to the element type name."},
                "placement": {"type": "string", "enum": ["inline", "hover", "sticky"]},
                "data": {"type": "object"}, "lifecycle": {"type": "object"},
                "component_id": {"type": "string"},
            }, "required": ["type"]},
        )

        tools["present_plan"] = ToolInfo(
            name="present_plan",
            handler=_core_present_plan,
            parameters={"type": "object", "properties": {
                "overview": {"type": "object",
                             "description": "Optional overview sections: {sections: [{label, status, value}]}"},
                "checklist": {"type": "array", "minItems": 1, "items": {
                    "type": "object", "properties": {
                        "id": {"type": "string", "description": "Optional legacy internal reference."},
                        "label": {"type": "string", "minLength": 3},
                        "done": {"type": "boolean"},
                        "depends_on": {"type": "array", "items": {"type": "string"}},
                        "verification": {"type": "string"},
                        "requirement_ids": {"type": "array", "items": {"type": "string"}},
                    }, "required": ["label", "done"],
                    "additionalProperties": False},
                    "description": "Complete desired execution-step list merged into Task Progress. Each label must contain enough detail to execute; add observable verification and only necessary dependencies."},
                "goal": {"type": "string"}, "criteria": {"type": "string"},
                "notes": {"type": "string"},
                "title": {"type": "string",
                          "description": "Short plan name — 1-2 words, max 3 (default 'Plan'). Names the persistent Plan Overview."},
            }, "required": ["checklist"]},
        )

        # ── Task Progress checkpoint tools (core) ─────────────────────────────
        # These are core task-state utilities, not ability-owned tools. Unlike
        # irreducible mode machinery, they honour an explicit per-agent or global
        # deny: the final loader filter removes them before they can be described
        # to, schema-sent to, or invoked by the model.
        from app.agent.task_state import checkpoint_schema as _checkpoint_schema
        tools["get_task_checkpoint"] = ToolInfo(
            name="get_task_checkpoint",
            handler=_core_get_task_checkpoint,
            parameters={"type": "object", "properties": {}, "required": []},
        )
        tools["update_task_checkpoint"] = ToolInfo(
            name="update_task_checkpoint",
            handler=_core_update_task_checkpoint,
            parameters=_checkpoint_schema(),
        )
        # web_access (web_search, maps_geocode, get_weather) → moved to plugins/abilities/web_access.py build_tools.

        # diagnostics (read_diagnostics) → moved to plugins/abilities/diagnostics.py build_tools.

        # agent_management (list/create/update agents, prompt + ability + skill edits)
        # → moved to plugins/abilities/agent_management.py build_tools.

        # browser_control (browser_action, http_request) -> moved to plugins/abilities/browser_control.py build_tools.
        # image_generation (generate_image) -> moved to plugins/abilities/image_generation.py build_tools.
        # codebase_admin db_query (context documents) -> moved to plugins/abilities/codebase_admin.py build_tools.

        # ── Memory (persistent knowledge pages) ──
        async def _memory_wrapper(
            action: str,
            slug: Optional[str] = None,
            query: Optional[str] = None,
            page_type: Optional[str] = None,
            title: Optional[str] = None,
            compiled_truth: Optional[str] = None,
            timeline: Optional[str] = None,
            limit: int = 10,
        ):
            # Block memory access for simulated/worker-trial sessions.
            # These use user IDs of the form "worker-test-<id>" and should
            # never read from or write to the real memory store.
            if user_id.startswith("worker-test-"):
                return json.dumps({
                    "status": "skipped",
                    "message": "Memory is disabled in simulated sessions.",
                })
            return await _core_memory(
                action=action,
                slug=slug,
                query=query,
                page_type=page_type,
                title=title,
                compiled_truth=compiled_truth,
                timeline=timeline,
                limit=limit,
                user_id=user_id,
            )

        tools["memory"] = ToolInfo(
            name="memory",
            handler=_memory_wrapper,
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["search", "list", "get", "upsert", "delete"],
                        "description": "Action: search, list, get, upsert, or delete",
                    },
                    "slug": {"type": "string", "description": "Unique page identifier — for get/upsert/delete actions"},
                    "query": {"type": "string", "description": "Search query — for search action"},
                    "page_type": {"type": "string", "description": "Page type (note, meeting, project, person) — for list/upsert actions"},
                    "title": {"type": "string", "description": "Page title — for upsert action"},
                    "compiled_truth": {"type": "string", "description": "Main content body — for upsert action"},
                    "timeline": {"type": "string", "description": "Optional timeline entry — for upsert action"},
                    "limit": {"type": "integer", "description": "Max results for search action (default 10)", "default": 10},
                },
                "required": ["action"],
            },
        )

        # ── Session search ──
        async def _session_search_wrapper(
            query: str = "", limit: int = 10, session_id: str = "",
            after_session_seq: int = 0,
        ):
            return await _core_session_search(
                query=query, limit=limit, user_id=user_id,
                session_id=session_id, after_session_seq=after_session_seq,
            )

        tools["session_search"] = ToolInfo(
            name="session_search",
            handler=_session_search_wrapper,
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Optional keyword; omit when reading an exact session"},
                    "session_id": {"type": "string", "description": "Exact owned source session to inspect"},
                    "after_session_seq": {"type": "integer", "description": "Only exact-session rows newer than this sequence", "default": 0},
                    "limit": {"type": "integer", "description": "Max results (default 10, maximum 50)", "default": 10},
                },
                "required": [],
            },
        )

        # similar_sessions — semantic sibling of session_search: ranks whole past
        # CONVERSATIONS by topic (meaning), not keywords. Excludes the current
        # session so it never returns "this chat".
        async def _similar_sessions_wrapper(query: str, limit: int = 8):
            return await _core_similar_sessions(
                query=query, limit=limit, user_id=user_id,
                exclude_session_id=session_id or "")

        tools["similar_sessions"] = ToolInfo(
            name="similar_sessions",
            handler=_similar_sessions_wrapper,
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Topic to find similar past conversations for (e.g. 'setting up the deploy pipeline')"},
                    "limit": {"type": "integer", "description": "Max results (default 8)", "default": 8},
                },
                "required": ["query"],
            },
        )

        # ── Self-prompt (read + improve your OWN prompt) ──
        # Surfaced under Core ▸ Base (see plugins/abilities/Core/base/base.json).
        # The standard Deny/Ask/Auto permission tri applies universally in both
        # ability tables. edit_own_prompt is marked destructive in
        # BUILTIN_TOOL_METADATA → confirms in Ask/Plan mode. Both close over the
        # running agent_id + user_id, so they can only ever touch THIS agent's own
        # prompt; admin-locked slots are read-only (enforced in core_tools).
        async def _read_own_prompt_wrapper():
            return await _core_read_own_prompt(agent_id=agent_id, user_id=user_id)
        _read_own_prompt_wrapper.__doc__ = _core_read_own_prompt.__doc__

        tools["read_own_prompt"] = ToolInfo(
            name="read_own_prompt",
            handler=_read_own_prompt_wrapper,
            parameters={"type": "object", "properties": {}, "required": []},
        )

        async def _edit_own_prompt_wrapper(slot_name: str, content: str, mode: str = "replace"):
            return await _core_edit_own_prompt(
                slot_name=slot_name, content=content, mode=mode,
                agent_id=agent_id, user_id=user_id,
            )
        _edit_own_prompt_wrapper.__doc__ = _core_edit_own_prompt.__doc__

        tools["edit_own_prompt"] = ToolInfo(
            name="edit_own_prompt",
            handler=_edit_own_prompt_wrapper,
            parameters={
                "type": "object",
                "properties": {
                    "slot_name": {"type": "string", "description": "The prompt section to change (see read_own_prompt). A new name creates a new section."},
                    "content": {"type": "string", "description": "The new text for the section."},
                    "mode": {"type": "string", "enum": ["replace", "append"], "description": "'replace' overwrites the section (default); 'append' adds to the end of the existing section.", "default": "replace"},
                },
                "required": ["slot_name", "content"],
            },
        )

        # ── Self-skills (teach yourself reusable how-to "skill-type memories") ──
        # Self-targeted skill authoring, surfaced under Core ▸ Base. The agent's
        # own skills appear in list_skills and load on demand via load_skill, so
        # these write into that same store. Both destructive (write); gateable.
        async def _save_own_skill_wrapper(name: str, description: str = None,
                                          instructions: str = None, mode: str = "selectable"):
            return await _core_save_own_skill(
                name=name, description=description, instructions=instructions,
                mode=mode, agent_id=agent_id, user_id=user_id,
            )
        _save_own_skill_wrapper.__doc__ = _core_save_own_skill.__doc__

        tools["save_own_skill"] = ToolInfo(
            name="save_own_skill",
            handler=_save_own_skill_wrapper,
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Short identifier you'll load the skill by later."},
                    "description": {"type": "string", "description": "ONE line saying WHEN to use this skill — always shown in your [SKILLS] catalog."},
                    "instructions": {"type": "string", "description": "The full step-by-step body (the actual know-how). Required for a new skill."},
                    "mode": {"type": "string", "enum": ["selectable", "always_on"], "description": "'selectable' (default) = body loaded on demand via load_skill; 'always_on' = body always in context (short essential guidance only).", "default": "selectable"},
                },
                "required": ["name"],
            },
        )

        async def _remove_own_skill_wrapper(name: str):
            return await _core_remove_own_skill(name=name, agent_id=agent_id, user_id=user_id)
        _remove_own_skill_wrapper.__doc__ = _core_remove_own_skill.__doc__

        tools["remove_own_skill"] = ToolInfo(
            name="remove_own_skill",
            handler=_remove_own_skill_wrapper,
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "The name of your own skill to delete (see list_skills)."},
                },
                "required": ["name"],
            },
        )

        # ── Time & Date ──
        tools["get_time"] = ToolInfo(
            name="get_time",
            handler=_core_get_time,
            parameters={
                "type": "object",
                "properties": {
                    "timezone": {"type": "string", "description": "IANA timezone (e.g. 'America/New_York', 'Europe/London'). Defaults to UTC.", "default": "UTC"},
                },
                "required": [],
            },
        )

        tools["get_date"] = ToolInfo(
            name="get_date",
            handler=_core_get_date,
            parameters={
                "type": "object",
                "properties": {
                    "timezone": {"type": "string", "description": "IANA timezone (e.g. 'America/New_York'). Defaults to UTC.", "default": "UTC"},
                    "format": {"type": "string", "enum": ["full", "short", "iso"], "description": "Date format: full (Monday, May 8, 2026), short (2026-05-08), iso (ISO 8601)", "default": "full"},
                },
                "required": [],
            },
        )

        # web_access get_weather (same ability as web_search) → moved to plugins/abilities/web_access.py build_tools.

        # ── Calculator ──
        tools["calculate"] = ToolInfo(
            name="calculate",
            handler=_core_calculate,
            parameters={
                "type": "object",
                "properties": {
                    "expression": {"type": "string", "description": "Mathematical expression (e.g. '2 + 2', 'sin(pi/4)', 'sqrt(144)')"},
                },
                "required": ["expression"],
            },
        )

        # browser_control http_request (same ability as browser_action) → moved to plugins/abilities/browser_control.py build_tools.

        # ── Webhook management (generic inbound webhooks) ──
        async def _register_webhook_wrapper(name: str, instructions: str = ""):
            """Register a new generic inbound webhook endpoint."""
            from app.db import get_db
            db = get_db()
            result = await db.register_webhook(
                user_id=user_id,
                name=name,
                instructions=instructions,
            )
            webhook_url = _get_webhook_base_url() + f"/api/v1/webhooks/generic/{result['id']}"
            result["url"] = webhook_url
            return json.dumps({
                "status": "ok",
                "webhook": result,
                "message": f"Webhook '{name}' registered at {webhook_url}",
            })

        tools["register_webhook"] = ToolInfo(
            name="register_webhook",
            handler=_register_webhook_wrapper,
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Human-readable name for this webhook (e.g. 'GitHub push events')"},
                    "instructions": {"type": "string", "description": "Instructions for the agent when this webhook fires (e.g. 'Analyze the push payload and summarize changes')"},
                },
                "required": ["name"],
            },
        )

        async def _list_webhooks_wrapper():
            """List all registered webhooks for the current user."""
            from app.db import get_db
            db = get_db()
            hooks = await db.list_webhooks(user_id=user_id)
            base_url = _get_webhook_base_url()
            for h in hooks:
                h["url"] = base_url + f"/api/v1/webhooks/generic/{h['id']}"
            return json.dumps({"status": "ok", "webhooks": hooks, "count": len(hooks)})

        tools["list_webhooks"] = ToolInfo(
            name="list_webhooks",
            handler=_list_webhooks_wrapper,
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
            },
        )

        async def _delete_webhook_wrapper(webhook_id: str):
            """Delete a webhook registration by id."""
            from app.db import get_db
            db = get_db()
            ok = await db.delete_webhook(webhook_id=webhook_id, user_id=user_id)
            if ok:
                return json.dumps({"status": "ok", "message": f"Webhook {webhook_id} deleted"})
            return json.dumps({"status": "error", "message": f"Webhook {webhook_id} not found or not owned by user"})

        tools["delete_webhook"] = ToolInfo(
            name="delete_webhook",
            handler=_delete_webhook_wrapper,
            parameters={
                "type": "object",
                "properties": {
                    "webhook_id": {"type": "string", "description": "The webhook registration id to delete"},
                },
                "required": ["webhook_id"],
            },
        )

        async def _get_webhook_log_wrapper(webhook_id: str, limit: int = 10):
            """View recent events for a webhook registration."""
            from app.db import get_db
            db = get_db()
            # Verify ownership
            reg = await db.get_webhook(webhook_id=webhook_id)
            if not reg or reg.get("user_id") != user_id:
                return json.dumps({"status": "error", "message": "Webhook not found or not owned by user"})
            events = await db.get_webhook_logs(webhook_id=webhook_id, limit=limit)
            return json.dumps({"status": "ok", "events": events, "count": len(events)})

        tools["get_webhook_log"] = ToolInfo(
            name="get_webhook_log",
            handler=_get_webhook_log_wrapper,
            parameters={
                "type": "object",
                "properties": {
                    "webhook_id": {"type": "string", "description": "The webhook registration id"},
                    "limit": {"type": "integer", "description": "Max events to return", "default": 10},
                },
                "required": ["webhook_id"],
            },
        )

        # automation event subscriptions (list_event_sources, list_delivery_channels,
        # event_subscribe, list_event_subscriptions, event_unsubscribe)
        # -> moved to plugins/abilities/automation.py build_tools.


        # ── Optimizer tools ──
        from app.tools.optimizer_tools import run_worker_trials, deploy_optimization

        def _optimizer_session_id() -> str:
            """The current Optimizer session.

            Tool handlers are built for one chat turn.  Selecting the newest
            optimizer session globally can attach a Planner's trial or handoff to
            another user's concurrent optimization, leaving the Closer without
            the right baseline and proposal.
            """
            import uuid as _uid
            if session_id.startswith("optimizer-"):
                return session_id
            from app.db import get_db as _gdb
            db = _gdb()._get_conn()
            try:
                row = db.execute(
                    "SELECT id FROM sessions WHERE id LIKE 'optimizer-%' ORDER BY created_at DESC LIMIT 1"
                ).fetchone()
            finally:
                db.close()
            return row[0] if row else f"optimizer-{str(_uid.uuid4())[:8]}"

        async def _run_worker_trials_wrapper(changes_json: str = ""):
            import traceback as _tb
            try:
                sid = _optimizer_session_id()
                result = await run_worker_trials(changes_json=changes_json, user_id=user_id, session_id=sid)
                return result
            except Exception as e:
                tb_str = _tb.format_exc()
                return json.dumps({"status": "error", "message": f"{type(e).__name__}: {e}", "traceback": tb_str[:500]})
        tools["run_worker_trials"] = ToolInfo(
            name="run_worker_trials",
            handler=_run_worker_trials_wrapper,
            parameters={
                "type": "object",
                "properties": {
                    "changes_json": {"type": "string", "description": "JSON array of changes to test, where each change has element, element_type, change_type, old_excerpt, new_content, reasoning"},
                },
                "required": ["changes_json"],
            },
        )

        async def _deploy_optimization_wrapper(changes_json: str = ""):
            sid = _optimizer_session_id()
            return await deploy_optimization(changes_json=changes_json, user_id=user_id, session_id=sid)
        tools["deploy_optimization"] = ToolInfo(
            name="deploy_optimization",
            handler=_deploy_optimization_wrapper,
            parameters={
                "type": "object",
                "properties": {
                    "changes_json": {"type": "string", "description": "JSON array of approved changes to deploy to the target agent"},
                },
                "required": ["changes_json"],
            },
        )

        # Lock optimizer-pipeline tools to the Optimizer agent ─────────────
        # The wrappers above are defined unconditionally (cheap closures), but a
        # normal agent must never be able to CALL them. Remove them unless this
        # run is the Optimizer agent. This is the guard that stops any non-optimizer
        # agent from invoking run_worker_trials or deploy_optimization.
        if not _is_opt_agent:
            for _opt_only in ("run_worker_trials", "deploy_optimization"):
                tools.pop(_opt_only, None)

        # ── check_oauth_connection ──────────────────────────────────────────────
        # Only registered when at least one OAuth provider is enabled AND
        # admin-configured for this agent. Its provider enum is restricted
        # to those providers so the LLM never learns about capabilities the
        # agent isn't supposed to have.
        _CT_TO_OAUTH = {
            "google": "google", "microsoft": "microsoft", "yahoo": "yahoo",
            "dropbox": "dropbox", "facebook": "meta", "instagram": "meta",
            "twitter": "twitter", "linkedin": "linkedin", "tiktok": "tiktok",
            "pinterest": "pinterest", "reddit": "reddit", "snapchat": "snapchat",
            "twitch": "twitch",
        }
        _oauth_enabled = sorted({_CT_TO_OAUTH[ct] for ct in enabled_providers if ct in _CT_TO_OAUTH})
        if not _oauth_enabled:
            return

        _captured_agent_id = agent_id

        async def _check_oauth_connection_wrapper(provider: str) -> str:
            import json as _json
            from app.db import get_db as _get_db
            from app.admin.integrations import (
                get_google_creds, build_google_authorize_url,
                get_microsoft_creds, build_microsoft_authorize_url,
                get_yahoo_creds, build_yahoo_authorize_url,
                get_dropbox_creds, build_dropbox_authorize_url,
                get_meta_creds, build_meta_authorize_url,
                get_twitter_creds, build_twitter_authorize_url,
                get_linkedin_creds, build_linkedin_authorize_url,
                get_tiktok_creds, build_tiktok_authorize_url,
                get_pinterest_creds, build_pinterest_authorize_url,
                get_reddit_creds, build_reddit_authorize_url,
                get_snapchat_creds, build_snapchat_authorize_url,
                get_twitch_creds, build_twitch_authorize_url,
            )

            _aliases = {"facebook": "meta", "instagram": "meta", "x": "twitter", "gmail": "google", "drive": "google", "calendar": "google", "outlook": "microsoft"}
            provider = _aliases.get(provider.lower().strip(), provider.lower().strip())

            _supported = {
                "google":    (get_google_creds,    build_google_authorize_url,    "Google"),
                "microsoft": (get_microsoft_creds, build_microsoft_authorize_url, "Microsoft"),
                "yahoo":     (get_yahoo_creds,     build_yahoo_authorize_url,     "Yahoo"),
                "dropbox":   (get_dropbox_creds,   build_dropbox_authorize_url,   "Dropbox"),
                "meta":      (get_meta_creds,      build_meta_authorize_url,      "Facebook/Instagram"),
                "twitter":   (get_twitter_creds,   build_twitter_authorize_url,   "Twitter/X"),
                "linkedin":  (get_linkedin_creds,  build_linkedin_authorize_url,  "LinkedIn"),
                "tiktok":    (get_tiktok_creds,    build_tiktok_authorize_url,    "TikTok"),
                "pinterest": (get_pinterest_creds, build_pinterest_authorize_url, "Pinterest"),
                "reddit":    (get_reddit_creds,    build_reddit_authorize_url,    "Reddit"),
                "snapchat":  (get_snapchat_creds,  build_snapchat_authorize_url,  "Snapchat"),
                "twitch":    (get_twitch_creds,    build_twitch_authorize_url,    "Twitch"),
            }

            if provider not in _supported:
                return _json.dumps({
                    "status": "unsupported",
                    "message": f"Provider '{provider}' is not recognized. Supported: {', '.join(_supported)}.",
                })

            get_creds_fn, build_url_fn, display_name = _supported[provider]
            _db = _get_db()

            # Check if integration is enabled for this agent
            if _captured_agent_id:
                try:
                    rows = await _db.get_agent_connections(_captured_agent_id)
                    conn_row = next((r for r in rows if r["connection_type"] == provider), None)
                    if not conn_row or not conn_row.get("enabled"):
                        return _json.dumps({
                            "status": "not_enabled",
                            "message": f"{display_name} integration is not enabled for this agent. Ask your agent admin to enable it in the Integrations tab.",
                        })
                except Exception:
                    pass  # If we can't check, proceed to creds check

            # Check if admin has configured OAuth credentials
            try:
                client_id, _ = await get_creds_fn()
            except Exception:
                client_id = None
            if not client_id:
                return _json.dumps({
                    "status": "not_configured",
                    "message": f"{display_name} integration has not been configured. An admin must set up the OAuth credentials in App Config → Integrations.",
                })

            # Check if user already has a connected token (per-agent scope)
            try:
                from app.integrations.oauth_helper import oauth_label as _oauth_label
                elem = await _db.auth_element_get(user_id, provider, _oauth_label(_captured_agent_id))
                if elem and elem.get("secret_ref"):
                    config = elem.get("config") or {}
                    if isinstance(config, str):
                        try:
                            config = _json.loads(config)
                        except Exception:
                            config = {}
                    email = config.get("email") or config.get("name") or ""
                    display = f" as {email}" if email else ""
                    return _json.dumps({
                        "status": "connected",
                        "message": f"Your {display_name} account is already connected{display}.",
                        "email": email,
                    })
            except Exception:
                pass

            # Generate the connect URL
            try:
                result = await build_url_fn(user_id=user_id, agent_id=_captured_agent_id)
                authorize_url = result[0] if isinstance(result, tuple) else result
            except Exception as e:
                return _json.dumps({
                    "status": "error",
                    "message": f"Could not generate a connect link for {display_name}: {e}",
                })

            return _json.dumps({
                "status": "not_connected",
                "message": f"{display_name} integration is set up. Please connect your account: [{display_name}]({authorize_url})",
                "authorize_url": authorize_url,
            })

        tools["check_oauth_connection"] = ToolInfo(
            name="check_oauth_connection",
            handler=_check_oauth_connection_wrapper,
            parameters={
                "type": "object",
                "properties": {
                    "provider": {
                        "type": "string",
                        "enum": _oauth_enabled,
                        "description": (
                            "The OAuth provider to check. Call this when the user wants to do "
                            "something that requires a connected account and you are not sure if "
                            "they have connected it. Allowed values for this agent: "
                            + ", ".join(_oauth_enabled) + "."
                        ),
                    },
                },
                "required": ["provider"],
            },
        )


    def _make_handler(self, row: dict, user_id: str) -> Callable:
        """Compile tool code and wrap it with user context."""
        name = row['name']
        code = row['code']

        if name == 'create_tool':
            async def passthrough(**kwargs):
                from app.tools.registry import create_tool as ct
                return await ct(user_id=user_id, **kwargs)
            return passthrough

        handler = self._compile_tool(code, name)

        # Inject user_id into the handler's kwargs if the function accepts it
        import inspect
        try:
            sig = inspect.signature(handler)
            if 'user_id' in sig.parameters:
                original = handler
                async def wrapped(**kwargs):
                    return await original(user_id=user_id, **kwargs)
                return wrapped
        except (ValueError, TypeError):
            pass

        return handler

    async def _fetch_user_tools(self, user_id: str) -> List[Dict]:
        """Fetch all active tools for a user from the app-owned tool catalog."""
        try:
            response = (
                # `tools` is app-plane owned. `get_db()` is a user-plane router
                # under the split SQLite layout, where this table does not exist.
                get_app_db().get_raw_client().table("tools")
                .select("*")
                .in_("created_by", [user_id, "__system__"])
                .eq("status", "active")
                .execute()
            )
            return response.data or []
        except Exception as e:
            db = get_db()
            local = getattr(db, "local", None)
            if local is not None and "no such table: tools" in str(e).lower():
                try:
                    response = (local.get_raw_client().table("tools").select("*")
                                .in_("created_by", [user_id, "__system__"])
                                .eq("status", "active").execute())
                    logger.warning("Remote tools authority lacks schema; using local mirror for %s", user_id)
                    return response.data or []
                except Exception as fallback_error:
                    logger.error("Local tools fallback failed for %s: %s", user_id, fallback_error)
            logger.error(f"Error fetching tools for user {user_id}: {e}")
            return []

    def _compile_tool(self, code_string: str, tool_name: str) -> Any:
        """
        Compile a tool code string into a Python function.
        Uses a restricted namespace that strips dangerous builtins
        (open, exec, eval, compile, __import__) as defense-in-depth
        against filesystem/shell/DB access via create_tool.

        Args:
            code_string: Full Python function code
            tool_name: Name of the tool for error reporting

        Returns:
            Compiled Python function object
        """
        try:
            # Strip dangerous builtins — defense-in-depth for create_tool
            safe_builtins = dict(__builtins__)
            for _dangerous in ("open", "exec", "eval", "compile", "__import__"):
                safe_builtins.pop(_dangerous, None)

            compiled = compile(code_string, f"<tool:{tool_name}>", "exec")
            namespace = {"__builtins__": safe_builtins}
            exec(compiled, namespace)
            return namespace[tool_name]
        except Exception as e:
            logger.error(f"Error compiling tool {tool_name}: {e}")
            async def error_tool(*args, **kwargs):
                raise RuntimeError(f"Failed to compile tool {tool_name}: {e}")
            return error_tool


# Global instance
_tool_loader = ToolLoader()


async def load_tools(
    user_id: str,
    agent_id: str = "",
    agent_template_id: Optional[str] = None,
    allowed_tools: Optional[List[str]] = None,
    custom_tool_ids: Optional[List[str]] = None,
    session_id: str = "",
    gate_caller_access: bool = False,
    turn_id: str = "",
) -> Dict[str, ToolInfo]:
    """
    Load all active tools for a user.

    Args:
        user_id: The user ID to load tools for.
        agent_template_id: Active agent template id - gates delegation tools;
            pipeline agents skip delegation tools.
        allowed_tools: List of tool names that are denied for this agent. Empty
            list means all non-tier-one tools are enabled. Tier-1 is reserved
            only for irreducible mode machinery that cannot be disabled.
        custom_tool_ids: Reserved - DB tool IDs opted in (not yet enforced).

    Returns:
        Dictionary mapping tool names to ToolInfo objects.
    """
    tools = await _tool_loader.load_tools(
        user_id, agent_id=agent_id, agent_template_id=agent_template_id,
        session_id=session_id, turn_id=turn_id,
        gate_caller_access=gate_caller_access,
    )

    # This is stricter than an ability-group filter: the core discovery and
    # self-improvement tools are not owned by an ability, so they would otherwise
    # let a guest learn privileged skill names or inspect the shared agent prompt.
    if gate_caller_access and _is_anonymous_user_id(user_id):
        public_policy = None
        try:
            agent = await get_db().get_agent_by_id(agent_id) if agent_id else None
            if agent:
                from app.agent.public_policy import normalize_public_access
                public_policy = normalize_public_access(agent)
        except Exception:
            public_policy = None
        caps = (public_policy or {}).get("capabilities") or {}
        if caps.get("mode") == "showcase" or not public_policy:
            allowed_public_tools = set(ANONYMOUS_CHAT_TOOLS)
        else:
            configured = set(caps.get("tools") or [])
            from app.agent.public_policy import NON_DELEGABLE_TOOLS
            allowed_public_tools = (configured - NON_DELEGABLE_TOOLS) | set(ANONYMOUS_CHAT_TOOLS)
        tools = {
            name: info for name, info in tools.items()
            if name in allowed_public_tools
        }

    if gate_caller_access and agent_id:
        try:
            from app.agent.profiles import filter_profile_tools
            tools, _member = await filter_profile_tools(agent_id, user_id, tools)
        except Exception as exc:
            logger.warning("agent profile tool gate failed closed for %s: %s", agent_id, exc)
            tools = {name: info for name, info in tools.items() if name in ANONYMOUS_CHAT_TOOLS}

    # Propagate requires_confirmation and destructive from BUILTIN_TOOL_METADATA
    # to built-in ToolInfo entries. DB tools already have these set from their row;
    # built-ins need them applied from metadata.
    for name, info in tools.items():
        if name in BUILTIN_TOOL_METADATA:
            meta = BUILTIN_TOOL_METADATA[name]
            if not info.requires_confirmation and meta.get("requires_confirmation", False):
                info.requires_confirmation = True
            if meta.get("destructive", False):
                info.destructive = True

    # Phase 5: enforce the per-agent deny list before any caller can construct a
    # prompt index or provider schema. An explicit deny is universal: every tool
    # is invisible and unavailable when denied.
    # NOTE: delegate_to_agent / list_delegatable_agents are deliberately NOT
    # always-on. They are opt-in via the "agent_orchestration" ability and are
    # only injected (in _inject_builtin_tools) when that ability is enabled.
    if allowed_tools:
        denied = set(allowed_tools)
        for name in list(tools.keys()):
            if name in denied:
                del tools[name]

    # Phase 5b: enforce custom_tool_ids (Tier-3 opt-in DB tools).
    # When a non-empty list is provided, DB tools not in that list are removed.
    # Empty list / None = keep all DB tools (backward-compatible default).
    if custom_tool_ids:
        allowed_id_set = set(custom_tool_ids)
        for name in list(tools.keys()):
            ti = tools[name]
            # Only filter tools that came from the DB (have a tool_id)
            if ti.tool_id and ti.tool_id not in allowed_id_set:
                del tools[name]

    # ── Optimizer pipeline agent: full tool access ────────────────────────────────
    # The single Optimizer agent gets the full toolset — it does everything:
    # analysis, codebase search, diagnostics, trials, judging, and deploying.
    # No tool restrictions needed (the old closer-only restriction is removed).
    _OPT_PIPELINE_TOOLS = {
    }
    _opt_keep = _OPT_PIPELINE_TOOLS.get(agent_template_id)
    if _opt_keep is not None:
        for name in list(tools.keys()):
            if name not in _opt_keep:
                del tools[name]

    # Contract children have a process-local immutable ceiling. This final
    # filter also removes Tier-1/bootstrap tools that ordinary allowed_tools
    # intentionally cannot hide.
    try:
        from app.agent.contract_permissions import filter_tools
        tools = filter_tools(tools)
    except Exception:
        if os.environ.get("WEBAGENT_CONTRACT_SUBPROCESS") == "1":
            tools = {}

    return tools
