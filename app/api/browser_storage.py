"""
Browser-authority storage endpoints — stateless chat that calls the REAL agent
loop.  The full transcript lives in the browser's IndexedDB; the server writes
nothing to any database.

Phase 2: real agent loop with full lifecycle — billing, credential resolution,
browser-session management, interrupt, turn hooks (titles), and memory
extraction.  Sync/promote endpoints move IndexedDB data into per-user SQLite.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import uuid
from typing import Any, AsyncGenerator, Dict, List, Optional
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.db import get_db
from app.auth.identity import assert_caller_is, request_user_id
from app.db.browser_authority import BrowserAuthorityDB
from app.agent.browser_history_cache import (
    get_browser_history_cache,
    get_browser_turn_replay_cache,
)
from app.agent.prompts import (
    build_system_prompt,
    build_system_prompt_parts,
    append_skills_section,
    CONTEXT_SECTION_TYPES,
)
from app.api.rate_limit import enforce_tier_chat

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/browser", tags=["browser_storage"])

# ── In-memory caches ────────────────────────────────────────────────────────
_INTERRUPTS: Dict[tuple[str, str], asyncio.Event] = {}
_ACTIVE_TURNS_LOCK = asyncio.Lock()
_BROWSER_SESSION_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"


# ── Request / response models ──────────────────────────────────────────────

class BrowserChatRequest(BaseModel):
    agent_id: str = Field(..., min_length=1, max_length=128)
    new_message: str = Field(..., min_length=1, max_length=200_000)
    interactions: List[Dict[str, Any]] = Field(default_factory=list, max_length=5000)
    session_id: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=_BROWSER_SESSION_ID_PATTERN,
    )
    config_hash: Optional[str] = Field(default=None, max_length=128)
    execution_mode: str = "auto"
    history_revision: int = Field(default=0, ge=0)
    history_token: Optional[str] = Field(default=None, max_length=256)
    idempotency_key: str = Field(..., min_length=8, max_length=128)


class BrowserSyncRequest(BaseModel):
    user_id: Optional[str] = None  # Legacy field; never trusted for identity.
    sessions: List[Dict[str, Any]] = Field(default_factory=list)
    interactions: List[Dict[str, Any]] = Field(default_factory=list)
    mutations: List[Dict[str, Any]] = Field(default_factory=list, max_length=10)


class BrowserPromoteRequest(BaseModel):
    anon_user_id: Optional[str] = None  # Legacy field; never trusted for identity.
    sessions: List[Dict[str, Any]] = Field(default_factory=list)
    interactions: List[Dict[str, Any]] = Field(default_factory=list)
    mutations: List[Dict[str, Any]] = Field(default_factory=list, max_length=10)
    email: Optional[str] = None
    name: Optional[str] = None


class BrowserAgentConfig(BaseModel):
    """Cache-safe subset of an agent record."""

    id: str
    name: str = ""
    description: str = ""
    model: str = ""


class BrowserToolConfig(BaseModel):
    """Non-secret tool metadata needed by the browser UI."""

    name: str
    requires_confirmation: bool = False
    destructive: bool = False


class BrowserConfigResponse(BaseModel):
    agent: BrowserAgentConfig
    config_hash: str
    tools: List[BrowserToolConfig] = Field(default_factory=list)
    abilities: List[str] = Field(default_factory=list)


# ── Helpers ─────────────────────────────────────────────────────────────────

def _require_browser_authority() -> None:
    from app.db.storage_router import browser_authority_enabled
    if not browser_authority_enabled():
        raise HTTPException(
            status_code=503,
            detail="Browser authority is disabled by server policy.",
        )


async def _require_admin(request: Request) -> str:
    uid = request_user_id(request)
    try:
        is_admin = bool(uid) and await get_db().is_user_admin(uid)
    except Exception:
        is_admin = False
    if not is_admin:
        raise HTTPException(status_code=403, detail="Restricted to admin users only.")
    return uid


def _compute_config_hash(agent: dict) -> str:
    import hashlib
    payload = json.dumps({
        "id": agent.get("id"),
        "name": agent.get("name"),
        "description": agent.get("description"),
        "model": agent.get("model"),
        "temperature": agent.get("temperature"),
        "max_tokens": agent.get("max_tokens"),
        "context_documents": agent.get("context_documents", []),
        "allowed_tools": agent.get("allowed_tools", []),
        "abilities": agent.get("abilities_list", []),
        "updated_at": agent.get("updated_at", ""),
    }, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


async def _get_agent_config(agent_id: str, user_id: str) -> dict:
    """Resolve caller-specific prompts and policy without a shared warm cache."""
    db = get_db()
    agent = await db.fetch_agent_by_id_with_context(
        agent_id,
        CONTEXT_SECTION_TYPES,
        user_id=user_id,
    )
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")

    agent.setdefault("context_documents", [])
    try:
        abilities = await db.get_agent_abilities(agent_id) or []
        agent["abilities_list"] = [a.get("ability_id") for a in abilities]
    except Exception:
        agent["abilities_list"] = []

    agent["_config_hash"] = _compute_config_hash(agent)
    return agent


def _disabled_agent_tools(agent: dict) -> List[str]:
    """Return the legacy ``allowed_tools`` deny/block list as strings."""
    raw = agent.get("allowed_tools", [])
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            raw = []
    if not isinstance(raw, list):
        return []
    return [name for name in raw if isinstance(name, str) and name]


def _public_browser_config(
    agent: dict,
    tools: Dict[str, Any],
) -> BrowserConfigResponse:
    """Build a DTO that cannot serialize handlers, credentials, or policy rows."""
    public_tools = [
        BrowserToolConfig(
            name=str(name),
            requires_confirmation=bool(
                getattr(info, "requires_confirmation", False)
            ),
            destructive=bool(getattr(info, "destructive", False)),
        )
        for name, info in sorted(tools.items())
    ]
    abilities = sorted({
        ability
        for ability in (agent.get("abilities_list") or [])
        if isinstance(ability, str) and ability
    })
    return BrowserConfigResponse(
        agent=BrowserAgentConfig(
            id=str(agent.get("id") or ""),
            name=str(agent.get("name") or ""),
            description=str(agent.get("description") or ""),
            model=str(agent.get("model") or ""),
        ),
        config_hash=str(agent.get("_config_hash") or ""),
        tools=public_tools,
        abilities=abilities,
    )


def _browser_interactions_to_history(interactions: List[dict]) -> List[dict]:
    """Convert IndexedDB interaction rows → OpenAI-style messages."""
    messages: List[dict] = []
    for ix in interactions:
        role = ix.get("role", "user")
        content = ix.get("content", "")

        if role == "tool":
            messages.append({
                "role": "tool",
                "tool_call_id": ix.get("tool_call_id", ""),
                "content": content or ix.get("output", ""),
            })
        elif role == "assistant":
            tool_calls = ix.get("tool_calls")
            if isinstance(tool_calls, str):
                try:
                    tool_calls = json.loads(tool_calls)
                except Exception:
                    tool_calls = None
            if tool_calls:
                messages.append({
                    "role": "assistant",
                    "content": content or None,
                    "tool_calls": tool_calls,
                })
            elif content:
                messages.append({"role": "assistant", "content": content})
        elif role in ("user", "system") and content:
            messages.append({"role": role, "content": content})

    return messages


def _sync_content_hash(session: dict, interactions: List[dict]) -> str:
    transient = {
        "_dirty", "_sync_error", "history_token", "synced_local_revision",
        "cache_expires_at", "last_accessed_at",
    }
    clean_session = {
        key: value for key, value in session.items() if key not in transient
    }
    clean_interactions = sorted(
        interactions,
        key=lambda item: (
            int(item.get("session_seq") or 0),
            str(item.get("id") or ""),
        ),
    )
    raw = json.dumps(
        {"session": clean_session, "interactions": clean_interactions},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _validate_sync_mutations(mutations: List[dict]) -> tuple[List[dict], List[dict]]:
    accepted: List[dict] = []
    rejected: List[dict] = []
    for mutation in mutations:
        session_id = str(mutation.get("session_id") or "")
        mutation_id = str(mutation.get("mutation_id") or "")
        operation = str(mutation.get("operation") or "upsert")
        payload_bytes = len(json.dumps(mutation, default=str).encode("utf-8"))
        error = ""
        if payload_bytes > 1024 * 1024:
            error = "session mutation exceeds 1 MiB"
        elif operation not in {"upsert", "delete"}:
            error = "operation must be upsert or delete"
        elif operation == "upsert":
            session = mutation.get("session")
            interactions = mutation.get("interactions") or []
            if not isinstance(session, dict):
                error = "upsert requires a session object"
            elif not isinstance(interactions, list) or len(interactions) > 2000:
                error = "upsert interactions must be a list of at most 2000 rows"
            elif any(str(ix.get("session_id") or "") != session_id for ix in interactions):
                error = "interaction references another session"
            else:
                expected = _sync_content_hash(session, interactions)
                supplied = str(mutation.get("content_hash") or "")
                if supplied and not secrets.compare_digest(supplied, expected):
                    error = "content_hash does not match payload"
                else:
                    mutation = {**mutation, "content_hash": expected}
        if error:
            rejected.append({
                "session_id": session_id,
                "mutation_id": mutation_id,
                "status": "rejected",
                "client_revision": int(mutation.get("client_revision") or 0),
                "error": error,
            })
        else:
            accepted.append(mutation)
    return accepted, rejected


async def _extract_memory(
    user_message: str, assistant_reply: str, session_id: str,
) -> Optional[dict]:
    """Run the same memory extraction as _save_chat_to_memory but return the
    result as a dict instead of writing to the DB."""
    if not assistant_reply:
        return None
    slug = f"chat/{session_id[:8]}"
    return {
        "slug": slug,
        "page_type": "meeting",
        "title": f"Session {session_id[:8]}",
        "compiled_truth": assistant_reply[:500],
        "timeline": user_message[:200],
        "summary": f"Saved chat: {user_message[:60]}...",
    }


# ── Endpoints ───────────────────────────────────────────────────────────────


@router.get("/config/{agent_id}")
async def get_browser_config(agent_id: str, fastapi_request: Request):
    """Return the agent's config payload for the browser to cache in IndexedDB."""
    _require_browser_authority()
    user_id = await assert_caller_is(fastapi_request, None)
    agent = await _get_agent_config(agent_id, user_id)

    db = get_db()
    from app.chat.execution import _enforce_agent_access_policy
    await _enforce_agent_access_policy(db, agent, user_id)
    tools = []
    try:
        from app.tools.loader import load_tools
        tools = await load_tools(
            user_id=user_id, agent_id=agent_id,
            agent_template_id=agent.get("template_id"),
            allowed_tools=_disabled_agent_tools(agent),
            gate_caller_access=True,
        )
    except Exception as e:
        logger.warning("Could not establish browser tool policy: %s", e)
        raise HTTPException(
            status_code=503,
            detail="Browser tool policy is temporarily unavailable.",
        ) from e

    return _public_browser_config(agent, tools)


