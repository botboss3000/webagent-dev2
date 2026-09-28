"""Best-effort read-only analytics for native Codex Portal tasks.

The App Server remains authoritative for task discovery and transcripts.  Its
thread/list response intentionally contains no aggregate usage fields, while
Codex's own local projections already maintain those aggregates.  This module
reads only those projections to decorate Portal rows; failures simply leave a
stat unknown and never prevent the native catalog from loading.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable


_USAGE_CACHE: dict[str, tuple[int, int, dict[str, Any]]] = {}
_USAGE_CACHE_LOCK = threading.Lock()


def task_stats(thread_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    ids = [str(value).strip() for value in thread_ids if str(value).strip()]
    if not ids:
        return {}
    result = {thread_id: {} for thread_id in ids}
    root = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
    state_db = _newest_db(root, "state_*.sqlite")
    history_db = _newest_db(root, "thread_history_*.sqlite")

    rollout_paths: dict[str, str] = {}
    if state_db:
        try:
            with _read_only(state_db) as conn:
                for batch in _batches(ids):
                    marks = ",".join("?" for _ in batch)
                    rows = conn.execute(
                        f"SELECT id, rollout_path, tokens_used FROM threads WHERE id IN ({marks})",
                        batch,
                    ).fetchall()
                    for thread_id, rollout_path, total_tokens in rows:
                        rollout_paths[str(thread_id)] = str(rollout_path or "")
                        if total_tokens is not None:
                            result[str(thread_id)]["total_tokens"] = int(total_tokens)
        except (OSError, sqlite3.Error):
            pass

    if history_db:
        try:
            with _read_only(history_db) as conn:
                for batch in _batches(ids):
                    marks = ",".join("?" for _ in batch)
                    for thread_id, message_count in conn.execute(
                        f"""SELECT thread_id, COUNT(*) FROM thread_items
                            WHERE thread_id IN ({marks})
                              AND item_type IN ('userMessage','agentMessage')
                            GROUP BY thread_id""",
                        batch,
                    ):
                        result[str(thread_id)]["message_count"] = int(message_count or 0)
                    for thread_id, duration_ms in conn.execute(
                        f"""SELECT thread_id, SUM(COALESCE(duration_ms,0)) FROM thread_turns
                            WHERE thread_id IN ({marks}) GROUP BY thread_id""",
                        batch,
                    ):
                        result[str(thread_id)]["total_duration_ms"] = int(duration_ms or 0)
        except (OSError, sqlite3.Error):
            pass

    for thread_id, rollout_path in rollout_paths.items():
        usage = _latest_rollout_usage(rollout_path)
        if usage:
            result[thread_id].update(usage)
    return result


def _newest_db(root: Path, pattern: str) -> Path | None:
    try:
        candidates = list(root.glob(pattern))
        return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None
    except OSError:
        return None


def _read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)


def _batches(values: list[str], size: int = 400) -> Iterable[list[str]]:
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _latest_rollout_usage(path_value: str, max_bytes: int = 512 * 1024) -> dict[str, Any]:
    """Read the latest token_count record from a bounded tail of one rollout."""
    if not path_value:
        return {}
    try:
        file_stat = os.stat(path_value)
        cache_key = str(path_value)
        with _USAGE_CACHE_LOCK:
            cached = _USAGE_CACHE.get(cache_key)
        if cached and cached[0] == file_stat.st_size and cached[1] == file_stat.st_mtime_ns:
            return dict(cached[2])
        with open(path_value, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            end = handle.tell()
            start = max(0, end - max_bytes)
            handle.seek(start)
            data = handle.read()
    except OSError:
        return {}
    for raw in reversed(data.splitlines()):
        if b'"token_count"' not in raw:
            continue
        try:
            record = json.loads(raw)
            payload = record.get("payload") or {}
            if payload.get("type") != "token_count":
                continue
            info = payload.get("info") or {}
            total = info.get("total_token_usage") or {}
            last = info.get("last_token_usage") or {}
            usage = {
                "total_input_tokens": int(total.get("input_tokens") or 0),
                "total_output_tokens": int(total.get("output_tokens") or 0),
                "context_tokens": int(last.get("input_tokens") or 0),
                "model_context_limit": int(info.get("model_context_window") or 0),
            }
            with _USAGE_CACHE_LOCK:
                _USAGE_CACHE[cache_key] = (file_stat.st_size, file_stat.st_mtime_ns, usage)
                if len(_USAGE_CACHE) > 1000:
                    _USAGE_CACHE.pop(next(iter(_USAGE_CACHE)))
            return dict(usage)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    return {}
