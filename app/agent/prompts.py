"""
Prompt construction with dynamic tool descriptions from database.
"""

import asyncio
import base64
import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Union

from app.db.offload import db_offload

from app.db.system_prompt_fragments import (
    format_tool_subheadings_markdown,
    get_prompt_fragments,
)

logger = logging.getLogger(__name__)


_ANONYMOUS_AGENT_PROMPT = """You are a helpful conversational assistant.
Answer the user's questions clearly and directly using the information already
available in this conversation and your general knowledge. If a request requires
an action outside this conversation, say that you cannot perform that action and
offer a useful explanation or alternative."""


def _is_anonymous_user(user_id: Optional[str]) -> bool:
    return bool(user_id) and str(user_id).startswith("anon_")


def _anonymous_showcase(user_id: Optional[str], agent: Optional[dict]) -> bool:
    if not _is_anonymous_user(user_id):
        return False
    try:
        from app.agent.public_policy import is_showcase_policy
        return is_showcase_policy(agent or {})
    except Exception:
        return True

_VISION_INLINE_MAX_BYTES = 20 * 1024 * 1024
_VISION_INLINE_MIMES = {"image/jpeg", "image/png", "image/gif", "image/webp"}


# Legacy slot names — kept as a public list so the prompt-build pipeline (and
# any external scripts that gate on context types) can still ask "what slots
# might exist". With free-form admin slots this is just a hint, not a filter.
CONTEXT_SECTION_TYPES = [
    "system", "agent", "user", "skills", "tasks", "misc",
    "bootstrap_tools", "optimizer", "memory", "project", "jobs", "tools",
]


@dataclass
class SystemPromptParts:
    """Prompt layers ordered from most reusable to most turn-specific."""

    shared_core: str = ""
    agent_context: str = ""
    turn_context: str = ""

    def render(self) -> str:
        return "\n\n".join(
            part.strip()
            for part in (self.shared_core, self.agent_context, self.turn_context)
            if part and part.strip()
        ).strip()


def _row_content(doc: Dict) -> str:
    return (doc.get("content") or "").strip()


# ── Role-directive injection (grep ROLE-DIRECTIVE-INJECT; was PREMIUM-DIRECTIVE-INJECT) ──
# Roster-role prompt directives: the agent's admin authors PER-AGENT prompt
# directives — one per fixed roster role (standard / premium / image_in /
# image_out / system / voice) plus one per custom roster slot, stored in the
# agent's own metadata['llm_config']['model_directives'] (app/admin/settings.py
# get_agent_model_directives; written via the agent-admin-gated PUT /agents).
# The directive for the session's ACTIVE slot rides into the assembled system
# prompt exactly like a loaded skill — triggered by the session's model
# CLASSIFICATION (the roster slot the picker / mode selector wrote), never by
# parsing a model name. Custom slots each carry their own injection keyed by
# slot_ref ('custom:<position>').
#
# The premium role additionally keeps the legacy agent-authored "Model Upgrade
# Directive" slot (self-authored via edit_own_prompt): when authored, that
# agent-level content WINS over the admin's premium directive.
_PREMIUM_DIRECTIVE_SLOT = "Model Upgrade Directive"


async def _session_slot(
    session_id: Optional[str],
    user_id: Optional[str],
    agent_id: Optional[str],
) -> Optional[dict]:
    """The session's ACTIVE roster slot as {"role": ..., "slot_ref": ...} —
    whose admin directive should ride into this turn's system prompt (grep
    ROLE-DIRECTIVE-INJECT). Pure slot logic, delegated to
    app.admin.settings.session_active_slot (the same resolver family the
    footer picker uses): the model-id string is never read for this decision.
    None when no slot resolves (no roster) — no directive rides in."""
    if not session_id:
        return None
    try:
        from app.admin.settings import session_active_slot
        from app.db import get_db
        agent_rec = None
        if agent_id:
            try:
                agent_rec = await get_db().get_agent_by_id(agent_id)
            except Exception:  # noqa: BLE001
                agent_rec = None
        return await session_active_slot(user_id or "", agent_rec=agent_rec,
                                         session_id=session_id)
    except Exception:  # noqa: BLE001 — gate failure = no directive
        return None


