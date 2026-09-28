"""Per-agent data home — one folder per agent for agent-specific data and DBs.

Mirrors ``app/user_workspace.py`` — same path-math pattern applied to agents.
The base dir holds a per-agent SQLite authority database plus room directories
for agent-produced outputs (screenshots, generated files, etc.).

    data/agent_data/<agent_id>/
        <agent_id>.db     per-agent authority database
        files/            generic agent outputs (reports, docs)
        knowledge/        durable files managed by the Knowledge ability
        screenshots/      agent browser screenshots

The base dir defaults to ``data/agent_data`` next to the repo and can be
overridden with ``WEBAGENT_AGENT_DATA_DIR``. This directory is NOT served over
HTTP — it is a backend-access-only store (the per-agent DB is opened by SQLite,
not mounted as static files).
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Optional
from app.runtime_mode import data_root

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_BASE = data_root() / "agent_data"


def base_dir() -> Path:
    """The root that holds every agent's home (env-overridable)."""
    override = os.environ.get("WEBAGENT_AGENT_DATA_DIR", "").strip()
    return Path(override) if override else _DEFAULT_BASE


def safe_segment(value: str, fallback: str = "unknown") -> str:
    """Reduce an arbitrary string to ONE safe path segment.

    Keeps letters/digits/dash/underscore; drops everything else (so ``:`` —
    illegal on Windows — and any separator can never escape the segment).
    """
    cleaned = "".join(c for c in (value or "") if c.isalnum() or c in "-_")
    return cleaned or fallback


def agent_home(agent_id: str) -> Path:
    """``data/agent_data/<safe agent id>/`` — created if missing."""
    d = base_dir() / safe_segment(agent_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def agent_dir(agent_id: str, room: str = "files") -> Path:
    """A named room inside the agent's home (e.g. 'files', 'screenshots').

    The room name is sanitised the same way the agent id is, so a caller can
    never use it to climb out of the home. Created if missing.
    """
    d = agent_home(agent_id) / safe_segment(room, fallback="files")
    d.mkdir(parents=True, exist_ok=True)
    return d


def agent_ui_dir(agent_id: str) -> Path:
    """Backend-only directory for self-contained, sandboxed agent UI assets."""
    return agent_dir(agent_id, "ui")


def resolve_ui_asset(agent_id: str, rel_path: str = "index.html") -> Optional[Path]:
    """Resolve an agent UI asset without allowing traversal or symlink escape."""
    root = agent_ui_dir(agent_id).resolve()
    candidate = (root / str(rel_path or "index.html").replace("\\", "/").lstrip("/")).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    if candidate.suffix.lower() in {".py", ".pyc", ".pyo", ".bak", ".db", ".sqlite"}:
        return None
    return candidate


def agent_db_path(agent_id: str) -> Path:
    """``data/agent_data/<safe id>/<safe id>.db`` — per-agent authority DB path."""
    return agent_home(agent_id) / f"{safe_segment(agent_id)}.db"


def subagent_home(parent_id: str, clone_id: str) -> Path:
    """``data/agent_data/<parent>/subagents/<clone>/`` — nested home for a
    spawned subagent (clone) under its parent agent. Created if missing."""
    d = agent_home(parent_id) / "subagents" / safe_segment(clone_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def subagent_db_path(parent_id: str, clone_id: str) -> Path:
    """``data/agent_data/<parent>/subagents/<clone>/<clone>.db`` — subagent DB
    nested inside the parent agent's home."""
    return subagent_home(parent_id, clone_id) / f"{safe_segment(clone_id)}.db"


def purge_subagent_home(parent_id: str, clone_id: str) -> bool:
    """Delete a subagent's nested home directory. Returns True if it existed."""
    import shutil

    d = agent_home(parent_id) / "subagents" / safe_segment(clone_id)
    if d.exists():
        shutil.rmtree(d)
        return True
    return False


def relocate_legacy_clone_home(clone_id: str, parent_id: str) -> bool:
    """Move a top-level ``data/agent_data/<clone>/`` (created before subagent
    nesting existed) into ``data/agent_data/<parent>/subagents/<clone>/``.

    Returns True when a top-level directory was moved, False when there was
    nothing to move (already nested, or never created)."""
    import shutil

    src = base_dir() / safe_segment(clone_id)
    if not src.exists():
        return False
    dst = agent_home(parent_id) / "subagents" / safe_segment(clone_id)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        # A nested copy already exists — the duplicate top-level one is stale
        # (both are derived from global.db). Drop the duplicate instead of
        # clobbering the nested copy.
        try:
            shutil.rmtree(src)
            return True
        except Exception:
            return False
    shutil.move(str(src), str(dst))
    return True


def purge_agent_home(agent_id: str) -> bool:
    """Delete the entire agent data directory tree. Returns True if it existed."""
    import shutil

    home = base_dir() / safe_segment(agent_id)
    if home.exists():
        shutil.rmtree(home)
        return True
    return False


def resolve_within_home(agent_id: str, rel_path: str) -> Optional[Path]:
    """Resolve ``rel_path`` INSIDE the agent's home. Returns None on escape attempt."""
    home = agent_home(agent_id).resolve()
    candidate = (home / str(rel_path or "").replace("\\", "/").lstrip("/")).resolve()
    try:
        candidate.relative_to(home)
    except ValueError:
        return None
    return candidate
