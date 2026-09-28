"""Per-agent SQLite authority below ``data/agent_data/{agent_id}/``.

Each file is authoritative for its agent's config, abilities, prompts,
connections, policies, and data-source bindings. ``sync_from_global`` is a
migration-only helper.

Usage::

    from app.db.agent_store import get_agent_store
    store = get_agent_store(agent_id)
    synced = await store.sync_from_global()
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

from app.agent_workspace import (
    agent_db_path,
    purge_agent_home,
    subagent_db_path,
    purge_subagent_home,
)
from app.db.schema import ensure_sqlite_plane_columns, render_plane

# ── Schema ─────────────────────────────────────────────────────────────────

_TOOL_MODES_KEY = "tool_modes"
_ABILITY_MODES_KEY = "ability_modes"
_ABILITY_ACCESS_KEY = "ability_access"
_SKILL_MODES_KEY = "skill_modes"

AGENT_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS agent_authority (
    agent_id TEXT PRIMARY KEY,
    name TEXT,
    description TEXT,
    template_id TEXT,
    model TEXT,
    provider TEXT,
    temperature REAL,
    max_tokens INTEGER,
    max_turn_count INTEGER,
    max_wall_seconds REAL,
    max_identical_tool_calls INTEGER,
    max_stall_strikes INTEGER,
    status TEXT,
    default_execution_mode TEXT,
    allowed_tools TEXT,
    custom_tool_ids TEXT,
    safety_policy TEXT,
    admin_users TEXT,
    member_users TEXT,
    authorized_users TEXT,
    icon TEXT,
    trigger_type TEXT,
    trigger_key TEXT,
    loop_logic TEXT,
    -- Self-healing is opt-out: manual-only recovery must be explicitly requested.
    auto_resume INTEGER NOT NULL DEFAULT 1,
    is_admin_agent INTEGER,
    sort_order INTEGER,
    metadata TEXT,
    created_at TEXT,
    updated_at TEXT,
    synced_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS agent_abilities (
    ability_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    source TEXT,
    byo_client_id TEXT,
    byo_client_secret_ref TEXT,
    config TEXT,
    created_at TEXT,
    updated_at TEXT,
    PRIMARY KEY (agent_id, ability_id)
);

CREATE TABLE IF NOT EXISTS agent_prompts (
    slot_name TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    content TEXT NOT NULL,
    order_index INTEGER,
    lock INTEGER,
    merge_mode TEXT,
    template_version INTEGER,
    updated_at TEXT,
    PRIMARY KEY (agent_id, slot_name)
);

CREATE TABLE IF NOT EXISTS agent_tool_modes (
    agent_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    mode TEXT NOT NULL,
    PRIMARY KEY (agent_id, tool_name)
);

CREATE TABLE IF NOT EXISTS agent_ability_modes (
    agent_id TEXT NOT NULL,
    ability_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    PRIMARY KEY (agent_id, ability_id)
);

CREATE TABLE IF NOT EXISTS agent_ability_access (
    agent_id TEXT NOT NULL,
    ability_id TEXT NOT NULL,
    access_level TEXT NOT NULL,
    PRIMARY KEY (agent_id, ability_id)
);

CREATE TABLE IF NOT EXISTS agent_skill_modes (
    agent_id TEXT NOT NULL,
    ability_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    PRIMARY KEY (agent_id, ability_id)
);

CREATE TABLE IF NOT EXISTS agent_connections (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    connection_type TEXT NOT NULL,
    section TEXT NOT NULL DEFAULT 'channel',
    enabled INTEGER NOT NULL DEFAULT 0,
    config TEXT NOT NULL DEFAULT '{}',
    created_at TEXT,
    updated_at TEXT,
    UNIQUE(agent_id, connection_type)
);

CREATE TABLE IF NOT EXISTS agent_soft_abilities (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    slug TEXT NOT NULL,
    display_name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    icon TEXT NOT NULL DEFAULT 'sparkles',
    enabled INTEGER NOT NULL DEFAULT 1,
    skill_summary TEXT NOT NULL DEFAULT '',
    skill_body TEXT NOT NULL DEFAULT '',
    workflow TEXT NOT NULL DEFAULT '{}',
    allowed_tools TEXT NOT NULL DEFAULT '[]',
    credential_schema TEXT NOT NULL DEFAULT '[]',
    policy TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'draft',
    version INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL,
    created_at TEXT,
    updated_at TEXT,
    UNIQUE(agent_id, slug)
);

CREATE TABLE IF NOT EXISTS agent_data_sources (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    data_source_id TEXT NOT NULL,
    tool_alias TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    inject_schema_in_prompt INTEGER NOT NULL DEFAULT 1,
    created_at TEXT,
    UNIQUE(agent_id, data_source_id)
);

CREATE TABLE IF NOT EXISTS sync_info (
    agent_id TEXT PRIMARY KEY,
    last_synced_at TEXT NOT NULL DEFAULT (datetime('now')),
    global_updated_at TEXT
);
"""

