"""Normalized per-agent settings for ephemeral voice conversations."""

from __future__ import annotations

import json
from typing import Any, Dict, Optional


DEFAULT_VOICE_CONVERSATION: Dict[str, Any] = {
    "continuous_context": True,
    "include_durable_chat": True,
    "include_agent_events": True,
    "session_timeout_minutes": 15,
    "max_voice_turns": 12,
    "max_durable_messages": 12,
    "max_agent_events": 8,
    "response_length": "brief",
    "response_instructions": "",
}


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def normalize_voice_conversation(
    value: Any,
    current: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return the closed, bounded Voice configuration contract.

    ``value`` is treated as a partial update over ``current``. Unknown keys are
    intentionally discarded so agent metadata cannot silently invent runtime
    behavior that the Voice coordinator does not implement.
    """

    merged = dict(DEFAULT_VOICE_CONVERSATION)
    if isinstance(current, dict):
        merged.update(current)
    if isinstance(value, dict):
        merged.update(value)

    response_length = str(merged.get("response_length") or "brief").strip().lower()
    if response_length not in {"brief", "normal", "detailed"}:
        response_length = "brief"

    instructions = str(merged.get("response_instructions") or "").strip()
    return {
        "continuous_context": bool(merged.get("continuous_context", True)),
        "include_durable_chat": bool(merged.get("include_durable_chat", True)),
        "include_agent_events": bool(merged.get("include_agent_events", True)),
        "session_timeout_minutes": _bounded_int(
            merged.get("session_timeout_minutes"), 15, 1, 60,
        ),
        "max_voice_turns": _bounded_int(merged.get("max_voice_turns"), 12, 2, 40),
        "max_durable_messages": _bounded_int(
            merged.get("max_durable_messages"), 12, 0, 40,
        ),
        "max_agent_events": _bounded_int(merged.get("max_agent_events"), 8, 0, 30),
        "response_length": response_length,
        "response_instructions": instructions[:2000],
    }


def voice_conversation_for_agent(agent: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    metadata: Any = (agent or {}).get("metadata")
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
            if isinstance(metadata, str):
                metadata = json.loads(metadata)
        except Exception:
            metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    return normalize_voice_conversation(metadata.get("voice_conversation"))


async def voice_conversation_for_session(
    db: Any,
    session_id: str,
) -> Dict[str, Any]:
    """Resolve Voice settings from the agent bound to a chat session."""

    try:
        agent_id = await db.get_session_agent_id(session_id)
        if not agent_id:
            return dict(DEFAULT_VOICE_CONVERSATION)
        agent = await db.get_agent_by_id(agent_id)
        return voice_conversation_for_agent(agent)
    except Exception:
        return dict(DEFAULT_VOICE_CONVERSATION)