async def _agent_directive(
    agent_id: Optional[str],
    role: Optional[str],
    slot_ref: Optional[str] = None,
) -> str:
    """This agent's admin-authored directive for a roster slot ('' when
    unset). Custom slots read model_directives.custom[slot_ref] — so custom 1,
    custom 2, ... each carry their own injection; fixed roles read their key."""
    if not role or not agent_id:
        return ""
    try:
        from app.admin.settings import get_agent_model_directives
        d = await get_agent_model_directives(agent_id)
    except Exception:  # noqa: BLE001
        return ""
    if role == "custom":
        custom = d.get("custom") or {}
        return str(custom.get(slot_ref or "") or "").strip()
    return str(d.get(role) or "").strip()


async def _premium_directive_content(
    docs: List[Dict],
    agent_id: Optional[str],
    user_id: Optional[str],
) -> str:
    """The directive slot's content — from the caller's resolved docs when the
    slot survived filtering, else a targeted DB read. Empty when not authored."""
    for doc in docs:
        if (doc.get("context_type") or doc.get("slot_name")) == _PREMIUM_DIRECTIVE_SLOT:
            return _row_content(doc)
    if not agent_id:
        return ""
    try:
        from app.db import get_db
        slots = await get_db().resolve_prompts(agent_id, user_id=user_id)
        for s in slots:
            if s.get("slot_name") == _PREMIUM_DIRECTIVE_SLOT:
                return (s.get("content") or "").strip()
    except Exception:  # noqa: BLE001
        pass
    return ""


async def _session_btw_directive(
    session_id: Optional[str], user_id: Optional[str],
) -> str:
    """Return the durable sidebar-fork overlay for a BTW session.

    The relationship lives on the session rather than in a visible user row so
    it survives compaction/reopening and is injected on every turn. Reads are
    ownership-scoped and best-effort; malformed legacy metadata is ignored.
    """
    if not session_id or not user_id:
        return ""

    async def _read() -> Optional[str]:
        from app.db import get_db
        conn = get_db()._get_conn()
        try:
            row = conn.execute(
                "SELECT metadata FROM sessions WHERE id = ? AND user_id = ?",
                (session_id, user_id),
            ).fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    try:
        raw = await db_offload(_read)
        metadata = json.loads(raw) if raw else {}
        btw = metadata.get("btw") if isinstance(metadata, dict) else None
        if not isinstance(btw, dict):
            return ""
        source_id = str(btw.get("source_session_id") or "")[:128]
        if not source_id:
            return ""
        source_title = str(btw.get("source_title") or "New Session")[:120]
        source_interaction = str(btw.get("source_interaction_id") or "")[:128]
        try:
            source_seq = max(0, int(btw.get("source_session_seq") or 0))
        except (TypeError, ValueError):
            source_seq = 0
    except Exception:  # noqa: BLE001 — metadata overlay is best-effort
        return ""

    return (
        "# [BTW SIDEBAR SESSION]\n"
        "This is an independent sidebar fork for discussing, monitoring, "
        "auditing, verifying, or troubleshooting another conversation.\n"
        f"Source session: {source_title} (id: {source_id}).\n"
        f"Snapshot point: session_seq {source_seq}"
        + (f", interaction {source_interaction}.\n" if source_interaction else ".\n")
        + "The transcript in this session is a fixed snapshot. Later source "
        "activity is not mirrored here, and actions taken here do not change "
        "the source session. When the user's request depends on current source "
        "state, inspect that exact owned session with session_search(session_id=..., "
        "after_session_seq=...) before answering. Inspect only when the BTW "
        "conversation calls for freshness; do not poll proactively. Clearly "
        "distinguish snapshot evidence from live inspection results."
    )


async def build_system_prompt(
    docs: List[Dict],
    brain_context: Optional[str] = None,
    user_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    session_id: Optional[str] = None,
    agent_rec: Optional[Dict] = None,
) -> str:
    """Backward-compatible flattened prompt builder."""
    return (
        await build_system_prompt_parts(
            docs,
            brain_context=brain_context,
            user_id=user_id,
            agent_id=agent_id,
            session_id=session_id,
            agent_rec=agent_rec,
        )
    ).render()


