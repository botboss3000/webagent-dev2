"""Chat endpoint for WebAgent."""

import asyncio
import json
import logging
import os
import re
import sqlite3
import time
import uuid
from typing import List, Any, Dict, Optional
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from app.models.schemas import ChatRequest, ChatResponse
from app.db import get_db
from app.db.turn_cache import with_turn_cache, turn_cache_scope
from app.db.offload import db_offload
from app.agent import turn_prewarm
from app.agent import session_gate
from app.agent.prompts import (
    build_system_prompt,
    build_system_prompt_parts,
    append_skills_section,
    build_user_message_content,
    format_attachments_for_prompt,
    CONTEXT_SECTION_TYPES,
)

from app.agent.loop import run_agent_loop_buffered, stream_agent_events
from app.agent.loop_executor import LoopConfig
from app.agent.session_history import build_openai_history_from_session
from app.agent.run_buffer import get_registry as get_run_buffer_registry
from app.agent.run_manager import get_run_manager
from app.agent.turn_reservations import stable_key as stable_turn_key
from app.optimizer.runner import run_optimizer_async
from app.agent import trigger_index
from plugins.billing.enforcement import check_access as billing_check_access

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/chat", tags=["chat"])


def _is_codex_portal_agent(agent: Optional[Dict[str, Any]]) -> bool:
    if not agent:
        return False
    metadata = agent.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except Exception:
            return False
    engine = str(agent.get("engine") or (metadata.get("engine") if isinstance(metadata, dict) else "") or "")
    if engine != "codex":
        return False
    codex_cfg = metadata.get("codex_code") if isinstance(metadata, dict) else None
    return isinstance(codex_cfg, dict) and codex_cfg.get("context_mode") == "codex_portal"


class ChatComponentRequest(BaseModel):
    component: Dict[str, Any]


class ChatComponentActionRequest(BaseModel):
    action: str
    payload: Dict[str, Any] = {}


def _component_caller(request: Request) -> str:
    from app.admin.integrations import resolve_user_id, ANONYMOUS_KEY
    uid = resolve_user_id(request.headers.get("Authorization", ""), request.query_params.get("token", ""))
    if not uid or uid == ANONYMOUS_KEY:
        raise HTTPException(status_code=401, detail="Sign in to use chat components.")
    return uid


@router.get("/components")
async def get_chat_components(request: Request, session_id: str = Query(...)):
    from app.chat_components import list_components
    uid = _component_caller(request)
    try:
        await get_db().assert_session_owned(uid, session_id)
    except PermissionError:
        raise HTTPException(status_code=403, detail="This session is not yours.")
    return {"components": await list_components(uid, session_id)}


@router.post("/components")
async def create_chat_component(request: Request, body: ChatComponentRequest, session_id: str = Query(...)):
    from app.chat_components import save_component
    uid = _component_caller(request)
    try:
        await get_db().assert_session_owned(uid, session_id)
        return {"component": await save_component(uid, session_id, body.component)}
    except PermissionError:
        raise HTTPException(status_code=403, detail="This session is not yours.")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.post("/components/{component_id}/actions")
async def act_on_chat_component(request: Request, component_id: str, body: ChatComponentActionRequest,
                                session_id: str = Query(...)):
    from app.chat_components import apply_action
    uid = _component_caller(request)
    try:
        await get_db().assert_session_owned(uid, session_id)
        return {"component": await apply_action(uid, session_id, component_id, body.action, body.payload)}
    except PermissionError:
        raise HTTPException(status_code=403, detail="This session is not yours.")
    except LookupError:
        raise HTTPException(status_code=404, detail="Component not found.")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

@router.delete("/components/{component_id}")
async def delete_chat_component(request: Request, component_id: str, session_id: str = Query(...)):
    from app.chat_components import delete_component
    uid = _component_caller(request)
    try:
        await get_db().assert_session_owned(uid, session_id)
        await delete_component(uid, session_id, component_id)
        return {"ok": True}
    except PermissionError:
        raise HTTPException(status_code=403, detail="This session is not yours.")
    except LookupError:
        raise HTTPException(status_code=404, detail="Component not found.")


class ChatComponentUiRequest(BaseModel):
    ui: Dict[str, Any]


@router.get("/components/ui-state")
async def get_chat_component_ui_state(request: Request, session_id: str = Query(...)):
    """Return the session's saved agent-panel layout state (minimized, height,
    width, expanded, active tab) so the panel restores it on reopen."""
    from app.chat_components import get_panel_ui
    uid = _component_caller(request)
    try:
        await get_db().assert_session_owned(uid, session_id)
    except PermissionError:
        raise HTTPException(status_code=403, detail="This session is not yours.")
    return {"ui": await get_panel_ui(uid, session_id)}


@router.put("/components/ui-state")
async def put_chat_component_ui_state(request: Request, body: ChatComponentUiRequest,
                                      session_id: str = Query(...)):
    """Persist the session's agent-panel layout state (minimized, height, width,
    expanded, active tab) so a later reopen restores the exact panel."""
    from app.chat_components import save_panel_ui
    uid = _component_caller(request)
    try:
        await get_db().assert_session_owned(uid, session_id)
        return {"ui": await save_panel_ui(uid, session_id, body.ui)}
    except PermissionError:
        raise HTTPException(status_code=403, detail="This session is not yours.")


# ── Fire-and-forget background tasks ──
# asyncio only keeps a WEAK reference to a bare ``create_task`` result, so a task
# whose handle isn't stored can be garbage-collected before it runs — silently.
# Keep a strong reference for the task's lifetime and drop it (logging any
# exception) when it finishes, so post-turn work (e.g. the Session Namer turn
# hook) actually executes instead of vanishing.
_BG_TASKS: set = set()


def _spawn_bg(coro, *, label: str = "bg") -> None:
    task = asyncio.ensure_future(coro)
    _BG_TASKS.add(task)

    def _done(t: "asyncio.Task") -> None:
        _BG_TASKS.discard(t)
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            logger.warning("background task %r failed: %s", label, exc, exc_info=exc)

    task.add_done_callback(_done)


def _cancel_if_pending(task) -> None:
    """Cancel a still-running helper task so it isn't orphaned when a turn errors
    out before its result is awaited. Without this, a fired ``memory_search`` /
    read-fan-out keeps running detached against the shared main-loop client after
    the turn already 500'd — logging "Task exception was never retrieved" and
    burning a stray round-trip. A no-op if the task is None or already done."""
    try:
        if task is not None and not task.done():
            task.cancel()
    except Exception:
        pass


# ── Concurrent pure-DB reads ──
# On a remote Postgres every DB call is a network round-trip of
# tens-to-hundreds of ms. The storage layer is synchronous psycopg wearing an
# ``async def`` hat, so a plain ``asyncio.gather`` of these coroutines does NOT
# overlap them — each one blocks the single event-loop thread for its whole
# round-trip, so they still run back-to-back.
#
# This helper drives each coroutine to completion in its OWN worker thread. The
# psycopg connection pool hands each thread a separate connection and releases
# the GIL while waiting on the socket, so the round-trips genuinely overlap —
# turning a stack of N serial waits into roughly one. The same holds for the
# SQLite backend (``_get_conn`` opens a fresh, thread-local connection per call).
#
# HARD CONSTRAINT: every factory must return a coroutine that does ONLY blocking
# DB work. It must NOT touch a main-loop-bound async client — notably the
# embedding client (memory recall) or the chat LLM client (history compaction) —
# because those are cached globally and bound to the main event loop; first-
# touching them from a worker loop corrupts them for the rest of the process.
# That is why memory_search and build_openai_history_from_session stay on the
# main loop and are deliberately NOT passed here.
async def _gather_db_concurrent(*factories):
    """Run independent, pure-DB coroutine factories truly concurrently.

    Each ``factory`` is a zero-arg callable returning a FRESH coroutine. Results
    come back in input order; a failure in one is returned in its slot as the
    exception instance (caller decides whether it's fatal), so one slow/failed
    read can't sink the batch.
    """
    if not factories:
        return []

    # Each factory runs on the PERSISTENT DB-worker pool (app/db/offload.py) so the
    # blocking round-trips TRULY overlap instead of serializing on the event-loop
    # thread, and the main loop stays free for the LLM stream. This uses db_offload
    # rather than the old ``asyncio.to_thread(lambda: asyncio.run(factory()))``: that
    # pattern spun up and tore down a BRAND-NEW event loop on every call and queued
    # behind unrelated work in the shared default executor — exactly the two costs
    # offload.py's docstring documents as the reason the persistent pool exists.
    # db_offload copies the turn-cache ContextVar, so caching still works. ONLY
    # pure-DB reads belong here — never the embedding client or WS broadcaster.
    return await asyncio.gather(
        *(db_offload(f) for f in factories),
        return_exceptions=True,
    )


# ── Cross-message cache for the agent's tool set ──────────────────────────────
# load_tools makes dozens of DB round-trips and its result depends only on the
# agent's config + the caller (not on the message). We cache it per (agent, user)
# keyed by the agent's ``updated_at`` (any agent edit bumps it → cache miss →
# fresh load), with a TTL backstop for edits that don't touch the agent row. A
# shallow copy is returned so a turn's own dict mutations never corrupt the cache.
async def _cached_load_tools(user_id: str, agent_id: str,
                             template_id: Optional[str], version: Any):
    from app.tools.loader import load_tools
    if not agent_id or version is None:
        return await load_tools(user_id, agent_id=agent_id or "",
                                agent_template_id=template_id, gate_caller_access=True)
    from app.agent import static_cache
    key = f"tools:{agent_id}:{user_id}:{template_id or ''}"
    result = await static_cache.get_or_compute(
        key, version,
        lambda: load_tools(user_id, agent_id=agent_id,
                           agent_template_id=template_id, gate_caller_access=True),
    )
    return dict(result) if isinstance(result, dict) else result


# ── Hot-path stopwatch (temporary latency diagnostic) ─────────────────────────
# DIAGNOSTIC: marks how long each stage of a single chat turn takes and writes
# the marks to the diagnostics log (category="perf", level INFO → durable in
# logs.db). Lets us see exactly which stage eats the wall-clock time on a slow
# turn — prep vs. DB reads vs. memory recall vs. the LLM provider's first token.
# Each mark records the delta since the previous mark and the total since the
# turn started. Cheap and never raises. REMOVE-WHEN: latency diagnosis is done.
_PERF_ENABLED = (os.environ.get("WEBAGENT_PERF_TRACE", "1") or "1").strip().lower() not in ("0", "false", "no", "off")


class _PerfTimer:
    def __init__(self, label: str, *, session_id=None, turn_id=None,
                 user_id=None, agent_id=None) -> None:
        self.label = label
        self.session_id = session_id
        self.turn_id = turn_id
        self.user_id = user_id
        self.agent_id = agent_id
        self._t0 = time.monotonic()
        self._last = self._t0

    def mark(self, name: str, **extra) -> None:
        if not _PERF_ENABLED:
            return
        now = time.monotonic()
        total_ms = int((now - self._t0) * 1000)
        delta_ms = int((now - self._last) * 1000)
        self._last = now
        try:
            from app.agent.diagnostics import record
            detail = {"phase": self.label, "mark": name,
                      "delta_ms": delta_ms, "total_ms": total_ms}
            if extra:
                detail.update(extra)
            record("info", "perf",
                   f"[perf] {self.label}:{name} +{delta_ms}ms (t={total_ms}ms)",
                   source="chat.hotpath", detail=detail,
                   session_id=self.session_id, turn_id=self.turn_id,
                   user_id=self.user_id, agent_id=self.agent_id)
        except Exception:
            pass


# ── Memory skip gate ──
# Skip memory_search for trivial messages (greetings, affirmations, commands).
_SKIP_MEMORY_PATTERN = re.compile(
    r"^(hi|hello|hey|sup|yo|thanks|thank you|ok|okay|got it|cool|sure|"
    r"yes|no|go ahead|keep going|continue|next|and\??|more|elaborate|"
    r"good (morning|afternoon|evening)|what'?s up|how are you|how'?s it going|"
    r"check my email|list my files|show my messages|"
    r"read my email|get my mail|open my inbox)[\s!?.]*$",
    re.IGNORECASE,
)


def _should_skip_memory(message: str) -> bool:
    """Return True if message is trivial and doesn't need brain context."""
    stripped = (message or "").strip()
    return bool(not stripped or _SKIP_MEMORY_PATTERN.match(stripped))


def _should_skip_vector(message: str) -> bool:
    """Return True for short messages that should use keyword-only memory search.

    These still get a memory lookup (unlike _should_skip_memory), but skip the
    remote query-embedding round-trip and rely on the porter-stemmed FTS index.
    Short follow-ups ("who offered?", "what's the floor?", "and the bike?") match
    fine on keywords, so paying for an embedding just adds latency to the turn.
    Longer, more semantic prompts keep the full hybrid (FTS + vector) search.
    """
    stripped = (message or "").strip()
    return len(stripped.split()) <= 4


def _session_title_from_message(message: str, max_words: int = 6) -> str:
    """Extract a concise 3–6 word session title from the user's first message.

    The ``"new: "`` prefix marks sessions that haven't been renamed by the
    Session Namer ability yet — once the namer runs, it overwrites the title
    entirely and the prefix drops.
    """
    words = (message or "").strip().split()
    if not words:
        return "New Session"
    title = " ".join(words[:max_words]).rstrip(".,!?;: ")
    title = (title[:60] or "New Session")
    return f"New: {title}"


async def _enforce_agent_access_policy(db, agent: dict, user_id: str) -> None:
    """Raise 403 if user is not allowed to chat with this agent under its user_mode policy."""
    # Authorization must never resolve from the local cache: read the access mode
    # straight from the authority, not the (Stage-3 possibly locally-served) agent
    # dict. db._get_conn() delegates to the remote authority under the hybrid
    # backend — the same authoritative-read idiom used for the tier check below.
    mode = (agent or {}).get("user_mode")
    agent_id = (agent or {}).get("id")
    mode = mode or "anonymous"
    from app.agent.profiles import resolve_member, VISITOR
    member = await resolve_member(agent_id, user_id)
    if member and member.get("is_agent_admin"):
        return
    profile_slug = ((member or {}).get("profile") or {}).get("slug")
    if profile_slug and profile_slug != VISITOR:
        return
    if mode == "anonymous" and profile_slug == VISITOR:
        return
    if mode == "anonymous" and not member and str(user_id or "").startswith("anon_"):
        return
    if mode == "register":
        raise HTTPException(status_code=403, detail="This agent requires agent membership.")
    if mode == "authorized":
        raise HTTPException(status_code=403, detail="This agent requires administrator-approved membership.")
    raise HTTPException(status_code=403, detail="This agent is not available to this visitor.")


async def _enforce_billing_access(db, agent: dict, user_id: str, message: str = "") -> None:
    """Gate chat on billing state — credits, subscription, or trial.

    Agents with no billing config (or strategy='free', or where the user is
    exempt) pass through. Otherwise we raise HTTP 402 with a structured
    reason so the frontend can show the right paywall."""
    from app.agent.profiles import resolve_member, VISITOR, consume_profile_turn, billing_subject
    member = await resolve_member((agent or {}).get("id") or "", user_id)
    is_visitor = ((member or {}).get("profile") or {}).get("slug") == VISITOR
    if member:
        try:
            await consume_profile_turn((agent or {}).get("id") or "", member, message)
        except PermissionError as exc:
            raise HTTPException(status_code=429, detail={"code": "agent_profile_limit", "message": str(exc)}) from exc
    if is_visitor or str(user_id or "").startswith("anon_"):
        # The public agent sponsor is the payer. Reserve its configured
        # allowance before any model work; never evaluate the visitor wallet.
        from app.agent.public_policy import consume_public_turn_budget
        await consume_public_turn_budget(db, agent, user_id, message)
        return
    funding_mode = str(((((member or {}).get("profile") or {}).get("policy") or {}).get("funding") or {}).get("mode") or "member")
    if funding_mode == "free":
        return
    if funding_mode in {"agent", "sponsor"}:
        from app.agent.public_policy import consume_public_turn_budget
        await consume_public_turn_budget(db, agent, user_id, message)
        return
    billing_user_id = await billing_subject((agent or {}).get("id") or "", member) if member else user_id
    try:
        from app.db import get_app_db
        decision = await billing_check_access(agent, billing_user_id, get_app_db())
    except Exception as e:
        logger.warning("billing access check failed closed: %s", e)
        raise HTTPException(
            status_code=503,
            detail={
                "code": "billing_policy_unavailable",
                "message": "Billing policy is temporarily unavailable.",
            },
        ) from e
    if decision.allow:
        return
    raise HTTPException(status_code=402, detail=decision.to_dict())


