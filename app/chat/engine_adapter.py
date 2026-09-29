"""Shared agent-work adapter for the controlled chat worker.

The accepted V2 message and run are authoritative. The legacy interaction
ledger remains an input to the reusable agent context builder, not a second
send, browser transcript, or run-state authority.
"""
import json
from .contracts import Conflict


async def prepare_controlled_turn(db, request, rows):
    from app.chat.execution import (
        CONTEXT_SECTION_TYPES, _ensure_session, _enforce_agent_access_policy,
        _is_codex_portal_agent, _record_session_execution_mode,
        _reset_agent_model_switcher, _session_title_from_message,
    )

    first = await _ensure_session(
        db, request.user_id, request.session_id,
        title=_session_title_from_message(request.message),
    )
    if await db.is_session_dead(request.session_id):
        raise Conflict("This session is in the recycle bin")
    agent = await db.fetch_agent_by_id_with_context(
        request.agent_id, CONTEXT_SECTION_TYPES, user_id=request.user_id)
    if not agent:
        raise Conflict("Session agent is unavailable")
    if _is_codex_portal_agent(agent):
        raise Conflict("Codex Portal tasks require the Portal endpoint")
    await _enforce_agent_access_policy(db, agent, request.user_id)
    bound = await db.get_session_agent_id(request.session_id)
    if bound and bound != agent["id"]:
        raise Conflict("Session is bound to a different agent")
    if not bound:
        await db.bind_session_to_agent(request.session_id, agent["id"])
    if not await db.is_session_participant(request.session_id, request.user_id, "user"):
        await db.add_session_participant(request.session_id, request.user_id, "user")
    if not await db.is_session_participant(request.session_id, agent["id"], "agent"):
        await db.add_session_participant(request.session_id, agent["id"], "agent")
    if str(request.user_id).startswith("anon_"):
        from app.agent.anonymous_data import enforce_anonymous_data_policy
        enforce_anonymous_data_policy(
            db, user_id=request.user_id, session_id=request.session_id, agent=agent)

    await _reset_agent_model_switcher(db, request.session_id, request.user_id)
    metadata = {
        "source": "controlled_chat",
        "cmid": rows[0]["client_message_id"],
        "chat_message_id": rows[0]["id"],
        "attachment_ids": request.attachment_ids,
    }
    interaction_id = await db.insert_interaction(
        request.user_id, request.session_id, role="user",
        content=request.message, channel="web_portal",
        metadata=json.dumps(metadata), sender_id=request.user_id,
        receiver_id=agent["id"],
    )
    await _record_session_execution_mode(db, request.session_id, request.execution_mode)
    return agent, interaction_id, first


async def execute_controlled_turn(db, request, agent, interaction_id, *, first):
    from app.chat.execution import run_shared_agent_turn
    await run_shared_agent_turn(
        db, request, agent, interaction_id, "web_portal", is_first_turn=first)