@router.get("/routing")
async def get_browser_routing(fastapi_request: Request):
    """Return the storage routing config so the client knows where to send data."""
    user_id = await assert_caller_is(fastapi_request, None)
    from app.auth.revocation import current_epoch
    from app.db.browser_policy import public_browser_cache_policy
    from app.db.storage_router import get_storage_router, storage_capabilities
    from app.db.browser_canary import (
        cache_canary_eligible,
        canary_percent,
        rollback_active,
    )
    sr = get_storage_router()
    revocation_epoch = current_epoch(user_id)
    cache_scope = hashlib.sha256(
        f"webagent-browser-cache:{user_id}:{revocation_epoch}".encode("utf-8")
    ).hexdigest()[:24]
    capabilities = storage_capabilities()
    configured_cache = bool(capabilities.get("browser_session_cache"))
    capabilities["browser_session_cache"] = (
        configured_cache and cache_canary_eligible(user_id)
    )
    # Legacy cache-timing overrides from storage_routing.json take precedence
    # over defaults. Transcript retention remains quota-only.
    base_policy = public_browser_cache_policy()
    for _k, _v in (sr.cache_policy or {}).items():
        if _k in base_policy and isinstance(_v, int) and _v > 0:
            base_policy[_k] = _v
    return {
        "routing": sr.routing,
        "capabilities": capabilities,
        "cache_scope": cache_scope,
        "revocation_epoch": revocation_epoch,
        "cache_policy": {
            **base_policy,
            "canary_percent": canary_percent(),
            "canary_eligible": capabilities["browser_session_cache"],
            "rollback_active": rollback_active(),
        },
    }


