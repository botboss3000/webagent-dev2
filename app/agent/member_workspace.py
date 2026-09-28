"""Agent-member identity and workspace path helpers.

Agent-native callers use an opaque internal user id that carries only the two
safe storage locators needed by the existing user-plane router.  This keeps the
large session/memory/attachment API surface unchanged while moving its physical
authority below the owning agent::

    data/agent_data/<agent>/members/<member>/<member>.db

The id is an internal subject, never a username or an authorization decision.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Tuple
import base64

from app.agent_workspace import agent_home, safe_segment


SUBJECT_PREFIX = "agentmember--"


def is_agent_member_subject(user_id: str) -> bool:
    return parse_subject_id(user_id) is not None


def subject_id(agent_id: str, member_id: str) -> str:
    agent = safe_segment(agent_id, fallback="")
    member = safe_segment(member_id, fallback="")
    if not agent or not member:
        raise ValueError("agent_id and member_id are required")
    def _enc(value: str) -> str:
        return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")
    return f"{SUBJECT_PREFIX}v1--{_enc(agent)}--{_enc(member)}"


def parse_subject_id(user_id: str) -> Optional[Tuple[str, str]]:
    raw = str(user_id or "")
    if not raw.startswith(SUBJECT_PREFIX):
        return None
    rest = raw[len(SUBJECT_PREFIX):]
    if not rest.startswith("v1--"):
        return None
    parts = rest.split("--")
    if len(parts) != 3:
        return None
    def _dec(value: str) -> str:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode("utf-8")
    try:
        agent_id, member_id = _dec(parts[1]), _dec(parts[2])
    except Exception:
        return None
    if not agent_id or not member_id:
        return None
    if safe_segment(agent_id, fallback="") != agent_id:
        return None
    if safe_segment(member_id, fallback="") != member_id:
        return None
    return agent_id, member_id


def member_home(agent_id: str, member_id: str, *, create: bool = True) -> Path:
    root = agent_home(agent_id) / "members" / safe_segment(member_id)
    if create:
        root.mkdir(parents=True, exist_ok=True)
    return root


def member_db_path(agent_id: str, member_id: str) -> Path:
    member = safe_segment(member_id)
    return member_home(agent_id, member) / f"{member}.db"


def member_dir(agent_id: str, member_id: str, room: str = "files") -> Path:
    target = member_home(agent_id, member_id) / safe_segment(room, fallback="files")
    target.mkdir(parents=True, exist_ok=True)
    return target


def resolve_within_member_home(agent_id: str, member_id: str, rel_path: str) -> Optional[Path]:
    home = member_home(agent_id, member_id).resolve()
    candidate = (home / str(rel_path or "").replace("\\", "/").lstrip("/")).resolve()
    try:
        candidate.relative_to(home)
    except ValueError:
        return None
    return candidate
