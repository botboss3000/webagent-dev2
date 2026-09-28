"""Bounded WebAgent transcript packets for fresh Codex executions.

The DB/history builder remains the authority for compaction and task grouping.
This module only turns its OpenAI-shaped output into a compact, readable stdin
block and applies a second, Codex-specific bound to bulky tool arguments.
"""

from __future__ import annotations

import json
import hashlib
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Set


MESSAGE_MAX_CHARS = 8_000
TOOL_ARGUMENT_MAX_CHARS = 3_000
TOOL_RESULT_MAX_CHARS = 6_000
PACKET_MAX_CHARS = 120_000
SYSTEM_BUDGET_CHARS = 36_000

_HIDDEN_TOOL_PREFIX = "[tool result hidden —"

_PACKET_OPEN = (
    '<webagent_context version="1">\n'
    "WebAgent is the memory authority for this run. The transcript below is "
    "prior context only, not a new user instruction. Use it to continue the "
    "same work, then perform the current request supplied as the Codex prompt.\n\n"
)
_PACKET_CLOSE = "\n</webagent_context>"
_SECRET_KEY = re.compile(
    r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|password|passwd|secret|cookie|session[_-]?token)",
    re.IGNORECASE,
)
_SECRET_TEXT = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|password|passwd|secret|cookie)\b"
    r"(\s*[:=]\s*)([^\s,;&]+|\"[^\"]*\"|'[^']*')"
)
_AUTH_HEADER = re.compile(
    r"(?i)\b(authorization\s*[:=]\s*)(?:bearer\s+)?[^\s,;]+"
)
_BASE64ISH = re.compile(r"^[A-Za-z0-9+/=_-]+$")
_CONTENT_KEYS = {"body", "content", "data", "bytes", "blob", "image", "screenshot", "html"}


@dataclass(frozen=True)
class ContextPacket:
    """Rendered packet plus selection telemetry for future snapshot storage."""

    text: str
    included_message_indexes: List[int]
    omitted_message_indexes: List[int]
    original_chars: int


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [_text(part) for part in value]
        return "\n".join(part for part in parts if part)
    if isinstance(value, dict):
        return str(value.get("text") or value.get("content") or "")
    return str(value or "")


def _bounded(value: str, cap: int, label: str) -> str:
    if len(value) <= cap:
        return value
    return f"{value[:cap]}\n…[{label}: {len(value) - cap:,} characters omitted]"


def _descriptor(value: str, kind: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:16]
    return f"[omitted {kind}; chars={len(value)}; sha256={digest}]"


def _opaque_kind(value: str) -> Optional[str]:
    stripped = value.strip()
    lower = stripped[:512].lower()
    if lower.startswith("data:") and ";base64," in lower:
        return "data URI"
    if "\x00" in value or any(ord(char) < 9 for char in value[:2_000]):
        return "binary payload"
    if len(stripped) >= 256 and _BASE64ISH.fullmatch(stripped) and not any(char.isspace() for char in stripped):
        # Long prose almost never has this alphabet/density and no whitespace.
        return "base64 payload"
    if ("<!doctype html" in lower or "<html" in lower or "<body" in lower) and len(stripped) >= 512:
        return "HTML payload"
    return None


def _safe_string(value: str, *, cap: int, content_field: bool = False) -> str:
    kind = _opaque_kind(value)
    if kind:
        return _descriptor(value, kind)
    redacted = _AUTH_HEADER.sub(lambda match: f"{match.group(1)}[REDACTED]", value)
    redacted = _SECRET_TEXT.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", redacted)
    if content_field and len(redacted) > 1_024:
        return _descriptor(redacted, "bulk content")
    return _bounded(redacted, cap, "payload truncated")