@router.post("/chat")
async def browser_chat(request: BrowserChatRequest, fastapi_request: Request):
    """Stateless browser-authority chat — REAL agent loop + full lifecycle.

    Events streamed (SSE):
      - All agent loop events (stream, tool_call, tool_result, response, error,
        interrupted, pipeline, execution_mode, …)
      - ``session_run``  — turn lifecycle (started / completed / interrupted /
                           error) so the client can track auto-resume state
      - ``session_title`` — auto-generated by the session titler hook
      - ``memory_saved``  — extracted knowledge from the completed turn

    Nothing is written to any database.
    """
    _require_browser_authority()
    user_id = await assert_caller_is(fastapi_request, None)
    if not request.new_message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    # 1. Agent config
    agent = await _get_agent_config(request.agent_id, user_id)
    db = get_db()
    from app.entitlements.service import resolve_capabilities
    capabilities = await resolve_capabilities(user_id, db=db)
    if not (capabilities.get("features") or {}).get("chat"):
        raise HTTPException(
            status_code=403,
            detail={"code": "upgrade_required", "feature": "chat"},
        )
    await enforce_tier_chat(
        user_id, fastapi_request, db=db, message=request.new_message or "",
    )
    from app.chat.execution import _enforce_agent_access_policy, _enforce_billing_access
    await _enforce_agent_access_policy(db, agent, user_id)
    await _enforce_billing_access(db, agent, user_id)
    try:
        metadata = agent.get("metadata") or {}
        if isinstance(metadata, str):
            metadata = json.loads(metadata or "{}")
        engine_id = str(metadata.get("engine") or "default") if isinstance(metadata, dict) else "default"
    except Exception:
        engine_id = "default"
    if engine_id not in ("", "default"):
        raise HTTPException(
            status_code=409,
            detail="This agent engine does not implement browser authority.",
        )

    # 2. Build system prompt
    context_docs = agent.get("context_documents", [])
    system_prompt_parts = await build_system_prompt_parts(
        context_docs, brain_context=None, user_id=user_id,
        agent_id=request.agent_id, agent_rec=agent,
    )
    system_prompt_parts.agent_context = await append_skills_section(
        system_prompt_parts.agent_context, agent,
        session_id=None, caller_user_id=user_id,
    )
    system_prompt = system_prompt_parts.render()

    # 3. Resolve browser history from either the authoritative cold payload or
    #    an exact, one-time server token issued after the previous turn.
    session_id = request.session_id or f"browser-{uuid.uuid4().hex[:12]}"
    scope_key = (user_id, session_id)
    request_hash = hashlib.sha256(
        json.dumps(
            {
                "agent_id": request.agent_id,
                "session_id": session_id,
                "new_message": request.new_message,
                "history_revision": request.history_revision,
                "execution_mode": request.execution_mode,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    replay_cache = get_browser_turn_replay_cache()
    try:
        replay = await replay_cache.get(
            user_id, session_id, request.idempotency_key, request_hash
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if replay is not None:
        async def _replay_generator() -> AsyncGenerator[str, None]:
            for replay_event in replay:
                yield f"data: {json.dumps(replay_event)}\n\n"
        return StreamingResponse(
            _replay_generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    from app.agent.turn_reservations import (
        fail as fail_turn_reservation,
        reserve_turn,
    )
    turn_reservation = reserve_turn(
        user_id,
        session_id,
        request.idempotency_key,
        request_hash,
        lease_seconds=3600,
    )
    if turn_reservation.state == "conflict":
        raise HTTPException(status_code=409, detail=turn_reservation.detail)
    if turn_reservation.state == "busy":
        raise HTTPException(
            status_code=409,
            detail={"code": "turn_in_progress", "message": "This turn is already running."},
        )
    if turn_reservation.state in {"uncertain", "replay"}:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "turn_recovery_required",
                "message": (
                    "This turn was already accepted. Reload its authoritative "
                    "history instead of replaying external side effects."
                ),
            },
        )
    interrupt_event = asyncio.Event()
    async with _ACTIVE_TURNS_LOCK:
        if scope_key in _INTERRUPTS:
            fail_turn_reservation(turn_reservation)
            raise HTTPException(
                status_code=409,
                detail="A browser-authority turn is already active for this session.",
            )
        _INTERRUPTS[scope_key] = interrupt_event
    try:
        raw_size = len(json.dumps(request.interactions, default=str).encode("utf-8"))
        if raw_size > 4 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="Browser history exceeds 4 MiB.")
        history_cache = get_browser_history_cache()
        if request.history_token:
            history = await history_cache.consume(
                user_id,
                session_id,
                token=request.history_token,
                revision=request.history_revision,
            )
            if history is None:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "history_required",
                        "message": "History token is missing, expired, stale, or already consumed.",
                    },
                )
        else:
            if request.history_revision > 0 and not request.interactions:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "history_required",
                        "message": "Full history is required when no valid token is supplied.",
                    },
                )
            history = _browser_interactions_to_history(request.interactions)
            if not await history_cache.accept_cold_revision(
                user_id, session_id, request.history_revision
            ):
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "stale_revision",
                        "message": "A newer authoritative browser revision exists.",
                    },
                )
    except BaseException:
        fail_turn_reservation(turn_reservation)
        if _INTERRUPTS.get(scope_key) is interrupt_event:
            _INTERRUPTS.pop(scope_key, None)
        raise

    # 4. Tools. ``allowed_tools`` is the legacy deny/block list despite its name.
    _raw_at = _disabled_agent_tools(agent)

    execution_mode = getattr(request, "execution_mode", "auto") or "auto"

    from app.agent.loop import stream_agent_events
    authority_db = BrowserAuthorityDB(db, user_id=user_id, session_id=session_id)

    async def _event_generator() -> AsyncGenerator[str, None]:
        turn_id = uuid.uuid4().hex[:12]
        assistant_reply = ""
        emitted_events: List[Dict[str, Any]] = []

        def _sse(event: Dict[str, Any]) -> str:
            emitted_events.append(event)
            return f"data: {json.dumps(event)}\n\n"

        from app.db import set_db_override, reset_db_override
        from app.agent import session_gate
        override_token = None
        try:
            await session_gate.acquire(session_id, user_id=user_id)
            override_token = set_db_override(authority_db)
            # ── Signal turn start ────────────────────────────────────────
            yield _sse({'type': 'session_run', 'status': 'started', 'turn_id': turn_id, 'session_id': session_id})

            # ── Run the real agent loop ───────────────────────────────────
            async for event in stream_agent_events(
                user_id=user_id,
                session_id=session_id,
                user_message=request.new_message,
                system_prompt=system_prompt,
                agent_id=request.agent_id,
                history=history,
                parent_interaction_id=None,
                interrupt_event=interrupt_event,
                max_turns=agent.get("max_turn_count", 0),
                channel="browser",
                db=authority_db,
                agent_template_id=agent.get("template_id"),
                allowed_tools=_raw_at,
                execution_mode=execution_mode,
                system_prompt_parts=system_prompt_parts,
                run_turn_id=turn_id,
                turn_reservation_key=turn_reservation.key,
            ):
                if event.get("type") == "response":
                    assistant_reply = event.get("content", "")
                yield _sse(event)

            # ── Signal turn complete ──────────────────────────────────────
            next_history = list(history)
            next_history.append({"role": "user", "content": request.new_message})
            next_history.extend(
                _browser_interactions_to_history(authority_db.export_interactions())
            )
            history_ack = await history_cache.put(
                user_id,
                session_id,
                revision=request.history_revision + 1,
                history=next_history,
            )
            if history_ack:
                yield _sse({'type': 'history_ack', **history_ack})

            # Legacy turn hooks accept a raw DB and can update same-ID server
            # sessions. Browser authority instead emits a deterministic local
            # title without invoking that persistence surface.
            from app.chat.execution import _session_title_from_message
            title = _session_title_from_message(request.new_message)
            if request.history_revision == 0 and title:
                yield _sse({'type': 'session_title', 'session_id': session_id, 'status': 'done', 'title': title})

            # ── Memory extraction (Option A: SSE event, not DB write) ─────
            memory = await _extract_memory(
                request.new_message, assistant_reply, session_id,
            )
            if memory:
                yield _sse({'type': 'memory_saved', 'memory': memory})

            yield _sse({'type': 'session_run', 'status': 'complete', 'turn_id': turn_id, 'session_id': session_id, 'final_response': assistant_reply[:200]})
            await replay_cache.put(
                user_id,
                session_id,
                request.idempotency_key,
                request_hash,
                emitted_events,
            )
            from app.agent.turn_reservations import complete as complete_turn_reservation
            complete_turn_reservation(
                turn_reservation,
                {
                    "status": "complete",
                    "history_revision": int(history_ack.get("revision") or 0)
                    if history_ack
                    else request.history_revision + 1,
                    "content_hash": history_ack.get("content_hash", "")
                    if history_ack
                    else "",
                },
            )

        except asyncio.CancelledError:
            fail_turn_reservation(turn_reservation, uncertain=True)
            yield _sse({'type': 'session_run', 'status': 'interrupted', 'turn_id': turn_id, 'session_id': session_id})
            yield _sse({'type': 'interrupted', 'message': 'Turn was cancelled.'})
        except GeneratorExit:
            fail_turn_reservation(turn_reservation, uncertain=True)
            raise
        except Exception as e:
            fail_turn_reservation(turn_reservation, uncertain=True)
            logger.error("Browser agent loop error: %s", e, exc_info=True)
            yield _sse({'type': 'session_run', 'status': 'error', 'turn_id': turn_id, 'session_id': session_id, 'error': str(e)[:500]})
            yield _sse({'type': 'error', 'message': str(e)})
        finally:
            authority_db.close()
            if override_token is not None:
                reset_db_override(override_token)
            try:
                await session_gate.release(session_id)
            except Exception:
                logger.debug("Browser-authority session gate release failed", exc_info=True)
            if _INTERRUPTS.get(scope_key) is interrupt_event:
                _INTERRUPTS.pop(scope_key, None)

    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


