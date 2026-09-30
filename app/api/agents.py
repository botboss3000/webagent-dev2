"""
Agent management API.

Provides endpoints for listing, creating, updating, deleting, and configuring
user agents. Also exposes the user profile (including is_admin flag).

Endpoints
---------
GET  /api/v1/user/profile               — user profile + admin flag
GET  /api/v1/agents                     — list all agents/templates visible to the user
POST /api/v1/agents                     — create a new custom agent (cloned from default)
GET  /api/v1/agents/{agent_id}          — get a single custom agent
PUT  /api/v1/agents/{agent_id}          — update editable fields on a custom agent
DELETE /api/v1/agents/{agent_id}        — delete a custom agent
GET  /api/v1/agents/templates           — list agent templates (for tool breakdown display)
POST /api/v1/agents/test                — run a test message through an agent config
GET  /api/v1/agents/{agent_id}/members  — list agent admins + members with stats (agent admin only)
"""

import asyncio
import json as _json
import logging
from pathlib import Path
import re
import secrets
import sqlite3
import time
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.auth.identity import assert_caller_is, request_user_id, user_may_access_page
from app.db import get_db
from app.entitlements.service import resolve_capabilities
from app.entitlements.resources import (
    ResourceEntitlementError,
    enforce_ability_group,
    enforce_agent_materialization,
    enforce_connection_change,
    connection_resource_lock,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["agents"])

SHARED_DEFAULT_AGENT_ID = "shared_default"

# ── Idempotent WebAgent provisioning ────────────────────────────────────────
# The default WebAgent is one app-level singleton, not one singleton per user.
# Both startup and request-time recovery converge on this fixed authority ID.
_provision_locks: Dict[str, asyncio.Lock] = {}
_agent_create_locks: Dict[str, asyncio.Lock] = {}


def _provision_lock(user_id: str) -> asyncio.Lock:
    lk = _provision_locks.get(user_id)
    if lk is None:
        lk = asyncio.Lock()
        _provision_locks[user_id] = lk
    return lk


def _agent_create_lock(user_id: str) -> asyncio.Lock:
    lock = _agent_create_locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _agent_create_locks[user_id] = lock
    return lock


def _template_is_allowed(capabilities: dict, template_id: str) -> bool:
    allowed = set(capabilities.get("agent_templates") or [])
    return "*" in allowed or template_id in allowed


def _owned_custom_agent_count(rows: List[dict], user_id: str) -> int:
    count = 0
    for row in rows:
        if row.get("source") != "custom" or row.get("status") == "clone":
            continue
        metadata = row.get("metadata") or {}
        if isinstance(metadata, str):
            try:
                metadata = _json.loads(metadata)
            except Exception:
                metadata = {}
        if isinstance(metadata, dict) and metadata.get("owner_user_id") == user_id:
            count += 1
    return count


def _resource_http_error(exc: ResourceEntitlementError) -> HTTPException:
    return HTTPException(status_code=403, detail=exc.detail())


# MODEL-NAME-LEVELS: the only fields a client may set on an adopted catalog row.
_CATALOG_CLIENT_FIELDS = ("high_effort_capable",)


def catalog_account_aliases(own_config) -> dict:
    """The caller's account-level display names for adopted catalog entries.

    Stored with the user's own model catalog as ``{entry_id: name}``; a blank or
    malformed entry is dropped so it can never blank out the catalog name.
    """
    return {
        str(key): str(value).strip()
        for key, value in ((own_config or {}).get("user_catalog_platform_aliases") or {}).items()
        if str(key) and str(value).strip()
    }


def merge_entitled_catalog_row(row, platform_by_id, account_aliases=None) -> dict:
    """Rehydrate one submitted catalog row from its entitled platform row.

    A WebAgent Catalog client submits only an opaque entry ID plus the names the
    user controls. Everything that decides *which* model runs — provider, model ID,
    credentials, base URL — is taken from the entitled platform row, so a client can
    never spoof it. Two names survive: the catalog's own model name (the entitled
    row's ``display_name``, used when the caller sets none) and the caller's own
    display name for this roster row, falling back to their account-level alias.
    ``_public_model_slot`` in app/admin/settings.py encodes the same ladder.
    """
    entry_id = str((row or {}).get("entry_id") or "")
    platform_row = (platform_by_id or {}).get(entry_id)
    if not platform_row:
        return row
    merged = {
        **platform_row,
        **{key: row[key] for key in _CATALOG_CLIENT_FIELDS if key in row},
    }
    alias = str(row.get("display_name") or "").strip() or str(
        (account_aliases or {}).get(entry_id) or ""
    ).strip()
    if alias:
        merged["display_name"] = alias
    return merged


async def _catalog_seed_for_new_agent(db, user_id: str) -> dict:
    """Resolve the caller's default model, falling back to ``shared_default``.

    Only the opaque catalog entry ID crosses into the new agent request.  The
    normal update path rehydrates it from this caller's entitlement/BYO roster,
    so the shared agent can guide onboarding without lending credentials.
    """
    from app.admin.settings import _load_own_llm_config
    from app.entitlements.rosters import resolve_platform_roster_config, select_roster_entry

    owned = await _load_own_llm_config(user_id) or {}
    platform = await resolve_platform_roster_config(user_id, db=db) or {}
    platform_entries = [row for row in platform.get("multi_providers") or [] if isinstance(row, dict)]
    owned_entries = [row for row in owned.get("multi_providers") or [] if isinstance(row, dict)]
    available = {str(row.get("entry_id") or ""): row for row in [*platform_entries, *owned_entries] if row.get("entry_id")}
    default_id = str(owned.get("user_catalog_default_entry_id") or "").strip()

    if not default_id:
        # Preserve legacy BYO behaviour until the user chooses a catalog default.
        default_id = next((str(row.get("entry_id") or "") for row in owned_entries
                           if row.get("enabled") is not False and row.get("text_capable") is not False), "")

    if not default_id:
        shared = await db.get_agent_by_id(SHARED_DEFAULT_AGENT_ID) or {}
        raw_meta = shared.get("metadata") if isinstance(shared, dict) else {}
        if isinstance(raw_meta, str):
            try:
                raw_meta = _json.loads(raw_meta or "{}")
            except Exception:
                raw_meta = {}
        shared_llm = raw_meta.get("llm_config") if isinstance(raw_meta, dict) else {}
        shared_rows = shared_llm.get("multi_providers") if isinstance(shared_llm, dict) else []
        shared_roles = shared_llm.get("role_assignments") if isinstance(shared_llm, dict) else {}
        shared_id = str((shared_roles or {}).get("standard") or "")
        shared_row = next((row for row in shared_rows or [] if isinstance(row, dict) and str(row.get("entry_id") or "") == shared_id), None)
        if shared_row is None:
            shared_row = next((row for row in shared_rows or [] if isinstance(row, dict) and row.get("text_capable") is not False), None)
        if isinstance(shared_row, dict):
            shared_key = str(shared_row.get("entry_id") or "")
            if shared_key in available:
                default_id = shared_key
            else:
                resolved = select_roster_entry(
                    available.values(),
                    entry_id=shared_key,
                    provider=shared_row.get("provider"),
                    model=shared_row.get("model"),
                )
                if resolved:
                    default_id = str(resolved.get("entry_id") or "")

    if default_id not in available:
        return {}
    return {
        "multi_providers": [{"entry_id": default_id}],
        "role_assignments": {"standard": default_id},
    }


async def _normalize_shared_default_llm_config(db, shared: dict) -> dict:
    """Persist the canonical role map for a legacy shared-agent roster.

    The shared default predates explicit role assignments.  Normalize it while
    provisioning so the Agents table and runtime read the same authoritative
    Standard/System selection.  Explicit administrator assignments are already
    canonical and therefore remain untouched.
    """
    raw_metadata = shared.get("metadata")
    if isinstance(raw_metadata, str):
        try:
            metadata = _json.loads(raw_metadata or "{}")
        except Exception:
            metadata = {}
    else:
        metadata = dict(raw_metadata) if isinstance(raw_metadata, dict) else {}
    llm_config = metadata.get("llm_config")
    if not isinstance(llm_config, dict):
        return shared

    from app.admin.settings import (
        _legacy_agent_config_to_roster,
        _load_agent_llm_config,
        _persist_agent_llm_config,
    )
    normalized, changed = _legacy_agent_config_to_roster(llm_config)
    if not changed:
        return shared

    agent_id = str(shared["id"])
    hydrated = await _load_agent_llm_config(agent_id, normalized)
    metadata["llm_config"] = await _persist_agent_llm_config(agent_id, hydrated)
    updated = await db.update_agent_fields(
        agent_id, "", {"metadata": metadata}, allow_install_admin=True,
    )
    if updated:
        logger.info("Normalized legacy LLM role assignments for shared_default")
        return updated
    logger.warning("Could not persist shared_default LLM role assignments")
    return shared


async def provision_default_agent(db, user_id: str) -> Optional[dict]:
    """Return the single admin-owned WebAgent shared by every app user.

    The App Settings switch controls whether this agent is available at all. It
    never enables the retired per-user clone path: when the switch is off,
    callers get ``None`` and a user with no custom agents has an empty roster.

    Creation is lazy as well as startup-seeded so a failed startup seed cannot
    leave login permanently broken. All users share the same lock and fixed ID.
    """
    from app.admin.settings import shared_default_agent_enabled as _sd_on
    if not _sd_on():
        return None

    async with _provision_lock(SHARED_DEFAULT_AGENT_ID):
        try:
            shared = await db.get_agent_by_id(SHARED_DEFAULT_AGENT_ID)
        except Exception as e:
            logger.warning(
                "Shared default lookup failed (%s); rebuilding the fixed authority",
                e,
            )
            shared = None
        if shared:
            return await _normalize_shared_default_llm_config(db, shared)
        try:
            return await db.create_agent_for_user(
                "admin", agent_id=SHARED_DEFAULT_AGENT_ID,
            )
        except Exception:
            # The in-process lock cannot serialize separate server processes.
            # If another instance won the fixed-ID insert, converge on its row.
            shared = await db.get_agent_by_id(SHARED_DEFAULT_AGENT_ID)
            if shared:
                return await _normalize_shared_default_llm_config(db, shared)
            raise


# ── Request / Response models ─────────────────────────────────────────────────

class ReorderAgentsRequest(BaseModel):
    user_id: str
    order: List[str]  # agent ids, top-to-bottom (index 0 = top of the list)


class SlotPayload(BaseModel):
    slot_name: str
    order_index: int = 0
    lock: bool = False
    merge_mode: str = "replace"
    content: str = ""


class UpdateAgentRequest(BaseModel):
    user_id: str
    name: Optional[str] = None
    description: Optional[str] = None
    # Icon (stored in metadata['icon'] — the agents table has no icon column)
    icon: Optional[str] = None
    max_turn_count: Optional[int] = None
    max_wall_seconds: Optional[float] = None
    max_identical_tool_calls: Optional[int] = None
    max_stall_strikes: Optional[int] = None
    model: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    allowed_tools: Optional[List[str]] = None
    custom_tool_ids: Optional[List[str]] = None
    trigger_type: Optional[str] = None
    trigger_key: Optional[str] = None
    loop_logic: Optional[List] = None
    safety_policy: Optional[Dict[str, Any]] = None
    user_mode: Optional[str] = None
    # Default chat execution mode for NEW sessions with this agent: 'ask' | 'plan'
    # | 'auto'. Stored in metadata['default_execution_mode']; the chat pill seeds a
    # fresh session from it (ui/chat/js/chat-ui.js). Blank ⇒ 'ask'.
    default_execution_mode: Optional[str] = None
    # Modular chat modes for this agent. Ask/Plan/Auto are always present and
    # may be customized; additional entries add modes to the chat footer.
    execution_modes: Optional[List[Dict[str, Any]]] = None
    # Default Remote Control target device for NEW sessions with this agent — the
    # instance-id of a device in the shared fleet (stored in
    # metadata['default_target_device']). Blank ⇒ run on "this device" (local).
    # A fresh session pre-selects it in the chat Remote Control pill, falling back
    # to local when that device is offline (ui/chat/js/chat-ui.js).
    default_target_device: Optional[str] = None
    # Per-agent LLM override (stored in metadata['llm_config'])
    llm_config: Optional[Dict[str, Any]] = None
    # Whether the agent's public /{agent_id} shared link is enabled.
    # Stored in metadata['public_link']; the public route checks this flag.
    public_link: Optional[bool] = None
    # Public anonymous policy: funding, usage/data limits, and UI narrowing.
    # A user-created agent cannot be published without valid explicit funding.
    public_access: Optional[Dict[str, Any]] = None
    # Per-agent chat UI override — partial dict deep-merged into the app-wide
    # into metadata['chat_ui']. Blank value for a key clears it (use default).
    chat_ui: Optional[Dict[str, Any]] = None
    # Per-agent ephemeral Voice coordinator settings.
    # Stored in metadata['voice_conversation'] as a closed configuration.
    voice_conversation: Optional[Dict[str, Any]] = None
    # Local Claude Code engine config — partial dict (folder/extra_flags/model/
    # act_freely/append_persona), shallow-merged into metadata['claude_code'].
    # Only meaningful on agents whose metadata.engine == "claude_code".
    claude_code: Optional[Dict[str, Any]] = None
    # Local Codex CLI engine config (folder/model/extra_flags), shallow-merged
    # into metadata['codex_code'].
    codex_code: Optional[Dict[str, Any]] = None
    # Terminal Chat engine config (command/working folder/environment),
    # shallow-merged into metadata['terminal_chat'].
    terminal_chat: Optional[Dict[str, Any]] = None
    # Website embed widget config — partial dict (enabled/allowed_domains/accent/
    # title/subtitle/greeting/placeholder/launcher_position),
    # normalized + shallow-merged into metadata['embed']. Drives the standalone
    # /embed/<agent_id> chat page + the /embed.js loader snippet. See
    # normalize_embed_config below and app/main.py's embed routes.
    embed: Optional[Dict[str, Any]] = None
    # Resume tail messages — how many recent messages to replay on resume.
    # Stored in metadata['resume_tail_messages']; 0 = use the app default (32).
    resume_tail_messages: Optional[int] = None
    # Per-agent auto-resume knobs (Agents → Runtime → Limits). Both live in agent
    # metadata; absent/0 = inherit the app-global value. The attempt count is
    # ALWAYS clamped to the app-global ceiling (run_max_resume_attempts) by the
    # runner, so a stale/oversized stored value can never exceed the cap.
    resume_max_attempts: Optional[int] = None
    resume_backoff_seconds: Optional[int] = None
    # Per-error-type resume cap (metadata['resume_max_attempts_per_cause']).
    # Absent/0 = inherit the app-global value; ALWAYS clamped to the app ceiling
    # (run_max_resume_attempts_per_cause) by the runner.
    resume_max_attempts_per_cause: Optional[int] = None
    # Closing-message requirement-compliance budgets (Agents → Runtime → Limits).
    # NOT the resume/error counters: the closing gate re-injects its instruction
    # while the run's final message does not yet meet the closing contract, and
    # nothing has failed. Absent/0 = inherit the app-global value; the gate always
    # clamps to the app ceiling (run_max_closing_attempts /
    # run_max_closing_attempts_per_requirement).
    closing_max_attempts: Optional[int] = None
    closing_max_attempts_per_requirement: Optional[int] = None
    # Assertions requirement-compliance budget (Agents → Runtime → Limits). The
    # assertions gate re-injects its resolution instruction while an assertion the
    # run declared is still unanswered, and nothing has failed. Absent/0 = inherit
    # the app-global value; the gate always clamps to the app ceiling
    # (run_max_assertions_attempts), exactly like the closing budgets above.
    assertions_max_attempts: Optional[int] = None
    # Close-out audit checklist — the Output Closer's checklist-auditor prop
    # (metadata['audit_checklist']). When set, after each final response the
    # closer audits the completed work against this checklist and either
    # closes (summary lane reporting the checklist status) or sends the verdict
    # back into the main loop as a synthetic [AUDITOR] message so the agent
    # finishes the missing items (bounded by max_rounds). Accepted shapes:
    #   - plain string: one checklist item per line
    #   - JSON array of strings
    #   - JSON object {"checklist": [...], "max_rounds": N, "send_back": bool}
    # Blank/empty clears the prop (audit disabled; plain summary behavior).
    audit_checklist: Optional[Any] = None
    closer_prompt: Optional[str] = None
    # Mode-aware Manager Loop. Partial objects are deep-merged over the
    # effective per-agent config and stored at metadata['manager'].
    manager_loop: Optional[Dict[str, Any]] = None
    # Standalone durable Task Progress policy. This is intentionally separate
    # from Manager Loop: it may run without a manager reviewer.
    task_progress: Optional[Dict[str, Any]] = None
    # Data-driven run-contract gates — which gate TYPES (plugins/gates/) this
    # agent runs, their params, and the modes they apply to. Stored at
    # metadata['gates'] and resolved by app/agent/gate_registry.py. Each mode card
    # in the Chat-modes section edits its own scoped entries here.
    gates: Optional[Dict[str, Any] | List[Any]] = None
    # Prompt slots — admin-only. Full slot set when present; reconciled against existing.
    slots: Optional[List[SlotPayload]] = None
    # Per-slot wipe of all user override rows at save time.
    reset_overrides_for: Optional[List[str]] = None


class CreateAgentRequest(UpdateAgentRequest):
    """Creation extends the exact persisted-agent configuration schema."""
    name: str
    description: Optional[str] = ""
    template_id: Optional[str] = "default"
    capability_profile: Optional[Literal["simple", "standard", "advanced"]] = None
    capability_extensions: List[str] = Field(default_factory=list)


