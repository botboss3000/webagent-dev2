"""
Shared message processing for communication channels.

Used by both the webhook handler (incoming from Telegram/WhatsApp) and
the polling loop (plugin pulls messages from Telegram API).
Avoids duplicating the agent loop logic.
"""

import json
import logging

# Display names for each channel -- shown as the session title in the UI
CHANNEL_DISPLAY_NAMES: dict[str, str] = {
    "telegram": "Telegram",
    "whatsapp": "WhatsApp",
    "sms": "SMS",
}


from app.communications.auth import (
    get_or_create_identity,
    get_registration_system_prompt,
    get_anonymous_limit_prompt,
    upgrade_to_verified,
    verify_code,
    ChannelIdentity,
)
from app.db import get_db
from app.agent.loop import run_agent_loop_buffered
from app.agent.prompts import build_system_prompt, CONTEXT_SECTION_TYPES
from app.agent.session_history import build_openai_history_from_session

logger = logging.getLogger(__name__)


async def _emit_channel_message_event(
    channel: str,
    external_id: str,
    message_text: str,
    user_id: str,
) -> None:
    """Normalize an inbound channel message and hand it to the event router.

    No-op if the event subsystem is unavailable. Each channel is also a
    registered event source (telegram/slack/discord/sms/whatsapp) — the
    router only fires if a subscription exists for this owner + channel,
    so this is cheap when nothing is listening.
    """
    try:
        from app.events.types import NormalizedEvent
        from app.events.router import route_event
    except Exception:
        return

    payload = {
        "channel": channel,
        "chat_id": external_id,
        "from_number": external_id if channel in ("sms", "whatsapp") else None,
        "text": message_text or "",
    }
    payload = {k: v for k, v in payload.items() if v is not None}

    # event_type "message_received" is the shared shape across all comms channels.
    import hashlib
    from datetime import datetime, timezone
    digest = hashlib.sha256(f"{channel}|{external_id}|{message_text or ''}".encode("utf-8")).hexdigest()[:16]
    evt = NormalizedEvent(
        source=channel,
        event_type="message_received",
        owner_user_id=user_id,
        external_id=f"{channel}:{external_id}:{digest}",
        occurred_at=datetime.now(timezone.utc).isoformat(),
        payload=payload,
        raw_ref={"channel": channel, "external_id": external_id},
    )

    try:
        await route_event(evt)
    except Exception as e:
        logger.debug("route_event from %s emit failed: %s", channel, e)


async def process_channel_message(
    channel: str,
    external_id: str,
    message_text: str,
    plugin,
    agent_id: str | None = None,
) -> str:
    """
    Process an incoming message from a communication channel.

    This is the shared entry point for both webhook and polling paths.
    It handles identity resolution, session creation, and running
    the agent loop (registration or normal).

    Args:
        channel: Plugin name, e.g. 'telegram'
        external_id: Channel-specific user ID, e.g. chat_id
        message_text: Raw message text
        plugin: The CommunicationPlugin instance (for sending replies)
        agent_id: Optional agent ID if the channel connection is per-agent

    Returns:
        The reply text that was sent to the user
    """
    # 1. Get or create identity
    identity = await get_or_create_identity(channel, external_id)
    user_id = identity.user_id

    # 1a. Fan out to any event subscriptions registered against this channel.
    # Fire-and-forget so a slow event subscriber never blocks the chat reply.
    try:
        await _emit_channel_message_event(channel, external_id, message_text, user_id)
    except Exception as _evt_err:
        logger.debug("Channel event emit failed (non-fatal): %s", _evt_err)

    # 2. Ensure a session exists
    db = get_db()
    session_id = user_id
    try:
        await db.assert_session_owned(user_id, session_id)
    except PermissionError:
        try:
            raw = db.get_raw_client()
            session_row = {
                "id": session_id,
                "user_id": user_id,
                "title": CHANNEL_DISPLAY_NAMES.get(channel, channel.capitalize()),
            }
            # Attach the owning agent when known so the session is bound from the start.
            if agent_id:
                session_row["agent_id"] = agent_id
            raw.table("sessions").insert(session_row).execute()
            logger.info("Created session %s for channel user %s (agent %s)",
                        session_id, user_id, agent_id)
        except Exception as sess_err:
            # Another concurrent request may have created the session already;
            # verify ownership rather than hard-failing.
            logger.warning("Session insert failed (%s) -- checking if it already exists", sess_err)
            await db.assert_session_owned(user_id, session_id)

    # 3. Route based on agent connection config.
    if agent_id:
        await db.add_agent_member(agent_id, user_id)

        user_mode = "anonymous"
        try:
            agent_row = await db.get_agent_by_id(agent_id)
            if agent_row:
                user_mode = agent_row.get("user_mode", "anonymous")
        except Exception as cfg_err:
            logger.warning("Could not read user_mode for agent %s: %s", agent_id, cfg_err)

        if user_mode == "register" and identity.user_tier == "anonymous":
            if message_text.strip().isdigit() and len(message_text.strip()) == 6:
                is_valid = await verify_code(channel, external_id, message_text.strip())
                if is_valid:
                    await upgrade_to_verified(identity)
                    reply = "You're verified! You now have full access. How can I help you?"
                    await plugin.send_message(external_id, reply)
                    return reply
            reply = await _run_registration_agent(plugin, identity, user_id, message_text, channel, agent_id=agent_id)
        else:
            reply = await _run_agent_loop(plugin, identity, user_id, message_text, channel, agent_id=agent_id)
    else:
        # Legacy path: no specific agent known -- fall back to tier-based routing.
        if identity.user_tier == "anonymous":
            if message_text.strip().isdigit() and len(message_text.strip()) == 6:
                is_valid = await verify_code(channel, external_id, message_text.strip())
                if is_valid:
                    await upgrade_to_verified(identity)
                    reply = "You're verified! You now have full access. How can I help you?"
                    await plugin.send_message(external_id, reply)
                    return reply
            reply = await _run_registration_agent(plugin, identity, user_id, message_text, channel)
        else:
            reply = await _run_agent_loop(plugin, identity, user_id, message_text, channel)

    # 4. Send reply
    await plugin.send_message(external_id, reply)
    return reply


