"""Agent Management ability — fully self-contained drop-in.

See plugins/abilities/_TEMPLATE.py for the contract. This file carries the
ENTIRE agent-management capability: the in-process tool handlers (agent/template/
prompt/ability/tool/skill CRUD, all user-scoped and ownership-enforced), their
JSON schemas, the danger labels, the factory that closes the handlers over the
calling ``user_id``, AND the per-agent limit enforcement layered on top.

These tools replace the old "manage agents via the REST API over http_request"
approach, which forced the agent to make HTTP calls against the very server it
runs inside (localhost:8080) — a self-referential loop that broke whenever the
server was busy or restarting.

Every tool here:
  - runs in-process (direct DB calls, no HTTP, no self-calling),
  - is scoped to the calling user via the harness-injected ``user_id`` — the
    model never passes an identity and cannot act on another user's agents,
  - enforces ownership in code (not by prompt instruction): writes go through
    DB methods that filter on the agent's ``admin_users`` membership.

Runtime enforcement of per-agent management limits (max agents, per-session
edit/update counters, custom-only restriction) is layered on top of the
handlers via wrapping in ``build_tools``. The config is read from the
agent_management connection row's ``config.ability_settings`` JSON, persisted by
the UI through the standard connections endpoint.

Heavy imports (the DB, tool_modes, loader) stay LAZY — inside ``build_tools`` /
handler closures — so importing this module is cheap and side-effect-free; the
loader reads TOOL_SCHEMAS / DESTRUCTIVE only AFTER calling ``build_tools``.

Data domains:
  | Domain                  | Access      | Tool(s)                                 |
  |-------------------------|-------------|-----------------------------------------|
  | agent_templates         | read        | list_agent_templates                    |
  | agent_prompt_templates  | read        | list_agent_templates(template_id=...)   |
  | agents (own only)       | read/write  | list_my_agents, get_agent, create_agent, update_agent |
  | agent_prompts (own)     | read/write  | edit_agent_prompt                       |
  | agent abilities (own)   | read/write  | set_agent_ability                       |
  | user sessions (own)     | read/write  | list_user_sessions, manage_user_session, create_user_session, kick_user_session |

Gated by the ``agent_management`` ability (a pure behavioural toggle — no
platform secret). Carries only a truncated last-message peek for session
triage (list_user_sessions) — full transcripts stay under the heavier
``codebase_admin`` ability. No filesystem access.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Populated inside build_tools() from the constants below so they can never
# drift; the loader reads them AFTER calling build_tools().
TOOL_SCHEMAS: dict = {}
DESTRUCTIVE: set = set()


# ── JSON helpers ──────────────────────────────────────────────────────────────

def _ok(**kw) -> str:
    return json.dumps({"status": "ok", **kw}, default=str)


def _err(message: str, **kw) -> str:
    return json.dumps({"status": "error", "message": message, **kw}, default=str)


async def _emit_agent_created(user_id: str, agent: dict) -> None:
    """Live-sync the owner's open Agents page after an agent is created in-process.

    The UI "New Agent" button creates via the REST endpoint (app/api/agents.py),
    which broadcasts an ``agent_created`` WebSocket event so every open Agents
    grid refreshes itself without a manual reload. This in-process tool path
    creates the agent directly in the DB and bypasses that endpoint, so without
    this emit the grid stayed stale until the user refreshed. Mirrors the REST
    event shape exactly so the existing frontend handler (``onAgentLifecycleEvent``)
    picks it up unchanged. Fire-and-forget: a delivery failure must never fail
    the creation."""
    if not user_id or not agent:
        return
    try:
        from app.api.chat import notify_user
        await notify_user(user_id, {
            "type": "agent_created", "user_id": user_id, "agent": agent,
        })
    except Exception as e:
        logger.debug("agent_created notify failed: %s", e)


async def _owns_agent(db, user_id: str, agent_id: str) -> bool:
    """True if the user is a global admin OR an admin of this agent.

    Mirrors app/api/agents.py::_is_agent_admin so the tool boundary matches the
    REST boundary exactly. Ownership is the gate for every write.
    """
    try:
        if await db.is_user_admin(user_id):
            return True
        roles = await db.get_agent_roles(agent_id)
        return user_id in (roles.get("admin_users") or [])
    except Exception as e:
        logger.warning("ownership check failed for agent %s / user %s: %s", agent_id, user_id, e)
        return False


def _read_template_slots(db, template_id: str) -> List[dict]:
    """Read the canonical (admin-base) prompt slots for a template, read-only.

    Uses the backend's portable connection (works on both SQLite and Postgres
    via pg_portable). agent_prompt_templates is read-only here — the Agent
    Manager may inspect canonical slot content but never edit the templates.
    """
    conn = db._get_conn()
    try:
        rows = conn.execute(
            """SELECT slot_name, order_index, lock, merge_mode, content, version, source
               FROM agent_prompt_templates
               WHERE template_id = ?
               ORDER BY order_index ASC""",
            (template_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ── Tool catalogue: availability + permission + danger ────────────────────────
# The two per-tool knobs from the Tools panel, in readable form:
#   availability — "sent" (full schema every turn) | "discover" (load on demand)
#   permission   — "auto" (runs unattended) | "ask" (confirm first) | "deny" (blocked)
_AVAIL_TO_MODE = {"sent": "always", "discover": "discoverable"}
_MODE_TO_AVAIL = {"always": "sent", "discoverable": "discover", "core": "core"}
_VALID_PERMISSIONS = ("auto", "ask", "deny")
_VALID_TRIGGERS = (
    "user_input", "slash_command", "tool_call", "schedule", "webhook", "background",
)


def _as_json_list(v) -> list:
    if isinstance(v, str):
        try:
            return json.loads(v) or []
        except Exception:
            return []
    return v or []


def _as_json_obj(v) -> dict:
    if isinstance(v, str):
        try:
            return json.loads(v) or {}
        except Exception:
            return {}
    return v or {}


async def _agent_tool_catalog(db, agent_id: str, user_id: str) -> Optional[List[dict]]:
    """Build the agent's full tool catalogue — one row per tool its enabled
    abilities provide, each carrying:
      - availability : "sent" | "discover" | "core"
      - permission   : "auto" | "ask" | "deny"
      - locked       : True for core meta-tools (cannot be changed)
      - destructive  : the tool's BUILT-IN danger label (read-only, code-defined)

    Returns None if the agent doesn't exist. The danger label comes from the
    tool's own definition — policy can require confirmation or deny a tool, but
    nothing here relabels an inherently dangerous tool as safe.
    """
    agent = await db.get_agent_by_id(agent_id)
    if not agent:
        return None
    from app.tools.loader import load_tools
    from app.tools.tool_modes import resolve_mode, is_locked, ability_for_tool

    deny = set(_as_json_list(agent.get("allowed_tools")))
    ask = set(_as_json_obj(agent.get("safety_policy")).get("destructive_tools") or [])
    modes = await db.get_agent_tool_modes(agent_id)

    # Load the full ability-gated set with an EMPTY deny list so denied tools
    # still appear — otherwise the manager couldn't see or re-enable them.
    try:
        tools = await load_tools(
            user_id, agent_id=agent_id,
            agent_template_id=agent.get("template_id"),
            allowed_tools=[], session_id="",
        )
    except Exception as e:
        logger.warning("_agent_tool_catalog: load_tools failed for %s: %s", agent_id, e)
        tools = {}

    out: List[dict] = []
    for name, info in sorted(tools.items()):
        try:
            desc = (info.handler.__doc__ or "").strip().split("\n")[0]
        except Exception:
            desc = ""
        permission = "deny" if name in deny else ("ask" if name in ask else "auto")
        out.append({
            "name": name,
            "description": desc,
            "ability": ability_for_tool(name),
            "availability": _MODE_TO_AVAIL.get(resolve_mode(name, modes), "sent"),
            "permission": permission,
            "locked": is_locked(name),
            "destructive": bool(getattr(info, "destructive", False)
                                or getattr(info, "requires_confirmation", False)),
        })
    return out


# ── Templates (read-only) ─────────────────────────────────────────────────────

async def list_agent_templates(
    template_id: Optional[str] = None,
    include_admin: bool = False,
    user_id: str = "",
) -> str:
    """List the agent templates available to build new agents from.

    Without ``template_id`` → returns the catalogue of templates (id, name,
    description, model, icon). With ``template_id`` → also returns that
    template's canonical prompt slots (read-only) so you can see what an agent
    cloned from it will start with.

    A template is only a starting point — create_agent can also build a
    from-scratch agent with NO template (pass template_id="none"), so if nothing
    here fits, start blank and write the persona yourself.
    """
    try:
        from app.db import get_db
        db = get_db()
        # Only platform admins may see admin-only templates.
        allow_admin = include_admin and await db.is_user_admin(user_id)
        templates = await db.list_agent_templates(include_admin=allow_admin)
        slim = [
            {
                "id": t.get("id"),
                "name": t.get("name"),
                "description": t.get("description"),
                "model": t.get("model"),
                "icon": t.get("icon"),
                "access_level": t.get("access_level"),
            }
            for t in templates
        ]
        if template_id:
            match = next((t for t in slim if t["id"] == template_id), None)
            if not match:
                return _err(f"Template '{template_id}' not found or not visible to you.")
            slots = _read_template_slots(db, template_id)
            return _ok(template=match, slots=slots, slot_count=len(slots))
        return _ok(count=len(slim), templates=slim)
    except Exception as e:
        logger.error("list_agent_templates failed: %s", e)
        return _err(str(e))


# ── Agents (read) ─────────────────────────────────────────────────────────────

async def list_my_agents(user_id: str = "") -> str:
    """List the agents this user owns (plus visible system agents).

    Each entry carries a ``source`` of 'custom' (the user's own, editable) or
    'template' (system, read-only).
    """
    try:
        from app.db import get_db
        db = get_db()
        is_admin = await db.is_user_admin(user_id)
        agents = await db.list_agents_for_user(user_id, include_admin=is_admin)
        slim = [
            {
                "id": a.get("id"),
                "name": a.get("name"),
                "description": a.get("description"),
                "template_id": a.get("template_id"),
                "model": a.get("model"),
                "provider": a.get("provider"),
                "source": a.get("source"),
            }
            for a in agents
        ]
        return _ok(count=len(slim), agents=slim)
    except Exception as e:
        logger.error("list_my_agents failed: %s", e)
        return _err(str(e))


async def get_agent(agent_id: str, user_id: str = "") -> str:
    """Get one agent the user can see, with its prompt slots and enabled abilities.

    Read-only. The agent must be owned by or visible to this user.
    """
    try:
        from app.db import get_db
        db = get_db()
        if not agent_id:
            return _err("agent_id is required.")
        # Visibility: it must appear in the user's agent list.
        is_admin = await db.is_user_admin(user_id)
        visible = await db.list_agents_for_user(user_id, include_admin=is_admin)
        if not any(a.get("id") == agent_id for a in visible):
            return _err("Agent not found or not visible to you.")

        full = await db.fetch_agent_by_id_with_context(agent_id, user_id=user_id)
        if not full:
            return _err("Agent not found.")
        slots = await db.list_slots(agent_id, user_id=user_id)
        try:
            conns = await db.get_agent_connections(agent_id)
            abilities = [c["connection_type"] for c in conns
                         if c.get("section") == "ability" and c.get("enabled")]
        except Exception:
            abilities = []
        agent = {
            "id": full.get("id"),
            "name": full.get("name"),
            "description": full.get("description"),
            "template_id": full.get("template_id"),
            "model": full.get("model"),
            "provider": full.get("provider"),
            "temperature": full.get("temperature"),
            "max_tokens": full.get("max_tokens"),
            "max_turn_count": full.get("max_turn_count"),
            "max_wall_seconds": full.get("max_wall_seconds"),
            "max_identical_tool_calls": full.get("max_identical_tool_calls"),
            "max_stall_strikes": full.get("max_stall_strikes"),
            "trigger_type": full.get("trigger_type"),
            "trigger_key": full.get("trigger_key"),
            "loop_logic": _as_json_list(full.get("loop_logic")),
            "user_mode": full.get("user_mode"),
        }
        from app.agent.cache_profiles import profile_from_metadata
        _cache_meta = _as_json_obj(full.get("metadata"))
        agent["capability_profile"] = profile_from_metadata(_cache_meta)
        agent["cache_profile_version"] = _cache_meta.get("cache_profile_version", 1)
        agent["prompt_layout_version"] = _cache_meta.get("prompt_layout_version", 2)
        agent["capability_extensions"] = _cache_meta.get("capability_extensions") or []
        # Tool overview: counts + only the tools whose settings differ from the
        # default (sent + auto). Use list_agent_tools for the full per-tool list.
        catalog = await _agent_tool_catalog(db, agent_id, user_id) or []
        customized = [t for t in catalog
                      if t["availability"] != "sent" or t["permission"] != "auto"]
        tools_overview = {
            "total": len(catalog),
            "discover": sum(1 for t in catalog if t["availability"] == "discover"),
            "ask": sum(1 for t in catalog if t["permission"] == "ask"),
            "deny": sum(1 for t in catalog if t["permission"] == "deny"),
            "customized": customized,
        }
        return _ok(agent=agent, abilities=abilities, tools=tools_overview,
                   slots=[{"slot_name": s["slot_name"], "order_index": s["order_index"],
                           "lock": s["lock"], "content": s["content"]} for s in slots])
    except Exception as e:
        logger.error("get_agent failed: %s", e)
        return _err(str(e))


# ── Agents (write) ────────────────────────────────────────────────────────────

async def create_agent(
    name: str,
    template_id: str = "",
    description: str = "",
    capability_profile: str = "simple",
    ability_extensions: Optional[List[str]] = None,
    user_id: str = "",
) -> str:
    """Create a new agent owned by this user. YOU choose how it starts.

    First call list_agent_templates to see the available starting points, then
    decide between two paths:
      - **From a template** — pass that template's ``template_id``. The new
        agent's config and prompt slots are copied from it.
      - **From scratch (no template)** — pass ``template_id="none"`` (or leave it
        blank). The agent is a true blank slate: no config or prompts inherited,
        just the app-global baseline identity. You then write its persona with
        edit_agent_prompt. Use this when no template fits.

    Choose the smallest nested cache profile that covers the job: ``simple``,
    ``standard``, or ``advanced``. Add unusual, explicitly justified abilities
    with ``ability_extensions``. Specialized schemas remain discoverable so
    differently capable agents share the same small first-call tool schema.
    """
    try:
        from app.db import get_db
        db = get_db()
        if not name or not name.strip():
            return _err("Agent name is required.")
        from app.agent.cache_profiles import PROFILE_ABILITIES, normalize_profile, profile_abilities
        _requested_profile = str(capability_profile or "").strip().lower()
        if _requested_profile not in PROFILE_ABILITIES:
            return _err("capability_profile must be simple, standard, or advanced.")
        profile = normalize_profile(_requested_profile)
        requested_abilities = profile_abilities(profile, ability_extensions)
        from app.admin.integrations import get_admin_configured_providers
        configured = set(await get_admin_configured_providers(user_id))
        unavailable = [a for a in requested_abilities if a not in configured]
        if unavailable:
            return _err(
                "These profile abilities are not configured by the app admin: "
                + ", ".join(unavailable)
            )
        from app.entitlements.resources import (
            ResourceEntitlementError, agent_resource_lock,
            enforce_agent_materialization,
        )
        async with agent_resource_lock(user_id):
            try:
                await enforce_agent_materialization(
                    db, user_id, template_id=template_id or "none",
                )
            except ResourceEntitlementError as exc:
                return _err(str(exc), **exc.detail())
            agent = await db.create_custom_agent(
                user_id=user_id,
                name=name.strip(),
                # Pass through verbatim: a real id clones that template; a blank /
                # "none" value creates a from-scratch agent (the DB layer normalises
                # the no-template sentinels). No silent fallback to "default".
                template_id=template_id or "",
                capability_profile=profile,
                capability_extensions=ability_extensions or [],
                seed_abilities=False,  # bare genui — abilities added deliberately
            )
        slim = {
            "id": agent.get("id"),
            "name": agent.get("name"),
            "description": agent.get("description"),
            "template_id": agent.get("template_id"),
            "capability_profile": profile,
            "abilities": requested_abilities,
        }
        # Live-sync the owner's open Agents page (see _emit_agent_created).
        await _emit_agent_created(user_id, slim)
        return _ok(agent=slim)
    except Exception as e:
        logger.error("create_agent failed: %s", e)
        return _err(str(e))


async def update_agent(
    agent_id: str,
    name: Optional[str] = None,
    description: Optional[str] = None,
    model: Optional[str] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    max_turn_count: Optional[int] = None,
    max_wall_seconds: Optional[int] = None,
    max_identical_tool_calls: Optional[int] = None,
    max_stall_strikes: Optional[int] = None,
    trigger_type: Optional[str] = None,
    trigger_key: Optional[str] = None,
    chat_ui: Optional[Dict[str, Any]] = None,
    user_id: str = "",
) -> str:
    """Update editable config fields on one of the user's own agents.

    Covers identity (name, description), the model settings (model, temperature,
    max_tokens), the guardrail limits (max_turn_count, max_wall_seconds,
    max_identical_tool_calls, max_stall_strikes), the trigger (trigger_type,
    trigger_key), and a partial per-agent ``chat_ui`` design override. ``chat_ui``
    is deep-merged into this agent's ``metadata.chat_ui``; it never changes the
    app-wide design defaults or another agent. Only supplied fields change. Use
    set_agent_tool for per-tool availability/permission, and set_agent_ability for abilities.

    Ownership is enforced: the update affects nothing (and returns an error) if
    the agent isn't yours.
    """
    try:
        from app.db import get_db
        db = get_db()
        if not agent_id:
            return _err("agent_id is required.")
        if not await _owns_agent(db, user_id, agent_id):
            return _err("You can only edit agents you own.")
        if trigger_type is not None and trigger_type not in _VALID_TRIGGERS:
            return _err(f"trigger_type must be one of: {', '.join(_VALID_TRIGGERS)}.")
        if chat_ui is not None and not isinstance(chat_ui, dict):
            return _err("chat_ui must be an object.")
        updates: Dict[str, Any] = {}
        for k, v in (
            ("name", name), ("description", description), ("model", model),
            ("temperature", temperature), ("max_tokens", max_tokens),
            ("max_turn_count", max_turn_count), ("max_wall_seconds", max_wall_seconds),
            ("max_identical_tool_calls", max_identical_tool_calls),
            ("max_stall_strikes", max_stall_strikes),
            ("trigger_type", trigger_type), ("trigger_key", trigger_key),
        ):
            if v is not None:
                updates[k] = v
        if chat_ui is not None:
            current = await db.get_agent_by_id(agent_id)
            if not current:
                return _err("Agent not found or not owned by you.")
            raw_meta = current.get("metadata")
            try:
                metadata = json.loads(raw_meta) if isinstance(raw_meta, str) else dict(raw_meta or {})
            except (TypeError, ValueError):
                metadata = {}
            from app.api.agents import _merge_chat_ui
            metadata["chat_ui"] = _merge_chat_ui(metadata.get("chat_ui"), chat_ui)
            updates["metadata"] = metadata
        if not updates:
            return _err("No fields to update were provided.")
        updated = await db.update_agent_fields(agent_id=agent_id, user_id=user_id, updates=updates)
        if updated is None:
            return _err("Agent not found or not owned by you.")
        return _ok(agent={"id": updated.get("id"), "name": updated.get("name"),
                          "description": updated.get("description"),
                          "model": updated.get("model")})
    except Exception as e:
        logger.error("update_agent failed: %s", e)
        return _err(str(e))


# ── Prompts (read/write, own agents) ──────────────────────────────────────────

async def edit_agent_prompt(
    action: str,
    agent_id: str,
    slot_name: Optional[str] = None,
    content: Optional[str] = None,
    lock: Optional[bool] = None,
    merge_mode: Optional[str] = None,
    order_index: Optional[int] = None,
    user_id: str = "",
) -> str:
    """Read or edit the prompt slots of one of the user's own agents.

    Actions:
      list   → list all admin-base slots for the agent
      get    → get one slot by slot_name
      insert → create a new slot (slot_name + content required)
      update → replace a slot's content (slot_name + content required)
      delete → delete a slot by slot_name

    Reads require the agent to be visible to you; writes require you to own it.
    This generalises the older single-agent db_query to any agent you own.
    """
    try:
        from app.db import get_db
        db = get_db()
        if not agent_id:
            return _err("agent_id is required.")

        writing = action in ("insert", "update", "delete")
        if writing:
            if not await _owns_agent(db, user_id, agent_id):
                return _err("You can only edit prompts on agents you own.")
        else:
            is_admin = await db.is_user_admin(user_id)
            visible = await db.list_agents_for_user(user_id, include_admin=is_admin)
            if not any(a.get("id") == agent_id for a in visible):
                return _err("Agent not found or not visible to you.")

        if action == "list":
            slots = await db.list_slots(agent_id, user_id=user_id)
            return _ok(count=len(slots), slots=slots)

        if action == "get":
            if not slot_name:
                return _err("slot_name required for get.")
            slots = await db.list_slots(agent_id, user_id=user_id)
            slot = next((s for s in slots if s["slot_name"] == slot_name), None)
            if not slot:
                return _err(f"Slot '{slot_name}' not found.")
            return _ok(slot=slot)

        if action in ("insert", "update"):
            if not slot_name or content is None:
                return _err("slot_name and content are required.")
            existing = await db.list_slots(agent_id)
            current = next((s for s in existing if s["slot_name"] == slot_name), None)
            resolved_order = order_index if order_index is not None else (
                current["order_index"] if current
                else (max((s["order_index"] or 0) for s in existing) + 10 if existing else 10)
            )
            resolved_lock = lock if lock is not None else (current["lock"] if current else False)
            resolved_mode = merge_mode if merge_mode is not None else (
                current.get("merge_mode") if current else "replace")
            await db.upsert_slot(
                agent_id=agent_id,
                slot_name=slot_name,
                order_index=int(resolved_order),
                lock=bool(resolved_lock),
                merge_mode=resolved_mode,
                content=content,
                updated_by=f"agent_mgmt:{user_id}",
            )
            return _ok(slot_name=slot_name)

        if action == "delete":
            if not slot_name:
                return _err("slot_name required for delete.")
            n = await db.delete_slot(agent_id, slot_name)
            return _ok(slot_name=slot_name, deleted_rows=n)

        return _err(f"Unknown action '{action}'. Use: list, get, insert, update, delete.")
    except Exception as e:
        logger.error("edit_agent_prompt failed: %s", e)
        return _err(str(e))


# ── Abilities (read/write, own agents) ────────────────────────────────────────

# Known write/mutating tools per ability — a safety net so read_only reliably
# denies them even if a tool's built-in `destructive` flag isn't set. The
# destructive-flag scan below is the primary mechanism; this just guarantees the
# common cases (notably UI Admin "read-only", used to let an agent SEE the app's
# UI code without being able to change it).
_READ_ONLY_DENY_FALLBACK = {
    "ui_admin": {"write_source", "edit_source", "patch_source", "delete_source"},
    "codebase_admin": {"write_source", "edit_source", "patch_source", "delete_source",
                       "run_command", "restart_server", "commit_and_push", "db_query"},
}


async def set_agent_ability(
    agent_id: str,
    ability: str,
    enabled: bool = True,
    read_only: bool = False,
    deny_tools: Optional[List[str]] = None,
    ask_tools: Optional[List[str]] = None,
    user_id: str = "",
) -> str:
    """Turn an ability on or off for one of the user's own agents.

    ``ability`` is a connection_type from the ability catalogue (e.g.
    'codebase_admin', 'web_access', 'diagnostics', 'agent_orchestration',
    'browser_control', 'create_tools', 'image_generation', 'visualizer',
    'ui_admin', 'agent_management').

    Optional one-call tool tightening (applied only when enabling):
      - read_only=True : deny every WRITE/mutating tool the ability provides, so
                         the agent gets the ability's read tools only. Use this to
                         give an agent UI Admin (or Codebase Admin) so it can READ
                         the app's UI/source without being able to change it.
      - deny_tools=[…] : also deny these specific tools (must belong to the ability).
      - ask_tools=[…]  : require user confirmation for these specific tools.
    These compose with set_agent_tool, which can still tune individual tools later.

    Enabling an ability the app admin hasn't configured is refused (mirrors the
    REST connection endpoint). Disabling is always allowed.
    """
    try:
        from app.db import get_db
        db = get_db()
        if not agent_id or not ability:
            return _err("agent_id and ability are required.")
        if not await _owns_agent(db, user_id, agent_id):
            return _err("You can only change abilities on agents you own.")

        if enabled:
            from app.admin.integrations import get_admin_configured_providers
            if ability not in await get_admin_configured_providers(user_id):
                return _err(
                    f"Ability '{ability}' is not configured in App Config → Agent Abilities. "
                    f"An app admin must enable it before it can be turned on per-agent."
                )
        from app.entitlements.resources import (
            ResourceEntitlementError, connection_resource_lock,
            enforce_connection_change,
        )
        async with connection_resource_lock(user_id):
            try:
                await enforce_connection_change(
                    db, user_id, agent_id, ability, enabling=bool(enabled),
                )
            except ResourceEntitlementError as exc:
                return _err(str(exc), **exc.detail())
            await db.upsert_agent_connection(
                agent_id=agent_id,
                connection_type=ability,
                section="ability",
                enabled=bool(enabled),
                config={},
            )
        # Newly-enabled ability → default its tools to "discoverable" (hybrid
        # rule); only fills tools with no explicit mode yet. No-op otherwise.
        applied: Dict[str, Any] = {}
        if enabled:
            try:
                from app.tools.tool_modes import tools_for_ability
                seed = tools_for_ability(ability)
                if seed:
                    await db.seed_agent_tool_modes(agent_id, seed, "discoverable")
            except Exception as e:
                logger.debug("tool-mode seed for ability %s failed: %s", ability, e)

            # ── Optional one-call read-only / deny / ask tightening ──
            if read_only or deny_tools or ask_tools:
                # Names this ability actually provides (from the live catalog so
                # destructive flags are accurate); reject names that don't belong.
                catalog = await _agent_tool_catalog(db, agent_id, user_id) or []
                ability_tools = {t["name"]: t for t in catalog if t.get("ability") == ability}
                deny: set = set()
                ask: set = set()
                if read_only:
                    deny |= {n for n, t in ability_tools.items() if t.get("destructive")}
                    deny |= (_READ_ONLY_DENY_FALLBACK.get(ability, set()) & set(ability_tools))
                for n in (deny_tools or []):
                    if n in ability_tools:
                        deny.add(n)
                for n in (ask_tools or []):
                    if n in ability_tools and n not in deny:
                        ask.add(n)
                if deny or ask:
                    await _apply_tool_permissions(db, agent_id, user_id, deny, ask)
                    applied = {"denied": sorted(deny), "ask": sorted(ask)}

        return _ok(agent_id=agent_id, ability=ability, enabled=bool(enabled), **({"tools": applied} if applied else {}))
    except Exception as e:
        logger.error("set_agent_ability failed: %s", e)
        return _err(str(e))


async def _apply_tool_permissions(db, agent_id: str, user_id: str, deny: set, ask: set) -> None:
    """Merge a batch of deny/ask tool permissions into the agent in one write.

    deny wins over ask for any tool named in both. Existing permissions for other
    tools are preserved (this is additive — it never clears another tool's gate).
    """
    agent = await db.get_agent_by_id(agent_id)
    if not agent:
        return
    deny_set = set(_as_json_list(agent.get("allowed_tools")))
    safety = _as_json_obj(agent.get("safety_policy"))
    ask_set = set(safety.get("destructive_tools") or [])
    for n in deny:
        ask_set.discard(n)
        deny_set.add(n)
    for n in ask:
        if n not in deny_set:
            ask_set.add(n)
    safety["destructive_tools"] = sorted(ask_set)
    await db.update_agent_fields(
        agent_id=agent_id, user_id=user_id,
        updates={"allowed_tools": sorted(deny_set), "safety_policy": safety},
    )


# ── Tools (read/write, own agents) ────────────────────────────────────────────

async def list_agent_tools(
    agent_id: str,
    ability: Optional[str] = None,
    query: Optional[str] = None,
    user_id: str = "",
) -> str:
    """List one of your visible agents' tools, each with its current settings.

    Every row carries:
      - availability : 'sent' (full schema every turn) | 'discover' (loaded on
                       demand) | 'core' (always sent, locked)
      - permission   : 'auto' (runs unattended) | 'ask' (confirm first) |
                       'deny' (the agent can't use it)
      - locked       : true for core tools (cannot be changed)
      - destructive  : the tool's built-in danger label (read-only)
      - ability      : the ability that provides the tool, if any

    Optional filters: ``ability`` (exact match) and ``query`` (substring on the
    tool name or description). Read-only — use set_agent_tool to change settings.
    """
    try:
        from app.db import get_db
        db = get_db()
        if not agent_id:
            return _err("agent_id is required.")
        is_admin = await db.is_user_admin(user_id)
        visible = await db.list_agents_for_user(user_id, include_admin=is_admin)
        if not any(a.get("id") == agent_id for a in visible):
            return _err("Agent not found or not visible to you.")
        catalog = await _agent_tool_catalog(db, agent_id, user_id)
        if catalog is None:
            return _err("Agent not found.")
        if ability:
            catalog = [t for t in catalog if t["ability"] == ability]
        if query:
            q = query.lower()
            catalog = [t for t in catalog
                       if q in t["name"].lower() or q in (t["description"] or "").lower()]
        return _ok(count=len(catalog), tools=catalog)
    except Exception as e:
        logger.error("list_agent_tools failed: %s", e)
        return _err(str(e))


async def set_agent_tool(
    agent_id: str,
    tool: str,
    availability: Optional[str] = None,
    permission: Optional[str] = None,
    user_id: str = "",
) -> str:
    """Set ONE tool's availability and/or permission on one of your own agents.

      availability : 'sent' = full schema sent every turn; 'discover' = name
                     only until the agent loads it on demand (saves context).
      permission   : 'auto' = runs unattended; 'ask' = requires confirmation
                     first; 'deny' = the agent cannot use the tool at all.

    Provide at least one of the two. Core tools are locked and cannot be changed.
    This sets POLICY only — a tool's built-in danger label is fixed in code and
    cannot be changed here (you can require confirmation or deny a tool, but you
    cannot mark a dangerous tool as safe). Check tool names with list_agent_tools.
    """
    try:
        from app.db import get_db
        db = get_db()
        if not agent_id or not tool:
            return _err("agent_id and tool are required.")
        if availability is None and permission is None:
            return _err("Provide availability and/or permission.")
        if availability is not None and availability not in _AVAIL_TO_MODE:
            return _err("availability must be 'sent' or 'discover'.")
        if permission is not None and permission not in _VALID_PERMISSIONS:
            return _err("permission must be 'auto', 'ask', or 'deny'.")
        if not await _owns_agent(db, user_id, agent_id):
            return _err("You can only change tools on agents you own.")

        from app.tools.tool_modes import is_locked
        if is_locked(tool):
            return _err(f"'{tool}' is a core tool — it is always available and cannot be changed.")

        catalog = await _agent_tool_catalog(db, agent_id, user_id)
        if catalog is None:
            return _err("Agent not found.")
        if not any(t["name"] == tool for t in catalog):
            return _err(
                f"'{tool}' is not one of this agent's tools. Enable the ability that "
                f"provides it first, or check the name with list_agent_tools."
            )

        changed: Dict[str, Any] = {}
        if availability is not None:
            await db.set_agent_tool_mode(agent_id, tool, _AVAIL_TO_MODE[availability])
            changed["availability"] = availability

        if permission is not None:
            agent = await db.get_agent_by_id(agent_id)
            deny_set = set(_as_json_list(agent.get("allowed_tools")))
            safety = _as_json_obj(agent.get("safety_policy"))
            ask_set = set(safety.get("destructive_tools") or [])
            # Clear then re-apply, so the three states are mutually exclusive.
            deny_set.discard(tool)
            ask_set.discard(tool)
            if permission == "deny":
                deny_set.add(tool)
            elif permission == "ask":
                ask_set.add(tool)
            safety["destructive_tools"] = sorted(ask_set)
            updated = await db.update_agent_fields(
                agent_id=agent_id, user_id=user_id,
                updates={"allowed_tools": sorted(deny_set), "safety_policy": safety},
            )
            if updated is None:
                return _err("Agent not found or not owned by you.")
            changed["permission"] = permission

        return _ok(agent_id=agent_id, tool=tool, **changed)
    except Exception as e:
        logger.error("set_agent_tool failed: %s", e)
        return _err(str(e))


# ── Skills (read/write, own agents) ───────────────────────────────────────────

_VALID_SKILL_MODES = ("always_on", "selectable")


async def manage_agent_skills(
    action: str,
    agent_id: str,
    name: Optional[str] = None,
    description: Optional[str] = None,
    body: Optional[str] = None,
    mode: Optional[str] = None,
    enabled: Optional[bool] = None,
    user_id: str = "",
) -> str:
    """Read or edit the on-demand SKILLS of one of the user's own agents.

    A skill is a named knowledge pack stored on the agent. Each has:
      - name        : short identifier the agent uses with load_skill
      - description : ALWAYS shown to the agent — say WHEN to use this skill
      - body        : the full step-by-step instructions
      - mode        : 'always_on'  → body is always in the agent's context, or
                      'selectable' → body is hidden; the agent only sees the
                       name + description and pulls the body in with load_skill
                       when a task matches (use this for specialized playbooks)
      - enabled     : whether the skill is active at all

    Actions:
      list   → list the agent's skills (name, description, mode, enabled)
      set    → add or update a skill by name. NEW skill: name required, provide
               description + body; mode defaults to 'selectable', enabled true.
               EXISTING skill: omitted fields keep their current value.
      remove → delete a skill by name

    Reads require the agent be visible to you; writes require you to own it.
    Prefer 'selectable' for niche/specialized skills and 'always_on' only for
    guidance the agent should follow on every turn.
    """
    try:
        from app.db import get_db
        db = get_db()
        if not agent_id:
            return _err("agent_id is required.")

        if action == "list":
            is_admin = await db.is_user_admin(user_id)
            visible = await db.list_agents_for_user(user_id, include_admin=is_admin)
            if not any(a.get("id") == agent_id for a in visible):
                return _err("Agent not found or not visible to you.")
            skills = await db.get_agent_skills(agent_id)
            return _ok(count=len(skills), skills=skills)

        if not await _owns_agent(db, user_id, agent_id):
            return _err("You can only edit skills on agents you own.")
        skills = await db.get_agent_skills(agent_id)

        if action == "set":
            if not name or not name.strip():
                return _err("name is required for set.")
            nm = name.strip()
            if mode is not None and mode not in _VALID_SKILL_MODES:
                return _err("mode must be 'always_on' or 'selectable'.")
            existing = next((s for s in skills if s["name"].lower() == nm.lower()), None)
            if existing:
                if description is not None:
                    existing["description"] = description
                if body is not None:
                    existing["body"] = body
                if mode is not None:
                    existing["mode"] = mode
                if enabled is not None:
                    existing["enabled"] = bool(enabled)
            else:
                skills.append({
                    "name": nm,
                    "description": description or "",
                    "body": body or "",
                    "mode": mode or "selectable",
                    "enabled": True if enabled is None else bool(enabled),
                })
            saved = await db.set_agent_skills(agent_id, skills, updated_by=f"agent_mgmt:{user_id}")
            return _ok(
                skill=nm, count=len(saved),
                skills=[{"name": s["name"], "mode": s["mode"], "enabled": s["enabled"]} for s in saved],
            )

        if action == "remove":
            if not name or not name.strip():
                return _err("name is required for remove.")
            target = name.strip().lower()
            kept = [s for s in skills if s["name"].lower() != target]
            if len(kept) == len(skills):
                return _err(f"No skill named '{name}'.")
            saved = await db.set_agent_skills(agent_id, kept, updated_by=f"agent_mgmt:{user_id}")
            return _ok(removed=name, count=len(saved))

        return _err(f"Unknown action '{action}'. Use: list, set, remove.")
    except Exception as e:
        logger.error("manage_agent_skills failed: %s", e)
        return _err(str(e))


# ── User sessions (read) ──────────────────────────────────────────────────────

async def list_user_sessions(
    limit: int = 15,
    agent_id: str = "",
    include_hidden: bool = False,
    include_recycled: bool = False,
    peek_chars: int = 500,
    user_id: str = "",
) -> str:
    """List the user's chat sessions with their latest messages.

    Each session includes its last USER message and last ASSISTANT (agent)
    message, truncated to ``peek_chars`` — enough to judge whether the session
    is waiting on the user (a call to action: an open question, a choice, a
    "let me know what you think") or is complete (a finished answer with
    nothing pending), and to summarise what the session is about in one
    sentence. Sort by most recent activity first.

    Hidden sessions and recycled (in-the-bin) sessions are excluded unless you
    ask for them; ephemeral helper sessions ('spawn-*') are always excluded.
    Only the user's own sessions (or ones shared with them) are returned.
    """
    try:
        from app.db import get_db
        db = get_db()
        limit = max(1, min(int(limit or 15), 50))
        peek = max(120, min(int(peek_chars or 500), 2000))
        if not user_id:
            return _err("Missing caller identity.")
        conn = db._get_conn()
        try:
            # Defensive: older DBs may lack the optional columns.
            sess_cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()}
            has_hidden = "hidden" in sess_cols
            has_status = "status" in sess_cols

            where = "(s.user_id = ? OR s.participants LIKE ?)"
            params: list = [user_id, f'%"id": "{user_id}"%']
            if agent_id:
                where += " AND s.agent_id = ?"
                params.append(agent_id)
            if has_status and not include_recycled:
                where += " AND (s.status IS NULL OR s.status != 'recycled')"
            if has_hidden and not include_hidden:
                where += " AND (s.hidden IS NULL OR s.hidden = 0)"
            where += " AND s.id NOT LIKE 'spawn-%'"

            rows = conn.execute(
                f"""SELECT s.id, s.title, s.user_id, s.participants, s.agent_id,
                           s.created_at, s.updated_at
                    FROM sessions s
                    WHERE {where}
                    ORDER BY COALESCE(
                        (SELECT MAX(i.created_at) FROM interactions i
                         WHERE i.session_id = s.id),
                        s.updated_at, s.created_at) DESC
                    LIMIT ?""",
                params + [limit],
            ).fetchall()

            # Agent authority is stored in the agent plane, not the user's
            # session database. Resolve each referenced agent once after the
            # user-plane query rather than joining its retired local table.
            agent_names: dict[str, str] = {}
            for resolved_agent_id in {r[4] for r in rows if r[4]}:
                try:
                    agent = await db.get_agent_by_id(resolved_agent_id)
                except Exception as e:
                    logger.debug(
                        "list_user_sessions: agent lookup failed for %s: %s",
                        resolved_agent_id, e,
                    )
                    continue
                if agent:
                    agent_names[resolved_agent_id] = str(agent.get("name") or "")

            # Python-side participant verification (the LIKE above is a pre-filter).
            owned = []
            for r in rows:
                parts = []
                try:
                    parts = json.loads(r[3] or "[]") or []
                except Exception:
                    parts = []
                ids = {p.get("id") for p in parts if isinstance(p, dict)}
                if r[2] == user_id or user_id in ids:
                    owned.append(r)
            if not owned:
                return _ok(count=0, sessions=[])
            sids = [r[0] for r in owned]

            # Latest few user/assistant messages per session (window function —
            # works on SQLite >= 3.25 and Postgres; legacy rows fall back to
            # created_at ordering when session_seq is NULL).
            last_msgs: dict = {}
            try:
                qmarks = ",".join("?" * len(sids))
                mrows = conn.execute(
                    f"""SELECT session_id, role, content, created_at FROM (
                            SELECT session_id, role, content, created_at,
                                   ROW_NUMBER() OVER (
                                       PARTITION BY session_id
                                       ORDER BY COALESCE(session_seq, 0) DESC,
                                                created_at DESC) AS rn
                            FROM interactions
                            WHERE session_id IN ({qmarks})
                              AND role IN ('user', 'assistant')
                        ) WHERE rn <= 6""",
                    sids,
                ).fetchall()
                for m in mrows:
                    last_msgs.setdefault(m[0], []).append({
                        "role": m[1],
                        "content": (m[2] or "")[:peek],
                        "created_at": m[3],
                    })
            except Exception as e:
                logger.debug("list_user_sessions: last-message fetch failed: %s", e)

            counts: dict = {}
            try:
                qmarks = ",".join("?" * len(sids))
                for c in conn.execute(
                    f"SELECT session_id, COUNT(*) FROM interactions "
                    f"WHERE session_id IN ({qmarks}) GROUP BY session_id",
                    sids,
                ).fetchall():
                    counts[c[0]] = int(c[1])
            except Exception as e:
                logger.debug("list_user_sessions: message-count fetch failed: %s", e)

            sessions = []
            for r in owned:
                msgs = last_msgs.get(r[0], [])
                last_user = next((m for m in msgs if m["role"] == "user"), None)
                last_agent = next((m for m in msgs if m["role"] == "assistant"), None)
                sessions.append({
                    "id": r[0],
                    "title": r[1] or r[0][:12],
                    "agent_id": r[4],
                    "agent_name": agent_names.get(r[4], ""),
                    "created_at": r[5],
                    "updated_at": r[6],
                    "message_count": counts.get(r[0], 0),
                    "last_message_role": msgs[0]["role"] if msgs else None,
                    "last_message_at": msgs[0]["created_at"] if msgs else None,
                    "last_user_message": last_user["content"] if last_user else None,
                    "last_agent_message": last_agent["content"] if last_agent else None,
                })
            return _ok(count=len(sessions), sessions=sessions)
        finally:
            conn.close()
    except Exception as e:
        logger.error("list_user_sessions failed: %s", e)
        return _err(str(e))


async def _enqueue_session_push(ids: list, op: str = "upsert") -> None:
    """Best-effort hybrid remote push — the same outbox the REST routes use.

    Local-first writes are instantly visible; this queues the authoritative
    push to the remote authority so other devices converge. No-op when hybrid
    is off; a failure only costs convergence latency (the sync sweep catches
    it), never correctness. Deliberately does NOT import app.api.db_viewer —
    that router module is heavy and this ability must stay self-contained."""
    ids = [i for i in (ids or []) if i]
    if not ids:
        return
    try:
        from app.db.hybrid import hybrid_enabled, HybridBackend
        if not hybrid_enabled():
            return
        from app.db import get_db
        inst = get_db()
        hb = inst if isinstance(inst, HybridBackend) else getattr(inst, "_inner", None)
        if not isinstance(hb, HybridBackend):
            return
        from app.db.sync.outbox import Outbox
        await Outbox(hb.local).enqueue_many([("sessions", i, op) for i in ids])
    except Exception as e:
        logger.debug("session remote push skipped: %s", e)


# ── User sessions (write) ─────────────────────────────────────────────────────

async def manage_user_session(
    action: str,
    session_id: str,
    name: str = "",
    user_id: str = "",
) -> str:
    """Rename, hide, show, recycle, or restore one of the user's sessions.

    Actions:
      - rename   — set a new display title (``name``, max 80 chars). Locks the
        title so the auto-titler stops overwriting it.
      - hide     — hide the session from the chat sidebar (kept, not deleted).
      - show     — unhide a hidden session.
      - recycle  — send the session to the recycling bin (soft delete: the
        transcript is kept and it can be restored; any active agent loop is
        stopped).
      - restore  — bring a recycled session back to active.

    Only the user's own sessions (or ones shared with them) can be managed.
    Session ids come from ``list_user_sessions``.
    """
    valid = {"rename", "hide", "show", "recycle", "restore"}
    if action not in valid:
        return _err(f"action must be one of: {', '.join(sorted(valid))}.")
    if not session_id:
        return _err("session_id is required.")
    if action == "rename" and not (name or "").strip():
        return _err("name is required for rename.")
    try:
        from app.db import get_db
        db = get_db()
        conn = db._get_conn()
        try:
            row = conn.execute(
                "SELECT user_id, participants, status FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if not row:
                return _err("Session not found.")
            parts = []
            try:
                parts = json.loads(row[1] or "[]") or []
            except Exception:
                parts = []
            ids = {p.get("id") for p in parts if isinstance(p, dict)}
            if row[0] != user_id and user_id not in ids:
                return _err("Not authorized for this session.")

            if action == "rename":
                title = (name or "").strip()[:80]
                meta = {}
                try:
                    mrow = conn.execute(
                        "SELECT metadata FROM sessions WHERE id = ?", (session_id,)
                    ).fetchone()
                    meta = json.loads(mrow[0]) if (mrow and mrow[0]) else {}
                except Exception:
                    meta = {}
                if not isinstance(meta, dict):
                    meta = {}
                meta["auto_title_locked"] = True
                conn.execute(
                    "UPDATE sessions SET title = ?, metadata = ?, updated_at = datetime('now') "
                    "WHERE id = ?",
                    (title, json.dumps(meta), session_id),
                )
            elif action in ("hide", "show"):
                try:
                    cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()}
                    if "hidden" not in cols:
                        conn.execute("ALTER TABLE sessions ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0")
                except Exception:
                    pass
                conn.execute(
                    "UPDATE sessions SET hidden = ?, updated_at = datetime('now') WHERE id = ?",
                    (1 if action == "hide" else 0, session_id),
                )
            else:  # recycle | restore
                status = "recycled" if action == "recycle" else "active"
                cur = conn.execute(
                    "UPDATE sessions SET status = ?, updated_at = datetime('now') WHERE id = ?",
                    (status, session_id),
                )
                if not cur.rowcount:
                    return _err(
                        f"Session is not currently "
                        f"{'active' if action == 'recycle' else 'recycled'}."
                    )
                # Carry the run-family (spawned helpers etc.) with the parent,
                # exactly like the REST recycle/restore cascade.
                affected = [session_id]
                try:
                    from app.db.local import resolve_child_sessions
                    for cid in resolve_child_sessions(conn, [session_id]):
                        conn.execute(
                            "UPDATE sessions SET status = ?, updated_at = datetime('now') "
                            "WHERE id = ?",
                            (status, cid),
                        )
                        affected.append(cid)
                except Exception as e:
                    logger.debug("manage_user_session: child cascade failed: %s", e)
                if action == "recycle":
                    # Stop any live loop + clear active state so it can't resume.
                    try:
                        for sid in affected:
                            try:
                                await db.set_interrupt(sid)
                            except Exception:
                                pass
                            try:
                                await db.clear_session_active_state(sid)
                            except Exception:
                                pass
                    except Exception as e:
                        logger.debug("manage_user_session: loop-kill failed: %s", e)
                conn.commit()
                await _enqueue_session_push(affected, "upsert")
                return _ok(session_id=session_id, action=action,
                           children=len(affected) - 1)
            conn.commit()
            await _enqueue_session_push([session_id], "upsert")
            return _ok(session_id=session_id, action=action)
        finally:
            conn.close()
    except Exception as e:
        logger.error("manage_user_session failed: %s", e)
        return _err(str(e))


async def create_user_session(
    name: str,
    agent_id: str = "",
    user_id: str = "",
) -> str:
    """Create a brand-new chat session with the given title.

    Returns the new ``session_id``. The session starts empty and appears at the
    top of the user's session list as active. Optionally bind it to one of the
    user's agents with ``agent_id`` (must be an agent the user owns); otherwise
    the session is unbound and the user can pick an agent when they open it.
    """
    title = (name or "").strip()
    if not title:
        return _err("A session name is required.")
    title = title[:120]
    try:
        from app.db import get_db
        db = get_db()
        if agent_id:
            try:
                agent = await db.get_agent_by_id(agent_id)
                if not agent:
                    return _err(f"Agent '{agent_id}' not found.")
                if not await _owns_agent(db, user_id, agent_id):
                    return _err(f"Agent '{agent_id}' is not owned by this user.")
            except Exception as e:
                logger.debug("create_user_session: agent check failed: %s", e)
                return _err(f"Agent '{agent_id}' is not usable: {e}")
        session_id = str(uuid.uuid4())
        conn = db._get_conn()
        try:
            conn.execute(
                "INSERT INTO sessions (id, user_id, title, agent_id, status, "
                "created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'active', datetime('now'), datetime('now'))",
                (session_id, user_id, title, agent_id or None),
            )
            conn.commit()
        finally:
            conn.close()
        await _enqueue_session_push([session_id], "upsert")
        return _ok(session_id=session_id, title=title, agent_id=agent_id or "")
    except Exception as e:
        logger.error("create_user_session failed: %s", e)
        return _err(str(e))


async def kick_user_session(
    session_id: str,
    prompt: str,
    mode: str = "auto",
    timeout_seconds: int = 600,
    wait: bool = False,
    user_id: str = "",
) -> str:
    """Start real work inside an existing session — the "run it for me" tool.

    Injects ``prompt`` into ``session_id`` as a new user message and launches a
    supervised agent turn for that session's agent, exactly like an automation
    run: the reply streams into the session live, the run is recorded in
    session_runs (survives the caller leaving), and the session's transcript
    keeps the injected prompt so the user sees what was dispatched.

    - ``mode`` — ``"auto"`` (default): the run proceeds unattended and the
      session's agent executes its tools without pausing for confirmations
      (the whole call is confirm-gated here, so this is a deliberate dispatch).
      ``"ask"``: respect the agent's own per-tool posture — the run may pause
      waiting for confirmations.
      ``"plan"``: run the session in PLAN mode — research freely with read-only
      tools, present a plan (present_plan workspace), and do NOT execute;
      write/edit tools are gated and require user confirmation in the
      conversation.
    - ``wait`` — ``False`` (default): kick and return immediately; the run
      continues supervised in the background and you check on it later with
      list_user_sessions. ``True``: block until the run finishes and return its
      final reply (capped by ``timeout_seconds`` + a grace margin).

    Refuses to kick a session the user does not own, a recycled/dead session,
    or a session with no agent bound (there must be something to run).
    """
    prompt = (prompt or "").strip()
    session_id = (session_id or "").strip()
    if not session_id:
        return _err("A session_id is required.")
    if not prompt:
        return _err("A kickoff prompt is required — what should the session's agent do?")
    mode = (mode or "auto").strip().lower()
    if mode not in ("auto", "ask", "plan"):
        return _err("mode must be 'auto' (run unattended), 'ask' (respect per-tool confirmations), or 'plan' (research + present a plan, no execution).")
    timeout_seconds = max(30, min(int(timeout_seconds or 600), 3600))
    try:
        from app.db import get_db
        db = get_db()

        # Ownership + liveness: only the user's own (or shared) active sessions.
        conn = db._get_conn()
        try:
            row = conn.execute(
                "SELECT id, user_id, participants, agent_id, status FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return _err(f"Session '{session_id}' not found.")
        parts = []
        try:
            parts = json.loads(row[2] or "[]") or []
        except Exception:
            parts = []
        ids = {p.get("id") for p in parts if isinstance(p, dict)}
        if row[1] != user_id and user_id not in ids:
            return _err("That session belongs to another user.")
        if (row[4] or "") == "recycled":
            return _err("That session is in the recycling bin — restore it before kicking.")
        try:
            if hasattr(db, "is_session_dead") and await db.is_session_dead(session_id):
                return _err("That session is recycled/deleted and cannot be run.")
        except Exception as _de:
            logger.debug("kick_user_session: is_session_dead check failed: %s", _de)

        # The session's agent must exist and be runnable.
        agent_id = row[3] or ""
        if not agent_id:
            try:
                agent_id = await db.get_session_agent_id(session_id) or ""
            except Exception as _sa:
                logger.debug("kick_user_session: get_session_agent_id failed: %s", _sa)
        if not agent_id:
            return _err("That session has no agent bound — nothing to run. Create sessions with agent_id or bind one.")
        agent = await db.get_agent_by_id(agent_id)
        if not agent:
            return _err(f"Session's agent '{agent_id}' not found.")

        # Build history + system prompt BEFORE inserting the kick message, so the
        # injected prompt is the turn's new user message (no duplication).
        history: list = []
        try:
            from app.agent.session_history import build_openai_history_from_session
            history = await build_openai_history_from_session(
                db, user_id, session_id, agent_id=agent_id)
        except Exception as e:
            logger.debug("kick_user_session: history build failed: %s", e)
        system_prompt = ""
        try:
            from app.agent.prompts import build_system_prompt
            resolved = await db.resolve_prompts(agent_id, user_id=user_id)
            context_docs = [
                {"id": s["slot_name"], "context_type": s["slot_name"],
                 "title": s["slot_name"], "content": s["content"], "tags": []}
                for s in resolved
                if (s.get("content") or "").strip() and s.get("slot_name") != "automation"
            ]
            system_prompt = await build_system_prompt(
                context_docs, brain_context=None, user_id=user_id, agent_id=agent_id)
        except Exception as e:
            logger.debug("kick_user_session: prompt build failed: %s", e)

        # Live broadcast so an open session updates in real time, like automation.
        try:
            from app.api.chat import _emit_to_visualizers as _emit_live
        except Exception:
            _emit_live = None

        async def _broadcast(ev: dict) -> None:
            if not _emit_live:
                return
            try:
                await _emit_live(session_id, ev, user_id=user_id)
            except Exception:
                pass

        # Insert the synthetic user message (durable, visible in the transcript).
        turn_uid = None
        try:
            from app.db import get_db as _gdb
            _u_seq = None
            try:
                _u_seq = await db.next_session_seq(session_id, 1)
            except Exception:
                _u_seq = None
            turn_uid = await db.insert_interaction(
                user_id, session_id, role="user", content=prompt,
                channel="kick",
                metadata=json.dumps({"source": "kick", "session_id": session_id}),
                sender_id=user_id, receiver_id=agent_id, source="kick",
                session_seq=_u_seq,
            )
        except Exception as e:
            logger.debug("kick_user_session: could not insert kick message: %s", e)
        if turn_uid:
            await _broadcast({
                "type": "user_message", "level": "user",
                "content": prompt, "id": turn_uid, "source": "kick",
            })

        raw_allowed = agent.get("allowed_tools", [])
        if isinstance(raw_allowed, str):
            try:
                raw_allowed = json.loads(raw_allowed)
            except Exception:
                raw_allowed = []

        from app.agent.loop import run_agent_loop_buffered
        from app.agent.runner import run_supervised_turn, RunOutcome
        from app.agent.run_buffer import get_registry as _get_rb_reg

        async def _build(replaced: bool) -> "RunOutcome":
            # Kicked runs must register a RunBuffer exactly like web-send turns do
            # (app/api/chat.py start_turn): alternate engines persist their rows
            # WITHOUT session_seq, and _emit_to_visualizers only backfills the
            # ordering columns when a buffer stamps the events. Without a buffer
            # every row of a kicked run stays NULL-session_seq — invisible to the
            # session list and the live reconcile poll, so the session looks hung.
            _rb = None
            try:
                _rb = await _get_rb_reg().start_turn(
                    session_id=session_id, user_id=user_id,
                    turn_id=turn_uid or session_id, db=db)
            except Exception as _rbe:
                logger.debug("kick_user_session: run buffer start failed: %s", _rbe)
            try:
                reply = await run_agent_loop_buffered(
                    user_id=user_id, session_id=session_id, user_message=prompt,
                    system_prompt=system_prompt, agent_id=agent_id, history=history,
                    # Parent the run's assistant rows to the injected kick message,
                    # exactly like chat_send does for a web turn. Without this the
                    # rows land with parent_id=NULL — invisible to the summarizer's
                    # recovery sweep (which requires parent_id IS NOT NULL) and to
                    # the checklist auditor's request-context extraction.
                    parent_interaction_id=turn_uid or None,
                    channel="kick", timeout_seconds=timeout_seconds, db=db,
                    agent_template_id=agent.get("template_id"),
                    allowed_tools=raw_allowed or None,
                    max_turns=agent.get("max_turn_count", 0),
                    execution_mode=mode,
                    event_callback=_broadcast,
                )
                return RunOutcome(status="complete", stop_cause="complete", reply=reply)
            finally:
                if _rb is not None:
                    try:
                        await _get_rb_reg().end_turn(session_id, db=db)
                    except Exception as _rbe:
                        logger.debug("kick_user_session: run buffer end failed: %s", _rbe)

        # New dispatched turn → same reset boundary as a fresh user message in
        # chat: drop any model/effort the session's agent upgraded itself onto,
        # so this kicked run starts on the agent's default model. (The user's
        # own footer-picker selection survives.)
        try:
            from app.api.chat import _reset_agent_model_switcher as _reset_ms
            await _reset_ms(db, session_id)
        except Exception as _ms_err:
            logger.debug("kick_user_session: model-switcher reset failed: %s", _ms_err)

        outcome = await run_supervised_turn(
            session_id=session_id, user_id=user_id, agent_id=agent_id,
            origin="kick", channel="kick", turn_id=turn_uid,
            relaunch_ctx={"origin": "kick", "session_id": session_id,
                          "user_id": user_id, "agent_id": agent_id,
                          "channel": "kick", "timeout_seconds": timeout_seconds},
            build_turn=_build, await_result=wait, result_timeout=timeout_seconds + 20,
        )
        if wait:
            status = (outcome.status if outcome else "error") or "error"
            reply = (outcome.reply if outcome and outcome.reply else "") or ""
            return _ok(session_id=session_id, status=status, reply=reply)
        return _ok(session_id=session_id, status="running",
                   reply="", note="Kicked — the run continues in the background. Check list_user_sessions for its result.")
    except Exception as e:
        logger.error("kick_user_session failed: %s", e)
        return _err(str(e))


# ── Tool schemas + danger labels ──────────────────────────────────────────────
# The 13 agent-management tool schemas. The read tools run free; the write
# tools confirm-gate (see _DESTRUCTIVE below). build_tools() copies these
# into the module-level TOOL_SCHEMAS / DESTRUCTIVE that the loader reads after
# the call.
# ── Session watch (event-triggered monitoring) ────────────────────────────────
#
# The session event source (app/events/sources/session_source.py) emits
# ``session_settled`` once per FINISHED turn — never while a turn is running.
# Exactly one watch row exists per (agent, user), keyed by this source_hash, so
# arming twice can never accumulate duplicate watches: the newest arm simply
# retargets the single existing row.
_WATCH_SOURCE = "session"
_WATCH_EVENT = "session_settled"
_WATCH_SOURCE_HASH = "session_watch_v1"


async def watch_sessions(
    action: str,
    session_id: str = "",
    outcome: str = "",
    max_per_day: int = 0,
    user_id: str = "",
    agent_id: str = "",
    caller_session_id: str = "",
) -> str:
    """Turn automatic session monitoring on or off, and choose where updates land.

    When armed, this agent is triggered once whenever one of your sessions
    settles — a turn that was running stops, whether it finished cleanly, died,
    or was stopped. It never fires while a turn is working, so a long run
    produces ONE update at the end rather than one per step.

    Updates are delivered into exactly ONE session: the one you arm from
    (``caller_session_id``) or an explicit ``session_id``. Arming again from a
    different session simply MOVES that pointer, so previous chats go quiet on
    their own and there is nothing to clean up.

    - ``action`` — ``"start"``: arm (or retarget). ``"stop"``: disarm; the watch
      row is kept so re-arming restores it. ``"status"``: report the current
      state without changing anything.
    - ``session_id`` — optional: the session that should receive the updates.
      Defaults to the session you are speaking in.
    - ``outcome`` — optional filter: ``"incomplete"`` (stopped involuntarily or
      errored), ``"completed"``, or ``"stopped"``. Blank = every settle.
    - ``max_per_day`` — optional cap on fires per day; 0 = platform default.

    The agent must have the Automation ability enabled, or the platform will not
    deliver the events this watch depends on (the start reply warns if it isn't).
    """
    action = (action or "").strip().lower()
    action = {
        "start": "start", "arm": "start", "on": "start", "enable": "start",
        "stop": "stop", "off": "stop", "disarm": "stop", "disable": "stop",
        "status": "status", "state": "status", "check": "status",
    }.get(action, action)
    if action not in ("start", "stop", "status"):
        return _err("action must be 'start' (arm), 'stop' (disarm), or 'status'.")
    if not user_id:
        return _err("No user context.")
    if not agent_id:
        return _err("I can't tell which agent this watch belongs to — no agent context.")

    # Platform-wide gate (App Settings → Agent Management). Per-agent control is
    # the ordinary tool permission dial.
    try:
        cfg = await _load_mgmt_config(agent_id)
        if not cfg.get("allow_session_watch", True):
            return _err("Session watching is switched off for this ability "
                        "(allow_session_watch). An admin can re-enable it in "
                        "App Settings → Agent Management.")
    except Exception as _ce:
        logger.debug("watch_sessions: config check failed: %s", _ce)

    outcome = (outcome or "").strip().lower()
    if outcome not in ("", "incomplete", "completed", "stopped"):
        return _err("outcome must be one of: incomplete, completed, stopped (or blank for all).")

    try:
        from app.db import get_db
        db = get_db()

        rows = await db.list_event_subscriptions(
            agent_id=agent_id, owner_user_id=user_id, source=_WATCH_SOURCE,
        )
        row = next((r for r in rows
                    if (r.get("source_hash") or "") == _WATCH_SOURCE_HASH), None)
        if row is None and rows:
            row = rows[0]  # a watch row created before the hash convention

        if action == "status":
            if not row:
                return _ok(armed=False, notify_session="",
                           note="No session watch exists for this agent yet.")
            return _ok(
                armed=bool(row.get("enabled")),
                notify_session=row.get("channel_recipient") or "",
                subscription_id=row.get("id") or "",
                event_type=row.get("event_type") or "",
                max_per_day=row.get("max_per_day"),
                runs_today=row.get("runs_today") or 0,
                fire_count=row.get("fire_count") or 0,
                last_status=row.get("last_status") or "",
                last_error=row.get("last_error") or "",
                last_notified_session=row.get("last_session_id") or "",
            )

        if action == "stop":
            if not row:
                return _ok(armed=False, note="Session monitoring was already off.")
            await db.update_event_subscription(row["id"], enabled=False)
            return _ok(
                armed=False, subscription_id=row.get("id") or "",
                note="Session monitoring is off. The watch is kept, so 'start' re-arms it.",
            )

        # ── start ─────────────────────────────────────────────────────────────
        target = (session_id or "").strip() or (caller_session_id or "").strip()
        if not target:
            return _err("I need a session to send the updates to — ask me from the chat "
                        "you want to be notified in, or pass session_id.")

        conn = db._get_conn()
        try:
            trow = conn.execute(
                "SELECT id, user_id, participants, status FROM sessions WHERE id = ?",
                (target,),
            ).fetchone()
        finally:
            conn.close()
        if not trow:
            return _err(f"Session '{target}' not found — I won't point a watch at nothing.")
        try:
            parts = json.loads(trow["participants"] or "[]") or []
        except Exception:
            parts = []
        ids = {p.get("id") for p in parts if isinstance(p, dict)}
        if trow["user_id"] != user_id and user_id not in ids:
            return _err("That session belongs to another user.")
        if (trow["status"] or "") == "recycled":
            return _err("That session is in the recycling bin — restore it, or arm from a live chat.")

        sub = await db.upsert_event_subscription(
            agent_id=agent_id,
            owner_user_id=user_id,
            source_hash=_WATCH_SOURCE_HASH,
            source=_WATCH_SOURCE,
            event_type=_WATCH_EVENT,
            filter_dict=({"outcome": outcome} if outcome else {}),
            task_label="Session watch",
            prompt="",
            trigger_natural="",
            channel="webchat",
            channel_recipient=target,
            silent=False,
            enabled=True,
            run_mode="inline",
            max_per_day=(int(max_per_day) if max_per_day else None),
            origin="slot",
        )

        warning = ""
        try:
            from app.admin.integrations import is_ability_enabled_for_agent
            if not await is_ability_enabled_for_agent(agent_id, "automation"):
                warning = ("This agent does not have the Automation ability enabled, so the "
                           "platform will not deliver session events until it does.")
        except Exception:
            warning = ""

        return _ok(
            armed=True,
            notify_session=target,
            subscription_id=sub.get("id") or "",
            event_type=_WATCH_EVENT,
            outcome_filter=outcome or "any",
            max_per_day=sub.get("max_per_day"),
            warning=warning,
            note=("Session monitoring is on — I'll report here when a session settles. "
                  "Arm again from another chat to move this pointer."),
        )
    except Exception as e:
        logger.error("watch_sessions failed: %s", e)
        return _err(str(e))


_TOOL_SCHEMAS: Dict[str, dict] = {
    "watch_sessions": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["start", "stop", "status"],
                "description": "'start' arms monitoring and points the updates at a session; 'stop' disarms; 'status' reports the current state.",
            },
            "session_id": {
                "type": "string",
                "description": "Optional: the session that should receive the updates. Defaults to the session you are speaking in.",
            },
            "outcome": {
                "type": "string",
                "enum": ["", "incomplete", "completed", "stopped"],
                "description": "Optional filter: only report settles with this outcome. 'incomplete' = stopped involuntarily or errored.",
            },
            "max_per_day": {
                "type": "integer",
                "description": "Optional cap on how many times this watch may fire per day. 0 = platform default.",
            },
        },
        "required": ["action"],
    },
    "list_agent_templates": {
        "type": "object",
        "properties": {
            "template_id": {"type": "string", "description": "Optional: a template id to also return its canonical prompt slots."},
            "include_admin": {"type": "boolean", "description": "Include admin-only templates (platform admins only).", "default": False},
        },
        "required": [],
    },
    "list_my_agents": {"type": "object", "properties": {}, "required": []},
    "get_agent": {
        "type": "object",
        "properties": {"agent_id": {"type": "string", "description": "The agent's id."}},
        "required": ["agent_id"],
    },
    "list_agent_tools": {
        "type": "object",
        "properties": {
            "agent_id": {"type": "string", "description": "The agent whose tools to list (must be visible to you)."},
            "ability": {"type": "string", "description": "Optional: only tools provided by this ability."},
            "query": {"type": "string", "description": "Optional: substring filter on tool name or description."},
        },
        "required": ["agent_id"],
    },
    "create_agent": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Display name for the new agent."},
            "template_id": {"type": "string", "description": "How the agent starts: a template id from list_agent_templates to clone that template's config + prompts, OR 'none' (or blank) for a from-scratch blank-slate agent with no template. There is no silent default — choose one."},
            "description": {"type": "string", "description": "Short description of the agent."},
            "capability_profile": {
                "type": "string",
                "enum": ["simple", "standard", "advanced"],
                "default": "simple",
                "description": "Smallest nested capability profile that covers the job.",
            },
            "ability_extensions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Explicitly justified abilities appended after the selected profile.",
            },
        },
        "required": ["name"],
    },
    "update_agent": {
        "type": "object",
        "properties": {
            "agent_id": {"type": "string", "description": "The agent to update (must be yours)."},
            "name": {"type": "string"},
            "description": {"type": "string"},
            "model": {"type": "string", "description": "Model id (e.g. an OpenRouter model slug)."},
            "temperature": {"type": "number"},
            "max_tokens": {"type": "integer"},
            "max_turn_count": {"type": "integer", "description": "Max turns per run; 0 = unlimited."},
            "max_wall_seconds": {"type": "integer", "description": "Wall-clock cap per run in seconds."},
            "max_identical_tool_calls": {"type": "integer", "description": "Loop-breaker for repeated identical calls; 0 = off."},
            "max_stall_strikes": {"type": "integer", "description": "Stall-guard strikes before stopping; 0 = off."},
            "trigger_type": {"type": "string", "enum": ["user_input", "slash_command", "tool_call", "schedule", "webhook", "background"], "description": "What starts the agent."},
            "trigger_key": {"type": "string", "description": "Command/event key for the trigger type."},
            "chat_ui": {"type": "object", "description": "Partial per-agent chat design override. Deep-merges into metadata.chat_ui. A self-targeted call may supply only agent_id and chat_ui; it cannot be combined with any other update field."},
        },
        "required": ["agent_id"],
    },
    "set_agent_tool": {
        "type": "object",
        "properties": {
            "agent_id": {"type": "string", "description": "The agent to change (must be yours)."},
            "tool": {"type": "string", "description": "The tool name (see list_agent_tools)."},
            "availability": {"type": "string", "enum": ["sent", "discover"], "description": "'sent' = full schema every turn; 'discover' = loaded on demand."},
            "permission": {"type": "string", "enum": ["auto", "ask", "deny"], "description": "'auto' = runs unattended; 'ask' = confirm first; 'deny' = blocked. Sets policy only — cannot relabel a tool's built-in danger."},
        },
        "required": ["agent_id", "tool"],
    },
    "edit_agent_prompt": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "get", "insert", "update", "delete"], "description": "What to do with the prompt slots."},
            "agent_id": {"type": "string", "description": "The agent whose prompts to read/edit."},
            "slot_name": {"type": "string", "description": "Slot name (e.g. system, agent, user, skills, tasks, misc)."},
            "content": {"type": "string", "description": "New slot content (for insert/update)."},
            "lock": {"type": "boolean", "description": "Whether per-user overrides are blocked for this slot."},
            "merge_mode": {"type": "string", "description": "'replace' or 'append'."},
            "order_index": {"type": "integer", "description": "Sort order of the slot."},
        },
        "required": ["action", "agent_id"],
    },
    "set_agent_ability": {
        "type": "object",
        "properties": {
            "agent_id": {"type": "string", "description": "The agent to change (must be yours)."},
            "ability": {"type": "string", "description": "Ability connection_type, e.g. codebase_admin, web_access, diagnostics, agent_orchestration, browser_control, create_tools, image_generation, visualizer, ui_admin, agent_management."},
            "enabled": {"type": "boolean", "description": "Turn the ability on (true) or off (false).", "default": True},
            "read_only": {"type": "boolean", "description": "When enabling: deny every write/mutating tool the ability provides, leaving only its read tools. Use to give an agent UI Admin or Codebase Admin so it can READ code without changing it.", "default": False},
            "deny_tools": {"type": "array", "items": {"type": "string"}, "description": "When enabling: also deny these specific tools of the ability (the agent can't use them)."},
            "ask_tools": {"type": "array", "items": {"type": "string"}, "description": "When enabling: require user confirmation for these specific tools of the ability."},
        },
        "required": ["agent_id", "ability"],
    },
    "manage_agent_skills": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "set", "remove"], "description": "list the agent's skills, set (add/update) one, or remove one."},
            "agent_id": {"type": "string", "description": "The agent whose skills to read/edit (must be yours to write)."},
            "name": {"type": "string", "description": "Skill name (required for set/remove)."},
            "description": {"type": "string", "description": "When to use the skill — ALWAYS shown to the agent."},
            "body": {"type": "string", "description": "The full step-by-step instructions for the skill."},
            "mode": {"type": "string", "enum": ["always_on", "selectable"], "description": "'always_on' = body always in context; 'selectable' = body hidden until the agent calls load_skill. Defaults to 'selectable'."},
            "enabled": {"type": "boolean", "description": "Whether the skill is active."},
        },
        "required": ["action", "agent_id"],
    },
    "list_user_sessions": {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "description": "Max sessions to return (1-50, default 15).", "default": 15},
            "agent_id": {"type": "string", "description": "Optional: only sessions bound to this agent."},
            "include_hidden": {"type": "boolean", "description": "Include sessions hidden from the sidebar (default False).", "default": False},
            "include_recycled": {"type": "boolean", "description": "Include sessions in the recycling bin (default False).", "default": False},
            "peek_chars": {"type": "integer", "description": "How many characters of each last message to include (120-2000, default 500).", "default": 500},
        },
        "required": [],
    },
    "manage_user_session": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["rename", "hide", "show", "recycle", "restore"], "description": "What to do with the session: rename (new title), hide/show (sidebar visibility), recycle (to the bin, transcript kept), restore (back to active)."},
            "session_id": {"type": "string", "description": "The session id (from list_user_sessions)."},
            "name": {"type": "string", "description": "New display title (required for rename; max 80 chars)."},
        },
        "required": ["action", "session_id"],
    },
    "create_user_session": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Display title for the new session."},
            "agent_id": {"type": "string", "description": "Optional: bind the session to one of the user's agents (must be owned by the user)."},
        },
        "required": ["name"],
    },
    "kick_user_session": {
        "type": "object",
        "properties": {
            "session_id": {"type": "string", "description": "The session id (from list_user_sessions) to start work in."},
            "prompt": {"type": "string", "description": "The kickoff message — the task you want the session's agent to work on."},
            "mode": {"type": "string", "enum": ["auto", "ask", "plan"], "default": "auto", "description": "auto = run unattended, the session's agent executes its tools freely (default — the whole call is confirm-gated, so this is a deliberate dispatch); ask = respect the agent's own per-tool confirmation posture (the run may pause for confirmations); plan = run in PLAN mode — research with read-only tools, present a plan workspace, do not execute until the user approves."},
            "timeout_seconds": {"type": "integer", "default": 600, "description": "Per-run wall-clock cap in seconds (30-3600)."},
            "wait": {"type": "boolean", "default": False, "description": "False = kick and return immediately (background supervised run); True = block until the run finishes and return its reply."},
        },
        "required": ["session_id", "prompt"],
    },
}

# The READ tools (list_agent_templates, list_my_agents, get_agent,
# list_agent_tools, list_user_sessions) run free. The WRITE tools reconfigure
# the user's agents or mutate their sessions (create/update/retool/re-prompt/
# re-ability/re-skill + session manage/create) so they confirm-gate in
# Ask/Plan mode. (Agent *deletion* is gated separately behind the
# allow_agent_deletion config flag, not exposed as its own tool here.)
_DESTRUCTIVE: set = {
    "create_agent",
    "update_agent",
    "set_agent_tool",
    "edit_agent_prompt",
    "set_agent_ability",
    "manage_agent_skills",
    "manage_user_session",
    "create_user_session",
    "kick_user_session",
}


# ── Factory: build the 10 handlers, each closing over user_id ─────────────────

def _build_agent_mgmt_tools(user_id: str, session_id: str = "",
                            agent_id: str = "") -> Dict[str, Any]:
    """Build the 15 agent-management tool handlers, each closing over ``user_id``.

    ``session_id`` / ``agent_id`` identify the CALLING session and agent — the
    session-watch tool needs them to default its notify target to the chat the
    user is actually speaking in.

    The nested wrappers carry no docstring of their own, so each tool's real
    usage doc is copied from the underlying in-process function — this feeds both
    the tool-call description and the # [TOOLS] index.
    """
    _caller_session_id = session_id
    _caller_agent_id = agent_id

    async def _amt_list_templates_wrapper(template_id: Optional[str] = None,
                                          include_admin: bool = False):
        return await list_agent_templates(template_id=template_id,
                                          include_admin=include_admin, user_id=user_id)

    async def _amt_list_agents_wrapper():
        return await list_my_agents(user_id=user_id)

    async def _amt_get_agent_wrapper(agent_id: str):
        return await get_agent(agent_id=agent_id, user_id=user_id)

    async def _amt_list_tools_wrapper(agent_id: str, ability: Optional[str] = None,
                                      query: Optional[str] = None):
        return await list_agent_tools(agent_id=agent_id, ability=ability,
                                      query=query, user_id=user_id)

    async def _amt_create_agent_wrapper(name: str, template_id: str = "",
                                        description: str = "",
                                        capability_profile: str = "simple",
                                        ability_extensions: Optional[List[str]] = None):
        return await create_agent(name=name, template_id=template_id,
                                  description=description,
                                  capability_profile=capability_profile,
                                  ability_extensions=ability_extensions,
                                  user_id=user_id)

    async def _amt_update_agent_wrapper(agent_id: str, name: Optional[str] = None,
                                        description: Optional[str] = None,
                                        model: Optional[str] = None,
                                        temperature: Optional[float] = None,
                                        max_tokens: Optional[int] = None,
                                        max_turn_count: Optional[int] = None,
                                        max_wall_seconds: Optional[int] = None,
                                        max_identical_tool_calls: Optional[int] = None,
                                        max_stall_strikes: Optional[int] = None,
                                        trigger_type: Optional[str] = None,
                                        trigger_key: Optional[str] = None,
                                        chat_ui: Optional[Dict[str, Any]] = None):
        return await update_agent(agent_id=agent_id, name=name,
                                  description=description, model=model,
                                  temperature=temperature, max_tokens=max_tokens,
                                  max_turn_count=max_turn_count,
                                  max_wall_seconds=max_wall_seconds,
                                  max_identical_tool_calls=max_identical_tool_calls,
                                  max_stall_strikes=max_stall_strikes,
                                  trigger_type=trigger_type, trigger_key=trigger_key,
                                  chat_ui=chat_ui, user_id=user_id)

    async def _amt_set_tool_wrapper(agent_id: str, tool: str,
                                    availability: Optional[str] = None,
                                    permission: Optional[str] = None):
        return await set_agent_tool(agent_id=agent_id, tool=tool,
                                    availability=availability, permission=permission,
                                    user_id=user_id)

    async def _amt_edit_prompt_wrapper(action: str, agent_id: str,
                                       slot_name: Optional[str] = None,
                                       content: Optional[str] = None,
                                       lock: Optional[bool] = None,
                                       merge_mode: Optional[str] = None,
                                       order_index: Optional[int] = None):
        return await edit_agent_prompt(action=action, agent_id=agent_id,
                                       slot_name=slot_name, content=content, lock=lock,
                                       merge_mode=merge_mode, order_index=order_index,
                                       user_id=user_id)

    async def _amt_set_ability_wrapper(agent_id: str, ability: str, enabled: bool = True,
                                       read_only: bool = False,
                                       deny_tools: Optional[List[str]] = None,
                                       ask_tools: Optional[List[str]] = None):
        return await set_agent_ability(agent_id=agent_id, ability=ability,
                                       enabled=enabled, read_only=read_only,
                                       deny_tools=deny_tools, ask_tools=ask_tools,
                                       user_id=user_id)

    async def _amt_manage_skills_wrapper(action: str, agent_id: str,
                                         name: Optional[str] = None,
                                         description: Optional[str] = None,
                                         body: Optional[str] = None,
                                         mode: Optional[str] = None,
                                         enabled: Optional[bool] = None):
        return await manage_agent_skills(action=action, agent_id=agent_id, name=name,
                                         description=description, body=body, mode=mode,
                                         enabled=enabled, user_id=user_id)

    async def _amt_list_sessions_wrapper(limit: int = 15, agent_id: str = "",
                                         include_hidden: bool = False,
                                         include_recycled: bool = False,
                                         peek_chars: int = 500):
        return await list_user_sessions(limit=limit, agent_id=agent_id,
                                        include_hidden=include_hidden,
                                        include_recycled=include_recycled,
                                        peek_chars=peek_chars, user_id=user_id)

    async def _amt_manage_session_wrapper(action: str, session_id: str,
                                          name: str = ""):
        return await manage_user_session(action=action, session_id=session_id,
                                         name=name, user_id=user_id)

    async def _amt_create_session_wrapper(name: str, agent_id: str = ""):
        return await create_user_session(name=name, agent_id=agent_id, user_id=user_id)

    async def _amt_kick_session_wrapper(session_id: str, prompt: str, mode: str = "auto",
                                        timeout_seconds: int = 600, wait: bool = False):
        return await kick_user_session(session_id=session_id, prompt=prompt, mode=mode,
                                       timeout_seconds=timeout_seconds, wait=wait,
                                       user_id=user_id)

    async def _amt_watch_sessions_wrapper(action: str, session_id: str = "",
                                          outcome: str = "", max_per_day: int = 0):
        return await watch_sessions(action=action, session_id=session_id,
                                    outcome=outcome, max_per_day=max_per_day,
                                    user_id=user_id, agent_id=_caller_agent_id,
                                    caller_session_id=_caller_session_id)

    # The nested wrappers carry no docstring of their own, so the model would see
    # an empty/generic description. Copy each tool's real usage doc from the
    # underlying in-process function — this feeds both the tool-call description
    # and the # [TOOLS] index.
    _amt_list_templates_wrapper.__doc__ = list_agent_templates.__doc__
    _amt_list_agents_wrapper.__doc__    = list_my_agents.__doc__
    _amt_get_agent_wrapper.__doc__      = get_agent.__doc__
    _amt_list_tools_wrapper.__doc__     = list_agent_tools.__doc__
    _amt_create_agent_wrapper.__doc__   = create_agent.__doc__
    _amt_update_agent_wrapper.__doc__   = update_agent.__doc__
    _amt_set_tool_wrapper.__doc__       = set_agent_tool.__doc__
    _amt_edit_prompt_wrapper.__doc__    = edit_agent_prompt.__doc__
    _amt_set_ability_wrapper.__doc__    = set_agent_ability.__doc__
    _amt_manage_skills_wrapper.__doc__  = manage_agent_skills.__doc__
    _amt_list_sessions_wrapper.__doc__  = list_user_sessions.__doc__
    _amt_manage_session_wrapper.__doc__ = manage_user_session.__doc__
    _amt_create_session_wrapper.__doc__ = create_user_session.__doc__
    _amt_kick_session_wrapper.__doc__  = kick_user_session.__doc__
    _amt_watch_sessions_wrapper.__doc__ = watch_sessions.__doc__

    return {
        "list_agent_templates": _amt_list_templates_wrapper,
        "list_my_agents": _amt_list_agents_wrapper,
        "get_agent": _amt_get_agent_wrapper,
        "list_agent_tools": _amt_list_tools_wrapper,
        "create_agent": _amt_create_agent_wrapper,
        "update_agent": _amt_update_agent_wrapper,
        "set_agent_tool": _amt_set_tool_wrapper,
        "edit_agent_prompt": _amt_edit_prompt_wrapper,
        "set_agent_ability": _amt_set_ability_wrapper,
        "manage_agent_skills": _amt_manage_skills_wrapper,
        "list_user_sessions": _amt_list_sessions_wrapper,
        "manage_user_session": _amt_manage_session_wrapper,
        "create_user_session": _amt_create_session_wrapper,
        "kick_user_session": _amt_kick_session_wrapper,
        "watch_sessions": _amt_watch_sessions_wrapper,
    }


# ── Per-agent config defaults ────────────────────────────────────────────────

_MGMT_CONFIG_DEFAULTS = {
    "max_agents_per_user": 10,
    "allow_agent_deletion": False,
    "allow_discoverable_agents": False,
    "max_prompt_edits_per_session": 10,
    "max_agent_updates_per_session": 20,
    "restrict_to_custom_only": False,
    # Session watching is armed from chat (watch_sessions). On by default so the
    # feature works out of the box; turn it off for a platform-wide kill switch.
    # Per-agent control is the normal tool permission dial (deny watch_sessions
    # on every agent except the overseer).
    "allow_session_watch": True,
}

# Per-session counters — resets on server restart (fine for a safety guardrail).
_SESSION_COUNTERS: dict = {}  # {session_id: {"prompt_edits": N, "agent_updates": N}}
_COUNTER_LOCK = asyncio.Lock()

# Tool names that increment the agent_updates counter.
_UPDATE_TOOLS = {"update_agent", "set_agent_tool", "set_agent_ability", "manage_agent_skills"}

# Tool names that need restrict_to_custom_only enforcement (target_agent_id
# is the first positional arg after self for all of these).
_RESTRICT_TOOLS = {"update_agent", "set_agent_tool", "edit_agent_prompt",
                   "set_agent_ability", "manage_agent_skills"}

# Tool names an agent must NOT be allowed to point at ITSELF. An agent
# re-enabling its own abilities or widening its own tools would let it climb
# out of the box its admin put it in (observed: an agent flipped its own
# agent_orchestration ability back on). Self-targeting these is always refused;
# the agent can still manage OTHER agents it owns.
_SELF_PROTECT_TOOLS = {"update_agent", "set_agent_tool", "edit_agent_prompt",
                       "set_agent_ability", "manage_agent_skills"}


# ── Config loader ────────────────────────────────────────────────────────────

async def _load_mgmt_config(agent_id: str) -> dict:
    """Read per-agent management limits from the agent_management connection
    row's config JSON.  Returns defaults for any missing keys."""
    if not agent_id:
        return dict(_MGMT_CONFIG_DEFAULTS)
    limits = dict(_MGMT_CONFIG_DEFAULTS)
    try:
        from app.db import get_db
        db = get_db()
        conns = await db.get_agent_connections(agent_id)
        for c in conns:
            if c.get("connection_type") == "agent_management" and c.get("section") == "ability":
                raw = c.get("config")
                if isinstance(raw, str):
                    raw = json.loads(raw or "{}")
                if isinstance(raw, dict):
                    settings = raw.get("ability_settings") or {}
                    for k in _MGMT_CONFIG_DEFAULTS:
                        if k in settings:
                            v = settings[k]
                            if k in ("max_agents_per_user", "max_prompt_edits_per_session",
                                     "max_agent_updates_per_session"):
                                try:
                                    v = int(v)
                                except (TypeError, ValueError):
                                    continue
                            elif k in ("allow_agent_deletion", "allow_discoverable_agents",
                                      "restrict_to_custom_only"):
                                v = bool(v)
                            limits[k] = v
                break
    except Exception as e:
        logger.debug("Failed to load management config for agent %s: %s", agent_id, e)
    return limits


# ── Limit-check helpers ──────────────────────────────────────────────────────

def _limit_err(msg: str) -> str:
    return json.dumps({"status": "error", "error": msg})


async def _target_source(agent_id: str) -> str:
    """Best-effort lookup of an agent's source field. Returns '' on failure."""
    try:
        from app.db import get_db
        db = get_db()
        ag = await db.get_agent_by_id(agent_id)
        return (ag or {}).get("source") or ""
    except Exception:
        return ""


async def _user_agent_count(user_id: str) -> int:
    """Best-effort count of agents owned by a user. Returns 0 on failure."""
    try:
        from app.db import get_db
        db = get_db()
        agents = await db.list_user_agents(user_id)
        return len(agents) if agents else 0
    except Exception:
        return 0


async def _check_mgmt_limit(agent_id: str, tool_name: str, session_id: str,
                            user_id: str = "", target_agent_id: str = "",
                            discoverable: bool = False) -> str:
    """Check management limits before executing a tool.

    Returns an error JSON string on breach, or an empty string if OK.
    The caller should return the error string if non-empty.
    """
    cfg = await _load_mgmt_config(agent_id)

    # ── Restrict to custom only ──────────────────────────────────────────
    if cfg.get("restrict_to_custom_only") and tool_name in _RESTRICT_TOOLS and target_agent_id:
        src = await _target_source(target_agent_id)
        if src and src != "custom":
            return _limit_err(
                f"This agent is restricted to custom agents only. "
                f"Target agent '{target_agent_id}' has source '{src}'."
            )

    # ── Max agents per user (create only) ────────────────────────────────
    if tool_name == "create_agent" and user_id:
        count = await _user_agent_count(user_id)
        max_agents = cfg.get("max_agents_per_user", 10)
        if count >= max_agents:
            return _limit_err(
                f"Agent limit reached: user already has {count} agents "
                f"(max {max_agents}). Delete unused agents or ask the human."
            )

    # ── Discoverable check (create only) ────────────────────────────────
    if tool_name == "create_agent" and discoverable and not cfg.get("allow_discoverable_agents"):
        return _limit_err(
            "Discoverable agents are not allowed by this agent's configuration. "
            "Set discoverable=false."
        )

    # ── Per-session counters ────────────────────────────────────────────
    ctr = None
    async with _COUNTER_LOCK:
        if session_id not in _SESSION_COUNTERS:
            _SESSION_COUNTERS[session_id] = {"prompt_edits": 0, "agent_updates": 0}
        ctr = _SESSION_COUNTERS[session_id]

    if tool_name == "edit_agent_prompt":
        ctr["prompt_edits"] += 1
        max_edits = cfg.get("max_prompt_edits_per_session", 10)
        if ctr["prompt_edits"] > max_edits:
            return _limit_err(
                f"Prompt edit limit reached: {ctr['prompt_edits']} edits this "
                f"session (max {max_edits}). Start a new session to reset."
            )

    if tool_name in _UPDATE_TOOLS:
        ctr["agent_updates"] += 1
        max_updates = cfg.get("max_agent_updates_per_session", 20)
        if ctr["agent_updates"] > max_updates:
            return _limit_err(
                f"Agent update limit reached: {ctr['agent_updates']} updates "
                f"this session (max {max_updates}). Start a new session to reset."
            )

    return ""


# ── build_tools — wraps the factory handlers with limit enforcement ──────────

# We need the wrapper to be an async callable that passes through keyword args.
# Since the factory returns handlers with varying signatures, we wrap each via
# a closure that knows which args to extract for limit checks.

def _wrap_with_limits(handler, agent_id: str, session_id: str,
                      tool_name: str, user_id: str):
    """Return an async wrapper that checks limits before delegating to handler."""
    # Extract target_agent_id from the first positional arg for tools that have one.
    # For create_agent, extract discoverable from kwargs.
    extract_target = tool_name in _RESTRICT_TOOLS

    async def wrapped(*args, **kwargs):
        target_agent_id = ""
        if extract_target:
            # The LLM may pass the target positionally OR as agent_id=...; check both
            # so neither the self-protect block nor the custom-only restriction can
            # be sidestepped by argument style.
            if args and args[0]:
                target_agent_id = str(args[0])
            elif kwargs.get("agent_id"):
                target_agent_id = str(kwargs["agent_id"])

        # ── Self-modification block ──────────────────────────────────────
        # An agent may manage other agents it owns, but never re-arm itself.
        # The single exception is a design-only update: its own partial chat_ui
        # override. It cannot be combined with an identity, model, limit, trigger,
        # ability, tool, prompt, or any other configuration change.
        self_design_only = (
            tool_name == "update_agent"
            and isinstance(kwargs.get("chat_ui"), dict)
            and len(args) <= 1
            and not any(
                value is not None
                for key, value in kwargs.items()
                if key not in {"agent_id", "chat_ui", "user_id"}
            )
        )
        if (tool_name in _SELF_PROTECT_TOOLS and agent_id
                and target_agent_id == agent_id and not self_design_only):
            return _limit_err(
                "An agent cannot modify its own abilities, tools, prompts, or "
                "config. Ask a human to change this agent's setup, or target a "
                "different agent you own."
            )

        discoverable = False
        if tool_name == "create_agent":
            discoverable = bool(kwargs.get("discoverable", False))

        error = await _check_mgmt_limit(
            agent_id=agent_id, tool_name=tool_name,
            session_id=session_id, user_id=user_id,
            target_agent_id=target_agent_id,
            discoverable=discoverable,
        )
        if error:
            return error
        return await handler(*args, **kwargs)

    # Copy docstring so the tool description stays intact.
    wrapped.__doc__ = handler.__doc__
    return wrapped


def build_tools(*, user_id: str = "", session_id: str = "", agent_id: str = "",
                agent_template_id: str = "", enabled_providers=None, **_ctx):
    """Return {tool_name: handler} for the 14 agent-management tools, each
    wrapped with per-agent limit enforcement (max agents, session counters,
    custom-only restriction)."""
    handlers = _build_agent_mgmt_tools(user_id, session_id=session_id,
                                       agent_id=agent_id)

    # Wrap mutation tools with limit checks.  Read-only tools pass through.
    enforce = (
        "create_agent", "update_agent", "set_agent_tool",
        "edit_agent_prompt", "set_agent_ability", "manage_agent_skills",
    )
    for name in enforce:
        if name in handlers and agent_id:
            handlers[name] = _wrap_with_limits(
                handlers[name], agent_id=agent_id, session_id=session_id,
                tool_name=name, user_id=user_id,
            )

    TOOL_SCHEMAS.clear()
    TOOL_SCHEMAS.update(_TOOL_SCHEMAS)
    DESTRUCTIVE.clear()
    DESTRUCTIVE.update(_DESTRUCTIVE)

    return handlers