async def build_system_prompt_parts(
    docs: List[Dict],
    brain_context: Optional[str] = None,
    user_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    session_id: Optional[str] = None,
    agent_rec: Optional[Dict] = None,
) -> SystemPromptParts:
    """Assemble the final system prompt from resolved slot docs + brain context.

    `docs` is the resolved slot list returned by `_docs_for_caller()` —
    each entry is already a fully merged admin-base + user-override slot,
    in the admin's chosen order. We concatenate the contents as-is so admins
    keep full control over headings and formatting.

    `session_id`, when provided, enables the role-directive injection
    (grep ROLE-DIRECTIVE-INJECT): the admin's directive for the session's
    ACTIVE roster role is appended (the premium role also keeps the legacy
    agent-authored "Model Upgrade Directive" slot, which wins when authored) —
    mirroring how a loaded skill renders, but triggered by the model
    classification instead of a load_skill call.
    """
    # The shared default agent is also used by public guests. Its admin-authored
    # persona, bootstrap slots, directives, and learned prompt
    # fragments may describe privileged capabilities. Anonymous callers receive
    # a deliberately self-contained chat persona instead, so forbidden abilities
    # are absent from the model context rather than merely denied at execution.
    if _is_anonymous_user(user_id) and (not agent_id or agent_id == "shared_default"):
        return SystemPromptParts(agent_context=_ANONYMOUS_AGENT_PROMPT)
    if agent_id:
        try:
            from app.agent.profiles import resolve_member, VISITOR
            _visitor_member = await resolve_member(agent_id, user_id, auto_link_app=False)
            if (((_visitor_member or {}).get("profile") or {}).get("slug") == VISITOR):
                _visitor_overlay = str((((_visitor_member.get("profile") or {}).get("policy") or {}).get("prompt_overlay") or "")).strip()
                return SystemPromptParts(agent_context="\n\n".join(
                    part for part in (_ANONYMOUS_AGENT_PROMPT, _visitor_overlay) if part
                ))
        except Exception:
            pass

    shared_sections: List[str] = []
    agent_sections: List[str] = []
    turn_sections: List[str] = []

    # App-global baseline — admin-only CRITICAL instructions prepended verbatim
    # ahead of every agent's own slots (fleet agents and spawned clones alike).
    # This is the one piece of identity a from-scratch clone always inherits.
    try:
        from app.admin.settings import get_global_system_prompt
        _baseline = get_global_system_prompt()
        if _baseline:
            shared_sections.append(_baseline)
    except Exception:  # noqa: BLE001 — baseline is best-effort, never fatal
        pass

    # Role-directive gate: decide once which roster slot's directive rides in
    # this turn (the session's model classification — never a model name).
    # (grep ROLE-DIRECTIVE-INJECT)
    active_slot = None
    if session_id:
        try:
            active_slot = await _session_slot(session_id, user_id, agent_id)
        except Exception:  # noqa: BLE001 — gate failure = no directive
            active_slot = None
    premium_active = bool(active_slot and active_slot.get("role") == "premium")

    doc_sections = 0
    directive_content = ""
    for doc in docs:
        slot_name = doc.get("context_type") or doc.get("slot_name") or ""
        # The `__skills__` slot holds skills as raw JSON — never dump it into the
        # prompt; it's rendered separately by append_skills_section().
        if slot_name == "__skills__":
            continue
        if slot_name == _PREMIUM_DIRECTIVE_SLOT:
            # Never unconditional — handled by the premium gate below.
            if premium_active:
                directive_content = _row_content(doc)
            continue
        content = _row_content(doc)
        if content:
            agent_sections.append(content)
            doc_sections += 1

    if active_slot:
        role = active_slot.get("role")
        slot_ref = active_slot.get("slot_ref")
        if role == "premium":
            # Legacy agent-authored slot wins; else the admin's per-agent
            # premium directive.
            if not directive_content:
                directive_content = await _premium_directive_content(docs, agent_id, user_id)
            if not directive_content:
                directive_content = await _agent_directive(agent_id, role, slot_ref)
        else:
            directive_content = await _agent_directive(agent_id, role, slot_ref)
        if directive_content:
            agent_sections.append(directive_content)

    # ── Tool-result update directive (grep OFF-TASK-HIDING) ──
    # When off-task tool-output hiding is on (see app/agent/session_history.py),
    # tool results from closed tasks appear as placeholders in the model
    # context. This directive tells the agent to narrate tool results / next
    # steps / reasoning after every call and how to handle a hidden result.
    # Text: the `updates_directive` knob on the Task Grouping app function
    # (App Settings ▸ App Functions) wins; the `tool_result_updates_directive`
    # runtime fragment is the fallback. Rides in the agent block so the
    # provider prompt-cache prefix stays stable. Best-effort — never breaks.
    try:
        from app.agent.session_history import off_task_hiding_enabled as _off_task_on
        if _off_task_on():
            _dir = ""
            try:
                from app.admin.ability_config import get_ability_config
                _dir = (get_ability_config("task_grouping").get("updates_directive") or "").strip()
            except Exception:  # pragma: no cover - config read is best-effort
                pass
            if not _dir:
                _dir = (get_prompt_fragments().get("tool_result_updates_directive") or "").strip()
            if _dir:
                agent_sections.append(_dir)
    except Exception:  # pragma: no cover - directive is best-effort, never fatal
        pass

    fr = get_prompt_fragments()

    # If no docs supplied a tools list and we have no caller, inject the
    # fallback tool list so anonymous endpoints still see something useful.
    # Keyed on DOC content only — the global baseline alone must not suppress it.
    has_any_content = doc_sections > 0
    if not has_any_content and not user_id:
        fallback = format_tool_subheadings_markdown(fr.get("fallback_tools") or "")
        if fallback:
            agent_sections.append(fallback)

    if agent_id:
        try:
            from app.agent.profiles import resolve_member
            _member = await resolve_member(agent_id, user_id)
            _overlay = str((((_member or {}).get("profile") or {}).get("policy") or {}).get("prompt_overlay") or "").strip()
            if _overlay:
                agent_sections.append(_overlay)
        except Exception:  # profile prompt overlay is best-effort
            pass

    if session_id:
        btw_directive = await _session_btw_directive(session_id, user_id)
        if btw_directive:
            turn_sections.append(btw_directive)

    if brain_context:
        turn_sections.append("# [BRAIN CONTEXT]")
        intro = (fr.get("brain_context_intro") or "").strip()
        if intro:
            turn_sections.append(intro + "\n")
        turn_sections.append(brain_context)

    def _join(sections: List[str]) -> str:
        return "\n\n".join(s for s in sections if s and s.strip()).strip()

    return SystemPromptParts(
        shared_core=_join(shared_sections),
        agent_context=_join(agent_sections),
        turn_context=_join(turn_sections),
    )