async def interrupt_all_browser_turns() -> int:
    """Interrupt every in-progress browser-authority turn (kill switch).

    Sets each session's interrupt event; the agent loop stops at its next
    boundary and the SSE generator emits ``session_run`` with status
    ``interrupted`` so the browser's IndexedDB run state clears (spinner
    stops). Returns the number of turns signalled.
    """
    async with _ACTIVE_TURNS_LOCK:
        events = list(_INTERRUPTS.values())
    for ev in events:
        try:
            ev.set()
        except Exception:  # noqa: BLE001
            pass
    if events:
        logger.info("Kill switch: interrupted %d browser-authority turn(s)", len(events))
    return len(events)


@router.post("/interrupt/{session_id}")
async def browser_interrupt(session_id: str, fastapi_request: Request):
    """Request interruption of an in-progress browser chat session.

    Sets the asyncio.Event that the agent loop polls before each tool
    execution and after each LLM chunk.  The loop yields an ``interrupted``
    event, and the SSE generator yields ``session_run`` with status
    ``interrupted``.
    """
    _require_browser_authority()
    user_id = await assert_caller_is(fastapi_request, None)
    if not re.fullmatch(_BROWSER_SESSION_ID_PATTERN, session_id):
        raise HTTPException(status_code=422, detail="Invalid browser session id.")
    ev = _INTERRUPTS.get((user_id, session_id))
    if ev is None:
        return {"status": "not_found", "message": "No active session with that id."}
    ev.set()
    return {"status": "ok", "message": "Interrupt requested."}


