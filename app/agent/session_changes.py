"""Attribute working-tree changes to the chat session that produced them."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from app.api.github import _run_git
from app.db import get_db

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
logger = logging.getLogger(__name__)

# These tools directly name the repository path they may change.
_PATH_TOOLS = {
    "write_source",
    "edit_source",
    "patch_source",
    "delete_source",
    "resolve_conflict",
}

# These tools can change arbitrary paths, so their complete dirty-tree state is
# compared before and after execution.
_BROAD_TOOLS = {"run_command", "run_python", "git_tool"}

# Runtime state is intentionally outside source-change ownership. Hashing every
# SQLite database (plus WAL/SHM companions) before and after each broad tool made
# a trivial run_command/run_python scan the entire application data set twice.
_RUNTIME_PREFIXES = (
    "data/agent_data/",
    "data/user_data/",
    "logs/",
    ".ast-index/",
)
_RUNTIME_SUFFIXES = (
    ".db", ".db-wal", ".db-shm", ".db-journal",
    ".sqlite", ".sqlite-wal", ".sqlite-shm", ".sqlite-journal",
)
_GIT_EXCLUDES = (
    ":(exclude)data/agent_data/**",
    ":(exclude)data/user_data/**",
    ":(exclude)logs/**",
    ":(exclude).ast-index/**",
    ":(exclude,glob)**/*.db",
    ":(exclude,glob)**/*.db-wal",
    ":(exclude,glob)**/*.db-shm",
    ":(exclude,glob)**/*.sqlite",
    ":(exclude,glob)**/*.sqlite-wal",
    ":(exclude,glob)**/*.sqlite-shm",
    ":(exclude,glob)**/*.sqlite-journal",
)


def _trackable_path(path: str) -> bool:
    value = (path or "").replace("\\", "/").strip().lstrip("/")
    lower = value.lower()
    return bool(value) and not lower.startswith(_RUNTIME_PREFIXES) and not lower.endswith(_RUNTIME_SUFFIXES)


def should_track_tool(name: str) -> bool:
    return name in _PATH_TOOLS or name in _BROAD_TOOLS


def _repo_relative(raw: Any) -> str | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    path = Path(raw.strip())
    if not path.is_absolute():
        path = _PROJECT_ROOT / path
    try:
        resolved = path.resolve()
        relative = resolved.relative_to(_PROJECT_ROOT.resolve())
    except (OSError, ValueError):
        return None
    value = relative.as_posix()
    return value if value and value != "." else None


def _direct_paths(name: str, args: dict) -> set[str] | None:
    if name not in _PATH_TOOLS:
        return None
    paths: set[str] = set()
    raw_paths = args.get("paths")
    if isinstance(raw_paths, list):
        for raw in raw_paths:
            if path := _repo_relative(raw):
                paths.add(path)
    if path := _repo_relative(args.get("path")):
        paths.add(path)
    return paths


def _status_paths(candidates: set[str] | None = None) -> dict[str, str]:
    cmd = [
        "-c", "core.quotePath=false", "status", "--porcelain",
        "--untracked-files=all",
    ]
    if candidates is not None:
        candidates = {path for path in candidates if _trackable_path(path)}
        if not candidates:
            return {}
        cmd.extend(["--", *sorted(candidates)])
    else:
        # Keep newly-created source files visible while preventing Git from
        # walking runtime data trees that can contain hundreds of mutable DBs.
        cmd.extend(["--", ".", *_GIT_EXCLUDES])
    # Route through the shared git runner (app.api.github._run_git) instead of a
    # raw subprocess: it serializes with the server's other git work AND sets
    # GIT_OPTIONAL_LOCKS=0, so this per-tool snapshot can never grab index.lock
    # and race a concurrent commit_and_push into "index.lock: File exists".
    status_out, status_err, status_rc = _run_git(cmd, timeout=20, cwd=_PROJECT_ROOT)
    if status_rc != 0:
        return {}
    result: dict[str, str] = {}
    for line in status_out.splitlines():
        if len(line) < 4:
            continue
        raw_path = line[3:].strip().strip('"')
        if " -> " in raw_path:
            raw_path = raw_path.rsplit(" -> ", 1)[-1]
        if (path := _repo_relative(raw_path)) and _trackable_path(path):
            result[path] = line[:2]
    return result


def _content_fingerprint(path: str, status: str) -> str:
    """Cheap change fingerprint for one dirty path.

    Git already tells us whether a path is dirty. Size + nanosecond mtime is
    enough to distinguish two snapshots around one tool invocation and avoids
    rereading every dirty file (formerly O(total dirty bytes) per snapshot).
    """
    target = _PROJECT_ROOT / path
    try:
        stat = target.stat()
        return f"{status}:{stat.st_mtime_ns}:{stat.st_size}:{int(target.is_dir())}"
    except OSError:
        return f"{status}:missing"


def capture_tool_state(name: str, args: dict) -> dict[str, str] | None:
    """Return dirty-path fingerprints for a repository-mutating tool."""
    if not should_track_tool(name):
        return None
    candidates = _direct_paths(name, args)
    statuses = _status_paths(candidates)
    return {
        path: _content_fingerprint(path, status)
        for path, status in statuses.items()
    }


async def record_tool_delta(
    session_id: str,
    before: dict[str, str] | None,
    after: dict[str, str] | None,
) -> list[str]:
    """Persist paths changed by one successful tool execution."""
    if not session_id or before is None or after is None:
        return []
    claimed = sorted(
        path for path, fingerprint in after.items()
        if before.get(path) != fingerprint
    )
    cleaned = sorted(path for path in before if path not in after)
    # A clean path becoming dirty starts a new ownership generation. Remove any
    # stale claims left by older sessions before assigning the new owner.
    reset = sorted(path for path in claimed if path not in before)
    if not claimed and not cleaned:
        return []
    db = get_db()
    updater = getattr(db, "update_session_change_claims", None)
    if updater is None:
        return []
    try:
        await updater(
            session_id,
            claimed=claimed,
            reset=reset,
            cleared=cleaned,
        )
    except Exception as exc:  # attribution must never turn a successful edit into a failed tool
        logger.warning("Could not record session change paths for %s: %s", session_id, exc)
        return []
    return claimed


async def capture_tool_state_async(name: str, args: dict) -> dict[str, str] | None:
    if not should_track_tool(name):
        return None
    try:
        return await asyncio.to_thread(capture_tool_state, name, args)
    except Exception as exc:
        logger.warning("Could not inspect working tree around %s: %s", name, exc)
        return None