async def append_skills_section(
    system_prompt: str,
    agent: Optional[Dict] = None,
    session_id: Optional[str] = None,
    caller_user_id: Optional[str] = None,
) -> str:
    """Append the `# [SKILLS]` block to a built system prompt.

    Reads the agent's skills (from its `__skills__` prompt slot) and the
    session's active (loaded) skill list, then renders name + description for
    every skill, plus the body for always-on/loaded skills and a placeholder for
    selectable ones. A no-op when the agent has no skills.

    `caller_user_id` is the live chatter; when given, ability-bundled skills for
    abilities the caller's access level can't reach (the per-agent "Available to"
    gate) are stripped, so the agent isn't told about tools it doesn't have this
    turn. The actual tool boundary is enforced in `loader.load_tools`.
    """
    # Authored skills are not necessarily tagged with an ability id and can
    # contain privileged operating instructions. Guests get no skill catalog;
    # their runtime tool surface is independently reduced in tools.loader.
    if _anonymous_showcase(caller_user_id, agent):
        return system_prompt

    from app.agent.skills import format_skills_section

    agent_id = (agent or {}).get("id")
    if not agent_id:
        return system_prompt

    try:
        from app.db import get_db
        db = get_db()
        # Offloaded: on remote Postgres this is a ~150ms round-trip; keeping it off
        # the event loop is part of stopping the build-prompt stall (see below).
        skills = await db_offload(lambda: db.get_agent_skills(agent_id, user_id=None))
    except Exception as e:
        logger.debug("Could not load skills for agent %s: %s", agent_id, e)
        skills = []

    # Merge in skills contributed by the agent's enabled abilities (drop-in
    # FEATURE.skill). Handle-keyed so they can't collide with authored skills.
    try:
        from app.agent.ability_skills import collect_ability_skills
        skills = list(skills or []) + await collect_ability_skills(agent_id, user_id=None)
    except Exception as e:
        logger.debug("Could not collect ability skills for agent %s: %s", agent_id, e)

    # Hide an ability-bundled skill while its ability is discoverable-and-unloaded
    # (revealed by load_ability). Authored skills and integration-module skills
    # carry no `ability_id`, so they are unaffected.
    try:
        from app.tools.tool_modes import ability_is_revealed, resolve_skill_mode
        from app.db import get_db as _gd
        _db = _gd()
        # Concurrent + offloaded: these five reads are independent and each a
        # ~150ms remote round-trip. Running them serially on the event loop was a
        # big chunk of the build-prompt stall (and froze the loop for other work).
        # Gather them off the loop in one shot so the whole cluster costs ~one
        # round-trip instead of five.
        async def _sess(_fn):
            return await db_offload(lambda: _fn(session_id)) if session_id else []
        _amodes, _smodes, _adefault, _aactive, _asuppressed = await asyncio.gather(
            db_offload(lambda: _db.get_agent_ability_modes(agent_id)),
            db_offload(lambda: _db.get_agent_skill_modes(agent_id)),
            db_offload(lambda: _db.get_agent_discovery_default(agent_id)),
            _sess(_db.get_session_active_abilities),
            _sess(_db.get_session_suppressed_abilities),
        )
        # Per-agent skill visibility override: visible = body shown every turn,
        # discoverable = load on demand. Applies to ability-bundled skills only
        # (they carry `ability_id`); the descriptor mode is the default.
        for s in skills:
            aid = s.get("ability_id")
            if aid:
                s["mode"] = resolve_skill_mode(
                    s.get("skill_key") or f"{aid}:core", _smodes, s.get("mode")
                )
        # Hide an ability-bundled skill while its ability is discoverable-and-unloaded.
        skills = [
            s for s in skills
            if not s.get("ability_id")
            or ability_is_revealed(s["ability_id"], _amodes, _aactive, _asuppressed,
                                   ability_default=_adefault)
        ]
    except Exception as e:
        logger.debug("ability-skill visibility filter failed for %s: %s", agent_id, e)

    # Caller-access gate: strip ability-bundled skills for abilities the live
    # caller's level can't reach (per-agent "Available to"). Mirrors the tool
    # boundary in loader.load_tools so the prompt never advertises tools the
    # caller doesn't have. Only ability-tagged skills are affected; authored and
    # integration-module skills carry no `ability_id`. Skipped when no caller is
    # threaded (local/admin contexts) — then nothing is gated.
    if caller_user_id:
        try:
            from app.agent.ability_access import filter_abilities_for_caller
            ability_ids = {s.get("ability_id") for s in skills if s.get("ability_id")}
            if ability_ids:
                allowed = await filter_abilities_for_caller(agent_id, ability_ids, caller_user_id)
                skills = [
                    s for s in skills
                    if not s.get("ability_id") or s["ability_id"] in allowed
                ]
                # Added directory skills can impose a stricter access rung than
                # their owning ability. Keep them out of both the skill catalog
                # and any always-on body for callers below that rung.
                from app.agent.ability_access import caller_access_rank
                from app.tools.tool_modes import ACCESS_RANK
                rank = await caller_access_rank(_db, caller_user_id, agent_id)
                skills = [
                    s for s in skills
                    if not s.get("ability_id")
                    or ACCESS_RANK.get(str(s.get("minimum_access") or "everyone"), 1) <= rank
                ]
        except Exception as e:
            logger.debug("ability-skill access filter failed for %s: %s", agent_id, e)

    if not skills:
        return system_prompt

    active: List[str] = []
    if session_id:
        try:
            active = await db_offload(lambda: db.get_session_active_skills(session_id))
        except Exception as e:
            logger.debug("Could not load active skills for session %s: %s", session_id, e)

    section = format_skills_section(skills, active)
    if not section:
        return system_prompt
    return f"{system_prompt}\n\n{section}".strip() if system_prompt else section