# Authority columns in global.db agents table — every column we mirror.
_AUTHORITY_COLS = [
    "agent_id",
    "name",
    "description",
    "template_id",
    "model",
    "provider",
    "temperature",
    "max_tokens",
    "max_turn_count",
    "max_wall_seconds",
    "max_identical_tool_calls",
    "max_stall_strikes",
    "status",
    "default_execution_mode",
    "allowed_tools",
    "custom_tool_ids",
    "safety_policy",
    "admin_users",
    "member_users",
    "authorized_users",
    "icon",
    "trigger_type",
    "trigger_key",
    "loop_logic",
    "auto_resume",
    "is_admin_agent",
    "sort_order",
    "metadata",
    "created_at",
    "updated_at",
    "synced_at",
]

# Map from global.db agents column name → per-agent column name.
# synced_at is our own, not from global.
_AUTHORITY_COL_MAP = {c: c for c in _AUTHORITY_COLS if c != "synced_at"}


# ── Connection pool ────────────────────────────────────────────────────────

_connections: Dict[str, sqlite3.Connection] = {}
_conn_lock = threading.Lock()


def get_agent_store(agent_id: str, parent_id: Optional[str] = None) -> "AgentStore":
    """Return (or create) an AgentStore for ``agent_id``.

    ``parent_id`` scopes the store to a subagent: the DB file lives nested
    under the parent agent's home at ``data/agent_data/<parent>/subagents/``
    (used for spawned clones), instead of at the top level."""
    return AgentStore(agent_id, parent_id=parent_id)


def close_agent_store(agent_id: str) -> None:
    """Close the connection for an agent id."""
    with _conn_lock:
        conn = _connections.pop(agent_id, None)
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def close_all() -> None:
    """Close every open agent connection."""
    with _conn_lock:
        for aid, conn in list(_connections.items()):
            try:
                conn.close()
            except Exception:
                pass
        _connections.clear()