@router.post("/config/evict")
async def evict_config(fastapi_request: Request, agent_id: str = Query(...)):
    """Compatibility endpoint; policy reads are authoritative on every request."""
    await _require_admin(fastapi_request)
    return {"ok": True}


# ── Sync & Promote ──────────────────────────────────────────────────────────


@router.post("/sync")
async def browser_sync(request: BrowserSyncRequest, fastapi_request: Request):
    """Apply revisioned browser mutations with independent results."""
    from app.db.user_store import get_user_store
    user_id = await assert_caller_is(fastapi_request, None)
    if not request.mutations:
        raise HTTPException(status_code=400, detail="Phase 1 sync requires mutations.")
    total_bytes = len(json.dumps(request.mutations, default=str).encode("utf-8"))
    if total_bytes > 4 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Sync request exceeds 4 MiB.")
    accepted, rejected = _validate_sync_mutations(request.mutations)
    store = get_user_store(user_id)
    try:
        results = await store.apply_sync_mutations(user_id, accepted)
        results.extend(rejected)
        applied = sum(r.get("status") in {"applied", "noop"} for r in results)
        return {
            "ok": all(r.get("status") in {"applied", "noop"} for r in results),
            "applied": applied,
            "results": results,
        }
    except Exception as e:
        logger.error("Browser sync failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Sync failed: {e}")