def format_attachments_for_prompt(attachments: List[Dict]) -> str:
    """
    Format attached files into a concise summary for the system prompt.

    Args:
        attachments: List of attachment dicts with original_name, mime_type, size_bytes, id

    Returns:
        Formatted string the agent can read to know what user attached.
    """
    if not attachments:
        return ""
    lines = ["# [USER ATTACHMENTS]"]
    lines.append("The user attached the following files to this message:")
    lines.append("")
    for att in attachments:
        name = att.get("original_name", "unknown")
        mime = att.get("mime_type", "application/octet-stream")
        size_kb = att.get("size_bytes", 0) / 1024
        size_str = f"{size_kb:.1f}KB" if size_kb < 1024 else f"{size_kb/1024:.1f}MB"
        att_id = att.get("id", "?")
        # Categorize for agent clarity
        if mime.startswith("image/"):
            icon = "🖼"
        elif mime.startswith("audio/"):
            icon = "🎤"
        elif mime.startswith("video/"):
            icon = "🎬"
        else:
            icon = "📎"
        lines.append(f"  - {icon} **{name}** ({mime}, {size_str})")
        lines.append(f"    attachment_id: `{att_id}` — use `read_attachment` tool to process")
    lines.append("")
    lines.append("Use the `read_attachment` tool to read content from attached files.")
    lines.append(
        "Image attachments are inlined directly with this user message and "
        "are visible to vision-capable models — no need to call `read_attachment` "
        "for them on this turn."
    )
    return "\n".join(lines)


