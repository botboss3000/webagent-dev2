"""
Dedicated, **always-local** SQLite store for operational logs & diagnostics.

This is deliberately separate from the main application database (``get_db()``
— SQLite ``local.db`` or a remote Postgres). Logs are a high-volume, per-machine
"firehose" that nobody else needs to read across instances, so they live in
their own SQLite files with their own WAL and their own write lock. That gives
two wins:

  1. **No contention with user data.** SQLite allows only one writer per *file*.
     Keeping the diagnostics / access / tool-execution firehose out of the file
     that holds ``interactions`` means a burst of log writes can never stall a
     user-facing write, and vice-versa.
  2. **Logs stay local even when the main DB is remote.** Per the design: when
     the main store is a remote Postgres, logging still belongs on the box that
     produced it (you debug one instance at a time). This store is SQLite-file
     based regardless of the main backend.

Two files, by contention domain:

  • ``logs.db``        — server-side operational logs: the ``diagnostics`` table
                         (server / http / loop / run / recovery / tool / access /
                         ws categories) and the structured ``tool_executions``
                         metrics table.
  • ``recordings.db``  — the browser-side render recorder (``render_recordings``),
                         a separate firehose with big HTML blobs and its own
                         lifecycle (off by default).

Every record carries an ``instance_id`` (a per-box identity generated once and
persisted next to the DB files) plus interaction-correlation keys
(``interaction_id`` / ``session_seq`` / ``turn_seq``) so a log line joins to the
exact ``interactions`` row it belongs to — by key, not by fuzzy timestamp match.

The method surface mirrors the names the recorders already call on the main
backend (``insert_diagnostics_batch`` / ``query_diagnostics`` /
``prune_diagnostics`` / ``clear_diagnostics`` and the ``*_render_recordings``
quartet), so wiring the recorders to this store is a one-line ``_db()`` swap.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import shutil
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from app.runtime_mode import data_root

logger = logging.getLogger(__name__)

# Runtime DBs live under data/db/ (the app's stored state), NOT app/db/ (logic).
# Files live alongside the main local.db. All gitignored runtime data.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_DB_DIR = str(data_root() / "db")
_LEGACY_DB_DIR = os.path.dirname(os.path.abspath(__file__))  # old app/db/ location
LOGS_DB_PATH = os.path.join(_DB_DIR, "logs.db")
RECORDINGS_DB_PATH = os.path.join(_DB_DIR, "recordings.db")
INSTANCE_ID_PATH = os.path.join(_DB_DIR, "instance_id.txt")

# Retention is deliberately incremental.  A batch is small enough to commit in
# a short transaction even when detail/html columns make the database hundreds
# of megabytes, and the dedicated executor prevents cleanup from consuming a
# thread needed by chat, auth, or session storage.
DEFAULT_PRUNE_BATCH_ROWS = 1000
PRUNE_BUSY_TIMEOUT_MS = 250


def _relocate_legacy_log_files() -> None:
    """One-time move of the log DBs from the legacy app/db/ dir to data/db/.
    Idempotent + safe (only moves when the destination is absent); never raises."""
    try:
        if os.path.abspath(_LEGACY_DB_DIR) == os.path.abspath(_DB_DIR):
            return
        os.makedirs(_DB_DIR, exist_ok=True)
        for name in ("logs.db", "logs.db-wal", "logs.db-shm", "logs.db-journal",
                     "recordings.db", "recordings.db-wal", "recordings.db-shm",
                     "recordings.db-journal", "instance_id.txt"):
            old = os.path.join(_LEGACY_DB_DIR, name)
            new = os.path.join(_DB_DIR, name)
            if os.path.exists(old) and not os.path.exists(new):
                try:
                    shutil.move(old, new)
                except Exception:
                    pass
    except Exception:
        pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uuid() -> str:
    return str(uuid.uuid4())


def _cutoff_iso(seconds: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


# ── Schema ──────────────────────────────────────────────────────────────────

_LOGS_SCHEMA = """
-- Server-side flight recorder. Rolling, auto-pruned log of server warnings /
-- errors (with tracebacks), agent-loop pipeline problems, run outcomes, tool
-- errors, every HTTP request (access) and WebSocket lifecycle (ws). The
-- correlation columns (interaction_id / session_seq / turn_seq) let a log line
-- join to the exact interactions row it belongs to; instance_id names the box.
CREATE TABLE IF NOT EXISTS diagnostics (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,                 -- ISO-8601 UTC capture time
    level TEXT NOT NULL,              -- debug | info | warning | error | critical
    category TEXT NOT NULL,           -- server | http | loop | run | recovery | tool | access | ws
    source TEXT,                      -- logger name / pipeline step / tool name
    message TEXT NOT NULL,
    detail TEXT,                      -- JSON blob (traceback, args, tokens, cost…)
    session_id TEXT,
    turn_id TEXT,
    agent_id TEXT,
    user_id TEXT,
    interaction_id TEXT,              -- FK-by-value to interactions.id (no cascade)
    session_seq INTEGER,              -- per-session ordering key (mirrors interactions)
    turn_seq INTEGER,                 -- per-turn ordering key
    instance_id TEXT,                 -- which box produced this record
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_diagnostics_ts ON diagnostics(ts);
CREATE INDEX IF NOT EXISTS idx_diagnostics_level ON diagnostics(level);
CREATE INDEX IF NOT EXISTS idx_diagnostics_category ON diagnostics(category);
CREATE INDEX IF NOT EXISTS idx_diagnostics_session ON diagnostics(session_id);
CREATE INDEX IF NOT EXISTS idx_diagnostics_interaction ON diagnostics(interaction_id);

-- Structured tool-execution metrics (revived from app/tools/tracker.py). Unlike
-- the free-text `tool` diagnostics category, this is columnar so you can sort /
-- aggregate by duration and success. One row per completed tool call.
CREATE TABLE IF NOT EXISTS tool_executions (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    session_id TEXT,
    turn_id TEXT,
    interaction_id TEXT,
    agent_id TEXT,
    user_id TEXT,
    success INTEGER NOT NULL DEFAULT 1,  -- 1 ok, 0 failed
    duration_ms INTEGER,
    error_type TEXT,
    error_message TEXT,
    input_params TEXT,                   -- truncated JSON
    output_preview TEXT,                 -- truncated result
    instance_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_tool_exec_ts ON tool_executions(ts);
CREATE INDEX IF NOT EXISTS idx_tool_exec_name ON tool_executions(tool_name);
CREATE INDEX IF NOT EXISTS idx_tool_exec_session ON tool_executions(session_id);
CREATE INDEX IF NOT EXISTS idx_tool_exec_success ON tool_executions(success);
"""

_RECORDINGS_SCHEMA = """
-- Browser-side render recorder (see app/agent/render_recorder.py). Big HTML
-- blobs, off by default; isolated in its own file so it never competes with the
-- server log firehose.
CREATE TABLE IF NOT EXISTS render_recordings (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    recv_ts TEXT,
    kind TEXT NOT NULL,
    session_id TEXT,
    turn_id TEXT,
    session_seq INTEGER,
    user_id TEXT,
    agent_id TEXT,
    client_id TEXT,
    seq INTEGER,
    url TEXT,
    level TEXT,
    label TEXT,
    value_num REAL,
    detail TEXT,
    html TEXT,
    html_bytes INTEGER,
    instance_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_render_rec_ts ON render_recordings(ts);
CREATE INDEX IF NOT EXISTS idx_render_rec_session ON render_recordings(session_id);
CREATE INDEX IF NOT EXISTS idx_render_rec_kind ON render_recordings(kind);
CREATE INDEX IF NOT EXISTS idx_render_rec_seq ON render_recordings(session_seq);
"""


class LogStore:
    """Manages the two dedicated local SQLite log files (own WAL, own lock)."""

    def __init__(self) -> None:
        self._logs_path = LOGS_DB_PATH
        self._rec_path = RECORDINGS_DB_PATH
        # One write lock per file → independent writer slots (the whole point of
        # splitting the firehoses into separate files).
        self._logs_lock = asyncio.Lock()
        self._rec_lock = asyncio.Lock()
        self._instance_id: Optional[str] = None
        self._maintenance_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="log-maintenance",
        )
        self._init_db()

    # ── Connections (same fast pragmas as the main backend) ──────────────────

    @staticmethod
    def _connect(path: str):
        # Routed through db_crypto: the canonical logs/recordings files honour the
        # full-DB-encryption config; any custom/test path stays plaintext. With
        # encryption OFF this is plain stdlib sqlite3, unchanged. Row factory is
        # set by db_crypto to match the driver.
        from app.db import db_crypto
        if path == LOGS_DB_PATH:
            db_id = "logs"
        elif path == RECORDINGS_DB_PATH:
            db_id = "recordings"
        else:
            db_id = "_logs_custom"
        conn = db_crypto.connect(path, db_id)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA cache_size=-16000")
        conn.execute("PRAGMA mmap_size=268435456")
        conn.execute("PRAGMA wal_autocheckpoint=2000")
        return conn

    def _init_db(self) -> None:
        try:
            # On the default install, relocate legacy app/db/ log files into
            # data/db/ before opening (one-time, safe). Skipped for custom/test
            # paths so they never touch the real runtime files.
            if self._logs_path == LOGS_DB_PATH and self._rec_path == RECORDINGS_DB_PATH:
                _relocate_legacy_log_files()
            for p in (self._logs_path, self._rec_path):
                try:
                    os.makedirs(os.path.dirname(os.path.abspath(p)), exist_ok=True)
                except Exception:
                    pass
            conn = self._connect(self._logs_path)
            try:
                conn.executescript(_LOGS_SCHEMA)
                self._reconcile_columns(conn, "diagnostics", _DIAG_COLS)
                self._reconcile_columns(conn, "tool_executions", _TOOL_COLS)
                conn.commit()
            finally:
                conn.close()
            conn = self._connect(self._rec_path)
            try:
                conn.executescript(_RECORDINGS_SCHEMA)
                self._reconcile_columns(conn, "render_recordings", _REC_COLS)
                conn.commit()
            finally:
                conn.close()
        except Exception as e:  # a logging store must never break boot
            logger.warning("LogStore init failed: %s", e)

    @staticmethod
    def _reconcile_columns(conn: sqlite3.Connection, table: str, want: Dict[str, str]) -> None:
        """Add any missing columns to an existing table (forward-compat for an
        older logs.db created before a column was introduced)."""
        try:
            have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            for col, decl in want.items():
                if col not in have:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        except Exception:
            pass

    # ── Instance identity ────────────────────────────────────────────────────

    def instance_id(self) -> str:
        """A stable per-box id, created once and persisted next to the DB files.
        Stamped on every record so logs from multiple instances never blur."""
        if self._instance_id:
            return self._instance_id
        try:
            if os.path.exists(INSTANCE_ID_PATH):
                with open(INSTANCE_ID_PATH, "r", encoding="utf-8") as f:
                    val = (f.read() or "").strip()
                    if val:
                        self._instance_id = val
                        return val
            val = _uuid()
            with open(INSTANCE_ID_PATH, "w", encoding="utf-8") as f:
                f.write(val)
            self._instance_id = val
            return val
        except Exception:
            # Fall back to an ephemeral id rather than crashing the recorder.
            self._instance_id = self._instance_id or _uuid()
            return self._instance_id

    # ── diagnostics (logs.db) ────────────────────────────────────────────────

    async def insert_diagnostics_batch(self, rows: List[Dict[str, Any]]) -> int:
        if not rows:
            return 0
        inst = self.instance_id()
        async with self._logs_lock:
            conn = self._connect(self._logs_path)
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
                    r.get("interaction_id"),
                    r.get("session_seq"),
                    r.get("turn_seq"),
                    r.get("instance_id") or inst,
                ) for r in rows]
                conn.executemany(
                    "INSERT OR IGNORE INTO diagnostics "
                    "(id, ts, level, category, source, message, detail, session_id, "
                    " turn_id, agent_id, user_id, interaction_id, session_seq, turn_seq, instance_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    values,
                )
                conn.commit()
                return len(values)
            except Exception as e:
                logger.debug("insert_diagnostics_batch failed: %s", e)
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
        conn = self._connect(self._logs_path)
        try:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
        except Exception as e:
            logger.debug("query_diagnostics failed: %s", e)
            return []
        finally:
            conn.close()

    async def prune_diagnostics(self, *, max_rows: int = 200000,
                                max_age_seconds: Optional[float] = None,
                                batch_rows: int = DEFAULT_PRUNE_BATCH_ROWS) -> int:
        return await self._prune(
            self._logs_lock, self._logs_path, "diagnostics", max_rows,
            max_age_seconds, batch_rows=batch_rows,
        )

    async def clear_diagnostics(
        self,
        *,
        older_than_seconds: Optional[float] = None,
        levels: Optional[List[str]] = None,
        categories: Optional[List[str]] = None,
        search: Optional[str] = None,
    ) -> int:
        where: List[str] = []
        params: List[Any] = []
        if older_than_seconds is not None:
            where.append("ts < ?")
            params.append(_cutoff_iso(older_than_seconds))
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
        async with self._logs_lock:
            conn = self._connect(self._logs_path)
            try:
                cur = conn.execute(sql, params)
                conn.commit()
                return cur.rowcount or 0
            except Exception as e:
                logger.debug("clear_diagnostics failed: %s", e)
                return 0
            finally:
                conn.close()

    # ── tool_executions (logs.db) ────────────────────────────────────────────

    async def insert_tool_executions(self, rows: List[Dict[str, Any]]) -> int:
        if not rows:
            return 0
        inst = self.instance_id()
        async with self._logs_lock:
            conn = self._connect(self._logs_path)
            try:
                values = [(
                    r.get("id") or _uuid(),
                    r.get("ts") or _now_iso(),
                    (r.get("tool_name") or "tool"),
                    r.get("session_id"),
                    r.get("turn_id"),
                    r.get("interaction_id"),
                    r.get("agent_id"),
                    r.get("user_id"),
                    1 if r.get("success", True) else 0,
                    r.get("duration_ms"),
                    r.get("error_type"),
                    r.get("error_message"),
                    r.get("input_params"),
                    r.get("output_preview"),
                    r.get("instance_id") or inst,
                ) for r in rows]
                conn.executemany(
                    "INSERT OR IGNORE INTO tool_executions "
                    "(id, ts, tool_name, session_id, turn_id, interaction_id, agent_id, user_id, "
                    " success, duration_ms, error_type, error_message, input_params, output_preview, instance_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    values,
                )
                conn.commit()
                return len(values)
            except Exception as e:
                logger.debug("insert_tool_executions failed: %s", e)
                return 0
            finally:
                conn.close()

    async def query_tool_executions(
        self,
        *,
        tool_name: Optional[str] = None,
        session_id: Optional[str] = None,
        success: Optional[bool] = None,
        since: Optional[str] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        where: List[str] = []
        params: List[Any] = []
        if tool_name:
            where.append("tool_name = ?")
            params.append(tool_name)
        if session_id:
            where.append("session_id = ?")
            params.append(session_id)
        if success is not None:
            where.append("success = ?")
            params.append(1 if success else 0)
        if since:
            where.append("ts >= ?")
            params.append(since)
        sql = "SELECT * FROM tool_executions"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(max(1, min(int(limit or 200), 2000)))
        conn = self._connect(self._logs_path)
        try:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
        except Exception as e:
            logger.debug("query_tool_executions failed: %s", e)
            return []
        finally:
            conn.close()

    def query_tool_executions_sync(
        self,
        *,
        tool_name: Optional[str] = None,
        session_id: Optional[str] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """Synchronous twin of ``query_tool_executions`` for SYNC (`def`) path
        operations (see db_viewer.py endpoints run in FastAPI's threadpool). The
        body was already fully synchronous; this just drops the ``async`` so a
        sync endpoint can call it without an event loop."""
        where: List[str] = []
        params: List[Any] = []
        if tool_name:
            where.append("tool_name = ?")
            params.append(tool_name)
        if session_id:
            where.append("session_id = ?")
            params.append(session_id)
        sql = "SELECT * FROM tool_executions"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(max(1, min(int(limit or 200), 2000)))
        conn = self._connect(self._logs_path)
        try:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
        except Exception as e:
            logger.debug("query_tool_executions_sync failed: %s", e)
            return []
        finally:
            conn.close()

    async def session_tool_usage(
        self,
        tool_names: List[str],
        *,
        user_id: Optional[str] = None,
        success_only: bool = True,
    ) -> Dict[str, set]:
        """Map each session_id to the SET of tool names it ran, restricted to
        ``tool_names``. One grouped query — used to flag which sessions touched a
        genui or a browser page without a per-session round-trip. Sessions that
        ran none of the listed tools are simply absent from the map."""
        names = [t for t in (tool_names or []) if t]
        if not names:
            return {}
        placeholders = ",".join("?" for _ in names)
        where = [f"tool_name IN ({placeholders})", "session_id IS NOT NULL"]
        params: List[Any] = list(names)
        if success_only:
            where.append("success = 1")
        if user_id:
            where.append("user_id = ?")
            params.append(user_id)
        sql = (
            "SELECT DISTINCT session_id, tool_name FROM tool_executions WHERE "
            + " AND ".join(where)
        )
        conn = self._connect(self._logs_path)
        try:
            out: Dict[str, set] = {}
            for row in conn.execute(sql, params).fetchall():
                out.setdefault(row["session_id"], set()).add(row["tool_name"])
            return out
        except Exception as e:
            logger.debug("session_tool_usage failed: %s", e)
            return {}
        finally:
            conn.close()

    async def prune_tool_executions(self, *, max_rows: int = 100000,
                                    max_age_seconds: Optional[float] = None,
                                    batch_rows: int = DEFAULT_PRUNE_BATCH_ROWS) -> int:
        return await self._prune(
            self._logs_lock, self._logs_path, "tool_executions", max_rows,
            max_age_seconds, batch_rows=batch_rows,
        )

    # ── render_recordings (recordings.db) ────────────────────────────────────

    async def insert_render_recordings_batch(self, rows: List[Dict[str, Any]]) -> int:
        if not rows:
            return 0
        inst = self.instance_id()
        async with self._rec_lock:
            conn = self._connect(self._rec_path)
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
                    r.get("instance_id") or inst,
                ) for r in rows]
                conn.executemany(
                    "INSERT OR IGNORE INTO render_recordings "
                    "(id, ts, recv_ts, kind, session_id, turn_id, session_seq, user_id, "
                    " agent_id, client_id, seq, url, level, label, value_num, detail, html, html_bytes, instance_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    values,
                )
                conn.commit()
                return len(values)
            except Exception as e:
                logger.debug("insert_render_recordings_batch failed: %s", e)
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
                "agent_id, client_id, seq, url, level, label, value_num, detail, html_bytes, instance_id")
        if include_html:
            cols += ", html"
        sql = f"SELECT {cols} FROM render_recordings"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(max(1, min(int(limit or 200), 2000)))
        conn = self._connect(self._rec_path)
        try:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
        except Exception as e:
            logger.debug("query_render_recordings failed: %s", e)
            return []
        finally:
            conn.close()

    async def prune_render_recordings(self, *, max_rows: int = 8000,
                                      max_age_seconds: Optional[float] = None,
                                      batch_rows: int = DEFAULT_PRUNE_BATCH_ROWS) -> int:
        return await self._prune(
            self._rec_lock, self._rec_path, "render_recordings", max_rows,
            max_age_seconds, batch_rows=batch_rows,
        )

    async def clear_render_recordings(
        self,
        *,
        older_than_seconds: Optional[float] = None,
        kinds: Optional[List[str]] = None,
        session_id: Optional[str] = None,
        search: Optional[str] = None,
    ) -> int:
        where: List[str] = []
        params: List[Any] = []
        if older_than_seconds is not None:
            where.append("ts < ?")
            params.append(_cutoff_iso(older_than_seconds))
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
        async with self._rec_lock:
            conn = self._connect(self._rec_path)
            try:
                cur = conn.execute(sql, params)
                conn.commit()
                return cur.rowcount or 0
            except Exception as e:
                logger.debug("clear_render_recordings failed: %s", e)
                return 0
            finally:
                conn.close()

    # ── Shared prune helper ──────────────────────────────────────────────────

    async def _prune(self, lock: asyncio.Lock, path: str, table: str,
                     max_rows: int, max_age_seconds: Optional[float], *,
                     batch_rows: int = DEFAULT_PRUNE_BATCH_ROWS) -> int:
        # A cold-start prune can scan/delete hundreds of MB from logs.db. SQLite
        # is synchronous, so running these statements in this coroutine used to
        # freeze the whole ASGI event loop: /health and even /ui/*.js stopped
        # answering until cleanup finished. Keep the async lock as the ordering
        # gate, but move all blocking SQLite work to a worker thread.
        batch_rows = max(1, min(int(batch_rows), 10000))

        def _prune_sync() -> int:
            deleted = 0
            conn = self._connect(path)
            try:
                # Maintenance must yield immediately when a foreground writer
                # owns SQLite.  The recorder will retry later; it must never sit
                # behind the normal 30-second connection timeout.
                conn.execute(f"PRAGMA busy_timeout={PRUNE_BUSY_TIMEOUT_MS}")
                if max_age_seconds:
                    cur = conn.execute(
                        f"DELETE FROM {table} WHERE rowid IN ("
                        f"SELECT rowid FROM {table} WHERE ts < ? "
                        f"ORDER BY ts ASC LIMIT ?)",
                        (_cutoff_iso(max_age_seconds), batch_rows),
                    )
                    deleted += cur.rowcount or 0
                    conn.commit()

                remaining = batch_rows - deleted
                if remaining > 0 and max_rows and max_rows > 0:
                    # Walk only the timestamp index through the retained window;
                    # COUNT(*) would scan the entire oversized database on every
                    # batch. Rows after the newest max_rows are disposable.
                    cur = conn.execute(
                        f"DELETE FROM {table} WHERE rowid IN ("
                        f"SELECT rowid FROM {table} "
                        f"ORDER BY ts DESC, rowid DESC LIMIT ? OFFSET ?)",
                        (remaining, int(max_rows)),
                    )
                    deleted += cur.rowcount or 0
                    conn.commit()
                return deleted
            except Exception as e:
                logger.debug("prune %s failed: %s", table, e)
                return deleted
            finally:
                conn.close()

        async with lock:
            loop = asyncio.get_running_loop()
            executor = getattr(self, "_maintenance_executor", None)
            if executor is None:  # supports lightweight test/custom instances
                executor = concurrent.futures.ThreadPoolExecutor(
                    max_workers=1,
                    thread_name_prefix="log-maintenance",
                )
                self._maintenance_executor = executor
            return await loop.run_in_executor(executor, _prune_sync)

    def paths(self) -> Dict[str, str]:
        return {"logs_db": self._logs_path, "recordings_db": self._rec_path,
                "instance_id": self.instance_id()}


# Column reconciliation maps (declared type used only when ALTERing an older file).
_DIAG_COLS = {
    "interaction_id": "TEXT", "session_seq": "INTEGER",
    "turn_seq": "INTEGER", "instance_id": "TEXT",
}
_TOOL_COLS: Dict[str, str] = {}
_REC_COLS = {"instance_id": "TEXT"}


# ── Module singleton ─────────────────────────────────────────────────────────

_store: Optional[LogStore] = None


def get_log_store() -> LogStore:
    global _store
    if _store is None:
        _store = LogStore()
    return _store