async def _run_registration_agent(
    plugin, identity: ChannelIdentity, user_id: str, message_text: str, channel: str,
    agent_id: str | None = None,
) -> str:
    """Run the agent loop with a registration-focused system prompt."""
    try:
        db = get_db()
        session_id = user_id

        await db.insert_interaction(
            user_id, session_id, role="user", content=message_text,
            channel=channel,
            metadata=json.dumps({"source": f"{channel}/registration"}),
            sender_id=user_id,
            receiver_id=agent_id,
        )

        # If we know which agent owns this channel, load it directly.
        # Otherwise look up the user's agent. We no longer auto-create one —
        # the user must set up an agent via the web UI before they can chat.
        if agent_id:
            agent = await db.get_agent_by_id(agent_id)
            agent_with_ctx = await db.fetch_agent_by_id_with_context(agent_id, CONTEXT_SECTION_TYPES, user_id=user_id)
            if agent_with_ctx:
                agent = agent_with_ctx
        else:
            agent = await db.get_agent_for_user(user_id)
            if agent is None:
                logger.info(
                    "Registration agent: user %s has no agent assigned; skipping run",
                    user_id,
                )
                return "No agent is set up for your account yet. Please create one in the web app before chatting."
            agent_with_ctx = await db.fetch_agent_with_context(user_id, CONTEXT_SECTION_TYPES)
            if agent_with_ctx:
                agent = agent_with_ctx

        if not agent.get("context_documents"):
            copied = await db.copy_defaults_to_agent(agent["id"])
            if copied > 0:
                if agent_id:
                    agent = await db.fetch_agent_by_id_with_context(agent_id, CONTEXT_SECTION_TYPES, user_id=user_id)
                else:
                    agent = await db.fetch_agent_with_context(user_id, CONTEXT_SECTION_TYPES)

        context_docs = agent.get("context_documents", [])

        registration_prompt = get_registration_system_prompt(identity)

        # Include channel context so the agent knows how to reach this user.
        if channel and identity.external_id:
            tool_name = f"send_{channel}_message"
            registration_prompt += (
                f"\n\n[Channel context: This conversation is happening over {channel}. "
                f"The user's {channel} ID is {identity.external_id}. "
                f"To send them a message use `{tool_name}` with chat_id={identity.external_id}.]"
            )

        # Registration prompt is a transient overlay — prepend it to the resolved slots.
        ctx_with_reg = [{"id": "_registration", "content": registration_prompt}] + context_docs
        system_prompt = await build_system_prompt(
            ctx_with_reg, brain_context=None, user_id=user_id, session_id=session_id,
        )

        history = await build_openai_history_from_session(
            db, user_id, session_id, exclude_interaction_ids=None,
        )

        reply = await run_agent_loop_buffered(
            user_id=user_id,
            session_id=session_id,
            user_message=message_text,
            system_prompt=system_prompt,
            agent_id=agent.get("id", ""),
            history=history,
            max_turns=5,
            channel=channel,
        )

        return reply
    except Exception as e:
        logger.error("Registration agent error for %s: %s", user_id, e, exc_info=True)
        return "Sorry, something went wrong. Please try again."