async def _attachment_to_image_data_url(att: Dict) -> Optional[str]:
    """Read an image attachment's bytes and return a base64 data URL, or None when
    the attachment isn't an inlinable image or can't be read. Shared by
    `build_user_message_content` (native inline) and `describe_image_attachment`
    (the vision sub-step) so the mime/size/storage guards live in one place."""
    mime = (att.get("mime_type") or "").lower()
    if mime not in _VISION_INLINE_MIMES:
        return None
    size = att.get("size_bytes") or 0
    if size and size > _VISION_INLINE_MAX_BYTES:
        logger.info(
            "Skipping inline of attachment %s: %d bytes exceeds %d limit",
            att.get("id"), size, _VISION_INLINE_MAX_BYTES,
        )
        return None
    storage_provider = att.get("storage_provider") or "local"
    if storage_provider == "browser":
        # Bytes live in the user's browser IndexedDB; the server can't read them.
        return None
    storage_path = att.get("storage_path")
    if not storage_path:
        return None
    try:
        from app.db.attachments import read_file
        file_bytes = await read_file(storage_path, storage_provider=storage_provider)
    except Exception as e:
        logger.warning("read_file failed for attachment %s: %s", att.get("id"), e)
        return None
    if not file_bytes:
        return None
    b64 = base64.b64encode(file_bytes).decode("ascii")
    return f"data:{mime};base64,{b64}"


async def build_user_message_content(
    user_text: str,
    attachment_docs: Optional[List[Dict]] = None,
) -> Union[str, List[Dict[str, Any]]]:
    """Build the LLM `content` field for the user message.

    Inlines image attachments as `image_url` parts (data URLs) so vision-capable
    models can see them directly. Non-image attachments are left to the
    `read_attachment` tool. Returns a plain string when there are no inlinable
    images, preserving the original message shape.
    """
    if not attachment_docs:
        return user_text

    image_parts: List[Dict[str, Any]] = []
    for att in attachment_docs:
        url = await _attachment_to_image_data_url(att)
        if url:
            image_parts.append({"type": "image_url", "image_url": {"url": url}})

    if not image_parts:
        return user_text

    text_part = {"type": "text", "text": user_text or ""}
    return [text_part, *image_parts]


async def describe_image_attachment(
    att: Dict,
    describer_cfg: Dict[str, Any],
    user_text_hint: str = "",
    system_prompt: Optional[str] = None,
    instruction: Optional[str] = None,
    error_sink: Optional[List[str]] = None,
) -> Optional[str]:
    """Call a configured vision model ONCE to describe a single image attachment.

    Returns the description text, or None when the image can't be read, the
    describer config is incomplete, or the call fails. This is a single
    in-process completion — no session, no agent loop, no HTTP self-call.
    Generic by design: future voice/video describers follow the same shape.

    ``system_prompt`` / ``instruction`` let the attachment type-router supply a
    context-tailored prompt (from the ability's companion JSON) so the description
    is shaped to what the conversation needs; both fall back to a generic default.

    ``error_sink``: optional list; on failure the real error string is appended
    (e.g. the provider's "no remaining credits" 400) so callers can surface WHY
    the description failed instead of only seeing None.
    """
    sys_line = (system_prompt or "").strip() or (
        "You are an image-description assistant. Describe the attached image in "
        "thorough, faithful detail so that another AI model which cannot see the "
        "image can fully understand it. Include any visible text verbatim, plus "
        "layout, objects, people, colours, charts and notable details. Do not add "
        "commentary or speculation beyond what is visible."
    )
    user_lines: List[str] = []
    hint = (user_text_hint or "").strip()
    if hint:
        user_lines.append(f"The user's message accompanying this image: {hint}")
    user_lines.append((instruction or "").strip() or "Describe this image in detail.")

    # Use the single bounded-completion route so API-key and ChatGPT OAuth models
    # receive the same image payload and provider-specific transport handling.
    from app.agent.provider_completion import ask_model
    return await ask_model(
        describer_cfg,
        sys_line,
        "\n\n".join(user_lines),
        attachments=[att],
        temperature=0.0,
        max_tokens=768,
        timeout=30.0,
        error_sink=error_sink,
    )



