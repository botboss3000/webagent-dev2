"""Normalization between Codex App Server tasks and WebAgent's chat view."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any


def thread_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    rows = result.get("data") or result.get("threads") or []
    return [normalize_thread(row) for row in rows]


def normalize_thread(row: dict[str, Any]) -> dict[str, Any]:
    thread_id = str(row.get("id") or row.get("threadId") or "")
    title = row.get("name") or row.get("title") or row.get("preview") or "Codex task"
    return {
        "thread_id": thread_id,
        "id": f"codex:{thread_id}",
        "title": str(title).strip()[:240] or "Codex task",
        "created_at": _timestamp(row.get("createdAt") or row.get("created_at")),
        "updated_at": _timestamp(row.get("updatedAt") or row.get("updated_at") or row.get("createdAt")),
        "cwd": row.get("cwd"),
        "status": row.get("status"),
    }


def messages_from_thread(result: dict[str, Any], thread_id: str) -> list[dict[str, Any]]:
    thread = result.get("thread") or result
    turns = thread.get("turns") or []
    messages: list[dict[str, Any]] = []
    seq = 0
    default_time = thread.get("updatedAt") or datetime.now(timezone.utc).isoformat()
    for turn in turns:
        turn_id = str(turn.get("id") or "")
        created = turn.get("startedAt") or turn.get("createdAt") or default_time
        for item in turn.get("items") or []:
            converted = _item_to_message(item, thread_id, turn_id, created)
            if not converted:
                continue
            seq += 1
            converted["session_seq"] = seq
            converted["turn_seq"] = seq
            messages.append(converted)
    return messages


def thread_metrics(result: dict[str, Any]) -> dict[str, Any]:
    """Aggregate transcript-local metrics when a full native task is open."""
    thread = result.get("thread") or result
    turns = thread.get("turns") or []
    message_count = 0
    duration_ms = 0
    for turn in turns:
        duration_ms += int(turn.get("durationMs") or 0)
        message_count += sum(
            1 for item in (turn.get("items") or [])
            if item.get("type") in {"userMessage", "agentMessage"}
        )
    return {"message_count": message_count, "total_duration_ms": duration_ms}


def _item_to_message(
    item: dict[str, Any], thread_id: str, turn_id: str, created_at: str
) -> dict[str, Any] | None:
    kind = str(item.get("type") or "")
    item_id = str(item.get("id") or f"{turn_id}:{kind}")
    metadata: dict[str, Any] = {"codex_item_type": kind, "phase": item.get("phase")}
    base = {
        "id": item_id,
        "session_id": f"codex:{thread_id}",
        "turn_id": turn_id or item_id,
        "created_at": _timestamp(item.get("createdAt") or created_at),
        "status": item.get("status") or "complete",
        "source": "codex:portal",
        "metadata": json.dumps(metadata, default=str),
    }
    if kind == "userMessage":
        return {**base, "role": "user", "content": _content_text(item)}
    if kind == "agentMessage":
        return {**base, "role": "assistant", "content": _content_text(item)}
    if kind in {"reasoning", "plan"}:
        return {**base, "role": "assistant", "content": _content_text(item), "message_type": "reasoning"}
    if kind in {"commandExecution", "fileChange", "mcpToolCall", "dynamicToolCall", "collabAgentToolCall", "collabToolCall", "webSearch", "imageGeneration", "imageView", "contextCompaction"}:
        name = {
            "commandExecution": "command",
            "fileChange": "file_change",
            "mcpToolCall": "mcp",
            "dynamicToolCall": "tool",
            "collabAgentToolCall": "collaboration",
            "collabToolCall": "collaboration",
            "webSearch": "web_search",
            "imageGeneration": "image_generation",
            "imageView": "image_view",
            "contextCompaction": "context_compaction",
        }[kind]
        output = item.get("aggregatedOutput") or item.get("output") or item.get("result") or ""
        content = _content_text(item) or str(
            item.get("command") or item.get("query") or item.get("tool")
            or item.get("path") or name
        )
        if kind == "fileChange":
            changes = item.get("changes") or []
            content = "\n".join(str(change.get("path") or "file") for change in changes) or "file_change"
            output = "\n\n".join(str(change.get("diff") or "") for change in changes if change.get("diff"))
        args = {
            key: item.get(key) for key in (
                "command", "cwd", "query", "server", "tool", "arguments", "input", "path"
            ) if item.get(key) is not None
        }
        metadata.update({
            "args": args,
            "duration_ms": item.get("durationMs"),
            "exit_code": item.get("exitCode"),
            "error": str(item.get("status") or "").lower() in {"failed", "error"},
        })
        return {
            **base,
            "role": "tool",
            "content": content,
            "tool_name": name,
            "tool_call_id": item_id,
            "output": output if isinstance(output, str) else json.dumps(output, default=str),
            "metadata": json.dumps(metadata, default=str),
        }
    return None


def _content_text(item: dict[str, Any]) -> str:
    direct = item.get("text") or item.get("message") or item.get("summary")
    if isinstance(direct, str):
        return direct
    if isinstance(direct, list):
        return "\n".join(str(value) for value in direct if value)
    content = item.get("content") or []
    if isinstance(content, str):
        return content
    chunks = []
    for part in content if isinstance(content, list) else []:
        if isinstance(part, str):
            chunks.append(part)
        elif isinstance(part, dict):
            text = part.get("text") or part.get("content")
            if text:
                chunks.append(str(text))
    return "\n".join(chunks)


def _timestamp(value: Any) -> Any:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
    return value