class AgentStore:
    """Per-agent SQLite store backed by ``data/agent_data/{agent_id}.db``
    (or ``data/agent_data/<parent>/subagents/{agent_id}.db`` for subagents)."""

    def __init__(self, agent_id: str, parent_id: Optional[str] = None):
        self._agent_id = agent_id
        self._parent_id = parent_id or None
        if self._parent_id:
            self._db_path = str(subagent_db_path(self._parent_id, agent_id))
        else:
            self._db_path = str(agent_db_path(agent_id))
        self._conn: Optional[sqlite3.Connection] = None

    # ── Connection management ───────────────────────────────────────────

    def _get_conn(self) -> sqlite3.Connection:
        """Get (or open + init) this agent's SQLite connection."""
        if self._conn is not None:
            return self._conn

        with _conn_lock:
            if self._conn is not None:
                return self._conn
            if self._agent_id in _connections:
                self._conn = _connections[self._agent_id]
                return self._conn

            os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
            conn = sqlite3.connect(self._db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=15000")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.executescript(AGENT_SCHEMA_SQL)
            # Canonical v2 agent tables. Early per-agent mirrors used compact
            # prompt/ability tables with incompatible primary keys; keep those
            # temporarily under a migration-only name while the canonical
            # tables are repopulated from the frozen legacy source.
            for table, required in (
                ("agent_prompts", {"id", "user_id", "updated_by"}),
                ("agent_abilities", {"id"}),
            ):
                columns = {
                    row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
                }
                if columns and not required.issubset(columns):
                    conn.execute(f"DROP TABLE IF EXISTS _v1_{table}")
                    conn.execute(f"ALTER TABLE {table} RENAME TO _v1_{table}")
            conn.executescript(render_plane("agent", "sqlite"))
            ensure_sqlite_plane_columns(conn, "agent")
            conn.commit()

            self._conn = conn
            _connections[self._agent_id] = conn
            return conn

    def close(self) -> None:
        """Explicitly close this store's connection."""
        close_agent_store(self._agent_id)

    def purge(self) -> bool:
        """Close the connection and delete the agent's data directory."""
        close_agent_store(self._agent_id)
        if self._parent_id:
            return purge_subagent_home(self._parent_id, self._agent_id)
        return purge_agent_home(self._agent_id)

    # ── Query helpers ──────────────────────────────────────────────────

    async def get_authority(self) -> Optional[dict]:
        """Return the full authority row for this agent."""
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM agents WHERE id = ?", (self._agent_id,)).fetchone()
        return dict(row) if row else None

    async def list_abilities(self) -> List[dict]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM agent_abilities WHERE agent_id = ? AND enabled = 1",
            (self._agent_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    async def list_prompts(self) -> List[dict]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM agent_prompts WHERE agent_id = ? ORDER BY order_index",
            (self._agent_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    async def list_tool_modes(self) -> Dict[str, str]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT tool_name, mode FROM agent_tool_modes WHERE agent_id = ?",
            (self._agent_id,),
        ).fetchall()
        return {r["tool_name"]: r["mode"] for r in rows}

    async def list_ability_modes(self) -> Dict[str, str]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT ability_id, mode FROM agent_ability_modes WHERE agent_id = ?",
            (self._agent_id,),
        ).fetchall()
        return {r["ability_id"]: r["mode"] for r in rows}

    async def list_ability_access(self) -> Dict[str, str]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT ability_id, access_level FROM agent_ability_access WHERE agent_id = ?",
            (self._agent_id,),
        ).fetchall()
        return {r["ability_id"]: r["access_level"] for r in rows}

    async def list_skill_modes(self) -> Dict[str, str]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT ability_id, mode FROM agent_skill_modes WHERE agent_id = ?",
            (self._agent_id,),
        ).fetchall()
        return {r["ability_id"]: r["mode"] for r in rows}

    async def list_connections(self) -> List[dict]:
        rows = self._get_conn().execute(
            "SELECT * FROM agent_connections WHERE agent_id=? ORDER BY section, connection_type",
            (self._agent_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    async def list_soft_abilities(self, *, enabled_only: bool = False) -> List[dict]:
        sql = "SELECT * FROM agent_soft_abilities WHERE agent_id=?"
        params: list[Any] = [self._agent_id]
        if enabled_only:
            sql += " AND enabled=1 AND status='ready'"
        sql += " ORDER BY LOWER(display_name)"
        return [dict(row) for row in self._get_conn().execute(sql, params).fetchall()]

    async def list_data_source_bindings(self, *, enabled_only: bool = False) -> List[dict]:
        sql = "SELECT * FROM agent_data_sources WHERE agent_id=?"
        params: list[Any] = [self._agent_id]
        if enabled_only:
            sql += " AND enabled=1"
        sql += " ORDER BY created_at"
        return [dict(row) for row in self._get_conn().execute(sql, params).fetchall()]

    def _project_catalog(self, agent: dict) -> None:
        """Refresh the small app.db discovery/ACL projection when it exists."""
        try:
            from app.db.storage_layout import APP_DB_PATH, get_app_store

            if not APP_DB_PATH.exists():
                return
            admin_users = agent.get("admin_users") or "[]"
            owner_user_id = None
            try:
                parsed = json.loads(admin_users) if isinstance(admin_users, str) else admin_users
                if isinstance(parsed, list) and parsed:
                    owner_user_id = str(parsed[0])
            except Exception:
                pass
            store = get_app_store()
            with store.connection() as conn:
                conn.execute(
                    """INSERT INTO agent_catalog
                       (agent_id,name,icon,status,template_id,owner_user_id,admin_users,
                        member_users,authorized_users,storage_ref,authority_revision,
                        created_at,updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,1,?,?)
                       ON CONFLICT(agent_id) DO UPDATE SET
                         name=excluded.name, icon=excluded.icon, status=excluded.status,
                         template_id=excluded.template_id, owner_user_id=excluded.owner_user_id,
                         admin_users=excluded.admin_users, member_users=excluded.member_users,
                         authorized_users=excluded.authorized_users,
                         storage_ref=excluded.storage_ref,
                         authority_revision=agent_catalog.authority_revision+1,
                         updated_at=excluded.updated_at""",
                    (
                        self._agent_id,
                        agent.get("name") or "",
                        agent.get("icon"),
                        agent.get("status") or "active",
                        agent.get("template_id"),
                        owner_user_id,
                        admin_users,
                        agent.get("member_users") or "[]",
                        agent.get("authorized_users") or "[]",
                        str(self._db_path),
                        agent.get("created_at"),
                        agent.get("updated_at"),
                    ),
                )
                conn.commit()
        except Exception as exc:
            logger.debug("Could not update app agent catalog for %s: %s", self._agent_id, exc)

    # ── Sync from global authority ──────────────────────────────────────

    async def sync_from_global(self) -> bool:
        """Pull this agent's full authority from global.db and write it into the
        per-agent store in one transaction.

        Returns True if the agent was found and synced, False if it doesn't
        exist in global.db (the per-agent store is left untouched).
        """
        from app.db.global_store import get_global_store

        gs = get_global_store()
        gconn = gs._conn

        # ── 1. Read the agent row from global.db ──
        agent_row = gconn.execute(
            "SELECT * FROM agents WHERE id = ?", (self._agent_id,)
        ).fetchone()
        if not agent_row:
            logger.debug(
                "Agent %s not found in global.db — skipping sync", self._agent_id
            )
            return False

        agent = dict(agent_row)

        # ── 2. Read abilities ──
        ability_rows = gconn.execute(
            "SELECT * FROM agent_abilities WHERE agent_id = ?",
            (self._agent_id,),
        ).fetchall()

        # ── 3. Read every prompt, including user-specific overrides ──
        prompt_rows = gconn.execute(
            "SELECT * FROM agent_prompts WHERE agent_id = ?",
            (self._agent_id,),
        ).fetchall()

        # ── 4. Parse metadata for mode maps ──
        meta = {}
        raw_meta = agent.get("metadata")
        if raw_meta:
            try:
                meta = json.loads(raw_meta) if isinstance(raw_meta, str) else raw_meta
            except (json.JSONDecodeError, TypeError):
                meta = {}
        if not isinstance(meta, dict):
            meta = {}

        tool_modes = meta.get(_TOOL_MODES_KEY) or {}
        ability_modes = meta.get(_ABILITY_MODES_KEY) or {}
        ability_access = meta.get(_ABILITY_ACCESS_KEY) or {}
        skill_modes = meta.get(_SKILL_MODES_KEY) or {}

        now = datetime.now(timezone.utc).isoformat()

        conn = self._get_conn()
        try:
            # Canonical row used directly by the runtime after cutover.
            agent_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(agents)").fetchall()
            }
            canonical_agent = {name: value for name, value in agent.items() if name in agent_columns}
            conn.execute(
                f"INSERT OR REPLACE INTO agents ({', '.join(canonical_agent)}) "
                f"VALUES ({', '.join('?' for _ in canonical_agent)})",
                tuple(canonical_agent.values()),
            )

            # ── agent_authority ──
            authority_values = [
                agent.get("id"),
                agent.get("name"),
                agent.get("description"),
                agent.get("template_id"),
                agent.get("model"),
                agent.get("provider"),
                agent.get("temperature"),
                agent.get("max_tokens"),
                agent.get("max_turn_count"),
                agent.get("max_wall_seconds"),
                agent.get("max_identical_tool_calls"),
                agent.get("max_stall_strikes"),
                agent.get("status"),
                agent.get("default_execution_mode"),
                agent.get("allowed_tools"),
                agent.get("custom_tool_ids"),
                agent.get("safety_policy"),
                agent.get("admin_users"),
                agent.get("member_users"),
                agent.get("authorized_users"),
                agent.get("icon"),
                agent.get("trigger_type"),
                agent.get("trigger_key"),
                agent.get("loop_logic"),
                agent.get("auto_resume"),
                agent.get("is_admin_agent"),
                agent.get("sort_order"),
                agent.get("metadata"),
                agent.get("created_at"),
                agent.get("updated_at"),
                now,  # synced_at
            ]
            conn.execute(
                f"""INSERT OR REPLACE INTO agent_authority ({', '.join(_AUTHORITY_COLS)})
                    VALUES ({', '.join('?' * len(_AUTHORITY_COLS))})""",
                authority_values,
            )

            # ── canonical agent_abilities ──
            conn.execute(
                "DELETE FROM agent_abilities WHERE agent_id = ?", (self._agent_id,)
            )
            for ab in ability_rows:
                a = dict(ab)
                target_columns = {
                    row[1] for row in conn.execute("PRAGMA table_info(agent_abilities)").fetchall()
                }
                cols = [name for name in a if name in target_columns]
                conn.execute(
                    f"INSERT OR REPLACE INTO agent_abilities ({', '.join(cols)}) "
                    f"VALUES ({', '.join('?' for _ in cols)})",
                    tuple(a[name] for name in cols),
                )

            # ── canonical agent_prompts ──
            conn.execute(
                "DELETE FROM agent_prompts WHERE agent_id = ?", (self._agent_id,)
            )
            for pr in prompt_rows:
                p = dict(pr)
                target_columns = {
                    row[1] for row in conn.execute("PRAGMA table_info(agent_prompts)").fetchall()
                }
                cols = [name for name in p if name in target_columns]
                conn.execute(
                    f"INSERT OR REPLACE INTO agent_prompts ({', '.join(cols)}) "
                    f"VALUES ({', '.join('?' for _ in cols)})",
                    tuple(p[name] for name in cols),
                )

            # ── agent_tool_modes ──
            conn.execute(
                "DELETE FROM agent_tool_modes WHERE agent_id = ?", (self._agent_id,)
            )
            if isinstance(tool_modes, dict):
                for tool_name, mode in tool_modes.items():
                    if isinstance(tool_name, str) and isinstance(mode, str):
                        conn.execute(
                            "INSERT OR REPLACE INTO agent_tool_modes (agent_id, tool_name, mode) VALUES (?, ?, ?)",
                            (self._agent_id, tool_name, mode),
                        )

            # ── agent_ability_modes ──
            conn.execute(
                "DELETE FROM agent_ability_modes WHERE agent_id = ?",
                (self._agent_id,),
            )
            if isinstance(ability_modes, dict):
                for ability_id, mode in ability_modes.items():
                    if isinstance(ability_id, str) and isinstance(mode, str):
                        conn.execute(
                            "INSERT OR REPLACE INTO agent_ability_modes (agent_id, ability_id, mode) VALUES (?, ?, ?)",
                            (self._agent_id, ability_id, mode),
                        )

            # ── agent_ability_access ──
            conn.execute(
                "DELETE FROM agent_ability_access WHERE agent_id = ?",
                (self._agent_id,),
            )
            if isinstance(ability_access, dict):
                for ability_id, access_level in ability_access.items():
                    if isinstance(ability_id, str) and isinstance(access_level, str):
                        conn.execute(
                            "INSERT OR REPLACE INTO agent_ability_access (agent_id, ability_id, access_level) VALUES (?, ?, ?)",
                            (self._agent_id, ability_id, access_level),
                        )

            # ── agent_skill_modes ──
            conn.execute(
                "DELETE FROM agent_skill_modes WHERE agent_id = ?", (self._agent_id,)
            )
            if isinstance(skill_modes, dict):
                for ability_id, mode in skill_modes.items():
                    if isinstance(ability_id, str) and isinstance(mode, str):
                        conn.execute(
                            "INSERT OR REPLACE INTO agent_skill_modes (agent_id, ability_id, mode) VALUES (?, ?, ?)",
                            (self._agent_id, ability_id, mode),
                        )

            # ── sync_info ──
            conn.execute(
                """INSERT OR REPLACE INTO sync_info (agent_id, last_synced_at, global_updated_at)
                   VALUES (?, ?, ?)""",
                (self._agent_id, now, agent.get("updated_at")),
            )

            conn.execute("DROP TABLE IF EXISTS _v1_agent_prompts")
            conn.execute("DROP TABLE IF EXISTS _v1_agent_abilities")

            conn.commit()
            self._project_catalog(agent)
            logger.info("Synced agent %s authority from global.db", self._agent_id)
            return True
        except Exception:
            conn.rollback()
            raise


# ── Bulk migration ─────────────────────────────────────────────────────────

async def migrate_all_agent_stores() -> dict:
    """One-time / startup: create per-agent stores for every active agent in
    global.db. Idempotent — syncing an already-populated store just overwrites
    it with current data, so this is safe to call on every boot.

    Clone agents (status='clone', ephemeral subagents) are NOT migrated into
    top-level stores: their stores are nested under the parent agent's home and
    created on spawn (see create_clone_agent). Any legacy top-level clone
    directory left over from before nesting is relocated under its parent (or
    purged when the parent no longer exists)."""
    from app.db.global_store import get_global_store

    gs = get_global_store()
    try:
        rows = gs._conn.execute(
            "SELECT id, status, metadata FROM agents"
        ).fetchall()
    except Exception as e:
        logger.warning("Cannot read agents from global.db for migration: %s", e)
        return {"migrated": 0, "error": str(e)}

    created = 0
    skipped = 0
    failed = 0
    relocated = 0
    for row in rows:
        agent_id = row["id"]
        status = row["status"] or ""
        parent_id = None
        if status == "clone":
            # Relocate any legacy top-level home under its parent, or purge if
            # the parent is gone. Clones are ephemeral — no top-level store.
            try:
                raw_meta = row["metadata"] or ""
                meta = json.loads(raw_meta) if raw_meta else {}
                parent_id = meta.get("clone_of") if isinstance(meta, dict) else None
                from app.agent_workspace import relocate_legacy_clone_home
                if parent_id and relocate_legacy_clone_home(agent_id, parent_id):
                    relocated += 1
            except Exception as e:
                logger.warning("Clone home cleanup failed for %s: %s", agent_id, e)
        try:
            store = AgentStore(agent_id, parent_id=parent_id)
            synced = await store.sync_from_global()
            if synced:
                created += 1
            else:
                skipped += 1
        except Exception as e:
            logger.warning(
                "Failed to sync agent store for %s: %s", agent_id, e
            )
            skipped += 1
            failed += 1

    logger.info(
        "Agent store migration: %d synced, %d skipped (%d clone homes relocated/purged)",
        created, skipped, relocated,
    )
    return {
        "migrated": created,
        "skipped": skipped,
        "failed": failed,
        "expected_non_clone": len(rows),
        "clones_relocated": relocated,
    }