@router.post("/promote")
async def browser_promote(request: BrowserPromoteRequest, fastapi_request: Request):
    """Promote browser data using the same revisioned mutation contract."""
    from app.db.user_store import get_user_store
    user_id = await assert_caller_is(fastapi_request, None)
    if not request.mutations:
        raise HTTPException(status_code=400, detail="Phase 1 promote requires mutations.")
    total_bytes = len(json.dumps(request.mutations, default=str).encode("utf-8"))
    if total_bytes > 4 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Promote request exceeds 4 MiB.")
    accepted, rejected = _validate_sync_mutations(request.mutations)
    store = get_user_store(user_id)
    try:
        results = await store.apply_sync_mutations(user_id, accepted)
        results.extend(rejected)
        applied = sum(r.get("status") in {"applied", "noop"} for r in results)
        return {
            "ok": all(r.get("status") in {"applied", "noop"} for r in results),
            "applied": applied,
            "results": results,
            "user_id": user_id,
        }
    except Exception as e:
        logger.error("Browser promote failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Promote failed: {e}")


@router.get("/stats/{user_id}")
async def browser_stats(user_id: str, fastapi_request: Request):
    """Return row counts for a user's server-side data."""
    from app.db.user_store import get_user_store
    verified_user_id = await assert_caller_is(fastapi_request, user_id)
    store = get_user_store(verified_user_id)
    return await store.stats()