async def _ensure_session(db, user_id: str, session_id: str, title: str = None) -> bool:
    """Create the session row if it doesn't exist yet, and update its title on
    first real message.

    Returns True when the session row did NOT exist and was just created — i.e.
    this is the very first message of a brand-new session. The front-end doesn't
    persist a session row until the first send (see session-init.js), so a
    missing row is a reliable "first turn, no history to load" signal that the
    caller uses to skip the history read + its "Loading history" pill step.
    """
    conn = db._get_conn()
    try:
        row = conn.execute(
            "SELECT title, user_id, status FROM sessions WHERE id=?",
            (session_id,),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        # Session doesn't exist — create it
        try:
            meta = {}
            try:
                from app.devices import identity as _identity
                meta["device"] = {"id": _identity.device_id(), "label": _identity.device_label()}
            except Exception:
                pass
            raw = db.get_raw_client()
            raw.table("sessions").insert({
                "id": session_id,
                "user_id": user_id,
                "title": title or "New Session",
                "pinned": 0,
                "metadata": json.dumps(meta),
            }).execute()
            logger.info(f"Created session {session_id[:12]} for user {user_id[:12]}")
            # Clean create of a row that didn't exist → genuinely the first turn.
            return True
        except Exception as create_err:
            # A failed insert means the row may have been created concurrently
            # (a race), so we can't assume there's no history — fall through and
            # let the caller take the normal history-loading path.
            logger.warning(f"Session creation failed (may already exist): {create_err}")
    elif row["user_id"] != user_id:
        raise PermissionError(
            f"Session {session_id} is not owned by user {user_id}"
        )
    elif title and row["title"] in (None, "New Session", session_id[:12]):
        # Session exists with placeholder title — update to first real message
        conn = db._get_conn()
        try:
            conn.execute("UPDATE sessions SET title=? WHERE id=?", (title, session_id))
            conn.commit()
        finally:
            conn.close()
    # Store the status so the caller can reject recycled sessions without any
    # auto-resurrection (a session in the bin must stay dead unless the user
    # explicitly restores it from the Sessions page).
    row_status = (row["status"] if row else None)
    return False


async def _require_chat_session_access(
    fastapi_request: Request,
    claimed_user_id: Optional[str],
    session_id: str,
) -> tuple[str, Any]:
    """Require a verified caller and ownership/participation in ``session_id``."""
    from app.auth.identity import assert_caller_is

    user_id = await assert_caller_is(fastapi_request, claimed_user_id)
    db = get_db()
    try:
        await db.assert_session_owned(user_id, session_id)
    except PermissionError:
        try:
            participant = await db.is_session_participant(
                session_id, user_id, "user"
            )
        except Exception:
            participant = False
        if not participant:
            raise HTTPException(status_code=404, detail="Session not found.")
    return user_id, db


async def _require_chat_agent_access(
    db: Any,
    agent_id: Optional[str],
    user_id: str,
) -> Optional[dict]:
    if not agent_id:
        return None
    agent = await db.fetch_agent_by_id_with_context(
        agent_id,
        CONTEXT_SECTION_TYPES,
        user_id=user_id,
    )
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found.")
    await _enforce_agent_access_policy(db, agent, user_id)
    return agent


# Canonical Ask/Plan/Auto, accepting the legacy Read/Write names (mirrors
# loop.py's _MODE_ALIASES) so saved DB values and the TUI bridge stay valid.
_CHAT_MODE_ALIASES = {'read': 'plan', 'write': 'ask', 'wkspc': 'wkspc'}


_CONTINUE_AUTHORIZATION_RE = re.compile(r"^\s*continue\s*[.!]?\s*$", re.IGNORECASE)
_WRITE_MODE_REQUEST_RE = re.compile(
    r"\b(?:switch\s+(?:to\s+)?auto|write-capable\s+(?:mode|authorization)|"
    r"proceed\s+autonomously|go\s+ahead\s+autonomously)\b",
    re.IGNORECASE,
)


def _continue_authorizes_auto(message: str, interactions: list[Any]) -> bool:
    """True when a standalone ``continue`` answers the latest mode-change ask.

    The reply is intentionally scoped to the immediately preceding assistant
    message. That makes "continue" a canonical authorization for the pending
    write-capable handoff without treating an ordinary conversational use of the
    word as consent to enable Auto mode.
    """
    if not _CONTINUE_AUTHORIZATION_RE.fullmatch(message or ""):
        return False
    for interaction in reversed(interactions):
        role = getattr(interaction, "role", None)
        if role == "system":
            continue
        if role != "assistant":
            return False
        return bool(_WRITE_MODE_REQUEST_RE.search(getattr(interaction, "content", "") or ""))
    return False


async def _record_session_execution_mode(db, session_id: str, raw_mode) -> None:
    """Stamp the mode each message actually runs in onto the session.

    The chat pill (Ask/Plan/Auto) is the user's per-message control, but it
    lives in the browser. Recording the mode server-side makes the SESSION the
    source of truth, so the pill can be restored correctly on any device / after
    a reload (db_viewer.get_session_messages returns it; the chat panel applies
    it on load) — closing the "pill says Ask but the run went Auto" desync. The
    agent's own set_execution_mode writes the same field, so last-run-wins stays
    correct. Best-effort: a failure here must never block the send.
    """
    try:
        from app.agent.execution_modes import normalize_mode_id
        raw = str(raw_mode or '').strip().lower()
        mode = _CHAT_MODE_ALIASES.get(raw) or normalize_mode_id(raw)
        await db.set_session_execution_mode(session_id, mode)
    except Exception as _mode_err:
        logger.debug("record session execution mode failed for %s: %s", session_id, _mode_err)


class InterruptRequest(BaseModel):
    session_id: str = Field(..., min_length=1, max_length=128)


class ResumeRequest(BaseModel):
    session_id: str = Field(..., min_length=1, max_length=128)

def _match_slash_command(message: str):
    """Match message against all slash_command triggers from the trigger index.

    Returns (trigger_key, arg, template_id) if matched, else None.
    trigger_key is e.g. '/optimize', arg is the text after the command.

    Also handles known aliases: /optimizer → /optimize.
    """
    stripped = (message or "").strip()
    if not stripped.startswith("/"):
        return None

    # ── /optimizer alias ──
    if stripped.lower().startswith("/optimizer"):
        arg = stripped[len("/optimizer"):].strip()
        return "/optimizer", arg, "optimizer"

    slash_cmds = trigger_index.get_slash_commands()
    for trigger_key, template_id in slash_cmds.items():
        # Require the command to be followed by whitespace or end-of-string, so a
        # longer word can't partial-match a shorter command (e.g. "/optimizer"
        # must NOT match "/optimize" and silently pass the trailing "r" as an arg).
        pattern = re.compile(
            r"^" + re.escape(trigger_key) + r"(?:\s+(.*))?$",
            re.IGNORECASE | re.DOTALL,
        )
        m = pattern.match(stripped)
        if m:
            return trigger_key, (m.group(1) or "").strip(), template_id
    return None


async def _handle_generic_slash_command(
    template_id: str,
    trigger_key: str,
    arg: str,
    user_id: str,
    session_id: str,
    channel: str,
    db,
) -> str:
    """Generic handler for slash commands that don't have a custom runner.

    Creates a new session bound to the matched agent template and returns
    a user-facing confirmation message.
    """
    new_sid = f"slash-{uuid.uuid4().hex[:12]}"
    try:
        raw = db.get_raw_client()
        raw.table("sessions").insert({
            "id": new_sid,
            "user_id": user_id,
            "title": f"{trigger_key} session",
            "metadata": json.dumps({"trigger_key": trigger_key, "arg": arg}),
        }).execute()
    except Exception as e:
        logger.warning("Could not create session for %s: %s", trigger_key, e)
        result = f"Could not start `{trigger_key}` — session creation failed."
        await _persist_system_interaction(db, user_id, session_id, result, "system:slash")
        return result

    result = (
        f"**{trigger_key}** session started.\n"
        f"Session ID: `{new_sid}`\n"
        + (f"Input: {arg}\n" if arg else "")
        + f"\nOpen the session to continue."
    )
    await _persist_system_interaction(db, user_id, session_id, result, "system:slash")
    return result


async def _handle_optimize_command(
    user_id: str,
    session_id: str,
    feedback: str,
    channel: str,
    db,
) -> str:
    """Run the optimizer against the user's current session.
    feedback is the text after the slash command (may be empty).
    Returns a user-facing message."""

    # Target THIS session — the one the user typed /optimize in — provided it is a
    # real chat (not itself an optimizer/worker/closer session). This is what the
    # user means by "optimize this conversation". Only when the command is run from
    # a non-real session (or an unknown id) do we fall back to the user's most
    # recent real session.
    def _is_real_session(sid: str) -> bool:
        return bool(sid) and not (
            sid.startswith('optimizer-') or sid.startswith('worker-') or sid.startswith('closer-')
        )

    target_session = ""
    try:
        from app.db import get_db as _get_db
        conn = _get_db()._get_conn()
        try:
            if _is_real_session(session_id):
                exists = conn.execute(
                    "SELECT 1 FROM sessions WHERE id=? LIMIT 1", (session_id,)
                ).fetchone()
                if exists:
                    target_session = session_id
            if not target_session:
                row = conn.execute(
                    "SELECT id FROM sessions WHERE user_id=? AND id NOT LIKE 'optimizer-%' "
                    "AND id NOT LIKE 'worker-%' AND id NOT LIKE 'closer-%' "
                    "ORDER BY created_at DESC LIMIT 1",
                    (user_id,)
                ).fetchone()
                target_session = row[0] if row else ""
        finally:
            conn.close()
    except Exception:
        target_session = session_id if _is_real_session(session_id) else ""

    if not target_session:
        result = "No chat session found to optimize. Send a few messages first, then try /optimize."
        await _persist_system_interaction(db, user_id, session_id, result, "system:optimizer")
        return result

    # Run optimizer inline (fast: no LLM calls, just prefilter + session setup)
    opt_sid = await run_optimizer_async(
        user_id=user_id,
        session_id=target_session,
        channel=channel,
        feedback=feedback,
        force=True,
    )

    msg = f"⚡ **Optimizer session opened!**\n"
    msg += f"• Target: `{target_session[:8]}`\n"
    msg += f"• Optimizer: `{opt_sid}`\n" if opt_sid else ""
    msg += f"• Feedback: {feedback}\n" if feedback else ""
    msg += f"\n📝 **Chat with the Optimizer** — tell it what to analyze, fix, or improve."
    await _persist_system_interaction(db, user_id, session_id, msg, "system:optimizer")
    return msg


def _is_compact_command(message: str) -> bool:
    """True if the message is the built-in ``/compact`` command (args ignored).

    ``/compact`` is a *system* command, not an agent — it is handled directly
    here rather than through the template-driven trigger index, so it works in any
    chat session without registering an agent. Matches ``/compact`` alone or
    followed by whitespace+text, but not ``/compactfoo``."""
    s = (message or "").strip()
    return bool(re.match(r"^/compact(?:\s+.*)?$", s, re.IGNORECASE | re.DOTALL))


async def _persist_system_interaction(db, user_id: str, session_id: str, content: str, source: str) -> None:
    """Persist a system-status message into the interactions table.

    These messages are visible to the user in chat (across refreshes) but never
    fed to the agent — ``interactions_to_openai_messages`` skips role='system' rows.
    Failure is non-fatal: we log and swallow so the caller's return value still reaches the user."""
    try:
        # System notices are durable transcript rows too. The live session-tail
        # endpoint filters out NULL session_seq rows, so omitting this made a
        # notice disappear until a full reload/cache merge and then surface at
        # the end instead of where the action occurred.
        session_seq = await db.next_session_seq(session_id)
        await db.insert_interaction(
            user_id=user_id,
            session_id=session_id,
            role="system",
            content=content,
            source=source,
            session_seq=session_seq,
        )
    except Exception as exc:
        logger.warning("Failed to persist system interaction (%s): %s", source, exc)


async def _handle_compact_command(user_id: str, session_id: str, db) -> str:
    """Force a compaction of the CURRENT session now (the ``/compact`` command).

    Deterministic, user-driven sibling of the agent's ``compact_context`` tool: it
    folds everything older than the verbatim 'hot tail' (Keep Verbatim) into frozen
    summary parts immediately — regardless of how full the context is — honouring
    the agent's Context Control settings (part size etc.). Nothing is deleted; the
    raw turns stay searchable. Failure-safe: returns a user-facing message either
    way, never raising into the chat.

    Every result (success, short-circuit, error) is persisted as a role='system'
    interaction so the user sees it in their chat history across refreshes."""
    result: str

    try:
        agent_id = await db.get_session_agent_id(session_id)
    except Exception:
        agent_id = None
    if not agent_id:
        # No agent is bound to this session yet (e.g. nothing has run in it). This is
        # NOT the same as "nothing old enough to fold" — say so honestly so the user
        # isn't told to send messages they may already have sent.
        result = (
            "Couldn't compact — this session isn't linked to an agent yet, so there's "
            "nothing to summarise. Send a message to the agent first, then run "
            "`/compact`."
        )
        await _persist_system_interaction(db, user_id, session_id, result, "system:compaction")
        return result

    # An alternate engine (e.g. Local Claude Code) keeps its memory OUTSIDE WebAgent,
    # so /compact means "compact & restart" for it: after the fold below, hand off to
    # the engine's hook to reshape what happens next. Generic lookup — no per-engine
    # branching here (the core stays small; engines are drop-in plug-ins).
    compact_hook = None
    try:
        agent_rec = await db.get_agent_by_id(agent_id)
        _eng_meta = (agent_rec or {}).get("metadata") or {}
        if isinstance(_eng_meta, str):
            _eng_meta = json.loads(_eng_meta or "{}")
        _engine_id = str((_eng_meta or {}).get("engine") or "").strip()
        if _engine_id and _engine_id != "default":
            from plugins.engines import get_engine_compact_hook
            compact_hook = get_engine_compact_hook(_engine_id)
    except Exception:
        compact_hook = None

    try:
        from app.agent.context_control import get_context_settings
        from app.agent.compaction import maybe_compact
        settings = await get_context_settings(db, agent_id, session_id, user_id)
        if not settings.get("enabled"):
            result = (
                "Context Control isn't active for this agent, so there's nothing to "
                "compact. Enable the Context Control ability to use `/compact`."
            )
            await _persist_system_interaction(db, user_id, session_id, result, "system:compaction")
            return result
        info = await maybe_compact(db, user_id, session_id, settings, force=True)
    except Exception as e:
        logger.warning("/compact failed for %s: %s", session_id, e)
        result = f"Couldn't compact this session — {e}"
        await _persist_system_interaction(db, user_id, session_id, result, "system:compaction")
        return result

    if info and info.get("status") == "failed":
        result = "Couldn't compact this session — " + str(
            info.get("reason") or "the System roster model could not create a summary."
        )
        await _persist_system_interaction(db, user_id, session_id, result, "system:compaction")
        return result
    if info and info.get("status") == "in_progress":
        result = "Compaction is already running for this session. Please wait for it to finish."
        await _persist_system_interaction(db, user_id, session_id, result, "system:compaction")
        return result

    # The summary train changed — drop any prewarmed prep bundle. A bundle built
    # before this fold carries the FULL transcript; without this invalidation it
    # stays inside its TTL and the next turn consumes it, so the fold has no
    # visible effect on context size.
    try:
        turn_prewarm.invalidate(session_id)
    except Exception:
        pass

    # Alternate engine: hand the fold result to its /compact behaviour. The Claude
    # Code engine uses this to compact-and-restart even when nothing was folded (a
    # short chat still seeds a fresh, clean session), so this runs before the
    # native "nothing to compact" short-circuit below.
    if compact_hook is not None:
        try:
            hook_result = await compact_hook(db, user_id, session_id, agent_id, info)
            if isinstance(hook_result, dict):
                hook_status = str(hook_result.get("status") or "")
                hook_message = str(hook_result.get("message") or "")
                if hook_status == "success":
                    metrics = info or hook_result.get("metrics") or {}
                    from app.agent.compaction import persist_compaction_success
                    notice = await persist_compaction_success(
                        db, user_id, session_id, metrics,
                        trigger="engine_hook", detail_suffix=hook_message,
                    )
                    return {
                        "message": str(notice.get("detail") or hook_message),
                        "compaction": {
                            "type": "compaction_completed",
                            "session_id": session_id,
                            **notice,
                        },
                    }
                result = hook_message or "Nothing to compact right now."
            else:
                result = str(hook_result or "")
                if info:
                    from app.agent.compaction import persist_compaction_success
                    notice = await persist_compaction_success(
                        db, user_id, session_id, info,
                        trigger="engine_hook", detail_suffix=result,
                    )
                    return {
                        "message": str(notice.get("detail") or result),
                        "compaction": {
                            "type": "compaction_completed",
                            "session_id": session_id,
                            **notice,
                        },
                    }
            await _persist_system_interaction(
                db, user_id, session_id, result, "system:compaction",
            )
            return result
        except Exception as e:
            logger.warning("/compact engine hook failed for %s: %s", session_id, e)
            result = f"Couldn't restart this session — {e}"
            await _persist_system_interaction(db, user_id, session_id, result, "system:compaction")
            return result

    if not info:
        # Distinguish the honest no-op cases: everything already fits in the hot
        # tail, no completed safe boundary exists yet, or a summariser attempt
        # failed and the failure-safe path left the checkpoint unchanged.
        reason = (
            "there is no completed safe milestone to fold yet — streaming messages "
            "and tool-call transactions remain verbatim"
        )
        try:
            n = await db.count_interactions(user_id, session_id)
            segments = None
            try:
                from app.agent.compaction import load_segments
                segments = await load_segments(db, user_id, session_id, total_count=n)
            except Exception:
                pass
            covered = max((int(s.get("end_index") or 0) for s in (segments or [])), default=0)
            tail_rows = await db.fetch_interactions_from_offset(user_id, session_id, covered)
            from app.agent.compaction import _is_plain_assistant_boundary, _span_tokens
            safe_boundaries = [
                row for i, row in enumerate(tail_rows)
                if i > 0 and (
                    getattr(row, "role", None) == "user"
                    or _is_plain_assistant_boundary(row)
                )
            ]
            tail_tokens = _span_tokens(tail_rows)
            verbatim_budget = int(settings.get("verbatim_tail_tokens") or 30_000)
            if tail_tokens <= verbatim_budget:
                reason = (
                    "all unsummarised turns already fit inside the configured "
                    "verbatim recent-tail budget"
                )
            elif safe_boundaries:
                reason = (
                    "the summary pass produced no checkpoint (the summariser may "
                    "have failed) — eligible older content was left verbatim"
                )
        except Exception:
            pass
        result = (
            "Nothing to compact right now — "
            + reason
            + ". No summary was created."
        )
        await _persist_system_interaction(db, user_id, session_id, result, "system:compaction")
        return result
    from app.agent.compaction import persist_compaction_success
    notice = await persist_compaction_success(
        db, user_id, session_id, info, trigger="manual",
    )
    return {
        "message": str(notice.get("detail") or ""),
        "compaction": {
            "type": "compaction_completed",
            "session_id": session_id,
            **notice,
        },
    }


# ── Compaction-in-progress registry + durable pending queue ─────────────────
# A manual /compact (or the composer's "Compact now") runs synchronously inside
# the send endpoint and can take many seconds (LLM summarisation). While it runs
# we must not start a second turn on the same session — instead, any message that
# arrives for the session is persisted as a DURABLE 'queued' row (metadata
# queued_for='compact'), so it survives a refresh, a session switch, and even a
# server restart (boot recovery drains leftover rows — see runner.py). When the
# compaction finishes, _drain_compaction_queue runs those rows as normal turns.
_COMPACTING_SESSIONS: set = set()


def _is_compacting(session_id: str) -> bool:
    """True while a /compact command is folding this session (in-process)."""
    return session_id in _COMPACTING_SESSIONS


async def _mark_interaction_queued_for_compact(db, interaction_id: str) -> None:
    """Flip a freshly-persisted user row to the durable compaction-queued state:
    status='queued' + metadata queued_for='compact'. The transcript restore path
    (session-load.js) renders such rows as pending bubbles, and the drain below
    (or boot recovery) starts their turn once the compaction is over."""
    conn = db._get_conn()
    try:
        row = conn.execute(
            "SELECT metadata FROM interactions WHERE id=?", (interaction_id,)
        ).fetchone()
        meta = {}
        if row and row[0]:
            try:
                meta = json.loads(row[0])
            except Exception:
                meta = {}
        if not isinstance(meta, dict):
            meta = {}
        meta["queued_for"] = "compact"
        conn.execute(
            "UPDATE interactions SET status='queued', metadata=? WHERE id=?",
            (json.dumps(meta), interaction_id),
        )
        conn.commit()
    finally:
        conn.close()


def _list_compaction_queued(db, session_id: str) -> List[Dict[str, Any]]:
    """Compaction-queued user rows for one session, oldest first."""
    conn = db._get_conn()
    try:
        rows = conn.execute(
            "SELECT id, content, metadata FROM interactions "
            "WHERE session_id=? AND role='user' AND status='queued' "
            "AND metadata LIKE '%queued_for%compact%' "
            "ORDER BY created_at ASC, rowid ASC",
            (session_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _list_compaction_queued_sessions(db) -> List[tuple]:
    """(session_id, user_id) pairs that still hold compaction-queued rows —
    used by boot recovery to drain messages left behind by a restart."""
    conn = db._get_conn()
    try:
        rows = conn.execute(
            "SELECT DISTINCT i.session_id, s.user_id FROM interactions i "
            "LEFT JOIN sessions s ON s.id = i.session_id "
            "WHERE i.role='user' AND i.status='queued' "
            "AND i.metadata LIKE '%queued_for%compact%'"
        ).fetchall()
        return [(r[0], r[1]) for r in rows]
    finally:
        conn.close()


async def _drain_compaction_queue(
    db, session_id: str, user_id: str, channel: str = "web_portal",
    execution_mode: str = "ask",
) -> int:
    """Run every compaction-queued message in this session as a normal turn.

    Called when a /compact finishes (the queue drains automatically), by the
    force-run escape hatch, and at boot for rows a restart left behind. Each
    queued row is flipped to 'complete' and dispatched through the same
    supervised background-turn path a normal send uses — so the reply streams
    over the WebSocket and the queued bubble clears on the 'running' broadcast.

    A row whose session has no agent is flipped to 'complete' without a run so
    it can never dangle as a phantom queued bubble."""
    rows = _list_compaction_queued(db, session_id)
    if not rows:
        return 0
    agent = None
    try:
        agent_id = await db.get_session_agent_id(session_id)
        if agent_id:
            agent = await db.fetch_agent_by_id_with_context(
                agent_id, CONTEXT_SECTION_TYPES, user_id=user_id)
    except Exception:
        agent = None
    if not agent:
        for r in rows:
            try:
                await db.update_interaction(r["id"], status="complete")
            except Exception:
                pass
        return 0
    from types import SimpleNamespace as _NS
    for r in rows:
        try:
            _meta = json.loads(r.get("metadata") or "{}") if r.get("metadata") else {}
        except Exception:
            _meta = {}
        if not isinstance(_meta, dict):
            _meta = {}
        # Flip BEFORE dispatch: the run broadcasts agent_status 'running', which
        # clears the client's queued styling for this turn id.
        try:
            await db.update_interaction(r["id"], status="complete")
        except Exception:
            pass
        _req = _NS(
            session_id=session_id,
            user_id=user_id,
            message=r.get("content") or "",
            execution_mode=execution_mode,
            agent_id=agent["id"],
            attachment_ids=list(_meta.get("attachment_ids") or []),
            client_msg_id=_meta.get("cmid"),
            app_control=None,
            target_device=None,
        )
        _rid = r["id"]
        await get_run_manager().start_or_replace(
            session_id=session_id,
            user_id=user_id,
            turn_id=_rid,
            db=db,
            run_factory=lambda replaced, _r=_rid, _q=_req: _run_turn_background(
                db, _q, agent, _r, channel, replaced=replaced,
                is_first_turn=False),
        )
    return len(rows)


@router.post("/interrupt")
async def interrupt_chat(request: InterruptRequest, fastapi_request: Request):
    """Request a graceful interruption for an ongoing chat session.

    Interrupt is the ONLY thing (besides finishing or a server restart) that
    stops a supervised run. Sets the DB flag the agent loop polls; the loop
    finalizes its partial answer as 'interrupted' and flips run-state."""
    try:
        _, db = await _require_chat_session_access(
            fastapi_request, None, request.session_id
        )
        was_running = await get_run_manager().hard_stop_session_tree(request.session_id, db)
        return {"status": "ok", "message": "Session stopped.", "was_running": was_running}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error setting interrupt: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/resume")
async def resume_chat(request: ResumeRequest, fastapi_request: Request):
    """Manually re-ignite a stopped run (the one-click path for a run held as
    'needs_manual_resume' by the auto-resume opt-out, or any interrupted/failed
    turn the user wants to continue). Backend-driven — works even with no live
    WebSocket. The resumed turn streams into the chat via the normal event path."""
    try:
        await _require_chat_session_access(
            fastapi_request, None, request.session_id
        )
        from app.agent.runner import manual_resume
        ok = await manual_resume(request.session_id)
        return {"status": "ok" if ok else "noop",
                "resumed": ok,
                "message": "Resuming." if ok else "Nothing to resume (already running or not resumable)."}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error resuming run %s: %s", request.session_id, e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/terminal-chat/activate")
async def terminal_chat_activate(fastapi_request: Request):
    """Activate a terminal for the Terminal Chat agent — spawns the PTY and
    emits a terminal_chat_state event so the frontend mounts xterm.js
    immediately, without waiting for a user message."""
    from app.auth.identity import assert_caller_is

    user_id = await assert_caller_is(fastapi_request, None)
    body = await fastapi_request.json()
    session_id = body.get("session_id", "")
    agent_id = body.get("agent_id", "")
    
    if not session_id or not agent_id:
        return {"status": "error", "error": "session_id and agent_id required"}
    
    from app.db import get_db
    db = get_db()
    await _require_chat_session_access(fastapi_request, user_id, session_id)
    
    # Resolve visibility/roles before allocating a terminal resource.
    agent_rec = await _require_chat_agent_access(db, agent_id, user_id)
    
    # Read engine from metadata
    meta = {}
    try:
        meta_raw = agent_rec.get("metadata", "{}")
        if isinstance(meta_raw, str):
            meta = json.loads(meta_raw)
        elif isinstance(meta_raw, dict):
            meta = meta_raw
    except Exception:
        pass
    
    engine = meta.get("engine", "")
    if engine != "terminal_chat":
        return {"status": "error", "error": "Agent is not a Terminal Chat agent"}
    
    tc_cfg = meta.get("terminal_chat", {}) if isinstance(meta.get("terminal_chat"), dict) else {}
    command = str(tc_cfg.get("command") or "").strip()
    
    # Spawn the PTY and emit the state event
    from plugins.engines.terminal_chat.terminal_chat import _get_or_create_pty
    tsid = await _get_or_create_pty(session_id, user_id, command=command)
    
    # Emit terminal_chat_state on the WS so the frontend mounts xterm.js
    await _emit_to_visualizers(session_id, {
        "type": "terminal_chat_state",
        "state": "active",
        "terminal_session_id": tsid,
        "command": command,
    }, user_id=user_id)
    
    return {"status": "ok", "terminal_session_id": tsid}


@router.get("/self-heal/status")
async def self_heal_status(fastapi_request: Request):
    """Observability: the liveness watchdog's status + counters, plus the list of
    runs currently awaiting a manual one-click resume."""
    from app.auth.identity import assert_caller_is

    user_id = await assert_caller_is(fastapi_request, None)
    db = get_db()
    try:
        is_admin = await db.is_user_admin(user_id)
    except Exception:
        is_admin = False
    try:
        from app.agent.watchdog import get_watchdog
        wd = await get_watchdog().get_status()
    except Exception as e:
        wd = {"error": str(e)}
    manual: List[Dict[str, Any]] = []
    try:
        conn = db._get_conn()
        try:
            sql = (
                "SELECT session_id, user_id, origin, resume_attempts, error, "
                "updated_at FROM session_runs "
                "WHERE stop_cause='needs_manual_resume'"
            )
            params: tuple[Any, ...] = ()
            if not is_admin:
                sql += " AND user_id = ?"
                params = (user_id,)
            sql += " ORDER BY updated_at DESC LIMIT 100"
            rows = conn.execute(sql, params).fetchall()
            manual = [dict(r) for r in rows]
        finally:
            conn.close()
    except Exception:
        manual = []
    return {"watchdog": wd, "awaiting_manual_resume": manual}


# ── Suggested replies (silent user-impersonator) ────────────────────────────
# These power the tappable suggestion chips above the chat pill. They run a
# single, hidden LLM call — never the agent loop, never persisted to the chat,
# never streamed over the agent WebSocket. See app/agent/suggestions.py.

class SuggestionsRequest(BaseModel):
    user_id: str
    session_id: Optional[str] = None
    count: Optional[int] = None


class SuggestionsConfigRequest(BaseModel):
    mode: Optional[str] = None          # "off" | "on" | "scheduler"
    count: Optional[int] = None
    idle_seconds: Optional[int] = None


class VoiceRouteRequest(BaseModel):
    user_id: str
    session_id: str
    utterance: str = Field(..., min_length=1, max_length=2000)
    voice_session_id: Optional[str] = Field(default=None, max_length=100)
    proposal_id: Optional[str] = Field(default=None, max_length=100)


class VoiceControlRequest(BaseModel):
    user_id: str
    session_id: str
    action: str = Field(..., min_length=1, max_length=32)
    voice_session_id: Optional[str] = Field(default=None, max_length=100)
    command: str = Field(default="", max_length=4000)
    target_turn_id: Optional[str] = Field(default=None, max_length=256)
    approval_id: Optional[str] = Field(default=None, max_length=256)
    idempotency_key: str = Field(..., min_length=8, max_length=128)


class VoiceNarrationRequest(BaseModel):
    user_id: str
    session_id: str
    voice_session_id: str = Field(..., min_length=1, max_length=100)
    detail: str = Field(default="normal", max_length=20)
    timeout_seconds: float = Field(default=20.0, ge=0.0, le=25.0)


class VoiceSpeechRequest(BaseModel):
    user_id: str
    session_id: str
    voice_session_id: str = Field(..., min_length=1, max_length=100)
    text: str = Field(..., min_length=1, max_length=4096)


def _voice_route_message(action: str, reason: str, *, active_run: bool) -> str:
    """Short deterministic acknowledgements for routes that need no model."""
    if action == "request_confirmation":
        return (
            "That will change the active work. Say yes to confirm, or no to keep "
            "the current run unchanged."
        )
    if action == "request_clarification":
        return (
            "Voice could not safely determine the intent of that message. Please try again."
        )
    if action == "reject_confirmation":
        return "There is no pending change to confirm."
    if action == "interrupt":
        if reason == "explicit_pause":
            return "Pausing the active run."
        if reason == "explicit_resume":
            return "Resuming the paused run."
        return "Stopping the active run."
    if reason == "already_active":
        return "The run is already active."
    if reason == "proposal_rejected":
        return "Okay. I will leave the current run unchanged."
    if reason == "no_active_run":
        return "There is no active run to stop."
    if action == "dispatch_work":
        return "Confirmed. I am sending that to the work agent." if active_run else "Got it. I am starting that now."
    return "Okay."


@router.post("/voice/route")
async def voice_route(req: VoiceRouteRequest, fastapi_request: Request):
    """Route ephemeral speech without letting the browser infer side effects.

    This endpoint classifies and manages confirmations, but deliberately does
    not dispatch durable work or interrupt a run. The client must hand an
    accepted command to the normal chat send path, or invoke the separately
    authenticated voice-control endpoint for an explicit control action.
    """
    user_id, db = await _require_chat_session_access(
        fastapi_request, req.user_id, req.session_id
    )
    from app.agent.voice_coordinator import (
        VoiceCoordinatorUnavailable,
        answer_voice_question_result,
        classify_natural_voice_intent,
    )
    from app.agent.voice_intent import (
        VoiceAction, route_model_intent, route_voice_utterance,
    )
    from app.api.transcription import _resolve_transcription_config
    from app.agent.voice_session import get_voice_session_registry
    from app.agent.voice_config import voice_conversation_for_session

    registry = get_voice_session_registry()
    conversation_config = await voice_conversation_for_session(db, req.session_id)
    try:
        session = await registry.get_or_create(
            user_id,
            req.session_id,
            voice_session_id=req.voice_session_id,
            db=db,
            config=conversation_config,
        )
    except PermissionError:
        raise HTTPException(status_code=404, detail="Voice session not found.")

    active_run = getattr(fastapi_request.state, 'controlled_voice_active_run', bool(session.active_run_id))
    utterance = req.utterance.strip()
    pending = registry.get_pending_proposal(
        session.voice_session_id,
        proposal_id=req.proposal_id,
    ) if req.proposal_id else registry.get_pending_proposal(session.voice_session_id)
    route = route_voice_utterance(
        utterance,
        active_run=active_run,
        pending_proposal=pending,
    )
    voice_config: Optional[Dict[str, Any]] = None
    route_issue = ""
    if (
        route.action == VoiceAction.REQUEST_CLARIFICATION
        and route.reason == "no_safe_deterministic_route"
    ):
        try:
            voice_config = await _resolve_transcription_config(
                user_id, req.session_id,
            )
            model_intent = await classify_natural_voice_intent(
                utterance,
                active_run=active_run,
                config=voice_config,
            )
            route = route_model_intent(
                utterance,
                model_intent,
                active_run=active_run,
            )
        except VoiceCoordinatorUnavailable as exc:
            logger.warning("voice intent classification unavailable: %s", exc)
            route_issue = "intent_classifier_unavailable"
        except Exception as exc:
            logger.warning("voice intent configuration failed: %s", exc)
            route_issue = "intent_classifier_unavailable"


    # The streaming transcription socket already recorded the final transcript.
    # Avoid duplicating it when routing follows immediately after capture.
    pending_intent = session.pending_intent or {}
    if str(pending_intent.get("text") or "").strip() != utterance:
        registry.record_utterance(
            session.voice_session_id,
            utterance,
            pending_kind=route.intent.value,
        )
    else:
        registry.set_pending_intent(
            session.voice_session_id,
            {"kind": route.intent.value, "text": utterance},
        )

    proposal = route.proposal
    command: Optional[str] = None
    control_action: Optional[str] = None
    answer = ""

    if route.intent.value == "confirmation" and pending:
        resolution = "accepted" if route.action == VoiceAction.DISPATCH_WORK else "rejected"
        resolved = registry.resolve_pending_proposal(
            session.voice_session_id,
            str(pending.get("proposal_id") or ""),
            resolution,
        )
        proposal = resolved or pending
        if resolution == "accepted":
            command = str(pending.get("utterance") or "").strip() or None
    elif route.action == VoiceAction.REQUEST_CONFIRMATION:
        try:
            proposal = registry.create_pending_proposal(
                session.voice_session_id,
                intent=route.intent.value,
                action=VoiceAction.DISPATCH_WORK.value,
                utterance=utterance,
            )
        except RuntimeError:
            proposal = registry.get_pending_proposal(session.voice_session_id)
    elif route.action == VoiceAction.DISPATCH_WORK:
        command = utterance
    elif route.action == VoiceAction.INTERRUPT:
        if route.reason == "explicit_pause":
            control_action = "pause"
        elif route.reason == "explicit_resume":
            control_action = "resume"
        else:
            control_action = "stop"

    response_source = "system"
    if route.action == VoiceAction.ANSWER:
        current = registry.get(session.voice_session_id) or session
        if voice_config is None:
            try:
                voice_config = await _resolve_transcription_config(
                    user_id, req.session_id,
                )
            except Exception as exc:
                logger.warning("voice answer configuration failed: %s", exc)
                voice_config = {}
        reply = await answer_voice_question_result(
            db,
            user_id,
            req.session_id,
            utterance,
            voice_session=current.public_snapshot(),
            llm_config=voice_config,
        )
        answer = reply.text
        response_source = reply.source
        route_issue = reply.issue
    elif route_issue == "intent_classifier_unavailable":
        answer = (
            "Voice intent classification is unavailable because the configured "
            "Voice model did not respond. Please try again or use text chat."
        )
    elif route.reason == "model_intent_ambiguous":
        route_issue = "intent_ambiguous"
        answer = (
            "Voice understood the recording but could not determine a safe intent. "
            "Please rephrase or use text chat."
        )
    else:
        answer = _voice_route_message(
            route.action.value,
            route.reason,
            active_run=active_run,
        )
    registry.record_response(session.voice_session_id, answer)
    current = registry.get(session.voice_session_id) or session
    result = route.to_dict()
    result.update({
        "answer": answer,
        "response_source": response_source,
        "issue": route_issue or None,
        "command": command,
        "control_action": control_action,
        "proposal": proposal,
        "ephemeral": True,
        "voice_session_id": current.voice_session_id,
        "expires_at": current.public_snapshot()["expires_at"],
        "last_agent_event_cursor": current.last_agent_event_cursor,
    })
    return result


@router.post("/voice/control")
async def voice_control(req: VoiceControlRequest, fastapi_request: Request):
    """Execute an explicit, authenticated voice control exactly once."""
    user_id, db = await _require_chat_session_access(
        fastapi_request, req.user_id, req.session_id
    )
    from app.agent.voice_controls import get_voice_control_coordinator
    from app.agent.voice_session import get_voice_session_registry

    if req.voice_session_id:
        try:
            voice_session = get_voice_session_registry().get(
                req.voice_session_id,
                user_id=user_id,
                chat_session_id=req.session_id,
            )
        except PermissionError:
            raise HTTPException(status_code=404, detail="Voice session not found.")
        if voice_session is None:
            raise HTTPException(status_code=410, detail="Voice session expired.")

    result = await get_voice_control_coordinator().execute(
        action=req.action,
        user_id=user_id,
        session_id=req.session_id,
        idempotency_key=req.idempotency_key,
        db=db,
        command=req.command,
        target_turn_id=req.target_turn_id,
        approval_id=req.approval_id,
    )
    return result.as_dict()


@router.post("/voice/narration/next")
async def voice_narration_next(req: VoiceNarrationRequest, fastapi_request: Request):
    """Long-poll the optional non-durable spoken run-status lane."""
    user_id, _db = await _require_chat_session_access(
        fastapi_request, req.user_id, req.session_id
    )
    from app.agent.voice_narration import get_voice_narration_broker
    from app.agent.voice_session import get_voice_session_registry

    try:
        voice_session = get_voice_session_registry().get(
            req.voice_session_id,
            user_id=user_id,
            chat_session_id=req.session_id,
        )
    except PermissionError:
        raise HTTPException(status_code=404, detail="Voice session not found.")
    if voice_session is None:
        raise HTTPException(status_code=410, detail="Voice session expired.")
    from app.agent.voice_narration import NarrationPriority
    detail_limits = {
        "quiet": NarrationPriority.CRITICAL,
        "normal": NarrationPriority.MILESTONE,
        "detailed": NarrationPriority.DETAIL,
    }
    limit = detail_limits.get(req.detail.lower(), NarrationPriority.MILESTONE)
    deadline = time.monotonic() + req.timeout_seconds
    item = None
    while True:
        remaining = max(0.0, deadline - time.monotonic())
        candidate = await get_voice_narration_broker().get(
            req.session_id,
            timeout=remaining,
        )
        if candidate is None:
            break
        if candidate.priority <= limit:
            item = candidate
            break
        if remaining <= 0:
            break
    return {
        "narration": item.as_dict() if item else None,
        "voice_session_id": voice_session.voice_session_id,
        "ephemeral": True,
    }


@router.get("/voice/speech/capabilities")
async def voice_speech_capabilities(
    fastapi_request: Request,
    user_id: str = Query(...),
    session_id: str = Query(...),
):
    await _require_chat_session_access(fastapi_request, user_id, session_id)
    from app.agent.voice_speech import VoiceSpeechService
    return VoiceSpeechService().availability().as_dict()


@router.post("/voice/speech")
async def voice_speech(req: VoiceSpeechRequest, fastapi_request: Request):
    """Stream sanitized speech audio; clients fall back to browser TTS."""
    user_id, _db = await _require_chat_session_access(
        fastapi_request, req.user_id, req.session_id
    )
    from app.agent.voice_session import get_voice_session_registry
    from app.agent.voice_speech import SpeechUnavailable, VoiceSpeechService

    try:
        voice_session = get_voice_session_registry().get(
            req.voice_session_id,
            user_id=user_id,
            chat_session_id=req.session_id,
        )
    except PermissionError:
        raise HTTPException(status_code=404, detail="Voice session not found.")
    if voice_session is None:
        raise HTTPException(status_code=410, detail="Voice session expired.")

    service = VoiceSpeechService()
    availability = service.availability()
    if not availability.available:
        raise HTTPException(status_code=503, detail=availability.as_dict())
    provider = service.provider
    audio_format = str(getattr(provider, "audio_format", "mp3"))
    content_types = {
        "mp3": "audio/mpeg", "opus": "audio/ogg", "aac": "audio/aac",
        "flac": "audio/flac", "wav": "audio/wav", "pcm": "audio/pcm",
    }

    async def audio_bytes():
        try:
            async for chunk in service.stream(req.text):
                yield chunk.data
        except SpeechUnavailable:
            return

    return StreamingResponse(
        audio_bytes(),
        media_type=content_types.get(audio_format, "application/octet-stream"),
        headers={"Cache-Control": "no-store", "X-Voice-Provider": availability.provider},
    )


@router.post("/suggestions")
async def chat_suggestions(
    req: SuggestionsRequest, fastapi_request: Request
):
    """Return up to N suggested next user-messages for the chat pill chips.

    Best-effort: returns an empty list (HTTP 200) when the engine is off,
    credentials are missing, or generation fails — the UI just shows no chips."""
    from app.auth.identity import assert_caller_is
    from app.agent.suggestions import generate_suggestions, load_runtime_config

    user_id = await assert_caller_is(fastapi_request, req.user_id)
    if req.session_id:
        await _require_chat_session_access(
            fastapi_request, user_id, req.session_id
        )
    cfg = load_runtime_config()
    if cfg.get("mode") == "off":
        return {"suggestions": [], "mode": "off"}
    try:
        db = get_db()
        items = await generate_suggestions(
            db, user_id, req.session_id, count=req.count
        )
    except Exception as e:
        logger.warning("chat_suggestions failed: %s", e)
        items = []
    return {"suggestions": items, "mode": cfg.get("mode"), "idle_seconds": cfg.get("idle_seconds")}


@router.get("/suggestions/config")
async def get_suggestions_config(fastapi_request: Request):
    """Read the Suggested-Replies runtime config (mode / count / idle seconds)."""
    from app.auth.identity import assert_caller_is
    from app.agent.suggestions import load_runtime_config

    await assert_caller_is(fastapi_request, None)
    return load_runtime_config()


# ── On-demand skills (active list + activate/deactivate) ───────────────────
# Power the active-skill chips below the chat box AND the skill-selector panel
# anchored to the right edge of the chat pill footer. The active list lives in
# the session's metadata; load_skill adds to it, these endpoints modify it.

class SkillActivateRequest(BaseModel):
    user_id: str
    session_id: str
    name: str


class SkillDeactivateRequest(BaseModel):
    user_id: str
    session_id: str
    name: str


def _anonymous_chat_only(user_id: Optional[str]) -> bool:
    return bool(user_id) and str(user_id).startswith("anon_")


async def _anonymous_showcase_only(db: Any, user_id: Optional[str], agent_id: Optional[str]) -> bool:
    if not _anonymous_chat_only(user_id):
        return False
    if not agent_id:
        return True
    try:
        from app.agent.public_policy import is_showcase_policy
        agent = await db.get_agent_by_id(agent_id)
        return is_showcase_policy(agent or {})
    except Exception:
        return True


@router.get("/skills")
async def chat_skills(
    fastapi_request: Request,
    user_id: str,
    session_id: str,
    agent_id: Optional[str] = None,
):
    """Return the active (loaded) selectable skills for a session, plus the
    agent's full selectable skill catalog (authored + ability-bundled) when
    `agent_id` is supplied. Each catalog entry carries a display_name for the
    UI panel and an `active` flag so the panel shows engaged skills.

    The active list alone needs no agent (it lives in session metadata), so the
    chat chips work even if agent_id isn't passed; the panel passes agent_id to
    also get descriptions + modes + display_name."""
    user_id, db = await _require_chat_session_access(
        fastapi_request, user_id, session_id
    )
    await _require_chat_agent_access(db, agent_id, user_id)
    if await _anonymous_showcase_only(db, user_id, agent_id):
        return {"active": [], "skills": []}
    active = await db.get_session_active_skills(session_id)
    active_set = set(active)

    all_skills = []

    # ── 1. Authored skills from the agent's __skills__ slot ──
    if agent_id:
        try:
            all_skills.extend(await db.get_agent_skills(agent_id) or [])
        except Exception:
            pass

    # ── 2. Ability-bundled skills — directly scan every ability file ──
    # Do NOT go through collect_ability_skills (which requires agent connections
    # and admin config). Instead, scan all ability modules for skill declarations
    # so the panel shows every discoverable skill regardless of how the agent is
    # configured. The agent's abilities are already gated by the admin; showing
    # all ability skills here is OK because the user activates them manually.
    try:
        from app.abilities import all_raw, _resolve_skill_body
        for ability_id, feat in all_raw().items():
            try:
                body = _resolve_skill_body(feat, ability_id)
                if not body:
                    continue
                from app.agent.ability_skills import _skill_from_feature
                skill = _skill_from_feature({**feat, "skill": body}, ability_id)
                if skill:
                    all_skills.append(skill)
            except Exception:
                continue
    except Exception:
        pass

    catalog = [
        {
            "name": s.get("handle") or s["name"],
            "display_name": s.get("display_name") or s.get("source") or s["name"],
            "description": s.get("description", ""),
            "mode": s.get("mode", "selectable"),
            "active": True if s.get("mode") == "always_on" else (s.get("handle") or s["name"]) in active_set,
        }
        for s in all_skills
        if s.get("enabled", True)
    ]
    return {"active": active, "skills": catalog}


@router.get("/abilities")
async def chat_abilities(
    fastapi_request: Request,
    user_id: str,
    session_id: str,
    agent_id: Optional[str] = None,
):
    """Return the agent's enabled abilities + which are active for this session.

    Drives the chat-side abilities panel + its counter. An ability is **active**
    (highlighted; counted) when it is `visible` (its tools + skill are shown to
    the agent now) OR the agent has pulled it in this session via `load_ability`.
    """
    user_id, db = await _require_chat_session_access(
        fastapi_request, user_id, session_id
    )
    if agent_id:
        try:
            await _require_chat_agent_access(db, agent_id, user_id)
        except HTTPException:
            # Stale/deleted agent id (e.g. a leftover mock-agent selection) or a
            # user without access to it: this is a read-only chat badge, so a
            # 404 must not fire in the console every page load — return an empty
            # list instead and let the panel/counter render as "none".
            return {"active": [], "abilities": []}
    loaded = set(await db.get_session_active_abilities(session_id))
    suppressed = set(await db.get_session_suppressed_abilities(session_id))
    out = []
    if agent_id:
        try:
            from app.tools.tool_modes import resolve_ability_mode
            from app.abilities import ui_catalog
            modes = await db.get_agent_ability_modes(agent_id)
            try:
                _adefault = await db.get_agent_discovery_default(agent_id)
            except Exception:
                _adefault = None
            cat_abilities = (ui_catalog() or {}).get("abilities", {})
            rows = await db.get_agent_connections(agent_id)
            from app.agent.ability_access import filter_abilities_for_caller
            configured_ids = {
                r.get("connection_type") for r in rows
                if r.get("section") == "ability" and r.get("enabled")
                and r.get("connection_type")
            }
            allowed_ids = await filter_abilities_for_caller(
                agent_id, configured_ids, user_id, db=db,
            )
            for r in rows:
                if r.get("section") != "ability" or not r.get("enabled"):
                    continue
                aid = r.get("connection_type")
                if not aid or aid not in allowed_ids:
                    continue
                meta = cat_abilities.get(aid, {})
                mode = resolve_ability_mode(aid, modes, _adefault)
                # Active = revealed to the agent this turn: visible-by-config OR
                # loaded this session, AND not suppressed from this panel.
                active = ((mode == "visible") or (aid in loaded)) and (aid not in suppressed)
                out.append({
                    "id": aid,
                    "display_name": meta.get("display_name") or aid,
                    "icon": meta.get("icon") or "plug",
                    "mode": mode,
                    "loaded": aid in loaded,
                    "suppressed": aid in suppressed,
                    "active": active,
                })
        except Exception as e:
            logger.debug("chat_abilities failed for %s: %s", agent_id, e)
    out.sort(key=lambda a: (a["display_name"] or a["id"]).lower())
    active_ids = [a["id"] for a in out if a["active"]]
    return {"active": active_ids, "abilities": out}


class AbilityToggleRequest(BaseModel):
    user_id: str
    session_id: str
    ability_id: str
    agent_id: Optional[str] = None


@router.post("/abilities/activate")
async def chat_ability_activate(
    req: AbilityToggleRequest, fastapi_request: Request
):
    """Arm an ability for this conversation from the chat panel. Clears any
    suppression and — for a ``discoverable`` ability — adds it to the session's
    active list (same effect as the agent calling ``load_ability``), so its tools
    + skill flow into the next turn. A ``visible`` ability just needs un-suppressing."""
    user_id, db = await _require_chat_session_access(
        fastapi_request, req.user_id, req.session_id
    )
    await _require_chat_agent_access(db, req.agent_id, user_id)
    from app.agent.ability_access import filter_abilities_for_caller
    allowed = await filter_abilities_for_caller(
        req.agent_id or "", {req.ability_id}, user_id, db=db,
    )
    if req.ability_id not in allowed:
        raise HTTPException(
            status_code=403,
            detail={"code": "ability_not_available", "message": "This ability is not available for your account."},
        )
    await db.set_session_suppressed_ability(req.session_id, req.ability_id, False)
    mode = "visible"
    if req.agent_id:
        try:
            from app.tools.tool_modes import resolve_ability_mode
            modes = await db.get_agent_ability_modes(req.agent_id)
            mode = resolve_ability_mode(req.ability_id, modes)
        except Exception as e:
            logger.debug("ability_activate mode-resolve failed for %s: %s", req.ability_id, e)
    if mode != "visible":
        await db.set_session_active_ability(req.session_id, req.ability_id, True)
    return {"ability_id": req.ability_id, "active": True}


@router.post("/abilities/deactivate")
async def chat_ability_deactivate(
    req: AbilityToggleRequest, fastapi_request: Request
):
    """Turn an ability OFF for this conversation from the chat panel. Drops it
    from the session's active list and — for a ``visible`` ability — records a
    suppression so it's withheld even though the agent's config makes it visible.
    Its tools + skill leave the model's context on the next turn."""
    user_id, db = await _require_chat_session_access(
        fastapi_request, req.user_id, req.session_id
    )
    await _require_chat_agent_access(db, req.agent_id, user_id)
    await db.set_session_active_ability(req.session_id, req.ability_id, False)
    # Only a `visible`-by-config ability needs an explicit suppression to stay
    # off; for a discoverable one, dropping it from the active list is enough —
    # and suppressing it would wrongly block a later `load_ability` by the agent.
    mode = "visible"
    if req.agent_id:
        try:
            from app.tools.tool_modes import resolve_ability_mode
            modes = await db.get_agent_ability_modes(req.agent_id)
            mode = resolve_ability_mode(req.ability_id, modes)
        except Exception as e:
            logger.debug("ability_deactivate mode-resolve failed for %s: %s", req.ability_id, e)
    await db.set_session_suppressed_ability(req.session_id, req.ability_id, mode == "visible")
    return {"ability_id": req.ability_id, "active": False}


@router.post("/skills/activate")
async def chat_skill_activate(
    req: SkillActivateRequest, fastapi_request: Request
):
    """Manually activate a selectable skill from the UI panel. Calls load_skill
    on behalf of the user so it counts as loaded the same way as if the agent
    called load_skill itself."""
    user_id, db = await _require_chat_session_access(
        fastapi_request, req.user_id, req.session_id
    )
    if await _anonymous_showcase_only(db, user_id, req.agent_id):
        raise HTTPException(
            status_code=403,
            detail={"code": "skill_not_available", "message": "Skills are not available for anonymous chat."},
        )
    active = await db.set_session_active_skill(req.session_id, req.name, True)
    return {"active": active, "name": req.name}


@router.post("/skills/deactivate")
async def chat_skill_deactivate(
    req: SkillDeactivateRequest, fastapi_request: Request
):
    """Drop a loaded skill from the conversation: remove it from the session's
    active list and neutralize its stored load result so the body leaves the
    model's context on the next turn."""
    _, db = await _require_chat_session_access(
        fastapi_request, req.user_id, req.session_id
    )
    active = await db.set_session_active_skill(req.session_id, req.name, False)
    try:
        await db.neutralize_skill_load(req.session_id, req.name)
    except Exception as e:
        logger.debug("neutralize_skill_load failed for %s: %s", req.name, e)
    return {"active": active, "name": req.name}


# ── Per-session model override ────────────────────────────────────────────────
# The chat footer model picker saves the chosen model HERE (per session) rather
# than on the agent, so each conversation remembers its own model. The agent loop
# layers this over the agent/app default on every turn (app-default → agent →
# session). An empty model clears the override.

class SessionModelRequest(BaseModel):
    user_id: str
    session_id: str
    selection_type: Optional[str] = None   # "role" | "custom" | None (clear)
    role: Optional[str] = None
    custom_position: Optional[int] = None
    entry_id: Optional[str] = None


async def _entitled_session_model_options(db, user_id: str, session_id: str) -> dict:
    """Resolve the server-authoritative model slots for a session's caller."""
    from app.admin.settings import resolve_agent_models

    agent_rec = None
    try:
        agent_id = await db.get_session_agent_id(session_id)
        if agent_id:
            agent_rec = await db.get_agent_by_id(agent_id)
    except Exception:
        agent_rec = None
    return await resolve_agent_models(user_id, agent_rec, session_id)


def _slot_ref_from_model_slot(slot: dict) -> str:
    # Every visible picker row is an ordered roster entry. Prefer its stable
    # entry ID over a role so a multi-role model has one selectable identity.
    entry_id = str(slot.get("entry_id") or "")
    if entry_id:
        return "entry:" + entry_id
    if slot.get("type") == "role":
        return "role:" + str(slot.get("role") or "")
    return "custom:" + str(slot.get("position") or slot.get("custom_position") or 0)


@router.post("/session-model")
async def set_session_model(
    req: SessionModelRequest, fastapi_request: Request
):
    """Set (or clear) this session's model override as a SLOT reference.

    Stores ``{use_default: False, selection_type: "role", role: "standard"}``
    (or ``selection_type: "custom", entry_id: "..."``) in the session's
    metadata. On every turn the backend resolves the slot LIVE against the
    agent's current roster — so admin changes to the model list take effect
    immediately. Pass ``selection_type`` = None to clear the override entirely.

    If the session row does not exist yet (model picker used before the first
    message), the DB layer creates it on the fly so the selection is stored
    per-session immediately — never falling back to agent metadata."""
    from app.auth.identity import assert_caller_is
    user_id = await assert_caller_is(fastapi_request, req.user_id)
    db = get_db()
    # Verify the caller owns this session OR it doesn't exist yet (the DB layer
    # will create the row in set_session_llm_override when it's missing).
    try:
        await db.assert_session_owned(user_id, req.session_id)
    except PermissionError:
        participant = False
        try:
            participant = await db.is_session_participant(req.session_id, user_id, "user")
        except Exception:
            pass
        if not participant:
            raise HTTPException(status_code=404, detail="Session not found.")
    # Model state BEFORE this write ('default' | 'override'). Recorded on the
    # durable notice so the transcript nets a run's switches against the state
    # the run began in — a pick that returns to the default model cancels the
    # notice. Best-effort read; ANY stored selection counts as an override, so an
    # unknown value can never cancel a real notice.
    _prev_model_cfg = None
    try:
        _prev_model_cfg = await db.get_session_llm_override(req.session_id)
    except Exception:
        _prev_model_cfg = None

    def _model_identity(cfg, ref: str) -> str:
        """Netting key for a model selection: the IDENTITY, never a coarse
        default/override state — premium → custom-2 is a real change, and only a
        return to the session's starting selection may cancel the notice."""
        if ref:
            return "default" if ref.strip().lower() == "role:default" else ref
        if cfg:
            _type = str(cfg.get("type") or "").strip().lower()
            if _type == "role":
                _role = str(cfg.get("role") or "").strip()
                return "default" if _role.lower() == "default" else "role:" + _role
            if _type == "custom":
                _eid = str(cfg.get("entry_id") or "").strip()
                if _eid:
                    return "entry:" + _eid
                return "custom:" + str(cfg.get("position") or 0)
        return "default"

    _requested_ref = ""
    if req.selection_type == "role":
        _requested_ref = "role:" + str(req.role or "").strip()
    elif req.selection_type == "custom":
        _requested_ref = (
            "entry:" + str(req.entry_id or "").strip()
            if req.entry_id else "custom:" + str(req.custom_position or 0)
        )
    _from_model_state = _model_identity(_prev_model_cfg, "")
    _to_model_state = _model_identity(None, _requested_ref)
    if req.selection_type:
        options = await _entitled_session_model_options(db, user_id, req.session_id)
        requested_ref = (
            "role:" + str(req.role or "").strip()
            if req.selection_type == "role"
            else ("entry:" + str(req.entry_id or "").strip()
                  if req.entry_id else "custom:" + str(req.custom_position or 0))
        )
        allowed_refs = {_slot_ref_from_model_slot(slot) for slot in options.get("slots") or []}
        # The picker displays each roster entry once, but an older session may
        # still reference any role assigned to that entry.
        allowed_refs.update(
            "role:" + str(role)
            for slot in options.get("slots") or []
            for role in (slot.get("roles") or [])
        )
        if req.selection_type not in {"role", "custom"} or requested_ref not in allowed_refs:
            raise HTTPException(
                status_code=403,
                detail={"code": "model_not_allowed", "slot_ref": requested_ref},
            )
        sel = {"type": req.selection_type}
        if req.selection_type == "role":
            sel["role"] = req.role or ""
        elif req.selection_type == "custom":
            sel["position"] = req.custom_position or 0
            sel["entry_id"] = req.entry_id or ""
        cfg = await db.set_session_llm_override(req.session_id, sel)
    else:
        cfg = await db.set_session_llm_override(req.session_id, None)
    # Durable twin of the footer-picker notice: persist a system:model row so
    # the transcript shows the switch on cold reload (and via the IndexedDB
    # cache). initiator='user' — the footer picker is a user action with no
    # tool call, so the row itself must say who caused the switch.
    try:
        _slot_ref = ""
        # The durable row's content is the FULL sentence the expanded transcript
        # shows ("Premium model enabled"); the collapsed group line is generic and
        # client-side. A provider model id is never named.
        _content = "Standard model enabled"
        if req.selection_type == "role":
            _role = (req.role or "").strip()
            _slot_ref = "role:" + _role
            if _role.lower() == "default":
                _content = "Standard model enabled"
            elif _role.lower() == "premium":
                _content = "Premium model enabled"
            else:
                _content = f"{_role[:1].upper()}{_role[1:]} model enabled"
        elif req.selection_type == "custom":
            _pos = req.custom_position or 0
            _slot_ref = ("entry:" + str(req.entry_id)
                         if req.entry_id else "custom:" + str(_pos))
            _content = f"Custom {_pos} model enabled"
        await db.add_model_switch_notice(
            user_id, req.session_id, _content,
            initiator="user", slot=_slot_ref,
            model=str((cfg or {}).get("model") or ""),
            from_state=_from_model_state, to_state=_to_model_state)
    except Exception:
        pass
    return {"llm_config": cfg, "active_slot": cfg.get("selection_type") if cfg else None}


class SessionEffortRequest(BaseModel):
    user_id: str
    session_id: str
    slot_ref: str   # e.g., "role:standard", "custom:2"
    reasoning_effort: Optional[str] = None


@router.post("/session-model-effort")
async def set_session_model_effort(
    req: SessionEffortRequest, fastapi_request: Request
):
    """Set (or clear, with 'default'/empty) the reasoning-effort level for a
    specific SLOT on THIS session. Each slot remembers its own level (the footer
    picker shows an effort selector per slot row). Doesn't change which slot is
    active — takes effect on the next turn for whichever model the slot resolves to."""
    user_id, db = await _require_chat_session_access(
        fastapi_request, req.user_id, req.session_id
    )
    slot_ref = (req.slot_ref or "").strip()
    options = await _entitled_session_model_options(db, user_id, req.session_id)
    allowed_refs = {_slot_ref_from_model_slot(slot) for slot in options.get("slots") or []}
    allowed_refs.update(
        "role:" + str(role)
        for slot in options.get("slots") or []
        for role in (slot.get("roles") or [])
    )
    if slot_ref not in allowed_refs:
        raise HTTPException(
            status_code=403,
            detail={"code": "model_not_allowed", "slot_ref": slot_ref},
        )
    requested_effort = (req.reasoning_effort or "").strip().lower()
    if requested_effort not in {"", "default"}:
        from app.entitlements.service import resolve_capabilities
        capabilities = await resolve_capabilities(user_id, db=db)
        from app.reasoning_effort import EFFORT_ORDER
        order = EFFORT_ORDER
        maximum = str((capabilities.get("models") or {}).get("max_reasoning_effort") or "default")
        if requested_effort not in order or order[requested_effort] > order.get(maximum, 0):
            raise HTTPException(
                status_code=403,
                detail={"code": "model_not_allowed", "reason": "reasoning_effort",
                        "maximum": maximum},
            )
    if requested_effort not in {"", "default"}:
        slot = next((slot for slot in options.get("slots") or []
                     if _slot_ref_from_model_slot(slot) == slot_ref
                     or slot_ref in {"role:" + str(role) for role in slot.get("roles") or []}), None)
        supported = (slot or {}).get("reasoning_efforts", ["minimal", "low", "medium", "high"])
        if requested_effort not in supported:
            raise HTTPException(status_code=400, detail={
                "code": "unsupported_reasoning_effort", "supported": supported,
            })
    cfg = await db.set_session_model_effort(
        req.session_id, slot_ref, requested_effort or None)
    effort_map = (cfg or {}).get("model_effort") or {}
    return {"llm_config": cfg, "slot_ref": slot_ref,
            "reasoning_effort": effort_map.get(slot_ref, "default")}


# ── Per-session execution-mode override ──────────────────────────────────────
# The chat pill (Ask/Plan/Auto) is the user's per-message control. When the user
# flips the pill mid-turn, the UI persists the choice HERE so the loop can re-read
# it before each tool gate — the mode change takes effect on the very next tool
# call without needing a new user message. Mirrors /session-model above.

class SessionModeRequest(BaseModel):
    user_id: str
    session_id: str
    mode: str  # configured mode id (Ask/Plan/Auto plus agent-defined modes)


@router.post("/session-mode")
async def set_session_mode(
    req: SessionModeRequest, fastapi_request: Request
):
    """Persist the session's execution mode (Ask/Plan/Auto). The loop re-reads
    this before every tool gate, so a pill flip mid-turn takes effect immediately."""
    _, db = await _require_chat_session_access(
        fastapi_request, req.user_id, req.session_id
    )
    from app.agent.execution_modes import normalize_mode_id
    raw_mode = str(req.mode or "").strip().lower()
    mode = "wkspc" if raw_mode == "wkspc" else normalize_mode_id(raw_mode)
    # The mode in force BEFORE this flip — recorded on the durable notice so the
    # UI can net a group's changes (a flip back to the group's starting mode
    # cancels the notice). Must be read before the write below overwrites it.
    try:
        from_mode = await db.get_session_execution_mode(req.session_id) or ""
    except Exception:
        from_mode = ""
    mode = await db.set_session_execution_mode(req.session_id, mode)
    _active_turn_id = None
    try:
        _run_state = await db.run_state_get(req.session_id)
        if _run_state and _run_state.get("status") == "running":
            _active_turn_id = _run_state.get("turn_id")
    except Exception:
        pass
    # Durable twin of the live pill notice: persist a system:mode row so the
    # transcript shows the flip on cold reload (and via the IndexedDB cache).
    # initiator='user' — the pill is a user action, no tool call precedes it,
    # so the row itself must say who caused the switch.
    try:
        await db.add_execution_mode_notice(
            req.user_id, req.session_id, mode, initiator="user",
            turn_id=_active_turn_id, from_mode=from_mode or "")
    except Exception:
        pass
    return {"execution_mode": mode}


# ── Per-session compaction override ──────────────────────────────────────────
# The footer model panel carries a small "Context compaction" section: two token
# sliders (activation target / verbatim hot tail) and a "Compact now" button. They
# save HERE as a per-session override so one chat can tune its own compaction without
# changing the agent-wide Context Control settings; the loop's get_context_settings
# layers this over the agent default each turn. "Compact now" just sends /compact.

async def _session_context_composition(
    db, user_id: str, session_id: str, settings: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Split THIS chat's last assembled context into the panel's three segments.

    Prefers the three-way split the turn itself recorded on the newest assistant
    row (computed against the FULL context it sent). For turns written before
    that was recorded, it falls back to classifying the persisted schema
    snapshot with the same helper the live ``context_budget`` pipeline event
    uses, so the footer panel can graph the moment it opens instead of waiting
    for the next turn. Best-effort: returns None when neither is usable — the
    panel then hides the bar rather than showing a made-up split.
    """
    if not settings.get("enabled"):
        return None
    from app.agent.context_control import context_composition, estimate_tokens
    try:
        count = await db.count_interactions(user_id, session_id)
        if not count:
            return None
        # Only the tail matters: the newest assistant row that carries a snapshot.
        rows = await db.fetch_interactions_from_offset(
            user_id, session_id, max(0, int(count) - 4))
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("context composition: transcript read failed: %s", e)
        return None
    for row in reversed(list(rows or [])):
        role = row.get("role") if isinstance(row, dict) else getattr(row, "role", None)
        if role != "assistant":
            continue
        raw = row.get("output") if isinstance(row, dict) else getattr(row, "output", None)
        if not raw:
            continue
        try:
            payload = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        # Preferred: the split the turn itself computed against the FULL context,
        # persisted beside the (size-capped) snapshot. That snapshot drops its
        # OLDEST messages first — the system prompt among them — so classifying
        # it understates prompt overhead whenever it was trimmed.
        stored = payload.get("_context_breakdown")
        if isinstance(stored, dict) and stored.get("estimate_total"):
            composition = dict(stored)
            composition["source"] = "turn"
            return composition
        sent = payload.get("_sent_messages")
        if not isinstance(sent, list) or not sent:
            continue
        tools = payload.get("_sent_tools")
        schema_tokens = 0
        if isinstance(tools, list) and tools:
            schema_tokens = estimate_tokens(
                [{"role": "system", "content": json.dumps(tools)}])
        composition = context_composition(
            sent,
            tool_schema_tokens=schema_tokens,
            verbatim_tail_tokens=int(settings.get("verbatim_tail_tokens") or 0),
        )
        composition["source"] = "snapshot"
        if payload.get("_snapshot_truncated"):
            composition["truncated"] = True
        return composition
    return None


class SessionCompactionRequest(BaseModel):
    user_id: str
    session_id: str
    compact_target_tokens: Optional[int] = None
    verbatim_tail_tokens: Optional[int] = None
    # Legacy clients may still post percentages during a rolling upgrade.
    compact_threshold_pct: Optional[float] = None
    tail_fraction_pct: Optional[float] = None


@router.get("/session-compaction")
async def get_session_compaction(
    fastapi_request: Request,
    session_id: str = Query(...),
    agent_id: Optional[str] = Query(None),
    user_id: Optional[str] = Query(None),
):
    """Return the EFFECTIVE compaction settings for this chat — the agent's Context
    Control knobs with any per-session override layered on top — so the footer panel
    shows the live absolute token budgets. Also reports whether each value
    is a per-session override or inherited from the agent (so the UI can hint that)."""
    verified_user_id, db = await _require_chat_session_access(
        fastapi_request, user_id, session_id
    )
    await _require_chat_agent_access(db, agent_id, verified_user_id)
    aid = (agent_id or "").strip()
    if not aid:
        aid = (await db.get_session_agent_id(session_id)) or ""
    from app.agent.context_control import get_context_settings
    settings = await get_context_settings(
        db, aid, session_id, verified_user_id
    )
    try:
        override = await db.get_session_context_override(session_id)
    except Exception:
        override = None
    override = override if isinstance(override, dict) else {}
    # Current COMPOSITION of the last assembled context (not the budgets): what
    # the model actually saw, split three ways for the panel's stacked bar.
    composition = await _session_context_composition(
        db, verified_user_id, session_id, settings)
    return {
        "enabled": bool(settings.get("enabled")),
        "compaction_enabled": bool(settings.get("compaction_enabled")),
        "token_limit": int(settings.get("token_limit") or 1_050_000),
        "compact_target_tokens": int(
            settings.get("compact_target_tokens") or 100_000),
        "verbatim_tail_tokens": int(
            settings.get("verbatim_tail_tokens") or 30_000),
        "composition": composition,
        "overridden": {
            "compact_target_tokens": (
                "compact_target_tokens" in override or "compact_threshold" in override),
            "verbatim_tail_tokens": (
                "verbatim_tail_tokens" in override or "tail_fraction" in override),
        },
    }


@router.post("/session-compaction")
async def set_session_compaction(
    req: SessionCompactionRequest, fastapi_request: Request
):
    """Save this chat's absolute per-session compaction token budgets."""
    verified_user_id, db = await _require_chat_session_access(
        fastapi_request, req.user_id, req.session_id
    )
    agent_id = (await db.get_session_agent_id(req.session_id)) or ""
    from app.agent.context_control import get_context_settings
    effective = await get_context_settings(
        db, agent_id, req.session_id, verified_user_id)
    limit = int(effective.get("token_limit") or 1_050_000)
    updates: Dict[str, Any] = {}
    if req.compact_target_tokens is not None:
        updates["compact_target_tokens"] = max(
            1_000, min(limit, int(req.compact_target_tokens)))
        updates["compact_threshold"] = None
    if req.verbatim_tail_tokens is not None:
        updates["verbatim_tail_tokens"] = max(
            1_000, min(limit, int(req.verbatim_tail_tokens)))
        updates["tail_fraction"] = None
    # Backward-compatible conversion only; new UI/API consumers use token keys.
    if req.compact_threshold_pct is not None:
        updates["compact_target_tokens"] = max(
            1_000, min(limit, int(limit * req.compact_threshold_pct / 100.0)))
    if req.tail_fraction_pct is not None:
        updates["verbatim_tail_tokens"] = max(
            1_000, min(limit, int(limit * req.tail_fraction_pct / 100.0)))
    ov = await db.set_session_context_override(req.session_id, updates)
    return {"ok": True, "context_override": ov or {}}


@router.put("/suggestions/config")
async def update_suggestions_config(
    req: SuggestionsConfigRequest, fastapi_request: Request
):
    """Update the Suggested-Replies runtime config. Used by the impersonator
    agent's config panel on the Agents page."""
    from app.auth.identity import assert_caller_is
    from app.agent.suggestions import save_runtime_config

    user_id = await assert_caller_is(fastapi_request, None)
    try:
        is_admin = await get_db().is_user_admin(user_id)
    except Exception:
        is_admin = False
    if not is_admin:
        raise HTTPException(status_code=403, detail="Admin access required.")
    updates = {k: v for k, v in req.dict().items() if v is not None}
    return save_runtime_config(updates)


class PrewarmRequest(BaseModel):
    user_id: str
    session_id: str
    agent_id: Optional[str] = None


@router.post("/prewarm")
async def chat_prewarm(req: PrewarmRequest, fastapi_request: Request):
    """Warm the read-only prep (tool set and chat history)
    for a session WHILE THE USER IS STILL TYPING, so the next send skips those
    remote round-trips. The front-end calls this on chat-input focus and on a
    debounced keystroke. Best-effort: any failure just returns ``ok: False`` and
    the turn builds the prep live as before — nothing here can break a send.
    """
    from app.auth.identity import assert_caller_is
    try:
        uid = await assert_caller_is(fastapi_request, req.user_id)
    except Exception:
        return {"ok": False, "reason": "auth"}
    db = get_db()
    try:
        await _require_chat_session_access(
            fastapi_request, uid, req.session_id
        )
        # Share the per-turn read cache so duplicate reads within this build are
        # de-duped, exactly like a real send.
        with turn_cache_scope():
            # Resolve the agent the same way a send does (light: no membership
            # writes). Engine agents own their whole turn → nothing to prewarm.
            if req.agent_id:
                agent = await db.fetch_agent_by_id_with_context(
                    req.agent_id, CONTEXT_SECTION_TYPES, user_id=uid)
                if agent:
                    await _enforce_agent_access_policy(db, agent, uid)
            else:
                agent = await db.get_agent_for_user(uid)
            if not agent:
                return {"ok": False, "reason": "no_agent"}
            try:
                _meta = agent.get("metadata")
                if isinstance(_meta, str):
                    _meta = json.loads(_meta or "{}")
                _eng = str((_meta or {}).get("engine") or "").strip()
                if _eng and _eng != "default":
                    return {"ok": False, "reason": "engine_agent"}
            except Exception:
                pass

            agent_id = agent.get("id")
            tools_version = agent.get("updated_at")

            # Build tools in a worker thread, then the
            # history (kept on the loop because its compaction step may call the
            # LLM). These are the same reads a turn makes.
            _factories = [
                lambda: _cached_load_tools(
                    uid, agent_id or "",
                    agent.get("template_id"), tools_version),
            ]
            _res = await _gather_db_concurrent(*_factories)
            if isinstance(_res[0], BaseException):
                return {"ok": False, "reason": "tools"}
            tools = _res[0]
            history = await build_openai_history_from_session(
                db, uid, req.session_id, agent_id=agent_id)

            turn_prewarm.store(
                req.session_id,
                sig=(uid, agent_id, tools_version),
                tools=tools, history=history,
            )
        return {"ok": True}
    except Exception as e:
        logger.debug("prewarm failed for %s: %s", req.session_id, e)
        return {"ok": False, "reason": "error"}



@router.post("", response_model=ChatResponse)
async def chat(request: ChatRequest, fastapi_request: Request):
    """
    Process a chat message with the agent.

    Uses the simple agent loop with tool-calling support.
    """
    # Open ONE per-turn read-cache scope around the whole live send. The turn-cache
    # bulk optimisations (a single query for ALL of a user's vault secrets, per-turn
    # dedup of invariant reads) only fire inside such a scope. The live path used to
    # run WITHOUT one — so load_tools re-resolved every integration provider's
    # credential as its own remote round-trip on EVERY loop iteration (~70-100 serial
    # ~150ms round-trips/turn on remote Postgres). The scope collapses those to one.
    return await run_internal_session_turn(request, fastapi_request)


async def run_internal_session_turn(
    request: ChatRequest, fastapi_request: Request,
) -> ChatResponse:
    """Reusable buffered session-turn service for API and internal workers.

    Contract subprocesses invoke this service directly; they never loop back
    through the HTTP route or duplicate the agent-loop/session machinery.
    """
    with turn_cache_scope():
        return await _chat_impl(request, fastapi_request)


async def _chat_impl(request: ChatRequest, fastapi_request: Request):
    try:
        # Tenant isolation: the JWT subject must match the user_id the
        # client says it's chatting as. Every tool wrapper down the call
        # graph closes over this user_id, so getting it wrong here lets one
        # authenticated user impersonate another for the whole session.
        from app.auth.identity import assert_caller_is
        request.user_id = await assert_caller_is(fastapi_request, request.user_id)

        # Abuse guard for PUBLIC (embed / anonymous) chat only — no-op for
        # registered users. See app/api/rate_limit.py + the security audit.
        from app.api.rate_limit import enforce_tier_chat
        await enforce_tier_chat(
            request.user_id, fastapi_request, message=request.message or "",
            agent_id=request.agent_id or request.agent_template_id or "",
        )

        db = get_db()

        # ── Temp DB resolution for optimizer/closer sessions ──
        _temp_db_path = None
        if request.session_id.startswith('optimizer-') or request.session_id.startswith('closer-'):
            _meta_conn = db._get_conn()
            try:
                _meta_row = _meta_conn.execute(
                    "SELECT metadata FROM sessions WHERE id=?", (request.session_id,)
                ).fetchone()
                if _meta_row and _meta_row[0]:
                    _meta = json.loads(_meta_row[0])
                    _temp_db_path = _meta.get('temp_db_path')
            finally:
                _meta_conn.close()
            if _temp_db_path:
                from app.db.local import LocalBackend as _OptBackend
                db = _OptBackend(db_path=_temp_db_path)
                logger.info("Using temp DB for %s session: %s", request.session_id[:12], _temp_db_path)

        # ── Terminal tunnel: the user is driving a program directly ──
        # If this chat session is bound to a live terminal, the message is a
        # keystroke line for that program, not a prompt for the agent. Route it
        # to the PTY (persisted but context-excluded) and return — the slash
        # parser and the whole agent loop are bypassed. The user ends the tunnel
        # with the UI "Hand back" button, never by typing.
        try:
            from app.agent.terminal_tunnel import route_user_line as _route_tunnel
            if await _route_tunnel(db, request.user_id, request.session_id, request.message or ""):
                return ChatResponse(reply="", response="", session_id=request.session_id)
        except Exception as _tun_err:
            logger.warning("tunnel routing failed for %s: %s", request.session_id, _tun_err)

        # ── Handle slash commands ──
        # Built-in system commands first (not template-driven). /compact folds the
        # session's older turns now; see _handle_compact_command.
        if _is_compact_command(request.message or ""):
            compact_result = await _handle_compact_command(
                request.user_id, request.session_id, db,
            )
            result = (
                str(compact_result.get("message") or "")
                if isinstance(compact_result, dict) else str(compact_result)
            )
            return ChatResponse(reply=result, response=result, session_id=request.session_id)
        _slash_match = _match_slash_command(request.message or "")
        if _slash_match:
            _slash_key, _slash_arg, _slash_tid = _slash_match
            if _slash_tid == "optimizer":
                result = await _handle_optimize_command(
                    request.user_id, request.session_id,
                    _slash_arg, "web_portal", db,
                )
            else:
                result = await _handle_generic_slash_command(
                    _slash_tid, _slash_key, _slash_arg,
                    request.user_id, request.session_id, "web_portal", db,
                )
            return ChatResponse(reply=result, response=result, session_id=request.session_id)

        # Ensure the session exists before inserting interactions
        _session_title = _session_title_from_message(request.message) if (request.message or "").strip() else None
        await _ensure_session(db, request.user_id, request.session_id, title=_session_title)

        # ── Optimizer / Closer session: route to dedicated agent ──
        agent = None
        opt_role = None
        opt_template_id = None
        opt_metadata = {}
        if request.session_id.startswith('optimizer-') or request.session_id.startswith('closer-'):
            conn = db._get_conn()
            try:
                meta_row = conn.execute(
                    "SELECT metadata FROM sessions WHERE id=?", (request.session_id,)
                ).fetchone()
                opt_metadata = json.loads(meta_row[0]) if meta_row and meta_row[0] else {}
                opt_template_id = trigger_index.get('tool_call', 'run_optimizer') or 'optimizer'
            finally:
                conn.close()

            # Resolve agent in main local.db so it is accessible for UI edits,
            # then bind the session in temp DB if one is active.
            _agent_db = get_db() if _temp_db_path else db
            agent = await _agent_db.get_or_resolve_session_agent(
                session_id=request.session_id,
                user_id=request.user_id,
                template_id=opt_template_id,
            )
            if _temp_db_path and agent and agent.get("id"):
                try:
                    await db.bind_session_to_agent(request.session_id, agent["id"])
                except Exception:
                    pass
            if not agent:
                raise RuntimeError(
                    f"Failed to resolve optimizer agent (role={opt_role}) for user {request.user_id}. "
                    f"Check that agent template '{opt_template_id}' exists."
                )

        # ── Assign agent first (context rows are keyed by agent_id) ──
        if agent is None and getattr(request, 'agent_id', None):
            agent = await db.get_agent_by_id(request.agent_id)
        # Honor the session's existing binding before falling back to the user's
        # default agent. A loopback POST that omits agent_id for an ALREADY-BOUND
        # session (e.g. an orchestrator spawn's wait-run, which drives a
        # `spawn-…` session bound to its own helper agent, or the orchestrator
        # re-wake) would otherwise resolve to the user's default agent — which
        # then trips the bind-mismatch guard below with a 500, so the spawn never
        # runs. Resolving via the binding here makes the resolved agent match the
        # bound one. New (unbound) sessions fall through unchanged.
        if agent is None:
            _bound_id = await db.get_session_agent_id(request.session_id)
            if _bound_id:
                agent = await db.get_agent_by_id(_bound_id)
        if agent is None:
            agent = await db.get_agent_for_user(request.user_id)
        if agent is None:
            from app.api.agents import provision_default_agent
            agent = await provision_default_agent(db, request.user_id)
        if agent is None:
            raise HTTPException(
                status_code=400,
                detail="No agent assigned. Create an agent before chatting.",
            )
        if os.environ.get("WEBAGENT_CONTRACT_SUBPROCESS") == "1":
            from app.agent.contract_permissions import clamp_runtime_agent
            agent = clamp_runtime_agent(agent)

        # ── Bind session to agent ──
        # The streaming send path does this in _prepare_send; the buffered path must
        # too, or sessions.agent_id stays NULL and anything that resolves the session's
        # agent later (e.g. /compact via get_session_agent_id) silently misfires.
        _existing_agent_id = await db.get_session_agent_id(request.session_id)
        if _existing_agent_id is None:
            await db.bind_session_to_agent(request.session_id, agent["id"])
        elif _existing_agent_id != agent["id"]:
            raise RuntimeError(
                f"Session {request.session_id[:8]} bound to agent {_existing_agent_id[:8]}, "
                f"but resolved agent is {agent['id'][:8]}. Cannot respond."
            )

        # ── Agent access policy enforcement ──
        await _enforce_agent_access_policy(db, agent, request.user_id)
        await _enforce_billing_access(db, agent, request.user_id, request.message or "")
        if str(request.user_id or "").startswith("anon_"):
            from app.agent.anonymous_data import enforce_anonymous_data_policy
            await asyncio.to_thread(
                enforce_anonymous_data_policy, db,
                user_id=request.user_id, session_id=request.session_id, agent=agent,
            )

        # ── Participants enforcement ──
        # Ensure the user and agent are registered as participants
        if not await db.is_session_participant(request.session_id, request.user_id, 'user'):
            await db.add_session_participant(request.session_id, request.user_id, 'user')
        if not await db.is_session_participant(request.session_id, agent["id"], 'agent'):
            await db.add_session_participant(request.session_id, agent["id"], 'agent')

        # ── Model Switcher reset: new user message → back to the agent's default
        # model. Any upgrade the agent applied for the previous turn group is
        # dropped here; the user's footer-picker selection survives.
        await _reset_agent_model_switcher(db, request.session_id, request.user_id)

        # Save user message and get its ID for parent linking
        # Optimizer/Closer sessions get source='optimizer' to distinguish from normal chats
        is_opt = request.session_id.startswith('optimizer-') or request.session_id.startswith('closer-')
        user_interaction_id = await db.insert_interaction(
            request.user_id, request.session_id, role="user", content=request.message,
            channel="web_portal",
            metadata=json.dumps({"source": "optimizer" if is_opt else "web_portal_chat",
                                 **({"attachment_ids": request.attachment_ids} if request.attachment_ids else {})}),
            sender_id=request.user_id,
            receiver_id=agent["id"],
            source="optimizer" if is_opt else None,
        )

        # Record the mode this turn runs in so the pill is restorable server-side.
        await _record_session_execution_mode(db, request.session_id, getattr(request, 'execution_mode', 'ask'))

        # ── Start a run buffer for this turn ──
        _run_buffer = await get_run_buffer_registry().start_turn(
            session_id=request.session_id,
            user_id=request.user_id,
            turn_id=user_interaction_id,
            db=db,
        )
        try:
            _user_ss, _user_ts = _run_buffer.next_seq()
            # Stamp the ordering number where the local-first row actually lives.
            # (A raw db._get_conn() UPDATE would hit the REMOTE authority, where the
            # just-inserted row doesn't exist yet, match zero rows, and leave the row
            # NULL-session_seq forever — invisible to the reconcile tail.)
            await db_offload(lambda: db.stamp_interaction_seq(
                user_interaction_id, _user_ss, user_interaction_id, _user_ts))
        except Exception as _seqerr:
            logger.debug("Failed to backfill seq on user row (buffered): %s", _seqerr)

        # ── Emit user message to visualizer listeners ──
        await _emit_to_visualizers(request.session_id, {
            "type": "user_message", "level": "user",
            "content": request.message, "id": user_interaction_id,
        })

        # ── Build loop config for pre-loop gating ──
        loop_config = LoopConfig.from_agent(agent)

        # ── Agent context docs (already included by get_or_resolve_session_agent / get_agent_for_user) ──
        context_docs = agent.get("context_documents", [])

        # Non-optimizer agents: copy defaults if no context docs exist
        if not context_docs and not is_opt and loop_config.is_enabled("copy_defaults"):
            copied = await db.copy_defaults_to_agent(agent["id"], template_id='default')
            if copied > 0:
                agent = await db.fetch_agent_by_id_with_context(agent["id"], CONTEXT_SECTION_TYPES, user_id=request.user_id)
                context_docs = agent.get("context_documents", [])

        # ── Pipeline: context loaded ──
        doc_types = list(set(
            (d.get("context_type") or d.get("doc_type") or "")
            for d in context_docs if d.get("context_type") or d.get("doc_type")
        ))
        await _emit_to_visualizers(request.session_id, {
            "type": "pipeline", "level": "pipeline",
            "step": "load_context", "count": len(context_docs),
            "types": doc_types,
        })

        # ── PHASE 1: Brain-first lookup (visible as tool interaction) ──
        # The lookup is LAUNCHED here but (when it runs) awaited later, just
        # before the system prompt is assembled — so its embedding round-trip
        # overlaps with attachment/image prep instead of stalling the turn (#2).
        brain_results = []
        brain_context = None
        parent_id = None
        _brain_task = None
        _ability_context_task = None
        if not _should_skip_memory(request.message):
            from app.abilities import prompt_context_for_agent
            _ability_context_task = asyncio.create_task(prompt_context_for_agent(
                agent["id"], request.user_id, request.message,
            ))
        if not loop_config.is_enabled("memory_search") or _should_skip_memory(request.message):
            _skip_reason = "node_disabled" if not loop_config.is_enabled("memory_search") else "greeting_or_cmd"
            await _emit_to_visualizers(request.session_id, {
                "type": "pipeline", "level": "pipeline",
                "step": "memory_search_skip", "reason": _skip_reason,
            })
            parent_id = await db.insert_interaction(
                request.user_id, request.session_id, role="tool",
                content=json.dumps({"skipped": True, "reason": _skip_reason}),
                parent_id=user_interaction_id,
                tool_name="memory_search",
                channel="web_portal",
                metadata=json.dumps({"brain": True, "skipped": True, "reason": _skip_reason,
                                     "args": {"query": request.message, "skipped": True}}),
                sender_id=agent["id"],
                receiver_id=agent["id"],
            )
            await _emit_to_visualizers(request.session_id, {
                "type": "db", "level": "db", "op": "insert_interaction",
                "role": "tool", "id": parent_id,
            }, db_override=db)
            await _emit_to_visualizers(request.session_id, {
                "type": "pipeline", "level": "pipeline",
                "step": "memory_search_end", "results_count": 0, "results": [],
                "skipped": True,
            })
        else:
            await _emit_to_visualizers(request.session_id, {
                "type": "pipeline", "level": "pipeline",
                "step": "memory_search_start", "query": request.message, "limit": 5,
            })
            # Short messages take the keyword-only fast path (no embedding) (#3).
            _use_vector = not _should_skip_vector(request.message)
            _brain_task = asyncio.create_task(
                db.memory_search(request.user_id, request.message, limit=5, vector=_use_vector)
            )

        # ── Resolve attachment references ──
        attachment_context = None
        attachment_docs: List[Dict[str, Any]] = []
        if request.attachment_ids:
            for att_id in request.attachment_ids:
                att = await db.get_attachment(att_id)
                if att:
                    attachment_docs.append(att)
            if attachment_docs:
                attachment_context = format_attachments_for_prompt(attachment_docs)
                await _emit_to_visualizers(request.session_id, {
                    "type": "attachment", "level": "agent",
                    "attachments": [
                        {"id": a["id"], "original_name": a["original_name"],
                         "mime_type": a["mime_type"], "size_bytes": a["size_bytes"],
                         "storage_path": a.get("storage_path", "")}
                        for a in attachment_docs
                    ],
                })
        _desc_out = {}
        async for _dev in _maybe_describe_images(
            db, request.user_id, request.message, user_interaction_id,
            loop_config, attachment_docs, _desc_out,
            agent_id=agent["id"], agent_rec=agent, session_id=request.session_id,
        ):
            await _emit_to_visualizers(request.session_id, _dev)
        # Launcher element-pickup fingerprint → foldable chip + this-turn fold.
        async for _dev in _maybe_emit_app_control(
            db, request, user_interaction_id, _desc_out,
            agent_id=agent["id"], session_id=request.session_id, channel="web_portal",
        ):
            await _emit_to_visualizers(request.session_id, _dev)
        user_message_content = await build_user_message_content(
            _desc_out.get("message_text", request.message),
            _desc_out.get("inline_docs", attachment_docs),
        )

        # ── PHASE 1 (cont.): await the memory lookup launched above ──
        # By awaiting here — after the attachment/image work — the embedding
        # round-trip has been overlapping with that prep, so it rarely adds
        # visible latency. Results are folded into the prompt and recorded as
        # the memory_search tool interaction (parent for the run that follows).
        if _brain_task is not None:
            brain_results = await _brain_task

            await _emit_to_visualizers(request.session_id, {
                "type": "pipeline", "level": "pipeline",
                "step": "memory_search_end", "results_count": len(brain_results),
                "results": [{"slug": r["slug"], "title": r.get("title", r["slug"]),
                             "score": round(r.get("rank", 0), 2)}
                            for r in (brain_results or [])],
            })

            # Format brain context for system prompt injection
            if brain_results:
                lines = []
                for r in brain_results:
                    slug = r.get("slug", "?")
                    title = r.get("title", slug)
                    ct = r.get("compiled_truth", "")[:300]
                    rank = r.get("rank", 0)
                    lines.append(f"## {slug} — {title} (score: {rank:.2f})")
                    if ct:
                        lines.append(ct)
                    lines.append("")
                brain_context = "\n".join(lines)

            # Save memory_search as tool interaction
            search_content = json.dumps({
                "query": request.message,
                "results": [
                    {"slug": r["slug"], "title": r.get("title",""),
                     "score": round(r.get("rank", 0), 2),
                     "snippet": r.get("compiled_truth", "")[:150]}
                    for r in (brain_results or [])
                ],
                "count": len(brain_results or []),
            }, indent=2)
            parent_id = await db.insert_interaction(
                request.user_id, request.session_id, role="tool",
                content=search_content,
                parent_id=user_interaction_id,
                tool_name="memory_search",
                channel="web_portal",
                metadata=json.dumps({
                    "count": len(brain_results or []),
                    "brain": True,
                    "has_results": bool(brain_results),
                    "args": {"query": request.message},
                }),
                output_data=search_content,
                sender_id=agent["id"],
                receiver_id=agent["id"],
            )

            await _emit_to_visualizers(request.session_id, {
                "type": "db", "level": "db", "op": "insert_interaction",
                "role": "tool", "id": parent_id,
            }, db_override=db)

            # Emit memory_search as a tool result
            await _emit_to_visualizers(request.session_id, {
                "type": "tool_result", "level": "agent",
                "tool": "memory_search",
                "result": search_content[:2000],
                "duration_ms": 0,
                "error": False,
            })

        if _ability_context_task is not None:
            ability_context = await _ability_context_task
            if ability_context:
                brain_context = "\n\n".join(
                    part for part in (brain_context, ability_context) if part
                )

        # Build system prompt with brain context + dynamic tools
        # context_docs is already the resolved per-caller slot list.
        _agent_id_for_prompt = agent.get("id") if agent else None
        system_prompt = await build_system_prompt(
            context_docs, brain_context, request.user_id,
            agent_id=_agent_id_for_prompt,
            session_id=request.session_id,
        )
        if attachment_context:
            system_prompt = system_prompt + "\n\n" + attachment_context
        system_prompt = await append_skills_section(system_prompt, agent, request.session_id, caller_user_id=request.user_id)

        # ── Pipeline: prompt built ──
        from app.tools.loader import load_tools
        tools = await load_tools(request.user_id,
                                 agent_id=_agent_id_for_prompt or "",
                                 agent_template_id=agent.get("template_id") if agent else None,
                                 gate_caller_access=True)
        tool_count_for_prompt = len(tools)
        section_names = ["SYSTEM"]  # Simplified section count — actual sections are dynamic

        await _emit_to_visualizers(request.session_id, {
            "type": "pipeline", "level": "pipeline",
            "step": "build_prompt", "sections": section_names,
            "brain_injected": bool(brain_context),
            "tool_count_in_prompt": tool_count_for_prompt,
        })

        # DB-backed conversation history (same session survives browser refresh)
        exclude_ids = {user_interaction_id} if user_interaction_id else set()
        # Build conversation history from DB. For closer sessions, all context
        # (judging criteria, baseline, trial transcripts) is pre-injected as real
        # interaction rows in the temp DB, so the standard
        # history builder works for both planner and closer sessions.
        history = await build_openai_history_from_session(
            db, request.user_id, request.session_id,
            exclude_interaction_ids=exclude_ids,
            agent_id=agent.get("id"),
        )

        # Create event callback that pushes to visualizer and user listeners
        async def event_callback(event: Dict[str, Any]):
            await _emit_to_visualizers(
                request.session_id, event, user_id=request.user_id, db_override=db
            )

        # Run the agent loop (with 5-minute timeout)
        # Resolve allowed_tools from agent config (may be list or JSON string)
        _raw_allowed = agent.get("allowed_tools", [])
        if isinstance(_raw_allowed, str):
            import json as _json
            try:
                _raw_allowed = _json.loads(_raw_allowed)
            except Exception:
                _raw_allowed = []
        assistant_reply = await run_agent_loop_buffered(
            user_id=request.user_id,
            session_id=request.session_id,
            user_message=user_message_content,
            system_prompt=system_prompt,
            agent_id=agent["id"],
            history=history,
            parent_interaction_id=parent_id,
            run_turn_id=user_interaction_id,
            event_callback=event_callback,
            max_turns=agent.get("max_turn_count", 0),
            channel="web_portal",
            timeout_seconds=300,
            db=db,
            agent_template_id=agent.get("template_id"),
            allowed_tools=_raw_allowed or None,
            execution_mode=getattr(request, 'execution_mode', 'ask') or 'ask',
        )

        # ── PHASE 3: Background memory save (visible tool interaction) ──
        # Skip if agent has disabled memory_save via allowed_tools or loop_logic
        if 'memory_save' not in set(_raw_allowed or []) and loop_config.is_enabled("memory_save"):
            asyncio.create_task(_save_chat_to_memory(
                db, request.user_id, request.session_id,
                request.message, assistant_reply, agent["id"], parent_id,
            ))
            # ── Pipeline: memory save notification ──
            await _emit_to_visualizers(request.session_id, {
                "type": "pipeline", "level": "pipeline",
                "step": "memory_save_start", "slug": f"chat/{request.session_id[:8]}",
            })

        # End the run buffer for this turn — starts the retention countdown.
        try:
            await get_run_buffer_registry().end_turn(request.session_id, db=db)
        except Exception as _eb:
            logger.debug("end_turn failed (buffered) for session %s: %s", request.session_id, _eb)

        return ChatResponse(
            reply=assistant_reply,
            response=assistant_reply,
            session_id=request.session_id,
        )

    except HTTPException:
        # Admission, entitlement and policy denials already carry their stable
        # status/detail payloads (notably anonymous budget exhaustion). Preserve
        # them instead of translating every denial into an opaque HTTP 500.
        raise
    except Exception as e:
        # Don't strand the brain lookup we may have fired earlier — cancel it so it
        # doesn't run on detached against the shared embedding client after we 500.
        _cancel_if_pending(locals().get("_brain_task"))
        # Make sure we mark the run buffer ended even on error path.
        try:
            await get_run_buffer_registry().end_turn(request.session_id, db=db)
        except Exception:
            pass
        logger.error(f"Chat endpoint error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


def _find_interaction_by_cmid(db, session_id: str, client_msg_id: str) -> Optional[str]:
    """Return the id of an already-persisted user interaction carrying this
    client message id (stored in metadata as ``cmid``), or None.

    Makes /send idempotent: the browser keeps unconfirmed sends in a localStorage
    outbox and retries them (see ui/chat/js/chat-send.js). Without this,
    a retry of a message the server already accepted inserts a duplicate row (the
    "it keeps resending to the DB" symptom). The id is matched with a LIKE against
    the metadata JSON; ``_``/``%``/``\\`` in the id are escaped so they can't act
    as LIKE wildcards. Best-effort: callers treat any error as "not found" and fall
    through to a normal insert, so this never blocks a legitimate send.
    """
    esc = client_msg_id.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = '%"cmid": "' + esc + '"%'
    conn = db._get_conn()
    try:
        row = conn.execute(
            "SELECT id FROM interactions WHERE session_id=? AND role='user' "
            "AND metadata LIKE ? ESCAPE '\\' LIMIT 1",
            (session_id, pattern),
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


# ── Idempotent-send serialization (cmid TOCTOU guard) ──
# The dedupe check + the user-row insert inside _prepare_send_inner straddle slow
# prep (attachment load, image-describe — seconds). Two sends carrying the SAME
# client_msg_id — the outbox retrying an unconfirmed send, the exact case the
# feature exists for — could BOTH pass the "have I seen this cmid?" check before
# either inserted, double-inserting the message and starting two runs. We
# serialize same-cmid sends on a per-cmid lock so the retry waits, then sees the
# first insert and short-circuits as a duplicate. Process-local: closes the
# common case (one browser → one server via its per-user WS); a cross-process
# dupe of an identical cmid is out of scope (that needs a DB unique constraint).
_CMID_LOCKS: Dict[str, asyncio.Lock] = {}
_CMID_LOCK_REFS: Dict[str, int] = {}


def _cmid_lock_acquire_ref(cmid: str) -> asyncio.Lock:
    """Get (or create) the lock for this cmid and bump its refcount. Sync + no
    await, so it is atomic w.r.t. other coroutines on the single event loop."""
    lock = _CMID_LOCKS.get(cmid)
    if lock is None:
        lock = asyncio.Lock()
        _CMID_LOCKS[cmid] = lock
    _CMID_LOCK_REFS[cmid] = _CMID_LOCK_REFS.get(cmid, 0) + 1
    return lock


def _cmid_lock_release_ref(cmid: str) -> None:
    """Drop a ref; evict the lock when the last holder/waiter is gone so the
    registry can't grow unbounded across many distinct message ids."""
    n = _CMID_LOCK_REFS.get(cmid, 0) - 1
    if n <= 0:
        _CMID_LOCK_REFS.pop(cmid, None)
        _CMID_LOCKS.pop(cmid, None)
    else:
        _CMID_LOCK_REFS[cmid] = n


# Recently-accepted (session, cmid) → user-row id, kept IN PROCESS. The per-cmid
# lock serializes two same-cmid sends, but the second's DB dedupe read may not yet
# see the first's just-committed insert (remote Postgres read-after-write across
# pooled connections isn't instant — a plain DB check alone still lets the racing
# pair both run). This map gives the serialized second send IMMEDIATE visibility
# of the first's insert without a DB round-trip, closing the race deterministically.
# Bounded FIFO so it can't grow without limit.
_RECENT_CMIDS: Dict[str, str] = {}
_RECENT_CMIDS_CAP = 4096
# Slash commands return before a normal user interaction is inserted, so their
# result cannot be recovered by _find_interaction_by_cmid. Keep their accepted
# result in the same bounded, process-local idempotency lane. The per-cmid lock
# above serializes an outbox retry behind the original command.
_RECENT_COMMAND_RESULTS: Dict[str, str] = {}


def _recent_cmid_key(session_id: str, cmid: str) -> str:
    return session_id + "\x1f" + cmid


def _recent_cmid_get(session_id: str, cmid: str) -> Optional[str]:
    return _RECENT_CMIDS.get(_recent_cmid_key(session_id, cmid))


def _recent_cmid_put(session_id: str, cmid: str, interaction_id: str) -> None:
    _RECENT_CMIDS[_recent_cmid_key(session_id, cmid)] = interaction_id
    # Dict preserves insertion order; drop the oldest once over the cap.
    while len(_RECENT_CMIDS) > _RECENT_CMIDS_CAP:
        try:
            _RECENT_CMIDS.pop(next(iter(_RECENT_CMIDS)))
        except (StopIteration, KeyError):
            break


def _recent_command_get(session_id: str, cmid: Optional[str]) -> Optional[str]:
    if not cmid:
        return None
    return _RECENT_COMMAND_RESULTS.get(_recent_cmid_key(session_id, cmid))


def _recent_command_put(session_id: str, cmid: Optional[str], result: str) -> None:
    if not cmid:
        return
    _RECENT_COMMAND_RESULTS[_recent_cmid_key(session_id, cmid)] = result
    while len(_RECENT_COMMAND_RESULTS) > _RECENT_CMIDS_CAP:
        try:
            _RECENT_COMMAND_RESULTS.pop(next(iter(_RECENT_COMMAND_RESULTS)))
        except (StopIteration, KeyError):
            break


# ── Model Switcher auto-reset on a new user message ─────────────────────────
# The Model Switcher ability lets the agent upgrade THIS conversation onto a
# pricier model (use_premium_model / set_model) or raise reasoning effort
# (set_effort) for a hard stretch. Those writes are agent-driven and must not
# leak into the user's NEXT message: a fresh user turn always starts on the
# agent's DEFAULT model at default effort. The reset drops exactly the
# agent-driven parts (concrete-model override + bare-model-id effort entries)
# and leaves the user's own footer-picker slot selection and its per-slot
# efforts intact. (grep SISTER-SYNC: SESSION-MODEL-OVERRIDE)

async def _reset_agent_model_switcher(db, session_id: str, user_id: str = "") -> None:
    if not session_id:
        return
    try:
        # Only announce/reset when an AGENT-driven model pick is actually
        # active (set_model / use_premium_model wrote {type:'model', model:…}).
        # The user's own footer-picker slot (selection_type) is not agent-driven
        # and must not produce a "reverted" notice. Best-effort read.
        _had_agent_pick = False
        try:
            _cur = await db.get_session_llm_override(session_id)
            if isinstance(_cur, dict) and (
                _cur.get("type") == "model" or bool(_cur.get("model"))):
                _had_agent_pick = True
        except Exception:
            pass
        if _had_agent_pick:
            # Durable twin of the footer revert: the agent's temporary upgrade
            # (or effort raise) is cleared before the user's next turn — persist
            # a system:model row so the transcript shows the revert happened and
            # why (initiator='agent', the run-end cleanup, not the user).
            try:
                await db.add_model_switch_notice(
                    user_id, session_id, "Standard model enabled",
                    initiator="agent", tool="reset",
                    reason="agent model override cleared at a new turn",
                    from_state="override", to_state="default")
            except Exception:  # noqa: BLE001
                pass
            clear = getattr(db, "clear_session_agent_model_override", None)
            if clear:
                await clear(session_id)
            # Tell the chat UI the agent-driven override is gone so the footer
            # model changer drops its warning color (see model_switcher.py
            # _emit_override_state). user_id is required for the event to reach
            # the per-user chat WebSocket — without it the event is dropped (the
            # per-session visualizer registry is never populated). Best-effort —
            # never break the send path. (grep SESSION-MODEL-OVERRIDE-EMIT)
            try:
                await _emit_to_visualizers(session_id, {
                    "type": "model_override", "active": False, "model": "",
                }, user_id=user_id)
            except Exception:  # noqa: BLE001
                pass
    except Exception as _ms_err:  # noqa: BLE001
        logger.debug("model-switcher reset failed for %s: %s", session_id, _ms_err)


async def _prepare_send(request: ChatRequest, fastapi_request: Request) -> Dict[str, Any]:
    """Serialize sends that share a client_msg_id around the real prep so the
    dedupe check + insert can't race into a double-insert. No cmid → straight
    through (no contention)."""
    cmid = getattr(request, "client_msg_id", None)
    if not cmid:
        return await _prepare_send_inner(request, fastapi_request)
    lock = _cmid_lock_acquire_ref(cmid)
    try:
        async with lock:
            return await _prepare_send_inner(request, fastapi_request)
    finally:
        _cmid_lock_release_ref(cmid)


@with_turn_cache
async def _prepare_send_inner(request: ChatRequest, fastapi_request: Request) -> Dict[str, Any]:
    """Synchronous prep shared by /send and /stream.

    Does everything that must happen *before* we hand the turn to the Run
    Manager: auth, agent resolution + access/billing gating, session + participant
    setup, persisting the user message, and emitting the user_message event so it
    shows on every device instantly. Returns a dict the background turn executor
    needs, or ``{"slash_result": "..."}`` when the message was a slash command.

    NOTE: the RunBuffer + run-state are started inside the turn coroutine (not
    here), so that when a new message INTERRUPTS an active run, the prior run's
    finalize and the new run's begin can't race on the single session_runs row.
    A new message never refuses — it interrupts the current run (see
    RunManager.start_or_replace).
    """
    from app.auth.identity import assert_caller_is
    _perf = _PerfTimer("prepare_send", session_id=request.session_id, user_id=request.user_id)
    request.user_id = await assert_caller_is(fastapi_request, request.user_id)
    _perf.mark("auth_done")

    # Abuse guard for PUBLIC (embed / anonymous) chat only — no-op for registered
    # users. This is the shared /send + /stream lane the embed widget uses, so the
    # guard MUST live here too (not just the blocking /chat path). See the security
    # audit's CRITICAL finding + app/api/rate_limit.py.
    from app.api.rate_limit import enforce_tier_chat
    await enforce_tier_chat(
        request.user_id, fastapi_request, message=request.message or "",
        agent_id=request.agent_id or request.agent_template_id or "",
    )

    db = get_db()
    channel = "web_portal"

    # ── Temp DB resolution for optimizer/closer sessions ──
    _temp_db_path = None
    if request.session_id.startswith('optimizer-') or request.session_id.startswith('closer-'):
        _meta_conn = db._get_conn()
        try:
            _meta_row = _meta_conn.execute(
                "SELECT metadata FROM sessions WHERE id=?", (request.session_id,)
            ).fetchone()
            if _meta_row and _meta_row[0]:
                _meta = json.loads(_meta_row[0])
                _temp_db_path = _meta.get('temp_db_path')
        finally:
            _meta_conn.close()
        if _temp_db_path:
            from app.db.local import LocalBackend as _OptBackend
            db = _OptBackend(db_path=_temp_db_path)
            logger.info("Using temp DB for %s session: %s", request.session_id[:12], _temp_db_path)

    # ── Terminal tunnel: the user is driving a program directly ──
    # If this chat is bound to a live terminal, the message is a keystroke line
    # for that program, not a prompt for the agent. Route it to the PTY
    # (persisted but context-excluded) and stop — no slash parsing, no agent run.
    # The user ends the tunnel with the UI "Hand back" button, never by typing.
    try:
        from app.agent.terminal_tunnel import route_user_line as _route_tunnel
        if await _route_tunnel(db, request.user_id, request.session_id, request.message or ""):
            return {"tunnel_handled": True}
    except Exception as _tun_err:
        logger.warning("tunnel routing failed for %s: %s", request.session_id, _tun_err)

    # ── Handle slash commands ──
    # Built-in system commands first (not template-driven). /compact folds the
    # session's older turns now; see _handle_compact_command.
    client_msg_id = getattr(request, "client_msg_id", None)
    _prior_command_result = _recent_command_get(request.session_id, client_msg_id)
    if _prior_command_result is not None:
        logger.info(
            "Duplicate command for session %s (cmid=%s) — returning cached result",
            request.session_id[:12], client_msg_id,
        )
        if isinstance(_prior_command_result, dict):
            return {
                "slash_result": str(_prior_command_result.get("message") or ""),
                "compaction": _prior_command_result.get("compaction"),
            }
        return {"slash_result": _prior_command_result}
    if _is_compact_command(request.message or ""):
        # Mark this session compacting so concurrent sends are durably queued
        # instead of racing the fold (see _is_compacting / chat_send), and
        # broadcast the state so EVERY device shows "Compacting…" above the
        # pill and locks its composer for the duration.
        _COMPACTING_SESSIONS.add(request.session_id)
        try:
            try:
                await notify_user(request.user_id, {
                    "type": "agent_status", "status": "compacting",
                    "session_id": request.session_id,
                })
            except Exception:
                pass
            result = await _handle_compact_command(request.user_id, request.session_id, db)
        finally:
            _COMPACTING_SESSIONS.discard(request.session_id)
            try:
                await notify_user(request.user_id, {
                    "type": "agent_status", "status": "compact_done",
                    "session_id": request.session_id,
                })
            except Exception:
                pass
        _recent_command_put(request.session_id, client_msg_id, result)
        compact_event = result.get("compaction") if isinstance(result, dict) else None
        result_text = (
            str(result.get("message") or "")
            if isinstance(result, dict) else str(result)
        )
        # Drain messages that were durably queued while this compaction ran —
        # they now run as normal turns (their pending bubbles clear on the
        # 'running' broadcast). A drain failure must never hide the result.
        try:
            await _drain_compaction_queue(
                db, request.session_id, request.user_id, channel,
                getattr(request, "execution_mode", "ask") or "ask",
            )
        except Exception as _dq:
            logger.warning("compaction drain failed for %s: %s",
                           request.session_id[:12], _dq)
        return {"slash_result": result_text, "compaction": compact_event}
    _slash_match = _match_slash_command(request.message or "")
    if _slash_match:
        _slash_key, _slash_arg, _slash_tid = _slash_match
        if _slash_tid == "optimizer":
            # Pass the parsed argument (text AFTER the command) as feedback — not the
            # whole raw message. Passing request.message smuggled the literal
            # "/optimize" in as user feedback to the Planner.
            result = await _handle_optimize_command(
                request.user_id, request.session_id, _slash_arg, channel, db,
            )
        else:
            result = await _handle_generic_slash_command(
                _slash_tid, _slash_key, _slash_arg,
                request.user_id, request.session_id, channel, db,
            )
        _recent_command_put(request.session_id, client_msg_id, result)
        return {"slash_result": result}

    # ── Message length cap (App Functions toggle) ──
    # Truncate overly-long messages server-side so both normal chat and genui
    # dispatches (which POST to the same /send endpoint) are capped at a single
    # enforcement point. When OFF, messages pass through uncapped.
    # A user-facing system notice is appended to the message so the sender sees
    # the truncation in the chat bubble. The cap is adjustable in App Settings.
    msg_str = request.message or ""
    _truncated_from_len = 0
    if msg_str:
        try:
            from app.abilities import app_function_enabled
            if app_function_enabled("message_length_cap"):
                from app.admin.ability_config import get_ability_config
                cfg = get_ability_config("message_length_cap")
                max_len_raw = cfg.get("max_length", 8000) if isinstance(cfg, dict) else 8000
                try:
                    max_len = int(float(max_len_raw))
                except (TypeError, ValueError):
                    max_len = 8000
                if max_len < 1:
                    max_len = 8000
                if len(msg_str) > max_len:
                    _truncated_from_len = len(msg_str)
                    _notice = (
                        f"[System: Your message was truncated from {_truncated_from_len:,} characters "
                        f"to the {max_len:,}-character limit. The cap can be adjusted or disabled "
                        f"in App Settings \u2192 App Functions \u2192 Message Length Cap.]\n\n"
                    )
                    request.message = _notice + msg_str[:max_len]
        except Exception:
            pass  # fail open — never block a send on config read errors

    # Ensure the session exists before inserting interactions. A freshly-created
    # session row means this is the first message of a brand-new session, so
    # there is no prior history to load — the turn executor uses this to skip the
    # history read and its "Loading history" pill step.
    _session_title = _session_title_from_message(request.message) if (request.message or "").strip() else None
    _is_first_turn = await _ensure_session(db, request.user_id, request.session_id, title=_session_title)

    # ── Recycled-session gate ──
    # A message sent to a session sitting in the recycle bin must NOT revive it
    # (that is a safety risk the user controls manually from the Sessions page).
    # We persist the message so the sender's intent is recorded, emit a visible
    # system notice so the user knows why nothing happened, and return early —
    # no agent run, no crash storm, no wasted LLM calls.
    try:
        _dead = await db.is_session_dead(request.session_id)
    except Exception:
        _dead = False
    if _dead:
        _user_id_for_notice = request.user_id or ""
        _sid = request.session_id
        try:
            _notice = (
                "This session is in the recycle bin and cannot accept messages. "
                "Restore it from the Sessions page first."
            )
            _user_content = request.message or ""
            _first_seq = await db.next_session_seq(_sid, count=2)
            await db.insert_interaction(
                _user_id_for_notice, _sid, role="user", content=_user_content,
                channel=channel,
                metadata=json.dumps({"source": "web_portal_chat"}),
                sender_id=_user_id_for_notice,
                receiver_id="",
                session_seq=_first_seq,
            )
            await db.insert_interaction(
                _user_id_for_notice, _sid, role="system", content=_notice,
                channel=channel,
                metadata=json.dumps({"source": "recycled_session"}),
                sender_id="system",
                receiver_id=_user_id_for_notice,
                session_seq=_first_seq + 1,
            )
            await _emit_to_visualizers(_sid, {
                "type": "user_message", "level": "user",
                "content": _user_content,
            }, user_id=_user_id_for_notice)
            await _emit_to_visualizers(_sid, {
                "type": "agent_message", "level": "system",
                "content": _notice,
            }, user_id=_user_id_for_notice)
        except Exception as _recycle_err:
            logger.warning("Recycled-session notice failed for %s: %s", _sid[:12], _recycle_err)
        logger.info("Message to recycled session %s — saved + system notice, no agent run", _sid[:12])
        return {
            "db": db,
            "agent": {"id": ""},
            "user_interaction_id": "",
            "channel": channel,
            "is_first_turn": _is_first_turn,
            "recycled": True,
        }

    # ── Idempotent send (dedupe outbox retries) ──
    # The browser parks unconfirmed sends in a localStorage outbox and re-POSTs
    # them every few seconds until it gets a clean success. If the original send
    # actually landed but the acknowledgement never made it back (slow remote DB,
    # dropped connection), each retry would otherwise insert the SAME message
    # again. We short-circuit here — before the costly agent/billing round-trips —
    # when this client message id was already accepted, returning the original
    # turn so the retry resolves without a duplicate row or a second agent run.
    if client_msg_id:
        # In-process first: catches a concurrent duplicate whose DB write isn't yet
        # visible to this read's connection (the per-cmid lock serialized us behind
        # the first send, which recorded here right after its insert). Fall back to
        # the DB check for cross-process / post-restart retries.
        _existing_id = _recent_cmid_get(request.session_id, client_msg_id)
        if not _existing_id:
            try:
                _existing_id = _find_interaction_by_cmid(db, request.session_id, client_msg_id)
            except Exception as _dedup_err:
                logger.debug("client_msg_id dedupe lookup failed (will insert): %s", _dedup_err)
                _existing_id = None
        if _existing_id:
            logger.info(
                "Duplicate send for session %s (cmid=%s) — returning existing turn %s",
                request.session_id[:12], client_msg_id, _existing_id,
            )
            return {"duplicate": True, "user_interaction_id": _existing_id}

    # ── Model Switcher reset: a NEW user message always starts on the agent's
    # default model. Any model/effort the agent switched to for the previous
    # turn group (use_premium_model / set_model / set_effort) is dropped here —
    # it never carries into the user's next message. The user's own footer
    # picker selection is slot-based and survives untouched.
    await _reset_agent_model_switcher(db, request.session_id, request.user_id)

    # ── Optimizer / Closer session: route to dedicated agent ──
    opt_template_id = None
    opt_metadata = {}
    if request.session_id.startswith('optimizer-') or request.session_id.startswith('closer-'):
        conn = db._get_conn()
        try:
            meta_row = conn.execute(
                "SELECT metadata FROM sessions WHERE id=?", (request.session_id,)
            ).fetchone()
            opt_metadata = json.loads(meta_row[0]) if meta_row and meta_row[0] else {}
            opt_template_id = trigger_index.get('tool_call', 'run_optimizer') or 'optimizer'
        finally:
            conn.close()

        _agent_db = get_db() if _temp_db_path else db
        agent = await _agent_db.get_or_resolve_session_agent(
            session_id=request.session_id,
            user_id=request.user_id,
            template_id=opt_template_id,
        )
        if _temp_db_path and agent and agent.get("id"):
            try:
                await db.bind_session_to_agent(request.session_id, agent["id"])
            except Exception:
                pass
        if not agent:
            raise RuntimeError(
                f"Failed to resolve optimizer agent (role={opt_role}) for user {request.user_id}. "
                f"Check that agent template '{opt_template_id}' exists."
            )
    else:
        req_agent_id = getattr(request, 'agent_id', None)
        req_template = getattr(request, 'agent_template_id', None)
        if req_template in ('admin-agent', 'integration-admin-agent', 'web-agent-tui'):
            if not await db.is_user_admin(request.user_id):
                raise HTTPException(status_code=403, detail="This agent is only available to admin users.")
            agent = await db.get_or_resolve_session_agent(
                session_id=request.session_id,
                user_id=request.user_id,
                template_id=req_template,
            )
        elif req_agent_id:
            # Fetch WITH context here so the turn executor doesn't re-fetch it
            # later (one DB round-trip saved per message — see the note below).
            agent = await db.fetch_agent_by_id_with_context(
                req_agent_id, CONTEXT_SECTION_TYPES, user_id=request.user_id)
            if agent:
                admin_users = agent.get("admin_users") or []
                if isinstance(admin_users, str):
                    try:
                        admin_users = json.loads(admin_users)
                    except Exception:
                        admin_users = []
                if request.user_id not in admin_users:
                    await db.add_agent_member(req_agent_id, request.user_id)
        else:
            # No explicit agent requested. Honor the session's existing binding
            # before defaulting. A loopback POST that omits agent_id for an
            # already-bound session (e.g. an orchestrator spawn's wait-run driving
            # a `spawn-…` session bound to its own helper agent, or the
            # orchestrator re-wake) would otherwise resolve to the user's default
            # agent and trip the bind-mismatch guard below with a 500 — so the
            # spawn never runs. Resolving via the binding makes the resolved agent
            # match the bound one. A genuinely new/unbound session (no binding
            # row yet) falls through to the default-agent fallback unchanged.
            agent = None  # must be bound before the None-check below (fresh unbound session)
            _bound_id = await db.get_session_agent_id(request.session_id)
            if _bound_id:
                agent = await db.fetch_agent_by_id_with_context(
                    _bound_id, CONTEXT_SECTION_TYPES, user_id=request.user_id)
            if agent is None:
                agent = await db.get_agent_for_user(request.user_id)
        if agent is None:
            # When shared default agent is enabled, every user resolves to the
            # single app-level agent — no per-user agent row is needed.
            from app.admin.settings import shared_default_agent_enabled as _sd_chat
            if _sd_chat():
                from app.api.agents import provision_default_agent
                agent = await provision_default_agent(db, request.user_id)
        if agent is None:
            raise HTTPException(
                status_code=400,
                detail="No agent assigned. Create an agent before chatting.",
            )

    # Resolve the agent WITH its context documents once, here. The turn executor
    # needs the context-loaded agent and would otherwise re-fetch it on every
    # message (a full extra DB round-trip — cheap on local SQLite, but costly on
    # a remote Postgres). get_or_resolve_session_agent already includes context;
    # get_agent_by_id / get_agent_for_user do not, so top it up when missing.
    if agent and agent.get("id") and not agent.get("context_documents"):
        try:
            _agent_with_ctx = await db.fetch_agent_by_id_with_context(
                agent["id"], CONTEXT_SECTION_TYPES, user_id=request.user_id)
            if _agent_with_ctx is not None:
                agent = _agent_with_ctx
        except Exception as _ctx_err:
            logger.debug("agent context prefetch failed (will re-fetch later): %s", _ctx_err)

    _perf.mark("agent_resolved", agent_id=(agent.get("id") if agent else None))

    # Hard persistence fence: even a stale client or direct API caller cannot
    # send a Portal agent through the legacy endpoint. This check is before
    # session binding, participant bookkeeping, and insert_interaction.
    if _is_codex_portal_agent(agent):
        raise HTTPException(
            status_code=409,
            detail="Codex Portal tasks must be sent through the Portal endpoint; no WebAgent session or interaction was created.",
        )

    # ── Access policy + billing enforcement + participant-state reads ──
    # All five are independent given the resolved agent: the access-policy check,
    # the billing check, the session's currently-bound agent, and whether the user
    # / agent are already participants. Each is PURE DB (no LLM / embedding / WS
    # client — safe to drive on a worker loop). Run serially they were ~5 stacked
    # remote round-trips (~2-3s); overlapped in worker threads they finish in about
    # one. Access/billing denials surface as an HTTPException in their result slot,
    # which we re-raise; a failed participant read is treated as "needs the write"
    # (the underlying add/bind ops are idempotent), the safe default.
    _access_r, _billing_r, _bound_agent, _user_is_part, _agent_is_part = await _gather_db_concurrent(
        lambda: _enforce_agent_access_policy(db, agent, request.user_id),
        lambda: _enforce_billing_access(db, agent, request.user_id, request.message or ""),
        lambda: db.get_session_agent_id(request.session_id),
        lambda: db.is_session_participant(request.session_id, request.user_id, 'user'),
        lambda: db.is_session_participant(request.session_id, agent["id"], 'agent'),
    )
    # Re-raise an access/billing denial (HTTPException) or any hard error from them.
    for _r in (_access_r, _billing_r):
        if isinstance(_r, BaseException):
            raise _r
    if str(request.user_id or "").startswith("anon_"):
        from app.agent.anonymous_data import enforce_anonymous_data_policy
        await asyncio.to_thread(
            enforce_anonymous_data_policy, db,
            user_id=request.user_id, session_id=request.session_id, agent=agent,
        )
    _perf.mark("access_billing_done")
    _perf.mark("participants_read")
    existing_agent_id = None if isinstance(_bound_agent, BaseException) else _bound_agent
    if existing_agent_id is not None and existing_agent_id != agent["id"]:
        # Correctness guard stays on the critical path — a session already bound to
        # a DIFFERENT agent must fail loudly, not respond as the wrong agent.
        raise RuntimeError(
            f"Session {request.session_id[:8]} bound to agent {existing_agent_id[:8]}, "
            f"but resolved agent is {agent['id'][:8]}. Cannot respond."
        )

    # The bind + participant-row writes are IDEMPOTENT bookkeeping that nothing in
    # this turn reads back (the turn uses ``agent`` directly). On a remote DB they
    # were up to three serial writes (~1s) sitting in the blocking send path on the
    # first message of a session. Fire them in the background so /send returns as
    # soon as the user message is persisted; they settle within a second, off the
    # critical path.
    _need_bind = existing_agent_id is None
    _need_user_part = isinstance(_user_is_part, BaseException) or not _user_is_part
    _need_agent_part = isinstance(_agent_is_part, BaseException) or not _agent_is_part
    if _need_bind or _need_user_part or _need_agent_part:
        async def _session_bookkeeping():
            try:
                if _need_bind:
                    await db_offload(lambda: db.bind_session_to_agent(request.session_id, agent["id"]))
                if _need_user_part:
                    await db_offload(lambda: db.add_session_participant(request.session_id, request.user_id, 'user'))
                if _need_agent_part:
                    await db_offload(lambda: db.add_session_participant(request.session_id, agent["id"], 'agent'))
            except Exception as _bk_err:  # noqa: BLE001
                logger.debug("deferred session bookkeeping failed: %s", _bk_err)
        _spawn_bg(_session_bookkeeping(), label="session_bookkeeping")
    _perf.mark("participants_written")

    # A standalone "continue" is canonical authorization for a pending request
    # to move into write-capable Auto mode. Resolve it before this turn is
    # persisted so every downstream consumer (Run Scout, loop, recovery) sees
    # the same effective mode; do not infer consent from ordinary prose.
    _continue_to_auto = False
    if _CONTINUE_AUTHORIZATION_RE.fullmatch(request.message or ""):
        try:
            _prior_interactions = await db.fetch_interactions(
                request.user_id, request.session_id,
            )
            _continue_to_auto = _continue_authorizes_auto(
                request.message or "", _prior_interactions,
            )
        except Exception as _continue_auth_err:
            logger.debug("continue authorization lookup failed: %s", _continue_auth_err)
    if _continue_to_auto:
        request.execution_mode = "auto"

    # ── Persist the user message ──
    # Carry the client message id (cmid) in metadata so an outbox retry of this
    # exact send is recognised as a duplicate next time (see _find_interaction_by_cmid).
    _user_meta = {"source": "web_portal_chat"}
    # GenUI-page-initiated sends (field/button prompts) carry a friendly label so
    # the chat UI can render a green notice instead of the raw prompt as a "You"
    # bubble. The raw prompt is still the persisted user turn — the agent sees it
    # exactly as before; only the presentation changes.
    _genui_label = (getattr(request, "genui_label", None) or "").strip()
    if _genui_label:
        _user_meta["genui"] = True
        _user_meta["genui_label"] = _genui_label[:200]
    if client_msg_id:
        _user_meta["cmid"] = client_msg_id
    if request.attachment_ids:
        # The message -> attachment link (recovered on reload to re-render pasted
        # images/files) rides in metadata now that the input column is gone.
        _user_meta["attachment_ids"] = request.attachment_ids
    # Persist the deterministic task-boundary decision used by context
    # reduction. This is diagnostic metadata only; the async LLM tie-breaker
    # may still refine ambiguous rows when history is assembled.
    try:
        from app.admin.tasks import boundary_diagnostic
        _user_meta["task_boundary"] = boundary_diagnostic(
            request.message or "", first_turn=bool(_is_first_turn),
        )
    except Exception as _task_diag_err:
        logger.debug("task-boundary diagnostic unavailable: %s", _task_diag_err)
    user_interaction_id = await db.insert_interaction(
        request.user_id, request.session_id, role="user", content=request.message,
        channel=channel,
        metadata=json.dumps(_user_meta),
        sender_id=request.user_id,
        receiver_id=agent["id"],
    )
    _perf.mark("user_message_persisted", turn_id=user_interaction_id)
    # Publish to the in-process dedupe map BEFORE releasing the per-cmid lock (the
    # wrapper holds it until this coroutine returns), so a serialized same-cmid
    # retry sees it immediately even if the DB write isn't visible on its read yet.
    if client_msg_id:
        _recent_cmid_put(request.session_id, client_msg_id, user_interaction_id)

    # Record the mode this turn runs in so the pill is restorable server-side.
    # A canonical authorization changes the current turn's permission boundary,
    # so persist it before the background runner can observe a stale session mode.
    if _continue_to_auto:
        await _record_session_execution_mode(db, request.session_id, "auto")
        try:
            await db.add_execution_mode_notice(
                request.user_id, request.session_id, "auto",
                reason="user authorized the pending work with continue",
                turn_id=user_interaction_id, initiator="user",
            )
        except Exception as _continue_notice_err:
            logger.debug("continue authorization notice failed: %s", _continue_notice_err)
    else:
        # Pure UI-restore bookkeeping the turn never reads back — defer it off the
        # blocking send path (a read+write round-trip on remote Postgres every message).
        _spawn_bg(
            _record_session_execution_mode(db, request.session_id, getattr(request, 'execution_mode', 'ask')),
            label="record_execution_mode")

    # ── Emit the user message so all subscribed devices render it instantly ──
    # (The RunBuffer + run-state for the new turn are started inside the turn
    # coroutine. If an old run is still active, its buffer stamps this event;
    # seq stays monotonic across the interrupt.)
    _user_event = {
        "type": "user_message", "level": "user",
        "content": request.message, "id": user_interaction_id,
    }
    if _genui_label:
        _user_event["genui_label"] = _genui_label[:200]
    await _emit_to_visualizers(request.session_id, _user_event, user_id=request.user_id)
    if _continue_to_auto:
        await _emit_to_visualizers(request.session_id, {
            "type": "execution_mode", "level": "pipeline", "mode": "auto",
            "reason": "user authorized the pending work with continue",
        }, user_id=request.user_id)

    # ── TUI bridge check: if the agent is a TUI bridge agent, forward the
    # message to the TUI instead of running the normal agent loop.
    is_tui_bridge = (
        agent.get("trigger_type") == "tui_bridge"
        or agent.get("template_id") == "web-agent-tui"
    )

    _perf.mark("prepare_send_done", turn_id=user_interaction_id)

    return {
        "db": db,
        "agent": agent,
        "user_interaction_id": user_interaction_id,
        "channel": channel,
        "is_tui_bridge": is_tui_bridge,
        "is_first_turn": _is_first_turn,
    }


@with_turn_cache
async def run_shared_agent_turn(
    db, request: ChatRequest, agent: Dict[str, Any],
    user_interaction_id: str, channel: str = "web_portal",
    replaced: bool = False, is_first_turn: bool = False,
) -> None:
    """Execute one agent turn to completion, fully decoupled from any client
    connection. Owned by the Run Manager — survives the sender leaving, closing
    the browser, switching sessions/devices. Every event is emitted via
    ``_emit_to_visualizers`` (→ RunBuffer stamp + per-user WS broadcast), and the
    agent loop streams its answer into the DB. On finish, run-state is flipped to
    its terminal status and the RunBuffer retention countdown begins.

    ``replaced`` is True when this turn is replacing a run the user just
    interrupted by sending a new message; the agent is told so it can read the
    new message as a course-correction / stop / addition relative to its
    interrupted partial answer."""
    session_id = request.session_id
    user_id = request.user_id
    from app.chat.context import current as controlled_execution
    controlled = controlled_execution.get() is not None
    final_status = "complete"
    _last_seq_persist = 0.0
    _perf = _PerfTimer("run_turn", session_id=session_id, turn_id=user_interaction_id,
                       user_id=user_id, agent_id=(agent.get("id") if agent else None))
    _perf.mark("turn_start")
    try:
        from app.agent.manager_config import resolve_manager_loop as _resolve_manager_loop
        _manager_loop_cfg = _resolve_manager_loop(
            agent, getattr(request, "execution_mode", "ask") or "ask",
        )
        _raw_communication_cfg = _manager_loop_cfg.get("communication")
        _communication_cfg = (
            _raw_communication_cfg
            if isinstance(_raw_communication_cfg, dict) else {}
        )
    except Exception:
        _manager_loop_cfg = {}
        _communication_cfg = {}
    _working_intro_text = ""
    _working_intro_id = None

    # ── Run Scout: durable parallel intake ───────────────────────────────
    # Register the durable revision now, but defer worker launch until after the
    # primary model's working introduction has been sequenced. A replacement
    # appends the new message to the prior logical starter and invalidates any
    # older in-flight completion via a revision guard.
    _run_scout_row = None
    _scout_task = None
    _scout_launch_fn = None
    try:
        from app.agent.run_scout import begin_turn as _scout_begin, launch_analysis as _scout_launch
        _scout_launch_fn = _scout_launch
        _run_scout_row = await _scout_begin(
            db,
            session_id=session_id,
            user_id=user_id,
            agent_id=agent.get("id"),
            turn_id=user_interaction_id,
            message=request.message or "",
            execution_mode=getattr(request, "execution_mode", "ask") or "ask",
            replaced=replaced,
            agent_rec=agent,
        )
        _perf.mark(
            "starter_begin_done",
            starter_id=(_run_scout_row or {}).get("id"),
            revision=(_run_scout_row or {}).get("revision"),
        )
        _starter_cfg = (_run_scout_row or {}).get("starter_config") or {}
        _perf.mark(
            "starter_registered",
            parallel=bool(_starter_cfg.get("parallel", True)),
            launched=False,
        )
    except Exception as _rse:
        # Intake is supervisory.  It must fail off just like the Manager and
        # Closer rather than delaying or breaking the user's actual request.
        logger.debug("Run Scout launch failed for %s: %s", session_id[:12], _rse)
        _perf.mark("starter_failed", error_type=type(_rse).__name__)

    # App-wide session cap (app/agent/session_gate.py): if max_active_sessions
    # is reached, wait in the FIFO queue until an active session completes. The
    # user message is already persisted/emitted, so the sender sees it instantly;
    # the run simply starts when a slot frees. Fail-open: a gate error must
    # never block chat (release() then no-ops).

    # Register a one-shot callback that fires if this session enters the queue,
    # so the frontend can show the queue position in the session dropdown and
    # apply pending styling to the user's message bubble.
    _agent_id = agent.get("id") if agent else None
    _queue_cb_fired = False

    async def _on_gate_queue(sid: str, pos: int, total: int) -> None:
        nonlocal _queue_cb_fired
        _queue_cb_fired = True
        if not controlled:
            try:
                await notify_user(user_id, {
                    "type": "agent_status", "status": "queued",
                    "agent_id": _agent_id, "session_id": sid,
                    "turn_id": user_interaction_id,
                    "queue_position": pos, "queue_total": total,
                })
            except Exception:
                pass
        # Durable queued marker (DB fix): persist status='queued' on the user
        # row so a reload / navigate-away-and-back re-renders the queued bubble
        # with its Force run button from the transcript alone. Written ONLY
        # while the session is still in the gate queue — if the turn already
        # started (or was force-run) before this fire-and-forget task ran, the
        # marker is skipped so it can never clobber the cleared state. The
        # gate re-fires this callback as waiters ahead complete, so the write
        # is idempotent (same status re-written). Fail-open: a DB error must
        # not break the queue notification path.
        try:
            if session_gate.queued_position(sid) is not None:
                await db.update_interaction(user_interaction_id, status="queued")
        except Exception as _qe:
            logger.debug("persist queued marker failed: %s", _qe)

    session_gate.register_queue_callback(_on_gate_queue)
    try:
        try:
            await session_gate.acquire(session_id, user_id=user_id)
        except asyncio.CancelledError:
            raise
        except Exception as _ge:
            logger.debug("session gate acquire failed for %s: %s", session_id[:12], _ge)
    finally:
        session_gate.unregister_queue_callback(_on_gate_queue)

    # Clear the durable queued marker now that the run is actually starting.
    # This covers BOTH wake paths: a slot freeing naturally and the force-run
    # escape hatch (force_acquire wakes this same acquire() call). Guarded by
    # _queue_cb_fired so the hot path pays nothing when the gate never queued
    # this turn (cap 0 / slot free). Idempotent: no-op when no marker exists.
    # The callback's queued_position check prevents a late callback task from
    # re-writing the marker after this clear.
    if _queue_cb_fired:
        try:
            await db.update_interaction(user_interaction_id, status="complete")
        except Exception as _qe:
            logger.debug("clear queued marker failed: %s", _qe)

    # The controlled worker owns its lease and publications. The legacy
    # RunBuffer and session_runs row belong only to the V1 transport.
    if not controlled:
        _run_buffer = await get_run_buffer_registry().start_turn(
            session_id=session_id, user_id=user_id, turn_id=user_interaction_id, db=db,
        )
        try:
            _web_relaunch_ctx = json.dumps({
                "origin": "web", "session_id": session_id, "user_id": user_id,
                "agent_id": agent.get("id"), "channel": channel,
                "turn_id": user_interaction_id,
                "execution_mode": getattr(request, "execution_mode", "ask") or "ask",
            })
            await db.run_state_begin(
                session_id, user_id, agent.get("id"), user_interaction_id,
                origin="web", relaunch_ctx=_web_relaunch_ctx,
            )
        except Exception as _rse:
            logger.debug("run_state_begin failed: %s", _rse)
        try:
            _user_ss, _user_ts = _run_buffer.next_seq()
            await db_offload(lambda: db.stamp_interaction_seq(
                user_interaction_id, _user_ss, user_interaction_id, _user_ts))
        except Exception as _seqerr:
            logger.debug("Failed to backfill seq on user row: %s", _seqerr)

    final_error = None
    final_stop_cause = None

    async def event_callback(event: Dict[str, Any]):
        nonlocal final_status, final_error, final_stop_cause, _last_seq_persist
        et = event.get("type")
        await _emit_to_visualizers(
            session_id, event, user_id=user_id, db_override=db
        )
        if et == "interrupted":
            final_status = "interrupted"
        elif et == "error":
            final_status = "error"
            final_error = str(event.get("message") or "Agent turn failed.")
            final_stop_cause = event.get("stop_cause") or None
        # Throttled advance of the durable latest_session_seq (drives WS resume
        # for cold devices). The RunBuffer holds the real events; this is just a
        # cheap pointer so a fresh device knows where the live stream is up to.
        ss = event.get("session_seq")
        if ss is not None and not controlled:
            now = time.monotonic()
            if now - _last_seq_persist > 1.0:
                _last_seq_persist = now
                try:
                    # Offloaded: this fires repeatedly DURING streaming; on the
                    # loop it would freeze the LLM stream every ~1s.
                    await db_offload(lambda: db.run_state_update_seq(session_id, int(ss)))
                except Exception:
                    pass

    # ── Primary working introduction ────────────────────────────────────
    # This is the selected primary model speaking, not Scout.  It runs with no
    # tools and before the normal context/memory/tool preparation below.  The
    # durable progress row is sequenced before its live event so reload renders
    # the same first entry in the turn's scrollable activity bubble.
    _intro_style = str(_communication_cfg.get("working_intro") or "off").strip().lower()
    if (_manager_loop_cfg.get("enabled") and _intro_style in {"brief", "plan"}
            and str(_communication_cfg.get("intro_timing") or "before_preparation")
            == "before_preparation"):
        try:
            await event_callback({
                "type": "pipeline", "level": "pipeline", "step": "working_intro_start",
            })
            from app.agent.working_intro import generate_working_intro
            _intro_result = await generate_working_intro(
                db=db,
                user_id=user_id,
                session_id=session_id,
                agent_rec=agent,
                user_message=request.message or "",
                current_interaction_id=user_interaction_id,
                execution_mode=getattr(request, "execution_mode", "ask") or "ask",
                timeout_seconds=int(_communication_cfg.get("intro_timeout_seconds") or 12),
                style=_intro_style,
            )
            _working_intro_text = str(_intro_result.get("message") or "").strip()
            if _intro_result.get("status") == "existing" and _working_intro_text:
                _working_intro_id = str(_intro_result.get("interaction_id") or "") or None
                await event_callback({
                    "type": "pipeline", "level": "pipeline", "step": "working_intro_ready",
                    "asst_id": _working_intro_id, "reused": True,
                })
            elif _intro_result.get("status") == "complete" and _working_intro_text:
                _intro_meta = json.dumps({
                    "provider": _intro_result.get("provider"),
                    "model": _intro_result.get("model"),
                    "role": "assistant",
                    "message_phase": "progress",
                    "progress_kind": "working_intro",
                    "task_type": _intro_result.get("task_type"),
                    "intro_fallback": bool(_intro_result.get("fallback")),
                })
                _working_intro_id = await db.insert_interaction(
                    user_id, session_id, role="assistant", content=_working_intro_text,
                    parent_id=user_interaction_id, channel=channel,
                    metadata=_intro_meta,
                    output_data=json.dumps({
                        "role": "assistant", "content": _working_intro_text,
                        "message_phase": "progress", "progress_kind": "working_intro",
                    }),
                    sender_id=agent.get("id"), receiver_id=user_id,
                )
                _intro_db_event = {
                    "type": "db", "level": "db", "op": "insert_interaction",
                    "role": "assistant", "id": _working_intro_id,
                }
                await event_callback(_intro_db_event)
                await event_callback({
                    "type": "agent_step_end", "level": "agent",
                    "content": _working_intro_text,
                    "asst_id": _working_intro_id,
                    "interaction_seq": _intro_db_event.get("interaction_seq"),
                    "message_phase": "progress",
                    "progress_kind": "working_intro",
                })
                await event_callback({
                    "type": "pipeline", "level": "pipeline", "step": "working_intro_ready",
                    "asst_id": _working_intro_id,
                })
            else:
                await event_callback({
                    "type": "pipeline", "level": "pipeline", "step": "working_intro_skipped",
                    "reason": str(_intro_result.get("reason") or "direct response")[:300],
                })
        except asyncio.CancelledError:
            raise
        except Exception as _wie:
            logger.debug("primary work introduction unavailable for %s: %s",
                         session_id[:12], _wie)
            await event_callback({
                "type": "pipeline", "level": "pipeline", "step": "working_intro_skipped",
                "reason": str(_wie)[:300],
            })

    # Only now may the background Starter begin.  This preserves the strict
    # visible/runtime order requested for managed runs: primary introduction,
    # then Scout, then context/memory/tool preparation.
    if _run_scout_row is not None and _scout_launch_fn is not None:
        try:
            _scout_task = _scout_launch_fn(db, _run_scout_row)
            _starter_cfg = (_run_scout_row or {}).get("starter_config") or {}
            _perf.mark(
                "starter_launched",
                parallel=bool(_starter_cfg.get("parallel", True)),
                launched=bool(_scout_task),
            )
            if _scout_task is not None and not _starter_cfg.get("parallel", True):
                await _scout_task
                _perf.mark("starter_wait_done")
        except asyncio.CancelledError:
            raise
        except Exception as _rse:
            logger.debug("Run Scout launch failed for %s: %s", session_id[:12], _rse)
            _perf.mark("starter_failed", error_type=type(_rse).__name__)

    try:
        # Re-fetch agent with context documents only if they were never resolved.
        # _prepare_send now loads context up front, so the common path skips this
        # entirely (a saved DB round-trip per message). Note: an agent with no
        # context docs has an *empty list* here — that still counts as resolved,
        # so we check for the key being absent (None), not merely falsy.
        nonlocal_agent = agent
        if nonlocal_agent.get("context_documents") is None:
            _fetched = await db.fetch_agent_by_id_with_context(
                nonlocal_agent["id"], CONTEXT_SECTION_TYPES, user_id=user_id)
            if _fetched is not None:
                nonlocal_agent = _fetched
        agent = nonlocal_agent

        loop_config = LoopConfig.from_agent(agent)

        # Local Claude Code (and any alternate-runtime) agent hands its WHOLE turn
        # to its own engine adapter (see the engine seam in app/agent/loop.py) — it
        # runs `claude` directly, not WebAgent's loop. None of the normal turn
        # plumbing applies, so skip the memory nodes: a Claude run should show no
        # memory_search / memory_save bubbles around it.
        _engine_id = ""
        try:
            _eng_meta = agent.get("metadata")
            if isinstance(_eng_meta, str):
                _eng_meta = json.loads(_eng_meta or "{}")
            if isinstance(_eng_meta, dict):
                _engine_id = str(_eng_meta.get("engine") or "").strip()
        except Exception:
            _engine_id = ""
        _is_engine_agent = bool(_engine_id) and _engine_id != "default"

        await event_callback({
            "type": "pipeline", "level": "pipeline", "step": "agent_assigned",
            "agent_id": agent["id"], "max_turn_count": agent.get("max_turn_count", 0),
        })

        if not agent.get("context_documents") and loop_config.is_enabled("copy_defaults"):
            copied = await db.copy_defaults_to_agent(agent["id"], template_id='default')
            if copied > 0:
                agent = await db.fetch_agent_by_id_with_context(
                    agent["id"], CONTEXT_SECTION_TYPES, user_id=user_id)

        context_docs = agent.get("context_documents", [])
        doc_types = list(set(
            (d.get("context_type") or d.get("doc_type") or "")
            for d in context_docs if d.get("context_type") or d.get("doc_type")
        ))
        await event_callback({
            "type": "pipeline", "level": "pipeline", "step": "load_context",
            "count": len(context_docs), "types": doc_types,
        })

        # ── PHASE 1: Brain-first lookup ──
        # Launched here, awaited just before the prompt is built, so the
        # embedding round-trip overlaps with attachment/image prep (#2).
        brain_results = []
        brain_context = None
        parent_id = None
        _brain_task = None
        _ability_context_task = None
        if not _is_engine_agent and not _should_skip_memory(request.message):
            from app.abilities import prompt_context_for_agent
            _ability_context_task = asyncio.create_task(prompt_context_for_agent(
                agent["id"], request.user_id, request.message,
            ))
        if _is_engine_agent:
            # No memory plumbing for engine agents — the adapter owns the whole
            # turn. The adapter's persisted rows parent to the user message.
            parent_id = user_interaction_id
        elif not loop_config.is_enabled("memory_search") or _should_skip_memory(request.message):
            _skip_reason = "node_disabled" if not loop_config.is_enabled("memory_search") else "greeting_or_cmd"
            await event_callback({"type": "pipeline", "level": "pipeline",
                                  "step": "memory_search_skip", "reason": _skip_reason})
            parent_id = await db.insert_interaction(
                user_id, session_id, role="tool",
                content=json.dumps({"skipped": True, "reason": _skip_reason}),
                parent_id=user_interaction_id, tool_name="memory_search", channel=channel,
                metadata=json.dumps({"brain": True, "skipped": True, "reason": _skip_reason,
                                     "args": {"query": request.message, "skipped": True}}),
                sender_id=agent["id"], receiver_id=agent["id"],
            )
            await event_callback({
                "type": "db", "level": "db", "op": "insert_interaction",
                "role": "tool", "id": parent_id,
            })
            await event_callback({"type": "pipeline", "level": "pipeline",
                                  "step": "memory_search_end", "results_count": 0,
                                  "results": [], "skipped": True})
        else:
            # The embedding/search is LAUNCHED here so it overlaps the tool +
            # history reads below, but the user-visible "Searching memory" step
            # is emitted later — right before we actually await its result — so
            # the activity pill steps through each prep stage in the order it is
            # genuinely waited on (building tools → loading history → searching
            # memory) instead of sitting on "Searching memory" for all of prep.
            # Short messages take the keyword-only fast path (no embedding) (#3).
            _use_vector = not _should_skip_vector(request.message)
            _brain_task = asyncio.create_task(
                db.memory_search(request.user_id, request.message, limit=5, vector=_use_vector)
            )

        # ── Launch the independent pure-DB reads concurrently (real parallelism) ──
        # Tool loading is independent of memory and attachment processing, so
        # launch it early and overlap the underlying DB reads. The history builder
        # remains on this event loop because its compaction step can call the LLM.
        _agent_id_for_prompt = agent.get("id") if agent else None
        _tools_version = agent.get("updated_at") if agent else None
        # ── PREWARM FAST PATH ──
        # If the front-end warmed this session's read-only prep while the user was
        # typing (tool set + history), consume it and SKIP those
        # remote round-trips entirely. The bundle is only returned when it's still
        # valid (same agent/version, within TTL, built after the last turn), so a
        # miss is always safe — we just build it live below.
        _pw = None
        # Skip the fast path on a REPLACE turn: an interrupted-and-replaced send
        # must rebuild history fresh (it includes the interrupted partial reply +
        # the interruption system note), which a pre-typed bundle wouldn't have.
        if not _is_engine_agent and not replaced:
            try:
                # The prewarm was assembled before this user row existed. If
                # the row opens a new task, its cached history would keep the
                # preceding task's tool payloads full for one extra run. Keep
                # the prewarmed tools, but force history to rebuild
                # with the persisted live row so reduction applies immediately.
                _pending_starts_new_task = bool(
                    isinstance(_user_meta.get("task_boundary"), dict)
                    and _user_meta["task_boundary"].get("is_new")
                    and not is_first_turn
                )
                _pw = turn_prewarm.consume(
                    session_id,
                    sig=(request.user_id, _agent_id_for_prompt, _tools_version),
                    pending_starts_new_task=_pending_starts_new_task,
                )
            except Exception:
                _pw = None
        _reads_task = None
        if _pw is None:
            _read_factories = [
                lambda: _cached_load_tools(
                    request.user_id, _agent_id_for_prompt or "",
                    agent.get("template_id") if agent else None, _tools_version),
            ]
            _reads_task = asyncio.ensure_future(_gather_db_concurrent(*_read_factories))
        _perf.mark("db_reads_launched", prewarmed=bool(_pw))
        # Activity-pill stage: we now wait on the tool-building reads
        # (the dominant cost of a warm turn). Surfaced so the user sees this stage
        # — and its live elapsed time — above the chat pill, not just "memory".
        await event_callback({"type": "pipeline", "level": "pipeline",
                              "step": "prep_tools", "prewarmed": bool(_pw)})

        # ── Resolve attachments + vision fallback ──
        attachment_context = None
        attachment_docs: List[Dict[str, Any]] = []
        if request.attachment_ids:
            for att_id in request.attachment_ids:
                att = await db.get_attachment(att_id)
                if att:
                    attachment_docs.append(att)
            if attachment_docs:
                attachment_context = format_attachments_for_prompt(attachment_docs)
                await event_callback({
                    "type": "attachment", "level": "agent",
                    "attachments": [
                        {"id": a["id"], "original_name": a["original_name"],
                         "mime_type": a["mime_type"], "size_bytes": a["size_bytes"],
                         "storage_path": a.get("storage_path", "")}
                        for a in attachment_docs
                    ],
                })
        _desc_out = {}
        # Engine agents (e.g. Local Claude Code) read attached images as real
        # files with their own vision-capable tools, so skip the vision-describe
        # router entirely — it would otherwise burn a vision-model call, or (on a
        # non-vision brain) fold a "you can't see this image" note that flatly
        # contradicts the image file the engine is about to hand Claude.
        if not _is_engine_agent:
            async for _dev in _maybe_describe_images(
                db, request.user_id, request.message, user_interaction_id,
                loop_config, attachment_docs, _desc_out,
                agent_id=agent["id"], agent_rec=agent, session_id=session_id,
            ):
                await event_callback(_dev)
        # Launcher element-pickup fingerprint → foldable chip + this-turn fold.
        async for _dev in _maybe_emit_app_control(
            db, request, user_interaction_id, _desc_out,
            agent_id=agent["id"], session_id=session_id, channel=channel,
        ):
            await event_callback(_dev)
        user_message_content = await build_user_message_content(
            _desc_out.get("message_text", request.message),
            _desc_out.get("inline_docs", attachment_docs),
        )
        _perf.mark("attachments_vision_done")

        # ── Collect the concurrent reads launched above ──
        # This ran in a worker thread alongside the memory embedding and the
        # attachment/vision work, so their round-trips have been overlapping
        # rather than stacking. The pipeline events are still emitted in their
        # original narrative order below, so the loop visualizer's sequence
        # (search → prompt) is unchanged.
        if _pw is not None:
            # Prewarm hit: tools + history came from the bundle the
            # front-end built while the user typed — no remote round-trips here.
            tools = _pw["tools"]
            if isinstance(tools, BaseException) or tools is None:
                raise RuntimeError("prewarmed tools invalid")
            _perf.mark("db_reads_collected", prewarmed=True,
                       tool_count=len(tools) if isinstance(tools, list) else None)
        else:
            _reads = await _reads_task
            tools = _reads[0]
            if isinstance(tools, BaseException):
                raise tools  # tool loading is essential — surface the real error
            _perf.mark("db_reads_collected", tool_count=len(tools) if isinstance(tools, list) else None)

        # ── Build the chat history (prior interactions) ──
        if is_first_turn:
            # Brand-new session: the user's message is the very first interaction,
            # so there is nothing prior to load. Skip both the DB round-trip AND
            # the user-visible "Loading history" pill step entirely.
            history = []
            _perf.mark("history_built", first_turn=True, history_len=0)
        elif _pw is not None and _pw.get("history") is not None:
            # Prewarm hit: history came from the pre-typed bundle.
            await event_callback({"type": "pipeline", "level": "pipeline",
                                  "step": "prep_history", "prewarmed": True})
            history = _pw["history"]
            _perf.mark("history_built", prewarmed=True,
                       history_len=len(history) if isinstance(history, list) else None)
        else:
            # Activity-pill stage: building the chat history (prior interactions).
            await event_callback({"type": "pipeline", "level": "pipeline",
                                  "step": "prep_history"})
            # History stays on the main loop (its compaction step may call the LLM).
            # Its DB round-trips overlap any reads still finishing in their threads.
            history = await build_openai_history_from_session(
                db, request.user_id, request.session_id,
                exclude_interaction_ids={
                    value for value in (user_interaction_id, _working_intro_id) if value
                },
                agent_id=agent.get("id"),
            )
            _perf.mark(
                "history_built",
                history_len=len(history) if isinstance(history, list) else None,
                prewarmed_tools=bool(_pw),
                rebuilt_for_task_boundary=bool(
                    _pw is not None and not _pw.get("history_reusable", True)
                ),
            )

        # ── PHASE 1 (cont.): await the memory lookup (its embedding has been
        # overlapping the assembly above) and fold it into the prompt ──
        if _brain_task is not None:
            # Activity-pill stage: now we actually wait on the memory lookup.
            await event_callback({"type": "pipeline", "level": "pipeline",
                                  "step": "memory_search_start", "query": request.message, "limit": 5})
            brain_results = await _brain_task
            await event_callback({
                "type": "pipeline", "level": "pipeline", "step": "memory_search_end",
                "results_count": len(brain_results),
                "results": [{"slug": r["slug"], "title": r.get("title", r["slug"]),
                             "score": round(r.get("rank", 0), 2)} for r in (brain_results or [])],
            })
            if brain_results:
                lines = []
                for r in brain_results:
                    slug = r.get("slug", "?")
                    title = r.get("title", slug)
                    ct = r.get("compiled_truth", "")[:300]
                    rank = r.get("rank", 0)
                    lines.append(f"## {slug} — {title} (score: {rank:.2f})")
                    if ct:
                        lines.append(ct)
                    lines.append("")
                brain_context = "\n".join(lines)
            search_content = json.dumps({
                "query": request.message,
                "results": [
                    {"slug": r["slug"], "title": r.get("title", ""), "score": round(r.get("rank", 0), 2),
                     "snippet": r.get("compiled_truth", "")[:150]}
                    for r in (brain_results or [])
                ],
                "count": len(brain_results or []),
            }, indent=2)
            parent_id = await db.insert_interaction(
                user_id, session_id, role="tool", content=search_content,
                parent_id=user_interaction_id, tool_name="memory_search", channel=channel,
                metadata=json.dumps({"count": len(brain_results or []), "brain": True,
                                     "has_results": bool(brain_results),
                                     "args": {"query": request.message}}),
                output_data=search_content,
                sender_id=agent["id"], receiver_id=agent["id"],
            )
            await event_callback({
                "type": "db", "level": "db", "op": "insert_interaction",
                "role": "tool", "id": parent_id,
            })
            await event_callback({"type": "tool_result", "level": "agent", "tool": "memory_search",
                                  "result": search_content[:2000], "duration_ms": 0, "error": False})

        if _ability_context_task is not None:
            ability_context = await _ability_context_task
            if ability_context:
                brain_context = "\n\n".join(
                    part for part in (brain_context, ability_context) if part
                )

        _perf.mark("memory_recall_done", memory_hits=len(brain_results or []))
        # When no attachments are present, strip read_attachment from the static
        # bootstrap_tools slot so the agent doesn't see it as a generic file-reading
        # tool. The # [USER ATTACHMENTS] section already tells the agent to call
        # read_attachment by attachment_id when there are attachments.
        if not attachment_docs:
            for _doc in context_docs:
                if _doc.get("context_type") == "bootstrap_tools":
                    _content = _doc.get("content", "")
                    import re
                    _content = re.sub(
                        r',?\s*read_attachment\s*,?\s*', ', ', _content
                    )
                    _content = re.sub(r',\s*,', ',', _content)
                    _content = re.sub(r',\s*\.', '.', _content)
                    _content = re.sub(r',\s*\n', '\n', _content)
                    _doc["content"] = _content
                    break
        system_prompt_parts = await build_system_prompt_parts(
            context_docs, brain_context, request.user_id,
            agent_id=_agent_id_for_prompt, agent_rec=agent)
        # Engine agents: capture the pure persona (agent-authored slots) BEFORE
        # append_skills_section appends the platform skills catalog. Engines want
        # just the admin's instructions, not tool schemas/capability layers.
        persona_prompt = system_prompt_parts.agent_context
        # The attachment summary tells the model to call the `read_attachment`
        # tool by attachment_id — meaningful only to the default loop. An engine
        # agent (Local Claude Code) has no such tool; it gets the real file paths
        # from its own adapter instead, so don't feed it this contradictory note.
        if attachment_context and not _is_engine_agent:
            system_prompt_parts.turn_context = "\n\n".join(
                p for p in (system_prompt_parts.turn_context, attachment_context) if p
            )
        if _working_intro_text:
            _update_policy = str(
                _communication_cfg.get("update_policy") or "material"
            ).strip().lower()
            _update_instruction = {
                "off": "Do not add a progress update solely because the approach changes; explain it in the final response.",
                "phase": "Publish a concise progress update at each major phase and whenever the approach materially changes.",
                "material": "If evidence materially changes the approach, publish one concise progress update before continuing.",
            }.get(_update_policy, "If evidence materially changes the approach, publish one concise progress update before continuing.")
            system_prompt_parts.turn_context = "\n\n".join(
                p for p in (
                    system_prompt_parts.turn_context,
                    "## Published working introduction\n"
                    "You already told the user the following provisional approach before run "
                    "preparation. Follow it where the evidence supports it. If memory, Scout, or "
                    "tool availability changes the approach, follow this communication policy: "
                    + _update_instruction
                    + " Do not repeat this introduction verbatim.\n"
                    + _working_intro_text,
                ) if p
            )
        system_prompt_parts.agent_context = await append_skills_section(
            system_prompt_parts.agent_context,
            agent,
            request.session_id,
            caller_user_id=request.user_id,
        )
        system_prompt = system_prompt_parts.render()

        # Emit the pipeline events now, in their original visual order (the work
        # above was reordered for overlap, but the narrative shown stays the same).
        await event_callback({
            "type": "pipeline", "level": "pipeline", "step": "build_prompt", "sections": ["SYSTEM"],
            "brain_injected": bool(brain_context), "tool_count_in_prompt": len(tools),
            "system_prompt": system_prompt[:8000],
        })
        # If this turn replaced one the user interrupted, tell the agent so it
        # reads the new message as a course-correction / stop / addition relative
        # to the partial answer it had started. The agent decides what to do.
        if replaced:
            _starter = str((_run_scout_row or {}).get("combined_request") or "").strip()
            history.append({
                "role": "system",
                "content": (
                    "The user sent a new message while you were still responding, so your "
                    "previous answer was interrupted (you can see your partial reply above). "
                    "Read their new message carefully and respond to it: they may be telling "
                    "you to STOP (acknowledge briefly and stop), steering you in a different "
                    "direction (adjust accordingly), or adding information (incorporate it). "
                    "Do not simply repeat your interrupted answer."
                    + (
                        "\n\nFor clarity, the Run Scout combined the messages in this "
                        "logical starter as follows:\n" + _starter
                        if _starter else ""
                    )
                ),
            })

        _raw_at = agent.get("allowed_tools", [])
        if isinstance(_raw_at, str):
            try:
                _raw_at = json.loads(_raw_at)
            except Exception:
                _raw_at = []

        assistant_reply = ""
        # A normal terminal status is valid only after the loop has emitted a
        # displayable final response. Do not infer completion from the stream
        # ending: an adapter or early return can otherwise leave the run
        # marked complete with no closer for the user.
        _saw_final_response = False
        _exec_mode = getattr(request, 'execution_mode', 'ask') or 'ask'
        _perf.mark("prep_complete_entering_loop")
        _seen_first_event = False
        _seen_first_text = False
        async for event in stream_agent_events(
            user_id=request.user_id, session_id=request.session_id,
            user_message=user_message_content, system_prompt=system_prompt,
            agent_id=agent["id"], history=history, parent_interaction_id=parent_id,
            run_turn_id=user_interaction_id,
            max_turns=agent.get("max_turn_count", 0), channel=channel, db=db,
            agent_template_id=agent.get("template_id"), allowed_tools=_raw_at or None,
            execution_mode=_exec_mode,
            # Pass the resolved attachment rows so an engine agent (e.g. Local
            # Claude Code) can read pasted images / attached files off disk with
            # its own tools — the default loop already inlines images itself.
            attachment_docs=attachment_docs,
            system_prompt_parts=system_prompt_parts,
            persona_prompt=persona_prompt,
            turn_reservation_key=stable_turn_key(
                "chat-turn", request.user_id, request.session_id, user_interaction_id
            ),
        ):
            if not _seen_first_event:
                _seen_first_event = True
                _perf.mark("loop_first_event", event_type=event.get("type"))
            _etype = event.get("type")
            if not _seen_first_text and _etype in ("stream", "response"):
                _seen_first_text = True
                _perf.mark("loop_first_text", event_type=_etype)
            await event_callback(event)
            if event["type"] == "response":
                assistant_reply = event["content"]
                _saw_final_response = bool(str(assistant_reply or "").strip())
            elif event["type"] == "error" and not assistant_reply:
                assistant_reply = f"I encountered an error: {event['message']}"
            elif event["type"] == "interrupted" and not assistant_reply:
                assistant_reply = f"I was interrupted: {event['message']}"
        if final_status == "complete" and not _saw_final_response:
            await event_callback({
                "type": "error", "level": "agent",
                "message": "The agent run ended without a visible final response; it will resume automatically.",
                "stop_cause": "empty_response",
            })
        _perf.mark("loop_done", reply_chars=len(assistant_reply or ""))

        # ── Background memory save ── (skipped for engine agents — no plumbing)
        if not _is_engine_agent and 'memory_save' not in set(_raw_at or []) and loop_config.is_enabled("memory_save"):
            asyncio.create_task(_save_chat_to_memory(
                db, request.user_id, request.session_id,
                request.message, assistant_reply, agent["id"], parent_id,
            ))
            await event_callback({"type": "pipeline", "level": "pipeline",
                                  "step": "memory_save_start", "slug": f"chat/{request.session_id[:8]}"})

        # ── Background turn-lifecycle hooks (ability TURN_HOOK) ──
        # Fire-and-forget after every turn. The primary consumer is the session
        # titler ability which auto-names the session from its first few turns.
        try:
            from app.abilities import turn_hooks_for_agent
            for _hook in await turn_hooks_for_agent(agent.get("id", "")):
                _spawn_bg(
                    _hook(
                        db, request.user_id, request.session_id,
                        lambda ev: _emit_to_user_listeners(request.user_id, ev),
                    ),
                    label=f"turn_hook:{session_id[:8]}",
                )
        except Exception as _th:
            logger.warning("turn hooks dispatch failed for %s: %s",
                           session_id[:12], _th)
    except asyncio.CancelledError:
        # Hard-cancelled (replace grace timeout, or watchdog frozen-cancel). Mark
        # interrupted so it isn't wrongly recorded 'complete'; the stop_cause was
        # already tagged by whoever requested the stop (replaced / frozen).
        final_status = "interrupted"
        raise
    except Exception as e:
        final_status = "error"
        logger.error("Background turn failed for session %s: %s", session_id, e, exc_info=True)
        try:
            await _emit_to_visualizers(session_id, {
                "type": "error", "level": "agent", "message": str(e),
            }, user_id=user_id, db_override=db)
        except Exception:
            pass
    finally:
        # Cancel any helper tasks (brain lookup / read fan-out) still in flight if the
        # turn is unwinding before they were awaited — otherwise they run detached and
        # log an unretrieved exception. locals().get avoids an unbound name if we
        # errored before they were assigned. No-op when already awaited/done.
        _cancel_if_pending(locals().get("_brain_task"))
        _cancel_if_pending(locals().get("_reads_task"))
        # The turn is done — drop any prewarm bundle so the NEXT turn re-warms with
        # history that includes the reply just produced (see app/agent/turn_prewarm).
        try:
            turn_prewarm.mark_turn_done(session_id)
        except Exception:
            pass
        # Derive the machine cause from the terminal status. A voluntary cause
        # (user_stop / replaced) already on the row is preserved by run_state_finish.
        _web_cause = ("complete" if final_status == "complete"
                      else (final_stop_cause or "crash") if final_status == "error" else None)
        if final_status == "interrupted":
            if controlled:
                _web_cause = "crash"
            else:
                try:
                    _prior = await db.run_state_get(session_id)
                    _web_cause = (_prior or {}).get("stop_cause") or "crash"
                except Exception:
                    _web_cause = "crash"
        if not controlled:
            try:
                await db.run_state_finish(
                    session_id, status=final_status, error=final_error,
                    stop_cause=_web_cause,
                )
                _perf.mark("run_state_finished", final_status=final_status)
            except Exception as _rsf:
                logger.exception("run_state_finish failed for %s: %s", session_id, _rsf)
                try:
                    await event_callback({
                        "type": "error", "level": "agent",
                        "message": "The agent stopped, but its final run status could not be saved: "
                                   f"{_rsf}",
                        "persistence_failure": True,
                    })
                except Exception:
                    logger.exception("Could not publish run-state persistence failure for %s", session_id)
        try:
            from app.agent.run_scout import mark_run_outcome as _scout_finish
            await _scout_finish(
                db, user_interaction_id, final_status, _web_cause,
            )
            _perf.mark("starter_outcome_saved", final_status=final_status)
        except Exception:
            logger.debug("Run Scout outcome update failed for %s",
                         session_id[:12], exc_info=True)
        if session_id.startswith("optimizer-") and final_status != "complete":
            try:
                from app.optimizer.runner import mark_optimizer_run_terminal
                await db_offload(lambda: mark_optimizer_run_terminal(
                    session_id, final_status, final_error or ""
                ))
            except Exception as _ore:
                logger.exception(
                    "optimizer terminal status update failed for %s: %s",
                    session_id, _ore,
                )
        if not controlled:
            try:
                await get_run_buffer_registry().end_turn(session_id, db=db)
            except Exception as _eb:
                logger.debug("end_turn failed for session %s: %s", session_id, _eb)

        # Free this session's slot for the next queued session (FIFO). Must
        # happen BEFORE the instant-resume launch below so a re-ignited run
        # re-acquires through the gate like any other run.
        try:
            await session_gate.release(session_id)
        except Exception as _gr:
            logger.debug("session gate release failed for %s: %s", session_id[:12], _gr)

        # Instant resume: if the turn crashed with a network error (tagged as
        # 'crash'), don't wait 30s for the watchdog — re-ignite immediately.
        # This makes transient network blips recover in ~1s instead of ~30s.
        if _web_cause == "crash" and os.environ.get("WEBAGENT_V2_ISOLATED") != "1":
            try:
                from app.agent.runner import resume_one
                asyncio.create_task(resume_one(session_id, reason="network_crash"))
            except Exception as _ire:
                logger.debug("instant resume launch failed for %s: %s", session_id, _ire)


# Existing V1 callers retain their historical entry point.
_run_turn_background = run_shared_agent_turn


async def _resume_web_turn(rc: Dict[str, Any], replaced: bool):
    """Self-healing resume builder for the 'web' origin. Re-ignites an
    involuntarily-stopped interactive turn from durable history, emitting through
    the SAME RunBuffer + WebSocket path a live turn uses, so an attached chat sees
    the continuation stream in. Run-state begin/finish are owned by the runner;
    this only executes the turn and returns the outcome."""
    from app.agent.runner import RunOutcome
    db = get_db()
    session_id = rc.get("session_id")
    user_id = rc.get("user_id")
    # Use the session's CURRENT agent — a mid-session delegation may have rebound it.
    agent_id = await db.get_session_agent_id(session_id) or rc.get("agent_id")
    agent = None
    if agent_id:
        agent = await db.fetch_agent_by_id_with_context(
            agent_id, CONTEXT_SECTION_TYPES, user_id=user_id)
        if agent is None:
            agent = await db.get_agent_by_id(agent_id)
    if not agent:
        return RunOutcome(status="error", stop_cause="failed",
                          error="agent not found for web resume")

    # Reuse the exact starter/revision captured by the original turn.  A
    # completed Scout analysis is left untouched; a queued/interrupted/failed
    # one is retried.  No new starter is fabricated during recovery.
    try:
        from app.agent.run_scout import revive_turn as _revive_scout
        await _revive_scout(db, str(rc.get("turn_id") or ""))
    except Exception:
        logger.debug("Run Scout revive failed for %s", session_id[:12], exc_info=True)

    final_status = "complete"
    final_stop_cause = None
    final_error = None
    reply = ""
    _saw_final_response = False
    _run_buffer = await get_run_buffer_registry().start_turn(
        session_id=session_id, user_id=user_id, turn_id=rc.get("turn_id"), db=db,
    )

    async def event_callback(event: Dict[str, Any]):
        nonlocal final_status, final_stop_cause, final_error
        await _emit_to_visualizers(
            session_id, event, user_id=user_id, db_override=db
        )
        et = event.get("type")
        if et == "interrupted":
            final_status = "interrupted"
        elif et == "error":
            final_status = "error"
            final_stop_cause = event.get("stop_cause") or None
            final_error = str(event.get("message") or "Agent turn failed.")[:500]
        ss = event.get("session_seq")
        if ss is not None:
            try:
                await db.run_state_update_seq(session_id, int(ss))
            except Exception:
                pass
    try:
        loop_config = LoopConfig.from_agent(agent)
        context_docs = agent.get("context_documents", [])
        system_prompt = await build_system_prompt(
            context_docs, None, user_id, agent_id=agent_id, session_id=session_id)
        system_prompt = await append_skills_section(system_prompt, agent, session_id, caller_user_id=user_id)
        # Pre-load full compacted history so the agent sees the conversation exactly
        # as it was before the crash — no bootstrap tool call, no nudge, no empty
        # trailing user. user_message is None, so the raw transcript (ending at the
        # last real message) is the whole context and the agent continues unaware
        # of the interruption.
        try:
            from app.agent.session_history import build_openai_history_from_session
            history = await build_openai_history_from_session(
                db, user_id, session_id, agent_id=agent_id)
        except Exception:
            history = []
        from app.agent.contracts import add_manual_resume_approval
        history = add_manual_resume_approval(
            history, rc.get("resume_reason"),
        )
        _raw_at = agent.get("allowed_tools", [])
        if isinstance(_raw_at, str):
            try:
                _raw_at = json.loads(_raw_at)
            except Exception:
                _raw_at = []
        await event_callback({
            "type": "resumed", "level": "agent",
            "reason": rc.get("resume_reason", "server_restart"),
            "recovery": rc.get("recovery") or {},
        })
        # `resume_one()` persists the durable synthetic tool row. Emit its live
        # counterpart here so the visible recovery action does not wait for the
        # reconcile poll; the regular tool UI handles call/result rendering.
        _recovery_tool = rc.get("recovery_tool") or {}
        if _recovery_tool:
            await event_callback({
                "type": "tool_call", "level": "agent", "tool": "resume_session",
                "args": _recovery_tool, "turn": 0,
            })
            await event_callback({
                "type": "tool_result", "level": "agent", "tool": "resume_session",
                "result": (
                    f"Retrying after {_recovery_tool.get('cause', 'unknown')} "
                    f"(attempt {_recovery_tool.get('attempt', '?')}/"
                    f"{_recovery_tool.get('max_attempts', '?')})."
                ),
                "duration_ms": 0, "error": False, "turn": 0,
            })

        # ── Restore the run's execution mode (ask/plan/auto) ──
        # A fresh send carries the mode on the request and stamps it on the session
        # row; a resume has no request. Prefer the session's persisted mode (the
        # source of truth the pill is restored from — the loop also re-reads it
        # live for tool gating), then the mode captured in the relaunch context,
        # then the agent's configured default, then the safe 'ask'. Without this,
        # every self-healed turn silently downgraded to Ask regardless of the
        # user's pill selection.
        from app.agent.execution_modes import normalize_mode_id as _norm_mode
        try:
            _resume_mode = (await db.get_session_execution_mode(session_id)) or rc.get("execution_mode")
        except Exception:
            _resume_mode = rc.get("execution_mode")
        _resume_mode = _resume_mode or agent.get("default_execution_mode") or "ask"
        _resume_mode = _norm_mode(_resume_mode, fallback="ask")

        async for event in stream_agent_events(
            user_id=user_id, session_id=session_id, user_message=None,
            system_prompt=system_prompt, agent_id=agent_id, history=history,
            run_turn_id=rc.get("turn_id"),
            resume_reason=rc.get("resume_reason") or rc.get("stop_cause"),
            max_turns=agent.get("max_turn_count", 0), channel=rc.get("channel"), db=db,
            agent_template_id=agent.get("template_id"), allowed_tools=_raw_at or None,
            execution_mode=_resume_mode,
            turn_reservation_key=stable_turn_key(
                "chat-turn", user_id, session_id, rc.get("turn_id") or ""
            ),
        ):
            await event_callback(event)
            if event["type"] == "response":
                reply = event["content"]
                _saw_final_response = bool(str(reply or "").strip())
        if final_status == "complete" and not _saw_final_response:
            await event_callback({
                "type": "error", "level": "agent",
                "message": "The resumed agent run ended without a visible final response; it will resume automatically.",
                "stop_cause": "empty_response",
            })
    except asyncio.CancelledError:
        final_status = "interrupted"
        raise
    except Exception as e:
        final_status = "error"
        logger.error("web resume failed for session %s: %s", session_id, e, exc_info=True)
    finally:
        try:
            from app.agent.run_scout import mark_run_outcome as _scout_finish
            _resume_cause = ("complete" if final_status == "complete"
                             else (final_stop_cause or "crash")
                             if final_status == "error" else None)
            await _scout_finish(
                db, str(rc.get("turn_id") or ""), final_status, _resume_cause,
            )
        except Exception:
            logger.debug("Run Scout resume outcome failed for %s",
                         session_id[:12], exc_info=True)
        try:
            await get_run_buffer_registry().end_turn(session_id, db=db)
        except Exception:
            pass

    from app.agent.runner import RunOutcome as _RO
    cause = ("complete" if final_status == "complete"
             else (final_stop_cause or "crash") if final_status == "error" else None)
    return _RO(status=final_status, stop_cause=cause, reply=reply,
               error=final_error)


# Register the web-origin resume builder so the self-healing layer (boot recovery
# + liveness watchdog) can re-ignite interactive turns with UI streaming intact.
try:
    from app.agent.runner import register_resume_builder as _rrb
    _rrb("web", _resume_web_turn)
except Exception as _rrb_err:  # pragma: no cover
    logger.debug("Could not register web resume builder: %s", _rrb_err)


async def _sse_tail_run(session_id: str):
    """SSE fallback: tail the RunBuffer for a session and yield events as they
    appear, then stop when the run completes. Fully decoupled from the run — if
    this client disconnects, the supervised run keeps going. The WebSocket
    subscriber is the primary live path; this exists so the old streaming
    endpoint keeps working."""
    reg = get_run_buffer_registry()
    rm = get_run_manager()
    last = 0
    idle = 0.0
    while True:
        buf = reg.get(session_id)
        if buf is not None:
            missed = buf.replay_after(last)
            for ev in missed:
                last = ev.get("session_seq", last)
                yield f"data: {json.dumps(ev)}\n\n"
            if buf.completed_at is not None and not buf.replay_after(last):
                break
        else:
            if not rm.is_running(session_id):
                break
        if not rm.is_running(session_id):
            # run ended; drain any final buffered events then stop
            buf = reg.get(session_id)
            if buf is None or not buf.replay_after(last):
                break
        await asyncio.sleep(0.08)
        idle += 0.08
        if idle >= 20:
            idle = 0.0
            yield ": ping\n\n"


# ── TUI Bridge registration ──────────────────────────────────────────────────

class TuiBridgeRegisterRequest(BaseModel):
    port: int
    user_id: str


@router.post("/tui-bridge/register")
async def tui_bridge_register(request: TuiBridgeRegisterRequest, fastapi_request: Request):
    """Register a TUI bridge server for the current user.
    
    Called by the TUI app when it starts its bridge server. The web app uses
    this to forward messages to the TUI agent when the user chats with the
    "Web Agent TUI" agent.
    
    Side effect: ensures the admin user has a materialized "web-agent-tui"
    agents row so it appears in the frontend agent dropdown as a selectable
    agent.
    """
    from app.auth.identity import assert_caller_is
    user_id = await assert_caller_is(fastapi_request, request.user_id)
    from app.tui_bridge import register_bridge
    register_bridge(user_id, request.port)

    # Ensure the admin has a materialized web-agent-tui agent
    try:
        db = get_db()
        agent = await db.get_or_resolve_session_agent(
            session_id=f"tui-init-{request.port}",
            user_id=user_id,
            template_id="web-agent-tui",
        )
        # Clean up the init session — it was just a vehicle for materialization
        try:
            conn = db._get_conn()
            conn.execute("DELETE FROM sessions WHERE id = ?", (f"tui-init-{request.port}",))
            conn.commit()
            conn.close()
        except Exception:
            pass
        logger.info("TUI bridge agent ready for user %s (agent_id=%s)", user_id[:12], agent.get("id", "?")[:12] if agent else "?")
    except Exception as e:
        logger.warning("Could not materialize web-agent-tui agent: %s", e)

    return {"status": "ok", "port": request.port}


@router.post("/tui-bridge/unregister")
async def tui_bridge_unregister(fastapi_request: Request):
    """Unregister the TUI bridge for the current user."""
    from app.auth.identity import assert_caller_is
    user_id = await assert_caller_is(fastapi_request, None)
    if user_id:
        from app.tui_bridge import unregister_bridge
        unregister_bridge(user_id)
    return {"status": "ok"}


@router.get("/tui-bridge/status")
async def tui_bridge_status(fastapi_request: Request):
    """Check if a TUI bridge is registered for the current user."""
    from app.auth.identity import assert_caller_is
    user_id = await assert_caller_is(fastapi_request, None)
    from app.tui_bridge import is_bridge_alive, get_bridge_port
    return {
        "alive": is_bridge_alive(user_id) if user_id else False,
        "port": get_bridge_port(user_id) if user_id else None,
    }


@router.post("/send")
async def chat_send(request: ChatRequest, fastapi_request: Request):
    """Fire-and-forget send. Saves the user message, starts the agent turn as a
    supervised background run, and returns immediately. All output (including for
    the sending device) is rendered from the DB + the per-user WebSocket — see
    /api/v1/agent/ws and /api/v1/db/session-messages. Leaving, closing the
    browser, switching sessions/devices does NOT interrupt the run.

    A new message sent while the agent is still working **interrupts** the
    current run and starts a fresh one that includes the interrupted partial +
    the new message; the agent then decides whether to stop, steer, or continue.
    """
    prep = await _prepare_send(request, fastapi_request)
    if prep.get("tunnel_handled"):
        # Routed straight to the bound terminal — no agent run. Output streams
        # back over the embedded terminal; nothing for the chat bubble flow.
        return {"status": "tunnelled", "session_id": request.session_id}
    if "slash_result" in prep:
        response = {
            "status": "ok",
            "session_id": request.session_id,
            "reply": prep["slash_result"],
        }
        if prep.get("compaction"):
            response["compaction"] = prep["compaction"]
        return response
    if prep.get("duplicate"):
        # Idempotent replay: the browser outbox re-sent a message we already
        # accepted. Acknowledge the original turn with a 200 so the retry clears
        # from the outbox — but do NOT start a second run for it.
        return {"status": "duplicate", "session_id": request.session_id,
                "turn_id": prep["user_interaction_id"]}
    if prep.get("recycled"):
        # The session is in the recycle bin — the message was persisted and a
        # system notice was emitted. Nothing else to do; no agent run.
        return {"status": "recycled", "session_id": request.session_id,
                "message": "Message saved — session is in the bin. Restore it first."}

    # ── Cross-device hand-off ──
    # If this turn targets another device, enqueue it for that device's worker to
    # run inside THIS session. The user turn is already saved (above), and the
    # reply is appended here over the shared DB when the target finishes. The
    # atomic claim guarantees exactly one device runs it. Targeting our own
    # device id just falls through to the normal local run.
    target_device = getattr(request, "target_device", None)
    if target_device:
        from app.devices import dispatch as _dispatch
        from app.devices import identity as _identity
        dev = await _dispatch.resolve_target(target_device)
        target_instance = (dev or {}).get("instance_id") or target_device
        target_label = (dev or {}).get("label") or target_device
        if target_instance != _identity.device_id():
            # The job queue is shared/remote, whereas the newly-created user
            # interaction is local-first.  Publish its skeleton before enqueueing
            # the job: otherwise the target can claim it, re-sync its SQLite copy,
            # and miss the very message it was asked to answer.
            publish_now = getattr(prep["db"], "publish_interaction_now", None)
            if callable(publish_now):
                try:
                    await publish_now(prep["user_interaction_id"])
                except Exception as e:
                    logger.warning("Remote hand-off blocked: user turn %s was not published: %s",
                                   prep["user_interaction_id"], e)
                    raise HTTPException(
                        status_code=503,
                        detail="Could not publish this message to the remote device. Please retry.",
                    ) from e
            job_id = await _dispatch.enqueue(
                owner_user_id=request.user_id,
                prompt=request.message or "",
                agent_id=prep["agent"]["id"],
                target_instance=target_instance,
                target_label=target_label,
                payload={"run_in_session": request.session_id,
                         "execution_mode": getattr(request, "execution_mode", "ask") or "ask",
                         # The user turn is already saved in the shared transcript;
                         # the target rebuilds conversation history from it and must
                         # EXCLUDE this id so the loop (which appends the prompt as the
                         # live user_message) doesn't replay the same turn twice.
                         "user_interaction_id": prep["user_interaction_id"],
                         # Attachment ids so the target can re-resolve pasted images/
                         # files from the shared DB and feed them to an engine (e.g.
                         # claude_code) that reads them off disk. Server/DB-stored
                         # attachments resolve on the target; browser-only ones can't
                         # be fetched cross-device (the engine flags them unreadable).
                         "attachment_ids": list(getattr(request, "attachment_ids", None) or []),
                         "source": "chat"},
            )
            logger.info("Chat turn handed to device %s (job %s, session %s)",
                        target_label, job_id[:8], request.session_id[:12])
            return {"status": "queued", "session_id": request.session_id,
                    "turn_id": prep["user_interaction_id"], "job_id": job_id,
                    "device": target_label}

    # ── TUI bridge path: forward to the TUI agent instead of running the
    # normal agent loop. The TUI processes the message and streams the reply
    # back. This is synchronous (the TUI bridge returns the full reply).
    # The web app does NOT apply its guardrails (tool gates, turn limits,
    # billing enforcement, memory save, context loading) — the TUI handles
    # all of that internally. Only execution_mode is passed through so the
    # frontend's read/write/auto toggle still works.
    if prep.get("is_tui_bridge"):
        from app.tui_bridge import forward_to_bridge, is_bridge_alive

        if is_bridge_alive(request.user_id):
            reply = await forward_to_bridge(
                request.user_id, request.session_id, request.message or "",
                execution_mode=getattr(request, 'execution_mode', 'ask') or 'ask',
            )
            if reply is not None:
                # Persist the assistant reply
                try:
                    await prep["db"].insert_interaction(
                        request.user_id, request.session_id,
                        role="assistant", content=reply,
                        parent_id=prep["user_interaction_id"],
                        channel=prep["channel"],
                        sender_id=prep["agent"]["id"],
                        receiver_id=request.user_id,
                    )
                except Exception as e:
                    logger.warning("Failed to persist TUI bridge reply: %s", e)

                # Emit the response event so the UI renders it
                await _emit_to_visualizers(request.session_id, {
                    "type": "response", "level": "agent",
                    "content": reply, "turn_id": prep["user_interaction_id"],
                }, user_id=request.user_id)

                return {
                    "status": "complete",
                    "session_id": request.session_id,
                    "turn_id": prep["user_interaction_id"],
                    "reply": reply,
                }
            else:
                # Bridge not available — fall through to error
                pass

        # Bridge not available — return an error message
        error_msg = ("The TUI agent is not running. "
                     "Start the Server Manager (TUI) to use this agent.")
        await _emit_to_visualizers(request.session_id, {
            "type": "error", "level": "agent",
            "message": error_msg, "turn_id": prep["user_interaction_id"],
        }, user_id=request.user_id)
        return {
            "status": "error",
            "session_id": request.session_id,
            "turn_id": prep["user_interaction_id"],
            "error": error_msg,
        }

    # ── Compaction in progress: queue this message durably instead of running ──
    # A /compact ("Compact now") is folding this session right now. Persist the
    # user row as status='queued' (queued_for='compact') so it survives refresh,
    # session switches and even a server restart, and let the drain that runs
    # when the compaction finishes start this turn. The client keeps the pending
    # bubble; the drain's 'running' broadcast clears it.
    if _is_compacting(request.session_id):
        try:
            await _mark_interaction_queued_for_compact(
                prep["db"], prep["user_interaction_id"])
            await notify_user(request.user_id, {
                "type": "agent_status", "status": "queued",
                "session_id": request.session_id,
                "turn_id": prep["user_interaction_id"],
                "queue_position": None,
                "queued_for": "compact",
            })
        except Exception as _qc:
            logger.warning("compact-queue marker failed for %s: %s",
                           request.session_id[:12], _qc)
        return {"status": "queued", "session_id": request.session_id,
                "turn_id": prep["user_interaction_id"], "queued_for": "compact"}

    # A normal message joins the active working loop at its next safe boundary.
    # Only /interrupt cancels a running task.
    _run_manager = get_run_manager()
    if _run_manager.is_running(request.session_id):
        await prep["db"].update_interaction(prep["user_interaction_id"], status="queued")
        await notify_user(request.user_id, {
            "type": "agent_status", "status": "queued",
            "session_id": request.session_id, "turn_id": prep["user_interaction_id"],
            "queued_for": "agent_inbox",
        })
        return {"status": "queued", "session_id": request.session_id,
                "turn_id": prep["user_interaction_id"], "queued_for": "agent_inbox"}

    status = await _run_manager.start_or_replace(
        session_id=request.session_id,
        user_id=request.user_id,
        turn_id=prep["user_interaction_id"],
        db=prep["db"],
        run_factory=lambda replaced: _run_turn_background(
            prep["db"], request, prep["agent"], prep["user_interaction_id"],
            prep["channel"], replaced=replaced,
            is_first_turn=prep.get("is_first_turn", False)),
    )
    return {"status": status, "session_id": request.session_id,
            "turn_id": prep["user_interaction_id"]}


@router.post("/stream")
async def chat_stream(request: ChatRequest, fastapi_request: Request):
    """
    SSE fallback for sending a message. Behaves like /send (saves the message and
    starts a supervised, connection-independent run) but also tails the run's
    events back over Server-Sent Events for this client. A disconnect here never
    interrupts the run — it keeps going server-side and is viewable from any
    device via the DB + WebSocket. Prefer /send + the WebSocket for new clients.
    """
    prep = await _prepare_send(request, fastapi_request)
    if prep.get("tunnel_handled"):
        async def _tunnel_noop():
            yield f"data: {json.dumps({'type': 'response', 'level': 'agent', 'content': ''})}\n\n"
        return StreamingResponse(_tunnel_noop(), media_type="text/event-stream")
    if "slash_result" in prep:
        result = prep["slash_result"]
        async def _slash_events():
            if prep.get("compaction"):
                yield f"data: {json.dumps(prep['compaction'])}\n\n"
                # The structured event renders the two-second concise notice
                # and folds its detail into the durable activity group.  Do not
                # stream that detail again as an ordinary assistant bubble.
                yield f"data: {json.dumps({'type': 'response', 'level': 'agent', 'content': ''})}\n\n"
                return
            yield f"data: {json.dumps({'type': 'stream', 'level': 'agent', 'content': result})}\n\n"
            yield f"data: {json.dumps({'type': 'response', 'level': 'agent', 'content': result})}\n\n"
        return StreamingResponse(_slash_events(), media_type="text/event-stream")
    if prep.get("duplicate"):
        # Idempotent replay (outbox retry of an already-accepted send): don't
        # start a second run — just close the stream so the client resolves.
        async def _dup_noop():
            yield f"data: {json.dumps({'type': 'response', 'level': 'agent', 'content': ''})}\n\n"
        return StreamingResponse(_dup_noop(), media_type="text/event-stream")

    # ── TUI bridge path ──
    if prep.get("is_tui_bridge"):
        from app.tui_bridge import forward_to_bridge, is_bridge_alive

        async def _tui_bridge_events():
            if is_bridge_alive(request.user_id):
                reply = await forward_to_bridge(
                    request.user_id, request.session_id, request.message or "",
                    execution_mode=getattr(request, 'execution_mode', 'ask') or 'ask',
                )
                if reply is not None:
                    # Persist the assistant reply
                    try:
                        await prep["db"].insert_interaction(
                            request.user_id, request.session_id,
                            role="assistant", content=reply,
                            parent_id=prep["user_interaction_id"],
                            channel=prep["channel"],
                            sender_id=prep["agent"]["id"],
                            receiver_id=request.user_id,
                        )
                    except Exception as e:
                        logger.warning("Failed to persist TUI bridge reply: %s", e)

                    yield f"data: {json.dumps({'type': 'stream', 'level': 'agent', 'content': reply})}\n\n"
                    yield f"data: {json.dumps({'type': 'response', 'level': 'agent', 'content': reply})}\n\n"
                else:
                    error_msg = "The TUI agent is not running. Start the Server Manager (TUI) to use this agent."
                    yield f"data: {json.dumps({'type': 'error', 'level': 'agent', 'message': error_msg})}\n\n"
            else:
                error_msg = "The TUI agent is not running. Start the Server Manager (TUI) to use this agent."
                yield f"data: {json.dumps({'type': 'error', 'level': 'agent', 'message': error_msg})}\n\n"

        return StreamingResponse(_tui_bridge_events(), media_type="text/event-stream")

    # ── Compaction in progress: durably queue + close the stream ──
    # Mirrors /send: while a /compact folds this session, the message is
    # persisted as status='queued' (queued_for='compact') and runs when the
    # compaction drain fires. The client sees a 'queued' event and keeps its
    # pending bubble.
    if _is_compacting(request.session_id):
        try:
            await _mark_interaction_queued_for_compact(
                prep["db"], prep["user_interaction_id"])
            await notify_user(request.user_id, {
                "type": "agent_status", "status": "queued",
                "session_id": request.session_id,
                "turn_id": prep["user_interaction_id"],
                "queue_position": None,
                "queued_for": "compact",
            })
        except Exception as _qc:
            logger.warning("compact-queue marker failed for %s: %s",
                           request.session_id[:12], _qc)

        async def _queued_events():
            yield f"data: {json.dumps({'type': 'queued', 'status': 'queued', 'queued_for': 'compact', 'session_id': request.session_id, 'turn_id': prep['user_interaction_id']})}\n\n"
        return StreamingResponse(_queued_events(), media_type="text/event-stream")

    _run_manager = get_run_manager()
    if _run_manager.is_running(request.session_id):
        await prep["db"].update_interaction(prep["user_interaction_id"], status="queued")
        await notify_user(request.user_id, {
            "type": "agent_status", "status": "queued",
            "session_id": request.session_id, "turn_id": prep["user_interaction_id"],
            "queued_for": "agent_inbox",
        })
    else:
        await _run_manager.start_or_replace(
            session_id=request.session_id,
            user_id=request.user_id,
            turn_id=prep["user_interaction_id"],
            db=prep["db"],
            run_factory=lambda replaced: _run_turn_background(
                prep["db"], request, prep["agent"], prep["user_interaction_id"],
                prep["channel"], replaced=replaced,
                is_first_turn=prep.get("is_first_turn", False)),
        )

    async def safe_event_generator():
        try:
            async for chunk in _sse_tail_run(request.session_id):
                yield chunk
        except Exception as e:
            logger.error("SSE tail unhandled error: %s", e, exc_info=True)
            yield f"data: {json.dumps({'type': 'error', 'level': 'agent', 'message': str(e)})}\n\n"

    return StreamingResponse(safe_event_generator(), media_type="text/event-stream")


class _ForceRunRequest(BaseModel):
    user_id: str   # caller (must own / participate in the session)
    session_id: str


@router.post("/force-run")
async def force_session_run(req: _ForceRunRequest, fastapi_request: Request):
    """Immediately grant a queued session a run slot, bypassing the session cap.

    When max_active_sessions is reached, a turn waits in the FIFO gate queue
    (app/agent/session_gate.py). This endpoint is the "force run" escape
    hatch: it pulls the session out of the queue and grants it a slot NOW, so
    its waiting background turn starts immediately — temporarily running one
    over the cap. The slot is released normally when the turn finishes.

    Only the session owner/participant may force their own session. No-op
    (but still 200) when the session isn't actually queued — e.g. it already
    got a slot or the gate is disabled.
    """
    from app.agent import session_gate
    user_id, db = await _require_chat_session_access(fastapi_request, req.user_id, req.session_id)

    queued = session_gate.queued_position(req.session_id) is not None
    gate_forced = await session_gate.force_acquire(req.session_id, user_id=user_id)
    if queued and not gate_forced:
        raise HTTPException(status_code=409, detail={
            "code": "quota_exceeded", "limit": "concurrent_sessions_per_user",
        })

    # Not gate-queued? The pending bubble may be a compaction-queued message
    # (durable status='queued' + metadata queued_for='compact', waiting for a
    # /compact to finish). "Force run" then means: skip the remaining compaction
    # wait and run the queued message NOW — the drain flips it to 'complete' and
    # dispatches the turn, whose 'running' broadcast clears the bubble.
    forced_compact = False
    if not queued:
        try:
            forced_compact = (await _drain_compaction_queue(
                db, req.session_id, user_id, "web_portal", "ask")) > 0
        except Exception as _fdq:
            logger.debug("force-run compaction drain failed for %s: %s",
                         req.session_id[:12], _fdq)

    # The turn wakes inside _run_turn_background and emits agent_status:
    # "running" itself (via run_state_begin), which clears the frontend's
    # queued styling — no extra broadcast needed here.

    # Fire-and-forget diagnostics record so the force is traceable.
    try:
        from app.agent.diagnostics import record as _diag
        _diag("info", "run", f"Session {req.session_id[:12]} force-run by user {user_id[:8]}",
              source="force_run", session_id=req.session_id)
    except Exception:
        pass

    return {"status": "ok", "session_id": req.session_id, "forced": queued or forced_compact}


async def _save_chat_to_memory(
    db, user_id: str, session_id: str,
    user_message: str, assistant_reply: str, agent_id: str,
    parent_interaction_id: Optional[str] = None,
) -> None:
    """Save chat conversation to memory as visible tool interaction."""
    slug = f"chat/{session_id[:8]}"
    # Describe the save up-front so the memory_save_end event can carry it as the
    # tool-result body for the LIVE chat bubble even if the upsert below fails
    # (the persisted row uses the same content; reload renders from that).
    save_content = json.dumps({
        "action": "upserted",
        "slug": slug,
        "summary": f"Saved chat: {user_message[:60]}...",
    }, indent=2)
    save_args = {"user_message": user_message[:200], "assistant_reply": assistant_reply[:200]}
    save_ok = False
    try:
        # Save chat as a memory page
        result = await db.memory_upsert(
            user_id, slug, "meeting",
            title=f"Session {session_id[:8]}",
            compiled_truth=assistant_reply[:500],
            timeline=user_message[:200],
            origin="distilled",
            source_session_id=session_id,
            source_interaction_id=parent_interaction_id,
        )

        # Save memory_save as visible tool interaction. Offloaded: this runs in a
        # background task that overlaps the NEXT turn — a blocking write here would
        # freeze that turn's stream. See app/db/offload.py.
        await db_offload(lambda: db.insert_interaction(
            user_id, session_id, role="tool",
            content=save_content,
            parent_id=parent_interaction_id,
            tool_name="memory_save",
            channel="web_portal",
            metadata=json.dumps({"brain": True, "slug": slug, "args": save_args}),
            output_data=save_content,
            sender_id=agent_id,
            receiver_id=agent_id,
        ))

        await _emit_to_visualizers(session_id, {
            "type": "db", "level": "db",
            "op": "memory_upsert", "slug": slug, "page_type": "meeting",
        }, user_id=user_id)
        save_ok = True
        logger.debug("Saved chat to memory: %s", slug)
    except Exception as e:
        logger.warning("Failed to save chat to memory: %s", e)
    finally:
        # Always tell the activity chip the save has FINISHED — on the error path
        # too — so the post-turn "Saving memory" note clears the moment the work is
        # actually done, rather than hanging until the frontend's settle backstop
        # (the bar treats this *_end as a terminal-for-housekeeping signal). The
        # args + result body let the live chat render this as its own foldable
        # memory_save tool bubble (mirroring the persisted reload render).
        try:
            await _emit_to_visualizers(session_id, {
                "type": "pipeline", "level": "pipeline",
                "step": "memory_save_end", "slug": slug,
                "args": save_args, "result": save_content, "ok": save_ok,
            }, user_id=user_id)
        except Exception:
            pass


# ── Visualizer listener registry ──
# WebSocket subscribers that receive pipeline events for HTTP chat sessions.
_visualizer_listeners: Dict[str, List[Any]] = {}  # session_id → list of WebSocket objects


def register_visualizer_listener(session_id: str, websocket: Any):
    """Register a WebSocket as a visualizer listener for a session."""
    if session_id not in _visualizer_listeners:
        _visualizer_listeners[session_id] = []
    _visualizer_listeners[session_id].append(websocket)


def unregister_visualizer_listener(session_id: str, websocket: Any):
    """Remove a WebSocket from the visualizer listeners."""
    if session_id in _visualizer_listeners:
        _visualizer_listeners[session_id] = [
            ws for ws in _visualizer_listeners[session_id] if ws is not websocket
        ]
        if not _visualizer_listeners[session_id]:
            del _visualizer_listeners[session_id]


async def _maybe_describe_images(db, user_id, message, user_interaction_id,
                                 loop_config, attachment_docs, out,
                                 agent_id="", agent_rec=None, session_id=""):
    """Attachment type-router step (async generator).

    Routes each attached image by the agent's actual model capability (see
    app/agent/attachment_router.py + plugins/abilities/image_vision.json):

      • inline     — the turn model can see images → leave them inlined.
      • describe   — blind brain + Image Vision enabled + a vision worker exists →
                     describe each image once via a one-shot vision model with a
                     context-tailored prompt, fold the description + guidance into
                     the user message, cache it on the attachment row.
      • unreadable — Image Vision off or no vision model configured → fold an
                     anti-hallucination note ("you can't read this; tell the user")
                     and DO NOT inline the image to the blind model.

    Yields pipeline event dicts for the caller to emit on its own transport, and
    writes results into ``out``:
      out["message_text"]  → text to send as the user message
      out["inline_docs"]   → attachments to pass to build_user_message_content
    """
    from app.agent.prompts import _VISION_INLINE_MIMES, describe_image_attachment
    from app.agent.attachment_router import plan_image_attachments

    out["message_text"] = message
    out["inline_docs"] = attachment_docs

    image_atts = [a for a in (attachment_docs or [])
                  if (a.get("mime_type") or "").lower() in _VISION_INLINE_MIMES]
    if not image_atts or not loop_config.is_enabled("attachment_describe"):
        return

    from app.admin.settings import load_llm_capabilities_for_user, media_routing
    try:
        caps = await load_llm_capabilities_for_user(user_id, agent_rec=agent_rec)
    except Exception as e:
        logger.warning("attachment route: capability read failed: %s", e)
        return
    routing = media_routing(caps)

    # Short conversation tail so the worker can tailor the description to context.
    context = ""
    if session_id:
        try:
            recs = await db.fetch_interactions(user_id, session_id)
            tail = [r for r in recs if getattr(r, "role", "") in ("user", "assistant")][-6:]
            context = "\n".join(
                f"{getattr(r, 'role', '')}: {(getattr(r, 'content', '') or '')[:500]}"
                for r in tail
            )
        except Exception:
            context = ""

    plan = await plan_image_attachments(
        agent_id=agent_id, user_id=user_id, image_atts=image_atts,
        caps=caps, routing=routing, context=context, request=message or "",
    )
    mode = plan.get("mode")
    if mode == "inline":
        return  # a turn model can see the image natively → leave it inlined

    # From here the image is NOT inlined to the (blind) brain; other types pass through.
    out["inline_docs"] = [a for a in (attachment_docs or [])
                          if (a.get("mime_type") or "").lower() not in _VISION_INLINE_MIMES]
    parts = [message] if message else []
    guidance = plan.get("guidance") or ""

    if mode == "unreadable":
        # The directive is an instruction to the AGENT, not content the user wrote.
        # Surface it as an inspectable tool row (foldable, like process_image) and
        # deliver it to the model for THIS turn via message_text — but DO NOT write
        # it into the user's interaction row, so it never shows in the chat bubble.
        names = ", ".join(a.get("original_name", "image") for a in image_atts) or "an image"
        reason = plan.get("reason") or "unreadable"
        yield {"type": "tool_call", "level": "agent", "tool": "route_attachment",
               "args": {"attachment": names, "decision": "unreadable", "reason": reason},
               "turn": 1}
        yield {"type": "tool_result", "level": "agent", "tool": "route_attachment",
               "result": guidance or "(no guidance)", "error": True, "turn": 1}
        # Persist the route decision as an inspectable tool row (same rationale as
        # the process_image row below) so this "unreadable" tool call survives a
        # reload instead of being a live-only event. tool_call_id stays None →
        # dropped from model history (the guidance reaches the model this turn via
        # message_text, and is intentionally NOT folded into the user turn).
        try:
            await db.insert_interaction(
                user_id, session_id, role="tool",
                content=guidance or "(no guidance)",
                parent_id=user_interaction_id,
                tool_name="route_attachment",
                channel="web_portal",
                metadata=json.dumps({"attachment": names, "decision": "unreadable",
                                     "reason": reason, "error": True,
                                     "args": {"attachment": names, "decision": "unreadable",
                                              "reason": reason}}),
                output_data=guidance or "",
                sender_id=agent_id or None,
                receiver_id=agent_id or None,
            )
        except Exception as e:
            logger.debug("attachment route: route_attachment row persist failed: %s", e)
        if guidance:
            parts.append(f"\n\n{guidance}")
        out["message_text"] = "".join(parts).strip() or (message or "")
        yield {"type": "pipeline", "level": "pipeline", "step": "attachment_describe_end",
               "image_count": len(image_atts), "status": "unreadable", "reason": reason}
        return

    # mode == "describe"
    describer = plan.get("describer") or {}
    import time as _t
    from datetime import datetime, timezone
    yield {"type": "pipeline", "level": "pipeline", "step": "attachment_describe_start",
           "image_count": len(image_atts), "vision_model": describer.get("model", "")}
    _start = _t.time()
    described = 0
    cached = 0
    for a in image_atts:
        name = a.get("original_name", "image")
        meta = a.get("metadata") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        # Surface the vision-worker call as an inspectable tool call so the user
        # can expand it to see the EXACT prompt we generated and the worker's
        # response. Tagged turn=1 so it attaches to this exchange's reply bubble.
        tool_args = {
            "attachment": name,
            "vision_model": describer.get("model", ""),
            "system_prompt": plan.get("worker_system") or "(built-in default vision-describe prompt)",
            "instruction": plan.get("worker_instruction") or "Describe this image in detail.",
            "user_message": message or "",
        }
        yield {"type": "tool_call", "level": "agent", "tool": "process_image",
               "args": tool_args, "turn": 1}
        _img_start = _t.time()
        desc = None
        was_cached = False
        err_sink: list = []
        if isinstance(meta, dict) and meta.get("vision_description") \
                and meta.get("vision_describer_model") == describer.get("model"):
            desc = meta.get("vision_description")
            cached += 1
            was_cached = True
        if not desc:
            desc = await describe_image_attachment(
                a, describer, user_text_hint=message,
                system_prompt=plan.get("worker_system") or None,
                instruction=plan.get("worker_instruction"),
                error_sink=err_sink,
            )
            if desc:
                described += 1
                try:
                    await db.update_attachment_metadata(a.get("id"), {
                        "vision_description": desc,
                        "vision_describer_model": describer.get("model", ""),
                        "vision_described_at": datetime.now(timezone.utc).isoformat(),
                    })
                except Exception as e:
                    logger.debug("attachment route: metadata cache failed: %s", e)
        _img_ms = int((_t.time() - _img_start) * 1000)
        # Surface the REAL failure reason (e.g. the provider being out of credits)
        # to the agent and the user instead of a silent generic fallback. The
        # description worker swallows exceptions and returns None, so without this
        # the failure was invisible: the foldable tool row said "could not describe"
        # and the user never learned WHY vision was down.
        err_text = (err_sink[-1] if err_sink else "").strip()
        if not desc and err_text:
            logger.warning(
                "attachment route: image describe failed (model=%s, att=%s): %s",
                describer.get("model", ""), a.get("id"), err_text)
            try:
                from app.util.alerts import is_provider_credit_error, persist_402_alert
                if is_provider_credit_error(err_text):
                    await persist_402_alert(err_text, user_id, session_id,
                                            describer.get("model", ""),
                                            describer.get("provider", ""))
            except Exception:
                pass
        fail_note = " — {}".format(err_text) if (not desc and err_text) else ""
        yield {"type": "tool_result", "level": "agent", "tool": "process_image",
               "result": (desc or "(the vision model could not describe this image)") + fail_note,
               "duration_ms": _img_ms,
               "error": not bool(desc), "turn": 1}
        # Persist the vision-describe step as an inspectable tool interaction —
        # mirrors how memory_search is recorded (role=tool, parent=the user turn).
        # Without this the foldable process_image row is a LIVE-ONLY event that
        # vanishes on reload, and the description was only recoverable from the
        # text folded into the user message (which then wrongly rendered inside
        # the user's chat bubble). tool_call_id stays None so the model-history
        # builder drops this synthetic row (a real model-issued process_image call
        # has a tool_call_id and is kept); the model still receives the
        # description via the fold persisted on the user turn below.
        try:
            await db.insert_interaction(
                user_id, session_id, role="tool",
                content=(desc or "(the vision model could not describe this image)") + fail_note,
                parent_id=user_interaction_id,
                tool_name="process_image",
                channel="web_portal",
                metadata=json.dumps({
                    "attachment": name,
                    "vision_model": describer.get("model", ""),
                    "cached": was_cached,
                    "error": not bool(desc),
                    "duration_ms": _img_ms,
                    "args": tool_args,
                    **({"error_detail": err_text} if err_text else {}),
                }),
                output_data=desc or "",
                sender_id=agent_id or None,
                receiver_id=agent_id or None,
            )
        except Exception as e:
            logger.debug("attachment route: process_image row persist failed: %s", e)
        if desc:
            parts.append(f"\n\n[Attached image — '{name}']:\n{desc}")
        else:
            parts.append(f"\n\n[Attached image — '{name}']:\n(Image could not be described.){fail_note}")

    # Persist the user turn with the image DESCRIPTION folded in (so the model keeps
    # it across turns) but NOT the guidance NOTE — the note is an instruction to the
    # agent, not user content, so it stays out of the chat bubble. The model still
    # receives the note this turn via message_text below.
    persisted = "".join(parts).strip() or (message or "")
    try:
        await db.update_interaction_content(user_interaction_id, persisted)
    except Exception as e:
        logger.debug("attachment route: persist failed: %s", e)
    if guidance:
        parts.append(f"\n\n{guidance}")
    out["message_text"] = "".join(parts).strip() or (message or "")

    yield {"type": "pipeline", "level": "pipeline", "step": "attachment_describe_end",
           "image_count": len(image_atts), "vision_model": describer.get("model", ""),
           "described": described, "cached": cached,
           "duration_ms": int((_t.time() - _start) * 1000),
           "status": "ok" if (described or cached) else "partial"}


# Default fold/chip wording if the catalog has no element_pickup.template.
# Mirrors data/config + app/defaults app-prompts.json; both the model fold and the
# foldable chip's readable summary are rendered from this exact text.
_APP_CONTROL_FALLBACK_TEMPLATE = (
    '[App Control · {intent} · pointing at "{label}" ({descriptor}) in the {region} '
    'of the {page} page, at x={x}, y={y}. Locator: {selector}. Current style: {styles}. '
    'Markup: {html}]: {text}'
)


def _app_control_template() -> str:
    """The element-pickup wording — backend-adjustable via app-prompts.json
    (ui_handoffs.element_pickup.template), no UI. Falls back to the built-in
    default when the catalog is unreachable or omits it."""
    try:
        from app.util.paths import app_prompts_path
        data = json.loads(app_prompts_path().read_text(encoding="utf-8"))
        tpl = ((data.get("ui_handoffs") or {}).get("element_pickup") or {}).get("template")
        if isinstance(tpl, str) and tpl.strip():
            return tpl
    except Exception:
        pass
    return _APP_CONTROL_FALLBACK_TEMPLATE


def _format_app_control(tpl: str, fields: Dict[str, Any]) -> str:
    import re
    return re.sub(r"\{(\w+)\}", lambda m: str(fields.get(m.group(1), m.group(0))), tpl)


async def _maybe_emit_app_control(db, request, user_interaction_id, desc_out,
                                  agent_id="", session_id="", channel="web_portal"):
    """Launcher element-pickup hand-off (async generator).

    When the user enables launcher element pickup and sends a widget message, the
    frontend forwards a small *fingerprint* (request.app_control): the human label,
    the element's role, the page + region it sits in, a CSS locator, a slice of its
    computed style + markup, the cursor x/y, and the words the user typed. We:
      • emit a foldable ``app_control`` tool call (LIVE render — same machinery as
        process_image, see chat-activity.js _SYNTH_TOOLS) so the technical detail
        lives in its own chip instead of bloating the user's message bubble;
      • persist it as a display-only role=tool row (tool_call_id stays None → out of
        model history) so the chip survives a reload (see db_viewer _row_to_msg);
      • fold the fingerprint into the user message FOR THIS TURN ONLY (via
        desc_out["message_text"]) so the agent acts on exactly what was clicked. The
        fingerprint is NOT written into the user turn, so the visible chat bubble
        keeps only the words the user typed.
    """
    fp = getattr(request, "app_control", None)
    if not fp or not isinstance(fp, dict):
        return

    def _clip(v, n):
        s = "" if v is None else str(v)
        return s.replace("\r", " ").replace("\n", " ").strip()[:n]

    fields = {
        "intent": _clip(fp.get("intent"), 80) or "Point and ask",
        "label": _clip(fp.get("label"), 80) or "the page",
        "descriptor": _clip(fp.get("descriptor"), 40) or "area",
        "region": _clip(fp.get("region"), 60) or "main area",
        "page": _clip(fp.get("page"), 40) or "app",
        "selector": _clip(fp.get("selector"), 200) or "(unknown)",
        "styles": _clip(fp.get("styles"), 300) or "(unavailable)",
        "html": _clip(fp.get("html"), 400) or "(unavailable)",
        "x": _clip(fp.get("x"), 8),
        "y": _clip(fp.get("y"), 8),
        "text": _clip(fp.get("text"), 2000),
    }
    summary = _format_app_control(_app_control_template(), fields)

    # LIVE foldable chip (paired call + result); turn=1 sits it with this exchange.
    yield {"type": "tool_call", "level": "agent", "tool": "app_control",
           "args": fields, "turn": 1}
    yield {"type": "tool_result", "level": "agent", "tool": "app_control",
           "result": summary, "duration_ms": 0, "error": False, "turn": 1}

    # Persist as a display-only synthetic tool row (parented to the user turn, no
    # tool_call_id) so reload rebuilds the chip — mirrors the process_image row.
    try:
        await db.insert_interaction(
            request.user_id, session_id, role="tool",
            content=summary,
            parent_id=user_interaction_id,
            tool_name="app_control",
            channel=channel,
            metadata=json.dumps({"app_control": True, "region": fields["region"],
                                 "page": fields["page"], "error": False, "args": fields}),
            output_data=summary,
            sender_id=agent_id or None,
            receiver_id=agent_id or None,
        )
    except Exception as e:
        logger.debug("app control: tool row persist failed: %s", e)

    # Deliver the fingerprint to the model THIS turn. The template already embeds
    # the user's words ({text}), so when no other fold is present the summary IS the
    # message; if an image description was folded in too, keep it and append context.
    base = desc_out.get("message_text", request.message) or ""
    words = request.message or ""
    if base.strip() == words.strip():
        desc_out["message_text"] = summary
    else:
        desc_out["message_text"] = (base + "\n\n" + summary).strip()


from app.events.delivery import _bounded_send, _close_evicted, _emit_to_user_listeners, notify_user


async def _emit_to_visualizers(
    session_id: str,
    event: Dict[str, Any],
    user_id: Optional[str] = None,
    db_override: Optional[Any] = None,
):
    """Push an event to all visualizer listeners for a session, and optionally user listeners.

    Side effect: if a RunBuffer is active for this session, the event is
    stamped with session_seq / turn_id / turn_seq / emit_time before broadcast,
    so reconnecting clients can replay events newer than their last seen seq.
    """
    from app.chat.context import current as _chat_execution
    _execution = _chat_execution.get()
    if _execution is not None:
        await _execution.observe(event)
        return
    import json
    # Tag the event with its originating session so per-USER WebSocket
    # subscribers (which receive events for ALL of the user's sessions) can
    # route it to the right session and NOT render it into whatever session
    # happens to be active. Without this, live events arrive untagged and the
    # frontend's session filter fails open. Set before stamp_event so the copy
    # stored in the RunBuffer (used for replay) carries it too.
    event["session_id"] = session_id
    # Stamp via the in-memory run buffer (if a turn is active for this session).
    # This mutates `event` to add session_seq / turn_id / turn_seq / emit_time.
    try:
        _reg = get_run_buffer_registry()
        _buf = _reg.get(session_id)
        if _buf is not None:
            _buf.stamp_event(event)
    except Exception as _be:
        logger.debug("RunBuffer stamp failed for session %s: %s", session_id, _be)

    # Mirror only a small sanitized event tail into any live ephemeral voice
    # session for this chat. This is process memory only: it gives the voice
    # coordinator the same progress cursor without creating interaction rows.
    try:
        from app.agent.voice_session import get_voice_session_registry

        _voice_registry = get_voice_session_registry()
        _voice_live = _voice_registry.has_live_session(session_id, user_id)
        _voice_registry.observe_agent_event(session_id, user_id, event)
        if _voice_live:
            from app.agent.voice_narration import get_voice_narration_broker
            get_voice_narration_broker().publish_nowait(
                session_id, event, detail="detailed",
            )
    except Exception as _ve:
        logger.debug("voice session event mirror failed for %s: %s", session_id, _ve)

    # Backfill the ordering number onto EVERY db-persisted row as its "db"
    # event is stamped. The loop saves rows (assistant on first token, tool
    # results, system messages etc.) WITHOUT a session_seq — so without this
    # they stay NULL and the reconcile tail's `session_seq IS NOT NULL` filter
    # can never see them (rows go missing on any DB-reconcile render). The stamp
    # goes local-first via the same helper the user row uses, so the row becomes
    # visible immediately and the push carries the number to remote too.
    try:
        if (event.get("type") == "db"
                and event.get("op") != "update_interaction"
                and event.get("id") and event.get("session_seq") is not None):
            _asst_id = event.get("id")
            # Candidate only: this event's replay cursor. It is the legacy value
            # for a row that somehow reached the store with no durable order —
            # the row's own session_seq always wins (see below).
            _asst_ss = int(event.get("session_seq"))
            _asst_tid = event.get("turn_id")
            _asst_ts = event.get("turn_seq")
            # Optimizer/closer sessions write to a temp DB. Reopening the global
            # backend here stamped the wrong database and left every assistant
            # row NULL-session_seq, breaking reconcile and replay ordering.
            _db_seq = db_override or get_db()
            _durable_ss = await db_offload(lambda: _db_seq.stamp_interaction_seq(
                _asst_id, _asst_ss, _asst_tid, _asst_ts))
            # State the durable row order separately from this event's replay
            # cursor. Clients cache it by interaction id and position transcript
            # nodes with it, so it MUST be the row's real session_seq: the replay
            # cursor advances once per EVENT while durable rows advance once per
            # ROW, so the cursor ends a run far above the ledger (observed 1158 vs
            # 49 in one run). Publishing the cursor here stamped the run's terminal
            # auto message (max-turns / stall guard) above every durable row, so it
            # sorted to the bottom of the transcript as the newest message.
            if _durable_ss is not None:
                event["interaction_seq"] = int(_durable_ss)
            else:
                # Row unreadable: publish no durable order rather than a transport
                # number the client would treat as one.
                event.pop("interaction_seq", None)
    except Exception as _ae:
        logger.debug("assistant row seq backfill failed for session %s: %s", session_id, _ae)

    if event.get("type") == "db" and event.get("id"):
        try:
            _db_activity = db_override or get_db()
            _summary = await db_offload(lambda: _db_activity.get_activity_summary_for_interaction(
                user_id, session_id, str(event["id"]),
            ))
            if _summary:
                event["activity_summary"] = _summary
        except Exception as _summary_error:
            logger.debug("activity summary snapshot unavailable for %s: %s", event.get("id"), _summary_error)

    # Durable in-flight-op snapshot: persist which tool is running so a refresh
    # can re-show the live "in-process" indicator (cleared when the tool finishes
    # or the run ends). Only tool events touch the DB — everything else is a no-op.
    _et = event.get("type")
    if _et in ("tool_call", "tool_result"):
        try:
            if _et == "tool_call":
                _tool = event.get("tool") or "tool"
                _op = json.dumps({
                    "tool": _tool,
                    "turn": event.get("turn"),
                    "note": "Toolcall " + _tool,
                })
            else:
                _op = None  # tool finished — back to a generic "thinking" state
            # Offloaded: emitted for every tool event mid-turn; a blocking write
            # here would freeze the loop (and the stream) on each tool call.
            _db_op = db_override or get_db()
            await db_offload(lambda: _db_op.run_state_set_op(session_id, _op))
        except Exception as _oe:
            logger.debug("run_state_set_op failed for session %s: %s", session_id, _oe)

    # RECORDER-HOOK: plugins/recorder/service.py observes stamped stream facts.
    # No listener means no copying or IO. Remove this try block only; retain
    # sequence stamping, durable writes and visualizer delivery around it.
    try:
        from app.observation import emit as _observe
        _observe("stream.emit", lambda: {**event, "user_id": user_id})
    except Exception:
        pass

    # Flight-recorder tap: keep interesting loop/tool events (pipeline problems,
    # tool errors) for post-hoc diagnosis. Cheap + swallows its own errors.
    try:
        from app.agent.diagnostics import tap_loop_event
        tap_loop_event(session_id, event)
    except Exception:
        pass

    listeners = _visualizer_listeners.get(session_id, [])
    disconnected = []
    for ws in listeners:
        if not await _bounded_send(ws, json.dumps(event)):
            disconnected.append(ws)
    for ws in disconnected:
        unregister_visualizer_listener(session_id, ws)
        await _close_evicted(ws)
    # Also broadcast to per-user listeners if user_id provided
    if user_id:
        await _emit_to_user_listeners(user_id, event)


