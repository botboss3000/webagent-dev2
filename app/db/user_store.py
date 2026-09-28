"""Per-user SQLite authority at ``data/user_data/{user_id}/{user_id}.db``.

Each file holds one user's sessions, interactions, memories, files, and
automations. Installation data lives in app.db and agent configuration lives in
per-agent authority files.

Usage::

    from app.db.user_store import get_user_store
    store = get_user_store(user_id)
    sid = await store.create_session(user_id, agent_id="default", title="My session")
    await store.insert_interaction(sid, role="user", content="hello")
"""

from __future__ import annotations

import json
import hashlib
import logging
import os
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from app.runtime_mode import data_root

logger = logging.getLogger(__name__)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
USER_DATA_DIR = str(data_root() / "user_data")
_USER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}$")
_WINDOWS_RESERVED_NAMES = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def _ensure_user_dir() -> None:
    os.makedirs(USER_DATA_DIR, exist_ok=True)


def _user_db_path(user_id: str) -> str:
    """Return a contained per-user DB path or reject an unsafe identifier."""
    # Agent-native subjects keep their user-plane database inside the owning
    # agent's member tree.  The rest of UserStore remains unchanged, so every
    # existing session/memory/attachment method automatically observes the new
    # ownership boundary.
    from app.agent.member_workspace import parse_subject_id, member_db_path
    agent_member = parse_subject_id(user_id)
    if agent_member:
        return str(member_db_path(*agent_member))
    if not isinstance(user_id, str) or not _USER_ID_RE.fullmatch(user_id):
        raise ValueError("Invalid user_id for per-user storage")
    if user_id.split(".", 1)[0].lower() in _WINDOWS_RESERVED_NAMES:
        raise ValueError("Invalid user_id for per-user storage")

    root = Path(USER_DATA_DIR).resolve()
    candidate = (root / user_id / f"{user_id}.db").resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("Per-user database path escapes storage root") from exc
    return str(candidate)


def _json_text(value: Any, default: str) -> str:
    if value is None:
        return default
    return value if isinstance(value, str) else json.dumps(value)


# ── Schema for user-specific tables ─────────────────────────────────────────
# These tables are the user-private subset of SCHEMA_SQL from app/db/local.py.
# Keep in sync when the main schema changes.

USER_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    title TEXT,
    metadata TEXT,
    agent_id TEXT,
    participants TEXT DEFAULT '[]',
    sort_order INTEGER,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    pinned INTEGER NOT NULL DEFAULT 0,
    hidden INTEGER NOT NULL DEFAULT 0,
    read_at TEXT,
    -- Explicit manual-unread marker. Keep read_at so the UI can show when the
    -- session was last read before it was marked unread.
    unread_at TEXT,
    authority_revision INTEGER NOT NULL DEFAULT 0,
    content_hash TEXT NOT NULL DEFAULT '',
    deleted_at TEXT
    chat_render_version INTEGER NOT NULL DEFAULT 2,
);

