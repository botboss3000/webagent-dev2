"""Storage-authority contract shared by server and browser implementations.

The Python protocol is the canonical shape for server SQLite. The IndexedDB
implementation uses the same wire fields in ``ui/chat/js/storage/sync.js``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional, Protocol


ConflictStatus = Literal["applied", "noop", "conflict", "rejected"]
MutationOperation = Literal["upsert", "delete"]


@dataclass(frozen=True)
class AuthorityOwner:
    user_id: str
    session_id: str


@dataclass(frozen=True)
class AuthorityRevision:
    revision: int
    content_hash: str
    schema_version: int = 1


@dataclass
class AuthoritySnapshot:
    owner: AuthorityOwner
    revision: AuthorityRevision
    session: dict[str, Any]
    interactions: list[dict[str, Any]] = field(default_factory=list)
    tombstoned: bool = False
    deleted_at: Optional[str] = None


@dataclass
class AuthorityMutation:
    mutation_id: str
    owner: AuthorityOwner
    operation: MutationOperation
    base_revision: int
    client_revision: int
    content_hash: str = ""
    session: Optional[dict[str, Any]] = None
    interactions: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class AuthorityMutationResult:
    mutation_id: str
    owner: AuthorityOwner
    status: ConflictStatus
    server_revision: int
    content_hash: str
    client_revision: int
    error: Optional[str] = None


class TranscriptAuthority(Protocol):
    """Minimum recoverable transcript authority.

    Implementations must be tenant-scoped, compare-and-swap revisions, retain
    tombstones long enough to prevent stale resurrection, and make mutation
    identifiers idempotent for a bounded documented window.
    """

    async def read(self, owner: AuthorityOwner) -> Optional[AuthoritySnapshot]:
        ...

    async def apply(
        self, owner: AuthorityOwner, mutations: list[AuthorityMutation]
    ) -> list[AuthorityMutationResult]:
        ...

    async def recover(
        self, owner: AuthorityOwner, known_revision: int, known_hash: str
    ) -> Optional[AuthoritySnapshot]:
        ...


class ServerSQLiteTranscriptAuthority:
    """TranscriptAuthority implementation backed by a per-user UserStore."""

    def __init__(self, user_store: Any, *, user_id: str) -> None:
        self._store = user_store
        self._user_id = user_id

    def _scope(self, owner: AuthorityOwner) -> None:
        if owner.user_id != self._user_id:
            raise PermissionError("authority owner mismatch")

    async def read(self, owner: AuthorityOwner) -> Optional[AuthoritySnapshot]:
        self._scope(owner)
        session = await self._store.get_session(owner.session_id)
        if session is None:
            return None
        interactions = await self._store.get_interactions(owner.session_id)
        return AuthoritySnapshot(
            owner=owner,
            revision=AuthorityRevision(
                revision=int(session.get("authority_revision") or 0),
                content_hash=str(session.get("content_hash") or ""),
            ),
            session=session,
            interactions=interactions,
            tombstoned=bool(session.get("deleted_at")),
            deleted_at=session.get("deleted_at"),
        )

    async def apply(
        self, owner: AuthorityOwner, mutations: list[AuthorityMutation]
    ) -> list[AuthorityMutationResult]:
        self._scope(owner)
        wire = []
        for mutation in mutations:
            self._scope(mutation.owner)
            if mutation.owner.session_id != owner.session_id:
                raise PermissionError("mutation session mismatch")
            wire.append({
                "mutation_id": mutation.mutation_id,
                "session_id": mutation.owner.session_id,
                "operation": mutation.operation,
                "base_server_revision": mutation.base_revision,
                "client_revision": mutation.client_revision,
                "content_hash": mutation.content_hash,
                "session": mutation.session,
                "interactions": mutation.interactions,
            })
        rows = await self._store.apply_sync_mutations(self._user_id, wire)
        return [
            AuthorityMutationResult(
                mutation_id=row.get("mutation_id", ""),
                owner=AuthorityOwner(self._user_id, row.get("session_id", "")),
                status=row.get("status", "rejected"),
                server_revision=int(row.get("server_revision") or 0),
                content_hash=str(row.get("content_hash") or ""),
                client_revision=int(row.get("client_revision") or 0),
                error=row.get("error"),
            )
            for row in rows
        ]

    async def recover(
        self, owner: AuthorityOwner, known_revision: int, known_hash: str
    ) -> Optional[AuthoritySnapshot]:
        snapshot = await self.read(owner)
        if snapshot is None:
            return None
        if (
            snapshot.revision.revision == known_revision
            and snapshot.revision.content_hash == known_hash
        ):
            return None
        return snapshot
