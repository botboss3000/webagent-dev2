"""Request/turn-local workspace authority.

Authentication proves the actor.  A :class:`WorkspaceContext` additionally
proves which user/agent authority that actor may operate in.  Scoped code must
not infer an authority from a client supplied id or from the bootstrap admin.
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from dataclasses import dataclass
from typing import FrozenSet, Iterator, Literal, Optional

from fastapi import HTTPException, Request

SubjectKind = Literal["user", "agent", "agent_member", "system"]


@dataclass(frozen=True)
class WorkspaceContext:
    actor_user_id: str
    subject_kind: SubjectKind
    subject_id: str
    capabilities: FrozenSet[str]
    reason: str = ""

    def require(self, capability: str) -> None:
        if capability not in self.capabilities:
            raise HTTPException(status_code=403, detail="Workspace capability required")


_current: contextvars.ContextVar[Optional[WorkspaceContext]] = contextvars.ContextVar(
    "webagent_workspace_context", default=None
)


def current_workspace() -> Optional[WorkspaceContext]:
    return _current.get()


def set_workspace(context: Optional[WorkspaceContext]):
    """Set the ambient context and return a token suitable for reset()."""
    return _current.set(context)


def reset_workspace(token) -> None:
    _current.reset(token)


@contextmanager
def workspace_scope(context: WorkspaceContext) -> Iterator[WorkspaceContext]:
    token = set_workspace(context)
    try:
        yield context
    finally:
        reset_workspace(token)


def actor_context(user_id: str) -> WorkspaceContext:
    return WorkspaceContext(
        actor_user_id=user_id,
        subject_kind="user",
        subject_id=user_id,
        capabilities=frozenset({"user:read", "user:write"}),
    )


def system_user_context(user_id: str, reason: str) -> WorkspaceContext:
    """Explicit authority for a background service with a known user owner."""
    if not user_id:
        raise ValueError("Background workspace owner is required")
    return WorkspaceContext(
        actor_user_id="system",
        subject_kind="user",
        subject_id=user_id,
        capabilities=frozenset({"user:read", "user:write"}),
        reason=reason,
    )


async def require_user_workspace(
    request: Request,
    requested_user_id: Optional[str] = None,
    *,
    write: bool = False,
) -> WorkspaceContext:
    """Resolve the caller's own user workspace.

    Platform administration is intentionally not an exception here. App-wide
    administration must use an explicitly admin-scoped control-plane tool or
    endpoint; ordinary user-data routes can only select the authenticated
    actor's workspace.
    """
    from app.auth.identity import request_user_id

    actor = request_user_id(request)
    if not actor:
        raise HTTPException(status_code=401, detail="Not authenticated")
    capability = "user:write" if write else "user:read"
    ambient = current_workspace()
    subject = str(requested_user_id or actor)
    if subject != actor:
        raise HTTPException(status_code=404, detail="Workspace not found")
    if (
        ambient and ambient.actor_user_id == actor
        and ambient.subject_kind == "user" and ambient.subject_id == subject
    ):
        ambient.require(capability)
        return ambient
    context = actor_context(actor)
    context.require(capability)
    set_workspace(context)
    return context
