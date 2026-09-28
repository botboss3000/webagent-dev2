"""Read-only adapter for the retired ``data/db/global.db`` migration source.

Runtime code uses app.db and per-agent authorities. This module exists only for
the one-shot v2 migration and refuses to create or modify the retired file.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
GLOBAL_DB_PATH = os.path.join(_PROJECT_ROOT, "data", "db", "global.db")

# ── Global shared tables ────────────────────────────────────────────────────
# These are the agent-config / shared-infra tables from SCHEMA_SQL.
# Keep in sync with app/db/local.py.

GLOBAL_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    template_id TEXT,
    name TEXT,
    description TEXT,
    is_user_default INTEGER,
    max_turn_count INTEGER,
    model TEXT,
    provider TEXT,
    temperature REAL,
    max_tokens INTEGER,
    status TEXT,
    metadata TEXT,
    trigger_type TEXT,
    trigger_key TEXT,
    loop_logic TEXT,
    assigned_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    turn_count INTEGER,
    admin_users TEXT,
    member_users TEXT,
    user_mode TEXT,
    sort_order INTEGER,
    -- Self-healing is opt-out: manual-only recovery must be explicitly requested.
    auto_resume INTEGER NOT NULL DEFAULT 1,
    max_wall_seconds REAL,
    max_identical_tool_calls INTEGER,
    max_stall_strikes INTEGER,
    allowed_tools TEXT,
    custom_tool_ids TEXT,
    safety_policy TEXT,
    is_admin_agent INTEGER,
    authorized_users TEXT,
    default_execution_mode TEXT DEFAULT 'ask',
    icon TEXT,
    current_context_id TEXT
);

CREATE TABLE IF NOT EXISTS agent_prompts (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    slot_name TEXT NOT NULL,
    user_id TEXT,
    order_index INTEGER,
    lock INTEGER,
    merge_mode TEXT,
    content TEXT NOT NULL,
    template_version INTEGER,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_by TEXT,
    UNIQUE (agent_id, slot_name)
);

CREATE TABLE IF NOT EXISTS agent_prompt_templates (
    id TEXT PRIMARY KEY,
    template_id TEXT NOT NULL,
    slot_name TEXT NOT NULL,
    order_index INTEGER,
    lock INTEGER,
    merge_mode TEXT,
    content TEXT NOT NULL,
    version INTEGER,
    source TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_by TEXT,
    UNIQUE (template_id, slot_name)
);

CREATE TABLE IF NOT EXISTS agent_templates (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    max_turn_count INTEGER,
    model TEXT,
    provider TEXT,
    temperature REAL,
    max_tokens INTEGER,
    metadata TEXT,
    trigger_type TEXT,
    trigger_key TEXT,
    loop_logic TEXT,
    max_wall_seconds REAL,
    max_identical_tool_calls INTEGER,
    max_stall_strikes INTEGER,
    icon TEXT,
    can_be_default INTEGER,
    is_system INTEGER,
    is_pipeline INTEGER,
    access_level TEXT,
    trigger_description TEXT,
    discoverable INTEGER,
    is_admin_agent INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS agent_abilities (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    ability_id TEXT NOT NULL,
    source TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    byo_client_id TEXT,
    byo_client_secret_ref TEXT,
    config TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (agent_id, ability_id)
);

CREATE TABLE IF NOT EXISTS tools (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    code TEXT,
    description TEXT,
    parameters TEXT,
    language TEXT,
    status TEXT,
    created_by TEXT,
    stages TEXT,
    destructive INTEGER,
    agent_types TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    requires_confirmation INTEGER
);

CREATE TABLE IF NOT EXISTS skills (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    code TEXT,
    version INTEGER,
    base_skill_id TEXT,
    is_official INTEGER,
    tags TEXT,
    is_active INTEGER,
    user_id TEXT,
    mode TEXT NOT NULL DEFAULT 'selectable',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS app_meta (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# ── Singleton connection ────────────────────────────────────────────────────
_global_conn: Optional[sqlite3.Connection] = None
_global_lock = threading.Lock()


def get_global_store() -> "GlobalStore":
    """Open the retired migration source without ever creating it."""
    return GlobalStore()


class GlobalStore:
    """Read-optimised store for the shared ``global.db``."""

    def __init__(self):
        global _global_conn
        with _global_lock:
            if _global_conn is None:
                if not os.path.isfile(GLOBAL_DB_PATH):
                    raise FileNotFoundError(
                        "global.db was retired by storage layout v2; this reader is migration-only"
                    )
                uri = f"file:{Path(GLOBAL_DB_PATH).as_posix()}?mode=ro"
                conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA busy_timeout=15000")
                conn.execute("PRAGMA foreign_keys=ON")
                _global_conn = conn
                logger.info("Legacy global migration source opened read-only at %s", GLOBAL_DB_PATH)
        self._conn = _global_conn

    # ── Agents ──────────────────────────────────────────────────────────

    async def get_agent(self, agent_id: str) -> Optional[dict]:
        conn = self._conn
        row = conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
        return dict(row) if row else None

    async def list_agents(self) -> List[dict]:
        conn = self._conn
        rows = conn.execute(
            "SELECT * FROM agents ORDER BY updated_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    async def get_agent_prompts(self, agent_id: str) -> List[dict]:
        conn = self._conn
        rows = conn.execute(
            "SELECT * FROM agent_prompts WHERE agent_id = ? ORDER BY order_index",
            (agent_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    async def get_agent_abilities(self, agent_id: str) -> List[dict]:
        conn = self._conn
        rows = conn.execute(
            "SELECT * FROM agent_abilities WHERE agent_id = ? AND enabled = 1",
            (agent_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    async def get_template(self, template_id: str) -> Optional[dict]:
        conn = self._conn
        row = conn.execute(
            "SELECT * FROM agent_templates WHERE id = ?", (template_id,)
        ).fetchone()
        return dict(row) if row else None

    async def get_tools(self) -> List[dict]:
        conn = self._conn
        rows = conn.execute("SELECT * FROM tools ORDER BY name").fetchall()
        return [dict(r) for r in rows]

    async def get_skills(self, agent_id: Optional[str] = None) -> List[dict]:
        conn = self._conn
        if agent_id:
            rows = conn.execute(
                "SELECT * FROM skills WHERE agent_id = ? OR agent_id IS NULL ORDER BY name",
                (agent_id,),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM skills ORDER BY name").fetchall()
        return [dict(r) for r in rows]

    # ── Bulk import (migration tool) ────────────────────────────────────

    async def import_agents(self, agents: List[dict]):
        conn = self._conn
        for a in agents:
            try:
                conn.execute(
                    """INSERT OR REPLACE INTO agents
                       (id, name, description, user_id, admin_users, model,
                        temperature, max_tokens, allowed_tools, template_id,
                        default_execution_mode, icon, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        a["id"], a.get("name"), a.get("description"),
                        a.get("user_id"), a.get("admin_users"), a.get("model"),
                        a.get("temperature"), a.get("max_tokens"),
                        a.get("allowed_tools"), a.get("template_id"),
                        a.get("default_execution_mode", "ask"), a.get("icon"),
                        a.get("created_at"), a.get("updated_at"),
                    ),
                )
            except Exception as e:
                logger.warning("Import agent %s: %s", a.get("id"), e)
        conn.commit()

    async def import_prompts(self, prompts: List[dict]):
        conn = self._conn
        for p in prompts:
            try:
                conn.execute(
                    """INSERT OR REPLACE INTO agent_prompts
                       (agent_id, slot_name, content, display_order, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        p["agent_id"], p["slot_name"], p.get("content", ""),
                        p.get("display_order", 0),
                        p.get("created_at"), p.get("updated_at"),
                    ),
                )
            except Exception as e:
                logger.warning("Import prompt %s/%s: %s", p.get("agent_id"), p.get("slot_name"), e)
        conn.commit()

    async def import_tools(self, tools: List[dict]):
        conn = self._conn
        for t in tools:
            try:
                conn.execute(
                    """INSERT OR REPLACE INTO tools
                       (name, description, parameters, provider, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        t["name"], t.get("description"), t.get("parameters"),
                        t.get("provider"), t.get("created_at"), t.get("updated_at"),
                    ),
                )
            except Exception as e:
                logger.warning("Import tool %s: %s", t.get("name"), e)
        conn.commit()

    async def stats(self) -> dict:
        conn = self._conn
        tables = ["agents", "agent_prompts", "agent_templates", "tools", "skills"]
        out = {}
        for t in tables:
            try:
                n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                out[t] = n
            except Exception:
                out[t] = -1
        return out
