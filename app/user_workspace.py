"""Per-user data home — one folder per user for chat-session inputs and outputs.

Everything an agent produces *for a user* (and everything a user sends *in*)
belongs under that user's own home so it never leaks into the project root or
mixes between users:

    data/user_data/<user_id>/
        files/          generic agent outputs (reports, docs, downloads)
        screenshots/    browser screenshots
        visuals/        (reserved) generated images
        uploads/        (reserved) inbound attachments
        <any room>/     any other named bucket an ability asks for

This module is just the **path math** — it computes/creates those folders,
sanitises the user id so it is always a safe single path segment, and builds an
opaque authenticated URL the chat UI can use to fetch a file. It writes nothing on its own; the
`user_files` ability (and the browser-screenshot tool) call in here so every
writer agrees on the same layout.

The base dir defaults to ``data/user_data`` next to the repo and can be
overridden with ``WEBAGENT_USER_DATA_DIR`` (parity with ``UPLOAD_DIR``). Files
are never statically mounted: access is mediated by the content broker, which
validates the authenticated workspace and resolves an opaque expiring grant.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Optional
from app.runtime_mode import data_root

# Repo root = app/.. ; the home tree lives under data/ alongside uploads/visuals.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_BASE = data_root() / "user_data"

def base_dir() -> Path:
    """The root that holds every user's home (env-overridable)."""
    override = os.environ.get("WEBAGENT_USER_DATA_DIR", "").strip()
    return Path(override) if override else _DEFAULT_BASE


def safe_segment(value: str, fallback: str = "anonymous") -> str:
    """Reduce an arbitrary string to ONE safe path segment.

    Keeps letters/digits/dash/underscore; drops everything else (so ``:`` —
    illegal on Windows — and any separator can never escape the segment).
    """
    cleaned = "".join(c for c in (value or "") if c.isalnum() or c in "-_")
    return cleaned or fallback


def user_home(user_id: str) -> Path:
    """``data/user_data/<safe user id>/`` — created if missing."""
    from app.agent.member_workspace import parse_subject_id, member_home
    agent_member = parse_subject_id(user_id)
    if agent_member:
        return member_home(*agent_member)
    d = base_dir() / safe_segment(user_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def user_dir(user_id: str, room: str = "files") -> Path:
    """A named room inside the user's home (e.g. 'files', 'screenshots').

    The room name is sanitised the same way the user id is, so a caller can never
    use it to climb out of the home. Created if missing.
    """
    d = user_home(user_id) / safe_segment(room, fallback="files")
    d.mkdir(parents=True, exist_ok=True)
    return d


def public_url(user_id: str, room: str, filename: str) -> str:
    """An opaque, expiring URL for a file in this user's workspace."""
    import mimetypes
    from app.content_access import artifact_url_for_path
    safe_uid = safe_segment(user_id)
    safe_room = safe_segment(room, fallback="files")
    safe_name = safe_filename(filename)
    return artifact_url_for_path(
        user_id=user_id,
        storage_path=f"{safe_uid}/{safe_room}/{safe_name}",
        original_name=safe_name,
        mime_type=mimetypes.guess_type(safe_name)[0] or "application/octet-stream",
    )


def safe_filename(filename: str, default_ext: str = "") -> str:
    """Reduce a requested filename to a safe basename (no directories).

    Strips any path components, keeps the final name, and falls back to a random
    name when nothing usable is left. ``default_ext`` (e.g. 'png') is appended
    only when the cleaned name has no extension of its own.
    """
    raw = (filename or "").replace("\\", "/").split("/")[-1].strip()
    # Allow a single dot for the extension; sanitise the rest conservatively.
    keep = []
    for c in raw:
        if c.isalnum() or c in "-_.":
            keep.append(c)
    name = "".join(keep).strip(".")
    if not name:
        name = uuid.uuid4().hex[:12]
    if default_ext and "." not in name:
        name = f"{name}.{default_ext.lstrip('.')}"
    return name


def unique_path(directory: Path, filename: str) -> Path:
    """A non-clobbering path in ``directory`` for ``filename``.

    If the name is free, use it as-is (predictable for the agent). If it already
    exists, insert a short random suffix before the extension so an earlier
    output is never silently overwritten.
    """
    dest = directory / filename
    if not dest.exists():
        return dest
    stem = dest.stem
    suffix = dest.suffix  # includes leading dot, or "" if none
    return directory / f"{stem}-{uuid.uuid4().hex[:6]}{suffix}"


def resolve_within_home(user_id: str, rel_path: str) -> Optional[Path]:
    """Resolve ``rel_path`` (given by an agent) INSIDE the user's home.

    Returns the absolute path only if it stays within the home tree; returns
    None on any attempt to climb out (``..`` / absolute paths). Used by read-back
    tools so an agent can only fetch its own user's files.
    """
    home = user_home(user_id).resolve()
    candidate = (home / str(rel_path or "").replace("\\", "/").lstrip("/")).resolve()
    try:
        candidate.relative_to(home)
    except ValueError:
        return None
    return candidate