def _normalize_codex_code_update(value: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Validate the closed Codex context/closer controls in a partial update."""
    if value is None:
        return None
    normalized = dict(value)
    if "context_mode" in normalized:
        mode = normalized["context_mode"]
        if not isinstance(mode, str) or mode not in ("native_codex", "webagent_wrapper", "codex_portal"):
            raise HTTPException(status_code=400, detail="Invalid codex_code.context_mode.")
    if "closer_enabled" in normalized and not isinstance(normalized["closer_enabled"], bool):
        raise HTTPException(status_code=400, detail="Invalid codex_code.closer_enabled.")
    return normalized


class UpdateMyPromptsItem(BaseModel):
    slot_name: str
    content: str


class UpdateMyPromptsRequest(BaseModel):
    user_id: str
    slots: List[UpdateMyPromptsItem]


class SoftAbilityRequest(BaseModel):
    user_id: str
    slug: str
    display_name: str
    description: str = ""
    icon: str = "sparkles"
    enabled: bool = True
    skill_summary: str = ""
    skill_body: str = ""
    workflow: Dict[str, Any] = Field(default_factory=dict)
    allowed_tools: List[str] = Field(default_factory=list)
    credential_schema: List[Dict[str, Any]] = Field(default_factory=list)
    policy: Dict[str, Any] = Field(default_factory=dict)
    status: str = "draft"


class UpdateTemplateRequest(BaseModel):
    user_id: str
    template_id: str
    discoverable: Optional[bool] = None


class UpsertConnectionRequest(BaseModel):
    user_id: str
    enabled: bool
    config: Optional[Dict[str, Any]] = None


class SaveAsTemplateRequest(BaseModel):
    user_id: str
    template_id: str
    name: str
    description: Optional[str] = ""
    icon: Optional[str] = ""
    discoverable: Optional[bool] = False
    access_level: Optional[str] = "all"


class AnonSessionRequest(BaseModel):
    browser_id: Optional[str] = None
    guest_credential: Optional[str] = None


class TestAgentRequest(BaseModel):
    user_id: str
    agent_id: str          # template id (e.g. 'default') or custom agents.id UUID
    message: str
    session_id: Optional[str] = None


# ── Helpers ───────────────────────────────────────────────────────────────────

# Built-in utility agents that should stay off the user's Agents page unless the
# "Show system agents" toggle is on. They are still fully usable/configurable.
_SYSTEM_UTILITY_IDS = {
    "agent-builder",        # Agent Manager
    "source-controller",    # GitHub agent
    "user-impersonator",    # Suggested Replies (placeholder) agent
    "admin-agent",
    "diagnostic-agent",
    "chat-panel-engineer",
    "codebase-engineer",
    "integration-admin-agent",
}


# Per-agent chat UI config override — stored in metadata["chat_ui"]. Any
# subset of the full data/config/chat_ui.json structure may be stored here
# (messages, chat_pill, chat_header, fade, launcher, etc.). At render time the
# agent's override is deep-merged over the app-wide defaults, so each agent can
# be completely customised without touching the global file.
#
# Consumed on the frontend by ui/shared/js/app-prompts.js (agentChatUi /
# agentChatMsg) and by the chat panel when rendering per-agent UI chrome.


def _deep_merge(base: dict, overrides: dict) -> dict:
    """Deep-merge two dicts — nested dicts are merged; everything else is replaced.
    Returns a NEW dict; neither argument is mutated."""
    out = dict(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _merge_chat_ui(existing, incoming) -> dict:
    """Deep-merge an incoming partial chat_ui override into the stored one.

    The stored metadata["chat_ui"] is a dict with only the keys this agent
    customises (the rest come from data/config/chat_ui.json at render time).
    Merging (rather than replacing) lets each save pass just its own keys
    without clobbering unrelated overrides. Nested dicts are merged key-by-key
    so e.g. chat_pill.stats.visible and chat_pill.buttons are independent.
    """
    base = dict(existing) if isinstance(existing, dict) else {}
    inc = dict(incoming) if isinstance(incoming, dict) else {}
    return _deep_merge(base, inc)


def _safe_agent(agent: dict) -> dict:
    """Strip locked/internal fields before returning to client.

    Prompt content lives in the agent_prompts table and is exposed via the
    dedicated /agents/{id}/slots and /agents/{id}/my-prompts endpoints, not on
    the agent row itself.
    """
    import json as _json
    HIDDEN = {"provider", "metadata"}
    result = {k: v for k, v in agent.items() if k not in HIDDEN}
    # Deserialize JSON list fields so the client receives actual arrays
    for field in ("allowed_tools", "custom_tool_ids", "loop_logic", "admin_users", "member_users", "authorized_users"):
        raw = result.get(field)
        if isinstance(raw, str):
            try:
                result[field] = _json.loads(raw)
            except Exception:
                result[field] = []
        elif raw is None:
            result[field] = []
    # Deserialize safety_policy JSON → dict
    sp_raw = result.get("safety_policy")
    if isinstance(sp_raw, str):
        try:
            result["safety_policy"] = _json.loads(sp_raw)
        except Exception:
            result["safety_policy"] = {}
    elif sp_raw is None:
        result["safety_policy"] = {}
    # Expose llm_config from metadata (defaults to use_default=True if absent)
    meta_raw = agent.get("metadata")
    meta = {}
    if isinstance(meta_raw, str):
        try:
            meta = _json.loads(meta_raw)
            # Guard against double-encoded JSON (a JSON string inside a JSON string).
            if isinstance(meta, str):
                meta = _json.loads(meta)
        except Exception:
            meta = {}
    result["llm_config"] = meta.get("llm_config") if isinstance(meta, dict) else {"use_default": True}
    # Per-agent chat UI override (deep-merged over defaults at render time).
    # Empty dict ⇒ this agent uses the app-wide defaults from chat_ui.json.
    # See _merge_chat_ui.
    cu = meta.get("chat_ui") if isinstance(meta, dict) else None
    result["chat_ui"] = cu if isinstance(cu, dict) else {}
    from app.agent.voice_config import voice_conversation_for_agent
    result["voice_conversation"] = voice_conversation_for_agent(
        {**agent, "metadata": meta})
    # Website embed widget config (enabled/allowed_domains/accent/greeting …).
    # Empty dict ⇒ never configured. Drives the Config tab's "Website Embed" card
    # + the /embed routes. See app/api/embed_config.py.
    emb = meta.get("embed") if isinstance(meta, dict) else None
    result["embed"] = emb if isinstance(emb, dict) else {}
    # Alternate runtime engine (e.g. "claude_code") + its per-agent config. Absent
    # ⇒ a normal WebAgent-LLM agent. Drives the Config tab's "Claude Code" card and
    # the loop's engine dispatch (app/agent/loop.py stream_agent_events).
    result["engine"] = meta.get("engine") if isinstance(meta, dict) else None
    cc = meta.get("claude_code") if isinstance(meta, dict) else None
    result["claude_code"] = cc if isinstance(cc, dict) else {}
    cx = meta.get("codex_code") if isinstance(meta, dict) else None
    result["codex_code"] = cx if isinstance(cx, dict) else {}
    tc = meta.get("terminal_chat") if isinstance(meta, dict) else None
    result["terminal_chat"] = tc if isinstance(tc, dict) else {}
    # Icon from metadata (the agents table has no icon column; custom agents store
    # it in metadata['icon']). Returns the raw string or empty string — the
    # frontend falls back to the default 'bot' icon when icon is falsy.
    result["icon"] = (meta.get("icon") or "") if isinstance(meta, dict) else ""
    # Modular execution modes. Defaults are materialized for legacy agents so
    # every client receives the same complete mode vocabulary.
    from app.agent.execution_modes import execution_modes_for_agent
    result["execution_modes"] = execution_modes_for_agent({**agent, "metadata": meta})
    # Default chat execution mode for NEW sessions. Edited
    # on the Config tab and read by ui/chat/js/chat-ui.js when seeding a
    # fresh session. When UNSET, agents cloned from the default WebAgent template
    # start in 'plan' (matches default.json metadata.default_execution_mode), so even
    # pre-existing default agents created before this field honour it; everything
    # else falls back to 'ask'. An explicit stored value always wins.
    _dem = meta.get("default_execution_mode") if isinstance(meta, dict) else ""
    _available_mode_ids = {m["id"] for m in result["execution_modes"]}
    if _dem not in _available_mode_ids:
        _engine = meta.get("engine") if isinstance(meta, dict) else None
        if _engine == "codex" and _dem == "wkspc":
            pass  # codex-engine mode (workspace-write) — keep as-is
        elif _engine == "claude_code":
            # Claude Code agents had only a binary act_freely toggle before this
            # field existed; surface its equivalent (True ⇒ Auto, False ⇒ Ask) so
            # the pill + Config selector match what the engine actually does until
            # the admin picks a mode. See engines/claude_code._resolve_permission_mode.
            _cc = meta.get("claude_code") if isinstance(meta.get("claude_code"), dict) else {}
            _dem = "auto" if _cc.get("act_freely", True) else "ask"
        elif agent.get("template_id") == "default":
            _dem = "plan"
        else:
            _dem = ""
    result["default_execution_mode"] = _dem
    # Default Remote Control target device (instance-id) for NEW sessions. Edited on
    # the Config tab and read by ui/chat/js/chat-ui.js to pre-select the
    # chat's target-device pill. Empty ⇒ run locally ("this device").
    result["default_target_device"] = (meta.get("default_target_device") or "") if isinstance(meta, dict) else ""
    # Public-link flag — gates the public /{agent_id} route. Reads fall back to
    # the legacy is_embeddable key so previously-enabled agents keep working.
    result["public_link"] = bool(meta.get("public_link", meta.get("is_embeddable"))) if isinstance(meta, dict) else False
    from app.agent.public_policy import normalize_public_access
    result["public_access"] = normalize_public_access({**result, "metadata": meta})
    # Resume tail messages — how many recent messages to replay on resume.
    # 0 = use the app default (32). Stored in metadata; edited on the Config tab.
    # Per-agent resume knobs — see the model comment. Blank/0 clears the key so
    # the agent inherits the app-global value again (mirrors resume_tail_messages).
    rtm = meta.get("resume_tail_messages") if isinstance(meta, dict) else None
    result["resume_tail_messages"] = int(rtm) if isinstance(rtm, int) else 0
    rma = meta.get("resume_max_attempts") if isinstance(meta, dict) else None
    result["resume_max_attempts"] = int(rma) if isinstance(rma, int) else 0
    rbk = meta.get("resume_backoff_seconds") if isinstance(meta, dict) else None
    result["resume_backoff_seconds"] = int(rbk) if isinstance(rbk, int) else 0
    rpc = meta.get("resume_max_attempts_per_cause") if isinstance(meta, dict) else None
    result["resume_max_attempts_per_cause"] = int(rpc) if isinstance(rpc, int) else 0
    # Closing-message requirement-compliance budgets — see the model comment.
    cma = meta.get("closing_max_attempts") if isinstance(meta, dict) else None
    result["closing_max_attempts"] = int(cma) if isinstance(cma, int) else 0
    cmr = meta.get("closing_max_attempts_per_requirement") if isinstance(meta, dict) else None
    result["closing_max_attempts_per_requirement"] = int(cmr) if isinstance(cmr, int) else 0
    # Assertions requirement-compliance budget — see the model comment.
    ama = meta.get("assertions_max_attempts") if isinstance(meta, dict) else None
    result["assertions_max_attempts"] = int(ama) if isinstance(ama, int) else 0
    # Close-out audit checklist — the Output Summarizer's checklist-auditor prop
    # (metadata['audit_checklist']). Read back verbatim so the Config tab can show
    # and re-edit it (string / JSON array / JSON object form). Absent ⇒ no audit.
    result["audit_checklist"] = meta.get("audit_checklist") if isinstance(meta, dict) else None
    # Complete normalized Manager Loop config (legacy flat metadata['manager']
    # rows are migrated in-memory so old agents remain editable).
    from app.agent.manager_config import manager_loop_for_agent
    result["manager_loop"] = manager_loop_for_agent({**agent, "metadata": meta})
    # Task Progress has its own lifecycle and may be enabled without Manager
    # Loop. Legacy manager.task_progress rows are normalized read-only here.
    from app.agent.task_progress_config import task_progress_for_agent
    result["task_progress"] = task_progress_for_agent({**agent, "metadata": meta})
    # Compatibility response field; the canonical owner is manager.closer.prompt.
    result["closer_prompt"] = result["manager_loop"]["closer"]["prompt"]
    # Run-contract gates: the catalog instances the standalone Gates UI edits,
    # plus its Markdown-derived type menu. Missing catalog rows are represented
    # as explicit disabled UI rows; legacy contract fields never activate them.
    from app.agent.gate_registry import gate_type_menu, instances_for_agent
    result["gates"] = {
        "instances": instances_for_agent({**agent, "metadata": meta}),
        "types": gate_type_menu(),
    }
    # Embed widget config (colors, fonts, title, custom CSS) — read by the embed page
    ec = meta.get("embed_config") if isinstance(meta, dict) else None
    result["embed_config"] = ec if isinstance(ec, dict) else {}
    # Expose template origin from metadata so the Config tab knows
    # this agent was cloned from a template and can show "push" buttons.
    result["template_origin"] = (meta.get("template_origin") or "") if isinstance(meta, dict) else ""
    # For templates (agent_templates rows), expose the source marker
    # so the frontend knows whether the template came from a JSON file.
    result["template_source"] = (meta.get("source") or "") if isinstance(meta, dict) else ""
    # Derive a single ``system`` flag the agents page uses to keep utility agents
    # (Suggested Replies / user-impersonator, source-controller, Agent Manager,
    # etc.) off the user's list by default, behind a "Show system agents" toggle.
    if agent.get("source") == "custom":
        # A user's own agent is only "system" if it explicitly opts in via metadata.
        result["system"] = bool(meta.get("system_agent") or meta.get("hidden_from_user")) if isinstance(meta, dict) else False
    else:
        tid = (agent.get("id") or agent.get("template_id") or "")
        result["system"] = bool(agent.get("is_system")) or tid in _SYSTEM_UTILITY_IDS
    return result


async def _require_admin(db, user_id: str) -> None:
    """Raise 403 if user is not an admin."""
    if not await db.is_user_admin(user_id):
        raise HTTPException(status_code=403, detail="Admin access required.")


async def _is_agent_admin(db, agent_id: str, user_id: str) -> bool:
    """Return whether the caller administers this agent.

    Installation administrators configure global service policy, but do not
    inherit authority over every independently owned agent.
    """
    from app.auth.identity import get_verified_caller_uid
    from app.agent.member_workspace import is_agent_member_subject
    if is_agent_member_subject(user_id):
        # Agent-native administrators use the agent-scoped profile APIs/cards;
        # their token is not an account for the hosting application's agent UI.
        return False
    verified = get_verified_caller_uid()
    if verified and verified != user_id:
        return False
    from app.agent.profiles import resolve_member
    member = await resolve_member(agent_id, user_id, auto_link_app=False)
    return bool(member and member.get("is_agent_admin"))


_RECENT_CONFIG_TOPIC_IDS = {
    "identity", "model", "voice", "deploy", "modes", "manager", "runtime",
}
_RECENT_CONFIG_TOPICS_LIMIT = 3
_RECENT_CONFIG_TOPICS_DDL = """
CREATE TABLE IF NOT EXISTS agent_config_recent_topics (
    user_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    topic_id TEXT NOT NULL,
    last_opened_at INTEGER NOT NULL,
    PRIMARY KEY (user_id, agent_id, topic_id)
);
CREATE INDEX IF NOT EXISTS idx_agent_config_recent_topics_lookup
    ON agent_config_recent_topics(user_id, agent_id, last_opened_at DESC);
"""


class AgentConfigRecentTopicRequest(BaseModel):
    topic_id: str = Field(..., min_length=1, max_length=32)


class MobileAgentFavoritesRequest(BaseModel):
    """One user's ordered, personal compact-sidebar favorites."""
    agent_ids: List[str] = Field(default_factory=list, max_length=200)
    expected_revision: int = Field(0, ge=0)


def _normalized_mobile_agent_favorite_ids(agent_ids: List[str]) -> List[str]:
    """Bound and de-duplicate a personal preference payload without agent lookup."""
    normalized: List[str] = []
    seen = set()
    for agent_id in agent_ids:
        if not isinstance(agent_id, str):
            raise HTTPException(status_code=422, detail="Favorite agent IDs must be strings.")
        agent_id = agent_id.strip()
        if not agent_id or len(agent_id) > 200:
            raise HTTPException(status_code=422, detail="Favorite agent IDs must be 1–200 characters.")
        if agent_id in seen:
            continue
        seen.add(agent_id)
        normalized.append(agent_id)
    return normalized


class AgentPageFavoritesRequest(BaseModel):
    """One user's ordered, global Agent-card subpage favorites."""
    page_ids: List[str] = Field(default_factory=list, max_length=100)
    expected_revision: int = Field(0, ge=0)


def _normalized_agent_page_favorite_ids(page_ids: List[str]) -> List[str]:
    """Bound/de-duplicate page IDs while preserving explicit user order."""
    normalized: List[str] = []
    seen = set()
    for page_id in page_ids:
        if not isinstance(page_id, str):
            raise HTTPException(status_code=422, detail="Favorite page IDs must be strings.")
        page_id = page_id.strip()
        if not page_id or len(page_id) > 64:
            raise HTTPException(status_code=422, detail="Favorite page IDs must be 1–64 characters.")
        if page_id in seen:
            continue
        seen.add(page_id)
        normalized.append(page_id)
    # Config is always present but never pinned: append it only if omitted.
    return normalized if "config" in seen else [*normalized, "config"]


class MyAgentsPageFavoritesRequest(BaseModel):
    """One user's ordered favorites for the My Agents (All Agents) card."""
    page_ids: List[str] = Field(default_factory=list, max_length=100)
    expected_revision: int = Field(0, ge=0)


def _normalized_my_agents_page_favorite_ids(page_ids: List[str]) -> List[str]:
    """Bound/de-duplicate My Agents page IDs while preserving explicit user order."""
    normalized: List[str] = []
    seen = set()
    for page_id in page_ids:
        if not isinstance(page_id, str):
            raise HTTPException(status_code=422, detail="Favorite page IDs must be strings.")
        page_id = page_id.strip()
        if not page_id or len(page_id) > 64:
            raise HTTPException(status_code=422, detail="Favorite page IDs must be 1–64 characters.")
        if page_id in seen:
            continue
        seen.add(page_id)
        normalized.append(page_id)
    # Config is always present but never pinned: append it only if omitted.
    return normalized if "config" in seen else [*normalized, "config"]


def _agent_config_recent_connection(db):
    """Return the local SQLite connection and self-heal pre-migration databases."""
    if not hasattr(db, "_get_conn"):
        raise HTTPException(status_code=503, detail="Agent Config recents require the local SQLite server store.")
    conn = db._get_conn()
    try:
        conn.executescript(_RECENT_CONFIG_TOPICS_DDL)
        conn.commit()
    except Exception:
        conn.close()
        raise
    return conn


async def _require_ability_enabled(db, agent_id: str, ability_id: str) -> None:
    """Raise 403 if the OAuth ability isn't enabled on this agent.

    Covers fine-grained capabilities in `agent_abilities` (distinct from the
    provider-level rows in `agent_connections`).
    """
    if not hasattr(db, "get_agent_abilities"):
        return
    rows = await db.get_agent_abilities(agent_id)
    match = next((r for r in rows if r.get("ability_id") == ability_id), None)
    if not match or not match.get("enabled"):
        raise HTTPException(status_code=403, detail=f"Ability '{ability_id}' is not enabled on this agent.")


# ── Routes ────────────────────────────────────────────────────────────────────


@router.get("/user/profile")
async def get_user_profile(request: Request, user_id: str = Query(...)):
    """Return the caller's basic user profile and cacheable personal UI preferences."""
    db = get_db()
    user_id = await assert_caller_is(request, user_id)
    profile = await db.get_user_profile(user_id)
    favorites = await db.get_user_agent_sidebar_preferences(user_id)
    page_favorites = await db.get_user_agent_page_preferences(user_id)
    my_agents_page_favorites = await db.get_user_my_agents_page_preferences(user_id)
    if not profile:
        # Return a safe default rather than 404 — profile is auto-created on first write.
        return {
            "user_id": user_id,
            "is_admin": False,
            "default_agent_id": None,
            "mobile_agent_favorites": favorites,
            "agent_page_favorites": page_favorites,
            "my_agents_page_favorites": my_agents_page_favorites,
        }
    return {
        "user_id": profile["user_id"],
        "is_admin": bool(profile.get("is_admin")),
        "default_agent_id": profile.get("default_agent_id"),
        "mobile_agent_favorites": favorites,
        "agent_page_favorites": page_favorites,
        "my_agents_page_favorites": my_agents_page_favorites,
    }


@router.get("/user/agent-page-favorites")
async def get_agent_page_favorites(request: Request, user_id: str = Query(...)):
    """Return this user's global, cross-device Agent-card subpage preference."""
    db = get_db()
    user_id = await assert_caller_is(request, user_id)
    return await db.get_user_agent_page_preferences(user_id)


@router.put("/user/agent-page-favorites")
async def replace_agent_page_favorites(
    payload: AgentPageFavoritesRequest,
    request: Request,
    user_id: str = Query(...),
):
    """Persist ordered global Agent-card page favorites with optimistic locking."""
    db = get_db()
    user_id = await assert_caller_is(request, user_id)
    result = await db.replace_user_agent_page_preferences(
        user_id,
        _normalized_agent_page_favorite_ids(payload.page_ids),
        payload.expected_revision,
    )
    if result.get("conflict"):
        return JSONResponse(
            status_code=409,
            content={"code": "agent_page_favorites_revision_conflict", "current": result["snapshot"]},
        )
    return result["snapshot"]


@router.get("/user/my-agents-page-favorites")
async def get_my_agents_page_favorites(request: Request, user_id: str = Query(...)):
    """Return this user's cross-device My Agents (All Agents) page preference."""
    db = get_db()
    user_id = await assert_caller_is(request, user_id)
    return await db.get_user_my_agents_page_preferences(user_id)


@router.put("/user/my-agents-page-favorites")
async def replace_my_agents_page_favorites(
    payload: MyAgentsPageFavoritesRequest,
    request: Request,
    user_id: str = Query(...),
):
    """Persist ordered My Agents page favorites with optimistic locking."""
    db = get_db()
    user_id = await assert_caller_is(request, user_id)
    result = await db.replace_user_my_agents_page_preferences(
        user_id,
        _normalized_my_agents_page_favorite_ids(payload.page_ids),
        payload.expected_revision,
    )
    if result.get("conflict"):
        return JSONResponse(
            status_code=409,
            content={"code": "my_agents_page_favorites_revision_conflict", "current": result["snapshot"]},
        )
    return result["snapshot"]


@router.put("/user/mobile-agent-favorites")
async def replace_mobile_agent_favorites(
    payload: MobileAgentFavoritesRequest,
    request: Request,
    user_id: str = Query(...),
):
    """Persist one user's ordered compact-sidebar favorites with optimistic locking."""
    db = get_db()
    user_id = await assert_caller_is(request, user_id)
    result = await db.replace_user_agent_sidebar_preferences(
        user_id,
        _normalized_mobile_agent_favorite_ids(payload.agent_ids),
        payload.expected_revision,
    )
    if result.get("conflict"):
        return JSONResponse(
            status_code=409,
            content={"code": "mobile_agent_favorites_revision_conflict", "current": result["snapshot"]},
        )
    return result["snapshot"]


@router.get("/agents/{agent_id}/config/recent-topics")
async def get_agent_config_recent_topics(
    request: Request,
    agent_id: str,
    user_id: str = Query(...),
):
    """Return this caller's three most recently opened Config topics for one agent."""
    db = get_db()
    user_id = await assert_caller_is(request, user_id)
    if not await _is_agent_admin(db, agent_id, user_id):
        raise HTTPException(status_code=403, detail="Agent admin access required.")
    conn = _agent_config_recent_connection(db)
    try:
        rows = conn.execute(
            """SELECT topic_id FROM agent_config_recent_topics
               WHERE user_id = ? AND agent_id = ?
               ORDER BY last_opened_at DESC
               LIMIT ?""",
            (user_id, agent_id, _RECENT_CONFIG_TOPICS_LIMIT),
        ).fetchall()
        topic_ids = [row["topic_id"] for row in rows if row["topic_id"] in _RECENT_CONFIG_TOPIC_IDS]
        return {"topic_ids": topic_ids}
    finally:
        conn.close()


@router.post("/agents/{agent_id}/config/recent-topics")
async def remember_agent_config_recent_topic(
    payload: AgentConfigRecentTopicRequest,
    request: Request,
    agent_id: str,
    user_id: str = Query(...),
):
    """Record one Config topic open and return the authoritative top-three list."""
    db = get_db()
    user_id = await assert_caller_is(request, user_id)
    if not await _is_agent_admin(db, agent_id, user_id):
        raise HTTPException(status_code=403, detail="Agent admin access required.")
    if payload.topic_id not in _RECENT_CONFIG_TOPIC_IDS:
        raise HTTPException(status_code=422, detail="Unknown Agent Config topic.")
    conn = _agent_config_recent_connection(db)
    try:
        conn.execute(
            """INSERT INTO agent_config_recent_topics (user_id, agent_id, topic_id, last_opened_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id, agent_id, topic_id)
               DO UPDATE SET last_opened_at = excluded.last_opened_at""",
            (user_id, agent_id, payload.topic_id, time.time_ns()),
        )
        conn.execute(
            """DELETE FROM agent_config_recent_topics
               WHERE user_id = ? AND agent_id = ?
                 AND topic_id NOT IN (
                   SELECT topic_id FROM agent_config_recent_topics
                   WHERE user_id = ? AND agent_id = ?
                   ORDER BY last_opened_at DESC
                   LIMIT ?
                 )""",
            (user_id, agent_id, user_id, agent_id, _RECENT_CONFIG_TOPICS_LIMIT),
        )
        conn.commit()
        rows = conn.execute(
            """SELECT topic_id FROM agent_config_recent_topics
               WHERE user_id = ? AND agent_id = ?
               ORDER BY last_opened_at DESC
               LIMIT ?""",
            (user_id, agent_id, _RECENT_CONFIG_TOPICS_LIMIT),
        ).fetchall()
        return {"topic_ids": [row["topic_id"] for row in rows]}
    finally:
        conn.close()


@router.get("/agents/templates")
async def list_agent_templates(
    request: Request,
    user_id: str = Query(...),
    include_admin: bool = Query(False),
    discoverable_only: bool = Query(False),
):
    """
    List agent templates (system agents, is_pipeline=0).
    If include_admin=true, requires the user to be an admin (bypasses discoverable filter).
    If discoverable_only=true, only returns templates with discoverable=1.
    """
    db = get_db()
    user_id = await assert_caller_is(request, user_id)
    if include_admin:
        await _require_admin(db, user_id)
    capabilities = await resolve_capabilities(user_id, db=db)
    templates = await db.list_agent_templates(
        include_admin=include_admin,
        discoverable_only=(discoverable_only or not include_admin),
    )
    return {
        "templates": [
            _safe_agent(template)
            for template in templates
            if _template_is_allowed(capabilities, str(template.get("id") or ""))
        ]
    }


@router.get("/agents")
async def list_agents(request: Request, user_id: str = Query(...), include_system: bool = Query(False),
                      view: str = Query("active")):
    """
    List the user's own agents (the agents they added themselves).

    By default only non-system agents the user administers are returned, so the
    Agents page stays clean. Pass ``include_system=true`` to ALSO include the
    built-in utility agents (Suggested Replies / user-impersonator, GitHub /
    source-controller, Agent Manager / agent-builder, etc.) — used by the
    "Show system agents" toggle. Each entry carries ``source`` ('custom' or
    'template') and a derived ``system`` boolean.

    When enabled in App Settings, every user also sees the one admin-owned shared
    WebAgent. No per-user default clone is created.
    """
    db = get_db()
    user_id = await assert_caller_is(request, user_id)
    from app.agent.member_workspace import is_agent_member_subject
    if is_agent_member_subject(user_id):
        raise HTTPException(status_code=403, detail="Agent-local identities do not have app roster access")
    if request_user_id(request) != user_id:
        raise HTTPException(status_code=403, detail="Agent rosters cannot be impersonated")
    bin_view = (view == "bin")
    clones_view = (view == "clones")
    if clones_view:
        all_agents = await db.list_agents_for_user(user_id, include_admin=False,
                                                   view="clones")
    else:
        all_agents = await db.list_agents_for_user(user_id, include_admin=False,
                                                   view=("bin" if bin_view else "active"))
    out = []
    for a in all_agents:
        safe = _safe_agent(a)
        if a.get("source") == "custom":
            # The user's own agents — hide system-flagged ones unless asked for.
            if safe.get("system") and not include_system:
                continue
            out.append(safe)
        elif include_system and safe.get("system"):
            # Built-in utility templates, only when the toggle is on.
            out.append(safe)

    # ── Enrich custom agents with their template's metadata source ──────────
    # so the Config tab knows whether the template has a JSON seed file
    # (showing "Push to file") or is admin-only (showing only "Push to DB").
    _custom_with_tpl = [a for a in out if a.get("template_id")]
    if _custom_with_tpl and hasattr(db, "_get_conn"):
        try:
            conn = db._get_conn()
            try:
                tpl_ids = list({a["template_id"] for a in _custom_with_tpl})
                placeholders = ",".join("?" for _ in tpl_ids)
                tpl_rows = conn.execute(
                    f"SELECT id, metadata FROM agent_templates WHERE id IN ({placeholders})",
                    tpl_ids,
                ).fetchall()
                tpl_source = {}
                for row in tpl_rows:
                    try:
                        m = _json.loads(row["metadata"]) if row["metadata"] else {}
                    except Exception:
                        m = {}
                    tpl_source[row["id"]] = (m.get("source") or "") if isinstance(m, dict) else ""
                for a in _custom_with_tpl:
                    a["template_source"] = tpl_source.get(a["template_id"], "")
                    # Derive: is this template backed by a JSON file?
                    tsrc = a.get("template_source", "")
                    a["template_has_json"] = (tsrc == "json_seed")
            finally:
                conn.close()
        except Exception as e:
            logger.debug("Failed to enrich template_source on agents: %s", e)

    # ── Auto-provision default agent for new users ──────────────────────────
    # When shared_default_agent_enabled is on, inject the single shared agent
    # (id="shared_default") into every user's roster so they see it from the
    # first page load — no per-user agent row is ever created.
    from app.admin.settings import shared_default_agent_enabled as _sd_list
    if _sd_list():
        try:
            shared = await provision_default_agent(db, user_id)
            if shared:
                # Avoid duplicating if the shared agent already appeared in out
                # (e.g. the user is the app admin and it showed up via custom path).
                if not any(a.get("id") == "shared_default" for a in out):
                    safe = _safe_agent(shared)
                    safe["is_user_default"] = 1
                    out.append(safe)
        except Exception as e:
            logger.warning("Shared default agent inject for %s failed: %s", user_id, e)
    # When disabled, do not resurrect the retired per-user WebAgent provisioning
    # path. A user with no custom agents intentionally receives an empty roster.

    return {"agents": out}


@router.post("/agents/reorder")
async def reorder_agents(req: ReorderAgentsRequest, request: Request):
    """
    Persist the manual order of a user's custom agents (drag-to-reorder in the
    chat-header agent dropdown). Each id in ``order`` gets sort_order = its
    index; only agents the caller administers are touched. The same order is
    consumed by the Agents page so the two views stay in sync.
    """
    db = get_db()
    req.user_id = await assert_caller_is(request, req.user_id)
    if not hasattr(db, "reorder_agents"):
        raise HTTPException(status_code=501, detail="Reordering is not supported by the active storage backend.")
    updated = await db.reorder_agents(req.user_id, req.order)
    return {"success": True, "updated": updated}


@router.post("/agents")
async def create_agent(req: CreateAgentRequest, request: Request):
    """
    Create a new custom agent cloned from the default template.
    Returns the new agent with editable fields only.
    """
    db = get_db()
    req.user_id = await assert_caller_is(request, req.user_id)
    from app.agent.member_workspace import is_agent_member_subject
    if req.user_id.startswith("anon_") or is_agent_member_subject(req.user_id):
        raise HTTPException(status_code=403, detail={
            "code": "registration_required",
            "feature": "agent_create",
            "message": "Register or sign in to create and manage agents.",
        })
    if not req.name or not req.name.strip():
        raise HTTPException(status_code=400, detail="Agent name is required.")
    # A clone of the default WebAgent inherits its Advanced capability shape.
    # Blank/custom agents start at Simple unless the caller chooses otherwise.
    _template_id = req.template_id or "default"
    _capability_profile = req.capability_profile or (
        "advanced" if _template_id == "default" else "simple"
    )
    async with _agent_create_lock(req.user_id):
        try:
            await enforce_agent_materialization(
                db, req.user_id, template_id=_template_id,
            )
        except ResourceEntitlementError as exc:
            raise _resource_http_error(exc) from exc
        agent = await db.create_custom_agent(
            user_id=req.user_id,
            name=req.name.strip(),
            description=req.description or "",
            template_id=_template_id,
            seed_abilities=False,
            capability_profile=_capability_profile,
            capability_extensions=req.capability_extensions or [],
        )
        # Direct/local backends do not pass through PlaneRouterBackend's create
        # hook. Seed the independent agent authorization plane here as well;
        # the operation is idempotent when the router already did it.
        from app.agent.profiles import ensure_builtins
        await ensure_builtins(agent["id"], agent=agent, creator_user_id=req.user_id)
    # Apply every create-time Config field through the same validation,
    # normalization, and persistence path used by the final Config page.
    _create_only = {
        "user_id", "name", "description", "template_id",
        "capability_profile", "capability_extensions",
    }
    _initial_updates = {
        key: value for key, value in req.model_dump(exclude_unset=True).items()
        if key not in _create_only
    }
    # Seed the new agent's output ceiling from the app-wide default (App Settings
    # → App Functions → Default Max Output Tokens) so a created agent starts at
    # the configured number instead of a hard-coded template one. From here the
    # agent's own Runtime → Limits value is authoritative at run time.
    if "max_tokens" not in _initial_updates:
        from app.admin.settings import get_default_max_tokens
        _initial_updates["max_tokens"] = get_default_max_tokens()
    # A caller-supplied roster is authoritative. Otherwise seed the new agent
    # from their catalog default (or the shared_default onboarding selection).
    if "llm_config" not in _initial_updates:
        catalog_seed = await _catalog_seed_for_new_agent(db, req.user_id)
        if catalog_seed:
            _initial_updates["llm_config"] = catalog_seed
    if _initial_updates:
        updated_result = await update_agent(
            agent["id"],
            UpdateAgentRequest(user_id=req.user_id, **_initial_updates),
            request,
        )
        agent = updated_result["agent"]
    # Platform admins' own agents are exempt from payment by default.
    # The admin can delete the exemption later via /billing/exemptions if
    # they want to charge for their own agent.
    try:
        if await db.is_user_admin(req.user_id):
            await _maybe_auto_exempt_agent(db, agent["id"], req.user_id)
    except Exception:
        pass
    safe = _safe_agent(agent)
    # Live-sync every open tab/device for this user so the Agents grid shows the
    # new agent without a manual refresh.
    from app.events.delivery import notify_user
    await notify_user(req.user_id, {
        "type": "agent_created", "user_id": req.user_id, "agent": safe,
    })
    return {"agent": safe}


class EnsureDefaultRequest(BaseModel):
    user_id: str


@router.post("/agents/ensure-default")
async def ensure_default_agent(req: EnsureDefaultRequest, request: Request):
    """Return the caller's WebAgent, creating it only if one doesn't already
    exist on the authority. Idempotent — the chat panel calls this on load, so
    repeated calls (and concurrent devices) all resolve to the SAME agent instead
    of minting duplicates. Contrast with POST /agents, which always creates."""
    db = get_db()
    req.user_id = await assert_caller_is(request, req.user_id)
    agent = await provision_default_agent(db, req.user_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="The shared default WebAgent is disabled.")
    # A freshly-created WebAgent gets the same admin payment exemption POST
    # /agents grants; a reused one already has it (the insert is a no-op).
    try:
        if await db.is_user_admin(req.user_id):
            await _maybe_auto_exempt_agent(db, agent["id"], req.user_id)
    except Exception:
        pass
    safe = _safe_agent(agent)
    # Live-sync open tabs/devices (harmless if the agent already existed).
    try:
        from app.events.delivery import notify_user
        await notify_user(req.user_id, {
            "type": "agent_created", "user_id": req.user_id, "agent": safe,
        })
    except Exception:
        pass
    return {"agent": safe}


async def _maybe_auto_exempt_agent(db, agent_id: str, granting_user_id: str) -> None:
    """Insert a kind='agent' exemption if one doesn't already exist."""
    if not hasattr(db, "_get_conn"):
        return
    import uuid as _uuid
    conn = db._get_conn()
    try:
        existing = conn.execute(
            "SELECT id FROM billing_exemptions WHERE kind='agent' AND agent_id=? LIMIT 1",
            (agent_id,),
        ).fetchone()
        if existing:
            return
        conn.execute(
            "INSERT INTO billing_exemptions (id, kind, agent_id, granted_by_user_id, reason) VALUES (?,?,?,?,?)",
            (str(_uuid.uuid4()), "agent", agent_id, granting_user_id,
             "auto: platform-admin-owned agent"),
        )
        conn.commit()
    except Exception:
        # Table may not exist yet pre-migration; safe to ignore.
        pass
    finally:
        conn.close()


# ── Agent display-fields cache (bulk /agents/display) ──────────────────────
# Session lists (chat dropdown, Sessions page, launcher widget) hydrate agent
# name/icon/engine from this lean endpoint so they never depend on full agent
# records — and never touch the agent plane more than once per agent per TTL,
# no matter how many consumers ask. The full agent rows are NOT cached here;
# only the three display fields, so a config edit invalidates cheaply.
_DISPLAY_CACHE_TTL_S = 60
_display_cache: Dict[str, Dict[str, Any]] = {}  # agent_id -> {"data": {...}, "ts": float}


def _invalidate_agent_display_cache(agent_id: str) -> None:
    """Drop one agent's display entry so the next bulk read re-fetches it."""
    _display_cache.pop(agent_id, None)


@router.get("/agents/display")
async def agent_display_bulk(
    request: Request,
    user_id: str = Query(...),
    ids: str = Query(..., description="Comma-separated agent ids to resolve"),
):
    """Return ONLY display fields (name/icon/engine) for a batch of agent ids.

    Used by session-list hydration: a session payload always carries agent_id,
    and the client fills missing display cells from this endpoint. Missing ids
    are simply absent from the response — never an error. Served from a short
    in-process TTL cache (one agent-plane read per agent per 60s, shared by
    every consumer).
    """
    await assert_caller_is(request, user_id)
    # Dedupe while preserving order — a client may list the same id twice.
    wanted = list(dict.fromkeys(a for a in (x.strip() for x in ids.split(",")) if a))
    now = time.time()
    out = []
    for aid in wanted:
        entry = _display_cache.get(aid)
        if entry and now - entry["ts"] < _DISPLAY_CACHE_TTL_S:
            out.append(entry["data"])
            continue
        try:
            agent = await get_db().get_agent_by_id(aid)
        except Exception:
            agent = None
        if not agent:
            # Don't cache misses long — a deleted/unknown agent may reappear.
            _display_cache.pop(aid, None)
            continue
        meta = agent.get("metadata") or {}
        if isinstance(meta, str):
            try:
                meta = _json.loads(meta)
                if isinstance(meta, str):  # double-encoded guard, like _safe_agent
                    meta = _json.loads(meta)
            except Exception:
                meta = {}
        if not isinstance(meta, dict):
            meta = {}
        rec = {
            "agent_id": aid,
            "name": agent.get("name") or "",
            "icon": agent.get("icon") or meta.get("icon") or "",
            "engine": meta.get("engine") or agent.get("engine") or "",
            "updated_at": agent.get("updated_at") or "",
        }
        _display_cache[aid] = {"data": rec, "ts": now}
        out.append(rec)
    return {"agents": out}


@router.get("/agents/{agent_id}")
async def get_agent(request: Request, agent_id: str, user_id: str = Query(...)):
    """Get a single custom agent by id (must be owned by user)."""
    db = get_db()
    user_id = await assert_caller_is(request, user_id)
    # Check custom agents table
    agents = await db.list_agents_for_user(user_id)
    for a in agents:
        if a.get("id") == agent_id:
            result = _safe_agent(a)
            llm_config = result.get("llm_config")
            if (isinstance(llm_config, dict)
                    and isinstance(llm_config.get("multi_providers"), list)):
                from app.admin.settings import (
                    _load_agent_llm_config,
                    _public_provider_config,
                )
                hydrated = await _load_agent_llm_config(agent_id, llm_config)
                result["llm_config"] = _public_provider_config(hydrated)
            # Enrich with the template's metadata source (for "Push to file" button).
            tid = result.get("template_id")
            if tid and hasattr(db, "_get_conn"):
                try:
                    conn = db._get_conn()
                    try:
                        tpl_row = conn.execute(
                            "SELECT metadata FROM agent_templates WHERE id = ?", (tid,)
                        ).fetchone()
                        if tpl_row:
                            try:
                                m = _json.loads(tpl_row["metadata"]) if tpl_row["metadata"] else {}
                            except Exception:
                                m = {}
                            tsrc = (m.get("source") or "") if isinstance(m, dict) else ""
                            result["template_source"] = tsrc
                            result["template_has_json"] = (tsrc == "json_seed")
                    finally:
                        conn.close()
                except Exception as e:
                    logger.debug("Failed to enrich template_source on get_agent %s: %s", agent_id, e)
            try:
                from app.abilities import app_function_enabled
                from app.agent.loop_executor import LoopConfig
                run_manager_ok = bool(app_function_enabled("run_manager"))
                manager_node_ok = LoopConfig.from_agent(a).is_enabled("manager_chk")
                orchestration_ok = any(
                    row.get("section") == "ability"
                    and row.get("connection_type") == "agent_orchestration"
                    and row.get("enabled")
                    for row in await db.get_agent_connections(agent_id)
                )
                result["manager_contract_preflight"] = {
                    "run_manager": run_manager_ok,
                    "manager_chk": manager_node_ok,
                    "agent_orchestration": orchestration_ok,
                    "ready": run_manager_ok and manager_node_ok and orchestration_ok,
                }
            except Exception as e:
                logger.debug("Could not resolve contract preflight for %s: %s", agent_id, e)
                result["manager_contract_preflight"] = {
                    "run_manager": False, "manager_chk": False,
                    "agent_orchestration": False, "ready": False,
                    "error": "Preflight could not be loaded.",
                }
            if agent_id == SHARED_DEFAULT_AGENT_ID:
                try:
                    seed_path = Path(__file__).resolve().parents[1] / "defaults" / "agents" / "default.json"
                    seed = _json.loads(seed_path.read_text(encoding="utf-8"))
                    seed_manager = ((seed.get("metadata") or {}).get("manager") or {})
                    from app.agent.manager_config import manager_loop_for_agent
                    normalized_seed = manager_loop_for_agent({
                        "metadata": {"manager": seed_manager},
                    })
                    # Compare every surface the seed is expected to own. The node
                    # drift that started this (manager_chk echoing a stale value
                    # while the master switch was off) was invisible while only
                    # manager_loop was compared.
                    _seeded_nodes = seed.get("loop_logic") or []
                    result["shared_default_seed_diverged"] = (
                        _json.dumps(result.get("manager_loop") or {}, sort_keys=True, default=str)
                        != _json.dumps(normalized_seed, sort_keys=True, default=str)
                        or _json.dumps(result.get("loop_logic") or [], sort_keys=True, default=str)
                        != _json.dumps(_seeded_nodes, sort_keys=True, default=str)
                    )
                except Exception:
                    result["shared_default_seed_diverged"] = None
            return {"agent": result}
    raise HTTPException(status_code=404, detail="Agent not found.")


@router.put("/agents/{agent_id}")
async def update_agent(agent_id: str, req: UpdateAgentRequest, request: Request):
    """
    Update editable fields on a custom agent. Caller must be an agent admin.

    Two write lanes coexist on this endpoint:
      - Agent-row fields (name, model, allowed_tools, etc.) via `updates`.
      - Admin-base prompt slots via `slots` (full slot set — reconciled).
        Optional `reset_overrides_for` wipes per-user override rows for the
        listed slot_names at save time.
    """
    db = get_db()
    req.user_id = await assert_caller_is(request, req.user_id)
    shared_default_edit = agent_id == SHARED_DEFAULT_AGENT_ID
    if shared_default_edit:
        # The live singleton is seed-backed but remains ordinary runtime state.
        # Installation admins may tune it here; exporting to the JSON seed is a
        # separate explicit operation.
        await _require_admin(db, req.user_id)
    elif not await _is_agent_admin(db, agent_id, req.user_id):
        raise HTTPException(status_code=403, detail="Only agent admins can edit this agent.")

    import json as _json
    payload = req.dict()
    slots_in = payload.pop("slots", None)
    reset_for = payload.pop("reset_overrides_for", None)
    icon_in = payload.pop("icon", None)
    llm_config_in = payload.pop("llm_config", None)
    chat_ui_in = payload.pop("chat_ui", None)
    voice_conversation_in = payload.pop("voice_conversation", None)
    claude_code_in = payload.pop("claude_code", None)
    codex_code_in = _normalize_codex_code_update(payload.pop("codex_code", None))
    embed_in = payload.pop("embed", None)
    terminal_chat_in = payload.pop("terminal_chat", None)
    exec_mode_in = payload.pop("default_execution_mode", None)
    exec_modes_in = payload.pop("execution_modes", None)
    target_device_in = payload.pop("default_target_device", None)
    public_link_in = payload.pop("public_link", None)
    public_access_in = payload.pop("public_access", None)
    resume_tail_in = payload.pop("resume_tail_messages", None)
    resume_max_in = payload.pop("resume_max_attempts", None)
    resume_backoff_in = payload.pop("resume_backoff_seconds", None)
    resume_per_cause_in = payload.pop("resume_max_attempts_per_cause", None)
    closing_max_in = payload.pop("closing_max_attempts", None)
    closing_per_req_in = payload.pop("closing_max_attempts_per_requirement", None)
    assertions_max_in = payload.pop("assertions_max_attempts", None)
    audit_checklist_in = payload.pop("audit_checklist", None)
    closer_prompt_in = payload.pop("closer_prompt", None)
    manager_loop_in = payload.pop("manager_loop", None)
    task_progress_in = payload.pop("task_progress", None)
    gates_in = payload.pop("gates", None)
    from app.agent.execution_modes import normalize_execution_modes, normalize_mode_id
    if exec_modes_in is not None:
        if not isinstance(exec_modes_in, list):
            raise HTTPException(status_code=400, detail="execution_modes must be a list.")
        exec_modes_in = normalize_execution_modes(exec_modes_in)
    # Normalize the default chat mode (accept legacy read/write aliases).
    if exec_mode_in is not None:
        _m = normalize_mode_id(exec_mode_in, fallback="")
        # 'wkspc' is the codex-engine workspace-write mode — offered only on codex
        # agent cards (native agents never send it).
        if not _m and str(exec_mode_in).strip().lower() != "wkspc":
            raise HTTPException(status_code=400, detail="Invalid default_execution_mode.")
        exec_mode_in = _m or "wkspc"
    updates = {k: v for k, v in payload.items()
               if k not in ("user_id",) and v is not None}
    removed_subscription_entry_ids = []

    # Merge metadata-backed blobs (llm_config, chat_ui, icon, default chat mode)
    # into the agent's metadata. All are read-modify-write against the current
    # metadata so they don't clobber each other or the rest of the blob.
    if (llm_config_in is not None or chat_ui_in is not None or voice_conversation_in is not None
            or claude_code_in is not None or codex_code_in is not None
            or terminal_chat_in is not None or embed_in is not None
            or icon_in is not None or exec_mode_in is not None or exec_modes_in is not None
            or target_device_in is not None
            or public_link_in is not None
            or public_access_in is not None
            or resume_tail_in is not None
            or resume_max_in is not None
            or resume_backoff_in is not None
            or audit_checklist_in is not None
            or closer_prompt_in is not None
            or manager_loop_in is not None
            or task_progress_in is not None
            or gates_in is not None):
        current = await db.get_agent_by_id(agent_id)
        meta = {}
        if current:
            meta_raw = current.get("metadata")
            if isinstance(meta_raw, str):
                try:
                    meta = _json.loads(meta_raw)
                except Exception:
                    pass
            elif isinstance(meta_raw, dict):
                meta = dict(meta_raw)
        if llm_config_in is not None:
            # The roster is the only persisted agent-model authority. Strip
            # legacy top-level model pins and assign opaque IDs to any ID-less
            # rows at the API boundary for every client.
            from app.admin.settings import _without_saved_default
            from app.entitlements.rosters import (
                RosterError, assign_entry_ids, validate_live_voice_assignments,
            )
            llm_config_in = _without_saved_default(llm_config_in)
            roster = llm_config_in.get("multi_providers")
            if isinstance(roster, list):
                existing_llm = meta.get("llm_config") if isinstance(meta.get("llm_config"), dict) else {}
                from app.admin.settings import (
                    _load_agent_llm_config, _load_app_settings, _load_own_llm_config,
                    _persist_llm_config,
                )
                existing_llm = await _load_agent_llm_config(agent_id, existing_llm)
                from app import openai_subscription
                allowed_existing = await openai_subscription.existing_entries_with_reservations(
                    roster, existing_llm.get("multi_providers") or [],
                    scope="agent", agent_id=agent_id,
                )
                # An agent may adopt only an entry from THIS caller's resolved
                # catalog: their owned BYO roster plus their published entitlement
                # roster. Never accept an app-global/admin catalog identity or a
                # client-invented entry ID. The sidecar remains agent-scoped.
                # Explicit adoption may use a caller-owned BYO entry or an entitled
                # App Catalog entry. This is validation for a submitted roster row,
                # never runtime/UI inheritance into the agent.
                if True:
                    from app.entitlements.rosters import resolve_platform_roster_config
                    owned_catalog = _without_saved_default(
                        await _load_own_llm_config(req.user_id) or {}
                    )
                    entitlement_catalog = _without_saved_default(
                        await resolve_platform_roster_config(req.user_id, db=get_db()) or {}
                    )
                    allowed_existing.extend(
                        row for catalog in (owned_catalog, entitlement_catalog)
                        for row in (catalog.get("multi_providers") or [])
                        if isinstance(row, dict) and row.get("entry_id")
                    )
                    # WebAgent Catalog clients carry only an opaque entry ID and
                    # alias. Rehydrate the real entitled platform row server-side
                    # before validation/persistence; never trust client provider/model.
                    platform_by_id = {
                        str(row.get("entry_id") or ""): row
                        for row in (entitlement_catalog.get("multi_providers") or [])
                        if isinstance(row, dict) and row.get("entry_id")
                    }
                    # MODEL-NAME-LEVELS: rehydrate every adopted catalog row from its
                    # entitled platform row, keeping the caller's own display name.
                    account_aliases = catalog_account_aliases(owned_catalog)
                    roster = [
                        merge_entitled_catalog_row(row, platform_by_id, account_aliases)
                        for row in roster
                    ]
                try:
                    scoped_roster = assign_entry_ids(
                        roster,
                        existing_entries=allowed_existing,
                    )
                except RosterError as exc:
                    raise HTTPException(status_code=422, detail=str(exc)) from exc
                try:
                    validate_live_voice_assignments(scoped_roster)
                except RosterError as exc:
                    raise HTTPException(status_code=422, detail=str(exc)) from exc
                for entry in scoped_roster:
                    entry["display_name"] = str(entry.get("display_name") or "").strip()
                    if len(entry["display_name"]) > 120:
                        raise HTTPException(status_code=422, detail="Model display name is too long")
                    entry["credential_scope"] = "agent"
                    if entry.get("provider") == "openai_subscription" or entry.get("auth_mode") == "chatgpt":
                        # Provider authorization controls whether this model can execute,
                        # not whether an agent may own its roster role. Retain role flags
                        # so adopted inherited rows persist their intended assignments;
                        # runtime credential checks report the separate authorization state.
                        entry["api_key"] = ""
                        entry["credential_scope"] = "agent"
                existing_by_id = {
                    str(row.get("entry_id") or ""): row
                    for row in existing_llm.get("multi_providers") or []
                    if isinstance(row, dict)
                }
                for entry in scoped_roster:
                    is_subscription = (
                        entry.get("provider") == "openai_subscription"
                        or entry.get("auth_mode") == "chatgpt"
                    )
                    if not entry.get("api_key") and not is_subscription:
                        prior = existing_by_id.get(str(entry.get("entry_id") or "")) or {}
                        if prior.get("api_key"):
                            entry["api_key"] = prior["api_key"]
                llm_config_in["multi_providers"] = scoped_roster
                from app.admin.settings import _role_assignments_for_config
                try:
                    llm_config_in, _ = _role_assignments_for_config(
                        llm_config_in, migrate_legacy=True,
                    )
                except ValueError as exc:
                    raise HTTPException(status_code=422, detail=str(exc)) from exc
                incoming_ids = {str(row.get("entry_id") or "") for row in scoped_roster}
                removed_subscription_entry_ids = [
                    str(row.get("entry_id") or "")
                    for row in existing_llm.get("multi_providers") or [] if isinstance(row, dict)
                    and str(row.get("entry_id") or "") not in incoming_ids
                    and (row.get("provider") == "openai_subscription" or row.get("auth_mode") == "chatgpt")
                ]
                from app.admin.settings import _persist_agent_llm_config
                llm_config_in = await _persist_agent_llm_config(agent_id, llm_config_in)
                # Explicitly adopting an App Catalog row for an agent also adds
                # its opaque ID to the caller's personal Available models
                # catalog. Keep only IDs in the sidecar; expanded provider/model
                # fields remain confined to the agent runtime configuration.
                adopted_platform_ids = {
                    str(row.get("entry_id") or "") for row in scoped_roster
                    if str(row.get("entry_id") or "") in platform_by_id
                }
                if adopted_platform_ids:
                    own_after = await _load_own_llm_config(req.user_id) or {}
                    current_ids = {
                        str(value) for value in (own_after.get("user_catalog_platform_entry_ids") or [])
                        if str(value)
                    }
                    own_after["user_catalog_platform_entry_ids"] = sorted(current_ids | adopted_platform_ids)
                    await _persist_llm_config(req.user_id, own_after)
            meta["llm_config"] = llm_config_in
        if chat_ui_in is not None:
            meta["chat_ui"] = _merge_chat_ui(meta.get("chat_ui"), chat_ui_in)
        if voice_conversation_in is not None:
            if not isinstance(voice_conversation_in, dict):
                raise HTTPException(status_code=400, detail="voice_conversation must be an object.")
            from app.agent.voice_config import normalize_voice_conversation
            current_voice = meta.get("voice_conversation") if isinstance(meta.get("voice_conversation"), dict) else {}
            meta["voice_conversation"] = normalize_voice_conversation(voice_conversation_in, current_voice)
        if claude_code_in is not None:
            # Shallow-merge so saving one field (e.g. just the folder) keeps the rest.
            _cc = meta.get("claude_code")
            _cc = dict(_cc) if isinstance(_cc, dict) else {}
            _cc.update(claude_code_in)
            meta["claude_code"] = _cc
        if codex_code_in is not None:
            _cx = meta.get("codex_code")
            _cx = dict(_cx) if isinstance(_cx, dict) else {}
            _cx.update(codex_code_in)
            meta["codex_code"] = _cx
        if terminal_chat_in is not None:
            # Shallow-merge so saving one field (e.g. just the command) keeps the rest.
            _tc = meta.get("terminal_chat")
            _tc = dict(_tc) if isinstance(_tc, dict) else {}
            _tc.update(terminal_chat_in)
            meta["terminal_chat"] = _tc
        if embed_in is not None:
            # Website embed widget — normalize + shallow-merge so saving one field
            # (e.g. just the accent) keeps the rest. See app/api/embed_config.py.
            from app.api.embed_config import normalize_embed_config
            _cur_embed = meta.get("embed") if isinstance(meta.get("embed"), dict) else {}
            meta["embed"] = normalize_embed_config(embed_in, _cur_embed)
            if bool(meta["embed"].get("enabled")):
                from app.agent.public_policy import validate_publication
                public_access_in = await validate_publication(
                    db, {**(current or {"id": agent_id}), "metadata": meta},
                    public_access_in if isinstance(public_access_in, dict)
                    else (meta.get("public_access") if isinstance(meta.get("public_access"), dict) else {}),
                )
        if icon_in is not None:
            # Store icon in metadata — the agents table has no dedicated icon column.
            # An empty/blank string means "clear the icon" (falls back to default).
            meta["icon"] = icon_in.strip() or ""
        if exec_mode_in is not None:
            meta["default_execution_mode"] = exec_mode_in
        if exec_modes_in is not None:
            meta["execution_modes"] = exec_modes_in
        if exec_mode_in is not None:
            available_ids = {
                item["id"] for item in normalize_execution_modes(meta.get("execution_modes"))
            }
            if exec_mode_in != "wkspc" and exec_mode_in not in available_ids:
                raise HTTPException(
                    status_code=400,
                    detail="default_execution_mode must reference a configured mode.",
                )
        if target_device_in is not None:
            # Instance-id of the default Remote Control device; blank clears it
            # (run locally). Stored raw — the chat pill resolves the label + online
            # state from the live device list.
            meta["default_target_device"] = str(target_device_in).strip()
        if public_link_in is not None:
            if bool(public_link_in):
                from app.agent.public_policy import validate_publication
                public_access_in = await validate_publication(
                    db, {**(current or {"id": agent_id}), "metadata": meta},
                    public_access_in if isinstance(public_access_in, dict)
                    else (meta.get("public_access") if isinstance(meta.get("public_access"), dict) else {}),
                )
            meta["public_link"] = bool(public_link_in)
            meta.pop("is_embeddable", None)  # legacy key — replaced by public_link
        if public_access_in is not None:
            if not isinstance(public_access_in, dict):
                raise HTTPException(status_code=400, detail="public_access must be an object.")
            # Enabling/changing a live public policy must remain funded.
            if bool(meta.get("public_link")) or bool((meta.get("embed") or {}).get("enabled")):
                from app.agent.public_policy import validate_publication
                public_access_in = await validate_publication(
                    db, {**(current or {"id": agent_id}), "metadata": meta}, public_access_in,
                )
            meta["public_access"] = public_access_in
        if resume_tail_in is not None:
            meta["resume_tail_messages"] = int(resume_tail_in)
        # Per-agent resume knobs (Agents → Runtime → Limits). Blank/0 clears the
        # key so the agent inherits the app-global value again; a set value is
        # clamped to the app-global attempt ceiling at resume time by the runner.
        # The closing-message budgets ride the SAME convention: blank/0 clears so
        # the agent inherits the app ceiling (run_max_closing_attempts /
        # run_max_closing_attempts_per_requirement), which closing_message.
        # effective_max_attempts also clamps to. They are requirement-compliance
        # budgets, NOT the resume/error counters — a different trigger/lifecycle.
        # The assertions budget rides the same convention (ceiling
        # run_max_assertions_attempts; assertions.effective_max_attempts clamps).
        for _rkey, _rval in (("resume_max_attempts", resume_max_in),
                             ("resume_backoff_seconds", resume_backoff_in),
                             ("resume_max_attempts_per_cause", resume_per_cause_in),
                             ("closing_max_attempts", closing_max_in),
                             ("closing_max_attempts_per_requirement", closing_per_req_in),
                             ("assertions_max_attempts", assertions_max_in)):
            if _rval is None:
                continue
            try:
                _riv = int(_rval)
            except (TypeError, ValueError):
                _riv = 0
            if _riv > 0:
                meta[_rkey] = _riv
            else:
                meta.pop(_rkey, None)
        if audit_checklist_in is not None:
            # Store the checklist prop as given; blank forms clear it so the
            # agent falls back to the app-level default / plain summary.
            _blank = (
                (isinstance(audit_checklist_in, str) and not audit_checklist_in.strip())
                or (isinstance(audit_checklist_in, (list, dict)) and not audit_checklist_in)
            )
            if _blank:
                meta.pop("audit_checklist", None)
            else:
                meta["audit_checklist"] = audit_checklist_in
        if closer_prompt_in is not None:
            # Compatibility input: keep it canonical instead of recreating the
            # deprecated standalone metadata field.
            from app.agent.manager_config import manager_loop_for_agent
            current_loop = manager_loop_for_agent({**(current or {"id": agent_id}), "metadata": meta})
            current_loop["closer"]["prompt"] = str(closer_prompt_in or "").strip()
            meta["manager"] = current_loop
            meta.pop("closer_prompt", None)
        if manager_loop_in is not None:
            from app.agent.manager_config import merge_manager_loop_update
            try:
                meta["manager"] = merge_manager_loop_update(
                    {**(current or {"id": agent_id}), "metadata": meta},
                    manager_loop_in,
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        if task_progress_in is not None:
            from app.agent.task_progress_config import merge_task_progress_update
            try:
                meta["task_progress"] = merge_task_progress_update(
                    {**(current or {"id": agent_id}), "metadata": meta},
                    task_progress_in,
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        if gates_in is not None:
            # Run-contract gate instances. Validated against the plugin registry
            # on write: an unknown type is rejected outright rather than stored as
            # a gate that silently never fires, and each row is normalised so the
            # stored shape is canonical. A gate row carries only what differs from
            # the type's defaults.
            #
            # For PARAMS, MESSAGE and SCOPE an absent key means "clear it": that is
            # how a client unsets a field. ACTIVATION is deliberately different —
            # an absent `enabled` means "leave the stored activation alone", never
            # "default to OFF" — see GATES-MERGE-ON-WRITE below.
            from app.agent.gate_registry import gate_types as _gate_types

            _rows = gates_in.get("gates") if isinstance(gates_in, dict) else gates_in
            if not isinstance(_rows, list):
                raise HTTPException(status_code=400, detail="gates must be an object or a list.")
            _known = _gate_types()
            _mode_source = exec_modes_in if exec_modes_in is not None else meta.get("execution_modes")
            _mode_ids = {item["id"] for item in normalize_execution_modes(_mode_source)}
            # GATES-MERGE-ON-WRITE: a gates payload is a MERGE keyed by gate type,
            # not a wholesale replacement. Both partial shapes used to change
            # activation the caller never asked to change:
            #   * a row that omitted `enabled` was normalised to OFF, silently
            #     disarming a gate whose params the caller was only editing;
            #   * the incoming list replaced the stored block, so a payload
            #     carrying one gate dropped every other gate's row — and a
            #     dropped type reads as OFF through the registry's fail-closed
            #     default.
            # So a type the payload does not mention keeps the row on disk, and a
            # row that omits `enabled` keeps its stored activation. An EXPLICIT
            # `enabled: false` still disarms. The Modes UI sends the whole catalog
            # with explicit flags, so it is unaffected; this protects scripted and
            # older clients, whose saves were the lossy ones.
            _prior_rows: Dict[str, Dict[str, Any]] = {}
            _prior_block = meta.get("gates")
            _prior_list = _prior_block.get("gates") if isinstance(_prior_block, dict) else _prior_block
            for _prior in _prior_list if isinstance(_prior_list, list) else []:
                if not isinstance(_prior, dict):
                    continue
                _prior_type = str(_prior.get("type") or "").strip()
                if _prior_type in _known and _prior_type not in _prior_rows:
                    _prior_rows[_prior_type] = _prior
            _sent_types = {
                str(_row.get("type") or "").strip()
                for _row in _rows
                if isinstance(_row, dict)
            }
            # Preserved rows are re-normalised by the loop below, so they are
            # appended to the row list rather than written straight through. One
            # whose scope now points at a deleted mode is dropped here instead of
            # failing an unrelated save on a row the registry cannot fire anyway.
            _rows = list(_rows) + [
                _prior
                for _prior_type, _prior in _prior_rows.items()
                if _prior_type not in _sent_types
                and (
                    not isinstance(_prior.get("scope"), list)
                    or set(_prior.get("scope") or []) <= _mode_ids
                )
            ]
            _cleaned: List[Dict[str, Any]] = []
            for _row in _rows:
                if not isinstance(_row, dict):
                    raise HTTPException(status_code=400, detail="each gate must be an object.")
                _type = str(_row.get("type") or "").strip()
                if _type not in _known:
                    raise HTTPException(
                        status_code=400,
                        detail=f"unknown gate type: {_type or '(missing)'}",
                    )
                # Activation: absent `enabled` means "leave the stored activation
                # alone", never "default to OFF" — see GATES-MERGE-ON-WRITE above.
                # A client that sends only the fields it is changing (a param-only
                # save) must not disarm the gate by omission; an explicit
                # `enabled: false` still disarms.
                if "enabled" in _row:
                    _activated = _row.get("enabled") is True
                else:
                    _prior = _prior_rows.get(_type)
                    _activated = (
                        _prior.get("enabled") is True
                        if _prior is not None
                        else _known[_type].get("default_enabled") is True
                    )
                # Closing Message is now an always-available coordinator.  A
                # legacy explicit master-off request becomes its canonical form:
                # the coordinator stays enabled while all three inner controls
                # are explicitly off.  This preserves older agents' behavior and
                # leaves only the independent controls as behavior switches.
                if _type == "closing_message" and not _activated:
                    _activated = True
                    _params = dict(_row.get("params")) if isinstance(_row.get("params"), dict) else {}
                    _params.update({
                        "require_flag": False,
                        "require_notification": False,
                        "output_closer_enabled": False,
                    })
                    _row = {**_row, "params": _params}
                _entry: Dict[str, Any] = {"type": _type, "enabled": _activated}
                _scope = _row.get("scope")
                if _known[_type].get("mode_scoped") is False:
                    _scope = None
                if isinstance(_scope, list) and _scope:
                    _normalized_scope = [normalize_mode_id(value, fallback="") for value in _scope]
                    if not all(_normalized_scope) or len(set(_normalized_scope)) != len(_normalized_scope):
                        raise HTTPException(status_code=400, detail="gate scope must contain unique configured mode IDs.")
                    if not set(_normalized_scope).issubset(_mode_ids):
                        raise HTTPException(status_code=400, detail="gate scope must reference configured modes only.")
                    _entry["scope"] = _normalized_scope
                _params = _row.get("params")
                if isinstance(_params, dict) and _params:
                    # App-level params (a descriptor param marked `level: "app"` —
                    # the closing_message flag taxonomy) are configured once for the
                    # whole app, so they must never be persisted on an agent row.
                    # The Modes UI strips them client-side;
                    # ui/main-panel/agents/configs/content.js is the companion copy
                    # of this rule. This write-path backstop keeps ONE app-wide
                    # vocabulary authoritative even for a legacy row or an older
                    # client that still sends one.
                    _app_level = {
                        _key
                        for _key, _spec in (_known[_type].get("params") or {}).items()
                        if isinstance(_spec, dict)
                        and str(_spec.get("level") or "") == "app"
                    }
                    _kept = {k: v for k, v in _params.items() if k not in _app_level}
                    if _kept:
                        _entry["params"] = _kept
                _message = _row.get("message")
                if isinstance(_message, str) and _message.strip():
                    _entry["message"] = _message.strip()
                _cleaned.append(_entry)
            meta["gates"] = {"schema_version": 1, "gates": _cleaned}
        updates["metadata"] = meta

    update_kwargs = {
        "agent_id": agent_id, "user_id": req.user_id, "updates": updates,
    }
    if shared_default_edit:
        update_kwargs["allow_install_admin"] = True
    updated = await db.update_agent_fields(**update_kwargs)
    if updated is None:
        raise HTTPException(status_code=404, detail="Agent not found or not owned by this user.")

    # The runtime resolves an agent through its per-agent authority store when
    # the storage layout is active. Keep that mirror in lockstep with an
    # authenticated Agents-page update before any live roster notification is
    # emitted; otherwise the footer/runtime can read an older role map.
    if llm_config_in is not None:
        try:
            from app.db.agent_store import get_agent_store
            await get_agent_store(agent_id).sync_from_global()
        except Exception:
            logger.warning("Could not sync model roster authority for %s", agent_id, exc_info=True)

    if removed_subscription_entry_ids:
        from app import openai_subscription
        for entry_id in removed_subscription_entry_ids:
            await openai_subscription.sign_out(
                scope="agent", agent_id=agent_id, entry_id=entry_id,
            )

    if slots_in is not None:
        # Reconcile admin-base slot set.
        await db.replace_slots(
            agent_id=agent_id,
            slots=[s if isinstance(s, dict) else s.dict() for s in slots_in],
            reset_overrides_for=reset_for or [],
            updated_by=f"admin:{req.user_id}",
        )

    if llm_config_in is not None:
        # A roster/role edit must immediately converge in every open chat for
        # this agent. The client re-fetches slots; running loops resolve config
        # again before their next provider invocation.
        from app.events.delivery import notify_user
        await notify_user(req.user_id, {
            "type": "agent_model_roster_changed",
            "agent_id": agent_id,
        })

    if any(k in updates for k in ("trigger_type", "trigger_key")):
        from app.agent import trigger_index
        trigger_index.build()
    # Name/icon/engine may have changed — drop the display cache entry so the
    # next /agents/display read serves the fresh fields.
    _invalidate_agent_display_cache(agent_id)
    return {"agent": _safe_agent(updated)}


@router.post("/agents/{agent_id}/save-as-template")
async def save_agent_as_template(agent_id: str, req: SaveAsTemplateRequest):
    """
    Snapshot a custom agent's current config + admin-base prompt slots into a
    new reusable template (rows in agent_templates + agent_prompt_templates).
    Admin-only.
    """
    db = get_db()
    await _require_admin(db, req.user_id)

    import re
    slug = (req.template_id or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,63}", slug):
        raise HTTPException(
            status_code=400,
            detail="template_id must be 2-64 chars: lowercase letters, digits, '_' or '-'.",
        )
    if not req.name or not req.name.strip():
        raise HTTPException(status_code=400, detail="Template name is required.")

    try:
        tpl = await db.save_agent_as_template(
            agent_id=agent_id,
            template_id=slug,
            name=req.name.strip(),
            description=(req.description or "").strip(),
            icon=(req.icon or "").strip(),
            discoverable=bool(req.discoverable),
            access_level=req.access_level or "all",
            updated_by=f"admin:{req.user_id}",
        )
    except ValueError as e:
        msg = str(e)
        status = 409 if "already exists" in msg else 400
        if "not found" in msg:
            status = 404
        raise HTTPException(status_code=status, detail=msg)

    return {"template": _safe_agent(tpl)}


class PushToTemplateRequest(BaseModel):
    user_id: str


@router.post("/agents/{agent_id}/push-to-template")
async def push_agent_to_template(agent_id: str, req: PushToTemplateRequest, request: Request):
    """
    Push a custom agent's current config + prompt slots back into its
    source template (determined by agents.template_id).

    Admin-only. Upserts the agent row fields and admin-base prompt slots
    into the matching agent_templates + agent_prompt_templates rows,
    stamped as source='admin' so future JSON re-seeds won't overwrite them.
    Returns the updated template row.
    """
    db = get_db()
    await _require_admin(db, req.user_id)

    try:
        tpl = await db.upsert_agent_to_template(
            agent_id=agent_id,
            updated_by=f"admin:{req.user_id}",
        )
    except ValueError as e:
        msg = str(e)
        status = 404 if "not found" in msg else 400
        raise HTTPException(status_code=status, detail=msg)

    return {"template": _safe_agent(tpl)}


@router.get("/agents/{agent_id}/slots")
async def get_agent_slots(request: Request, agent_id: str, user_id: str = Query(...)):
    user_id = await assert_caller_is(request, user_id)
    """Return admin-base slot definitions for an agent or template.

    Custom agents return the rows scoped to their agent_id. System templates
    return the rows scoped to template_id (agent_prompts.agent_id = template_id).
    """
    db = get_db()
    agent = await db.get_agent_by_id(agent_id)
    is_template = False
    if not agent:
        # Try as a template id (system templates have no agents row).
        templates = await db.list_agent_templates(include_admin=True)
        tpl = next((t for t in templates if t.get("id") == agent_id), None)
        if not tpl:
            raise HTTPException(status_code=404, detail="Agent not found.")
        is_template = True
    slots = await db.list_slots(agent_id, user_id=user_id)
    # The `__skills__` slot is managed via the dedicated skills endpoints, not
    # the generic slot editor — hide it here so it doesn't show as raw JSON.
    slots = [s for s in slots if s.get("slot_name") != "__skills__"]
    if is_template:
        # Templates have no per-agent admin role — only global admins may edit.
        is_admin = await db.is_user_admin(user_id)
    else:
        is_admin = await _is_agent_admin(db, agent_id, user_id)
    return {"slots": slots, "user_role": "admin" if is_admin else "member"}


@router.get("/agents/{agent_id}/skills")
async def get_agent_skills_ep(request: Request, agent_id: str, user_id: str = Query(...)):
    """Return the agent's on-demand skills (from its `__skills__` prompt slot)
    plus whether the caller may edit them."""
    user_id = await assert_caller_is(request, user_id)
    db = get_db()
    skills = await db.get_agent_skills(agent_id)
    try:
        is_admin = await _is_agent_admin(db, agent_id, user_id)
    except Exception:
        is_admin = await db.is_user_admin(user_id)
    return {"skills": skills, "user_role": "admin" if is_admin else "member"}


@router.get("/agents/{agent_id}/skills-summary")
async def get_agent_skills_summary_ep(request: Request, agent_id: str, user_id: str = Query(...)):
    """Return the resolved Skills catalog counts used by the Abilities summary.

    This includes authored skills and skills contributed by enabled abilities.
    Discoverable skills remain in the catalog count even before a chat session has
    loaded them, so the UI can accurately distinguish visible from discoverable.
    """
    user_id = await assert_caller_is(request, user_id)
    db = get_db()
    agent = await db.get_agent_by_id(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found.")
    if not await _is_agent_admin(db, agent_id, user_id):
        raise HTTPException(status_code=403, detail="Only agent admins can view skills summary.")

    from app.agent.ability_skills import collect_ability_skills
    from app.tools.tool_modes import resolve_skill_mode

    authored = await db.get_agent_skills(agent_id) or []
    bundled = await collect_ability_skills(agent_id, user_id=None)
    skill_modes = await db.get_agent_skill_modes(agent_id)
    skills = list(authored) + list(bundled)
    visible = 0
    discoverable = 0
    for skill in skills:
        mode = skill.get("mode")
        if skill.get("ability_id"):
            mode = resolve_skill_mode(skill["ability_id"], skill_modes, mode)
        if mode in ("visible", "always_on"):
            visible += 1
        else:
            discoverable += 1
    return {"total": len(skills), "visible": visible, "discoverable": discoverable}


class UpdateSkillsRequest(BaseModel):
    user_id: str
    skills: List[Dict[str, Any]]


@router.put("/agents/{agent_id}/skills")
async def put_agent_skills_ep(agent_id: str, req: UpdateSkillsRequest, request: Request):
    """Replace the agent's full skills list (admin-only). Persists to the
    `__skills__` prompt slot in agent_prompts."""
    db = get_db()
    req.user_id = await assert_caller_is(request, req.user_id)
    if not await _is_agent_admin(db, agent_id, req.user_id):
        raise HTTPException(status_code=403, detail="Only agent admins can edit skills.")
    saved = await db.set_agent_skills(agent_id, req.skills, updated_by=f"admin:{req.user_id}")
    return {"skills": saved}


@router.get("/agents/{agent_id}/my-prompts")
async def get_my_prompts(request: Request, agent_id: str, user_id: str = Query(...)):
    """Return the caller's prompt slots with per-user override map.

    Shape matches what the frontend prompt-slots panel expects:
    ``{slots, overrides, user_role}``.
    """
    user_id = await assert_caller_is(request, user_id)
    db = get_db()
    agent = await db.get_agent_by_id(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found.")
    slots = await db.list_slots(agent_id, user_id=user_id)
    slots = [s for s in slots if s.get("slot_name") != "__skills__"]
    overrides: Dict[str, str] = {}
    for s in slots:
        oc = s.get("override_content")
        if oc is not None:
            overrides[s["slot_name"]] = oc
        del s["override_content"]
    is_admin = await _is_agent_admin(db, agent_id, user_id)
    return {"slots": slots, "overrides": overrides, "user_role": "admin" if is_admin else "member"}


@router.put("/agents/{agent_id}/my-prompts")
async def update_my_prompts(request: Request, agent_id: str, req: UpdateMyPromptsRequest):
    """Write the caller's per-user override rows for one or more unlocked slots.

    Locked slots and unknown slot_names are rejected per slot (the rest still
    write). Returns the resolved slot list for this caller.

    If the ``automation`` slot is among the writes, also re-parse it into
    structured ``agent_automations`` rows and return them under
    ``automation_tasks``.
    """
    req.user_id = await assert_caller_is(request, req.user_id)
    db = get_db()
    agent = await db.get_agent_by_id(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found.")
    rejected: List[Dict[str, str]] = []
    written: List[str] = []
    automation_content: Optional[str] = None
    for item in req.slots:
        res = await db.upsert_override(
            agent_id=agent_id,
            slot_name=item.slot_name,
            user_id=req.user_id,
            content=item.content or "",
            updated_by=f"user:{req.user_id}",
        )
        if res is None:
            rejected.append({"slot_name": item.slot_name, "reason": "locked_or_unknown"})
        else:
            written.append(item.slot_name)
            if item.slot_name == "automation":
                automation_content = item.content or ""

    slots = await db.list_slots(agent_id, user_id=req.user_id)

    automation_payload: Optional[Dict[str, Any]] = None
    if automation_content is not None:
        try:
            from app.automation.sync import sync_automations
            automation_payload = await sync_automations(
                db=db,
                agent_id=agent_id,
                owner_user_id=req.user_id,
                slot_content=automation_content,
                agent_context={
                    "name": agent.get("name"),
                    "description": agent.get("description"),
                },
            )
        except Exception as e:
            logger.warning("Automation sync failed: %s", e)
            automation_payload = {"tasks": [], "removed": 0, "error": str(e)}

    response = {"written": written, "rejected": rejected, "slots": slots}
    if automation_payload is not None:
        response["automation_tasks"] = automation_payload.get("tasks", [])
        response["automation_error"] = automation_payload.get("error")
        # Surface the event-trigger half of the parse result too, with
        # the same health badge the UI's event-subscriptions panel uses.
        evt_subs = automation_payload.get("event_subscriptions") or []
        response["automation_event_subscriptions"] = [
            _decorate_subscription_health(s) for s in evt_subs
        ]
        response["automation_removed_event_subscriptions"] = (
            automation_payload.get("removed_event_subscriptions") or []
        )
    return response


@router.get("/agents/{agent_id}/automations")
async def list_agent_automations(request: Request, agent_id: str, user_id: str = Query(...)):
    """List parsed scheduled task rows for this user/agent."""
    user_id = await assert_caller_is(request, user_id)
    db = get_db()
    agent = await db.get_agent_by_id(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found.")
    rows = await db.list_automations(agent_id=agent_id, owner_user_id=user_id)
    return {"tasks": rows}


@router.post("/agents/{agent_id}/automations/parse")
async def reparse_agent_automations(request: Request, agent_id: str, user_id: str = Query(...)):
    """Re-run the LLM parser against the caller's resolved ``automation`` slot."""
    user_id = await assert_caller_is(request, user_id)
    db = get_db()
    agent = await db.get_agent_by_id(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found.")

    slots = await db.list_slots(agent_id, user_id=user_id)
    content = ""
    for s in slots:
        if s.get("slot_name") == "automation":
            content = s.get("override_content") or s.get("content") or ""
            break

    from app.automation.sync import sync_automations
    try:
        result = await sync_automations(
            db=db, agent_id=agent_id, owner_user_id=user_id,
            slot_content=content,
            agent_context={
                "name": agent.get("name"),
                "description": agent.get("description"),
            },
        )
    except Exception as e:
        logger.warning("Re-parse failed: %s", e)
        result = {
            "tasks": [], "removed": 0,
            "event_subscriptions": [], "removed_event_subscriptions": [],
            "error": str(e),
        }
    evt_subs = result.get("event_subscriptions") or []
    return {
        "tasks": result.get("tasks", []),
        "removed": result.get("removed", 0),
        "event_subscriptions": [_decorate_subscription_health(s) for s in evt_subs],
        "removed_event_subscriptions": result.get("removed_event_subscriptions") or [],
        "error": result.get("error"),
    }


class UpdateAutomationBody(BaseModel):
    user_id: str
    enabled: Optional[bool] = None
    silent: Optional[bool] = None
    channel: Optional[str] = None
    channel_recipient: Optional[str] = None
    target_device: Optional[str] = None      # device instance-id to run on; '' clears it (run locally)
    target_offline: Optional[str] = None      # 'wait' | 'skip' when the target is offline at fire time


@router.patch("/agents/{agent_id}/automations/{automation_id}")
async def patch_agent_automation(request: Request, agent_id: str, automation_id: str, body: UpdateAutomationBody):
    """Toggle enabled / channel / silent for a single task without re-parsing."""
    body.user_id = await assert_caller_is(request, body.user_id)
    db = get_db()
    row = await db.get_automation(automation_id)
    if not row or row.get("agent_id") != agent_id:
        raise HTTPException(status_code=404, detail="Automation not found.")
    if row.get("owner_user_id") != body.user_id and not await db.is_user_admin(body.user_id):
        raise HTTPException(status_code=403, detail="Not the owner of this automation.")
    fields: Dict[str, Any] = {}
    if body.enabled is not None:
        fields["enabled"] = body.enabled
    if body.silent is not None:
        fields["silent"] = body.silent
    if body.channel is not None:
        fields["channel"] = body.channel or None
    if body.channel_recipient is not None:
        fields["channel_recipient"] = body.channel_recipient or None
    if body.target_device is not None:
        # '' / whitespace clears the target (run on the firing device locally).
        fields["target_device"] = body.target_device.strip() or None
    if body.target_offline is not None:
        fields["target_offline"] = "skip" if body.target_offline == "skip" else "wait"
    updated = await db.update_automation(automation_id, **fields)
    return {"task": updated}


@router.post("/agents/{agent_id}/automations/{automation_id}/run-now")
async def run_agent_automation_now(request: Request, agent_id: str, automation_id: str, user_id: str = Query(...)):
    """Fire an automation immediately. Does not affect ``next_run_at``."""
    user_id = await assert_caller_is(request, user_id)
    db = get_db()
    row = await db.get_automation(automation_id)
    if not row or row.get("agent_id") != agent_id:
        raise HTTPException(status_code=404, detail="Automation not found.")
    if row.get("owner_user_id") != user_id and not await db.is_user_admin(user_id):
        raise HTTPException(status_code=403, detail="Not the owner of this automation.")
    from app.scheduler import get_scheduler
    try:
        result = await get_scheduler().run_now(automation_id)
    except Exception as e:
        logger.warning("run_now failed: %s", e)
        result = {"ok": False, "error": str(e)}
    fresh = await db.get_automation(automation_id)
    return {"result": result, "task": fresh}


@router.post("/automations/fire/{automation_id}")
async def fire_remote_automation(automation_id: str, token: str = Query(...)):
    """Webhook entry point used by remote scheduler providers (Google Cloud
    Scheduler, cron-job.org, generic webhooks). Validates the per-automation
    ``fire_token`` and runs the executor.
    """
    db = get_db()
    row = await db.get_automation_by_fire_token(automation_id, token)
    if not row:
        raise HTTPException(status_code=404, detail="Unknown automation or bad token.")
    if not row.get("enabled"):
        return {"ok": False, "skipped": "disabled"}
    from app.scheduler.executor import execute_automation
    result = await execute_automation(row)
    return {"ok": bool(result.get("ok")), "session_id": result.get("session_id"), "error": result.get("error")}


@router.delete("/agents/{agent_id}/automations/{automation_id}")
async def delete_agent_automation(request: Request, agent_id: str, automation_id: str, user_id: str = Query(...)):
    """Delete one automation row (the file is left untouched)."""
    user_id = await assert_caller_is(request, user_id)
    db = get_db()
    row = await db.get_automation(automation_id)
    if not row or row.get("agent_id") != agent_id:
        raise HTTPException(status_code=404, detail="Automation not found.")
    if row.get("owner_user_id") != user_id and not await db.is_user_admin(user_id):
        raise HTTPException(status_code=403, detail="Not the owner of this automation.")
    # Clean up any dedicated clone agent this automation created
    if row.get("runner_agent_id"):
        try:
            conn = db._get_conn()
            if conn:
                # Capture the clone's parent before the row is deleted so the
                # nested subagent home can be purged afterwards.
                clone_parent = None
                try:
                    _mrow = conn.execute(
                        "SELECT metadata FROM agents WHERE id = ? AND status = 'clone'",
                        (row["runner_agent_id"],),
                    ).fetchone()
                    if _mrow:
                        import json as _json
                        _mmeta = _json.loads(_mrow["metadata"] or "{}")
                        if isinstance(_mmeta, dict):
                            clone_parent = _mmeta.get("clone_of")
                except Exception:
                    pass
                conn.execute("DELETE FROM agents WHERE id = ? AND status = 'clone'", (row["runner_agent_id"],))
                conn.commit()
                conn.close()
                if clone_parent:
                    try:
                        from app.agent_workspace import purge_subagent_home
                        purge_subagent_home(clone_parent, row["runner_agent_id"])
                    except Exception:
                        pass
        except Exception as e:
            logger.warning("Failed to clean up clone agent %s: %s", row["runner_agent_id"], e)

    deleted = await db.delete_automation(automation_id)
    return {"deleted": bool(deleted)}


# ============================================================
# Per-agent event subscriptions (push + poll triggers)
#
# The Automation slot can produce both cron rows (agent_automations) and
# event-trigger rows (agent_event_subscriptions). These endpoints expose
# the event-trigger rows to the Automation tab UI so the user can see
# provider health, toggle, edit, test-fire, re-register, or delete them
# directly instead of editing the English file.
#
# Source-of-truth model (option 3): the English file is the user's
# **intent**; the DB rows are the **active** state. UI edits write the DB
# only, leaving the file alone. Re-parsing the file always merges new
# triggers in (existing rows with matching source_hash are no-ops).
# ============================================================


@router.get("/agents/{agent_id}/event-subscriptions")
async def list_agent_event_subscriptions(request: Request, agent_id: str, user_id: str = Query(...)):
    user_id = await assert_caller_is(request, user_id)
    """List the caller's event-trigger rows for this agent.

    Each row carries the same observability fields the renewer/poller write:
    last_status, last_error, last_event_at, fire_count, external_expiration_at.
    """
    db = get_db()
    agent = await db.get_agent_by_id(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found.")
    rows = await db.list_event_subscriptions(agent_id=agent_id, owner_user_id=user_id)
    # Decorate each row with a "health" badge derived from its state.
    decorated = [_decorate_subscription_health(r) for r in rows]
    return {"subscriptions": decorated}


def _decorate_subscription_health(row: Dict[str, Any]) -> Dict[str, Any]:
    """Compute a health string the UI can render directly."""
    from datetime import datetime, timezone, timedelta
    out = dict(row)
    if not row.get("enabled"):
        out["health"] = "disabled"
        return out
    last_status = row.get("last_status") or ""
    if last_status == "error":
        out["health"] = "error"
        return out
    exp = row.get("external_expiration_at")
    if exp:
        try:
            t = datetime.fromisoformat(exp.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            if t <= now:
                out["health"] = "expired"
            elif (t - now) < timedelta(hours=24):
                out["health"] = "expiring_soon"
            else:
                out["health"] = "ok"
        except Exception:
            out["health"] = "ok"
    else:
        # No TTL (poll source, or comms bridge). OK as long as no error.
        out["health"] = "ok"
    return out


class UpdateEventSubscriptionBody(BaseModel):
    user_id: str
    enabled: Optional[bool] = None
    silent: Optional[bool] = None
    channel: Optional[str] = None
    channel_recipient: Optional[str] = None
    prompt: Optional[str] = None
    task_label: Optional[str] = None
    filter: Optional[Dict[str, Any]] = None


@router.patch("/agents/{agent_id}/event-subscriptions/{sub_id}")
async def patch_agent_event_subscription(
    request: Request, agent_id: str, sub_id: str, body: UpdateEventSubscriptionBody,
):
    body.user_id = await assert_caller_is(request, body.user_id)
    """Edit a single event subscription without re-parsing the English file.

    Per option 3: the row is the active state; the Automation slot is the
    user's original intent. A subsequent re-parse will *re-enable* anything
    whose English line still exists, so disabling via this endpoint is a
    soft-state change rather than a permanent erase. To permanently remove
    a trigger, either delete the row (DELETE) AND edit the English file, or
    just edit the English file and re-save.
    """
    db = get_db()
    row = await db.get_event_subscription(sub_id)
    if not row or row.get("agent_id") != agent_id:
        raise HTTPException(status_code=404, detail="Event subscription not found.")
    if row.get("owner_user_id") != body.user_id and not await db.is_user_admin(body.user_id):
        raise HTTPException(status_code=403, detail="Not the owner of this subscription.")

    fields: Dict[str, Any] = {}
    if body.enabled is not None:
        fields["enabled"] = body.enabled
    if body.silent is not None:
        fields["silent"] = body.silent
    if body.channel is not None:
        fields["channel"] = body.channel or None
    if body.channel_recipient is not None:
        fields["channel_recipient"] = body.channel_recipient or None
    if body.prompt is not None:
        fields["prompt"] = body.prompt
    if body.task_label is not None:
        fields["task_label"] = body.task_label
    if body.filter is not None:
        import json as _json
        fields["filter_json"] = _json.dumps(body.filter or {}, sort_keys=True)

    updated = await db.update_event_subscription(sub_id, **fields)

    # If the filter changed and this is a push source, the provider-side
    # watch may no longer reflect the new filter (e.g. Gmail label filter).
    # Re-register so the watch matches.
    needs_rereg = (body.filter is not None) and bool(row.get("external_subscription_id"))
    if needs_rereg:
        try:
            await _rereg_subscription(updated)
            updated = await db.get_event_subscription(sub_id)
        except Exception as e:
            logger.warning("Auto re-register after PATCH failed for %s: %s", sub_id, e)
    return {"subscription": _decorate_subscription_health(updated)}


@router.post("/agents/{agent_id}/event-subscriptions/{sub_id}/re-register")
async def reregister_agent_event_subscription(
    request: Request, agent_id: str, sub_id: str, user_id: str = Query(...),
):
    user_id = await assert_caller_is(request, user_id)
    """Re-run the source plugin's ``register_subscription`` for this row.

    Used after Pub/Sub topic recreation, OAuth reconnect, or to clear a
    ``last_status=error`` state. Updates external_subscription_id,
    external_expiration_at, and external_metadata to the new values.
    """
    db = get_db()
    row = await db.get_event_subscription(sub_id)
    if not row or row.get("agent_id") != agent_id:
        raise HTTPException(status_code=404, detail="Event subscription not found.")
    if row.get("owner_user_id") != user_id and not await db.is_user_admin(user_id):
        raise HTTPException(status_code=403, detail="Not the owner of this subscription.")
    try:
        await _rereg_subscription(row)
    except Exception as e:
        logger.warning("re-register failed for %s: %s", sub_id, e)
        await db.update_event_subscription(sub_id, last_status="error", last_error=str(e)[:400])
        raise HTTPException(status_code=400, detail=f"register failed: {e}")
    fresh = await db.get_event_subscription(sub_id)
    return {"subscription": _decorate_subscription_health(fresh)}


@router.post("/agents/{agent_id}/event-subscriptions/{sub_id}/test-fire")
async def test_fire_event_subscription(
    request: Request, agent_id: str, sub_id: str, user_id: str = Query(...),
):
    user_id = await assert_caller_is(request, user_id)
    """Synthesize a fake event and run it through the router for this sub.

    Bypasses the source's filter (we manufacture a NormalizedEvent that
    already matches) so you can verify the agent's behavior end-to-end
    without waiting for a real event from the provider. Logs a delivery
    row marked status='test' for audit clarity.
    """
    db = get_db()
    row = await db.get_event_subscription(sub_id)
    if not row or row.get("agent_id") != agent_id:
        raise HTTPException(status_code=404, detail="Event subscription not found.")
    if row.get("owner_user_id") != user_id and not await db.is_user_admin(user_id):
        raise HTTPException(status_code=403, detail="Not the owner of this subscription.")

    from app.events.types import NormalizedEvent
    from app.events.executor import execute_event_subscription
    from datetime import datetime, timezone
    import uuid as _uuid

    fake = NormalizedEvent(
        source=row["source"],
        event_type=row["event_type"],
        owner_user_id=row["owner_user_id"],
        external_id=f"test-{_uuid.uuid4().hex[:12]}",
        occurred_at=datetime.now(timezone.utc).isoformat(),
        payload={
            "_test_fire": True,
            "label": row.get("task_label"),
            "filter": row.get("filter") or {},
            "note": "Synthesized by /test-fire — not from a real provider event.",
        },
        raw_ref={},
    )

    delivery_id = await db.insert_event_delivery(
        subscription_id=sub_id,
        source=row["source"],
        event_type=row["event_type"],
        event_external_id=fake.external_id,
        owner_user_id=row["owner_user_id"],
        agent_id=agent_id,
        status="pending",
        payload_excerpt=fake.payload_excerpt(),
    )
    try:
        result = await execute_event_subscription(row, fake)
        await db.update_event_delivery(
            delivery_id,
            status="test" if result.get("ok") else "error",
            error=result.get("error"),
            session_id=result.get("session_id"),
        )
        await db.update_event_subscription(
            sub_id,
            last_status="ok" if result.get("ok") else "error",
            last_error=result.get("error"),
            last_event_at=fake.occurred_at,
        )
        return {"result": result, "delivery_id": delivery_id}
    except Exception as e:
        logger.exception("test-fire failed: %s", e)
        await db.update_event_delivery(delivery_id, status="error", error=str(e)[:400])
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/agents/{agent_id}/event-subscriptions/{sub_id}")
async def delete_agent_event_subscription(
    request: Request, agent_id: str, sub_id: str, user_id: str = Query(...),
):
    user_id = await assert_caller_is(request, user_id)
    """Delete the row AND unregister the provider-side watch.

    Per option 3, the English file is left alone — a future re-parse with
    the same line in the file will re-create the row + register a new
    provider watch.
    """
    db = get_db()
    row = await db.get_event_subscription(sub_id)
    if not row or row.get("agent_id") != agent_id:
        raise HTTPException(status_code=404, detail="Event subscription not found.")
    if row.get("owner_user_id") != user_id and not await db.is_user_admin(user_id):
        raise HTTPException(status_code=403, detail="Not the owner of this subscription.")

    # Best-effort unregister at provider; deletion proceeds either way.
    try:
        from app.events import get_manager
        src = get_manager().get(row["source"])
        if src is not None:
            await src.unregister_subscription(row)
    except Exception as e:
        logger.warning("Provider unregister failed for %s (ignored): %s", sub_id, e)

    # Clean up clone agent if this subscription had one
    if row.get("runner_agent_id"):
        try:
            conn = db._get_conn()
            try:
                conn.execute("DELETE FROM agents WHERE id = ? AND status = 'clone'", (row["runner_agent_id"],))
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            logger.warning("Failed to clean up clone agent %s: %s", row.get("runner_agent_id"), e)

    deleted = await db.delete_event_subscription(sub_id)
    return {"deleted": bool(deleted)}


async def _rereg_subscription(row: Dict[str, Any]) -> None:
    """Re-run ``register_subscription`` for a row and persist the new IDs.

    Shared by the PATCH (filter changed) and POST re-register endpoints.
    Tries to unregister the old watch first so providers don't keep firing
    against a stale subscription id.
    """
    from app.events import get_manager
    db = get_db()
    src = get_manager().get(row["source"])
    if src is None:
        raise RuntimeError(f"Source {row['source']!r} not registered")
    if row.get("external_subscription_id"):
        try:
            await src.unregister_subscription(row)
        except Exception as e:
            logger.debug("unregister-before-rereg failed: %s", e)
    reg = await src.register_subscription(
        owner_user_id=row["owner_user_id"],
        event_type=row["event_type"],
        filter_dict=row.get("filter") or {},
    )
    await db.update_event_subscription(
        row["id"],
        external_subscription_id=reg.external_subscription_id,
        external_resource_id=reg.external_resource_id,
        external_expiration_at=reg.external_expiration_at,
        external_metadata=reg.external_metadata or {},
        poll_cursor=reg.poll_cursor,
        last_status="ok",
        last_error=None,
    )


@router.delete("/agents/{agent_id}/my-prompts/{slot_name}")
async def delete_my_prompt(request: Request, agent_id: str, slot_name: str, user_id: str = Query(...)):
    """Remove the caller's override row for a single slot."""
    user_id = await assert_caller_is(request, user_id)
    db = get_db()
    deleted = await db.delete_override(agent_id, slot_name, user_id)
    return {"deleted": bool(deleted)}


@router.delete("/agents/{agent_id}")
async def delete_agent(request: Request, agent_id: str, user_id: str = Query(...),
                       permanent: bool = Query(False)):
    """
    Recycling-bin delete for a custom agent. Cannot touch system agents.

    Default (``permanent=false``): SOFT delete — the agent moves to the bin
    (status -> 'trashed') and disappears from the Agents page, but keeps its
    prompts, sessions and transcripts so it can be restored.

    ``permanent=true``: HARD delete — used by the bin's own trash button. Erases
    the agent and everything that belongs to it (prompts, sessions, transcripts,
    connections, abilities). This is irreversible.
    """
    db = get_db()
    user_id = await assert_caller_is(request, user_id)
    if not await _is_agent_admin(db, agent_id, user_id):
        raise HTTPException(status_code=403, detail="Only agent administrators can delete this agent.")
    if permanent:
        deleted = await db.delete_custom_agent(agent_id=agent_id, user_id=user_id)
    else:
        deleted = await db.trash_custom_agent(agent_id=agent_id, user_id=user_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Agent not found, not owned by this user, or is a system agent.")
    # A permanent delete erases the agent row from the authority, so the hybrid
    # puller (which only ever pulls rows whose watermark advanced) can never carry
    # the erasure to another device's local mirror — it would show a ghost agent in
    # the dropdown until cold start. Drop a tombstone so peers prune it (and its
    # sessions/transcript) on the next sync tick. (Soft trash needs no tombstone:
    # the status flip rides the normal watermark pull.)
    if permanent:
        try:
            from app.db.sync.tombstones import record_tombstones
            record_tombstones(db, [("agents", agent_id)], user_id)
        except Exception:  # noqa: BLE001
            pass
    # Live-sync every open tab/device: a permanent delete drops the card from the
    # bin view; a soft delete moves it out of the main grid into the bin.
    # Either way, stop serving the deleted/trashed agent's display fields.
    _invalidate_agent_display_cache(agent_id)
    from app.events.delivery import notify_user
    await notify_user(user_id, {
        "type": "agent_deleted" if permanent else "agent_trashed",
        "user_id": user_id, "agent_id": agent_id, "permanent": permanent,
    })
    return {"deleted": True, "agent_id": agent_id, "permanent": permanent}


@router.post("/agents/{agent_id}/restore")
async def restore_agent(request: Request, agent_id: str, user_id: str = Query(...)):
    """Restore a trashed agent from the recycling bin back to the Agents page."""
    db = get_db()
    user_id = await assert_caller_is(request, user_id)
    if not await _is_agent_admin(db, agent_id, user_id):
        raise HTTPException(status_code=403, detail="Only agent administrators can restore this agent.")
    trashed = next(
        (row for row in await db.list_agents_for_user(user_id, include_admin=False, view="bin")
         if str(row.get("id") or "") == agent_id),
        None,
    )
    if not trashed:
        raise HTTPException(status_code=404, detail="Agent not found in the bin, or not owned by this user.")
    async with _agent_create_lock(user_id):
        try:
            await enforce_agent_materialization(
                db, user_id, template_id=str(trashed.get("template_id") or "default"),
                restoring=True,
            )
        except ResourceEntitlementError as exc:
            raise _resource_http_error(exc) from exc
        restored = await db.restore_custom_agent(agent_id=agent_id, user_id=user_id)
    if not restored:
        raise HTTPException(status_code=404, detail="Agent not found in the bin, or not owned by this user.")
    # Live-sync: the agent leaves the bin and reappears on the main grid.
    from app.events.delivery import notify_user
    await notify_user(user_id, {
        "type": "agent_restored", "user_id": user_id, "agent_id": agent_id,
    })
    return {"restored": True, "agent_id": agent_id}


@router.post("/agents/test")
async def test_agent(req: TestAgentRequest, request: Request):
    """
    Run a sample message through an agent configuration and return the response.
    Used by the Agent Management panel test sandbox — does NOT create a persistent session.
    The agent's prompts are used as-is from its current saved state.
    """
    import uuid as _uuid_mod
    db = get_db()
    req.user_id = await assert_caller_is(request, req.user_id)

    # Resolve the agent config
    agents = await db.list_agents_for_user(req.user_id, include_admin=False)
    target = next((a for a in agents if a.get("id") == req.agent_id), None)
    if not target:
        # Try looking up by template id directly
        templates = await db.list_agent_templates(include_admin=True)
        target = next((t for t in templates if t.get("id") == req.agent_id), None)
    if not target:
        raise HTTPException(status_code=404, detail="Agent not found.")

    # Build a system prompt by resolving the caller's slots (admin base + overrides).
    from app.agent.prompts import build_system_prompt

    test_session_id = req.session_id or f"test-{str(_uuid_mod.uuid4())[:8]}"

    agent_id_for_resolve = target.get("id", req.agent_id)
    resolved_slots = await db.resolve_prompts(agent_id_for_resolve, user_id=req.user_id)
    context_docs = [
        {"id": s["slot_name"], "content": s["content"]}
        for s in resolved_slots if (s.get("content") or "").strip()
    ]

    system_prompt = await build_system_prompt(
        context_docs,
        brain_context=None,
        user_id=req.user_id,
        session_id=test_session_id,
    )

    # Run a single-turn agent loop (non-streaming)
    from app.agent.loop import run_agent_loop_buffered
    result = await run_agent_loop_buffered(
        user_id=req.user_id,
        session_id=test_session_id,
        user_message=req.message,
        system_prompt=system_prompt,
        agent_id=target.get("id", req.agent_id),
        history=[],
        max_turns=3,
        db=db,
        agent_template_id=target.get("template_id") or target.get("id"),
    )

    # Fetch the interactions stored during this test run so the UI can render
    # a pipeline loop without a separate round-trip (avoids session-ownership checks).
    interactions: List[Dict[str, Any]] = []
    try:
        from app.db import get_db as _get_db
        _conn = _get_db()._get_conn()
        try:
            _rows = _conn.execute(
                "SELECT id, session_id, role, content, tool_name, metadata, created_at "
                "FROM interactions WHERE session_id = ? ORDER BY created_at ASC",
                (test_session_id,),
            ).fetchall()
            interactions = [dict(r) for r in _rows]
        finally:
            _conn.close()
    except Exception:
        pass  # frontend falls back to plain reply display

    return {"reply": result, "session_id": test_session_id, "interactions": interactions}


# ── Connection catalog — defines all known connection types ───────────────────

_CONNECTION_CATALOG = [
    # ── Channels ──
    {"connection_type": "telegram",  "section": "channel",     "display_name": "Telegram",        "status": "available"},
    {"connection_type": "twilio",    "section": "channel",     "display_name": "Twilio (SMS/Call)","status": "coming_soon"},
    {"connection_type": "email",     "section": "channel",     "display_name": "Email",            "status": "coming_soon"},
    {"connection_type": "whatsapp",  "section": "channel",     "display_name": "WhatsApp",         "status": "coming_soon"},
    {"connection_type": "discord",   "section": "channel",     "display_name": "Discord",          "status": "coming_soon"},
    {"connection_type": "slack",     "section": "channel",     "display_name": "Slack",            "status": "coming_soon"},
    # ── Integrations · Productivity ──
    {"connection_type": "google",    "section": "integration", "display_name": "Google",           "status": "available"},
    {"connection_type": "microsoft", "section": "integration", "display_name": "Microsoft 365",    "status": "available"},
    {"connection_type": "yahoo",     "section": "integration", "display_name": "Yahoo",            "status": "available"},
    {"connection_type": "dropbox",   "section": "integration", "display_name": "Dropbox",          "status": "available"},
    # ── Integrations · Developer ──
    {"connection_type": "github",    "section": "integration", "display_name": "GitHub",           "status": "coming_soon"},
    {"connection_type": "gitlab",    "section": "integration", "display_name": "GitLab",           "status": "coming_soon"},
    {"connection_type": "jira",      "section": "integration", "display_name": "Jira / Linear",    "status": "coming_soon"},
    # ── Integrations · CRM & Email ──
    {"connection_type": "hubspot",    "section": "integration", "display_name": "HubSpot",         "status": "coming_soon"},
    {"connection_type": "salesforce", "section": "integration", "display_name": "Salesforce",      "status": "coming_soon"},
    {"connection_type": "mailchimp",  "section": "integration", "display_name": "Mailchimp",       "status": "coming_soon"},
    # ── Integrations · Financial data ──
    # Commerce payment providers (Stripe, PayPal, Square, BTCPay) are discovered
    # from plugins/abilities/Commerce below, including their credential forms.
    {"connection_type": "bank",      "section": "integration", "display_name": "Bank Accounts",    "status": "coming_soon"},
    {"connection_type": "search",    "section": "integration", "display_name": "Search Engine",    "status": "coming_soon"},
    # ── Social Media ──
    {"connection_type": "facebook",  "section": "social",      "display_name": "Facebook",         "status": "available"},
    {"connection_type": "instagram", "section": "social",      "display_name": "Instagram",        "status": "available"},
    {"connection_type": "twitter",   "section": "social",      "display_name": "X (Twitter)",      "status": "available"},
    {"connection_type": "linkedin",  "section": "social",      "display_name": "LinkedIn",         "status": "available"},
    {"connection_type": "tiktok",    "section": "social",      "display_name": "TikTok",           "status": "available"},
    {"connection_type": "pinterest", "section": "social",      "display_name": "Pinterest",        "status": "available"},
    {"connection_type": "reddit",    "section": "social",      "display_name": "Reddit",           "status": "available"},
    {"connection_type": "snapchat",  "section": "social",      "display_name": "Snapchat",         "status": "available"},
    {"connection_type": "twitch",    "section": "social",      "display_name": "Twitch",           "status": "available"},
    # ── Marketplaces ──
    {"connection_type": "ebay",      "section": "marketplace", "display_name": "eBay",             "status": "available"},
    {"connection_type": "etsy",      "section": "marketplace", "display_name": "Etsy",             "status": "available"},
    {"connection_type": "shopify",   "section": "marketplace", "display_name": "Shopify",          "status": "available"},
    {"connection_type": "amazon",    "section": "marketplace", "display_name": "Amazon Seller",    "status": "available"},

    # ── Agent Tools (host-side privileged capabilities) ──
    # DROP-IN: the host-ability rows are injected below from plugins/abilities/
    # (see app.abilities). Do NOT hardcode abilities here — drop a file in that
    # folder instead. (Web Scraper / Browser Cookies are now ordinary drop-in
    # abilities under plugins/abilities/Web/, supplied via _inject_ability_rows.)
]


def _inject_ability_rows() -> None:
    """Append the discovered host-ability rows to the connection catalog.

    Each row carries its UI metadata (description/icon/color/group/simple) so the
    two ability panels render generically. Fail-open: a scan error just leaves
    the catalog without ability rows rather than breaking the connections API.

    ⚠ DROP-IN POLICY — abilities are discovered from plugins/abilities/ here; do
    NOT hardcode a new ability anywhere in this file. Drop a file in
    plugins/abilities/ and it appears in the catalog + both panels automatically.
    See CLAUDE.md "Core vs. plugins".
    """
    try:
        from app.abilities import connection_rows
        existing = {c["connection_type"] for c in _CONNECTION_CATALOG}
        for row in connection_rows():
            if row["connection_type"] not in existing:
                _CONNECTION_CATALOG.append(row)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("Could not inject ability rows from plugins/abilities: %s", e)


_inject_ability_rows()


# Short-TTL cache for the ability catalog. The catalog is GLOBAL (no per-user /
# per-agent data) but each fetch does a filesystem re-scan (reload() walks
# plugins/abilities/) synchronously on the event loop. At cold page-load the
# browser calls this ~2× while ~30 other requests contend for the loop, so an
# uncached scan serialized and measured ~12s (47ms warm). Caching the scan result
# for a few seconds collapses the boot's repeat calls to a single scan and keeps
# the drop-in story honest (edit a descriptor → reload the page → see it within
# the TTL). Tunable via WEBAGENT_CATALOG_CACHE_SECONDS; set 0 to always re-scan.
_CATALOG_CACHE: Dict[str, Any] = {"at": 0.0, "data": None}


@router.get("/abilities/catalog")
async def get_abilities_catalog(request: Request):
    """Render-time metadata for the two ability panels (admin Agent Settings +
    per-agent Abilities tab). Pure static catalogue (no per-agent data), built
    from the drop-in files in plugins/abilities/ — so both panels render
    generically with no hardcoded per-ability constants.
    """
    import os as _os
    import time as _time
    try:
        ttl = float(_os.environ.get("WEBAGENT_CATALOG_CACHE_SECONDS") or 8.0)
    except ValueError:
        ttl = 8.0
    now = _time.monotonic()
    cached = _CATALOG_CACHE.get("data")
    cache_fresh = ttl > 0 and cached is not None and (now - _CATALOG_CACHE["at"]) < ttl
    if cache_fresh:
        data = cached
    else:
        try:
            from app.abilities import ui_catalog, reload
            # Re-scan plugins/abilities/ so a freshly-dropped or freshly-edited
            # <id>.json descriptor appears with no server restart.
            reload()
            data = ui_catalog()
            # Tool handlers are the canonical description source for both the
            # runtime tool index and the ability tables. Keep their one-line
            # docstrings beside each declared tool in the cached drop-in catalog,
            # rather than maintaining a UI-only tool-description registry.
            from app.abilities import tool_descriptions
            from app.admin.settings import _load_app_settings
            descriptions = tool_descriptions()
            raw_overrides = _load_app_settings().get("tool_description_overrides") or {}
            overrides = raw_overrides if isinstance(raw_overrides, dict) else {}
            for ability_id, item in (data.get("abilities") or {}).items():
                ability_overrides = overrides.get(ability_id) or {}
                if not isinstance(ability_overrides, dict):
                    ability_overrides = {}
                item["tool_descriptions"] = {
                    name: str(ability_overrides.get(name) or descriptions.get(name) or "").strip()
                    for name in (item.get("tools") or [])
                    if str(ability_overrides.get(name) or descriptions.get(name) or "").strip()
                }
            _CATALOG_CACHE["data"] = data
            _CATALOG_CACHE["at"] = now
        except Exception as e:
            logger.warning("Could not build abilities catalog: %s", e)
            data = cached or {"groups": [], "abilities": {}, "credential_members": []}

    # Catalog metadata is caller-specific presentation. Keep the cached plugin
    # scan immutable, then annotate every ability with the same entitlement
    # decision used by runtime assembly so the UI can render denied rows locked.
    import copy as _copy
    result = _copy.deepcopy(data)
    try:
        uid = request_user_id(request)
        capabilities = await resolve_capabilities(uid, db=get_db())
        allowed = set(capabilities.get("ability_groups") or [])
        for ability_id, item in (result.get("abilities") or {}).items():
            group = str(item.get("entitlement_group") or "platform_admin")
            item["entitlement_allowed"] = group in allowed
            item["entitlement_reason"] = "allowed" if group in allowed else "tier_denied"
    except Exception:
        for item in (result.get("abilities") or {}).values():
            item["entitlement_allowed"] = False
            item["entitlement_reason"] = "policy_unavailable"
    return result


class UpdateToolDescriptionRequest(BaseModel):
    """Administrator-supplied display text for one catalogued tool."""

    description: str = Field(default="", max_length=8000)


@router.put("/abilities/{ability_id}/tools/{tool_name}/description")
async def update_ability_tool_description(
    ability_id: str,
    tool_name: str,
    body: UpdateToolDescriptionRequest,
    request: Request,
):
    """Persist an App-admin display-description override for one declared tool.

    An empty description intentionally removes the override, returning the UI to
    the tool handler's canonical docstring rather than storing a blank label.
    """
    db = get_db()
    user_id = request_user_id(request)
    await _require_admin(db, user_id)

    from app.abilities import all_raw, tool_descriptions
    ability = all_raw().get(ability_id)
    if not ability or tool_name not in (ability.get("tools") or []):
        raise HTTPException(status_code=404, detail="Unknown ability tool.")

    description = str(body.description or "").strip()
    from app.admin.settings import _load_app_settings, _save_app_settings
    settings = _load_app_settings()
    raw_overrides = settings.get("tool_description_overrides") or {}
    overrides = dict(raw_overrides) if isinstance(raw_overrides, dict) else {}
    ability_overrides = overrides.get(ability_id) or {}
    ability_overrides = dict(ability_overrides) if isinstance(ability_overrides, dict) else {}

    if description:
        ability_overrides[tool_name] = description
        overrides[ability_id] = ability_overrides
    else:
        ability_overrides.pop(tool_name, None)
        if ability_overrides:
            overrides[ability_id] = ability_overrides
        else:
            overrides.pop(ability_id, None)

    if overrides:
        settings["tool_description_overrides"] = overrides
    else:
        settings.pop("tool_description_overrides", None)
    _save_app_settings(settings)
    _CATALOG_CACHE["data"] = None
    _CATALOG_CACHE["at"] = 0.0

    canonical = tool_descriptions().get(tool_name, "")
    return {"ok": True, "description": description or canonical, "overridden": bool(description)}


@router.get("/pages/catalog")
async def get_pages_catalog(request: Request):
    """Render-time metadata for the app shell's main header tabs and the Admin
    Tools sidebar views. Built from the drop-in ``page.json`` descriptors under
    ui/ and ui/admin-tools/, merged with the admin's order/label/icon/hidden
    overrides in data/config/main-panel-pages.json + admin-panel-pages.json — so
    the shell renders generically with no hardcoded per-page constants.
    """
    try:
        from app import ui_pages
        from app.admin import page_config
        # Both discovery and page-config already maintain explicit reload hooks
        # for admin writes and deployment changes.  Re-reading every descriptor
        # and config file on every browser boot turns this hot endpoint into a
        # filesystem gate for the entire app.
        catalog = ui_pages.ui_catalog()
        uid = request_user_id(request)
        db = get_db()
        capabilities = await resolve_capabilities(uid, db=db)
        filtered = {
            "main": [], "admin": [], "splash": list(catalog.get("splash") or []),
            "_meta": {
                "tier_id": (capabilities.get("tier") or {}).get("id"),
                "tier_revision": (capabilities.get("tier") or {}).get("revision"),
                "roster_revision": (capabilities.get("models") or {}).get("revision"),
                "evaluation_revision": (capabilities.get("evaluation") or {}).get("revision"),
                "evaluated_at": capabilities.get("evaluated_at"),
                "subject_class": (capabilities.get("subject") or {}).get("class"),
            },
        }
        is_admin = bool((capabilities.get("subject") or {}).get("is_admin"))
        anonymous = not uid or str(uid).startswith("anon_")
        for kind in ("main", "admin"):
            for page in catalog.get(kind) or []:
                page_id = str(page.get("id") or "")
                visibility = str(page.get("visibility") or "auth")
                installation_allowed = (
                    visibility == "all"
                    or is_admin
                    or (visibility == "auth" and not anonymous)
                )
                required_capability = str(page.get("required_backend_capability") or "")
                if required_capability == "role:platform_admin" and not is_admin:
                    installation_allowed = False
                entitlement_page = "admin-tools" if kind == "admin" else page_id
                if installation_allowed and bool((capabilities.get("pages") or {}).get(entitlement_page)):
                    filtered[kind].append(page)
        return filtered
    except Exception as e:
        logger.warning("Could not build pages catalog: %s", e)
        return {"main": [], "admin": []}


@router.get("/abilities/{ability_id}/config-schema")
async def get_ability_config_schema(
    ability_id: str,
    scope: str = Query("agent"),
):
    """Return the per-ability config schema (the companion .json file beside
    the .py plugin) so the frontend can render per-ability settings rows.
    Returns 404 when the ability has no config schema."""
    try:
        from app.abilities import ability_config_schema
        schema = ability_config_schema(ability_id)
        # Dual-surface abilities may expose a compact per-agent schema while
        # keeping infrastructure/safety controls in App Functions.  Unscoped
        # fields remain visible everywhere for backward compatibility.
        requested_scope = "app" if str(scope).strip().lower() == "app" else "agent"
        if isinstance(schema, dict) and isinstance(schema.get("settings"), list):
            schema = dict(schema)
            schema["settings"] = [
                field for field in schema["settings"]
                if not isinstance(field, dict)
                or str(field.get("scope") or "both").lower() in ("both", requested_scope)
            ]
        # Agent controls inherit the stored app-level value until explicitly
        # overridden, so expose that value as their rendered default. For fields
        # with a `ceiling` rule, also attach the hard clamp used by the agent UI.
        if isinstance(schema, dict) and isinstance(schema.get("settings"), list):
            try:
                from app.admin import ability_config as _abcfg
                admin_vals = _abcfg.get_ability_config(ability_id)
            except Exception:
                admin_vals = {}
            # COPY before annotating — `ability_config_schema` hands back the
            # cached descriptor's settings list by reference, so mutating a field
            # in place would pollute the shared catalog across requests.
            schema = dict(schema)
            new_settings = []
            for field in schema["settings"]:
                if isinstance(field, dict):
                    field = dict(field)
                    key = field.get("key")
                    descriptor_default = field.get("default")
                    if requested_scope == "agent" and key in admin_vals:
                        field["default"] = admin_vals[key]
                    if field.get("ceiling"):
                        ceiling_key = field.get("ceiling_key") or key
                        field["ceiling_value"] = admin_vals.get(
                            ceiling_key, descriptor_default)
                new_settings.append(field)
            schema["settings"] = new_settings
        # Return null rather than 404 so browsers don't log this as a console
        # error. Frontends check for null instead of relying on status code.
        return schema or None
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("Could not load ability config schema for %s: %s", ability_id, e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/abilities/{ability_id}/config")
async def get_ability_config_values(
    ability_id: str,
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Return the stored APP-LEVEL (admin scope) non-secret config values for an
    ability, as ``{ability_settings: {key: value}}`` — so the admin Agent Tools
    panel can pre-fill the fields with what was previously saved. Empty map when
    nothing has been saved yet. Backed by app/admin/ability_config.py."""
    from app.admin import ability_config as _abcfg
    # Auto-seed: make sure the repo-local config file exists (created/seeded from
    # the vault on first access) before we read, so a missing file self-heals
    # rather than returning stale emptiness.
    try:
        await _abcfg.ensure_bootstrapped(get_db())
    except Exception as e:
        logger.debug("ability_config ensure_bootstrapped (get) skipped: %s", e)
    return {"ability_settings": _abcfg.get_ability_config(ability_id)}


class AbilityConfigRequest(BaseModel):
    """Save payload for an ability's non-secret APP-LEVEL config knobs (admin
    scope). ``ability_settings`` is a flat ``{key: value}`` map matching the
    ability's config-schema fields."""
    ability_settings: Dict[str, Any] = {}


@router.put("/abilities/{ability_id}/config")
async def save_ability_config_values(
    ability_id: str,
    req: AbilityConfigRequest,
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Persist an ability's APP-LEVEL (admin scope) non-secret config values into
    the repo-local ``data/config/agent-abilities.json`` (NOT the vault — these are
    non-secret knobs). The file is auto-created/seeded if missing, and the change
    is written immediately. Backed by app/admin/ability_config.py
    ``set_ability_config``."""
    from app.admin import ability_config as _abcfg
    from app.admin.integrations import resolve_user_id
    db = get_db()
    user_id = resolve_user_id(authorization or "", token or "")
    await _require_admin(db, user_id)
    # Auto-seed before writing: ensures the config file exists with its other
    # sections (abilities/tools/order) intact, so saving config never clobbers a
    # not-yet-loaded file. Self-heals if the file was deleted.
    try:
        await _abcfg.ensure_bootstrapped(db)
    except Exception as e:
        logger.debug("ability_config ensure_bootstrapped (put) skipped: %s", e)
    settings = req.ability_settings if isinstance(req.ability_settings, dict) else {}
    _abcfg.set_ability_config(ability_id, settings)
    return {"status": "ok", "ability_settings": _abcfg.get_ability_config(ability_id)}


class AbilityCredentialsRequest(BaseModel):
    """Save payload for an ability's declarative credentials. ``values`` carries
    one entry per declared field; a blank secret field leaves it unchanged."""
    values: Dict[str, Any] = {}
    agent_id: Optional[str] = None


@router.get("/abilities/{ability_id}/credentials")
async def get_ability_credentials(
    ability_id: str,
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Return the ability's credential fields + non-secret values + which secrets
    are set (NEVER the secret values). Returns null when the ability declares no
    ``credentials`` block. The caller is resolved from the Bearer token. See
    app/abilities/credentials.py."""
    from app.abilities import credentials as _creds
    from app.admin.integrations import resolve_user_id
    spec = _creds.ability_credentials_spec(ability_id)
    if not spec:
        return None
    user_id = resolve_user_id(authorization or "", token or "")
    db = get_db()
    try:
        is_admin = bool(user_id) and await db.is_user_admin(user_id)
    except Exception:
        is_admin = False
    view = await _creds.public_view(ability_id, user_id=user_id)
    if view is not None:
        # admin-scope secrets are editable only by an admin; user/agent scope is
        # the caller's own.
        view["can_edit"] = bool(is_admin) or spec.get("scope") != "admin"
    return view


@router.post("/abilities/{ability_id}/credentials")
async def save_ability_credentials(
    ability_id: str,
    req: AbilityCredentialsRequest,
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Persist an ability's credentials into the encrypted vault (generic)."""
    from app.abilities import credentials as _creds
    from app.admin.integrations import resolve_user_id, ANONYMOUS_KEY
    spec = _creds.ability_credentials_spec(ability_id)
    if not spec:
        raise HTTPException(status_code=404, detail="This ability has no credentials to configure.")
    user_id = resolve_user_id(authorization or "", token or "")
    db = get_db()
    scope = spec.get("scope", "user")
    if scope == "admin":
        await _require_admin(db, user_id)
    elif not user_id or user_id == ANONYMOUS_KEY:
        raise HTTPException(status_code=401, detail="Sign in to save these credentials.")
    ok = await _creds.save_credentials(
        ability_id, req.values or {}, user_id=user_id, agent_id=req.agent_id or "",
    )
    return {"status": "ok" if ok else "error",
            "configured": await _creds.is_configured(ability_id, user_id=user_id, agent_id=req.agent_id or "")}


@router.delete("/abilities/{ability_id}/credentials")
async def delete_ability_credentials(
    ability_id: str,
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
    agent_id: Optional[str] = Query(None),
):
    """Clear an ability's stored credentials."""
    from app.abilities import credentials as _creds
    from app.admin.integrations import resolve_user_id
    spec = _creds.ability_credentials_spec(ability_id)
    if not spec:
        raise HTTPException(status_code=404, detail="This ability has no credentials to configure.")
    user_id = resolve_user_id(authorization or "", token or "")
    db = get_db()
    if spec.get("scope") == "admin":
        await _require_admin(db, user_id)
    ok = await _creds.delete_credentials(ability_id, user_id=user_id, agent_id=agent_id or "")
    return {"status": "ok" if ok else "error"}


@router.get("/agents/{agent_id}/connections")
async def get_agent_connections(request: Request, agent_id: str, user_id: str = Query(...)):
    """
    Return connections for an agent, filtered to those the admin has configured.
    Unconfigured providers are hidden so they can't be toggled on from the UI.
    Bot tokens in config are masked to last 4 chars.
    For Google: merges user's auth_elements status (email, name, picture).
    """
    import json as _json
    from app.admin.integrations import get_admin_configured_providers
    db = get_db()
    user_id = await assert_caller_is(request, user_id)

    # These six reads are independent of one another, so issue them together
    # rather than one-await-after-another. (On the current remote/hybrid backend
    # the DB layer still services them serially, so the bigger win is that
    # get_admin_configured_providers — historically ~5s of per-service round-trips
    # and the dominant cost of this handler — now batches its admin auth lookups
    # into a single query; see app/admin/integrations.py.) Error semantics are
    # preserved EXACTLY: the two originally-unguarded reads (connections +
    # admin-configured providers) still hard-fail the request; the four originally
    # try/except'd reads still fall back to their defaults on error.
    #   [0] connections (surfaced as `saved`)          — was unguarded
    #   [1] ability visibility modes                    — was → {}
    #   [2] bundled-skill visibility modes              — was → {}
    #   [3] per-agent DEFAULT visibility (discovery)    — was → None
    #   [4] per-agent ability ACCESS level              — was → {}
    #   [5] admin-configured providers                  — was unguarded
    _r = await asyncio.gather(
        db.get_agent_connections(agent_id),
        db.get_agent_ability_modes(agent_id),
        db.get_agent_skill_modes(agent_id),
        db.get_agent_discovery_default(agent_id),
        db.get_agent_ability_access(agent_id),
        get_admin_configured_providers(user_id),
        return_exceptions=True,
    )
    if isinstance(_r[0], Exception):
        raise _r[0]
    if isinstance(_r[5], Exception):
        raise _r[5]
    rows = _r[0]
    _ability_modes = {} if isinstance(_r[1], Exception) else _r[1]
    _skill_modes = {} if isinstance(_r[2], Exception) else _r[2]
    _discovery_default = None if isinstance(_r[3], Exception) else _r[3]
    _ability_access = {} if isinstance(_r[4], Exception) else _r[4]
    configured_providers = _r[5]
    saved = {r["connection_type"]: r for r in rows}

    # Fetch auth_elements for all OAuth-backed providers.
    # Maps connection_type → service key stored in auth_elements.
    # facebook and instagram both alias to the "meta" OAuth app.
    _OAUTH_PROVIDERS = {
        "google":    "google",
        "microsoft": "microsoft",
        "yahoo":     "yahoo",
        "dropbox":   "dropbox",
        "facebook":  "meta",
        "instagram": "meta",
        "twitter":   "twitter",
        "linkedin":  "linkedin",
        "tiktok":    "tiktok",
        "pinterest": "pinterest",
        "reddit":    "reddit",
        "snapchat":  "snapchat",
        "twitch":    "twitch",
        "ebay":      "ebay",
        "etsy":      "etsy",
        "shopify":   "shopify",
        "amazon":    "amazon",
    }
    # Tokens are scoped per (user, agent); look up only this agent's row so that
    # signing into Google on Agent A does NOT make Agent B appear connected.
    from app.integrations.oauth_helper import oauth_label
    _label = oauth_label(agent_id)
    # Pull ALL of this user's auth elements in ONE query, then index by service —
    # replacing a loop of ~13 per-service remote round-trips (the dominant cost of
    # this handler on a remote DB). We keep only rows for THIS agent's label, so a
    # sign-in on Agent A still doesn't make Agent B look connected.
    _OAUTH_SERVICE_KEYS = set(_OAUTH_PROVIDERS.values())
    try:
        _all_elems = await db.auth_element_list(user_id)
    except Exception:
        _all_elems = []
    _service_cache: dict[str, dict] = {}
    for _el in _all_elems:
        if _el.get("label") != _label:
            continue
        sk = _el.get("service")
        if sk not in _OAUTH_SERVICE_KEYS:
            continue
        cfg = _el.get("config", {})
        if isinstance(cfg, str):
            try:
                cfg = _json.loads(cfg)
            except Exception:
                cfg = {}
        _service_cache[sk] = cfg or {}
    provider_auth: dict[str, dict] = {}
    for ct, service_key in _OAUTH_PROVIDERS.items():
        cfg = _service_cache.get(service_key) or {}
        if cfg:
            provider_auth[ct] = cfg

    result = []
    for entry in _CONNECTION_CATALOG:
        ct = entry["connection_type"]
        if ct not in configured_providers:
            continue
        row = saved.get(ct)
        config = {}
        if row:
            try:
                config = _json.loads(row.get("config") or "{}")
            except Exception:
                config = {}
            # Mask sensitive token fields
            if "bot_token" in config and config["bot_token"]:
                tok = config["bot_token"]
                config["bot_token"] = "•" * max(0, len(tok) - 4) + tok[-4:]

        # A locked-on safety ability (e.g. Context Control) is always enabled —
        # its toggle is fixed ON and cannot be turned off — regardless of whether
        # a per-agent row exists or what it says.
        _locked_on = bool(entry.get("locked_on"))
        item = {
            **entry,
            "enabled": True if _locked_on else (bool(row["enabled"]) if row else False),
            "config": config,
        }

        # Ability + bundled-skill visibility (per-agent discovery).
        if entry.get("section") == "ability":
            from app.tools.tool_modes import resolve_ability_mode, resolve_skill_mode, resolve_ability_access
            item["ability_mode"] = resolve_ability_mode(ct, _ability_modes, _discovery_default)
            item["available_to"] = resolve_ability_access(ct, _ability_access)
            try:
                from app.abilities import ability_general_skills
                _skills = ability_general_skills(ct)
            except Exception:
                _skills = []
            item["general_skills"] = [
                {
                    "skill_id": s["skill_id"], "skill_key": s["skill_key"],
                    "title": s.get("title") or s["skill_id"],
                    "when_to_use": s.get("when_to_use") or "",
                    "minimum_access": s.get("minimum_access") or "everyone",
                    "mode": resolve_skill_mode(s["skill_key"], _skill_modes, s.get("default_mode")),
                }
                for s in _skills
            ]
            item["has_skill"] = bool(item["general_skills"])
            # Root aliases preserve old consumers until they adopt general_skills.
            _core = next((s for s in item["general_skills"] if s["skill_id"] == "core"), None)
            item["skill_summary"] = (_core or {}).get("when_to_use", "")
            item["skill_mode"] = (_core or {}).get("mode", "discoverable")

        # Merge OAuth account info for providers that use auth_elements
        if ct in _OAUTH_PROVIDERS:  # dict membership check
            auth = provider_auth.get(ct)
            if auth:
                item[f"{ct}_connected"] = True
                item[f"{ct}_email"] = auth.get("email", "")
                item[f"{ct}_name"] = auth.get("name", "")
                item[f"{ct}_picture"] = auth.get("picture", "")
                item[f"{ct}_connected_at"] = auth.get("connected_at", "")
            else:
                item[f"{ct}_connected"] = False

        result.append(item)
    is_admin = await _is_agent_admin(db, agent_id, user_id)
    return {"connections": result, "user_role": "admin" if is_admin else "member"}


_SOFT_SLUG = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_SOFT_STATUSES = {"draft", "ready", "disabled", "error"}
_SOFT_WORKFLOW_ACTIONS = {
    "tool.call", "credential.ensure", "approval.require", "return",
}


def _validate_soft_ability(body: SoftAbilityRequest) -> List[str]:
    errors: List[str] = []
    if not _SOFT_SLUG.fullmatch(body.slug or ""):
        errors.append("Slug must be 2-64 lowercase letters, numbers, or underscores, starting with a letter.")
    if not (body.display_name or "").strip():
        errors.append("Display name is required.")
    if len(body.skill_body or "") > 50000:
        errors.append("Skill body is limited to 50,000 characters.")
    if body.status not in _SOFT_STATUSES:
        errors.append("Invalid status.")
    if len(body.allowed_tools) > 100 or any(not isinstance(t, str) or not t.strip() for t in body.allowed_tools):
        errors.append("Allowed tools must be a list of at most 100 tool names.")
    if len(set(body.allowed_tools)) != len(body.allowed_tools):
        errors.append("Allowed tools cannot contain duplicates.")
    if len(body.credential_schema) > 20:
        errors.append("Credential schema is limited to 20 entries.")
    for i, cred in enumerate(body.credential_schema):
        if not isinstance(cred, dict) or not cred.get("id"):
            errors.append(f"Credential entry {i + 1} requires an id.")
            continue
        if any(key in cred for key in ("value", "secret_value", "password_value", "token_value")):
            errors.append(f"Credential entry {i + 1} contains a secret value; store only field metadata here.")
        if cred.get("type") not in (None, "vault", "oauth"):
            errors.append(f"Credential entry {i + 1} has an unsupported type.")
    steps = (body.workflow or {}).get("steps", [])
    if not isinstance(steps, list) or len(steps) > 100:
        errors.append("Workflow steps must be a list containing at most 100 entries.")
    else:
        for i, step in enumerate(steps):
            if not isinstance(step, dict) or step.get("action") not in _SOFT_WORKFLOW_ACTIONS:
                errors.append(f"Workflow step {i + 1} has an unsupported action.")
            elif step.get("action") == "tool.call" and step.get("tool") not in body.allowed_tools:
                errors.append(f"Workflow step {i + 1} calls a tool not present in allowed_tools.")
    if body.status == "ready" and not (body.skill_body or "").strip():
        errors.append("A ready ability must include a skill body.")
    return errors


@router.get("/agents/{agent_id}/soft-abilities")
async def list_soft_abilities(request: Request, agent_id: str, user_id: str = Query(...)):
    db = get_db()
    user_id = await assert_caller_is(request, user_id)
    if not await _is_agent_admin(db, agent_id, user_id):
        raise HTTPException(status_code=403, detail="Only agent admins can view custom abilities.")
    return {"abilities": await db.get_agent_soft_abilities(agent_id)}


@router.post("/agents/{agent_id}/soft-abilities")
async def create_soft_ability(agent_id: str, body: SoftAbilityRequest, request: Request):
    db = get_db()
    body.user_id = await assert_caller_is(request, body.user_id)
    if not await _is_agent_admin(db, agent_id, body.user_id):
        raise HTTPException(status_code=403, detail="Only agent admins can create custom abilities.")
    errors = _validate_soft_ability(body)
    if errors:
        raise HTTPException(status_code=422, detail={"errors": errors})
    try:
        row = await db.upsert_agent_soft_ability({
            **body.model_dump(exclude={"user_id"}), "agent_id": agent_id,
            "created_by": body.user_id,
        })
    except Exception as exc:
        if "UNIQUE" in str(exc).upper():
            raise HTTPException(status_code=409, detail="That custom ability slug is already in use.")
        raise
    return {"ability": row}


@router.put("/agents/{agent_id}/soft-abilities/{ability_id}")
async def update_soft_ability(agent_id: str, ability_id: str, body: SoftAbilityRequest, request: Request):
    db = get_db()
    body.user_id = await assert_caller_is(request, body.user_id)
    if not await _is_agent_admin(db, agent_id, body.user_id):
        raise HTTPException(status_code=403, detail="Only agent admins can edit custom abilities.")
    existing = next((r for r in await db.get_agent_soft_abilities(agent_id) if r["id"] == ability_id), None)
    if not existing:
        raise HTTPException(status_code=404, detail="Custom ability not found.")
    errors = _validate_soft_ability(body)
    if errors:
        raise HTTPException(status_code=422, detail={"errors": errors})
    row = await db.upsert_agent_soft_ability({
        **body.model_dump(exclude={"user_id"}), "id": ability_id,
        "agent_id": agent_id, "created_by": existing.get("created_by") or body.user_id,
    })
    return {"ability": row}


@router.delete("/agents/{agent_id}/soft-abilities/{ability_id}")
async def delete_soft_ability(agent_id: str, ability_id: str, request: Request, user_id: str = Query(...)):
    db = get_db()
    user_id = await assert_caller_is(request, user_id)
    if not await _is_agent_admin(db, agent_id, user_id):
        raise HTTPException(status_code=403, detail="Only agent admins can delete custom abilities.")
    if not await db.delete_agent_soft_ability(agent_id, ability_id):
        raise HTTPException(status_code=404, detail="Custom ability not found.")
    return {"status": "ok"}


@router.put("/agents/{agent_id}/connections/{connection_type}")
async def upsert_agent_connection(
    agent_id: str,
    connection_type: str,
    req: UpsertConnectionRequest,
    request: Request,
):
    """
    Create or update a connection on an agent.
    Accepts enabled flag and arbitrary config dict.
    If bot_token in config starts with bullets (masked), preserve existing token.
    """
    import json as _json
    db = get_db()

    req.user_id = await assert_caller_is(request, req.user_id)
    if not await _is_agent_admin(db, agent_id, req.user_id):
        raise HTTPException(status_code=403, detail="Only agent admins can modify connection settings.")

    # Resolve catalog entry for section
    catalog_entry = next(
        (c for c in _CONNECTION_CATALOG if c["connection_type"] == connection_type), None
    )
    if catalog_entry is None:
        raise HTTPException(status_code=400, detail=f"Unknown connection type: {connection_type}")
    if catalog_entry["status"] == "coming_soon":
        raise HTTPException(status_code=400, detail=f"{catalog_entry['display_name']} is not yet available.")

    # A locked-on safety ability cannot be disabled — coerce any disable attempt
    # back to enabled so the row stays on no matter what the client sends.
    if catalog_entry.get("locked_on") and not req.enabled:
        req.enabled = True

    # Refuse to enable a provider the admin hasn't configured. (Disable is
    # always allowed so the UI can clean up stale rows if creds were removed.)
    if req.enabled:
        from app.admin.integrations import get_admin_configured_providers
        if connection_type not in await get_admin_configured_providers(req.user_id):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{catalog_entry['display_name']} has not been configured in "
                    f"App Config → Integrations. Ask an admin to set it up first."
                ),
            )

    new_config = dict(req.config or {})

    # If bot_token is masked (starts with bullet), keep existing token
    incoming_token = new_config.get("bot_token", "")
    if incoming_token and "•" in incoming_token:
        existing_rows = await db.get_agent_connections(agent_id)
        existing = next((r for r in existing_rows if r["connection_type"] == connection_type), None)
        if existing:
            try:
                old_cfg = _json.loads(existing.get("config") or "{}")
                new_config["bot_token"] = old_cfg.get("bot_token", "")
            except Exception:
                pass

    async with connection_resource_lock(req.user_id):
        try:
            await enforce_connection_change(
                db, req.user_id, agent_id, connection_type, enabling=bool(req.enabled),
            )
        except ResourceEntitlementError as exc:
            raise _resource_http_error(exc) from exc
        row = await db.upsert_agent_connection(
            agent_id=agent_id,
            connection_type=connection_type,
            section=catalog_entry["section"],
            enabled=req.enabled,
            config=new_config,
        )

    # Newly-enabled ability → default its tools to "discoverable" (hybrid rule).
    # Only sets tools that have no explicit per-tool mode yet, so prior choices
    # survive. No-op for abilities that provide no built-in tools.
    if req.enabled:
        try:
            from app.tools.tool_modes import tools_for_ability
            _seed = tools_for_ability(connection_type)
            if _seed:
                await db.seed_agent_tool_modes(agent_id, _seed, "discoverable")
        except Exception as _se:
            logger.debug("tool-mode seed (connection %s) failed: %s", connection_type, _se)

    # Signal manager to reload Telegram connections if needed
    if connection_type == "telegram":
        try:
            from app.communications.manager import get_plugin_manager
            pm = get_plugin_manager()
            if hasattr(pm, "reload_agent_connections"):
                import asyncio
                asyncio.create_task(pm.reload_agent_connections())
        except Exception:
            pass

    # Return masked config
    resp_config = dict(new_config)
    if "bot_token" in resp_config and resp_config["bot_token"]:
        tok = resp_config["bot_token"]
        resp_config["bot_token"] = "•" * max(0, len(tok) - 4) + tok[-4:]

    return {
        "connection": {
            **catalog_entry,
            "enabled": req.enabled,
            "config": resp_config,
        }
    }


class SetToolModeRequest(BaseModel):
    user_id: str
    mode: str  # "always" | "discoverable" | (anything else clears the override)


class SetAbilityAccessRequest(BaseModel):
    user_id: str
    level: str  # "everyone" | "registered" | "admin" | (anything else → everyone)


@router.get("/agents/{agent_id}/tools")
async def list_agent_tools(request: Request, agent_id: str, user_id: str = Query(...)):
    """Return the agent's actual loaded tool set with each tool's exposure mode.

    Drives the per-agent Tools panel. The list comes from the real tool loader
    (after ability gating), so it matches exactly what the agent can use. Each
    entry carries: name, description, ability (the gating ability or null),
    the resolved mode (core / always / discoverable), locked (core tools), and
    whether its full schema is sent by default.
    """
    import json as _json
    db = get_db()
    user_id = await assert_caller_is(request, user_id)
    agent = await db.get_agent_by_id(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found.")

    from app.tools.loader import load_tools
    from app.tools.tool_modes import (
        resolve_mode, is_locked, is_sent, ability_for_tool,
    )
    from app.tools.tool_defaults import resolve_permission
    # Display-only grouping for always-on core tools (no ability gates them, but a
    # "virtual" row like Core ▸ Base lists them). Kept separate from
    # ability_for_tool so these tools are never withheld by ability gating.
    from app.abilities import virtual_ability_for_tool as _virtual_ability_for_tool

    # App-wide global per-tool defaults — so the panel shows the EFFECTIVE
    # inherited mode/permission (agent-override ▸ global ▸ built-in), not just
    # the agent's own settings. Empty {} when unset.
    try:
        from app.admin.integrations import get_global_tool_defaults
        global_defaults = await get_global_tool_defaults()
    except Exception:
        global_defaults = {}

    allowed = agent.get("allowed_tools")
    if isinstance(allowed, str):
        try:
            allowed = _json.loads(allowed)
        except Exception:
            allowed = []

    try:
        tools = await load_tools(
            user_id,
            agent_id=agent_id,
            agent_template_id=agent.get("template_id"),
            allowed_tools=allowed or [],
            session_id="",
        )
    except Exception as e:
        logger.warning("list_agent_tools: load_tools failed for %s: %s", agent_id, e)
        tools = {}

    # ≈token estimator (the same char-based heuristic that drives the chat
    # context gauge) — so the "see schema" preview can show each tool's cost in
    # each visibility mode without a heavyweight tokenizer.
    try:
        from app.agent.context_control import estimate_tokens as _est
    except Exception:
        def _est(_msgs):
            return 0

    modes = await db.get_agent_tool_modes(agent_id)
    out: list[dict] = []
    for name, info in sorted(tools.items()):
        try:
            desc = (info.handler.__doc__ or "").strip().split("\n")[0]
        except Exception:
            desc = ""
        params = info.parameters if hasattr(info, "parameters") else {}
        # "visible" = the full tool definition the model receives every turn;
        # "discoverable" = just the one-line # [TOOLS] index entry.
        try:
            _vis_text = _json.dumps({"name": name, "description": desc, "parameters": params})
        except Exception:
            _vis_text = name
        _disc_text = f"- `{name}` — {desc}" if desc else f"- `{name}`"
        # The GLOBAL ceiling (admin per-tool default) — the floor of strictness
        # this agent can't go below: Auto < Ask < Deny. Explicit global denial
        # binds for every tool, so the UI never advertises an unusable exception.
        _g_perm = (global_defaults.get(name) or {}).get("permission")
        _ceiling = _g_perm if _g_perm in ("auto", "ask", "deny") else "auto"
        # A tool that inherently pauses for confirmation can't be shown — or set —
        # looser than "Ask". Fold that built-in floor into the ceiling (take the
        # stricter of the admin global limit and this floor). Auto < Ask < Deny.
        if getattr(info, "requires_confirmation", False):
            _rank = {"auto": 0, "ask": 1, "deny": 2}
            if _rank.get(_ceiling, 0) < 1:
                _ceiling = "ask"
        out.append({
            "name": name,
            "description": desc,
            # Real gating ability first; fall back to a display-only virtual row
            # (Core ▸ Base) so always-on core tools still group under a heading.
            "ability": ability_for_tool(name) or _virtual_ability_for_tool(name),
            "mode": resolve_mode(name, modes, global_defaults),
            "permission": resolve_permission(name, agent, global_defaults),
            "ceiling": _ceiling,
            "locked": is_locked(name),
            "sent": is_sent(name, modes, [], global_defaults),
            "destructive": bool(getattr(info, "destructive", False)
                                or getattr(info, "requires_confirmation", False)),
            "schema": params,
            "tokens_visible": _est([{"content": _vis_text}]),
            "tokens_discoverable": _est([{"content": _disc_text}]),
        })
    is_admin = await _is_agent_admin(db, agent_id, user_id)
    return {"tools": out, "count": len(out),
            "user_role": "admin" if is_admin else "member"}


@router.get("/agents/{agent_id}/context-tokens")
async def agent_context_tokens(
    request: Request,
    agent_id: str,
    user_id: str = Query(...),
):
    """Return the deterministic fresh-session Prompt & tools baseline.

    This mirrors the loop's static system-message layers, compact skills catalog,
    tool/ability indexes, and immediately-sent native schemas. It deliberately
    excludes session history and turn-specific routing, so it cannot exceed a
    real session footer's Prompt & tools value.
    """
    db = get_db()
    user_id = await assert_caller_is(request, user_id)
    agent = await db.get_agent_by_id(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found.")
    if not await _is_agent_admin(db, agent_id, user_id):
        raise HTTPException(status_code=403, detail="Only agent admins can view system-prompt context.")

    try:
        from app.agent.context_control import estimate_tokens
        from app.agent.prompts import append_skills_section, build_system_prompt_parts
        from app.admin.integrations import get_global_tool_defaults
        from app.abilities import ui_catalog
        from app.tools.loader import load_tools
        from app.tools.tool_modes import (
            ability_for_tool, is_sent, render_ability_index,
            render_index, resolve_mode, tool_hidden_by_ability,
        )
    except Exception as exc:  # pragma: no cover - a packaging error
        raise HTTPException(status_code=500, detail=f"Unable to load context builders: {exc}")

    try:
        resolved = await db.resolve_prompts(agent_id, user_id=user_id)
        context_docs = [
            {"id": slot["slot_name"], "context_type": slot["slot_name"],
             "title": slot["slot_name"], "content": slot["content"], "tags": []}
            for slot in resolved
            if (slot.get("content") or "").strip() and slot.get("slot_name") != "automation"
        ]
    except Exception:
        context_docs = []

    prompt_parts = await build_system_prompt_parts(
        context_docs, brain_context=None, user_id=user_id, agent_id=agent_id,
        session_id=None,  # deterministic fresh-session baseline
    )
    skills_block = await append_skills_section(
        "", agent, session_id=None, caller_user_id=user_id,
    )

    allowed_tools = agent.get("allowed_tools") or []
    if isinstance(allowed_tools, str):
        try:
            allowed_tools = _json.loads(allowed_tools)
        except Exception:
            allowed_tools = []
    try:
        tools = await load_tools(
            user_id, agent_id=agent_id, agent_template_id=agent.get("template_id"),
            allowed_tools=allowed_tools, session_id="", gate_caller_access=True,
        )
    except Exception as exc:
        logger.warning("context-tokens: load_tools failed for %s: %s", agent_id, exc)
        tools = {}

    try:
        raw_global_defaults = await get_global_tool_defaults() or {}
    except Exception:
        raw_global_defaults = {}
    # Older configurations may store a bare visibility string. Normalize it to
    # the current {tool: {visibility}} contract before the mode helpers read it.
    global_defaults = {
        name: value if isinstance(value, dict) else {"visibility": value}
        for name, value in raw_global_defaults.items()
    } if isinstance(raw_global_defaults, dict) else {}
    tool_modes = await db.get_agent_tool_modes(agent_id)
    ability_modes = await db.get_agent_ability_modes(agent_id)
    ability_default = await db.get_agent_discovery_default(agent_id)

    # A fresh session has not loaded any discoverable ability or tool yet.
    entries, native_definitions, ability_entries = [], [], []
    seen_abilities = set()
    catalog = (ui_catalog() or {}).get("abilities", {})
    for name, info in tools.items():
        ability_id = ability_for_tool(name)
        if tool_hidden_by_ability(name, ability_modes, (), (), (), ability_default):
            if ability_id and ability_id not in seen_abilities:
                seen_abilities.add(ability_id)
                meta = catalog.get(ability_id, {})
                ability_entries.append({
                    "id": ability_id,
                    "name": meta.get("display_name") or ability_id,
                    "desc": meta.get("skill_summary") or meta.get("description") or "",
                })
            continue
        description = (getattr(info, "handler", None).__doc__ or "").strip().split("\n")[0] \
            if getattr(info, "handler", None) else ""
        mode = resolve_mode(name, tool_modes, global_defaults)
        entries.append({"name": name, "desc": description, "mode": mode, "active": False})
        if is_sent(name, tool_modes, (), global_defaults):
            params = getattr(info, "parameters", None) or {
                "type": "object", "properties": {}, "required": [],
            }
            native_definitions.append({
                "type": "function",
                "function": {"name": name, "description": description, "parameters": params},
            })

    tools_index = render_index(entries)
    abilities_index = render_ability_index(ability_entries)

    def message_tokens(text: str) -> int:
        if not text or not text.strip():
            return 0
        return estimate_tokens([{"role": "system", "content": text}]) + 4

    # Skills are appended to the agent system layer in the runtime, so their text
    # gets no additional frame unless that layer would otherwise be empty.
    agent_layer = "\n\n".join(part for part in (
        prompt_parts.agent_context, skills_block,
    ) if part and part.strip()).strip()
    schema_tokens = estimate_tokens([
        {"role": "system", "content": _json.dumps(native_definitions)},
    ]) if native_definitions else 0
    breakdown = {
        "system_prompt": message_tokens(prompt_parts.shared_core)
            + message_tokens(agent_layer) - estimate_tokens(skills_block),
        "skills": estimate_tokens(skills_block),
        "tools": message_tokens(tools_index) + message_tokens(abilities_index)
            + schema_tokens,
    }
    breakdown = {name: value for name, value in breakdown.items() if value}
    total = sum(breakdown.values())
    return {"tokens": total, "breakdown": breakdown}


@router.put("/agents/{agent_id}/tools/{tool_name}")
async def set_agent_tool_mode_endpoint(
    agent_id: str, tool_name: str, req: SetToolModeRequest, request: Request,
):
    """Set how a tool is exposed to the agent: 'always' (full schema every turn)
    or 'discoverable' (load on demand). Any other value clears the override back
    to the default. Core tools cannot be changed."""
    db = get_db()
    req.user_id = await assert_caller_is(request, req.user_id)
    if not await _is_agent_admin(db, agent_id, req.user_id):
        raise HTTPException(status_code=403, detail="Only agent admins can change tool settings.")
    from app.tools.tool_modes import is_locked, resolve_mode
    if is_locked(tool_name):
        raise HTTPException(
            status_code=400,
            detail=f"'{tool_name}' is a core tool — it is always available and cannot be changed.",
        )
    modes = await db.set_agent_tool_mode(agent_id, tool_name, req.mode)
    return {"tool": {"name": tool_name, "mode": resolve_mode(tool_name, modes)}}


@router.put("/agents/{agent_id}/abilities/{ability_id}/visibility")
async def set_agent_ability_visibility(
    agent_id: str, ability_id: str, req: SetToolModeRequest, request: Request,
):
    """Set how an ability is exposed to the agent: 'visible' (its tools + how-to
    skill are shown now) or 'discoverable' (collapsed to one # [ABILITIES] entry;
    the agent reveals it with load_ability). Any other value clears the override
    back to the 'visible' default. Per-agent only — admins set the ceiling
    (enable + permission), agents tune discovery for their role."""
    db = get_db()
    req.user_id = await assert_caller_is(request, req.user_id)
    if not await _is_agent_admin(db, agent_id, req.user_id):
        raise HTTPException(status_code=403, detail="Only agent admins can change ability settings.")
    from app.tools.tool_modes import resolve_ability_mode
    modes = await db.set_agent_ability_mode(agent_id, ability_id, req.mode)
    return {"ability": {"id": ability_id, "mode": resolve_ability_mode(ability_id, modes)}}


@router.put("/agents/{agent_id}/abilities/{ability_id}/access")
async def set_agent_ability_access(
    agent_id: str, ability_id: str, req: SetAbilityAccessRequest, request: Request,
):
    """Set an ability's required caller-access level on this agent: 'everyone'
    (anyone, incl. anonymous guests — the default), 'registered' (signed-in
    accounts only), or 'admin' (admins only). 'everyone' (or any unknown value)
    clears the restriction. Enforced server-side at tool-assembly time
    (app/tools/loader.load_tools via app/agent/ability_access) — the gated
    ability's tools are never built for a caller below the level, so it's a real
    boundary, not a prompt hint. Per-agent only."""
    db = get_db()
    req.user_id = await assert_caller_is(request, req.user_id)
    if not await _is_agent_admin(db, agent_id, req.user_id):
        raise HTTPException(status_code=403, detail="Only agent admins can change ability settings.")
    from app.tools.tool_modes import resolve_ability_access
    access = await db.set_agent_ability_access(agent_id, ability_id, req.level)
    return {"ability": {"id": ability_id, "available_to": resolve_ability_access(ability_id, access)}}


@router.put("/agents/{agent_id}/abilities/{ability_id}/skill-visibility")
async def set_agent_skill_visibility(
    agent_id: str, ability_id: str, req: SetToolModeRequest, request: Request,
    skill_id: str = Query("core"),
):
    """Set how an ability's bundled **skill** is exposed to the agent: 'visible'
    (its how-to body is shown every turn), 'off' (unavailable), or 'discoverable' (load on demand via
    load_skill). Any other value clears the override back to the descriptor
    default. Per-agent."""
    db = get_db()
    req.user_id = await assert_caller_is(request, req.user_id)
    if not await _is_agent_admin(db, agent_id, req.user_id):
        raise HTTPException(status_code=403, detail="Only agent admins can change skill settings.")
    from app.abilities import ability_general_skills
    record = next((s for s in ability_general_skills(ability_id) if s.get("skill_id") == skill_id), None)
    if not record:
        raise HTTPException(status_code=404, detail="Unknown ability skill.")
    from app.tools.tool_modes import resolve_skill_mode
    skill_key = record["skill_key"]
    # Legacy root override storage stays ability-id keyed; added skills use the
    # stable composite key and therefore never collide with their siblings.
    stored_key = ability_id if skill_id == "core" else skill_key
    modes = await db.set_agent_skill_mode(agent_id, stored_key, req.mode)
    return {"skill": {"ability_id": ability_id, "skill_id": skill_id,
                      "skill_key": skill_key,
                      "mode": resolve_skill_mode(skill_key, modes, record.get("default_mode"))}}


@router.get("/agents/{agent_id}/abilities/{ability_id}/schema-preview")
async def ability_schema_preview(request: Request, agent_id: str, ability_id: str, user_id: str = Query(...)):
    """The ACTUAL text the agent receives for one ability, given its current
    visibility config — so the Abilities tab can show exactly what's sent:

    - ability **discoverable** → just the one-line `# [ABILITIES]` entry (hidden);
    - ability **visible**, every tool+skill visible → the **complete** schema;
    - ability **visible**, mixed → the real mix (visible tools' full schemas +
      discoverable tools' one-line entries; skill body vs its load-on-demand line).

    Returns `{state: hidden|partial|full, text, tokens}` (≈tokens via the same
    char heuristic as the chat gauge).
    """
    import json as _json
    db = get_db()
    user_id = await assert_caller_is(request, user_id)
    agent = await db.get_agent_by_id(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found.")

    from app.tools.tool_modes import (
        resolve_mode, resolve_ability_mode, resolve_skill_mode, VISIBLE,
    )
    from app.tools.loader import load_tools, ABILITY_TOOLS
    from app.abilities import ui_catalog, ability_feature_with_skill
    try:
        from app.agent.context_control import estimate_tokens as _est
    except Exception:
        def _est(_m):
            return 0

    ab_modes = await db.get_agent_ability_modes(agent_id)
    sk_modes = await db.get_agent_skill_modes(agent_id)
    tool_modes = await db.get_agent_tool_modes(agent_id)
    try:
        from app.admin.integrations import get_global_tool_defaults
        gdef = await get_global_tool_defaults()
    except Exception:
        gdef = {}

    meta = (ui_catalog() or {}).get("abilities", {}).get(ability_id, {})
    display = meta.get("display_name") or ability_id
    summary = meta.get("skill_summary") or meta.get("description") or ""

    # Discoverable ability → the agent only sees the one-line [ABILITIES] entry.
    if resolve_ability_mode(ability_id, ab_modes) != VISIBLE:
        text = f"# [ABILITIES]\n- `{ability_id}` — {display}: {summary}".strip()
        return {"state": "hidden", "text": text, "tokens": _est([{"content": text}])}

    # Visible ability → compose the real tool + skill text.
    allowed = agent.get("allowed_tools")
    if isinstance(allowed, str):
        try:
            allowed = _json.loads(allowed)
        except Exception:
            allowed = []
    try:
        tools = await load_tools(
            user_id, agent_id=agent_id, agent_template_id=agent.get("template_id"),
            allowed_tools=allowed or [], session_id="",
        )
    except Exception:
        tools = {}

    parts: list[str] = []
    n_visible = 0
    n_total = 0
    for name in [n for n in ABILITY_TOOLS.get(ability_id, []) if n in tools]:
        info = tools.get(name)
        desc = ((info.handler.__doc__ or "").strip().split("\n")[0]
                if info and hasattr(info, "handler") else "")
        n_total += 1
        if resolve_mode(name, tool_modes, gdef) == VISIBLE:
            n_visible += 1
            params = info.parameters if (info and hasattr(info, "parameters")) else {}
            parts.append(_json.dumps({"name": name, "description": desc, "parameters": params}, indent=2))
        else:
            parts.append(f"- `{name}` — {desc}")

    feat = ability_feature_with_skill(ability_id)
    if feat and resolve_skill_mode(f"{ability_id}:core", sk_modes, feat.get("skill_mode")) != "off":
        n_total += 1
        if resolve_skill_mode(f"{ability_id}:core", sk_modes, feat.get("skill_mode")) == VISIBLE:
            n_visible += 1
            parts.append(f"# [SKILL: {display}]\n{(feat.get('skill') or '').strip()}".strip())
        else:
            parts.append(f"# [SKILLS]\n- {display} (load on demand): {feat.get('skill_summary') or summary}".strip())

    text = "\n\n".join(p for p in parts if p).strip() or "(this ability exposes no tools or skill)"
    if n_total == 0:
        state = "full"
    elif n_visible == 0:
        state = "hidden"
    elif n_visible == n_total:
        state = "full"
    else:
        state = "partial"
    return {"state": state, "text": text, "tokens": _est([{"content": text}])}


class ManageAdminRequest(BaseModel):
    user_id: str        # caller (must already be admin)
    target_user_id: str # user to add as admin


@router.post("/agents/{agent_id}/admins")
async def add_agent_admin(agent_id: str, req: ManageAdminRequest, request: Request):
    """Add target_user_id to an agent's admin_users list. Caller must be an existing admin."""
    db = get_db()
    req.user_id = await assert_caller_is(request, req.user_id)
    if not await _is_agent_admin(db, agent_id, req.user_id):
        raise HTTPException(status_code=403, detail="Admin access required.")
    from app.agent import profiles as agent_profiles
    target = await agent_profiles.ensure_app_member(agent_id, req.target_user_id)
    promoted = await agent_profiles.set_member_admin(agent_id, target["id"], True)
    return {"principal": agent_profiles.safe_principal(promoted), "added": True}


@router.get("/agents/{agent_id}/members")
async def list_agent_members(request: Request, agent_id: str, user_id: str = Query(...)):
    """
    List the agent's admin_users and member_users joined with profile + activity stats.
    Caller must be a global admin OR in the agent's admin_users list.
    Returns {"admins": [...], "members": [...]} where each entry has
    user_id, username, display_name, is_admin, is_approved, channel,
    last_login_at, created_at, session_count, interaction_count.
    """
    from app.auth.users import get_user_by_id as _auth_get_user_by_id

    async def _load_auth_map(uids: list[str]) -> dict:
        """Pre-fetch account rows for `uids` (the store is async now, but the
        per-row _build() below is sync, so gather them up front like the other
        *_map lookups)."""
        out: dict[str, object] = {}
        for _uid in uids:
            out[_uid] = await _auth_get_user_by_id(_uid)
        return out

    db = get_db()
    user_id = await assert_caller_is(request, user_id)
    if not await _is_agent_admin(db, agent_id, user_id):
        raise HTTPException(status_code=403, detail="Admin access required.")

    agent_row = await db.get_agent_by_id(agent_id)
    if not agent_row:
        raise HTTPException(status_code=404, detail="Agent not found.")
    user_mode = agent_row.get("user_mode") or "anonymous"

    roles = await db.get_agent_roles(agent_id)
    admin_ids: list[str] = list(roles.get("admin_users") or [])
    member_ids: list[str] = list(roles.get("member_users") or [])
    authorized_set = set(roles.get("authorized_users") or [])
    all_ids = list(dict.fromkeys(admin_ids + member_ids))

    # Reuse the same authorization gate as the existing Members table so
    # legacy agent administrators can see the policy summary too.
    from app.agent import profiles as agent_profiles
    if not all_ids:
        profiles_list = await agent_profiles.list_profiles(agent_id)
        return {
            "admins": [],
            "members": [],
            "profiles": profiles_list,
            "profile_members": [],
            "user_mode": user_mode,
        }

    # Profiles and channel identities are installation/control-plane data.  The
    # agent's user-scoped connection deliberately does not own those tables.
    from app.db.storage_layout import get_app_store

    conn = db._get_conn()
    profile_map: dict[str, dict] = {}
    identity_map: dict[str, dict] = {}
    session_counts: dict[str, int] = {}
    interaction_counts: dict[str, int] = {}
    try:
        placeholders = ",".join("?" * len(all_ids))
        app_store = get_app_store()
        for r in app_store.fetchall(
            f"SELECT user_id, is_admin, created_at, last_login_at "
            f"FROM user_profiles WHERE user_id IN ({placeholders})",
            all_ids,
        ):
            profile_map[r["user_id"]] = r
        for r in app_store.fetchall(
            f"SELECT user_id, display_name, channel FROM channel_identities "
            f"WHERE user_id IN ({placeholders})",
            all_ids,
        ):
            # Keep first/most-recent-ish; if multiple, last wins (fine for display)
            identity_map[r["user_id"]] = r
        for r in conn.execute(
            f"SELECT user_id, COUNT(*) AS n FROM sessions "
            f"WHERE agent_id = ? AND user_id IN ({placeholders}) GROUP BY user_id",
            [agent_id, *all_ids],
        ).fetchall():
            session_counts[r["user_id"]] = r["n"]
        for r in conn.execute(
            f"SELECT s.user_id AS user_id, COUNT(*) AS n FROM interactions i "
            f"JOIN sessions s ON s.id = i.session_id "
            f"WHERE s.agent_id = ? AND s.user_id IN ({placeholders}) "
            f"GROUP BY s.user_id",
            [agent_id, *all_ids],
        ).fetchall():
            interaction_counts[r["user_id"]] = r["n"]
    finally:
        conn.close()

    auth_map = await _load_auth_map(all_ids)

    # Membership profiles live in the agent authority plane rather than the
    # legacy roles list. Resolve only existing links here: listing members must
    # never auto-enrol or otherwise mutate a profile assignment.
    member_profile_map = {
        uid: await agent_profiles.resolve_member(agent_id, uid, auto_link_app=False)
        for uid in all_ids
    }

    def _build(uid: str) -> dict:
        prof = profile_map.get(uid, {})
        profile_member = member_profile_map.get(uid)
        assigned_profile = (profile_member or {}).get("profile") or {}
        member_level = {
            "slug": assigned_profile.get("slug") or "member",
            "name": assigned_profile.get("name") or "Member",
            "assigned": bool(assigned_profile),
        }
        ident = identity_map.get(uid, {})
        auth_user = auth_map.get(uid)
        username = auth_user.username if auth_user else None
        display_name = (
            (auth_user.display_name if auth_user else None)
            or ident.get("display_name")
            or username
            or uid
        )
        return {
            "user_id": uid,
            "username": username,
            "display_name": display_name,
            "channel": ident.get("channel"),
            "is_admin": bool(prof.get("is_admin", 0)),
            "is_approved": bool(auth_user.is_approved) if auth_user else None,
            "created_at": prof.get("created_at"),
            "last_login_at": prof.get("last_login_at"),
            "session_count": session_counts.get(uid, 0),
            "interaction_count": interaction_counts.get(uid, 0),
            "member_level": member_level,
            "is_authorized": uid in authorized_set,
        }

    admins = [_build(uid) for uid in admin_ids]
    members = [_build(uid) for uid in member_ids if uid not in set(admin_ids)]
    # Reuse the same authorization gate as the existing Members table so
    # legacy agent administrators can see the policy summary too.
    profiles_list = await agent_profiles.list_profiles(agent_id)
    profile_members = [agent_profiles.safe_principal(m) for m in await agent_profiles.list_members(agent_id)]
    return {
        "admins": admins,
        "members": members,
        "profiles": profiles_list,
        "profile_members": profile_members,
        "user_mode": user_mode,
    }


class _AuthorizeRequest(BaseModel):
    user_id: str   # caller (must be agent admin)


@router.post("/agents/{agent_id}/members/{target_user_id}/authorize")
async def authorize_agent_member(agent_id: str, target_user_id: str, req: _AuthorizeRequest, request: Request):
    """Mark a user as authorized for this agent. Caller must be agent admin."""
    db = get_db()
    req.user_id = await assert_caller_is(request, req.user_id)
    if not await _is_agent_admin(db, agent_id, req.user_id):
        raise HTTPException(status_code=403, detail="Admin access required.")
    authorized_users = await db.set_agent_authorized(agent_id, target_user_id, True)
    return {"authorized_users": authorized_users, "is_authorized": True}


@router.post("/agents/{agent_id}/members/{target_user_id}/restrict")
async def restrict_agent_member(agent_id: str, target_user_id: str, req: _AuthorizeRequest, request: Request):
    """Remove a user from the authorized list for this agent. Caller must be agent admin."""
    db = get_db()
    req.user_id = await assert_caller_is(request, req.user_id)
    if not await _is_agent_admin(db, agent_id, req.user_id):
        raise HTTPException(status_code=403, detail="Admin access required.")
    authorized_users = await db.set_agent_authorized(agent_id, target_user_id, False)
    return {"authorized_users": authorized_users, "is_authorized": False}


class _SetUserModeRequest(BaseModel):
    user_id: str   # caller (must be agent admin)
    user_mode: str # 'anonymous' | 'register' | 'authorized'


@router.post("/agents/{agent_id}/user-mode")
async def set_agent_user_mode(agent_id: str, req: _SetUserModeRequest, request: Request):
    """Set the agent's user_mode policy. Caller must be agent admin."""
    if req.user_mode not in ("anonymous", "register", "authorized"):
        raise HTTPException(status_code=400, detail="Invalid user_mode.")
    db = get_db()
    req.user_id = await assert_caller_is(request, req.user_id)
    if not await _is_agent_admin(db, agent_id, req.user_id):
        raise HTTPException(status_code=403, detail="Admin access required.")
    conn = db._get_conn()
    try:
        cursor = conn.execute(
            "UPDATE agents SET user_mode = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (req.user_mode, agent_id),
        )
        conn.commit()
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Agent not found.")
    finally:
        conn.close()
    return {"agent_id": agent_id, "user_mode": req.user_mode}


@router.post("/agents/{agent_id}/anon-session")
async def create_anon_session(agent_id: str, req: AnonSessionRequest, request: Request):
    """
    Create an anonymous session for a public agent URL visitor.
    No JWT required. Returns a token so the visitor can chat.
    """
    import uuid as _uuid_mod
    db = get_db()

    agent = await db.get_agent_by_id(agent_id)
    if not agent and agent_id == SHARED_DEFAULT_AGENT_ID:
        agent = await provision_default_agent(db, "admin")
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found.")

    # Public-link policy is authoritative for both the share URL and this API.
    # Keep disabled agents indistinguishable from missing ones to avoid exposing
    # otherwise-private agent IDs.
    meta = agent.get("metadata") or {}
    if isinstance(meta, str):
        try:
            meta = _json.loads(meta)
        except Exception:
            meta = {}
    if not isinstance(meta, dict) or not meta.get("public_link", meta.get("is_embeddable")):
        raise HTTPException(status_code=404, detail="Agent not found.")

    mode = (agent.get("user_mode") or "anonymous")
    if mode in ("register", "authorized"):
        raise HTTPException(
            status_code=403,
            detail=("This agent requires a registered account."
                    if mode == "register"
                    else "This agent requires admin authorization. Sign in with an authorized account."),
        )

    from app.agent.public_policy import require_public_funding
    await require_public_funding(db, agent)

    browser_id = req.browser_id or _uuid_mod.uuid4().hex
    from app.api.rate_limit import enforce_anon_session_creation, bind_anon_session_identity
    reservation = await enforce_anon_session_creation(request, browser_id)
    from app.agent.profiles import ensure_guest_member
    member, durable_credential = await ensure_guest_member(agent_id, req.guest_credential or "")
    visitor_user_id = member["subject_id"]
    await bind_anon_session_identity(reservation, visitor_user_id)

    from app.auth.jwt import create_access_token
    token = create_access_token(
        username=visitor_user_id,
        user_id=visitor_user_id,
        expires_minutes=60,
        extra_claims={
            "anon_admission": True,
            "anon_admission_id": secrets.token_urlsafe(18),
            "anon_source": reservation["source_hash"],
            "agent_id": agent_id,
            "agent_member_id": member["id"],
            "agent_identity": True,
        },
    )

    return {
        "token": token,
        "user_id": visitor_user_id,
        "session_id": visitor_user_id,
        "guest_credential": durable_credential,
        "access_token_expires_in": 3600,
    }


@router.get("/agents/{agent_id}/embed")
async def get_agent_embed(agent_id: str, request: Request):
    """Public embed descriptor for a website widget.

    No auth: everything returned is presentation-only (display config + the
    copy-paste snippet), so both the standalone /embed/<agent_id> page and an
    owner's config UI read the same source. The domain allowlist is deliberately
    NOT included — it's enforced server-side via the page's CSP header, never
    trusted to the client. ``embeddable`` folds the two gates a visitor must
    pass: the owner turned the widget on AND the agent accepts anonymous chat."""
    from app.api.embed_config import public_embed_config, build_embed_snippet
    db = get_db()
    agent = await db.get_agent_by_id(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found.")
    cfg = public_embed_config(agent)
    origin = str(request.base_url).rstrip("/")
    fwd_proto = request.headers.get("x-forwarded-proto", "")
    if fwd_proto and origin.startswith("http://"):
        from app.admin.integrations import _is_trusted_proxy
        if _is_trusted_proxy(request):
            origin = "https://" + origin[len("http://"):]
    anon_ok = (agent.get("user_mode") or "anonymous") == "anonymous"
    payload = {
        "agent_id": agent_id,
        "agent_name": agent.get("name") or "Agent",
        "agent_icon": cfg.get("agent_icon") or "bot",
        "enabled": bool(cfg.get("enabled")),
        "anonymous_chat": anon_ok,
        "embeddable": bool(cfg.get("enabled")) and anon_ok,
        "config": cfg,
        "snippet": build_embed_snippet(origin, agent_id, cfg),
    }
    return JSONResponse(
        content=payload,
        headers={
            # Open CORS by design: the embed loader fetches this descriptor from
            # the CUSTOMER's site (any origin) with no credentials, and everything
            # here is public presentation data. Granting "*" means a client can
            # paste the snippet and the widget works with ZERO per-client server
            # config — no WEBAGENT_ALLOWED_ORIGINS, no restart. The real security
            # gate (who may FRAME the chat page) stays server-side via the embed
            # page's CSP frame-ancestors from the agent's allowed_domains; the
            # allowlist is deliberately stripped from this payload.
            "Access-Control-Allow-Origin": "*",
            "Vary": "Origin",
        },
    )


# ── Per-agent OAuth Abilities (3-tier OAuth system) ──────────────────────

class UpsertAbilityRequest(BaseModel):
    user_id: str                                # caller (must be agent admin)
    enabled: Optional[bool] = None              # toggle on/off
    source:  Optional[str]  = None              # "platform" | "byo"


class SetByoCredsRequest(BaseModel):
    user_id: str
    client_id: str
    client_secret: str


def _is_known_ability(ability_id: str) -> bool:
    from app.integrations.ability_registry import get_ability
    return get_ability(ability_id) is not None


@router.get("/agents/{agent_id}/abilities")
async def list_agent_abilities(request: Request, agent_id: str, user_id: str = Query(...)):
    """Return every registered OAuth ability with its enabled/source/byo state for this agent.

    Merges three layers in one response so the agent admin UI can render the
    full picture in a single fetch:
      - registry definition (display name, scopes, provider)
      - app-admin policy (mode, scope caps)
      - per-agent state (enabled flag, source, BYO client id [secret masked])

    Read-only members get the same payload — server-side mutation gates kick
    in on the PUT endpoints below.
    """
    from app.integrations.ability_registry import ABILITIES
    from app.admin.integrations import get_all_oauth_ability_configs
    db = get_db()
    user_id = await assert_caller_is(request, user_id)
    rows = (await db.get_agent_abilities(agent_id)) if hasattr(db, "get_agent_abilities") else []
    by_id = {r["ability_id"]: r for r in rows}

    # Batch-fetch all ability admin policies in a single query instead of
    # calling get_oauth_ability_config N times (N = ~58 abilities).
    all_configs = await get_all_oauth_ability_configs()

    out: list[dict] = []
    for ab in ABILITIES.values():
        row = by_id.get(ab.id) or {}
        policy = all_configs.get(ab.id, {
            "mode": "platform_only",
            "platform_scopes": list(ab.scopes),
            "max_byo_scopes": list(ab.scopes),
        })
        byo_cid = row.get("byo_client_id", "") or ""
        # Mask BYO client_id to last 6 chars so members never see real values.
        masked_cid = ("•" * max(0, len(byo_cid) - 6) + byo_cid[-6:]) if byo_cid else ""
        out.append({
            "id": ab.id,
            "provider": ab.provider,
            "display_name": ab.display_name,
            "description": ab.description,
            "scopes": list(ab.scopes),
            "implicit": ab.implicit,
            "mode": policy["mode"],
            "enabled": bool(row.get("enabled", False)),
            "source": row.get("source") or "platform",
            "byo_client_id_masked": masked_cid,
            "byo_configured": bool(row.get("byo_client_id") and row.get("byo_client_secret_ref")),
        })
    is_admin = await _is_agent_admin(db, agent_id, user_id)
    return {"abilities": out, "user_role": "admin" if is_admin else "member"}


@router.put("/agents/{agent_id}/abilities/{ability_id}")
async def upsert_agent_ability(
    agent_id: str,
    ability_id: str,
    req: UpsertAbilityRequest,
    request: Request,
):
    """Toggle an ability on/off and/or switch source (platform vs BYO).

    On enable, returns `reauth_required: true` plus a fresh authorize URL when
    the agent's existing token doesn't already cover this ability's scopes —
    the UI prompts the user to re-consent before the agent can use the new
    capability.
    """
    if not _is_known_ability(ability_id):
        raise HTTPException(status_code=404, detail=f"Unknown ability: {ability_id}")
    db = get_db()
    req.user_id = await assert_caller_is(request, req.user_id)
    if not await _is_agent_admin(db, agent_id, req.user_id):
        raise HTTPException(status_code=403, detail="Only agent admins can change abilities.")
    if req.enabled:
        try:
            await enforce_ability_group(db, req.user_id, ability_id)
        except ResourceEntitlementError as exc:
            raise _resource_http_error(exc) from exc

    from app.admin.integrations import get_oauth_ability_config
    from app.integrations.ability_registry import get_ability

    ab = get_ability(ability_id)
    policy = await get_oauth_ability_config(ability_id)
    # Enforce app-admin mode policy.
    if req.enabled and policy["mode"] == "disabled":
        raise HTTPException(status_code=400, detail=f"Ability '{ability_id}' is disabled by the app admin.")
    new_source = req.source
    if new_source is None:
        new_source = "platform"
    if new_source not in ("platform", "byo"):
        raise HTTPException(status_code=400, detail="source must be 'platform' or 'byo'")
    if new_source == "platform" and policy["mode"] == "byo_only":
        raise HTTPException(status_code=400, detail="App admin requires BYO for this ability.")
    if new_source == "byo" and policy["mode"] == "platform_only":
        raise HTTPException(status_code=400, detail="App admin disallows BYO for this ability.")

    if not hasattr(db, "upsert_agent_ability"):
        raise HTTPException(status_code=500, detail="Backend does not support agent_abilities yet.")
    row = await db.upsert_agent_ability(
        agent_id, ability_id,
        enabled=req.enabled,
        source=new_source,
    )

    # Newly-enabled ability → default its tools to "discoverable" (hybrid rule).
    # No-op for OAuth abilities that provide no built-in tools.
    if req.enabled:
        try:
            from app.tools.tool_modes import tools_for_ability
            _seed = tools_for_ability(ability_id)
            if _seed:
                await db.seed_agent_tool_modes(agent_id, _seed, "discoverable")
        except Exception as _se:
            logger.debug("tool-mode seed (ability %s) failed: %s", ability_id, _se)

    # Check whether the existing token already covers this ability — if not,
    # build a fresh authorize URL so the UI can prompt re-consent.
    reauth_required = False
    authorize_url = None
    if req.enabled and ab is not None:
        try:
            from app.integrations.oauth_helper import check_ability_authorized
            authorized = await check_ability_authorized(req.user_id, agent_id, ab.provider, ability_id)
            if not authorized:
                reauth_required = True
                authorize_url = await _build_authorize_url_for(ab.provider, req.user_id, agent_id, source=new_source)
        except Exception as e:
            logger.debug("re-auth check failed for %s: %s", ability_id, e)

    return {
        "ability": {
            "id": ability_id,
            "enabled": bool(row.get("enabled")) if row else False,
            "source": row.get("source") if row else new_source,
        },
        "reauth_required": reauth_required,
        "authorize_url": authorize_url,
    }


@router.put("/agents/{agent_id}/abilities/{ability_id}/byo-creds")
async def set_agent_ability_byo_creds(
    agent_id: str,
    ability_id: str,
    req: SetByoCredsRequest,
    request: Request,
):
    """Set the agent admin's BYO OAuth client id + secret for this ability."""
    if not _is_known_ability(ability_id):
        raise HTTPException(status_code=404, detail=f"Unknown ability: {ability_id}")
    db = get_db()
    req.user_id = await assert_caller_is(request, req.user_id)
    if not await _is_agent_admin(db, agent_id, req.user_id):
        raise HTTPException(status_code=403, detail="Only agent admins can set BYO credentials.")
    try:
        await enforce_ability_group(db, req.user_id, ability_id)
    except ResourceEntitlementError as exc:
        raise _resource_http_error(exc) from exc
    cid = (req.client_id or "").strip()
    csec = (req.client_secret or "").strip()
    if not cid or not csec:
        raise HTTPException(status_code=400, detail="client_id and client_secret are required.")
    if not hasattr(db, "upsert_agent_ability"):
        raise HTTPException(status_code=500, detail="Backend does not support agent_abilities yet.")
    await db.upsert_agent_ability(
        agent_id, ability_id,
        source="byo", byo_client_id=cid, byo_client_secret_ref=csec,
    )
    return {"status": "ok"}


async def _build_authorize_url_for(
    provider: str, user_id: str, agent_id: str, *, source: str = "platform",
    request: Optional[Request] = None,
) -> Optional[str]:
    """Centralized authorize-URL builder used by ability re-consent flow."""
    from app.admin import integrations as ints
    p = (provider or "").lower()
    if p == "google":
        return await ints.build_google_authorize_url(user_id, agent_id, request, source=source)
    if p == "microsoft":
        return await ints.build_microsoft_authorize_url(user_id, agent_id, request, source=source)
    if p == "yahoo":
        return await ints.build_yahoo_authorize_url(user_id, agent_id, request, source=source)
    if p == "dropbox":
        return await ints.build_dropbox_authorize_url(user_id, agent_id, request, source=source)
    if p == "meta":
        return await ints.build_meta_authorize_url(user_id, agent_id, request, source=source)
    if p == "twitter":
        url, _ = await ints.build_twitter_authorize_url(user_id, agent_id, request, source=source)
        return url
    if p == "linkedin":
        return await ints.build_linkedin_authorize_url(user_id, agent_id, request, source=source)
    if p == "tiktok":
        return await ints.build_tiktok_authorize_url(user_id, agent_id, request, source=source)
    if p == "pinterest":
        return await ints.build_pinterest_authorize_url(user_id, agent_id, request, source=source)
    if p == "reddit":
        return await ints.build_reddit_authorize_url(user_id, agent_id, request, source=source)
    if p == "snapchat":
        return await ints.build_snapchat_authorize_url(user_id, agent_id, request, source=source)
    if p == "twitch":
        return await ints.build_twitch_authorize_url(user_id, agent_id, request, source=source)
    if p == "ebay":
        return await ints.build_ebay_authorize_url(user_id, agent_id, request, source=source)
    if p == "etsy":
        return await ints.build_etsy_authorize_url(user_id, agent_id, request, source=source)
    if p == "amazon":
        return await ints.build_amazon_authorize_url(user_id, "NA", agent_id, request, source=source)
    # shopify needs a shop domain — caller has to use the per-provider authorize endpoint.
    return None


# ── Per-agent OAuth landing routes + homepage stub ────────────────────────

# Routes outside the `/api/v1` prefix — these are user-facing URLs registered
# on the global app router, mounted from app/main.py. Anchored to the agent's
# stable homepage `/agents/{id}` so the visualizer can take it over later.

from fastapi.responses import RedirectResponse

# Separate router — these URLs (e.g. `/agents/{id}`) live outside the
# `/api/v1` prefix used by the main agents router above.
agent_pages_router = APIRouter()


@agent_pages_router.get("/api/v1/agents/{agent_id}/ui/{asset_path:path}")
async def agent_ui_asset(agent_id: str, asset_path: str, request: Request):
    """Serve an authorized agent UI inside a CSP sandbox.

    Phase one intentionally supports self-contained assets only. A future parent
    message bridge can expose explicitly allowlisted capabilities without giving
    the iframe app credentials or same-origin authority.
    """
    from fastapi.responses import FileResponse
    from app.auth.identity import request_user_id
    from app.agent_workspace import resolve_ui_asset

    user_id = request_user_id(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    db = get_db()
    agent = await db.fetch_agent_by_id_with_context(agent_id, [], user_id=user_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent UI not found")
    from app.chat.execution import _enforce_agent_access_policy
    await _enforce_agent_access_policy(db, agent, user_id)
    path = resolve_ui_asset(agent_id, asset_path or "index.html")
    if path is None or not path.is_file():
        raise HTTPException(status_code=404, detail="Agent UI not found")
    return FileResponse(path, headers={
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": (
            "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
            "img-src data: blob:; media-src data: blob:; connect-src 'none'; "
            "base-uri 'none'; form-action 'none'; sandbox allow-scripts"
        ),
    })


@agent_pages_router.get("/agents/{agent_id}")
async def agent_homepage(agent_id: str):
    """Agent homepage stub. 302s to the agent detail view in the main app.

    The visualizer integration will eventually render the agent UI at this
    URL directly; for now we keep the URL stable and bounce users into the
    SPA so deep-links still work.
    """
    return RedirectResponse(url=f"/#agent/{agent_id}", status_code=302)


@agent_pages_router.get("/agents/{agent_id}/oauth/callback/{provider}")
async def per_agent_oauth_landing(agent_id: str, provider: str, request: Request):
    """Per-agent OAuth landing URL.

    For platform OAuth flows (Phase 2), the provider redirects to the standard
    `/api/v1/oauth/callback/{provider}` URI directly — this route is reserved
    for BYO flows where the agent admin has registered THIS URI in their own
    OAuth project (Phase 3). On hit, we forward to the standard callback with
    the same query string so the existing token-exchange logic picks up.
    """
    target = f"/api/v1/oauth/callback/{provider}"
    qs = request.url.query
    if qs:
        target = f"{target}?{qs}"
    return RedirectResponse(url=target, status_code=307)


@router.post("/agent-templates/config")
async def set_template_discoverable(req: UpdateTemplateRequest):
    """
    Update admin-controlled fields on an agent template (admin only).
    Currently supports: discoverable.
    """
    db = get_db()
    await _require_admin(db, req.user_id)
    updates = {}
    if req.discoverable is not None:
        updates["discoverable"] = 1 if req.discoverable else 0
    if not updates:
        raise HTTPException(status_code=400, detail="No updatable fields provided.")
    updated = await db.update_agent_template_fields(template_id=req.template_id, updates=updates)
    if updated is None:
        raise HTTPException(status_code=404, detail="Template not found.")
    return {"template": _safe_agent(updated)}


