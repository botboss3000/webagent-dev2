"""Read-only answers for the ephemeral conversational voice surface."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


logger = logging.getLogger(__name__)

_STATUS_TERMS = (
    "status", "progress", "what are you doing", "what is it doing",
    "what's happening", "what is happening", "how is it going",
    "how far", "is it done", "are you done", "is it finished",
    "still running", "still working", "working on", "taking so long",
)


@dataclass(frozen=True)
class VoiceReply:
    text: str
    source: str
    issue: str = ""


class VoiceCoordinatorUnavailable(RuntimeError):
    """A configured voice coordinator could not classify the message."""


_MODEL_INTENTS = {
    "conversation": "conversation_question",
    "conversation_question": "conversation_question",
    "question": "conversation_question",
    "new_work": "new_work",
    "work": "new_work",
    "modify_active_work": "modify_active_work",
    "modify_work": "modify_active_work",
    "uncertain": "uncertain",
}


def _openai_client(config: Dict[str, Any]):
    model = str(config.get("model") or "").strip()
    base_url = str(config.get("base_url") or "").rstrip("/")
    api_key = str(config.get("api_key") or "")
    if not model or not base_url or not api_key:
        raise VoiceCoordinatorUnavailable(
            "the configured Voice model is missing a model, base URL, or credential"
        )
    try:
        from openai import AsyncOpenAI
    except ImportError:  # pragma: no cover - compatibility environment
        from app.openai_compat import AsyncOpenAI
    return AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=25.0), model


def _parse_model_intent(value: str):
    from app.agent.voice_intent import VoiceIntent

    normalized = re.sub(r"[^a-z_]+", " ", str(value or "").lower()).strip()
    for token in normalized.split():
        mapped = _MODEL_INTENTS.get(token)
        if mapped:
            return VoiceIntent(mapped)
    raise VoiceCoordinatorUnavailable("the Voice model returned an invalid intent")


async def classify_natural_voice_intent(
    utterance: str,
    *,
    active_run: bool,
    config: Dict[str, Any],
):
    """Use the configured Voice model only for natural-language intent."""

    client, model = _openai_client(config)
    prompt = (
        "Classify this spoken message for a parallel voice coordinator. "
        "Return exactly one label: conversation_question, new_work, "
        "modify_active_work, or uncertain. conversation_question includes "
        "questions, greetings, discussion, and requests for explanation that "
        "do not ask the work agent to act. new_work asks the work agent to do "
        "something. modify_active_work changes ongoing work. uncertain is only "
        "for genuinely unintelligible or incomplete messages. "
        f"An agent run is {'active' if active_run else 'not active'}.\n\n"
        f"Spoken message: {utterance.strip()}"
    )
    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You classify voice intent. Return one allowed label "
                        "and no explanation. Never execute or answer the message."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0,
            max_tokens=16,
        )
        content = str(response.choices[0].message.content or "")
        return _parse_model_intent(content)
    except VoiceCoordinatorUnavailable:
        raise
    except Exception as exc:
        logger.warning("voice intent classifier failed: %s", exc)
        raise VoiceCoordinatorUnavailable(
            "the configured Voice model did not respond"
        ) from exc




def is_status_question(question: str) -> bool:
    text = " ".join(str(question or "").lower().split())
    return any(term in text for term in _STATUS_TERMS)


def _current_operation(row: Optional[Dict[str, Any]]) -> str:
    if not row:
        return ""
    op = row.get("current_op")
    if isinstance(op, str):
        try:
            op = json.loads(op)
        except Exception:
            op = None
    if not isinstance(op, dict):
        return ""
    note = str(op.get("note") or "").strip()
    tool = str(op.get("tool") or "").strip()
    if note.lower().startswith("toolcall "):
        note = "using " + note[9:].strip()
    return note or (("using " + tool) if tool else "")


def describe_run_state(row: Optional[Dict[str, Any]]) -> str:
    if not row:
        return "There is no run for this session yet."
    status = str(row.get("status") or "").lower()
    operation = _current_operation(row)
    if status == "running":
        if operation:
            return f"The run is active and is currently {operation}."
        return "The run is active and still working."
    if status == "queued":
        return "The run is queued and will start when the current work slot is available."
    if status == "complete":
        return "The most recent run is complete."
    if status == "interrupted":
        return "The most recent run was stopped before it completed."
    if status == "error":
        return "The most recent run ended with an error. The details are available in the chat."
    return f"The current run status is {status or 'unknown'}."


def _text_transcript(history: List[Dict[str, Any]], limit: int = 12) -> str:
    if limit <= 0:
        return ""
    lines: List[str] = []
    for message in history[-limit:]:
        role = str(message.get("role") or "")
        content = message.get("content")
        if role not in {"user", "assistant"} or not isinstance(content, str):
            continue
        clean = " ".join(content.split()).strip()
        if clean:
            lines.append(f"{role.title()}: {clean[:1200]}")
    return "\n".join(lines)


def _event_transcript(events: List[Dict[str, Any]], limit: int = 8) -> str:
    if limit <= 0:
        return ""
    lines: List[str] = []
    for event in events[-limit:]:
        event_type = str(event.get("type") or "event")
        detail = str(
            event.get("message") or event.get("tool") or event.get("status") or ""
        ).strip()
        if detail:
            lines.append(f"{event_type}: {detail[:360]}")
        else:
            lines.append(event_type)
    return "\n".join(lines)


async def answer_voice_question_result(
    db: Any,
    user_id: str,
    session_id: str,
    question: str,
    *,
    voice_session: Optional[Dict[str, Any]] = None,
    llm_config: Optional[Dict[str, Any]] = None,
) -> VoiceReply:
    """Answer without tools, loop execution, streaming, or interaction writes."""
    run = None
    try:
        run = await db.run_state_get(session_id)
    except Exception:
        pass
    status_text = describe_run_state(run)
    from app.agent.voice_config import normalize_voice_conversation
    conversation_config = normalize_voice_conversation(
        (voice_session or {}).get("conversation_config")
    )
    if is_status_question(question):
        return VoiceReply(status_text, "system")

    history: List[Dict[str, Any]] = []
    if voice_session and conversation_config["include_durable_chat"]:
        history = list(voice_session.get("durable_context") or [])
    if not history and conversation_config["include_durable_chat"]:
        try:
            from app.agent.session_history import build_openai_history_from_session
            history = await build_openai_history_from_session(db, user_id, session_id)
        except Exception as exc:
            logger.debug("voice coordinator history unavailable: %s", exc)

    try:
        if llm_config is None:
            from app.admin.settings import _resolve_user_config
            llm_config = await _resolve_user_config(user_id, role="voice")
        client, model = _openai_client(llm_config)
        transcript = _text_transcript(
            history, limit=conversation_config["max_durable_messages"])
        voice_summary = (
            str((voice_session or {}).get("conversation_summary") or "").strip()
            if conversation_config["continuous_context"] else ""
        )
        agent_events = (
            _event_transcript(
                list((voice_session or {}).get("recent_agent_events") or []),
                limit=conversation_config["max_agent_events"],
            )
            if conversation_config["include_agent_events"] else ""
        )
        response_length = conversation_config["response_length"]
        length_rule, token_limit = {
            "brief": ("Answer in one or two short spoken sentences.", 180),
            "normal": ("Answer in no more than four spoken sentences.", 320),
            "detailed": (
                "Give a concise spoken explanation of at most two short paragraphs.", 500,
            ),
        }[response_length]
        custom_guidance = conversation_config["response_instructions"]
        prompt = (
            f"Current run state: {status_text}\n\n"
            + (f"Recent durable chat:\n{transcript}\n\n" if transcript else "")
            + (f"Current ephemeral voice exchange:\n{voice_summary}\n\n" if voice_summary else "")
            + (f"Recent agent events:\n{agent_events}\n\n" if agent_events else "")
            + f"Ephemeral voice message: {question.strip()}"
        )
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a fast, read-only voice coordinator for an agent run. "
                        f"{length_rule} Use only the supplied "
                        "run state and durable chat. Never claim to have changed, stopped, "
                        "started, or completed work. Do not call tools. If the question asks "
                        "for a change, say it needs to be sent to the work agent."
                        + (f" Additional response guidance: {custom_guidance}" if custom_guidance else "")
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
            max_tokens=token_limit,
        )
        answer = str(response.choices[0].message.content or "").strip()
        try:
            usage = getattr(response, "usage", None)
            if usage:
                from plugins.billing.usage import record_background_usage
                await record_background_usage(
                    model=model,
                    input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                    output_tokens=getattr(usage, "completion_tokens", 0) or 0,
                    label="voice-coordinator",
                )
        except Exception:
            pass
        if answer:
            return VoiceReply(answer, "model")
        return VoiceReply(
            "Voice answering failed because the configured Voice model returned no response.",
            "system",
            "answer_empty",
        )
    except VoiceCoordinatorUnavailable as exc:
        return VoiceReply(
            f"Voice answering is unavailable because {exc}.",
            "system",
            "answer_model_unavailable",
        )
    except Exception as exc:
        logger.warning("voice coordinator call failed: %s", exc)
        return VoiceReply(
            "Voice answering failed because the configured Voice model did not respond.",
            "system",
            "answer_model_failed",
        )


async def answer_voice_question(*args, **kwargs) -> str:
    """Compatibility wrapper for callers that only need spoken text."""
    return (await answer_voice_question_result(*args, **kwargs)).text