async def _run_agent_loop(
    plugin, identity: ChannelIdentity, user_id: str, message_text: str, channel: str,
    agent_id: str | None = None,
) -> str:
    """Run the normal agent loop for a registered user."""
    try:
        db = get_db()
        session_id = user_id

        _turn_uid = await db.insert_interaction(
            user_id, session_id, role="user", content=message_text,
            channel=channel,
            metadata=json.dumps({"source": f"{channel}/message"}),
            sender_id=user_id,
            receiver_id=agent_id,
        )

        # If we know which agent owns this channel, load it directly.
        # Otherwise look up the user's agent. We no longer auto-create one —
        # the user must set up an agent via the web UI before they can chat.
        if agent_id:
            agent = await db.get_agent_by_id(agent_id)
            agent_with_ctx = await db.fetch_agent_by_id_with_context(agent_id, CONTEXT_SECTION_TYPES, user_id=user_id)
            if agent_with_ctx:
                agent = agent_with_ctx
        else:
            agent = await db.get_agent_for_user(user_id)
            if agent is None:
                logger.info(
                    "Agent loop: user %s has no agent assigned; skipping run",
                    user_id,
                )
                return "No agent is set up for your account yet. Please create one in the web app before chatting."
            agent_with_ctx = await db.fetch_agent_with_context(user_id, CONTEXT_SECTION_TYPES)
            if agent_with_ctx:
                agent = agent_with_ctx

        if not agent.get("context_documents"):
            copied = await db.copy_defaults_to_agent(agent["id"])
            if copied > 0:
                if agent_id:
                    agent = await db.fetch_agent_by_id_with_context(agent_id, CONTEXT_SECTION_TYPES, user_id=user_id)
                else:
                    agent = await db.fetch_agent_with_context(user_id, CONTEXT_SECTION_TYPES)

        context_docs = agent.get("context_documents", [])

        brain_results = await db.memory_search(user_id, message_text, limit=5)
        brain_context = None
        if brain_results:
            lines = []
            for r in brain_results:
                slug = r.get("slug", "?")
                title = r.get("title", slug)
                ct = r.get("compiled_truth", "")[:300]
                rank = r.get("rank", 0)
                lines.append(f"[{slug}] {title} (score: {rank:.2f})")
                if ct:
                    lines.append(ct)
                lines.append("")
            brain_context = "\n".join(lines)

        # Channel + anonymity overlay (transient — prepended as a synthetic doc).
        overlay_parts: list[str] = []
        if identity.user_tier == "anonymous":
            overlay_parts.append(get_anonymous_limit_prompt())
        if channel and identity.external_id:
            tool_name = f"send_{channel}_message"
            overlay_parts.append(
                f"[Channel context: This conversation is happening over {channel}. "
                f"The user's {channel} ID is {identity.external_id}. "
                f"To proactively send them a message use the `{tool_name}` tool "
                f"with chat_id={identity.external_id}.]"
            )
        if overlay_parts:
            context_docs = [{"id": "_channel_overlay", "content": "\n\n".join(overlay_parts)}] + context_docs

        system_prompt = await build_system_prompt(
            context_docs, brain_context, user_id, session_id=session_id,
        )

        history = await build_openai_history_from_session(
            db, user_id, session_id, exclude_interaction_ids=None,
        )

        # Run supervised + recorded so an inbound run survives a restart/freeze.
        # No delivery block in the relaunch recipe: on resume the agent re-sends
        # via its own send_{channel}_message tool (per the channel overlay above),
        # which avoids a double-send.
        from app.agent.runner import run_supervised_turn, RunOutcome
        _inbound_agent_id = agent.get("id", "")
        _relaunch_ctx = {
            "origin": "inbound", "session_id": session_id, "user_id": user_id,
            "agent_id": _inbound_agent_id, "channel": channel, "timeout_seconds": 600,
        }

        async def _build_inbound_turn(replaced: bool) -> RunOutcome:
            _reply = await run_agent_loop_buffered(
                user_id=user_id,
                session_id=session_id,
                user_message=message_text,
                system_prompt=system_prompt,
                agent_id=_inbound_agent_id,
                history=history,
                max_turns=agent.get("max_turn_count", 0),
                channel=channel,
            )
            return RunOutcome(status="complete", stop_cause="complete", reply=_reply)

        _outcome = await run_supervised_turn(
            session_id=session_id, user_id=user_id, agent_id=_inbound_agent_id,
            origin="inbound", channel=channel, turn_id=_turn_uid,
            relaunch_ctx=_relaunch_ctx, build_turn=_build_inbound_turn,
            await_result=True, result_timeout=620,
        )
        reply = (_outcome.reply if _outcome else "") or ""

        return reply
    except Exception as e:
        logger.error("Agent loop error for %s: %s", user_id, e, exc_info=True)
        return "Sorry, I encountered an error. Please try again."