CREATE TABLE IF NOT EXISTS interactions (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    parent_id TEXT,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    tool_name TEXT,
    tool_call_id TEXT,
    channel TEXT,
    metadata TEXT,
    output TEXT,
    source TEXT,
    from_id TEXT,
    to_id TEXT,
    session_seq INTEGER,
    turn_id TEXT,
    turn_seq INTEGER,
    chat_render_kind TEXT NOT NULL DEFAULT 'activity',
    status TEXT NOT NULL DEFAULT 'complete',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_interactions_session ON interactions(session_id);
CREATE INDEX IF NOT EXISTS idx_interactions_created ON interactions(created_at);
CREATE INDEX IF NOT EXISTS idx_interactions_session_created ON interactions(session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_interactions_render_v2 ON interactions(session_id, chat_render_kind, session_seq);

CREATE TABLE IF NOT EXISTS session_runs (
    session_id TEXT PRIMARY KEY REFERENCES sessions(id),
    user_id TEXT NOT NULL,
    agent_id TEXT,
    turn_id TEXT,
    assistant_interaction_id TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    latest_session_seq INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    stop_cause TEXT,
    origin TEXT,
    resume_attempts INTEGER NOT NULL DEFAULT 0,
    max_resume_attempts INTEGER,
    resume_cause_counts TEXT,
    heartbeat_at TEXT,
    next_resume_at TEXT,
    owner_token TEXT,
    lease_expires_at TEXT,
    relaunch_ctx TEXT,
    current_op TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_id_user
    ON sessions(id, user_id);

CREATE TABLE IF NOT EXISTS session_interrupts (
    session_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    interrupt_requested INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (session_id, user_id)
        REFERENCES sessions(id, user_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_session_interrupts_user
    ON session_interrupts(user_id);

-- Session-completion notifications shown by the chat panel's sliding toast.
-- Persisted per-user so an undismissed "Session complete" re-appears on every
-- device (pushed via the hybrid sync engine — see SYNCED_SPECS) until the user
-- dismisses it. Dismissal is a soft flag so the upsert-only puller can
-- propagate it across devices; old dismissed rows are pruned on read.
CREATE TABLE IF NOT EXISTS session_notifications (
    session_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    dismissed INTEGER NOT NULL DEFAULT 0,
    dismissed_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_session_notifications_user
    ON session_notifications(user_id);

CREATE TABLE IF NOT EXISTS attachments (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    session_id      TEXT,
    original_name   TEXT NOT NULL,
    mime_type       TEXT NOT NULL,
    size_bytes      INTEGER NOT NULL,
    storage_path    TEXT NOT NULL,
    storage_provider TEXT NOT NULL DEFAULT 'local',
    metadata        TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_attachments_user ON attachments(user_id);
CREATE INDEX IF NOT EXISTS idx_attachments_session ON attachments(session_id);

CREATE TABLE IF NOT EXISTS workspace_artifacts (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    storage_path TEXT NOT NULL,
    storage_provider TEXT NOT NULL DEFAULT 'local',
    original_name TEXT NOT NULL DEFAULT '',
    mime_type TEXT NOT NULL DEFAULT 'application/octet-stream',
    size_bytes INTEGER NOT NULL DEFAULT 0,
    source_type TEXT NOT NULL DEFAULT 'file',
    source_id TEXT,
    created_by TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    deleted_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_workspace_artifacts_user
    ON workspace_artifacts(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_workspace_artifacts_source
    ON workspace_artifacts(source_type, source_id);

CREATE TABLE IF NOT EXISTS session_summaries (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    session_id TEXT NOT NULL UNIQUE REFERENCES sessions(id),
    title TEXT,
    summary TEXT NOT NULL,
    message_count INTEGER NOT NULL DEFAULT 0,
    covered_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_summaries_user ON session_summaries(user_id);

CREATE TABLE IF NOT EXISTS session_summary_segments (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    seq INTEGER NOT NULL DEFAULT 0,
    start_index INTEGER NOT NULL DEFAULT 0,
    end_index INTEGER NOT NULL DEFAULT 0,
    summary TEXT NOT NULL,
    token_estimate INTEGER NOT NULL DEFAULT 0,
    topic TEXT,
    tier INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(session_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_summary_segments_session ON session_summary_segments(session_id, seq);

CREATE TABLE IF NOT EXISTS memories (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    slug            TEXT NOT NULL,
    page_type       TEXT NOT NULL CHECK (page_type IN (
                        'person', 'company', 'deal', 'meeting', 'project',
                        'idea', 'concept', 'writing', 'program', 'personal',
                        'media', 'inbox', 'archive'
                    )),
    title           TEXT NOT NULL,
    compiled_truth  TEXT NOT NULL DEFAULT '',
    timeline        TEXT NOT NULL DEFAULT '',
    frontmatter     TEXT NOT NULL DEFAULT '{}',
    origin          TEXT NOT NULL DEFAULT 'distilled',
    pinned          INTEGER NOT NULL DEFAULT 0,
    provenance      TEXT NOT NULL DEFAULT '[]',
    needs_review    INTEGER NOT NULL DEFAULT 0,
    content_hash    TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (user_id, slug)
);

CREATE TABLE IF NOT EXISTS user_profiles (
    user_id TEXT PRIMARY KEY,
    is_admin INTEGER NOT NULL DEFAULT 0,
    default_agent_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_login_at TEXT,
    appearance TEXT
);

CREATE TABLE IF NOT EXISTS user_accounts (
    user_id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    remember_token TEXT DEFAULT '',
    is_approved INTEGER NOT NULL DEFAULT 1,
    session_lifetime_minutes INTEGER NOT NULL DEFAULT 43200,
    auto_renew INTEGER NOT NULL DEFAULT 1,
    social_links TEXT DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_user_accounts_remember ON user_accounts(remember_token);

CREATE TABLE IF NOT EXISTS browser_sync_receipts (
    mutation_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_browser_sync_receipts_session
    ON browser_sync_receipts(user_id, session_id);
"""

# The hand-written block above is retained for rolling compatibility with old
# installations.  All current user-plane tables are generated from the central
# ownership registry, so adding a core user table can no longer silently omit it
# from newly created per-user databases.
from app.db.schema import render_plane as _render_plane_schema
USER_SCHEMA_SQL += "\n" + _render_plane_schema("user", "sqlite")

# ── Connection pool ─────────────────────────────────────────────────────────
# One connection per user, held open for the lifetime of the store reference.
# A simple dict cache; ThreadPool-friendly because SQLite connections are
# thread-safe when each thread gets its own connection (WAL mode).

_connections: Dict[str, sqlite3.Connection] = {}
_conn_lock = threading.Lock()


def get_user_store(user_id: str) -> "UserStore":
    """Return (or create) a `UserStore` for `user_id`.

    The underlying SQLite file is at ``data/user_data/{user_id}.db``.
    Reusing the same store across requests is safe — the connection
    is kept open and the schema is idempotent.
    """
    _ensure_user_dir()
    return UserStore(user_id)


def close_user_store(user_id: str) -> None:
    """Close the connection for a user id (e.g. on server shutdown)."""
    with _conn_lock:
        conn = _connections.pop(user_id, None)
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def close_all() -> None:
    """Close every open user connection."""
    with _conn_lock:
        for uid, conn in list(_connections.items()):
            try:
                conn.close()
            except Exception:
                pass
        _connections.clear()


async def export_user_store_data(user_id: str) -> Optional[dict]:
    """Export the local browser-sync sidecar when it exists.

    The sidecar may be the primary split SQLite data plane or a staging store
    used by browser promotion. Credential rows are intentionally excluded by
    the shared lifecycle exporter.
    """
    path = Path(_user_db_path(user_id))
    if not path.exists():
        return None
    store = get_user_store(user_id)
    from app.db.user_lifecycle import export_user_data

    return await export_user_data(store._get_conn(), user_id)


async def purge_user_store_files(
    user_id: str,
    *,
    remove_files: bool = True,
) -> dict[str, int]:
    """Erase one browser-sync sidecar, optionally removing its files.

    Account deletion uses a two-step call: erase rows before removing the
    central login, then unlink the now-empty sidecar after that login is gone.
    """
    path = Path(_user_db_path(user_id))
    counts: dict[str, int] = {"sidecar_files": 0}
    if path.exists():
        store = get_user_store(user_id)
        from app.db.user_lifecycle import erase_user_data

        counts.update(await erase_user_data(
            store._get_conn(), user_id, include_account=False
        ))
    close_user_store(user_id)
    if not remove_files:
        return counts

    secret_path = path.with_name(f"{path.stem}.user-secrets.db")
    candidates = (
        path,
        Path(f"{path}-wal"),
        Path(f"{path}-shm"),
        Path(f"{path}.plain-premigrate"),
        Path(f"{path}.enc-premigrate"),
        Path(f"{path}.bak"),
        Path(f"{path}.backup"),
        secret_path,
        Path(f"{secret_path}-wal"),
        Path(f"{secret_path}-shm"),
        Path(f"{secret_path}.plain-premigrate"),
        Path(f"{secret_path}.enc-premigrate"),
        Path(f"{secret_path}.bak"),
        Path(f"{secret_path}.backup"),
    )
    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            candidate.unlink()
            counts["sidecar_files"] += 1
    try:
        path.parent.rmdir()
    except OSError:
        pass
    return counts


class UserStore:
    """Per-user SQLite store backed by ``data/user_data/{user_id}.db``."""

    def __init__(self, user_id: str):
        self._user_id = user_id
        self._db_path = _user_db_path(user_id)
        self._conn: Optional[sqlite3.Connection] = None

    # ── Connection management ───────────────────────────────────────────

    def _get_conn(self) -> sqlite3.Connection:
        """Get (or open + init) this user's SQLite connection."""
        if self._conn is not None:
            return self._conn

        with _conn_lock:
            # Double-check under lock
            if self._conn is not None:
                return self._conn
            if self._user_id in _connections:
                self._conn = _connections[self._user_id]
                return self._conn

            os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
            conn = sqlite3.connect(self._db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA foreign_keys=ON")

            # Forward-compatible pre-migration. The canonical user-plane DDL
            # creates indexes after CREATE TABLE IF NOT EXISTS; on an existing
            # browser_sessions table the new chat index therefore needs its
            # additive column present before executescript reaches that index.
            browser_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='browser_sessions'"
            ).fetchone()
            if browser_table:
                browser_columns = {
                    row[1] for row in conn.execute(
                        "PRAGMA table_info(browser_sessions)"
                    ).fetchall()
                }
                if "chat_session_id" not in browser_columns:
                    conn.execute(
                        "ALTER TABLE browser_sessions ADD COLUMN chat_session_id TEXT"
                    )

            # Idempotent schema plus additive reconciliation for all canonical
            # non-key user-plane columns.
            conn.executescript(USER_SCHEMA_SQL)
            from app.db.schema import ensure_sqlite_plane_columns
            ensure_sqlite_plane_columns(conn, "user")
            existing_session_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()
            }
            for name, ddl in (
                ("unread_at", "TEXT"),
                ("authority_revision", "INTEGER NOT NULL DEFAULT 0"),
                ("content_hash", "TEXT NOT NULL DEFAULT ''"),
                ("deleted_at", "TEXT"),
            ):
                if name not in existing_session_columns:
                    conn.execute(f"ALTER TABLE sessions ADD COLUMN {name} {ddl}")
            existing_interaction_columns = {row[1] for row in conn.execute("PRAGMA table_info(interactions)").fetchall()}
            if "chat_render_version" not in existing_session_columns:
                # Migration marker only. The reader has one render contract and
                # never branches on this value.
                conn.execute("ALTER TABLE sessions ADD COLUMN chat_render_version INTEGER NOT NULL DEFAULT 2")
                conn.execute("UPDATE sessions SET chat_render_version=1")
            if "chat_render_kind" not in existing_interaction_columns:
                conn.execute("ALTER TABLE interactions ADD COLUMN chat_render_kind TEXT NOT NULL DEFAULT 'activity'")
            # Bootstrap historical rows once. Metadata is consumed only here,
            # then the new strict render column is the sole runtime authority.
            scope = "session_id IN (SELECT id FROM sessions WHERE chat_render_version < 2)"
            conn.execute(f"UPDATE interactions SET chat_render_kind='user' WHERE role='user' AND chat_render_kind='activity' AND {scope}")
            conn.execute(f"UPDATE interactions SET chat_render_kind='promoted_answer' WHERE role='assistant' AND content <> '' AND chat_render_kind='activity' AND {scope} AND (metadata LIKE '%\"closing_promoted\": true%' OR metadata LIKE '%\"message_phase\": \"main\"%' OR metadata LIKE '%\"message_phase\": \"final\"%')")
            conn.execute(f"UPDATE interactions SET chat_render_kind='notice' WHERE role='system' AND source IN ('system:closer','system:summary','system:overview','system:mode','system:model') AND chat_render_kind='activity' AND {scope}")
            conn.execute(f"UPDATE interactions SET chat_render_kind='error' WHERE role='system' AND source LIKE 'system:closing_partial%' AND chat_render_kind='activity' AND {scope}")
            conn.execute("UPDATE sessions SET chat_render_version=2 WHERE chat_render_version < 2")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_interactions_render_v2 ON interactions(session_id, chat_render_kind, session_seq)")
            conn.commit()

            self._conn = conn
            _connections[self._user_id] = conn
            return conn

    def close(self) -> None:
        """Explicitly close this store's connection."""
        close_user_store(self._user_id)

    # ── Sessions ────────────────────────────────────────────────────────

    async def create_session(
        self,
        user_id: str,
        *,
        agent_id: str = "",
        title: str = "",
        session_id: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> str:
        """Create a new session row and return its id."""
        sid = session_id or str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        conn = self._get_conn()
        conn.execute(
            """INSERT INTO sessions (id, user_id, title, agent_id, metadata, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (sid, user_id, title, agent_id, json.dumps(metadata or {}), now, now),
        )
        conn.commit()
        return sid

    async def get_session(self, session_id: str) -> Optional[dict]:
        """Return a session row or None."""
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if not row:
            return None
        return dict(row)

    async def update_session(self, session_id: str, changes: dict) -> bool:
        """Update session fields. Bumps updated_at automatically."""
        if not changes:
            return True
        now = datetime.now(timezone.utc).isoformat()
        sets = ", ".join(f"{k} = ?" for k in changes)
        vals = list(changes.values())
        vals.append(now)
        vals.append(session_id)
        conn = self._get_conn()
        conn.execute(f"UPDATE sessions SET {sets}, updated_at = ? WHERE id = ?", vals)
        conn.commit()
        return conn.total_changes > 0

    async def delete_session(self, session_id: str) -> None:
        """Delete a session and its interactions."""
        conn = self._get_conn()
        conn.execute("DELETE FROM interactions WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        conn.commit()
        try:
            from app.agent.file_checkout import release_session_checkouts
            await release_session_checkouts(session_id, "Session deleted")
        except Exception:
            pass

    async def list_sessions(self, user_id: str, limit: int = 50) -> List[dict]:
        """Return the most recently updated sessions for this user."""
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT * FROM sessions
               WHERE user_id = ? AND status = 'active'
               ORDER BY updated_at DESC
               LIMIT ?""",
            (user_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Interactions ────────────────────────────────────────────────────

    async def insert_interaction(
        self,
        session_id: str,
        *,
        role: str,
        content: str,
        interaction_id: Optional[str] = None,
        parent_id: Optional[str] = None,
        tool_name: Optional[str] = None,
        channel: Optional[str] = None,
        metadata: Optional[dict] = None,
        output: Optional[str] = None,
        status: str = "complete",
        chat_render_kind: Optional[str] = None,
    ) -> str:
        """Insert one interaction row. Returns the new id."""
        iid = interaction_id or str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        conn = self._get_conn()
        conn.execute(
            """INSERT INTO interactions (id, session_id, parent_id, role, content,
               tool_name, channel, metadata, output, chat_render_kind, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                iid, session_id, parent_id, role, content,
                tool_name, channel, json.dumps(metadata or {}), output, (chat_render_kind or ("user" if role == "user" else "activity")), status, now,
            ),
        )
        conn.commit()
        return iid

    async def get_interactions(
        self, session_id: str, limit: int = 200
    ) -> List[dict]:
        """Return interactions for a session, oldest first."""
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT * FROM interactions
               WHERE session_id = ?
               ORDER BY created_at ASC
               LIMIT ?""",
            (session_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Memories ────────────────────────────────────────────────────────

    async def upsert_memory(self, user_id: str, slug: str, memory: dict) -> None:
        """Insert or update a memory row."""
        now = datetime.now(timezone.utc).isoformat()
        conn = self._get_conn()
        conn.execute(
            """INSERT INTO memories (id, user_id, slug, page_type, title,
               compiled_truth, timeline, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id, slug) DO UPDATE SET
                 page_type=excluded.page_type,
                 title=excluded.title,
                 compiled_truth=excluded.compiled_truth,
                 timeline=excluded.timeline,
                 updated_at=excluded.updated_at""",
            (
                str(uuid.uuid4()), user_id, slug,
                memory.get("page_type"), memory.get("title"),
                memory.get("compiled_truth"), memory.get("timeline"),
                now, now,
            ),
        )
        conn.commit()

    async def get_memories(self, user_id: str, limit: int = 100) -> List[dict]:
        """Return all memories for a user, most recently updated first."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM memories WHERE user_id = ? ORDER BY updated_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Bulk / migration ────────────────────────────────────────────────

    async def import_sessions(
        self, sessions: List[dict], interactions: List[dict]
    ) -> int:
        """Atomically import browser sessions and interactions."""
        conn = self._get_conn()
        now = datetime.now(timezone.utc).isoformat()
        try:
            for s in sessions:
                conn.execute(
                    """INSERT INTO sessions
                       (id, user_id, title, metadata, agent_id, participants,
                        sort_order, status, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(id) DO UPDATE SET
                         user_id=excluded.user_id,
                         title=excluded.title,
                         metadata=excluded.metadata,
                         agent_id=excluded.agent_id,
                         participants=excluded.participants,
                         sort_order=excluded.sort_order,
                         status=excluded.status,
                         updated_at=excluded.updated_at""",
                    (
                        s["id"], s["user_id"], s.get("title"),
                        _json_text(s.get("metadata"), "{}"), s.get("agent_id"),
                        _json_text(s.get("participants"), "[]"),
                        s.get("sort_order"), s.get("status", "active"),
                        s.get("created_at") or now, s.get("updated_at") or now,
                    ),
                )

            for ix in interactions:
                conn.execute(
                    """INSERT INTO interactions
                       (id, session_id, parent_id, role, content, tool_name,
                        tool_call_id, channel, metadata, output, source, from_id,
                        to_id, session_seq, turn_id, turn_seq, status, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(id) DO UPDATE SET
                         session_id=excluded.session_id,
                         parent_id=excluded.parent_id,
                         role=excluded.role,
                         content=excluded.content,
                         tool_name=excluded.tool_name,
                         tool_call_id=excluded.tool_call_id,
                         channel=excluded.channel,
                         metadata=excluded.metadata,
                         output=excluded.output,
                         source=excluded.source,
                         from_id=excluded.from_id,
                         to_id=excluded.to_id,
                         session_seq=excluded.session_seq,
                         turn_id=excluded.turn_id,
                         turn_seq=excluded.turn_seq,
                         status=excluded.status""",
                    (
                        ix["id"], ix["session_id"], ix.get("parent_id"),
                        ix["role"], ix["content"], ix.get("tool_name"),
                        ix.get("tool_call_id"), ix.get("channel"),
                        _json_text(ix.get("metadata"), "{}"),
                        ix.get("output"), ix.get("source"),
                        ix.get("from_id"), ix.get("to_id"),
                        ix.get("session_seq"), ix.get("turn_id"),
                        ix.get("turn_seq"), ix.get("status", "complete"),
                        ix.get("created_at") or now,
                    ),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return len(sessions)

    async def apply_sync_mutations(
        self, user_id: str, mutations: List[dict]
    ) -> List[dict]:
        """Apply independent CAS/idempotent browser mutations.

        Each mutation commits or rolls back independently so one conflict never
        hides successful siblings. Callers must mark only ``applied``/``noop``
        results clean.
        """
        conn = self._get_conn()
        from app.db.browser_policy import load_browser_storage_policy
        receipt_days = load_browser_storage_policy().receipt_retention_days
        receipt_cutoff = (
            datetime.now(timezone.utc) - timedelta(days=receipt_days)
        ).isoformat()
        conn.execute(
            "DELETE FROM browser_sync_receipts WHERE created_at < ?",
            (receipt_cutoff,),
        )
        conn.commit()
        results: List[dict] = []
        for mutation in mutations:
            mutation_id = str(mutation.get("mutation_id") or "")
            session_id = str(mutation.get("session_id") or "")
            operation = str(mutation.get("operation") or "upsert")
            request_hash = hashlib.sha256(
                json.dumps(mutation, sort_keys=True, separators=(",", ":"), default=str)
                .encode("utf-8")
            ).hexdigest()
            base_revision = int(mutation.get("base_server_revision") or 0)
            client_revision = int(mutation.get("client_revision") or 0)
            content_hash = str(mutation.get("content_hash") or "")

            if not mutation_id or not session_id:
                results.append({
                    "session_id": session_id,
                    "mutation_id": mutation_id,
                    "status": "rejected",
                    "error": "mutation_id and session_id are required",
                })
                continue

            receipt = conn.execute(
                "SELECT request_hash, result_json FROM browser_sync_receipts "
                "WHERE mutation_id=?",
                (mutation_id,),
            ).fetchone()
            if receipt:
                if receipt["request_hash"] != request_hash:
                    results.append({
                        "session_id": session_id,
                        "mutation_id": mutation_id,
                        "status": "rejected",
                        "error": "idempotency key reused with different payload",
                    })
                else:
                    results.append(json.loads(receipt["result_json"]))
                continue

            try:
                conn.execute("BEGIN")
                current = conn.execute(
                    "SELECT authority_revision, content_hash, deleted_at "
                    "FROM sessions WHERE id=? AND user_id=?",
                    (session_id, user_id),
                ).fetchone()
                current_revision = int(current["authority_revision"] or 0) if current else 0
                current_hash = str(current["content_hash"] or "") if current else ""

                if operation == "delete":
                    if current and current["deleted_at"]:
                        result = {
                            "session_id": session_id,
                            "mutation_id": mutation_id,
                            "status": "noop",
                            "server_revision": current_revision,
                            "content_hash": current_hash,
                            "client_revision": client_revision,
                        }
                    elif current and base_revision != current_revision:
                        result = {
                            "session_id": session_id,
                            "mutation_id": mutation_id,
                            "status": "conflict",
                            "server_revision": current_revision,
                            "content_hash": current_hash,
                            "client_revision": client_revision,
                            "error": "stale base revision",
                        }
                    else:
                        now = datetime.now(timezone.utc).isoformat()
                        next_revision = current_revision + 1
                        if current:
                            conn.execute(
                                "UPDATE sessions SET deleted_at=?, status='deleted', "
                                "authority_revision=?, content_hash='', updated_at=? "
                                "WHERE id=? AND user_id=?",
                                (now, next_revision, now, session_id, user_id),
                            )
                        else:
                            conn.execute(
                                "INSERT INTO sessions "
                                "(id,user_id,status,created_at,updated_at,"
                                "authority_revision,content_hash,deleted_at) "
                                "VALUES (?,?,'deleted',?,?,?,?,?)",
                                (
                                    session_id, user_id, now, now,
                                    next_revision, "", now,
                                ),
                            )
                        conn.execute(
                            "DELETE FROM interactions WHERE session_id=?",
                            (session_id,),
                        )
                        result = {
                            "session_id": session_id,
                            "mutation_id": mutation_id,
                            "status": "applied",
                            "server_revision": next_revision,
                            "content_hash": "",
                            "client_revision": client_revision,
                        }
                elif operation == "upsert":
                    session = dict(mutation.get("session") or {})
                    interactions = list(mutation.get("interactions") or [])
                    if current and current["deleted_at"]:
                        result = {
                            "session_id": session_id,
                            "mutation_id": mutation_id,
                            "status": "conflict",
                            "server_revision": current_revision,
                            "content_hash": current_hash,
                            "client_revision": client_revision,
                            "error": "server tombstone prevents resurrection",
                        }
                    elif current and base_revision != current_revision:
                        status = "noop" if content_hash and content_hash == current_hash else "conflict"
                        result = {
                            "session_id": session_id,
                            "mutation_id": mutation_id,
                            "status": status,
                            "server_revision": current_revision,
                            "content_hash": current_hash,
                            "client_revision": client_revision,
                        }
                        if status == "conflict":
                            result["error"] = "stale base revision"
                    elif not current and base_revision != 0:
                        result = {
                            "session_id": session_id,
                            "mutation_id": mutation_id,
                            "status": "conflict",
                            "server_revision": 0,
                            "content_hash": "",
                            "client_revision": client_revision,
                            "error": "server session does not exist",
                        }
                    else:
                        next_revision = current_revision + 1
                        now = datetime.now(timezone.utc).isoformat()
                        conn.execute(
                            """INSERT INTO sessions
                               (id,user_id,title,metadata,agent_id,participants,
                                sort_order,status,created_at,updated_at,
                                authority_revision,content_hash,deleted_at)
                               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,NULL)
                               ON CONFLICT(id) DO UPDATE SET
                                 user_id=excluded.user_id,
                                 title=excluded.title,
                                 metadata=excluded.metadata,
                                 agent_id=excluded.agent_id,
                                 participants=excluded.participants,
                                 sort_order=excluded.sort_order,
                                 status=excluded.status,
                                 updated_at=excluded.updated_at,
                                 authority_revision=excluded.authority_revision,
                                 content_hash=excluded.content_hash,
                                 deleted_at=NULL""",
                            (
                                session_id, user_id, session.get("title"),
                                _json_text(session.get("metadata"), "{}"),
                                session.get("agent_id"),
                                _json_text(session.get("participants"), "[]"),
                                session.get("sort_order"),
                                session.get("status", "active"),
                                session.get("created_at") or now,
                                session.get("updated_at") or now,
                                next_revision, content_hash,
                            ),
                        )
                        conn.execute(
                            "DELETE FROM interactions WHERE session_id=?",
                            (session_id,),
                        )
                        for ix in interactions:
                            conn.execute(
                                """INSERT INTO interactions
                                   (id,session_id,parent_id,role,content,tool_name,
                                    tool_call_id,channel,metadata,output,source,
                                    from_id,to_id,session_seq,turn_id,turn_seq,
                                    status,created_at)
                                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                (
                                    ix["id"], session_id, ix.get("parent_id"),
                                    ix["role"], ix.get("content", ""),
                                    ix.get("tool_name"), ix.get("tool_call_id"),
                                    ix.get("channel"),
                                    _json_text(ix.get("metadata"), "{}"),
                                    _json_text(ix.get("output"), "") if isinstance(ix.get("output"), (dict, list)) else ix.get("output"),
                                    ix.get("source"), ix.get("from_id"),
                                    ix.get("to_id"), ix.get("session_seq"),
                                    ix.get("turn_id"), ix.get("turn_seq"),
                                    ix.get("status", "complete"),
                                    ix.get("created_at") or now,
                                ),
                            )
                        result = {
                            "session_id": session_id,
                            "mutation_id": mutation_id,
                            "status": "applied",
                            "server_revision": next_revision,
                            "content_hash": content_hash,
                            "client_revision": client_revision,
                        }
                else:
                    result = {
                        "session_id": session_id,
                        "mutation_id": mutation_id,
                        "status": "rejected",
                        "error": f"unsupported operation: {operation}",
                        "client_revision": client_revision,
                    }

                conn.execute(
                    "INSERT INTO browser_sync_receipts "
                    "(mutation_id,user_id,session_id,request_hash,result_json,created_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (
                        mutation_id, user_id, session_id, request_hash,
                        json.dumps(result, sort_keys=True),
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                conn.commit()
                results.append(result)
            except Exception as exc:
                conn.rollback()
                results.append({
                    "session_id": session_id,
                    "mutation_id": mutation_id,
                    "status": "rejected",
                    "client_revision": client_revision,
                    "error": str(exc)[:300],
                })
        return results

    async def stats(self) -> dict:
        """Return row counts for diagnostics."""
        conn = self._get_conn()
        tables = ["sessions", "interactions", "session_runs", "attachments", "memories"]
        out = {}
        for t in tables:
            try:
                n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                out[t] = n
            except Exception:
                out[t] = -1
        return out
