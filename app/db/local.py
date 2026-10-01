"""
Local SQLite storage backend for WebAgent.

Completely self-contained. Uses a local SQLite database file.
Auto-creates tables on first use matching the canonical schema.
"""

import asyncio
import contextvars
import json
import logging
import os
import re
import sqlite3
import struct
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from app.db.turn_cache import turn_cached, turn_scope_active, turn_cache_invalidate
from app.runtime_mode import data_root
from app.db.session_activity import (
    activity_summary_for_interaction,
    ensure_session_activity_summaries,
    project_interaction_activity,
)

# numpy is a heavy import (~0.8s and hundreds of files to scan on a cold
# disk) but is ONLY needed for local vector search. Load it lazily on first
# use so it never slows down server startup / the agents page load.
np = None


def _ensure_np():
    """Import numpy on first vector-search use, caching it as the module global."""
    global np
    if np is None:
        import numpy as _numpy
        np = _numpy
    return np

from app.models.schemas import InteractionRecord
from app.db.interface import StorageBackend
from app.agent.embed import embed_text, embed_dim, embed_model_name

logger = logging.getLogger(__name__)

# All runtime databases live under data/db/ — `data/` is the app's stored state
# (alongside data/config, data/agents, data/uploads, data/visuals); `app/` is
# logic only. The DB *code* stays in app/db/; the DB *files* live in data/db/.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB_DIR = str(data_root() / "db")
DEFAULT_DB_PATH = os.path.join(DB_DIR, "app.db")

# Contextvar that carries the current user_id through the call stack so
# _get_conn() knows which per-user database to attach.  Set by the ASGI
# middleware or by background-task scaffolding; defaults to "admin" so
# boot-time / background code gets the bootstrap user's database.
_current_db_user_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "_current_db_user_id", default=""
)


def set_db_user_context(user_id: str):
    """Set the user_id for the current async context (request / background task)."""
    return _current_db_user_id.set(user_id)


def reset_db_user_context(token) -> None:
    _current_db_user_id.reset(token)


def get_db_user_context() -> str:
    """Return the current explicit user id, or an empty string when unset."""
    return _current_db_user_id.get()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# Fixed-width (always microseconds) UTC ISO timestamps. Used by the self-healing
# run-state logic where next_resume_at / heartbeat_at / lease_expires_at are
# compared lexicographically in SQL — both sides MUST come from these helpers so
# the string order matches chronological order (a bare isoformat() omits the
# fractional part when microseconds are 0, which would break the comparison).
def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _iso_in(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="microseconds")


def _uuid() -> str:
    return str(uuid.uuid4())


async def _emit_agent_run_status(user_id, agent_id, session_id, status: str,
                               turn_id: Optional[str] = None) -> None:
    """Broadcast an agent's run start/stop to the user's live WebSockets so the
    Agents grid status dot updates without a refresh. Fire-and-forget: lazily
    imports the API broadcaster (avoids a db→api import cycle) and never raises —
    status signalling must never disturb run-state persistence."""
    if not user_id or not agent_id:
        return
    try:
        from app.events.delivery import notify_user
        payload: Dict[str, Any] = {
            "type": "agent_status", "status": status,
            "agent_id": agent_id, "session_id": session_id,
        }
        if turn_id:
            payload["turn_id"] = turn_id
        await notify_user(user_id, payload)
    except Exception:
        pass


def cascade_delete_clones(conn, root_session_ids) -> int:
    """Permanently delete the CLONE agents (and their sessions + transcripts)
    spawned under the given orchestrator session ids — recursively, so a clone's
    own sub-clones go too.

    Generic + best-effort, so it stays decoupled from the orchestration plugin:
      • Keys off the plugin-owned ``agent_spawns`` ledger
        (orchestrator_session_id → spawn_session_id / spawn_agent_id) WHEN it
        exists; if the table is absent (orchestration ability never used) it
        simply no-ops.
      • Only ever deletes an agent row — AND its agent-scoped data (prompts,
        connections, abilities) — whose ``status='clone'``. A real fleet agent can
        never be caught by this sweep even if its id appears in the ledger (e.g. a
        ``kind='delegate'`` row points at an agent the user owns): only the
        delegate SUB-SESSION and the ledger row are cleaned up, its config is left
        wholly intact.

    Operates on the caller's open DBAPI connection (does NOT commit). Returns the
    number of clone agents removed."""
    removed = 0
    seen: set = set()
    queue = [s for s in (root_session_ids or []) if s]
    while queue:
        sid = queue.pop()
        if sid in seen:
            continue
        seen.add(sid)
        try:
            rows = conn.execute(
                "SELECT spawn_session_id, spawn_agent_id FROM agent_spawns "
                "WHERE orchestrator_session_id = ?",
                (sid,),
            ).fetchall()
        except Exception:  # noqa: BLE001 — ledger table absent → nothing to cascade
            return removed
        for r in rows:
            spawn_sid, spawn_aid = r[0], r[1]
            if spawn_sid:
                conn.execute("DELETE FROM interactions WHERE session_id = ?", (spawn_sid,))
                for tbl in ("session_summaries", "pipeline_events",
                            "run_contract_checks", "run_contract_state"):
                    try:
                        conn.execute(f"DELETE FROM {tbl} WHERE session_id = ?", (spawn_sid,))
                    except Exception:  # noqa: BLE001
                        pass
                conn.execute("DELETE FROM sessions WHERE id = ?", (spawn_sid,))
                queue.append(spawn_sid)  # this clone may have orchestrated sub-clones
            if spawn_aid:
                # Only a throwaway CLONE's agent record + its agent-scoped data
                # (prompts / connections / abilities) may be reaped. A real saved
                # agent can appear in this ledger too — a `kind='delegate'` row
                # points at an agent the user built and owns (e.g. a Local Claude
                # Code specialist someone delegated a task to). We must clean up
                # only its delegate SUB-SESSION (done above) and the ledger row
                # (below), NEVER the agent's own config. Gate every agent-scoped
                # delete on the agent actually being a clone — checked up front so
                # it's robust across drivers whose DELETE rowcount is unreliable.
                was_clone = False
                clone_parent = None
                try:
                    srow = conn.execute(
                        "SELECT status, metadata FROM agents WHERE id = ?", (spawn_aid,)).fetchone()
                    was_clone = bool(srow) and (srow[0] == "clone")
                    if srow and was_clone:
                        try:
                            _m = json.loads(srow[1] or "{}")
                            if isinstance(_m, dict):
                                clone_parent = _m.get("clone_of")
                        except Exception:  # noqa: BLE001
                            pass
                except Exception:  # noqa: BLE001
                    was_clone = False
                if was_clone:
                    cur = conn.execute(
                        "DELETE FROM agents WHERE id = ? AND status = 'clone'", (spawn_aid,))
                    try:
                        if cur.rowcount and cur.rowcount > 0:
                            removed += cur.rowcount
                    except Exception:  # noqa: BLE001
                        pass
                    conn.execute("DELETE FROM agent_prompts WHERE agent_id = ?", (spawn_aid,))
                    for tbl in ("agent_connections", "agent_abilities", "agent_soft_abilities"):
                        try:
                            conn.execute(f"DELETE FROM {tbl} WHERE agent_id = ?", (spawn_aid,))
                        except Exception:  # noqa: BLE001
                            pass
                    # Reap the clone's nested subagent home (data/agent_data/<parent>/subagents/<id>/)
                    # so ephemeral clones leave no orphaned directories behind.
                    if clone_parent:
                        try:
                            from app.agent_workspace import purge_subagent_home
                            purge_subagent_home(clone_parent, spawn_aid)
                        except Exception:  # noqa: BLE001
                            pass
        try:
            conn.execute("DELETE FROM agent_spawns WHERE orchestrator_session_id = ?", (sid,))
        except Exception:  # noqa: BLE001
            pass
    return removed


def resolve_child_sessions(conn, base_session_ids) -> list:
    """Resolve the child SESSION ids that hang off the given base/parent sessions.

    Used so deleting/recycling a parent takes its whole run-family with it.
    Mirrors the family grouping the Sessions page + chat session list draw:

      • spawned helper sessions — ``agent_spawns.orchestrator_session_id`` == base,
        walked recursively (a spawn may itself orchestrate sub-spawns).

    Optimizer Planner/Closer sessions are TOP-LEVEL sessions of their own (not
    children of the base session they ran on), so they are deliberately NOT
    included here.

    Best-effort + decoupled: any absent table/column is skipped. Returns a
    de-duplicated list that EXCLUDES the base ids themselves. Does not mutate or
    commit anything — purely a lookup."""
    bases = {s for s in (base_session_ids or []) if s}
    if not bases:
        return []
    children: set = set()

    # 1) Spawned helpers — recursive walk of the orchestration ledger.
    queue = list(bases)
    seen: set = set()
    while queue:
        sid = queue.pop()
        if sid in seen:
            continue
        seen.add(sid)
        try:
            rows = conn.execute(
                "SELECT spawn_session_id FROM agent_spawns WHERE orchestrator_session_id = ?",
                (sid,),
            ).fetchall()
        except Exception:  # noqa: BLE001 — ledger absent → no spawns
            rows = []
        for r in rows:
            csid = r[0]
            if csid and csid not in bases:
                children.add(csid)
                queue.append(csid)

    # 2) No optimizer membership — Planner/Closer sessions are top-level rows
    #    of their own, so nothing further is appended here.

    for b in bases:
        children.discard(b)
    children.discard(None)
    return list(children)


# FTS5 MATCH treats AND/OR/NOT/NEAR, quotes, and parens as syntax — never pass raw chat text.
_FTS5_QUERY_OPS = frozenset({"AND", "OR", "NOT", "NEAR"})


def _fts5_safe_match_query(raw: str, max_tokens: int = 12, max_token_len: int = 64) -> Optional[str]:
    """Turn free text into a conservative prefix OR-query safe for FTS5 MATCH."""
    if not raw or not raw.strip():
        return None
    tokens = re.findall(r"\w+", raw, flags=re.UNICODE)
    parts: List[str] = []
    for t in tokens:
        if len(t) < 2 or t.upper() in _FTS5_QUERY_OPS:
            continue
        if len(t) > max_token_len:
            t = t[:max_token_len]
        parts.append(f"{t}*")
        if len(parts) >= max_tokens:
            break
    if not parts:
        return None
    return " OR ".join(parts)


# ── Schema DDL ──────────────────────────────────────────────────────────────
#
# ⚠ DROP-IN POLICY — CORE, app-wide tables only (this is the SQLite mirror of
# app/db/schema/tables.py). Do NOT add a table here for a drop-in plugin
# capability (ability / integration / connector / …). A self-contained plugin
# creates its own storage lazily with `CREATE TABLE IF NOT EXISTS` on first use
# (worked example: plugins/abilities/agent_orchestration.py), so the capability
# stays deletable as a single file. See CLAUDE.md "Core vs. plugins".

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    title TEXT,
    metadata TEXT,
    agent_id TEXT,
    participants TEXT DEFAULT '[]',
    sort_order INTEGER,
    -- Recycling-bin lifecycle: 'active' = live, 'recycled' = soft-deleted from
    -- the chat header (hidden, but kept until its agent is permanently emptied).
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    read_at TEXT,
    unread_at TEXT
    ,chat_render_version INTEGER NOT NULL DEFAULT 2
);

-- Browser sessions — first-class, persistent browser TABS that live BESIDE chat.
-- Agent-created rows are scoped to one chat session so concurrent chats cannot
-- cross-wire navigation, reads, or cookie jars. User-created tabs may keep this
-- nullable until they are explicitly handed to an agent chat.
-- its own cookie jar (storage_state) so logins survive restarts, an optional
-- linked agent, and a `shared` flag (0 = private, 1 = visible to the linked
-- agent). The Web tab and the agent's browser_action both address a tab by THIS
-- id, so they drive the same Playwright page. agent_id/user_id are free TEXT (NOT
-- foreign keys) so a tab is independent of any chat session. See
-- app/tools/browser.py and app/api/browser_stream.py.
CREATE TABLE IF NOT EXISTS browser_sessions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    agent_id TEXT,
    chat_session_id TEXT,
    title TEXT,
    url TEXT,
    shared INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    storage_state TEXT,
    position INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_browser_sessions_user ON browser_sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_browser_sessions_agent ON browser_sessions(agent_id);
CREATE INDEX IF NOT EXISTS idx_browser_sessions_chat ON browser_sessions(chat_session_id);

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

CREATE UNIQUE INDEX IF NOT EXISTS idx_interactions_session_seq_unique ON interactions(session_id, session_seq);

-- O(1) transcript validation after the first read following a mutation.
-- Triggers only mark/revision the manifest; the canonical hash is rebuilt once
-- on demand by app.db.session_manifest and then reused across processes.
CREATE TABLE IF NOT EXISTS session_manifests (
    session_id TEXT PRIMARY KEY,
    authority_revision INTEGER NOT NULL DEFAULT 0,
    content_hash TEXT NOT NULL DEFAULT '',
    interaction_count INTEGER NOT NULL DEFAULT 0,
    max_session_seq INTEGER NOT NULL DEFAULT 0,
    dirty INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Per-session run state — the durable record of an in-flight (or recently
-- finished) agent turn. One row per session (upserted). Lets a cold device
-- (fresh load, even after a server restart) learn from the DB alone that a
-- run is in progress and where the live stream is up to, without depending on
-- the in-memory RunBuffer. Orphan cleanup on boot flips stale 'running' rows
-- to 'interrupted'. See app/agent/run_manager.py.
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
    -- Self-healing / auto-resume bookkeeping (see app/agent/runner.py + watchdog.py).
    -- stop_cause is the machine taxonomy of WHY a run ended (error holds the human reason):
    --   complete | user_stop | replaced | server_restart | zombie | frozen | crash | failed | needs_manual_resume
    stop_cause TEXT,
    -- origin is the launch source; drives resume eligibility + how to relaunch headlessly:
    --   web | automation | event | inbound | webhook | sandbox | optimizer
    origin TEXT,
    resume_attempts INTEGER NOT NULL DEFAULT 0,
    max_resume_attempts INTEGER,
    -- Per-error-type resume tally (JSON: {"crash": 2, ...}) so one repeating
    -- cause can never consume the whole total budget. Reset on a fresh run.
    resume_cause_counts TEXT,
    heartbeat_at TEXT,        -- last time the live loop proved it is alive (distinct from updated_at)
    next_resume_at TEXT,      -- backoff gate: do not resume before this
    owner_token TEXT,         -- lease token of the task/process that owns this run (multi-process safety)
    lease_expires_at TEXT,    -- when the lease goes stale and another worker may claim
    relaunch_ctx TEXT,        -- JSON recipe to rebuild a turn with no new user message
    current_op TEXT           -- JSON snapshot of the in-flight op (tool/turn/note) for refresh-safe live indicator
);

CREATE INDEX IF NOT EXISTS idx_session_runs_user_status ON session_runs(user_id, status);
-- idx_session_runs_status_heartbeat, idx_interactions_session_seq and
-- idx_interactions_turn are created by the ALTER TABLE migration block after the
-- columns are guaranteed to exist. They are NOT declared here: on an existing DB
-- the SCHEMA_SQL runs BEFORE the ALTER TABLE migrations, so a migration-added
-- column (e.g. session_runs.heartbeat_at) wouldn't exist yet and the CREATE
-- INDEX would abort the whole init with "no such column".

-- Background-leader lock (see app/coordination/leader.py). A single shared row
-- (lock_key='background') elects ONE worker/instance to run the singleton
-- background loops — scheduler, event runtime, ability pollers, watchdog,
-- remote access, boot orphan-resume — so multi-worker / multi-instance deploys
-- don't double-fire automations or re-ignite the same orphaned runs N times.
-- The holder renews a TTL'd lease; if it dies, another worker claims it.
CREATE TABLE IF NOT EXISTS background_leader (
    lock_key TEXT PRIMARY KEY,
    holder_id TEXT,
    heartbeat_at TEXT,
    expires_at TEXT
);

-- ── Multi-device coordination (see app/devices/) ───────────────────────────
-- A SHARED database lets several WebAgent instances (one user's devices) see
-- one another. Two tables make that useful:
--   • device_presence — a heartbeat/registry: which devices are online right now
--     and what each can do (its capabilities). A device upserts its own row every
--     few seconds; a stale last_seen means it went away.
--   • device_jobs — a cross-device dispatch QUEUE. One device enqueues "run this
--     prompt on agent X", optionally addressed to a specific target_instance
--     (NULL = any device may take it). EXACTLY ONE device claims each job via an
--     atomic UPDATE (same serialise-the-writer idiom as background_leader above),
--     runs it locally, and records the result. Unlike background_leader (which
--     elects ONE global leader), EVERY instance runs its own device worker and
--     claims only the jobs addressed to its own instance_id (or broadcast jobs).
--     A claimed job carries a TTL'd lease so a crashed claimer's job is reclaimed.
CREATE TABLE IF NOT EXISTS device_presence (
    instance_id TEXT PRIMARY KEY,
    label TEXT,                                  -- friendly name (hostname; self-reported by the device)
    custom_label TEXT,                           -- admin's chosen display name; overrides label, never overwritten by the heartbeat
    custom_icon TEXT,                            -- admin's chosen icon (Lucide name); overrides the platform icon
    capabilities TEXT NOT NULL DEFAULT '{}',     -- JSON: platform, has_browser, …
    endpoint TEXT,                               -- reachable base URL for nudges (optional)
    last_seen TEXT,                              -- ISO heartbeat; stale = offline
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS device_jobs (
    id TEXT PRIMARY KEY,
    created_by_instance TEXT,                    -- which device enqueued it
    target_instance TEXT,                        -- NULL = any device may claim (broadcast)
    target_label TEXT,                           -- friendly target shown in the UI
    owner_user_id TEXT NOT NULL,
    agent_id TEXT,                               -- agent to run on the target device
    prompt TEXT NOT NULL DEFAULT '',
    payload TEXT NOT NULL DEFAULT '{}',          -- JSON extras
    status TEXT NOT NULL DEFAULT 'pending',      -- pending | claimed | done | error
    claimed_by TEXT,                             -- instance that won the claim
    claimed_at TEXT,
    lease_expires_at TEXT,                       -- TTL'd; expired+claimed = reclaimable
    result_excerpt TEXT,
    error TEXT,
    session_id TEXT,                             -- session the run created on the target
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_device_jobs_claim ON device_jobs(status, target_instance);
CREATE INDEX IF NOT EXISTS idx_device_jobs_created ON device_jobs(created_at);
CREATE INDEX IF NOT EXISTS idx_device_presence_seen ON device_presence(last_seen);

-- ── Instance metadata (shared identity store) ────────────────────────────────
-- Persistent facts about any instance — cloud VMs, fleet devices, local checkouts,
-- Cloud Run services, manual servers. One row per instance, keyed by ``ref`` (the
-- same format the Instances UI already uses everywhere: "provider|zone|name" for
-- cloud VMs, the device UUID for fleet devices, "local:host:port" for checkouts).
-- ``metadata`` is a JSON bag for everything optional or kind-specific (domains,
-- repo, custom_icon, tunnel, …) — adding a new field never needs a migration.
-- SEPARATE from ``device_presence`` (heartbeat/liveness) and ``device_jobs``
-- (dispatch queue) — those have fundamentally different access patterns.
CREATE TABLE IF NOT EXISTS instances (
    ref TEXT PRIMARY KEY,                        -- "google_vm|us-central1-a|webagent-abc" or device UUID
    kind TEXT NOT NULL,                          -- "cloud_vm" | "local_device" | "cloud_run" | "manual_server"
    display_name TEXT,                           -- admin-chosen or auto-discovered
    provider TEXT,                               -- "google_vm" | "ssh_vm" | "google_cloud_run" | ""
    status TEXT,                                 -- "RUNNING" | "TERMINATED" | "" (liveness is device_presence)
    ip TEXT,
    endpoint TEXT,                               -- reachable URL
    platform TEXT,                               -- "linux" | "macos" | "windows" | "cloud" | ""
    zone TEXT,                                   -- GCE zone (null for non-cloud)
    machine_type TEXT,                           -- "e2-small" etc.
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    metadata TEXT NOT NULL DEFAULT '{}'           -- JSON bag: {domains, repo, custom_icon, tunnel, ...}
);
CREATE INDEX IF NOT EXISTS idx_instances_kind ON instances(kind);

-- Diagnostic flight-recorder (see app/agent/diagnostics.py). A rolling,
-- auto-pruned log of server warnings/errors, agent-loop pipeline problems and
-- run outcomes, queryable by an operator or a diagnostic AI agent. session_id /
-- agent_id are free TEXT (NOT foreign keys) so a record outlives the row it
-- referenced and a delete never cascades diagnostics away.
CREATE TABLE IF NOT EXISTS diagnostics (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,                 -- ISO-8601 UTC capture time
    level TEXT NOT NULL,              -- debug | info | warning | error | critical
    category TEXT NOT NULL,           -- server | loop | run | tool | http
    source TEXT,                      -- logger name / pipeline step / tool name
    message TEXT NOT NULL,
    detail TEXT,                      -- JSON blob (traceback, args, tokens, cost…)
    session_id TEXT,
    turn_id TEXT,
    agent_id TEXT,
    user_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_diagnostics_ts ON diagnostics(ts);
CREATE INDEX IF NOT EXISTS idx_diagnostics_level ON diagnostics(level);
CREATE INDEX IF NOT EXISTS idx_diagnostics_category ON diagnostics(category);
CREATE INDEX IF NOT EXISTS idx_diagnostics_session ON diagnostics(session_id);

-- Client render recorder (see app/agent/render_recorder.py + ui/js/recorder.js).
-- A rolling, auto-pruned record of what the BROWSER actually rendered and felt:
-- HTML snapshots, DOM-mutation deltas, lag/long-task metrics, JS errors,
-- console warnings and failed network calls — the client-side blind spot the
-- server logs (diagnostics) can never see. Every row carries `session_seq`, the
-- same monotonic per-session counter stamped on WS events and interactions, so
-- a render moment joins exactly to the interaction / diagnostics row beside it.
-- `kind` discriminates the payload; `html` holds a full snapshot today (Level 1)
-- and is designed to also hold serialized mutation deltas for full replay later
-- (Level 2). session_id / agent_id are free TEXT (NOT foreign keys) so a record
-- outlives the row it referenced.
CREATE TABLE IF NOT EXISTS render_recordings (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,                 -- ISO-8601 UTC: browser capture time
    recv_ts TEXT,                     -- ISO-8601 UTC: server receive time (clock-skew check)
    kind TEXT NOT NULL,               -- snapshot | mutation | lag | js_error | console | network | nav | meta
    session_id TEXT,
    turn_id TEXT,
    session_seq INTEGER,              -- correlation key → interactions / WS events / diagnostics
    user_id TEXT,
    agent_id TEXT,
    client_id TEXT,                   -- per-tab browser instance id (distinguishes open tabs)
    seq INTEGER,                      -- per-client monotonic record number (ordering / replay)
    url TEXT,                         -- page URL / path at capture time
    level TEXT,                       -- info | warning | error (for js_error / console)
    label TEXT,                       -- short tag: error message, event name, selector, method+status
    value_num REAL,                   -- numeric metric: lag ms, render ms, duration ms, byte size
    detail TEXT,                      -- JSON blob: stack trace, network detail, perf entry
    html TEXT,                        -- L1: full HTML snapshot; L2: serialized mutation delta(s)
    html_bytes INTEGER,               -- size of the html payload (quota / stats)
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_render_rec_ts ON render_recordings(ts);
CREATE INDEX IF NOT EXISTS idx_render_rec_session ON render_recordings(session_id);
CREATE INDEX IF NOT EXISTS idx_render_rec_kind ON render_recordings(kind);
CREATE INDEX IF NOT EXISTS idx_render_rec_seq ON render_recordings(session_seq);

CREATE TABLE IF NOT EXISTS session_summaries (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    session_id TEXT NOT NULL UNIQUE REFERENCES sessions(id),
    title TEXT,
    summary TEXT NOT NULL,
    message_count INTEGER NOT NULL DEFAULT 0,
    covered_count INTEGER NOT NULL DEFAULT 0,  -- compaction marker (leading interactions folded into summary)
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_summaries_user ON session_summaries(user_id);

-- Segmented compaction "train": one row per FROZEN summary car (see
-- app/agent/compaction.py). Replaces the single rolling summary above with an
-- ordered list of cars, each summarising one [start_index, end_index) span of raw
-- turns once. The raw turns stay in `interactions` and stay retrievable by range.
CREATE TABLE IF NOT EXISTS session_summary_segments (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    seq INTEGER NOT NULL DEFAULT 0,            -- order in the train (0 = oldest)
    start_index INTEGER NOT NULL DEFAULT 0,    -- half-open covered range [start, end)
    end_index INTEGER NOT NULL DEFAULT 0,
    summary TEXT NOT NULL,
    token_estimate INTEGER NOT NULL DEFAULT 0, -- rough token size of `summary`
    topic TEXT,                                -- short retrieval-hint label
    tier INTEGER NOT NULL DEFAULT 0,           -- 0 = normal car, 1 = far-back merged block
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(session_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_summary_segments_session ON session_summary_segments(session_id, seq);

CREATE TABLE IF NOT EXISTS agent_templates (
    id TEXT PRIMARY KEY DEFAULT 'default',
    max_turn_count INTEGER NOT NULL DEFAULT 0,
    model TEXT,
    provider TEXT,
    temperature REAL NOT NULL DEFAULT 0.0,
    max_tokens INTEGER NOT NULL DEFAULT 8096,
    metadata TEXT NOT NULL DEFAULT '{}',
    trigger_type TEXT NOT NULL DEFAULT 'user_input',
    trigger_key TEXT,
    loop_logic TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Agent templates seeded from data/agents/*.json — no hardcoded defaults

CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    template_id TEXT,
    name TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    is_user_default INTEGER NOT NULL DEFAULT 0,
    max_turn_count INTEGER NOT NULL DEFAULT 0,
    model TEXT,
    provider TEXT,
    temperature REAL NOT NULL DEFAULT 0.0,
    max_tokens INTEGER NOT NULL DEFAULT 8096,
    status TEXT NOT NULL DEFAULT 'active',
    metadata TEXT NOT NULL DEFAULT '{}',
    trigger_type TEXT NOT NULL DEFAULT 'user_input',
    trigger_key TEXT,
    loop_logic TEXT NOT NULL DEFAULT '[]',
    assigned_at TEXT NOT NULL DEFAULT (datetime('now')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    turn_count INTEGER NOT NULL DEFAULT 0,
    admin_users TEXT NOT NULL DEFAULT '[]',
    member_users TEXT NOT NULL DEFAULT '[]',
    user_mode TEXT NOT NULL DEFAULT 'anonymous',
    sort_order INTEGER,
    -- Self-healing opt-out: 0 = never auto-resume this agent's runs (they wait
    -- for a one-click manual resume instead). Default 1 = auto-resume eligible.
    auto_resume INTEGER NOT NULL DEFAULT 1
);

-- ============================================================
-- Agent Prompts: per-slot content with optional per-user overrides.
-- One row per (agent_id, slot_name, user_id). user_id IS NULL = admin base;
-- non-null user_id = override owned by that user (incl. anon visitors).
-- Slot policy (order_index, lock, merge_mode) lives only on admin base rows.
-- ============================================================

-- Per-user, per-agent Config-topic history. This is server authority for the
-- Agents page's small Recent group; the browser maintains only a fast cache.
CREATE TABLE IF NOT EXISTS agent_config_recent_topics (
    user_id         TEXT NOT NULL,
    agent_id        TEXT NOT NULL,
    topic_id        TEXT NOT NULL,
    last_opened_at  INTEGER NOT NULL,
    PRIMARY KEY (user_id, agent_id, topic_id)
);

CREATE INDEX IF NOT EXISTS idx_agent_config_recent_topics_lookup
    ON agent_config_recent_topics(user_id, agent_id, last_opened_at DESC);

CREATE TABLE IF NOT EXISTS agent_prompts (
    id              TEXT PRIMARY KEY,
    agent_id        TEXT NOT NULL,
    slot_name       TEXT NOT NULL,
    user_id         TEXT,
    order_index     INTEGER,
    lock            INTEGER,
    merge_mode      TEXT,
    content         TEXT NOT NULL DEFAULT '',
    -- Origin version this row was cloned from (in agent_prompt_templates).
    -- NULL on user-override rows and on legacy rows pre-versioning.
    template_version INTEGER,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_by      TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_prompts_slot_user
    ON agent_prompts(agent_id, slot_name, IFNULL(user_id, ''));
CREATE INDEX IF NOT EXISTS idx_agent_prompts_agent ON agent_prompts(agent_id);
CREATE INDEX IF NOT EXISTS idx_agent_prompts_user  ON agent_prompts(user_id);

-- ============================================================
-- Agent Prompt Templates: canonical slot defaults per template.
-- JSON files in data/agents/*.json seed this table; admin edits
-- promoted here are protected from re-seed via source='admin'.
-- When a new agent is created, rows here are cloned into agent_prompts
-- under the new agent's id (with template_version stamped on each row).
-- ============================================================

CREATE TABLE IF NOT EXISTS agent_prompt_templates (
    id              TEXT PRIMARY KEY,
    template_id     TEXT NOT NULL,
    slot_name       TEXT NOT NULL,
    order_index     INTEGER NOT NULL DEFAULT 0,
    lock            INTEGER NOT NULL DEFAULT 0,
    merge_mode      TEXT NOT NULL DEFAULT 'replace',
    content         TEXT NOT NULL DEFAULT '',
    version         INTEGER NOT NULL DEFAULT 1,
    source          TEXT NOT NULL DEFAULT 'json' CHECK (source IN ('json','admin')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_by      TEXT NOT NULL DEFAULT 'system',
    UNIQUE (template_id, slot_name)
);

CREATE INDEX IF NOT EXISTS idx_agent_prompt_templates_tpl
    ON agent_prompt_templates(template_id);

-- ============================================================
-- App Meta: generic key/value store for cross-cutting runtime
-- metadata (manifest hashes, schema versions, feature toggles).
-- ============================================================

CREATE TABLE IF NOT EXISTS app_meta (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL DEFAULT '',
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ============================================================
-- Agent Connections: per-agent channel/integration config
-- ============================================================

CREATE TABLE IF NOT EXISTS agent_connections (
    id              TEXT PRIMARY KEY,
    agent_id        TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    connection_type TEXT NOT NULL,
    section         TEXT NOT NULL DEFAULT 'channel',
    enabled         INTEGER NOT NULL DEFAULT 0,
    config          TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(agent_id, connection_type)
);

CREATE INDEX IF NOT EXISTS idx_agent_conn_agent ON agent_connections(agent_id);
CREATE INDEX IF NOT EXISTS idx_agent_conn_type ON agent_connections(connection_type);

-- ============================================================
-- Agent Abilities: per-agent OAuth capability + BYO creds
--   - ability_id is the registry key e.g. "google.gmail_read"
--   - source: "platform" (uses app-admin OAuth creds) or "byo"
--     (uses agent admin's own creds stored on this row)
--   - byo_client_id / byo_client_secret_ref hold BYO OAuth creds.
--     Empty when source="platform". secret_ref will move to a vault.
-- ============================================================

CREATE TABLE IF NOT EXISTS agent_abilities (
    id                    TEXT PRIMARY KEY,
    agent_id              TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    ability_id            TEXT NOT NULL,
    source                TEXT NOT NULL DEFAULT 'platform',
    enabled               INTEGER NOT NULL DEFAULT 0,
    byo_client_id         TEXT NOT NULL DEFAULT '',
    byo_client_secret_ref TEXT NOT NULL DEFAULT '',
    config                TEXT NOT NULL DEFAULT '{}',
    created_at            TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at            TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(agent_id, ability_id)
);

CREATE INDEX IF NOT EXISTS idx_agent_ability_agent ON agent_abilities(agent_id);
CREATE INDEX IF NOT EXISTS idx_agent_ability_id    ON agent_abilities(ability_id);

-- ============================================================
-- Soft Abilities: per-agent declarative skills/workflows
-- ============================================================

CREATE TABLE IF NOT EXISTS agent_soft_abilities (
    id                TEXT PRIMARY KEY,
    agent_id          TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    slug              TEXT NOT NULL,
    display_name      TEXT NOT NULL,
    description       TEXT NOT NULL DEFAULT '',
    icon              TEXT NOT NULL DEFAULT 'sparkles',
    enabled           INTEGER NOT NULL DEFAULT 1,
    skill_summary     TEXT NOT NULL DEFAULT '',
    skill_body        TEXT NOT NULL DEFAULT '',
    workflow          TEXT NOT NULL DEFAULT '{}',
    allowed_tools     TEXT NOT NULL DEFAULT '[]',
    credential_schema TEXT NOT NULL DEFAULT '[]',
    policy            TEXT NOT NULL DEFAULT '{}',
    status            TEXT NOT NULL DEFAULT 'draft'
                      CHECK(status IN ('draft','ready','disabled','error')),
    version           INTEGER NOT NULL DEFAULT 1,
    created_by        TEXT NOT NULL,
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at        TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(agent_id, slug)
);

CREATE INDEX IF NOT EXISTS idx_agent_soft_abilities_agent
    ON agent_soft_abilities(agent_id);

CREATE TABLE IF NOT EXISTS soft_ability_runs (
    id              TEXT PRIMARY KEY,
    ability_id      TEXT NOT NULL REFERENCES agent_soft_abilities(id) ON DELETE CASCADE,
    agent_id        TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    user_id         TEXT NOT NULL,
    session_id      TEXT NOT NULL DEFAULT '',
    ability_version INTEGER NOT NULL,
    status          TEXT NOT NULL,
    tools           TEXT NOT NULL DEFAULT '[]',
    elapsed_ms      INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_soft_ability_runs_ability
    ON soft_ability_runs(ability_id, created_at);

-- ============================================================
-- Agent Automations: scheduled background prompts per-agent.
-- Parsed from the `automation` prompt slot by the LLM, stored as
-- structured rows. The local scheduler polls this table for due rows.
-- ============================================================

CREATE TABLE IF NOT EXISTS agent_automations (
    id                  TEXT PRIMARY KEY,
    agent_id            TEXT NOT NULL,
    owner_user_id       TEXT NOT NULL,
    task_label          TEXT NOT NULL DEFAULT '',
    prompt              TEXT NOT NULL DEFAULT '',
    schedule_cron       TEXT NOT NULL DEFAULT '',
    schedule_natural    TEXT NOT NULL DEFAULT '',
    timezone            TEXT NOT NULL DEFAULT 'UTC',
    channel             TEXT,
    channel_recipient   TEXT,
    silent              INTEGER NOT NULL DEFAULT 0,
    enabled             INTEGER NOT NULL DEFAULT 1,
    next_run_at         TEXT,
    last_run_at         TEXT,
    last_status         TEXT,
    last_error          TEXT,
    last_session_id     TEXT,
    source_hash         TEXT NOT NULL DEFAULT '',
    fire_token          TEXT,
    external_job_id     TEXT,
    external_provider   TEXT,
    schedule_kind       TEXT NOT NULL DEFAULT 'cron',
    delivery_json       TEXT NOT NULL DEFAULT '{}',
    run_mode            TEXT NOT NULL DEFAULT 'inline',
    runner_agent_id     TEXT,
    clone_abilities     TEXT NOT NULL DEFAULT '[]',
    max_per_day         INTEGER,
    runs_today          INTEGER NOT NULL DEFAULT 0,
    runs_today_date     TEXT,
    fail_count          INTEGER NOT NULL DEFAULT 0,
    disable_after_failures INTEGER,
    expires_at          TEXT,
    retry_max           INTEGER NOT NULL DEFAULT 0,
    retry_backoff_seconds INTEGER NOT NULL DEFAULT 0,
    next_retry_at       TEXT,
    memory_json         TEXT NOT NULL DEFAULT '{}',
    origin              TEXT NOT NULL DEFAULT 'slot',
    -- Cross-device targeting (see app/devices/): run this automation on another
    -- device instead of the firing box. NULL/'' = run locally. target_offline
    -- decides what happens when the target is offline at fire time.
    target_device       TEXT,
    target_offline      TEXT DEFAULT 'wait',   -- 'wait' (queue until it wakes) | 'skip'
    -- Recycling-bin marker: NULL = active, ISO timestamp = soft-deleted (in the
    -- Automations bin). Recycling also flips enabled=0 so the scheduler never
    -- fires a binned row (see trash_automation). Permanent delete removes the row.
    deleted_at          TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_agent_automations_agent ON agent_automations(agent_id);
CREATE INDEX IF NOT EXISTS idx_agent_automations_owner ON agent_automations(owner_user_id);
CREATE INDEX IF NOT EXISTS idx_agent_automations_next  ON agent_automations(next_run_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_automations_hash
    ON agent_automations(agent_id, owner_user_id, source_hash);

-- ============================================================
-- Agent Event Subscriptions: push/poll-based triggers, sibling
-- to agent_automations (which are cron-based). See migration 016.
-- ============================================================

CREATE TABLE IF NOT EXISTS agent_event_subscriptions (
    id                          TEXT PRIMARY KEY,
    agent_id                    TEXT NOT NULL,
    owner_user_id               TEXT NOT NULL,
    source                      TEXT NOT NULL,
    event_type                  TEXT NOT NULL,
    filter_json                 TEXT NOT NULL DEFAULT '{}',
    task_label                  TEXT NOT NULL DEFAULT '',
    prompt                      TEXT NOT NULL DEFAULT '',
    trigger_natural             TEXT NOT NULL DEFAULT '',
    channel                     TEXT,
    channel_recipient           TEXT,
    silent                      INTEGER NOT NULL DEFAULT 0,
    enabled                     INTEGER NOT NULL DEFAULT 1,
    external_subscription_id    TEXT,
    external_resource_id        TEXT,
    external_expiration_at      TEXT,
    external_metadata           TEXT NOT NULL DEFAULT '{}',
    poll_cursor                 TEXT,
    poll_interval_seconds       INTEGER,
    last_polled_at              TEXT,
    last_event_at               TEXT,
    last_event_external_id      TEXT,
    last_status                 TEXT,
    last_error                  TEXT,
    last_session_id             TEXT,
    fire_count                  INTEGER NOT NULL DEFAULT 0,
    source_hash                 TEXT NOT NULL DEFAULT '',
    delivery_json               TEXT NOT NULL DEFAULT '{}',
    run_mode                    TEXT NOT NULL DEFAULT 'inline',
    runner_agent_id             TEXT,
    clone_abilities             TEXT NOT NULL DEFAULT '[]',
    max_per_day                 INTEGER,
    runs_today                  INTEGER NOT NULL DEFAULT 0,
    runs_today_date             TEXT,
    fail_count                  INTEGER NOT NULL DEFAULT 0,
    disable_after_failures      INTEGER,
    expires_at                  TEXT,
    retry_max                   INTEGER NOT NULL DEFAULT 0,
    retry_backoff_seconds       INTEGER NOT NULL DEFAULT 0,
    next_retry_at               TEXT,
    memory_json                 TEXT NOT NULL DEFAULT '{}',
    origin                      TEXT NOT NULL DEFAULT 'slot',
    -- Recycling-bin marker: NULL = active, ISO timestamp = soft-deleted (in the
    -- Automations bin). Recycling also flips enabled=0 so no consumer fires a
    -- binned row. Permanent delete removes the row (and unregisters externally).
    deleted_at                  TEXT,
    created_at                  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at                  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_evt_sub_agent ON agent_event_subscriptions(agent_id);
CREATE INDEX IF NOT EXISTS idx_evt_sub_owner ON agent_event_subscriptions(owner_user_id);
CREATE INDEX IF NOT EXISTS idx_evt_sub_source ON agent_event_subscriptions(source, event_type);
CREATE INDEX IF NOT EXISTS idx_evt_sub_ext ON agent_event_subscriptions(source, external_subscription_id);
CREATE INDEX IF NOT EXISTS idx_evt_sub_expiry ON agent_event_subscriptions(external_expiration_at);
CREATE INDEX IF NOT EXISTS idx_evt_sub_poll ON agent_event_subscriptions(source, last_polled_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_evt_sub_hash
    ON agent_event_subscriptions(agent_id, owner_user_id, source_hash);

CREATE TABLE IF NOT EXISTS event_deliveries (
    id                  TEXT PRIMARY KEY,
    subscription_id     TEXT NOT NULL,
    source              TEXT NOT NULL,
    event_type          TEXT NOT NULL,
    event_external_id   TEXT NOT NULL,
    owner_user_id       TEXT NOT NULL,
    agent_id            TEXT NOT NULL,
    session_id          TEXT,
    status              TEXT NOT NULL DEFAULT 'pending',
    error               TEXT,
    payload_excerpt     TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_evt_del_sub ON event_deliveries(subscription_id);
CREATE INDEX IF NOT EXISTS idx_evt_del_dedup
    ON event_deliveries(subscription_id, event_external_id);
CREATE INDEX IF NOT EXISTS idx_evt_del_created ON event_deliveries(created_at DESC);

-- ============================================================
-- automation_runs: per-run history for cron tasks, one-shot
-- timers, and event subscriptions. Powers the dashboard's run
-- history + failure visibility. See migration 029.
-- ============================================================

CREATE TABLE IF NOT EXISTS automation_runs (
    id                  TEXT PRIMARY KEY,
    kind                TEXT NOT NULL DEFAULT 'schedule',
    automation_id       TEXT,
    subscription_id     TEXT,
    agent_id            TEXT NOT NULL,
    owner_user_id       TEXT NOT NULL,
    runner_agent_id     TEXT,
    run_mode            TEXT NOT NULL DEFAULT 'inline',
    session_id          TEXT,
    status              TEXT NOT NULL DEFAULT 'running',
    started_at          TEXT,
    finished_at         TEXT,
    reply_excerpt       TEXT,
    delivery_json       TEXT NOT NULL DEFAULT '{}',
    error               TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_automation_runs_auto  ON automation_runs(automation_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_automation_runs_sub   ON automation_runs(subscription_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_automation_runs_owner ON automation_runs(owner_user_id, created_at DESC);

-- ============================================================
-- Memory System: core knowledge brain
-- ============================================================

-- Core memory pages (compiled truth + timeline pattern)
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
    frontmatter     TEXT NOT NULL DEFAULT '{}',  -- JSON
    -- Cross-session knowledge engine fields (Context Control):
    origin          TEXT NOT NULL DEFAULT 'distilled',  -- 'deliberate' | 'distilled'
    pinned          INTEGER NOT NULL DEFAULT 0,          -- protected from auto-merge/trim
    provenance      TEXT NOT NULL DEFAULT '[]',          -- JSON list of source session/interaction ids
    needs_review    INTEGER NOT NULL DEFAULT 0,          -- flagged when new evidence touches it
    content_hash    TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(user_id, slug)
);

CREATE INDEX IF NOT EXISTS idx_memories_user ON memories(user_id);
CREATE INDEX IF NOT EXISTS idx_memories_type ON memories(page_type);
CREATE INDEX IF NOT EXISTS idx_memories_updated ON memories(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_memories_trgm ON memories(title COLLATE NOCASE);

-- FTS5 full-text search index
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    slug UNINDEXED,
    title,
    compiled_truth,
    timeline,
    content='memories',
    content_rowid='rowid',
    tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS trg_memories_fts_insert
    AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, slug, title, compiled_truth, timeline)
    VALUES (new.rowid, new.slug, new.title, new.compiled_truth, new.timeline);
END;

CREATE TRIGGER IF NOT EXISTS trg_memories_fts_delete
    AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, slug, title, compiled_truth, timeline)
    VALUES ('delete', old.rowid, old.slug, old.title, old.compiled_truth, old.timeline);
END;

CREATE TRIGGER IF NOT EXISTS trg_memories_fts_update
    AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, slug, title, compiled_truth, timeline)
    VALUES ('delete', old.rowid, old.slug, old.title, old.compiled_truth, old.timeline);
    INSERT INTO memories_fts(rowid, slug, title, compiled_truth, timeline)
    VALUES (new.rowid, new.slug, new.title, new.compiled_truth, new.timeline);
END;

-- Chunked content with optional vector embeddings
CREATE TABLE IF NOT EXISTS memory_chunks (
    id            TEXT PRIMARY KEY,
    memory_id     TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    chunk_index   INTEGER NOT NULL,
    chunk_text    TEXT NOT NULL,
    chunk_source  TEXT NOT NULL DEFAULT 'compiled_truth'
                  CHECK (chunk_source IN ('compiled_truth', 'timeline')),
    embedding     BLOB,       -- numpy float32 array for local vector search
    token_count   INTEGER,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(memory_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_chunks_memory ON memory_chunks(memory_id);
CREATE INDEX IF NOT EXISTS idx_chunks_source ON memory_chunks(chunk_source);

CREATE TABLE IF NOT EXISTS tools (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    code TEXT NOT NULL,
    description TEXT NOT NULL,
    parameters TEXT NOT NULL DEFAULT '{}',
    language TEXT NOT NULL DEFAULT 'python',
    status TEXT NOT NULL DEFAULT 'active',
    created_by TEXT NOT NULL,
    stages TEXT NOT NULL DEFAULT '[]',
    destructive INTEGER NOT NULL DEFAULT 0,
    agent_types TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_tools_name ON tools(name);
CREATE INDEX IF NOT EXISTS idx_tools_status ON tools(status);
CREATE INDEX IF NOT EXISTS idx_tools_creator ON tools(created_by);

-- tool_executions merged into interactions.metadata

CREATE TABLE IF NOT EXISTS agent_credentials (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    credential_type TEXT NOT NULL,
    encrypted_data TEXT NOT NULL,
    display_name TEXT,
    expires_at TEXT,
    scopes TEXT DEFAULT '[]',
    last_used_at TEXT,
    use_count INTEGER NOT NULL DEFAULT 0,
    is_active INTEGER NOT NULL DEFAULT 1,
    requires_renewal INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ============================================================
-- Skill System: agent capabilities with performance tracking
-- ============================================================

CREATE TABLE IF NOT EXISTS skills (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    name            TEXT NOT NULL,
    description     TEXT,
    code            TEXT,
    version         INTEGER NOT NULL DEFAULT 1,
    base_skill_id   TEXT,                    -- NULL for official, points to parent for user forks
    is_official     INTEGER NOT NULL DEFAULT 0,
    tags            TEXT NOT NULL DEFAULT '[]', -- JSON array
    is_active       INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_skills_user ON skills(user_id);
CREATE INDEX IF NOT EXISTS idx_skills_name ON skills(name);
CREATE INDEX IF NOT EXISTS idx_skills_official ON skills(is_official);

-- Execution log: every skill invocation
CREATE TABLE IF NOT EXISTS skill_executions (
    id              TEXT PRIMARY KEY,
    skill_id        TEXT NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
    user_id         TEXT NOT NULL,
    session_id      TEXT NOT NULL REFERENCES sessions(id),
    interaction_id  TEXT,                    -- links to interactions table
    success         INTEGER NOT NULL DEFAULT 1,
    duration_ms     INTEGER NOT NULL DEFAULT 0,
    steps_to_complete INTEGER NOT NULL DEFAULT 1,
    error_message   TEXT,
    input_params    TEXT DEFAULT '{}',        -- JSON
    output_summary  TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_exec_skill ON skill_executions(skill_id);
CREATE INDEX IF NOT EXISTS idx_exec_user ON skill_executions(user_id);
CREATE INDEX IF NOT EXISTS idx_exec_created ON skill_executions(created_at);
CREATE INDEX IF NOT EXISTS idx_exec_success ON skill_executions(success);

-- User feedback on skill outcomes
CREATE TABLE IF NOT EXISTS skill_feedback (
    id              TEXT PRIMARY KEY,
    skill_id        TEXT NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
    execution_id    TEXT REFERENCES skill_executions(id) ON DELETE SET NULL,
    user_id         TEXT NOT NULL,
    feedback_type   TEXT NOT NULL CHECK (feedback_type IN ('positive', 'negative', 'correction')),
    message         TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_feedback_skill ON skill_feedback(skill_id);
CREATE INDEX IF NOT EXISTS idx_feedback_user ON skill_feedback(user_id);
CREATE INDEX IF NOT EXISTS idx_feedback_type ON skill_feedback(feedback_type);

CREATE TABLE IF NOT EXISTS session_interrupts (
    session_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    interrupt_requested INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (session_id, user_id)
        REFERENCES sessions(id, user_id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_id_user ON sessions(id, user_id);

-- ============================================================
-- Attachment storage
-- ============================================================

CREATE TABLE IF NOT EXISTS attachments (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    session_id      TEXT REFERENCES sessions(id),
    original_name   TEXT NOT NULL,
    mime_type       TEXT NOT NULL,
    size_bytes      INTEGER NOT NULL,
    storage_path    TEXT NOT NULL,
    storage_provider TEXT NOT NULL DEFAULT 'local',
    metadata        TEXT NOT NULL DEFAULT '{}',  -- JSON: duration (voice), dimensions (image)
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_attachments_user ON attachments(user_id);
CREATE INDEX IF NOT EXISTS idx_attachments_session ON attachments(session_id);

-- ============================================================
-- Communication channels (Telegram, WhatsApp, SMS, etc.)
-- ============================================================

CREATE TABLE IF NOT EXISTS channel_identities (
    id              TEXT PRIMARY KEY,
    channel         TEXT NOT NULL,
    external_id     TEXT NOT NULL,
    user_id         TEXT NOT NULL,
    user_tier       TEXT NOT NULL DEFAULT 'anonymous'
                    CHECK (user_tier IN ('anonymous', 'channel_verified', 'full')),
    display_name    TEXT NOT NULL DEFAULT '',
    email           TEXT NOT NULL DEFAULT '',
    email_verified  INTEGER NOT NULL DEFAULT 0,
    linked_user_id  TEXT,
    metadata        TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(channel, external_id)
);

CREATE INDEX IF NOT EXISTS idx_channel_user ON channel_identities(user_id);
CREATE INDEX IF NOT EXISTS idx_channel_channel_ext ON channel_identities(channel, external_id);

CREATE TABLE IF NOT EXISTS anonymous_guest_credentials (
    credential_hash TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    channel         TEXT NOT NULL,
    external_id     TEXT NOT NULL,
    agent_id        TEXT NOT NULL DEFAULT '',
    expires_at      TEXT NOT NULL,
    last_used_at    TEXT NOT NULL DEFAULT (datetime('now')),
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_guest_credentials_user
ON anonymous_guest_credentials(user_id);

CREATE TABLE IF NOT EXISTS linking_codes (
    id              TEXT PRIMARY KEY,
    code            TEXT NOT NULL UNIQUE,
    source_user_id  TEXT NOT NULL,
    target_channel  TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at      TEXT NOT NULL,
    used            INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_linking_codes_code ON linking_codes(code);

-- Generic inbound webhook registrations
-- ============================================================

CREATE TABLE IF NOT EXISTS webhook_registrations (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    name            TEXT NOT NULL,
    instructions    TEXT NOT NULL DEFAULT '',
    active          INTEGER NOT NULL DEFAULT 1,
    -- Recycling-bin marker: NULL = active, ISO timestamp = soft-deleted (in the
    -- Automations bin). Recycling also flips active=0 so inbound delivery stops.
    deleted_at      TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_webhook_reg_user ON webhook_registrations(user_id);

-- Inbound webhook event log
CREATE TABLE IF NOT EXISTS webhook_event_log (
    id              TEXT PRIMARY KEY,
    webhook_id      TEXT NOT NULL REFERENCES webhook_registrations(id) ON DELETE CASCADE,
    method          TEXT NOT NULL,
    headers         TEXT NOT NULL DEFAULT '{}',
    payload         TEXT NOT NULL DEFAULT '',
    response_status INTEGER NOT NULL DEFAULT 200,
    response_body   TEXT NOT NULL DEFAULT '',
    duration_ms     INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_webhook_log_hook ON webhook_event_log(webhook_id);
CREATE INDEX IF NOT EXISTS idx_webhook_log_created ON webhook_event_log(created_at DESC);

-- ============================================================
-- Auth Elements: per-user service credentials (LLM, Telegram, Google, etc.)
-- MOVED OUT of this user-data DB into the dedicated, attached `vault` DB so a
-- user-data reset never wipes credentials. The table is created in the vault by
-- VAULT_SCHEMA (see LocalBackend._init_db); every unqualified `auth_elements`
-- query resolves to the attached vault. Postgres keeps it as a normal table
-- (tables.py / render_postgres). Do NOT re-add a CREATE here — it would shadow
-- the vault copy via SQLite's main-first name resolution.
-- ============================================================

-- ============================================================
-- User Profiles: admin flag and per-user preferences
-- ============================================================
CREATE TABLE IF NOT EXISTS user_profiles (
    user_id             TEXT PRIMARY KEY,
    is_admin            INTEGER NOT NULL DEFAULT 0,
    default_agent_id    TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now')),
    last_login_at       TEXT
);

-- Personal cross-device ordering for the compact mobile Agents sidebar. This
-- is intentionally separate from shared agent records and profile metadata.
CREATE TABLE IF NOT EXISTS user_agent_sidebar_preferences (
    user_id                 TEXT PRIMARY KEY,
    favorite_agent_ids_json TEXT NOT NULL DEFAULT '[]',
    revision                INTEGER NOT NULL DEFAULT 0,
    updated_at              TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Personal cross-device ordering for Agent-card subpage tabs. Separate from
-- sidebar favorites because this is one global page layout per user.
CREATE TABLE IF NOT EXISTS user_agent_page_preferences (
    user_id                TEXT PRIMARY KEY,
    favorite_page_ids_json TEXT NOT NULL DEFAULT '[]',
    revision               INTEGER NOT NULL DEFAULT 0,
    updated_at             TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Personal cross-device ordering for the My Agents (All Agents) card's subpage
-- tabs. Deliberately separate from user_agent_page_preferences: the id sets are
-- different (the All Agents key 'models' is not a per-agent catalog page), so the
-- two preferences cannot share one record.
CREATE TABLE IF NOT EXISTS user_my_agents_page_preferences (
    user_id                TEXT PRIMARY KEY,
    favorite_page_ids_json TEXT NOT NULL DEFAULT '[]',
    revision               INTEGER NOT NULL DEFAULT 0,
    updated_at             TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Account/identity plane — login credentials (was app/auth/users.json).
-- Central table (never per-tenant routed; see app/db/router.py CONTROL_METHODS).
-- password_hash is bcrypt, never plaintext. Sibling of user_profiles by user_id.
CREATE TABLE IF NOT EXISTS model_rosters (
    id TEXT PRIMARY KEY, slug TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '', entries_json TEXT NOT NULL DEFAULT '[]',
    default_entry_id TEXT, status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft','published','retired')),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    published_revision INTEGER,
    source TEXT NOT NULL DEFAULT 'admin', created_by TEXT, updated_by TEXT,
    published_at TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_model_rosters_status ON model_rosters(status);

CREATE TABLE IF NOT EXISTS model_roster_revisions (
    roster_id TEXT NOT NULL REFERENCES model_rosters(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    payload_json TEXT NOT NULL,
    action TEXT NOT NULL DEFAULT 'published',
    created_by TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (roster_id, revision)
);
CREATE INDEX IF NOT EXISTS idx_model_roster_revisions_created
    ON model_roster_revisions(roster_id, created_at DESC);

CREATE TABLE IF NOT EXISTS experience_tiers (
    id TEXT PRIMARY KEY, slug TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '', policy_json TEXT NOT NULL DEFAULT '{}',
    policy_schema_version INTEGER NOT NULL DEFAULT 1 CHECK (policy_schema_version >= 1),
    roster_id TEXT REFERENCES model_rosters(id), is_system INTEGER NOT NULL DEFAULT 0,
    is_locked INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft','published','retired')),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    published_revision INTEGER,
    created_by TEXT, updated_by TEXT, published_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_experience_tiers_status ON experience_tiers(status);
CREATE INDEX IF NOT EXISTS idx_experience_tiers_roster ON experience_tiers(roster_id);

CREATE TABLE IF NOT EXISTS experience_tier_revisions (
    tier_id TEXT NOT NULL REFERENCES experience_tiers(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    payload_json TEXT NOT NULL,
    action TEXT NOT NULL DEFAULT 'published',
    created_by TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (tier_id, revision)
);
CREATE INDEX IF NOT EXISTS idx_experience_tier_revisions_created
    ON experience_tier_revisions(tier_id, created_at DESC);

CREATE TABLE IF NOT EXISTS user_tier_assignments (
    id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
    tier_id TEXT NOT NULL REFERENCES experience_tiers(id),
    source TEXT NOT NULL CHECK (source IN ('default','manual','billing','import','system')),
    starts_at TEXT NOT NULL DEFAULT (datetime('now')), expires_at TEXT,
    assigned_by TEXT, reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_tier_assignments_user
    ON user_tier_assignments(user_id, starts_at DESC);
CREATE INDEX IF NOT EXISTS idx_tier_assignments_tier ON user_tier_assignments(tier_id);

CREATE TABLE IF NOT EXISTS entitlement_audit_events (
    id TEXT PRIMARY KEY, subject_user_id TEXT, actor_user_id TEXT,
    action TEXT NOT NULL, entity_type TEXT NOT NULL, entity_id TEXT,
    previous_json TEXT, new_json TEXT, reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_entitlement_audit_subject
    ON entitlement_audit_events(subject_user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_entitlement_audit_entity
    ON entitlement_audit_events(entity_type, entity_id, created_at DESC);

-- Durable app-credit allowance ledger. principal_key is an account id for
-- registered tiers or a one-way coarse-network hash for Anonymous; raw network
-- data is never persisted. Reservations make admission atomic and actual usage
-- is reconciled against immutable usage_events separately.
CREATE TABLE IF NOT EXISTS tier_credit_allowances (
    id TEXT PRIMARY KEY, scope_kind TEXT NOT NULL CHECK(scope_kind IN ('account','network')),
    principal_key TEXT NOT NULL, tier_id TEXT NOT NULL, tier_revision INTEGER NOT NULL,
    period_kind TEXT NOT NULL CHECK(period_kind IN ('weekly','monthly','one_time')),
    period_key TEXT NOT NULL, limit_units REAL, reserved_units REAL NOT NULL DEFAULT 0,
    used_units REAL NOT NULL DEFAULT 0, reset_generation INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')), updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(scope_kind, principal_key, tier_id, tier_revision, period_kind, period_key, reset_generation)
);
CREATE INDEX IF NOT EXISTS idx_tier_credit_allowances_subject
    ON tier_credit_allowances(scope_kind, principal_key, updated_at DESC);
CREATE TABLE IF NOT EXISTS tier_credit_resets (
    id TEXT PRIMARY KEY, scope_kind TEXT NOT NULL, principal_key TEXT NOT NULL,
    tier_id TEXT, actor_user_id TEXT NOT NULL, reason TEXT NOT NULL,
    reset_generation INTEGER NOT NULL, created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
-- A reservation can debit both a tier allowance and a shared network ceiling.
-- Composite identity keeps retries idempotent without duplicating usage.
CREATE TABLE IF NOT EXISTS tier_credit_reservations (
    reservation_id TEXT NOT NULL,
    allowance_id TEXT NOT NULL REFERENCES tier_credit_allowances(id) ON DELETE CASCADE,
    estimated_units REAL NOT NULL,
    actual_units REAL,
    status TEXT NOT NULL CHECK(status IN ('reserved','reconciled','released')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    reconciled_at TEXT,
    PRIMARY KEY(reservation_id, allowance_id)
);
CREATE INDEX IF NOT EXISTS idx_tier_credit_reservations_status
    ON tier_credit_reservations(reservation_id, status);

CREATE TABLE IF NOT EXISTS user_accounts (
    user_id                     TEXT PRIMARY KEY,
    username                    TEXT NOT NULL UNIQUE,
    password_hash               TEXT NOT NULL DEFAULT '',
    display_name                TEXT NOT NULL DEFAULT '',
    remember_token              TEXT NOT NULL DEFAULT '',
    is_approved                 INTEGER NOT NULL DEFAULT 1,
    session_lifetime_minutes    INTEGER NOT NULL DEFAULT 43200,
    auto_renew                  INTEGER NOT NULL DEFAULT 1,
    social_links                TEXT NOT NULL DEFAULT '{}',
    created_at                  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at                  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_user_accounts_remember ON user_accounts(remember_token);

-- ============================================================
-- Per-Agent External Data Sources
-- Each agent can attach data sources (SQL DBs, doc stores, REST APIs,
-- domain-scoped web search, etc). Connector implementations live in
-- app/connectors/. The agent gains synthetic tools at load time
-- (not stored in the `tools` table — rebuilt every request).
-- ============================================================
CREATE TABLE IF NOT EXISTS data_sources (
    id                    TEXT PRIMARY KEY,
    user_id               TEXT NOT NULL,
    name                  TEXT NOT NULL,
    type                  TEXT NOT NULL,   -- validated in code by CONNECTOR_REGISTRY (drop-in); no CHECK so new connector files need no schema edit
    config                TEXT NOT NULL DEFAULT '{}',   -- JSON: non-sensitive
    auth_element_id       TEXT,                          -- FK -> auth_elements(id), nullable
    schema_cache          TEXT NOT NULL DEFAULT '{}',   -- JSON: introspected tables/endpoints
    safety_policy         TEXT NOT NULL DEFAULT '{}',   -- JSON: allowed_statements, allowed_tables, row_limit, destructive
    status                TEXT NOT NULL DEFAULT 'unverified'
                          CHECK (status IN ('unverified','active','error','disabled')),
    last_test_message     TEXT,
    last_tested_at        TEXT,
    last_introspected_at  TEXT,
    created_at            TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at            TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(user_id, name)
);

CREATE INDEX IF NOT EXISTS idx_data_sources_user ON data_sources(user_id);
CREATE INDEX IF NOT EXISTS idx_data_sources_type ON data_sources(type);

CREATE TABLE IF NOT EXISTS agent_data_sources (
    id                       TEXT PRIMARY KEY,
    agent_id                 TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    data_source_id           TEXT NOT NULL REFERENCES data_sources(id) ON DELETE CASCADE,
    tool_alias               TEXT,
    enabled                  INTEGER NOT NULL DEFAULT 1,
    inject_schema_in_prompt  INTEGER NOT NULL DEFAULT 1,
    created_at               TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(agent_id, data_source_id)
);

CREATE INDEX IF NOT EXISTS idx_agent_data_sources_agent ON agent_data_sources(agent_id);
CREATE INDEX IF NOT EXISTS idx_agent_data_sources_source ON agent_data_sources(data_source_id);

-- Chunked + embedded docs for doc_store connectors. Mirrors memory_chunks shape
-- but keyed by data_source_id so customer wiki content stays separate from
-- a user's personal memory.
CREATE TABLE IF NOT EXISTS doc_chunks (
    id              TEXT PRIMARY KEY,
    data_source_id  TEXT NOT NULL REFERENCES data_sources(id) ON DELETE CASCADE,
    source_ref      TEXT NOT NULL DEFAULT '',   -- file path / URL / page key
    chunk_index     INTEGER NOT NULL DEFAULT 0,
    chunk_text      TEXT NOT NULL,
    content_hash    TEXT,                        -- file-level hash for re-embed avoidance
    embedding       BLOB,                        -- numpy float32 array
    token_count     INTEGER,
    metadata        TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_doc_chunks_source ON doc_chunks(data_source_id);
CREATE INDEX IF NOT EXISTS idx_doc_chunks_hash ON doc_chunks(content_hash);

CREATE VIRTUAL TABLE IF NOT EXISTS doc_chunks_fts USING fts5(
    chunk_text,
    source_ref UNINDEXED,
    content='doc_chunks',
    content_rowid='rowid',
    tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS trg_doc_chunks_fts_insert
    AFTER INSERT ON doc_chunks BEGIN
    INSERT INTO doc_chunks_fts(rowid, chunk_text, source_ref)
    VALUES (new.rowid, new.chunk_text, new.source_ref);
END;

CREATE TRIGGER IF NOT EXISTS trg_doc_chunks_fts_delete
    AFTER DELETE ON doc_chunks BEGIN
    INSERT INTO doc_chunks_fts(doc_chunks_fts, rowid, chunk_text, source_ref)
    VALUES ('delete', old.rowid, old.chunk_text, old.source_ref);
END;

CREATE TRIGGER IF NOT EXISTS trg_doc_chunks_fts_update
    AFTER UPDATE ON doc_chunks BEGIN
    INSERT INTO doc_chunks_fts(doc_chunks_fts, rowid, chunk_text, source_ref)
    VALUES ('delete', old.rowid, old.chunk_text, old.source_ref);
    INSERT INTO doc_chunks_fts(rowid, chunk_text, source_ref)
    VALUES (new.rowid, new.chunk_text, new.source_ref);
END;

-- ============================================================
-- Tenant Key Metadata (encryption support)
-- Tracks which DEK versions exist per tenant. NO key material here —
-- wrapped DEKs live in the configured SecretsBackend at
-- wa:dek:<user_id>:v<key_version>.
-- ============================================================
CREATE TABLE IF NOT EXISTS tenant_key_meta (
    user_id      TEXT    NOT NULL,
    key_version  INTEGER NOT NULL,
    algo         TEXT    NOT NULL DEFAULT 'fernet',
    status       TEXT    NOT NULL DEFAULT 'active' CHECK (status IN ('active','retired')),
    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    retired_at   TEXT,
    PRIMARY KEY (user_id, key_version)
);

CREATE INDEX IF NOT EXISTS idx_tenant_key_meta_active
    ON tenant_key_meta(user_id, status);

-- ============================================================
-- Billing / monetization tables
-- ============================================================

-- Per-agent pricing config, keyed by scope 'agent:<id>'.
-- strategy may hold a comma-separated combination (e.g. 'trial,credits'); the
-- API validates it via parse_strategy_selection (no DB-level CHECK so combined
-- values are storable).
CREATE TABLE IF NOT EXISTS billing_configs (
    scope                      TEXT    PRIMARY KEY,
    strategy                   TEXT    NOT NULL DEFAULT 'free',
    allowed_strategies         TEXT    NOT NULL DEFAULT '[]',
    allowed_processors         TEXT    NOT NULL DEFAULT '[]',
    rate_card_default_llm      TEXT    NOT NULL DEFAULT '{}',
    rate_card_byo_llm          TEXT    NOT NULL DEFAULT '{}',
    -- Cost-based pricing overrides. NULL = inherit the platform default
    -- (platform_billing_config is the source of truth; the agent row only
    -- overrides when a value is explicitly set).
    cost_multiplier            REAL,
    min_charge_cents           INTEGER,
    flat_image_cost_usd        REAL,
    trial_config               TEXT    NOT NULL DEFAULT '{}',
    subscription_price_cents   INTEGER NOT NULL DEFAULT 0,
    currency                   TEXT    NOT NULL DEFAULT 'usd',
    created_at                 TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at                 TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_by                 TEXT
);

-- Per-LLM-call usage + billing record. Always inserted, even for free/exempt
-- chats (with zero amounts and is_exempt=1) so admins see full traffic.
CREATE TABLE IF NOT EXISTS usage_events (
    id                          TEXT PRIMARY KEY,
    agent_id                    TEXT NOT NULL,
    user_id                     TEXT NOT NULL,
    interaction_id              TEXT,
    input_tokens                INTEGER NOT NULL DEFAULT 0,
    output_tokens               INTEGER NOT NULL DEFAULT 0,
    provider_cost_cents         INTEGER NOT NULL DEFAULT 0,
    end_user_charge_cents       INTEGER NOT NULL DEFAULT 0,
    agent_admin_earnings_cents  INTEGER NOT NULL DEFAULT 0,
    strategy                    TEXT NOT NULL DEFAULT 'free',
    is_byo_llm                  INTEGER NOT NULL DEFAULT 0,
    is_trial                    INTEGER NOT NULL DEFAULT 0,
    is_exempt                   INTEGER NOT NULL DEFAULT 0,
    model                       TEXT,
    provider                    TEXT,
    -- MODEL-ROW-USAGE: the exact saved model-connection row (roster entry_id)
    -- that served this call. model+provider can repeat across rows (same model
    -- with a different credential / effort / injections), so per-row usage must
    -- key on this. NULL on legacy rows written before the column existed.
    entry_id                    TEXT,
    -- Canonical, locked-in cost of this single call in USD: computed at save
    -- time from the model's published per-1M price × this call's tokens, so it
    -- never re-prices when the session later switches models. Summing this
    -- column gives accurate session / agent / global cost. provider_cost_cents
    -- above stays as the secondary "actually billed" figure (only some
    -- providers report it). cost_source records which estimate we used.
    cost_usd                    REAL NOT NULL DEFAULT 0,
    cost_source                 TEXT,
    -- Immutable user-facing house-credit charge for this call. Existing rows
    -- remain zero; no historical chats are repriced or backfilled.
    credit_units                REAL NOT NULL DEFAULT 0,
    credit_source               TEXT,
    -- Provider-native usage detail. These make cached-input pricing auditable
    -- and let the UI show the exact prompt size of the latest completed call.
    cached_input_tokens         INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens          INTEGER NOT NULL DEFAULT 0,
    uncached_input_tokens       INTEGER,
    reasoning_tokens            INTEGER NOT NULL DEFAULT 0,
    -- Direct session attribution (chat rows), so totals don't depend on a join
    -- through interaction_id. NULL for background rows that belong to no chat.
    session_id                  TEXT,
    -- 'chat' for agent turns, 'background' for git messages / placeholder text /
    -- embeddings / titles. Background rows use 'system' for agent_id/user_id.
    source                      TEXT NOT NULL DEFAULT 'chat',
    created_at                  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_usage_events_agent_created
    ON usage_events(agent_id, created_at);
CREATE INDEX IF NOT EXISTS idx_usage_events_user_created
    ON usage_events(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_usage_events_model_created
    ON usage_events(model, created_at);
-- NOTE: the session_id index is intentionally NOT created here. session_id is a
-- migrated column (added by ALTER below for DBs created before it existed). This
-- SCHEMA_SQL runs *before* that migration, so indexing session_id here would
-- raise "no such column: session_id" on any pre-existing DB and abort the whole
-- executescript. The migration block creates idx_usage_events_session after the
-- ALTER, which covers fresh and upgraded DBs alike.

-- Credit wallets. owner_type='user' for end-user purchased credits;
-- owner_type='agent_admin' for informational earnings (real money lives in
-- the payment processor's connected account).
CREATE TABLE IF NOT EXISTS wallets (
    id            TEXT PRIMARY KEY,
    owner_type    TEXT NOT NULL CHECK (owner_type IN ('user','agent_admin')),
    owner_id      TEXT NOT NULL,
    balance_cents INTEGER NOT NULL DEFAULT 0,
    hold_cents    INTEGER NOT NULL DEFAULT 0,
    currency      TEXT NOT NULL DEFAULT 'usd',
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(owner_type, owner_id, currency)
);

-- Immutable ledger of every wallet movement.
CREATE TABLE IF NOT EXISTS wallet_transactions (
    id          TEXT PRIMARY KEY,
    wallet_id   TEXT NOT NULL,
    delta_cents INTEGER NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN
        ('purchase','usage','refund','earnings','hold','release')),
    ref_id      TEXT,
    note        TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_wallet_tx_wallet ON wallet_transactions(wallet_id);

-- One subscription per (user, agent). Processor is whichever the user paid via.
CREATE TABLE IF NOT EXISTS subscriptions (
    id                       TEXT PRIMARY KEY,
    user_id                  TEXT NOT NULL,
    agent_id                 TEXT NOT NULL,
    processor                TEXT NOT NULL,
    external_subscription_id TEXT,
    status                   TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','active','past_due','cancelled','expired')),
    current_period_end       TEXT,
    created_at               TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at               TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(user_id, agent_id)
);

CREATE INDEX IF NOT EXISTS idx_subscriptions_user ON subscriptions(user_id);
CREATE INDEX IF NOT EXISTS idx_subscriptions_agent ON subscriptions(agent_id);

-- One trial allotment per (user, agent). The trial is a credit grant:
-- credit_cents = original allotment, remaining_cents burns down with the same
-- cost × multiplier charges as purchased credits. Legacy columns kept for rows
-- created before the credit-grant cutover (the engine ignores them).
CREATE TABLE IF NOT EXISTS trials (
    id                  TEXT PRIMARY KEY,
    user_id             TEXT NOT NULL,
    agent_id            TEXT NOT NULL,
    started_at          TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at          TEXT,
    credit_cents        INTEGER,
    remaining_cents     INTEGER,
    messages_remaining  INTEGER,
    tokens_remaining    INTEGER,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(user_id, agent_id)
);

CREATE INDEX IF NOT EXISTS idx_trials_user_agent ON trials(user_id, agent_id);

-- Source of truth for inbound payments. Webhook handlers write here.
CREATE TABLE IF NOT EXISTS payments (
    id                  TEXT PRIMARY KEY,
    processor           TEXT NOT NULL,
    external_payment_id TEXT,
    user_id             TEXT NOT NULL,
    agent_id            TEXT,
    amount_cents        INTEGER NOT NULL DEFAULT 0,
    currency            TEXT NOT NULL DEFAULT 'usd',
    kind                TEXT NOT NULL CHECK (kind IN ('purchase','subscription','one_off')),
    status              TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','completed','failed','refunded')),
    metadata            TEXT NOT NULL DEFAULT '{}',
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_payments_user ON payments(user_id);
CREATE INDEX IF NOT EXISTS idx_payments_external
    ON payments(processor, external_payment_id);

-- Exemption rules. Three kinds:
--   'agent'          — whole agent is free   (agent_id set, user_id NULL)
--   'user'           — user is exempt across all agents (user_id set, agent_id NULL)
--   'user_for_agent' — user is exempt for one agent     (both set)
CREATE TABLE IF NOT EXISTS billing_exemptions (
    id                  TEXT PRIMARY KEY,
    kind                TEXT NOT NULL CHECK (kind IN ('agent','user','user_for_agent')),
    agent_id            TEXT,
    user_id             TEXT,
    granted_by_user_id  TEXT,
    reason              TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_exemptions_agent ON billing_exemptions(agent_id);
CREATE INDEX IF NOT EXISTS idx_exemptions_user  ON billing_exemptions(user_id);
CREATE INDEX IF NOT EXISTS idx_exemptions_kind  ON billing_exemptions(kind);

-- ============================================================
-- Gen UI (the genui workspace)
-- Used by DatabaseGenuiStore and HybridGenuiStore. In hybrid mode the
-- `html` column stays NULL — the body lives on disk.
-- ============================================================
CREATE TABLE IF NOT EXISTS genui (
    id            TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL,
    slug          TEXT NOT NULL,
    title         TEXT NOT NULL,
    agent_context TEXT NOT NULL DEFAULT '',
    -- agent_id: the agent that created/manages this genui (mirrors
    -- browser_sessions.agent_id). Nullable — a user-made genui has none until
    -- an agent renders into it; the footer falls back to the default WebAgent.
    agent_id      TEXT,
    html          TEXT,
    -- session_config: JSON object describing how this genui's actions/chat
    -- target agent sessions. REQUIRED at create time. Shape:
    --   { "target_name": "Code Index — initial scan",   // mandatory session title
    --     "mode": "new_reuse" | "new_each" | "existing", // mandatory
    --     "session_id": "uuid" }                        // required iff mode==existing
    -- new_reuse (default): first dispatch creates a session titled target_name and
    --   reuses it for follow-ups; new_each: every action starts a fresh session;
    --   existing: the page always dispatches into the given session id.
    session_config TEXT NOT NULL DEFAULT '{}',
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(user_id, slug)
);

CREATE INDEX IF NOT EXISTS idx_genui_user ON genui(user_id);

-- NOTE: the company-wide Wiki lives in its OWN dedicated SQLite file
-- (data/wiki.db, see app/wiki/db.py) — not in this schema or the main backend.

"""


# ── Slot helpers ──────────────────────────────────────────────────────────────
# Prompts are stored as rows in `agent_prompts`, one per (agent, slot_name, user_id).
# user_id IS NULL = admin base (canonical); non-null = override owned by that user.

VALID_MERGE_MODES = ("replace", "append")


def _slot_apply(base: str, override: Optional[str], lock: bool, merge_mode: str) -> str:
    """Resolve a single slot's effective content given admin base + optional user override."""
    base = base or ""
    if lock or override is None:
        return base
    if merge_mode == "append":
        if not base.strip():
            return override or ""
        if not (override or "").strip():
            return base
        return base.rstrip() + "\n\n" + override.lstrip()
    # default / "replace"
    return override


# ── Three-vault secrets architecture ───────────────────────────────────────────
# Secrets are split by ownership into three dedicated database files, each
# attached under its own schema so unqualified ``auth_elements`` fall-through
# never happens — every query targets the correct vault explicitly.
#
#   vault_app   → app_secrets.db   (App infra: deploy keys, LLM fallback, OAuth
#                                   app creds, DNS keys, encryption master keys)
#   vault_agent → agent_secrets.db (Per-agent: OAuth tokens, ability creds, BYO
#                                   client secrets, per-user LLM overrides)
#   vault_user  → user_secrets.db  (Per-user: genui vault keys, browser cookies,
#                                   user-scoped ability creds)
#
# All three use the SAME table schema — a mirror of the legacy ``auth_elements``.
# Postgres keeps them as three normal tables in the main database (tables.py).
#
# The dispatch function ``_vault_for(user_id, service, label)`` decides which
# vault a call targets; the auth_element_* methods route transparently. Legacy
# ``vault.db`` rows are migrated to the correct vaults on startup.

# Current schema version of the auth_elements table. Bump this when the DDL
# changes (add/remove/rename columns) so P2P sync can detect incompatibility.
AUTH_ELEMENTS_SCHEMA_VERSION = 1

_AUTH_ELEMENTS_DDL = """
CREATE TABLE IF NOT EXISTS {schema}.auth_elements (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    service         TEXT NOT NULL,
    label           TEXT NOT NULL DEFAULT 'default',
    config          TEXT NOT NULL DEFAULT '{{}}',
    secret_ref      TEXT NOT NULL DEFAULT '',
    is_active       INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    _version        INTEGER NOT NULL DEFAULT 1,
    _schema_version INTEGER NOT NULL DEFAULT 1
);
CREATE UNIQUE INDEX IF NOT EXISTS {schema}.idx_auth_elements_user_service_label
    ON auth_elements(user_id, service, label);
"""

_VAULT_SCHEMAS = {
    "vault_app":   _AUTH_ELEMENTS_DDL.format(schema="vault_app"),
    "vault_agent": _AUTH_ELEMENTS_DDL.format(schema="vault_agent"),
    "vault_user":  _AUTH_ELEMENTS_DDL.format(schema="vault_user"),
}

# Legacy — kept for one-shot migration; removed after all rows are relocated.
VAULT_SCHEMA = """
CREATE TABLE IF NOT EXISTS vault.auth_elements (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    service         TEXT NOT NULL,
    label           TEXT NOT NULL DEFAULT 'default',
    config          TEXT NOT NULL DEFAULT '{}',
    secret_ref      TEXT NOT NULL DEFAULT '',
    is_active       INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    _version        INTEGER NOT NULL DEFAULT 1,
    _schema_version INTEGER NOT NULL DEFAULT 1
);
CREATE UNIQUE INDEX IF NOT EXISTS vault.idx_auth_elements_user_service_label
    ON auth_elements(user_id, service, label);
"""


def _vault_for(user_id: str, service: str, label: str = "") -> str:
    """Route an auth_elements call to the correct vault schema.

    Rules (ordered — first match wins):
      1. Master encryption keys → vault_app  (user_id="_vault", service="_secrets_vault")
      2. Admin infra services → vault_app    (deploy, DNS, OAuth app creds, channels, global LLM)
      3. User-scoped services → vault_user   (genui_vault, browser_session)
      4. Everything else → vault_agent       (OAuth tokens, ability creds, per-user LLM, BYO secrets)

    "Agent owns it when in doubt" is rule 4.
    """
    # Rule 1 — encryption root of trust
    if user_id == "_vault" and service == "_secrets_vault":
        return "vault_app"

    # Rule 2 — admin-scoped infrastructure
    # Named platform rosters belong to the app control plane, independent of
    # any human admin, selected agent, or user database.
    if user_id == "_platform" and service in {"llm_roster", "openai_subscription_oauth"}:
        return "vault_app"

    if user_id == "admin":
        infra_prefixes = (
            "deploy_cred:", "deploy_connect:", "dns_cred:",
            "deploy_github_token", "production_mirror",
        )
        if service.startswith(infra_prefixes):
            return "vault_app"
        if service.endswith("_oauth_config"):
            return "vault_app"
        if service.startswith("social_login:"):
            return "vault_app"
        if service.startswith("channel_"):
            return "vault_app"
        if service.startswith("oauth_ability_"):
            return "vault_app"
        if service == "tool_defaults":
            return "vault_app"
        # Global LLM fallback — admin's "llm" row is the shared default
        if service == "llm":
            return "vault_app"
        # Admin-scoped ability creds — label is "default" for admin scope
        if service.startswith("ability_cred:") and label == "default":
            return "vault_app"
        # image_gen legacy fallback
        if service == "image_gen":
            return "vault_app"

    # Rule 3 — user-scoped (per-user vault keys, browser sessions)
    if service in ("genui_vault", "browser_session"):
        return "vault_user"

    # Rule 3a — ability creds with user scope (label="default", non-admin user)
    if service.startswith("ability_cred:") and label == "default" and user_id != "admin":
        return "vault_user"

    # Rule 4 — default: agent owns it
    return "vault_agent"


def _vault_path_for(db_path: str) -> str:
    """Return the legacy vault path used only for one-shot secret relocation."""
    d, base = os.path.split(db_path)
    stem = base[:-3] if base.endswith(".db") else base
    name = "vault.db" if stem in {"local", "app"} else f"{stem}.vault.db"
    return os.path.join(d or ".", name)


def _app_vault_path(db_path: str = DEFAULT_DB_PATH) -> str:
    d, base = os.path.split(db_path)
    stem = base[:-3] if base.endswith(".db") else base
    name = "app_secrets.db" if stem in {"local", "app"} else f"{stem}.app_secrets.db"
    return os.path.join(d or ".", name)


def _agent_vault_path(db_path: str = DEFAULT_DB_PATH) -> str:
    d, base = os.path.split(db_path)
    stem = base[:-3] if base.endswith(".db") else base
    name = "agent_secrets.db" if stem in {"local", "app"} else f"{stem}.agent_secrets.db"
    return os.path.join(d or ".", name)


def _user_vault_path(db_path: str = DEFAULT_DB_PATH) -> str:
    d, base = os.path.split(db_path)
    stem = base[:-3] if base.endswith(".db") else base
    name = "user_secrets.db" if stem in {"local", "app"} else f"{stem}.user_secrets.db"
    return os.path.join(d or ".", name)


class _DbWriteLock:
    """Serializes DB writes across BOTH the main event loop and worker threads.

    The write-guard used to be an ``asyncio.Lock``, which is bound to the loop it
    was created on. That made it impossible to run a write coroutine in a worker
    thread (``db_offload``) — the loop-bound lock threw "is bound to a different
    event loop" and killed the turn (message stuck at "pending"). Yet offloading
    the blocking round-trips to worker threads is exactly what keeps a remote DB
    from freezing the shared loop (and stalling the LLM stream).

    A ``threading.Semaphore(1)`` is a non-reentrant mutex with NO thread
    ownership, so (unlike ``RLock``) it can be released from a different thread
    than acquired — which is required because ``__aenter__`` may acquire via an
    executor thread while ``__aexit__`` releases on the caller's thread. Its
    non-reentrant semantics match the original ``asyncio.Lock`` exactly (the code
    has no nested writes), so behaviour is unchanged; only the cross-loop/thread
    capability is added.

    Supports BOTH ``async with`` (the 100+ existing write sites) and plain
    ``with`` (for code running inside a worker-thread loop). On the main loop a
    *contended* acquire is awaited via the executor so the loop is never frozen
    waiting for the lock; an *uncontended* acquire takes the instant fast path.
    """

    __slots__ = ("_sem",)

    def __init__(self) -> None:
        self._sem = threading.Semaphore(1)

    # -- sync context (worker-thread paths) --
    def __enter__(self):
        self._sem.acquire()
        return self

    def __exit__(self, *exc):
        self._sem.release()
        return False

    # -- async context (the inherited `async with self._write_lock` sites) --
    async def __aenter__(self):
        # Fast path: lock free → grab it without touching the loop/executor.
        if self._sem.acquire(blocking=False):
            return self
        # Contended: wait WITHOUT freezing the loop by blocking in the executor.
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._sem.acquire)
        return self

    async def __aexit__(self, *exc):
        self._sem.release()
        return False


def _cleanup_legacy_vault_file(path: str) -> None:
    """Remove the legacy vault.db file and any WAL/journal sidecars."""
    for _sfx in ("", "-wal", "-shm", "-journal"):
        _sp = path + _sfx
        if os.path.exists(_sp):
            try:
                os.remove(_sp)
            except Exception:
                pass


# ── One-shot vault migration: legacy vault.db → three new vaults ─────────────
# Runs inside _init_db on the first boot after the three-vault split is deployed.
# Idempotent — once completed the legacy vault.db file is deleted and the
# function becomes a no-op on subsequent boots.
def _migrate_vault_to_three(
    conn: sqlite3.Connection,
    legacy_path: str,
    app_path: str,
    agent_path: str,
    user_path: str,
) -> None:
    """Relocate every row from legacy vault.db into the correct new vault.
    After verifying all rows are present in the new vaults, deletes the
    legacy vault.db file. Safe to re-run — a missing legacy file is a no-op."""
    if not os.path.exists(legacy_path):
        return

    # Ensure legacy vault is attached (it may have been attached in _get_conn)
    try:
        conn.execute("SELECT 1 FROM vault.auth_elements LIMIT 0")
    except Exception:
        # Legacy vault exists on disk but wasn't attached — attach it now
        try:
            conn.execute("ATTACH DATABASE ? AS vault", (legacy_path,))
        except Exception:
            pass

    # Check if the auth_elements table still exists in the attached vault.
    # A previous migration may have dropped it but left the empty file.
    try:
        src_n = conn.execute("SELECT COUNT(*) FROM vault.auth_elements").fetchone()[0]
    except Exception:
        # Table already gone — clean up the empty file and return
        _cleanup_legacy_vault_file(legacy_path)
        return
    if src_n == 0:
        # Empty vault — clean up the file
        _cleanup_legacy_vault_file(legacy_path)
        return

    rows = conn.execute("SELECT * FROM vault.auth_elements").fetchall()
    migrated = 0
    for row in rows:
        uid = row["user_id"]
        svc = row["service"]
        lbl = row["label"] or "default"
        target = _vault_for(uid, svc, lbl)

        # INSERT OR IGNORE — skip rows that somehow already exist in the target
        conn.execute(
            f"INSERT OR IGNORE INTO {target}.auth_elements "
            "(id, user_id, service, label, config, secret_ref, is_active, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (row["id"], uid, svc, lbl,
             row["config"] or "{}", row["secret_ref"] or "",
             row["is_active"] or 1, row["created_at"] or "", row["updated_at"] or ""),
        )
        migrated += 1

    conn.commit()

    # Verify — count rows in all three targets
    target_n = 0
    for target in ("vault_app", "vault_agent", "vault_user"):
        try:
            n = conn.execute(f"SELECT COUNT(*) FROM {target}.auth_elements").fetchone()[0]
            target_n += n
        except Exception:
            pass

    if target_n >= src_n:
        # All rows accounted for — drop the legacy table and delete the file
        try:
            conn.execute("DROP TABLE IF EXISTS vault.auth_elements")
            conn.commit()
            try:
                conn.execute("DETACH DATABASE vault")
            except Exception:
                pass
            _cleanup_legacy_vault_file(legacy_path)
            logger.info(
                "Three-vault migration complete: %d rows relocated from %s → "
                "app_secrets.db / agent_secrets.db / user_secrets.db",
                src_n, legacy_path,
            )
        except Exception as e:
            logger.warning("Legacy vault cleanup failed (non-fatal): %s", e)
    else:
        logger.warning(
            "Three-vault migration incomplete (%d of %d rows relocated) — "
            "keeping legacy vault.db",
            target_n, src_n,
        )


def _migrate_auth_elements_schema_version(conn):
    """Add _version and _schema_version columns to all auth_elements tables if missing.

    Idempotent — checks each column per schema before ALTER. Called during _init_db
    right after the three-vault split migration, and safe to call on any version.
    """
    for schema in ("vault_app", "vault_agent", "vault_user", "vault"):
        try:
            cols = {r[1] for r in conn.execute(f"PRAGMA {schema}.table_info(auth_elements)").fetchall()}
        except Exception:
            continue
        if "_version" not in cols:
            conn.execute(f"ALTER TABLE {schema}.auth_elements ADD COLUMN _version INTEGER NOT NULL DEFAULT 1")
        if "_schema_version" not in cols:
            conn.execute(f"ALTER TABLE {schema}.auth_elements ADD COLUMN _schema_version INTEGER NOT NULL DEFAULT 1")
    conn.commit()


def _migrate_model_roster_publications(conn) -> None:
    """Backfill immutable live snapshots for legacy mutable published rosters."""
    roster_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(model_rosters)").fetchall()
    }
    if not roster_cols:
        return
    if "published_revision" not in roster_cols:
        conn.execute("ALTER TABLE model_rosters ADD COLUMN published_revision INTEGER")
    for roster_row in conn.execute(
        "SELECT * FROM model_rosters WHERE status='published' AND published_revision IS NULL"
    ).fetchall():
        roster = dict(roster_row)
        revision = max(1, int(roster.get("revision") or 1))
        payload = {
            key: roster.get(key) for key in (
                "slug", "name", "description", "entries_json",
                "default_entry_id", "source",
            )
        }
        conn.execute(
            """INSERT OR IGNORE INTO model_roster_revisions
               (roster_id, revision, payload_json, action, created_by, created_at)
               VALUES (?, ?, ?, 'compatibility-backfill', ?, ?)""",
            (roster["id"], revision,
             json.dumps(payload, separators=(",", ":"), sort_keys=True),
             roster.get("updated_by") or roster.get("created_by") or "migration",
             roster.get("published_at") or roster.get("updated_at") or _now_iso()),
        )
        conn.execute(
            "UPDATE model_rosters SET published_revision=? WHERE id=?",
            (revision, roster["id"]),
        )
    conn.commit()


def _migrate_experience_tier_publications(conn) -> None:
    """Backfill immutable live snapshots for legacy mutable published tiers."""
    tier_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(experience_tiers)").fetchall()
    }
    if not tier_cols:
        return
    if "published_revision" not in tier_cols:
        conn.execute("ALTER TABLE experience_tiers ADD COLUMN published_revision INTEGER")
    for tier_row in conn.execute(
        "SELECT * FROM experience_tiers WHERE status='published' AND published_revision IS NULL"
    ).fetchall():
        tier = dict(tier_row)
        revision = max(1, int(tier.get("revision") or 1))
        payload = {
            key: tier.get(key) for key in (
                "slug", "name", "description", "policy_json",
                "policy_schema_version", "roster_id", "is_system", "is_locked",
            )
        }
        conn.execute(
            """INSERT OR IGNORE INTO experience_tier_revisions
               (tier_id, revision, payload_json, action, created_by, created_at)
               VALUES (?, ?, ?, 'compatibility-backfill', ?, ?)""",
            (tier["id"], revision,
             json.dumps(payload, separators=(",", ":"), sort_keys=True),
             tier.get("updated_by") or tier.get("created_by") or "migration",
             tier.get("published_at") or tier.get("updated_at") or _now_iso()),
        )
        conn.execute(
            "UPDATE experience_tiers SET published_revision=? WHERE id=?",
            (revision, tier["id"]),
        )
    conn.commit()


def _migrate_attachment_session_fk(conn: sqlite3.Connection) -> bool:
    """Remove the legacy attachment-to-session FK when it is still present.

    Fresh chats upload against a client-generated session id before /chat/send
    creates the durable session row. Plane-scoped databases bypass the legacy
    catch-all migration block, so they need this migration during initialization.
    """
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='attachments'"
    ).fetchone()
    if not table:
        return False
    has_session_fk = any(
        row[3] == "session_id"
        for row in conn.execute("PRAGMA foreign_key_list(attachments)").fetchall()
    )
    if not has_session_fk:
        return False

    logger.info("Removing legacy attachments.session_id FK constraint")
    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DROP TABLE IF EXISTS attachments_without_session_fk")
        conn.execute(
            """CREATE TABLE attachments_without_session_fk (
                id               TEXT PRIMARY KEY,
                user_id          TEXT NOT NULL,
                session_id       TEXT,
                original_name    TEXT NOT NULL,
                mime_type        TEXT NOT NULL,
                size_bytes       INTEGER NOT NULL,
                storage_path     TEXT NOT NULL,
                storage_provider TEXT NOT NULL DEFAULT 'local',
                metadata         TEXT NOT NULL DEFAULT '{}',
                created_at       TEXT NOT NULL DEFAULT (datetime('now'))
            )"""
        )
        conn.execute(
            """INSERT INTO attachments_without_session_fk
               SELECT id, user_id, session_id, original_name, mime_type,
                      size_bytes, storage_path, storage_provider, metadata, created_at
               FROM attachments"""
        )
        conn.execute("DROP TABLE attachments")
        conn.execute(
            "ALTER TABLE attachments_without_session_fk RENAME TO attachments"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_attachments_user ON attachments(user_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_attachments_session ON attachments(session_id)"
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
    logger.info("Legacy attachments.session_id FK constraint removed")
    return True


class LocalBackend(StorageBackend):
    """SQLite implementation of StorageBackend."""

    def __init__(self, db_path: Optional[str] = None, seed: bool = True, plane: Optional[str] = None):
        self._db_path = db_path or DEFAULT_DB_PATH
        self._schema_plane = plane or ("app" if db_path is None else None)
        self._canonical_app_plane = False
        self._canonical_authority_plane = False
        if self._schema_plane == "app":
            try:
                from app.db.storage_layout import APP_DB_PATH
                self._canonical_app_plane = os.path.abspath(self._db_path) == os.path.abspath(str(APP_DB_PATH))
            except Exception:
                pass
        if self._schema_plane in {"app", "user", "agent"}:
            try:
                from app.db.storage_layout import APP_DB_PATH, PROJECT_ROOT
                target = os.path.abspath(self._db_path)
                roots = [
                    os.path.abspath(str(PROJECT_ROOT / "data" / "user_data")),
                    os.path.abspath(str(PROJECT_ROOT / "data" / "agent_data")),
                ]
                self._canonical_authority_plane = (
                    target == os.path.abspath(str(APP_DB_PATH))
                    or any(os.path.commonpath((target, root)) == root for root in roots)
                )
            except (Exception, ValueError):
                pass
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self._db_path)), exist_ok=True)
        except Exception:
            pass
        # Three-vault secrets architecture — credentials split by ownership.
        # Legacy vault.db still attached for one-shot migration on first boot.
        vault_base = DEFAULT_DB_PATH if self._canonical_authority_plane else self._db_path
        self._vault_path = _vault_path_for(vault_base)
        self._app_vault_path = _app_vault_path(vault_base)
        self._agent_vault_path = _agent_vault_path(vault_base)
        self._user_vault_path = _user_vault_path(vault_base)
        if self._schema_plane == "agent":
            # Agent credentials are owned and deleted with the agent, while app
            # infrastructure credentials stay in the installation authority.
            _agent_dir, _agent_file = os.path.split(self._db_path)
            _agent_stem = _agent_file[:-3] if _agent_file.endswith(".db") else _agent_file
            self._app_vault_path = _app_vault_path(DEFAULT_DB_PATH)
            self._agent_vault_path = os.path.join(_agent_dir, f"{_agent_stem}.agent-secrets.db")
            self._user_vault_path = _user_vault_path(DEFAULT_DB_PATH)
        elif self._schema_plane == "user":
            # User-owned model credentials follow the user's authority directory.
            # The auth_elements routing schema remains vault_agent for backward
            # compatibility, but its physical file is user-owned.
            _member_dir, _member_file = os.path.split(self._db_path)
            _member_stem = _member_file[:-3] if _member_file.endswith(".db") else _member_file
            _members_dir = os.path.dirname(_member_dir)
            self._app_vault_path = _app_vault_path(DEFAULT_DB_PATH)
            if os.path.basename(_members_dir).lower() == "members":
                # Agent-native members share agent-owned credentials while their
                # personal vault follows the member.
                _agent_dir = os.path.dirname(_members_dir)
                _agent_stem = os.path.basename(_agent_dir)
                self._agent_vault_path = os.path.join(
                    _agent_dir, "%s.agent-secrets.db" % _agent_stem
                )
                self._user_vault_path = os.path.join(
                    _member_dir, "%s.user-secrets.db" % _member_stem
                )
            else:
                self._agent_vault_path = os.path.join(
                    _member_dir, "%s.user-secrets.db" % _member_stem
                )
        # Full-DB-encryption ids: only the canonical app + vault files
        # participate in the at-rest encryption config. A
        # custom/test db_path stays plaintext regardless of the global setting.
        if self._canonical_app_plane:
            self._enc_main_id = "app"
            self._enc_vault_id = "vault"
            self._enc_app_vault_id = "app_secrets"
            self._enc_agent_vault_id = "agent_secrets"
            self._enc_user_vault_id = "user_secrets"
        elif self._db_path == DEFAULT_DB_PATH:
            self._enc_main_id = "app"
            self._enc_vault_id = "vault"  # legacy — still used during migration
            self._enc_app_vault_id = "app_secrets"
            self._enc_agent_vault_id = "agent_secrets"
            self._enc_user_vault_id = "user_secrets"
        else:
            self._enc_main_id = "_local_custom"
            self._enc_vault_id = "_vault_custom"
            self._enc_app_vault_id = "_app_secrets_custom"
            self._enc_agent_vault_id = "_agent_secrets_custom"
            self._enc_user_vault_id = "_user_secrets_custom"
        self._write_lock = _DbWriteLock()
        # Subclasses (PostgresBackend) flip these. seed=False builds a schema-only
        # reference instance (used to introspect the canonical column set).
        # _scan_sibling_dbs is a SQLite-only feature (parallel agent .db files)
        # that has no meaning on a server-backed store.
        self._seed_on_init = seed
        self._scan_sibling_dbs = True
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        """Get a new connection (thread-safe: each call gets its own).

        Tuned for many parallel agent writes. SQLite still allows only one writer
        at a time (the app already serialises writes through self._write_lock), so
        the goal here is to make each write *commit fast* and hold that single
        writer slot for as little time as possible:

          - journal_mode=WAL    Readers never block the writer (persistent, but
                                re-asserted cheaply on every connection).
          - synchronous=NORMAL  Biggest write-throughput win. In WAL mode this is
                                crash-SAFE (no corruption); the only cost is that a
                                hard power loss / OS crash can drop the last few
                                not-yet-checkpointed transactions. With FULL (the
                                default) every commit fsyncs to disk, which under a
                                burst of parallel writes serialises everyone behind
                                disk-sync latency.
          - busy_timeout=30000  A write that finds the file momentarily locked
                                waits up to 30s instead of erroring "database is
                                locked".
          - temp_store=MEMORY   Sorts / temp B-trees stay in RAM, off the hot path.
          - cache_size=-16000   ~16 MB page cache (negative = KiB) to cut re-reads
                                within a multi-statement transaction.
          - mmap_size=256MB     Memory-mapped reads reduce syscall overhead.
          - wal_autocheckpoint  Larger WAL before an (in-line) checkpoint, so heavy
                                write bursts aren't interrupted by frequent
                                checkpoint stalls.
        """
        # isolation_level="IMMEDIATE": Python's sqlite3 defaults to DEFERRED, so a
        # write transaction begins as a reader and only tries to *upgrade* to a
        # writer at the first INSERT/UPDATE. Under WAL, when two connections both
        # hold a read lock and then try to upgrade, SQLite returns "database is
        # locked" IMMEDIATELY (busy_timeout deliberately won't wait — waiting could
        # deadlock). The hot paths here (the raw-client proxy's SELECT-then-INSERT
        # upsert, session-create, interaction inserts) bypass the in-process
        # _write_lock, and a follower instance bypasses it cross-process — so they
        # hit exactly that upgrade race and 500 under parallel agents. BEGIN
        # IMMEDIATE acquires the RESERVED (write) lock up front, so concurrent
        # writers queue on busy_timeout instead of failing. Pure SELECTs never
        # issue BEGIN, so reads are unaffected.
        # Routed through db_crypto so every file is opened with the right driver +
        # per-file key when full-DB encryption is enabled. With encryption OFF this
        # is byte-for-byte equivalent to plain sqlite3.connect + ATTACH.
        # Three vaults: app (infra keys), agent (OAuth tokens + per-agent creds),
        # user (personal vault keys). Legacy "vault" still attached when it exists
        # so the one-shot migration can relocate its rows into the new vaults.
        from app.db import db_crypto
        attaches: list = []
        # Legacy vault — attached only if it still exists (dropped after migration)
        if os.path.exists(self._vault_path):
            attaches.append(("vault", self._vault_path, self._enc_vault_id))
        attaches.append(("vault_app", self._app_vault_path, self._enc_app_vault_id))
        attaches.append(("vault_agent", self._agent_vault_path, self._enc_agent_vault_id))
        attaches.append(("vault_user", self._user_vault_path, self._enc_user_vault_id))
        if self._schema_plane == "agent":
            from app.db.storage_layout import APP_DB_PATH
            attaches.append(("_app", str(APP_DB_PATH), "app"))
        conn = db_crypto.connect(
            self._db_path, self._enc_main_id,
            isolation_level="IMMEDIATE",
            attaches=attaches,
        )
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA cache_size=-16000")
        conn.execute("PRAGMA mmap_size=268435456")
        conn.execute("PRAGMA wal_autocheckpoint=2000")
        # Mirror the per-DB tuning pragmas on every attached vault (non-fatal).
        for _vname, _vpath in [("vault_app", self._app_vault_path),
                                ("vault_agent", self._agent_vault_path),
                                ("vault_user", self._user_vault_path)]:
            try:
                conn.execute(f"PRAGMA {_vname}.journal_mode=WAL")
                conn.execute(f"PRAGMA {_vname}.synchronous=NORMAL")
            except Exception as e:
                logger.debug("vault pragma failed (%s): %s", _vpath, e)
        # Legacy vault — still apply pragmas while it exists (non-fatal).
        try:
            conn.execute("PRAGMA vault.journal_mode=WAL")
            conn.execute("PRAGMA vault.synchronous=NORMAL")
        except Exception:
            pass

        return conn

    def _migrate_session_interrupt_ownership(
        self, conn: sqlite3.Connection
    ) -> None:
        """Make interrupt rows ownership-scoped and remove proven placeholders.

        A previous fallback created ``sessions.user_id=''`` rows when an
        interrupt referenced a missing client-side session. Only the exact
        empty shape with no child data is deleted automatically; anything with
        content is retained and reported for operator review.
        """
        table = conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='session_interrupts'"
        ).fetchone()
        if not table:
            return

        # Guard: if the sessions table lacks the metadata column (pre-migration
        # schema), skip this pass — the column-adding migration runs later in
        # _init_db and this migration will catch up on next startup.
        sess_cols = {
            col[1]
            for col in conn.execute(
                "PRAGMA table_info(sessions)"
            ).fetchall()
        }
        if "metadata" not in sess_cols:
            return

        child_tables: List[str] = []
        for table_row in conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall():
            table_name = table_row[0]
            if table_name in {"sessions", "session_interrupts"}:
                continue
            escaped = table_name.replace('"', '""')
            try:
                columns = {
                    col[1]
                    for col in conn.execute(
                        f'PRAGMA table_info("{escaped}")'
                    ).fetchall()
                }
            except sqlite3.DatabaseError:
                continue
            if "session_id" in columns:
                child_tables.append(table_name)

        ambiguous: List[str] = []
        placeholders = conn.execute(
            "SELECT s.id, s.title, s.metadata, s.agent_id, s.participants, "
            "s.sort_order, s.status, i.session_id AS interrupt_evidence "
            "FROM sessions AS s "
            "LEFT JOIN session_interrupts AS i ON i.session_id = s.id "
            "WHERE s.user_id = ''"
        ).fetchall()
        for row in placeholders:
            session_id = row["id"]
            empty_shape = (
                row["title"] is None
                and row["metadata"] is None
                and row["agent_id"] is None
                and (row["participants"] in (None, "", "[]"))
                and row["sort_order"] is None
                and (row["status"] in (None, "active"))
            )
            has_child_data = False
            for child_table in child_tables:
                escaped = child_table.replace('"', '""')
                if conn.execute(
                    f'SELECT 1 FROM "{escaped}" WHERE session_id = ? LIMIT 1',
                    (session_id,),
                ).fetchone():
                    has_child_data = True
                    break
            if (
                row["interrupt_evidence"] is not None
                and empty_shape
                and not has_child_data
            ):
                conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            else:
                ambiguous.append(session_id)

        columns = {
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(session_interrupts)"
            ).fetchall()
        }
        foreign_keys = conn.execute(
            "PRAGMA foreign_key_list(session_interrupts)"
        ).fetchall()
        has_owned_fk = (
            "user_id" in columns
            and any(
                row[3] == "user_id" and row[4] == "user_id"
                for row in foreign_keys
            )
        )
        if not has_owned_fk:
            legacy_rows = conn.execute(
                "SELECT i.session_id, i.interrupt_requested, i.created_at, "
                "s.user_id FROM session_interrupts AS i "
                "JOIN sessions AS s ON s.id = i.session_id "
                "WHERE s.user_id <> ''"
            ).fetchall()
            conn.execute("DROP TABLE session_interrupts")
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_id_user "
                "ON sessions(id, user_id)"
            )
            conn.execute(
                """CREATE TABLE session_interrupts (
                    session_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    interrupt_requested INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    FOREIGN KEY (session_id, user_id)
                        REFERENCES sessions(id, user_id) ON DELETE CASCADE
                )"""
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_session_interrupts_user "
                "ON session_interrupts(user_id)"
            )
            conn.executemany(
                "INSERT INTO session_interrupts "
                "(session_id, user_id, interrupt_requested, created_at) "
                "VALUES (?, ?, ?, ?)",
                [
                    (
                        row["session_id"],
                        row["user_id"],
                        row["interrupt_requested"],
                        row["created_at"],
                    )
                    for row in legacy_rows
                ],
            )
        else:
            conn.execute(
                "DELETE FROM session_interrupts "
                "WHERE NOT EXISTS ("
                "SELECT 1 FROM sessions "
                "WHERE sessions.id = session_interrupts.session_id "
                "AND sessions.user_id = session_interrupts.user_id)"
            )

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_session_interrupts_user "
            "ON session_interrupts(user_id)"
        )
        conn.commit()
        if ambiguous:
            logger.warning(
                "Retained %d empty-owner session(s) with non-placeholder "
                "shape or child data for operator review: %s",
                len(ambiguous),
                ", ".join(session_id[:12] for session_id in ambiguous[:20]),
            )

    def _init_db(self) -> None:
        if self._schema_plane:
            from app.db.schema import ensure_sqlite_plane_columns, render_plane

            conn = self._get_conn()
            try:
                conn.executescript(render_plane(self._schema_plane, "sqlite"))
                ensure_sqlite_plane_columns(conn, self._schema_plane)
                ensure_session_activity_summaries(conn)
                # Repair legacy session_interrupts tables (the pre-ownership
                # 3-column shape) on every plane that owns the table. The
                # non-plane/legacy _init_db path below already ran this, but the
                # plane path — which is what real per-user DBs take, since they
                # open as LocalBackend(..., plane="user") — did not. Without it a
                # per-user DB kept the legacy table forever, set_interrupt's
                # INSERT failed on the missing user_id column, and the footer Stop
                # button silently did nothing. The method is self-guarding (no-op
                # when the table or the sessions.metadata column is absent).
                self._migrate_session_interrupt_ownership(conn)
                _migrate_attachment_session_fk(conn)
                if self._schema_plane == "app":
                    _migrate_model_roster_publications(conn)
                    _migrate_experience_tier_publications(conn)
                # Plane-scoped canonical SQLite handles still own/attach the
                # centralized app, agent and user secret vault files. Fresh bare
                # installs reach this branch before the setup page is visited;
                # without these schemas P2P bootstrap fails on its first secret
                # with `no such table: vault_app.auth_elements`.
                if self._canonical_authority_plane:
                    for _schema_ddl in _VAULT_SCHEMAS.values():
                        conn.executescript(_schema_ddl)
                    _migrate_auth_elements_schema_version(conn)
                conn.commit()
            finally:
                conn.close()
            return
        """Create tables if they don't exist."""
        conn = self._get_conn()
        try:
            # ── Three-vault secrets: create schemas in all three vaults ──
            # Each vault file gets its own auth_elements table. Legacy vault.db
            # rows are migrated into the correct target vault on first boot.
            # Legacy main-db auth_elements (older installs) are relocated first.
            try:
                # 1. Create all three new vault schemas
                for _schema_ddl in _VAULT_SCHEMAS.values():
                    conn.executescript(_schema_ddl)

                # 2. Legacy main-db → vault.db (older installs)
                if conn.execute("SELECT 1 FROM main.sqlite_master WHERE type='table' "
                                "AND name='auth_elements'").fetchone():
                    # Ensure legacy vault schema exists for the copy target
                    conn.executescript(VAULT_SCHEMA)
                    src_n = conn.execute("SELECT COUNT(*) FROM main.auth_elements").fetchone()[0]
                    conn.execute(
                        "INSERT OR IGNORE INTO vault.auth_elements "
                        "(id, user_id, service, label, config, secret_ref, is_active, created_at, updated_at) "
                        "SELECT id, user_id, service, label, "
                        "COALESCE(config,'{}'), COALESCE(secret_ref,''), COALESCE(is_active,1), "
                        "COALESCE(created_at, datetime('now')), COALESCE(updated_at, datetime('now')) "
                        "FROM main.auth_elements"
                    )
                    unmigrated = conn.execute(
                        "SELECT COUNT(*) FROM main.auth_elements "
                        "WHERE id NOT IN (SELECT id FROM vault.auth_elements)"
                    ).fetchone()[0]
                    if unmigrated == 0:
                        conn.execute("DROP TABLE main.auth_elements")
                        logger.info("Relocated %d auth_elements row(s) into legacy vault.db (%s)",
                                    src_n, self._vault_path)
                    else:
                        logger.warning("Vault migration incomplete (%d of %d rows not copied) — "
                                       "leaving auth_elements in the main DB for now", unmigrated, src_n)

                # 3. Legacy vault.db → three new vaults (one-shot)
                if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                                "AND name='auth_elements' AND sql LIKE '%vault.%'").fetchone() \
                   or os.path.exists(self._vault_path):
                    _migrate_vault_to_three(conn, self._vault_path,
                                           self._app_vault_path, self._agent_vault_path,
                                           self._user_vault_path)
                conn.commit()

                # 4. Migrate: add _version and _schema_version columns (Phase 0 schema versioning)
                _migrate_auth_elements_schema_version(conn)
            except Exception as e:
                logger.warning("Vault init/migration failed: %s", e)

            # ── Pre-migration: handle old agents table ──
            # Old schema (v1): (id TEXT PK DEFAULT 'default_agent', max_turn_count INT)
            # New schema (v2): (id TEXT PK, user_id TEXT NOT NULL UNIQUE, ...)
            # Detect old schema and rename before SCHEMA_SQL runs
            cursor = conn.execute("PRAGMA table_info(agents)")
            cols = {row[1] for row in cursor.fetchall()}
            # Only rename when truly ancient v1 schema (no user_id AND no admin_users)
            if cols and "user_id" not in cols and "admin_users" not in cols:
                # Drop stale agents_v1 if it exists from a previous partial run
                conn.execute("DROP TABLE IF EXISTS agents_v1")
                conn.execute("ALTER TABLE agents RENAME TO agents_v1")
                conn.commit()

            conn.executescript(SCHEMA_SQL)
            ensure_session_activity_summaries(conn)
            conn.commit()

            # Model-roster publication history.  Older databases stored only one
            # mutable published row; preserve that live value as revision 1 (or
            # its current revision) before administrators begin editing drafts.
            roster_cols = {row[1] for row in conn.execute(
                "PRAGMA table_info(model_rosters)"
            ).fetchall()}
            if "published_revision" not in roster_cols:
                conn.execute("ALTER TABLE model_rosters ADD COLUMN published_revision INTEGER")
            for roster_row in conn.execute(
                "SELECT * FROM model_rosters WHERE status='published' AND published_revision IS NULL"
            ).fetchall():
                roster = dict(roster_row)
                revision = max(1, int(roster.get("revision") or 1))
                payload = {
                    key: roster.get(key) for key in (
                        "slug", "name", "description", "entries_json",
                        "default_entry_id", "source",
                    )
                }
                conn.execute(
                    """INSERT OR IGNORE INTO model_roster_revisions
                       (roster_id, revision, payload_json, action, created_by, created_at)
                       VALUES (?, ?, ?, 'compatibility-backfill', ?, ?)""",
                    (roster["id"], revision,
                     json.dumps(payload, separators=(",", ":"), sort_keys=True),
                     roster.get("updated_by") or roster.get("created_by") or "migration",
                     roster.get("published_at") or roster.get("updated_at") or _now_iso()),
                )
                conn.execute(
                    "UPDATE model_rosters SET published_revision=? WHERE id=?",
                    (revision, roster["id"]),
                )
            conn.commit()
            _migrate_experience_tier_publications(conn)
            self._migrate_session_interrupt_ownership(conn)

            # ── Migration: add channel column to interactions ──
            cursor = conn.execute("PRAGMA table_info(interactions)")
            cols = {row[1] for row in cursor.fetchall()}
            if "channel" not in cols:
                conn.execute("ALTER TABLE interactions ADD COLUMN channel TEXT")
                conn.commit()

            # ── Migration: add source column to interactions (optimizer tracking) ──
            cursor = conn.execute("PRAGMA table_info(interactions)")
            cols2 = {row[1] for row in cursor.fetchall()}
            if "source" not in cols2:
                conn.execute("ALTER TABLE interactions ADD COLUMN source TEXT DEFAULT 'user'")
                conn.commit()
                logger.info("Added interactions.source column")

            # ── Migration: add output column to interactions ──
            cursor = conn.execute("PRAGMA table_info(interactions)")
            cols3 = {row[1] for row in cursor.fetchall()}
            if "output" not in cols3:
                conn.execute("ALTER TABLE interactions ADD COLUMN output TEXT")
                conn.commit()
                logger.info("Added interactions.output column")

            # ── Migration: add from_id and to_id columns to interactions ──
            cursor = conn.execute("PRAGMA table_info(interactions)")
            cols_ft = {row[1] for row in cursor.fetchall()}
            if "from_id" not in cols_ft:
                conn.execute("ALTER TABLE interactions ADD COLUMN from_id TEXT")
                conn.execute("ALTER TABLE interactions ADD COLUMN to_id TEXT")
                conn.commit()
                logger.info("Added interactions.from_id and to_id columns")

            # ── Migration: add session_seq / turn_id / turn_seq for stream persistence ──
            cursor = conn.execute("PRAGMA table_info(interactions)")
            cols_seq = {row[1] for row in cursor.fetchall()}
            added = False
            if "session_seq" not in cols_seq:
                conn.execute("ALTER TABLE interactions ADD COLUMN session_seq INTEGER")
                added = True
            if "turn_id" not in cols_seq:
                conn.execute("ALTER TABLE interactions ADD COLUMN turn_id TEXT")
                added = True
            if "turn_seq" not in cols_seq:
                conn.execute("ALTER TABLE interactions ADD COLUMN turn_seq INTEGER")
                added = True
            # Indexes always (idempotent) — also covers fresh DBs where the cols
            # were created by SCHEMA_SQL and the ALTER branches above were skipped.
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_interactions_session_seq "
                "ON interactions(session_id, session_seq)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_interactions_session_created "
                "ON interactions(session_id, created_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_interactions_turn "
                "ON interactions(turn_id)"
            )
            conn.commit()
            if added:
                logger.info("Added interactions.session_seq / turn_id / turn_seq columns + indexes")

            # ── Migration: upgrade idx_interactions_session_seq to UNIQUE ──
            # The in-memory RunBuffer can drift ahead of the durable manifest,
            # causing next_session_seq to re-issue already-used numbers after a
            # restart.  A UNIQUE index catches any future collision at the DB
            # level.  If existing duplicates already exist (from the very bug
            # this prevents), we log them and skip; the application-layer fix
            # (MAX of manifest + data) is already in place from the same commit.
            try:
                conn.execute(
                    "DROP INDEX IF EXISTS idx_interactions_session_seq")
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS "
                    "idx_interactions_session_seq_unique "
                    "ON interactions(session_id, session_seq)")
                conn.commit()
            except Exception as _uniq_err:
                logger.warning(
                    "Could not create UNIQUE index on interactions(session_id, session_seq): %s. "
                    "Existing duplicate session_seq rows may be present; the application-layer "
                    "allocator fix will prevent future duplicates. Run a manual dedup to "
                    "restore the constraint.",
                    _uniq_err)

            # ── Migration: add status column to interactions (streaming persistence) ──
            # Existing rows are all finished, so they default to 'complete'.
            # New assistant rows are written 'streaming' then flipped to
            # 'complete' / 'interrupted' / 'error' by the loop. See migration
            # 023_run_persistence.sql for the Postgres equivalent.
            cursor = conn.execute("PRAGMA table_info(interactions)")
            cols_status = {row[1] for row in cursor.fetchall()}
            if "status" not in cols_status:
                conn.execute(
                    "ALTER TABLE interactions ADD COLUMN status TEXT NOT NULL DEFAULT 'complete'"
                )
                conn.commit()

            session_cols = {row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
            if "chat_render_version" not in session_cols:
                conn.execute("ALTER TABLE sessions ADD COLUMN chat_render_version INTEGER NOT NULL DEFAULT 2")
                conn.execute("UPDATE sessions SET chat_render_version=1")
                conn.commit()
            interaction_cols = {row[1] for row in conn.execute("PRAGMA table_info(interactions)").fetchall()}
            if "chat_render_kind" not in interaction_cols:
                conn.execute("ALTER TABLE interactions ADD COLUMN chat_render_kind TEXT NOT NULL DEFAULT 'activity'")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_interactions_render_v2 ON interactions(session_id, chat_render_kind, session_seq)")
                conn.commit()
            # Upgrade every historical session to the one render contract. This
            # one-time bootstrap consumes the former metadata verdict; metadata
            # is never a runtime display or grouping authority.
            conn.execute("UPDATE interactions SET chat_render_kind='user' WHERE role='user' AND chat_render_kind='activity' AND session_id IN (SELECT id FROM sessions WHERE chat_render_version < 2)")
            conn.execute("UPDATE interactions SET chat_render_kind='notice' WHERE role='system' AND source IN ('system:closer','system:summary','system:overview','system:mode','system:model') AND chat_render_kind='activity' AND session_id IN (SELECT id FROM sessions WHERE chat_render_version < 2)")
            conn.execute("UPDATE interactions SET chat_render_kind='error' WHERE role='system' AND source LIKE 'system:closing_partial%' AND chat_render_kind='activity' AND session_id IN (SELECT id FROM sessions WHERE chat_render_version < 2)")
            conn.execute("UPDATE interactions SET chat_render_kind='promoted_answer' WHERE role='assistant' AND content <> '' AND chat_render_kind='activity' AND session_id IN (SELECT id FROM sessions WHERE chat_render_version < 2) AND (metadata LIKE '%\"closing_promoted\": true%' OR metadata LIKE '%\"message_phase\": \"main\"%' OR metadata LIKE '%\"message_phase\": \"final\"%')")
            conn.execute("UPDATE sessions SET chat_render_version=2 WHERE chat_render_version < 2")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_interactions_render_v2 ON interactions(session_id, chat_render_kind, session_seq)")
            conn.commit()

            logger.info("Added interactions.status column")

            # ── Migration: ensure session_runs table exists (run-state tracking) ──
            # SCHEMA_SQL already creates it for fresh DBs; this covers DBs created
            # before the table was introduced. Idempotent.
            conn.execute(
                """CREATE TABLE IF NOT EXISTS session_runs (
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
                    relaunch_ctx TEXT
                )"""
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_session_runs_user_status "
                "ON session_runs(user_id, status)"
            )
            # ── Migration: background-leader lock (single-worker guard) ──
            # Covers DBs created before app/coordination/leader.py was added.
            conn.execute(
                """CREATE TABLE IF NOT EXISTS background_leader (
                    lock_key TEXT PRIMARY KEY,
                    holder_id TEXT,
                    heartbeat_at TEXT,
                    expires_at TEXT
                )"""
            )
            # ── Migration: multi-device coordination (see app/devices/) ──
            # For DBs created before cross-device dispatch existed.
            conn.execute(
                """CREATE TABLE IF NOT EXISTS device_presence (
                    instance_id TEXT PRIMARY KEY,
                    label TEXT,
                    custom_label TEXT,
                    custom_icon TEXT,
                    capabilities TEXT NOT NULL DEFAULT '{}',
                    endpoint TEXT,
                    last_seen TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS device_jobs (
                    id TEXT PRIMARY KEY,
                    created_by_instance TEXT,
                    target_instance TEXT,
                    target_label TEXT,
                    owner_user_id TEXT NOT NULL,
                    agent_id TEXT,
                    prompt TEXT NOT NULL DEFAULT '',
                    payload TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'pending',
                    claimed_by TEXT,
                    claimed_at TEXT,
                    lease_expires_at TEXT,
                    result_excerpt TEXT,
                    error TEXT,
                    session_id TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
                )"""
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_device_jobs_claim ON device_jobs(status, target_instance)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_device_jobs_created ON device_jobs(created_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_device_presence_seen ON device_presence(last_seen)"
            )
            # ── Migration: instance_meta (shared identity store) ──
            conn.execute(
                """CREATE TABLE IF NOT EXISTS instances (
                    ref TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    display_name TEXT,
                    provider TEXT,
                    status TEXT,
                    ip TEXT,
                    endpoint TEXT,
                    platform TEXT,
                    zone TEXT,
                    machine_type TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                    metadata TEXT NOT NULL DEFAULT '{}'
                )"""
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_instances_kind ON instances(kind)"
            )
            # ── Migration: admin display overrides on device_presence ──
            # custom_label / custom_icon let an admin rename a device and pick its
            # icon from the Instances page; both live in the shared DB (so every
            # device shows the same) and are NEVER written by the heartbeat, so the
            # override survives every check-in. Guarded ADD COLUMN (no IF NOT EXISTS
            # in SQLite) so re-runs are no-ops on already-migrated DBs.
            cursor = conn.execute("PRAGMA table_info(device_presence)")
            _dp_cols = {row[1] for row in cursor.fetchall()}
            for _col in ("custom_label", "custom_icon"):
                if _col not in _dp_cols:
                    conn.execute(f"ALTER TABLE device_presence ADD COLUMN {_col} TEXT")
            conn.commit()

            # ── Migration: self-healing/auto-resume columns on session_runs ──
            # For DBs created before these columns existed. Each ADD COLUMN is
            # guarded so re-runs are no-ops (SQLite has no ADD COLUMN IF NOT EXISTS).
            cursor = conn.execute("PRAGMA table_info(session_runs)")
            _sr_cols = {row[1] for row in cursor.fetchall()}
            _sr_adds = [
                ("stop_cause", "ALTER TABLE session_runs ADD COLUMN stop_cause TEXT"),
                ("origin", "ALTER TABLE session_runs ADD COLUMN origin TEXT"),
                ("resume_attempts", "ALTER TABLE session_runs ADD COLUMN resume_attempts INTEGER NOT NULL DEFAULT 0"),
                ("max_resume_attempts", "ALTER TABLE session_runs ADD COLUMN max_resume_attempts INTEGER"),
                ("resume_cause_counts", "ALTER TABLE session_runs ADD COLUMN resume_cause_counts TEXT"),
                ("heartbeat_at", "ALTER TABLE session_runs ADD COLUMN heartbeat_at TEXT"),
                ("next_resume_at", "ALTER TABLE session_runs ADD COLUMN next_resume_at TEXT"),
                ("owner_token", "ALTER TABLE session_runs ADD COLUMN owner_token TEXT"),
                ("lease_expires_at", "ALTER TABLE session_runs ADD COLUMN lease_expires_at TEXT"),
                ("relaunch_ctx", "ALTER TABLE session_runs ADD COLUMN relaunch_ctx TEXT"),
                ("current_op", "ALTER TABLE session_runs ADD COLUMN current_op TEXT"),
            ]
            _sr_added = []
            for _col, _sql in _sr_adds:
                if _col not in _sr_cols:
                    conn.execute(_sql)
                    _sr_added.append(_col)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_session_runs_status_heartbeat "
                "ON session_runs(status, heartbeat_at)"
            )
            conn.commit()
            if _sr_added:
                logger.info("Added session_runs self-healing columns: %s", ", ".join(_sr_added))

            # ── Migration: per-call cost + session/source columns on usage_events ──
            # cost_usd is the locked-in published-price cost per call (summing it
            # gives accurate session/agent/global cost across model switches);
            # session_id attributes chat rows directly; source separates 'chat'
            # from 'background' (git messages, placeholders, embeddings, titles).
            cursor = conn.execute("PRAGMA table_info(usage_events)")
            _ue_cols = {row[1] for row in cursor.fetchall()}
            _ue_adds = [
                ("cost_usd", "ALTER TABLE usage_events ADD COLUMN cost_usd REAL NOT NULL DEFAULT 0"),
                ("cost_source", "ALTER TABLE usage_events ADD COLUMN cost_source TEXT"),
                ("credit_units", "ALTER TABLE usage_events ADD COLUMN credit_units REAL NOT NULL DEFAULT 0"),
                ("credit_source", "ALTER TABLE usage_events ADD COLUMN credit_source TEXT"),
                ("cached_input_tokens", "ALTER TABLE usage_events ADD COLUMN cached_input_tokens INTEGER NOT NULL DEFAULT 0"),
                ("cache_write_tokens", "ALTER TABLE usage_events ADD COLUMN cache_write_tokens INTEGER NOT NULL DEFAULT 0"),
                ("uncached_input_tokens", "ALTER TABLE usage_events ADD COLUMN uncached_input_tokens INTEGER"),
                ("reasoning_tokens", "ALTER TABLE usage_events ADD COLUMN reasoning_tokens INTEGER NOT NULL DEFAULT 0"),
                ("session_id", "ALTER TABLE usage_events ADD COLUMN session_id TEXT"),
                ("source", "ALTER TABLE usage_events ADD COLUMN source TEXT NOT NULL DEFAULT 'chat'"),
                ("entry_id", "ALTER TABLE usage_events ADD COLUMN entry_id TEXT"),
            ]
            _ue_added = []
            for _col, _sql in _ue_adds:
                if _col not in _ue_cols:
                    conn.execute(_sql)
                    _ue_added.append(_col)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_usage_events_model_created "
                "ON usage_events(model, created_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_usage_events_entry_created "
                "ON usage_events(entry_id, created_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_usage_events_session "
                "ON usage_events(session_id)"
            )
            conn.commit()

            # ── Migration: trial credit-grant columns ──
            # Trials went from message/token counters to a credit allotment
            # (credit_cents / remaining_cents). Guarded ADD COLUMN; legacy rows
            # keep NULL remaining_cents which the engine treats as exhausted
            # (clean cutover — admin can delete/reset a trial row to re-grant).
            cursor = conn.execute("PRAGMA table_info(trials)")
            _tr_cols = {row[1] for row in cursor.fetchall()}
            _tr_added = []
            for _col, _sql in (
                ("credit_cents", "ALTER TABLE trials ADD COLUMN credit_cents INTEGER"),
                ("remaining_cents", "ALTER TABLE trials ADD COLUMN remaining_cents INTEGER"),
            ):
                if _col not in _tr_cols:
                    conn.execute(_sql)
                    _tr_added.append(_col)
            conn.commit()
            if _tr_added:
                logger.info("Added trials credit-grant columns: %s", ", ".join(_tr_added))

            # ── Migration: cost-based pricing + combined strategies ──
            # billing_configs gains three nullable cost-pricing columns
            # (NULL = inherit the platform default) and loses the old
            # single-strategy CHECK (so 'trial,credits' is storable; the API
            # validates strategies). SQLite can't drop a CHECK or NOT NULL via
            # ALTER, so: add missing columns nullable, then rebuild once when
            # either the CHECK is present OR an earlier migration added the
            # columns as NOT NULL.
            cursor = conn.execute("PRAGMA table_info(billing_configs)")
            _bc_cols = {row[1]: (row[3] or 0, row[2] or "") for row in cursor.fetchall()}
            for _col, _sql in (
                ("cost_multiplier", "ALTER TABLE billing_configs ADD COLUMN cost_multiplier REAL"),
                ("min_charge_cents", "ALTER TABLE billing_configs ADD COLUMN min_charge_cents INTEGER"),
                ("flat_image_cost_usd", "ALTER TABLE billing_configs ADD COLUMN flat_image_cost_usd REAL"),
            ):
                if _col not in _bc_cols:
                    conn.execute(_sql)
                    _bc_cols[_col] = (0, "")
            conn.commit()

            _bc_sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='billing_configs'"
            ).fetchone()
            _needs_rebuild = bool(_bc_sql and _bc_sql[0] and "CHECK (strategy IN" in _bc_sql[0])
            if not _needs_rebuild:
                for _col in ("cost_multiplier", "min_charge_cents", "flat_image_cost_usd"):
                    if _bc_cols.get(_col, (0,))[0]:
                        _needs_rebuild = True
                        break
            if _needs_rebuild:
                try:
                    conn.executescript("""
                        CREATE TABLE billing_configs_new (
                            scope                      TEXT    PRIMARY KEY,
                            strategy                   TEXT    NOT NULL DEFAULT 'free',
                            allowed_strategies         TEXT    NOT NULL DEFAULT '[]',
                            allowed_processors         TEXT    NOT NULL DEFAULT '[]',
                            rate_card_default_llm      TEXT    NOT NULL DEFAULT '{}',
                            rate_card_byo_llm          TEXT    NOT NULL DEFAULT '{}',
                            cost_multiplier            REAL,
                            min_charge_cents           INTEGER,
                            flat_image_cost_usd        REAL,
                            trial_config               TEXT    NOT NULL DEFAULT '{}',
                            subscription_price_cents   INTEGER NOT NULL DEFAULT 0,
                            currency                   TEXT    NOT NULL DEFAULT 'usd',
                            created_at                 TEXT    NOT NULL DEFAULT (datetime('now')),
                            updated_at                 TEXT    NOT NULL DEFAULT (datetime('now')),
                            updated_by                 TEXT
                        );
                        INSERT INTO billing_configs_new SELECT
                            scope, strategy, allowed_strategies, allowed_processors,
                            rate_card_default_llm, rate_card_byo_llm,
                            cost_multiplier, min_charge_cents, flat_image_cost_usd,
                            trial_config, subscription_price_cents, currency,
                            created_at, updated_at, updated_by
                        FROM billing_configs;
                        DROP TABLE billing_configs;
                        ALTER TABLE billing_configs_new RENAME TO billing_configs;
                    """)
                    conn.commit()
                    logger.info("Rebuilt billing_configs (nullable cost columns, no strategy CHECK)")
                except Exception as _e:
                    logger.debug("billing_configs rebuild skipped: %s", _e)
            if _ue_added:
                logger.info("Added usage_events cost columns: %s", ", ".join(_ue_added))

            # ── Migration: add metadata column to sessions (for optimizer tracking) ──
            cursor = conn.execute("PRAGMA table_info(sessions)")
            sess_cols = {row[1] for row in cursor.fetchall()}
            if "metadata" not in sess_cols:
                conn.execute("ALTER TABLE sessions ADD COLUMN metadata TEXT")
                conn.commit()
                logger.info("Added sessions.metadata column")

            # ── Migration: add pinned column to sessions ──
            cursor = conn.execute("PRAGMA table_info(sessions)")
            sess_cols_p = {row[1] for row in cursor.fetchall()}
            if "pinned" not in sess_cols_p:
                conn.execute("ALTER TABLE sessions ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
                conn.commit()
                logger.info("Added sessions.pinned column")

            # ── Migration: add hidden column to sessions (declutter without delete) ──
            cursor = conn.execute("PRAGMA table_info(sessions)")
            sess_cols_h = {row[1] for row in cursor.fetchall()}
            if "hidden" not in sess_cols_h:
                conn.execute("ALTER TABLE sessions ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0")
                conn.commit()
                logger.info("Added sessions.hidden column")

            # ── Migration: add sort_order column to sessions (manual row order) ──
            cursor = conn.execute("PRAGMA table_info(sessions)")
            sess_cols_so = {row[1] for row in cursor.fetchall()}
            if "sort_order" not in sess_cols_so:
                conn.execute("ALTER TABLE sessions ADD COLUMN sort_order INTEGER")
                conn.commit()
                logger.info("Added sessions.sort_order column")

            # ── Migration: add sort_order column to agents (manual row order) ──
            cursor = conn.execute("PRAGMA table_info(agents)")
            agent_cols_so = {row[1] for row in cursor.fetchall()}
            if "sort_order" not in agent_cols_so:
                conn.execute("ALTER TABLE agents ADD COLUMN sort_order INTEGER")
                conn.commit()
                logger.info("Added agents.sort_order column")

            # ── Migration: add auto_resume opt-out column to agents ──
            cursor = conn.execute("PRAGMA table_info(agents)")
            agent_cols_ar = {row[1] for row in cursor.fetchall()}
            if "auto_resume" not in agent_cols_ar:
                conn.execute("ALTER TABLE agents ADD COLUMN auto_resume INTEGER NOT NULL DEFAULT 1")
                conn.commit()
                logger.info("Added agents.auto_resume column")

            # ── Migration: add max_wall_seconds to agents and agent_templates ──
            for tbl in ("agents", "agent_templates"):
                tbl_cols = {row[1] for row in conn.execute(f"PRAGMA table_info({tbl})").fetchall()}
                if "max_wall_seconds" not in tbl_cols:
                    conn.execute(f"ALTER TABLE {tbl} ADD COLUMN max_wall_seconds REAL")
                    logger.info("Added %s.max_wall_seconds column", tbl)
            conn.commit()

            # ── Migration: add max_identical_tool_calls / max_stall_strikes to agents and agent_templates ──
            for tbl in ("agents", "agent_templates"):
                tbl_cols = {row[1] for row in conn.execute(f"PRAGMA table_info({tbl})").fetchall()}
                if "max_identical_tool_calls" not in tbl_cols:
                    conn.execute(f"ALTER TABLE {tbl} ADD COLUMN max_identical_tool_calls INTEGER NOT NULL DEFAULT 0")
                    logger.info("Added %s.max_identical_tool_calls column", tbl)
                if "max_stall_strikes" not in tbl_cols:
                    conn.execute(f"ALTER TABLE {tbl} ADD COLUMN max_stall_strikes INTEGER NOT NULL DEFAULT 0")
                    logger.info("Added %s.max_stall_strikes column", tbl)
            conn.commit()

            # ── Migration: cross-session knowledge engine fields on memories ──
            # New fields for the Context Control memory engine (origin/pinned/
            # provenance/needs_review). Guarded so re-runs are no-ops.
            cursor = conn.execute("PRAGMA table_info(memories)")
            _mem_cols = {row[1] for row in cursor.fetchall()}
            _mem_adds = [
                ("origin", "ALTER TABLE memories ADD COLUMN origin TEXT NOT NULL DEFAULT 'distilled'"),
                ("pinned", "ALTER TABLE memories ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0"),
                ("provenance", "ALTER TABLE memories ADD COLUMN provenance TEXT NOT NULL DEFAULT '[]'"),
                ("needs_review", "ALTER TABLE memories ADD COLUMN needs_review INTEGER NOT NULL DEFAULT 0"),
            ]
            _mem_added = []
            for _col, _sql in _mem_adds:
                if _col not in _mem_cols:
                    conn.execute(_sql)
                    _mem_added.append(_col)
            conn.commit()
            if _mem_added:
                logger.info("Added memories engine columns: %s", ", ".join(_mem_added))

            # ── Migration: drop dead memory_links / memory_timeline tables ──
            # Unused CRM-era knowledge-graph + timeline tables, retired with the
            # cross-session knowledge rebuild. Safe to drop (no app flow used them).
            for _dead in ("memory_links", "memory_timeline"):
                try:
                    conn.execute(f"DROP TABLE IF EXISTS {_dead}")
                except Exception as _e:
                    logger.warning("Could not drop %s: %s", _dead, _e)
            conn.commit()

            # ── Migration: add covered_count to session_summaries (compaction) ──
            cursor = conn.execute("PRAGMA table_info(session_summaries)")
            _ss_cols = {row[1] for row in cursor.fetchall()}
            if "covered_count" not in _ss_cols:
                conn.execute("ALTER TABLE session_summaries ADD COLUMN covered_count INTEGER NOT NULL DEFAULT 0")
                conn.commit()
                logger.info("Added session_summaries.covered_count column")

            # ── Migration 033: rename the `canvases` table to `genui` ──
            # The "Canvas" feature was renamed to "Gen UI". On an existing DB the
            # data lives in `canvases`; SCHEMA_SQL above may have just created an
            # EMPTY `genui` (CREATE … IF NOT EXISTS). Move the real rows across:
            # drop the empty placeholder, then rename. Guarded so re-runs no-op.
            def _tbl_exists(_name):
                return conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (_name,),
                ).fetchone() is not None
            if _tbl_exists("canvases"):
                if _tbl_exists("genui"):
                    _n = conn.execute("SELECT COUNT(*) FROM genui").fetchone()[0]
                    if _n == 0:
                        conn.execute("DROP TABLE genui")
                        conn.execute("ALTER TABLE canvases RENAME TO genui")
                        logger.info("Renamed canvases -> genui (Canvas → Gen UI)")
                    # else: genui already holds data; leave canvases as a backup.
                else:
                    conn.execute("ALTER TABLE canvases RENAME TO genui")
                    logger.info("Renamed canvases -> genui (Canvas → Gen UI)")
                conn.execute("DROP INDEX IF EXISTS idx_canvases_user")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_genui_user ON genui(user_id)")
                conn.commit()

            # ── Migration: add data column to genui (per-genui data bag) ──
            # Holds a genui's content (the records it renders) as a JSON object,
            # kept separate from the html body so the agent updates data without
            # rewriting the page. Guarded so re-runs are no-ops.
            cursor = conn.execute("PRAGMA table_info(genui)")
            _cv_cols = {row[1] for row in cursor.fetchall()}
            if _cv_cols and "data" not in _cv_cols:
                conn.execute("ALTER TABLE genui ADD COLUMN data TEXT")
                conn.commit()
                logger.info("Added genui.data column")

            # ── Migration: add agent_id column to genui (owning agent link) ──
            # Records the agent that created/manages each genui (mirrors
            # browser_sessions.agent_id) so the Gen UI footer can name that agent
            # and route its chat to it, instead of always using the default agent.
            # Guarded so re-runs are no-ops.
            cursor = conn.execute("PRAGMA table_info(genui)")
            _cv_cols2 = {row[1] for row in cursor.fetchall()}
            if _cv_cols2 and "agent_id" not in _cv_cols2:
                conn.execute("ALTER TABLE genui ADD COLUMN agent_id TEXT")
                conn.commit()
                logger.info("Added genui.agent_id column")

            # ── Migration: add session_config column to genui (session targeting) ──
            # Stores the REQUIRED session contract for a genui's actions/chat:
            # target session title + new-session mode (+ explicit session id when
            # mode == "existing"). See the genui CREATE TABLE for the shape.
            # Existing rows get '{}' → legacy pages keep current behavior.
            cursor = conn.execute("PRAGMA table_info(genui)")
            _cv_cols3 = {row[1] for row in cursor.fetchall()}
            if _cv_cols3 and "session_config" not in _cv_cols3:
                conn.execute("ALTER TABLE genui ADD COLUMN session_config TEXT NOT NULL DEFAULT '{}'")
                conn.commit()
                logger.info("Added genui.session_config column")

            # ── Migration: add widget column to genui (per-genui launcher config) ──
            # Holds the page's launcher/widget configuration (widget.json: which
            # agent the page's chat launcher opens, icon, corner buttons, widget
            # options) as a JSON object, kept separate from html/data so the agent
            # edits config without rewriting the page. Guarded so re-runs no-op.
            cursor = conn.execute("PRAGMA table_info(genui)")
            _cv_cols4 = {row[1] for row in cursor.fetchall()}
            if _cv_cols4 and "widget" not in _cv_cols4:
                conn.execute("ALTER TABLE genui ADD COLUMN widget TEXT")
                conn.commit()
                logger.info("Added genui.widget column")

            # ── Migration: add fire_token / external_job_id / external_provider to agent_automations ──
            try:
                cursor = conn.execute("PRAGMA table_info(agent_automations)")
                auto_cols = {row[1] for row in cursor.fetchall()}
                if auto_cols:
                    if "fire_token" not in auto_cols:
                        conn.execute("ALTER TABLE agent_automations ADD COLUMN fire_token TEXT")
                    if "external_job_id" not in auto_cols:
                        conn.execute("ALTER TABLE agent_automations ADD COLUMN external_job_id TEXT")
                    if "external_provider" not in auto_cols:
                        conn.execute("ALTER TABLE agent_automations ADD COLUMN external_provider TEXT")
                    conn.commit()
            except Exception as _e:
                logger.warning("agent_automations migration failed: %s", _e)

            # ── Migration 029: feature-rich automation columns on both trigger tables ──
            try:
                _auto_extra = [
                    ("schedule_kind", "TEXT NOT NULL DEFAULT 'cron'"),
                    ("delivery_json", "TEXT NOT NULL DEFAULT '{}'"),
                    ("run_mode", "TEXT NOT NULL DEFAULT 'inline'"),
                    ("runner_agent_id", "TEXT"),
                    ("clone_abilities", "TEXT NOT NULL DEFAULT '[]'"),
                    ("max_per_day", "INTEGER"),
                    ("runs_today", "INTEGER NOT NULL DEFAULT 0"),
                    ("runs_today_date", "TEXT"),
                    ("fail_count", "INTEGER NOT NULL DEFAULT 0"),
                    ("disable_after_failures", "INTEGER"),
                    ("expires_at", "TEXT"),
                    ("retry_max", "INTEGER NOT NULL DEFAULT 0"),
                    ("retry_backoff_seconds", "INTEGER NOT NULL DEFAULT 0"),
                    ("next_retry_at", "TEXT"),
                    ("memory_json", "TEXT NOT NULL DEFAULT '{}'"),
                    ("origin", "TEXT NOT NULL DEFAULT 'slot'"),
                ]
                # agent_event_subscriptions gets the same set minus schedule_kind (cron-only).
                _sub_extra = [c for c in _auto_extra if c[0] != "schedule_kind"]
                for _tbl, _cols in (
                    ("agent_automations", _auto_extra),
                    ("agent_event_subscriptions", _sub_extra),
                ):
                    cur = conn.execute(f"PRAGMA table_info({_tbl})")
                    have = {row[1] for row in cur.fetchall()}
                    if not have:
                        continue
                    for _name, _ddl in _cols:
                        if _name not in have:
                            conn.execute(f"ALTER TABLE {_tbl} ADD COLUMN {_name} {_ddl}")
                conn.commit()
            except Exception as _e:
                logger.warning("automation 029 migration failed: %s", _e)

            # ── Migration 030: recycling-bin marker on the automation surfaces ──
            # NULL = active, ISO timestamp = soft-deleted (Automations bin). Lets
            # the dashboard show a recycling bin (soft-delete → restore → purge)
            # mirroring the Agents and Sessions pages.
            try:
                for _tbl in ("agent_automations", "agent_event_subscriptions",
                             "webhook_registrations"):
                    cur = conn.execute(f"PRAGMA table_info({_tbl})")
                    have = {row[1] for row in cur.fetchall()}
                    if have and "deleted_at" not in have:
                        conn.execute(f"ALTER TABLE {_tbl} ADD COLUMN deleted_at TEXT")
                conn.commit()
            except Exception as _e:
                logger.warning("automation 030 (deleted_at) migration failed: %s", _e)

            # ── Migration 031: cross-device targeting on automations (app/devices/) ──
            # target_device = device instance-id to run on (NULL/'' = local);
            # target_offline = 'wait' | 'skip' when the target is offline at fire time.
            try:
                cur = conn.execute("PRAGMA table_info(agent_automations)")
                have = {row[1] for row in cur.fetchall()}
                if have:
                    if "target_device" not in have:
                        conn.execute("ALTER TABLE agent_automations ADD COLUMN target_device TEXT")
                    if "target_offline" not in have:
                        conn.execute("ALTER TABLE agent_automations ADD COLUMN target_offline TEXT DEFAULT 'wait'")
                    conn.commit()
            except Exception as _e:
                logger.warning("automation 031 (target_device) migration failed: %s", _e)

            # ── Migration: backfill 'automation' admin-base slot for every agent ──
            try:
                agent_ids = [
                    r[0] for r in conn.execute(
                        "SELECT DISTINCT agent_id FROM agent_prompts WHERE user_id IS NULL"
                    ).fetchall()
                ]
                _auto_now = _now_iso()
                _backfilled = 0
                for aid in agent_ids:
                    existing = conn.execute(
                        """SELECT 1 FROM agent_prompts
                           WHERE agent_id = ? AND slot_name = 'automation' AND user_id IS NULL""",
                        (aid,),
                    ).fetchone()
                    if not existing:
                        conn.execute(
                            """INSERT INTO agent_prompts
                               (id, agent_id, slot_name, user_id, order_index,
                                lock, merge_mode, content, updated_at, updated_by)
                               VALUES (?, ?, 'automation', NULL, 70, 0, 'replace', '', ?, 'migration')""",
                            (_uuid(), aid, _auto_now),
                        )
                        _backfilled += 1
                if _backfilled:
                    conn.commit()
                    logger.info("Backfilled 'automation' slot on %d agent(s)", _backfilled)
            except Exception as _bf_e:
                logger.warning("Automation slot backfill failed: %s", _bf_e)

            conn.commit()

            # ── Post-migration: move data from old agents_v1 ──
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='agents_v1'"
            )
            if cursor.fetchone():
                _mig_now = _now_iso()
                conn.execute(
                    """INSERT OR IGNORE INTO agents
                       (id, max_turn_count, status, assigned_at, created_at, updated_at)
                       SELECT id, max_turn_count, 'active',
                              ?, ?, ?
                       FROM agents_v1 WHERE id = 'default_agent'""",
                    (_mig_now, _mig_now, _mig_now),
                )
                conn.execute("DROP TABLE agents_v1")
                conn.commit()
                logger.info("Agents table migration complete")

            # Drop stale legacy context_defaults table if it still exists.
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='context_defaults'"
            ).fetchone():
                conn.execute("DROP TABLE context_defaults")
                conn.commit()
                logger.info("Dropped legacy context_defaults table")

            # ── Migration: add turn_count column to agents ──
            cursor = conn.execute("PRAGMA table_info(agents)")
            agent_cols = {row[1] for row in cursor.fetchall()}
            if "turn_count" not in agent_cols:
                conn.execute("ALTER TABLE agents ADD COLUMN turn_count INTEGER NOT NULL DEFAULT 0")
                conn.commit()
                logger.info("Added agents.turn_count column")

            # ── Migration: add agent_id column to sessions ──
            cursor = conn.execute("PRAGMA table_info(sessions)")
            sess_cols = {row[1] for row in cursor.fetchall()}
            if "agent_id" not in sess_cols:
                conn.execute("ALTER TABLE sessions ADD COLUMN agent_id TEXT")
                conn.commit()
                logger.info("Added sessions.agent_id column")

            # ── Migration: add participants column to sessions ──
            cursor = conn.execute("PRAGMA table_info(sessions)")
            sess_cols = {row[1] for row in cursor.fetchall()}
            if "participants" not in sess_cols:
                conn.execute("ALTER TABLE sessions ADD COLUMN participants TEXT DEFAULT '[]'")
                conn.commit()
                logger.info("Added sessions.participants column")

            # ── Migration: add status (recycling-bin) column to sessions ──
            cursor = conn.execute("PRAGMA table_info(sessions)")
            sess_cols = {row[1] for row in cursor.fetchall()}
            if "status" not in sess_cols:
                conn.execute("ALTER TABLE sessions ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")
                conn.commit()
                logger.info("Added sessions.status column")

            # ── Migration: add template_id column to agents ──
            cursor = conn.execute("PRAGMA table_info(agents)")
            agent_cols = {row[1] for row in cursor.fetchall()}
            if "template_id" not in agent_cols:
                conn.execute("ALTER TABLE agents ADD COLUMN template_id TEXT")
                conn.commit()
                logger.info("Added agents.template_id column")

            # ── Migration: add context columns to agent_templates ──
            cursor = conn.execute("PRAGMA table_info(agent_templates)")
            at_cols = {row[1] for row in cursor.fetchall()}
            for col in ("agent_prompt", "user_prompt", "skills_prompt", "tasks_prompt", "misc_prompt"):
                if col not in at_cols:
                    conn.execute(f"ALTER TABLE agent_templates ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
            conn.commit()

            # ── Migration: add context columns to agents ──
            cursor = conn.execute("PRAGMA table_info(agents)")
            ag_cols = {row[1] for row in cursor.fetchall()}
            for col in ("agent_prompt", "user_prompt", "skills_prompt", "tasks_prompt", "misc_prompt"):
                if col not in ag_cols:
                    conn.execute(f"ALTER TABLE agents ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
            conn.commit()

            # ── Migration: add bootstrap_tools column ──
            for tbl in ("agents", "agent_templates"):
                tbl_cols = {row[1] for row in conn.execute(f"PRAGMA table_info({tbl})").fetchall()}
                if "bootstrap_tools" not in tbl_cols:
                    conn.execute(f"ALTER TABLE {tbl} ADD COLUMN bootstrap_tools TEXT NOT NULL DEFAULT ''")
                    logger.info("Added bootstrap_tools column to %s", tbl)
            conn.commit()

            # ── Migration: add tool metadata columns ──
            tool_cols = {row[1] for row in conn.execute("PRAGMA table_info(tools)").fetchall()}
            for col, default in [("stages", "'[]'"), ("destructive", "0"), ("agent_types", "'[]'")]:
                if col not in tool_cols:
                    conn.execute(f"ALTER TABLE tools ADD COLUMN {col} TEXT NOT NULL DEFAULT {default}")
                    logger.info("Added %s column to tools", col)
            conn.commit()

            # ── Migration: add allowed_tools and custom_tool_ids to agents ──
            ag_cols2 = {row[1] for row in conn.execute("PRAGMA table_info(agents)").fetchall()}
            for col, default in [("allowed_tools", "'[]'"), ("custom_tool_ids", "'[]'")]:
                if col not in ag_cols2:
                    conn.execute(f"ALTER TABLE agents ADD COLUMN {col} TEXT NOT NULL DEFAULT {default}")
                    logger.info("Added agents.%s column", col)
            conn.commit()

            # ── Migration: copy _ctx → _prompt and drop old _ctx columns ──
            _ctx_map = [
                ("agent_ctx",  "agent_prompt"),
                ("user_ctx",   "user_prompt"),
                ("skills_ctx", "skills_prompt"),
                ("tasks_ctx",  "tasks_prompt"),
                ("misc_ctx",   "misc_prompt"),
            ]
            for tbl in ("agents", "agent_templates"):
                tbl_cols = {row[1] for row in conn.execute(f"PRAGMA table_info({tbl})").fetchall()}
                old_cols_present = [old for old, _ in _ctx_map if old in tbl_cols]
                if old_cols_present:
                    # Copy data: only overwrite _prompt where it's currently empty
                    for old_col, new_col in _ctx_map:
                        if old_col in tbl_cols:
                            conn.execute(
                                f"UPDATE {tbl} SET {new_col} = {old_col} "
                                f"WHERE ({new_col} IS NULL OR {new_col} = '') AND {old_col} != ''"
                            )
                    conn.commit()
                    # Drop the old columns (requires SQLite 3.35+)
                    for old_col, _ in _ctx_map:
                        if old_col in tbl_cols:
                            try:
                                conn.execute(f"ALTER TABLE {tbl} DROP COLUMN {old_col}")
                            except Exception as drop_err:
                                logger.warning("Could not drop %s.%s: %s", tbl, old_col, drop_err)
                    conn.commit()
                    logger.info("Dropped _ctx columns from %s", tbl)

            # ── Migration: add multi-agent fields to agent_templates ──
            at_cols = {row[1] for row in conn.execute("PRAGMA table_info(agent_templates)").fetchall()}
            _at_new_cols = [
                ("name",          "TEXT NOT NULL DEFAULT ''"),
                ("description",   "TEXT NOT NULL DEFAULT ''"),
                ("icon",          "TEXT NOT NULL DEFAULT ''"),
                ("can_be_default","INTEGER NOT NULL DEFAULT 1"),
                ("is_system",     "INTEGER NOT NULL DEFAULT 0"),
                ("is_pipeline",   "INTEGER NOT NULL DEFAULT 0"),
                ("access_level",  "TEXT NOT NULL DEFAULT 'all'"),
                ("trigger_description", "TEXT NOT NULL DEFAULT ''"),
                ("discoverable",  "INTEGER NOT NULL DEFAULT 0"),
            ]
            discoverable_was_missing = "discoverable" not in at_cols
            for col, col_def in _at_new_cols:
                if col not in at_cols:
                    conn.execute(f"ALTER TABLE agent_templates ADD COLUMN {col} {col_def}")
                    logger.info("Added agent_templates.%s column", col)
            if discoverable_was_missing:
                # Seed the default template as discoverable on first migration
                conn.execute("UPDATE agent_templates SET discoverable = 1 WHERE id = 'default'")
                logger.info("Seeded discoverable=1 for default agent_template")
            conn.commit()

            # ── Migration: add trigger_type/trigger_key/loop_logic columns ──
            for tbl in ("agents", "agent_templates"):
                tbl_cols = {row[1] for row in conn.execute(f"PRAGMA table_info({tbl})").fetchall()}
                for col, col_def in [
                    ("trigger_type", "TEXT NOT NULL DEFAULT 'user_input'"),
                    ("trigger_key",  "TEXT"),
                    ("loop_logic",   "TEXT NOT NULL DEFAULT '[]'"),
                ]:
                    if col not in tbl_cols:
                        conn.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {col_def}")
                        logger.info("Added %s.%s column", tbl, col)
            conn.commit()

            # ── Migration: add multi-agent fields to agents ──
            ag_cols = {row[1] for row in conn.execute("PRAGMA table_info(agents)").fetchall()}
            _ag_new_cols = [
                ("is_user_default","INTEGER NOT NULL DEFAULT 0"),
                ("name",           "TEXT NOT NULL DEFAULT ''"),
                ("description",    "TEXT NOT NULL DEFAULT ''"),
            ]
            for col, col_def in _ag_new_cols:
                if col not in ag_cols:
                    conn.execute(f"ALTER TABLE agents ADD COLUMN {col} {col_def}")
                    logger.info("Added agents.%s column", col)
            conn.commit()

            # ── Migration: drop UNIQUE constraint on agents.user_id ──
            # Original schema enforced one agent per user. Multi-agent model
            # allows many rows per user_id; recreate table without UNIQUE.
            ag_sql_row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='agents'"
            ).fetchone()
            if ag_sql_row and "user_id TEXT NOT NULL UNIQUE" in (ag_sql_row[0] or ""):
                cols = [r[1] for r in conn.execute("PRAGMA table_info(agents)").fetchall()]
                col_list = ", ".join(cols)
                conn.execute("PRAGMA foreign_keys = OFF")
                conn.executescript(f"""
                    CREATE TABLE agents_new (
                        id TEXT PRIMARY KEY,
                        user_id TEXT NOT NULL,
                        template_id TEXT,
                        owner_user_id TEXT,
                        name TEXT NOT NULL DEFAULT '',
                        description TEXT NOT NULL DEFAULT '',
                        is_user_default INTEGER NOT NULL DEFAULT 0,
                        system_prompt TEXT NOT NULL DEFAULT '',
                        max_turn_count INTEGER NOT NULL DEFAULT 0,
                        model TEXT,
                        provider TEXT,
                        temperature REAL NOT NULL DEFAULT 0.0,
                        max_tokens INTEGER NOT NULL DEFAULT 8096,
                        status TEXT NOT NULL DEFAULT 'active',
                        metadata TEXT NOT NULL DEFAULT '{{}}',
                        agent_prompt TEXT NOT NULL DEFAULT '',
                        user_prompt TEXT NOT NULL DEFAULT '',
                        skills_prompt TEXT NOT NULL DEFAULT '',
                        tasks_prompt TEXT NOT NULL DEFAULT '',
                        misc_prompt TEXT NOT NULL DEFAULT '',
                        bootstrap_tools TEXT NOT NULL DEFAULT '',
                        assigned_at TEXT NOT NULL DEFAULT (datetime('now')),
                        created_at TEXT NOT NULL DEFAULT (datetime('now')),
                        updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                        turn_count INTEGER NOT NULL DEFAULT 0
                    );
                    INSERT INTO agents_new ({col_list}) SELECT {col_list} FROM agents;
                    DROP TABLE agents;
                    ALTER TABLE agents_new RENAME TO agents;
                    CREATE INDEX IF NOT EXISTS idx_agents_user ON agents(user_id);
                """)
                conn.execute("PRAGMA foreign_keys = ON")
                conn.commit()
                logger.info("Dropped UNIQUE constraint on agents.user_id")

            # ── Migration: backfill admin_users for existing default agents (legacy path) ──
            _ag_cols_uid = {row[1] for row in conn.execute("PRAGMA table_info(agents)").fetchall()}
            if "owner_user_id" in _ag_cols_uid and "user_id" in _ag_cols_uid:
                conn.execute(
                    """UPDATE agents SET owner_user_id = user_id
                       WHERE owner_user_id IS NULL
                       AND (template_id = 'default' OR template_id IS NULL)
                       AND user_id NOT LIKE 'opt_%'"""
                )
                conn.commit()

            # ── Migration: set is_user_default=1 for existing default agents ──
            conn.execute(
                """UPDATE agents SET is_user_default = 1
                   WHERE (template_id = 'default' OR template_id IS NULL)
                   AND is_user_default = 0"""
            )
            conn.commit()

            # ── Migration 010: backfill agents.name for user-owned agents ──
            # Any agent with owner_user_id set but no name yet gets the default 'genui'.
            conn.execute(
                """UPDATE agents SET name = 'genui'
                   WHERE (name IS NULL OR name = '')"""
            )
            conn.commit()
            logger.info("Backfilled agents.name = 'genui' for user-owned agents")

            # ── Migration 011: add last_login_at to user_profiles ──
            cursor = conn.execute("PRAGMA table_info(user_profiles)")
            up_cols = {row[1] for row in cursor.fetchall()}
            if "last_login_at" not in up_cols:
                conn.execute("ALTER TABLE user_profiles ADD COLUMN last_login_at TEXT")
                conn.commit()
                logger.info("Added user_profiles.last_login_at column")

            # ── Migration 012: clear hardcoded model from user agents ──
            # Backend ignores agent.model; display now shows "default" when null.
            # Clear any stored model so existing agents show "default" in the UI.
            conn.execute(
                """UPDATE agents SET model = NULL
                   WHERE model IS NOT NULL"""
            )
            conn.commit()
            logger.info("Cleared agent.model for user-owned agents (now show 'default')")

            # ── Migration 013: add safety_policy to agents + requires_confirmation to tools ──
            ag_cols_013 = {row[1] for row in conn.execute("PRAGMA table_info(agents)").fetchall()}
            if "safety_policy" not in ag_cols_013:
                conn.execute("ALTER TABLE agents ADD COLUMN safety_policy TEXT NOT NULL DEFAULT '{}'")
                logger.info("Added agents.safety_policy column")
            tool_cols_013 = {row[1] for row in conn.execute("PRAGMA table_info(tools)").fetchall()}
            if "requires_confirmation" not in tool_cols_013:
                conn.execute("ALTER TABLE tools ADD COLUMN requires_confirmation INTEGER NOT NULL DEFAULT 0")
                logger.info("Added tools.requires_confirmation column")
            conn.commit()

            # ── Migration 014: add is_admin_agent to agent_templates + agents ──
            tpl_cols_014 = {row[1] for row in conn.execute("PRAGMA table_info(agent_templates)").fetchall()}
            if "is_admin_agent" not in tpl_cols_014:
                conn.execute("ALTER TABLE agent_templates ADD COLUMN is_admin_agent INTEGER NOT NULL DEFAULT 0")
                logger.info("Added agent_templates.is_admin_agent column")
            ag_cols_014 = {row[1] for row in conn.execute("PRAGMA table_info(agents)").fetchall()}
            if "is_admin_agent" not in ag_cols_014:
                conn.execute("ALTER TABLE agents ADD COLUMN is_admin_agent INTEGER NOT NULL DEFAULT 0")
                logger.info("Added agents.is_admin_agent column")
            conn.commit()

            # ── Seed: ensure admin always has is_admin=1 ──
            _mig_now2 = _now_iso()
            conn.execute(
                """INSERT INTO user_profiles (user_id, is_admin, created_at, updated_at)
                   VALUES ('admin', 1, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET is_admin = 1""",
                (_mig_now2, _mig_now2),
            )
            conn.commit()
            logger.info("Ensured admin is_admin=1")

            # ── Migration 015: backfill is_admin_agent=1 on agents rows from admin templates ──
            # Any row in the agents table whose template_id maps to an admin-flagged template
            # should inherit is_admin_agent=1.  We backfill by cross-referencing agent_templates.
            conn.execute(
                """UPDATE agents
                   SET is_admin_agent = 1
                   WHERE is_admin_agent = 0
                     AND template_id IN (
                         SELECT id FROM agent_templates WHERE is_admin_agent = 1
                     )"""
            )
            # Also directly flag any row whose template_id is 'admin-agent' in case the
            # agent_templates row hasn't been re-seeded yet during this startup pass.
            conn.execute(
                "UPDATE agents SET is_admin_agent = 1 WHERE template_id = 'admin-agent' AND is_admin_agent = 0"
            )
            conn.commit()
            logger.info("Migration 015: backfilled is_admin_agent on agents rows")

            # ── Migration 016: add admin_users + member_users to agents ──
            ag_cols_016 = {row[1] for row in conn.execute("PRAGMA table_info(agents)").fetchall()}
            if "admin_users" not in ag_cols_016:
                conn.execute("ALTER TABLE agents ADD COLUMN admin_users TEXT NOT NULL DEFAULT '[]'")
                logger.info("Added agents.admin_users column")
            if "member_users" not in ag_cols_016:
                conn.execute("ALTER TABLE agents ADD COLUMN member_users TEXT NOT NULL DEFAULT '[]'")
                logger.info("Added agents.member_users column")
            # Backfill: agent owner becomes the first admin
            _ag_cols_016b = {row[1] for row in conn.execute("PRAGMA table_info(agents)").fetchall()}
            if "user_id" in _ag_cols_016b:
                conn.execute(
                    """UPDATE agents SET admin_users = json_array(user_id)
                       WHERE admin_users = '[]' AND user_id IS NOT NULL AND user_id != ''"""
                )
            elif "owner_user_id" in _ag_cols_016b:
                conn.execute(
                    """UPDATE agents SET admin_users = json_array(owner_user_id)
                       WHERE admin_users = '[]' AND owner_user_id IS NOT NULL AND owner_user_id != ''"""
                )
            conn.commit()
            logger.info("Migration 016: added admin_users/member_users, backfilled agent owners as admins")

            # ── Migration 017: add user_mode to agents ──
            ag_cols_017 = {row[1] for row in conn.execute("PRAGMA table_info(agents)").fetchall()}
            if "user_mode" not in ag_cols_017:
                conn.execute("ALTER TABLE agents ADD COLUMN user_mode TEXT NOT NULL DEFAULT 'anonymous'")
                conn.commit()
                logger.info("Added agents.user_mode column")

            # ── Migration 018: drop agents.user_id and owner_user_id columns ──
            ag_cols_018 = {row[1] for row in conn.execute("PRAGMA table_info(agents)").fetchall()}
            _cols_to_drop_018 = [c for c in ("user_id", "owner_user_id") if c in ag_cols_018]
            if _cols_to_drop_018:
                try:
                    conn.execute("DROP INDEX IF EXISTS idx_agents_user")
                    for _col in _cols_to_drop_018:
                        conn.execute(f"ALTER TABLE agents DROP COLUMN {_col}")
                    conn.commit()
                    logger.info("Migration 018: dropped agents columns: %s", _cols_to_drop_018)
                except Exception as _e018:
                    logger.warning("Migration 018: could not drop via ALTER TABLE DROP COLUMN (%s); falling back to table recreation", _e018)
                    conn.rollback()
                    # Build SELECT list from columns that exist (minus the ones to drop)
                    _keep_018 = [r[1] for r in conn.execute("PRAGMA table_info(agents)").fetchall() if r[1] not in ("user_id", "owner_user_id")]
                    col_list_018 = ", ".join(_keep_018)
                    conn.execute("PRAGMA foreign_keys = OFF")
                    # Use explicit DDL so PRIMARY KEY is preserved (CREATE TABLE ... AS SELECT drops constraints)
                    conn.executescript(f"""
                        CREATE TABLE agents_018 (
                            id TEXT PRIMARY KEY,
                            template_id TEXT,
                            name TEXT NOT NULL DEFAULT '',
                            description TEXT NOT NULL DEFAULT '',
                            is_user_default INTEGER NOT NULL DEFAULT 0,
                            system_prompt TEXT NOT NULL DEFAULT '',
                            max_turn_count INTEGER NOT NULL DEFAULT 0,
                            model TEXT,
                            provider TEXT,
                            temperature REAL NOT NULL DEFAULT 0.0,
                            max_tokens INTEGER NOT NULL DEFAULT 8096,
                            status TEXT NOT NULL DEFAULT 'active',
                            metadata TEXT NOT NULL DEFAULT '{{}}',
                            agent_prompt TEXT NOT NULL DEFAULT '',
                            user_prompt TEXT NOT NULL DEFAULT '',
                            skills_prompt TEXT NOT NULL DEFAULT '',
                            tasks_prompt TEXT NOT NULL DEFAULT '',
                            misc_prompt TEXT NOT NULL DEFAULT '',
                            bootstrap_tools TEXT NOT NULL DEFAULT '',
                            allowed_tools TEXT NOT NULL DEFAULT '[]',
                            custom_tool_ids TEXT NOT NULL DEFAULT '[]',
                            trigger_type TEXT NOT NULL DEFAULT 'user_input',
                            trigger_key TEXT,
                            loop_logic TEXT NOT NULL DEFAULT '[]',
                            safety_policy TEXT NOT NULL DEFAULT '{{}}',
                            is_admin_agent INTEGER NOT NULL DEFAULT 0,
                            assigned_at TEXT NOT NULL DEFAULT (datetime('now')),
                            created_at TEXT NOT NULL DEFAULT (datetime('now')),
                            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                            turn_count INTEGER NOT NULL DEFAULT 0,
                            admin_users TEXT NOT NULL DEFAULT '[]',
                            member_users TEXT NOT NULL DEFAULT '[]',
                            user_mode TEXT NOT NULL DEFAULT 'anonymous'
                        );
                        INSERT INTO agents_018 ({col_list_018}) SELECT {col_list_018} FROM agents;
                        DROP TABLE agents;
                        ALTER TABLE agents_018 RENAME TO agents;
                    """)
                    conn.execute("PRAGMA foreign_keys = ON")
                    conn.commit()
                    logger.info("Migration 018 (fallback): recreated agents table without user_id/owner_user_id")

            # ── Migration 019: repair agents table if PRIMARY KEY was lost by migration 018 fallback ──
            # The CREATE TABLE ... AS SELECT fallback drops all constraints; check and fix.
            _pk_col_019 = next(
                (r[1] for r in conn.execute("PRAGMA table_info(agents)").fetchall() if r[5] == 1),
                None,
            )
            if _pk_col_019 != "id":
                logger.warning("Migration 019: agents.id is not PRIMARY KEY (pk=%s); recreating table", _pk_col_019)
                _keep_019 = [r[1] for r in conn.execute("PRAGMA table_info(agents)").fetchall() if r[1] not in ("user_id", "owner_user_id")]
                col_list_019 = ", ".join(_keep_019)
                conn.execute("PRAGMA foreign_keys = OFF")
                conn.executescript(f"""
                    CREATE TABLE agents_019 (
                        id TEXT PRIMARY KEY,
                        template_id TEXT,
                        name TEXT NOT NULL DEFAULT '',
                        description TEXT NOT NULL DEFAULT '',
                        is_user_default INTEGER NOT NULL DEFAULT 0,
                        system_prompt TEXT NOT NULL DEFAULT '',
                        max_turn_count INTEGER NOT NULL DEFAULT 0,
                        model TEXT,
                        provider TEXT,
                        temperature REAL NOT NULL DEFAULT 0.0,
                        max_tokens INTEGER NOT NULL DEFAULT 8096,
                        status TEXT NOT NULL DEFAULT 'active',
                        metadata TEXT NOT NULL DEFAULT '{{}}',
                        agent_prompt TEXT NOT NULL DEFAULT '',
                        user_prompt TEXT NOT NULL DEFAULT '',
                        skills_prompt TEXT NOT NULL DEFAULT '',
                        tasks_prompt TEXT NOT NULL DEFAULT '',
                        misc_prompt TEXT NOT NULL DEFAULT '',
                        bootstrap_tools TEXT NOT NULL DEFAULT '',
                        allowed_tools TEXT NOT NULL DEFAULT '[]',
                        custom_tool_ids TEXT NOT NULL DEFAULT '[]',
                        trigger_type TEXT NOT NULL DEFAULT 'user_input',
                        trigger_key TEXT,
                        loop_logic TEXT NOT NULL DEFAULT '[]',
                        safety_policy TEXT NOT NULL DEFAULT '{{}}',
                        is_admin_agent INTEGER NOT NULL DEFAULT 0,
                        assigned_at TEXT NOT NULL DEFAULT (datetime('now')),
                        created_at TEXT NOT NULL DEFAULT (datetime('now')),
                        updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                        turn_count INTEGER NOT NULL DEFAULT 0,
                        admin_users TEXT NOT NULL DEFAULT '[]',
                        member_users TEXT NOT NULL DEFAULT '[]',
                        user_mode TEXT NOT NULL DEFAULT 'anonymous'
                    );
                    INSERT INTO agents_019 ({col_list_019}) SELECT {col_list_019} FROM agents;
                    DROP TABLE agents;
                    ALTER TABLE agents_019 RENAME TO agents;
                """)
                conn.execute("PRAGMA foreign_keys = ON")
                conn.commit()
                logger.info("Migration 019: agents table repaired with proper PRIMARY KEY")

            # ── Migration 021: drop legacy prompt columns from agents + agent_templates ──
            # All prompts now live in agent_prompts (one row per slot, with optional
            # per-user override rows). The old per-table prompt columns are removed.
            _legacy_prompt_cols = (
                "system_prompt", "agent_prompt", "user_prompt",
                "skills_prompt", "tasks_prompt", "misc_prompt", "bootstrap_tools",
            )

            def _drop_legacy_prompt_cols(table: str) -> None:
                tbl_cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
                to_drop = [c for c in _legacy_prompt_cols if c in tbl_cols]
                if not to_drop:
                    return
                for col in to_drop:
                    try:
                        conn.execute(f"ALTER TABLE {table} DROP COLUMN {col}")
                    except Exception as drop_err:
                        logger.warning("Could not drop %s.%s: %s", table, col, drop_err)
                conn.commit()
                logger.info("Migration 021: dropped legacy prompt columns from %s: %s", table, to_drop)

            _drop_legacy_prompt_cols("agents")
            _drop_legacy_prompt_cols("agent_templates")

            # Ensure agent_prompts table exists (SCHEMA_SQL already covers this, but
            # be defensive in case this migration runs on a DB that pre-dates the schema change).
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS agent_prompts (
                    id              TEXT PRIMARY KEY,
                    agent_id        TEXT NOT NULL,
                    slot_name       TEXT NOT NULL,
                    user_id         TEXT,
                    order_index     INTEGER,
                    lock            INTEGER,
                    merge_mode      TEXT,
                    content         TEXT NOT NULL DEFAULT '',
                    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_by      TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_prompts_slot_user
                    ON agent_prompts(agent_id, slot_name, IFNULL(user_id, ''));
                CREATE INDEX IF NOT EXISTS idx_agent_prompts_agent ON agent_prompts(agent_id);
                CREATE INDEX IF NOT EXISTS idx_agent_prompts_user  ON agent_prompts(user_id);
                """
            )
            conn.commit()

            # ── Migration 022: drop context_documents + context_templates tables ──
            # Prompts now live in agent_prompts (per-slot rows). Optimizer prompt
            # loader reads from app/context/optimizer_prompts/ markdown files.
            for _tbl in ("context_documents", "context_templates"):
                try:
                    conn.execute(f"DROP TABLE IF EXISTS {_tbl}")
                except Exception as _drop_err:
                    logger.warning("Migration 022: could not drop %s: %s", _tbl, _drop_err)
            conn.commit()

            # ── Migration 020: fix agent_connections FK if it references stale agents_v1 ──
            # SQLite auto-updates FK refs when a table is renamed with foreign_keys=ON.
            # If agents was renamed → agents_v1 during pre-migration, agent_connections
            # ends up with REFERENCES agents_v1(id) instead of agents(id).
            _ac_fk_rows = conn.execute("PRAGMA foreign_key_list(agent_connections)").fetchall()
            _ac_fk_wrong = any(
                r[2] != "agents" for r in _ac_fk_rows if r[3] == "agent_id"
            )
            if _ac_fk_wrong or not any(r[3] == "agent_id" for r in _ac_fk_rows):
                logger.warning("Migration 020: agent_connections FK is stale; recreating table")
                conn.execute("PRAGMA foreign_keys = OFF")
                conn.executescript("""
                    CREATE TABLE agent_connections_020 (
                        id              TEXT PRIMARY KEY,
                        agent_id        TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
                        connection_type TEXT NOT NULL,
                        section         TEXT NOT NULL DEFAULT 'channel',
                        enabled         INTEGER NOT NULL DEFAULT 0,
                        config          TEXT NOT NULL DEFAULT '{}',
                        created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                        updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
                        UNIQUE(agent_id, connection_type)
                    );
                    INSERT OR IGNORE INTO agent_connections_020
                        SELECT id, agent_id, connection_type, section, enabled, config, created_at, updated_at
                        FROM agent_connections;
                    DROP TABLE agent_connections;
                    ALTER TABLE agent_connections_020 RENAME TO agent_connections;
                    CREATE INDEX IF NOT EXISTS idx_agent_conn_agent ON agent_connections(agent_id);
                    CREATE INDEX IF NOT EXISTS idx_agent_conn_type  ON agent_connections(connection_type);
                """)
                conn.execute("PRAGMA foreign_keys = ON")
                conn.commit()
                logger.info("Migration 020: agent_connections FK fixed to reference agents")
            # Clean up any stale agents_v1 left by the pre-migration rename
            _has_agents_v1 = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='agents_v1'"
            ).fetchone()
            if _has_agents_v1:
                conn.execute("DROP TABLE agents_v1")
                conn.commit()
                logger.info("Migration 020: dropped stale agents_v1 table")

            # ── Migration 023: add authorized_users to agents ──
            ag_cols_023 = {row[1] for row in conn.execute("PRAGMA table_info(agents)").fetchall()}
            if "authorized_users" not in ag_cols_023:
                conn.execute("ALTER TABLE agents ADD COLUMN authorized_users TEXT NOT NULL DEFAULT '[]'")
                conn.commit()
                logger.info("Migration 023: added agents.authorized_users column")

            # ── Migration 024: scope OAuth tokens per agent ──
            # Tokens previously stored under label='oauth' were shared across all
            # agents for a user. After this change each agent gets its own
            # label='oauth:<agent_id>' row. Legacy 'oauth' rows are unreachable;
            # drop them so users re-sign in per agent (matches intended UX).
            legacy_count = conn.execute(
                "SELECT COUNT(*) FROM auth_elements WHERE label = 'oauth'"
            ).fetchone()[0]
            if legacy_count:
                conn.execute("DELETE FROM auth_elements WHERE label = 'oauth'")
                conn.commit()
                logger.info("Migration 024: dropped %d legacy global OAuth token row(s) — users will re-sign in per agent", legacy_count)

            # ── Migration 025: add template_version column to agent_prompts ──
            ap_cols_025 = {row[1] for row in conn.execute("PRAGMA table_info(agent_prompts)").fetchall()}
            if "template_version" not in ap_cols_025:
                conn.execute("ALTER TABLE agent_prompts ADD COLUMN template_version INTEGER")
                conn.commit()
                logger.info("Migration 025: added agent_prompts.template_version column")

            # ── Migration 026: move admin-base slot rows from agent_prompts → agent_prompt_templates ──
            # Pre-refactor, agent_prompts double-dutied as both per-agent rows AND
            # template defaults (the latter identified by user_id IS NULL + agent_id
            # matching a row in agent_templates). The new agent_prompt_templates table
            # is the canonical home for template defaults. One-shot, idempotent: gated
            # by app_meta key 'admin_base_migrated' so it never re-runs.
            _migrated_flag = conn.execute(
                "SELECT value FROM app_meta WHERE key = 'admin_base_migrated'"
            ).fetchone()
            if not _migrated_flag:
                _mig_now = _now_iso()
                # Pull every admin-base row whose agent_id is a known template id.
                _tpl_rows = conn.execute(
                    """SELECT ap.agent_id AS template_id, ap.slot_name, ap.order_index,
                              ap.lock, ap.merge_mode, ap.content, ap.updated_at, ap.updated_by
                       FROM agent_prompts ap
                       INNER JOIN agent_templates t ON t.id = ap.agent_id
                       WHERE ap.user_id IS NULL"""
                ).fetchall()
                _copied = 0
                for r in _tpl_rows:
                    try:
                        conn.execute(
                            """INSERT INTO agent_prompt_templates
                               (id, template_id, slot_name, order_index, lock,
                                merge_mode, content, version, source,
                                updated_at, updated_by)
                               VALUES (?, ?, ?, ?, ?, ?, ?, 1, 'json', ?, ?)
                               ON CONFLICT(template_id, slot_name) DO NOTHING""",
                            (
                                _uuid(),
                                r["template_id"], r["slot_name"],
                                r["order_index"] if r["order_index"] is not None else 0,
                                r["lock"] if r["lock"] is not None else 0,
                                r["merge_mode"] or "replace",
                                r["content"] or "",
                                r["updated_at"] or _mig_now,
                                r["updated_by"] or "migration",
                            ),
                        )
                        _copied += 1
                    except Exception as _ins_err:
                        logger.warning("Migration 026: failed copying %s/%s: %s",
                                       r["template_id"], r["slot_name"], _ins_err)
                # Drop the admin-base rows now that they live in agent_prompt_templates.
                _del_cur = conn.execute(
                    """DELETE FROM agent_prompts
                       WHERE user_id IS NULL
                         AND agent_id IN (SELECT id FROM agent_templates)"""
                )
                conn.execute(
                    """INSERT INTO app_meta (key, value, updated_at)
                       VALUES ('admin_base_migrated', '1', ?)
                       ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                                     updated_at = excluded.updated_at""",
                    (_mig_now,),
                )
                conn.commit()
                logger.info(
                    "Migration 026: moved %d admin-base slot rows into agent_prompt_templates (dropped %d from agent_prompts)",
                    _copied, _del_cur.rowcount,
                )

            # ── Migration 031: add appearance to user_profiles ──
            # Per-user theme override: a SPARSE JSON patch of the same appearance
            # keys as data/config/app-settings.json (palette tokens, fonts,
            # border, background). Layered on top of the global appearance by
            # /api/v1/auth/ui-config when the admin enables allow_user_appearance;
            # NULL/empty = the user follows the global theme. (up_cols was read
            # before this block, so it never already contains "appearance".)
            if "appearance" not in up_cols:
                conn.execute("ALTER TABLE user_profiles ADD COLUMN appearance TEXT")
                conn.commit()
                logger.info("Added user_profiles.appearance column")

            # ── Migration 028: add storage_provider to attachments ──
            # Records which backend (local / browser / s3 / gcs) was
            # active when each file was uploaded, so retrieval works after the
            # admin switches the active backend.
            cursor = conn.execute("PRAGMA table_info(attachments)")
            att_cols = {row[1] for row in cursor.fetchall()}
            if att_cols and "storage_provider" not in att_cols:
                conn.execute(
                    "ALTER TABLE attachments ADD COLUMN storage_provider TEXT NOT NULL DEFAULT 'local'"
                )
                conn.commit()
                logger.info("Added attachments.storage_provider column")

            # ── Migration 029: drop session_id FK from attachments ──
            # The FK constraint on session_id REFERENCES sessions(id) breaks paste-to-attach
            # because the frontend creates a new session client-side before it exists in the DB.
            # We drop the constraint — the app manages the relationship at the code level.
            cursor = conn.execute("PRAGMA foreign_key_list(attachments)")
            _has_session_fk = any(
                row["from"] == "session_id" for row in cursor.fetchall()
            )
            if _has_session_fk:
                logger.info("Migration 029: dropping attachments.session_id FK constraint")
                conn.execute("PRAGMA foreign_keys = OFF")
                conn.execute(
                    """CREATE TABLE attachments_v2 (
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
                    )"""
                )
                conn.execute(
                    """INSERT INTO attachments_v2
                       SELECT id, user_id, session_id, original_name, mime_type,
                              size_bytes, storage_path, storage_provider, metadata, created_at
                       FROM attachments"""
                )
                conn.execute("DROP TABLE attachments")
                conn.execute("ALTER TABLE attachments_v2 RENAME TO attachments")
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_attachments_user ON attachments(user_id)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_attachments_session ON attachments(session_id)"
                )
                conn.execute("PRAGMA foreign_keys = ON")
                conn.commit()
                logger.info("Migration 029 complete: attachments FK constraint removed")

            # ── Migration: add read_at column to sessions (unread tracking) ──
            cursor = conn.execute("PRAGMA table_info(sessions)")
            sess_cols = {row[1] for row in cursor.fetchall()}
            if "read_at" not in sess_cols:
                conn.execute("ALTER TABLE sessions ADD COLUMN read_at TEXT")
                conn.commit()
                logger.info("Added sessions.read_at column")
            if "unread_at" not in sess_cols:
                conn.execute("ALTER TABLE sessions ADD COLUMN unread_at TEXT")
                conn.commit()
                logger.info("Added sessions.unread_at column")

            # ── Migration 031b: backfill agent_templates.metadata.source ──
            # Existing pre-migration rows lack the `source` marker in metadata
            # needed by the Config tab to know whether to show "Export to file".
            try:
                _src_gate = conn.execute(
                    "SELECT value FROM app_meta WHERE key = 'backfilled_template_source'"
                ).fetchone()
                if not _src_gate:
                    _tpl_rows = conn.execute(
                        "SELECT id, metadata FROM agent_templates"
                    ).fetchall()
                    _src_count = 0
                    for _r in _tpl_rows:
                        try:
                            _m = json.loads(_r["metadata"]) if _r["metadata"] else {}
                        except (json.JSONDecodeError, TypeError):
                            _m = {}
                        if not isinstance(_m, dict):
                            _m = {}
                        if _m.get("source"):
                            continue
                        _m["source"] = "json_seed"
                        conn.execute(
                            "UPDATE agent_templates SET metadata = ?, updated_at = ? WHERE id = ?",
                            (json.dumps(_m), _now_iso(), _r["id"]),
                        )
                        _src_count += 1
                    if _src_count:
                        conn.commit()
                        logger.info("Backfilled metadata.source on %d agent_templates row(s)", _src_count)
                    conn.execute(
                        "INSERT OR REPLACE INTO app_meta (key, value, updated_at) VALUES (?, '1', ?)",
                        ("backfilled_template_source", _now_iso()),
                    )
                    conn.commit()
            except Exception as _src_e:
                logger.warning("Migration 031b (template source backfill) skipped: %s", _src_e)

            # ── Migration 031c: backfill agents.template_id ──
            # Agents created before this column existed (or before create_agent_for_user
            # included it) have NULL template_id. Backfill from the agents that clearly
            # came from the default template: is_user_default=1 or name='WebAgent'.
            try:
                _tid_gate = conn.execute(
                    "SELECT value FROM app_meta WHERE key = 'backfilled_agent_template_id'"
                ).fetchone()
                if not _tid_gate:
                    _tid_count = conn.execute(
                        "UPDATE agents SET template_id = 'default', updated_at = ? "
                        "WHERE (template_id IS NULL OR template_id = '') "
                        "AND (is_user_default = 1 OR name = 'WebAgent')",
                        (_now_iso(),),
                    ).rowcount
                    if _tid_count:
                        conn.commit()
                        logger.info("Backfilled template_id='default' on %d agent(s)", _tid_count)
                    # Also stamp template_origin on these agents' metadata
                    _tid_rows = conn.execute(
                        "SELECT id, metadata FROM agents WHERE template_id = 'default'"
                    ).fetchall()
                    _to_count = 0
                    for _r in _tid_rows:
                        try:
                            _m = json.loads(_r["metadata"]) if _r["metadata"] else {}
                        except (json.JSONDecodeError, TypeError):
                            _m = {}
                        if not isinstance(_m, dict):
                            _m = {}
                        if _m.get("template_origin"):
                            continue
                        _m["template_origin"] = "default"
                        conn.execute(
                            "UPDATE agents SET metadata = ?, updated_at = ? WHERE id = ?",
                            (json.dumps(_m), _now_iso(), _r["id"]),
                        )
                        _to_count += 1
                    if _to_count:
                        conn.commit()
                        logger.info("Backfilled template_origin on %d agent(s)", _to_count)
                    conn.execute(
                        "INSERT OR REPLACE INTO app_meta (key, value, updated_at) VALUES (?, '1', ?)",
                        ("backfilled_agent_template_id", _now_iso()),
                    )
                    conn.commit()
            except Exception as _tid_e:
                logger.warning("Migration 031c (agent template_id backfill) skipped: %s", _tid_e)

            # ── Seed: agent templates from data/agents/*.json (full schema) ──
            if getattr(self, "_seed_on_init", True):
                self._seed_agent_templates_from_json_files(conn)

            # ── Backfill: default WebAgent agents → discoverable visibility ──
            # Existing default agents were created before the discovery_default
            # flag existed, so they still ship every tool schema each turn. Flip
            # each user's default agent to "discoverable" ONCE (only when it has no
            # explicit choice), so the in-place default agent benefits without a
            # re-create. New agents get the flag at creation. Idempotent + guarded.
            try:
                from app.agent.cache_profiles import (
                    DEFAULT_WEBAGENT_PROFILE, with_cache_profile,
                )
                _def_rows = conn.execute(
                    "SELECT id, metadata FROM agents WHERE is_user_default = 1"
                ).fetchall()
                _bf_now = _now_iso()
                _bf_count = 0
                for _r in _def_rows:
                    try:
                        _m = json.loads(_r["metadata"]) if _r["metadata"] else {}
                    except (json.JSONDecodeError, TypeError):
                        _m = {}
                    if not isinstance(_m, dict):
                        _m = {}
                    _updated_meta = with_cache_profile(
                        _m, DEFAULT_WEBAGENT_PROFILE,
                    )
                    if _updated_meta == _m:
                        continue
                    conn.execute(
                        "UPDATE agents SET metadata = ?, updated_at = ? WHERE id = ?",
                        (json.dumps(_updated_meta), _bf_now, _r["id"]),
                    )
                    _bf_count += 1
                if _bf_count:
                    conn.commit()
                    logger.info(
                        "Backfilled advanced cache profile on %d default agent(s)",
                        _bf_count,
                    )
            except Exception as _bfe:
                logger.warning("discovery_default backfill skipped: %s", _bfe)

        except Exception as e:
            import traceback as _tb
            logger.error("Error initializing local database: %s\nTRACEBACK:\n%s", e, ''.join(_tb.format_exc()))
            raise
        finally:
            conn.close()

    # ---- Raw client access ----

    def get_raw_client(self) -> Any:
        """
        Return a proxy object implementing the raw query-builder interface.
        This allows code that uses the .table().select().eq().execute() idiom
        (ToolLoader, ToolExecutionTracker, etc.) to work with minimal changes.
        """
        return _LocalTableProxy(self._get_conn)

    # ---- Sessions ----

    async def assert_session_owned(self, user_id: str, session_id: str) -> None:
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT id FROM sessions WHERE id = ? AND user_id = ?",
                (session_id, user_id),
            ).fetchone()
            if not row:
                raise PermissionError(
                    f"Session {session_id} not found or not owned by user {user_id}"
                )
        finally:
            conn.close()

    async def upsert_session_summary(
        self,
        user_id: str,
        session_id: str,
        summary: str,
        message_count: int,
        title: Optional[str] = None,
        covered_count: int = 0,
    ) -> None:
        conn = self._get_conn()
        try:
            existing = conn.execute(
                "SELECT id FROM session_summaries WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            now = _now_iso()
            if existing:
                conn.execute(
                    """UPDATE session_summaries
                       SET summary = ?, message_count = ?, covered_count = ?,
                           title = COALESCE(?, title), updated_at = ?
                       WHERE session_id = ?""",
                    (summary, message_count, covered_count, title, now, session_id),
                )
            else:
                conn.execute(
                    """INSERT INTO session_summaries (id, user_id, session_id, title, summary, message_count, covered_count, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (_uuid(), user_id, session_id, title, summary, message_count, covered_count, now),
                )
            conn.commit()
            logger.debug("Upserted session summary for session %s (covered=%d)", session_id, covered_count)
        except Exception as e:
            logger.error("Error upserting session summary: %s", e)
            raise
        finally:
            conn.close()

    async def get_session_summary(
        self, user_id: str, session_id: str
    ) -> Optional[dict]:
        """Return the rolling compaction summary row for a session, or None.

        Shape: {summary, covered_count, message_count, title, updated_at}.
        """
        conn = self._get_conn()
        try:
            row = conn.execute(
                """SELECT summary, covered_count, message_count, title, updated_at
                   FROM session_summaries WHERE user_id = ? AND session_id = ?""",
                (user_id, session_id),
            ).fetchone()
            return dict(row) if row else None
        except Exception as e:
            logger.error("Error reading session summary: %s", e)
            return None
        finally:
            conn.close()

    async def get_session_segments(
        self, user_id: str, session_id: str
    ) -> List[dict]:
        """Ordered compaction train (frozen summary cars) for a session, seq asc."""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                """SELECT seq, start_index, end_index, summary, token_estimate,
                          topic, tier, updated_at
                   FROM session_summary_segments
                   WHERE user_id = ? AND session_id = ?
                   ORDER BY seq ASC""",
                (user_id, session_id),
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.error("Error reading session segments: %s", e)
            return []
        finally:
            conn.close()

    async def replace_session_segments(
        self, user_id: str, session_id: str, segments: List[dict]
    ) -> None:
        """Replace a session's whole train in one transaction (delete-all + insert)."""
        conn = self._get_conn()
        try:
            now = _now_iso()
            conn.execute(
                "DELETE FROM session_summary_segments WHERE session_id = ?",
                (session_id,),
            )
            for seg in segments:
                conn.execute(
                    """INSERT INTO session_summary_segments
                         (id, user_id, session_id, seq, start_index, end_index,
                          summary, token_estimate, topic, tier, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        _uuid(), user_id, session_id,
                        int(seg.get("seq", 0)),
                        int(seg.get("start_index", 0)),
                        int(seg.get("end_index", 0)),
                        seg.get("summary") or "",
                        int(seg.get("token_estimate", 0)),
                        seg.get("topic"),
                        int(seg.get("tier", 0)),
                        now,
                    ),
                )
            conn.commit()
            logger.debug(
                "Replaced %d summary segment(s) for session %s",
                len(segments), session_id,
            )
        except Exception as e:
            logger.error("Error replacing session segments: %s", e)
            raise
        finally:
            conn.close()

    # ---- Interactions ----

    async def fetch_interactions(
        self, user_id: str, session_id: str
    ) -> List[InteractionRecord]:
        await self.assert_session_owned(user_id, session_id)
        conn = self._get_conn()
        try:
            rows = conn.execute(
                """SELECT id, session_id, parent_id, role, content, tool_name,
                          tool_call_id, channel, metadata, output, from_id, to_id,
                          source, status, session_seq, turn_id, turn_seq, chat_render_kind, created_at
                     FROM interactions
                    WHERE session_id = ?
                      AND (status IS NULL OR status != 'deleted')
                 ORDER BY created_at ASC, rowid ASC""",
                (session_id,),
            ).fetchall()
            return [InteractionRecord(**dict(r)) for r in rows]
        finally:
            conn.close()

    async def count_interactions(self, user_id: str, session_id: str) -> int:
        await self.assert_session_owned(user_id, session_id)
        conn = self._get_conn()
        try:
            row = conn.execute(
                """SELECT COUNT(*) AS n
                     FROM interactions
                    WHERE session_id = ?
                      AND (status IS NULL OR status != 'deleted')""",
                (session_id,),
            ).fetchone()
            return int(row["n"] if row else 0)
        finally:
            conn.close()

    async def fetch_session_interaction(
        self, user_id: str, session_id: str, interaction_id: str,
    ) -> Optional[InteractionRecord]:
        """Load one transcript row without materializing the whole session."""
        await self.assert_session_owned(user_id, session_id)
        conn = self._get_conn()
        try:
            row = conn.execute(
                """SELECT id, session_id, parent_id, role, content, tool_name,
                          tool_call_id, channel, metadata, output, from_id, to_id,
                          source, status, session_seq, turn_id, turn_seq, chat_render_kind, created_at
                     FROM interactions
                    WHERE session_id = ? AND id = ?
                      AND (status IS NULL OR status != 'deleted')
                    LIMIT 1""",
                (session_id, interaction_id),
            ).fetchone()
            return InteractionRecord(**dict(row)) if row else None
        finally:
            conn.close()

    async def has_promoted_closing_terminal(
        self, user_id: str, session_id: str,
    ) -> bool:
        """Bounded probe: did THIS run already promote its closing terminal?

        The run boundary is its user turn (the transcript's own run group), so
        the probe looks only at assistant rows newer than the session's newest
        user row. A durable ``promoted_answer`` is the only promotion signal, so
        a hit means the run already delivered its one promoted answer and a re-run (crash-resume, browser
        outbox retry) must not promote a second. A session with no user row has no run boundary — report False
        rather than matching some older run's promotion.
        """
        await self.assert_session_owned(user_id, session_id)
        conn = self._get_conn()
        try:
            row = conn.execute(
                """SELECT 1
                     FROM interactions
                    WHERE session_id = ?
                      AND role = 'assistant'
                      AND (status IS NULL OR status != 'deleted')
                      AND EXISTS (SELECT 1 FROM interactions
                                   WHERE session_id = ? AND role = 'user')
                      AND session_seq > COALESCE((
                            SELECT MAX(session_seq) FROM interactions
                             WHERE session_id = ? AND role = 'user'), 0)
                      AND chat_render_kind = 'promoted_answer'
                    LIMIT 1""",
                (session_id, session_id, session_id),
            ).fetchone()
            return bool(row)
        finally:
            conn.close()

    async def has_pre_tool_progress_message(
        self, user_id: str, session_id: str,
    ) -> bool:
        """Bounded probe: did THIS run already publish its progress update?

        The pre-tool progress-message gate asks once per run; the run boundary is its
        user turn (the transcript's own run group), so the probe looks only at
        assistant rows newer than the session's newest user row.
        ``pre_tool_progress_message_sent`` is written ONLY by the loop's publish site, so a hit
        means this run already announced and a re-entry (crash-resume, browser
        outbox retry) must not ask for or publish a second progress update. A session with
        no user row has no run boundary — report False rather than matching some
        older run's announcement.
        """
        await self.assert_session_owned(user_id, session_id)
        conn = self._get_conn()
        try:
            row = conn.execute(
                """SELECT 1
                     FROM interactions
                    WHERE session_id = ?
                      AND role = 'assistant'
                      AND (status IS NULL OR status != 'deleted')
                      AND EXISTS (SELECT 1 FROM interactions
                                   WHERE session_id = ? AND role = 'user')
                      AND session_seq > COALESCE((
                            SELECT MAX(session_seq) FROM interactions
                             WHERE session_id = ? AND role = 'user'), 0)
                      AND (metadata LIKE '%"pre_tool_progress_message_sent": true%'
                           OR metadata LIKE '%"plan_announced": true%')
                    LIMIT 1""",
                (session_id, session_id, session_id),
            ).fetchone()
            return bool(row)
        finally:
            conn.close()

    async def closing_attempts_for_run(
        self, user_id: str, session_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Bounded probe: how much of the closing budget THIS run already spent.

        The closing gate re-injects its instruction until its compliance budget is
        spent, then promotes the last message as a labelled PARTIAL. That budget
        lives in per-run contract state, which dies with the run — so a
        crash-resume would restart it at attempt 1 and the gate could never reach
        ``attempts_exhausted``, looping instead of terminating within budget. The
        folded draft rows carry ``closing_attempts`` (written ONLY by the closing
        retry site) and the run boundary is its user turn, so the probe reads the
        NEWEST matching assistant row after the session's newest user row — the
        run's high-water mark. A session with no user row has no run boundary —
        report None rather than matching some older run's tally.
        """
        await self.assert_session_owned(user_id, session_id)
        conn = self._get_conn()
        try:
            row = conn.execute(
                """SELECT id, content, metadata
                     FROM interactions
                    WHERE session_id = ?
                      AND role = 'assistant'
                      AND (status IS NULL OR status != 'deleted')
                      AND EXISTS (SELECT 1 FROM interactions
                                   WHERE session_id = ? AND role = 'user')
                      AND session_seq > COALESCE((
                            SELECT MAX(session_seq) FROM interactions
                             WHERE session_id = ? AND role = 'user'), 0)
                      AND metadata LIKE '%"closing_attempts"%'
                    ORDER BY session_seq DESC
                    LIMIT 1""",
                (session_id, session_id, session_id),
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        try:
            meta = json.loads(row["metadata"] or "{}")
        except Exception:  # noqa: BLE001 — malformed metadata is not a tally
            return None
        marker = meta.get("closing_attempts") if isinstance(meta, dict) else None
        if not isinstance(marker, dict):
            return None
        result = dict(marker)
        if bool(meta.get("closing_metadata_candidate")):
            result["metadata_candidate"] = {
                "interaction_id": str(row["id"] or ""),
                "content": str(row["content"] or ""),
            }
        return result

    async def has_declared_assertions(
        self, user_id: str, session_id: str,
    ) -> bool:
        """Bounded probe: did THIS run already publish its introduction?

        The introductory gate publishes exactly one opening bubble per run; the
        run boundary is its user turn (the transcript's own run group), so the
        probe looks only at assistant rows newer than the session's newest user
        row. ``assertions_announced`` is written ONLY by the loop's publish site,
        so a hit means this run already introduced itself and a re-entry
        (crash-resume, browser outbox retry) must not publish a second. A session
        with no user row has no run boundary — report False rather than matching
        some older run's introduction.
        """
        await self.assert_session_owned(user_id, session_id)
        conn = self._get_conn()
        try:
            row = conn.execute(
                """SELECT 1
                     FROM interactions
                    WHERE session_id = ?
                      AND role = 'assistant'
                      AND (status IS NULL OR status != 'deleted')
                      AND EXISTS (SELECT 1 FROM interactions
                                   WHERE session_id = ? AND role = 'user')
                      AND session_seq > COALESCE((
                            SELECT MAX(session_seq) FROM interactions
                             WHERE session_id = ? AND role = 'user'), 0)
                      AND metadata LIKE '%"assertions_announced": true%'
                    LIMIT 1""",
                (session_id, session_id, session_id),
            ).fetchone()
            return bool(row)
        finally:
            conn.close()

    async def fetch_interactions_from_offset(
        self, user_id: str, session_id: str, offset: int
    ) -> List[InteractionRecord]:
        """Materialize only the uncompacted suffix of a large transcript."""
        await self.assert_session_owned(user_id, session_id)
        conn = self._get_conn()
        try:
            rows = conn.execute(
                """SELECT id, session_id, parent_id, role, content, tool_name,
                          tool_call_id, channel, metadata, output, from_id, to_id,
                          source, status, session_seq, turn_id, turn_seq, created_at
                     FROM interactions
                    WHERE session_id = ?
                      AND (status IS NULL OR status != 'deleted')
                 ORDER BY created_at ASC, rowid ASC
                    LIMIT -1 OFFSET ?""",
                (session_id, max(0, int(offset or 0))),
            ).fetchall()
            return [InteractionRecord(**dict(r)) for r in rows]
        finally:
            conn.close()

    async def fetch_first_user_messages(
        self, user_id: str, session_id: str, limit: int = 3
    ) -> List[str]:
        """Opening user-message texts (oldest first), bounded to ``limit``.

        Used by the session-namer, which titles a chat from its first few user
        turns and then locks. A direct, LIMITed query keeps this O(limit) and
        loads only those rows — unlike fetch_interactions, which pulls the whole
        transcript into memory. Filters match the agent-context view: real user
        rows with non-blank content, terminal-tunnel traffic excluded.
        """
        await self.assert_session_owned(user_id, session_id)
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT content FROM interactions "
                "WHERE session_id = ? AND role = 'user' "
                "AND TRIM(COALESCE(content, '')) != '' "
                "AND COALESCE(source, '') != 'terminal_tunnel' "
                "ORDER BY created_at ASC LIMIT ?",
                (session_id, int(limit)),
            ).fetchall()
            return [r["content"] for r in rows]
        finally:
            conn.close()

    @staticmethod
    def _chat_render_kind(role: str, source: Optional[str], explicit: Optional[str]) -> str:
        """Choose the durable V2 render contract at the storage boundary."""
        if explicit:
            return explicit
        if role == "user":
            return "user"
        if role == "system" and source in {"system:closer", "system:summary", "system:overview", "system:mode", "system:model"}:
            return "notice"
        if role == "system" and str(source or "").startswith("system:closing_partial"):
            return "error"
        return "activity"

    async def insert_interaction_with_sequence(self, user_id: str, session_id: str, role: str, content: str, **kwargs) -> tuple[str, int]:
        """Atomically allocate and persist a durable transcript sequence."""
        await self.assert_session_owned(user_id, session_id)
        source = kwargs.get("source") or "user"
        chat_render_kind = self._chat_render_kind(role, source, kwargs.get("chat_render_kind"))
        async with self._write_lock:
            conn = self._get_conn()
            try:
                manifest = conn.execute("SELECT max_session_seq FROM session_manifests WHERE session_id=?", (session_id,)).fetchone()
                data = conn.execute("SELECT COALESCE(MAX(session_seq), 0) FROM interactions WHERE session_id=?", (session_id,)).fetchone()
                seq = max((manifest[0] if manifest and manifest[0] is not None else 0), data[0] if data else 0) + 1
                interaction_id = _uuid()
                conn.execute("INSERT INTO interactions (id, session_id, parent_id, role, content, tool_name, tool_call_id, channel, metadata, output, source, from_id, to_id, session_seq, turn_id, turn_seq, chat_render_kind, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (interaction_id, session_id, kwargs.get("parent_id"), role, content, kwargs.get("tool_name"), kwargs.get("tool_call_id"), kwargs.get("channel"), kwargs.get("metadata"), kwargs.get("output_data"), source, kwargs.get("sender_id"), kwargs.get("receiver_id"), seq, kwargs.get("turn_id"), kwargs.get("turn_seq"), chat_render_kind, kwargs.get("status", "complete")))
                project_interaction_activity(conn, interaction_id)
                conn.execute("INSERT INTO session_manifests (session_id, authority_revision, content_hash, interaction_count, max_session_seq, dirty, updated_at) VALUES (?, 1, '', 0, ?, 1, datetime('now')) ON CONFLICT(session_id) DO UPDATE SET max_session_seq=MAX(session_manifests.max_session_seq, excluded.max_session_seq), dirty=1, updated_at=datetime('now')", (session_id, seq))
                conn.commit(); return interaction_id, seq
            except Exception:
                conn.rollback(); raise
            finally: conn.close()

    async def insert_interaction(
        self,
        user_id: str,
        session_id: str,
        role: str,
        content: str,
        parent_id: Optional[str] = None,
        tool_name: Optional[str] = None,
        tool_call_id: Optional[str] = None,
        channel: Optional[str] = None,
        metadata: Optional[str] = None,
        output_data: Optional[str] = None,
        sender_id: Optional[str] = None,
        receiver_id: Optional[str] = None,
        source: Optional[str] = None,
        session_seq: Optional[int] = None,
        turn_id: Optional[str] = None,
        chat_render_kind: Optional[str] = None,
        turn_seq: Optional[int] = None,
        status: str = "complete",
    ) -> str:
        if session_seq is None:
            interaction_id, _ = await self.insert_interaction_with_sequence(user_id, session_id, role, content, parent_id=parent_id, tool_name=tool_name, tool_call_id=tool_call_id, channel=channel, metadata=metadata, output_data=output_data, sender_id=sender_id, receiver_id=receiver_id, source=source, turn_id=turn_id, turn_seq=turn_seq, status=status, chat_render_kind=chat_render_kind)
            return interaction_id
        await self.assert_session_owned(user_id, session_id)
        # Optimizer/Closer sessions get source='optimizer' for all interactions
        if source is None and (session_id.startswith('optimizer-') or session_id.startswith('closer-')):
            source = 'optimizer'
        chat_render_kind = self._chat_render_kind(role, source, chat_render_kind)

        async with self._write_lock:
            conn = self._get_conn()
            try:
                interaction_id = _uuid()
                conn.execute(
                    "INSERT INTO interactions (id, session_id, parent_id, role, content, tool_name, tool_call_id, channel, metadata, output, source, from_id, to_id, session_seq, turn_id, turn_seq, chat_render_kind, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (interaction_id, session_id, parent_id, role, content, tool_name, tool_call_id, channel, metadata, output_data, source or 'user', sender_id, receiver_id, session_seq, turn_id, turn_seq, chat_render_kind, status),
                )
                project_interaction_activity(conn, interaction_id)
                conn.commit()
                logger.debug("Inserted interaction %s", interaction_id)
                return interaction_id
            except Exception as e:
                logger.error("Error inserting interaction: %s", e)
                raise
            finally:
                conn.close()

    async def insert_interactions_batch(self, rows: List[Dict[str, Any]]) -> List[str]:
        """Bulk-insert interactions in one transaction.

        Each row dict may contain any of the columns: id, session_id, parent_id,
        role, content, tool_name, tool_call_id, channel, metadata, output,
        source, from_id, to_id, session_seq, turn_id, turn_seq, created_at.
        Missing optional fields default to None / sane defaults.
        Caller is responsible for session ownership.
        Returns the list of inserted ids in the same order.
        """
        if not rows:
            return []
        async with self._write_lock:
            conn = self._get_conn()
            try:
                ids: List[str] = []
                values: List[tuple] = []
                for r in rows:
                    rid = r.get("id") or _uuid()
                    ids.append(rid)
                    created_at = r.get("created_at")  # caller-supplied ISO timestamp or None
                    source = r.get("source") or "user"
                    role = r.get("role", "tool")
                    chat_render_kind = self._chat_render_kind(role, source, r.get("chat_render_kind"))
                    values.append((
                        rid,
                        r["session_id"],
                        r.get("parent_id"),
                        role,
                        r.get("content", ""),
                        r.get("tool_name"),
                        r.get("tool_call_id"),
                        r.get("channel"),
                        r.get("metadata"),
                        r.get("output"),
                        source,
                        r.get("from_id"),
                        r.get("to_id"),
                        r.get("session_seq"),
                        r.get("turn_id"),
                        r.get("turn_seq"),
                        chat_render_kind,
                        created_at,
                    ))
                # Use COALESCE to keep schema default when created_at is None.
                conn.executemany(
                    "INSERT INTO interactions (id, session_id, parent_id, role, content, tool_name, tool_call_id, channel, metadata, output, source, from_id, to_id, session_seq, turn_id, turn_seq, chat_render_kind, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, datetime('now')))",
                    values,
                )
                for interaction_id in ids:
                    project_interaction_activity(conn, interaction_id)
                conn.commit()
                logger.debug("Bulk inserted %d interactions", len(ids))
                return ids
            except Exception as e:
                logger.error("Error in insert_interactions_batch: %s", e)
                raise
            finally:
                conn.close()

    async def next_session_seq(self, session_id: str, count: int = 1) -> int:
        """Reserve `count` consecutive session_seq values; return the FIRST one.

        Returns 1 if the session has no prior rows. Caller assigns
        FIRST, FIRST+1, ..., FIRST+count-1 to its rows.

        Always takes the MAX of the manifest counter AND the actual interaction
        data.  The manifest can drift behind the data when an in-memory
        RunBuffer allocates sequences across turns without persisting its
        high-water mark — a server restart (or buffer sweep) would re-read the
        stale manifest, re-issue already-used numbers, and produce duplicate
        session_seq values that cause duplicated messages in the UI.
        """
        if count < 1:
            count = 1
        async with self._write_lock:
            conn = self._get_conn()
            try:
                # Read the manifest counter (may be stale)
                row = conn.execute(
                    "SELECT max_session_seq FROM session_manifests WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                manifest_val = row[0] if row and row[0] is not None else 0

                # Always also scan the actual data — the manifest can only drift
                # behind, never ahead.  The covering index
                # idx_interactions_session_seq makes this a cheap index seek.
                row = conn.execute(
                    "SELECT COALESCE(MAX(session_seq), 0) FROM interactions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                data_val = row[0] if row else 0

                current = max(manifest_val, data_val)
                reserved = current + count

                # Reserve the range by upserting the manifest
                conn.execute(
                    "INSERT INTO session_manifests "
                    "(session_id, authority_revision, content_hash, interaction_count, "
                    " max_session_seq, dirty, updated_at) "
                    "VALUES (?, 1, '', 0, ?, 1, datetime('now')) "
                    "ON CONFLICT(session_id) DO UPDATE SET "
                    "max_session_seq = MAX(session_manifests.max_session_seq, ?), "
                    "dirty = 1, updated_at = datetime('now')",
                    (session_id, reserved, reserved),
                )
                conn.commit()
                return current + 1
            finally:
                conn.close()

    async def persist_session_manifest_seq(self, session_id: str, high_water: int) -> None:
        """Persist the in-memory RunBuffer counter into the durable manifest.

        Called at turn-end so the manifest never drifts behind the actual data.
        Without this, a server restart (or buffer sweep) can cause
        ``next_session_seq`` to re-issue already-used numbers that produce
        duplicated messages in the UI.
        """
        if high_water < 1:
            return
        async with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    "INSERT INTO session_manifests "
                    "(session_id, authority_revision, content_hash, interaction_count, "
                    " max_session_seq, dirty, updated_at) "
                    "VALUES (?, 1, '', 0, ?, 1, datetime('now')) "
                    "ON CONFLICT(session_id) DO UPDATE SET "
                    "max_session_seq = MAX(session_manifests.max_session_seq, ?), "
                    "dirty = 1, updated_at = datetime('now')",
                    (session_id, high_water, high_water),
                )
                conn.commit()
            finally:
                conn.close()

    async def stamp_interaction_seq(
        self,
        interaction_id: str,
        session_seq: int,
        turn_id: Optional[str] = None,
        turn_seq: Optional[int] = None,
    ) -> Optional[int]:
        """Backfill the ordering columns (session_seq / turn_id / turn_seq) on an
        already-inserted interaction row.

        A row is often written before its RunBuffer-allocated sequence number is
        known (the user turn is saved, then the buffer starts; an assistant row is
        created on the first token, then finalized). This stamps the number in
        afterwards. On the plain (non-hybrid) backend the row lives in this same
        store, so it updates directly; the HybridBackend overrides this to write
        the LOCAL copy (where the local-first row actually lives) and enqueue the
        change for the background push to remote.

        Returns the row's authoritative durable ``session_seq`` — the number the
        transcript is actually ordered by: the existing one when the row already
        carries it, else the value just stamped. ``None`` when the row is gone.
        Callers use this to publish durable row order; a transport event cursor
        must never stand in for it, because the cursor advances once per EVENT,
        not once per ROW, and drifts far above the durable ledger."""
        async with self._write_lock:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    "SELECT session_id, created_at, metadata, session_seq FROM interactions WHERE id=?",
                    (interaction_id,),
                ).fetchone()
                if not row:
                    return None

                session_id = row[0]
                row_created_at = row[1]
                row_metadata = row[2]
                # New writes receive their sequence during INSERT. Late stamps are
                # legacy-only and must never overwrite durable commit ordering.
                if row[3] is not None:
                    return int(row[3])

                recovery_enabled = True
                recovery_threshold_seconds = 60
                try:
                    from app.admin import settings as _app_settings
                    recovery_enabled = bool(_app_settings.get_transcript_timestamp_order_recovery_enabled())
                    recovery_threshold_seconds = int(_app_settings.get_transcript_timestamp_order_recovery_threshold_seconds())
                except Exception:  # noqa: BLE001
                    recovery_enabled = True
                    recovery_threshold_seconds = 60

                recovery_threshold_seconds = max(0, int(recovery_threshold_seconds))

                if recovery_enabled and row_created_at and row[3] is None:
                    prior = conn.execute(
                        "SELECT created_at FROM interactions WHERE session_id=? AND session_seq IS NOT NULL AND session_seq < ? ORDER BY session_seq DESC LIMIT 1",
                        (session_id, session_seq),
                    ).fetchone()

                    def _parse_dt(value):
                        if value is None:
                            return None
                        try:
                            text = str(value).strip().replace(" ", "T")
                            if text.endswith("Z"):
                                text = text[:-1] + "+00:00"
                            parsed = datetime.fromisoformat(text)
                            if parsed.tzinfo is None:
                                parsed = parsed.replace(tzinfo=timezone.utc)
                            return parsed
                        except (TypeError, ValueError):
                            return None

                    prior_ts = _parse_dt(prior[0]) if prior else None
                    current_ts = _parse_dt(row_created_at)
                    if prior_ts is not None and current_ts is not None:
                        skew = abs((prior_ts - current_ts).total_seconds())
                    else:
                        skew = 0

                    if skew > recovery_threshold_seconds:
                        try:
                            interaction_metadata = json.loads(row_metadata or "{}")
                        except (TypeError, json.JSONDecodeError):
                            interaction_metadata = {}
                        interaction_metadata.update({
                            "timestamp_order_anomaly": True,
                            "timestamp_order_skew_seconds": int(skew),
                        })
                        conn.execute(
                            "UPDATE interactions SET metadata=? WHERE id=?",
                            (json.dumps(interaction_metadata), interaction_id),
                        )

                        session_row = conn.execute(
                            "SELECT metadata FROM sessions WHERE id=?",
                            (session_id,),
                        ).fetchone()
                        try:
                            session_metadata = json.loads(session_row[0] or "{}") if session_row else {}
                        except (TypeError, json.JSONDecodeError):
                            session_metadata = {}
                        session_metadata["transcript_timestamp_order_recovery"] = True
                        conn.execute(
                            "UPDATE sessions SET metadata=? WHERE id=?",
                            (json.dumps(session_metadata), session_id),
                        )

                conn.execute(
                    "UPDATE interactions SET session_seq=?, turn_id=COALESCE(?, turn_id), "
                    "turn_seq=COALESCE(?, turn_seq) WHERE id=?",
                    (session_seq, turn_id, turn_seq, interaction_id),
                )
                conn.commit()
                return int(session_seq)
            finally:
                conn.close()

    # ── Diagnostics (flight-recorder durable store) ───────────────────────────

    async def insert_diagnostics_batch(self, rows: List[Dict[str, Any]]) -> int:
        """Bulk-insert diagnostic records. Idempotent on id (INSERT OR IGNORE).

        ``detail`` is expected to already be a JSON string (the recorder
        serializes it). Returns the number of rows written."""
        if not rows:
            return 0
        async with self._write_lock:
            conn = self._get_conn()
            try:
                values = [(
                    r.get("id") or _uuid(),
                    r.get("ts") or _now_iso(),
                    (r.get("level") or "info"),
                    (r.get("category") or "server"),
                    r.get("source"),
                    (r.get("message") or ""),
                    r.get("detail"),
                    r.get("session_id"),
                    r.get("turn_id"),
                    r.get("agent_id"),
                    r.get("user_id"),
                ) for r in rows]
                conn.executemany(
                    "INSERT OR IGNORE INTO diagnostics "
                    "(id, ts, level, category, source, message, detail, session_id, turn_id, agent_id, user_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    values,
                )
                conn.commit()
                return len(values)
            except Exception as e:
                logger.error("Error in insert_diagnostics_batch: %s", e)
                return 0
            finally:
                conn.close()

    async def query_diagnostics(
        self,
        *,
        levels: Optional[List[str]] = None,
        categories: Optional[List[str]] = None,
        session_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        since: Optional[str] = None,
        search: Optional[str] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """Query durable diagnostic rows, newest first."""
        where: List[str] = []
        params: List[Any] = []
        if levels:
            where.append("level IN (%s)" % ",".join("?" * len(levels)))
            params.extend([str(x).lower() for x in levels])
        if categories:
            where.append("category IN (%s)" % ",".join("?" * len(categories)))
            params.extend([str(x).lower() for x in categories])
        if session_id:
            where.append("session_id = ?")
            params.append(session_id)
        if agent_id:
            where.append("agent_id = ?")
            params.append(agent_id)
        if since:
            where.append("ts >= ?")
            params.append(since)
        if search:
            where.append("(message LIKE ? OR source LIKE ? OR detail LIKE ?)")
            like = f"%{search}%"
            params.extend([like, like, like])
        sql = "SELECT * FROM diagnostics"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(max(1, min(int(limit or 200), 2000)))
        conn = self._get_conn()
        try:
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    async def prune_diagnostics(
        self, *, max_rows: int = 20000, max_age_seconds: Optional[float] = None
    ) -> int:
        """Delete diagnostics older than the age cap and beyond the row cap."""
        deleted = 0
        async with self._write_lock:
            conn = self._get_conn()
            try:
                if max_age_seconds:
                    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds)).isoformat()
                    cur = conn.execute("DELETE FROM diagnostics WHERE ts < ?", (cutoff,))
                    deleted += cur.rowcount or 0
                if max_rows and max_rows > 0:
                    cur = conn.execute(
                        "DELETE FROM diagnostics WHERE id NOT IN "
                        "(SELECT id FROM diagnostics ORDER BY ts DESC LIMIT ?)",
                        (int(max_rows),),
                    )
                    deleted += cur.rowcount or 0
                conn.commit()
                return deleted
            except Exception as e:
                logger.error("Error in prune_diagnostics: %s", e)
                return deleted
            finally:
                conn.close()

    async def clear_diagnostics(
        self,
        *,
        older_than_seconds: Optional[float] = None,
        levels: Optional[List[str]] = None,
        categories: Optional[List[str]] = None,
        search: Optional[str] = None,
    ) -> int:
        """Delete diagnostic rows matching the scope. No scope → delete all.
        Returns the number of rows deleted."""
        where: List[str] = []
        params: List[Any] = []
        if older_than_seconds is not None:
            cutoff = (datetime.now(timezone.utc) - timedelta(seconds=older_than_seconds)).isoformat()
            where.append("ts < ?")
            params.append(cutoff)
        if levels:
            where.append("level IN (%s)" % ",".join("?" * len(levels)))
            params.extend([str(x).lower() for x in levels])
        if categories:
            where.append("category IN (%s)" % ",".join("?" * len(categories)))
            params.extend([str(x).lower() for x in categories])
        if search:
            where.append("(message LIKE ? OR source LIKE ? OR detail LIKE ?)")
            like = f"%{search}%"
            params.extend([like, like, like])
        sql = "DELETE FROM diagnostics"
        if where:
            sql += " WHERE " + " AND ".join(where)
        async with self._write_lock:
            conn = self._get_conn()
            try:
                cur = conn.execute(sql, params)
                conn.commit()
                return cur.rowcount or 0
            except Exception as e:
                logger.error("Error in clear_diagnostics: %s", e)
                return 0
            finally:
                conn.close()

    # ── Client render recorder (see app/agent/render_recorder.py) ──────────────

    async def insert_render_recordings_batch(self, rows: List[Dict[str, Any]]) -> int:
        """Bulk-insert client render-recorder rows. Idempotent on id.

        ``detail`` is expected to already be a JSON string. Returns rows written."""
        if not rows:
            return 0
        async with self._write_lock:
            conn = self._get_conn()
            try:
                values = [(
                    r.get("id") or _uuid(),
                    r.get("ts") or _now_iso(),
                    r.get("recv_ts"),
                    (r.get("kind") or "meta"),
                    r.get("session_id"),
                    r.get("turn_id"),
                    r.get("session_seq"),
                    r.get("user_id"),
                    r.get("agent_id"),
                    r.get("client_id"),
                    r.get("seq"),
                    r.get("url"),
                    r.get("level"),
                    r.get("label"),
                    r.get("value_num"),
                    r.get("detail"),
                    r.get("html"),
                    r.get("html_bytes"),
                ) for r in rows]
                conn.executemany(
                    "INSERT OR IGNORE INTO render_recordings "
                    "(id, ts, recv_ts, kind, session_id, turn_id, session_seq, user_id, "
                    " agent_id, client_id, seq, url, level, label, value_num, detail, html, html_bytes) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    values,
                )
                conn.commit()
                return len(values)
            except Exception as e:
                logger.error("Error in insert_render_recordings_batch: %s", e)
                return 0
            finally:
                conn.close()

    async def query_render_recordings(
        self,
        *,
        rec_id: Optional[str] = None,
        kinds: Optional[List[str]] = None,
        levels: Optional[List[str]] = None,
        session_id: Optional[str] = None,
        client_id: Optional[str] = None,
        session_seq: Optional[int] = None,
        since: Optional[str] = None,
        search: Optional[str] = None,
        include_html: bool = False,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """Query durable render-recorder rows, newest first.

        ``include_html=False`` omits the (potentially large) ``html`` column from
        the result so list views stay light — fetch a single row by ``rec_id``
        with ``include_html=True`` on demand instead."""
        where: List[str] = []
        params: List[Any] = []
        if rec_id:
            where.append("id = ?")
            params.append(rec_id)
        if kinds:
            where.append("kind IN (%s)" % ",".join("?" * len(kinds)))
            params.extend([str(x).lower() for x in kinds])
        if levels:
            where.append("level IN (%s)" % ",".join("?" * len(levels)))
            params.extend([str(x).lower() for x in levels])
        if session_id:
            where.append("session_id = ?")
            params.append(session_id)
        if client_id:
            where.append("client_id = ?")
            params.append(client_id)
        if session_seq is not None:
            where.append("session_seq = ?")
            params.append(int(session_seq))
        if since:
            where.append("ts >= ?")
            params.append(since)
        if search:
            where.append("(label LIKE ? OR detail LIKE ? OR url LIKE ?)")
            like = f"%{search}%"
            params.extend([like, like, like])
        cols = ("id, ts, recv_ts, kind, session_id, turn_id, session_seq, user_id, "
                "agent_id, client_id, seq, url, level, label, value_num, detail, html_bytes")
        if include_html:
            cols += ", html"
        sql = f"SELECT {cols} FROM render_recordings"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(max(1, min(int(limit or 200), 2000)))
        conn = self._get_conn()
        try:
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    async def prune_render_recordings(
        self, *, max_rows: int = 8000, max_age_seconds: Optional[float] = None
    ) -> int:
        """Delete render recordings older than the age cap and beyond the row cap."""
        deleted = 0
        async with self._write_lock:
            conn = self._get_conn()
            try:
                if max_age_seconds:
                    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds)).isoformat()
                    cur = conn.execute("DELETE FROM render_recordings WHERE ts < ?", (cutoff,))
                    deleted += cur.rowcount or 0
                if max_rows and max_rows > 0:
                    cur = conn.execute(
                        "DELETE FROM render_recordings WHERE id NOT IN "
                        "(SELECT id FROM render_recordings ORDER BY ts DESC LIMIT ?)",
                        (int(max_rows),),
                    )
                    deleted += cur.rowcount or 0
                conn.commit()
                return deleted
            except Exception as e:
                logger.error("Error in prune_render_recordings: %s", e)
                return deleted
            finally:
                conn.close()

    async def clear_render_recordings(
        self,
        *,
        older_than_seconds: Optional[float] = None,
        kinds: Optional[List[str]] = None,
        session_id: Optional[str] = None,
        search: Optional[str] = None,
    ) -> int:
        """Delete render-recorder rows matching the scope. No scope → delete all."""
        where: List[str] = []
        params: List[Any] = []
        if older_than_seconds is not None:
            cutoff = (datetime.now(timezone.utc) - timedelta(seconds=older_than_seconds)).isoformat()
            where.append("ts < ?")
            params.append(cutoff)
        if kinds:
            where.append("kind IN (%s)" % ",".join("?" * len(kinds)))
            params.extend([str(x).lower() for x in kinds])
        if session_id:
            where.append("session_id = ?")
            params.append(session_id)
        if search:
            where.append("(label LIKE ? OR detail LIKE ? OR url LIKE ?)")
            like = f"%{search}%"
            params.extend([like, like, like])
        sql = "DELETE FROM render_recordings"
        if where:
            sql += " WHERE " + " AND ".join(where)
        async with self._write_lock:
            conn = self._get_conn()
            try:
                cur = conn.execute(sql, params)
                conn.commit()
                return cur.rowcount or 0
            except Exception as e:
                logger.error("Error in clear_render_recordings: %s", e)
                return 0
            finally:
                conn.close()

    async def render_recordings_stats(self) -> Dict[str, Any]:
        """Row count, total bytes and per-kind breakdown for the recorder table."""
        conn = self._get_conn()
        try:
            total, by_kind = 0, {}
            row = conn.execute(
                "SELECT COUNT(*) c, COALESCE(SUM(html_bytes),0) b FROM render_recordings"
            ).fetchone()
            total = row["c"] if row else 0
            total_bytes = row["b"] if row else 0
            for r in conn.execute(
                "SELECT kind, COUNT(*) c FROM render_recordings GROUP BY kind"
            ).fetchall():
                by_kind[r["kind"]] = r["c"]
            return {"rows": total, "html_bytes": total_bytes, "by_kind": by_kind}
        except Exception as e:
            logger.error("Error in render_recordings_stats: %s", e)
            return {"rows": 0, "html_bytes": 0, "by_kind": {}}
        finally:
            conn.close()

    async def copy_defaults_to_agent(self, agent_id: str, template_id: Optional[str] = None) -> int:
        """
        Copy admin-base prompt slots from the 'default' template into this agent.
        Only copies if the agent has no admin-base slot rows yet.

        Isolation gate: non-default templates do NOT inherit WebAgent context.
        """
        # Isolation gate: only 'default' templates inherit WebAgent context
        if template_id is not None and template_id != "default":
            logger.info(
                "Skipping context copy for template_id=%s (agent=%s)",
                template_id, agent_id[:8],
            )
            return 0
        conn = self._get_conn()
        try:
            existing = conn.execute(
                "SELECT 1 FROM agent_prompts WHERE agent_id = ? AND user_id IS NULL LIMIT 1",
                (agent_id,),
            ).fetchone()
            if existing:
                logger.debug("Agent %s already has slot rows — skipping copy", agent_id[:8])
                return 0

            tpl_rows = conn.execute(
                """SELECT slot_name, order_index, lock, merge_mode, content, version
                   FROM agent_prompt_templates
                   WHERE template_id = 'default'""",
            ).fetchall()
            if not tpl_rows:
                logger.warning("No default template slots found for context copy")
                return 0

            now = _now_iso()
            for row in tpl_rows:
                conn.execute(
                    """INSERT INTO agent_prompts
                       (id, agent_id, slot_name, user_id, order_index, lock, merge_mode,
                        content, template_version, updated_at, updated_by)
                       VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, 'system')""",
                    (_uuid(), agent_id, row["slot_name"], row["order_index"],
                     row["lock"], row["merge_mode"], row["content"], row["version"], now),
                )
            conn.execute("UPDATE agents SET updated_at = ? WHERE id = ?", (now, agent_id))
            conn.commit()
            logger.info(
                "Copied %d slots from default template to agent %s",
                len(tpl_rows), agent_id[:8],
            )
            return len(tpl_rows)
        except Exception as e:
            logger.error("Error copying defaults to agent: %s", e)
            raise
        finally:
            conn.close()

    @staticmethod
    def _execute_if_table_present(
        conn: sqlite3.Connection,
        table_name: str,
        statement: str,
        parameters: tuple,
    ) -> None:
        """Execute a legacy propagation statement when its table still exists."""
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
        if row:
            conn.execute(statement, parameters)

    def _seed_agent_templates_from_json_files(
        self,
        conn: sqlite3.Connection,
        force: bool = False,
    ) -> dict:
        """
        Manifest-gated, non-destructive seeder.

        Source of truth = data/agents/*.json. Writes:
          - agent_templates row (config only) — seed-managed upsert; rows whose
            metadata.source starts with ``admin`` are preserved unless forced
          - agent_prompt_templates rows (one per slot) — version-aware:
              * missing row              → INSERT (source='json')
              * existing source='json'   → UPDATE iff JSON version > DB version
              * existing source='admin'  → SKIP (admin edits are protected)
              * force=True               → overwrite admin rows too (bumps version)

        Short-circuit: hash data/agents/* into a manifest digest. If it matches
        app_meta['last_agent_manifest_hash'], force is False, AND the
        agent_templates table is non-empty, return immediately with
        {"changed": 0, "skipped_admin": 0, "cached": True}.

        Returns summary dict:
            {
              "changed": int,         # rows inserted/updated this pass
              "skipped_admin": int,   # admin-sourced rows we left alone
              "templates": int,       # config rows upserted
              "cached": bool,         # whether the manifest short-circuit fired
              "manifest_hash": str,
            }
        """
        from app.context.md_seeder import (
            scan_agent_json_files,
            compute_agent_manifest_hash,
        )

        manifest_hash = compute_agent_manifest_hash()

        # Short-circuit: if hash matches what's already applied, nothing to do.
        # Guard: only trust the hash if the table is actually populated. A stale
        # hash with an empty table (after a wipe, a fresh/diverged DB, or a
        # backend switch) would otherwise leave the app with 0 templates forever.
        if not force:
            row = conn.execute(
                "SELECT value FROM app_meta WHERE key = 'last_agent_manifest_hash'"
            ).fetchone()
            existing_count = conn.execute(
                "SELECT COUNT(*) FROM agent_templates"
            ).fetchone()[0]
            if row and row["value"] == manifest_hash and existing_count > 0:
                return {
                    "changed": 0,
                    "skipped_admin": 0,
                    "templates": 0,
                    "cached": True,
                    "manifest_hash": manifest_hash,
                }

        templates = scan_agent_json_files()
        if not templates:
            return {
                "changed": 0,
                "skipped_admin": 0,
                "templates": 0,
                "cached": False,
                "manifest_hash": manifest_hash,
            }

        now = _now_iso()
        changed = 0
        skipped_admin = 0

        for tpl in templates:
            tpl_id = tpl["id"]
            tpl_version = int(tpl.get("version") or 1)

            # Stamp origin marker so Config tab knows this template came from a JSON file.
            _tpl_meta = tpl.get("metadata", "{}")
            if isinstance(_tpl_meta, str):
                try:
                    _tpl_meta_obj = json.loads(_tpl_meta)
                except (json.JSONDecodeError, TypeError):
                    _tpl_meta_obj = {}
            elif isinstance(_tpl_meta, dict):
                _tpl_meta_obj = dict(_tpl_meta)
            else:
                _tpl_meta_obj = {}
            if not _tpl_meta_obj.get("source"):
                _tpl_meta_obj["source"] = "json_seed"
            tpl["metadata"] = json.dumps(_tpl_meta_obj)

            # 1. agent_templates row — config only. An administrator may edit
            # discoverability or save a customized agent back into the template;
            # that claims the row (metadata.source=admin_*). A later shipped JSON
            # change must not silently erase those live database edits.
            _existing_tpl = conn.execute(
                "SELECT metadata FROM agent_templates WHERE id = ?", (tpl_id,)
            ).fetchone()
            _existing_source = ""
            if _existing_tpl:
                try:
                    _existing_meta = json.loads(_existing_tpl["metadata"] or "{}")
                    _existing_source = str((_existing_meta or {}).get("source") or "")
                except (json.JSONDecodeError, TypeError):
                    _existing_source = ""
            if _existing_source.startswith("admin") and not force:
                skipped_admin += 1
            else:
                conn.execute(
                """INSERT INTO agent_templates
                   (id, name, description, icon, max_turn_count, max_wall_seconds,
                    max_identical_tool_calls, max_stall_strikes,
                    model, provider,
                    temperature, max_tokens, metadata,
                    can_be_default, is_system, is_pipeline, access_level,
                    is_admin_agent, discoverable, trigger_type, trigger_key, loop_logic,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    description = excluded.description,
                    icon = excluded.icon,
                    max_turn_count = excluded.max_turn_count,
                    max_wall_seconds = excluded.max_wall_seconds,
                    max_identical_tool_calls = excluded.max_identical_tool_calls,
                    max_stall_strikes = excluded.max_stall_strikes,
                    model = excluded.model,
                    provider = excluded.provider,
                    temperature = excluded.temperature,
                    max_tokens = excluded.max_tokens,
                    metadata = excluded.metadata,
                    can_be_default = excluded.can_be_default,
                    is_system = excluded.is_system,
                    is_pipeline = excluded.is_pipeline,
                    access_level = excluded.access_level,
                    is_admin_agent = excluded.is_admin_agent,
                    discoverable = excluded.discoverable,
                    trigger_type = excluded.trigger_type,
                    trigger_key = excluded.trigger_key,
                    loop_logic = excluded.loop_logic,
                    updated_at = excluded.updated_at""",
                    (tpl_id, tpl.get("name", tpl_id), tpl.get("description", ""),
                 tpl.get("icon", ""), tpl["max_turn_count"],
                 tpl.get("max_wall_seconds"),
                 tpl.get("max_identical_tool_calls", 0),
                 tpl.get("max_stall_strikes", 0),
                 tpl["model"], tpl["provider"], tpl["temperature"],
                 tpl["max_tokens"], tpl["metadata"],
                 tpl.get("can_be_default", 1), tpl.get("is_system", 0),
                 tpl.get("is_pipeline", 0), tpl.get("access_level", "all"),
                 1 if tpl.get("is_admin_agent") else 0,
                 1 if tpl.get("discoverable") else 0,
                 tpl.get("trigger_type", "user_input"), tpl.get("trigger_key"),
                 tpl.get("loop_logic", "[]"),
                     now, now),
                )

            # 2. agent_prompt_templates rows — per slot, version-gated upsert
            for s in self._slots_from_template_data(tpl):
                slot_name = s["slot_name"]
                existing = conn.execute(
                    """SELECT id, version, source FROM agent_prompt_templates
                       WHERE template_id = ? AND slot_name = ?""",
                    (tpl_id, slot_name),
                ).fetchone()

                slot_payload = (
                    s.get("order_index", 0) or 0,
                    1 if s.get("lock") else 0,
                    s.get("merge_mode", "replace"),
                    s.get("content", "") or "",
                )

                if existing is None:
                    # Brand-new slot row.
                    conn.execute(
                        """INSERT INTO agent_prompt_templates
                           (id, template_id, slot_name, order_index, lock,
                            merge_mode, content, version, source,
                            updated_at, updated_by)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'json', ?, 'system')""",
                        (_uuid(), tpl_id, slot_name,
                         slot_payload[0], slot_payload[1],
                         slot_payload[2], slot_payload[3],
                         tpl_version, now),
                    )
                    changed += 1
                    continue

                # Existing row — respect source guard.
                if existing["source"] == "admin" and not force:
                    skipped_admin += 1
                    continue

                # JSON-sourced (or force=True): bump iff version advanced.
                # Force always bumps (so admin → re-pinned to JSON content + version).
                if force or tpl_version > (existing["version"] or 0):
                    conn.execute(
                        """UPDATE agent_prompt_templates
                           SET order_index = ?,
                               lock = ?,
                               merge_mode = ?,
                               content = ?,
                               version = ?,
                               source = 'json',
                               updated_at = ?,
                               updated_by = ?
                           WHERE id = ?""",
                        (slot_payload[0], slot_payload[1],
                         slot_payload[2], slot_payload[3],
                         tpl_version, now,
                         "system" if not force else "system-force",
                         existing["id"]),
                    )
                    changed += 1

                    # Propagate the bump into existing per-agent admin-base rows
                    # (agent_prompts.user_id IS NULL) that have NOT been
                    # admin-edited (updated_by = 'system'). Admin-edited rows
                    # stay pinned; user override rows (user_id IS NOT NULL)
                    # are untouched regardless.
                    self._execute_if_table_present(conn, "agent_prompts",
                        """UPDATE agent_prompts
                           SET order_index = ?,
                               lock = ?,
                               merge_mode = ?,
                               content = ?,
                               template_version = ?,
                               updated_at = ?,
                               updated_by = 'system'
                           WHERE slot_name = ?
                             AND user_id IS NULL
                             AND updated_by = 'system'
                             AND (template_version IS NULL OR template_version < ?)
                             AND agent_id IN (
                               SELECT id FROM agents WHERE template_id = ?
                             )""",
                        (slot_payload[0], slot_payload[1],
                         slot_payload[2], slot_payload[3],
                         tpl_version, now,
                         slot_name, tpl_version, tpl_id),
                    )

        # Stamp the new manifest hash so the next call short-circuits.
        conn.execute(
            """INSERT INTO app_meta (key, value, updated_at)
               VALUES ('last_agent_manifest_hash', ?, ?)
               ON CONFLICT(key) DO UPDATE
                 SET value = excluded.value,
                     updated_at = excluded.updated_at""",
            (manifest_hash, now),
        )
        conn.commit()

        logger.info(
            "Seeded %d agent template(s) from JSON: %d slot rows changed, %d admin rows skipped%s",
            len(templates), changed, skipped_admin,
            " (force=True)" if force else "",
        )
        return {
            "changed": changed,
            "skipped_admin": skipped_admin,
            "templates": len(templates),
            "cached": False,
            "manifest_hash": manifest_hash,
        }

    @staticmethod
    def _with_discovery_default(metadata, mode: str) -> str:
        """Return the agent ``metadata`` JSON string with the per-agent default
        visibility (``discovery_default``) set to ``mode`` — UNLESS the metadata
        already carries an explicit choice (respecting a template/admin override).
        Accepts a dict or a JSON string; always returns a JSON string. Never
        raises — on any parse error it returns the input unchanged so agent
        creation can't be broken by a malformed template."""
        try:
            from app.tools.tool_modes import (
                AGENT_DISCOVERY_DEFAULT_KEY, normalize_visibility,
            )
            meta = json.loads(metadata) if isinstance(metadata, str) else dict(metadata or {})
            if not isinstance(meta, dict):
                return metadata if isinstance(metadata, str) else json.dumps(meta)
            nv = normalize_visibility(mode)
            if nv and not meta.get(AGENT_DISCOVERY_DEFAULT_KEY):
                meta[AGENT_DISCOVERY_DEFAULT_KEY] = nv
            return json.dumps(meta)
        except Exception:
            return metadata if isinstance(metadata, str) else json.dumps(metadata or {})

    @staticmethod
    def _tool_perm_columns(metadata) -> tuple:
        """Derive the (allowed_tools, safety_policy) column JSON for a NEW agent
        from a template's ``metadata.tool_permissions`` map {name: auto|ask|deny}.

        Mapping (the inverse of how the API/UI reconstruct the three-way toggle):
          - ``deny`` → tool added to ``allowed_tools`` (the blocked / disabled list)
          - ``ask``  → tool added to ``safety_policy.destructive_tools`` (needs
                       per-call confirmation)
          - ``auto`` / unknown → neither (the default; tool runs unattended)

        Accepts a metadata dict or its JSON string. Returns ``('[]', '{}')`` when
        no permissions are declared, so callers can use it unconditionally. Note:
        this only seeds INITIAL values for a new agent — once created, the agent's
        own columns are the source of truth (edited via the Tools panel)."""
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata or "{}")
            except (TypeError, ValueError):
                metadata = {}
        perms = metadata.get("tool_permissions") if isinstance(metadata, dict) else None
        if not isinstance(perms, dict) or not perms:
            return ("[]", "{}")
        deny = sorted({n for n, v in perms.items() if v == "deny"})
        ask = sorted({n for n, v in perms.items() if v == "ask"})
        allowed_tools = json.dumps(deny)
        safety_policy = json.dumps({"destructive_tools": ask}) if ask else "{}"
        return (allowed_tools, safety_policy)

    @staticmethod
    def _slots_from_template_data(tpl: dict) -> List[dict]:
        """Build a list of slot dicts from a template JSON.

        Prefers an explicit `slots` array. Falls back to converting the legacy
        keys (system_prompt, agent_prompt, user_prompt, skills_prompt,
        tasks_prompt, misc_prompt, bootstrap_tools) into slots.
        """
        raw_slots = tpl.get("slots")
        if isinstance(raw_slots, list) and raw_slots:
            out: List[dict] = []
            for i, s in enumerate(raw_slots):
                if not isinstance(s, dict):
                    continue
                name = (s.get("slot_name") or "").strip()
                if not name:
                    continue
                out.append({
                    "slot_name": name,
                    "order_index": int(s.get("order_index", (i + 1) * 10)),
                    "lock": bool(s.get("lock", False)),
                    "merge_mode": s.get("merge_mode") if s.get("merge_mode") in VALID_MERGE_MODES else "replace",
                    "content": s.get("content", "") or "",
                })
            out.extend(LocalBackend._skills_slot_from_template(tpl))
            return out

        # Legacy: derive slots from flat keys.
        legacy_map = [
            ("system",          "system_prompt",    10, True),
            ("agent",           "agent_prompt",     20, False),
            ("user",            "user_prompt",      30, False),
            ("skills",          "skills_prompt",    40, False),
            ("tasks",           "tasks_prompt",     50, False),
            ("misc",            "misc_prompt",      60, False),
            ("automation",      "automation_prompt", 70, False),
            ("bootstrap_tools", "bootstrap_tools",  90, True),
        ]
        out = []
        for slot_name, src_key, order, lock in legacy_map:
            content = tpl.get(src_key, "") or ""
            out.append({
                "slot_name": slot_name,
                "order_index": order,
                "lock": lock,
                "merge_mode": "replace",
                "content": content,
            })
        out.extend(LocalBackend._skills_slot_from_template(tpl))
        return out

    @staticmethod
    def _skills_slot_from_template(tpl: dict) -> List[dict]:
        """Build the single `__skills__` slot (JSON of skills) from a template's
        top-level `skills` array, or [] when there are none. The slot is locked
        (admin-managed) and ordered last; its content is never dumped raw into
        the prompt — the skills renderer reads it instead."""
        raw = tpl.get("skills")
        if not isinstance(raw, list) or not raw:
            return []
        from app.context.md_seeder import normalize_skills
        normalized = normalize_skills(raw)
        if not normalized:
            return []
        return [{
            "slot_name": "__skills__",
            "order_index": 200,
            "lock": True,
            "merge_mode": "replace",
            "content": json.dumps(normalized),
        }]

    def _clone_template_slots(
        self,
        conn: sqlite3.Connection,
        source_id: str,
        target_id: str,
        now: Optional[str] = None,
    ) -> int:
        """Clone canonical slot rows from agent_prompt_templates → agent_prompts.

        Reads agent_prompt_templates WHERE template_id = source_id. Falls back
        to template_id = 'default' if the requested source has no slots.
        Each cloned row gets template_version stamped from the source row so the
        agent can later be told "your template moved to v4, refresh?".
        """
        now = now or _now_iso()
        rows = conn.execute(
            """SELECT slot_name, order_index, lock, merge_mode, content, version
               FROM agent_prompt_templates WHERE template_id = ?
               ORDER BY order_index""",
            (source_id,),
        ).fetchall()
        if not rows and source_id != "default":
            rows = conn.execute(
                """SELECT slot_name, order_index, lock, merge_mode, content, version
                   FROM agent_prompt_templates WHERE template_id = 'default'
                   ORDER BY order_index""",
            ).fetchall()
        for r in rows:
            conn.execute(
                """INSERT INTO agent_prompts
                   (id, agent_id, slot_name, user_id, order_index, lock, merge_mode,
                    content, template_version, updated_at, updated_by)
                   VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, 'system')""",
                (_uuid(), target_id, r["slot_name"], r["order_index"],
                 r["lock"], r["merge_mode"], r["content"], r["version"], now),
            )
        return len(rows)

    # ---- Prompt slots: read / write / resolve ------------------------------

    async def list_slots(self, agent_id: str, user_id: Optional[str] = None) -> List[dict]:
        """Return admin-base slot rows for an agent, ordered by order_index.

        If user_id is provided, each row gains an `override_content` field
        (or None) showing the caller's user override, if any.
        """
        conn = self._get_conn()
        try:
            rows = conn.execute(
                """SELECT id, slot_name, order_index, lock, merge_mode, content,
                          updated_at, updated_by
                   FROM agent_prompts
                   WHERE agent_id = ? AND user_id IS NULL
                   ORDER BY order_index ASC""",
                (agent_id,),
            ).fetchall()
            result: List[dict] = []
            override_map: Dict[str, str] = {}
            if user_id:
                ov_rows = conn.execute(
                    """SELECT slot_name, content FROM agent_prompts
                       WHERE agent_id = ? AND user_id = ?""",
                    (agent_id, user_id),
                ).fetchall()
                override_map = {r["slot_name"]: r["content"] for r in ov_rows}
            for r in rows:
                d = dict(r)
                d["lock"] = bool(d.get("lock"))
                if user_id:
                    d["override_content"] = override_map.get(d["slot_name"])
                result.append(d)
            return result
        finally:
            conn.close()

    async def resolve_prompts(self, agent_id: str, user_id: Optional[str] = None) -> List[dict]:
        """Return resolved per-slot content for an agent + caller.

        Each entry: {slot_name, order_index, lock, merge_mode, content, used_override}.
        Locked slots ignore overrides. Unlocked slots apply replace/append based
        on the slot's merge_mode.
        """
        slots = await self.list_slots(agent_id, user_id=user_id)
        out: List[dict] = []
        for s in slots:
            base = s.get("content", "") or ""
            override = s.get("override_content") if user_id else None
            lock = bool(s.get("lock"))
            mode = s.get("merge_mode") if s.get("merge_mode") in VALID_MERGE_MODES else "replace"
            resolved = _slot_apply(base, override, lock, mode)
            out.append({
                "slot_name": s["slot_name"],
                "order_index": s["order_index"],
                "lock": lock,
                "merge_mode": mode,
                "content": resolved,
                "used_override": (not lock and override is not None),
            })
        return out

    async def assemble_prompt(self, agent_id: str, user_id: Optional[str] = None) -> str:
        """Return all resolved slot content concatenated in order. Empty slots are skipped."""
        slots = await self.resolve_prompts(agent_id, user_id=user_id)
        parts: List[str] = []
        for s in slots:
            txt = (s.get("content") or "").strip()
            if txt:
                parts.append(txt)
        return "\n\n".join(parts)

    async def upsert_slot(
        self,
        agent_id: str,
        slot_name: str,
        order_index: int,
        lock: bool,
        merge_mode: str,
        content: str,
        updated_by: str = "admin",
    ) -> dict:
        """Insert or update an admin-base slot row (user_id IS NULL)."""
        if merge_mode not in VALID_MERGE_MODES:
            merge_mode = "replace"
        slot_name = (slot_name or "").strip()
        if not slot_name:
            raise ValueError("slot_name required")
        now = _now_iso()
        async with self._write_lock:
            conn = self._get_conn()
            try:
                existing = conn.execute(
                    """SELECT id FROM agent_prompts
                       WHERE agent_id = ? AND slot_name = ? AND user_id IS NULL""",
                    (agent_id, slot_name),
                ).fetchone()
                if existing:
                    conn.execute(
                        """UPDATE agent_prompts SET
                              order_index = ?, lock = ?, merge_mode = ?,
                              content = ?, updated_at = ?, updated_by = ?
                           WHERE id = ?""",
                        (int(order_index), 1 if lock else 0, merge_mode,
                         content or "", now, updated_by, existing["id"]),
                    )
                    row_id = existing["id"]
                else:
                    row_id = _uuid()
                    conn.execute(
                        """INSERT INTO agent_prompts
                           (id, agent_id, slot_name, user_id, order_index, lock, merge_mode,
                            content, updated_at, updated_by)
                           VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)""",
                        (row_id, agent_id, slot_name, int(order_index),
                         1 if lock else 0, merge_mode, content or "", now, updated_by),
                    )
                conn.commit()
                return {
                    "id": row_id,
                    "agent_id": agent_id,
                    "slot_name": slot_name,
                    "order_index": int(order_index),
                    "lock": bool(lock),
                    "merge_mode": merge_mode,
                    "content": content or "",
                    "updated_at": now,
                    "updated_by": updated_by,
                }
            finally:
                conn.close()

    async def delete_slot(self, agent_id: str, slot_name: str) -> int:
        """Delete an admin-base slot AND every per-user override for that slot."""
        async with self._write_lock:
            conn = self._get_conn()
            try:
                cur = conn.execute(
                    "DELETE FROM agent_prompts WHERE agent_id = ? AND slot_name = ?",
                    (agent_id, slot_name),
                )
                conn.commit()
                return cur.rowcount
            finally:
                conn.close()

    async def reset_overrides(self, agent_id: str, slot_name: str) -> int:
        """Delete all user override rows for a slot. Leaves the admin base intact."""
        async with self._write_lock:
            conn = self._get_conn()
            try:
                cur = conn.execute(
                    """DELETE FROM agent_prompts
                       WHERE agent_id = ? AND slot_name = ? AND user_id IS NOT NULL""",
                    (agent_id, slot_name),
                )
                conn.commit()
                return cur.rowcount
            finally:
                conn.close()

    async def upsert_override(
        self,
        agent_id: str,
        slot_name: str,
        user_id: str,
        content: str,
        updated_by: str = "user",
    ) -> Optional[dict]:
        """Insert or update the caller's override row for a slot.

        Refuses (returns None) if the slot is not defined in the agent's
        admin-base rows. Locked slots are still writable — the lock guards
        runtime prompt assembly (via _slot_apply), not saving. This allows
        the Config tab's push-to-template button to promote overrides on
        locked slots into the template.
        """
        if not user_id:
            raise ValueError("user_id required for override write")
        now = _now_iso()
        async with self._write_lock:
            conn = self._get_conn()
            try:
                base = conn.execute(
                    """SELECT lock FROM agent_prompts
                       WHERE agent_id = ? AND slot_name = ? AND user_id IS NULL""",
                    (agent_id, slot_name),
                ).fetchone()
                if not base:
                    return None
                existing = conn.execute(
                    """SELECT id FROM agent_prompts
                       WHERE agent_id = ? AND slot_name = ? AND user_id = ?""",
                    (agent_id, slot_name, user_id),
                ).fetchone()
                if existing:
                    conn.execute(
                        """UPDATE agent_prompts SET content = ?, updated_at = ?, updated_by = ?
                           WHERE id = ?""",
                        (content or "", now, updated_by, existing["id"]),
                    )
                    row_id = existing["id"]
                else:
                    row_id = _uuid()
                    conn.execute(
                        """INSERT INTO agent_prompts
                           (id, agent_id, slot_name, user_id, content, updated_at, updated_by)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (row_id, agent_id, slot_name, user_id, content or "", now, updated_by),
                    )
                conn.commit()
                return {
                    "id": row_id,
                    "agent_id": agent_id,
                    "slot_name": slot_name,
                    "user_id": user_id,
                    "content": content or "",
                    "updated_at": now,
                    "updated_by": updated_by,
                }
            finally:
                conn.close()

    async def delete_override(self, agent_id: str, slot_name: str, user_id: str) -> bool:
        """Remove the caller's override row for a slot. Returns True if a row was deleted."""
        async with self._write_lock:
            conn = self._get_conn()
            try:
                cur = conn.execute(
                    """DELETE FROM agent_prompts
                       WHERE agent_id = ? AND slot_name = ? AND user_id = ?""",
                    (agent_id, slot_name, user_id),
                )
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    async def replace_slots(
        self,
        agent_id: str,
        slots: List[dict],
        reset_overrides_for: Optional[List[str]] = None,
        updated_by: str = "admin",
    ) -> List[dict]:
        """Reconcile an agent's admin-base slot set against a desired list.

        - Slots in `slots` are upserted (matched by slot_name).
        - Admin-base rows whose slot_name is no longer in the payload are deleted
          (which cascades — also drops the slot's per-user overrides).
        - `reset_overrides_for` is a list of slot_names whose user overrides
          should be wiped at save time.
        Returns the new resolved admin-base list.
        """
        desired_names = {(s.get("slot_name") or "").strip() for s in slots if isinstance(s, dict)}
        desired_names.discard("")

        # 1. Reconcile: delete slots that no longer exist.
        conn = self._get_conn()
        try:
            existing_names = {r["slot_name"] for r in conn.execute(
                "SELECT slot_name FROM agent_prompts WHERE agent_id = ? AND user_id IS NULL",
                (agent_id,),
            ).fetchall()}
        finally:
            conn.close()
        # Never let the generic slot editor delete the skills slot — it's managed
        # through the dedicated skills endpoints, not the slot reconcile.
        stale_names = existing_names - desired_names
        stale_names.discard("__skills__")
        for stale in stale_names:
            await self.delete_slot(agent_id, stale)

        # 2. Upsert each slot.
        for s in slots:
            if not isinstance(s, dict):
                continue
            name = (s.get("slot_name") or "").strip()
            if not name:
                continue
            await self.upsert_slot(
                agent_id=agent_id,
                slot_name=name,
                order_index=int(s.get("order_index", 0) or 0),
                lock=bool(s.get("lock", False)),
                merge_mode=s.get("merge_mode", "replace"),
                content=s.get("content", "") or "",
                updated_by=updated_by,
            )

        # 3. Reset overrides where requested.
        if reset_overrides_for:
            for name in reset_overrides_for:
                if not name:
                    continue
                await self.reset_overrides(agent_id, name)

        return await self.list_slots(agent_id)

    # ---- Memory System (knowledge brain) ----

    async def memory_upsert(
        self,
        user_id: str,
        slug: str,
        page_type: str,
        title: str,
        compiled_truth: str = "",
        timeline: str = "",
        frontmatter: Optional[dict] = None,
        origin: str = "distilled",
        source_session_id: Optional[str] = None,
        source_interaction_id: Optional[str] = None,
    ) -> dict:
        async with self._write_lock:
            conn = self._get_conn()
            try:
                now = _now_iso()
                existing = conn.execute(
                    "SELECT id, origin, provenance FROM memories WHERE user_id = ? AND slug = ?",
                    (user_id, slug),
                ).fetchone()

                provenance = []
                if existing:
                    try:
                        provenance = json.loads(existing["provenance"] or "[]")
                    except (TypeError, json.JSONDecodeError):
                        provenance = []
                if not isinstance(provenance, list):
                    provenance = []
                if source_session_id:
                    source = {
                        "session_id": source_session_id,
                        "interaction_id": source_interaction_id or "",
                        "supported_at": now,
                    }
                    provenance = [
                        item for item in provenance
                        if not (isinstance(item, dict)
                                and item.get("session_id") == source_session_id
                                and (item.get("interaction_id") or "") == (source_interaction_id or ""))
                    ]
                    provenance.append(source)

                resolved_origin = str(origin or "distilled")
                if existing and str(existing["origin"] or "") == "deliberate":
                    resolved_origin = "deliberate"

                data = {
                    "user_id": user_id,
                    "slug": slug,
                    "page_type": page_type,
                    "title": title,
                    "compiled_truth": compiled_truth,
                    "timeline": timeline,
                    "frontmatter": json.dumps(frontmatter or {}),
                    "origin": resolved_origin,
                    "provenance": json.dumps(provenance, separators=(",", ":")),
                    "updated_at": now,
                }

                if existing:
                    set_parts = [f"{k} = ?" for k in data]
                    set_vals = list(data.values())
                    conn.execute(
                        f"UPDATE memories SET {', '.join(set_parts)} WHERE id = ?",
                        set_vals + [existing["id"]],
                    )
                    data["id"] = existing["id"]
                else:
                    data["id"] = _uuid()
                    data["created_at"] = now
                    cols = ", ".join(data.keys())
                    placeholders = ", ".join("?" for _ in data)
                    conn.execute(
                        f"INSERT INTO memories ({cols}) VALUES ({placeholders})",
                        list(data.values()),
                    )

                conn.commit()
                memory_id = data["id"]

                # ── Embed on write: chunk + embed + store ──
                embed_sources = []
                if compiled_truth and compiled_truth.strip():
                    embed_sources.append((compiled_truth, "compiled_truth"))
                if timeline and timeline.strip():
                    embed_sources.append((timeline, "timeline"))
                if embed_sources:
                    for text, source in embed_sources:
                        try:
                            await self._embed_and_store_chunks(conn, memory_id, text, source)
                        except Exception as chunk_err:
                            logger.warning("Chunk+embed failed for memory %s (%s): %s", slug, source, chunk_err)
                    # Persist the chunk inserts (the memory row was already
                    # committed above; without this the chunks + embeddings are
                    # discarded when the connection closes).
                    conn.commit()

                logger.debug("Memory upserted: %s (%s)", slug, "updated" if existing else "created")
                return data
            except Exception as e:
                logger.error("Error upserting memory %s: %s", slug, e)
                raise
            finally:
                conn.close()

    async def memory_get(self, user_id: str, slug: str) -> Optional[dict]:
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM memories WHERE user_id = ? AND slug = ?",
                (user_id, slug),
            ).fetchone()
            if row:
                result = dict(row)
                result["frontmatter"] = json.loads(result.get("frontmatter", "{}"))
                return result
            return None
        except Exception as e:
            logger.error("Error getting memory %s: %s", slug, e)
            raise
        finally:
            conn.close()

    async def memory_delete(self, user_id: str, slug: str) -> bool:
        conn = self._get_conn()
        try:
            cursor = conn.execute(
                "DELETE FROM memories WHERE user_id = ? AND slug = ?",
                (user_id, slug),
            )
            conn.commit()
            # CASCADE deletes memory_chunks rows
            deleted = cursor.rowcount > 0
            if deleted:
                logger.debug("Deleted memory: %s", slug)
            return deleted
        except Exception as e:
            logger.error("Error deleting memory %s: %s", slug, e)
            raise
        finally:
            conn.close()

    async def memory_list(
        self, user_id: str, page_type: Optional[str] = None
    ) -> List[dict]:
        conn = self._get_conn()
        try:
            if page_type:
                rows = conn.execute(
                    "SELECT * FROM memories WHERE user_id = ? AND page_type = ? ORDER BY updated_at DESC",
                    (user_id, page_type),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM memories WHERE user_id = ? ORDER BY updated_at DESC",
                    (user_id,),
                ).fetchall()
            result = []
            for r in rows:
                d = dict(r)
                d["frontmatter"] = json.loads(d.get("frontmatter", "{}"))
                result.append(d)
            return result
        except Exception as e:
            logger.error("Error listing memories: %s", e)
            raise
        finally:
            conn.close()

    async def memory_search(
        self, user_id: str, query: str, limit: int = 10, vector: bool = True
    ) -> List[dict]:
        """Hybrid search: FTS5 + vector cosine similarity, merged via RRF.

        When ``vector`` is False, the remote query-embedding call is skipped and
        only the local FTS5 keyword index is consulted. Callers use this fast
        path for short/simple messages where the embedding round-trip costs more
        than it's worth (the porter-stemmed FTS index still matches word
        variants like offer/offered/offers).
        """
        fts_task = asyncio.create_task(self._fts5_search(user_id, query, limit * 3))
        if not vector:
            fts_results = await fts_task
            return (fts_results or [])[:limit]
        vec_task = asyncio.create_task(self._vector_search(user_id, query, limit * 3))

        fts_results, vec_results = await asyncio.gather(fts_task, vec_task, return_exceptions=True)
        if isinstance(fts_results, BaseException):
            logger.warning("FTS5 search failed: %s", fts_results)
            fts_results = []
        if isinstance(vec_results, BaseException):
            logger.warning("Vector search failed: %s", vec_results)
            vec_results = []

        if not vec_results:
            return fts_results[:limit] if fts_results else []
        if not fts_results:
            return vec_results[:limit]

        # ── RRF merge ──
        k = 60
        rrf_scores: Dict[str, float] = {}
        for rank, page in enumerate(fts_results, start=1):
            slug = page.get("slug", "")
            rrf_scores[slug] = rrf_scores.get(slug, 0.0) + 1.0 / (k + rank)
        for rank, page in enumerate(vec_results, start=1):
            slug = page.get("slug", "")
            rrf_scores[slug] = rrf_scores.get(slug, 0.0) + 1.0 / (k + rank)

        all_pages: Dict[str, dict] = {}
        for p in fts_results + vec_results:
            s = p.get("slug", "")
            if s and s not in all_pages:
                all_pages[s] = p

        merged = []
        for slug, score in sorted(rrf_scores.items(), key=lambda x: -x[1]):
            if slug in all_pages:
                entry = dict(all_pages[slug])
                entry["rank"] = round(score, 4)
                merged.append(entry)
        return merged[:limit]

    async def _fts5_search(
        self, user_id: str, query: str, limit: int = 10
    ) -> List[dict]:
        """FTS5-only keyword search (internal, called within hybrid memory_search)."""
        match_expr = _fts5_safe_match_query(query)
        if not match_expr:
            return []
        conn = self._get_conn()
        try:
            rows = conn.execute(
                """SELECT m.*, rank FROM memories_fts fts
                   JOIN memories m ON m.rowid = fts.rowid
                   WHERE memories_fts MATCH ? AND m.user_id = ?
                   ORDER BY rank
                   LIMIT ?""",
                (match_expr, user_id, limit),
            ).fetchall()
            result = []
            for r in rows:
                d = dict(r)
                d["frontmatter"] = json.loads(d.get("frontmatter", "{}"))
                result.append(d)
            return result
        finally:
            conn.close()

    @staticmethod
    def _chunk_text(text: str, max_chars: int = 500) -> List[str]:
        """Split text into ~max_chars chunks, breaking at sentence boundaries."""
        if len(text) <= max_chars:
            return [text]
        chunks = []
        pos = 0
        while pos < len(text):
            end = min(pos + max_chars, len(text))
            if end < len(text):
                best_break = max(
                    text.rfind(". ", pos, end),
                    text.rfind(".\n", pos, end),
                    text.rfind("\n", pos, end),
                    text.rfind(". ", pos, end),
                    text.rfind(" ", pos + max_chars // 2, end),
                )
                if best_break > pos + max_chars // 2:
                    end = best_break + 1
            chunk = text[pos:end].strip()
            if chunk:
                chunks.append(chunk)
            pos = end
        return chunks

    async def _vector_search(
        self, user_id: str, query_text: str, limit: int = 10
    ) -> List[dict]:
        """Search memory pages by embedding cosine similarity."""
        _ensure_np()
        conn = self._get_conn()
        try:
            rows = conn.execute(
                """SELECT mc.memory_id, mc.embedding,
                          m.slug, m.title, m.compiled_truth, m.timeline,
                          m.page_type, m.frontmatter, m.created_at, m.updated_at
                   FROM memory_chunks mc
                   JOIN memories m ON m.id = mc.memory_id
                   WHERE m.user_id = ? AND mc.embedding IS NOT NULL""",
                (user_id,),
            ).fetchall()
            if not rows:
                return []
        finally:
            conn.close()

        # Get query embedding via OpenRouter
        try:
            query_vec_list = await embed_text(query_text)
        except Exception as e:
            logger.warning("Query embed failed, skipping vector search: %s", e)
            return []
        query_vec = np.array(query_vec_list, dtype=np.float32)

        # Build matrix from stored embeddings
        memory_ids = []
        vecs = []
        for r in rows:
            if r["embedding"]:
                vec = np.frombuffer(r["embedding"], dtype=np.float32)
                if vec.shape[0] == embed_dim():
                    memory_ids.append(r["memory_id"])
                    vecs.append(vec)
        if not vecs:
            return []

        matrix = np.stack(vecs)
        norms = np.linalg.norm(matrix, axis=1)
        q_norm = np.linalg.norm(query_vec)
        scores = np.dot(matrix, query_vec) / (norms * q_norm + 1e-12)

        # Group by memory_id, keep best score per page
        page_best: Dict[str, float] = {}
        for i, mid in enumerate(memory_ids):
            s = float(scores[i])
            if mid not in page_best or s > page_best[mid]:
                page_best[mid] = s

        # Build result dicts from rows, matched by memory_id
        page_rows: Dict[str, dict] = {}
        for r in rows:
            mid = r["memory_id"]
            if mid not in page_rows:
                page_rows[mid] = {
                    "slug": r["slug"],
                    "title": r["title"],
                    "compiled_truth": r["compiled_truth"],
                    "timeline": r["timeline"],
                    "page_type": r["page_type"],
                    "frontmatter": json.loads(r["frontmatter"] or "{}"),
                    "created_at": r["created_at"],
                    "updated_at": r["updated_at"],
                }

        result = []
        for mid, score in sorted(page_best.items(), key=lambda x: -x[1]):
            if mid in page_rows:
                entry = dict(page_rows[mid])
                entry["rank"] = round(float(score), 4)
                result.append(entry)
                if len(result) >= limit:
                    break
        return result

    async def _embed_and_store_chunks(
        self, conn: sqlite3.Connection, memory_id: str, text: str, source: str
    ) -> None:
        """Chunk text, embed each chunk via OpenRouter, store in memory_chunks.

        IMPORTANT — embed FIRST, write SECOND. ``conn`` is opened with
        ``isolation_level="IMMEDIATE"``, so the very first write statement
        (the DELETE) acquires SQLite's RESERVED write lock and holds it until the
        caller commits. ``embed_text`` is a slow network round-trip per chunk, so
        embedding *inside* an open write transaction used to pin the single writer
        slot for tens of seconds — long past the 30s ``busy_timeout`` — starving
        every other writer (most visibly the Session Namer's one-row title UPDATE,
        which then failed with "database is locked" and left sessions on their
        fallback name). Do all the network work up front with NO lock held, then
        take the write lock only for the quick DELETE+INSERT burst and release it
        immediately. See plugins/app_functions/session_titler."""
        chunks = self._chunk_text(text, max_chars=500)

        # ── Phase 1: embed every chunk (slow network, NO write lock held) ──
        pending = []  # (chunk_id, index, chunk_text, embedding_blob, token_count)
        for i, chunk in enumerate(chunks):
            if not chunk.strip():
                continue
            embedding_blob = None
            try:
                emb_list = await embed_text(chunk)
                embedding_blob = struct.pack(f"{len(emb_list)}f", *emb_list)
            except Exception as e:
                logger.warning("Chunk embed failed (idx=%d mem=%s): %s", i, memory_id, e)
            pending.append((_uuid(), i, chunk, embedding_blob, len(chunk.split())))

        # ── Phase 2: write everything in one short, committed transaction so the
        # writer slot is held for milliseconds, not the whole embedding pass ──
        conn.execute(
            "DELETE FROM memory_chunks WHERE memory_id = ? AND chunk_source = ?",
            (memory_id, source),
        )
        for chunk_id, i, chunk, embedding_blob, token_count in pending:
            conn.execute(
                """INSERT OR REPLACE INTO memory_chunks
                   (id, memory_id, chunk_index, chunk_text, chunk_source, embedding, token_count)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (chunk_id, memory_id, i, chunk, source, embedding_blob, token_count),
            )
        conn.commit()  # release the write lock now — don't carry it into the next source's embeds
        if pending:
            logger.debug("Stored %d chunks for memory %s (%s)", len(pending), memory_id, source)

    async def reindex_embeddings(
        self, *, tables: tuple = ("memory_chunks", "doc_chunks"), batch: int = 64
    ) -> dict:
        """Re-embed every stored chunk with the CURRENT embedding model/source.

        Run after an admin switches embedding source/model: the old vectors were
        produced by a different model (usually a different width too), so a search
        against them returns nothing useful — they must be regenerated. SQLite
        stores embeddings as raw float32 BLOBs with no fixed width, so nothing in
        the schema changes here; each blob is simply overwritten (the Postgres
        backend overrides this to also resize its fixed-width vector column).

        Mirrors _embed_and_store_chunks' discipline: do the slow embed work with
        NO write lock held, then take the lock only for short write bursts, so a
        long reindex never starves other writers (chat, session titler, …).
        """
        target_dim = embed_dim()
        out: dict = {"model": embed_model_name(), "dim": target_dim, "tables": {}}
        for table in tables:
            conn = self._get_conn()
            try:
                rows = conn.execute(f"SELECT id, chunk_text FROM {table}").fetchall()
            except Exception as e:
                logger.warning("reindex: cannot read %s (skipping): %s", table, e)
                conn.close()
                out["tables"][table] = {"error": str(e)}
                continue
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

            written = failed = 0
            pending: list = []  # (blob_or_None, id)

            async def _flush(items):
                if not items:
                    return
                async with self._write_lock:
                    c = self._get_conn()
                    try:
                        for blob, rid in items:
                            c.execute(
                                f"UPDATE {table} SET embedding = ? WHERE id = ?",
                                (blob, rid),
                            )
                        c.commit()
                    finally:
                        c.close()

            for r in rows:
                txt = (r["chunk_text"] or "")
                blob = None
                if txt.strip():
                    try:
                        emb = await embed_text(txt)
                        blob = struct.pack(f"{len(emb)}f", *emb)
                        written += 1
                    except Exception as e:
                        failed += 1
                        logger.warning("reindex embed failed (%s id=%s): %s", table, r["id"], e)
                pending.append((blob, r["id"]))
                if len(pending) >= batch:
                    await _flush(pending)
                    pending = []
            await _flush(pending)
            out["tables"][table] = {"rows": len(rows), "written": written, "failed": failed}
            logger.info("reindex %s: %d rows, %d embedded, %d failed (dim=%d)",
                        table, len(rows), written, failed, target_dim)
        return out

    # ---- Session Search ----

    async def search_sessions(
        self, user_id: str, query: str, limit: int = 5
    ) -> List[dict]:
        conn = self._get_conn()
        try:
            # Try finding summaries first
            rows = conn.execute(
                """SELECT * FROM session_summaries
                   WHERE user_id = ? AND summary LIKE ?
                   ORDER BY updated_at DESC
                   LIMIT ?""",
                (user_id, f"%{query}%", limit),
            ).fetchall()
            if rows:
                logger.debug(
                    "Found %s session summaries for query %s", len(rows), query
                )
                return [dict(r) for r in rows]

            # Fallback: search interactions and find distinct sessions
            msg_rows = conn.execute(
                """SELECT session_id, content, created_at FROM interactions
                   WHERE content LIKE ?
                   LIMIT ?""",
                (f"%{query}%", limit * 5),
            ).fetchall()

            if not msg_rows:
                return []

            session_ids = list(set(r["session_id"] for r in msg_rows))

            sessions_rows = conn.execute(
                "SELECT id, title, created_at, updated_at FROM sessions WHERE id IN ({})".format(
                    ",".join("?" for _ in session_ids)
                ),
                session_ids,
            ).fetchall()

            results = []
            for s in sessions_rows:
                matched = [r for r in msg_rows if r["session_id"] == s["id"]]
                temp_summary = "Messages found: " + "; ".join(
                    r["content"][:100] for r in matched[:3]
                )
                results.append({
                    "session_id": s["id"],
                    "title": s.get("title", "Untitled"),
                    "summary": temp_summary,
                    "message_count": len(matched),
                    "updated_at": s.get("updated_at", ""),
                })
            logger.debug(
                "Found %s sessions via message search for query %s",
                len(results),
                query,
            )
            return results

        except Exception as e:
            logger.error("Error searching sessions: %s", e)
            raise
        finally:
            conn.close()

    # ---- Skills & Performance Tracking ----

    async def list_skills(self, user_id: str, limit: int = 50) -> List[dict]:
        conn = self._get_conn()
        try:
            rows = conn.execute(
                """SELECT s.*,
                    COALESCE(e.total_exec, 0) AS total_executions,
                    COALESCE(e.success_count, 0) AS successful_executions,
                    e.avg_duration_ms
                   FROM skills s
                   LEFT JOIN (
                       SELECT skill_id,
                           COUNT(*) AS total_exec,
                           SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) AS success_count,
                           AVG(duration_ms) AS avg_duration_ms
                       FROM skill_executions
                       GROUP BY skill_id
                   ) e ON e.skill_id = s.id
                   WHERE s.user_id = ? AND s.is_active = 1
                   ORDER BY e.total_exec DESC
                   LIMIT ?""",
                (user_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.error("Error listing skills: %s", e)
            raise
        finally:
            conn.close()

    async def skill_track_execution(
        self,
        skill_id: str,
        user_id: str,
        session_id: str,
        success: bool,
        duration_ms: int,
        interaction_id: Optional[str] = None,
        error_message: Optional[str] = None,
        input_params: Optional[dict] = None,
        output_summary: Optional[str] = None,
        steps_to_complete: int = 1,
    ) -> str:
        """Record a skill execution. Returns the execution id."""
        async with self._write_lock:
            conn = self._get_conn()
            try:
                eid = _uuid()
                now = _now_iso()
                conn.execute(
                    """INSERT INTO skill_executions
                       (id, skill_id, user_id, session_id, interaction_id,
                        success, duration_ms, steps_to_complete, error_message,
                        input_params, output_summary, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (eid, skill_id, user_id, session_id, interaction_id,
                     1 if success else 0, duration_ms, steps_to_complete, error_message,
                     json.dumps(input_params or {}), output_summary, now),
                )
                conn.commit()
                return eid
            except Exception as e:
                logger.error("Error tracking skill execution: %s", e)
                raise
            finally:
                conn.close()

    async def skill_get_rating(
        self, skill_id: str, user_id: Optional[str] = None
    ) -> dict:
        """
        Compute composite rating for a skill.
        Returns dict with score (0-100), success_rate, efficiency, feedback_score, execution_count.
        """
        conn = self._get_conn()
        try:
            # Get executions
            if user_id:
                rows = conn.execute(
                    "SELECT success, duration_ms, steps_to_complete FROM skill_executions WHERE skill_id = ? AND user_id = ?",
                    (skill_id, user_id),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT success, duration_ms, steps_to_complete FROM skill_executions WHERE skill_id = ?",
                    (skill_id,),
                ).fetchall()

            total = len(rows)
            if total == 0:
                return {
                    "skill_id": skill_id,
                    "score": None,
                    "success_rate": None,
                    "efficiency": None,
                    "feedback_score": None,
                    "execution_count": 0,
                    "avg_duration_ms": None,
                }

            successes = sum(1 for r in rows if r["success"])
            success_rate = successes / total

            avg_steps = sum(r["steps_to_complete"] for r in rows) / total
            avg_duration = sum(r["duration_ms"] for r in rows) / total

            # Efficiency: compare to 1.0 as baseline (1 step is ideal)
            efficiency = max(0, 1.0 - (avg_steps - 1.0) / 5.0) if avg_steps > 0 else 1.0

            # Feedback score
            fb_rows = conn.execute(
                "SELECT feedback_type FROM skill_feedback WHERE skill_id = ?",
                (skill_id,),
            ).fetchall()
            pos = sum(1 for r in fb_rows if r["feedback_type"] == "positive")
            neg = sum(1 for r in fb_rows if r["feedback_type"] == "negative")
            feedback_score = (pos - neg) / (pos + neg + 1) if (pos + neg) > 0 else 0

            # Composite: 40% success rate, 30% efficiency, 20% feedback, 10% recency bonus
            recency = min(1.0, total / 20.0)  # plateaus at 20+ executions
            score = round((
                success_rate * 0.4 +
                efficiency * 0.3 +
                (feedback_score + 1) / 2 * 0.2 +  # normalize feedback to 0-1
                recency * 0.1
            ) * 100, 1)

            return {
                "skill_id": skill_id,
                "score": score,
                "success_rate": round(success_rate * 100, 1),
                "efficiency": round(efficiency * 100, 1),
                "feedback_score": round(feedback_score, 2),
                "execution_count": total,
                "avg_duration_ms": round(avg_duration, 0),
            }
        except Exception as e:
            logger.error("Error getting skill rating: %s", e)
            raise
        finally:
            conn.close()

    async def skill_add_feedback(
        self,
        skill_id: str,
        user_id: str,
        feedback_type: str,
        execution_id: Optional[str] = None,
        message: Optional[str] = None,
    ) -> str:
        """Record user feedback on a skill execution. Returns the feedback id."""
        conn = self._get_conn()
        try:
            fid = _uuid()
            now = _now_iso()
            conn.execute(
                """INSERT INTO skill_feedback
                   (id, skill_id, execution_id, user_id, feedback_type, message, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (fid, skill_id, execution_id, user_id, feedback_type, message, now),
            )
            conn.commit()
            return fid
        except Exception as e:
            logger.error("Error adding skill feedback: %s", e)
            raise
        finally:
            conn.close()

    async def skill_get_id_by_name(self, user_id: str, name: str) -> Optional[str]:
        """Look up a skill's id by name for a user."""
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT id FROM skills WHERE user_id = ? AND name = ? AND is_active = 1 LIMIT 1",
                (user_id, name),
            ).fetchone()
            return row["id"] if row else None
        finally:
            conn.close()
    
    async def get_agent_for_user(self, user_id: str) -> Optional[dict]:
        # Returns any agent the user owns (oldest first). There is no longer
        # a "default agent" concept — callers that need a specific agent
        # should pass agent_id explicitly. This is a fallback for non-web
        # entry points (webhooks, comms processors) that need *some* agent.
        conn = self._get_conn()
        try:
            row = conn.execute(
                """SELECT * FROM agents
                   WHERE EXISTS (SELECT 1 FROM json_each(admin_users) WHERE value = ?)
                   ORDER BY created_at ASC LIMIT 1""", (user_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    async def get_agent_by_id(self, agent_id: str) -> Optional[dict]:
        try:
            from app.db.storage_layout import is_layout_active
            if is_layout_active() and self._schema_plane != "agent":
                from app.db.agent_store import get_agent_store
                authority = await get_agent_store(agent_id).get_authority()
                if authority:
                    if "agent_id" in authority:
                        authority["id"] = authority.pop("agent_id")
                    authority.pop("synced_at", None)
                    return authority
        except Exception as exc:
            logger.debug("Agent authority read-through failed for %s: %s", agent_id, exc)
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM agents WHERE id = ?", (agent_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    # ── On-demand skills: definitions live in the `__skills__` prompt slot ──

    async def get_agent_skills(self, agent_id: str, user_id: Optional[str] = None) -> List[dict]:
        """Return the agent's on-demand skills, parsed from its `__skills__`
        prompt slot. `user_id=None` reads the admin-base definition (the source
        of truth for skills, which are admin-managed/locked)."""
        from app.agent.skills import SKILLS_SLOT_NAME, parse_skills_content
        try:
            slots = await self.resolve_prompts(agent_id, user_id=user_id)
        except Exception:
            return []
        for s in slots:
            if s.get("slot_name") == SKILLS_SLOT_NAME:
                return parse_skills_content(s.get("content"))
        return []

    async def set_agent_skills(
        self, agent_id: str, skills, updated_by: str = "admin"
    ) -> List[dict]:
        """Write the agent's full skills list into its admin-base `__skills__`
        slot (normalized). Returns the normalized list."""
        from app.agent.skills import SKILLS_SLOT_NAME, skills_to_content
        from app.context.md_seeder import normalize_skills
        content = skills_to_content(skills)
        await self.upsert_slot(
            agent_id=agent_id,
            slot_name=SKILLS_SLOT_NAME,
            order_index=200,
            lock=True,
            merge_mode="replace",
            content=content,
            updated_by=updated_by,
        )
        return normalize_skills(skills)

    # ── Session-owned working-tree paths (sessions.metadata) ──

    @staticmethod
    def _session_change_paths_from_meta(meta_raw) -> List[str]:
        try:
            meta = json.loads(meta_raw) if meta_raw else {}
        except (json.JSONDecodeError, TypeError):
            return []
        if not isinstance(meta, dict):
            return []
        paths = meta.get("session_change_paths") or []
        if not isinstance(paths, list):
            return []
        return sorted({
            p.replace("\\", "/").strip().lstrip("/")
            for p in paths
            if isinstance(p, str) and p.strip()
        })

    async def get_session_change_claims(self, session_id: str) -> List[str]:
        if not session_id:
            return []
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT metadata FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            return self._session_change_paths_from_meta(row["metadata"]) if row else []
        finally:
            conn.close()

    async def get_session_change_owners(
        self, paths: List[str]
    ) -> Dict[str, List[str]]:
        wanted = {
            p.replace("\\", "/").strip().lstrip("/")
            for p in paths if isinstance(p, str) and p.strip()
        }
        owners: Dict[str, List[str]] = {path: [] for path in wanted}
        if not wanted:
            return owners
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT id, metadata FROM sessions WHERE status = 'active'"
            ).fetchall()
            for row in rows:
                for path in self._session_change_paths_from_meta(row["metadata"]):
                    if path in wanted:
                        owners[path].append(row["id"])
            return owners
        finally:
            conn.close()

    async def update_session_change_claims(
        self,
        session_id: str,
        *,
        claimed: List[str] | None = None,
        reset: List[str] | None = None,
        cleared: List[str] | None = None,
        released: List[str] | None = None,
    ) -> List[str]:
        """Atomically reset stale owners, clear clean paths, and add new claims."""
        claimed_set = {
            p.replace("\\", "/").strip().lstrip("/")
            for p in (claimed or []) if isinstance(p, str) and p.strip()
        }
        reset_set = {
            p.replace("\\", "/").strip().lstrip("/")
            for p in (reset or []) if isinstance(p, str) and p.strip()
        }
        cleared_set = {
            p.replace("\\", "/").strip().lstrip("/")
            for p in (cleared or []) if isinstance(p, str) and p.strip()
        }
        released_set = {
            p.replace("\\", "/").strip().lstrip("/")
            for p in (released or []) if isinstance(p, str) and p.strip()
        }
        async with self._write_lock:
            conn = self._get_conn()
            try:
                rows = conn.execute(
                    "SELECT id, metadata FROM sessions WHERE status = 'active'"
                ).fetchall()
                current: List[str] = []
                now = _now_iso()
                for row in rows:
                    try:
                        meta = json.loads(row["metadata"]) if row["metadata"] else {}
                        if not isinstance(meta, dict):
                            meta = {}
                    except (json.JSONDecodeError, TypeError):
                        meta = {}
                    paths = set(self._session_change_paths_from_meta(row["metadata"]))
                    before = set(paths)
                    paths -= cleared_set
                    if row["id"] != session_id:
                        paths -= reset_set
                    else:
                        paths -= released_set
                        paths |= claimed_set
                    if paths != before:
                        meta["session_change_paths"] = sorted(paths)
                        conn.execute(
                            "UPDATE sessions SET metadata = ?, updated_at = ? WHERE id = ?",
                            (json.dumps(meta), now, row["id"]),
                        )
                    if row["id"] == session_id:
                        current = sorted(paths)
                conn.commit()
                return current
            finally:
                conn.close()

    async def clear_session_change_claims(
        self, session_id: str, paths: List[str]
    ) -> List[str]:
        return await self.update_session_change_claims(
            session_id, released=paths
        )

    # ── On-demand skills: per-session active list (lives in sessions.metadata) ──
    # "active_skills" = the names of selectable skills the agent has loaded this
    # conversation via load_skill. Drives the loadable-vs-loaded catalog in the
    # system prompt and the active-skill chips in the chat UI.

    @staticmethod
    def _active_skills_from_meta(meta_raw) -> List[str]:
        try:
            meta = json.loads(meta_raw) if meta_raw else {}
        except (json.JSONDecodeError, TypeError):
            return []
        if not isinstance(meta, dict):
            return []
        lst = meta.get("active_skills") or []
        return [n for n in lst if isinstance(n, str)] if isinstance(lst, list) else []

    async def get_session_active_skills(self, session_id: str) -> List[str]:
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT metadata FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            return self._active_skills_from_meta(row["metadata"]) if row else []
        finally:
            conn.close()

    async def set_session_active_skill(
        self, session_id: str, name: str, active: bool
    ) -> List[str]:
        """Add/remove a skill name in the session's active list. Returns new list."""
        async with self._write_lock:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    "SELECT metadata FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()
                if not row:
                    return []
                try:
                    meta = json.loads(row["metadata"]) if row["metadata"] else {}
                    if not isinstance(meta, dict):
                        meta = {}
                except (json.JSONDecodeError, TypeError):
                    meta = {}
                lst = [n for n in (meta.get("active_skills") or []) if isinstance(n, str)]
                if active:
                    if name not in lst:
                        lst.append(name)
                else:
                    lst = [n for n in lst if n != name]
                meta["active_skills"] = lst
                conn.execute(
                    "UPDATE sessions SET metadata = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(meta), _now_iso(), session_id),
                )
                conn.commit()
                return lst
            finally:
                conn.close()

    # ── On-demand tools: per-session active list (lives in sessions.metadata) ──
    # "active_tools" = names of discoverable tools the agent has pulled into
    # context this conversation via load_tool. The loop sends a discoverable
    # tool's full schema only once it appears here. Mirrors active_skills.

    async def get_session_active_tools(self, session_id: str) -> List[str]:
        if not session_id:
            return []
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT metadata FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if not row:
                return []
            try:
                meta = json.loads(row["metadata"]) if row["metadata"] else {}
            except (json.JSONDecodeError, TypeError):
                return []
            if not isinstance(meta, dict):
                return []
            lst = meta.get("active_tools") or []
            return [n for n in lst if isinstance(n, str)] if isinstance(lst, list) else []
        finally:
            conn.close()

    async def set_session_active_tool(
        self, session_id: str, name: str, active: bool
    ) -> List[str]:
        """Add/remove a tool name in the session's active list. Returns new list."""
        async with self._write_lock:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    "SELECT metadata FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()
                if not row:
                    return []
                try:
                    meta = json.loads(row["metadata"]) if row["metadata"] else {}
                    if not isinstance(meta, dict):
                        meta = {}
                except (json.JSONDecodeError, TypeError):
                    meta = {}
                lst = [n for n in (meta.get("active_tools") or []) if isinstance(n, str)]
                if active:
                    if name not in lst:
                        lst.append(name)
                else:
                    lst = [n for n in lst if n != name]
                meta["active_tools"] = lst
                conn.execute(
                    "UPDATE sessions SET metadata = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(meta), _now_iso(), session_id),
                )
                conn.commit()
                return lst
            finally:
                conn.close()

    # ── Execution mode override (sessions.metadata.execution_mode) ──
    # The chat pill (Ask/Plan/Auto) is the user's per-message control, sent with
    # each request. When the AGENT switches mode mid-conversation (the
    # set_execution_mode tool — e.g. flipping Plan→Auto once the user approves a
    # plan), it records the choice here so a cold device / reconnecting UI can
    # read the current mode, and the live UI pill is updated via the broadcast
    # `execution_mode` event. Stored as a plain string alongside active_tools.

    async def get_session_execution_mode(self, session_id: str) -> Optional[str]:
        if not session_id:
            return None
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT metadata FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if not row:
                return None
            try:
                meta = json.loads(row["metadata"]) if row["metadata"] else {}
            except (json.JSONDecodeError, TypeError):
                return None
            if not isinstance(meta, dict):
                return None
            val = meta.get("execution_mode")
            return val if isinstance(val, str) and val else None
        finally:
            conn.close()

    async def get_session_execution_mode_history(self, session_id: str) -> list[str]:
        """Return unique mode ids used by this session, in first-use order."""
        if not session_id:
            return []
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT metadata FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if not row:
                return []
            try:
                meta = json.loads(row["metadata"]) if row["metadata"] else {}
            except (json.JSONDecodeError, TypeError):
                return []
            raw = meta.get("execution_mode_history") if isinstance(meta, dict) else []
            return list(dict.fromkeys(str(x).strip().lower() for x in (raw or [])
                                      if isinstance(x, str) and x.strip()))[:20]
        finally:
            conn.close()

    # File checkout setting in sessions metadata. This session-local escape
    # hatch never mutates an agent contract or another session's policy.

    async def get_session_file_checkout_enabled(self, session_id: str) -> bool:
        if not session_id:
            return True
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT metadata FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if not row:
                return True
            try:
                meta = json.loads(row["metadata"]) if row["metadata"] else {}
            except (json.JSONDecodeError, TypeError):
                return True
            return not (isinstance(meta, dict) and meta.get("file_checkout_enabled") is False)
        finally:
            conn.close()

    async def set_session_file_checkout_enabled(
        self, session_id: str, enabled: bool
    ) -> bool:
        """Persist a session-local checkout override without changing agent gates."""
        if not session_id:
            return bool(enabled)
        async with self._write_lock:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    "SELECT metadata FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()
                if not row:
                    return bool(enabled)
                try:
                    meta = json.loads(row["metadata"]) if row["metadata"] else {}
                    if not isinstance(meta, dict):
                        meta = {}
                except (json.JSONDecodeError, TypeError):
                    meta = {}
                meta["file_checkout_enabled"] = bool(enabled)
                conn.execute(
                    "UPDATE sessions SET metadata = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(meta), _now_iso(), session_id),
                )
                conn.commit()
                return bool(enabled)
            finally:
                conn.close()

    async def set_session_execution_mode(
        self, session_id: str, mode: str, reason: str = ""
    ) -> str:
        """Persist the session's active execution mode (ask/plan/auto). Returns it."""
        async with self._write_lock:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    "SELECT metadata FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()
                if not row:
                    meta = {}
                    conn.execute(
                        """INSERT INTO sessions (id, user_id, metadata, status, created_at, updated_at)
                           VALUES (?, '', '{}', 'active', ?, ?)""",
                        (session_id, _now_iso(), _now_iso()),
                    )
                else:
                    try:
                        meta = json.loads(row["metadata"]) if row["metadata"] else {}
                        if not isinstance(meta, dict):
                            meta = {}
                    except (json.JSONDecodeError, TypeError):
                        meta = {}
                meta["execution_mode"] = mode
                history = meta.get("execution_mode_history")
                history = history if isinstance(history, list) else []
                normalized = str(mode or "").strip().lower()
                if normalized and normalized not in history:
                    history.append(normalized)
                meta["execution_mode_history"] = history[-20:]
                if reason:
                    meta["execution_mode_reason"] = reason
                conn.execute(
                    "UPDATE sessions SET metadata = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(meta), _now_iso(), session_id),
                )
                conn.commit()
                return mode
            finally:
                conn.close()

    # ── Agent component panel: per-session layout state ──
    # The chat-component drawer (todo lists, plan overviews, forms…) keeps its
    # UI state per session — minimized/collapsed, drawer height, frozen width,
    # full-height expanded mode, and the active component tab. Stored as a JSON
    # object under sessions.metadata["component_ui"] (same home as execution
    # mode) so reopening the session restores the exact panel layout.

    async def get_session_component_ui(self, session_id: str) -> Optional[dict]:
        """Return the session's saved agent-panel layout state, or None."""
        if not session_id:
            return None
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT metadata FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if not row:
                return None
            try:
                meta = json.loads(row["metadata"]) if row["metadata"] else {}
            except (json.JSONDecodeError, TypeError):
                return None
            if not isinstance(meta, dict):
                return None
            val = meta.get("component_ui")
            return val if isinstance(val, dict) else None
        finally:
            conn.close()

    async def set_session_component_ui(self, session_id: str, ui: dict) -> dict:
        """Persist the session's agent-panel layout state. Returns it."""
        async with self._write_lock:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    "SELECT metadata FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()
                if not row:
                    return ui
                try:
                    meta = json.loads(row["metadata"]) if row["metadata"] else {}
                    if not isinstance(meta, dict):
                        meta = {}
                except (json.JSONDecodeError, TypeError):
                    meta = {}
                meta["component_ui"] = ui
                conn.execute(
                    "UPDATE sessions SET metadata = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(meta), _now_iso(), session_id),
                )
                conn.commit()
                return ui
            finally:
                conn.close()

    # ── Local Claude Code engine: per-session Claude conversation id ──
    # A "Local Claude Code" agent (metadata.engine="claude_code") maps each
    # WebAgent chat session to ONE Claude CLI conversation, stored in the
    # session's metadata so every turn resumes the same thread (Claude keeps its
    # own memory of it). The working folder is recorded too, for reference.
    # See plugins/engines/claude_code/claude_code.py.

    async def get_session_claude_id(self, session_id: str) -> Optional[str]:
        """Return the Claude CLI conversation id mapped to this chat, or None."""
        if not session_id:
            return None
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT metadata FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if not row:
                return None
            try:
                meta = json.loads(row["metadata"]) if row["metadata"] else {}
            except (json.JSONDecodeError, TypeError):
                return None
            if not isinstance(meta, dict):
                return None
            cid = meta.get("claude_session_id")
            return cid if isinstance(cid, str) and cid else None
        finally:
            conn.close()

    async def set_session_claude_id(
        self, session_id: str, claude_id: str, folder: Optional[str] = None
    ) -> None:
        """Persist the Claude conversation id (and folder) for this chat session."""
        if not session_id or not claude_id:
            return
        async with self._write_lock:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    "SELECT metadata FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()
                if not row:
                    return
                try:
                    meta = json.loads(row["metadata"]) if row["metadata"] else {}
                    if not isinstance(meta, dict):
                        meta = {}
                except (json.JSONDecodeError, TypeError):
                    meta = {}
                meta["claude_session_id"] = claude_id
                if folder:
                    meta["claude_folder"] = folder
                conn.execute(
                    "UPDATE sessions SET metadata = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(meta), _now_iso(), session_id),
                )
                conn.commit()
            finally:
                conn.close()

    # ── Local Codex engine: per-session Codex thread id ─────────────────────
    # Kept alongside the Claude mapping above: `codex exec resume <id>` lets a
    # WebAgent chat keep one Codex conversation across headless turns.
    async def get_session_codex_id(self, session_id: str) -> Optional[str]:
        if not session_id:
            return None
        conn = self._get_conn()
        try:
            row = conn.execute("SELECT metadata FROM sessions WHERE id = ?", (session_id,)).fetchone()
            try:
                meta = json.loads(row["metadata"]) if row and row["metadata"] else {}
            except (json.JSONDecodeError, TypeError):
                meta = {}
            value = meta.get("codex_thread_id") if isinstance(meta, dict) else None
            return value if isinstance(value, str) and value else None
        finally:
            conn.close()

    async def set_session_codex_id(self, session_id: str, thread_id: str) -> None:
        if not session_id or not thread_id:
            return
        async with self._write_lock:
            conn = self._get_conn()
            try:
                row = conn.execute("SELECT metadata FROM sessions WHERE id = ?", (session_id,)).fetchone()
                if not row:
                    return
                try:
                    meta = json.loads(row["metadata"]) if row["metadata"] else {}
                    if not isinstance(meta, dict): meta = {}
                except (json.JSONDecodeError, TypeError):
                    meta = {}
                meta["codex_thread_id"] = thread_id
                conn.execute("UPDATE sessions SET metadata = ?, updated_at = ? WHERE id = ?",
                             (json.dumps(meta), _now_iso(), session_id))
                conn.commit()
            finally:
                conn.close()

    # ── Provider-neutral Responses API continuation state ──────────────────
    # This is an optimisation hint, never the transcript authority.  The agent
    # loop validates the identity + local-history fingerprint before resuming;
    # otherwise it discards this state and reconstructs from interactions.

    async def get_session_response_chain(self, session_id: str) -> Optional[dict]:
        if not session_id:
            return None
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT metadata FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            try:
                meta = json.loads(row["metadata"]) if row and row["metadata"] else {}
            except (json.JSONDecodeError, TypeError):
                meta = {}
            value = meta.get("response_chain") if isinstance(meta, dict) else None
            return dict(value) if isinstance(value, dict) else None
        finally:
            conn.close()

    async def set_session_response_chain(
        self, session_id: str, state: Optional[dict]
    ) -> None:
        if not session_id:
            return
        async with self._write_lock:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    "SELECT metadata FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()
                if not row:
                    return
                try:
                    meta = json.loads(row["metadata"]) if row["metadata"] else {}
                    if not isinstance(meta, dict):
                        meta = {}
                except (json.JSONDecodeError, TypeError):
                    meta = {}
                if state:
                    # Keep the durable hint intentionally small and non-secret.
                    meta["response_chain"] = {
                        key: state[key]
                        for key in (
                            "transport", "family", "identity", "model",
                            "previous_response_id", "history_hash", "updated_at",
                        )
                        if key in state
                    }
                else:
                    meta.pop("response_chain", None)
                conn.execute(
                    "UPDATE sessions SET metadata = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(meta), _now_iso(), session_id),
                )
                conn.commit()
            finally:
                conn.close()

    # ── Local Claude Code engine: one-shot "compact & restart" (/compact) ──
    # A Claude Code agent's memory lives inside the `claude` CLI's own session,
    # resumed each turn — it only ever grows. The /compact command arms a one-shot
    # reseed: WebAgent's Context Control folds this chat into a compact recap, we
    # stash it here AND drop the stored Claude id, so the engine's next turn starts
    # a BRAND-NEW Claude session seeded with the recap instead of resuming the old
    # bloated thread. Consumed (cleared) by the engine on that next turn.
    # See plugins/engines/claude_code/claude_code.py + the /compact handler.

    async def set_session_claude_reseed(self, session_id: str, seed: str) -> None:
        """Arm a one-shot compact-and-restart: store the recap, forget the old id."""
        if not session_id:
            return
        async with self._write_lock:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    "SELECT metadata FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()
                if not row:
                    return
                try:
                    meta = json.loads(row["metadata"]) if row["metadata"] else {}
                    if not isinstance(meta, dict):
                        meta = {}
                except (json.JSONDecodeError, TypeError):
                    meta = {}
                meta["claude_reseed_context"] = seed or ""
                # Forget the old Claude thread so the next turn does NOT --resume it.
                meta.pop("claude_session_id", None)
                conn.execute(
                    "UPDATE sessions SET metadata = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(meta), _now_iso(), session_id),
                )
                conn.commit()
            finally:
                conn.close()

    async def get_session_claude_reseed(self, session_id: str) -> Optional[str]:
        """Return the pending compact-and-restart recap for this session, or None."""
        if not session_id:
            return None
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT metadata FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if not row:
                return None
            try:
                meta = json.loads(row["metadata"]) if row["metadata"] else {}
            except (json.JSONDecodeError, TypeError):
                return None
            if not isinstance(meta, dict):
                return None
            seed = meta.get("claude_reseed_context")
            return seed if isinstance(seed, str) and seed else None
        finally:
            conn.close()

    async def clear_session_claude_reseed(self, session_id: str) -> None:
        """Drop a consumed recap once the fresh Claude session has been seeded."""
        if not session_id:
            return
        async with self._write_lock:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    "SELECT metadata FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()
                if not row:
                    return
                try:
                    meta = json.loads(row["metadata"]) if row["metadata"] else {}
                    if not isinstance(meta, dict):
                        meta = {}
                except (json.JSONDecodeError, TypeError):
                    meta = {}
                if "claude_reseed_context" in meta:
                    meta.pop("claude_reseed_context", None)
                    conn.execute(
                        "UPDATE sessions SET metadata = ?, updated_at = ? WHERE id = ?",
                        (json.dumps(meta), _now_iso(), session_id),
                    )
                    conn.commit()
            finally:
                conn.close()

    # ── Codex engine: one-shot "compact & restart" (/compact) ──────────────────
    # Mirror of the Claude reseed above for the Codex CLI: /compact folds this
    # chat into a recap, we stash it in the session metadata AND drop the stored
    # codex thread id, so the engine's next turn starts a BRAND-NEW Codex thread
    # seeded with the recap instead of resuming the old (only-ever-growing) one.
    # Consumed (cleared) by the engine on that next turn once the fresh thread
    # has actually produced an id — a failed restart retries the recap.

    async def set_session_codex_reseed(self, session_id: str, seed: str) -> None:
        """Arm a one-shot compact-and-restart: store the recap, forget the old thread."""
        if not session_id:
            return
        async with self._write_lock:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    "SELECT metadata FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()
                if not row:
                    return
                try:
                    meta = json.loads(row["metadata"]) if row["metadata"] else {}
                    if not isinstance(meta, dict):
                        meta = {}
                except (json.JSONDecodeError, TypeError):
                    meta = {}
                meta["codex_reseed_context"] = seed or ""
                # Forget the old Codex thread so the next turn does NOT resume it.
                meta.pop("codex_thread_id", None)
                conn.execute(
                    "UPDATE sessions SET metadata = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(meta), _now_iso(), session_id),
                )
                conn.commit()
            finally:
                conn.close()

    async def get_session_codex_reseed(self, session_id: str) -> Optional[str]:
        """Return the pending compact-and-restart recap for this session, or None."""
        if not session_id:
            return None
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT metadata FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if not row:
                return None
            try:
                meta = json.loads(row["metadata"]) if row["metadata"] else {}
            except (json.JSONDecodeError, TypeError):
                return None
            if not isinstance(meta, dict):
                return None
            seed = meta.get("codex_reseed_context")
            return seed if isinstance(seed, str) and seed else None
        finally:
            conn.close()

    async def clear_session_codex_reseed(self, session_id: str) -> None:
        """Drop a consumed recap once the fresh Codex thread has been seeded."""
        if not session_id:
            return
        async with self._write_lock:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    "SELECT metadata FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()
                if not row:
                    return
                try:
                    meta = json.loads(row["metadata"]) if row["metadata"] else {}
                    if not isinstance(meta, dict):
                        meta = {}
                except (json.JSONDecodeError, TypeError):
                    meta = {}
                if "codex_reseed_context" in meta:
                    meta.pop("codex_reseed_context", None)
                    conn.execute(
                        "UPDATE sessions SET metadata = ?, updated_at = ? WHERE id = ?",
                        (json.dumps(meta), _now_iso(), session_id),
                    )
                    conn.commit()
            finally:
                conn.close()

    # ── On-demand abilities: per-session active list (sessions.metadata) ──
    # "active_abilities" = ids of discoverable abilities the agent has pulled in
    # this conversation via load_ability. While listed, the ability's tools +
    # bundled skill flow into the prompt as if it were visible. Mirrors
    # active_tools / active_skills.

    async def get_session_active_abilities(self, session_id: str) -> List[str]:
        if not session_id:
            return []
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT metadata FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if not row:
                return []
            try:
                meta = json.loads(row["metadata"]) if row["metadata"] else {}
            except (json.JSONDecodeError, TypeError):
                return []
            if not isinstance(meta, dict):
                return []
            lst = meta.get("active_abilities") or []
            return [n for n in lst if isinstance(n, str)] if isinstance(lst, list) else []
        finally:
            conn.close()

    async def set_session_active_ability(
        self, session_id: str, ability_id: str, active: bool
    ) -> List[str]:
        """Add/remove an ability id in the session's active list. Returns new list."""
        async with self._write_lock:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    "SELECT metadata FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()
                if not row:
                    return []
                try:
                    meta = json.loads(row["metadata"]) if row["metadata"] else {}
                    if not isinstance(meta, dict):
                        meta = {}
                except (json.JSONDecodeError, TypeError):
                    meta = {}
                lst = [n for n in (meta.get("active_abilities") or []) if isinstance(n, str)]
                if active:
                    if ability_id not in lst:
                        lst.append(ability_id)
                else:
                    lst = [n for n in lst if n != ability_id]
                meta["active_abilities"] = lst
                conn.execute(
                    "UPDATE sessions SET metadata = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(meta), _now_iso(), session_id),
                )
                conn.commit()
                return lst
            finally:
                conn.close()

    async def get_session_suppressed_abilities(self, session_id: str) -> List[str]:
        """Abilities the user turned OFF for this session from the chat Abilities
        panel — withheld even when the agent's config makes them ``visible``."""
        if not session_id:
            return []
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT metadata FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if not row:
                return []
            try:
                meta = json.loads(row["metadata"]) if row["metadata"] else {}
            except (json.JSONDecodeError, TypeError):
                return []
            if not isinstance(meta, dict):
                return []
            lst = meta.get("suppressed_abilities") or []
            return [n for n in lst if isinstance(n, str)] if isinstance(lst, list) else []
        finally:
            conn.close()

    async def set_session_suppressed_ability(
        self, session_id: str, ability_id: str, suppressed: bool
    ) -> List[str]:
        """Add/remove an ability id in the session's suppressed list. Returns new list."""
        async with self._write_lock:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    "SELECT metadata FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()
                if not row:
                    return []
                try:
                    meta = json.loads(row["metadata"]) if row["metadata"] else {}
                    if not isinstance(meta, dict):
                        meta = {}
                except (json.JSONDecodeError, TypeError):
                    meta = {}
                lst = [n for n in (meta.get("suppressed_abilities") or []) if isinstance(n, str)]
                if suppressed:
                    if ability_id not in lst:
                        lst.append(ability_id)
                else:
                    lst = [n for n in lst if n != ability_id]
                meta["suppressed_abilities"] = lst
                conn.execute(
                    "UPDATE sessions SET metadata = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(meta), _now_iso(), session_id),
                )
                conn.commit()
                return lst
            finally:
                conn.close()

    # ── Session lifecycle: status check + cleanup for recycle/delete safety ──

    async def get_session_status(self, session_id: str) -> Optional[str]:
        """Return the session's status field (e.g. 'active', 'recycled') or None if not found."""
        if not session_id:
            return None
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT status FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            return row["status"] if row else None
        finally:
            conn.close()

    async def clear_session_active_state(self, session_id: str) -> None:
        """Clear all active state (tools, skills, abilities) for a session.
        Called when a session is recycled to prevent any further loop activation."""
        if not session_id:
            return
        async with self._write_lock:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    "SELECT metadata FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()
                if not row:
                    return
                try:
                    meta = json.loads(row["metadata"]) if row["metadata"] else {}
                    if not isinstance(meta, dict):
                        meta = {}
                except (json.JSONDecodeError, TypeError):
                    meta = {}
                # Clear all active state fields that could trigger the loop
                meta.pop("active_tools", None)
                meta.pop("active_skills", None)
                meta.pop("active_abilities", None)
                meta.pop("suppressed_abilities", None)
                meta.pop("execution_mode", None)
                meta.pop("execution_mode_history", None)
                conn.execute(
                    "UPDATE sessions SET metadata = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(meta), _now_iso(), session_id),
                )
                conn.commit()
            finally:
                conn.close()

    async def is_session_dead(self, session_id: str) -> bool:
        """True if the session is permanently deleted (no row exists) or recycled.
        Used as a safety check before running any loop or automation."""
        if not session_id:
            return True
        status = await self.get_session_status(session_id)
        # Dead if: no row found (hard-deleted), or status is 'recycled' (soft-deleted)
        return status is None or status == "recycled"

    # ── Per-session model override (lives in sessions.metadata["llm_config"]) ──
    # Lets a single chat run on a model different from the agent's default — the
    # footer model picker writes here so each session remembers its own model.
    # Same shape as an agent override: {use_default: False, model: "<id>"}.
    # Resolution order at run time is app-default → agent → session.

    async def get_session_llm_override(self, session_id: str) -> Optional[dict]:
        """Return the session's stored llm_config dict, or None if unset."""
        if not session_id:
            return None
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT metadata FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if not row or not row["metadata"]:
                return None
            try:
                meta = json.loads(row["metadata"])
            except (json.JSONDecodeError, TypeError):
                return None
            cfg = meta.get("llm_config") if isinstance(meta, dict) else None
            return cfg if isinstance(cfg, dict) else None
        finally:
            conn.close()

    async def get_session_context_override(self, session_id: str) -> Optional[dict]:
        """Return this session's per-chat Context-Control override, or None if unset.

        The footer compaction panel saves ``compact_target_tokens`` and
        ``verbatim_tail_tokens`` under ``metadata['context_override']`` so ONE chat can
        tune its own compaction without touching the agent default — mirroring the
        per-session model override. Only the keys the user changed are present."""
        if not session_id:
            return None
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT metadata FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if not row or not row["metadata"]:
                return None
            try:
                meta = json.loads(row["metadata"])
            except (json.JSONDecodeError, TypeError):
                return None
            ov = meta.get("context_override") if isinstance(meta, dict) else None
            return ov if isinstance(ov, dict) and ov else None
        finally:
            conn.close()

    async def set_session_context_override(
        self, session_id: str, updates: Dict[str, Any]
    ) -> Optional[dict]:
        """Merge per-chat Context-Control overrides into this session's metadata.

        ``updates`` maps the absolute token-budget keys to an integer to set, or
        ``None`` to clear that one key. Legacy fraction keys remain readable. When no override
        keys remain the whole ``context_override`` block is removed and the chat
        falls back to the agent's Context Control settings. Takes effect on the next
        turn (the gauge + the next automatic/manual compaction re-resolve settings).
        Returns the new override dict (or None when cleared)."""
        if not session_id:
            return None
        updates = updates or {}
        async with self._write_lock:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    "SELECT metadata FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()
                if not row:
                    return None
                try:
                    meta = json.loads(row["metadata"]) if row["metadata"] else {}
                    if not isinstance(meta, dict):
                        meta = {}
                except (json.JSONDecodeError, TypeError):
                    meta = {}
                ov = meta.get("context_override")
                ov = dict(ov) if isinstance(ov, dict) else {}
                for key, val in updates.items():
                    if val is None:
                        ov.pop(key, None)
                    else:
                        ov[key] = val
                if ov:
                    meta["context_override"] = ov
                    result = ov
                else:
                    meta.pop("context_override", None)
                    result = None
                conn.execute(
                    "UPDATE sessions SET metadata = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(meta), _now_iso(), session_id),
                )
                conn.commit()
                return result
            finally:
                conn.close()

    async def set_session_llm_override(
        self, session_id: str, selection: Optional[dict] = None
    ) -> Optional[dict]:
        """Set (or clear) the session's model override.

        ``selection`` is one of:
          - ``None`` → clear the override entirely (fall back to agent/app default);
          - a slot-selection dict ``{"type": "role", "role": "standard"}`` or
            ``{"type": "custom", "position": 2}`` (the chat footer model picker);
          - a concrete model ``{"type": "model", "model": "<model id>"}`` or a bare
            model-id STRING (the Model Switcher ability's set_model /
            use_premium_model contract) — stored as ``{use_default: False,
            model: <id>}``, the concrete-model form the runtime resolver
            (``_session_llm_override`` in app/admin/settings.py) already supports.

        Stores the override in the session's metadata. Returns the new cfg (or
        None when cleared). A bare string that is empty/blank falls through to
        the clear path.

        If the session row does not exist yet, it is created on the fly so the
        footer model picker works before the first message is sent — model
        selections are per-session and must never bleed into agent metadata."""
        if not session_id:
            return None
        async with self._write_lock:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    "SELECT metadata FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()
                if not row:
                    # Session row does not exist yet — create a minimal row so the
                    # model override can be stored per-session immediately (the
                    # footer picker may be used before the first message is sent).
                    conn.execute(
                        """INSERT INTO sessions (id, user_id, metadata, status, created_at, updated_at)
                           VALUES (?, '', '{}', 'active', ?, ?)""",
                        (session_id, _now_iso(), _now_iso()),
                    )
                    conn.commit()
                    meta = {}
                else:
                    try:
                        meta = json.loads(row["metadata"]) if row["metadata"] else {}
                        if not isinstance(meta, dict):
                            meta = {}
                    except (json.JSONDecodeError, TypeError):
                        meta = {}
                # Preserve any per-slot reasoning-effort map across a model change
                # so each slot keeps its own remembered effort (the footer picker
                # stores an effort per row).
                prev = meta.get("llm_config") if isinstance(meta.get("llm_config"), dict) else {}
                effort_map = prev.get("model_effort") if isinstance(prev.get("model_effort"), dict) else {}
                if isinstance(selection, str):
                    # Bare model-id string — the Model Switcher ability's contract.
                    model_id = selection.strip()
                    if model_id:
                        cfg = {"use_default": False, "model": model_id}
                        if effort_map:
                            cfg["model_effort"] = effort_map
                        meta["llm_config"] = cfg
                    else:
                        # Empty/blank string → clear, same as None.
                        cfg = None
                        meta.pop("llm_config", None)
                elif selection:
                    sel_type = selection.get("type", "")
                    if sel_type == "model":
                        # Explicit dict form of the concrete-model override.
                        model_id = (selection.get("model") or "").strip()
                        cfg = {"use_default": False, "model": model_id}
                        if effort_map:
                            cfg["model_effort"] = effort_map
                        # Stash-restore: carry a prior USER footer-picker slot
                        # selection so the agent's temporary upgrade can be reset
                        # back to it (the Model Switcher ability passes
                        # ``_prior_slot``). Inert to the runtime resolver.
                        prior_slot = selection.get("_prior_slot")
                        if isinstance(prior_slot, dict) and prior_slot.get("selection_type"):
                            cfg["_prior_slot"] = prior_slot
                        meta["llm_config"] = cfg
                    else:
                        cfg = {"use_default": False, "selection_type": sel_type}
                        if sel_type == "role":
                            cfg["role"] = selection.get("role", "")
                        elif sel_type == "custom":
                            cfg["custom_position"] = selection.get("position", 0)
                            cfg["entry_id"] = selection.get("entry_id", "")
                        if effort_map:
                            cfg["model_effort"] = effort_map
                        meta["llm_config"] = cfg
                elif effort_map:
                    cfg = {"model_effort": effort_map}
                    meta["llm_config"] = cfg
                else:
                    cfg = None
                    meta.pop("llm_config", None)
                conn.execute(
                    "UPDATE sessions SET metadata = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(meta), _now_iso(), session_id),
                )
                conn.commit()
                return cfg
            finally:
                conn.close()

    async def set_session_model_effort(
        self, session_id: str, slot_ref: str, effort: Optional[str]
    ) -> Optional[dict]:
        """Set (or clear) the per-SLOT reasoning-effort level for THIS session.

        Stores a ``{slot_ref: level}`` map under ``metadata['llm_config']
        ['model_effort']`` so each slot (e.g. 'role:standard', 'custom:2') remembers
        its own effort level. A falsy/``"default"`` level removes that entry.
        Leaves the picked slot untouched.
        Returns the new llm_config dict (or None when the override is now empty)."""
        if not session_id or not slot_ref:
            return None
        level = (effort or "").strip().lower()
        async with self._write_lock:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    "SELECT metadata FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()
                if not row:
                    return None
                try:
                    meta = json.loads(row["metadata"]) if row["metadata"] else {}
                    if not isinstance(meta, dict):
                        meta = {}
                except (json.JSONDecodeError, TypeError):
                    meta = {}
                cfg = meta.get("llm_config") if isinstance(meta.get("llm_config"), dict) else {}
                effort_map = dict(cfg.get("model_effort") or {}) if isinstance(cfg.get("model_effort"), dict) else {}
                if level and level != "default":
                    effort_map[slot_ref] = level
                else:
                    effort_map.pop(slot_ref, None)
                if effort_map:
                    cfg = dict(cfg)
                    cfg["model_effort"] = effort_map
                else:
                    cfg = dict(cfg)
                    cfg.pop("model_effort", None)
                # Drop the override entirely if nothing meaningful is left.
                if not cfg.get("model") and not cfg.get("selection_type") and not cfg.get("model_effort"):
                    meta.pop("llm_config", None)
                    cfg = None
                else:
                    meta["llm_config"] = cfg
                conn.execute(
                    "UPDATE sessions SET metadata = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(meta), _now_iso(), session_id),
                )
                conn.commit()
                return cfg
            finally:
                conn.close()

    async def clear_session_llm_override(self, session_id: str) -> None:
        """Remove the session's ENTIRE llm_config override — both the picked model
        AND every per-model reasoning-effort entry — returning the chat to the
        agent's default model at default effort. The clean "task done" revert used
        by the Model Switcher ability (set_session_llm_override alone preserves the
        effort map, by design, so a model swap keeps each model's level)."""
        if not session_id:
            return
        async with self._write_lock:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    "SELECT metadata FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()
                if not row:
                    return
                try:
                    meta = json.loads(row["metadata"]) if row["metadata"] else {}
                    if not isinstance(meta, dict):
                        meta = {}
                except (json.JSONDecodeError, TypeError):
                    meta = {}
                if "llm_config" not in meta:
                    return
                meta.pop("llm_config", None)
                conn.execute(
                    "UPDATE sessions SET metadata = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(meta), _now_iso(), session_id),
                )
                conn.commit()
            finally:
                conn.close()

    async def clear_session_agent_model_override(self, session_id: str) -> None:
        """New-user-message reset for the Model Switcher ability.

        Strips ONLY the AGENT-driven parts of the session's model override:
          - a concrete-model pick (``{use_default: False, model: <id>}``) — the
            form the Model Switcher's set_model / use_premium_model writes;
          - reasoning-effort entries keyed by a BARE MODEL ID — written by
            set_effort (whether or not a model switch also happened).

        The user's own footer-picker slot selection (``selection_type`` + role /
        custom_position) and its per-slot effort keys (``role:*`` / ``custom:*``)
        survive — a model the USER chose persists across messages, while the
        agent's temporary upgrade (or effort raise) never leaks into the user's
        next turn. No-op when there is nothing agent-driven to clear."""
        if not session_id:
            return
        async with self._write_lock:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    "SELECT metadata FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()
                if not row:
                    return
                try:
                    meta = json.loads(row["metadata"]) if row["metadata"] else {}
                    if not isinstance(meta, dict):
                        meta = {}
                except (json.JSONDecodeError, TypeError):
                    meta = {}
                cfg = meta.get("llm_config")
                if not isinstance(cfg, dict):
                    return
                # Copy, then strip the agent-driven keys only.
                new_cfg = dict(cfg)
                if "model" in new_cfg:
                    new_cfg.pop("model", None)
                # Restore the user's stashed footer-picker slot selection, if the
                # agent's upgrade stashed one (see set_model / use_premium_model
                # in the Model Switcher ability — SISTER-SYNC stash-restore).
                prior_slot = new_cfg.pop("_prior_slot", None)
                if (not new_cfg.get("selection_type")
                        and isinstance(prior_slot, dict)
                        and prior_slot.get("selection_type")):
                    new_cfg["selection_type"] = prior_slot["selection_type"]
                    if prior_slot.get("role"):
                        new_cfg["role"] = prior_slot["role"]
                    elif "role" in new_cfg:
                        new_cfg.pop("role", None)
                    if prior_slot.get("custom_position") is not None:
                        new_cfg["custom_position"] = prior_slot["custom_position"]
                    elif "custom_position" in new_cfg:
                        new_cfg.pop("custom_position", None)
                    if prior_slot.get("entry_id"):
                        new_cfg["entry_id"] = prior_slot["entry_id"]
                    elif "entry_id" in new_cfg:
                        new_cfg.pop("entry_id", None)
                    new_cfg["use_default"] = False
                effort_map = new_cfg.get("model_effort")
                if isinstance(effort_map, dict):
                    kept = {k: v for k, v in effort_map.items()
                            if (k.startswith("role:") or k.startswith("custom:")
                                or k.startswith("entry:"))}
                    if kept:
                        new_cfg["model_effort"] = kept
                    else:
                        new_cfg.pop("model_effort", None)
                # A concrete override's use_default:False marker is meaningless
                # once its model is gone — and must not keep the override active.
                if not new_cfg.get("selection_type"):
                    new_cfg.pop("use_default", None)
                # Nothing meaningful left → clear the override entirely.
                if not new_cfg.get("selection_type") and not new_cfg.get("model_effort"):
                    new_cfg = None
                if new_cfg is None:
                    if "llm_config" not in meta:
                        return
                    meta.pop("llm_config", None)
                elif new_cfg == cfg:
                    return  # nothing agent-driven to clear
                else:
                    meta["llm_config"] = new_cfg
                conn.execute(
                    "UPDATE sessions SET metadata = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(meta), _now_iso(), session_id),
                )
                conn.commit()
            finally:
                conn.close()

    # ── Terminal tunnel binding (lives in sessions.metadata under "tunnel") ──
    # When set, this chat session is bound to a live terminal: the user's chat
    # messages are keystrokes for that program and its output streams back into
    # chat (the agent steps aside). See app/agent/terminal_tunnel.py.

    async def get_session_tunnel(self, session_id: str) -> Dict[str, Any]:
        """Return the tunnel binding dict for a session ({} if none)."""
        if not session_id:
            return {}
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT metadata FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if not row:
                return {}
            try:
                meta = json.loads(row["metadata"]) if row["metadata"] else {}
            except (json.JSONDecodeError, TypeError):
                return {}
            if not isinstance(meta, dict):
                return {}
            tun = meta.get("tunnel") or {}
            return tun if isinstance(tun, dict) else {}
        finally:
            conn.close()

    async def set_session_tunnel(
        self, session_id: str, config: Optional[Dict[str, Any]]
    ) -> None:
        """Store (or clear, if config is None) the session's tunnel binding under
        sessions.metadata["tunnel"]."""
        async with self._write_lock:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    "SELECT metadata FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()
                if not row:
                    return
                try:
                    meta = json.loads(row["metadata"]) if row["metadata"] else {}
                    if not isinstance(meta, dict):
                        meta = {}
                except (json.JSONDecodeError, TypeError):
                    meta = {}
                if config:
                    meta["tunnel"] = config
                else:
                    meta.pop("tunnel", None)
                conn.execute(
                    "UPDATE sessions SET metadata = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(meta), _now_iso(), session_id),
                )
                conn.commit()
            finally:
                conn.close()

    @turn_cached
    async def _agent_metadata(self, agent_id: str) -> dict:
        """Parsed ``agents.metadata`` for one agent, read ONCE per turn.

        The many per-agent getters below (tool / ability / skill modes, the
        discovery default, ability access) each used to run their OWN
        ``SELECT metadata FROM agents`` — so a single chat turn re-read the same
        row 5-9× (each round-trip ~150ms on a remote DB). They now all parse from
        this one cached snapshot, collapsing those to a single read. Returns ``{}``
        for a missing / blank / corrupt row. Turn-invariant: agent config is not
        rewritten mid-chat-turn, so every getter in a turn safely sees the same
        snapshot (writes go through the ``set_*`` methods under the write lock and
        are not part of the chat read path)."""
        if not agent_id:
            return {}
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT metadata FROM agents WHERE id = ?", (agent_id,)
            ).fetchone()
            if not row:
                return {}
            try:
                meta = json.loads(row["metadata"]) if row["metadata"] else {}
            except (json.JSONDecodeError, TypeError):
                return {}
            return meta if isinstance(meta, dict) else {}
        finally:
            conn.close()

    # ── Per-agent tool modes (lives in agents.metadata under "tool_modes") ──
    # Flat {tool_name: "visible" | "discoverable"} map. Absent tools resolve to
    # the "visible" default (see app/tools/tool_modes.resolve_mode). Legacy
    # "always" values are normalized to "visible" on read. Core tools are never
    # stored here — they are always sent and not toggleable.

    @turn_cached
    async def get_agent_tool_modes(self, agent_id: str) -> Dict[str, str]:
        if not agent_id:
            return {}
        from app.tools.tool_modes import AGENT_TOOL_MODES_KEY, normalize_visibility
        meta = await self._agent_metadata(agent_id)
        modes = (meta or {}).get(AGENT_TOOL_MODES_KEY) or {}
        if not isinstance(modes, dict):
            return {}
        out: Dict[str, str] = {}
        for k, v in modes.items():
            nv = normalize_visibility(v)
            if isinstance(k, str) and nv:
                out[k] = nv
        return out

    async def set_agent_tool_mode(
        self, agent_id: str, tool_name: str, mode: str
    ) -> Dict[str, str]:
        """Set one tool's mode for an agent. mode in {"visible","discoverable"}
        (legacy "always" accepted → "visible"); anything else clears the explicit
        setting (back to default). Returns the updated tool_modes map."""
        from app.tools.tool_modes import AGENT_TOOL_MODES_KEY, normalize_visibility
        nmode = normalize_visibility(mode)
        async with self._write_lock:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    "SELECT metadata FROM agents WHERE id = ?", (agent_id,)
                ).fetchone()
                if not row:
                    return {}
                try:
                    meta = json.loads(row["metadata"]) if row["metadata"] else {}
                    if not isinstance(meta, dict):
                        meta = {}
                except (json.JSONDecodeError, TypeError):
                    meta = {}
                modes = meta.get(AGENT_TOOL_MODES_KEY)
                if not isinstance(modes, dict):
                    modes = {}
                if nmode:
                    modes[tool_name] = nmode
                else:
                    modes.pop(tool_name, None)
                meta[AGENT_TOOL_MODES_KEY] = modes
                conn.execute(
                    "UPDATE agents SET metadata = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(meta), _now_iso(), agent_id),
                )
                conn.commit()
                return modes
            finally:
                conn.close()

    async def seed_agent_tool_modes(
        self, agent_id: str, tool_names: List[str], mode: str
    ) -> Dict[str, str]:
        """Set `mode` for each tool name that has NO explicit setting yet (leaves
        existing choices untouched). Used to default a newly-enabled ability's
        tools to "discoverable". Returns the updated map."""
        from app.tools.tool_modes import AGENT_TOOL_MODES_KEY, normalize_visibility
        mode = normalize_visibility(mode)
        if not tool_names or not mode:
            return await self.get_agent_tool_modes(agent_id)
        async with self._write_lock:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    "SELECT metadata FROM agents WHERE id = ?", (agent_id,)
                ).fetchone()
                if not row:
                    return {}
                try:
                    meta = json.loads(row["metadata"]) if row["metadata"] else {}
                    if not isinstance(meta, dict):
                        meta = {}
                except (json.JSONDecodeError, TypeError):
                    meta = {}
                modes = meta.get(AGENT_TOOL_MODES_KEY)
                if not isinstance(modes, dict):
                    modes = {}
                changed = False
                for n in tool_names:
                    if isinstance(n, str) and n not in modes:
                        modes[n] = mode
                        changed = True
                if changed:
                    meta[AGENT_TOOL_MODES_KEY] = modes
                    conn.execute(
                        "UPDATE agents SET metadata = ?, updated_at = ? WHERE id = ?",
                        (json.dumps(meta), _now_iso(), agent_id),
                    )
                    conn.commit()
                return modes
            finally:
                conn.close()

    # ── Per-agent ability visibility (agents.metadata under "ability_modes") ──
    # Flat {ability_id: "visible" | "discoverable"} map. Absent abilities resolve
    # to the "visible" default (see tool_modes.resolve_ability_mode). Discovery is
    # tuned per agent; the admin sets only the ceiling (enable + permission).

    @turn_cached
    async def get_agent_ability_modes(self, agent_id: str) -> Dict[str, str]:
        if not agent_id:
            return {}
        from app.tools.tool_modes import AGENT_ABILITY_MODES_KEY, normalize_visibility
        meta = await self._agent_metadata(agent_id)
        modes = (meta or {}).get(AGENT_ABILITY_MODES_KEY) or {}
        if not isinstance(modes, dict):
            return {}
        out: Dict[str, str] = {}
        for k, v in modes.items():
            nv = normalize_visibility(v)
            if isinstance(k, str) and nv:
                out[k] = nv
        return out

    @turn_cached
    async def get_agent_discovery_default(self, agent_id: str) -> Optional[str]:
        """The agent's per-agent DEFAULT visibility ("visible"|"discoverable"),
        applied to any ability with no explicit per-ability choice. Returns None
        when unset (→ the built-in "visible" behaviour). Lives in
        agents.metadata[discovery_default]."""
        if not agent_id:
            return None
        from app.tools.tool_modes import AGENT_DISCOVERY_DEFAULT_KEY, normalize_visibility
        meta = await self._agent_metadata(agent_id)
        return normalize_visibility((meta or {}).get(AGENT_DISCOVERY_DEFAULT_KEY))

    async def set_agent_discovery_default(
        self, agent_id: str, mode: Optional[str]
    ) -> Optional[str]:
        """Set (or clear, when ``mode`` is falsy/invalid) the agent's default
        visibility. Stored in agents.metadata[discovery_default]. Returns the
        stored value (or None when cleared)."""
        from app.tools.tool_modes import AGENT_DISCOVERY_DEFAULT_KEY, normalize_visibility
        nv = normalize_visibility(mode)
        async with self._write_lock:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    "SELECT metadata FROM agents WHERE id = ?", (agent_id,)
                ).fetchone()
                if not row:
                    return None
                try:
                    meta = json.loads(row["metadata"]) if row["metadata"] else {}
                except (json.JSONDecodeError, TypeError):
                    meta = {}
                if not isinstance(meta, dict):
                    meta = {}
                if nv:
                    meta[AGENT_DISCOVERY_DEFAULT_KEY] = nv
                else:
                    meta.pop(AGENT_DISCOVERY_DEFAULT_KEY, None)
                conn.execute(
                    "UPDATE agents SET metadata = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(meta), _now_iso(), agent_id),
                )
                conn.commit()
                return nv
            finally:
                conn.close()

    async def set_agent_ability_mode(
        self, agent_id: str, ability_id: str, mode: str
    ) -> Dict[str, str]:
        """Set one ability's visibility for an agent. mode in
        {"visible","discoverable"}; anything else clears the explicit setting
        (back to the "visible" default). Returns the updated ability_modes map."""
        from app.tools.tool_modes import AGENT_ABILITY_MODES_KEY, normalize_visibility
        nmode = normalize_visibility(mode)
        async with self._write_lock:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    "SELECT metadata FROM agents WHERE id = ?", (agent_id,)
                ).fetchone()
                if not row:
                    return {}
                try:
                    meta = json.loads(row["metadata"]) if row["metadata"] else {}
                    if not isinstance(meta, dict):
                        meta = {}
                except (json.JSONDecodeError, TypeError):
                    meta = {}
                modes = meta.get(AGENT_ABILITY_MODES_KEY)
                if not isinstance(modes, dict):
                    modes = {}
                if nmode:
                    modes[ability_id] = nmode
                else:
                    modes.pop(ability_id, None)
                meta[AGENT_ABILITY_MODES_KEY] = modes
                conn.execute(
                    "UPDATE agents SET metadata = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(meta), _now_iso(), agent_id),
                )
                conn.commit()
                return modes
            finally:
                conn.close()

    # ── Per-agent ability ACCESS level (agents.metadata "ability_access") ──
    # {ability_id: "everyone"|"registered"|"admin"}. Decides WHICH CALLER may
    # trigger the ability (everyone = anyone incl. anon guests, the default).
    # A different axis from visibility (which tunes how the AGENT sees it).

    @turn_cached
    async def get_agent_ability_access(self, agent_id: str) -> Dict[str, str]:
        if not agent_id:
            return {}
        from app.tools.tool_modes import AGENT_ABILITY_ACCESS_KEY, normalize_access
        meta = await self._agent_metadata(agent_id)
        access = (meta or {}).get(AGENT_ABILITY_ACCESS_KEY) or {}
        if not isinstance(access, dict):
            return {}
        out: Dict[str, str] = {}
        for k, v in access.items():
            nv = normalize_access(v)
            if isinstance(k, str) and nv:
                out[k] = nv
        return out

    async def set_agent_ability_access(
        self, agent_id: str, ability_id: str, level: str
    ) -> Dict[str, str]:
        """Set one ability's required caller-access level for an agent. level in
        {"everyone","registered","admin"}; "everyone" (or anything unknown)
        clears the explicit setting back to the no-restriction default. Returns
        the updated ability_access map."""
        from app.tools.tool_modes import (
            AGENT_ABILITY_ACCESS_KEY, normalize_access, ACCESS_EVERYONE,
        )
        nlevel = normalize_access(level)
        async with self._write_lock:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    "SELECT metadata FROM agents WHERE id = ?", (agent_id,)
                ).fetchone()
                if not row:
                    return {}
                try:
                    meta = json.loads(row["metadata"]) if row["metadata"] else {}
                    if not isinstance(meta, dict):
                        meta = {}
                except (json.JSONDecodeError, TypeError):
                    meta = {}
                access = meta.get(AGENT_ABILITY_ACCESS_KEY)
                if not isinstance(access, dict):
                    access = {}
                # "everyone" is the default — store nothing for it so the map only
                # ever holds genuine restrictions.
                if nlevel and nlevel != ACCESS_EVERYONE:
                    access[ability_id] = nlevel
                else:
                    access.pop(ability_id, None)
                meta[AGENT_ABILITY_ACCESS_KEY] = access
                conn.execute(
                    "UPDATE agents SET metadata = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(meta), _now_iso(), agent_id),
                )
                conn.commit()
                return access
            finally:
                conn.close()

    # ── Per-agent ability-skill visibility (agents.metadata "skill_modes") ──
    # {skill_key: "off"|"visible"|"discoverable"}. Off withholds the skill; visible = its body
    # is always shown; discoverable = load on demand. Absent → descriptor default.

    @turn_cached
    async def get_agent_skill_modes(self, agent_id: str) -> Dict[str, str]:
        if not agent_id:
            return {}
        from app.tools.tool_modes import AGENT_SKILL_MODES_KEY, normalize_skill_mode
        meta = await self._agent_metadata(agent_id)
        modes = (meta or {}).get(AGENT_SKILL_MODES_KEY) or {}
        if not isinstance(modes, dict):
            return {}
        out: Dict[str, str] = {}
        for k, v in modes.items():
            nv = normalize_skill_mode(v)
            if isinstance(k, str) and nv:
                out[k] = nv
        return out

    async def set_agent_skill_mode(
        self, agent_id: str, ability_id: str, mode: str
    ) -> Dict[str, str]:
        """Set the per-agent visibility of an ability's bundled skill. mode in
        {"off","visible","discoverable"}; anything else clears the override. Returns the
        updated skill_modes map."""
        from app.tools.tool_modes import AGENT_SKILL_MODES_KEY, normalize_skill_mode
        nmode = normalize_skill_mode(mode)
        async with self._write_lock:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    "SELECT metadata FROM agents WHERE id = ?", (agent_id,)
                ).fetchone()
                if not row:
                    return {}
                try:
                    meta = json.loads(row["metadata"]) if row["metadata"] else {}
                    if not isinstance(meta, dict):
                        meta = {}
                except (json.JSONDecodeError, TypeError):
                    meta = {}
                modes = meta.get(AGENT_SKILL_MODES_KEY)
                if not isinstance(modes, dict):
                    modes = {}
                if nmode:
                    modes[ability_id] = nmode
                else:
                    modes.pop(ability_id, None)
                meta[AGENT_SKILL_MODES_KEY] = modes
                conn.execute(
                    "UPDATE agents SET metadata = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(meta), _now_iso(), agent_id),
                )
                conn.commit()
                return modes
            finally:
                conn.close()

    async def neutralize_skill_load(self, session_id: str, name: str) -> int:
        """Overwrite stored load_skill result rows for `name` so the body leaves
        context once the user deactivates the skill. Keeps the row (and its
        tool_call_id pairing) intact — only the content changes."""
        from app.agent.skills import loaded_result_prefix, deactivated_text
        async with self._write_lock:
            conn = self._get_conn()
            try:
                cur = conn.execute(
                    "UPDATE interactions SET content = ? "
                    "WHERE session_id = ? AND tool_name = 'load_skill' AND content LIKE ?",
                    (deactivated_text(name), session_id, loaded_result_prefix(name) + "%"),
                )
                conn.commit()
                return cur.rowcount or 0
            finally:
                conn.close()

    async def fetch_agent_with_context(
        self,
        user_id: str,
        context_types: Optional[List[str]] = None,
    ) -> Optional[dict]:
        """Fetch any agent owned by the user (oldest first) + resolved prompt slots."""
        conn = self._get_conn()
        try:
            row = conn.execute(
                """SELECT * FROM agents
                   WHERE EXISTS (SELECT 1 FROM json_each(admin_users) WHERE value = ?)
                   ORDER BY created_at ASC LIMIT 1""",
                (user_id,)
            ).fetchone()
            if not row:
                return None
            agent_dict = dict(row)
            agent_dict["context_documents"] = await self._docs_for_caller(
                agent_dict["id"], user_id, context_types,
            )
            return agent_dict
        except Exception as e:
            logger.error("Error fetching agent with context: %s", e)
            raise
        finally:
            conn.close()

    async def fetch_agent_by_id_with_context(
        self,
        agent_id: str,
        context_types: Optional[List[str]] = None,
        user_id: Optional[str] = None,
    ) -> Optional[dict]:
        """Fetch one agent by id plus its resolved prompt slots for `user_id`.

        If user_id is None, returns admin-base content only.
        """
        conn = self._get_conn()
        try:
            row = conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
            if not row:
                return None
            agent_dict = dict(row)
            agent_dict["context_documents"] = await self._docs_for_caller(
                agent_id, user_id, context_types,
            )
            return agent_dict
        except Exception as e:
            logger.error("Error fetching agent by id with context: %s", e)
            raise
        finally:
            conn.close()

    async def _docs_for_caller(
        self,
        agent_id: str,
        user_id: Optional[str],
        context_types: Optional[List[str]] = None,
    ) -> List[dict]:
        """Resolved per-slot content shaped as legacy context_documents list."""
        slots = await self.resolve_prompts(agent_id, user_id=user_id)
        docs: List[dict] = []
        for s in slots:
            content = (s.get("content") or "").strip()
            if not content:
                continue
            docs.append({
                "id": s["slot_name"],
                "context_type": s["slot_name"],
                "title": s["slot_name"],
                "content": s["content"],
                "tags": [],
            })
        if context_types:
            docs = [d for d in docs if d["context_type"] in context_types]
        return docs

    async def create_agent_for_user(
        self, user_id: str, *, agent_id: Optional[str] = None
    ) -> dict:
        conn = self._get_conn()
        try:
            # Templates are seeded at boot (manifest-gated) + on admin re-seed.
            # No per-call re-seed: avoids needless DB churn and protects admin edits.

            # Clone the default template
            tpl = conn.execute(
                "SELECT * FROM agent_templates WHERE id = 'default'"
            ).fetchone()
            if not tpl:
                logger.warning(
                    "No 'default' agent template found after JSON seeding — "
                    "check app/defaults/agents/default.json (or data/agents/ override)"
                )
                raise ValueError("No default agent template available")

            tpl_data = dict(tpl)
            now = _now_iso()
            agent_id = agent_id or _uuid()
            # Default WebAgent ships with DISCOVERABLE tools/abilities: its whole
            # tool surface is withheld behind the `# [ABILITIES]` menu and pulled
            # in on demand via load_ability, instead of shipping every tool schema
            # each turn. See tool_modes.AGENT_DISCOVERY_DEFAULT_KEY.
            from app.agent.cache_profiles import (
                DEFAULT_WEBAGENT_PROFILE, with_cache_profile,
            )
            _meta_obj = with_cache_profile(
                tpl_data["metadata"], DEFAULT_WEBAGENT_PROFILE,
            )
            _meta_obj["template_origin"] = "default"
            _meta_json = json.dumps(_meta_obj)
            _allowed_tools, _safety_policy = self._tool_perm_columns(_meta_json)
            conn.execute(
                """INSERT INTO agents
                   (id, name,
                    max_turn_count, max_wall_seconds,
                    max_identical_tool_calls, max_stall_strikes,
                    model, provider,
                    temperature, max_tokens, status, metadata,
                    template_id,
                    trigger_type, trigger_key, loop_logic,
                    allowed_tools, safety_policy, auto_resume,
                    is_user_default, admin_users, assigned_at, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, 1, 1, ?, ?, ?, ?)""",
                (agent_id, tpl_data.get("name", "genui"),
                 tpl_data["max_turn_count"],
                 tpl_data.get("max_wall_seconds"),
                 tpl_data.get("max_identical_tool_calls", 0),
                 tpl_data.get("max_stall_strikes", 0),
                 tpl_data["model"],
                 tpl_data["provider"],
                 tpl_data["temperature"],
                 tpl_data["max_tokens"],
                 _meta_json,
                 "default",
                 tpl_data.get("trigger_type", "user_input"),
                 tpl_data.get("trigger_key"),
                 tpl_data.get("loop_logic", "[]"),
                 _allowed_tools, _safety_policy,
                 json.dumps([user_id]),
                 now, now, now),
            )
            self._clone_template_slots(conn, source_id="default", target_id=agent_id, now=now)
            self._seed_pre_enabled_connections(conn, agent_id, _meta_json, now)
            conn.commit()

            # Set as user's default agent in user_profiles
            conn.execute(
                """INSERT INTO user_profiles (user_id, is_admin, default_agent_id, created_at, updated_at)
                   VALUES (?, 0, ?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET
                     default_agent_id = excluded.default_agent_id,
                     updated_at = excluded.updated_at""",
                (user_id, agent_id, now, now),
            )
            conn.commit()

            row = conn.execute(
                "SELECT * FROM agents WHERE id = ?", (agent_id,)
            ).fetchone()
            logger.info(
                "Created agent %s for user %s from JSON template",
                agent_id, user_id,
            )
            return dict(row)
        except Exception as e:
            logger.error("Error creating agent for user %s: %s", user_id, e)
            raise
        finally:
            conn.close()

    async def increment_agent_turn_count(self, agent_id: str) -> int:
        async with self._write_lock:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    "SELECT turn_count FROM agents WHERE id = ?", (agent_id,)
                ).fetchone()
                current = (row["turn_count"] if row else None) or 0
                new_count = current + 1
                # NOTE: do NOT touch ``updated_at`` here — turn_count is an
                # activity counter, not a config edit. Bumping updated_at every
                # turn would invalidate the per-agent tool cache (whose version
                # IS updated_at) on every message, so load_tools would re-run its
                # dozens of round-trips each time. Real config edits (tool/ability
                # modes, connections, data sources) still bump updated_at. Matches
                # the Postgres backend, which only updates turn_count.
                conn.execute(
                    "UPDATE agents SET turn_count = ? WHERE id = ?",
                    (new_count, agent_id),
                )
                conn.commit()
                return new_count
            except Exception as e:
                logger.error("Error incrementing turn count for agent %s: %s", agent_id, e)
                raise
            finally:
                conn.close()

    async def get_default_template(self) -> dict:
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM agent_templates WHERE id = 'default'"
            ).fetchone()
            if row:
                return dict(row)
            logger.warning(
                "No 'default' agent template in DB — check app/defaults/agents/default.json (or data/agents/ override)"
            )
            # Fallback: minimal dict — JSON is the real source of truth
            return {
                "id": "default",
                "system_prompt": "",
                "max_turn_count": 0,
                "max_wall_seconds": None,
                "model": None,
                "provider": None,
                "temperature": 0.0,
                "max_tokens": 8096,
                "metadata": "{}",
                "created_at": _now_iso(),
                "updated_at": _now_iso(),
            }
        finally:
            conn.close()

    async def get_max_turn_count(self, agent_id: str = "default_agent") -> int:
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT max_turn_count FROM agents WHERE id = ?", (agent_id,)
            ).fetchone()
            if row:
                return row["max_turn_count"]
            logger.warning("Agent %s not found for max_turn_count lookup", agent_id)
            return 10
        finally:
            conn.close()

    async def seed_agent_templates(self, force: bool = False) -> dict:
        """Re-seed agent_templates + agent_prompt_templates from JSON.

        force=True overrides the manifest short-circuit AND overwrites
        agent_prompt_templates rows whose source = 'admin'. Use sparingly.
        """
        conn = self._get_conn()
        try:
            return self._seed_agent_templates_from_json_files(conn, force=force)
        finally:
            conn.close()

    # ---- Agent Resolution & Session Binding ----

    async def find_default_agent(self, user_id: str) -> Optional[dict]:
        """Authoritative singleton lookup for a user's **WebAgent** — their oldest
        ACTIVE agent cloned from the ``default`` template.

        Returns the agents row (dict, with ``source='custom'``) or ``None``. This is
        the anti-duplication cornerstone: the idempotent provisioner calls it BEFORE
        creating a WebAgent, so a device never mints a second one when one already
        exists. It is deliberately NOT overridden on the HybridBackend, so
        ``__getattr__`` routes it to the REMOTE authority (Postgres) — the whole
        point is to see agents this device's local mirror may not have pulled yet,
        which is exactly the gap that let every device spawn its own duplicate.

        Clones (``status='clone'``) and trashed rows are excluded; ties break on the
        earliest creation so the choice is stable across devices."""
        conn = self._get_conn()
        try:
            row = conn.execute(
                """SELECT * FROM agents
                   WHERE template_id = 'default'
                     AND (status IS NULL OR status = '' OR status = 'active')
                     AND EXISTS (
                           SELECT 1 FROM json_each(admin_users) WHERE value = ?
                         )
                   ORDER BY (sort_order IS NULL), sort_order ASC, created_at ASC
                   LIMIT 1""",
                (user_id,),
            ).fetchone()
            if not row:
                return None
            entry = dict(row)
            entry["source"] = "custom"
            return entry
        except Exception as e:
            logger.error("find_default_agent failed for %s: %s", user_id, e)
            return None
        finally:
            conn.close()

    async def resolve_agent(self, user_id: str, template_id: str) -> dict:
        """
        Resolve an agent for a user + template combo.

        1. Look for existing active agent row with matching user_id + template_id.
        2. If found as 'active', return it (with status 'active').
        3. If not found, return a virtual dict with status='template' and all
           fields from the agent_templates table, ready for the caller to
           materialize into a real agents row.
        4. If no template found, raise ValueError.
        """
        conn = self._get_conn()
        try:
            # Templates are seeded at boot (manifest-gated) + on admin re-seed.
            # No per-call re-seed here either.

            # Check for existing agent with matching admin_users + template_id
            row = conn.execute(
                """SELECT * FROM agents WHERE template_id = ?
                   AND EXISTS (SELECT 1 FROM json_each(admin_users) WHERE value = ?) LIMIT 1""",
                (template_id, user_id),
            ).fetchone()
            if row:
                ad = dict(row)
                if ad.get("status") == "active":
                    return ad

            # Look up the template
            tpl = conn.execute(
                "SELECT * FROM agent_templates WHERE id = ?", (template_id,)
            ).fetchone()
            if not tpl:
                raise ValueError(f"No agent template found for id '{template_id}'")

            tpl_data = dict(tpl)
            return {
                "status": "template",
                "template_id": template_id,
                "name": tpl_data.get("name", ""),
                "max_turn_count": tpl_data.get("max_turn_count", 0),
                "max_wall_seconds": tpl_data.get("max_wall_seconds"),
                "model": tpl_data.get("model"),
                "provider": tpl_data.get("provider"),
                "temperature": tpl_data.get("temperature", 0.0),
                "max_tokens": tpl_data.get("max_tokens", 8096),
                "metadata": tpl_data.get("metadata", "{}"),
                "trigger_type": tpl_data.get("trigger_type", "user_input"),
                "trigger_key": tpl_data.get("trigger_key"),
                "loop_logic": tpl_data.get("loop_logic", "[]"),
            }
        except ValueError:
            raise
        except Exception as e:
            logger.error("Error resolving agent: %s", e)
            raise RuntimeError(
                f"Failed to resolve agent for template_id={template_id}: {e}"
            )
        finally:
            conn.close()

    async def bind_session_to_agent(self, session_id: str, agent_id: str) -> None:
        """Bind a session to an agent by setting sessions.agent_id."""
        async with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    "UPDATE sessions SET agent_id = ?, updated_at = ? WHERE id = ?",
                    (agent_id, _now_iso(), session_id),
                )
                conn.commit()
                logger.debug("Bound session %s to agent %s", session_id, agent_id)
            except Exception as e:
                logger.error("Error binding session to agent: %s", e)
                raise
            finally:
                conn.close()

    async def get_session_agent_id(self, session_id: str) -> Optional[str]:
        """Get the agent_id bound to a session."""
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT agent_id FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            return row["agent_id"] if row else None
        except Exception as e:
            logger.error("Error getting session agent_id: %s", e)
            raise
        finally:
            conn.close()

    # ---- Session Participants ----

    async def add_session_participant(
        self, session_id: str, participant_id: str, role: str
    ) -> None:
        """Add a participant to a session. role is 'user' or 'agent'."""
        # Serialize through the shared write lock: this is a SELECT-then-UPDATE on
        # the same connection, which under WAL hits the lock-upgrade race and fails
        # "database is locked" if another writer is mid-transaction (busy_timeout
        # deliberately won't wait on an upgrade). Holding _write_lock makes it queue
        # behind other in-process writers instead of 500ing — matching the pattern
        # in insert_interaction and the other hot write paths.
        async with self._write_lock:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    "SELECT participants FROM sessions WHERE id=?", (session_id,)
                ).fetchone()
                participants = json.loads(row[0]) if row and row[0] else []
                # Don't add duplicate
                if not any(p.get("id") == participant_id for p in participants):
                    participants.append({"id": participant_id, "role": role})
                    conn.execute(
                        "UPDATE sessions SET participants=? WHERE id=?",
                        (json.dumps(participants), session_id),
                    )
                    conn.commit()
            finally:
                conn.close()

    async def remove_session_participant(
        self, session_id: str, participant_id: str
    ) -> None:
        """Remove a participant from a session by id."""
        # Serialize through the shared write lock (same SELECT-then-UPDATE
        # upgrade-race reason as add_session_participant above).
        async with self._write_lock:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    "SELECT participants FROM sessions WHERE id=?", (session_id,)
                ).fetchone()
                participants = json.loads(row[0]) if row and row[0] else []
                participants = [p for p in participants if p.get("id") != participant_id]
                conn.execute(
                    "UPDATE sessions SET participants=? WHERE id=?",
                    (json.dumps(participants), session_id),
                )
                conn.commit()
            finally:
                conn.close()

    async def is_session_participant(
        self, session_id: str, participant_id: str, role: Optional[str] = None
    ) -> bool:
        """Check if participant_id is in a session. If role specified, also checks role matches."""
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT participants FROM sessions WHERE id=?", (session_id,)
            ).fetchone()
            participants = json.loads(row[0]) if row and row[0] else []
            for p in participants:
                if p.get("id") == participant_id:
                    if role is None or p.get("role") == role:
                        return True
            return False
        finally:
            conn.close()

    async def get_session_participants(
        self, session_id: str
    ) -> List[dict]:
        """Return the full participants array for a session."""
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT participants FROM sessions WHERE id=?", (session_id,)
            ).fetchone()
            return json.loads(row[0]) if row and row[0] else []
        finally:
            conn.close()

    async def get_or_resolve_session_agent(
        self,
        session_id: str,
        user_id: str,
        template_id: Optional[str] = None,
    ) -> Optional[dict]:
        """
        Get the agent for a session, creating/binding it if needed.

        1. If ``sessions.agent_id`` is set → ``fetch_agent_by_id_with_context(agent_id)``
           (direct FK lookup, zero inference chain).
        2. If not set → ``resolve_agent(user_id, template_id)`` to obtain the agent.
           - If status is 'template' or 'filesystem', materialize a real ``agents`` row.
           - Call ``bind_session_to_agent(session_id, agent_id)``.
           - Return ``fetch_agent_by_id_with_context(agent_id)``.
        """
        # Check existing binding first
        existing_id = await self.get_session_agent_id(session_id)
        if existing_id:
            agent = await self.fetch_agent_by_id_with_context(existing_id, user_id=user_id)
            if agent:
                return agent
            logger.warning(
                "Session %s bound to agent %s but agent not found — re-resolving",
                session_id[:8], existing_id[:8],
            )

        # No binding or agent gone — resolve and bind. The default template is
        # special: it is the one app-level singleton, never materialized into a
        # new per-user row by this generic template path.
        resolved_template = template_id or "default"
        if resolved_template == "default":
            from app.api.agents import provision_default_agent
            agent = await provision_default_agent(self, user_id)
            if agent is None:
                return None
        else:
            agent = await self.resolve_agent(user_id, resolved_template)

        # If virtual (template/filesystem), materialize as real agents row
        if agent.get("status") in ("template", "filesystem"):
            agent_id = _uuid()
            conn = self._get_conn()
            try:
                now = _now_iso()
                _owner = user_id
                # Normalise metadata to a JSON *string* (resolve_agent hands it
                # back already-stringified for template rows; re-dumping a string
                # would double-encode it and hide tool_modes / pre_enabled_*).
                _meta_in = agent.get("metadata", {})
                _meta_str = _meta_in or "{}" if isinstance(_meta_in, str) else json.dumps(_meta_in or {})
                _allowed_tools, _safety_policy = self._tool_perm_columns(_meta_str)
                conn.execute(
                    """INSERT INTO agents
                       (id, template_id, name, max_turn_count, max_wall_seconds, model, provider,
                        temperature, max_tokens, status, metadata, trigger_type, trigger_key, loop_logic,
                        allowed_tools, safety_policy,
                        is_admin_agent, admin_users, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (agent_id, resolved_template,
                     agent.get("name", ""),
                     agent.get("max_turn_count", 0),
                     agent.get("max_wall_seconds"),
                     agent.get("model"),
                     agent.get("provider"),
                     agent.get("temperature", 0.0),
                     agent.get("max_tokens", 8096),
                     _meta_str,
                     agent.get("trigger_type", "user_input"),
                     agent.get("trigger_key"),
                     agent.get("loop_logic", "[]"),
                     _allowed_tools, _safety_policy,
                     1 if agent.get("is_admin_agent") else 0,
                     json.dumps([user_id]),
                     now, now),
                )
                # Clone admin-base slots from the source template into the new agent row.
                self._clone_template_slots(
                    conn,
                    source_id=resolved_template,
                    target_id=agent_id,
                    now=now,
                )
                # Seed the template's pre-enabled abilities (expanding "*" /
                # group wildcards) so an agent materialized straight from a
                # template — e.g. a session bound to "default" with no
                # pre-created agent row — gets the same ability set as one made
                # via create_custom_agent. Without this the default agent would
                # materialize with zero abilities.
                self._seed_pre_enabled_connections(conn, agent_id, _meta_str, now)
                conn.commit()
            except Exception as e:
                logger.error("Error materializing agent: %s", e)
                raise
            finally:
                conn.close()
            agent["id"] = agent_id

        # Bind session to agent
        if agent.get("id"):
            await self.bind_session_to_agent(session_id, agent["id"])
            # Auto-register user and agent as participants
            if not await self.is_session_participant(session_id, agent["id"], 'agent'):
                await self.add_session_participant(session_id, agent["id"], 'agent')
            if not await self.is_session_participant(session_id, user_id, 'user'):
                await self.add_session_participant(session_id, user_id, 'user')

        # Fetch with context (by ID, not user_id)
        if agent.get("id"):
            return await self.fetch_agent_by_id_with_context(agent["id"], user_id=user_id)
        return agent

    # ---- Interrupt Handling ----

    async def set_interrupt(self, session_id: str) -> None:
        async with self._write_lock:
            conn = self._get_conn()
            try:
                owner = conn.execute(
                    "SELECT user_id FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if not owner or not owner["user_id"]:
                    raise LookupError(
                        f"Cannot interrupt missing or unowned session {session_id}"
                    )
                now = _now_iso()
                # Schema-drift guard. On a database that predates ownership-
                # scoped interrupt rows the additive repair runs at open, but a
                # DB reached through a path that skips it still has the legacy
                # 3-column table. Writing ``user_id`` there raised
                # "table session_interrupts has no column named user_id", so the
                # stop flag never landed and the run kept executing — the footer
                # Stop button appeared to do nothing. Fall back to the legacy
                # shape so a Stop is ALWAYS recorded; the ownership migration
                # backfills the real owner on the next open.
                columns = {
                    row[1] for row in conn.execute(
                        "PRAGMA table_info(session_interrupts)"
                    ).fetchall()
                }
                if "user_id" in columns:
                    conn.execute(
                        """INSERT INTO session_interrupts
                               (session_id, user_id, interrupt_requested, created_at)
                           VALUES (?, ?, 1, ?)
                           ON CONFLICT(session_id) DO UPDATE SET
                               user_id = excluded.user_id,
                               interrupt_requested = 1,
                               created_at = excluded.created_at""",
                        (session_id, owner["user_id"], now),
                    )
                else:
                    logger.warning(
                        "set_interrupt: %s has a legacy session_interrupts table "
                        "(no user_id column); recording the stop in compatibility "
                        "shape — ownership migration will backfill on next open",
                        session_id[:12],
                    )
                    conn.execute(
                        """INSERT INTO session_interrupts
                               (session_id, interrupt_requested, created_at)
                           VALUES (?, 1, ?)
                           ON CONFLICT(session_id) DO UPDATE SET
                               interrupt_requested = 1,
                               created_at = excluded.created_at""",
                        (session_id, now),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    async def clear_interrupt(self, session_id: str) -> None:
        async with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    "DELETE FROM session_interrupts WHERE session_id = ?",
                    (session_id,)
                )
                conn.commit()
            except Exception as e:
                logger.error("Error clearing interrupt for %s: %s", session_id, e)
                raise
            finally:
                conn.close()

    # ---- Attachments ----

    async def insert_attachment(
        self,
        user_id: str,
        session_id: str,
        original_name: str,
        mime_type: str,
        size_bytes: int,
        storage_path: str,
        metadata: Optional[dict] = None,
        storage_provider: str = "local",
    ) -> str:
        """Insert an attachment record. Returns the attachment id."""
        att_id = _uuid()
        conn = self._get_conn()
        try:
            conn.execute(
                """INSERT INTO attachments
                   (id, user_id, session_id, original_name, mime_type, size_bytes,
                    storage_path, storage_provider, metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (att_id, user_id, session_id, original_name, mime_type, size_bytes,
                 storage_path, storage_provider, json.dumps(metadata or {})),
            )
            conn.commit()
            logger.debug("Inserted attachment %s: %s (provider=%s)", att_id, original_name, storage_provider)
            return att_id
        finally:
            conn.close()

    async def get_attachment(self, attachment_id: str) -> Optional[dict]:
        """Get a single attachment by id."""
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM attachments WHERE id = ?", (attachment_id,)
            ).fetchone()
            if not row:
                return None
            return dict(row)
        finally:
            conn.close()

    async def get_session_attachments(self, session_id: str) -> List[dict]:
        """Get all attachments for a session."""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM attachments WHERE session_id = ? ORDER BY created_at",
                (session_id,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    async def delete_attachment(self, attachment_id: str) -> bool:
        """Delete an attachment record by id."""
        conn = self._get_conn()
        try:
            cur = conn.execute("DELETE FROM attachments WHERE id = ?", (attachment_id,))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    async def update_attachment_metadata(self, attachment_id: str, patch: dict) -> bool:
        """Merge `patch` into an attachment's metadata JSON (preserving existing keys
        such as image dimensions). Returns True if a row was updated."""
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT metadata FROM attachments WHERE id = ?", (attachment_id,)
            ).fetchone()
            if not row:
                return False
            try:
                meta = json.loads(row["metadata"] or "{}")
                if not isinstance(meta, dict):
                    meta = {}
            except Exception:
                meta = {}
            meta.update(patch or {})
            cur = conn.execute(
                "UPDATE attachments SET metadata = ? WHERE id = ?",
                (json.dumps(meta), attachment_id),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    async def update_interaction_content(self, interaction_id: str, content: str) -> bool:
        """Replace an interaction row's content (used to persist injected attachment
        descriptions into the user turn so later turns retain them)."""
        conn = self._get_conn()
        try:
            cur = conn.execute(
                "UPDATE interactions SET content = ? WHERE id = ?",
                (content, interaction_id),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    async def update_interaction(
        self,
        interaction_id: str,
        *,
        content: Optional[str] = None,
        status: Optional[str] = None,
        output_data: Optional[str] = None,
        metadata: Optional[str] = None,
        chat_render_kind: Optional[str] = None,
    ) -> bool:
        """Patch selected fields of an interaction row in one statement.

        Used by the streaming-answer persistence path: the assistant row is
        inserted up front as status='streaming', its `content` is updated as
        tokens arrive, and it is finalized with status='complete' (plus the
        tool_calls payload in `output`) when the step ends. None fields are
        left untouched."""
        sets: List[str] = []
        vals: List[Any] = []
        if content is not None:
            sets.append("content = ?")
            vals.append(content)
        if status is not None:
            sets.append("status = ?")
            vals.append(status)
        if output_data is not None:
            sets.append("output = ?")
            vals.append(output_data)
        if metadata is not None:
            sets.append("metadata = ?")
            vals.append(metadata)
        if chat_render_kind is not None:
            sets.append("chat_render_kind = ?")
            vals.append(chat_render_kind)
        if not sets:
            return False
        vals.append(interaction_id)
        async with self._write_lock:
            conn = self._get_conn()
            try:
                cur = conn.execute(
                    f"UPDATE interactions SET {', '.join(sets)} WHERE id = ?",
                    tuple(vals),
                )
                if cur.rowcount > 0:
                    row = conn.execute(
                        "SELECT session_id, status FROM interactions WHERE id=?", (interaction_id,),
                    ).fetchone()
                    # Streaming snapshots classify as pending. Once a row reaches
                    # a durable phase, update its single range projection without
                    # rescanning the session.
                    if row and str(row["status"] or "") != "streaming":
                        project_interaction_activity(conn, interaction_id)
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    async def get_activity_summary_for_interaction(
        self, user_id: str, session_id: str, interaction_id: str,
    ) -> Optional[Dict[str, Any]]:
        await self.assert_session_owned(user_id, session_id)
        conn = self._get_conn()
        try:
            ensure_session_activity_summaries(conn)
            snapshot = activity_summary_for_interaction(conn, interaction_id)
            return snapshot if snapshot and snapshot.get("session_id") == session_id else None
        finally:
            conn.close()

    # ──────────────────────────────────────────────────────────────────────
    # Run state — durable per-session record of an in-flight agent turn.
    # The in-memory RunBuffer (app/agent/run_buffer.py) is a low-latency replay
    # cache in front of THIS; these rows are the source of truth that survives
    # buffer eviction and lets a cold device discover an active run from the DB.
    # ──────────────────────────────────────────────────────────────────────

    async def background_leader_acquire(self, holder_id: str, ttl_seconds: int = 30) -> bool:
        """Atomically claim or renew the singleton background-leader lease.

        Returns True if THIS ``holder_id`` now owns the lease (freshly acquired
        or renewed). Multi-worker safe: SQLite serializes the UPDATE, so exactly
        one worker can flip an unheld/expired lock to itself; everyone else gets
        rowcount 0. The lease is TTL'd via ``expires_at`` so a dead leader's lock
        is reclaimable. See app/coordination/leader.py."""
        now = datetime.now(timezone.utc)
        now_s = now.isoformat()
        exp_s = (now + timedelta(seconds=ttl_seconds)).isoformat()
        async with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO background_leader (lock_key, holder_id, heartbeat_at, expires_at) "
                    "VALUES ('background', NULL, NULL, NULL)"
                )
                cur = conn.execute(
                    "UPDATE background_leader SET holder_id=?, heartbeat_at=?, expires_at=? "
                    "WHERE lock_key='background' AND "
                    "(holder_id=? OR holder_id IS NULL OR expires_at IS NULL OR expires_at < ?)",
                    (holder_id, now_s, exp_s, holder_id, now_s),
                )
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    async def background_leader_release(self, holder_id: str) -> None:
        """Release the background-leader lease if we hold it (clean shutdown handoff)."""
        async with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    "UPDATE background_leader SET holder_id=NULL, heartbeat_at=NULL, expires_at=NULL "
                    "WHERE lock_key='background' AND holder_id=?",
                    (holder_id,),
                )
                conn.commit()
            finally:
                conn.close()

    # ── Multi-device coordination (see app/devices/) ──────────────────────────
    # A shared DB lets one user's several WebAgent instances ("devices") see each
    # other and hand work back and forth. device_heartbeat / list_devices power
    # the presence registry; enqueue/claim/finish/reclaim are the cross-device
    # dispatch QUEUE. The claim is atomic (SQLite serialises the UPDATE, exactly
    # like background_leader_acquire) so EXACTLY ONE device runs each job even when
    # several share the database — but UNLIKE the leader lock, every device runs
    # its own worker and claims only jobs addressed to its own instance_id.

    async def device_heartbeat(self, instance_id, label=None, capabilities=None,
                               endpoint=None):
        """Upsert THIS device's presence row, stamping last_seen=now."""
        import json as _json
        now = _now_iso()
        caps = capabilities if isinstance(capabilities, str) else _json.dumps(capabilities or {})
        async with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    """INSERT INTO device_presence
                         (instance_id, label, capabilities, endpoint, last_seen, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT (instance_id) DO UPDATE SET
                         label=excluded.label,
                         capabilities=excluded.capabilities,
                         endpoint=excluded.endpoint,
                         last_seen=excluded.last_seen""",
                    (instance_id, label, caps, endpoint, now, now),
                )
                conn.commit()
            finally:
                conn.close()

    async def list_devices(self, online_within_seconds=60):
        """All known devices, newest heartbeat first, each tagged online/offline."""
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(seconds=int(online_within_seconds))).isoformat()
        conn = self._get_conn()
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM device_presence ORDER BY last_seen DESC"
            ).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["online"] = bool(d.get("last_seen") and d["last_seen"] >= cutoff)
                out.append(d)
            return out
        finally:
            conn.close()

    async def delete_device(self, instance_id):
        """Remove a device's presence row (admin unlink of a stale device). This
        only clears the record — it never reaches the machine; a still-running
        device pointed at this database re-registers on its next heartbeat.
        Returns the number of rows removed (0 if it was already gone)."""
        async with self._write_lock:
            conn = self._get_conn()
            try:
                cur = conn.execute(
                    "DELETE FROM device_presence WHERE instance_id = ?",
                    (instance_id,),
                )
                conn.commit()
                return cur.rowcount
            finally:
                conn.close()

    async def set_device_override(self, instance_id, label=None, icon=None):
        """Set the admin's custom display name / icon for a device (shared
        registry). These override the self-reported hostname + platform icon and
        are NEVER written by the heartbeat, so they persist across check-ins and
        show identically on every device pointed at this database.

        Semantics per field: None = leave unchanged; "" (empty) = clear the
        override (fall back to the default); any other string = set it. Only an
        EXISTING presence row is updated (a device must have checked in to be
        renamed); returns True when a row was updated."""
        sets, params = [], []
        if label is not None:
            sets.append("custom_label = ?")
            params.append(label or None)
        if icon is not None:
            sets.append("custom_icon = ?")
            params.append(icon or None)
        if not sets:
            return False
        params.append(instance_id)
        async with self._write_lock:
            conn = self._get_conn()
            try:
                cur = conn.execute(
                    f"UPDATE device_presence SET {', '.join(sets)} WHERE instance_id = ?",
                    params,
                )
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    async def enqueue_device_job(self, *, owner_user_id, prompt, agent_id=None,
                                 target_instance=None, target_label=None,
                                 created_by_instance=None, payload=None):
        """Insert a pending cross-device job; returns its id."""
        import json as _json
        job_id = _uuid()
        now = _now_iso()
        pl = payload if isinstance(payload, str) else _json.dumps(payload or {})
        async with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    """INSERT INTO device_jobs
                         (id, created_by_instance, target_instance, target_label,
                          owner_user_id, agent_id, prompt, payload, status,
                          created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
                    (job_id, created_by_instance, target_instance, target_label,
                     owner_user_id, agent_id, prompt or "", pl, now, now),
                )
                conn.commit()
            finally:
                conn.close()
        return job_id

    async def claim_device_jobs(self, instance_id, *, limit=4, lease_seconds=600):
        """Atomically claim up to ``limit`` pending jobs addressed to this device
        (target_instance == instance_id) OR broadcast jobs (target_instance NULL).

        Multi-instance safe: each candidate is flipped pending→claimed with a
        conditional UPDATE, so when several devices race, only one wins each row
        (SQLite serialises the writer; everyone else sees rowcount 0). Returns the
        rows THIS device now owns, each as a dict."""
        now = datetime.now(timezone.utc)
        now_s = now.isoformat()
        exp_s = (now + timedelta(seconds=int(lease_seconds))).isoformat()
        claimed = []
        async with self._write_lock:
            conn = self._get_conn()
            try:
                conn.row_factory = sqlite3.Row
                candidates = conn.execute(
                    """SELECT id FROM device_jobs
                       WHERE status='pending'
                         AND (target_instance = ? OR target_instance IS NULL)
                       ORDER BY created_at ASC
                       LIMIT ?""",
                    (instance_id, int(limit)),
                ).fetchall()
                for row in candidates:
                    jid = row["id"]
                    cur = conn.execute(
                        """UPDATE device_jobs
                           SET status='claimed', claimed_by=?, claimed_at=?,
                               lease_expires_at=?, updated_at=?
                           WHERE id=? AND status='pending'""",
                        (instance_id, now_s, exp_s, now_s, jid),
                    )
                    if cur.rowcount > 0:
                        full = conn.execute(
                            "SELECT * FROM device_jobs WHERE id=?", (jid,)
                        ).fetchone()
                        if full is not None:
                            claimed.append(dict(full))
                conn.commit()
            finally:
                conn.close()
        return claimed

    async def get_device_job(self, job_id):
        """Fetch a single device job by id (or None). Lets the Instances page poll
        an enqueued fleet ACTION's outcome — status / result_excerpt / error — for
        actions that leave no visible heartbeat state (git pull, commit+push)."""
        conn = self._get_conn()
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM device_jobs WHERE id=?", (job_id,)
            ).fetchone()
            return dict(row) if row is not None else None
        finally:
            conn.close()

    async def finish_device_job(self, job_id, *, status, result_excerpt=None,
                                error=None, session_id=None):
        """Close out a claimed job (status: done | error)."""
        now = _now_iso()
        async with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    """UPDATE device_jobs
                       SET status=?, result_excerpt=?, error=?, session_id=?, updated_at=?
                       WHERE id=?""",
                    (status, (result_excerpt or "")[:2000], error, session_id, now, job_id),
                )
                conn.commit()
            finally:
                conn.close()

    async def reclaim_expired_device_jobs(self):
        """Return claimed-but-stalled jobs (lease expired, never finished) to the
        pending pool so another device retries them after a crash. Returns the
        number reclaimed."""
        now_s = datetime.now(timezone.utc).isoformat()
        async with self._write_lock:
            conn = self._get_conn()
            try:
                cur = conn.execute(
                    """UPDATE device_jobs
                       SET status='pending', claimed_by=NULL, claimed_at=NULL,
                           lease_expires_at=NULL, updated_at=?
                       WHERE status='claimed'
                         AND lease_expires_at IS NOT NULL
                         AND lease_expires_at < ?""",
                    (now_s, now_s),
                )
                conn.commit()
                return cur.rowcount or 0
            finally:
                conn.close()

    async def run_state_begin(
        self, session_id: str, user_id: str, agent_id: Optional[str], turn_id: Optional[str],
        origin: Optional[str] = None, relaunch_ctx: Optional[str] = None,
        max_resume_attempts: Optional[int] = None,
    ) -> None:
        """Mark a session as having a FRESH run in progress (upsert, status='running').

        This is for a new logical turn — it resets the self-healing bookkeeping
        (stop_cause cleared, resume_attempts=0, backoff/lease cleared, heartbeat
        stamped now) and records ``origin`` + ``relaunch_ctx`` so the run can be
        rebuilt headlessly later. Resumes do NOT call this (that would zero the
        retry budget); they use ``run_state_claim_for_resume``."""
        now = _iso_now()
        async with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    """INSERT INTO session_runs
                         (session_id, user_id, agent_id, turn_id, assistant_interaction_id,
                          status, latest_session_seq, error, started_at, updated_at,
                          stop_cause, origin, resume_attempts, max_resume_attempts,
                          heartbeat_at, next_resume_at, owner_token, lease_expires_at, relaunch_ctx)
                       VALUES (?, ?, ?, ?, NULL, 'running', 0, NULL, ?, ?,
                               NULL, ?, 0, ?, ?, NULL, NULL, NULL, ?)
                       ON CONFLICT(session_id) DO UPDATE SET
                         user_id=excluded.user_id,
                         agent_id=excluded.agent_id,
                         turn_id=excluded.turn_id,
                         assistant_interaction_id=NULL,
                         status='running',
                         latest_session_seq=0,
                         error=NULL,
                         started_at=excluded.started_at,
                         updated_at=excluded.updated_at,
                         stop_cause=NULL,
                         origin=excluded.origin,
                         resume_attempts=0,
                         max_resume_attempts=excluded.max_resume_attempts,
                         resume_cause_counts=NULL,
                         heartbeat_at=excluded.heartbeat_at,
                         next_resume_at=NULL,
                         owner_token=NULL,
                         lease_expires_at=NULL,
                         relaunch_ctx=excluded.relaunch_ctx,
                         current_op=NULL""",
                    (session_id, user_id, agent_id, turn_id, now, now,
                     origin, max_resume_attempts, now, relaunch_ctx),
                )
                conn.commit()
            finally:
                conn.close()
        # Tell every open tab/device this agent just started a run so its grid
        # status dot lights up live. All run paths (web chat, supervised runner,
        # watchdog) funnel through run_state_begin/finish, so this single pair of
        # chokepoints covers every way a run can start or stop.
        await _emit_agent_run_status(user_id, agent_id, session_id, "running", turn_id=turn_id)

    async def run_state_set_assistant(self, session_id: str, assistant_interaction_id: str) -> None:
        """Record which assistant interaction row is the in-progress answer."""
        async with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    "UPDATE session_runs SET assistant_interaction_id=?, updated_at=? WHERE session_id=?",
                    (assistant_interaction_id, _now_iso(), session_id),
                )
                conn.commit()
            finally:
                conn.close()

    async def run_state_update_seq(self, session_id: str, latest_session_seq: int) -> None:
        """Advance the highest session_seq emitted so far (drives WS resume)."""
        async with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    "UPDATE session_runs SET latest_session_seq=?, updated_at=? "
                    "WHERE session_id=? AND latest_session_seq < ?",
                    (latest_session_seq, _now_iso(), session_id, latest_session_seq),
                )
                conn.commit()
            finally:
                conn.close()

    async def run_state_set_op(self, session_id: str, current_op: Optional[str]) -> None:
        """Record (or clear, when None) the in-flight operation snapshot — a small
        JSON string like {"tool": "...", "turn": N, "note": "..."}. Lets a refresh
        re-show the live 'in-process' indicator. Only touches a RUNNING row so a
        late tool_result can't resurrect the indicator after the turn finished."""
        async with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    "UPDATE session_runs SET current_op=?, updated_at=? "
                    "WHERE session_id=? AND status='running'",
                    (current_op, _now_iso(), session_id),
                )
                conn.commit()
            finally:
                conn.close()

    async def run_state_finish(
        self, session_id: str, status: str = "complete", error: Optional[str] = None,
        stop_cause: Optional[str] = None,
        expected_owner_token: Optional[str] = None,
        expected_resume_attempts: Optional[int] = None,
    ) -> bool:
        """Mark the run finished ('complete' | 'interrupted' | 'error').

        ``stop_cause`` records WHY (machine taxonomy). A voluntary cause already
        set by the user-Stop / replace paths (user_stop, replaced,
        needs_manual_resume) is NEVER overwritten here — the loop's generic
        terminal status must not erase the recorded intent. Passing
        ``stop_cause=None`` leaves the existing cause untouched. Ownership and
        recovery-attempt fences make a stale finisher a no-op; the current
        owner's lease is released only when those fences match."""
        _run_user_id = None
        _run_agent_id = None
        updated = False
        async with self._write_lock:
            conn = self._get_conn()
            try:
                _ident = conn.execute(
                    "SELECT user_id, agent_id FROM session_runs WHERE session_id=?",
                    (session_id,),
                ).fetchone()
                if _ident:
                    _run_user_id = _ident["user_id"]
                    _run_agent_id = _ident["agent_id"]
                cur = conn.execute(
                    """UPDATE session_runs SET
                         status=?,
                         error=CASE
                           WHEN ?='complete' THEN NULL
                           ELSE COALESCE(?, error)
                         END,
                         stop_cause=CASE
                           WHEN stop_cause IN ('user_stop','replaced','needs_manual_resume')
                             THEN stop_cause
                           ELSE COALESCE(?, stop_cause)
                         END,
                         owner_token=NULL,
                         lease_expires_at=NULL,
                         current_op=NULL,
                         updated_at=?
                       WHERE session_id=?
                         AND (? IS NULL OR owner_token=?)
                         AND (? IS NULL OR resume_attempts=?)""",
                    (
                        status, status, error, stop_cause, _iso_now(), session_id,
                        expected_owner_token, expected_owner_token,
                        expected_resume_attempts, expected_resume_attempts,
                    ),
                )
                updated = (cur.rowcount or 0) > 0
                conn.commit()
            finally:
                conn.close()
        # Run is over (any terminal status) — clear this agent's live status dot.
        if updated:
            await _emit_agent_run_status(_run_user_id, _run_agent_id, session_id, "idle")
            try:
                from app.agent.file_checkout import release_session_checkouts
                await release_session_checkouts(session_id, "Run reached terminal state")
            except Exception:
                logger.debug(
                    "file checkout terminal cleanup failed for %s",
                    session_id, exc_info=True,
                )
        return updated

    async def run_state_get(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Return the run-state row for a session, or None."""
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM session_runs WHERE session_id=?", (session_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    async def run_state_list_active(self, user_id: str) -> List[Dict[str, Any]]:
        """All sessions for a user with a currently-running turn."""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM session_runs WHERE user_id=? AND status='running'",
                (user_id,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    async def cleanup_orphaned_runs(self) -> int:
        """On boot, flip runs the previous process left mid-flight to a clean
        terminal state so no device hangs forever. Any session_runs row still
        'running' becomes 'interrupted', and any assistant interaction still
        'streaming' becomes 'interrupted'. Returns the number of runs reset."""
        async with self._write_lock:
            conn = self._get_conn()
            try:
                cur = conn.execute(
                    "UPDATE session_runs SET status='interrupted', "
                    "error='Server restarted while this run was in progress.', "
                    "updated_at=? WHERE status='running'",
                    (_now_iso(),),
                )
                n = cur.rowcount or 0
                conn.execute(
                    "UPDATE interactions SET status='interrupted' WHERE status='streaming'"
                )
                # Gate-queued turns exist only in this process's memory: after a
                # restart nothing will ever wake them, so a durable 'queued' user
                # row must not dangle behind a dead Force-run button. Flip it to
                # interrupted (the message shows as a normal stopped turn; the
                # user can resend it). Same policy as the streaming flip above.
                conn.execute(
                    "UPDATE interactions SET status='interrupted' WHERE status='queued'"
                )
                conn.commit()
                return n
            finally:
                conn.close()

    async def flip_queued_interactions_to_interrupted(self) -> int:
        """Finalize user messages still parked in the gate queue.

        The kill switch uses this AFTER cancelling every run: a 'queued'
        interaction (session-gate wait or compaction queue) had its turn
        cancelled before it ever began, so the durable row must not dangle
        behind a dead queue entry — flip it to interrupted (the message shows
        as a normal stopped turn, the user can resend). Mirrors the boot
        cleanup's queued-flip in cleanup_orphaned_runs. Returns the number of
        rows finalised."""
        async with self._write_lock:
            conn = self._get_conn()
            try:
                cur = conn.execute(
                    "UPDATE interactions SET status='interrupted' WHERE status='queued'"
                )
                n = cur.rowcount or 0
                conn.commit()
                return n
            finally:
                conn.close()

    # ── Self-healing / auto-resume helpers ────────────────────────────────
    # Resumable causes are involuntary infrastructure failures only. Contract,
    # no-work, and audit outcomes wait for a new user turn rather than replaying
    # the exhausted turn through the recovery ladder.
    _RESUMABLE_CAUSES = (
        "server_restart", "zombie", "frozen", "crash", "empty_response",
    )

    async def run_state_session_tool_names(self, session_id: str) -> List[str]:
        """Distinct tool names invoked anywhere in this session. Used by the
        resume opt-out: if a run touched a tool flagged non-auto-resumable, the
        run is held for one-click manual resume instead of auto-resumed."""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT DISTINCT tool_name FROM interactions "
                "WHERE session_id=? AND role='tool' AND tool_name IS NOT NULL",
                (session_id,),
            ).fetchall()
            return [r["tool_name"] for r in rows if r["tool_name"]]
        finally:
            conn.close()

    async def run_state_set_cause(self, session_id: str, stop_cause: str) -> None:
        """Tag WHY a run is stopping, while it is still live, so the loop's
        terminal finish does not have to guess intent. Used by the user-Stop
        and replace paths to record 'user_stop' / 'replaced' (never resumed)."""
        async with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    "UPDATE session_runs SET stop_cause=?, updated_at=? WHERE session_id=?",
                    (stop_cause, _iso_now(), session_id),
                )
                conn.commit()
            finally:
                conn.close()

    async def run_state_heartbeat(
        self, session_id: str, owner_token: Optional[str] = None, lease_seconds: float = 120.0
    ) -> None:
        """Best-effort liveness ping from the running loop. Advances heartbeat_at
        and refreshes the lease. Only touches rows that are still 'running'.
        If owner_token is given, also (re)claims ownership for this task."""
        now = _iso_now()
        lease_exp = _iso_in(lease_seconds)
        async with self._write_lock:
            conn = self._get_conn()
            try:
                if owner_token is not None:
                    conn.execute(
                        "UPDATE session_runs SET heartbeat_at=?, lease_expires_at=?, "
                        "owner_token=?, updated_at=? WHERE session_id=? AND status='running'",
                        (now, lease_exp, owner_token, now, session_id),
                    )
                else:
                    conn.execute(
                        "UPDATE session_runs SET heartbeat_at=?, lease_expires_at=?, "
                        "updated_at=? WHERE session_id=? AND status='running'",
                        (now, lease_exp, now, session_id),
                    )
                conn.commit()
            finally:
                conn.close()
        try:
            from app.agent.file_checkout import heartbeat_file_checkouts
            await heartbeat_file_checkouts(session_id)
        except Exception:
            logger.debug("file checkout heartbeat failed for %s", session_id, exc_info=True)

    async def run_state_list_active_all(self) -> List[Dict[str, Any]]:
        """Every session with a currently-'running' turn, across all users.
        The liveness watchdog scans these to find frozen / zombie runs."""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM session_runs WHERE status='running'"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    async def run_state_list_resumable(self, now_iso: Optional[str] = None) -> List[Dict[str, Any]]:
        """Runs eligible for auto-resume: stopped with a resumable cause, past
        their backoff gate, and not a throwaway/sandbox/optimizer session.
        Eligibility is also re-checked by the runner before each resume."""
        now = now_iso or _iso_now()
        placeholders = ",".join("?" for _ in self._RESUMABLE_CAUSES)
        conn = self._get_conn()
        try:
            rows = conn.execute(
                f"""SELECT * FROM session_runs
                     WHERE status NOT IN ('running','complete')
                       AND stop_cause IN ({placeholders})
                       AND (next_resume_at IS NULL OR next_resume_at <= ?)
                     ORDER BY started_at ASC""",
                (*self._RESUMABLE_CAUSES, now),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    async def run_state_claim_for_resume(
        self, session_id: str, owner_token: str, lease_seconds: float,
        backoff_seconds: float, effective_max: int,
        cause_counts_json: Optional[str] = None,
    ) -> bool:
        """Atomically claim a stopped run for resume. Succeeds (returns True) only
        if it is not already running, the lease is free or stale, and the retry
        budget is not exhausted. On success: flips to 'running', increments
        resume_attempts, refreshes the per-error-type tally (``cause_counts_json``),
        clears stop_cause, stamps heartbeat, takes the lease, and sets
        next_resume_at to the NEXT backoff gate. The rowcount guard makes this safe
        even across processes."""
        now = _iso_now()
        lease_exp = _iso_in(lease_seconds)
        next_resume = _iso_in(backoff_seconds)
        async with self._write_lock:
            conn = self._get_conn()
            try:
                cur = conn.execute(
                    """UPDATE session_runs SET
                         status='running',
                         resume_attempts = resume_attempts + 1,
                         resume_cause_counts = COALESCE(?, resume_cause_counts),
                         stop_cause = NULL,
                         error = NULL,
                         current_op = NULL,
                         heartbeat_at = ?,
                         owner_token = ?,
                         lease_expires_at = ?,
                         next_resume_at = ?,
                         updated_at = ?
                       WHERE session_id = ?
                         AND status != 'running'
                         AND (owner_token IS NULL OR lease_expires_at < ?)
                         AND resume_attempts < ?""",
                    (cause_counts_json, now, owner_token, lease_exp, next_resume, now,
                     session_id, now, effective_max),
                )
                conn.commit()
                return (cur.rowcount or 0) > 0
            finally:
                conn.close()

    async def run_state_mark_failed(self, session_id: str, error: Optional[str] = None) -> None:
        """Terminal: retry budget exhausted (or unrecoverable). status='error',
        stop_cause='failed' so it is never auto-resumed again."""
        async with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    "UPDATE session_runs SET status='error', stop_cause='failed', "
                    "error=COALESCE(?, error), owner_token=NULL, lease_expires_at=NULL, "
                    "updated_at=? WHERE session_id=?",
                    (error, _iso_now(), session_id),
                )
                conn.commit()
            finally:
                conn.close()

    async def run_state_mark_manual(self, session_id: str, error: Optional[str] = None) -> None:
        """Mark a run as awaiting a human one-click resume (the opt-out path):
        status='interrupted', stop_cause='needs_manual_resume' (never auto)."""
        async with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    "UPDATE session_runs SET status='interrupted', "
                    "stop_cause='needs_manual_resume', error=COALESCE(?, error), "
                    "owner_token=NULL, lease_expires_at=NULL, updated_at=? WHERE session_id=?",
                    (error, _iso_now(), session_id),
                )
                conn.commit()
            finally:
                conn.close()

    async def run_state_defer_resume(self, session_id: str, backoff_seconds: float,
                                     note: Optional[str] = None) -> None:
        """Defer a stopped run and charge one bounded recovery attempt.

        Provider-probe failures use this path. Charging them prevents a
        permanently unavailable provider from re-arming a run forever.
        No-op on a live run."""
        now = _iso_now()
        next_resume = _iso_in(backoff_seconds)
        async with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    """UPDATE session_runs SET
                         next_resume_at = ?,
                         resume_attempts = resume_attempts + 1,
                         error = CASE WHEN ? IS NOT NULL THEN ? ELSE error END,
                         updated_at = ?
                       WHERE session_id = ? AND status != 'running'""",
                    (next_resume, note, note, now, session_id),
                )
                conn.commit()
            finally:
                conn.close()

    async def run_state_tag_resumable_as_user_stop(self) -> int:
        """Kill-switch sweep: tag every not-running resumable run as user_stop.

        ``cancel_all`` only tags LIVE tasks; a run parked in retry-backoff —
        or one whose backoff gate has just elapsed and is awaiting the next
        watchdog tick — is not a live task and would survive the kill, then be
        re-ignited by the watchdog / boot orphan-resume after disengage.
        Tagging them user_stop (excluded from _RESUMABLE_CAUSES) makes the
        kill complete: the retry ladder can never resume after the switch is
        off. Returns the number of runs tagged."""
        async with self._write_lock:
            conn = self._get_conn()
            try:
                placeholders = ",".join("?" for _ in self._RESUMABLE_CAUSES)
                cur = conn.execute(
                    f"""UPDATE session_runs SET stop_cause='user_stop', updated_at=?
                        WHERE status NOT IN ('running','complete')
                          AND stop_cause IN ({placeholders})""",
                    (_iso_now(), *self._RESUMABLE_CAUSES),
                )
                conn.commit()
                return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            finally:
                conn.close()

    async def mark_orphans_for_resume(self) -> int:
        """Boot recovery (supersedes cleanup_orphaned_runs). Any run the previous
        process left 'running' is flipped to 'interrupted' with
        stop_cause='server_restart' so it is a resume candidate (the actual
        eligibility — origin / throwaway / budget — is enforced by
        run_state_list_resumable and the runner). Streaming assistant rows are
        flipped to 'interrupted' as before. Returns the number of runs reset."""
        async with self._write_lock:
            conn = self._get_conn()
            try:
                cur = conn.execute(
                    "UPDATE session_runs SET status='interrupted', "
                    "stop_cause='server_restart', "
                    "error='Server restarted while this run was in progress.', "
                    "owner_token=NULL, lease_expires_at=NULL, updated_at=? "
                    "WHERE status='running'",
                    (_iso_now(),),
                )
                n = cur.rowcount or 0
                conn.execute(
                    "UPDATE interactions SET status='interrupted' WHERE status='streaming'"
                )
                conn.commit()
                return n
            finally:
                conn.close()

    async def check_interrupt(self, session_id: str) -> bool:
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT interrupt_requested, created_at "
                "FROM session_interrupts WHERE session_id = ?",
                (session_id,)
            ).fetchone()
            if not row:
                return False
            try:
                created_at = datetime.fromisoformat(
                    str(row["created_at"]).replace("Z", "+00:00")
                )
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) - created_at > timedelta(hours=24):
                    conn.execute(
                        "DELETE FROM session_interrupts WHERE session_id = ?",
                        (session_id,),
                    )
                    conn.commit()
                    return False
            except (TypeError, ValueError):
                conn.execute(
                    "DELETE FROM session_interrupts WHERE session_id = ?",
                    (session_id,),
                )
                conn.commit()
                return False
            return bool(row["interrupt_requested"])
        except Exception as e:
            logger.error("Error checking interrupt for %s: %s", session_id, e)
            return False
        finally:
            conn.close()

    # ---- Webhook Registrations ----

    async def register_webhook(
        self,
        user_id: str,
        name: str,
        instructions: str = "",
    ) -> dict:
        """Create a generic inbound webhook registration."""
        from datetime import datetime, timezone
        webhook_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        conn = self._get_conn()
        try:
            conn.execute(
                """INSERT INTO webhook_registrations (id, user_id, name, instructions, active, created_at, updated_at)
                   VALUES (?, ?, ?, ?, 1, ?, ?)""",
                (webhook_id, user_id, name, instructions, now, now),
            )
            conn.commit()
            return {
                "id": webhook_id,
                "user_id": user_id,
                "name": name,
                "instructions": instructions,
                "active": True,
                "created_at": now,
            }
        finally:
            conn.close()

    async def get_webhook(self, webhook_id: str) -> Optional[dict]:
        """Get a webhook registration by id."""
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM webhook_registrations WHERE id = ?", (webhook_id,)
            ).fetchone()
            if not row:
                return None
            d = dict(row)
            d["active"] = bool(d["active"])
            return d
        finally:
            conn.close()

    async def list_webhooks(self, user_id: str, bin_view: bool = False) -> List[dict]:
        """List webhook registrations for a user. By default only active
        (non-recycled) rows; pass bin_view=True for only rows in the recycling
        bin (deleted_at set)."""
        conn = self._get_conn()
        try:
            cond = "deleted_at IS NOT NULL" if bin_view else "deleted_at IS NULL"
            rows = conn.execute(
                f"SELECT * FROM webhook_registrations WHERE user_id = ? AND {cond} "
                "ORDER BY created_at DESC",
                (user_id,),
            ).fetchall()
            result = []
            for r in rows:
                d = dict(r)
                d["active"] = bool(d["active"])
                result.append(d)
            return result
        finally:
            conn.close()

    async def update_webhook(
        self, webhook_id: str, user_id: str, **fields
    ) -> Optional[dict]:
        """Update a webhook registration (scoped to user_id)."""
        allowed = {"name", "instructions", "active"}
        sets, params = [], []
        for k, v in fields.items():
            if k not in allowed:
                continue
            if k == "active":
                v = 1 if v else 0
            sets.append(f"{k} = ?")
            params.append(v)
        if not sets:
            return await self.get_webhook(webhook_id)
        params.append(webhook_id)
        params.append(user_id)
        conn = self._get_conn()
        try:
            conn.execute(
                f"UPDATE webhook_registrations SET {', '.join(sets)} WHERE id = ? AND user_id = ?",
                params,
            )
            conn.commit()
            return await self.get_webhook(webhook_id)
        finally:
            conn.close()

    async def delete_webhook(self, webhook_id: str, user_id: str) -> bool:
        """Delete a webhook registration (scoped to user_id)."""
        conn = self._get_conn()
        try:
            cur = conn.execute(
                "DELETE FROM webhook_registrations WHERE id = ? AND user_id = ?",
                (webhook_id, user_id),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    async def trash_webhook(self, webhook_id: str, user_id: str) -> bool:
        """Soft-delete a webhook into the recycling bin. active=0 stops inbound
        delivery (the receiver only fires active webhooks)."""
        ts = _now_iso()
        conn = self._get_conn()
        try:
            cur = conn.execute(
                "UPDATE webhook_registrations SET deleted_at = ?, active = 0, updated_at = ? "
                "WHERE id = ? AND user_id = ? AND deleted_at IS NULL",
                (ts, ts, webhook_id, user_id),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    async def restore_webhook(self, webhook_id: str, user_id: str) -> bool:
        """Restore a binned webhook back to active (and re-activate delivery)."""
        conn = self._get_conn()
        try:
            cur = conn.execute(
                "UPDATE webhook_registrations SET deleted_at = NULL, active = 1, updated_at = ? "
                "WHERE id = ? AND user_id = ? AND deleted_at IS NOT NULL",
                (_now_iso(), webhook_id, user_id),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    async def log_webhook_event(
        self,
        webhook_id: str,
        method: str,
        headers: str,
        payload: str,
        response_status: int,
        response_body: str,
        duration_ms: int,
    ) -> str:
        """Log an incoming webhook event. Returns the event id."""
        event_id = str(uuid.uuid4())
        conn = self._get_conn()
        try:
            conn.execute(
                """INSERT INTO webhook_event_log (id, webhook_id, method, headers, payload,
                   response_status, response_body, duration_ms)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (event_id, webhook_id, method, headers, payload,
                 response_status, response_body, duration_ms),
            )
            conn.commit()
            return event_id
        finally:
            conn.close()

    async def get_webhook_logs(
        self, webhook_id: str, limit: int = 20
    ) -> List[dict]:
        """Get recent webhook events for a registration."""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM webhook_event_log WHERE webhook_id = ? ORDER BY created_at DESC LIMIT ?",
                (webhook_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ---- Auth Elements ----

    @turn_cached
    async def _auth_elements_for_user(self, user_id: str) -> Dict[tuple, dict]:
        """ALL of a user's auth_elements, indexed by ``(service, label)`` and loaded
        in ONE query, cached for the turn.

        The chat hot path resolves many DISTINCT credentials per turn — the LLM key
        PLUS one per connected integration tool — and each used to be its own remote
        round-trip (~150ms). Because they have different (service, label) keys the
        per-method turn cache can't share them; pulling the whole set in a single
        query collapses those N reads to one. Turn-invariant: credentials are not
        rewritten mid-chat-turn (writes happen in separate admin requests).

        With the three-vault split, queries all three vaults via UNION ALL."""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM vault_app.auth_elements WHERE user_id = ? "
                "UNION ALL "
                "SELECT * FROM vault_agent.auth_elements WHERE user_id = ? "
                "UNION ALL "
                "SELECT * FROM vault_user.auth_elements WHERE user_id = ?",
                (user_id, user_id, user_id),
            ).fetchall()
            return {(r["service"], r["label"]): dict(r) for r in rows}
        finally:
            conn.close()

    @turn_cached
    async def auth_element_get(
        self, user_id: str, service: str, label: str = "default"
    ) -> Optional[dict]:
        # Inside a chat turn, serve from the one-shot per-user bulk load so the many
        # DISTINCT credential reads in a turn collapse to a single query. Every other
        # caller (admin endpoints, background jobs) keeps the plain point read.
        if turn_scope_active():
            allrows = await self._auth_elements_for_user(user_id)
            return allrows.get((service, label))
        schema = _vault_for(user_id, service, label)
        conn = self._get_conn()
        try:
            row = conn.execute(
                f"SELECT * FROM {schema}.auth_elements WHERE user_id = ? AND service = ? AND label = ?",
                (user_id, service, label),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    async def auth_element_set(
        self,
        user_id: str,
        service: str,
        config: dict,
        secret_ref: str = "",
        label: str = "default",
    ) -> dict:
        import uuid
        schema = _vault_for(user_id, service, label)
        conn = self._get_conn()
        try:
            existing = conn.execute(
                f"SELECT id, _version FROM {schema}.auth_elements WHERE user_id = ? AND service = ? AND label = ?",
                (user_id, service, label),
            ).fetchone()
            now = _now_iso()
            config_json = __import__('json').dumps(config)
            if existing:
                new_version = (existing["_version"] or 0) + 1
                conn.execute(
                    f"UPDATE {schema}.auth_elements SET config = ?, secret_ref = ?, updated_at = ?, _version = ? WHERE id = ?",
                    (config_json, secret_ref, now, new_version, existing["id"]),
                )
                row_id = existing["id"]
            else:
                new_version = 1
                row_id = str(uuid.uuid4())
                conn.execute(
                    f"INSERT INTO {schema}.auth_elements (id, user_id, service, label, config, secret_ref, created_at, updated_at, _version, _schema_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (row_id, user_id, service, label, config_json, secret_ref, now, now, new_version, AUTH_ELEMENTS_SCHEMA_VERSION),
                )
            conn.commit()
            # If this write happens inside a chat turn (e.g. a tool refreshing an
            # OAuth token), drop the turn-cached credential reads so a later read in
            # the same turn sees the new value instead of the pre-write snapshot.
            turn_cache_invalidate("auth_element_get", "_auth_elements_for_user")
            return {
                "id": row_id,
                "user_id": user_id,
                "service": service,
                "label": label,
                "config": config_json,
                "secret_ref": secret_ref,
                "is_active": 1,
                "_version": new_version,
                "_schema_version": AUTH_ELEMENTS_SCHEMA_VERSION,
            }
        finally:
            conn.close()

    async def find_user_by_oauth_account(
        self,
        service: str,
        email_or_account: str,
    ) -> Optional[str]:
        """Return user_id whose auth_elements row for service+OAuth-label has the
        given email/account in its config JSON. Used by inbound event sources
        (Gmail Pub/Sub, Graph notifications) to map provider events back to
        a WebAgent user. Matches any per-agent OAuth label (``oauth:<agent_id>``)
        for the user. None if no match.

        All OAuth tokens live in vault_agent."""
        if not email_or_account:
            return None
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT user_id, config FROM vault_agent.auth_elements "
                "WHERE service = ? AND label LIKE 'oauth:%'",
                (service,),
            ).fetchall()
            target = email_or_account.strip().lower()
            for r in rows:
                raw = r["config"] if "config" in r.keys() else None
                if not raw:
                    continue
                try:
                    cfg = json.loads(raw)
                except Exception:
                    continue
                acct = (cfg.get("email") or cfg.get("account") or cfg.get("name") or "").strip().lower()
                if acct == target:
                    return r["user_id"]
            return None
        finally:
            conn.close()

    async def auth_element_list(
        self, user_id: str, service: Optional[str] = None
    ) -> List[dict]:
        """List auth_elements for a user, optionally filtered by service.

        When ``service`` is given we query only the vault that owns that service
        type. When ``service`` is None (a full dump used by admin config pages
        and agent setup) we UNION across all three vaults, since a single user
        may have rows in vault_app, vault_agent, and vault_user.
        """
        conn = self._get_conn()
        try:
            if service:
                # Targeted query — hit the single vault that owns this service
                schema = _vault_for(user_id, service, "")
                rows = conn.execute(
                    f"SELECT * FROM {schema}.auth_elements "
                    "WHERE user_id = ? AND service = ? ORDER BY created_at",
                    (user_id, service),
                ).fetchall()
            else:
                # Full dump — UNION across all three vaults
                rows = conn.execute(
                    "SELECT * FROM vault_app.auth_elements WHERE user_id = ? "
                    "UNION ALL "
                    "SELECT * FROM vault_agent.auth_elements WHERE user_id = ? "
                    "UNION ALL "
                    "SELECT * FROM vault_user.auth_elements WHERE user_id = ? "
                    "ORDER BY service, label",
                    (user_id, user_id, user_id),
                ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    async def auth_element_delete(
        self, user_id: str, service: str, label: str = "default"
    ) -> bool:
        schema = _vault_for(user_id, service, label)
        conn = self._get_conn()
        try:
            cur = conn.execute(
                f"DELETE FROM {schema}.auth_elements WHERE user_id = ? AND service = ? AND label = ?",
                (user_id, service, label),
            )
            conn.commit()
            turn_cache_invalidate("auth_element_get", "_auth_elements_for_user")
            return cur.rowcount > 0
        finally:
            conn.close()


    # ──────────────────────────────────────────────────────────────────────────
    # User Profiles
    # ──────────────────────────────────────────────────────────────────────────

    # App-plane experience tiers, model rosters, and assignments

    _MODEL_ROSTER_FIELDS = frozenset({
        "slug", "name", "description", "entries_json", "default_entry_id",
        "status", "revision", "source", "created_by", "updated_by",
        "published_at", "published_revision",
    })
    _EXPERIENCE_TIER_FIELDS = frozenset({
        "slug", "name", "description", "policy_json", "policy_schema_version",
        "roster_id", "is_system", "is_locked", "status", "revision",
        "created_by", "updated_by", "published_at", "published_revision",
    })
    _ASSIGNMENT_FIELDS = frozenset({
        "user_id", "tier_id", "source", "starts_at", "expires_at",
        "assigned_by", "reason",
    })

    @staticmethod
    def _entitlement_json(value, fallback):
        if value is None:
            value = fallback
        if isinstance(value, str):
            json.loads(value)  # validate before persisting
            return value
        return json.dumps(value, separators=(",", ":"), sort_keys=True)

    async def get_model_roster(self, roster_id: str) -> Optional[dict]:
        conn = self._get_conn()
        try:
            row = conn.execute("SELECT * FROM model_rosters WHERE id=?", (roster_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    async def get_model_roster_by_slug(self, slug: str) -> Optional[dict]:
        conn = self._get_conn()
        try:
            row = conn.execute("SELECT * FROM model_rosters WHERE slug=?", (slug,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    async def list_model_rosters(self, status: Optional[str] = None) -> List[dict]:
        conn = self._get_conn()
        try:
            sql, params = "SELECT * FROM model_rosters", ()
            if status:
                sql, params = sql + " WHERE status=?", (status,)
            rows = conn.execute(
                sql + " ORDER BY CASE lower(slug) "
                "WHEN 'base' THEN 0 "
                "WHEN 'admin' THEN 0 "
                "WHEN 'anonymous' THEN 1 "
                "WHEN 'anon' THEN 1 "
                "WHEN 'pending' THEN 2 "
                "WHEN 'free' THEN 3 "
                "ELSE 4 END, lower(name), lower(slug), id",
                params,
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    async def get_published_model_roster(self, roster_id: str) -> Optional[dict]:
        """Return the immutable live snapshot, never unpublished draft fields."""
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM model_rosters WHERE id=?", (roster_id,)
            ).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT * FROM model_rosters WHERE slug=?", (roster_id,)
                ).fetchone()
            if not row or str(row["status"] or "") != "published":
                return None
            current = dict(row)
            published_revision = current.get("published_revision")
            if published_revision is None:
                # Compatibility for a database opened before the backfill could
                # complete.  The legacy published row is itself the live value.
                return current
            snapshot = conn.execute(
                "SELECT * FROM model_roster_revisions WHERE roster_id=? AND revision=?",
                (current["id"], int(published_revision)),
            ).fetchone()
            if not snapshot:
                return None
            payload = json.loads(snapshot["payload_json"] or "{}")
            live = dict(current)
            live.update(payload if isinstance(payload, dict) else {})
            live["revision"] = int(snapshot["revision"])
            live["published_revision"] = int(snapshot["revision"])
            live["status"] = "published"
            live["published_at"] = snapshot["created_at"]
            return live
        finally:
            conn.close()

    async def list_model_roster_revisions(self, roster_id: str) -> List[dict]:
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM model_roster_revisions WHERE roster_id=? ORDER BY revision DESC",
                (roster_id,),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    async def get_model_roster_revision(self, roster_id: str, revision: int) -> Optional[dict]:
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM model_roster_revisions WHERE roster_id=? AND revision=?",
                (roster_id, int(revision)),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    async def publish_model_roster(
        self, roster_id: str, *, actor_user_id: Optional[str] = None,
        expected_revision: Optional[int] = None, action: str = "published",
    ) -> dict:
        """Atomically snapshot the working draft and advance the live pointer."""
        async with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                raw = conn.execute("SELECT * FROM model_rosters WHERE id=?", (roster_id,)).fetchone()
                if not raw:
                    raise KeyError(roster_id)
                row = dict(raw)
                revision = max(1, int(row.get("revision") or 1))
                if expected_revision is not None and revision != int(expected_revision):
                    raise ValueError("model roster revision conflict")
                occupied = conn.execute(
                    "SELECT 1 FROM model_roster_revisions WHERE roster_id=? AND revision=?",
                    (roster_id, revision),
                ).fetchone()
                if occupied:
                    maximum = conn.execute(
                        "SELECT COALESCE(MAX(revision),0) FROM model_roster_revisions WHERE roster_id=?",
                        (roster_id,),
                    ).fetchone()[0]
                    revision = max(revision, int(maximum or 0)) + 1
                payload = {
                    key: row.get(key) for key in (
                        "slug", "name", "description", "entries_json",
                        "default_entry_id", "source",
                    )
                }
                now = _now_iso()
                conn.execute(
                    """INSERT INTO model_roster_revisions
                       (roster_id, revision, payload_json, action, created_by, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (roster_id, revision,
                     json.dumps(payload, separators=(",", ":"), sort_keys=True),
                     str(action or "published"), actor_user_id, now),
                )
                conn.execute(
                    """UPDATE model_rosters
                       SET status='published', revision=?, published_revision=?,
                           published_at=?, updated_at=?, updated_by=? WHERE id=?""",
                    (revision, revision, now, now, actor_user_id, roster_id),
                )
                conn.commit()
                return dict(conn.execute(
                    "SELECT * FROM model_rosters WHERE id=?", (roster_id,)
                ).fetchone())
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    async def rollback_model_roster(
        self, roster_id: str, revision: int, *, actor_user_id: Optional[str] = None,
    ) -> dict:
        """Republish an old immutable payload as a new immutable revision."""
        async with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                current_raw = conn.execute(
                    "SELECT * FROM model_rosters WHERE id=?", (roster_id,)
                ).fetchone()
                snapshot = conn.execute(
                    "SELECT * FROM model_roster_revisions WHERE roster_id=? AND revision=?",
                    (roster_id, int(revision)),
                ).fetchone()
                if not current_raw or not snapshot:
                    raise KeyError((roster_id, revision))
                payload = json.loads(snapshot["payload_json"] or "{}")
                maximum = conn.execute(
                    "SELECT COALESCE(MAX(revision),0) FROM model_roster_revisions WHERE roster_id=?",
                    (roster_id,),
                ).fetchone()[0]
                new_revision = max(int(current_raw["revision"] or 1), int(maximum or 0)) + 1
                now = _now_iso()
                conn.execute(
                    """INSERT INTO model_roster_revisions
                       (roster_id, revision, payload_json, action, created_by, created_at)
                       VALUES (?, ?, ?, 'rollback', ?, ?)""",
                    (roster_id, new_revision,
                     json.dumps(payload, separators=(",", ":"), sort_keys=True),
                     actor_user_id, now),
                )
                editable = {key: payload.get(key) for key in (
                    "slug", "name", "description", "entries_json",
                    "default_entry_id", "source",
                )}
                conn.execute(
                    """UPDATE model_rosters SET slug=?, name=?, description=?, entries_json=?,
                       default_entry_id=?, source=?, status='published', revision=?,
                       published_revision=?, published_at=?, updated_at=?, updated_by=? WHERE id=?""",
                    (editable["slug"], editable["name"], editable["description"],
                     editable["entries_json"], editable["default_entry_id"], editable["source"],
                     new_revision, new_revision, now, now, actor_user_id, roster_id),
                )
                conn.commit()
                return dict(conn.execute(
                    "SELECT * FROM model_rosters WHERE id=?", (roster_id,)
                ).fetchone())
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    async def claim_model_roster(self, roster_id: str, *, actor_user_id: Optional[str] = None) -> dict:
        """Mark a roster administrator-owned without creating a metadata draft."""
        async with self._write_lock:
            conn = self._get_conn()
            try:
                now = _now_iso()
                conn.execute(
                    "UPDATE model_rosters SET source='admin', updated_by=?, updated_at=? WHERE id=?",
                    (actor_user_id, now, roster_id),
                )
                conn.commit()
                row = conn.execute("SELECT * FROM model_rosters WHERE id=?", (roster_id,)).fetchone()
                if not row:
                    raise KeyError(roster_id)
                return dict(row)
            finally:
                conn.close()

    async def upsert_model_roster(self, roster_id: str, **fields) -> dict:
        unknown = set(fields) - self._MODEL_ROSTER_FIELDS
        if unknown:
            raise ValueError(f"Unknown model roster fields: {sorted(unknown)}")
        if "entries_json" in fields:
            fields["entries_json"] = self._entitlement_json(fields["entries_json"], [])
        async with self._write_lock:
            conn = self._get_conn()
            try:
                existing = conn.execute("SELECT 1 FROM model_rosters WHERE id=?", (roster_id,)).fetchone()
                now = _now_iso()
                if existing:
                    updates = dict(fields)
                    if "revision" not in updates:
                        current = conn.execute("SELECT revision FROM model_rosters WHERE id=?", (roster_id,)).fetchone()
                        updates["revision"] = int(current[0]) + 1
                    if updates:
                        sets = ", ".join(f"{key}=?" for key in updates)
                        conn.execute(f"UPDATE model_rosters SET {sets}, updated_at=? WHERE id=?",
                                     (*updates.values(), now, roster_id))
                else:
                    if not fields.get("slug") or not fields.get("name"):
                        raise ValueError("New model roster requires slug and name")
                    columns = ["id", *fields, "created_at", "updated_at"]
                    conn.execute(
                        f"INSERT INTO model_rosters ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
                        (roster_id, *fields.values(), now, now),
                    )
                conn.commit()
                return dict(conn.execute("SELECT * FROM model_rosters WHERE id=?", (roster_id,)).fetchone())
            finally:
                conn.close()

    async def delete_model_roster(self, roster_id: str) -> bool:
        async with self._write_lock:
            conn = self._get_conn()
            try:
                cur = conn.execute("DELETE FROM model_rosters WHERE id=?", (roster_id,))
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    async def get_experience_tier(self, tier_id: str) -> Optional[dict]:
        conn = self._get_conn()
        try:
            row = conn.execute("SELECT * FROM experience_tiers WHERE id=?", (tier_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    async def get_experience_tier_by_slug(self, slug: str) -> Optional[dict]:
        conn = self._get_conn()
        try:
            row = conn.execute("SELECT * FROM experience_tiers WHERE slug=?", (slug,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    async def list_experience_tiers(self, status: Optional[str] = None) -> List[dict]:
        conn = self._get_conn()
        try:
            sql, params = "SELECT * FROM experience_tiers", ()
            if status:
                sql, params = sql + " WHERE status=?", (status,)
            rows = conn.execute(
                sql + " ORDER BY CASE lower(slug) "
                "WHEN 'base' THEN 0 "
                "WHEN 'admin' THEN 0 "
                "WHEN 'anonymous' THEN 1 "
                "WHEN 'anon' THEN 1 "
                "WHEN 'pending' THEN 2 "
                "WHEN 'free' THEN 3 "
                "ELSE 4 END, lower(name), lower(slug), id",
                params,
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    async def get_published_experience_tier(self, tier_id: str) -> Optional[dict]:
        """Return the immutable live policy, never unpublished working fields."""
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM experience_tiers WHERE id=? OR slug=? ORDER BY id=? DESC LIMIT 1",
                (tier_id, tier_id, tier_id),
            ).fetchone()
            if not row or str(row["status"] or "") != "published":
                return None
            current = dict(row)
            published_revision = current.get("published_revision")
            if published_revision is None:
                return current
            snapshot = conn.execute(
                "SELECT * FROM experience_tier_revisions WHERE tier_id=? AND revision=?",
                (current["id"], int(published_revision)),
            ).fetchone()
            if not snapshot:
                return None
            payload = json.loads(snapshot["payload_json"] or "{}")
            live = dict(current)
            live.update(payload if isinstance(payload, dict) else {})
            live["revision"] = int(snapshot["revision"])
            live["published_revision"] = int(snapshot["revision"])
            live["status"] = "published"
            live["published_at"] = snapshot["created_at"]
            return live
        finally:
            conn.close()

    async def list_experience_tier_revisions(self, tier_id: str) -> List[dict]:
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM experience_tier_revisions WHERE tier_id=? ORDER BY revision DESC",
                (tier_id,),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    async def get_experience_tier_revision(self, tier_id: str, revision: int) -> Optional[dict]:
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM experience_tier_revisions WHERE tier_id=? AND revision=?",
                (tier_id, int(revision)),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    async def publish_experience_tier(
        self, tier_id: str, *, actor_user_id: Optional[str] = None,
        expected_revision: Optional[int] = None, action: str = "published",
    ) -> dict:
        async with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                raw = conn.execute("SELECT * FROM experience_tiers WHERE id=?", (tier_id,)).fetchone()
                if not raw:
                    raise KeyError(tier_id)
                row = dict(raw)
                revision = max(1, int(row.get("revision") or 1))
                if expected_revision is not None and revision != int(expected_revision):
                    raise ValueError("experience tier revision conflict")
                occupied = conn.execute(
                    "SELECT 1 FROM experience_tier_revisions WHERE tier_id=? AND revision=?",
                    (tier_id, revision),
                ).fetchone()
                if occupied:
                    maximum = conn.execute(
                        "SELECT COALESCE(MAX(revision),0) FROM experience_tier_revisions WHERE tier_id=?",
                        (tier_id,),
                    ).fetchone()[0]
                    revision = max(revision, int(maximum or 0)) + 1
                payload = {key: row.get(key) for key in (
                    "slug", "name", "description", "policy_json",
                    "policy_schema_version", "roster_id", "is_system", "is_locked",
                )}
                now = _now_iso()
                conn.execute(
                    """INSERT INTO experience_tier_revisions
                       (tier_id, revision, payload_json, action, created_by, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (tier_id, revision, json.dumps(payload, separators=(",", ":"), sort_keys=True),
                     str(action or "published"), actor_user_id, now),
                )
                conn.execute(
                    """UPDATE experience_tiers
                       SET status='published', revision=?, published_revision=?,
                           published_at=?, updated_at=?, updated_by=? WHERE id=?""",
                    (revision, revision, now, now, actor_user_id, tier_id),
                )
                conn.commit()
                return dict(conn.execute(
                    "SELECT * FROM experience_tiers WHERE id=?", (tier_id,)
                ).fetchone())
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    async def rollback_experience_tier(
        self, tier_id: str, revision: int, *, actor_user_id: Optional[str] = None,
    ) -> dict:
        """Republish an old immutable policy as a new immutable revision."""
        async with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                current_raw = conn.execute(
                    "SELECT * FROM experience_tiers WHERE id=?", (tier_id,)
                ).fetchone()
                snapshot = conn.execute(
                    "SELECT * FROM experience_tier_revisions WHERE tier_id=? AND revision=?",
                    (tier_id, int(revision)),
                ).fetchone()
                if not current_raw or not snapshot:
                    raise KeyError((tier_id, revision))
                payload = json.loads(snapshot["payload_json"] or "{}")
                maximum = conn.execute(
                    "SELECT COALESCE(MAX(revision),0) FROM experience_tier_revisions WHERE tier_id=?",
                    (tier_id,),
                ).fetchone()[0]
                new_revision = max(int(current_raw["revision"] or 1), int(maximum or 0)) + 1
                now = _now_iso()
                conn.execute(
                    """INSERT INTO experience_tier_revisions
                       (tier_id, revision, payload_json, action, created_by, created_at)
                       VALUES (?, ?, ?, 'rollback', ?, ?)""",
                    (tier_id, new_revision,
                     json.dumps(payload, separators=(",", ":"), sort_keys=True), actor_user_id, now),
                )
                editable = {key: payload.get(key) for key in (
                    "slug", "name", "description", "policy_json",
                    "policy_schema_version", "roster_id", "is_system", "is_locked",
                )}
                conn.execute(
                    """UPDATE experience_tiers SET slug=?, name=?, description=?, policy_json=?,
                       policy_schema_version=?, roster_id=?, is_system=?, is_locked=?,
                       status='published', revision=?, published_revision=?, published_at=?,
                       updated_at=?, updated_by=? WHERE id=?""",
                    (editable["slug"], editable["name"], editable["description"],
                     editable["policy_json"], editable["policy_schema_version"],
                     editable["roster_id"], editable["is_system"], editable["is_locked"],
                     new_revision, new_revision, now, now, actor_user_id, tier_id),
                )
                conn.commit()
                return dict(conn.execute(
                    "SELECT * FROM experience_tiers WHERE id=?", (tier_id,)
                ).fetchone())
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    async def upsert_experience_tier(
        self, tier_id: str, *, expected_revision: Optional[int] = None, **fields,
    ) -> dict:
        unknown = set(fields) - self._EXPERIENCE_TIER_FIELDS
        if unknown:
            raise ValueError(f"Unknown experience tier fields: {sorted(unknown)}")
        if "policy_json" in fields:
            fields["policy_json"] = self._entitlement_json(fields["policy_json"], {})
        for flag in ("is_system", "is_locked"):
            if flag in fields:
                fields[flag] = 1 if fields[flag] else 0
        async with self._write_lock:
            conn = self._get_conn()
            try:
                existing = conn.execute("SELECT revision FROM experience_tiers WHERE id=?", (tier_id,)).fetchone()
                now = _now_iso()
                if existing:
                    if expected_revision is not None and int(existing[0] or 1) != int(expected_revision):
                        raise ValueError("experience tier revision conflict")
                    updates = dict(fields)
                    if "revision" not in updates:
                        updates["revision"] = int(existing[0]) + 1
                    if updates:
                        sets = ", ".join(f"{key}=?" for key in updates)
                        conn.execute(f"UPDATE experience_tiers SET {sets}, updated_at=? WHERE id=?",
                                     (*updates.values(), now, tier_id))
                else:
                    if not fields.get("slug") or not fields.get("name"):
                        raise ValueError("New experience tier requires slug and name")
                    columns = ["id", *fields, "created_at", "updated_at"]
                    conn.execute(
                        f"INSERT INTO experience_tiers ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
                        (tier_id, *fields.values(), now, now),
                    )
                conn.commit()
                return dict(conn.execute("SELECT * FROM experience_tiers WHERE id=?", (tier_id,)).fetchone())
            finally:
                conn.close()

    async def delete_experience_tier(self, tier_id: str) -> bool:
        async with self._write_lock:
            conn = self._get_conn()
            try:
                cur = conn.execute("DELETE FROM experience_tiers WHERE id=?", (tier_id,))
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    async def get_user_tier_assignment(self, assignment_id: str) -> Optional[dict]:
        conn = self._get_conn()
        try:
            row = conn.execute("SELECT * FROM user_tier_assignments WHERE id=?", (assignment_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    async def list_user_tier_assignments(self, user_id: Optional[str] = None) -> List[dict]:
        conn = self._get_conn()
        try:
            sql, params = "SELECT * FROM user_tier_assignments", ()
            if user_id is not None:
                sql, params = sql + " WHERE user_id=?", (user_id,)
            rows = conn.execute(sql + " ORDER BY starts_at DESC, created_at DESC", params).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    async def upsert_user_tier_assignment(self, assignment_id: str, **fields) -> dict:
        unknown = set(fields) - self._ASSIGNMENT_FIELDS
        if unknown:
            raise ValueError(f"Unknown tier assignment fields: {sorted(unknown)}")
        async with self._write_lock:
            conn = self._get_conn()
            try:
                existing = conn.execute("SELECT 1 FROM user_tier_assignments WHERE id=?", (assignment_id,)).fetchone()
                now = _now_iso()
                if existing:
                    if fields:
                        sets = ", ".join(f"{key}=?" for key in fields)
                        conn.execute(f"UPDATE user_tier_assignments SET {sets}, updated_at=? WHERE id=?",
                                     (*fields.values(), now, assignment_id))
                else:
                    required = ("user_id", "tier_id", "source")
                    if any(not fields.get(key) for key in required):
                        raise ValueError("New tier assignment requires user_id, tier_id, and source")
                    columns = ["id", *fields, "created_at", "updated_at"]
                    conn.execute(
                        f"INSERT INTO user_tier_assignments ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
                        (assignment_id, *fields.values(), now, now),
                    )
                conn.commit()
                return dict(conn.execute("SELECT * FROM user_tier_assignments WHERE id=?", (assignment_id,)).fetchone())
            finally:
                conn.close()

    async def delete_user_tier_assignment(self, assignment_id: str) -> bool:
        async with self._write_lock:
            conn = self._get_conn()
            try:
                cur = conn.execute("DELETE FROM user_tier_assignments WHERE id=?", (assignment_id,))
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    async def append_entitlement_audit_event(
        self, event_id: str, *, action: str, entity_type: str,
        subject_user_id: Optional[str] = None, actor_user_id: Optional[str] = None,
        entity_id: Optional[str] = None, previous_json=None, new_json=None,
        reason: str = "", created_at: Optional[str] = None,
    ) -> dict:
        previous = None if previous_json is None else self._entitlement_json(previous_json, {})
        new = None if new_json is None else self._entitlement_json(new_json, {})
        async with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    "INSERT INTO entitlement_audit_events "
                    "(id,subject_user_id,actor_user_id,action,entity_type,entity_id,previous_json,new_json,reason,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (event_id, subject_user_id, actor_user_id, action, entity_type,
                     entity_id, previous, new, reason, created_at or _now_iso()),
                )
                conn.commit()
                return dict(conn.execute("SELECT * FROM entitlement_audit_events WHERE id=?", (event_id,)).fetchone())
            finally:
                conn.close()

    async def reset_tier_credit_allowances(
        self, *, scope_kind: str, principal_key: str, actor_user_id: str,
        reason: str, tier_id: Optional[str] = None,
    ) -> dict:
        """Atomically invalidate one subject's tier allowance(s), preserving history."""
        if scope_kind not in {"account", "network"} or not principal_key or not reason.strip():
            raise ValueError("scope, principal, and reset reason are required")
        async with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                where, params = "scope_kind=? AND principal_key=?", [scope_kind, principal_key]
                if tier_id:
                    where += " AND tier_id=?"; params.append(tier_id)
                previous = conn.execute(
                    f"SELECT * FROM tier_credit_allowances WHERE {where}", params
                ).fetchall()
                next_generation = max([int(row["reset_generation"] or 0) for row in previous] or [0]) + 1
                conn.execute(f"DELETE FROM tier_credit_allowances WHERE {where}", params)
                reset_id = str(uuid.uuid4())
                now = _now_iso()
                conn.execute(
                    "INSERT INTO tier_credit_resets (id,scope_kind,principal_key,tier_id,actor_user_id,reason,reset_generation,created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (reset_id, scope_kind, principal_key, tier_id, actor_user_id, reason.strip(), next_generation, now),
                )
                conn.commit()
                return {"id": reset_id, "scope_kind": scope_kind, "tier_id": tier_id,
                        "reset_generation": next_generation, "cleared_allowances": len(previous), "created_at": now}
            except Exception:
                conn.rollback(); raise
            finally:
                conn.close()

    async def list_entitlement_audit_events(
        self, *, subject_user_id: Optional[str] = None,
        entity_type: Optional[str] = None, entity_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[dict]:
        clauses, params = [], []
        if subject_user_id is not None:
            clauses.append("subject_user_id=?")
            params.append(subject_user_id)
        if entity_type is not None:
            clauses.append("entity_type=?")
            params.append(entity_type)
        if entity_id is not None:
            clauses.append("entity_id=?")
            params.append(entity_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                f"SELECT * FROM entitlement_audit_events{where} ORDER BY created_at DESC LIMIT ?",
                (*params, max(1, min(int(limit), 1000))),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    async def get_user_profile(self, user_id: str) -> Optional[dict]:
        """Return the user_profiles row for user_id, or None if not found."""
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM user_profiles WHERE user_id = ?", (user_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    @staticmethod
    def _mobile_agent_sidebar_snapshot(row) -> dict:
        """Normalize one persisted mobile-sidebar row into its public snapshot."""
        if not row:
            return {"agent_ids": [], "revision": 0, "configured": False}
        try:
            raw_ids = json.loads(row["favorite_agent_ids_json"] or "[]")
        except (TypeError, ValueError):
            raw_ids = []
        seen = set()
        agent_ids = []
        for value in raw_ids if isinstance(raw_ids, list) else []:
            if not isinstance(value, str) or not value or value in seen:
                continue
            seen.add(value)
            agent_ids.append(value)
        try:
            revision = max(0, int(row["revision"]))
        except (TypeError, ValueError):
            revision = 0
        return {"agent_ids": agent_ids, "revision": revision, "configured": True}

    async def get_user_agent_sidebar_preferences(self, user_id: str) -> dict:
        """Return the central, personal mobile Agents-sidebar preference snapshot."""
        conn = self._get_conn()
        try:
            row = conn.execute(
                """SELECT favorite_agent_ids_json, revision
                   FROM user_agent_sidebar_preferences WHERE user_id = ?""",
                (user_id,),
            ).fetchone()
            return self._mobile_agent_sidebar_snapshot(row)
        finally:
            conn.close()

    async def replace_user_agent_sidebar_preferences(
        self, user_id: str, favorite_agent_ids: List[str], expected_revision: int,
    ) -> dict:
        """Atomically replace this user's favorites or report an optimistic-lock conflict."""
        seen = set()
        normalized = []
        for value in favorite_agent_ids if isinstance(favorite_agent_ids, list) else []:
            if not isinstance(value, str) or not value or value in seen:
                continue
            seen.add(value)
            normalized.append(value)
            if len(normalized) >= 200:
                break
        try:
            expected = max(0, int(expected_revision))
        except (TypeError, ValueError):
            expected = 0

        async with self._write_lock:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    """SELECT favorite_agent_ids_json, revision
                       FROM user_agent_sidebar_preferences WHERE user_id = ?""",
                    (user_id,),
                ).fetchone()
                current = self._mobile_agent_sidebar_snapshot(row)
                if current["revision"] != expected:
                    return {"conflict": True, "snapshot": current}

                next_snapshot = {
                    "agent_ids": normalized,
                    "revision": current["revision"] + 1,
                    "configured": True,
                }
                conn.execute(
                    """INSERT INTO user_agent_sidebar_preferences
                       (user_id, favorite_agent_ids_json, revision, updated_at)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(user_id) DO UPDATE SET
                         favorite_agent_ids_json = excluded.favorite_agent_ids_json,
                         revision = excluded.revision,
                         updated_at = excluded.updated_at""",
                    (user_id, json.dumps(normalized), next_snapshot["revision"], _now_iso()),
                )
                conn.commit()
                return {"conflict": False, "snapshot": next_snapshot}
            finally:
                conn.close()

    @staticmethod
    def _agent_page_preferences_snapshot(row) -> dict:
        """Normalize one persisted Agent-card subpage preference row."""
        if not row:
            return {"page_ids": [], "revision": 0, "configured": False}
        try:
            raw_ids = json.loads(row["favorite_page_ids_json"] or "[]")
        except (TypeError, ValueError):
            raw_ids = []
        seen = set()
        page_ids = []
        for value in raw_ids if isinstance(raw_ids, list) else []:
            if not isinstance(value, str) or not value or value in seen:
                continue
            seen.add(value)
            page_ids.append(value)
        try:
            revision = max(0, int(row["revision"]))
        except (TypeError, ValueError):
            revision = 0
        return {"page_ids": page_ids, "revision": revision, "configured": True}

    async def get_user_agent_page_preferences(self, user_id: str) -> dict:
        """Return this user's global, cross-device Agent-card page preference."""
        conn = self._get_conn()
        try:
            row = conn.execute(
                """SELECT favorite_page_ids_json, revision
                   FROM user_agent_page_preferences WHERE user_id = ?""",
                (user_id,),
            ).fetchone()
            return self._agent_page_preferences_snapshot(row)
        finally:
            conn.close()

    async def replace_user_agent_page_preferences(
        self, user_id: str, favorite_page_ids: List[str], expected_revision: int,
    ) -> dict:
        """Atomically replace Agent-card page preferences with optimistic locking."""
        seen = set()
        normalized = []
        for value in favorite_page_ids if isinstance(favorite_page_ids, list) else []:
            if not isinstance(value, str) or not value or value in seen:
                continue
            seen.add(value)
            normalized.append(value)
            if len(normalized) >= 100:
                break
        try:
            expected = max(0, int(expected_revision))
        except (TypeError, ValueError):
            expected = 0

        async with self._write_lock:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    """SELECT favorite_page_ids_json, revision
                       FROM user_agent_page_preferences WHERE user_id = ?""",
                    (user_id,),
                ).fetchone()
                current = self._agent_page_preferences_snapshot(row)
                if current["revision"] != expected:
                    return {"conflict": True, "snapshot": current}

                next_snapshot = {
                    "page_ids": normalized,
                    "revision": current["revision"] + 1,
                    "configured": True,
                }
                conn.execute(
                    """INSERT INTO user_agent_page_preferences
                       (user_id, favorite_page_ids_json, revision, updated_at)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(user_id) DO UPDATE SET
                         favorite_page_ids_json = excluded.favorite_page_ids_json,
                         revision = excluded.revision,
                         updated_at = excluded.updated_at""",
                    (user_id, json.dumps(normalized), next_snapshot["revision"], _now_iso()),
                )
                conn.commit()
                return {"conflict": False, "snapshot": next_snapshot}
            finally:
                conn.close()

    @staticmethod
    def _my_agents_page_preferences_snapshot(row) -> dict:
        """Normalize one persisted My Agents (All Agents) page preference row."""
        if not row:
            return {"page_ids": [], "revision": 0, "configured": False}
        try:
            raw_ids = json.loads(row["favorite_page_ids_json"] or "[]")
        except (TypeError, ValueError):
            raw_ids = []
        seen = set()
        page_ids = []
        for value in raw_ids if isinstance(raw_ids, list) else []:
            if not isinstance(value, str) or not value or value in seen:
                continue
            seen.add(value)
            page_ids.append(value)
        try:
            revision = max(0, int(row["revision"]))
        except (TypeError, ValueError):
            revision = 0
        return {"page_ids": page_ids, "revision": revision, "configured": True}

    async def get_user_my_agents_page_preferences(self, user_id: str) -> dict:
        """Return this user's cross-device My Agents (All Agents) page preference."""
        conn = self._get_conn()
        try:
            row = conn.execute(
                """SELECT favorite_page_ids_json, revision
                   FROM user_my_agents_page_preferences WHERE user_id = ?""",
                (user_id,),
            ).fetchone()
            return self._my_agents_page_preferences_snapshot(row)
        finally:
            conn.close()

    async def replace_user_my_agents_page_preferences(
        self, user_id: str, favorite_page_ids: List[str], expected_revision: int,
    ) -> dict:
        """Atomically replace My Agents page preferences with optimistic locking."""
        seen = set()
        normalized = []
        for value in favorite_page_ids if isinstance(favorite_page_ids, list) else []:
            if not isinstance(value, str) or not value or value in seen:
                continue
            seen.add(value)
            normalized.append(value)
            if len(normalized) >= 100:
                break
        try:
            expected = max(0, int(expected_revision))
        except (TypeError, ValueError):
            expected = 0

        async with self._write_lock:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    """SELECT favorite_page_ids_json, revision
                       FROM user_my_agents_page_preferences WHERE user_id = ?""",
                    (user_id,),
                ).fetchone()
                current = self._my_agents_page_preferences_snapshot(row)
                if current["revision"] != expected:
                    return {"conflict": True, "snapshot": current}

                next_snapshot = {
                    "page_ids": normalized,
                    "revision": current["revision"] + 1,
                    "configured": True,
                }
                conn.execute(
                    """INSERT INTO user_my_agents_page_preferences
                       (user_id, favorite_page_ids_json, revision, updated_at)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(user_id) DO UPDATE SET
                         favorite_page_ids_json = excluded.favorite_page_ids_json,
                         revision = excluded.revision,
                         updated_at = excluded.updated_at""",
                    (user_id, json.dumps(normalized), next_snapshot["revision"], _now_iso()),
                )
                conn.commit()
                return {"conflict": False, "snapshot": next_snapshot}
            finally:
                conn.close()

    async def upsert_user_profile(self, user_id: str, **kwargs) -> dict:
        """Create or update a user_profiles row. Returns the full updated row."""
        async with self._write_lock:
            conn = self._get_conn()
            try:
                now = _now_iso()
                existing = conn.execute(
                    "SELECT * FROM user_profiles WHERE user_id = ?", (user_id,)
                ).fetchone()
                if existing:
                    if kwargs:
                        sets = ", ".join(f"{k} = ?" for k in kwargs)
                        vals = list(kwargs.values()) + [now, user_id]
                        conn.execute(
                            f"UPDATE user_profiles SET {sets}, updated_at = ? WHERE user_id = ?",
                            vals,
                        )
                else:
                    cols = ["user_id", "created_at", "updated_at"] + list(kwargs.keys())
                    placeholders = ", ".join("?" for _ in cols)
                    vals = [user_id, now, now] + list(kwargs.values())
                    conn.execute(
                        f"INSERT INTO user_profiles ({', '.join(cols)}) VALUES ({placeholders})",
                        vals,
                    )
                conn.commit()
                row = conn.execute(
                    "SELECT * FROM user_profiles WHERE user_id = ?", (user_id,)
                ).fetchone()
                return dict(row)
            finally:
                conn.close()

    @turn_cached
    async def is_user_admin(self, user_id: str) -> bool:
        """Return True if the user has is_admin = 1."""
        profile = await self.get_user_profile(user_id)
        return bool(profile and profile.get("is_admin"))

    async def set_user_admin(self, user_id: str, is_admin: bool) -> dict:
        """Set the is_admin flag for a user. Creates the profile row if needed."""
        return await self.upsert_user_profile(user_id, is_admin=1 if is_admin else 0)

    # ── Per-user appearance overrides (user_profiles.appearance) ──
    # A sparse JSON patch of the same keys as app-settings.json's appearance
    # block. Layered on top of the global theme by /api/v1/auth/ui-config when
    # the admin enables allow_user_appearance. Self-service via the account
    # page's "My appearance" editor → /api/v1/auth/me/appearance.

    async def get_user_appearance(self, user_id: str) -> dict:
        """Return the user's sparse appearance-override dict, or {} if none."""
        profile = await self.get_user_profile(user_id)
        raw = (profile or {}).get("appearance")
        if not raw:
            return {}
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    async def set_user_appearance(self, user_id: str, overrides: dict) -> dict:
        """Replace the user's appearance overrides with `overrides` (sparse dict).
        Pass {} to clear (the user falls back to the global theme). Returns it."""
        overrides = overrides or {}
        await self.upsert_user_profile(user_id, appearance=json.dumps(overrides))
        return overrides

    async def merge_user_appearance(self, user_id: str, patch: dict) -> dict:
        """Merge `patch` into the user's stored overrides and persist. A key with
        a blank value is REMOVED (so that token falls back to the global theme).
        Returns the merged dict."""
        current = await self.get_user_appearance(user_id)
        for key, val in (patch or {}).items():
            if isinstance(val, str) and not val.strip():
                current.pop(key, None)  # blank = clear this token → inherit global
            else:
                current[key] = val
        return await self.set_user_appearance(user_id, current)

    # ──────────────────────────────────────────────────────────────────────────
    # User Accounts — the login/credential plane (was app/auth/users.json).
    # CENTRAL: registered in app/db/router.py CONTROL_METHODS so these always hit
    # the authority DB, never a per-tenant database. password_hash is bcrypt.
    # The set of updatable columns is fixed (see _USER_ACCOUNT_COLS) so a caller
    # can't smuggle arbitrary column names into the UPDATE.
    # ──────────────────────────────────────────────────────────────────────────

    _USER_ACCOUNT_COLS = (
        "username", "password_hash", "display_name", "remember_token",
        "is_approved", "session_lifetime_minutes", "auto_renew", "social_links",
    )

    async def get_user_account_by_id(self, user_id: str) -> Optional[dict]:
        """Return the user_accounts row for this canonical user_id, or None."""
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM user_accounts WHERE user_id = ?", (user_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    async def get_user_account_by_username(self, username: str) -> Optional[dict]:
        """Return the account whose login name matches `username`, or None."""
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM user_accounts WHERE username = ?", (username,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    async def get_user_account_by_remember_token(self, token: str) -> Optional[dict]:
        """Return the account holding this (non-empty) remember token, or None."""
        if not token:
            return None
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM user_accounts WHERE remember_token = ? AND remember_token != ''",
                (token,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    async def get_user_account_by_social(self, provider: str, external_id: str) -> Optional[dict]:
        """Return the account linked to (provider, external_id), or None.

        social_links is a small JSON map {provider: external_id}; there are few
        accounts, so a scan-and-match in Python is simpler and fully portable
        (no JSON SQL functions across SQLite/Postgres)."""
        if not provider or not external_id:
            return None
        ext = str(external_id)
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM user_accounts WHERE social_links LIKE ?",
                (f'%"{provider}"%',),
            ).fetchall()
        finally:
            conn.close()
        for row in rows:
            d = dict(row)
            try:
                links = json.loads(d.get("social_links") or "{}")
            except Exception:
                links = {}
            if str(links.get(provider, "")) == ext:
                return d
        return None

    async def list_user_accounts(self) -> List[dict]:
        """Return every account row (ordered by creation)."""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM user_accounts ORDER BY created_at ASC"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    async def count_user_accounts(self) -> int:
        """Return how many accounts exist (used for the first-run / seed gate)."""
        conn = self._get_conn()
        try:
            return int(conn.execute("SELECT COUNT(*) FROM user_accounts").fetchone()[0])
        finally:
            conn.close()

    async def create_user_account(self, user_id: str, username: str,
                                  password_hash: str, display_name: str = "",
                                  is_approved: bool = True,
                                  session_lifetime_minutes: int = 43200,
                                  auto_renew: bool = True,
                                  social_links: Optional[dict] = None) -> Optional[dict]:
        """Insert a new account. Returns the row, or None if the username or
        user_id is already taken (mirrors the old register_user contract)."""
        # A new account must have a fresh user authority before becoming visible.
        # This is initialization through the canonical storage factory, not migration.
        from app.db.storage_layout import is_layout_active
        if is_layout_active():
            from app.db import get_user_db
            get_user_db(user_id)
        async with self._write_lock:
            conn = self._get_conn()
            try:
                clash = conn.execute(
                    "SELECT 1 FROM user_accounts WHERE user_id = ? OR username = ?",
                    (user_id, username),
                ).fetchone()
                if clash:
                    return None
                now = _now_iso()
                conn.execute(
                    "INSERT INTO user_accounts (user_id, username, password_hash, "
                    "display_name, remember_token, is_approved, session_lifetime_minutes, "
                    "auto_renew, social_links, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, '', ?, ?, ?, ?, ?, ?)",
                    (user_id, username, password_hash, display_name or username,
                     1 if is_approved else 0, int(session_lifetime_minutes),
                     1 if auto_renew else 0, json.dumps(social_links or {}), now, now),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT * FROM user_accounts WHERE user_id = ?", (user_id,)
                ).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

    async def update_user_account(self, user_id: str, **fields) -> Optional[dict]:
        """Update whitelisted columns on an account. Returns the updated row, or
        None if the account doesn't exist. Raises ValueError('username_taken')
        if a username change collides with another account.

        Booleans are coerced to 0/1; a dict `social_links` is JSON-encoded."""
        updates = {k: v for k, v in fields.items() if k in self._USER_ACCOUNT_COLS}
        if not updates:
            return await self.get_user_account_by_id(user_id)
        if "is_approved" in updates:
            updates["is_approved"] = 1 if updates["is_approved"] else 0
        if "auto_renew" in updates:
            updates["auto_renew"] = 1 if updates["auto_renew"] else 0
        if isinstance(updates.get("social_links"), dict):
            updates["social_links"] = json.dumps(updates["social_links"])
        async with self._write_lock:
            conn = self._get_conn()
            try:
                if not conn.execute(
                    "SELECT 1 FROM user_accounts WHERE user_id = ?", (user_id,)
                ).fetchone():
                    return None
                if "username" in updates:
                    taken = conn.execute(
                        "SELECT 1 FROM user_accounts WHERE username = ? AND user_id != ?",
                        (updates["username"], user_id),
                    ).fetchone()
                    if taken:
                        raise ValueError("username_taken")
                sets = ", ".join(f"{k} = ?" for k in updates)
                vals = list(updates.values()) + [_now_iso(), user_id]
                conn.execute(
                    f"UPDATE user_accounts SET {sets}, updated_at = ? WHERE user_id = ?",
                    vals,
                )
                conn.commit()
                row = conn.execute(
                    "SELECT * FROM user_accounts WHERE user_id = ?", (user_id,)
                ).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

    async def delete_user_account(self, user_id: str) -> bool:
        """Delete the central account and profile rows after data-plane erasure."""
        async with self._write_lock:
            conn = self._get_conn()
            try:
                cur = conn.execute(
                    "DELETE FROM user_accounts WHERE user_id = ?", (user_id,)
                )
                conn.execute(
                    "DELETE FROM user_profiles WHERE user_id = ?", (user_id,)
                )
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    async def erase_user_owned_data(self, user_id: str) -> dict:
        """Erase data-plane rows while leaving the central login row intact."""
        conn = self._get_conn()
        try:
            from app.db.user_lifecycle import erase_user_data
            return await erase_user_data(conn, user_id, include_account=False)
        finally:
            conn.close()

    async def export_user_data(self, user_id: str) -> dict:
        """Export user-owned server data from the active SQLite/Postgres store."""
        conn = self._get_conn()
        try:
            from app.db.user_lifecycle import export_user_data
            return await export_user_data(conn, user_id)
        finally:
            conn.close()

    # ──────────────────────────────────────────────────────────────────────────
    # Multi-agent: template listing and custom agent CRUD
    # ──────────────────────────────────────────────────────────────────────────

    async def list_agent_templates(self, include_admin: bool = False, discoverable_only: bool = False) -> List[dict]:
        """
        Return agent_templates that are user-visible (is_pipeline=0).
        If include_admin=False, excludes access_level='admin_only' templates.
        If discoverable_only=True, only returns templates with discoverable=1 (ignored when include_admin=True).
        """
        conn = self._get_conn()
        try:
            if include_admin:
                rows = conn.execute(
                    "SELECT * FROM agent_templates WHERE is_pipeline = 0 ORDER BY is_system DESC, name ASC"
                ).fetchall()
            elif discoverable_only:
                rows = conn.execute(
                    "SELECT * FROM agent_templates WHERE is_pipeline = 0 AND access_level != 'admin_only' AND discoverable = 1 ORDER BY is_system DESC, name ASC"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM agent_templates WHERE is_pipeline = 0 AND access_level != 'admin_only' ORDER BY is_system DESC, name ASC"
                ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    async def update_agent_template_fields(self, template_id: str, updates: dict) -> Optional[dict]:
        """
        Update allowed fields on an agent_templates row.
        Returns the updated row, or None if not found.
        Allowed fields: discoverable.
        """
        ALLOWED = {"discoverable"}
        safe = {k: v for k, v in updates.items() if k in ALLOWED}
        if not safe:
            return None
        conn = self._get_conn()
        try:
            existing = conn.execute(
                "SELECT * FROM agent_templates WHERE id = ?", (template_id,)
            ).fetchone()
            if not existing:
                return None
            try:
                metadata = json.loads(existing["metadata"] or "{}")
                if not isinstance(metadata, dict):
                    metadata = {}
            except (json.JSONDecodeError, TypeError):
                metadata = {}
            metadata["source"] = "admin_edit"
            safe["metadata"] = json.dumps(metadata)
            set_clause = ", ".join(f"{k} = ?" for k in safe)
            conn.execute(
                f"UPDATE agent_templates SET {set_clause}, updated_at = ? WHERE id = ?",
                (*safe.values(), _now_iso(), template_id),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM agent_templates WHERE id = ?", (template_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    async def save_agent_as_template(
        self,
        agent_id: str,
        template_id: str,
        name: str,
        description: str = "",
        icon: str = "",
        discoverable: bool = False,
        access_level: str = "all",
        updated_by: str = "admin",
    ) -> dict:
        """Snapshot a custom agent into agent_templates + agent_prompt_templates.

        Reads the agent's row from `agents` for config (model, temperature,
        loop_logic, etc.) and the admin-base slot rows from `agent_prompts`
        (user_id IS NULL) for prompt content. Writes:
          - one row in agent_templates (config + new template_id)
          - one row per slot in agent_prompt_templates with source='admin'
            so future JSON re-seed will not overwrite it.

        Raises ValueError if the agent does not exist or the template_id is
        already taken.
        """
        template_id = (template_id or "").strip()
        if not template_id:
            raise ValueError("template_id required")
        if not name or not name.strip():
            raise ValueError("name required")

        async with self._write_lock:
            conn = self._get_conn()
            try:
                agent_row = conn.execute(
                    "SELECT * FROM agents WHERE id = ?", (agent_id,)
                ).fetchone()
                if not agent_row:
                    raise ValueError(f"agent {agent_id} not found")
                agent = dict(agent_row)

                existing_tpl = conn.execute(
                    "SELECT id FROM agent_templates WHERE id = ?", (template_id,)
                ).fetchone()
                if existing_tpl:
                    raise ValueError(f"template_id '{template_id}' already exists")

                slot_rows = conn.execute(
                    """SELECT slot_name, order_index, lock, merge_mode, content
                       FROM agent_prompts
                       WHERE agent_id = ? AND user_id IS NULL
                       ORDER BY order_index ASC""",
                    (agent_id,),
                ).fetchall()

                now = _now_iso()
                # Stamp origin marker so the template knows it was admin-saved.
                _sv_meta = agent.get("metadata") or "{}"
                try:
                    _sv_meta_obj = json.loads(_sv_meta) if isinstance(_sv_meta, str) else dict(_sv_meta or {})
                except (json.JSONDecodeError, TypeError):
                    _sv_meta_obj = {}
                _sv_meta_obj["source"] = "admin_saved"
                _sv_meta_json = json.dumps(_sv_meta_obj)

                conn.execute(
                    """INSERT INTO agent_templates
                       (id, name, description, icon, max_turn_count, max_wall_seconds,
                        max_identical_tool_calls, max_stall_strikes,
                        model, provider,
                        temperature, max_tokens, metadata,
                        can_be_default, is_system, is_pipeline, access_level,
                        is_admin_agent, discoverable,
                        trigger_type, trigger_key, loop_logic,
                        created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        template_id,
                        name.strip(),
                        description or "",
                        icon or "",
                        agent.get("max_turn_count") if agent.get("max_turn_count") is not None else 0,
                        agent.get("max_wall_seconds"),
                        agent.get("max_identical_tool_calls", 0),
                        agent.get("max_stall_strikes", 0),
                        agent.get("model"),
                        agent.get("provider"),
                        agent.get("temperature") if agent.get("temperature") is not None else 0.0,
                        agent.get("max_tokens") or 8096,
                        _sv_meta_json,
                        1,  # can_be_default
                        0,  # is_system (user-created)
                        0,  # is_pipeline
                        access_level or "all",
                        int(bool(agent.get("is_admin_agent"))),
                        1 if discoverable else 0,
                        agent.get("trigger_type") or "user_input",
                        agent.get("trigger_key"),
                        agent.get("loop_logic") or "[]",
                        now, now,
                    ),
                )

                slot_count = 0
                for r in _resolved_slots:
                    conn.execute(
                        """INSERT INTO agent_prompt_templates
                           (id, template_id, slot_name, order_index, lock,
                            merge_mode, content, version, source,
                            updated_at, updated_by)
                           VALUES (?, ?, ?, ?, ?, ?, ?, 1, 'admin', ?, ?)""",
                        (
                            _uuid(),
                            template_id,
                            r["slot_name"],
                            int(r["order_index"] or 0),
                            int(r["lock"] or 0),
                            r["merge_mode"] or "replace",
                            r["content"] or "",
                            now,
                            updated_by,
                        ),
                    )
                    slot_count += 1

                conn.commit()
                tpl_row = conn.execute(
                    "SELECT * FROM agent_templates WHERE id = ?", (template_id,)
                ).fetchone()
                result = dict(tpl_row) if tpl_row else {}
                result["slot_count"] = slot_count
                return result
            finally:
                conn.close()

    async def upsert_agent_to_template(
        self,
        agent_id: str,
        updated_by: str = "admin",
    ) -> dict:
        """Push a custom agent's current config + prompt slots back into its
        template row (determined by ``agents.template_id``).

        Upserts into ``agent_templates`` and ``agent_prompt_templates`` with
        ``source='admin'`` so future JSON re-seeds won't overwrite them.
        Bumps the template version so manifest-aware re-seeds detect the change.

        Returns the updated template row dict, or raises ValueError if the
        agent has no template_id or neither the agent nor template exist.
        """
        conn = self._get_conn()
        try:
            agent_row = conn.execute(
                "SELECT * FROM agents WHERE id = ?", (agent_id,)
            ).fetchone()
            if not agent_row:
                raise ValueError(f"Agent {agent_id} not found")
            agent = dict(agent_row)

            template_id = agent.get("template_id")
            if not template_id:
                raise ValueError(f"Agent {agent_id} has no template_id — cannot push")

            # Read the current template to bump its version
            tpl_row = conn.execute(
                "SELECT * FROM agent_templates WHERE id = ?", (template_id,)
            ).fetchone()
            if not tpl_row:
                raise ValueError(f"Template {template_id} not found for agent {agent_id}")

            # Bump the prompt-slot version so manifest-aware re-seeds pick up the change.
            _max_ver = conn.execute(
                "SELECT MAX(version) FROM agent_prompt_templates WHERE template_id = ?",
                (template_id,),
            ).fetchone()[0]
            _new_version = (_max_ver or 0) + 1

            # Read the agent's admin-base prompt slots, then overlay the
            # owner's per-user overrides so the template push picks up
            # edits made through the Prompts tab — even for locked slots.
            # This makes the Config tab's "Push to DB template" the
            # canonical promotion step: users edit via overrides, push
            # promotes those overrides into the template.
            slot_rows = conn.execute(
                """SELECT slot_name, order_index, lock, merge_mode, content
                   FROM agent_prompts
                   WHERE agent_id = ? AND user_id IS NULL
                   ORDER BY order_index ASC""",
                (agent_id,),
            ).fetchall()

            _owner_id = agent.get("owner_user_id")
            _overrides: Dict[str, str] = {}
            if _owner_id:
                _ov_rows = conn.execute(
                    """SELECT slot_name, content FROM agent_prompts
                       WHERE agent_id = ? AND user_id = ?""",
                    (agent_id, _owner_id),
                ).fetchall()
                _overrides = {r["slot_name"]: r["content"] for r in _ov_rows}

            # Resolve each slot: merge base + owner override. We bypass the
            # normal lock guard (_slot_apply ignores overrides on locked slots)
            # because the push button is the explicit "promote to template"
            # action — the user's override always wins here.
            _resolved_slots = []
            for r in slot_rows:
                _base = r["content"] or ""
                _ov = _overrides.get(r["slot_name"])
                _mode = r["merge_mode"] if r["merge_mode"] in ("replace", "append") else "replace"
                if _ov is None:
                    _content = _base
                elif _mode == "append" and _base.strip():
                    _content = _base.rstrip() + "\n\n" + _ov.lstrip()
                else:
                    _content = _ov
                _resolved_slots.append({
                    "slot_name": r["slot_name"],
                    "order_index": r["order_index"],
                    "lock": r["lock"],
                    "merge_mode": _mode,
                    "content": _content,
                })

            now = _now_iso()

            # Merge agent's metadata into the template metadata, preserving
            # the template's origin source marker and any admin-only keys.
            _tpl_meta_raw = tpl_row["metadata"] or "{}"
            _ag_meta_raw = agent.get("metadata") or "{}"
            try:
                _tpl_meta = json.loads(_tpl_meta_raw) if isinstance(_tpl_meta_raw, str) else dict(_tpl_meta_raw or {})
            except (json.JSONDecodeError, TypeError):
                _tpl_meta = {}
            try:
                _ag_meta = json.loads(_ag_meta_raw) if isinstance(_ag_meta_raw, str) else dict(_ag_meta_raw or {})
            except (json.JSONDecodeError, TypeError):
                _ag_meta = {}
            # Pull over everything except internal runtime keys
            _skip_keys = {"template_origin", "owner_user_id", "user_id",
                          "cache_family", "cache_profile_version",
                          "prompt_layout_version", "discovery_default"}
            for k, v in _ag_meta.items():
                if k not in _skip_keys:
                    _tpl_meta[k] = v
            _tpl_meta["source"] = "admin_saved"
            _tpl_meta_json = json.dumps(_tpl_meta)

            # Bump the prompt-slot version so manifest-aware re-seeds detect the change.
            _max_ver = conn.execute(
                "SELECT MAX(version) FROM agent_prompt_templates WHERE template_id = ?",
                (template_id,),
            ).fetchone()[0]
            _new_version = (_max_ver or 0) + 1

            conn.execute(
                """UPDATE agent_templates SET
                   name = ?, description = ?, icon = ?,
                   max_turn_count = ?, max_wall_seconds = ?,
                   max_identical_tool_calls = ?, max_stall_strikes = ?,
                   model = ?, provider = ?,
                   temperature = ?, max_tokens = ?,
                   metadata = ?,
                   trigger_type = ?, trigger_key = ?, loop_logic = ?,
                   updated_at = ?
                   WHERE id = ?""",
                (
                    agent.get("name", ""),
                    agent.get("description", ""),
                    agent.get("icon", ""),
                    agent.get("max_turn_count") if agent.get("max_turn_count") is not None else 0,
                    agent.get("max_wall_seconds"),
                    agent.get("max_identical_tool_calls", 0),
                    agent.get("max_stall_strikes", 0),
                    agent.get("model"),
                    agent.get("provider"),
                    agent.get("temperature") if agent.get("temperature") is not None else 0.0,
                    agent.get("max_tokens") or 8096,
                    _tpl_meta_json,
                    agent.get("trigger_type") or "user_input",
                    agent.get("trigger_key"),
                    agent.get("loop_logic") or "[]",
                    now,
                    template_id,
                ),
            )

            # Per-slot upsert in agent_prompt_templates (source='admin')
            slot_count = 0
            from app.context.md_seeder import compute_agent_manifest_hash
            for r in slot_rows:
                existing = conn.execute(
                    "SELECT id FROM agent_prompt_templates WHERE template_id = ? AND slot_name = ?",
                    (template_id, r["slot_name"]),
                ).fetchone()
                if existing:
                    conn.execute(
                        """UPDATE agent_prompt_templates
                           SET order_index = ?, lock = ?, merge_mode = ?,
                               content = ?, version = ?, source = 'admin',
                               updated_at = ?, updated_by = ?
                           WHERE id = ?""",
                        (int(r["order_index"] or 0), int(r["lock"] or 0),
                         r["merge_mode"] or "replace", r["content"] or "",
                         _new_version, now, updated_by,
                         existing["id"]),
                    )
                else:
                    conn.execute(
                        """INSERT INTO agent_prompt_templates
                           (id, template_id, slot_name, order_index, lock,
                            merge_mode, content, version, source,
                            updated_at, updated_by)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'admin', ?, ?)""",
                        (_uuid(), template_id, r["slot_name"],
                         int(r["order_index"] or 0), int(r["lock"] or 0),
                         r["merge_mode"] or "replace", r["content"] or "",
                         _new_version, now, updated_by),
                    )
                slot_count += 1

            # Invalidate the stored manifest hash so next boot re-seeds detect the change
            conn.execute(
                "INSERT OR REPLACE INTO app_meta (key, value, updated_at) VALUES ('last_agent_manifest_hash', '', ?)",
                (now,),
            )

            conn.commit()

            updated = conn.execute(
                "SELECT * FROM agent_templates WHERE id = ?", (template_id,)
            ).fetchone()
            result = dict(updated) if updated else {}
            result["slot_count"] = slot_count
            return result
        finally:
            conn.close()

    async def list_agents_for_user(self, user_id: str, include_admin: bool = False,
                                   view: str = "active") -> List[dict]:
        """
        Return all agents visible to a user:
        - System agent templates (is_pipeline=0), filtered by access_level
        - User's own custom agents (owner_user_id = user_id, is_system=0 equivalent)
        Each item includes a 'source' key: 'template' or 'custom'.
        Custom agents also carry their is_user_default flag.

        `view` controls the filter on CUSTOM agents:
          'active' (default) — templates + custom agents NOT in the bin
          'bin'              — only custom agents whose status == 'trashed'
                               (templates are never trashed, so none appear)
          'clones'           — only custom agents whose status == 'clone',
                               returned with optional spawn-ledger metadata
        A NULL/empty status counts as active (Postgres adds the column without a
        default, so pre-existing rows read back as NULL).
        """
        bin_view = (view == "bin")
        clones_view = (view == "clones")

        def _in_view(entry: dict) -> bool:
            # Ephemeral clones are never part of the fleet roster — not in the
            # active list, not in the recycling bin. They live and die with their
            # orchestrator session (see cascade_delete_clones). 'clone_trashed' is
            # a clone the user recycled from the Automations bin — likewise hidden
            # from the agent roster.
            if entry.get("status") in ("clone", "clone_trashed"):
                return False
            trashed = (entry.get("status") == "trashed")
            return trashed if bin_view else (not trashed)

        # ── Clones view: return status='clone' agents with spawn-ledger fields ──
        # Clones are hidden from normal views; this dedicated view lets the user
        # inspect them grouped by orchestrator.
        if clones_view:
            result = []
            conn = self._get_conn()
            try:
                rows = conn.execute(
                    """SELECT a.* FROM agents a
                       WHERE a.status = 'clone'
                         AND EXISTS (
                               SELECT 1 FROM json_each(a.admin_users)
                               WHERE value = ?
                             )
                       ORDER BY a.created_at DESC""",
                    (user_id,),
                ).fetchall()
                for row in rows:
                    entry = dict(row)
                    entry["source"] = "custom"
                    result.append(entry)
            finally:
                conn.close()

            # Best-effort enrich from the agent_spawns ledger table.
            # This table exists only when the Agent Orchestration ability has been
            # used at least once. A missing table is non-fatal.
            try:
                sconn = self._get_conn()
                try:
                    # Check if the table exists
                    tbl = sconn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name='agent_spawns'"
                    ).fetchone()
                    if tbl:
                        for entry in result:
                            spawn_row = sconn.execute(
                                """SELECT status, result_summary,
                                          orchestrator_session_id, orchestrator_agent_id
                                   FROM agent_spawns
                                   WHERE spawn_agent_id = ?
                                   ORDER BY created_at DESC
                                   LIMIT 1""",
                                (entry["id"],),
                            ).fetchone()
                            if spawn_row:
                                entry["spawn_status"] = spawn_row["status"]
                                entry["result_summary"] = spawn_row["result_summary"]
                                entry["orchestrator_session_id"] = spawn_row["orchestrator_session_id"]
                                entry["orchestrator_agent_id"] = spawn_row["orchestrator_agent_id"]
                                # Resolve the orchestrator's display name
                                orch_id = spawn_row["orchestrator_agent_id"]
                                if orch_id:
                                    orch_row = sconn.execute(
                                        "SELECT name FROM agents WHERE id = ?", (orch_id,)
                                    ).fetchone()
                                    entry["orchestrator_name"] = orch_row["name"] if orch_row else None
                                else:
                                    entry["orchestrator_name"] = None
                            else:
                                entry["spawn_status"] = None
                                entry["result_summary"] = None
                                entry["orchestrator_session_id"] = None
                                entry["orchestrator_agent_id"] = None
                                entry["orchestrator_name"] = None
                finally:
                    sconn.close()
            except Exception as e:
                logger.debug("Could not enrich clones from agent_spawns: %s", e)
                for entry in result:
                    entry["spawn_status"] = None
                    entry["result_summary"] = None
                    entry["orchestrator_session_id"] = None
                    entry["orchestrator_agent_id"] = None
                    entry["orchestrator_name"] = None

            return result

        # 1. System templates the user can see (never trashed → hidden in bin view)
        result = []
        if not bin_view:
            templates = await self.list_agent_templates(include_admin=include_admin)
            for tpl in templates:
                entry = dict(tpl)
                entry["source"] = "template"
                entry["is_user_default"] = 0
                result.append(entry)

        # 2. User's agents — both assigned (user_id) and custom-created (owner_user_id)
        seen_ids: set = set()
        conn = self._get_conn()
        try:
            rows = conn.execute(
                """SELECT * FROM agents
                   WHERE EXISTS (
                           SELECT 1 FROM json_each(admin_users)
                           WHERE value = ?
                         )
                      OR EXISTS (
                           SELECT 1 FROM json_each(member_users)
                           WHERE value = ?
                         )
                   ORDER BY (sort_order IS NULL), sort_order ASC, created_at ASC""",
                (user_id, user_id),
            ).fetchall()
            for row in rows:
                entry = dict(row)
                if entry["id"] in seen_ids:
                    continue
                seen_ids.add(entry["id"])
                entry["source"] = "custom"
                if not _in_view(entry):
                    continue
                result.append(entry)
        finally:
            conn.close()

        # 2b. Aggregate agents from sibling .db files (parallel agent databases).
        # SQLite-only: a server-backed store (Postgres) has no sibling .db files.
        if getattr(self, "_scan_sibling_dbs", True):
            db_dir = os.path.dirname(os.path.abspath(self._db_path))
            primary_name = os.path.basename(self._db_path)
            # These canonical DBs never hold an agents table and may be encrypted
            # at rest (full-DB SQLCipher) — opening them raw here would just fail
            # and get skipped, so exclude them up front. Per-session vault sidecars
            # (``*.vault.db``) are likewise not agent DBs.
            _NON_AGENT_DBS = {"vault.db", "app_secrets.db", "agent_secrets.db", "user_secrets.db",
                               "logs.db", "recordings.db", "wiki.db"}
            try:
                sibling_paths = [
                    os.path.join(db_dir, f)
                    for f in os.listdir(db_dir)
                    if f.endswith(".db") and f != primary_name
                    and f not in _NON_AGENT_DBS and not f.endswith(".vault.db")
                ]
            except OSError:
                sibling_paths = []
        else:
            sibling_paths = []

        for sibling_path in sorted(sibling_paths):
            try:
                sconn = sqlite3.connect(sibling_path)
                sconn.row_factory = sqlite3.Row
                sconn.execute("PRAGMA journal_mode=WAL")
                sconn.execute("PRAGMA busy_timeout=5000")
                try:
                    srows = sconn.execute(
                        """SELECT * FROM agents
                           WHERE EXISTS (
                                   SELECT 1 FROM json_each(admin_users)
                                   WHERE value = ?
                                 )
                              OR EXISTS (
                                   SELECT 1 FROM json_each(member_users)
                                   WHERE value = ?
                                 )
                           ORDER BY created_at ASC""",
                        (user_id, user_id),
                    ).fetchall()
                    for row in srows:
                        entry = dict(row)
                        if entry["id"] in seen_ids:
                            continue
                        seen_ids.add(entry["id"])
                        entry["source"] = "custom"
                        if entry.get("template_id") in pipeline_tpl_ids:
                            continue
                        if not _in_view(entry):
                            continue
                        result.append(entry)
                finally:
                    sconn.close()
            except Exception as e:
                logger.debug("Skipping sibling DB %s: %s", sibling_path, e)

        # Mark the user's current default
        profile = await self.get_user_profile(user_id)
        default_id = profile.get("default_agent_id") if profile else None
        for entry in result:
            if default_id and entry.get("id") == default_id:
                entry["is_user_default"] = 1
        return result

    # ── Custom agent CRUD ─────────────────────────────────────────────────────

    def _seed_pre_enabled_connections(self, conn, agent_id: str, metadata, now: str) -> int:
        """Create enabled ``agent_connections`` 'ability' rows for the ids in a
        template's ``metadata.pre_enabled_connections``, so the agent's tools are
        available at runtime.

        ``metadata`` may be the raw JSON string or an already-parsed dict.
        Wildcards in the list are expanded first — ``"*"`` (every ability),
        ``"Core/*"`` / ``"group:web"`` (every ability in a group) — via
        ``abilities.expand_ability_selectors``, so a template can ask for "all
        abilities" (or "all in a group") without naming each id, and newly
        dropped-in abilities are auto-included. Existing rows are left untouched
        (idempotent). Fail-open: an expansion error falls back to the literal
        list. Returns the number of rows inserted.

        Shared by every create/materialize path (create_custom_agent + the
        session-agent materializer) so the default agent ends up with the same
        ability set no matter which path built it.
        """
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except Exception:
                metadata = {}
        if not isinstance(metadata, dict):
            return 0
        _pec = metadata.get("pre_enabled_connections")
        if not (_pec and isinstance(_pec, list)):
            return 0
        try:
            from app import abilities as _abilities_mgr
            _pec = _abilities_mgr.expand_ability_selectors(_pec)
        except Exception as _exp_e:
            logger.warning(
                "Ability selector expansion failed (%s) — seeding literal list",
                _exp_e,
            )
        inserted = 0
        for _ct in _pec:
            if not isinstance(_ct, str) or not _ct.strip():
                continue
            _existing = conn.execute(
                "SELECT 1 FROM agent_connections WHERE agent_id = ? AND connection_type = ?",
                (agent_id, _ct),
            ).fetchone()
            if _existing:
                continue
            conn.execute(
                """INSERT INTO agent_connections
                       (id, agent_id, connection_type, section, enabled, config, created_at, updated_at)
                   VALUES (?, ?, ?, 'ability', 1, '{}', ?, ?)""",
                (_uuid(), agent_id, _ct, now, now),
            )
            inserted += 1
        return inserted

    async def create_custom_agent(
        self, user_id: str, name: str, description: str = "", template_id: str = "default",
        seed_abilities: bool = True,
        capability_profile: Optional[str] = None,
        capability_extensions: Optional[List[str]] = None,
        agent_id: Optional[str] = None,
    ) -> dict:
        """
        Create a new custom agent for a user, cloned from the specified template.
        Returns the new agents row as a dict (with source='custom').

        ``template_id`` chooses the starting point:
          - a real template id  → clone that template's config + prompt slots;
          - falsy / "none" / "blank" / "scratch" → NO template: a true blank
            slate. Nothing is cloned (sane config defaults, no prompt slots); the
            agent runs on the app-global baseline identity and whatever the caller
            adds afterwards. Stored template_id is "".

        ``capability_profile`` provisions the exact nested Simple, Standard, or
        Advanced ability sequence (plus explicit extensions). Without an explicit
        profile, ``seed_abilities`` preserves the legacy template-cloning behavior.
        """
        import uuid as _uuid_mod
        # ── No-template ("blank slate") path ─────────────────────────────────
        # A falsy or explicit "none"/"blank"/"scratch" template_id means: clone
        # NOTHING. The agent is created bare — no config inherited (the .get()
        # fallbacks below supply sane defaults) and no prompt slots cloned — so
        # it runs purely on the app-global baseline identity (see
        # app/agent/prompts.build_system_prompt) plus whatever the creator adds
        # afterwards. This is the deliberate counterpart to picking a template:
        # the agent CHOOSES a starting point or chooses to start from scratch,
        # rather than always being silently seeded from "default". The stored
        # template_id is left "" so nothing downstream treats it as a real
        # template (it just won't match optimizer etc.).
        _NO_TEMPLATE = {"", "none", "blank", "scratch", "no_template", "no-template"}
        blank = template_id is None or str(template_id).strip().lower() in _NO_TEMPLATE
        if blank:
            tpl: dict = {}
            template_id = ""
        else:
            conn = self._get_conn()
            try:
                # Templates are seeded at boot (manifest-gated) + on admin re-seed.
                # No per-call re-seed: protects admin edits + avoids DB churn.
                tpl_row = conn.execute(
                    "SELECT * FROM agent_templates WHERE id = ?", (template_id,)
                ).fetchone()
                tpl = dict(tpl_row) if tpl_row else {}
                # Fall back to default if the requested template doesn't exist
                if not tpl and template_id != "default":
                    tpl_row = conn.execute(
                        "SELECT * FROM agent_templates WHERE id = 'default'"
                    ).fetchone()
                    tpl = dict(tpl_row) if tpl_row else {}
                    template_id = "default"
            finally:
                conn.close()

            if not tpl:
                from app.context.md_seeder import scan_agent_json_files
                for entry in scan_agent_json_files():
                    if entry.get("id") == template_id or entry.get("id") == "default":
                        tpl = entry
                        template_id = entry.get("id", "default")
                        break

        agent_id = agent_id or str(_uuid_mod.uuid4())
        now = _now_iso()
        # New agents get a large finite turn ceiling (not 0). At runtime 0 means
        # "unlimited", but seeding a bounded value keeps the agent safe even on an
        # older build that mis-reads 0; the wall-clock cap is the real backstop.
        # Coerce a missing / 0 / non-positive template value up to the default.
        _tpl_mtc = tpl.get("max_turn_count")
        _new_max_turns = _tpl_mtc if (isinstance(_tpl_mtc, int) and _tpl_mtc > 0) else 12
        from app.agent.cache_profiles import (
            DEFAULT_PROFILE, profile_abilities, with_cache_profile,
        )
        _profile = capability_profile or (
            "advanced" if template_id == "default" and seed_abilities else DEFAULT_PROFILE
        )
        _profile_meta = with_cache_profile(
            tpl.get("metadata", "{}"), _profile, capability_extensions,
        )
        if capability_profile and not seed_abilities:
            _profile_meta["pre_enabled_connections"] = profile_abilities(
                _profile, capability_extensions,
            )
        # Stamp which template this agent was created from, so the Config tab
        # can show the "Push to template" button.
        if not blank and template_id:
            _profile_meta["template_origin"] = template_id
        _allowed_tools, _safety_policy = self._tool_perm_columns(_profile_meta)
        conn = self._get_conn()
        try:
            conn.execute(
                """INSERT INTO agents
                   (id, name, description,
                    max_turn_count, max_wall_seconds,
                    max_identical_tool_calls, max_stall_strikes,
                    model, provider,
                    temperature, max_tokens, metadata,
                    template_id, is_user_default,
                    allowed_tools, custom_tool_ids,
                    trigger_type, trigger_key, loop_logic,
                    safety_policy, is_admin_agent,
                    admin_users,
                    created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,'[]',?,?,?,?,?,?,?,?)""",
                (
                    agent_id, name, description,
                    _new_max_turns,
                    tpl.get("max_wall_seconds"),
                    tpl.get("max_identical_tool_calls", 0),
                    tpl.get("max_stall_strikes", 0),
                    tpl.get("model", ""),
                    tpl.get("provider", ""),
                    tpl.get("temperature", 0.7),
                    tpl.get("max_tokens", 8096),
                    # Agent-Manager-created agents are DISCOVERABLE by default:
                    # their tools/abilities are pulled in on demand (load_ability)
                    # rather than shipping every schema each turn. Mirrors the
                    # default WebAgent agent (create_agent_for_user).
                    json.dumps({**_profile_meta, "owner_user_id": user_id}),
                    template_id,
                    _allowed_tools,
                    tpl.get("trigger_type", "user_input"),
                    tpl.get("trigger_key"),
                    tpl.get("loop_logic", "[]"),
                    _safety_policy,
                    1 if tpl.get("is_admin_agent") else 0,
                    json.dumps([user_id]),
                    now, now,
                ),
            )
            # A blank agent clones no prompt slots — it relies on the app-global
            # baseline identity. Template-based agents clone the template's slots.
            if not blank:
                self._clone_template_slots(conn, source_id=template_id, target_id=agent_id, now=now)

            # ── Seed pre-enabled connections ──
            # An explicit profile is authoritative. Legacy callers without one
            # retain the prior template-seeding behavior.
            now = now or _now_iso()
            if capability_profile:
                self._seed_pre_enabled_connections(
                    conn, agent_id, _profile_meta, now,
                )
            elif seed_abilities:
                self._seed_pre_enabled_connections(
                    conn, agent_id, tpl.get("metadata", "{}"), now,
                )

            conn.commit()
            row = conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
        finally:
            conn.close()

        result = dict(row) if row else {"id": agent_id}
        result["source"] = "custom"
        return result

    async def create_clone_agent(
        self, *, user_id: str, master_agent_id: str, name: str,
        description: str = "", abilities: Optional[List[str]] = None,
        allowed_tools: Optional[List[str]] = None,
        destructive_ask: Optional[List[str]] = None,
        agent_id: Optional[str] = None,
    ) -> dict:
        """Create an ephemeral CLONE agent for an orchestrator's spawn.

        Unlike ``create_custom_agent``, a clone is built FROM SCRATCH:
          • ``status='clone'`` → hidden from the fleet roster AND the recycling
            bin; reaped by the cascade when its orchestrator session is deleted.
          • ``metadata.kind='clone'`` + ``metadata.clone_of=<master id>`` → every
            clone is traceable home to a stable master agent id.
          • Inherits ONLY the master's mechanical runtime (model / provider /
            temperature / max_tokens) — NOT its persona. The caller sets the
            clone's directive in a single ``orchestrator_directive`` slot; the
            app-global baseline supplies the mandatory identity.
          • NO template slots cloned and NO pre-enabled connections. Abilities
            start fully OFF; only the explicitly ``abilities`` granted are enabled
            (the caller has already clamped them to the master's — the ceiling).
            ``allowed_tools`` (deny list) and ``destructive_ask`` (confirm list)
            are the caller's already-clamped per-tool permissions.
        """
        import uuid as _uuid_mod
        abilities = [a for a in (abilities or []) if isinstance(a, str) and a.strip()]
        allowed_tools = [t for t in (allowed_tools or []) if isinstance(t, str) and t.strip()]
        destructive_ask = [t for t in (destructive_ask or []) if isinstance(t, str) and t.strip()]

        master = await self.get_agent_by_id(master_agent_id) or {}

        # ── Ceiling clamp (defense in depth) ─────────────────────────────
        # Callers (orchestration / automation) are expected to clamp grants to
        # the master's, but this is the last gate before the rows hit the DB:
        # a clone must NEVER end up with abilities its master lacks, nor a
        # narrower tool-deny list than its master. Enforce both here so no spawn
        # path can hand a clone more power than the agent that created it.
        if master_agent_id:
            try:
                master_conns = await self.get_agent_connections(master_agent_id)
                master_abilities = {
                    c.get("connection_type") for c in master_conns
                    if c.get("section") == "ability" and c.get("enabled")
                }
                requested = set(abilities)
                dropped = requested - master_abilities
                if dropped:
                    logger.warning(
                        "Clone ceiling: dropped abilities %s not held by master %s",
                        sorted(dropped), master_agent_id)
                abilities = [a for a in abilities if a in master_abilities]
                # allowed_tools is a DENY list — the clone must deny at least
                # everything the master denies (union), never less.
                try:
                    master_deny = set(json.loads(master.get("allowed_tools") or "[]"))
                except Exception:
                    master_deny = set()
                allowed_tools = sorted(set(allowed_tools) | master_deny)
            except Exception as e:
                logger.error("Clone ceiling enforcement failed for master %s: %s",
                             master_agent_id, e)
        agent_id = agent_id or str(_uuid_mod.uuid4())
        now = _now_iso()
        safety_policy = json.dumps({"destructive_tools": sorted(set(destructive_ask))}) \
            if destructive_ask else "{}"
        # Inherit the master's model ROSTER, not just its flat model/provider
        # columns. A role (standard / premium / …) resolves ONLY through a
        # literal `role_assignments` entry id inside `llm_config` — the flat
        # `model` column is never consulted (see
        # app/admin/settings._runtime_config_from_roster). Copying only the flat
        # columns left every clone with a model string but no appointed Standard
        # role, so its first run failed red with "Standard model role not
        # assigned" and every spawn returned an empty reply. The clone keeps the
        # master's provider account too, which is what the flat columns were
        # meant to carry.
        _clone_meta = {
            "owner_user_id": user_id,
            "kind": "clone",
            "clone_of": master_agent_id,
        }
        try:
            _master_meta = master.get("metadata")
            if isinstance(_master_meta, str):
                _master_meta = json.loads(_master_meta) if _master_meta.strip() else {}
            _master_llm = (_master_meta or {}).get("llm_config")
            if isinstance(_master_llm, dict) and _master_llm:
                _clone_meta["llm_config"] = _master_llm
            else:
                logger.warning(
                    "Clone of master %s has no llm_config to inherit — its runs "
                    "may fail with a missing Standard model role.", master_agent_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("Clone could not inherit llm_config from master %s: %s",
                           master_agent_id, e)
        metadata = json.dumps(_clone_meta)
        conn = self._get_conn()
        try:
            conn.execute(
                """INSERT INTO agents
                   (id, name, description, status,
                    max_turn_count, max_wall_seconds,
                    max_identical_tool_calls, max_stall_strikes,
                    model, provider, temperature, max_tokens, metadata,
                    template_id, is_user_default,
                    allowed_tools, custom_tool_ids,
                    trigger_type, trigger_key, loop_logic,
                    safety_policy, is_admin_agent, admin_users,
                    created_at, updated_at)
                   VALUES (?,?,?, 'clone', ?,?,?,?,?,?,?,?,?,?,0,?,'[]','user_input',NULL,'[]',?,0,?,?,?)""",
                (
                    agent_id, name or "Clone", description,
                    12, master.get("max_wall_seconds"),
                    master.get("max_identical_tool_calls", 0),
                    master.get("max_stall_strikes", 0),
                    master.get("model", ""), master.get("provider", ""),
                    master.get("temperature", 0.7), master.get("max_tokens", 8096),
                    metadata,
                    master.get("template_id") or "default",
                    json.dumps(sorted(set(allowed_tools))),
                    safety_policy,
                    json.dumps([user_id]),
                    now, now,
                ),
            )
            # Enable ONLY the granted abilities (everything else stays off).
            for ability_id in sorted(set(abilities)):
                conn.execute(
                    """INSERT INTO agent_connections
                           (id, agent_id, connection_type, section, enabled, config, created_at, updated_at)
                       VALUES (?, ?, ?, 'ability', 1, '{}', ?, ?)""",
                    (str(_uuid_mod.uuid4()), agent_id, ability_id, now, now),
                )
            conn.commit()
            row = conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
        finally:
            conn.close()

        result = dict(row) if row else {"id": agent_id}
        result["source"] = "clone"
        # Subagent store: nested under the master's home, not top-level.
        return result

    async def delete_clone_agent(self, agent_id: str, *, session_ids: Optional[List[str]] = None) -> bool:
        """Hard-delete a CLONE agent (status 'clone' or 'clone_trashed') and its
        transcripts.

        Used by the automation run engine to reap an ephemeral fresh-clone runner
        after its run, and by the Automations bin to permanently empty a recycled
        clone. Refuses to touch a real fleet agent (any other status).
        Pass the run session_ids to also purge their interactions/sessions.
        """
        async with self._write_lock:
            conn = self._get_conn()
            try:
                for sid in (session_ids or []):
                    if not sid:
                        continue
                    conn.execute("DELETE FROM interactions WHERE session_id = ?", (sid,))
                    for tbl in ("session_summaries", "pipeline_events",
                                "run_contract_checks", "run_contract_state"):
                        try:
                            conn.execute(f"DELETE FROM {tbl} WHERE session_id = ?", (sid,))
                        except Exception:  # noqa: BLE001
                            pass
                    conn.execute("DELETE FROM sessions WHERE id = ?", (sid,))
                # Capture the clone's parent BEFORE deleting the row, so the
                # nested subagent home can be purged afterwards.
                clone_parent = None
                try:
                    _mrow = conn.execute(
                        "SELECT metadata FROM agents WHERE id = ? AND status IN ('clone', 'clone_trashed')",
                        (agent_id,),
                    ).fetchone()
                    if _mrow:
                        _mmeta = json.loads(_mrow["metadata"] or "{}")
                        if isinstance(_mmeta, dict):
                            clone_parent = _mmeta.get("clone_of")
                except Exception:  # noqa: BLE001
                    pass
                cur = conn.execute(
                    "DELETE FROM agents WHERE id = ? AND status IN ('clone', 'clone_trashed')",
                    (agent_id,))
                removed = bool(cur.rowcount and cur.rowcount > 0)
                if removed:
                    conn.execute("DELETE FROM agent_prompts WHERE agent_id = ?", (agent_id,))
                    for tbl in ("agent_connections", "agent_abilities", "agent_soft_abilities"):
                        try:
                            conn.execute(f"DELETE FROM {tbl} WHERE agent_id = ?", (agent_id,))
                        except Exception:  # noqa: BLE001
                            pass
                conn.commit()
                # Reap the clone's nested subagent home (data/agent_data/<parent>/subagents/<id>/).
                if removed and clone_parent:
                    try:
                        from app.agent_workspace import purge_subagent_home
                        purge_subagent_home(clone_parent, agent_id)
                    except Exception:  # noqa: BLE001
                        pass
                return removed
            finally:
                conn.close()

    async def trash_clone_agent(self, agent_id: str) -> bool:
        """Soft-delete a clone into the recycling bin (status 'clone' ->
        'clone_trashed'). The dashboard's active clone list shows status='clone';
        the bin shows status='clone_trashed'."""
        async with self._write_lock:
            conn = self._get_conn()
            try:
                cur = conn.execute(
                    "UPDATE agents SET status = 'clone_trashed', updated_at = ? "
                    "WHERE id = ? AND status = 'clone'",
                    (_now_iso(), agent_id),
                )
                conn.commit()
                return bool(cur.rowcount and cur.rowcount > 0)
            finally:
                conn.close()

    async def restore_clone_agent(self, agent_id: str) -> bool:
        """Restore a binned clone back to the dashboard (status 'clone_trashed'
        -> 'clone')."""
        async with self._write_lock:
            conn = self._get_conn()
            try:
                cur = conn.execute(
                    "UPDATE agents SET status = 'clone', updated_at = ? "
                    "WHERE id = ? AND status = 'clone_trashed'",
                    (_now_iso(), agent_id),
                )
                conn.commit()
                return bool(cur.rowcount and cur.rowcount > 0)
            finally:
                conn.close()

    async def trash_custom_agent(self, agent_id: str, user_id: str) -> bool:
        """
        Move a custom agent to the recycling bin (soft delete). Caller must be in
        admin_users. Nothing is erased: the agent's status flips to 'trashed' so
        it drops out of the Agents page but keeps its prompts, sessions and
        transcripts until it is permanently emptied from the bin. EVERY session
        that belongs to the agent (plus spawned helper sessions in the run-family,
        resolved via resolve_child_sessions) is binned with it (status ->
        'recycled'), so a trashed agent can't keep serving active chats.
        System agents (template rows) live in agent_templates, not here, and the
        built-in 'default' row carries no admin_users — so the ownership check
        alone already protects them.
        Returns True if a row was flipped, False if not found or not owned.
        """
        conn = self._get_conn()
        recycled_ids: List[str] = []
        flipped = False
        try:
            # Bump updated_at with the status flip so the hybrid puller carries this
            # trash to every device's local mirror (it pulls only rows whose
            # watermark advanced); otherwise the trashed agent would linger in the
            # agents dropdown on other devices until a cold start.
            cursor = conn.execute(
                """UPDATE agents SET status = 'trashed', updated_at = ?
                   WHERE id = ?
                   AND EXISTS (SELECT 1 FROM json_each(admin_users) WHERE value = ?)""",
                (_now_iso(), agent_id, user_id),
            )
            flipped = cursor.rowcount > 0
            conn.commit()
            if flipped:
                # ── Cascade: bin every session that belongs to this agent ──
                # Base sessions + their whole run-family (spawned helpers, walked
                # recursively) go to the recycling bin with the agent. Bumping
                # updated_at lets the hybrid puller carry the flips to every
                # device's local mirror, exactly like the agent row above.
                # Best-effort: a missing table/column can't break the trash.
                try:
                    base_ids = [r[0] for r in conn.execute(
                        "SELECT id FROM sessions WHERE agent_id = ? "
                        "AND COALESCE(status, 'active') != 'recycled'",
                        (agent_id,),
                    ).fetchall()]
                    recycled_ids = list(base_ids)
                    for cid in resolve_child_sessions(conn, base_ids):
                        if cid not in recycled_ids:
                            recycled_ids.append(cid)
                    if recycled_ids:
                        now = _now_iso()
                        for sid in recycled_ids:
                            conn.execute(
                                "UPDATE sessions SET status = 'recycled', updated_at = ? "
                                "WHERE id = ?",
                                (now, sid),
                            )
                        conn.commit()
                except Exception:  # noqa: BLE001 — cascade is best-effort
                    logger.exception("trash_custom_agent: session cascade failed for %s", agent_id)
        finally:
            conn.close()
        # ── Safety: kill any active loop and clear active state ──
        # A loop may be mid-flight on a now-binned session: set an interrupt so it
        # halts on the next interrupt check, and clear the metadata active state so
        # it can't re-activate from a cold read. Mirrors the session-recycle sweep
        # in app/api/db_viewer.py. Best-effort — never fail the trash over this.
        if flipped and recycled_ids:
            for sid in recycled_ids:
                try:
                    await self.set_interrupt(sid)
                except Exception:  # noqa: BLE001
                    pass
                try:
                    await self.clear_session_active_state(sid)
                except Exception:  # noqa: BLE001
                    pass
        return flipped

    async def restore_custom_agent(self, agent_id: str, user_id: str) -> bool:
        """
        Restore a trashed agent back to the Agents page (status -> 'active').
        Caller must own it. Sessions that were binned with the agent are brought
        back too (status -> 'active', including spawned helpers in the run-family),
        so the agent returns with its chats. Returns True if a trashed row was
        restored.
        """
        conn = self._get_conn()
        restored_ids: List[str] = []
        restored = False
        try:
            cursor = conn.execute(
                """UPDATE agents SET status = 'active', updated_at = ?
                   WHERE id = ? AND status = 'trashed'
                   AND EXISTS (SELECT 1 FROM json_each(admin_users) WHERE value = ?)""",
                (_now_iso(), agent_id, user_id),
            )
            restored = cursor.rowcount > 0
            conn.commit()
            if restored:
                # ── Cascade: bring the agent's binned sessions back ──
                # Symmetric with trash_custom_agent: base sessions that were
                # binned (status 'recycled') flip back to 'active', and so does
                # their run-family (spawned helpers). Sessions the user binned on
                # their own before the agent trash are restored as well — the bin
                # is a single bucket and we can't tell the two apart. Best-effort.
                try:
                    base_ids = [r[0] for r in conn.execute(
                        "SELECT id FROM sessions WHERE agent_id = ? AND status = 'recycled'",
                        (agent_id,),
                    ).fetchall()]
                    restored_ids = list(base_ids)
                    for cid in resolve_child_sessions(conn, base_ids):
                        if cid not in restored_ids:
                            restored_ids.append(cid)
                    if restored_ids:
                        now = _now_iso()
                        for sid in restored_ids:
                            conn.execute(
                                "UPDATE sessions SET status = 'active', updated_at = ? "
                                "WHERE id = ? AND status = 'recycled'",
                                (now, sid),
                            )
                        conn.commit()
                except Exception:  # noqa: BLE001 — cascade is best-effort
                    logger.exception("restore_custom_agent: session cascade failed for %s", agent_id)
        finally:
            conn.close()
        return restored

    async def delete_custom_agent(self, agent_id: str, user_id: str) -> bool:
        """
        PERMANENTLY delete a custom agent (empty it from the recycling bin).
        Caller must be in admin_users. This is the ONLY place agent data is truly
        erased: the agent row, all of its agent_prompts (admin base + overrides),
        every chat session that belongs to it, and those sessions' interactions /
        summaries / pipeline events. Connection + ability rows are dropped too so
        nothing is left orphaned.
        We must NOT require is_user_default = 0: a user's own agent is normally
        their default (is_user_default = 1), so that guard would silently match
        zero rows and make the delete appear to do nothing.
        Returns True if a row was deleted, False if not found or not owned.
        """
        conn = self._get_conn()
        try:
            cursor = conn.execute(
                """DELETE FROM agents
                   WHERE id = ?
                   AND EXISTS (SELECT 1 FROM json_each(admin_users) WHERE value = ?)""",
                (agent_id, user_id),
            )
            if cursor.rowcount > 0:
                # Cascade: if this agent orchestrated any clones (in its sessions),
                # reap them too — clone agents, their sessions + transcripts — before
                # we drop this agent's own sessions. Best-effort; status='clone' only.
                try:
                    orch_sids = [row[0] for row in conn.execute(
                        "SELECT id FROM sessions WHERE agent_id = ?", (agent_id,)).fetchall()]
                    if orch_sids:
                        cascade_delete_clones(conn, orch_sids)
                except Exception:  # noqa: BLE001
                    pass
                # Children first, then the session rows themselves.
                conn.execute(
                    """DELETE FROM interactions
                       WHERE session_id IN (SELECT id FROM sessions WHERE agent_id = ?)""",
                    (agent_id,),
                )
                for tbl in ("session_summaries", "pipeline_events",
                            "session_runs", "skill_executions",
                            "run_contract_checks", "run_contract_state"):
                    try:
                        conn.execute(
                            f"""DELETE FROM {tbl}
                               WHERE session_id IN (SELECT id FROM sessions WHERE agent_id = ?)""",
                            (agent_id,),
                        )
                    except Exception:
                        pass
                conn.execute("DELETE FROM sessions WHERE agent_id = ?", (agent_id,))
                conn.execute("DELETE FROM agent_prompts WHERE agent_id = ?", (agent_id,))
                # Automation surface too: leaving agent_automations behind means
                # the scheduler keeps claiming rows for an agent that no longer
                # exists; runs/subscriptions go with them.
                for tbl in ("agent_connections", "agent_abilities", "agent_soft_abilities",
                            "agent_automations", "agent_event_subscriptions",
                            "automation_runs"):
                    try:
                        conn.execute(f"DELETE FROM {tbl} WHERE agent_id = ?", (agent_id,))
                    except Exception:
                        pass
                # ── Vault cleanup: OAuth tokens + ability creds for this agent ──
                # OAuth tokens are stored as (user_id, provider, "oauth:<agent_id>")
                # in vault_agent. Ability creds use label="agent:<agent_id>".
                # BYO client secrets use service="byo_cred:<provider>".
                try:
                    conn.execute(
                        "DELETE FROM vault_agent.auth_elements "
                        "WHERE label = ? OR label = ? OR label LIKE ?",
                        (f"oauth:{agent_id}", f"agent:{agent_id}", f"agent:{agent_id}:%"),
                    )
                except Exception:
                    pass
            conn.commit()
            # ── Purge the per-agent authority store ──
            try:
                from app.agent_workspace import purge_agent_home
                purge_agent_home(agent_id)
            except Exception:
                pass
            return cursor.rowcount > 0
        finally:
            conn.close()

    async def get_user_default_agent_id(self, user_id: str) -> Optional[str]:
        """Return the user's preferred default_agent_id, or None if not set."""
        profile = await self.get_user_profile(user_id)
        if profile:
            return profile.get("default_agent_id")
        return None

    async def set_user_default_agent(self, user_id: str, agent_id: str) -> None:
        """
        Set the user's preferred default agent.
        Upserts a user_profiles row and updates default_agent_id.
        """
        now = _now_iso()
        conn = self._get_conn()
        try:
            conn.execute(
                """INSERT INTO user_profiles (user_id, is_admin, default_agent_id, created_at, updated_at)
                   VALUES (?, 0, ?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET
                     default_agent_id = excluded.default_agent_id,
                     updated_at = excluded.updated_at""",
                (user_id, agent_id, now, now),
            )
            conn.commit()
        finally:
            conn.close()

    async def update_agent_fields(
        self,
        agent_id: str,
        user_id: str,
        updates: dict,
        *,
        allow_install_admin: bool = False,
    ) -> Optional[dict]:
        """
        Update editable fields on a custom agent. Caller must be in admin_users.
        Prompt slots are NOT updated here — use upsert_slot / replace_slots instead.
        Returns the updated agent row dict, or None if not found/not owned.
        """
        ALLOWED = {
            "name", "description", "max_turn_count", "max_wall_seconds",
            "model", "temperature", "max_tokens",
            "allowed_tools", "custom_tool_ids",
            "trigger_type", "trigger_key", "loop_logic",
            "safety_policy", "user_mode", "metadata",
            "sort_order",
            "max_identical_tool_calls", "max_stall_strikes",
        }
        safe = {}
        for k, v in updates.items():
            if k not in ALLOWED:
                continue
            # Serialize list/dict fields to JSON strings for storage
            if k in ("allowed_tools", "custom_tool_ids", "loop_logic") and isinstance(v, list):
                v = json.dumps(v)
            if k in ("safety_policy", "metadata") and isinstance(v, dict):
                v = json.dumps(v)
            safe[k] = v
        if not safe:
            # Nothing to update; return current state
            conn = self._get_conn()
            try:
                row = conn.execute(
                    """SELECT * FROM agents WHERE id = ?
                       AND EXISTS (SELECT 1 FROM json_each(admin_users) WHERE value = ?)""",
                    (agent_id, user_id),
                ).fetchone()
            finally:
                conn.close()
            return dict(row) if row else None

        now = _now_iso()
        safe["updated_at"] = now
        set_clause = ", ".join(f"{k} = ?" for k in safe)
        values = list(safe.values()) + [agent_id]
        owner_clause = ""
        if not allow_install_admin:
            owner_clause = " AND EXISTS (SELECT 1 FROM json_each(admin_users) WHERE value = ?)"
            values.append(user_id)

        conn = self._get_conn()
        try:
            cursor = conn.execute(
                f"UPDATE agents SET {set_clause} WHERE id = ?{owner_clause}",
                values,
            )
            conn.commit()
            if cursor.rowcount == 0:
                return None
            row = conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
        finally:
            conn.close()

        result = dict(row) if row else None
        if result:
            result["source"] = "custom"
        return result

    async def reorder_agents(self, user_id: str, ordered_ids: List[str]) -> int:
        """
        Persist the manual display order for a user's agents.

        Assigns sort_order = position (0-based) to each agent in ``ordered_ids``,
        but only for agents the caller administers (present in admin_users).
        Returns the number of rows updated. Agents not listed keep their
        existing sort_order — they sort after the ordered set via the
        NULLS-LAST clause in list_agents_for_user.
        """
        if not ordered_ids:
            return 0
        now = _now_iso()
        updated = 0
        conn = self._get_conn()
        try:
            for position, agent_id in enumerate(ordered_ids):
                cursor = conn.execute(
                    """UPDATE agents SET sort_order = ?, updated_at = ?
                       WHERE id = ?
                         AND EXISTS (SELECT 1 FROM json_each(admin_users) WHERE value = ?)""",
                    (position, now, agent_id, user_id),
                )
                updated += cursor.rowcount
            conn.commit()
        finally:
            conn.close()
        return updated

    # ---- Agent Connections ----

    @turn_cached
    async def get_agent_connections(self, agent_id: str) -> List[dict]:
        """Return all agent_connections rows for an agent."""
        try:
            from app.db.storage_layout import is_layout_active
            if is_layout_active() and self._schema_plane != "agent":
                from app.db.agent_store import get_agent_store
                return await get_agent_store(agent_id).list_connections()
        except Exception as exc:
            logger.debug("Agent connection read-through failed for %s: %s", agent_id, exc)
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM agent_connections WHERE agent_id = ? ORDER BY section, connection_type",
                (agent_id,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    async def upsert_agent_connection(
        self,
        agent_id: str,
        connection_type: str,
        section: str,
        enabled: bool,
        config: dict,
    ) -> dict:
        """Insert or update a connection row. Returns the final row."""
        now = _now_iso()
        config_str = json.dumps(config)
        async with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    """INSERT INTO agent_connections
                           (id, agent_id, connection_type, section, enabled, config, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(agent_id, connection_type) DO UPDATE SET
                           section = excluded.section,
                           enabled = excluded.enabled,
                           config  = excluded.config,
                           updated_at = excluded.updated_at""",
                    (_uuid(), agent_id, connection_type, section,
                     1 if enabled else 0, config_str, now, now),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT * FROM agent_connections WHERE agent_id = ? AND connection_type = ?",
                    (agent_id, connection_type),
                ).fetchone()
                result = dict(row) if row else {}
            finally:
                conn.close()
        return result

    async def get_all_connections_by_type(self, connection_type: str) -> List[dict]:
        """Return all enabled connections of a given type across all agents."""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM agent_connections WHERE connection_type = ? AND enabled = 1",
                (connection_type,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ---- Agent Abilities (per-agent OAuth capability rows) ----

    # ---- Soft Abilities (per-agent declarative skills/workflows) ----

    async def get_agent_soft_abilities(self, agent_id: str, enabled_only: bool = False) -> List[dict]:
        conn = None
        try:
            from app.db.storage_layout import is_layout_active
            if is_layout_active() and self._schema_plane != "agent":
                from app.db.agent_store import get_agent_store
                rows = await get_agent_store(agent_id).list_soft_abilities(enabled_only=enabled_only)
            else:
                conn = self._get_conn()
                sql = "SELECT * FROM agent_soft_abilities WHERE agent_id = ?"
                params = [agent_id]
                if enabled_only:
                    sql += " AND enabled = 1 AND status = 'ready'"
                sql += " ORDER BY LOWER(display_name)"
                rows = conn.execute(sql, params).fetchall()
            out = []
            for row in rows:
                item = dict(row)
                item["enabled"] = bool(item.get("enabled"))
                for key, fallback in (("workflow", {}), ("allowed_tools", []),
                                      ("credential_schema", []), ("policy", {})):
                    try:
                        item[key] = json.loads(item.get(key) or json.dumps(fallback))
                    except Exception:
                        item[key] = fallback
                out.append(item)
            return out
        finally:
            if conn is not None:
                conn.close()

    async def upsert_agent_soft_ability(self, ability: dict) -> dict:
        now = _now_iso()
        ability_id = ability.get("id") or _uuid()
        async with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    """INSERT INTO agent_soft_abilities
                       (id, agent_id, slug, display_name, description, icon, enabled,
                        skill_summary, skill_body, workflow, allowed_tools,
                        credential_schema, policy, status, version, created_by,
                        created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(id) DO UPDATE SET
                         slug=excluded.slug, display_name=excluded.display_name,
                         description=excluded.description, icon=excluded.icon,
                         enabled=excluded.enabled, skill_summary=excluded.skill_summary,
                         skill_body=excluded.skill_body, workflow=excluded.workflow,
                         allowed_tools=excluded.allowed_tools,
                         credential_schema=excluded.credential_schema,
                         policy=excluded.policy, status=excluded.status,
                         version=agent_soft_abilities.version + 1,
                         updated_at=excluded.updated_at""",
                    (ability_id, ability["agent_id"], ability["slug"], ability["display_name"],
                     ability.get("description", ""), ability.get("icon", "sparkles"),
                     1 if ability.get("enabled", True) else 0,
                     ability.get("skill_summary", ""), ability.get("skill_body", ""),
                     json.dumps(ability.get("workflow") or {}),
                     json.dumps(ability.get("allowed_tools") or []),
                     json.dumps(ability.get("credential_schema") or []),
                     json.dumps(ability.get("policy") or {}), ability.get("status", "draft"),
                     1, ability["created_by"], now, now),
                )
                conn.commit()
            finally:
                conn.close()
        rows = await self.get_agent_soft_abilities(ability["agent_id"])
        return next((r for r in rows if r["id"] == ability_id), {})

    async def delete_agent_soft_ability(self, agent_id: str, ability_id: str) -> bool:
        async with self._write_lock:
            conn = self._get_conn()
            try:
                cur = conn.execute(
                    "DELETE FROM agent_soft_abilities WHERE id = ? AND agent_id = ?",
                    (ability_id, agent_id),
                )
                conn.commit()
                deleted = cur.rowcount > 0
            finally:
                conn.close()
        return deleted

    async def record_soft_ability_run(self, ability_id: str, agent_id: str,
                                      user_id: str, session_id: str,
                                      ability_version: int, status: str,
                                      tools: List[str], elapsed_ms: int) -> dict:
        run_id = _uuid()
        async with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    """INSERT INTO soft_ability_runs
                       (id, ability_id, agent_id, user_id, session_id,
                        ability_version, status, tools, elapsed_ms, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (run_id, ability_id, agent_id, user_id, session_id or "",
                     int(ability_version or 1), status, json.dumps(tools or []),
                     int(elapsed_ms or 0), _now_iso()),
                )
                conn.commit()
                return {"id": run_id, "status": status}
            finally:
                conn.close()

    async def get_agent_abilities(self, agent_id: str) -> List[dict]:
        """Return all agent_abilities rows for an agent. Empty list when none exist."""
        conn = None
        try:
            from app.db.storage_layout import is_layout_active
            if is_layout_active() and self._schema_plane != "agent":
                from app.db.agent_store import get_agent_store
                rows = await get_agent_store(agent_id).list_abilities()
            else:
                conn = self._get_conn()
                rows = conn.execute(
                    "SELECT * FROM agent_abilities WHERE agent_id = ? ORDER BY ability_id",
                    (agent_id,),
                ).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["enabled"] = bool(d.get("enabled"))
                out.append(d)
            return out
        finally:
            if conn is not None:
                conn.close()

    async def upsert_agent_ability(
        self,
        agent_id: str,
        ability_id: str,
        *,
        enabled: Optional[bool] = None,
        source: Optional[str] = None,
        byo_client_id: Optional[str] = None,
        byo_client_secret_ref: Optional[str] = None,
        config: Optional[dict] = None,
    ) -> dict:
        """Insert or partially-update an agent_ability row. Returns the resulting row.

        Only fields explicitly passed (not None) are written so callers can
        toggle `enabled` without clobbering BYO creds, and vice versa.
        """
        now = _now_iso()
        async with self._write_lock:
            conn = self._get_conn()
            try:
                # Read existing row (if any) to preserve untouched fields.
                existing = conn.execute(
                    "SELECT * FROM agent_abilities WHERE agent_id = ? AND ability_id = ?",
                    (agent_id, ability_id),
                ).fetchone()
                if existing:
                    new_enabled = 1 if (enabled if enabled is not None else existing["enabled"]) else 0
                    new_source = source if source is not None else existing["source"]
                    new_byo_cid = byo_client_id if byo_client_id is not None else existing["byo_client_id"]
                    new_byo_sec = byo_client_secret_ref if byo_client_secret_ref is not None else existing["byo_client_secret_ref"]
                    if config is None:
                        cfg_str = existing["config"] or "{}"
                    else:
                        cfg_str = json.dumps(config)
                    conn.execute(
                        """UPDATE agent_abilities
                           SET enabled = ?, source = ?, byo_client_id = ?,
                               byo_client_secret_ref = ?, config = ?, updated_at = ?
                           WHERE agent_id = ? AND ability_id = ?""",
                        (new_enabled, new_source, new_byo_cid, new_byo_sec,
                         cfg_str, now, agent_id, ability_id),
                    )
                else:
                    conn.execute(
                        """INSERT INTO agent_abilities
                            (id, agent_id, ability_id, source, enabled,
                             byo_client_id, byo_client_secret_ref, config,
                             created_at, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            _uuid(), agent_id, ability_id,
                            source or "platform",
                            1 if (enabled or False) else 0,
                            byo_client_id or "",
                            byo_client_secret_ref or "",
                            json.dumps(config or {}),
                            now, now,
                        ),
                    )
                conn.commit()
                row = conn.execute(
                    "SELECT * FROM agent_abilities WHERE agent_id = ? AND ability_id = ?",
                    (agent_id, ability_id),
                ).fetchone()
                if not row:
                    return {}
                d = dict(row)
                d["enabled"] = bool(d.get("enabled"))
                return d
            finally:
                conn.close()

    async def delete_agent_ability(self, agent_id: str, ability_id: str) -> bool:
        async with self._write_lock:
            conn = self._get_conn()
            try:
                cur = conn.execute(
                    "DELETE FROM agent_abilities WHERE agent_id = ? AND ability_id = ?",
                    (agent_id, ability_id),
                )
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    async def get_agent_byo_creds(self, agent_id: str, provider: str) -> tuple:
        """Return (client_id, client_secret) for any BYO ability on this provider.

        All BYO rows for a given provider on a single agent share creds — they
        come from the same Google Cloud / app-of-apps project. Returns ("","")
        when no BYO ability is configured.
        """
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT ability_id, byo_client_id, byo_client_secret_ref "
                "FROM agent_abilities WHERE agent_id = ? AND source = 'byo'",
                (agent_id,),
            ).fetchall()
            for r in rows:
                ab = r["ability_id"] or ""
                if not ab.startswith(provider + "."):
                    continue
                cid = r["byo_client_id"] or ""
                csec = r["byo_client_secret_ref"] or ""
                if cid and csec:
                    return (cid, csec)
            return ("", "")
        finally:
            conn.close()

    # ── Agent membership (admin_users / member_users) ───────────────────────

    async def get_agent_roles(self, agent_id: str) -> dict:
        """Return {'admin_users': [...], 'member_users': [...], 'authorized_users': [...]} for an agent."""
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT admin_users, member_users, authorized_users FROM agents WHERE id = ?", (agent_id,)
            ).fetchone()
            if not row:
                return {"admin_users": [], "member_users": [], "authorized_users": []}
            return {
                "admin_users": json.loads(row["admin_users"] or "[]"),
                "member_users": json.loads(row["member_users"] or "[]"),
                "authorized_users": json.loads(row["authorized_users"] or "[]"),
            }
        finally:
            conn.close()

    async def set_agent_authorized(self, agent_id: str, user_id: str, authorized: bool) -> list:
        """Add or remove user_id from authorized_users. Returns updated list."""
        roles = await self.get_agent_roles(agent_id)
        current = roles.get("authorized_users") or []
        if authorized and user_id not in current:
            current = current + [user_id]
        elif not authorized and user_id in current:
            current = [u for u in current if u != user_id]
        else:
            return current
        now = _now_iso()
        async with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    "UPDATE agents SET authorized_users = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(current), now, agent_id),
                )
                conn.commit()
            finally:
                conn.close()
        return current

    async def add_agent_member(self, agent_id: str, user_id: str) -> bool:
        """
        Add user_id to an agent's member_users list if not already present.
        Returns True if the user was newly added, False if already a member.
        """
        roles = await self.get_agent_roles(agent_id)
        if user_id in roles["member_users"] or user_id in roles["admin_users"]:
            return False
        new_members = roles["member_users"] + [user_id]
        now = _now_iso()
        async with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    "UPDATE agents SET member_users = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(new_members), now, agent_id),
                )
                conn.commit()
            finally:
                conn.close()
        return True

    async def add_agent_admin(self, agent_id: str, user_id: str) -> bool:
        """
        Add user_id to an agent's admin_users list if not already present.
        Returns True if newly added, False if already an admin.
        """
        roles = await self.get_agent_roles(agent_id)
        if user_id in roles["admin_users"]:
            return False
        new_admins = roles["admin_users"] + [user_id]
        now = _now_iso()
        async with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    "UPDATE agents SET admin_users = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(new_admins), now, agent_id),
                )
                conn.commit()
            finally:
                conn.close()
        return True

    async def backfill_agent_admin_users(self) -> int:
        """
        One-time migration: for agents where admin_users is empty, no owner can be determined.
        Returns number of rows updated.
        """
        conn = self._get_conn()
        try:
            rows = conn.execute(
                """SELECT id FROM agents
                   WHERE admin_users = '[]' OR admin_users IS NULL OR admin_users = ''"""
            ).fetchall()
            updated = 0
            now = _now_iso()
            for row in rows:
                owner = None
                if owner:
                    conn.execute(
                        "UPDATE agents SET admin_users = ?, updated_at = ? WHERE id = ?",
                        (json.dumps([owner]), now, row["id"]),
                    )
                    updated += 1
            conn.commit()
            return updated
        finally:
            conn.close()

    async def is_agent_member(self, agent_id: str, user_id: str) -> bool:
        """Return True if user_id is a member or admin of the agent."""
        roles = await self.get_agent_roles(agent_id)
        return user_id in roles["member_users"] or user_id in roles["admin_users"]

    # ────────────────────────────────────────────────────────────────────
    # Per-Agent External Data Sources
    # ────────────────────────────────────────────────────────────────────

    @staticmethod
    def _row_to_data_source(row: sqlite3.Row) -> dict:
        d = dict(row)
        for k in ("config", "schema_cache", "safety_policy"):
            raw = d.get(k) or "{}"
            try:
                d[k] = json.loads(raw) if isinstance(raw, str) else raw
            except Exception:
                d[k] = {}
        return d

    # ── Gen UI (genui workspace) ───────────────────────────────────

    async def genui_list(self, user_id: str) -> List[dict]:
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM genui WHERE user_id = ? "
                "ORDER BY (slug = 'home') DESC, updated_at DESC",
                (user_id,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    async def genui_get(self, user_id: str, slug: str) -> Optional[dict]:
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM genui WHERE user_id = ? AND slug = ?",
                (user_id, slug),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    async def genui_upsert(
        self,
        user_id: str,
        slug: str,
        title: str,
        agent_context: str = "",
        html: Optional[str] = None,
        agent_id: str = "",
        session_config: Optional[str] = None,
    ) -> dict:
        async with self._write_lock:
            conn = self._get_conn()
            try:
                now = _now_iso()
                existing = conn.execute(
                    "SELECT id, html, agent_id, session_config FROM genui WHERE user_id = ? AND slug = ?",
                    (user_id, slug),
                ).fetchone()
                if existing:
                    # Owning agent: a freshly-supplied agent_id wins (the agent that
                    # just rendered manages it); otherwise keep the existing owner so
                    # a metadata-only update never clears it.
                    new_agent = agent_id or (existing["agent_id"] if "agent_id" in existing.keys() else None) or None
                    # session_config: a supplied value wins; otherwise preserve the
                    # existing config (metadata-only updates must not wipe it).
                    new_cfg = session_config if session_config is not None else (existing["session_config"] if "session_config" in existing.keys() else None) or '{}'
                    # html=None means "leave body alone" (hybrid metadata update)
                    if html is None:
                        conn.execute(
                            "UPDATE genui SET title = ?, agent_context = ?, "
                            "agent_id = ?, session_config = ?, updated_at = ? WHERE id = ?",
                            (title, agent_context, new_agent, new_cfg, now, existing["id"]),
                        )
                    else:
                        conn.execute(
                            "UPDATE genui SET title = ?, agent_context = ?, "
                            "agent_id = ?, session_config = ?, html = ?, updated_at = ? WHERE id = ?",
                            (title, agent_context, new_agent, new_cfg, html, now, existing["id"]),
                        )
                    conn.commit()
                    return dict(conn.execute(
                        "SELECT * FROM genui WHERE id = ?", (existing["id"],),
                    ).fetchone())
                row_id = _uuid()
                conn.execute(
                    "INSERT INTO genui (id, user_id, slug, title, agent_context, "
                    "agent_id, session_config, html, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (row_id, user_id, slug, title, agent_context, agent_id or None, session_config or '{}', html, now, now),
                )
                conn.commit()
                return dict(conn.execute(
                    "SELECT * FROM genui WHERE id = ?", (row_id,),
                ).fetchone())
            finally:
                conn.close()

    async def genui_delete(self, user_id: str, slug: str) -> bool:
        async with self._write_lock:
            conn = self._get_conn()
            try:
                cur = conn.execute(
                    "DELETE FROM genui WHERE user_id = ? AND slug = ?",
                    (user_id, slug),
                )
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    async def genui_get_data(self, user_id: str, slug: str) -> Optional[str]:
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT data FROM genui WHERE user_id = ? AND slug = ?",
                (user_id, slug),
            ).fetchone()
            return (row["data"] if row else None)
        except Exception:
            # Tolerate a DB created before the `data` column existed.
            return None
        finally:
            conn.close()

    async def genui_set_data(self, user_id: str, slug: str, data_json: str) -> bool:
        async with self._write_lock:
            conn = self._get_conn()
            try:
                cur = conn.execute(
                    "UPDATE genui SET data = ?, updated_at = ? "
                    "WHERE user_id = ? AND slug = ?",
                    (data_json, _now_iso(), user_id, slug),
                )
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    async def genui_get_widget(self, user_id: str, slug: str) -> Optional[str]:
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT widget FROM genui WHERE user_id = ? AND slug = ?",
                (user_id, slug),
            ).fetchone()
            return (row["widget"] if row else None)
        except Exception:
            # Tolerate a DB created before the `widget` column existed.
            return None
        finally:
            conn.close()

    async def genui_set_widget(self, user_id: str, slug: str, widget_json: str) -> bool:
        async with self._write_lock:
            conn = self._get_conn()
            try:
                cur = conn.execute(
                    "UPDATE genui SET widget = ?, updated_at = ? "
                    "WHERE user_id = ? AND slug = ?",
                    (widget_json, _now_iso(), user_id, slug),
                )
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    async def data_source_create(
        self,
        user_id: str,
        name: str,
        type: str,
        config: Optional[dict] = None,
        auth_element_id: Optional[str] = None,
        safety_policy: Optional[dict] = None,
    ) -> dict:
        ds_id = _uuid()
        now = _now_iso()
        async with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    """INSERT INTO data_sources
                       (id, user_id, name, type, config, auth_element_id,
                        schema_cache, safety_policy, status, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, '{}', ?, 'unverified', ?, ?)""",
                    (
                        ds_id, user_id, name, type,
                        json.dumps(config or {}),
                        auth_element_id,
                        json.dumps(safety_policy or {}),
                        now, now,
                    ),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT * FROM data_sources WHERE id = ?", (ds_id,)
                ).fetchone()
                return self._row_to_data_source(row)
            finally:
                conn.close()

    async def data_source_update(
        self,
        ds_id: str,
        user_id: str,
        **fields,
    ) -> Optional[dict]:
        allowed = {
            "name", "config", "auth_element_id", "schema_cache",
            "safety_policy", "status", "last_test_message",
            "last_tested_at", "last_introspected_at",
        }
        sets = []
        vals: List[Any] = []
        for k, v in fields.items():
            if k not in allowed:
                continue
            if k in ("config", "schema_cache", "safety_policy") and not isinstance(v, str):
                v = json.dumps(v or {})
            sets.append(f"{k} = ?")
            vals.append(v)
        if not sets:
            return await self.data_source_get(ds_id, user_id)
        sets.append("updated_at = ?")
        vals.append(_now_iso())
        vals.extend([ds_id, user_id])
        async with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    f"UPDATE data_sources SET {', '.join(sets)} WHERE id = ? AND user_id = ?",
                    vals,
                )
                conn.commit()
            finally:
                conn.close()
        return await self.data_source_get(ds_id, user_id)

    async def data_source_get(self, ds_id: str, user_id: Optional[str] = None) -> Optional[dict]:
        conn = self._get_conn()
        try:
            if user_id is not None:
                row = conn.execute(
                    "SELECT * FROM data_sources WHERE id = ? AND user_id = ?",
                    (ds_id, user_id),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM data_sources WHERE id = ?", (ds_id,)
                ).fetchone()
            return self._row_to_data_source(row) if row else None
        finally:
            conn.close()

    async def data_source_list(self, user_id: str) -> List[dict]:
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM data_sources WHERE user_id = ? ORDER BY created_at DESC",
                (user_id,),
            ).fetchall()
            return [self._row_to_data_source(r) for r in rows]
        finally:
            conn.close()

    async def data_source_delete(self, ds_id: str, user_id: str) -> bool:
        async with self._write_lock:
            conn = self._get_conn()
            try:
                cur = conn.execute(
                    "DELETE FROM data_sources WHERE id = ? AND user_id = ?",
                    (ds_id, user_id),
                )
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    @turn_cached
    async def agent_data_source_list(self, agent_id: str, enabled_only: bool = False) -> List[dict]:
        """Return data sources attached to the given agent, joined with source rows."""
        conn = self._get_conn()
        try:
            sql = """SELECT ads.id AS attachment_id, ads.agent_id, ads.data_source_id,
                            ads.tool_alias, ads.enabled, ads.inject_schema_in_prompt,
                            ads.created_at AS attached_at,
                            ds.user_id AS owner_user_id, ds.name, ds.type, ds.config,
                            ds.auth_element_id, ds.schema_cache, ds.safety_policy,
                            ds.status, ds.last_tested_at, ds.last_introspected_at
                     FROM agent_data_sources ads
                     JOIN data_sources ds ON ds.id = ads.data_source_id
                     WHERE ads.agent_id = ?"""
            params: List[Any] = [agent_id]
            if enabled_only:
                sql += " AND ads.enabled = 1"
            sql += " ORDER BY ads.created_at"
            rows = conn.execute(sql, params).fetchall()
            results = []
            for r in rows:
                d = dict(r)
                for k in ("config", "schema_cache", "safety_policy"):
                    try:
                        d[k] = json.loads(d.get(k) or "{}")
                    except Exception:
                        d[k] = {}
                results.append(d)
            return results
        finally:
            conn.close()

    async def agent_data_source_attach(
        self,
        agent_id: str,
        data_source_id: str,
        tool_alias: Optional[str] = None,
        inject_schema_in_prompt: bool = True,
    ) -> dict:
        attach_id = _uuid()
        now = _now_iso()
        async with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO agent_data_sources
                       (id, agent_id, data_source_id, tool_alias, enabled, inject_schema_in_prompt, created_at)
                       VALUES (?, ?, ?, ?, 1, ?, ?)""",
                    (attach_id, agent_id, data_source_id, tool_alias,
                     1 if inject_schema_in_prompt else 0, now),
                )
                # Bump the agent's version so the cached tool set (keyed on
                # agents.updated_at) refreshes on the next message.
                conn.execute("UPDATE agents SET updated_at = ? WHERE id = ?", (now, agent_id))
                conn.commit()
                row = conn.execute(
                    "SELECT * FROM agent_data_sources WHERE agent_id = ? AND data_source_id = ?",
                    (agent_id, data_source_id),
                ).fetchone()
                result = dict(row) if row else {}
            finally:
                conn.close()
        return result

    async def agent_data_source_update(
        self,
        agent_id: str,
        data_source_id: str,
        **fields,
    ) -> Optional[dict]:
        allowed = {"tool_alias", "enabled", "inject_schema_in_prompt"}
        sets = []
        vals: List[Any] = []
        for k, v in fields.items():
            if k not in allowed:
                continue
            if k in ("enabled", "inject_schema_in_prompt"):
                v = 1 if v else 0
            sets.append(f"{k} = ?")
            vals.append(v)
        if not sets:
            return None
        vals.extend([agent_id, data_source_id])
        async with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    f"UPDATE agent_data_sources SET {', '.join(sets)} WHERE agent_id = ? AND data_source_id = ?",
                    vals,
                )
                conn.execute("UPDATE agents SET updated_at = ? WHERE id = ?", (_now_iso(), agent_id))
                conn.commit()
                row = conn.execute(
                    "SELECT * FROM agent_data_sources WHERE agent_id = ? AND data_source_id = ?",
                    (agent_id, data_source_id),
                ).fetchone()
                result = dict(row) if row else None
            finally:
                conn.close()
        return result

    async def agent_data_source_detach(self, agent_id: str, data_source_id: str) -> bool:
        async with self._write_lock:
            conn = self._get_conn()
            try:
                cur = conn.execute(
                    "DELETE FROM agent_data_sources WHERE agent_id = ? AND data_source_id = ?",
                    (agent_id, data_source_id),
                )
                conn.execute("UPDATE agents SET updated_at = ? WHERE id = ?", (_now_iso(), agent_id))
                conn.commit()
                detached = cur.rowcount > 0
            finally:
                conn.close()
        return detached

    # ────────────────────────────────────────────────────────────────────
    # doc_chunks: ingestion + hybrid search (FTS5 + vector RRF)
    # ────────────────────────────────────────────────────────────────────

    async def doc_chunk_upsert(
        self,
        data_source_id: str,
        source_ref: str,
        chunk_index: int,
        chunk_text: str,
        content_hash: Optional[str] = None,
        embedding: Optional[List[float]] = None,
        metadata: Optional[dict] = None,
    ) -> str:
        chunk_id = _uuid()
        blob = None
        if embedding is not None:
            blob = struct.pack(f"{len(embedding)}f", *embedding)
        async with self._write_lock:
            conn = self._get_conn()
            try:
                # Replace by (data_source_id, source_ref, chunk_index) tuple
                conn.execute(
                    """DELETE FROM doc_chunks
                       WHERE data_source_id = ? AND source_ref = ? AND chunk_index = ?""",
                    (data_source_id, source_ref, chunk_index),
                )
                conn.execute(
                    """INSERT INTO doc_chunks
                       (id, data_source_id, source_ref, chunk_index, chunk_text,
                        content_hash, embedding, token_count, metadata)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        chunk_id, data_source_id, source_ref, chunk_index,
                        chunk_text, content_hash, blob,
                        len(chunk_text.split()) if chunk_text else 0,
                        json.dumps(metadata or {}),
                    ),
                )
                conn.commit()
                return chunk_id
            finally:
                conn.close()

    async def doc_chunk_delete_by_source_ref(self, data_source_id: str, source_ref: str) -> int:
        async with self._write_lock:
            conn = self._get_conn()
            try:
                cur = conn.execute(
                    "DELETE FROM doc_chunks WHERE data_source_id = ? AND source_ref = ?",
                    (data_source_id, source_ref),
                )
                conn.commit()
                return cur.rowcount
            finally:
                conn.close()

    async def doc_chunk_count(self, data_source_id: str) -> int:
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM doc_chunks WHERE data_source_id = ?",
                (data_source_id,),
            ).fetchone()
            return int(row["n"]) if row else 0
        finally:
            conn.close()

    async def doc_chunk_search(
        self, data_source_id: str, query: str, limit: int = 5
    ) -> List[dict]:
        """Hybrid FTS5 + vector cosine search scoped to a single data source."""
        fts_task = asyncio.create_task(self._doc_fts5_search(data_source_id, query, limit * 3))
        vec_task = asyncio.create_task(self._doc_vector_search(data_source_id, query, limit * 3))
        fts_results, vec_results = await asyncio.gather(fts_task, vec_task, return_exceptions=True)
        if isinstance(fts_results, BaseException):
            logger.warning("doc_chunks FTS5 search failed: %s", fts_results)
            fts_results = []
        if isinstance(vec_results, BaseException):
            logger.warning("doc_chunks vector search failed: %s", vec_results)
            vec_results = []
        if not vec_results:
            return (fts_results or [])[:limit]
        if not fts_results:
            return vec_results[:limit]
        k = 60
        scores: Dict[str, float] = {}
        by_id: Dict[str, dict] = {}
        for rank, h in enumerate(fts_results, start=1):
            cid = h["id"]
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank)
            by_id.setdefault(cid, h)
        for rank, h in enumerate(vec_results, start=1):
            cid = h["id"]
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank)
            by_id.setdefault(cid, h)
        merged = []
        for cid, sc in sorted(scores.items(), key=lambda x: -x[1]):
            entry = dict(by_id[cid])
            entry["rank"] = round(sc, 4)
            merged.append(entry)
        return merged[:limit]

    async def _doc_fts5_search(
        self, data_source_id: str, query: str, limit: int
    ) -> List[dict]:
        match_expr = _fts5_safe_match_query(query)
        if not match_expr:
            return []
        conn = self._get_conn()
        try:
            rows = conn.execute(
                """SELECT dc.*, rank FROM doc_chunks_fts fts
                   JOIN doc_chunks dc ON dc.rowid = fts.rowid
                   WHERE doc_chunks_fts MATCH ? AND dc.data_source_id = ?
                   ORDER BY rank LIMIT ?""",
                (match_expr, data_source_id, limit),
            ).fetchall()
            return [
                {
                    "id": r["id"],
                    "source_ref": r["source_ref"],
                    "chunk_index": r["chunk_index"],
                    "chunk_text": r["chunk_text"],
                }
                for r in rows
            ]
        finally:
            conn.close()

    async def _doc_vector_search(
        self, data_source_id: str, query_text: str, limit: int
    ) -> List[dict]:
        _ensure_np()
        conn = self._get_conn()
        try:
            rows = conn.execute(
                """SELECT id, source_ref, chunk_index, chunk_text, embedding
                   FROM doc_chunks
                   WHERE data_source_id = ? AND embedding IS NOT NULL""",
                (data_source_id,),
            ).fetchall()
        finally:
            conn.close()
        if not rows:
            return []
        try:
            q_list = await embed_text(query_text)
        except Exception as e:
            logger.warning("doc_chunks query embed failed: %s", e)
            return []
        q_vec = np.array(q_list, dtype=np.float32)
        ids = []
        vecs = []
        meta = []
        for r in rows:
            if r["embedding"]:
                v = np.frombuffer(r["embedding"], dtype=np.float32)
                if v.shape[0] == embed_dim():
                    ids.append(r["id"])
                    vecs.append(v)
                    meta.append({
                        "id": r["id"],
                        "source_ref": r["source_ref"],
                        "chunk_index": r["chunk_index"],
                        "chunk_text": r["chunk_text"],
                    })
        if not vecs:
            return []
        matrix = np.stack(vecs)
        norms = np.linalg.norm(matrix, axis=1)
        qn = np.linalg.norm(q_vec)
        scores = np.dot(matrix, q_vec) / (norms * qn + 1e-12)
        ranked = sorted(zip(meta, scores), key=lambda x: -x[1])
        out = []
        for m, sc in ranked[:limit]:
            entry = dict(m)
            entry["rank"] = round(float(sc), 4)
            out.append(entry)
        return out

    # ─── Agent automations ──────────────────────────────────────────────

    @staticmethod
    def _automation_row_to_dict(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["silent"] = bool(d.get("silent"))
        d["enabled"] = bool(d.get("enabled"))
        # Parse JSON convenience fields (raw strings kept alongside).
        for raw_key, parsed_key, default in (
            ("delivery_json", "delivery", {}),
            ("clone_abilities", "clone_abilities_list", []),
            ("memory_json", "memory", {}),
        ):
            try:
                d[parsed_key] = json.loads(d.get(raw_key) or "null")
                if d[parsed_key] is None:
                    d[parsed_key] = default
            except Exception:
                d[parsed_key] = default
        return d

    async def list_automations(
        self,
        agent_id: Optional[str] = None,
        owner_user_id: Optional[str] = None,
        bin_view: bool = False,
    ) -> List[dict]:
        """List automations. By default only active (non-recycled) rows; pass
        bin_view=True to return only rows in the recycling bin (deleted_at set)."""
        conn = self._get_conn()
        try:
            clauses = []
            params: List[Any] = []
            if agent_id:
                clauses.append("agent_id = ?")
                params.append(agent_id)
            if owner_user_id:
                clauses.append("owner_user_id = ?")
                params.append(owner_user_id)
            clauses.append("deleted_at IS NOT NULL" if bin_view else "deleted_at IS NULL")
            where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
            rows = conn.execute(
                f"SELECT * FROM agent_automations {where} ORDER BY created_at ASC",
                params,
            ).fetchall()
            return [self._automation_row_to_dict(r) for r in rows]
        finally:
            conn.close()

    async def get_automation(self, automation_id: str) -> Optional[dict]:
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM agent_automations WHERE id = ?",
                (automation_id,),
            ).fetchone()
            return self._automation_row_to_dict(row) if row else None
        finally:
            conn.close()

    async def get_automation_by_fire_token(self, automation_id: str, token: str) -> Optional[dict]:
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM agent_automations WHERE id = ? AND fire_token = ?",
                (automation_id, token),
            ).fetchone()
            return self._automation_row_to_dict(row) if row else None
        finally:
            conn.close()

    async def upsert_automation(
        self,
        *,
        agent_id: str,
        owner_user_id: str,
        source_hash: str,
        task_label: str,
        prompt: str,
        schedule_cron: str,
        schedule_natural: str = "",
        timezone: str = "UTC",
        channel: Optional[str] = None,
        channel_recipient: Optional[str] = None,
        silent: bool = False,
        enabled: bool = True,
        next_run_at: Optional[str] = None,
    ) -> dict:
        now = _now_iso()
        async with self._write_lock:
            conn = self._get_conn()
            try:
                existing = conn.execute(
                    """SELECT id FROM agent_automations
                       WHERE agent_id = ? AND owner_user_id = ? AND source_hash = ?""",
                    (agent_id, owner_user_id, source_hash),
                ).fetchone()
                if existing:
                    row_id = existing["id"]
                    conn.execute(
                        """UPDATE agent_automations SET
                              task_label = ?, prompt = ?, schedule_cron = ?,
                              schedule_natural = ?, timezone = ?,
                              channel = ?, channel_recipient = ?,
                              silent = ?, enabled = ?,
                              next_run_at = COALESCE(?, next_run_at),
                              updated_at = ?
                           WHERE id = ?""",
                        (task_label, prompt, schedule_cron, schedule_natural,
                         timezone, channel, channel_recipient,
                         1 if silent else 0, 1 if enabled else 0,
                         next_run_at, now, row_id),
                    )
                else:
                    row_id = _uuid()
                    conn.execute(
                        """INSERT INTO agent_automations
                           (id, agent_id, owner_user_id, task_label, prompt,
                            schedule_cron, schedule_natural, timezone,
                            channel, channel_recipient, silent, enabled,
                            next_run_at, source_hash, created_at, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (row_id, agent_id, owner_user_id, task_label, prompt,
                         schedule_cron, schedule_natural, timezone,
                         channel, channel_recipient,
                         1 if silent else 0, 1 if enabled else 0,
                         next_run_at, source_hash, now, now),
                    )
                conn.commit()
                row = conn.execute(
                    "SELECT * FROM agent_automations WHERE id = ?",
                    (row_id,),
                ).fetchone()
                return self._automation_row_to_dict(row)
            finally:
                conn.close()

    async def create_automation(
        self,
        *,
        agent_id: str,
        owner_user_id: str,
        prompt: str,
        task_label: str = "",
        schedule_cron: str = "",
        schedule_natural: str = "",
        schedule_kind: str = "cron",
        timezone: str = "UTC",
        next_run_at: Optional[str] = None,
        delivery_json: str = "{}",
        run_mode: str = "inline",
        runner_agent_id: Optional[str] = None,
        clone_abilities: str = "[]",
        max_per_day: Optional[int] = None,
        disable_after_failures: Optional[int] = None,
        expires_at: Optional[str] = None,
        retry_max: int = 0,
        retry_backoff_seconds: int = 0,
        memory_json: str = "{}",
        origin: str = "tool",
        enabled: bool = True,
        target_device: Optional[str] = None,
        target_offline: str = "wait",
    ) -> dict:
        """Create an imperative automation (tool / dashboard / timer).

        Unlike ``upsert_automation`` (slot reconciliation, keyed by source_hash),
        this always inserts a fresh row with a unique synthetic source_hash so it
        is NEVER touched by slot sync (which only deletes ``origin='slot'`` rows).
        """
        row_id = _uuid()
        source_hash = f"{origin}:{row_id}"
        now = _now_iso()
        if not isinstance(delivery_json, str):
            delivery_json = json.dumps(delivery_json)
        if not isinstance(clone_abilities, str):
            clone_abilities = json.dumps(clone_abilities)
        if not isinstance(memory_json, str):
            memory_json = json.dumps(memory_json)
        async with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    """INSERT INTO agent_automations
                       (id, agent_id, owner_user_id, task_label, prompt,
                        schedule_cron, schedule_natural, schedule_kind, timezone,
                        silent, enabled, next_run_at, source_hash,
                        delivery_json, run_mode, runner_agent_id, clone_abilities,
                        max_per_day, disable_after_failures, expires_at,
                        retry_max, retry_backoff_seconds, memory_json, origin,
                        target_device, target_offline,
                        created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?,
                               ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (row_id, agent_id, owner_user_id, task_label, prompt,
                     schedule_cron, schedule_natural, schedule_kind, timezone,
                     1 if enabled else 0, next_run_at, source_hash,
                     delivery_json, run_mode, runner_agent_id, clone_abilities,
                     max_per_day, disable_after_failures, expires_at,
                     retry_max, retry_backoff_seconds, memory_json, origin,
                     (target_device or None), (target_offline or "wait"),
                     now, now),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT * FROM agent_automations WHERE id = ?", (row_id,),
                ).fetchone()
                return self._automation_row_to_dict(row)
            finally:
                conn.close()

    async def update_automation(
        self,
        automation_id: str,
        **fields,
    ) -> Optional[dict]:
        if not fields:
            return await self.get_automation(automation_id)
        allowed = {
            "task_label", "prompt", "schedule_cron", "schedule_natural",
            "timezone", "channel", "channel_recipient", "silent",
            "enabled", "next_run_at", "last_run_at", "last_status",
            "last_error", "last_session_id",
            "fire_token", "external_job_id", "external_provider",
            # ── feature-rich automation ──
            "schedule_kind", "delivery_json", "run_mode", "runner_agent_id",
            "clone_abilities", "max_per_day", "runs_today", "runs_today_date",
            "fail_count", "disable_after_failures", "expires_at",
            "retry_max", "retry_backoff_seconds", "next_retry_at",
            "memory_json", "origin",
            # ── cross-device targeting ──
            "target_device", "target_offline",
        }
        # JSON-encode dict/list values destined for *_json / clone_abilities columns.
        _json_cols = {"delivery_json", "memory_json", "clone_abilities"}
        sets = []
        params: List[Any] = []
        for k, v in fields.items():
            if k not in allowed:
                continue
            if k in ("silent", "enabled"):
                v = 1 if v else 0
            elif k in _json_cols and not isinstance(v, str):
                v = json.dumps(v)
            sets.append(f"{k} = ?")
            params.append(v)
        if not sets:
            return await self.get_automation(automation_id)
        sets.append("updated_at = ?")
        params.append(_now_iso())
        params.append(automation_id)
        async with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    f"UPDATE agent_automations SET {', '.join(sets)} WHERE id = ?",
                    params,
                )
                conn.commit()
            finally:
                conn.close()
        return await self.get_automation(automation_id)

    async def delete_automation(self, automation_id: str) -> bool:
        async with self._write_lock:
            conn = self._get_conn()
            try:
                cur = conn.execute(
                    "DELETE FROM agent_automations WHERE id = ?",
                    (automation_id,),
                )
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    async def trash_automation(self, automation_id: str) -> bool:
        """Soft-delete an automation into the recycling bin.

        Three things happen atomically so the binned row is inert and invisible
        to the rest of the engine:
          • deleted_at is stamped  → it drops out of the active dashboard list.
          • enabled = 0            → the scheduler only claims enabled=1 rows, so
                                     a binned automation never fires (no hot-path
                                     query change needed).
          • source_hash is mangled → it releases its (agent, owner, hash) unique
                                     slot, so slot reconciliation can recreate a
                                     fresh active row without colliding and never
                                     resurrects this binned one.
        """
        ts = _now_iso()
        async with self._write_lock:
            conn = self._get_conn()
            try:
                cur = conn.execute(
                    "UPDATE agent_automations "
                    "SET deleted_at = ?, enabled = 0, "
                    "    source_hash = source_hash || ':trashed:' || ?, "
                    "    updated_at = ? "
                    "WHERE id = ? AND deleted_at IS NULL",
                    (ts, ts, ts, automation_id),
                )
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    async def restore_automation(self, automation_id: str) -> bool:
        """Restore a binned automation back to active and re-enable it. We flip
        origin to 'dashboard' so it is treated as user-managed (imperative) and
        slot reconciliation leaves it alone — its old slot hash was mangled away
        and may have been recreated as a separate active row."""
        async with self._write_lock:
            conn = self._get_conn()
            try:
                cur = conn.execute(
                    "UPDATE agent_automations "
                    "SET deleted_at = NULL, enabled = 1, origin = 'dashboard', "
                    "    updated_at = ? "
                    "WHERE id = ? AND deleted_at IS NOT NULL",
                    (_now_iso(), automation_id),
                )
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    async def delete_automations_not_in(
        self,
        agent_id: str,
        owner_user_id: str,
        keep_hashes: List[str],
    ) -> int:
        """Remove SLOT-managed rows whose source_hash is not in keep_hashes.

        Scoped to ``origin='slot'`` so imperative automations (created via tools
        or the dashboard) survive prompt-slot reconciliation.
        """
        async with self._write_lock:
            conn = self._get_conn()
            try:
                if keep_hashes:
                    placeholders = ",".join(["?"] * len(keep_hashes))
                    cur = conn.execute(
                        f"""DELETE FROM agent_automations
                            WHERE agent_id = ? AND owner_user_id = ?
                              AND origin = 'slot'
                              AND deleted_at IS NULL
                              AND source_hash NOT IN ({placeholders})""",
                        [agent_id, owner_user_id, *keep_hashes],
                    )
                else:
                    cur = conn.execute(
                        """DELETE FROM agent_automations
                           WHERE agent_id = ? AND owner_user_id = ?
                             AND origin = 'slot'
                             AND deleted_at IS NULL""",
                        (agent_id, owner_user_id),
                    )
                conn.commit()
                return cur.rowcount
            finally:
                conn.close()

    async def delete_all_automations(self) -> int:
        """Wipe every row from agent_automations. Returns the count removed."""
        async with self._write_lock:
            conn = self._get_conn()
            try:
                cur = conn.execute("DELETE FROM agent_automations")
                conn.commit()
                return cur.rowcount
            finally:
                conn.close()

    async def claim_due_automations(self, now_iso: Optional[str] = None) -> List[dict]:
        """Return enabled automation rows due to run now.

        Due = a scheduled time (next_run_at) has elapsed OR a pending retry
        (next_retry_at) has elapsed. Expired rows (expires_at in the past) are
        excluded so the engine never fires them.
        """
        ts = now_iso or _now_iso()
        conn = self._get_conn()
        try:
            rows = conn.execute(
                """SELECT * FROM agent_automations
                   WHERE enabled = 1
                     AND (expires_at IS NULL OR expires_at > ?)
                     AND (
                           (next_run_at   IS NOT NULL AND next_run_at   <= ?)
                        OR (next_retry_at IS NOT NULL AND next_retry_at <= ?)
                     )
                   ORDER BY COALESCE(next_retry_at, next_run_at) ASC""",
                (ts, ts, ts),
            ).fetchall()
            return [self._automation_row_to_dict(r) for r in rows]
        finally:
            conn.close()

    # ─── Automation run history ─────────────────────────────────────────

    async def create_automation_run(
        self,
        *,
        kind: str,
        agent_id: str,
        owner_user_id: str,
        automation_id: Optional[str] = None,
        subscription_id: Optional[str] = None,
        runner_agent_id: Optional[str] = None,
        run_mode: str = "inline",
        session_id: Optional[str] = None,
        status: str = "running",
    ) -> str:
        """Open a run-history row; returns its id. Close it with finish_automation_run."""
        run_id = _uuid()
        now = _now_iso()
        async with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    """INSERT INTO automation_runs
                       (id, kind, automation_id, subscription_id, agent_id, owner_user_id,
                        runner_agent_id, run_mode, session_id, status, started_at, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (run_id, kind, automation_id, subscription_id, agent_id, owner_user_id,
                     runner_agent_id, run_mode, session_id, status, now, now),
                )
                conn.commit()
            finally:
                conn.close()
        return run_id

    async def finish_automation_run(
        self,
        run_id: str,
        *,
        status: str,
        reply_excerpt: Optional[str] = None,
        delivery_json: Optional[Any] = None,
        error: Optional[str] = None,
        session_id: Optional[str] = None,
        runner_agent_id: Optional[str] = None,
    ) -> None:
        sets = ["status = ?", "finished_at = ?"]
        params: List[Any] = [status, _now_iso()]
        if reply_excerpt is not None:
            sets.append("reply_excerpt = ?"); params.append(reply_excerpt[:2000])
        if delivery_json is not None:
            sets.append("delivery_json = ?")
            params.append(delivery_json if isinstance(delivery_json, str) else json.dumps(delivery_json))
        if error is not None:
            sets.append("error = ?"); params.append(str(error)[:1000])
        if session_id is not None:
            sets.append("session_id = ?"); params.append(session_id)
        if runner_agent_id is not None:
            sets.append("runner_agent_id = ?"); params.append(runner_agent_id)
        params.append(run_id)
        async with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    f"UPDATE automation_runs SET {', '.join(sets)} WHERE id = ?", params,
                )
                conn.commit()
            finally:
                conn.close()

    async def list_automation_runs(
        self,
        *,
        automation_id: Optional[str] = None,
        subscription_id: Optional[str] = None,
        owner_user_id: Optional[str] = None,
        limit: int = 50,
    ) -> List[dict]:
        conn = self._get_conn()
        try:
            clauses, params = [], []
            if automation_id:
                clauses.append("automation_id = ?"); params.append(automation_id)
            if subscription_id:
                clauses.append("subscription_id = ?"); params.append(subscription_id)
            if owner_user_id:
                clauses.append("owner_user_id = ?"); params.append(owner_user_id)
            where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
            params.append(int(limit))
            rows = conn.execute(
                f"SELECT * FROM automation_runs {where} ORDER BY created_at DESC LIMIT ?",
                params,
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    async def list_clone_agents(self, owner_user_id: str) -> List[dict]:
        """Return clone agents owned by this user (status='clone'), each annotated
        with its master id parsed from metadata.clone_of."""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM agents WHERE status = 'clone' ORDER BY created_at DESC",
            ).fetchall()
            out: List[dict] = []
            for r in rows:
                d = dict(r)
                meta = {}
                try:
                    meta = json.loads(d.get("metadata") or "{}")
                except Exception:
                    meta = {}
                if (meta.get("owner_user_id") or "") != owner_user_id:
                    continue
                d["clone_of"] = meta.get("clone_of")
                out.append(d)
            return out
        finally:
            conn.close()

    # ─── Agent event subscriptions ──────────────────────────────────────

    @staticmethod
    def _event_sub_row_to_dict(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["silent"] = bool(d.get("silent"))
        d["enabled"] = bool(d.get("enabled"))
        for k in ("filter_json", "external_metadata"):
            raw = d.get(k) or "{}"
            try:
                d[k.replace("_json", "") if k == "filter_json" else k] = json.loads(raw) if isinstance(raw, str) else (raw or {})
            except Exception:
                d[k.replace("_json", "") if k == "filter_json" else k] = {}
        # Expose `filter` as the parsed dict; keep `filter_json` for raw access.
        if "filter" not in d:
            d["filter"] = {}
        # Parse feature-rich JSON convenience fields (present after migration 029).
        for raw_key, parsed_key, default in (
            ("delivery_json", "delivery", {}),
            ("clone_abilities", "clone_abilities_list", []),
            ("memory_json", "memory", {}),
        ):
            try:
                d[parsed_key] = json.loads(d.get(raw_key) or "null")
                if d[parsed_key] is None:
                    d[parsed_key] = default
            except Exception:
                d[parsed_key] = default
        return d

    async def list_event_subscriptions(
        self,
        agent_id: Optional[str] = None,
        owner_user_id: Optional[str] = None,
        source: Optional[str] = None,
        enabled_only: bool = False,
        bin_view: bool = False,
    ) -> List[dict]:
        """List event subscriptions. By default only active (non-recycled) rows;
        pass bin_view=True for only rows in the recycling bin (deleted_at set)."""
        conn = self._get_conn()
        try:
            clauses, params = [], []
            if agent_id:
                clauses.append("agent_id = ?"); params.append(agent_id)
            if owner_user_id:
                clauses.append("owner_user_id = ?"); params.append(owner_user_id)
            if source:
                clauses.append("source = ?"); params.append(source)
            if enabled_only:
                clauses.append("enabled = 1")
            clauses.append("deleted_at IS NOT NULL" if bin_view else "deleted_at IS NULL")
            where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
            rows = conn.execute(
                f"SELECT * FROM agent_event_subscriptions {where} ORDER BY created_at ASC",
                params,
            ).fetchall()
            return [self._event_sub_row_to_dict(r) for r in rows]
        finally:
            conn.close()

    async def get_event_subscription(self, sub_id: str) -> Optional[dict]:
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM agent_event_subscriptions WHERE id = ?",
                (sub_id,),
            ).fetchone()
            return self._event_sub_row_to_dict(row) if row else None
        finally:
            conn.close()

    async def find_event_subscriptions_for_event(
        self,
        owner_user_id: str,
        source: str,
        event_type: str,
    ) -> List[dict]:
        """All enabled subs matching (owner, source, event_type). Router applies filters."""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                """SELECT * FROM agent_event_subscriptions
                   WHERE enabled = 1
                     AND owner_user_id = ?
                     AND source = ?
                     AND event_type = ?
                   ORDER BY created_at ASC""",
                (owner_user_id, source, event_type),
            ).fetchall()
            return [self._event_sub_row_to_dict(r) for r in rows]
        finally:
            conn.close()

    async def upsert_event_subscription(
        self,
        *,
        agent_id: str,
        owner_user_id: str,
        source_hash: str,
        source: str,
        event_type: str,
        filter_dict: Optional[dict] = None,
        task_label: str = "",
        prompt: str = "",
        trigger_natural: str = "",
        channel: Optional[str] = None,
        channel_recipient: Optional[str] = None,
        silent: bool = False,
        enabled: bool = True,
        delivery_json: str = "{}",
        run_mode: str = "inline",
        clone_abilities: str = "[]",
        max_per_day: Optional[int] = None,
        disable_after_failures: Optional[int] = None,
        expires_at: Optional[str] = None,
        retry_max: int = 0,
        retry_backoff_seconds: int = 0,
        origin: str = "slot",
    ) -> dict:
        now = _now_iso()
        filter_json = json.dumps(filter_dict or {}, sort_keys=True)
        if not isinstance(delivery_json, str):
            delivery_json = json.dumps(delivery_json)
        if not isinstance(clone_abilities, str):
            clone_abilities = json.dumps(clone_abilities)
        async with self._write_lock:
            conn = self._get_conn()
            try:
                existing = conn.execute(
                    """SELECT id FROM agent_event_subscriptions
                       WHERE agent_id = ? AND owner_user_id = ? AND source_hash = ?""",
                    (agent_id, owner_user_id, source_hash),
                ).fetchone()
                if existing:
                    row_id = existing["id"]
                    conn.execute(
                        """UPDATE agent_event_subscriptions SET
                              source = ?, event_type = ?, filter_json = ?,
                              task_label = ?, prompt = ?, trigger_natural = ?,
                              channel = ?, channel_recipient = ?,
                              silent = ?, enabled = ?,
                              delivery_json = ?, run_mode = ?, clone_abilities = ?,
                              max_per_day = ?, disable_after_failures = ?, expires_at = ?,
                              retry_max = ?, retry_backoff_seconds = ?, origin = ?,
                              updated_at = ?
                           WHERE id = ?""",
                        (source, event_type, filter_json,
                         task_label, prompt, trigger_natural,
                         channel, channel_recipient,
                         1 if silent else 0, 1 if enabled else 0,
                         delivery_json, run_mode, clone_abilities,
                         max_per_day, disable_after_failures, expires_at,
                         retry_max, retry_backoff_seconds, origin,
                         now, row_id),
                    )
                else:
                    row_id = _uuid()
                    conn.execute(
                        """INSERT INTO agent_event_subscriptions
                           (id, agent_id, owner_user_id, source, event_type, filter_json,
                            task_label, prompt, trigger_natural, channel, channel_recipient,
                            silent, enabled, source_hash,
                            delivery_json, run_mode, clone_abilities,
                            max_per_day, disable_after_failures, expires_at,
                            retry_max, retry_backoff_seconds, origin,
                            created_at, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                   ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (row_id, agent_id, owner_user_id, source, event_type, filter_json,
                         task_label, prompt, trigger_natural, channel, channel_recipient,
                         1 if silent else 0, 1 if enabled else 0, source_hash,
                         delivery_json, run_mode, clone_abilities,
                         max_per_day, disable_after_failures, expires_at,
                         retry_max, retry_backoff_seconds, origin,
                         now, now),
                    )
                conn.commit()
                row = conn.execute(
                    "SELECT * FROM agent_event_subscriptions WHERE id = ?",
                    (row_id,),
                ).fetchone()
                return self._event_sub_row_to_dict(row)
            finally:
                conn.close()

    async def update_event_subscription(self, sub_id: str, **fields) -> Optional[dict]:
        if not fields:
            return await self.get_event_subscription(sub_id)
        allowed = {
            "task_label", "prompt", "trigger_natural", "filter_json",
            "channel", "channel_recipient", "silent", "enabled",
            "external_subscription_id", "external_resource_id",
            "external_expiration_at", "external_metadata",
            "poll_cursor", "poll_interval_seconds", "last_polled_at",
            "last_event_at", "last_event_external_id",
            "last_status", "last_error", "last_session_id",
            "fire_count",
            # ── feature-rich automation ──
            "delivery_json", "run_mode", "runner_agent_id", "clone_abilities",
            "max_per_day", "runs_today", "runs_today_date", "fail_count",
            "disable_after_failures", "expires_at", "retry_max",
            "retry_backoff_seconds", "next_retry_at", "memory_json", "origin",
        }
        sets, params = [], []
        for k, v in fields.items():
            if k not in allowed:
                continue
            if k in ("silent", "enabled"):
                v = 1 if v else 0
            if k in ("filter_json", "external_metadata") and isinstance(v, dict):
                v = json.dumps(v, sort_keys=True)
            if k in ("delivery_json", "memory_json", "clone_abilities") and not isinstance(v, str):
                v = json.dumps(v)
            sets.append(f"{k} = ?"); params.append(v)
        if not sets:
            return await self.get_event_subscription(sub_id)
        sets.append("updated_at = ?"); params.append(_now_iso())
        params.append(sub_id)
        async with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    f"UPDATE agent_event_subscriptions SET {', '.join(sets)} WHERE id = ?",
                    params,
                )
                conn.commit()
            finally:
                conn.close()
        return await self.get_event_subscription(sub_id)

    async def delete_event_subscription(self, sub_id: str) -> bool:
        async with self._write_lock:
            conn = self._get_conn()
            try:
                cur = conn.execute(
                    "DELETE FROM agent_event_subscriptions WHERE id = ?",
                    (sub_id,),
                )
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    async def trash_event_subscription(self, sub_id: str) -> bool:
        """Soft-delete an event subscription into the recycling bin. enabled=0
        keeps it dormant for every consumer (matcher/renewer/poller all filter
        enabled=1); source_hash is mangled so slot reconciliation can't resurrect
        it. The external provider subscription is left registered and is only
        torn down on permanent delete."""
        ts = _now_iso()
        async with self._write_lock:
            conn = self._get_conn()
            try:
                cur = conn.execute(
                    "UPDATE agent_event_subscriptions "
                    "SET deleted_at = ?, enabled = 0, "
                    "    source_hash = source_hash || ':trashed:' || ?, "
                    "    updated_at = ? "
                    "WHERE id = ? AND deleted_at IS NULL",
                    (ts, ts, ts, sub_id),
                )
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    async def restore_event_subscription(self, sub_id: str) -> bool:
        """Restore a binned event subscription back to active and re-enable it.
        origin flips to 'dashboard' so slot reconciliation leaves it alone."""
        async with self._write_lock:
            conn = self._get_conn()
            try:
                cur = conn.execute(
                    "UPDATE agent_event_subscriptions "
                    "SET deleted_at = NULL, enabled = 1, origin = 'dashboard', "
                    "    updated_at = ? "
                    "WHERE id = ? AND deleted_at IS NOT NULL",
                    (_now_iso(), sub_id),
                )
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    async def delete_all_event_subscriptions(self) -> int:
        """Wipe every row from agent_event_subscriptions. Returns the count removed."""
        async with self._write_lock:
            conn = self._get_conn()
            try:
                cur = conn.execute("DELETE FROM agent_event_subscriptions")
                conn.commit()
                return cur.rowcount
            finally:
                conn.close()

    async def delete_all_ability_connections(self, connection_type: str) -> int:
        """Wipe every agent_connections row matching the given ability connection_type."""
        async with self._write_lock:
            conn = self._get_conn()
            try:
                cur = conn.execute(
                    "DELETE FROM agent_connections WHERE section = 'ability' AND connection_type = ?",
                    (connection_type,),
                )
                conn.commit()
                return cur.rowcount
            finally:
                conn.close()

    async def delete_event_subscriptions_not_in(
        self,
        agent_id: str,
        owner_user_id: str,
        keep_hashes: List[str],
    ) -> List[dict]:
        """Remove SLOT-managed rows whose source_hash is not in keep_hashes; return the
        removed rows so the caller can unregister provider-side watches.

        Scoped to ``origin='slot'`` so imperative subscriptions (tool/dashboard)
        survive prompt-slot reconciliation.
        """
        async with self._write_lock:
            conn = self._get_conn()
            try:
                if keep_hashes:
                    placeholders = ",".join(["?"] * len(keep_hashes))
                    to_remove = conn.execute(
                        f"""SELECT * FROM agent_event_subscriptions
                            WHERE agent_id = ? AND owner_user_id = ?
                              AND origin = 'slot'
                              AND deleted_at IS NULL
                              AND source_hash NOT IN ({placeholders})""",
                        [agent_id, owner_user_id, *keep_hashes],
                    ).fetchall()
                    conn.execute(
                        f"""DELETE FROM agent_event_subscriptions
                            WHERE agent_id = ? AND owner_user_id = ?
                              AND origin = 'slot'
                              AND deleted_at IS NULL
                              AND source_hash NOT IN ({placeholders})""",
                        [agent_id, owner_user_id, *keep_hashes],
                    )
                else:
                    to_remove = conn.execute(
                        """SELECT * FROM agent_event_subscriptions
                           WHERE agent_id = ? AND owner_user_id = ? AND origin = 'slot'
                             AND deleted_at IS NULL""",
                        (agent_id, owner_user_id),
                    ).fetchall()
                    conn.execute(
                        """DELETE FROM agent_event_subscriptions
                           WHERE agent_id = ? AND owner_user_id = ? AND origin = 'slot'
                             AND deleted_at IS NULL""",
                        (agent_id, owner_user_id),
                    )
                conn.commit()
                return [self._event_sub_row_to_dict(r) for r in to_remove]
            finally:
                conn.close()

    async def find_event_subscriptions_by_external(
        self,
        source: str,
        external_subscription_id: str,
    ) -> List[dict]:
        conn = self._get_conn()
        try:
            rows = conn.execute(
                """SELECT * FROM agent_event_subscriptions
                   WHERE source = ? AND external_subscription_id = ?""",
                (source, external_subscription_id),
            ).fetchall()
            return [self._event_sub_row_to_dict(r) for r in rows]
        finally:
            conn.close()

    async def find_event_subscriptions_for_user_source(
        self,
        owner_user_id: str,
        source: str,
    ) -> List[dict]:
        conn = self._get_conn()
        try:
            rows = conn.execute(
                """SELECT * FROM agent_event_subscriptions
                   WHERE owner_user_id = ? AND source = ?""",
                (owner_user_id, source),
            ).fetchall()
            return [self._event_sub_row_to_dict(r) for r in rows]
        finally:
            conn.close()

    async def list_event_subscriptions_needing_renewal(
        self,
        cutoff_iso: str,
    ) -> List[dict]:
        """Enabled push-style subs whose external_expiration_at is past `cutoff_iso`."""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                """SELECT * FROM agent_event_subscriptions
                   WHERE enabled = 1
                     AND external_expiration_at IS NOT NULL
                     AND external_expiration_at <= ?
                   ORDER BY external_expiration_at ASC""",
                (cutoff_iso,),
            ).fetchall()
            return [self._event_sub_row_to_dict(r) for r in rows]
        finally:
            conn.close()

    async def list_event_subscriptions_for_poll(
        self,
        source: str,
        before_iso: str,
    ) -> List[dict]:
        """Poll-source subs that haven't been polled since `before_iso` (or never)."""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                """SELECT * FROM agent_event_subscriptions
                   WHERE enabled = 1
                     AND source = ?
                     AND (last_polled_at IS NULL OR last_polled_at <= ?)
                   ORDER BY (last_polled_at IS NULL) DESC, last_polled_at ASC""",
                (source, before_iso),
            ).fetchall()
            return [self._event_sub_row_to_dict(r) for r in rows]
        finally:
            conn.close()

    # ─── Event deliveries (dedup + audit log) ───────────────────────────

    async def find_event_delivery(
        self,
        subscription_id: str,
        event_external_id: str,
    ) -> Optional[dict]:
        conn = self._get_conn()
        try:
            row = conn.execute(
                """SELECT * FROM event_deliveries
                   WHERE subscription_id = ? AND event_external_id = ?
                   ORDER BY created_at DESC LIMIT 1""",
                (subscription_id, event_external_id),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    async def insert_event_delivery(
        self,
        *,
        subscription_id: str,
        source: str,
        event_type: str,
        event_external_id: str,
        owner_user_id: str,
        agent_id: str,
        session_id: Optional[str] = None,
        status: str = "pending",
        error: Optional[str] = None,
        payload_excerpt: Optional[str] = None,
    ) -> str:
        row_id = _uuid()
        async with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    """INSERT INTO event_deliveries
                       (id, subscription_id, source, event_type, event_external_id,
                        owner_user_id, agent_id, session_id, status, error, payload_excerpt)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (row_id, subscription_id, source, event_type, event_external_id,
                     owner_user_id, agent_id, session_id, status, error, payload_excerpt),
                )
                conn.commit()
            finally:
                conn.close()
        return row_id

    async def update_event_delivery(
        self,
        delivery_id: str,
        **fields,
    ) -> None:
        allowed = {"status", "error", "session_id", "payload_excerpt"}
        sets, params = [], []
        for k, v in fields.items():
            if k in allowed:
                sets.append(f"{k} = ?"); params.append(v)
        if not sets:
            return
        params.append(delivery_id)
        async with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    f"UPDATE event_deliveries SET {', '.join(sets)} WHERE id = ?",
                    params,
                )
                conn.commit()
            finally:
                conn.close()


class _LocalTableProxy:
    """
    Emulates the backend's raw query-builder interface (``.table().select().eq().execute()``)
    so that ToolLoader, ToolExecutionTracker, admin/review, and registry work across
    every backend (SQLite, Postgres) without provider-specific code.

    Usage: proxy.table("tools").select("*").eq("status", "active").execute()
    """

    def __init__(self, conn_factory):
        # conn_factory: a zero-arg callable returning a connection that emulates
        # the sqlite3 API (LocalBackend._get_conn for SQLite, or the Postgres
        # PgPortableConnection). Routing through the backend's own connection
        # makes this proxy backend-agnostic.
        self._conn_factory = conn_factory

    def table(self, table_name: str) -> "_LocalQueryBuilder":
        return _LocalQueryBuilder(self._conn_factory, table_name)


class _LocalQueryBuilder:
    """
    Minimal query builder implementing the canonical query chain:
        .select(columns).eq(field, value).in_(field, values).order(...).limit(n).execute()
    Returns objects with .data (list of dicts), matching the expected response shape.
    """

    def __init__(self, conn_factory, table_name: str):
        self._conn_factory = conn_factory
        self._table_name = table_name
        self._select_cols = "*"
        self._filters: List[tuple[str, str, Any]] = []  # (op, field, value)
        self._order_by: List[tuple[str, bool]] = []  # (field, desc)
        self._limit_val: Optional[int] = None
        self._count: bool = False

    def select(self, columns: str = "*") -> "_LocalQueryBuilder":
        self._select_cols = columns
        return self

    def eq(self, field: str, value: Any) -> "_LocalQueryBuilder":
        self._filters.append(("eq", field, value))
        return self

    def in_(self, field: str, values: list) -> "_LocalQueryBuilder":
        self._filters.append(("in", field, values))
        return self

    def neq(self, field: str, value: Any) -> "_LocalQueryBuilder":
        self._filters.append(("neq", field, value))
        return self

    def gt(self, field: str, value: Any) -> "_LocalQueryBuilder":
        self._filters.append(("gt", field, value))
        return self

    def gte(self, field: str, value: Any) -> "_LocalQueryBuilder":
        self._filters.append(("gte", field, value))
        return self

    def ilike(self, field: str, pattern: str) -> "_LocalQueryBuilder":
        self._filters.append(("ilike", field, pattern))
        return self

    def order(self, field: str, *, desc: bool = False) -> "_LocalQueryBuilder":
        self._order_by.append((field, desc))
        return self

    def limit(self, n: int) -> "_LocalQueryBuilder":
        self._limit_val = n
        return self

    def _build_where(self) -> tuple[str, list]:
        """Build WHERE clause from accumulated filters. Returns (sql_fragment, params)."""
        clauses = []
        params: list = []
        for op, field, value in self._filters:
            if op == "eq":
                clauses.append(f"{field} = ?")
                params.append(value)
            elif op == "neq":
                clauses.append(f"{field} != ?")
                params.append(value)
            elif op == "gt":
                clauses.append(f"{field} > ?")
                params.append(value)
            elif op == "gte":
                clauses.append(f"{field} >= ?")
                params.append(value)
            elif op == "in":
                placeholders = ",".join("?" for _ in value)
                clauses.append(f"{field} IN ({placeholders})")
                params.extend(value)
            elif op == "ilike":
                # Portable case-insensitive match: SQLite LIKE is case-insensitive
                # for ASCII but Postgres LIKE is not. LOWER() on both sides is
                # case-insensitive on both backends.
                clauses.append(f"LOWER({field}) LIKE LOWER(?)")
                params.append(value)
        if clauses:
            return " WHERE " + " AND ".join(clauses), params
        return "", []

    def execute(self) -> Any:
        # Backend-agnostic: SQLite returns sqlite3.Row, Postgres returns PgRow;
        # both support dict(row). No need to set row_factory here.
        conn = self._conn_factory()
        try:
            # ---- UPDATE ----
            if hasattr(self, '_update_data') and self._update_data is not None:
                set_parts = [f"{k} = ?" for k in self._update_data]
                set_params = list(self._update_data.values())
                where_clause, where_params = self._build_where()
                sql = f"UPDATE {self._table_name} SET {', '.join(set_parts)}{where_clause}"
                conn.execute(sql, set_params + where_params)
                conn.commit()
                # Return a result with the updated data (mimics the query-builder API)
                return _LocalQueryResult([self._update_data])

            # ---- DELETE ----
            if hasattr(self, '_is_delete') and self._is_delete:
                where_clause, where_params = self._build_where()
                sql = f"DELETE FROM {self._table_name}{where_clause}"
                cursor = conn.execute(sql, where_params)
                conn.commit()
                return _LocalQueryResult([])

            # ---- INSERT ----
            if hasattr(self, '_insert_data') and self._insert_data is not None:
                if not self._insert_data:
                    return _LocalQueryResult([])
                import uuid as _uuid_mod
                columns = list(self._insert_data[0].keys())
                placeholders = ",".join("?" for _ in columns)
                col_names = ",".join(columns)
                inserted_rows = []
                for row_data in self._insert_data:
                    # Auto-generate UUID for id if missing or None
                    if 'id' in row_data and not row_data['id']:
                        row_data['id'] = str(_uuid_mod.uuid4())
                    params = [row_data.get(c) for c in columns]
                    conn.execute(
                        f"INSERT INTO {self._table_name} ({col_names}) VALUES ({placeholders})",
                        params,
                    )
                    inserted_rows.append(row_data)
                conn.commit()
                return _LocalQueryResult(inserted_rows)

            # ---- UPSERT ----
            if hasattr(self, '_upsert_data') and self._upsert_data is not None:
                import uuid as _uuid_mod
                data = dict(self._upsert_data)
                conflict_col = getattr(self, '_on_conflict', '')
                columns = list(data.keys())
                placeholders = ",".join("?" for _ in columns)
                col_names = ",".join(columns)
                params = [data.get(c) for c in columns]

                if conflict_col:
                    # Support both single col and composite keys (comma-separated)
                    conflict_cols = [c.strip() for c in conflict_col.split(",")]
                    where_parts = [f"{c} = ?" for c in conflict_cols]
                    where_vals = []
                    for c in conflict_cols:
                        val = data.get(c)
                        if val is not None:
                            where_vals.append(val)
                        else:
                            # Fall back to the raw conflict_col string (legacy)
                            where_vals.append(data.get(conflict_col))
                            break
                    existing = conn.execute(
                        f"SELECT 1 FROM {self._table_name} WHERE {' AND '.join(where_parts)} LIMIT 1",
                        where_vals,
                    ).fetchone()
                    if existing:
                        set_parts = [f"{k} = ?" for k in columns]
                        set_params = [data.get(c) for c in columns]
                        conn.execute(
                            f"UPDATE {self._table_name} SET {', '.join(set_parts)} WHERE {' AND '.join(where_parts)}",
                            set_params + where_vals,
                        )
                    else:
                        if 'id' in data and not data['id']:
                            data['id'] = str(_uuid_mod.uuid4())
                        conn.execute(
                            f"INSERT INTO {self._table_name} ({col_names}) VALUES ({placeholders})",
                            params,
                        )
                else:
                    if 'id' in data and not data['id']:
                        data['id'] = str(_uuid_mod.uuid4())
                    conn.execute(
                        f"INSERT INTO {self._table_name} ({col_names}) VALUES ({placeholders})",
                        params,
                    )
                conn.commit()
                return _LocalQueryResult([data])

            # ---- SELECT (default) ----
            sql = f"SELECT {self._select_cols} FROM {self._table_name}"
            where_clause, where_params = self._build_where()
            sql += where_clause

            if self._order_by:
                order_parts = []
                for field, desc in self._order_by:
                    order_parts.append(f"{field} {'DESC' if desc else 'ASC'}")
                sql += " ORDER BY " + ", ".join(order_parts)

            if self._limit_val is not None:
                sql += f" LIMIT {self._limit_val}"

            rows = conn.execute(sql, where_params).fetchall()
            return _LocalQueryResult([dict(r) for r in rows])
        finally:
            conn.close()

    def update(self, data: dict) -> "_LocalQueryBuilder":
        self._update_data = data
        return self

    def delete(self) -> "_LocalQueryBuilder":
        self._is_delete = True
        return self

    def insert(self, data: dict | list) -> "_LocalQueryBuilder":
        self._insert_data = data if isinstance(data, list) else [data]
        return self

    def upsert(self, data: dict, on_conflict: str = "") -> "_LocalQueryBuilder":
        self._upsert_data = data
        self._on_conflict = on_conflict
        return self


class _LocalQueryResult:
    def __init__(self, data):
        self.data = data
    def __bool__(self):
        return bool(self.data)