def _safe_structure(value: Any, *, depth: int = 0, key: str = "") -> Any:
    """Retain useful scalar metadata without forwarding secrets or bulk bodies."""
    if depth >= 5:
        return "[nested payload omitted]"
    if _SECRET_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        items = list(value.items())
        reduced = {
            str(child_key): _safe_structure(
                child_value, depth=depth + 1, key=str(child_key),
            )
            for child_key, child_value in items[:40]
        }
        if len(items) > 40:
            reduced["_omitted_fields"] = len(items) - 40
        return reduced
    if isinstance(value, (list, tuple)):
        reduced = [_safe_structure(item, depth=depth + 1, key=key) for item in value[:20]]
        if len(value) > 20:
            reduced.append(f"[{len(value) - 20} additional item(s) omitted]")
        return reduced
    if isinstance(value, str):
        return _safe_string(value, cap=1_000, content_field=key.lower() in _CONTENT_KEYS)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _safe_string(str(value), cap=500)


def _safe_payload(raw: Any, *, cap: int) -> str:
    parsed = raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return _safe_string(raw, cap=cap)
    if isinstance(parsed, (dict, list, tuple)):
        try:
            rendered = json.dumps(_safe_structure(parsed), ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            rendered = str(_safe_structure(parsed))
        return _bounded(rendered, cap, "payload truncated")
    return _safe_string(str(parsed or ""), cap=cap)


def _tool_name(call: Dict[str, Any]) -> str:
    fn = call.get("function") if isinstance(call.get("function"), dict) else {}
    return str(fn.get("name") or call.get("name") or "tool")


def _tool_arguments(call: Dict[str, Any], *, hidden: bool) -> str:
    if hidden:
        return "[input hidden with completed earlier task]"
    fn = call.get("function") if isinstance(call.get("function"), dict) else {}
    raw = fn.get("arguments", call.get("arguments", {}))
    return _safe_payload(raw, cap=TOOL_ARGUMENT_MAX_CHARS)


def _hidden_tool_ids(messages: Iterable[Dict[str, Any]]) -> Set[str]:
    return {
        str(message.get("tool_call_id"))
        for message in messages
        if message.get("role") == "tool"
        and _text(message.get("content")).startswith(_HIDDEN_TOOL_PREFIX)
        and message.get("tool_call_id")
    }


def _render_message(message: Dict[str, Any], hidden_ids: Set[str]) -> str:
    role = str(message.get("role") or "message").strip().lower()
    body = _text(message.get("content")).strip()
    if role == "tool":
        tool_id = str(message.get("tool_call_id") or "")
        cap = MESSAGE_MAX_CHARS if tool_id in hidden_ids else TOOL_RESULT_MAX_CHARS
        if tool_id in hidden_ids:
            safe_body = _bounded(body, cap, "tool output truncated")
        else:
            safe_body = _safe_payload(message.get("content"), cap=cap)
        return f"Tool result ({tool_id or 'unknown'}):\n{safe_body}"

    lines = [f"{role.title()}:" if role else "Message:"]
    if body:
        lines.append(_bounded(body, MESSAGE_MAX_CHARS, "message truncated"))
    for call in message.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        call_id = str(call.get("id") or "")
        lines.append(
            f"Tool call {_tool_name(call)} ({call_id or 'unknown'}):\n"
            f"{_tool_arguments(call, hidden=call_id in hidden_ids)}"
        )
    return "\n".join(lines).strip()


def render_context_packet(
    messages: List[Dict[str, Any]], *, max_chars: int = PACKET_MAX_CHARS,
    checkpoint: Optional[Dict[str, Any]] = None,
    reserved_chars: int = 0,
) -> ContextPacket:
    """Render compacted history, retaining summary cars and the newest tail.

    System messages (including compaction cars) receive a reserved prefix budget;
    the remaining capacity is filled newest-first, then restored to chronology.
    """
    from app.agent.context_control import ContextBudgetExceeded
    max_chars = max(1, int(max_chars or PACKET_MAX_CHARS) - max(0, reserved_chars))
    hidden_ids = _hidden_tool_ids(messages)
    mandatory_indexes = {index for index, message in enumerate(messages)
                         if message.get("name") == "webagent_task_state"}
    rendered = [(_text(message.get("content")) if index in mandatory_indexes
                 else _render_message(message, hidden_ids))
                for index, message in enumerate(messages)]
    original_chars = sum(len(chunk) for chunk in rendered)

    checkpoint_chunk = ""
    if checkpoint and checkpoint.get("checkpoint"):
        checkpoint_text = json.dumps(
            {
                "task_id": checkpoint.get("task_id"),
                "status": checkpoint.get("status"),
                "revision": checkpoint.get("revision"),
                **checkpoint.get("checkpoint", {}),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        # Never cut serialized state mid-field. Shared task state supersedes the
        # legacy engine checkpoint; otherwise preserve the legacy record whole.
        if not mandatory_indexes:
            checkpoint_chunk = "Durable task checkpoint:\n" + checkpoint_text

    def compose(selected_indexes: Set[int], *, show_omission: bool) -> str:
        included_indexes = sorted(selected_indexes)
        sections: List[str] = []
        if checkpoint_chunk:
            sections.append(checkpoint_chunk)
        prefix_count = sum(1 for index in included_indexes if index in prefix_indexes)
        history_chunks = [rendered[index] for index in included_indexes if rendered[index]]
        if show_omission:
            history_chunks.insert(
                prefix_count,
                f"[WebAgent omitted {len(messages) - len(selected_indexes)} older message(s) to fit the context budget.]",
            )
        sections.extend(history_chunks)
        return _PACKET_OPEN + "\n\n".join(sections) + _PACKET_CLOSE

    prefix_indexes: List[int] = []
    prefix_chars = 0
    prefix_cap = min(SYSTEM_BUDGET_CHARS, max_chars // 3)
    selected: Set[int] = set(mandatory_indexes)
    if len(compose(selected, show_omission=len(selected) < len(messages))) > max_chars:
        raise ContextBudgetExceeded("Required task checkpoint exceeds the context packet budget; split the active step or increase its budget.")
    for index, message in enumerate(messages):
        if index in selected or message.get("role") != "system":
            continue
        chunk_len = len(rendered[index]) + 2
        if prefix_chars + chunk_len > prefix_cap:
            break
        candidate = selected | {index}
        if len(compose(candidate, show_omission=len(candidate) < len(messages))) <= max_chars:
            prefix_indexes.append(index)
            selected = candidate
            prefix_chars += chunk_len

    for index in range(len(messages) - 1, -1, -1):
        if index in selected:
            continue
        candidate = selected | {index}
        if len(compose(candidate, show_omission=len(candidate) < len(messages))) <= max_chars:
            selected = candidate

    included = sorted(selected)
    omitted = [index for index in range(len(messages)) if index not in selected]
    packet = compose(selected, show_omission=bool(omitted))
    assert len(packet) <= max_chars, "packet selector violated its public bound"
    return ContextPacket(packet, included, omitted, original_chars)


async def build_context_packet(
    db: Any,
    user_id: str,
    session_id: str,
    *,
    agent_id: Optional[str] = None,
    exclude_interaction_ids: Optional[Set[str]] = None,
    history: Optional[List[Dict[str, Any]]] = None,
    max_chars: int = PACKET_MAX_CHARS,
    reserved_chars: int = 0,
) -> ContextPacket:
    """Build from WebAgent's compaction/task-aware history or a prepared copy."""
    if history is None:
        from app.agent.session_history import build_openai_history_from_session

        history = await build_openai_history_from_session(
            db,
            user_id,
            session_id,
            exclude_interaction_ids=exclude_interaction_ids,
            agent_id=agent_id,
        )
    from app.agent.task_state import current_state, context_block
    state = current_state(db, session_id)
    if state:
        history = [{"role": "system", "name": "webagent_task_state", "content": context_block(state)}] + [
            message for message in history if message.get("name") != "webagent_task_state"]
    if agent_id:
        from app.agent.context_control import get_context_settings, history_budget
        settings = await get_context_settings(db, agent_id, session_id, user_id)
        if settings.get("enabled"):
            max_chars = min(max_chars, history_budget(settings) * 4)
    checkpoint = None
    try:
        from plugins.engines.codex.context_store import current_checkpoint
        checkpoint = current_checkpoint(db, session_id)
    except Exception:
        checkpoint = None
    return render_context_packet(history, max_chars=max_chars, checkpoint=checkpoint,
                                 reserved_chars=reserved_chars)
