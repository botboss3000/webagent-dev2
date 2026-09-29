"""
Database viewer API — backend-aware DB introspection for the terminal UI.

Reads/writes the active application database: SQLite by default, or Postgres
when it is the active backend (the main DB routes through a standalone autocommit
PgPortableConnection; SQLite-dialect SQL is translated on the fly — see
app/db/pg_portable.py). Temp/optimizer `.db` scratch files are always SQLite.
Dispatch happens in `_open()`.
"""

import asyncio
import concurrent.futures
import copy
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from app.auth.db_auth import require_db_auth
from app.auth.jwt import decode_token
from app.db.browser_canary import rollback_active
from app.db.session_manifest import compute_session_manifest


def _compute_session_manifest_for_db(db_name: str, session_id: str) -> dict:
    """Build one manifest on a worker-owned connection.

    A dirty manifest hashes the full transcript and may also install/reconcile
    SQLite triggers. The sessions list is polled in the background, so doing
    that synchronous work on its async request task can starve even /health for
    seconds when a long active transcript becomes dirty.
    """
    conn, _dialect = _open_read(db_name)
    try:
        return compute_session_manifest(conn, session_id)
    finally:
        conn.close()


async def _compute_session_manifest_offloop(db_name: str, session_id: str) -> dict:
    return await asyncio.to_thread(_compute_session_manifest_for_db, db_name, session_id)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/db", tags=["db_viewer"])


def _interaction_order_parts(tiebreaker: str, timestamp_recovery: bool = False) -> tuple[str, ...]:
    if tiebreaker not in ("rowid", "id"):
        raise ValueError(f"unsupported interaction tiebreaker: {tiebreaker}")
    if timestamp_recovery:
        # Preserve the cursor tuple's four fields while making created_at the order authority.
        return ("CASE WHEN 1 THEN 0 END", "CASE WHEN 1 THEN 0 END", "created_at", tiebreaker)
    return (
        "CASE WHEN session_seq IS NULL THEN 0 ELSE 1 END",
        "COALESCE(session_seq, 0)",
        "created_at",
        tiebreaker,
    )


def _interaction_order_key(tiebreaker: str, timestamp_recovery: bool = False) -> str:
    """SQL tuple used by every transcript pagination cursor."""
    # Parts are kept as one comma-delimited string for row-value comparisons.
    return ", ".join(_interaction_order_parts(tiebreaker, timestamp_recovery))


def _interaction_order_by(tiebreaker: str, direction: str = "ASC", timestamp_recovery: bool = False) -> str:
    """ORDER BY terms with direction applied to every component."""
    direction = direction.upper()
    if direction not in ("ASC", "DESC"):
        raise ValueError(f"unsupported interaction order direction: {direction}")
    return ", ".join(
        f"{part} {direction}" for part in _interaction_order_parts(tiebreaker, timestamp_recovery)
    )


def _interaction_cursor_values(session_seq, created_at, tiebreaker):
    return (
        0 if session_seq is None else 1,
        int(session_seq or 0),
        created_at,
        tiebreaker,
    )


def _manifest_cache_not_modified(
    known_revision: Optional[int],
    known_hash: Optional[str],
    manifest: dict,
) -> bool:
    """Validate one browser-cache manifest against the live rollback state."""
    if rollback_active():
        return False
    return (
        known_revision is not None
        and bool(known_hash)
        and int(known_revision) == int(manifest["authority_revision"])
        and str(known_hash) == str(manifest["content_hash"])
    )


def _session_messages_needs_manifest(
    manifest_only: bool,
    known_revision: Optional[int],
    known_hash: Optional[str],
) -> bool:
    """Only explicit cache validation should hash a full transcript.

    Ordinary window/delta responses are mergeable without a server manifest;
    their browser cache paths already treat it as optional. Computing it on
    every background poll turns one active long transcript into repeated full
    JSON hashing across tabs.
    """
    return bool(manifest_only or known_revision is not None or known_hash)


def _is_loser_row(metadata_str) -> bool:
    """True when an interactions row is a parallel-racing LOSER (metadata
    parallel_loser=True). These are persisted only for diagnostics; the chat
    transcript should show one answer per turn (the winner), not the losers too."""
    if not metadata_str:
        return False
    try:
        return bool(json.loads(metadata_str).get("parallel_loser"))
    except (json.JSONDecodeError, TypeError, AttributeError):
        return False

# SQLite files live under data/db/ alongside every other runtime DB. Import the
# canonical location from app.db.local so this stays in lockstep if it ever moves
# again — recomputing the path here is what left this endpoint pointing at the old
# app/db/ location after the relocation, so the chat dropdown read an empty/missing
# file and every session showed as "New Session".
from app.db.local import DB_DIR as _DB_DIR_STR
_DB_FILES_DIR = Path(_DB_DIR_STR)

# In-process cache for /tables responses. Keyed by absolute db path.
# Counts can be expensive for large tables; clients poll this every ~20s, and
# the underlying SQLite file is updated by writes that we can't easily hook,
# so we key on (mtime, size) and fall back to a short TTL.
_TABLES_CACHE: dict[str, tuple[float, float, int, dict]] = {}
_TABLES_CACHE_TTL_S = 5.0

# The chat reconcile endpoint can poll the same open session every 800 ms.  A
# session's temp-DB routing is effectively immutable, but resolving it used to
# open user.db, query sessions.metadata, and close the connection on *every*
# poll before opening the database a second time for the actual tail query.
# Cache positive routes for the life of an ordinary chat and negative routes
# briefly so a newly-created temp session can still become visible promptly.
_SESSION_DB_ROUTE_CACHE: dict[tuple[str, str], tuple[float, str]] = {}
_SESSION_DB_ROUTE_CACHE_MAX = 4096
_SESSION_DB_ROUTE_POSITIVE_TTL_S = 300.0
_SESSION_DB_ROUTE_NEGATIVE_TTL_S = 5.0

# Schema migration is a deployment concern, not a per-poll concern.  Cache the
# one capability that the session-tail hot path needs so active streaming does
# not run PRAGMA table_info(interactions) on every 800 ms request.
_SESSION_TAIL_STATUS_CACHE: dict[str, bool] = {}
_SESSION_TAIL_STATUS_CACHE_MAX = 64

def _session_tail_has_status(cur, db_name: str) -> bool:
    cached = _SESSION_TAIL_STATUS_CACHE.get(db_name)
    if cached is not None:
        return cached
    try:
        has_status = any(r[1] == "status" for r in cur.execute("PRAGMA table_info(interactions)").fetchall())
    except Exception:
        has_status = False
    if len(_SESSION_TAIL_STATUS_CACHE) >= _SESSION_TAIL_STATUS_CACHE_MAX:
        _SESSION_TAIL_STATUS_CACHE.pop(next(iter(_SESSION_TAIL_STATUS_CACHE)), None)
    _SESSION_TAIL_STATUS_CACHE[db_name] = has_status
    return has_status

# Session summaries parse every interaction and perform several cross-plane
# SQLite reads.  Keep that work off the main event loop and out of the shared
# default executor: a slow/locked user database must not stall health, auth, or
# chat requests.  The pool is deliberately small because each worker owns its
# own SQLite connections for the duration of one summary build.
_SESSION_STATS_EXECUTOR: concurrent.futures.ThreadPoolExecutor | None = None
_SESSION_STATS_EXECUTOR_LOCK = threading.Lock()
_SESSION_STATS_WORKERS = 2
_SESSION_STATS_CACHE_LOCK = threading.RLock()
_SESSION_STATS_CACHE: dict[tuple[str, str, str], tuple[float, dict]] = {}
_SESSION_STATS_INFLIGHT: dict[
    tuple[str, str, str], concurrent.futures.Future
] = {}
_SESSION_STATS_CACHE_TTL_S = 15.0
_SESSION_STATS_CACHE_MAX_STALE_S = 120.0
_SESSION_STATS_CACHE_MAX_ENTRIES = 128
_SESSION_STATS_CACHE_EPOCH = 0


def _get_session_stats_executor() -> concurrent.futures.ThreadPoolExecutor:
    global _SESSION_STATS_EXECUTOR
    if _SESSION_STATS_EXECUTOR is None:
        with _SESSION_STATS_EXECUTOR_LOCK:
            if _SESSION_STATS_EXECUTOR is None:
                _SESSION_STATS_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
                    max_workers=_SESSION_STATS_WORKERS,
                    thread_name_prefix="session-stats",
                )
    return _SESSION_STATS_EXECUTOR


def _session_stats_cache_key(
    user_id: str, db: str, status: str
) -> tuple[str, str, str]:
    normalized_status = status if status in {"active", "recycled"} else "all"
    return (str(user_id or ""), str(db or "user.db"), normalized_status)


def _invalidate_session_stats_cache() -> None:
    """Invalidate summaries without letting an older in-flight build repopulate them."""
    global _SESSION_STATS_CACHE_EPOCH
    with _SESSION_STATS_CACHE_LOCK:
        _SESSION_STATS_CACHE_EPOCH += 1
        _SESSION_STATS_CACHE.clear()


def _submit_session_stats_refresh(
    key: tuple[str, str, str],
) -> concurrent.futures.Future:
    """Return the one in-flight summary build for ``key``, submitting if absent."""
    with _SESSION_STATS_CACHE_LOCK:
        existing = _SESSION_STATS_INFLIGHT.get(key)
        if existing is not None:
            return existing
        epoch = _SESSION_STATS_CACHE_EPOCH
        future = _get_session_stats_executor().submit(
            _build_session_stats_sync, key[0], key[1], key[2]
        )
        _SESSION_STATS_INFLIGHT[key] = future

        def completed(done: concurrent.futures.Future) -> None:
            try:
                value = done.result()
            except Exception:
                value = None
            with _SESSION_STATS_CACHE_LOCK:
                if _SESSION_STATS_INFLIGHT.get(key) is done:
                    _SESSION_STATS_INFLIGHT.pop(key, None)
                if value is None or epoch != _SESSION_STATS_CACHE_EPOCH:
                    return
                _SESSION_STATS_CACHE[key] = (time.monotonic(), value)
                while len(_SESSION_STATS_CACHE) > _SESSION_STATS_CACHE_MAX_ENTRIES:
                    oldest = min(
                        _SESSION_STATS_CACHE,
                        key=lambda cache_key: _SESSION_STATS_CACHE[cache_key][0],
                    )
                    _SESSION_STATS_CACHE.pop(oldest, None)

        future.add_done_callback(completed)
        return future


def _cached_session_stats(
    key: tuple[str, str, str],
) -> tuple[Optional[dict], Optional[concurrent.futures.Future]]:
    """Return cached data plus a Future only when a cold caller must await it."""
    now = time.monotonic()
    with _SESSION_STATS_CACHE_LOCK:
        cached = _SESSION_STATS_CACHE.get(key)
        if cached and now - cached[0] <= _SESSION_STATS_CACHE_TTL_S:
            return copy.deepcopy(cached[1]), None

    future = _submit_session_stats_refresh(key)
    if cached and now - cached[0] <= _SESSION_STATS_CACHE_MAX_STALE_S:
        # Stale-while-refresh: the callback updates the cache.  This request is
        # deliberately not coupled to the 20s+ refresh.
        return copy.deepcopy(cached[1]), None
    return None, future


def _invalidate_tables_cache(db_path: Path) -> None:
    _TABLES_CACHE.pop(str(db_path), None)


# ── Per-user row-level access control ─────────────────────────────────────
# Tables where filtering uses a column on the table itself.
_USER_ID_COLUMN: dict[str, str] = {
    "sessions": "user_id",
    "session_summaries": "user_id",
    "memories": "user_id",
    "agent_credentials": "user_id",
    "auth_elements": "user_id",
    "data_sources": "user_id",
    "skills": "user_id",
    "skill_executions": "user_id",
    "skill_feedback": "user_id",
    "attachments": "user_id",
    "channel_identities": "user_id",
    "webhook_registrations": "user_id",
    "user_profiles": "user_id",
    "tenant_key_meta": "user_id",
    "usage_events": "user_id",
    "subscriptions": "user_id",
    "trials": "user_id",
    "payment_accounts": "user_id",
    "payments": "user_id",
    "billing_exemptions": "user_id",
    "linking_codes": "source_user_id",
}

# Tables reached via a FK to a table with a user_id column.
# Map: table -> (fk_column, parent_table, parent_pk, parent_user_col)
_LINKED_USER_TABLES: dict[str, tuple[str, str, str, str]] = {
    "interactions": ("session_id", "sessions", "id", "user_id"),
    "session_interrupts": ("session_id", "sessions", "id", "user_id"),
    "memory_chunks": ("memory_id", "memories", "id", "user_id"),
    "webhook_event_log": ("webhook_id", "webhook_registrations", "id", "user_id"),
    "doc_chunks": ("data_source_id", "data_sources", "id", "user_id"),
}

# Tables only admins may read. Catalogs, system configs, and agent-scoped
# tables whose membership lives in JSON arrays we don't want to parse in SQL.
_ADMIN_ONLY_TABLES: set[str] = {
    "agent_templates",
    "agent_prompt_templates",
    "app_meta",
    "billing_configs",
    "tools",
    "agents",
    "agent_connections",
    "agent_abilities",
    "agent_data_sources",
    "agent_prompts",
}


async def require_admin(_auth: dict = Depends(require_db_auth)) -> dict:
    """FastAPI dependency: require the caller to be a global admin."""
    _uid, is_admin = _get_caller(_auth)
    if not is_admin:
        raise HTTPException(status_code=403, detail="Admin privileges required")
    return _auth


def _get_caller(payload: Optional[dict]) -> tuple[Optional[str], bool]:
    """Extract (user_id, is_admin) from a JWT payload.

    Admin status is looked up from app.db's authoritative user_profiles table — the
    same source `app.db.local.is_user_admin` consults — so the check is
    consistent regardless of which db file is being viewed.
    """
    if not payload:
        return None, False
    user_id = payload.get("user_id") or payload.get("sub")
    if not user_id:
        return None, False
    is_admin = False
    try:
        conn, _dialect = _open("app.db")
        try:
            row = conn.execute(
                "SELECT is_admin FROM user_profiles WHERE user_id = ?", (user_id,)
            ).fetchone()
        finally:
            conn.close()
        if row and row[0]:
            is_admin = True
    except Exception:
        pass
    return user_id, is_admin


def _acl_clause(table: str, user_id: Optional[str], is_admin: bool) -> tuple[Optional[str], list]:
    """Return (where_fragment, params) restricting `table` to rows owned by `user_id`.

    - Admins: returns (None, []) — no filtering.
    - Non-admins on admin-only tables, unknown tables, or with no user_id:
      returns ("1=0", []) — denies all rows.
    - Otherwise returns the table-specific predicate.
    """
    if is_admin:
        return None, []
    if not user_id:
        return "1=0", []
    if table in _ADMIN_ONLY_TABLES:
        return "1=0", []
    if table in _USER_ID_COLUMN:
        col = _USER_ID_COLUMN[table]
        return f'"{col}" = ?', [user_id]
    if table in _LINKED_USER_TABLES:
        fk, parent, parent_pk, parent_user = _LINKED_USER_TABLES[table]
        return (
            f'"{fk}" IN (SELECT "{parent_pk}" FROM "{parent}" WHERE "{parent_user}" = ?)',
            [user_id],
        )
    if table == "wallets":
        return '"owner_type" = ? AND "owner_id" = ?', ["user", user_id]
    if table == "wallet_transactions":
        return (
            '"wallet_id" IN (SELECT "id" FROM "wallets" WHERE "owner_type" = ? AND "owner_id" = ?)',
            ["user", user_id],
        )
    # Unknown table — deny by default for non-admins.
    return "1=0", []


def _is_open_access_mode() -> bool:
    """Always False — the 'open' auto-admin access mode was retired. Kept as a
    stable name for the per-session participant gates below, which now always
    apply strict token checks."""
    return False


def _get_db_path(name: str = "user.db") -> Path:
    if Path(name).name != name:
        raise HTTPException(status_code=400, detail="Database name must be a plain filename")
    if name == "user.db":
        from app.db.local import get_db_user_context
        from app.db.user_store import _user_db_path
        db_path = Path(_user_db_path(get_db_user_context()))
    else:
        db_path = _DB_FILES_DIR / name
    # When Postgres is the active backend, the main DB has no file — callers that
    # only need it for the (now Postgres) connection still call this; don't 404.
    if not db_path.exists() and _pg_conninfo_for(name) is None:
        # Never disclose the host's absolute storage layout to public callers.
        # The server log already has enough context to diagnose a missing DB.
        logger.warning("Database %s not found at %s", name, db_path)
        raise HTTPException(status_code=404, detail=f"Database '{name}' not found")
    return db_path


def _pg_conninfo_for(db: str):
    """Return PG conninfo when Postgres is the active backend AND `db` is the
    main database. Temp/optimizer .db files always stay on SQLite."""
    if db not in ("user.db", "", None):
        return None
    try:
        from app.db import active_postgres_conninfo
        return active_postgres_conninfo()
    except Exception:
        return None


def _open_local_sqlite(db: str = "user.db"):
    """Open the on-disk SQLite file for `db` (plaintext through db_crypto).

    Factored out of `_open` so the chat-panel READ endpoints can force the local
    file even when Postgres is the active backend (see `_open_read`)."""
    # Route through db_crypto so the viewer can open SQLCipher-encrypted files.
    # Map the canonical filenames to their db_id; anything else (per-session temp
    # / sibling DBs) is opened as plaintext via an unrecognised id. db_crypto sets
    # the matching row factory — do NOT reassign it (stdlib Row rejects a cipher
    # cursor).
    from app.db import db_crypto
    _DB_ID_BY_FILE = {
        "app.db": "app", "vault.db": "vault",
        "logs.db": "logs", "recordings.db": "recordings", "wiki.db": "wiki",
    }
    db_id = _DB_ID_BY_FILE.get(Path(db).name, "_viewer_other")
    conn = db_crypto.connect(str(_get_db_path(db)), db_id)
    # Under hybrid the local file is written continuously by the backend (streaming
    # persist), the sync puller, and now the panel's own local-first writes. WAL
    # lets readers proceed during a write, but a concurrent WRITER must wait for the
    # lock — without a busy_timeout it would raise "database is locked" immediately.
    # Give these short-lived panel connections a few seconds to acquire it.
    try:
        conn.execute("PRAGMA busy_timeout=5000")
    except Exception:
        pass

    return conn


def _session_columns(conn, dialect: str) -> set[str]:
    """Return session-table columns on either supported database backend."""
    if dialect == "postgres":
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'sessions'"
        ).fetchall()
        return {row[0] for row in rows}
    return {row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}


def _ensure_session_read_tracking_columns(conn, dialect: str) -> None:
    """Add the durable session read-state columns to legacy databases."""
    try:
        columns = _session_columns(conn, dialect)
        if dialect == "postgres":
            if "read_at" not in columns:
                conn.execute("ALTER TABLE sessions ADD COLUMN IF NOT EXISTS read_at TEXT")
            if "unread_at" not in columns:
                conn.execute("ALTER TABLE sessions ADD COLUMN IF NOT EXISTS unread_at TEXT")
            return
        changed = False
        if "read_at" not in columns:
            conn.execute("ALTER TABLE sessions ADD COLUMN read_at TEXT")
            changed = True
        if "unread_at" not in columns:
            conn.execute("ALTER TABLE sessions ADD COLUMN unread_at TEXT")
            changed = True
        if changed:
            conn.commit()
    except Exception:
        logger.exception("Could not add session read-tracking columns")
        raise


def _open(db: str = "user.db"):
    """Open the right connection for `db`. Returns (conn, dialect).

    - Postgres active + main DB → standalone autocommit PgPortableConnection
      ('postgres'). Autocommit isolates each statement so the endpoints'
      best-effort per-statement guards behave like they do on SQLite.
    - Otherwise the SQLite file ('sqlite').
    """
    conninfo = _pg_conninfo_for(db)
    if conninfo:
        # Reuse a warm autocommit connection from the shared viewer pool instead of
        # paying a fresh TLS handshake per call (the polled tail/related endpoints
        # hit this often). Falls back to a standalone connection automatically.
        from app.db.pg_portable import connect_viewer_pooled
        return connect_viewer_pooled(conninfo), "postgres"
    return _open_local_sqlite(db), "sqlite"


def _open_read(db: str = "user.db"):
    """Read-only opener for the CHAT-PANEL endpoints (session list, transcript,
    live-reconcile tail, related/family). Returns (conn, dialect).

    When the hybrid local-first layer is active AND `db` is the main database,
    serve the read from the LOCAL SQLite mirror — sub-millisecond, no network —
    instead of a fresh remote round-trip. The mirror is kept current by the sync
    puller (sessions / agents / session_runs / …) and the local-first transcript
    writes; a session that hasn't been mirrored on this device yet reads back
    empty, so callers that need a hard guarantee fall back to `_open` themselves
    (see get_session_messages). Everything else — the admin DB Viewer, arbitrary
    table browsing, all writes — keeps using `_open` and sees the real active
    backend.

    IMPORTANT: the local mirror is NOT user-scoped (the puller pulls every remote
    row), so callers MUST keep their existing per-user WHERE filters. They already
    do — this only swaps where the same query runs."""
    try:
        if _pg_conninfo_for(db) is not None:
            from app.db.hybrid import hybrid_enabled
            if hybrid_enabled():
                return _open_local_sqlite(db), "sqlite"
    except Exception:
        # Any doubt about the local mirror → fall back to the authoritative open.
        pass
    return _open(db)


# ── Cross-device transcript reconcile (Remote Control split-transcript fix) ────
# The chat read endpoints serve from the LOCAL mirror for speed, but `interactions`
# are PUSH-ONLY: a device pushes its own writes to the remote authority and NEVER
# pulls back rows another device wrote into the SAME session. So when Remote Control
# runs a turn on a different device, the SENDER (and any third viewer) reads only
# its own half of the transcript — the executor's reply lives on the executor + the
# remote authority but never reaches the viewer's local store, so it stays invisible
# even on reload (the reload only escapes to remote when the local copy is COMPLETELY
# empty, and the sender isn't — it holds its own user turn). This is the "split
# transcript" bug. Before a chat read we pull any of the session's remote rows this
# device is missing into local (INSERT OR IGNORE — the device's own full-fidelity
# rows are untouched, only genuinely-absent rows are added), bounded to at most once
# per `_PULL_TTL` seconds per session so a fast poll doesn't hammer the remote. This
# mirrors HybridBackend._ensure_local_session(refresh=True), which the raw-SQL viewer
# endpoints bypass. No-op on single-device installs and on sessions that already have
# every row locally (the INSERT OR IGNORE finds nothing new).
_PULL_AT: dict = {}
_PULL_TTL = 3.0


def _reconcile_session_from_remote(db: str, session_id: str) -> None:
    if not session_id:
        return
    try:
        if _pg_conninfo_for(db) is None:
            return
        from app.db.hybrid import hybrid_enabled
        if not hybrid_enabled():
            return
    except Exception:
        return
    now = time.monotonic()
    last = _PULL_AT.get(session_id)
    if last is not None and (now - last) < _PULL_TTL:
        return
    _PULL_AT[session_id] = now
    try:
        from app.db import get_db
        bk = get_db()
        hyb = getattr(bk, "backend", bk)
        remote = getattr(hyb, "_remote", None)
        if remote is None:
            return
        rraw = remote.get_raw_client()
        irows = rraw.table("interactions").select("*").eq(
            "session_id", session_id).execute().data or []
        if not irows:
            return
        conn = _open_local_sqlite(db)
        try:
            # Do not let an old remote-only column make the whole reconcile
            # incompatible with this device's local SQLite schema.  The typed
            # interaction surface is the contract; remote schema drift is not.
            _local_cols = {
                r[1] for r in conn.execute("PRAGMA table_info(interactions)").fetchall()
            }
            cols = [c for c in irows[0] if c in _local_cols]
            if not cols:
                return
            col_sql = ",".join(cols)
            ph = ",".join("?" * len(cols))
            # (1) Add any genuinely-missing rows. INSERT OR IGNORE never touches a
            # row this device already holds full-fidelity — only foreign rows land.
            conn.executemany(
                f"INSERT OR IGNORE INTO interactions ({col_sql}) VALUES ({ph})",
                [tuple(r.get(c) for c in cols) for r in irows],
            )
            # (2) Finalize a foreign row that was pulled while still STREAMING (its
            # id already existed locally with partial text, so (1) ignored it). Only
            # promote to a TERMINAL remote state (complete/error/interrupted) and
            # only when the local copy isn't already there — this refreshes the
            # executor's finished reply without clobbering a fresher local streaming
            # row (a device only streams its OWN runs, shown live over WS anyway).
            # ALSO: skip remote rows marked as placeholders (tool results >2048 chars
            # or slimmed output skeletons) — these are stubs pushed by the sync engine
            # and must never overwrite the local full-fidelity copy.
            for r in irows:
                st = r.get("status")
                if st not in ("complete", "error", "interrupted"):
                    continue
                # Skip remote placeholder rows — the full content lives locally.
                # The sync engine marks these in two ways:
                #   • tool rows: remote_placeholder=True in the metadata column
                #   • assistant rows: _remote_placeholder=true in the output JSON
                _meta = r.get("metadata")
                if _meta:
                    try:
                        _parsed = json.loads(_meta) if isinstance(_meta, str) else _meta
                        if isinstance(_parsed, dict) and _parsed.get("remote_placeholder"):
                            continue
                    except (json.JSONDecodeError, TypeError):
                        pass
                _output = r.get("output")
                if _output and isinstance(_output, str):
                    try:
                        _oparsed = json.loads(_output)
                        if isinstance(_oparsed, dict) and _oparsed.get("_remote_placeholder"):
                            continue
                    except (json.JSONDecodeError, TypeError):
                        pass
                conn.execute(
                        "UPDATE interactions SET content=?, status=?, output=?, "
                        "metadata=?, session_seq=? WHERE id=? AND "
                        "(status IS NULL OR status != ?)",
                        (r.get("content"), st, r.get("output"), r.get("metadata"),
                         r.get("session_seq"), r.get("id"), st),
                    )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.debug("reconcile_session_from_remote(%s) failed: %s", session_id, e)


def _hybrid_backend_for(db: str):
    """Return the active HybridBackend (unwrapped from the encryption decorator)
    when the local-first layer is on AND `db` is the main database, else None.

    Used by the chat-panel WRITE endpoints (rename / delete) to apply the edit to
    the LOCAL mirror immediately — so the panel's now-local reads are instantly
    consistent — and queue the authoritative push to the remote via the sync
    engine's outbox (no remote round-trip in the request)."""
    try:
        if _pg_conninfo_for(db) is None:
            return None
        from app.db.hybrid import hybrid_enabled, HybridBackend
        if not hybrid_enabled():
            return None
        from app.db import get_db
        inst = get_db()
        inner = inst if isinstance(inst, HybridBackend) else getattr(inst, "_inner", None)
        return inner if isinstance(inner, HybridBackend) else None
    except Exception:
        return None


async def _enqueue_remote_push(hb, ids, op: str = "upsert") -> None:
    """Queue ``sessions`` rows for the sync engine to push to the remote authority.

    ``op='upsert'`` propagates a rename / pin / hide / soft-delete (status flip);
    ``op='delete'`` propagates a hard delete (the pusher issues a remote DELETE)."""
    ids = [i for i in (ids or []) if i]
    if not hb or not ids:
        return
    try:
        from app.db.sync.outbox import Outbox
        await Outbox(hb.local).enqueue_many([("sessions", i, op) for i in ids])
    except Exception as e:
        logger.warning("hybrid: enqueue of session push failed: %s", e)


@router.post("/sessions/refresh")
async def refresh_session_metadata(
    request: Request,
    user_id: str = Query(..., description="User ID"),
    db: str = Query("user.db", description="Database filename"),
):
    """Refresh only newer session-list metadata into the local hybrid mirror.

    The chat picker calls this after it paints its cached local list.  Returning
    only a change flag lets the browser avoid a second list render when the
    shared authority has not changed.
    """
    token = request.headers.get("Authorization", "")
    token = token[7:] if token.startswith("Bearer ") else request.query_params.get("token", "")
    payload = decode_token(token) if token else None
    identities = {v for v in ((payload or {}).get("user_id"), (payload or {}).get("sub")) if v}
    if payload and user_id not in identities:
        raise HTTPException(status_code=403, detail="Not authorized for this user's sessions")
    hb = _hybrid_backend_for(db)
    if not hb:
        return {"changed": False, "session_ids": []}
    try:
        changed = await hb.refresh_session_metadata(user_id)
        return {"changed": bool(changed), "session_ids": changed}
    except Exception as e:
        logger.debug("hybrid: session metadata refresh failed: %s", e)
        # The cached local list remains usable if the remote is briefly offline.
        return {"changed": False, "session_ids": []}


def _hard_delete_family(conn, targets) -> dict:
    """Erase a session run-family — interactions, summaries, pipeline events, the
    session rows themselves, and the spawned-clone cascade — on ``conn``, commit,
    and return counts. Runs identically on the local mirror and the remote
    authority (portable SQL), so a hard delete cleans BOTH."""
    cur = conn.cursor()
    interactions_deleted = 0
    session_deleted = 0
    try:
        memory_links_removed, memory_pages_deleted = _quota_prune_memory_provenance(
            conn, {str(target) for target in targets if target},
        )
    except Exception:
        memory_links_removed = memory_pages_deleted = 0

    def _delete_optional(sql, params):
        try:
            cur.execute(sql, params)
        except Exception:
            pass

    for tid in targets:
        cur.execute('DELETE FROM interactions WHERE session_id = ?', (tid,))
        interactions_deleted += cur.rowcount
        # Delete all session-scoped state, including the run and spawn ledgers.
        # Leaving either behind makes a permanently deleted session reappear in
        # a related-session view on another device.
        for table in ("session_summaries", "session_summary_segments", "pipeline_events",
                      "session_runs", "messages", "browser_sessions", "session_notifications"):
            _delete_optional(f'DELETE FROM {table} WHERE session_id = ?', (tid,))
        _delete_optional('DELETE FROM agent_spawns WHERE orchestrator_session_id = ?', (tid,))
        _delete_optional('DELETE FROM agent_spawns WHERE spawn_session_id = ?', (tid,))
        cur.execute('DELETE FROM sessions WHERE id = ?', (tid,))
        session_deleted += cur.rowcount
    # Cascade: an orchestrator session takes its spawned CLONES with it — their
    # agents, sessions and transcripts (recursively). Best-effort; only touches
    # status='clone' agents, so a real agent is never caught.
    clones_deleted = 0
    try:
        from app.db.local import cascade_delete_clones
        clones_deleted = cascade_delete_clones(conn, targets)
    except Exception as _ce:  # noqa: BLE001
        logger.debug("clone cascade on session delete failed: %s", _ce)
    conn.commit()
    return {
        "interactions_deleted": interactions_deleted,
        "session_deleted": session_deleted,
        "clones_deleted": clones_deleted,
        "memory_links_removed": memory_links_removed,
        "memory_pages_deleted": memory_pages_deleted,
    }


_DEFAULT_USER_DATABASE_LIMIT_MB = 100
_MIN_USER_DATABASE_LIMIT_MB = 10
_MAX_USER_DATABASE_LIMIT_MB = 10_240
_DEFAULT_TOOL_OUTPUT_SHARE_PERCENT = 10
_DEFAULT_MEMORY_SHARE_PERCENT = 10
_MIN_CONVERSATION_SHARE_PERCENT = 10
_QUOTA_TOOL_OUTPUT_PLACEHOLDER = (
    "Stored tool output removed to manage storage. The original tool request is "
    "retained. Re-run only if the operation is safe and current output is needed."
)
_QUOTA_CLEANUP_LOCKS: dict[str, asyncio.Lock] = {}


def _user_database_limit_bytes() -> Optional[int]:
    """Return the configured per-user SQLite limit, or ``None`` when disabled.

    The App Functions catalog owns both the switch and its number field.  Keep
    parsing defensive because the generic config endpoint intentionally stores
    JSON values as supplied by the browser (and older hand-edited configs may
    contain a string).
    """
    try:
        from app.abilities import app_function_enabled
        if not app_function_enabled("user_database_size_limit"):
            return None
        from app.admin import ability_config
        configured = ability_config.get_ability_config("user_database_size_limit")
        raw_mb = configured.get("max_size_mb", _DEFAULT_USER_DATABASE_LIMIT_MB)
        megabytes = int(float(raw_mb))
    except (TypeError, ValueError, OverflowError):
        megabytes = _DEFAULT_USER_DATABASE_LIMIT_MB
    except Exception:
        # A configuration read failure must retain the documented safe default,
        # rather than silently allowing an unlimited database to grow.
        megabytes = _DEFAULT_USER_DATABASE_LIMIT_MB
    megabytes = max(_MIN_USER_DATABASE_LIMIT_MB, min(_MAX_USER_DATABASE_LIMIT_MB, megabytes))
    return megabytes * 1024 * 1024


def _user_database_quota_shares() -> tuple[int, int, int]:
    """Return conversation/tool/memory preferred shares as whole percentages.

    The two configurable cache shares are clamped defensively and conversation
    storage receives the remainder.  Keeping at least ten percent for the core
    transcript prevents a malformed config from making messages the first thing
    quota cleanup has to sacrifice.
    """
    tool_percent = _DEFAULT_TOOL_OUTPUT_SHARE_PERCENT
    memory_percent = _DEFAULT_MEMORY_SHARE_PERCENT
    try:
        from app.admin import ability_config
        configured = ability_config.get_ability_config("user_database_size_limit")
        tool_percent = int(float(configured.get(
            "tool_output_share_percent", _DEFAULT_TOOL_OUTPUT_SHARE_PERCENT)))
        memory_percent = int(float(configured.get(
            "memory_share_percent", _DEFAULT_MEMORY_SHARE_PERCENT)))
    except (TypeError, ValueError, OverflowError):
        tool_percent = _DEFAULT_TOOL_OUTPUT_SHARE_PERCENT
        memory_percent = _DEFAULT_MEMORY_SHARE_PERCENT
    except Exception:
        pass
    tool_percent = max(0, min(80, tool_percent))
    memory_percent = max(0, min(50, memory_percent))
    available = 100 - _MIN_CONVERSATION_SHARE_PERCENT
    if tool_percent + memory_percent > available:
        # Preserve the user's tool preference first; memory's rebuildable index
        # is the safer bucket to squeeze when a hand-edited config exceeds 90%.
        memory_percent = max(0, available - tool_percent)
    return 100 - tool_percent - memory_percent, tool_percent, memory_percent


def _sqlite_file_footprint(db_path: Path) -> int:
    """Bytes occupied by a SQLite authority file and its live WAL.

    SQLite commonly keeps recently committed transcript data in ``-wal`` until
    a checkpoint.  Counting it prevents the quota from appearing satisfied
    while the disk is actually over budget.  The shared-memory sidecar is not
    counted: it is a small coordination file, not durable user content.
    """
    total = 0
    for path in (db_path, Path(f"{db_path}-wal")):
        try:
            total += path.stat().st_size
        except OSError:
            pass
    return total


def _compact_sqlite_for_quota(conn) -> None:
    """Checkpoint and reclaim freed SQLite pages once after quota cleanup."""
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception as exc:  # A concurrent writer may temporarily hold the WAL.
        logger.debug("quota WAL checkpoint skipped: %s", exc)
    try:
        conn.execute("VACUUM")
    except Exception as exc:
        logger.debug("quota VACUUM skipped: %s", exc)


def _sqlite_used_bytes(conn) -> int:
    """Return SQLite's live main-file allocation, excluding reusable pages.

    File size is the quota trigger, but it is a bad loop condition: DELETE makes
    pages reusable without shrinking the file until VACUUM.  Measuring live pages
    lets the worker stop deleting at the quota and compact only once afterward.
    """
    page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
    page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
    freelist_count = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
    return max(0, page_count - freelist_count) * page_size


def _quota_sort_time(value) -> float:
    """Normalize mixed legacy SQLite/ISO timestamps for deterministic ordering."""
    if not value:
        return 0.0
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _quota_json(value, fallback):
    if not value:
        return fallback
    try:
        parsed = json.loads(value)
        return parsed
    except (TypeError, json.JSONDecodeError):
        return fallback


def _quota_text_bytes(value) -> int:
    if value is None:
        return 0
    if isinstance(value, bytes):
        return len(value)
    return len(str(value).encode("utf-8", "replace"))


def _quota_logical_usage(conn) -> dict:
    """Estimate reclaimable content by policy bucket, independent of DB pages."""
    usage = {"conversation": 0, "tool_outputs": 0, "memory": 0}
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(interactions)")}
        wanted = [name for name in ("role", "content", "output", "metadata") if name in columns]
        if wanted:
            for row in conn.execute(f"SELECT {', '.join(wanted)} FROM interactions"):
                item = dict(zip(wanted, row))
                role = str(item.get("role") or "")
                if role == "tool":
                    usage["tool_outputs"] += sum(
                        _quota_text_bytes(item.get(name)) for name in ("content", "output", "metadata")
                    )
                else:
                    usage["conversation"] += _quota_text_bytes(item.get("content"))
                    output = item.get("output")
                    if role == "assistant" and output:
                        parsed = _quota_json(output, None)
                        if isinstance(parsed, dict):
                            tool_part = {}
                            for key in ("tool_calls", "_sent_messages"):
                                if key in parsed:
                                    tool_part[key] = parsed.pop(key)
                            usage["tool_outputs"] += _quota_text_bytes(
                                json.dumps(tool_part, separators=(",", ":")) if tool_part else None)
                            usage["conversation"] += _quota_text_bytes(
                                json.dumps(parsed, separators=(",", ":")) if parsed else None)
                        else:
                            usage["conversation"] += _quota_text_bytes(output)
                    else:
                        usage["conversation"] += _quota_text_bytes(output)
                    usage["conversation"] += _quota_text_bytes(item.get("metadata"))
    except Exception:
        pass

    try:
        for row in conn.execute(
            "SELECT compiled_truth, timeline, frontmatter, provenance FROM memories"
        ):
            usage["memory"] += sum(_quota_text_bytes(value) for value in row)
    except Exception:
        pass
    try:
        for row in conn.execute(
            "SELECT chunk_text, embedding FROM memory_chunks"
        ):
            usage["memory"] += sum(_quota_text_bytes(value) for value in row)
    except Exception:
        pass
    usage["total_content"] = sum(usage.values())
    return usage


def _quota_evict_tool_outputs(
    conn, ranked_families: list[tuple], target_bytes: int,
) -> tuple[int, int]:
    """Replace old tool results with replay-safe receipts until the share fits."""
    usage = _quota_logical_usage(conn)
    remaining = usage["tool_outputs"]
    if remaining <= target_bytes:
        return 0, 0
    columns = {row[1] for row in conn.execute("PRAGMA table_info(interactions)")}
    required = {"id", "session_id", "role", "content"}
    if not required <= columns:
        return 0, 0
    has_output = "output" in columns
    has_metadata = "metadata" in columns
    has_created = "created_at" in columns
    has_status = "status" in columns
    select_cols = ["id", "content"]
    if has_output:
        select_cols.append("output")
    if has_metadata:
        select_cols.append("metadata")
    # chat_component rows are LIVE UI state (the agent panel + its tabs), not
    # replayable history — evicting their content would wipe active components.
    has_tool_name = "tool_name" in columns
    if has_tool_name:
        select_cols.append("tool_name")
    evicted = reclaimed = 0
    placeholder_bytes = _quota_text_bytes(_QUOTA_TOOL_OUTPUT_PLACEHOLDER)

    for _tier, _age, _root, family_ids in ranked_families:
        if remaining <= target_bytes:
            break
        marks = ",".join("?" for _ in family_ids)
        status_sql = " AND status != 'deleted'" if has_status else ""
        order_sql = " ORDER BY created_at ASC, id ASC" if has_created else " ORDER BY id ASC"
        rows = conn.execute(
            f"SELECT {', '.join(select_cols)} FROM interactions "
            f"WHERE session_id IN ({marks}) AND role = 'tool'{status_sql}{order_sql}",
            family_ids,
        ).fetchall()
        for row in rows:
            if remaining <= target_bytes:
                break
            item = dict(zip(select_cols, row))
            if has_tool_name and item.get("tool_name") == "chat_component":
                continue  # live UI state — never evict
            meta = _quota_json(item.get("metadata"), {}) if has_metadata else {}
            if isinstance(meta, dict) and meta.get("payload_state") == "evicted_local":
                continue
            before = sum(_quota_text_bytes(item.get(name)) for name in ("content", "output", "metadata"))
            if before <= placeholder_bytes + 256:
                continue
            if not isinstance(meta, dict):
                meta = {}
            # The canonical request remains on the assistant row, so duplicated
            # result-row args can go while small execution receipts stay useful.
            meta.pop("args", None)
            meta.update({
                "payload_state": "evicted_local",
                "storage_reason": "tool_payload_quota",
                "original_bytes": before,
                "evicted_at": datetime.now(timezone.utc).isoformat(),
            })
            assignments = ["content = ?"]
            values = [_QUOTA_TOOL_OUTPUT_PLACEHOLDER]
            if has_output:
                assignments.append("output = NULL")
            if has_metadata:
                assignments.append("metadata = ?")
                values.append(json.dumps(meta, separators=(",", ":")))
            values.append(item["id"])
            conn.execute(
                f"UPDATE interactions SET {', '.join(assignments)} WHERE id = ?", values,
            )
            after = placeholder_bytes + (_quota_text_bytes(values[-2]) if has_metadata else 0)
            delta = max(0, before - after)
            remaining = max(0, remaining - delta)
            reclaimed += delta
            evicted += 1
    if evicted:
        conn.commit()
    return evicted, reclaimed


def _quota_reclaim_memory_cache(conn, target_bytes: int) -> tuple[int, int]:
    """Drop rebuildable vector chunks, unpinned before pinned, to fit memory share."""
    usage = _quota_logical_usage(conn)
    remaining = usage["memory"]
    if remaining <= target_bytes:
        return 0, 0
    try:
        rows = conn.execute(
            """SELECT m.id, COALESCE(m.pinned, 0), m.updated_at,
                      COALESCE(SUM(LENGTH(mc.chunk_text)), 0) +
                      COALESCE(SUM(LENGTH(mc.embedding)), 0) AS chunk_bytes
               FROM memories m JOIN memory_chunks mc ON mc.memory_id = m.id
               GROUP BY m.id, m.pinned, m.updated_at
               ORDER BY COALESCE(m.pinned, 0) ASC, m.updated_at ASC, m.id ASC"""
        ).fetchall()
    except Exception:
        return 0, 0
    pages = reclaimed = 0
    for memory_id, _pinned, _updated_at, chunk_bytes in rows:
        if remaining <= target_bytes:
            break
        conn.execute("DELETE FROM memory_chunks WHERE memory_id = ?", (memory_id,))
        delta = max(0, int(chunk_bytes or 0))
        remaining = max(0, remaining - delta)
        reclaimed += delta
        pages += 1
    if pages:
        conn.commit()
    return pages, reclaimed


def _quota_prune_memory_provenance(conn, deleted_session_ids: set[str]) -> tuple[int, int]:
    """Remove deleted-session evidence and orphaned automatic memory pages."""
    if not deleted_session_ids:
        return 0, 0
    try:
        rows = conn.execute(
            "SELECT id, slug, origin, COALESCE(pinned, 0), provenance FROM memories"
        ).fetchall()
    except Exception:
        return 0, 0
    links_removed = pages_deleted = 0
    for memory_id, slug, origin, pinned, raw_provenance in rows:
        provenance = _quota_json(raw_provenance, [])
        if not isinstance(provenance, list) or not provenance:
            continue

        def _source_session(item):
            if isinstance(item, dict):
                return item.get("session_id") or item.get("session")
            return item if isinstance(item, str) else None

        kept = [item for item in provenance if _source_session(item) not in deleted_session_ids]
        removed = len(provenance) - len(kept)
        if not removed:
            continue
        links_removed += removed
        if not kept and not pinned and str(origin or "distilled") == "distilled" \
                and str(slug or "").startswith("chat/"):
            conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            pages_deleted += 1
        else:
            conn.execute(
                "UPDATE memories SET provenance = ?, updated_at = updated_at WHERE id = ?",
                (json.dumps(kept, separators=(",", ":")), memory_id),
            )
    if links_removed:
        conn.commit()
    return links_removed, pages_deleted


def _quota_cleanup_local(
    db: str,
    db_path: Path,
    user_id: str,
    protected_session_ids: set[str],
    limit_bytes: int,
    tool_output_share_percent: int = _DEFAULT_TOOL_OUTPUT_SHARE_PERCENT,
    memory_share_percent: int = _DEFAULT_MEMORY_SHARE_PERCENT,
) -> dict:
    """Reclaim cache payloads, then locally ranked session families, and compact.

    This function is intentionally synchronous: its caller runs it in a worker
    thread so checkpoint/VACUUM never stalls FastAPI's event loop.
    """
    result = {
        "enabled": True,
        "limit_bytes": limit_bytes,
        "size_before_bytes": _sqlite_file_footprint(db_path),
        "size_after_bytes": _sqlite_file_footprint(db_path),
        "used_before_bytes": 0,
        "used_after_bytes": 0,
        "purged_sessions": 0,
        "purged_interactions": 0,
        "purged_recycled": 0,
        "purged_active": 0,
        "purged_unpinned": 0,
        "purged_pinned": 0,
        "evicted_tool_outputs": 0,
        "tool_output_bytes_reclaimed": 0,
        "memory_cache_pages_reclaimed": 0,
        "memory_cache_bytes_reclaimed": 0,
        "memory_links_removed": 0,
        "memory_pages_deleted": 0,
        "conversation_share_percent": 100 - tool_output_share_percent - memory_share_percent,
        "tool_output_share_percent": tool_output_share_percent,
        "memory_share_percent": memory_share_percent,
        "usage_before": {},
        "usage_after": {},
        "protected_families": 0,
        "deleted_families": [],
    }
    conn = _open_local_sqlite(db)
    try:
        # Reclaim a stale WAL before deciding that user history must be removed.
        # A busy reader may prevent truncation; logical live pages below remain a
        # safe deletion threshold and avoid treating WAL bytes as session data.
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception as exc:
            logger.debug("quota preflight WAL checkpoint skipped: %s", exc)

        used_bytes = _sqlite_used_bytes(conn)
        result["used_before_bytes"] = used_bytes
        result["usage_before"] = _quota_logical_usage(conn)
        result["size_before_bytes"] = _sqlite_file_footprint(db_path)
        if used_bytes <= limit_bytes:
            # The overage was reusable main-file space rather than live history.
            # Reclaim it without deleting a session (still exactly one VACUUM,
            # and still on the worker thread).
            if result["size_before_bytes"] > limit_bytes:
                _compact_sqlite_for_quota(conn)
            result["used_after_bytes"] = used_bytes
            result["usage_after"] = result["usage_before"]
            result["size_after_bytes"] = _sqlite_file_footprint(db_path)
            return result

        session_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()
        }
        pinned_sql = "COALESCE(pinned, 0)" if "pinned" in session_columns else "0"
        rows = conn.execute(
            f"""SELECT id, status, {pinned_sql} AS pinned, created_at, updated_at
                FROM sessions WHERE user_id = ?""",
            (user_id,),
        ).fetchall()
        session_rows = {
            str(row[0]): {
                "status": str(row[1] or "active"),
                "pinned": bool(row[2]),
                "created_at": row[3],
                "updated_at": row[4],
            }
            for row in rows if row[0]
        }
        # Build connected components from the spawn ledger. Quota decisions and
        # hard deletes operate on a whole run-family, never an isolated child.
        parent = {sid: sid for sid in session_rows}

        def _find(sid: str) -> str:
            while parent[sid] != sid:
                parent[sid] = parent[parent[sid]]
                sid = parent[sid]
            return sid

        def _union(left: str, right: str) -> None:
            left_root, right_root = _find(left), _find(right)
            if left_root != right_root:
                parent[max(left_root, right_root)] = min(left_root, right_root)

        try:
            edges = conn.execute(
                "SELECT orchestrator_session_id, spawn_session_id FROM agent_spawns"
            ).fetchall()
        except Exception:
            edges = []
        for edge in edges:
            left, right = str(edge[0] or ""), str(edge[1] or "")
            if left in parent and right in parent:
                _union(left, right)

        families: dict[str, list[str]] = {}
        for sid in session_rows:
            families.setdefault(_find(sid), []).append(sid)

        interaction_activity = {}
        try:
            for row in conn.execute(
                "SELECT session_id, MAX(created_at) FROM interactions GROUP BY session_id"
            ).fetchall():
                if row[0]:
                    interaction_activity[str(row[0])] = row[1]
        except Exception:
            pass

        live_session_ids: set[str] = set()
        try:
            for row in conn.execute(
                "SELECT session_id FROM session_runs WHERE status IN ('running', 'queued')"
            ).fetchall():
                if row[0]:
                    live_session_ids.add(str(row[0]))
        except Exception:
            pass

        candidates = []
        for root, family_ids in families.items():
            family_set = set(family_ids)
            if family_set & protected_session_ids or family_set & live_session_ids:
                result["protected_families"] += 1
                continue
            members = [session_rows[sid] for sid in family_ids]
            all_recycled = all(member["status"] == "recycled" for member in members)
            any_pinned = any(member["pinned"] for member in members)
            tier = 0 if all_recycled else (2 if any_pinned else 1)
            if tier == 0:
                # Recycle stamps updated_at, so the newest member determines when
                # the entire family became safely eligible for bin eviction.
                age = max(
                    _quota_sort_time(member["updated_at"] or member["created_at"])
                    for member in members
                )
            else:
                # Metadata changes must not make old sessions look recently used.
                age = max(
                    _quota_sort_time(
                        interaction_activity.get(sid) or session_rows[sid]["created_at"]
                    )
                    for sid in family_ids
                )
            candidates.append((tier, age, root, sorted(family_ids)))
        candidates.sort(key=lambda item: (item[0], item[1], item[2]))

        # Preferred shares are enforced only while the total database is under
        # pressure. Tool output is the cheapest context to lose, followed by
        # rebuildable vector chunks; core messages remain intact through both.
        initial_overage = max(0, used_bytes - limit_bytes)
        tool_target = limit_bytes * max(0, tool_output_share_percent) // 100
        memory_target = limit_bytes * max(0, memory_share_percent) // 100
        evicted, tool_reclaimed = _quota_evict_tool_outputs(
            conn, candidates, tool_target,
        )
        result["evicted_tool_outputs"] = evicted
        result["tool_output_bytes_reclaimed"] = tool_reclaimed
        memory_pages, memory_reclaimed = _quota_reclaim_memory_cache(
            conn, memory_target,
        )
        result["memory_cache_pages_reclaimed"] = memory_pages
        result["memory_cache_bytes_reclaimed"] = memory_reclaimed
        reclaimed_before_sessions = tool_reclaimed + memory_reclaimed
        used_bytes = _sqlite_used_bytes(conn)

        for tier, _age, root, family_ids in candidates:
            if used_bytes <= limit_bytes or reclaimed_before_sessions >= initial_overage:
                break
            counts = _hard_delete_family(conn, family_ids)
            result["memory_links_removed"] += counts.get("memory_links_removed", 0)
            result["memory_pages_deleted"] += counts.get("memory_pages_deleted", 0)
            used_bytes = _sqlite_used_bytes(conn)
            result["purged_sessions"] += counts["session_deleted"]
            result["purged_interactions"] += counts["interactions_deleted"]
            if tier == 0:
                result["purged_recycled"] += counts["session_deleted"]
            else:
                result["purged_active"] += counts["session_deleted"]
                key = "purged_pinned" if tier == 2 else "purged_unpinned"
                result[key] += counts["session_deleted"]
            result["deleted_families"].append({
                "root_session_id": root,
                "session_ids": family_ids,
                "tier": ("recycled", "active", "pinned")[tier],
            })

        result["used_after_bytes"] = used_bytes
        if evicted or memory_pages or result["deleted_families"]:
            _compact_sqlite_for_quota(conn)
            result["used_after_bytes"] = _sqlite_used_bytes(conn)
        result["usage_after"] = _quota_logical_usage(conn)
        result["size_after_bytes"] = _sqlite_file_footprint(db_path)
        return result
    finally:
        conn.close()


async def _replicate_hard_session_delete(
    db: str, hb, targets: list[str], request: Optional[Request],
) -> bool:
    """Mirror a permanent session-family delete and publish peer tombstones.

    Returns whether a durable outbox retry was required.  This is shared by the
    explicit permanent-delete route and automatic quota cleanup so their hybrid
    semantics cannot drift apart.
    """
    if not hb or not targets:
        return False
    remote_pending = False
    remote_deleted = False
    try:
        rconn, _ = _open(db)
        try:
            _hard_delete_family(rconn, targets)
            remote_deleted = True
        finally:
            rconn.close()
    except Exception as exc:  # noqa: BLE001
        remote_pending = True
        logger.warning("hybrid: remote hard-delete replication failed (%s); queued durable retry", exc)
        await _enqueue_remote_push(hb, targets, "delete")
    if remote_deleted:
        try:
            from app.db.sync.tombstones import record_tombstones
            from app.db import get_db as _get_db
            owner = None
            if request is not None:
                try:
                    from app.auth.identity import caller_uid_sync
                    owner = caller_uid_sync(request)
                except Exception:  # noqa: BLE001
                    pass
            written = record_tombstones(_get_db(), [("sessions", target) for target in targets], owner)
            if written != len(targets):
                remote_pending = True
                await _enqueue_remote_push(hb, targets, "delete")
        except Exception as exc:  # noqa: BLE001
            logger.debug("hybrid: session tombstone record failed: %s", exc)
            remote_pending = True
            await _enqueue_remote_push(hb, targets, "delete")
    return remote_pending


async def _enforce_user_database_size_limit(
    db: str,
    db_path: Path,
    user_id: Optional[str],
    protected_session_ids: list[str],
    hb,
    request: Optional[Request],
) -> dict:
    """Keep one user's SQLite store within the configured limit after recycling.

    The freshly recycled session family is protected: quota cleanup only removes
    *older* binned sessions first. Once no older bin entries remain, it moves to
    old active sessions. If the protected recycle alone exceeds the quota, it is
    retained and the result reports the remaining excess rather than deleting
    the session family the user just chose to keep recoverable.
    """
    limit_bytes = _user_database_limit_bytes()
    conversation_share, tool_share, memory_share = _user_database_quota_shares()
    protected_ids = {session_id for session_id in protected_session_ids if session_id}
    if db != "user.db" or not user_id or limit_bytes is None or not db_path.exists():
        return {"enabled": limit_bytes is not None, "purged_sessions": 0, "remote_pending": False}
    size_bytes = _sqlite_file_footprint(db_path)
    if size_bytes <= limit_bytes:
        return {
            "enabled": True,
            "limit_bytes": limit_bytes,
            "size_before_bytes": size_bytes,
            "size_after_bytes": size_bytes,
            "used_before_bytes": size_bytes,
            "used_after_bytes": size_bytes,
            "purged_sessions": 0,
            "purged_interactions": 0,
            "purged_recycled": 0,
            "purged_active": 0,
            "purged_unpinned": 0,
            "purged_pinned": 0,
            "evicted_tool_outputs": 0,
            "tool_output_bytes_reclaimed": 0,
            "memory_cache_pages_reclaimed": 0,
            "memory_cache_bytes_reclaimed": 0,
            "memory_links_removed": 0,
            "memory_pages_deleted": 0,
            "conversation_share_percent": conversation_share,
            "tool_output_share_percent": tool_share,
            "memory_share_percent": memory_share,
            "protected_families": 0,
            "remote_pending": False,
        }

    lock_key = f"{db_path.resolve()}::{user_id}"
    lock = _QUOTA_CLEANUP_LOCKS.setdefault(lock_key, asyncio.Lock())
    async with lock:
        # All SQLite file maintenance runs outside the event loop. The request may
        # wait for its cleanup result, but health checks and unrelated users remain
        # responsive while a large database is compacted.
        result = await asyncio.to_thread(
            _quota_cleanup_local,
            db,
            db_path,
            user_id,
            protected_ids,
            limit_bytes,
            tool_share,
            memory_share,
        )
        result["remote_pending"] = False
        for family in result.pop("deleted_families", []):
            result["remote_pending"] |= await _replicate_hard_session_delete(
                db, hb, family["session_ids"], request,
            )
    if result["used_after_bytes"] > limit_bytes:
        logger.warning(
            "user database quota remains exceeded for %s (%d live bytes > %d); protected/live sessions retained",
            user_id[:12], result["used_after_bytes"], limit_bytes,
        )
    return result


def _resolve_session_db(session_id: str, fallback_db: str = "user.db") -> str:
    """If a session has temp_db_path in its metadata, route to that temp DB.
    Otherwise return fallback_db. Reads local-first when hybrid is on (this is a
    hot pre-read on every transcript/tail open) — falls back to the active
    backend automatically."""
    cache_key = (str(fallback_db or "user.db"), str(session_id or ""))
    now = time.monotonic()
    cached = _SESSION_DB_ROUTE_CACHE.get(cache_key)
    if cached is not None:
        expires_at, resolved = cached
        if expires_at > now:
            return resolved
        _SESSION_DB_ROUTE_CACHE.pop(cache_key, None)

    resolved = fallback_db
    lookup_succeeded = False
    try:
        conn, _dialect = _open_read(fallback_db)
        try:
            row = conn.execute("SELECT metadata FROM sessions WHERE id=?", (session_id,)).fetchone()
        finally:
            conn.close()
        lookup_succeeded = True
        if row and row['metadata']:
            meta = json.loads(row['metadata'])
            tdb = meta.get('temp_db_path', '')
            if tdb:
                resolved = os.path.basename(tdb)
    except Exception:
        # A transient lock/error must not poison routing for several seconds.
        # Preserve the legacy fallback for this request and retry next poll.
        return fallback_db

    ttl = (_SESSION_DB_ROUTE_POSITIVE_TTL_S
           if resolved != fallback_db else _SESSION_DB_ROUTE_NEGATIVE_TTL_S)
    if len(_SESSION_DB_ROUTE_CACHE) >= _SESSION_DB_ROUTE_CACHE_MAX:
        # Expiry timestamps make the oldest entry the cheapest deterministic
        # victim; boundedness matters because session ids are client supplied.
        oldest = min(_SESSION_DB_ROUTE_CACHE, key=lambda key: _SESSION_DB_ROUTE_CACHE[key][0])
        _SESSION_DB_ROUTE_CACHE.pop(oldest, None)
    if lookup_succeeded:
        _SESSION_DB_ROUTE_CACHE[cache_key] = (now + ttl, resolved)
    return resolved


@router.get("/tables")
async def list_tables(
    db: str = Query("user.db", description="Database filename"),
    _auth: dict = Depends(require_db_auth),
):
    """List all tables in the database."""
    user_id, is_admin = _get_caller(_auth)
    is_pg = _pg_conninfo_for(db) is not None
    cache_key = f"{db}|{1 if is_admin else 0}|{user_id or ''}"

    # File-stat cache only applies to SQLite (Postgres has no file to stat, and
    # its row counts change without touching any file). On PG we always compute.
    mtime, size = 0.0, 0
    if not is_pg:
        db_path = _get_db_path(db)
        try:
            st = db_path.stat()
            mtime, size = st.st_mtime, st.st_size
        except OSError:
            mtime, size = 0.0, 0
        now = time.monotonic()
        cached = _TABLES_CACHE.get(cache_key)
        if cached is not None:
            c_mtime, c_size, c_expiry, c_payload = cached
            if c_mtime == mtime and c_size == size and now < c_expiry:
                return c_payload

    try:
        conn, _dialect = _open(db)
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        tables = []
        for row in cur.fetchall():
            name = row["name"]
            # Get column info
            cur.execute(f"PRAGMA table_info(\"{name}\")")
            columns = [
                {"name": col[1], "type": col[2], "notnull": bool(col[3]), "pk": bool(col[5])}
                for col in cur.fetchall()
            ]
            # Row count is scoped to what the caller is allowed to see.
            clause, params = _acl_clause(name, user_id, is_admin)
            where_sql = f" WHERE {clause}" if clause else ""
            try:
                cur.execute(f'SELECT COUNT(*) FROM "{name}"{where_sql}', params)
                count = cur.fetchone()[0]
            except Exception:
                # ACL parent table missing (e.g. FTS or stale schema) — treat as 0
                # for non-admins; admins still see the real count via the
                # unfiltered query above falling through.
                count = 0
            tables.append({"name": name, "columns": columns, "row_count": count})
        conn.close()
        payload = {"tables": tables, "db": db, "is_admin": is_admin}
        if not is_pg:
            _TABLES_CACHE[cache_key] = (mtime, size, time.monotonic() + _TABLES_CACHE_TTL_S, payload)
        return payload
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/users")
async def list_users(
    db: str = Query("user.db", description="Database filename"),
    _auth: dict = Depends(require_db_auth),
):
    """List distinct user IDs from the database.

    Non-admins only see their own user_id (so they can't enumerate other
    accounts via this endpoint).
    """
    user_id, is_admin = _get_caller(_auth)
    if not is_admin:
        return {"users": [user_id] if user_id else [], "db": db}

    db_path = _get_db_path(db)
    try:
        conn, _dialect = _open(db)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        users = set()
        for tbl in ["sessions", "interactions", "messages"]:
            try:
                cur.execute(f'SELECT DISTINCT user_id FROM "{tbl}" WHERE user_id IS NOT NULL')
                for row in cur.fetchall():
                    users.add(row[0])
            except Exception:
                pass
        conn.close()
        return {"users": sorted(users), "db": db}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/users/{user_id}")
async def delete_user(
    user_id: str,
    db: str = Query("user.db", description="Database filename"),
    _auth: dict = Depends(require_db_auth),
):
    """Delete all sessions, interactions, and messages for a user."""
    caller_uid, is_admin = _get_caller(_auth)
    if not is_admin and caller_uid != user_id:
        raise HTTPException(status_code=403, detail="Not authorized to delete this user's data")
    from app.db.browser_policy import require_delete_enabled
    try:
        require_delete_enabled()
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    db_path = _get_db_path(db)
    try:
        conn, _dialect = _open(db)
        cur = conn.cursor()
        deleted = {}

        # Get all session IDs for this user
        sessions = conn.execute(
            "SELECT id FROM sessions WHERE user_id = ?", (user_id,)
        ).fetchall()
        session_ids = [row[0] for row in sessions]

        # Delete interactions for those sessions
        for sid in session_ids:
            cur.execute("DELETE FROM interactions WHERE session_id = ?", (sid,))
            deleted["interactions"] = deleted.get("interactions", 0) + cur.rowcount
            try:
                cur.execute("DELETE FROM pipeline_events WHERE session_id = ?", (sid,))
            except Exception:
                pass
            try:
                cur.execute("DELETE FROM messages WHERE session_id = ?", (sid,))
            except Exception:
                pass

        # Delete sessions
        cur.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        deleted["sessions"] = cur.rowcount

        # Delete session summaries
        try:
            cur.execute("DELETE FROM session_summaries WHERE user_id = ?", (user_id,))
            deleted["summaries"] = cur.rowcount
        except Exception:
            pass

        # Delete attachments
        try:
            cur.execute("DELETE FROM attachments WHERE user_id = ?", (user_id,))
            deleted["attachments"] = cur.rowcount
        except Exception:
            pass

        conn.commit()
        conn.close()
        logger.info(f"Deleted user {user_id[:12]}: {deleted}")
        return {"success": True, "user_id": user_id, "deleted": deleted}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/sessions/{session_id}")
async def delete_session(
    session_id: str,
    request: Request,
    db: str = Query("user.db", description="Database filename"),
    permanent: bool = Query(False, description="Hard-delete instead of recycling"),
):
    """Recycle a session (soft delete) or, with ``permanent=true``, erase it.

    Default: the session is moved to the bin (status -> 'recycled') and hidden
    from the chat dropdown, but its transcript is kept. It is truly erased only
    when its agent is permanently emptied from the recycling bin (or via an
    explicit ``permanent=true`` call from the future sessions page).
    """
    if permanent:
        from app.db.browser_policy import require_delete_enabled
        try:
            require_delete_enabled()
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc))
    db_path = _get_db_path(db)
    hb = _hybrid_backend_for(db)
    try:
        # Local-first: apply the recycle/delete to the LOCAL mirror (instant, and
        # consistent with the panel's now-local reads). The authoritative push is
        # queued below — an upsert of the recycled row for a soft delete, or a
        # remote DELETE for a hard erase — and drained by the sync engine in ~1s.
        conn, _dialect = (_open_local_sqlite(db), "sqlite") if hb else _open(db)
        cur = conn.cursor()

        # Ownership gate: only the session's owner/participant (or an admin, or
        # open/local mode) may delete it. Returns None for a phantom session with
        # no `sessions` row — those fall through to the orphan-transcript cleanup
        # below, exactly like the transcript read routes.
        if _session_access_ok(cur, session_id, request, None) is False:
            conn.close()
            raise HTTPException(status_code=403, detail="Not authorized for this session")

        # Resolve the run-family children (optimizer Planner/Closer + spawned
        # helpers) ONCE up front, so both the soft- and hard-delete paths take the
        # whole family with the parent. Best-effort; empty when nothing hangs off.
        try:
            from app.db.local import resolve_child_sessions
            child_ids = resolve_child_sessions(conn, [session_id])
        except Exception as _re:  # noqa: BLE001
            logger.debug("child-session resolve on delete failed: %s", _re)
            child_ids = []

        # ── Soft delete (default): just flip the status, keep everything ──
        if not permanent:
            has_status = False
            try:
                cur.execute("PRAGMA table_info(sessions)")
                has_status = "status" in {row[1] for row in cur.fetchall()}
            except Exception:
                pass
            if has_status:
                owner_row = cur.execute("SELECT user_id FROM sessions WHERE id = ?", (session_id,)).fetchone()
                owner_user_id = owner_row[0] if owner_row else None
                # Bump updated_at alongside the status flip: the puller on ANOTHER
                # device only pulls rows whose watermark (updated_at) advanced, so
                # without this the recycle would never reach a second device's local
                # mirror and the session would linger there until a cold start.
                # (restore_session already stamps the time the same way.)
                cur.execute("UPDATE sessions SET status = 'recycled', updated_at = datetime('now') WHERE id = ?", (session_id,))
                recycled = cur.rowcount
                # A real session row was recycled → carry its children to the bin
                # too. If NOTHING matched (recycled == 0), this is a "phantom"
                # session: the Sessions page derived it straight from the
                # interactions log because it has no row in `sessions`. There's
                # nothing to recycle, so a soft delete would be a silent no-op and
                # the row would reappear on the next reload. In that case we fall
                # through to a hard delete of its orphan transcript so it actually
                # disappears.
                if recycled:
                    children_recycled = 0
                    recycled_ids = [session_id]
                    for cid in child_ids:
                        cur.execute("UPDATE sessions SET status = 'recycled', updated_at = datetime('now') WHERE id = ?", (cid,))
                        if cur.rowcount:
                            recycled_ids.append(cid)
                        children_recycled += cur.rowcount
                    conn.commit()
                    conn.close()
                    # ── Safety: kill any active loop and clear active state ──
                    # Set an interrupt signal so any running agent loop for this
                    # session (or its children) halts on the next interrupt check.
                    # Also clear the metadata active state so the loop can't
                    # re-activate from a cold read.
                    try:
                        from app.db import get_db as _get_db
                        _db = _get_db()
                        for _sid in recycled_ids:
                            try:
                                await _db.set_interrupt(_sid)
                            except Exception:
                                pass
                            try:
                                await _db.clear_session_active_state(_sid)
                            except Exception:
                                pass
                        logger.info("Recycled: killed loops for %d sessions", len(recycled_ids))
                    except Exception as _ke:
                        logger.warning("Recycled: loop-kill sweep failed: %s", _ke)
                    # Push the status flip to the remote authority (upsert).
                    if hb:
                        await _enqueue_remote_push(hb, recycled_ids, "upsert")
                    try:
                        from app.agent.file_checkout import release_session_checkouts
                        for _sid in recycled_ids:
                            await release_session_checkouts(_sid, "Session recycled")
                    except Exception as _ce:
                        logger.warning("Recycled: checkout release failed: %s", _ce)
                    quota = await _enforce_user_database_size_limit(
                        db, db_path, owner_user_id, recycled_ids, hb, request,
                    )
                    logger.info(f"Recycled session {session_id[:12]}: {recycled} session, "
                                f"{children_recycled} children")
                    return {
                        "success": True,
                        "session_id": session_id,
                        "recycled": recycled,
                        "children_recycled": children_recycled,
                        "quota_cleanup": quota,
                    }
            # Fall through to hard delete if the column isn't there yet OR this is
            # a phantom session with no `sessions` row to recycle.

        # ── Hard delete: erase the base session AND its whole run-family ──
        # Phantom-safe: deleting by session_id removes the orphan interactions
        # even when no `sessions` row exists.
        targets = [session_id] + [c for c in child_ids if c != session_id]
        # Kill any active loops before erasing the session row
        try:
            from app.db import get_db as _get_db2
            _db2 = _get_db2()
            for _sid in targets:
                try:
                    await _db2.set_interrupt(_sid)
                except Exception:
                    pass
        except Exception:
            pass
        counts = _hard_delete_family(conn, targets)
        conn.close()
        interactions_deleted = counts["interactions_deleted"]
        session_deleted = counts["session_deleted"]
        clones_deleted = counts["clones_deleted"]

        # A permanent erase must ALSO clean the shared remote authority — leaving
        # orphan transcript on the server would contradict the intent and could
        # resurface as a phantom session on another device. Hard delete is rare
        # (emptying the bin / an explicit permanent call), so a synchronous remote
        # pass is acceptable — unlike soft actions it is NOT queued to the outbox
        # (the outbox is row-id keyed and can't express "delete all interactions
        # for session X").
        remote_pending = await _replicate_hard_session_delete(db, hb, targets, request)
        try:
            from app.agent.file_checkout import release_session_checkouts
            for _sid in targets:
                await release_session_checkouts(_sid, "Session deleted")
        except Exception as _ce:
            logger.warning("Deleted: checkout release failed: %s", _ce)

        logger.info(f"Deleted session {session_id[:12]}: {session_deleted} session(s) "
                    f"(+{len(targets) - 1} family), {interactions_deleted} interactions, "
                    f"{clones_deleted} clones")
        return {
            "success": True,
            "session_id": session_id,
            "session_deleted": session_deleted,
            "interactions_deleted": interactions_deleted,
            "children_deleted": len(targets) - 1,
            "clones_deleted": clones_deleted,
            "remote_pending": remote_pending,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class SessionForkRequest(BaseModel):
    """Body for POST /sessions/{id}/fork — fork a session at a given interaction."""
    up_to_interaction_id: Optional[str] = None
    user_id: Optional[str] = None
    fork_kind: Literal["fork", "btw"] = "fork"


@router.post("/sessions/{session_id}/fork")
async def fork_session(
    session_id: str,
    req: SessionForkRequest,
    request: Request,
    db: str = Query("user.db", description="Database filename"),
):
    """Fork a session at a given interaction. Creates a new session as a copy of
    the original up to and including the specified interaction.

    Ordinary forks require ``up_to_interaction_id`` and keep the existing
    ``Fork:`` title. ``fork_kind='btw'`` may omit the interaction id, in which
    case the latest durable interaction is selected atomically; BTW forks use a
    ``btw:`` title and carry a durable source-session prompt overlay in metadata.
    The interaction's
    ``session_seq`` is used as the cut-off, so all interactions with
    ``session_seq <= target.session_seq`` are copied (with new IDs and remapped
    ``parent_id`` references). The forked session is independent: subsequent
    messages in the original are not mirrored.
    """
    hb = _hybrid_backend_for(db)
    try:
        conn, _dialect = (_open_local_sqlite(db), "sqlite") if hb else _open(db)
        cur = conn.cursor()

        # Ownership gate.
        if _session_access_ok(cur, session_id, request, req.user_id) is False:
            conn.close()
            raise HTTPException(status_code=403, detail="Not authorized for this session")

        # Resolve the target interaction's session_seq. BTW is a composer-level
        # command, so the server owns the "latest durable row" lookup; doing it
        # here avoids a race between a client tail read and the actual fork.
        target_interaction_id = (req.up_to_interaction_id or "").strip()
        if not target_interaction_id and req.fork_kind != "btw":
            conn.close()
            raise HTTPException(status_code=422, detail="up_to_interaction_id is required")
        if target_interaction_id:
            target_row = cur.execute(
                "SELECT id, session_seq, created_at FROM interactions WHERE id = ? AND session_id = ?",
                (target_interaction_id, session_id),
            ).fetchone()
        else:
            target_row = cur.execute(
                "SELECT id, session_seq, created_at FROM interactions "
                "WHERE session_id = ? AND session_seq IS NOT NULL "
                "AND (status IS NULL OR status != 'deleted') "
                "ORDER BY session_seq DESC, created_at DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        if not target_row:
            conn.close()
            detail = ("This session has no durable interactions to fork"
                      if req.fork_kind == "btw" and not target_interaction_id
                      else "Interaction not found in this session")
            raise HTTPException(status_code=404, detail=detail)
        target_interaction_id = target_row[0]
        target_seq = target_row[1] if target_row[1] is not None else 0

        # Fetch the original session's title and metadata.
        src_row = cur.execute(
            "SELECT title, user_id, metadata, agent_id, participants FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if not src_row:
            conn.close()
            raise HTTPException(status_code=404, detail="Session not found")
        src_title, src_user_id, src_meta_json, src_agent_id, src_participants = src_row
        if req.fork_kind == "btw" and not src_agent_id:
            conn.close()
            raise HTTPException(
                status_code=409,
                detail="This session is not linked to an agent yet",
            )

        # Determine the user_id for the new session.
        # Use the fork request's user_id, then fall back to the original session's owner.
        resolved_user_id = req.user_id or src_user_id

        # Create the new session.
        new_session_id = str(uuid.uuid4())
        title_prefix = "btw: " if req.fork_kind == "btw" else "Fork: "
        fork_title = (title_prefix + (src_title or "New Session"))[:120]
        # Copy metadata, strip run-specific fields, add fork origin.
        try:
            src_meta = json.loads(src_meta_json) if src_meta_json else {}
        except (json.JSONDecodeError, TypeError):
            src_meta = {}
        if not isinstance(src_meta, dict):
            src_meta = {}
        src_meta.pop("remote_executor", None)
        src_meta.pop("execution_mode", None)
        src_meta["forked_from"] = {
            "session_id": session_id,
            "interaction_id": target_interaction_id,
            "session_seq": target_seq,
        }
        if req.fork_kind == "btw":
            src_meta["auto_title_locked"] = True
            src_meta["btw"] = {
                "source_session_id": session_id,
                "source_title": src_title or "New Session",
                "source_interaction_id": target_interaction_id,
                "source_session_seq": target_seq,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }

        cur.execute(
            "INSERT INTO sessions (id, user_id, title, metadata, agent_id, participants, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'active', datetime('now'), datetime('now'))",
            (new_session_id, resolved_user_id, fork_title, json.dumps(src_meta),
             src_agent_id, src_participants),
        )

        # Fetch all interactions up to and including the target seq, ordered by
        # session_seq ASC so the new session_seq assignment is monotonic.
        rows = cur.execute(
            "SELECT id, parent_id, role, content, tool_name, tool_call_id, channel, "
            "       metadata, output, source, from_id, to_id, turn_id, turn_seq, status, "
            "       created_at "
            "FROM interactions "
            "WHERE session_id = ? AND session_seq IS NOT NULL AND session_seq <= ? "
            "ORDER BY session_seq ASC",
            (session_id, target_seq),
        ).fetchall()

        # Build a map: old_id → new_id, then insert with remapped parent_id.
        id_map: dict[str, str] = {}
        for row in rows:
            old_id = row[0]
            id_map[old_id] = str(uuid.uuid4())

        new_seq = 1
        for row in rows:
            old_id, old_parent_id, role, content, tool_name, tool_call_id, channel, \
                metadata, output, source, from_id, to_id, turn_id, turn_seq, status, created_at = row

            new_id = id_map[old_id]
            new_parent_id = id_map.get(old_parent_id) if old_parent_id else None
            if req.fork_kind == "btw" and status in {
                    "streaming", "queued", "running", "pending"}:
                # A snapshot must never inherit live/queued run state from its
                # source. Preserve the content but make its incompleteness clear.
                status = "interrupted"

            cur.execute(
                "INSERT INTO interactions (id, session_id, parent_id, role, content, "
                "    tool_name, tool_call_id, channel, metadata, output, source, "
                "    from_id, to_id, session_seq, turn_id, turn_seq, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (new_id, new_session_id, new_parent_id, role, content, tool_name,
                 tool_call_id, channel, metadata, output, source, from_id, to_id,
                 new_seq, turn_id, turn_seq, status, created_at),
            )
            new_seq += 1

        conn.commit()
        conn.close()

        if hb:
            await _enqueue_remote_push(hb, [new_session_id], "upsert")

        return {
            "success": True,
            "session_id": new_session_id,
            "title": fork_title,
            "interactions_copied": len(rows),
            "snapshot_session_seq": len(rows),
            "source_session_seq": target_seq,
            "source_interaction_id": target_interaction_id,
            "agent_id": src_agent_id,
            "fork_kind": req.fork_kind,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/sessions/{session_id}/restore")
async def restore_session(
    session_id: str,
    request: Request,
    db: str = Query("user.db", description="Database filename"),
):
    """Restore a recycled session back to active status."""
    db_path = _get_db_path(db)
    hb = _hybrid_backend_for(db)
    try:
        # Local-first (see delete_session) + queued upsert push.
        conn, _dialect = (_open_local_sqlite(db), "sqlite") if hb else _open(db)
        cur = conn.cursor()
        # Ownership gate (open/local mode + admins pass; non-owner refused).
        if _session_access_ok(cur, session_id, request, None) is False:
            conn.close()
            raise HTTPException(status_code=403, detail="Not authorized for this session")
        cur.execute("UPDATE sessions SET status = 'active', updated_at = datetime('now') WHERE id = ? AND status = 'recycled'", (session_id,))
        restored = cur.rowcount
        restored_ids = [session_id]
        # Bring the run-family back with the parent (mirrors the recycle cascade).
        children_restored = 0
        try:
            from app.db.local import resolve_child_sessions
            for cid in resolve_child_sessions(conn, [session_id]):
                cur.execute("UPDATE sessions SET status = 'active', updated_at = datetime('now') WHERE id = ? AND status = 'recycled'", (cid,))
                if cur.rowcount:
                    restored_ids.append(cid)
                children_restored += cur.rowcount
        except Exception as _re:  # noqa: BLE001
            logger.debug("child-session restore cascade failed: %s", _re)
        conn.commit()
        conn.close()
        if hb:
            await _enqueue_remote_push(hb, restored_ids, "upsert")
        return {"success": True, "session_id": session_id, "restored": restored, "children_restored": children_restored}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class SessionPatchRequest(BaseModel):
    """Body for PATCH /sessions/{id} — rename, pin, hide, or set the Remote
    Control executor."""
    title: Optional[str] = None
    pinned: Optional[bool] = None
    hidden: Optional[bool] = None
    # Remote Control (session roaming): which device runs this session's turns.
    # Persisted in session metadata so EVERY device that opens the session routes
    # its sends to the same executor (not a per-browser local choice). An empty
    # string clears it = run locally. ``None`` (field omitted) leaves it unchanged.
    remote_executor_instance: Optional[str] = None
    remote_executor_label: Optional[str] = None


class SessionReorderRequest(BaseModel):
    """Body for POST /sessions/reorder — persist manual drag order."""
    user_id: str
    order: list[str]  # session ids, top-to-bottom (index 0 = top of the list)


@router.patch("/sessions/{session_id}")
async def patch_session(
    session_id: str,
    req: SessionPatchRequest,
    request: Request,
    db: str = Query("user.db", description="Database filename"),
):
    """Update a session's title, pinned, hidden, and/or Remote Control executor."""
    if (req.title is None and req.pinned is None and req.hidden is None
            and req.remote_executor_instance is None):
        raise HTTPException(status_code=400,
                            detail="Provide title, pinned, hidden, and/or remote_executor_instance")

    db_path = _get_db_path(db)
    hb = _hybrid_backend_for(db)
    try:
        # Local-first: when hybrid is on, apply the rename/pin/hide to the LOCAL
        # mirror (instant, and consistent with the panel's now-local list read),
        # then queue the authoritative push below — no remote round-trip in the
        # request. Any session the user can see to rename came FROM the local list,
        # so it's present in the mirror and the UPDATE matches.
        conn, _dialect = (_open_local_sqlite(db), "sqlite") if hb else _open(db)
        cur = conn.cursor()

        # Ownership gate (open/local mode + admins pass; non-owner refused).
        if _session_access_ok(cur, session_id, request, None) is False:
            conn.close()
            raise HTTPException(status_code=403, detail="Not authorized for this session")

        sets = []
        params: list[object] = []
        # Any field that lives in the metadata JSON (auto_title_locked, the Remote
        # Control executor) merges into a SINGLE read-modify-write so two of them in
        # one request can't each emit their own `metadata = ?` and clobber the other.
        _meta_dirty = False
        _meta: dict = {}
        if req.title is not None or req.remote_executor_instance is not None:
            try:
                cur.execute("SELECT metadata FROM sessions WHERE id = ?", (session_id,))
                _mrow = cur.fetchone()
                _meta = json.loads(_mrow[0]) if (_mrow and _mrow[0]) else {}
                if not isinstance(_meta, dict):
                    _meta = {}
            except Exception:
                _meta = {}
        if req.title is not None:
            sets.append("title = ?")
            params.append(req.title)
            # A manual rename wins over the auto-namer: lock it so the background
            # Session Namer app function (plugins/app_functions/session_titler/)
            # stops overwriting it.
            _meta["auto_title_locked"] = True
            _meta_dirty = True
        if req.remote_executor_instance is not None:
            # Remote Control: stamp (or clear) which device runs this session. An
            # empty instance means "run locally" — drop the key so a cleared
            # session is indistinguishable from one that never had an executor.
            inst = (req.remote_executor_instance or "").strip()
            if inst:
                _meta["remote_executor"] = {
                    "instance_id": inst,
                    "label": (req.remote_executor_label or "").strip() or inst,
                }
            else:
                _meta.pop("remote_executor", None)
            _meta_dirty = True
        if _meta_dirty:
            sets.append("metadata = ?")
            params.append(json.dumps(_meta))
        if req.pinned is not None:
            # Pin order belongs to the owner and is persisted independently from
            # activity. A newly pinned session is appended; drag-and-drop later
            # writes the user's complete 0..N order through /sessions/reorder.
            cur.execute("PRAGMA table_info(sessions)")
            cols = {row[1] for row in cur.fetchall()}
            if "pinned" not in cols:
                cur.execute("ALTER TABLE sessions ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
            if "sort_order" not in cols:
                cur.execute("ALTER TABLE sessions ADD COLUMN sort_order INTEGER")
            cur.execute("SELECT user_id FROM sessions WHERE id = ?", (session_id,))
            owner_row = cur.fetchone()
            owner_id = owner_row[0] if owner_row else None
            sets.append("pinned = ?")
            params.append(1 if req.pinned else 0)
            if req.pinned and owner_id:
                cur.execute(
                    "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM sessions "
                    "WHERE user_id = ? AND pinned = 1 AND id != ?",
                    (owner_id, session_id),
                )
                sets.append("sort_order = ?")
                params.append(cur.fetchone()[0])
            elif not req.pinned:
                sets.append("sort_order = NULL")
        if req.hidden is not None:
            # Confirm column exists before writing
            cur.execute("PRAGMA table_info(sessions)")
            cols = {row[1] for row in cur.fetchall()}
            if "hidden" not in cols:
                cur.execute("ALTER TABLE sessions ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0")
            sets.append("hidden = ?")
            params.append(1 if req.hidden else 0)
        sets.append("updated_at = CURRENT_TIMESTAMP")

        params.append(session_id)
        cur.execute(f"UPDATE sessions SET {', '.join(sets)} WHERE id = ?", params)
        affected = cur.rowcount
        conn.commit()
        conn.close()

        if affected == 0:
            raise HTTPException(status_code=404, detail="Session not found")
        # Queue the authoritative remote push (a rename/pin/hide is an upsert of
        # the row; the sync engine re-reads the current local row and pushes it).
        if hb:
            await _enqueue_remote_push(hb, [session_id], "upsert")
        return {"success": True, "session_id": session_id, "affected": affected}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/sessions/{session_id}/auto-title")
async def auto_title_session(
    session_id: str,
    request: Request,
    db: str = Query("user.db", description="Database filename"),
):
    """On-demand "Auto rename": re-title a session via the Session Namer app
    function, forcing past any lock or special prefix.

    The background namer only names the first few turns, skips optimizer-/
    closer-/slash- sessions, and stops once a name is locked (manual rename or
    the 3-turn lock). This action calls the same LLM titler with ``force=True``
    and a larger message sample so ANY session gets a fresh name, and re-locks
    the result. It emits the same ``session_title`` WS events as a normal turn,
    so the header spinner + live rename work identically. When the Session
    Namer app function is disabled (App Settings ▸ App Functions), it returns
    ``status: "disabled"`` with the current title untouched.
    """
    # App Functions gate — the Session Namer is an app function (not an agent
    # ability); its on/off lives in App Settings ▸ App Functions. Fail open on
    # a read error, matching the turn-hook dispatch.
    try:
        from app.abilities import app_function_enabled
        if not app_function_enabled("session_titler"):
            return {"status": "disabled", "title": None,
                    "message": "Session Namer is turned off (App Settings ▸ App Functions)"}
    except Exception:
        pass

    db_path = _get_db_path(db)
    hb = _hybrid_backend_for(db)
    conn, _dialect = (_open_local_sqlite(db), "sqlite") if hb else _open(db)
    try:
        cur = conn.cursor()
        # Ownership gate (open/local mode + admins pass; non-owner refused).
        if _session_access_ok(cur, session_id, request, None) is False:
            raise HTTPException(status_code=403, detail="Not authorized for this session")
        row = cur.execute(
            "SELECT user_id, title, agent_id FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Session not found")
    owner_id, current_title, agent_id = row[0], row[1], row[2]

    # The titler's internal fetch asserts ownership — use the session's OWNER id
    # (the requester already passed the access gate above), so open-mode / admin
    # requests don't trip the strict owner check.
    from app.db import get_db as _get_app_db
    app_db = _get_app_db()

    # Best-effort WS emit to the REQUESTER (who clicked Auto rename) so the
    # header shows the titling spinner and swaps the name live.
    _token = ""
    _auth_header = request.headers.get("Authorization", "")
    if _auth_header.startswith("Bearer "):
        _token = _auth_header[7:]
    if not _token:
        _token = request.query_params.get("token", "")
    _payload = decode_token(_token) if _token else None
    requester_id = _payload.get("user_id") if _payload else None

    from app.events.delivery import _emit_to_user_listeners

    async def _emit(ev: dict) -> None:
        if not requester_id:
            return
        try:
            await _emit_to_user_listeners(requester_id, ev)
        except Exception:
            pass

    from plugins.app_functions.session_titler.session_titler import (
        _maybe_title_session, _model,
    )

    # The titler resolves its model from env vars (LLM_MODEL / CLASSIFIER_MODEL),
    # which the run loop populates via apply_provider_for_run(apply_env=True).
    # This on-demand endpoint fires OUTSIDE a chat run, so if no turn has run
    # since boot the env is empty and the LLM call silently no-ops (title
    # unchanged, status "ok"). Resolve + apply the session's AGENT provider
    # config explicitly, and prefer the roster row flagged "System" (the model
    # the agent admin marked for app misc. LLM tasks — session naming, context
    # mgmt) over the agent's plain chat default.
    import os as _os
    try:
        from app.admin.settings import apply_provider_for_run
        agent_rec = None
        if agent_id:
            agent_rec = await app_db.get_agent_by_id(agent_id)
        effective = await apply_provider_for_run(
            owner_id, agent_rec, session_id, apply_env=True
        )
        # Find the agent's "System" model in the effective roster (inherited or
        # own). Fall back to the effective default model if no System row exists.
        sys_row = None
        for _p in (effective.get("multi_providers") or []):
            if isinstance(_p, dict) and _p.get("use_for_system") and _p.get("model"):
                sys_row = _p
                break
        if sys_row:
            _m = sys_row.get("model", "")
            _b = sys_row.get("base_url", "") or effective.get("base_url", "")
            _k = sys_row.get("api_key", "") or effective.get("api_key", "")
            if _m and _b:
                _os.environ["CLASSIFIER_MODEL"] = _m
                _os.environ["LLM_MODEL"] = _m
                _os.environ["OPENROUTER_MODEL"] = _m
                _os.environ["LLM_BASE_URL"] = _b
                _os.environ["OPENROUTER_BASE_URL"] = _b
                if _k:
                    _os.environ["LLM_API_KEY"] = _k
                    _os.environ["OPENROUTER_API_KEY"] = _k
    except Exception as _pe:
        logger.warning("auto_title_session: provider resolution failed: %s", _pe)

    _diag_model = _model()
    logger.info("auto_title_session: session=%s model=%s force=True",
                session_id, _diag_model or "<none>")
    await _maybe_title_session(
        app_db, owner_id, session_id, emit=_emit, force=True, sample_limit=30
    )

    # Re-read the stored title (the titler updated it in place; the done event
    # also carried it) so the response is authoritative even if emit was a no-op.
    conn2, _dialect2 = (_open_local_sqlite(db), "sqlite") if hb else _open(db)
    try:
        trow = conn2.execute(
            "SELECT title FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
    finally:
        conn2.close()
    new_title = trow[0] if trow else current_title
    return {"status": "ok", "session_id": session_id, "title": new_title}


@router.post("/sessions/reorder")
async def reorder_sessions(
    req: SessionReorderRequest,
    request: Request,
    db: str = Query("user.db", description="Database filename"),
):
    """Persist the manual drag order of the requesting user's sessions.

    Each distinct pinned id in ``order`` gets sort_order = its index (0 = top).
    Writes are scoped to sessions owned by ``user_id`` so a user can't reorder
    another user's rows. Unpinned rows are deliberately unaffected because they
    are always ordered by recent activity.
    """
    # Identity gate: the claimed user_id must be the authenticated caller (or an
    # admin); open/local mode is full-trust. Without this a caller could reorder
    # another user's sessions by passing their user_id + session ids.
    if not _is_open_access_mode():
        from app.auth.identity import assert_caller_is
        await assert_caller_is(request, req.user_id)
    # Preserve first occurrence and discard empty/duplicate ids. Apart from
    # producing ambiguous positions, duplicates used to make the response's
    # updated count look successful even though fewer distinct rows were saved.
    ordered_ids = list(dict.fromkeys(sid for sid in req.order if sid))
    if not ordered_ids:
        return {"success": True, "updated": 0}
    db_path = _get_db_path(db)
    hb = _hybrid_backend_for(db)
    try:
        def _write(conn, *, enqueue: bool) -> tuple[int, list[str]]:
            cur = conn.cursor()
            cur.execute("PRAGMA table_info(sessions)")
            cols = {row[1] for row in cur.fetchall()}
            if "sort_order" not in cols:
                cur.execute("ALTER TABLE sessions ADD COLUMN sort_order INTEGER")
            if "pinned" not in cols:
                cur.execute("ALTER TABLE sessions ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")

            # One fixed-width, timezone-qualified watermark for the whole reorder.
            # This avoids second-resolution LWW ties in hybrid pull/push races.
            changed_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
            updated = 0
            pushed_ids: list[str] = []
            for position, sid in enumerate(ordered_ids):
                cur.execute(
                    "UPDATE sessions SET sort_order = ?, updated_at = ? "
                    "WHERE id = ? AND user_id = ? AND pinned = 1",
                    (position, changed_at, sid, req.user_id),
                )
                if cur.rowcount:
                    pushed_ids.append(sid)
                    updated += cur.rowcount

            if enqueue and pushed_ids:
                # The row mutations and their durable outbox markers MUST commit
                # together. Previously _enqueue_remote_push ran afterward in a
                # second transaction, leaving a crash window where local order
                # changed but could never reach the remote authority.
                cur.execute(
                    "CREATE TABLE IF NOT EXISTS hybrid_outbox ("
                    "seq INTEGER PRIMARY KEY AUTOINCREMENT, table_name TEXT NOT NULL, "
                    "row_id TEXT NOT NULL, op TEXT NOT NULL DEFAULT 'upsert', "
                    "created_at TEXT NOT NULL)"
                )
                cur.executemany(
                    "INSERT INTO hybrid_outbox "
                    "(table_name, row_id, op, created_at) VALUES (?, ?, ?, ?)",
                    [("sessions", sid, "upsert", changed_at) for sid in pushed_ids],
                )
            conn.commit()
            return updated, pushed_ids

        if hb:
            # Serialize against the hybrid pusher/puller and commit data + outbox
            # atomically on the exact local backend connection they share.
            async with hb.local._write_lock:
                conn = hb.local._get_conn()
                try:
                    updated, pushed_ids = _write(conn, enqueue=True)
                except Exception:
                    conn.rollback()
                    raise
                finally:
                    conn.close()
        else:
            conn, _dialect = _open(db)
            try:
                updated, pushed_ids = _write(conn, enqueue=False)
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise
            finally:
                conn.close()
        return {
            "success": updated == len(ordered_ids),
            "updated": updated,
            "requested": len(ordered_ids),
            "updated_ids": pushed_ids,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class SessionNotificationRequest(BaseModel):
    """Body for POST /session-notifications — persist (or dismiss) one
    session-completion toast for the caller."""
    session_id: str
    title: str = ""
    dismissed: bool = False


# ── Session-completion notifications (chat-panel sliding toast) ──────────────
# Persisted in the caller's per-user database (data/user_data/{user_id}/{user_id}.db
# in split mode, attached as `_user`) so an undismissed toast survives refresh AND
# shows on every device. Dismissal is a soft `dismissed` flag so the upsert-only
# hybrid puller can propagate it cross-device; old dismissed rows are pruned on read.

_SESSION_NOTIFICATIONS_DDL = (
    "CREATE TABLE IF NOT EXISTS session_notifications ("
    "session_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, "
    "title TEXT NOT NULL DEFAULT '', dismissed INTEGER NOT NULL DEFAULT 0, "
    "dismissed_at TEXT, "
    "created_at TEXT NOT NULL DEFAULT (datetime('now')), "
    "updated_at TEXT NOT NULL DEFAULT (datetime('now')))"
)

_SESSION_NOTIFICATIONS_UPSERT = (
    "INSERT INTO session_notifications (session_id, user_id, title, dismissed, dismissed_at, updated_at) "
    "VALUES (?, ?, ?, ?, NULL, datetime('now')) "
    "ON CONFLICT(session_id) DO UPDATE SET "
    "title = excluded.title, "
    "dismissed = excluded.dismissed, "
    "dismissed_at = CASE WHEN excluded.dismissed = 1 THEN datetime('now') ELSE NULL END, "
    "updated_at = datetime('now') "
    "WHERE session_notifications.user_id = excluded.user_id"
)


def _ensure_session_notifications(conn, dialect: str) -> None:
    """Ensure the table in the caller's already-scoped user authority."""
    if dialect != "sqlite":
        return
    conn.execute(_SESSION_NOTIFICATIONS_DDL)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_session_notifications_user "
        "ON session_notifications(user_id)"
    )

def _caller_user_id(request: Request) -> str:
    """Resolve the authenticated caller's user_id for the notification endpoints."""
    from app.auth.identity import request_user_id
    return request_user_id(request)


@router.get("/session-notifications")
async def list_session_notifications(
    request: Request,
    db: str = Query("user.db", description="Database filename"),
):
    """List the caller's undismissed session-completion notifications.

    Served from the caller's per-user database (attached to the main connection
    in split mode), so a toast dismissed on one device is gone on the next poll
    on every device. Also prunes dismissed rows older than 7 days."""
    user_id = _caller_user_id(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    conn, _dialect = _open_read(db)
    try:
        _ensure_session_notifications(conn, _dialect)
        if _dialect == "sqlite":
            try:
                conn.execute(
                    "DELETE FROM session_notifications "
                    "WHERE dismissed = 1 AND dismissed_at IS NOT NULL "
                    "AND dismissed_at < datetime('now', '-7 days')"
                )
                conn.commit()
            except Exception:
                pass
        # A visit is persisted in sessions.read_at. Keep notification rows
        # durable (rather than bulk-dismissing them) but hide any created at or
        # before that visit, so every navigation path and every device agrees.
        session_cols = {row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
        if "read_at" in session_cols:
            rows = conn.execute(
                "SELECT n.session_id, n.title, n.created_at, n.updated_at "
                "FROM session_notifications n "
                "JOIN sessions s ON s.id = n.session_id AND s.user_id = n.user_id "
                "WHERE n.user_id = ? AND n.dismissed = 0 "
                "AND (s.read_at IS NULL OR julianday(n.created_at) > julianday(s.read_at)) "
                "ORDER BY n.created_at ASC",
                (user_id,),
            ).fetchall()
        else:
            # Compatibility for a pre-read_at database that has not migrated yet.
            rows = conn.execute(
                "SELECT n.session_id, n.title, n.created_at, n.updated_at "
                "FROM session_notifications n "
                "JOIN sessions s ON s.id = n.session_id AND s.user_id = n.user_id "
                "WHERE n.user_id = ? AND n.dismissed = 0 "
                "ORDER BY n.created_at ASC",
                (user_id,),
            ).fetchall()
        return {"notifications": [dict(r) for r in rows]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()


@router.post("/session-notifications")
async def upsert_session_notification(
    req: SessionNotificationRequest,
    request: Request,
    db: str = Query("user.db", description="Database filename"),
):
    """Persist one session-completion toast for the caller.

    ``dismissed=false`` marks the session as needing a toast (shown on every
    device until dismissed); ``dismissed=true`` soft-dismisses it. Local-first:
    written to the per-user SQLite DB immediately and queued for the hybrid
    sync engine when a remote authority is configured, so other devices pick
    the change up on their next pull."""
    user_id = _caller_user_id(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    sid = (req.session_id or "").strip()
    if not sid:
        raise HTTPException(status_code=400, detail="session_id is required")
    title = (req.title or "")[:500]
    dismissed = 1 if req.dismissed else 0
    hb = _hybrid_backend_for(db)
    try:
        def _write(conn, *, enqueue: bool) -> None:
            _ensure_session_notifications(conn, "sqlite")
            conn.execute(
                _SESSION_NOTIFICATIONS_UPSERT,
                (sid, user_id, title, dismissed),
            )
            if enqueue:
                # Row mutation + durable outbox marker commit together so a crash
                # can't leave a local toast that never reaches other devices.
                cur = conn.cursor()
                cur.execute(
                    "CREATE TABLE IF NOT EXISTS hybrid_outbox ("
                    "seq INTEGER PRIMARY KEY AUTOINCREMENT, table_name TEXT NOT NULL, "
                    "row_id TEXT NOT NULL, op TEXT NOT NULL DEFAULT 'upsert', "
                    "created_at TEXT NOT NULL)"
                )
                cur.execute(
                    "INSERT INTO hybrid_outbox "
                    "(table_name, row_id, op, created_at) VALUES (?, ?, ?, ?)",
                    ("session_notifications", sid, "upsert",
                     datetime.now(timezone.utc).isoformat(timespec="microseconds")),
                )
            conn.commit()

        if hb:
            async with hb.local._write_lock:
                conn = hb.local._get_conn()
                try:
                    _write(conn, enqueue=True)
                except Exception:
                    conn.rollback()
                    raise
                finally:
                    conn.close()
        else:
            conn, _dialect = _open(db)
            try:
                _write(conn, enqueue=False)
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise
            finally:
                conn.close()
        return {"success": True, "session_id": sid}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/sessions")
async def list_sessions(
    request: Request,
    user_id: str = Query(..., description="User ID"),
    db: str = Query("user.db", description="Database filename"),
    agent_id: Optional[str] = Query(None, description="Filter to sessions bound to this agent"),
    limit: int = Query(20, ge=0, le=200, description="Max sessions to return (0 = no limit, all matching sessions)"),
    include_hidden: bool = Query(False, description="Include sessions flagged hidden"),
    q: Optional[str] = Query(None, description="Search term: sessions whose title contains it (case-insensitive)"),
    include_recycled: bool = Query(False, description="Include sessions in the recycling bin (flagged recycled:true)"),
    include_manifest: bool = Query(True, description="Compute per-session manifest fields (disable for lightweight UI lists that never read them)"),
):
    """Run the legacy session-list implementation outside the serving loop.

    In addition to dirty-manifest hashing, the query computes correlated
    interaction activity/counts and inspects several SQLite tables. Background
    tab polling can therefore monopolize the event loop even with manifests
    disabled. A worker-owned event loop preserves the implementation's async
    agent enrichment while isolating all of its synchronous DB/CPU work.
    """
    def _run():
        return asyncio.run(_list_sessions_impl(
            request=request,
            user_id=user_id,
            db=db,
            agent_id=agent_id,
            limit=limit,
            include_hidden=include_hidden,
            q=q,
            include_recycled=include_recycled,
            include_manifest=include_manifest,
        ))

    return await asyncio.to_thread(_run)


#: Metadata marker the ``closing_message`` gate stamps on a run's promoted final
#: assistant row (app/agent/closing_message.py, META_KEY). Used as a LIKE filter
#: so the batch probe below never deserializes unrelated metadata blobs.
_CLOSING_META_MARKER = '%"closing_message"%'


def _fetch_closing_flags(conn, session_ids):
    """Latest promoted closing-message flag per session — one bounded batch.

    The ``closing_message`` gate stamps ``metadata.closing_message`` on the run's
    promoted final assistant row, so the closer's OWN verdict (``needs_input`` /
    ``partial`` / ``unverified`` — states ``session_runs.status`` has no word for)
    is durable exactly where the transcript already lives. The session list
    surfaces it here so the status chip and the notification pill can prefer the
    closer's flag over the coarser runtime status, which stays the fallback when
    the gate is off or a run predates it.

    Ascending ``session_seq`` means the last write for a session wins, i.e. its
    most recent run. A session with no closing record is simply absent from the
    returned map, and the caller falls back to ``run_status``. Best-effort: an
    unreadable interactions table leaves the map empty rather than failing the
    list.
    """
    out: dict = {}
    ids = [sid for sid in session_ids if sid]
    if not ids:
        return out
    try:
        placeholders = ",".join("?" * len(ids))
        rows = conn.execute(
            f"""SELECT session_id, metadata FROM interactions
                 WHERE session_id IN ({placeholders})
                   AND role = 'assistant'
                   AND (status IS NULL OR status != 'deleted')
                   AND metadata LIKE ?
                 ORDER BY session_seq ASC""",
            (*ids, _CLOSING_META_MARKER),
        ).fetchall()
    except Exception:
        return out
    for r in rows:
        try:
            meta = json.loads(r["metadata"]) if r["metadata"] else {}
        except (json.JSONDecodeError, TypeError):
            continue
        record = meta.get("closing_message") if isinstance(meta, dict) else None
        if not isinstance(record, dict):
            continue
        flag = str(record.get("flag") or "").strip().lower()
        cta = str(record.get("cta") or "").strip()
        if not flag and not cta:
            continue
        out[r["session_id"]] = {"flag": flag, "cta": cta}
    return out


async def _list_sessions_impl(
    request: Request,
    user_id: str,
    db: str,
    agent_id: Optional[str],
    limit: int,
    include_hidden: bool,
    q: Optional[str],
    include_recycled: bool,
    include_manifest: bool,
):
    """List sessions for a user (owner or participant).

    When ``agent_id`` is supplied, only sessions bound to that agent are
    returned. Sessions with a NULL ``agent_id`` (never bound to an agent)
    are filtered out in that case — they appear as orphans that don't
    belong to any specific agent.

    Sessions are returned strictly by their most recent activity, newest first.
    ``total_count`` reports the total number of matching sessions.

    ``limit=0`` returns every matching session (no cap) — the chat-header
    dropdown uses it so its list matches the Sessions page, which reads the
    same data from /session-stats without any limit.

    ``q`` filters to sessions whose title contains the term (case-insensitive)
    and, when set, lifts the row cap so ALL matches are returned (the dropdown
    search must find sessions past the normal 50-row ceiling). ``include_recycled``
    brings recycling-bin rows back into the result, each flagged
    ``recycled: true``; without it (and without ``q``) binned sessions stay
    hidden, exactly as before.
    """
    # Resolve requester identities from token
    _token = ""
    _auth_header = request.headers.get("Authorization", "")
    if _auth_header.startswith("Bearer "):
        _token = _auth_header[7:]
    if not _token:
        _token = request.query_params.get("token", "")
    _payload = decode_token(_token) if _token else None
    requesting_user_id = _payload.get("user_id") if _payload else None
    requesting_username = _payload.get("sub") if _payload else None
    # The SQL below only returns rows whose owner/participant id is in this set.
    # A claimed `user_id` query param is honoured as a fallback identity ONLY
    # when there's no valid token (unauthenticated local UUID users) or in open
    # mode — so a token-bearing caller can't pass `?user_id=<someone else>` to
    # read another user's session list.
    requester_identities = {v for v in (requesting_user_id, requesting_username) if v}
    if (not _payload or _is_open_access_mode()) and user_id:
        requester_identities.add(user_id)
    # Split storage resolves user.db from an explicit request-scoped identity.
    # The worker owns a copied ContextVar context, so bind the identity already
    # validated above before either the path check or connection opener runs.
    if _payload and not _is_open_access_mode():
        db_user_id = requesting_user_id or requesting_username
    else:
        db_user_id = user_id
    from app.db.local import set_db_user_context
    set_db_user_context(db_user_id or "")


    db_path = _get_db_path(db)
    try:
        # Chat-panel session list → served from the local mirror when hybrid is on
        # (the puller keeps `sessions` current within ~5s; the user's own edits are
        # instant). The per-user WHERE filter below still scopes the rows.
        conn, _dialect = _open_read(db)
        _ensure_session_read_tracking_columns(conn, _dialect)
        # Only a plain stdlib sqlite3 connection needs its row_factory set here.
        # db_crypto's SQLCipher connections already carry a dict-capable Row (and
        # MUST NOT be reassigned — stdlib sqlite3.Row rejects a cipher cursor), and
        # PgPortableConnection ignores it. Guarding avoids breaking reads off an
        # encrypted local mirror now that this endpoint reads local.
        if isinstance(conn, sqlite3.Connection):
            conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        sessions = []
        total_count = 0
        try:
            # Detect optional columns (older DBs may not have them yet).
            sess_cols = _session_columns(conn, _dialect)
            has_pinned = "pinned" in sess_cols
            has_sort_order = "sort_order" in sess_cols
            has_read_at = "read_at" in sess_cols
            has_unread_at = "unread_at" in sess_cols
            has_status = "status" in sess_cols
            has_hidden = "hidden" in sess_cols
            # session_runs was introduced after some local user databases. Its
            # absence must not make the session picker query fail.
            has_session_runs = False
            try:
                cur.execute("SELECT 1 FROM session_runs LIMIT 1")
                has_session_runs = True
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass

            # A terminal run displays its completion time in the picker, so it
            # must also count as activity for server ordering. Otherwise a row
            # shown as “10m ago” can appear below one shown as “12m ago” because
            # the old expression considered only interaction timestamps.
            # Session.updated_at remains a fallback for truly empty sessions:
            # pin/title edits must not reorder normal rows.
            interaction_activity_expr = (
                "COALESCE((SELECT MAX(i.created_at) FROM interactions i "
                "WHERE i.session_id = s.id), s.updated_at, s.created_at)"
            )
            if has_session_runs:
                activity_expr = (
                    "COALESCE((SELECT MAX(sr.updated_at) FROM session_runs sr "
                    "WHERE sr.session_id = s.id AND sr.status IN ('complete', 'interrupted', 'error')), "
                    f"{interaction_activity_expr})"
                )
            else:
                activity_expr = interaction_activity_expr
            # Count user turns rather than every persisted row: assistant/tool
            # fan-out would otherwise make tool-heavy sessions look artificially
            # busy. The picker uses this with bucketed recency for a stable,
            # engagement-aware unpinned order.
            activity_count_expr = (
                "(SELECT COUNT(*) FROM interactions ai "
                "WHERE ai.session_id = s.id AND ai.role = 'user')"
            )
            select_cols = (
                's.id, s.title, s.created_at, s.user_id, s.participants, '
                f's.agent_id, s.metadata, {activity_expr} AS activity_at, '
                f'{activity_count_expr} AS activity_count'
            )
            if has_pinned:
                select_cols += ', s.pinned'
            if has_sort_order:
                select_cols += ', s.sort_order'
            if has_read_at:
                select_cols += ', s.read_at'
            if has_unread_at:
                select_cols += ', s.unread_at'
            if has_hidden:
                select_cols += ', s.hidden'
            if has_status:
                select_cols += ', s.status'
            # Agent authority lives in per-agent databases now. Never join the
            # user-plane session query to a retired/co-located `agents` table;
            # display metadata is enriched from the agent plane below.
            where_clause = '1=1'
            params: list = []
            if agent_id:
                where_clause = 's.agent_id = ?'
                params.append(agent_id)

            # Title search (dropdown search bar): case-insensitive contains,
            # escaped so literal % _ \ in the term can't widen the match.
            _search = (q or "").strip()
            if _search:
                _escaped = _search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                where_clause = f"({where_clause}) AND (s.title LIKE ? ESCAPE '\\')"
                params.append(f"%{_escaped}%")

            # Recycling bin: hidden from the chat header by default (a NULL
            # status counts as live — Postgres adds the column without a
            # default, so pre-existing rows read back as NULL). include_recycled
            # brings bin rows back (each flagged recycled:true); an active
            # search also surfaces them so a name search can find binned
            # sessions, matching the Sessions page cross-catalog search.
            if has_status and not include_recycled and not _search:
                where_clause = f"({where_clause}) AND (s.status IS NULL OR s.status != 'recycled')"

            # Hidden sessions: the chat-header "manage list" eye-toggle declutters
            # the dropdown without deleting. Excluded by default (NULL = visible,
            # same convention as status); revealed when include_hidden is set.
            if has_hidden and not include_hidden:
                where_clause = f"({where_clause}) AND (s.hidden IS NULL OR s.hidden = 0)"

            # Hide spawned-clone sessions from the sidebar: they belong to
            # ephemeral clone agents and live/die with their orchestrator (see
            # cascade_delete_clones). Their ids are always prefixed 'spawn-'.
            # Optimizer Planner ('optimizer-*') and Closer ('closer-*') sessions
            # are ordinary TOP-LEVEL sessions of their own — the optimizer
            # family is no longer nested under the base session — so they show
            # up here as regular rows like any other session.
            where_clause = (
                f"({where_clause}) AND (s.id NOT LIKE 'spawn-%')"
            )

            # Live pinned sessions stay at the top in the order the user set by
            # drag-and-drop. Every unpinned (and recycled) session remains newest
            # first. Null orders are legacy pins awaiting their first manual drag.
            if has_pinned:
                _live_pin = (
                    "COALESCE(s.pinned, 0) = 1"
                    + (" AND (s.status IS NULL OR s.status != 'recycled')" if has_status else "")
                )
                _manual_order = "s.sort_order" if has_sort_order else "NULL"
                activity_order_clause = (
                    f"CASE WHEN {_live_pin} THEN 0 ELSE 1 END ASC, "
                    f"CASE WHEN {_live_pin} AND {_manual_order} IS NULL THEN 1 ELSE 0 END ASC, "
                    f"CASE WHEN {_live_pin} THEN COALESCE({_manual_order}, 0) ELSE 0 END ASC, "
                    f"{activity_expr} DESC NULLS LAST, s.id ASC"
                )
            else:
                activity_order_clause = f'{activity_expr} DESC NULLS LAST, s.id ASC'

            # Pre-fetch run statuses for all sessions in one query. stop_cause
            # lets a stopped-but-still-recoverable run read as 'resuming'
            # instead of a finished status (see _resume_pending).
            run_statuses = {}
            try:
                cur2 = conn.cursor()
                cur2.execute(
                    "SELECT session_id, status, updated_at, stop_cause, "
                    "resume_attempts, max_resume_attempts FROM session_runs"
                )
                for r in cur2.fetchall():
                    run_statuses[r[0]] = {
                        "status": r[1], "updated_at": r[2], "stop_cause": r[3],
                        "resume_attempts": r[4], "max_resume_attempts": r[5],
                    }
            except Exception:
                pass
            # Pre-fetch child counts so a parent row can show an expand caret.
            # A session has children only if it is an orchestrator (spawned
            # helpers in agent_spawns). Optimizer Planner/Closer sessions are
            # top-level sessions of their own, so they contribute no child
            # count to the base session.
            child_counts: dict = {}
            try:
                for r in cur2.execute(
                    "SELECT orchestrator_session_id AS p, COUNT(*) AS n FROM agent_spawns "
                    "GROUP BY orchestrator_session_id"
                ).fetchall():
                    if r[0]:
                        child_counts[r[0]] = child_counts.get(r[0], 0) + int(r[1] or 0)
            except Exception:
                pass

            # Count and fetch one globally activity-ordered result set. Search
            # mode and limit=0 return every match, while a positive limit caps
            # the newest results only.
            count_sql = f'SELECT COUNT(*) FROM sessions s WHERE {where_clause}'
            cur.execute(count_sql, params)
            total_count = cur.fetchone()[0]

            session_limit = None if (_search or limit <= 0) else max(0, limit)
            sessions_sql = (
                f'SELECT {select_cols} '
                f'FROM sessions s '
                f'WHERE {where_clause} '
                f'ORDER BY {activity_order_clause} '
                + ('' if session_limit is None else 'LIMIT ?')
            )
            cur.execute(sessions_sql, params if session_limit is None else params + [session_limit])
            all_rows = cur.fetchall()

            # The closer's own flag for this page of sessions — see
            # _fetch_closing_flags. One query for the whole page; absent sessions
            # fall back to run_status.
            closing_by_sid = _fetch_closing_flags(
                conn, [row["id"] for row in all_rows])

            # Resolve each referenced agent once from its own authority plane.
            # Keeping this outside the row loop avoids an N+1 lookup when many
            # sessions share the same agent.
            agent_display: dict[str, dict] = {}
            agent_ids = {row["agent_id"] for row in all_rows if row["agent_id"]}
            if agent_ids:
                try:
                    from app.db import get_db
                    authority = get_db()
                    for aid in agent_ids:
                        try:
                            agent = await authority.get_agent_by_id(aid)
                        except Exception:
                            agent = None
                        if not agent:
                            continue
                        metadata = agent.get("metadata") or {}
                        if isinstance(metadata, str):
                            try:
                                metadata = json.loads(metadata)
                            except (json.JSONDecodeError, TypeError):
                                metadata = {}
                        agent_display[aid] = {
                            "name": agent.get("name") or "",
                            "icon": agent.get("icon") or metadata.get("icon") or "",
                            "engine": metadata.get("engine") or agent.get("engine") or "",
                        }
                except Exception as exc:
                    logger.debug("session list agent-plane enrichment failed: %s", exc)

            for row in all_rows:
                owner_id = row[3]
                participants_raw = row[4] or "[]"
                try:
                    participants = json.loads(participants_raw)
                except (json.JSONDecodeError, TypeError):
                    participants = []
                participant_ids = {p.get("id") for p in participants if isinstance(p, dict)}
                all_ids = ({owner_id} | participant_ids) - {None}
                if requester_identities & all_ids:
                    sid = row["id"]
                    # A dirty manifest can require hashing an entire long
                    # transcript. Keep that synchronous SQLite/CPU work off the
                    # serving loop; the helper owns its worker-thread connection.
                    manifest = (
                        await _compute_session_manifest_offloop(db, sid)
                        if include_manifest else {}
                    )
                    display = agent_display.get(row["agent_id"], {})
                    pinned_val = bool(row["pinned"]) if has_pinned else False
                    read_at = row["read_at"] if has_read_at else None
                    unread_at = row["unread_at"] if has_unread_at else None
                    # In-process session-gate queue state takes precedence: a
                    # session waiting in the FIFO queue is "queued" even when a
                    # stale terminal session_runs row exists from its last turn.
                    queue_position = None
                    queue_total = None
                    try:
                        from app.agent.session_gate import queue_info as _gate_qi
                        _qi = _gate_qi(sid)
                        if _qi:
                            queue_position = _qi.get("position")
                            queue_total = _qi.get("total")
                    except Exception:
                        pass
                    if queue_position is not None:
                        run_status = "queued"
                        run_updated_at = None  # no completed time while queued
                    else:
                        run = run_statuses.get(sid)
                        run_status = run["status"] if run else None
                        run_updated_at = run["updated_at"] if run else None
                        # Stopped involuntarily but still inside the recovery
                        # ladder → in-progress, not finished. Keeps the dropdown
                        # and Sessions page spinners lit across the revival gap.
                        if run and _resume_pending(run):
                            run_status = "resuming"
                    # A session is unread when a terminal session has newer message
                    # activity than its last visit. Use timestamp normalization because
                    # SQLite legacy values and ISO read_at use different text shapes.
                    has_unread = bool(unread_at)
                    latest_activity_at = row["activity_at"] or run_updated_at
                    if not has_unread and run_status in ("complete", "interrupted", "error") and latest_activity_at:
                        if not read_at or _quota_sort_time(latest_activity_at) > _quota_sort_time(read_at):
                            has_unread = True
                    # Headless engine session id (Codex thread / Claude session) —
                    # the id to recall inside the CLI (`codex exec resume <id>` /
                    # `claude --resume <id>`). Surfaced for the dropdown's
                    # Codex:/Claude: row; None for plain WebAgent sessions.
                    # NOTE: direct row["metadata"] indexing — sqlite3.Row has no .get().
                    _engine_thread_id = None
                    try:
                        _smeta = json.loads(row["metadata"]) if row["metadata"] else {}
                        if isinstance(_smeta, dict):
                            _engine_thread_id = (_smeta.get("codex_thread_id")
                                                 or _smeta.get("claude_session_id") or None)
                    except (json.JSONDecodeError, TypeError):
                        _engine_thread_id = None
                    sessions.append({
                        "id": sid,
                        "title": row["title"] or sid[:12],
                        "created_at": row["created_at"],
                        "updated_at": row["activity_at"],
                        "activity_at": row["activity_at"],
                        "activity_count": int(row["activity_count"] or 0),
                        "agent_id": row["agent_id"],
                        "agent_name": display.get("name", ""),
                        "agent_icon": display.get("icon", ""),
                        "agent_engine": display.get("engine", ""),
                        "engine_thread_id": _engine_thread_id,
                        # A completed Plan-mode turn is deliberately paused for
                        # the user's approval or feedback. Surface the mode to
                        # session-list consumers so they can distinguish that
                        # follow-up state from an ordinary completed run.
                        "execution_mode": (_smeta.get("execution_mode")
                                           if isinstance(_smeta, dict) else None),
                        "pinned": pinned_val,
                        "sort_order": row["sort_order"] if has_sort_order else None,
                        "hidden": bool(row["hidden"]) if has_hidden else False,
                        "recycled": bool(has_status and row["status"] == "recycled"),
                        "run_status": run_status,
                        "run_updated_at": run_updated_at,
                        # The closer's own verdict for this session's latest run,
                        # preferred over run_status by the UI (which keeps
                        # run_status as the fallback). None when the gate is off
                        # or the run predates it.
                        "closing_flag": (closing_by_sid.get(sid) or {}).get("flag"),
                        "closing_cta": (closing_by_sid.get(sid) or {}).get("cta"),
                        "queue_position": queue_position,
                        "queue_total": queue_total,
                        "has_unread": has_unread,
                        "read_at": read_at,
                        "unread_at": unread_at,
                        "child_count": child_counts.get(sid, 0),
                        **manifest,
                    })
        except Exception:
            # Do not disguise a query failure as a real empty account. The
            # session picker treats non-2xx responses as retryable and keeps its
            # warm cache (or cold-load skeleton) visible while it retries.
            conn.close()
            raise

        conn.close()
        return {"sessions": sessions, "db": db, "total_count": total_count}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/sessions/{session_id}/read")
async def mark_session_read(session_id: str, request: Request, db: str = Query("user.db", description="Database filename")):
    """Mark a session as read by setting read_at to now."""
    if db == "user.db":
        from app.db.local import set_db_user_context
        set_db_user_context(_caller_user_id(request) or "")
    db_path = _get_db_path(db)
    hb = _hybrid_backend_for(db)
    try:
        # Local-first so the list's unread badge (computed from read_at) clears
        # instantly; queued upsert keeps the remote read-state in step.
        conn, _dialect = (_open_local_sqlite(db), "sqlite") if hb else _open(db)
        _ensure_session_read_tracking_columns(conn, _dialect)
        # Ownership gate (open/local mode + admins pass; non-owner refused).
        if _session_access_ok(conn.cursor(), session_id, request, None) is False:
            conn.close()
            raise HTTPException(status_code=403, detail="Not authorized for this session")
        # Legacy databases can predate unread tracking. Marking read is an
        # optional UI enhancement, so do not block opening a session if the
        # migration has not added sessions.read_at yet.
        session_cols = _session_columns(conn, _dialect)
        if "read_at" not in session_cols:
            conn.close()
            return {"ok": True, "session_id": session_id, "read_tracking": False}
        read_at = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")
        if "unread_at" in session_cols:
            conn.execute(
                "UPDATE sessions SET read_at = ?, unread_at = NULL WHERE id = ?",
                (read_at, session_id),
            )
        else:
            conn.execute(
                "UPDATE sessions SET read_at = ? WHERE id = ?",
                (read_at, session_id),
            )
        conn.commit()
        conn.close()
        if hb:
            await _enqueue_remote_push(hb, [session_id], "upsert")
        # Return the committed value so clients can apply the exact persisted
        # read state, rather than racing a locally generated timestamp against
        # concurrent metadata/session-list refreshes.
        return {"ok": True, "session_id": session_id, "read_at": read_at}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/sessions/{session_id}/unread")
async def mark_session_unread(session_id: str, request: Request, db: str = Query("user.db", description="Database filename")):
    """Mark a session unread while retaining its last-read timestamp."""
    if db == "user.db":
        from app.db.local import set_db_user_context
        set_db_user_context(_caller_user_id(request) or "")
    hb = _hybrid_backend_for(db)
    try:
        conn, _dialect = (_open_local_sqlite(db), "sqlite") if hb else _open(db)
        _ensure_session_read_tracking_columns(conn, _dialect)
        if _session_access_ok(conn.cursor(), session_id, request, None) is False:
            conn.close()
            raise HTTPException(status_code=403, detail="Not authorized for this session")
        session_cols = _session_columns(conn, _dialect)
        if "read_at" not in session_cols:
            conn.close()
            return {"ok": True, "session_id": session_id, "read_tracking": False}
        # Do not change session activity, transcript records, or the last-read
        # timestamp. The separate marker makes manually unread state durable.
        unread_at = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")
        if "unread_at" in session_cols:
            conn.execute("UPDATE sessions SET unread_at = ? WHERE id = ?", (unread_at, session_id))
        else:
            # Compatibility fallback for a database that has not yet received
            # the additive migration; it keeps the prior behavior rather than
            # failing the action.
            conn.execute("UPDATE sessions SET read_at = NULL WHERE id = ?", (session_id,))
            unread_at = None
        conn.commit()
        conn.close()
        if hb:
            await _enqueue_remote_push(hb, [session_id], "upsert")
        return {"ok": True, "session_id": session_id, "unread_at": unread_at}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def _session_title(cur, sid: str):
    """Best-effort session title lookup (None if absent)."""
    try:
        row = cur.execute("SELECT title FROM sessions WHERE id = ?", (sid,)).fetchone()
        return row["title"] if row else None
    except Exception:
        return None


def _session_updated_at(cur, sid: str):
    """Best-effort session activity timestamp for session-tab ordering."""
    try:
        row = cur.execute("SELECT updated_at FROM sessions WHERE id = ?", (sid,)).fetchone()
        return row["updated_at"] if row else None
    except Exception:
        return None


@router.get("/sessions/{session_id}/related")
def get_session_related(
    session_id: str,
    request: Request,
    db: str = Query("user.db", description="Database filename"),
    include_hidden: bool = Query(False, description="Include hidden family members"),
):
    """Return spawned helpers, browser sessions, and genui links for a session.

    * Spawned helpers: rows from ``agent_spawns`` where the orchestrator
      session matches this session id. Returns the helper's session id,
      name, status, and result summary. Only LIVE members are listed: a
      spawn whose session row is gone, recycled, or hidden (unless
      ``include_hidden``) is filtered out, so closing/binning a sub-agent tab
      really removes the tab.
    * Browser sessions: rows from ``browser_sessions`` where the linked
      agent matches the session's agent_id (if any).
    * GenUI pages: derived from the tool log — genui build/edit tools run by
      this session (link is not stored; "unlinking" is client-side only).
    """
    from app.auth.identity import caller_uid_sync
    user_id = caller_uid_sync(request)

    db_path = _get_db_path(db)
    # `children` is the unified family-member list (spawned helpers). Each
    # carries a `label`/`role` so one frontend renderer draws both the
    # sub-header tab bar AND the session-list tree the same way. `spawns` is
    # kept as a back-compat alias for the spawn children.
    result = {"spawns": [], "children": [], "browser_sessions": [],
              "genui_sessions": [], "parent": None, "orchestrator": None,
              "group_kind": None, "root_label": "Main"}

    try:
        # NOTE: stays on the authoritative backend. Reads `agent_spawns` +
        # `browser_sessions`, which are NOT mirrored to local (written remote-only
        # during operations), so the family tree would be empty on the mirror.
        # Its per-call connection is pooled (Tier C).
        conn, _dialect = _open(db)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # ── 1. Resolve the "family root" (orchestrator) for this session ──
        # The chat sub-header shows a persistent tab bar for a whole spawn
        # family — a "Main" tab for the orchestrator plus one tab per spawned
        # helper — and it must look identical whether the user is viewing the
        # orchestrator OR any of its spawns. So first find the family root:
        #   • if THIS session was itself spawned, the root is its orchestrator
        #     (one hop up), and `parent` is surfaced for back-compat;
        #   • otherwise THIS session IS the root.
        # Spawn ids are written as ``spawn-<uuid>`` by the orchestration
        # ability, so a spawn always resolves to exactly one orchestrator row.
        family_root = session_id
        try:
            tbl = cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='agent_spawns'"
            ).fetchone()
            if tbl:
                parent_row = cur.execute(
                    """SELECT orchestrator_session_id
                       FROM agent_spawns
                       WHERE spawn_session_id = ?
                       ORDER BY created_at ASC LIMIT 1""",
                    (session_id,),
                ).fetchone()
                if parent_row and parent_row["orchestrator_session_id"]:
                    family_root = parent_row["orchestrator_session_id"]
                    result["parent"] = {
                        "session_id": family_root,
                        "title": _session_title(cur, family_root),
                    }

                # ── 2. The family's spawned helpers (siblings of THIS session
                #       when it is a spawn; own children when it is the root) ──
                # `kind` distinguishes an ordinary clone spawn (NULL/'' — the
                # historical default) from a delegation to a REAL saved agent
                # ('delegate'), which the tab bar labels + icons differently.
                # COALESCE keeps the query working on older ledgers created before
                # the column existed.
                #
                # Only LIVE members surface: a spawn whose session row is gone,
                # recycled, or hidden (unless include_hidden) is filtered out
                # here, so closing/binning a sub-agent tab actually makes the
                # tab disappear. Before, every agent_spawns row was listed
                # forever, so a recycled session's tab came straight back on the
                # next poll — the "× doesn't close the tab" bug.
                _has_status = False
                _has_hidden = False
                _has_updated_at = False
                try:
                    _sess_cols = {r[1] for r in cur.execute("PRAGMA table_info(sessions)").fetchall()}
                    _has_status = "status" in _sess_cols
                    _has_hidden = "hidden" in _sess_cols
                    _has_updated_at = "updated_at" in _sess_cols
                except Exception:
                    pass
                _spawn_sql = (
                    """SELECT a.id, a.spawn_session_id, a.name, a.task, a.status,
                              a.result_summary, a.created_at, COALESCE(a.kind, '') AS kind"""
                    + (", s.hidden AS hidden" if _has_hidden else "")
                    + (", s.updated_at AS session_updated_at" if _has_updated_at else "")
                    + """
                       FROM agent_spawns a
                       LEFT JOIN sessions s ON s.id = a.spawn_session_id
                       WHERE a.orchestrator_session_id = ?
                         AND s.id IS NOT NULL"""
                    + (" AND (s.status IS NULL OR s.status != 'recycled')" if _has_status else "")
                    + (" AND (? = 1 OR s.hidden IS NULL OR s.hidden = 0)" if _has_hidden else "")
                    + " ORDER BY a.created_at ASC"
                )
                _spawn_params = [family_root]
                if _has_hidden:
                    _spawn_params.append(1 if include_hidden else 0)
                spawn_rows = cur.execute(_spawn_sql, _spawn_params).fetchall()
                result["spawns"] = [
                    {
                        "id": r["id"],
                        "spawn_session_id": r["spawn_session_id"],
                        "name": r["name"],
                        "task": r["task"],
                        "status": r["status"],
                        "result_summary": r["result_summary"],
                        "created_at": r["created_at"],
                        "kind": r["kind"] or "spawn",
                        "hidden": bool(r["hidden"]) if _has_hidden else False,
                        "updated_at": (r["session_updated_at"] if _has_updated_at else None)
                                      or r["created_at"],
                    }
                    for r in spawn_rows
                ]

                # The "Main" tab target. Only meaningful once the family has at
                # least one spawn; the frontend hides it otherwise.
                if result["spawns"]:
                    result["orchestrator"] = {
                        "session_id": family_root,
                        "title": _session_title(cur, family_root),
                        "updated_at": _session_updated_at(cur, family_root),
                    }
                    result["group_kind"] = "orchestrator"
                    result["root_label"] = "Main"
                    # Unified children list (label=None → frontend defaults to
                    # "Spawn N"); same shape the optimizer family produces below.
                    # A delegation carries role 'delegate' and shows the target
                    # agent's own name as its tab label (a clone spawn stays
                    # label=None → the frontend falls back to "Spawn N").
                    result["children"] = [
                        {
                            "session_id": s["spawn_session_id"],
                            "label": (s["name"] if s.get("kind") == "delegate" else None),
                            "role": ("delegate" if s.get("kind") == "delegate" else "spawn"),
                            "name": s["name"],
                            "status": s["status"],
                            "hidden": s.get("hidden", False),
                            "created_at": s.get("created_at"),
                            "updated_at": s.get("updated_at") or s.get("created_at"),
                        }
                        for s in result["spawns"]
                    ]
        except Exception:
            pass

        # ── 1c. Optimizer family ──────────────────────────────────────────────
        # Optimizer Planner ('optimizer-*') and Closer ('closer-*') sessions are
        # TOP-LEVEL sessions of their own — they appear in the session list as
        # regular rows — so they are deliberately NOT surfaced here as children
        # of the base session they ran on. Only the spawn family above applies.
        pass

        # ── 1d. Enrich children with completed_at / run_status from session_runs ──
        if result.get("children"):
            try:
                _child_ids = [c["session_id"] for c in result["children"] if c.get("session_id")]
                if _child_ids:
                    _placeholders = ",".join("?" for _ in _child_ids)
                    _child_runs = {}
                    for _cr in cur.execute(
                        f"SELECT session_id, status, updated_at, stop_cause, resume_attempts, "
                        f"max_resume_attempts FROM session_runs WHERE session_id IN ({_placeholders})",
                        _child_ids,
                    ).fetchall():
                        # A helper still inside the recovery ladder is in-progress:
                        # report 'resuming' so its row keeps the spinner too.
                        _cst = _cr["status"]
                        if _resume_pending({
                            "status": _cst, "stop_cause": _cr["stop_cause"],
                            "resume_attempts": _cr["resume_attempts"],
                            "max_resume_attempts": _cr["max_resume_attempts"],
                        }):
                            _cst = "resuming"
                        _child_runs[_cr["session_id"]] = {
                            "run_status": _cst,
                            "completed_at": _cr["updated_at"],
                        }
                    _child_agents = {
                        _row["id"]: _row["agent_id"]
                        for _row in cur.execute(
                            f"SELECT id, agent_id FROM sessions WHERE id IN ({_placeholders})",
                            _child_ids,
                        ).fetchall()
                    }
                    for _c in result["children"]:
                        _r = _child_runs.get(_c["session_id"])
                        _c["agent_id"] = _child_agents.get(_c["session_id"])
                        if _r:
                            _c["run_status"] = _r["run_status"]
                            _c["completed_at"] = _r["completed_at"]
                        else:
                            _c["run_status"] = None
                            _c["completed_at"] = None
            except Exception:
                pass

        # ── 2. Resolve the owning agent for this session ──
        agent_id = None
        try:
            agent_row = cur.execute(
                "SELECT agent_id FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if agent_row:
                agent_id = agent_row["agent_id"]
        except Exception:
            pass

        # ── 3. Browser sessions owned by this chat ──
        # Gate: only surface the agent's browser tab(s) if the agent ACTUALLY
        # drove the browser in THIS chat session. A browser_sessions row exists
        # as soon as the user opens the Web tab for an agent (browser_stream
        # auto-creates one via resolve_agent_session), so the row alone is not
        # proof of use. The reliable per-session signal is a browser_action tool
        # execution recorded against this session_id in logs.db.
        used_browser = False
        try:
            from app.db.logs_store import get_log_store
            _bx = get_log_store().query_tool_executions_sync(
                tool_name="browser_action", session_id=session_id, limit=1
            )
            used_browser = bool(_bx)
        except Exception:
            used_browser = False

        if agent_id and used_browser:
            try:
                bs_rows = cur.execute(
                    """SELECT id, title, url, status, shared, created_at, updated_at
                       FROM browser_sessions
                       WHERE user_id = ? AND agent_id = ? AND chat_session_id = ?
                       ORDER BY position ASC, created_at ASC""",
                    (user_id, agent_id, session_id),
                ).fetchall()
                result["browser_sessions"] = [
                    {
                        "id": r["id"],
                        "title": r["title"],
                        "url": r["url"],
                        "status": r["status"],
                        "shared": bool(r["shared"]),
                        "created_at": r["created_at"],
                        "updated_at": r["updated_at"],
                    }
                    for r in bs_rows
                ]
            except Exception:
                pass

        # ── 4. GenUI pages linked to THIS session ──────────────────────────
        # A session is "linked" to a genui when it ran a genui build/edit tool
        # (render_visual / create_genui / edit_genui / set_genui_data /
        # rename_genui / screenshot_genui). The link is DERIVED from the tool
        # log, not stored — so "unlinking" from the tab bar is a pure
        # client-side dismissal; the page itself is never touched. The slug is
        # recovered from the tool's input params; the authoritative title comes
        # from the genui table (a page may have been renamed since the build).
        result["genui_sessions"] = []
        try:
            from app.db.logs_store import get_log_store
            _GENUI_TOOLS = {
                "render_visual", "create_genui", "edit_genui",
                "set_genui_data", "rename_genui", "screenshot_genui",
            }
            _seen_slugs: list = []
            _slug_activity: dict = {}
            _slug_first: dict = {}   # earliest tool activity per slug = tab appearance
            for _row in get_log_store().query_tool_executions_sync(
                session_id=session_id, limit=200
            ):
                if (_row.get("tool_name") or "") not in _GENUI_TOOLS:
                    continue
                _slug = None
                try:
                    _params = json.loads(_row.get("input_params") or "{}")
                    _slug = str(_params.get("slug") or "").strip() or None
                except Exception:
                    _slug = None
                if _slug and _slug not in _seen_slugs:
                    _seen_slugs.append(_slug)
                if _slug:
                    _ts = str(_row.get("ts") or "")
                    _slug_activity[_slug] = max(
                        str(_slug_activity.get(_slug) or ""),
                        _ts,
                    )
                    _prev_first = _slug_first.get(_slug)
                    if _ts and (_prev_first is None or _ts < _prev_first):
                        _slug_first[_slug] = _ts
            if _seen_slugs:
                _qmarks = ",".join("?" for _ in _seen_slugs)
                _genui_rows = cur.execute(
                    f"SELECT slug, title, created_at, updated_at FROM genui WHERE user_id = ? AND slug IN ({_qmarks})",
                    [user_id, *_seen_slugs],
                ).fetchall()
                _genui_meta = {r["slug"]: dict(r) for r in _genui_rows}
                result["genui_sessions"] = [
                    {
                        "slug": s,
                        "title": (_genui_meta.get(s) or {}).get("title") or s,
                        # First appearance in THIS session = the earliest genui
                        # tool call on the slug (fall back to the page's own
                        # creation time). Backs the header tab's age counter.
                        "created_at": (
                            str(_slug_first.get(s) or "")
                            or str((_genui_meta.get(s) or {}).get("created_at") or "")
                        ) or None,
                        "updated_at": max(
                            str((_genui_meta.get(s) or {}).get("updated_at") or ""),
                            str(_slug_activity.get(s) or ""),
                        ) or None,
                    }
                    for s in _seen_slugs
                ]
        except Exception:
            pass

        conn.close()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return result


def _strip_folded_attachments(content: str) -> str:
    """Remove image-description blocks folded into a stored user message.

    The attachment-describe step (app/api/chat.py _maybe_describe_images) appends
    a "[Attached image - ...]: <description>" block to the persisted USER turn so a
    blind model keeps the description across turns. That belongs in the model's
    context, not the user's chat bubble — on reload it made the whole description
    render *inside* the user's message. The description is shown instead via the
    persisted process_image tool row, so we strip the fold for DISPLAY only here.
    The model path (fetch_interactions -> interactions_to_openai_messages) reads
    the raw row and is unaffected. Marker matched on its ASCII prefix (the next
    char is an em dash) so the original typed message before it is returned as-is.
    """
    if not content:
        return content
    idx = content.find("\n\n[Attached image ")
    return content[:idx].rstrip() if idx != -1 else content


def _user_row_attachments(meta_raw, att_by_id):
    """Resolve the attachment records referenced by a stored user turn.

    The message -> attachment link lives in the user row's metadata JSON
    (`attachment_ids`); the bytes live in the session's attachments table. Returns
    frontend-shaped records (matching renderAttachmentElement / _resolveAttachmentUrl)
    so a reloaded user bubble can re-render its pasted images/files — the live
    bubble renders them from the send flow, but a cold reload has only the row.
    Reads metadata directly so it still works in light mode (metadata is never
    slimmed).
    """
    if not meta_raw or not att_by_id:
        return []
    try:
        ids = (json.loads(meta_raw) or {}).get("attachment_ids") or []
    except Exception:
        return []
    return [att_by_id[i] for i in ids if i in att_by_id]


def _interaction_message_phase(metadata, role, status, output):
    """Return the durable UI phase without changing the protocol role.

    New assistant rows carry metadata.message_phase. Older tool-bearing
    assistant rows are unambiguously progress; a phase-less row is activity
    too — nothing but an explicit main/final promotes a row to the run's
    answer. System/debug rows are the engine's own console output, and a
    stopped/errored assistant row is a status outcome — both classified
    "system" so the UI renders a System/Stopped/Error status row.
    """
    if role == "system":
        return "system"
    if role != "assistant":
        return None
    # A crash/stop can leave metadata stamped as pending. The durable row status
    # is authoritative once the run has stopped.
    if status in ("interrupted", "error"):
        return "system"
    if status == "streaming":
        return "pending"
    try:
        meta = json.loads(metadata) if isinstance(metadata, str) else (metadata or {})
        phase = str(meta.get("message_phase") or "").strip().lower()
        # Legacy engine rows may carry the old "terminal" phase label — treat
        # it as "system", the current name for engine console/status rows.
        if phase == "terminal":
            phase = "system"
        if phase in ("pending", "progress", "main", "final", "system"):
            return phase
    except Exception:
        pass
    try:
        out = json.loads(output) if isinstance(output, str) else (output or {})
        if isinstance(out, dict) and out.get("tool_calls"):
            return "progress"
    except Exception:
        pass
    return None


def _mid_run_opening_of(metadata) -> bool:
    """True for a reply the run posted BEFORE it finished — a mid-run OPENING.

    The pre-tool progress message (``pre_tool_progress_message``) and the assertions contract's
    introduction are genuine ``main`` replies, so without this mark the transcript
    reads them as the run's answer: the terminal lane paints them as the run's one
    closer, and the work that follows them collapses into the activity group above
    them. Projecting the mark onto the row is what lets the client treat an opening
    as a BOUNDARY instead (see ui/chat/js/chat-bubble.js ``isMidRunOpeningEl``) —
    the durable projection already splits activity counters at every such row
    (``_compact_activity_rows`` flushes at any non-activity row).
    """
    try:
        meta = json.loads(metadata) if isinstance(metadata, str) else (metadata or {})
    except Exception:
        return False
    if not isinstance(meta, dict):
        return False
    return bool(
        meta.get("pre_tool_progress_message_sent")
        or meta.get("assertions_announced")
        or meta.get("message_visibility_notice")
    )


def _message_type_of(metadata, role, status, output, source):
    """Derive the user-facing message lane type (for the per-type visibility
    toggles). This is a *derived* classification — the durable role/phase/source
    axes stay as written; clients that only care about lanes read this field.

      user     — the user's own messages (always visible)
      main     — the agent's promoted reply: the closing gate's verdict (assistant,
                 phase main/final) or an in-flight streaming row
      progress — mid-turn step bubbles (assistant, phase progress)
      tool     — tool-call rows (role tool)
      summary  — Output Closer recap (system row, sources 'system:closer' / legacy 'system:summary' / 'system:overview')
      system   — engine console/status rows (system, incl. errors/debug)
    """
    if role == "user":
        return "user"
    if role == "tool":
        return "tool"
    if role == "system":
        return "summary" if source in ("system:overview", "system:summary", "system:closer") else "system"
    if role == "assistant":
        # An in-flight streaming row is the live reply (phase "pending").
        if status == "streaming":
            return "main"
        # The closing gate's verdict is the ONLY durable promotion authority:
        # a row is `main` iff its persisted phase is an explicit main/final.
        phase = _interaction_message_phase(metadata, role, status, output)
        if phase in ("main", "final"):
            return "main"
        if phase == "system":
            return "system"  # interrupted/error status rows
        # Phase-less, ``pending`` and ``progress`` rows are activity only — the
        # client applies the same rule, so a reload cannot resurrect a draft as
        # the run's answer.
        return "progress"
    return None


def _legacy_chat_render_kind(metadata, role, status, output, source, content):
    """Recover a visible lane for transcript rows written before V2 labels.

    ``chat_render_kind`` is the durable authority for new rows. Older rows and
    cached projections may not have it, though, and excluding an otherwise
    complete text answer makes a normal greeting appear to have no answer after
    reload. Ordinary progress and tool rows remain activity.
    """
    if role == "user":
        return "user"
    if role != "assistant":
        if role == "system" and str(source or "").startswith("system:closing_partial"):
            return "error"
        return "activity"
    if status in ("interrupted", "error"):
        return "error"
    phase = _interaction_message_phase(metadata, role, status, output)
    if phase in ("main", "final"):
        return "notice" if _mid_run_opening_of(metadata) else "promoted_answer"
    # Legacy text-only assistant completions had neither a V2 kind nor a
    # message phase. Tool-bearing and explicitly-progress rows stay folded.
    if phase is None and str(content or "").strip():
        try:
            payload = json.loads(output) if isinstance(output, str) else (output or {})
            if not (isinstance(payload, dict) and payload.get("tool_calls")):
                return "promoted_answer"
        except (TypeError, ValueError, json.JSONDecodeError):
            return "promoted_answer"
    return "activity"


def _resume_pending(run: Optional[dict]) -> bool:
    """True while a stopped run is still inside the auto-resume ladder.

    A run stopped involuntarily (crash / server_restart / frozen / zombie) is
    marked terminal BEFORE the watchdog or boot sweep re-ignites it. The read
    endpoints report that window as ``run_status='resuming'`` (and
    ``recovering`` on the transcript run payload) so the session keeps its
    in-progress spinner and the open chat keeps its "re-assessing…" note instead
    of flashing idle → working. Delegates to the runner so the resumable-cause
    list and the "gave up" boundary live in exactly one place (see
    ``recovery_pending``: it holds until the run is retired as ``failed``).
    """
    try:
        from app.agent.runner import recovery_pending
        return recovery_pending(run or {})
    except Exception:
        return False


def _filter_orphan_streaming_messages(messages, run_info):
    """Expose only the streaming row owned by the currently running turn.

    Contract retries in older builds could leave throttled assistant snapshots
    marked streaming after the run moved on. They remain durable audit data but
    are not transcript messages and must not render as truncated replies.
    """
    active_id = ""
    if run_info and run_info.get("active"):
        active_id = str(run_info.get("assistant_interaction_id") or "")
    return [
        message for message in messages
        if message.get("status") != "streaming"
        or (active_id and str(message.get("id") or "") == active_id)
    ]


@router.get("/session-messages")
def get_session_messages(
    request: Request,
    session_id: str = Query(..., description="Session ID"),
    limit: int = Query(20, description="Max messages to return (per edge when `around_id` is set)"),
    before_id: Optional[str] = Query(None, description="If set, return the batch of messages immediately older than this message (backward pagination)"),
    after_id: Optional[str] = Query(None, description="If set, return the batch of messages immediately newer than this message (forward pagination)"),
    around_id: Optional[str] = Query(None, description="If set, return a window centred on this message: up to `limit` older rows plus up to `limit` newer rows. Used to reopen a session on the user's saved scroll position without downloading the whole tail."),
    at_start: bool = Query(False, description="If set, return the OLDEST `limit` rows (the true start of the session) instead of the newest. Used by the double-chevron 'jump to start' nav."),
    nearest_user_before_id: Optional[str] = Query(None, description="If set, return the single most-recent USER row strictly older than this message (the true 'last user message' for the single-chevron nav, even when it was never loaded)."),
    nearest_user_after_id: Optional[str] = Query(None, description="If set, return the single earliest USER row strictly newer than this message (the next user-turn anchor, even when it was never loaded)."),
    after_seq: Optional[int] = Query(None, description="If set, return only rows with session_seq > this value. Used by hybrid/browser caching to fetch only new messages since last sync (incremental delta)."),
    known_revision: Optional[int] = Query(None, ge=0, description="Cached authority revision to validate."),
    known_hash: Optional[str] = Query(None, max_length=128, description="Cached authoritative content hash to validate."),
    manifest_only: bool = Query(False, description="Validate the cached manifest without transferring transcript rows."),
    complete_turn_boundary: bool = Query(False, description="For a newest-tail open, extend the window to its owning USER row in the same query so an activity group, its counters, and its closer render atomically."),
    max_complete_turn_rows: int = Query(0, ge=0, le=500, description="Optional cap for a complete-turn tail. When exceeded, return only the newest capped rows and signal truncation."),
    light: int = Query(0, description="When 1, blank heavy tool-call bodies while keeping disclosure descriptors. One call's full body loads via /session-tool-detail only when that row is expanded."),
    compact_activity: bool = Query(False, description="When true, project completed historical activity into counters-only summary records. Child descriptors load from /session-activity-group-detail on expansion."),
    db: str = Query("user.db", description="Database filename"),
):
    """Return a window of a session's messages, oldest-first.

    Open modes, by cursor:
      • none → the NEWEST `limit` rows (a long session opens on its latest
        messages, not its oldest).
      • `around_id` → a window CENTRED on that message (`limit` older + `limit`
        newer). This is the fast "reopen where I left off" path.
      • `at_start` → the OLDEST `limit` rows (jump-to-start nav).
      • `nearest_user_before_id` → the single most-recent USER row older than
        that message (jump-to-last-user-message nav).
      • `nearest_user_after_id` → the single earliest USER row newer than
        that message (jump-to-next-user-message nav).
      • `before_id` / `after_id` → the batch immediately older / newer than that
        message (infinite scroll up / down).

    `has_more` reports whether still-older rows remain; `has_newer` whether
    still-newer rows remain (relevant after an `around_id`/`before_id` open).
    Within a batch, rows are ordered oldest-first for top-to-bottom rendering.
    `max_session_seq` is the session's true latest seq (so the live reconcile
    poll doesn't backfill the gap when opened mid-history); `context_tokens` is
    a whole-session token estimate for the ctx indicator (the windowed payload
    alone would under-report it).
    """
    # Route to temp DB if session has one
    resolved_db = _resolve_session_db(session_id, db)
    if resolved_db != db:
        db = resolved_db
    db_path = _get_db_path(db)
    # Remote Control continuity: pull any rows another device wrote into this
    # session into the local mirror first (bounded), so a device that only viewed
    # or only sent a remotely-executed turn sees the WHOLE transcript, not just its
    # own half. No-op single-device / when already complete locally. See helper.
    _reconcile_session_from_remote(db, session_id)
    try:
        # Transcript open → local mirror when hybrid is on (the transcript is
        # written local-first, so on the originating device it's the freshest copy
        # and costs no network). Cold-session safety net: if this session has NOT
        # been mirrored on THIS device yet (e.g. it was created/chatted on another
        # device and never opened here), the local read would be empty — so probe
        # for any local row and fall back to the authoritative remote when absent.
        conn, _dialect = _open_read(db)
        if _dialect == "sqlite" and _pg_conninfo_for(db) is not None:
            try:
                _local_hit = conn.execute(
                    "SELECT 1 FROM interactions WHERE session_id = ? LIMIT 1",
                    (session_id,),
                ).fetchone()
            except Exception:
                _local_hit = None
            if _local_hit is None:
                conn.close()
                conn, _dialect = _open(db)
        cur = conn.cursor()
        # Same-second tie-breaker for stable pagination: SQLite has an implicit
        # monotonic `rowid`; Postgres doesn't, so fall back to the (uuid) `id`.
        _tb = "rowid" if _dialect == "sqlite" else "id"

        # Participant check: requesting user must own or be a participant in the session.
        # Decode JWT directly — BaseHTTPMiddleware state doesn't reliably propagate to handlers.
        _token = ""
        _auth_header = request.headers.get("Authorization", "")
        if _auth_header.startswith("Bearer "):
            _token = _auth_header[7:]
        if not _token:
            _token = request.query_params.get("token", "")
        _payload = decode_token(_token) if _token else None
        requesting_user_id = _payload.get("user_id") if _payload else None
        requesting_username = _payload.get("sub") if _payload else None
        requester_identities = {v for v in (requesting_user_id, requesting_username) if v}
        # The session's recorded execution mode (Ask/Plan/Auto) — the chat panel
        # applies this on load so the pill matches what the server will actually
        # do, even on a cold device. None when never set (UI defaults to Ask).
        _session_exec_mode = None
        _timestamp_order_recovery = False
        _timestamp_order_recovery_enabled = True
        _timestamp_order_recovery_threshold_seconds = 60
        try:
            from app.admin.settings import (
                get_transcript_timestamp_order_recovery_enabled,
                get_transcript_timestamp_order_recovery_threshold_seconds,
            )
            _timestamp_order_recovery_enabled = bool(
                get_transcript_timestamp_order_recovery_enabled(),
            )
            _timestamp_order_recovery_threshold_seconds = (
                get_transcript_timestamp_order_recovery_threshold_seconds()
            )
        except Exception:  # noqa: BLE001
            pass
        # Remote Control executor for this session (instance_id + label) — the chat
        # pill applies it on load so EVERY device that opens the session shows and
        # routes to the same executor. None when the session runs locally.
        _session_remote_exec = None
        try:
            cur.execute(
                "SELECT user_id, participants, metadata FROM sessions WHERE id = ?",
                (session_id,)
            )
            session_row = cur.fetchone()
            if session_row:
                owner_id = session_row[0]
                participants_raw = session_row[1] or "[]"
                try:
                    _smeta = json.loads(session_row[2]) if session_row[2] else {}
                    if isinstance(_smeta, dict):
                        _mv = _smeta.get("execution_mode")
                        if isinstance(_mv, str) and _mv:
                            _session_exec_mode = _mv
                        _timestamp_order_recovery = (
                            _timestamp_order_recovery_enabled
                            and bool(_smeta.get("transcript_timestamp_order_recovery"))
                        )
                        _re = _smeta.get("remote_executor")
                        if isinstance(_re, dict) and _re.get("instance_id"):
                            _session_remote_exec = {
                                "instance_id": _re.get("instance_id"),
                                "label": _re.get("label") or _re.get("instance_id"),
                            }
                except (json.JSONDecodeError, TypeError):
                    pass
                try:
                    participants = json.loads(participants_raw)
                except (json.JSONDecodeError, TypeError):
                    participants = []
                participant_ids = {p.get("id") for p in participants if isinstance(p, dict)}
                is_authorized = bool(requester_identities) and bool(
                    requester_identities & ({owner_id} | participant_ids)
                )
                if not is_authorized and not _is_open_access_mode():
                    conn.close()
                    return {"messages": [], "session_id": session_id, "db": db, "restricted": True}
        except Exception:
            pass  # No sessions table — fall through to message fetch

        manifest = None
        if _session_messages_needs_manifest(manifest_only, known_revision, known_hash):
            manifest = compute_session_manifest(conn, session_id)
        if manifest_only:
            not_modified = _manifest_cache_not_modified(
                known_revision,
                known_hash,
                manifest,
            )
            conn.close()
            return {
                "messages": [],
                "session_id": session_id,
                "db": db,
                "manifest": manifest,
                "not_modified": not_modified,
                "cache_status": "validated" if not_modified else "stale",
            }

        messages = []
        # One durable transcript contract: storage decides visibility by kind.
        # Activity summaries are persisted projections, never reader-side groups.
        _activity_summaries = {}
        if _dialect == "sqlite":
            try:
                from app.db.session_activity import (
                    ACTIVITY_PROJECTION_VERSION,
                    ensure_session_activity_summaries,
                    rebuild_session_activity_summaries,
                )
                # The projection is maintained by interaction writes. A bounded,
                # per-session rebuild is only a migration for an older projection
                # version; it is not a reader-side grouping fallback.
                ensure_session_activity_summaries(conn)
                stale = cur.execute(
                    "SELECT 1 FROM session_activity_summaries "
                    "WHERE session_id=? AND COALESCE(projection_version, 0) < ? LIMIT 1",
                    (session_id, ACTIVITY_PROJECTION_VERSION),
                ).fetchone()
                if stale:
                    rebuild_session_activity_summaries(conn, session_id)
                    conn.commit()
                cur.execute("SELECT * FROM session_activity_summaries WHERE session_id=?", (session_id,))
                _activity_summaries = {int(row["start_seq"]): dict(row) for row in cur.fetchall()}
            except Exception:
                _activity_summaries = {}
        # Resolve this session's attachments once so a reloaded user turn can
        # re-render its pasted images/files (the live bubble gets them from the
        # send flow; a cold reload has only the stored row). Shaped to match the
        # frontend attachment object (renderAttachmentElement/_resolveAttachmentUrl).
        att_by_id = {}
        try:
            cur.execute(
                "SELECT id, original_name, mime_type, size_bytes, storage_path, storage_provider, user_id "
                "FROM attachments WHERE session_id = ?",
                (session_id,),
            )
            for _a in cur.fetchall():
                att_by_id[_a[0]] = {
                    "attachment_id": _a[0],
                    "original_name": _a[1],
                    "mime_type": _a[2],
                    "size_bytes": _a[3],
                    "storage_provider": _a[5],
                    "url": __import__("app.content_access", fromlist=["issue_content_grant"]).issue_content_grant(
                        user_id=_a[6], artifact_id=_a[0]
                    ),
                }
        except Exception:
            att_by_id = {}
        # role-by-id (filled once `rows` is fetched below) lets _row_to_msg tell a
        # SYNTHETIC standalone tool row — vision (process_image/route_attachment
        # parented to the user turn) or a loop-node memory row (search/save, tagged
        # metadata.brain) — both written with no assistant tool_call to pair with —
        # from a real model-issued one, so the reload renderer can show each as its
        # own foldable tool call.
        role_by_id = {}
        has_more = False
        has_newer = False
        turn_boundary_complete = False
        turn_boundary_truncated = False
        # Canonical transcript order. session_seq records emission order and is
        # authoritative; created_at/rowid are deterministic tie-breakers and the
        # compatibility path for rows created before sequencing existed.
        _order_key = _interaction_order_key(_tb, _timestamp_order_recovery)
        _order_asc = _interaction_order_by(_tb, timestamp_recovery=_timestamp_order_recovery)
        _order_desc = _interaction_order_by(_tb, "DESC", _timestamp_order_recovery)

        def _synthetic_cursor_target(raw):
            """The durable row a PROJECTED transcript id was built from.

            Compact activity emits ``activity-summary:<child id|session_seq>``
            rows. They are transport projections with no interaction row of their
            own, so passing one as ``before_id`` resolved to "no cursor" and
            silently answered with the NEWEST window — which wedged load-older
            permanently (the oldest cached row never advanced). Recover the
            embedded child instead.
            """
            value = str(raw or "")
            if not value.startswith("activity-summary:"):
                return None
            body = value.split(":", 1)[1].strip()
            if not body:
                return None
            if body.isdigit():
                # The projection embeds the child's session_seq when it has no id;
                # resolve that INSIDE this session only.
                try:
                    cur.execute(
                        'SELECT id FROM interactions WHERE session_id = ? '
                        'AND session_seq = ? LIMIT 1',
                        (session_id, int(body)),
                    )
                    _hit = cur.fetchone()
                except Exception:
                    return None
                return _hit[0] if _hit else None
            return body

        def _cursor_for(mid):
            for _tbl in ("interactions", "messages"):
                try:
                    _seq_col = "session_seq" if _tbl == "interactions" else "NULL"
                    cur.execute(
                        f'SELECT {_seq_col}, created_at, {_tb} FROM "{_tbl}" WHERE id = ?',
                        (mid,),
                    )
                    _r = cur.fetchone()
                    if _r:
                        return _r[0], _r[1], _r[2]
                except Exception:
                    pass
            # A projected/synthetic id is not a durable row. Resolve the row it was
            # projected FROM so any caller holding one still pages correctly; only
            # an unrecoverable id degrades to "no cursor".
            _target = _synthetic_cursor_target(mid)
            if _target and str(_target) != str(mid):
                return _cursor_for(_target)
            return None, None, None

        before_seq = before_ts = before_rowid = None
        after_seq_cursor = after_ts = after_rowid = None
        around_seq = around_ts = around_rowid = None
        if before_id:
            before_seq, before_ts, before_rowid = _cursor_for(before_id)
        if after_id:
            after_seq_cursor, after_ts, after_rowid = _cursor_for(after_id)
        if around_id:
            around_seq, around_ts, around_rowid = _cursor_for(around_id)
        nearest_user_seq = nearest_user_ts = nearest_user_rowid = None
        if nearest_user_before_id:
            nearest_user_seq, nearest_user_ts, nearest_user_rowid = _cursor_for(nearest_user_before_id)
        nearest_user_after_seq = nearest_user_after_ts = nearest_user_after_rowid = None
        if nearest_user_after_id:
            nearest_user_after_seq, nearest_user_after_ts, nearest_user_after_rowid = _cursor_for(nearest_user_after_id)

        lim = limit or 20
        # Fetch one extra row per edge so we can detect remaining rows without a
        # second existence query.
        fetch_n = lim + 1

        # Light mode blanks the heavy bodies while keeping the wire shape, so the
        # renderer is unchanged but the transcript opens on a tiny payload. Tool
        # result bodies are blanked and the assistant's output is slimmed to just
        # the tool-call NAMES (so the "N tool calls" heading and each row's name +
        # duration still render). Full bodies load on demand when the user expands
        # an individual row — see /session-tool-detail.
        def _is_brain_row(meta_str):
            # Loop-node memory rows (the pre-turn search / post-turn save) carry
            # metadata.brain=True. That distinguishes them from a model-issued
            # memory_search the agent calls as a normal tool (which pairs with an
            # assistant tool_call and renders the ordinary way). A SKIPPED search
            # (greeting/command — nothing actually happened) is left untagged so it
            # doesn't render an empty bubble; the live path skips it too (no
            # tool_result fires), so both paths stay consistent.
            try:
                meta = json.loads(meta_str) if meta_str else {}
                return bool(meta.get("brain")) and not meta.get("skipped")
            except Exception:
                return False

        def _slim(m):
            role = m.get("role")
            if role == "assistant":
                out = m.get("output")
                if out:
                    try:
                        _o = json.loads(out)
                        _tcs = _o.get("tool_calls") or []
                        _slim_tcs = [
                            {
                                "id": tc.get("id"),
                                "function": {"name": (tc.get("function") or {}).get("name"), "arguments": ""},
                            }
                            for tc in _tcs if isinstance(tc, dict)
                        ]
                        _slim_o = {}
                        if _slim_tcs:
                            _slim_o["tool_calls"] = _slim_tcs
                        # Strip the full LLM schema snapshot in light mode — it's
                        # heavy payload that the UI lazy-loads on demand via
                        # /session-tool-detail when the user expands that call.
                        # Preserve a boolean flag so the frontend knows this turn
                        # HAS a schema available to fetch.
                        if _o.get("_sent_messages"):
                            _slim_o["_has_sent_schema"] = True
                        m["output"] = json.dumps(_slim_o) if _slim_o else None
                    except Exception:
                        m["output"] = None
            elif role == "tool":
                # Tool rows normally cache only disclosure descriptors. A saved
                # file is the one deliberate exception: its compact result is the
                # trusted, user-visible attachment descriptor the transcript needs
                # to render a download card after reload. Validate and reduce it
                # server-side; arbitrary tool bodies remain unavailable in light mode.
                _attachment = None
                if m.get("tool_name") in {"save_file", "attach_file"}:
                    try:
                        _candidate = json.loads(m.get("content") or "")
                        if (
                            isinstance(_candidate, dict)
                            and _candidate.get("kind") == "file_attachment"
                            and _candidate.get("status") == "ok"
                            and isinstance(_candidate.get("filename"), str)
                            and isinstance(_candidate.get("url"), str)
                            and _candidate["url"].startswith("/api/v1/content/")
                        ):
                            _attachment = {
                                key: _candidate.get(key)
                                for key in ("kind", "status", "filename", "mime_type", "url", "bytes")
                            }
                    except (TypeError, ValueError, json.JSONDecodeError):
                        pass
                m["content"] = json.dumps(_attachment) if _attachment else ""
                m["output"] = None
                try:
                    _meta = json.loads(m.get("metadata")) if m.get("metadata") else {}
                except Exception:
                    _meta = {}
                _kept = {
                    key: _meta[key]
                    for key in ("duration_ms", "error", "brain", "skipped", "message_phase", "message_type")
                    if key in _meta
                }
                m["metadata"] = json.dumps(_kept) if _kept else None
            return m

        def _row_to_msg(row):
            if row[11] == "activity_summary":
                summary = _activity_summaries.get(int(row[7])) or {}
                counts = {"tools": int(summary.get("tool_count") or 0), "messages": int(summary.get("progress_count") or 0), "mode": int(summary.get("mode_count") or 0), "model": int(summary.get("model_count") or 0), "compactions": int(summary.get("compaction_count") or 0), "updates": int(summary.get("activity_count") or 1)}
                return {
                    "id": row[0], "session_id": row[1], "role": "activity_summary",
                    "_activity_summary": True, "activity_group_id": row[0],
                    "owner_turn_id": row[12] or "", "first_interaction_id": summary.get("first_interaction_id"),
                    "activity_start_seq": row[7], "activity_end_seq": summary.get("end_seq"),
                    "session_seq": row[7], "interaction_seq": row[7], "created_at": row[5],
                    "summary_counts": counts, "activity_open": bool(summary.get("is_open")),
                    "chat_render_kind": "activity",
                }
            m = {
                "id": row[0],
                "session_id": row[1],
                "role": row[2],
                "content": row[3],
                "tool_name": row[4],
                "created_at": row[5],
                "status": row[6],
                "session_seq": row[7],
                "output": row[8],
                "metadata": row[9],
                "parent_id": row[10],
                "source": row[11] if len(row) > 11 else None,
                "turn_id": row[12] if len(row) > 12 else None,
                "turn_seq": row[13] if len(row) > 13 else None,
                "chat_render_kind": row[14] if len(row) > 14 else None,
            }
            # chat_render_kind is the sole transcript display contract.
            # Missing labels stay activity; do not infer from metadata.
            m["message_phase"] = _interaction_message_phase(
                m["metadata"], m["role"], m["status"], m["output"])
            m["message_type"] = _message_type_of(
                m["metadata"], m["role"], m["status"], m["output"], m.get("source"))
            m["interaction_seq"] = m["session_seq"]
            m["mid_run_opening"] = _mid_run_opening_of(m["metadata"])
            if _timestamp_order_recovery:
                m["timestamp_order_recovery"] = True
            try:
                m["timestamp_order_anomaly"] = bool(json.loads(m["metadata"] or "{}").get("timestamp_order_anomaly"))
            except (TypeError, json.JSONDecodeError):
                m["timestamp_order_anomaly"] = False
            from app.content_access import refresh_content_urls_in_text
            m["content"] = refresh_content_urls_in_text(m.get("content"), requesting_user_id)
            m["output"] = refresh_content_urls_in_text(m.get("output"), requesting_user_id)
            # Hide any image-description block folded into the user turn — it is
            # shown as a process_image tool row, not inside the user's bubble.
            if m.get("role") == "user":
                m["content"] = _strip_folded_attachments(m.get("content"))
                # Re-attach the turn's pasted images/files so they survive reload.
                _atts = _user_row_attachments(row[9], att_by_id)
                if _atts:
                    m["attachments"] = _atts
            elif m.get("role") == "tool" and m.get("tool_name") in ("process_image", "route_attachment", "app_control") \
                    and role_by_id.get(row[10]) == "user":
                # Image-processing AND App Control rows are parented to the USER turn
                # and have no assistant tool_call to pair with, so the normal renderer
                # (which matches results to an assistant's saved tool_calls) skips
                # them. Mark them so the reload path renders each as its own foldable
                # tool call (and so _slim keeps the body intact in light mode).
                m["_synth_tool"] = True
            elif m.get("role") == "tool" and m.get("tool_name") == "resume_session" \
                    and m.get("source") == "system:recovery":
                # Automatic recovery is a display-only tool result, written by
                # resume_one() without a model-issued parent tool call.
                m["_synth_tool"] = True
            elif m.get("role") == "tool" and m.get("tool_name") in ("memory_search", "memory_save") \
                    and _is_brain_row(row[9]):
                # Loop-node memory rows (brain search before the turn, save after)
                # are written WITHOUT an assistant tool_call to pair against —
                # exactly the vision case — so the normal renderer skips them too.
                # Flag them to render as their own foldable tool bubble on reload.
                m["_synth_tool"] = True
            return _slim(m) if light else m

        # Try interactions table first (has richer data)
        try:
            # `status` may not exist on very old DBs — probe and fall back.
            _has_status = False
            try:
                _icols = {r[1] for r in cur.execute("PRAGMA table_info(interactions)").fetchall()}
                _has_status = "status" in _icols
            except Exception:
                _has_status = False
            _status_col = "status" if _has_status else "'complete' AS status"
            _base = (
                f'SELECT id, session_id, role, content, tool_name, created_at, {_status_col}, '
                f'session_seq, output, metadata, parent_id, source, turn_id, turn_seq, NULL AS chat_render_kind FROM interactions'
            )
            _tb = "id"
            _order_key = _interaction_order_key(_tb, _timestamp_order_recovery)
            _order_asc = _interaction_order_by(_tb, timestamp_recovery=_timestamp_order_recovery)
            _order_desc = _interaction_order_by(_tb, "DESC", _timestamp_order_recovery)
            # V2 is a storage projection: raw activity/hidden rows never leave
            # this endpoint for the browser to classify.
            _base = (
                "SELECT id, session_id, role, content, tool_name, created_at, status, session_seq, output, metadata, parent_id, source, turn_id, turn_seq, chat_render_kind FROM ("
                "SELECT id, session_id, role, content, tool_name, created_at, status, session_seq, output, metadata, parent_id, source, turn_id, turn_seq, chat_render_kind FROM interactions WHERE chat_render_kind IN ('user','promoted_answer','notice','error') AND (status IS NULL OR status != 'deleted') "
                "UNION ALL SELECT 'activity-summary:' || first_interaction_id, session_id, 'activity_summary', '', NULL, created_at, 'complete', start_seq, NULL, NULL, NULL, 'activity_summary', owner_turn_id, NULL, 'activity' FROM session_activity_summaries"
                ") transcript"
            )

            if at_start:
                # True start of the session — oldest `limit` rows, oldest-first.
                # Nothing can be older than the first row, so has_more is always
                # False; has_newer reports whether rows remain beyond the window.
                cur.execute(
                    _base + f' WHERE session_id = ? ORDER BY {_order_asc} LIMIT ?',
                    (session_id, fetch_n),
                )
                rows = cur.fetchall()  # oldest-first
                has_newer = len(rows) > lim
                rows = rows[:lim]
                has_more = False
            elif nearest_user_before_id and nearest_user_ts is not None:
                # Single most-recent USER row strictly older than the cursor —
                # the jump-to-last-user-message nav, accurate even when that turn
                # was never loaded into the open window.
                cur.execute(
                    _base + f" WHERE session_id = ? AND role = 'user' AND ({_order_key}) < (?, ?, ?, ?) "
                    f"ORDER BY {_order_desc} LIMIT 1",
                    (session_id, *_interaction_cursor_values(nearest_user_seq, nearest_user_ts, nearest_user_rowid)),
                )
                rows = cur.fetchall()
                has_more = False
                has_newer = False
            elif nearest_user_after_id and nearest_user_after_ts is not None:
                # Single earliest USER row strictly newer than the cursor — the
                # jump-to-next-user-message nav, accurate beyond the loaded window.
                cur.execute(
                    _base + f" WHERE session_id = ? AND role = 'user' AND ({_order_key}) > (?, ?, ?, ?) "
                    f"ORDER BY {_order_asc} LIMIT 1",
                    (session_id, *_interaction_cursor_values(nearest_user_after_seq, nearest_user_after_ts, nearest_user_after_rowid)),
                )
                rows = cur.fetchall()
                has_more = False
                has_newer = False
            elif around_ts is not None:
                # Window centred on the anchor: anchor-and-older (newest-first)
                # plus strictly-newer (oldest-first); each edge reports whether
                # more rows remain beyond it.
                cur.execute(
                    _base + f' WHERE session_id = ? AND ({_order_key}) <= (?, ?, ?, ?) '
                    f'ORDER BY {_order_desc} LIMIT ?',
                    (session_id, *_interaction_cursor_values(around_seq, around_ts, around_rowid), fetch_n),
                )
                older = cur.fetchall()
                has_more = len(older) > lim
                older = older[:lim]
                cur.execute(
                    _base + f' WHERE session_id = ? AND ({_order_key}) > (?, ?, ?, ?) '
                    f'ORDER BY {_order_asc} LIMIT ?',
                    (session_id, *_interaction_cursor_values(around_seq, around_ts, around_rowid), fetch_n),
                )
                newer = cur.fetchall()
                has_newer = len(newer) > lim
                newer = newer[:lim]
                rows = list(reversed(older)) + list(newer)  # oldest-first
            elif after_ts is not None:
                cur.execute(
                    _base + f' WHERE session_id = ? AND ({_order_key}) > (?, ?, ?, ?) '
                    f'ORDER BY {_order_asc} LIMIT ?',
                    (session_id, *_interaction_cursor_values(after_seq_cursor, after_ts, after_rowid), fetch_n),
                )
                rows = cur.fetchall()  # oldest-first
                has_newer = len(rows) > lim
                rows = rows[:lim]
                has_more = True  # paged forward from a point → older rows exist
            else:
                _where = "session_id = ?"
                _params: list = [session_id]
                if before_ts is not None:
                    _where += f" AND ({_order_key}) < (?, ?, ?, ?)"
                    _params.extend(_interaction_cursor_values(before_seq, before_ts, before_rowid))
                if after_seq is not None:
                    _where += " AND session_seq IS NOT NULL AND session_seq > ?"
                    _params.append(after_seq)
                cur.execute(
                    _base + f' WHERE {_where} ORDER BY {_order_desc} LIMIT ?',
                    (*_params, fetch_n),
                )
                rows = cur.fetchall()  # newest-first
                has_more = len(rows) > lim
                rows = rows[:lim]
                rows.reverse()  # oldest-first
                has_newer = before_ts is not None

                # A newest-N slice can start halfway through a long activity
                # phase. The old client repaired that with as many as eight serial
                # page requests, exposing provisional counts and making session
                # open unnecessarily slow. Expand from the nearest HARD render
                # boundary in one query. Only a USER row owns a run: system
                # notices may flush a visual phase, but cannot establish the
                # user-turn ownership needed for accurate tool/message totals or
                # for the terminal closer. Starting at a debug/system row used to
                # return a deceptively "complete" orphaned tail.
                if (complete_turn_boundary and before_ts is None and after_seq is None
                        and rows):
                    _first_seq, _first_ts, _first_rowid = _cursor_for(rows[0][0])
                    if _first_ts is not None:
                        _first_cursor = _interaction_cursor_values(
                            _first_seq, _first_ts, _first_rowid)
                        cur.execute(
                            f"SELECT session_seq, created_at, {_tb} FROM interactions "
                            f"WHERE session_id = ? AND role = 'user' "
                            f"AND ({_order_key}) <= (?, ?, ?, ?) "
                            f"ORDER BY {_order_desc} LIMIT 1",
                            (session_id, *_first_cursor),
                        )
                        _boundary = cur.fetchone()
                        if _boundary:
                            _boundary_cursor = _interaction_cursor_values(*_boundary)
                            cur.execute(
                                _base + f' WHERE session_id = ? AND ({_order_key}) >= (?, ?, ?, ?) '
                                f'ORDER BY {_order_asc}',
                                (session_id, *_boundary_cursor),
                            )
                            _boundary_rows = cur.fetchall()
                            if max_complete_turn_rows and len(_boundary_rows) > max_complete_turn_rows:
                                rows = _boundary_rows[-max_complete_turn_rows:]
                                has_more = True
                                turn_boundary_truncated = True
                            else:
                                rows = _boundary_rows
                                cur.execute(
                                    f"SELECT 1 FROM interactions WHERE session_id = ? "
                                    f"AND ({_order_key}) < (?, ?, ?, ?) LIMIT 1",
                                    (session_id, *_boundary_cursor),
                                )
                                has_more = cur.fetchone() is not None
                                turn_boundary_complete = True
                            has_newer = False

            # Map id -> role for this window so _row_to_msg can classify a synthetic
            # vision tool row by its parent turn's role.
            for _r in rows:
                role_by_id[_r[0]] = _r[2]
            for row in rows:
                # Hide parallel-racing loser rows (kept only for diagnostics) so the
                # transcript shows one answer per turn, not the winner + 3 losers.
                if _is_loser_row(row[9]):
                    continue
                messages.append(_row_to_msg(row))
        except Exception:
            pass

        if not messages:
            # Fallback to messages table (legacy DBs). Only newest/before paging;
            # an anchor/forward cursor degrades to newest.
            try:
                _where = "session_id = ?"
                _params = [session_id]
                if before_ts is not None and around_ts is None and after_ts is None:
                    _where += f" AND (created_at < ? OR (created_at = ? AND {_tb} < ?))"
                    _params.extend([before_ts, before_ts, before_rowid])
                cur.execute(
                    f'SELECT id, session_id, role, content, created_at '
                    f'FROM messages WHERE {_where} ORDER BY created_at DESC, {_tb} DESC LIMIT ?',
                    (*_params, fetch_n)
                )
                _rows = cur.fetchall()
                has_more = len(_rows) > lim
                _rows = _rows[:lim]
                _rows.reverse()
                for row in _rows:
                    m = {
                        "id": row[0],
                        "session_id": row[1],
                        "role": row[2],
                        "content": row[3],
                        "created_at": row[4],
                    }
                    if light and m.get("role") == "tool":
                        m["content"] = ""
                    messages.append(m)
            except Exception:
                pass

        # ── Per-message context (the actual provider prompt behind each turn) ──
        # The ledger's usage_events.interaction_id points at the TOOL row that was
        # current when each LLM call billed (the turn's parent), so the reliable
        # join to a message is via interactions.turn_id: group usage by
        # interaction_id, resolve those to turn_ids in the per-user DB, then attach
        # the turn's MAX input_tokens to every assistant message of that turn. A
        # user message inherits the context of the NEXT assistant message (the
        # prompt that included it). Close-out-lane (system:closer /
        # system:summary / system:overview legacy) messages carry the closer's OWN prompt size (linked
        # via interaction_id), and compaction notices carry the summariser's
        # prompt size (linked by created_at window). The field is omitted when
        # the ledger has no row; the client then shows the session-latest value
        # instead.
        try:
            _turn_ctx: dict = {}      # turn_id -> max input_tokens (chat calls)
            _overview_ctx: dict = {}  # interaction_id -> input_tokens (overview calls)
            _compact_rows: list = []  # (created_at, input_tokens, cost_usd) asc
            _overview_rows: list = []  # (created_at, input_tokens, cost_usd) asc — time fallback
            # Locked-in per-call cost (published $/1M × tokens at that call's
            # model — the same usage_events figures the session-cost chip sums)
            # for the per-message Cost readout, keyed exactly like the ctx maps.
            _turn_cost: dict = {}      # turn_id -> cost_usd (chat calls)
            _overview_cost: dict = {}  # interaction_id -> cost_usd (overview calls)
            if requesting_user_id:
                from app.db import get_app_db
                _cdb = get_app_db()
                if hasattr(_cdb, "_get_conn"):
                    _cdb_conn = _cdb._get_conn()
                    try:
                        _cdb_cur = _cdb_conn.cursor()
                        for _src in ("chat", "background:closer", "background:summarizer", "background:overview"):
                            # Per interaction keep ONLY the row with the largest
                            # prompt — the call that actually produced the turn —
                            # and carry its locked-in cost_usd alongside, so the
                            # per-message Cost readout always pairs with the ctx
                            # the Context row shows (same usage row, never a
                            # re-priced estimate).
                            _by_int: dict = {}
                            _by_int_cost: dict = {}
                            _last_iid = None
                            for _r in _cdb_cur.execute(
                                "SELECT interaction_id, input_tokens, output_tokens, cost_usd "
                                "FROM usage_events "
                                "WHERE user_id = ? AND session_id = ? AND source = ? "
                                "AND interaction_id IS NOT NULL "
                                "ORDER BY interaction_id, input_tokens DESC",
                                (requesting_user_id, session_id, _src),
                            ).fetchall():
                                _iid = str(_r[0] or "")
                                if not _iid or _iid == _last_iid:
                                    continue  # first row per interaction IS its max input
                                _last_iid = _iid
                                if _r[1]:
                                    _by_int[_iid] = int(_r[1])
                                    _by_int_cost[_iid] = float(_r[3] or 0.0)
                            if _src in ("background:overview", "background:summarizer", "background:closer"):
                                _overview_ctx.update(_by_int)
                                _overview_cost.update(_by_int_cost)
                            elif _by_int:
                                # Resolve interaction_id -> turn_id in the per-user DB.
                                _int_ids = list(_by_int.keys())
                                _ph = ",".join("?" * len(_int_ids))
                                for _tr in cur.execute(
                                    f"SELECT id, turn_id FROM interactions "
                                    f"WHERE session_id = ? AND id IN ({_ph}) "
                                    f"AND turn_id IS NOT NULL",
                                    (session_id, *_int_ids),
                                ).fetchall():
                                    if _tr[1]:
                                        _tid = str(_tr[1])
                                        _inp = _by_int.get(str(_tr[0]), 0)
                                        if _inp and _inp >= _turn_ctx.get(_tid, 0):
                                            # Cost follows the max-input call of the turn.
                                            _turn_ctx[_tid] = _inp
                                            _turn_cost[_tid] = _by_int_cost.get(str(_tr[0]), 0.0)
                        # Compaction summariser calls: this session's folds (old
                        # rows predating session-linkage have session_id NULL —
                        # match those by time window against the notice instead).
                        for _r in _cdb_cur.execute(
                            "SELECT created_at, input_tokens, output_tokens, cost_usd "
                            "FROM usage_events "
                            "WHERE source = 'background:compact' "
                            "AND (session_id = ? OR session_id IS NULL) "
                            "ORDER BY created_at ASC, input_tokens DESC",
                            (session_id,),
                        ).fetchall():
                            if _r[1]:
                                _compact_rows.append((str(_r[0]), int(_r[1]), float(_r[3] or 0.0)))
                        # Same time-window fallback for the closer's historical
                        # Summary-lane calls (predate interaction linkage).
                        _overview_rows: list = []  # (created_at, input_tokens, cost_usd) asc
                        for _r in _cdb_cur.execute(
                            "SELECT created_at, input_tokens, output_tokens, cost_usd "
                            "FROM usage_events "
                            "WHERE source IN ('background:overview', 'background:summarizer', 'background:closer') "
                            "AND (session_id = ? OR session_id IS NULL) "
                            "ORDER BY created_at ASC, input_tokens DESC",
                            (session_id,),
                        ).fetchall():
                            if _r[1]:
                                _overview_rows.append((str(_r[0]), int(_r[1]), float(_r[3] or 0.0)))
                    finally:
                        _cdb_conn.close()
            for _i, _m in enumerate(messages):
                _src = _m.get("source") or ""
                _c = None
                _cost = None
                if _m.get("role") == "assistant":
                    _tid = str(_m.get("turn_id") or "")
                    _c = _turn_ctx.get(_tid)
                    if _c:
                        _cost = _turn_cost.get(_tid)
                elif _m.get("role") == "user":
                    # Inherit the context of the answer that consumed this prompt.
                    for _n in messages[_i + 1:]:
                        if _n.get("role") == "assistant":
                            _tid = str(_n.get("turn_id") or "")
                            _c = _turn_ctx.get(_tid)
                            if _c:
                                _cost = _turn_cost.get(_tid)
                                break
                elif _src in ("system:overview", "system:summary", "system:closer"):
                    _c = _overview_ctx.get(str(_m.get("id") or ""))
                    if _c:
                        _cost = _overview_cost.get(str(_m.get("id") or ""))
                    if not _c and _overview_rows:
                        # Historical fallback — nearest closer call at/before it.
                        _ts = str(_m.get("created_at") or "")
                        if _ts:
                            for _ct, _in, _cs in _overview_rows:
                                if _ct <= _ts:
                                    _c = _in
                                    _cost = _cs
                                else:
                                    break
                elif _src == "system:compaction" and _compact_rows:
                    # The fold's summariser prompt — nearest call at/before the notice.
                    _ts = str(_m.get("created_at") or "")
                    if _ts:
                        for _ct, _in, _cs in _compact_rows:
                            if _ct <= _ts:
                                _c = _in
                                _cost = _cs
                            else:
                                break
                if _c:
                    _m["context_tokens"] = _c
                if _cost:
                    _m["cost_usd"] = round(_cost, 6)
        except Exception:
            pass  # enrichment is best-effort — never fail the transcript

        # ── Durable run-state: is a turn in progress for this session? ──
        # Lets a cold/second device know to show the live indicator and where to
        # resume the WebSocket stream from, even after a server restart.
        run_info = None
        try:
            cur.execute(
                "SELECT status, turn_id, assistant_interaction_id, latest_session_seq, "
                "updated_at, current_op, stop_cause, resume_attempts, max_resume_attempts "
                "FROM session_runs WHERE session_id = ?",
                (session_id,)
            )
            r = cur.fetchone()
            if r:
                # A run parked in the auto-resume ladder is NOT finished: keep
                # the durable payload `active` so the chat re-lights its activity
                # bar (with the caller's "re-assessing…" note) and flag it so the
                # reconcile poll keeps that note armed until the revival fails.
                _recovering = _resume_pending({
                    "status": r[0], "stop_cause": r[6],
                    "resume_attempts": r[7], "max_resume_attempts": r[8],
                })
                run_info = {
                    "status": "resuming" if _recovering else r[0],
                    "active": r[0] == "running" or _recovering,
                    "recovering": _recovering,
                    "turn_id": r[1],
                    "assistant_interaction_id": r[2],
                    "latest_session_seq": r[3],
                    "updated_at": r[4],
                    "current_op": r[5],
                }
        except Exception:
            pass  # session_runs table not present (legacy/temp DB)
        messages = _filter_orphan_streaming_messages(messages, run_info)
        if run_info and run_info.get("active"):
            open_summaries = [m for m in messages if m.get("_activity_summary") and m.get("activity_open")]
            if open_summaries:
                max(open_summaries, key=lambda m: int(m.get("activity_end_seq") or 0))["activity_glimmer"] = True

        # The session's true latest seq — the live reconcile poll seeds its
        # cursor here so opening mid-history (an `around_id` window) doesn't make
        # it backfill every newer row; the gap is reached by scrolling down.
        max_session_seq = 0
        try:
            cur.execute("SELECT MAX(session_seq) FROM interactions WHERE session_id = ?", (session_id,))
            _r = cur.fetchone()
            if _r and _r[0] is not None:
                max_session_seq = _r[0]
        except Exception:
            pass

        # Most recent provider-reported prompt sent this session (the LAST chat
        # usage row's input_tokens — the actual context sent to the model, which
        # drops after compaction folds older turns into summary cars), plus
        # session totals, from the append-only usage ledger.
        # This is local-first in hybrid mode and replaces the misleading
        # transcript-character estimate and a second API request. Usage events
        # live in the CONTROL database (billing writes there via
        # get_control_db()), not the per-user database — query that DB directly
        # so the session list shows real context / cost instead of zeros.
        context_tokens = 0
        context_model = ""
        context_created_at = ""
        usage = {"input_tokens": 0, "output_tokens": 0, "total_cost_usd": 0.0}
        _uid = requesting_user_id
        if _uid:
            try:
                from app.db import get_app_db
                _cdb = get_app_db()
                if hasattr(_cdb, "_get_conn"):
                    _cdb_conn = _cdb._get_conn()
                    try:
                        _cdb_cur = _cdb_conn.cursor()
                        _cdb_cur.execute(
                            "SELECT COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0), "
                            "COALESCE(SUM(cost_usd),0), "
                            "COALESCE(SUM(CASE WHEN cost_source='unknown' THEN 1 ELSE 0 END),0) "
                            "FROM usage_events "
                            "WHERE user_id = ? AND session_id = ?",
                            (_uid, session_id),
                        )
                        _r = _cdb_cur.fetchone()
                        if _r:
                            usage = {"input_tokens": int(_r[0] or 0), "output_tokens": int(_r[1] or 0),
                                     "total_cost_usd": float(_r[2] or 0.0),
                                     "has_unknown": bool(_r[3] and _r[3] > 0)}
                        _cdb_cur.execute(
                            "SELECT input_tokens, model, created_at FROM usage_events WHERE user_id = ? AND session_id = ? "
                            "AND source = 'chat' ORDER BY created_at DESC, id DESC LIMIT 1",
                            (_uid, session_id),
                        )
                        _r = _cdb_cur.fetchone()
                        if _r and _r[0] is not None:
                            context_tokens = int(_r[0] or 0)
                            context_model = str(_r[1] or "")
                            context_created_at = str(_r[2] or "")
                        # ── Background summariser work counts toward the session ──
                        # The compaction summariser and close-out-lane closer calls are REAL LLM
                        # calls made for this session, so their tokens belong in the
                        # session's totals/cost. New rows carry session_id+user_id
                        # (already summed above); rows that predate that linkage are
                        # UNLINKED (session_id NULL, user_id 'system') — credit them
                        # by time window: each system:compaction / system:closer / system:summary / system:overview
                        # message claims the nearest unlinked call at/before it,
                        # exactly the rule the per-message enrichment uses.
                        try:
                            _bg_in = _bg_out = 0
                            _bg_cost = 0.0
                            _bg_unknown = 0
                            for _bg_srcs, _notice_srcs in (
                                (("background:compact",), ("system:compaction",)),
                                (("background:overview", "background:summarizer", "background:closer"),
                                 ("system:overview", "system:summary", "system:closer")),
                            ):
                                _nph = ",".join("?" * len(_notice_srcs))
                                _notices = (
                                    str(r[0]) for r in cur.execute(
                                        "SELECT created_at FROM interactions "
                                        f"WHERE session_id=? AND source IN ({_nph}) "
                                        "ORDER BY created_at ASC",
                                        (session_id, *_notice_srcs)).fetchall())
                                _sph = ",".join("?" * len(_bg_srcs))
                                _rows = _cdb_cur.execute(
                                    "SELECT id, created_at, input_tokens, output_tokens, cost_usd, cost_source "
                                    f"FROM usage_events WHERE source IN ({_sph}) AND session_id IS NULL "
                                    "ORDER BY created_at ASC",
                                    (*_bg_srcs,),
                                ).fetchall()
                                # Credit the ONE nearest unlinked call at/before each
                                # notice (each fold/summary owns its own call), never
                                # consuming the whole unlinked pool — those rows are
                                # shared across all pre-linkage sessions.
                                _credited_ids = set()
                                for _ts in _notices:
                                    _best = None
                                    for _rr in _rows:
                                        if str(_rr[1]) <= _ts:
                                            if _rr[0] not in _credited_ids:
                                                _best = _rr
                                        else:
                                            break
                                    if _best is not None:
                                        _credited_ids.add(_best[0])
                                        _bg_in += int(_best[2] or 0)
                                        _bg_out += int(_best[3] or 0)
                                        _bg_cost += float(_best[4] or 0)
                                        if _best[5] == "unknown":
                                            _bg_unknown += 1
                            if _bg_in or _bg_out or _bg_cost:
                                usage["input_tokens"] = int(usage.get("input_tokens") or 0) + _bg_in
                                usage["output_tokens"] = int(usage.get("output_tokens") or 0) + _bg_out
                                usage["total_cost_usd"] = round(float(usage.get("total_cost_usd") or 0) + _bg_cost, 6)
                                if _bg_unknown:
                                    usage["has_unknown"] = True
                        except Exception:
                            pass
                    finally:
                        _cdb_conn.close()
            except Exception:
                pass

        # Footer context is provider usage only. Compaction notices carry a
        # history estimate, not a post-compaction provider measurement.

        # Older/local usage rows can carry the exact provider token count while
        # leaving their model column blank. The assistant interaction written by
        # that same provider call records the actual model in metadata, so use
        # the newest such row as the label fallback. This keeps the composer
        # truthful after model switches without falling back to today's agent
        # configuration for a historical session.
        if context_tokens and not context_model:
            try:
                _model_rows = cur.execute(
                    "SELECT metadata FROM interactions "
                    "WHERE session_id = ? AND role = 'assistant' "
                    "ORDER BY created_at DESC, rowid DESC LIMIT 20",
                    (session_id,),
                ).fetchall()
                for _mr in _model_rows or []:
                    _meta = _mr[0]
                    if isinstance(_meta, str):
                        _meta = json.loads(_meta or "{}")
                    if isinstance(_meta, dict) and _meta.get("model"):
                        context_model = str(_meta["model"])
                        break
            except Exception:
                pass

        # ── In-process gate queue state (backup source for the queued bubble) ──
        # The durable status='queued' marker (written by _run_turn_background)
        # is the source of truth across reloads; this annotation is the
        # in-memory BACKUP that (a) covers the brief window before the DB write
        # lands and (b) supplies LIVE position/total from the gate. Mirrors the
        # session-list enrichment. When queued, the session's most recent user
        # row is flagged so the client re-applies the queued bubble + Force run
        # button even if the durable write is absent or was already cleared.
        queue = None
        try:
            from app.agent.session_gate import queue_info as _gate_qi
            _qi = _gate_qi(session_id)
            if _qi:
                queue = {"position": _qi.get("position"), "total": _qi.get("total")}
        except Exception:
            pass
        if queue:
            for _m in reversed(messages):
                if _m.get("role") == "user":
                    _m["status"] = "queued"
                    _m["queue_position"] = queue["position"]
                    _m["queue_total"] = queue["total"]
                    break

        # Legacy rows remain raw until an explicit background repair persists their summaries.
        # The request path never deserializes their historical tool payloads.

        conn.close()
        return {
            "messages": messages,
            "queue": queue,
            "session_id": session_id,
            "db": db,
            "run": run_info,
            "has_more": has_more,
            "has_newer": has_newer,
            "turn_boundary_complete": turn_boundary_complete,
            "turn_boundary_truncated": turn_boundary_truncated,
            "light": bool(light),
            "max_session_seq": max_session_seq,
            "context_tokens": context_tokens,
            "context_model": context_model,
            "usage": usage,
            "execution_mode": _session_exec_mode,
            "remote_executor": _session_remote_exec,
            "timestamp_order_recovery": _timestamp_order_recovery,
            "timestamp_order_recovery_enabled": _timestamp_order_recovery_enabled,
            "timestamp_order_recovery_threshold_seconds": _timestamp_order_recovery_threshold_seconds,
            "manifest": manifest,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/session-turn-detail")
async def get_session_turn_detail(
    request: Request,
    session_id: str = Query(..., description="Session ID (for the participant check)"),
    ids: str = Query(..., description="Comma-separated assistant interaction ids to expand"),
    db: str = Query("user.db", description="Database filename"),
):
    """Full tool-call bodies for specific assistant turns, loaded on demand.

    The chat transcript opens in ``light`` mode (tool-call bodies blanked — see
    /session-messages). When the user expands a tool-call panel, the frontend
    calls this for just the assistant ids in that panel and gets back, per id,
    the full LLM ``output``/``metadata`` plus the child tool rows'
    ``content``/``output``/``metadata`` to populate the body. This keeps the
    rarely-viewed heavy payload off the initial load entirely.
    """
    resolved_db = _resolve_session_db(session_id, db)
    if resolved_db != db:
        db = resolved_db
    try:
        conn, _dialect = _open(db)
        cur = conn.cursor()
        _tb = "rowid" if _dialect == "sqlite" else "id"

        # Participant gate — identical to /session-messages.
        _token = ""
        _auth_header = request.headers.get("Authorization", "")
        if _auth_header.startswith("Bearer "):
            _token = _auth_header[7:]
        if not _token:
            _token = request.query_params.get("token", "")
        _payload = decode_token(_token) if _token else None
        requesting_user_id = _payload.get("user_id") if _payload else None
        requesting_username = _payload.get("sub") if _payload else None
        requester_identities = {v for v in (requesting_user_id, requesting_username) if v}
        try:
            cur.execute("SELECT user_id, participants FROM sessions WHERE id = ?", (session_id,))
            session_row = cur.fetchone()
            if session_row:
                owner_id = session_row[0]
                try:
                    participants = json.loads(session_row[1] or "[]")
                except (json.JSONDecodeError, TypeError):
                    participants = []
                participant_ids = {p.get("id") for p in participants if isinstance(p, dict)}
                is_authorized = bool(requester_identities) and bool(
                    requester_identities & ({owner_id} | participant_ids)
                )
                if not is_authorized and not _is_open_access_mode():
                    conn.close()
                    return {"details": {}, "session_id": session_id, "db": db, "restricted": True}
        except Exception:
            pass  # No sessions table — fall through

        id_list = [s for s in (ids or "").split(",") if s]
        details = {}
        needs_merge = []  # ids whose fat output may be split-local (hybrid)
        for aid in id_list:
            try:
                cur.execute(
                    "SELECT output, metadata FROM interactions WHERE id = ? AND session_id = ?",
                    (aid, session_id),
                )
                r = cur.fetchone()
                if not r:
                    continue
                tools = []
                try:
                    cur.execute(
                        "SELECT id, tool_name, content, output, metadata FROM interactions "
                        f"WHERE parent_id = ? AND session_id = ? AND (status IS NULL OR status != 'deleted') ORDER BY created_at ASC, {_tb} ASC",
                        (aid, session_id),
                    )
                    for tr in cur.fetchall():
                        tools.append({
                            "id": tr[0],
                            "tool_name": tr[1],
                            "content": tr[2],
                            "output": tr[3],
                            "metadata": tr[4],
                        })
                        if tr[3] is None:
                            needs_merge.append(tr[0])
                except Exception:
                    pass
                details[aid] = {"output": r[0], "metadata": r[1], "tools": tools}
                if r[0] is None:
                    needs_merge.append(aid)
            except Exception:
                continue

        # Hybrid split: the fat output for newer rows lives in the LOCAL
        # side-table, not on the remote skeleton this viewer just queried. Restore
        # it so the admin sees the full payloads. Strict no-op when hybrid is off.
        try:
            from app.db.hybrid import hybrid_enabled, load_local_payloads_sync
            if needs_merge and hybrid_enabled():
                payloads = load_local_payloads_sync(needs_merge)
                if payloads:
                    for _aid, _d in details.items():
                        p = payloads.get(_aid)
                        if p:
                            if _d.get("output") is None and p.get("output") is not None:
                                _d["output"] = p["output"]
                        for _t in _d.get("tools", []):
                            tp = payloads.get(_t.get("id"))
                            if tp and _t.get("output") is None and tp.get("output") is not None:
                                _t["output"] = tp["output"]
        except Exception:
            pass

        conn.close()
        return {"details": details, "session_id": session_id, "db": db}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/session-activity-group-detail")
def get_session_activity_group_detail(
    request: Request,
    session_id: str = Query(..., description="Session ID for the participant check"),
    activity_group_id: Optional[str] = Query(None, description="Durable activity summary id"),
    start_seq: Optional[int] = Query(None, ge=0, description="Inclusive legacy activity segment sequence"),
    end_seq: Optional[int] = Query(None, ge=0, description="Inclusive legacy activity segment sequence"),
    db: str = Query("user.db", description="Database filename"),
):
    """Return light descriptors for one previously collapsed activity group.

    This endpoint intentionally returns neither tool arguments nor tool result
    bodies. Individual rows continue to use ``/session-tool-detail`` for those
    sensitive/heavy disclosures after the group itself is expanded.
    """
    resolved_db = _resolve_session_db(session_id, db)
    if resolved_db != db:
        db = resolved_db
    try:
        conn, dialect = _open_read(db)
        cur = conn.cursor()
        if _session_access_ok(cur, session_id, request, None) is False:
            conn.close()
            return {"entries": [], "session_id": session_id, "db": db, "restricted": True}
        if isinstance(activity_group_id, str) and activity_group_id:
            summary = cur.execute(
                "SELECT start_seq, end_seq FROM session_activity_summaries "
                "WHERE session_id=? AND (group_id=? OR 'activity-summary:' || first_interaction_id=?)",
                (session_id, activity_group_id, activity_group_id),
            ).fetchone()
            if summary is None:
                logger.warning("Activity detail summary missing: session=%s group=%s", session_id, activity_group_id)
                conn.close()
                raise HTTPException(status_code=404, detail="Activity group not found")
            start_seq, end_seq = int(summary[0]), int(summary[1])
        elif start_seq is None or end_seq is None:
            conn.close()
            raise HTTPException(status_code=400, detail="activity_group_id or sequence range is required")
        if end_seq < start_seq:
            conn.close()
            raise HTTPException(status_code=400, detail="Invalid activity sequence range")
        status_col = "status"
        try:
            columns = {r[1] for r in cur.execute("PRAGMA table_info(interactions)").fetchall()}
            if "status" not in columns:
                status_col = "'complete' AS status"
        except Exception:
            pass
        order = "rowid" if dialect == "sqlite" else "id"
        cur.execute(
            f"SELECT id, session_id, role, content, tool_name, created_at, {status_col}, "
            "session_seq, output, metadata, parent_id, source, turn_id, turn_seq "
            "FROM interactions WHERE session_id = ? AND session_seq >= ? AND session_seq <= ? "
            f"ORDER BY session_seq ASC, created_at ASC, {order} ASC",
            (session_id, start_seq, end_seq),
        )
        messages = []
        for row in cur.fetchall():
            output = row[8]
            if row[2] == "assistant" and output:
                try:
                    parsed = json.loads(output)
                    calls = parsed.get("tool_calls") or [] if isinstance(parsed, dict) else []
                    output = json.dumps({"tool_calls": [
                        {"id": c.get("id"), "function": {"name": (c.get("function") or {}).get("name"), "arguments": ""}}
                        for c in calls if isinstance(c, dict)
                    ]}) if calls else None
                except (TypeError, ValueError, json.JSONDecodeError):
                    output = None
            metadata = row[9]
            if row[2] == "tool":
                content = ""
                output = None
                try:
                    parsed_meta = json.loads(metadata or "{}")
                    metadata = json.dumps({key: parsed_meta[key] for key in ("duration_ms", "error", "brain", "skipped", "message_phase", "message_type") if key in parsed_meta})
                except (TypeError, ValueError, json.JSONDecodeError):
                    metadata = None
            else:
                content = row[3]
            messages.append({
                "id": row[0], "session_id": row[1], "role": row[2], "content": content,
                "tool_name": row[4], "created_at": row[5], "status": row[6],
                "session_seq": row[7], "interaction_seq": row[7], "output": output,
                "metadata": metadata, "parent_id": row[10], "source": row[11],
                "turn_id": row[12], "turn_seq": row[13],
            })
        # This endpoint is a semantic projection, not a raw transcript. The
        # folded heading counts durable tool calls and messages, so opening it
        # receives those same units rather than assistant/result rows.
        results_by_parent = {}
        for message in messages:
            if message["role"] == "tool" and message.get("parent_id"):
                results_by_parent.setdefault(message["parent_id"], []).append(message)

        def _metadata(value):
            try:
                parsed = json.loads(value or "{}")
                return parsed if isinstance(parsed, dict) else {}
            except (TypeError, ValueError, json.JSONDecodeError):
                return {}

        entries = []
        paired_tool_ids = set()
        for message in messages:
            role = message["role"]
            if role == "assistant":
                calls = []
                try:
                    parsed = json.loads(message.get("output") or "{}")
                    calls = parsed.get("tool_calls") or [] if isinstance(parsed, dict) else []
                except (TypeError, ValueError, json.JSONDecodeError):
                    pass
                results = results_by_parent.get(message["id"], [])
                for index, call in enumerate(calls):
                    if not isinstance(call, dict):
                        continue
                    function = call.get("function") or {}
                    name = function.get("name") or "(unknown)"
                    result = next((item for item in results
                                   if item["id"] not in paired_tool_ids and item.get("tool_name") == name), None)
                    if result is None:
                        result = next((item for item in results if item["id"] not in paired_tool_ids), None)
                    if result is not None:
                        paired_tool_ids.add(result["id"])
                    result_meta = _metadata(result.get("metadata")) if result else {}
                    entries.append({
                        "kind": "tool", "tool": name, "toolCallId": call.get("id"),
                        "args": {}, "status": "deleted" if result and result.get("status") == "deleted" else "done",
                        "result": None, "durationMs": result_meta.get("duration_ms"),
                        "errorType": None, "turn": 0, "open": False,
                        "_detailMsgId": message["id"], "_detailIdx": index,
                        "_needsDetail": True, "interactionSeq": message["session_seq"],
                    })
                if str(message.get("content") or "").strip():
                    entries.append({
                        "kind": "progress", "id": message["id"],
                        "content": message["content"], "createdAt": message["created_at"],
                        "interactionSeq": message["session_seq"],
                    })
                elif not calls:
                    entries.append({
                        "kind": "system", "notice": "activity", "id": message["id"],
                        "label": "Assistant activity update", "content": "",
                        "counted": False, "createdAt": message["created_at"],
                        "interactionSeq": message["session_seq"],
                    })
            elif role == "system":
                source = message.get("source") or ""
                notice = "compaction" if source == "system:compaction" else None
                entries.append({
                    "kind": "system", "notice": notice, "id": message["id"],
                    "label": "Compaction details" if notice else "System update",
                    "content": message.get("content") or "", "createdAt": message["created_at"],
                    "interactionSeq": message["session_seq"],
                })
            elif role == "tool" and message["id"] not in paired_tool_ids:
                meta = _metadata(message.get("metadata"))
                parent = next((item for item in messages if item["id"] == message.get("parent_id")), None)
                parent_role = parent.get("role") if parent else ""
                synthetic = (
                    message.get("tool_name") in {"process_image", "route_attachment", "app_control"}
                    and parent_role == "user"
                ) or (
                    message.get("tool_name") == "resume_session" and message.get("source") == "system:recovery"
                ) or (
                    message.get("tool_name") in {"memory_search", "memory_save"}
                    and bool(meta.get("brain")) and not bool(meta.get("skipped"))
                )
                if not synthetic:
                    entries.append({
                        "kind": "system", "notice": "activity", "id": message["id"],
                        "label": ("Skipped" if meta.get("skipped") else "Uncounted")
                            + " tool: " + (message.get("tool_name") or "tool"),
                        "content": "", "counted": False, "createdAt": message["created_at"],
                        "interactionSeq": message["session_seq"],
                    })
                    continue
                entries.append({
                    "kind": "tool", "tool": message.get("tool_name") or "tool",
                    "args": {}, "status": "error" if meta.get("error") else "done",
                    "result": None, "durationMs": meta.get("duration_ms"),
                    "errorType": None, "turn": 0, "open": False,
                    "_detailToolId": message["id"], "_detailIdx": 0,
                    "_needsDetail": True, "interactionSeq": message["session_seq"],
                })
        conn.close()
        return {"entries": entries, "session_id": session_id, "db": db}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Activity detail projection failed: session=%s group=%s", session_id, activity_group_id)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/session-tool-detail")
def get_session_tool_detail(
    request: Request,
    session_id: str = Query(..., description="Session ID"),
    assistant_id: Optional[str] = Query(None, description="Assistant interaction owning the call"),
    tool_index: int = Query(0, ge=0, description="Zero-based call index within the assistant output"),
    tool_id: Optional[str] = Query(None, description="Synthetic standalone tool interaction id"),
    user_id: Optional[str] = Query(None, description="Local-user identity fallback"),
    db: str = Query("user.db", description="Database filename"),
):
    """Return exactly one tool call's arguments/result.

    Transcript cache rows contain disclosure descriptors only. This endpoint is
    intentionally per-call so expanding one row never transfers sibling calls
    or persists their potentially large arguments/results in the browser cache.
    """
    resolved_db = _resolve_session_db(session_id, db)
    if resolved_db != db:
        db = resolved_db
    try:
        conn, dialect = _open(db)
        cur = conn.cursor()
        if _session_access_ok(cur, session_id, request, user_id) is False:
            conn.close()
            return {"detail": None, "session_id": session_id, "db": db, "restricted": True}
        tie_breaker = "rowid" if dialect == "sqlite" else "id"

        if tool_id:
            cur.execute(
                "SELECT id, tool_name, content, output, metadata FROM interactions "
                "WHERE id = ? AND session_id = ? AND role = 'tool'",
                (tool_id, session_id),
            )
            row = cur.fetchone()
            conn.close()
            if not row:
                return {"detail": None, "session_id": session_id, "db": db}
            args = None
            try:
                meta = json.loads(row[4]) if row[4] else {}
                args = meta.get("args") if isinstance(meta, dict) else None
            except Exception:
                pass
            return {
                "detail": {
                    "tool_id": row[0], "tool_name": row[1], "arguments": args,
                    "content": row[2], "output": row[3], "metadata": row[4],
                },
                "session_id": session_id, "db": db,
            }

        if not assistant_id:
            conn.close()
            raise HTTPException(status_code=400, detail="assistant_id or tool_id is required")
        cur.execute(
            "SELECT output FROM interactions WHERE id = ? AND session_id = ? AND role = 'assistant'",
            (assistant_id, session_id),
        )
        assistant = cur.fetchone()
        if not assistant:
            conn.close()
            return {"detail": None, "session_id": session_id, "db": db}
        assistant_output = assistant[0]
        if assistant_output is None:
            try:
                from app.db.hybrid import hybrid_enabled, load_local_payloads_sync
                if hybrid_enabled():
                    payload = load_local_payloads_sync([assistant_id]).get(assistant_id) or {}
                    assistant_output = payload.get("output")
            except Exception:
                pass
        calls = []
        try:
            parsed = json.loads(assistant_output) if assistant_output else {}
            calls = parsed.get("tool_calls") or []
        except Exception:
            calls = []
        call = calls[tool_index] if tool_index < len(calls) else None
        function = (call or {}).get("function") or {}
        tool_name = function.get("name")
        cur.execute(
            "SELECT id, tool_name, content, output, metadata FROM interactions "
            f"WHERE parent_id = ? AND session_id = ? AND role = 'tool' "
            f"AND (status IS NULL OR status != 'deleted') ORDER BY created_at ASC, {tie_breaker} ASC",
            (assistant_id, session_id),
        )
        tool_rows = cur.fetchall()
        matching = [row for row in tool_rows if row[1] == tool_name]
        result = matching[0] if len(matching) == 1 else (
            tool_rows[tool_index] if tool_index < len(tool_rows) else None
        )
        result_output = result[3] if result else None
        if result and result_output is None:
            try:
                from app.db.hybrid import hybrid_enabled, load_local_payloads_sync
                if hybrid_enabled():
                    payload = load_local_payloads_sync([result[0]]).get(result[0]) or {}
                    result_output = payload.get("output")
            except Exception:
                pass
        conn.close()
        return {
            "detail": {
                "assistant_id": assistant_id,
                "tool_index": tool_index,
                "tool_call_id": (call or {}).get("id"),
                "tool_name": tool_name,
                "arguments": function.get("arguments"),
                "tool_id": result[0] if result else None,
                "content": result[2] if result else None,
                "output": result_output,
                "metadata": result[4] if result else None,
            },
            "session_id": session_id, "db": db,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/session-tail")
def get_session_tail(
    request: Request,
    session_id: str = Query(..., description="Session ID"),
    after_session_seq: int = Query(0, description="Return only interactions with session_seq greater than this"),
    user_id: Optional[str] = Query(None, description="Active client identity — fallback when no JWT (local users)"),
    db: str = Query("user.db", description="Database filename"),
):
    """Incremental tail of a session's interactions for the live DB-reconcile path.

    Returns only interactions whose ``session_seq > after_session_seq`` (ascending),
    PLUS the in-progress streaming assistant row, PLUS the durable ``run`` object.

    This is the cheap poll the chat UI runs (gated on WebSocket silence) to stream
    a reply when the live WebSocket and the agent run are on DIFFERENT server
    processes/workers — the DB is the shared source of truth, so this path is
    correct regardless of process topology. The active streaming row is unioned
    in explicitly because its ``session_seq`` is assigned once at insert and does
    NOT advance as its content is updated every ~0.6s — a ``session_seq > after``
    filter alone would return it once and then miss its GROWING text. Mirrors the
    row shape of ``/session-messages`` so the frontend uses one renderer.
    """
    resolved_db = _resolve_session_db(session_id, db)
    if resolved_db != db:
        db = resolved_db
    try:
        # This endpoint is polled as often as every 800 ms, so it is intentionally
        # restricted to the local SQLite mirror. Never reconcile or fall back to
        # Postgres from this hot path.
        conn = _open_local_sqlite(db)
        _dialect = "sqlite"
        cur = conn.cursor()

        # Same participant gate as /session-messages (False = deny; None/True = proceed).
        if _session_access_ok(cur, session_id, request, user_id) is False:
            conn.close()
            return {"messages": [], "session_id": session_id, "db": db, "run": None, "restricted": True}

        # `status` may not exist on very old DBs — use the process-local
        # capability cache rather than probing schema on every hot-tail poll.
        _status_col = "status" if _session_tail_has_status(cur, db) else "'complete' AS status"
        _cols = (f'id, session_id, role, content, tool_name, created_at, {_status_col}, '
                 f'session_seq, output, metadata, parent_id, source, turn_id, turn_seq, chat_render_kind')

        def _row_to_msg(row):
            m = {
                "id": row[0], "session_id": row[1], "role": row[2], "content": row[3],
                "tool_name": row[4], "created_at": row[5], "status": row[6],
                "session_seq": row[7], "output": row[8], "metadata": row[9],
                "parent_id": row[10], "source": row[11] if len(row) > 11 else None,
                "turn_id": row[12] if len(row) > 12 else None,
                "turn_seq": row[13] if len(row) > 13 else None,
                "chat_render_kind": row[14] if len(row) > 14 else "activity",
            }
            m["interaction_seq"] = m["session_seq"]
            return m

        messages = []
        seen_ids = set()
        try:
            cur.execute(
                f'SELECT {_cols} FROM interactions '
                f'WHERE session_id = ? AND session_seq IS NOT NULL AND session_seq > ? '
                f'ORDER BY session_seq ASC LIMIT 200',
                (session_id, after_session_seq),
            )
            for row in cur.fetchall():
                if _is_loser_row(row[9]):
                    continue
                m = _row_to_msg(row)
                messages.append(m)
                seen_ids.add(m["id"])
        except Exception:
            pass

        # Durable run-state: is a turn in progress for this session?
        run_info = None
        try:
            cur.execute(
                "SELECT status, turn_id, assistant_interaction_id, latest_session_seq, "
                "updated_at, current_op, stop_cause, resume_attempts, max_resume_attempts "
                "FROM session_runs WHERE session_id = ?",
                (session_id,),
            )
            r = cur.fetchone()
            if r:
                # See get_session_messages: a run inside the recovery ladder
                # reads as active + recovering so the open chat holds its
                # "re-assessing…" note instead of settling to idle.
                _recovering = _resume_pending({
                    "status": r[0], "stop_cause": r[6],
                    "resume_attempts": r[7], "max_resume_attempts": r[8],
                })
                run_info = {
                    "status": "resuming" if _recovering else r[0],
                    "active": r[0] == "running" or _recovering,
                    "recovering": _recovering,
                    "turn_id": r[1],
                    "assistant_interaction_id": r[2],
                    "latest_session_seq": r[3],
                    "updated_at": r[4],
                    "current_op": r[5],
                }
        except Exception:
            pass  # session_runs table not present (legacy/temp DB)
        messages = _filter_orphan_streaming_messages(messages, run_info)
        seen_ids = {message.get("id") for message in messages}

        # Union the in-progress streaming row (see docstring) so its growing text
        # rides every poll even though its session_seq never advances.
        _asst = run_info.get("assistant_interaction_id") if run_info else None
        if _asst and _asst not in seen_ids:
            try:
                cur.execute(f'SELECT {_cols} FROM interactions WHERE id = ?', (_asst,))
                row = cur.fetchone()
                if row:
                    messages.append(_row_to_msg(row))
            except Exception:
                pass

        conn.close()
        return {"messages": messages, "session_id": session_id, "db": db, "run": run_info}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def _session_access_ok(cur, session_id: str, request: Request, user_id: Optional[str] = None):
    """Return True if the requester owns / participates in the session, False if
    they clearly don't, or None when there's no session row to check against
    (legacy / temp DB) — in which case the caller should fall through, exactly
    like get_session_messages does. Mirrors that endpoint's auth logic.

    ``user_id`` is the active client identity (``app.currentUserId``) passed as a
    query param. It's accepted as a fallback identity when there's no matching
    JWT — exactly like get_user_sessions does — so unauthenticated local UUID
    users (and sessions created from the TUI / launcher under ``admin``)
    can manage their own rows. The session row's owner / participant list is
    still the gate: a claimed ``user_id`` only authorizes when it actually
    matches the session, so it can't widen access to other users' sessions."""
    _token = ""
    _auth_header = request.headers.get("Authorization", "")
    if _auth_header.startswith("Bearer "):
        _token = _auth_header[7:]
    if not _token:
        _token = request.query_params.get("token", "")
    _payload = decode_token(_token) if _token else None
    requesting_user_id = _payload.get("user_id") if _payload else None
    requesting_username = _payload.get("sub") if _payload else None
    requester_identities = {v for v in (requesting_user_id, requesting_username, user_id) if v}
    try:
        cur.execute(
            "SELECT user_id, participants FROM sessions WHERE id = ?",
            (session_id,),
        )
        session_row = cur.fetchone()
    except Exception:
        return None  # no sessions table — fall through
    if not session_row:
        return None  # unknown / anonymous session — fall through
    owner_id = session_row[0]
    participants_raw = session_row[1] or "[]"
    try:
        participants = json.loads(participants_raw)
    except (json.JSONDecodeError, TypeError):
        participants = []
    participant_ids = {p.get("id") for p in participants if isinstance(p, dict)}
    if _is_open_access_mode():
        return True  # open mode: middleware already trusts the request as admin
    return bool(requester_identities) and bool(
        requester_identities & ({owner_id} | participant_ids)
    )


@router.delete("/turn")
async def delete_turn(
    request: Request,
    session_id: str = Query(..., description="Session ID"),
    interaction_id: str = Query(..., description="Any interaction id within the turn to delete"),
    user_id: Optional[str] = Query(None, description="Active client identity — fallback when no JWT (local users)"),
    permanent: bool = Query(False, description="Hard-delete instead of recycling"),
    db: str = Query("user.db", description="Database filename"),
):
    """Delete one whole conversation turn from `interactions`.

    A "turn" is the **parent-chain closure rooted at the user message** that
    started it: the user row (``parent_id IS NULL``) plus every assistant step,
    tool call and memory write that descended from it.

    Default (permanent=false): soft-deletes by setting status='deleted'.  Rows
    stay in the transcript (struck through) but are excluded from the agent
    context.  With ``permanent=true`` the rows are hard-deleted from the table
    and are gone for good.

    ``interaction_id`` may be any row in the turn (the clicked bubble's id); the
    server walks up to the root itself."""
    resolved_db = _resolve_session_db(session_id, db)
    db_path = _get_db_path(resolved_db)
    try:
        conn, _dialect = _open(db)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        access = _session_access_ok(cur, session_id, request, user_id)
        if access is False:
            conn.close()
            raise HTTPException(status_code=403, detail="Not a participant in this session")

        rows = cur.execute(
            "SELECT id, parent_id, turn_id FROM interactions WHERE session_id = ?",
            (session_id,),
        ).fetchall()
        by_id = {r["id"]: r for r in rows}
        children: dict = {}
        for r in rows:
            children.setdefault(r["parent_id"], []).append(r["id"])

        # ── Resolve the turn root: walk parent_id up to the user message. ──
        # User rows have parent_id = NULL; every other row in the turn descends
        # from one. A visited set guards against any accidental cycle.
        cur_id = interaction_id
        visited = set()
        while (cur_id in by_id and by_id[cur_id]["parent_id"]
               and by_id[cur_id]["parent_id"] in by_id and cur_id not in visited):
            visited.add(cur_id)
            cur_id = by_id[cur_id]["parent_id"]
        root = cur_id

        # ── Collect the whole descendant tree from the root (BFS/DFS). ──
        to_delete = set()
        stack = [root]
        while stack:
            n = stack.pop()
            if n in to_delete:
                continue
            to_delete.add(n)
            stack.extend(children.get(n, []))

        # Belt-and-suspenders: also any row explicitly tagged with this turn_id
        # (future-proofs the day assistant/tool rows start carrying turn_id).
        for r in rows:
            if r["turn_id"] and r["turn_id"] == root:
                to_delete.add(r["id"])

        # Only ever delete ids that are real rows in THIS session, so a stale or
        # foreign interaction_id can never widen the blast radius.
        to_delete = {i for i in to_delete if i in by_id}
        if not to_delete:
            conn.close()
            return {"deleted_ids": [], "count": 0, "turn_root": root, "session_id": session_id}

        if permanent:
            cur.executemany(
                "DELETE FROM interactions WHERE id = ?",
                [(i,) for i in to_delete],
            )
        else:
            cur.executemany(
                "UPDATE interactions SET status = 'deleted' WHERE id = ?",
                [(i,) for i in to_delete],
            )
        conn.commit()
        conn.close()
        return {
            "deleted_ids": list(to_delete),
            "count": len(to_delete),
            "turn_root": root,
            "session_id": session_id,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/interaction")
async def delete_interaction(
    request: Request,
    session_id: str = Query(..., description="Session ID"),
    interaction_id: str = Query(..., description="The interaction id to delete"),
    include_children: bool = Query(False, description="Also delete direct tool-result children"),
    user_id: Optional[str] = Query(None, description="Active client identity — fallback when no JWT (local users)"),
    permanent: bool = Query(False, description="Hard-delete instead of recycling"),
    db: str = Query("user.db", description="Database filename"),
):
    """Delete ONE interaction row (not a whole turn).

    Default (permanent=false): soft-deletes by setting status='deleted'.
    With ``permanent=true`` the row is hard-deleted.

    When ``include_children=true``, direct tool-result child rows
    (role='tool' with parent_id = interaction_id) are also deleted."""
    resolved_db = _resolve_session_db(session_id, db)
    db_path = _get_db_path(resolved_db)
    try:
        conn, _dialect = _open(db)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        access = _session_access_ok(cur, session_id, request, user_id)
        if access is False:
            conn.close()
            raise HTTPException(status_code=403, detail="Not a participant in this session")

        # Verify the interaction exists in this session
        row = cur.execute(
            "SELECT id FROM interactions WHERE id = ? AND session_id = ?",
            (interaction_id, session_id),
        ).fetchone()
        if not row:
            conn.close()
            raise HTTPException(status_code=404, detail="Interaction not found")

        to_delete = {interaction_id}

        if include_children:
            child_rows = cur.execute(
                "SELECT id FROM interactions WHERE parent_id = ? AND role = 'tool' AND session_id = ?",
                (interaction_id, session_id),
            ).fetchall()
            for cr in child_rows:
                to_delete.add(cr["id"])

        if permanent:
            cur.executemany(
                "DELETE FROM interactions WHERE id = ?",
                [(i,) for i in to_delete],
            )
        else:
            cur.executemany(
                "UPDATE interactions SET status = 'deleted' WHERE id = ?",
                [(i,) for i in to_delete],
            )
        conn.commit()
        conn.close()
        return {
            "deleted_ids": list(to_delete),
            "count": len(to_delete),
            "interaction_id": interaction_id,
            "session_id": session_id,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/tool-call")
async def delete_tool_call(
    request: Request,
    session_id: str = Query(..., description="Session ID"),
    interaction_id: str = Query(..., description="Assistant interaction id whose tool_call to remove"),
    tool_call_idx: int = Query(..., description="Index of the tool_call in output.tool_calls[] to remove"),
    user_id: Optional[str] = Query(None, description="Active client identity — fallback when no JWT (local users)"),
    permanent: bool = Query(False, description="Hard-delete instead of recycling"),
    db: str = Query("user.db", description="Database filename"),
):
    """Remove a single tool call from context.

    Default (permanent=false): soft-deletes the tool-result child row by setting
    status='deleted'.  With ``permanent=true`` the row is hard-deleted."""
    db_path = _get_db_path(db)
    try:
        conn, _dialect = _open(db)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        access = _session_access_ok(cur, session_id, request, user_id)
        if access is False:
            conn.close()
            raise HTTPException(status_code=403, detail="Not a participant in this session")

        # ── Load the assistant interaction ──
        row = cur.execute(
            "SELECT id, output FROM interactions WHERE id = ? AND session_id = ?",
            (interaction_id, session_id),
        ).fetchone()
        if not row:
            conn.close()
            raise HTTPException(status_code=404, detail="Interaction not found")
        if not row["output"]:
            conn.close()
            raise HTTPException(status_code=400, detail="Interaction has no output")

        try:
            output = json.loads(row["output"])
        except json.JSONDecodeError:
            conn.close()
            raise HTTPException(status_code=400, detail="Invalid output JSON")

        tool_calls = output.get("tool_calls", [])
        if tool_call_idx < 0 or tool_call_idx >= len(tool_calls):
            conn.close()
            raise HTTPException(status_code=400, detail=f"tool_call_idx {tool_call_idx} out of range (0-{len(tool_calls)-1})")

        removed_call = tool_calls[tool_call_idx]
        removed_name = removed_call.get("function", {}).get("name", "?")

        # ── Find corresponding tool result child rows ──
        child_rows = cur.execute(
            "SELECT id, tool_name FROM interactions WHERE parent_id = ? AND role = 'tool' ORDER BY session_seq ASC, created_at ASC",
            (interaction_id,),
        ).fetchall()

        deleted_child_ids = []
        matched = False
        for cr in child_rows:
            if cr["tool_name"] == removed_name and not matched:
                deleted_child_ids.append(cr["id"])
                matched = True
                break

        if not matched and tool_call_idx < len(child_rows):
            deleted_child_ids.append(child_rows[tool_call_idx]["id"])

        for child_id in deleted_child_ids:
            if permanent:
                cur.execute("DELETE FROM interactions WHERE id = ?", (child_id,))
            else:
                cur.execute("UPDATE interactions SET status = 'deleted' WHERE id = ?", (child_id,))

        conn.commit()
        conn.close()

        return {
            "removed": True,
            "interaction_id": interaction_id,
            "tool_call_idx": tool_call_idx,
            "tool_name": removed_name,
            "deleted_child_ids": deleted_child_ids,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/turn/restore")
async def restore_turn(
    request: Request,
    session_id: str = Query(..., description="Session ID"),
    interaction_id: str = Query(..., description="Any interaction id within the turn to restore"),
    user_id: Optional[str] = Query(None, description="Active client identity — fallback when no JWT (local users)"),
    db: str = Query("user.db", description="Database filename"),
):
    """Restore a soft-deleted turn back to active.

    Walks the same parent tree as delete_turn and flips ``status = 'deleted'``
    back to ``'complete'`` for every row in the turn."""
    resolved_db = _resolve_session_db(session_id, db)
    try:
        conn, _dialect = _open(db)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        access = _session_access_ok(cur, session_id, request, user_id)
        if access is False:
            conn.close()
            raise HTTPException(status_code=403, detail="Not a participant in this session")

        rows = cur.execute(
            "SELECT id, parent_id, turn_id FROM interactions WHERE session_id = ?",
            (session_id,),
        ).fetchall()
        by_id = {r["id"]: r for r in rows}
        children: dict = {}
        for r in rows:
            children.setdefault(r["parent_id"], []).append(r["id"])

        # Walk up to the root user message
        cur_id = interaction_id
        visited = set()
        while (cur_id in by_id and by_id[cur_id]["parent_id"]
               and by_id[cur_id]["parent_id"] in by_id and cur_id not in visited):
            visited.add(cur_id)
            cur_id = by_id[cur_id]["parent_id"]
        root = cur_id

        # Collect the descendant tree
        to_restore = set()
        stack = [root]
        while stack:
            n = stack.pop()
            if n in to_restore:
                continue
            to_restore.add(n)
            stack.extend(children.get(n, []))

        for r in rows:
            if r["turn_id"] and r["turn_id"] == root:
                to_restore.add(r["id"])

        to_restore = {i for i in to_restore if i in by_id}
        if not to_restore:
            conn.close()
            return {"restored_ids": [], "count": 0, "turn_root": root, "session_id": session_id}

        cur.executemany(
            "UPDATE interactions SET status = 'complete' WHERE id = ? AND status = 'deleted'",
            [(i,) for i in to_restore],
        )
        conn.commit()
        conn.close()
        return {
            "restored_ids": list(to_restore),
            "count": len(to_restore),
            "turn_root": root,
            "session_id": session_id,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/interaction/restore")
async def restore_interaction(
    request: Request,
    session_id: str = Query(..., description="Session ID"),
    interaction_id: str = Query(..., description="The interaction id to restore"),
    include_children: bool = Query(False, description="Also restore direct tool-result children"),
    user_id: Optional[str] = Query(None, description="Active client identity — fallback when no JWT (local users)"),
    db: str = Query("user.db", description="Database filename"),
):
    """Restore a single soft-deleted interaction back to active.

    Flips ``status = 'deleted'`` back to ``'complete'`` for the interaction
    row. When ``include_children=true``, direct tool-result children are
    also restored."""
    resolved_db = _resolve_session_db(session_id, db)
    try:
        conn, _dialect = _open(db)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        access = _session_access_ok(cur, session_id, request, user_id)
        if access is False:
            conn.close()
            raise HTTPException(status_code=403, detail="Not a participant in this session")

        row = cur.execute(
            "SELECT id FROM interactions WHERE id = ? AND session_id = ?",
            (interaction_id, session_id),
        ).fetchone()
        if not row:
            conn.close()
            raise HTTPException(status_code=404, detail="Interaction not found")

        to_restore = {interaction_id}

        if include_children:
            child_rows = cur.execute(
                "SELECT id FROM interactions WHERE parent_id = ? AND role = 'tool' AND session_id = ?",
                (interaction_id, session_id),
            ).fetchall()
            for cr in child_rows:
                to_restore.add(cr["id"])

        cur.executemany(
            "UPDATE interactions SET status = 'complete' WHERE id = ? AND status = 'deleted'",
            [(i,) for i in to_restore],
        )
        conn.commit()
        conn.close()
        return {
            "restored_ids": list(to_restore),
            "count": len(to_restore),
            "interaction_id": interaction_id,
            "session_id": session_id,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/tool-call/restore")
async def restore_tool_call(
    request: Request,
    session_id: str = Query(..., description="Session ID"),
    interaction_id: str = Query(..., description="Assistant interaction id whose tool_call to restore"),
    tool_call_idx: int = Query(..., description="Index of the tool_call in output.tool_calls[] to restore"),
    user_id: Optional[str] = Query(None, description="Active client identity — fallback when no JWT (local users)"),
    db: str = Query("user.db", description="Database filename"),
):
    """Restore a soft-deleted tool-call result row.

    Flips the matching tool-result child row's ``status`` from ``'deleted'``
    back to ``'complete'`` so it re-enters the agent context."""
    try:
        conn, _dialect = _open(db)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        access = _session_access_ok(cur, session_id, request, user_id)
        if access is False:
            conn.close()
            raise HTTPException(status_code=403, detail="Not a participant in this session")

        # Load the assistant interaction to get the tool call name
        row = cur.execute(
            "SELECT id, output FROM interactions WHERE id = ? AND session_id = ?",
            (interaction_id, session_id),
        ).fetchone()
        if not row or not row["output"]:
            conn.close()
            raise HTTPException(status_code=404, detail="Interaction not found")
        try:
            output = json.loads(row["output"])
        except json.JSONDecodeError:
            conn.close()
            raise HTTPException(status_code=400, detail="Invalid output JSON")

        tool_calls = output.get("tool_calls", [])
        if tool_call_idx < 0 or tool_call_idx >= len(tool_calls):
            conn.close()
            raise HTTPException(status_code=400, detail=f"tool_call_idx out of range")
        removed_name = tool_calls[tool_call_idx].get("function", {}).get("name", "?")

        child_rows = cur.execute(
            "SELECT id, tool_name FROM interactions WHERE parent_id = ? AND role = 'tool' ORDER BY session_seq ASC, created_at ASC",
            (interaction_id,),
        ).fetchall()

        restored_id = None
        for cr in child_rows:
            if cr["tool_name"] == removed_name:
                restored_id = cr["id"]
                break
        if not restored_id and tool_call_idx < len(child_rows):
            restored_id = child_rows[tool_call_idx]["id"]

        if restored_id:
            cur.execute(
                "UPDATE interactions SET status = 'complete' WHERE id = ? AND status = 'deleted'",
                (restored_id,),
            )
        conn.commit()
        conn.close()
        return {
            "restored": True,
            "interaction_id": interaction_id,
            "tool_call_idx": tool_call_idx,
            "tool_name": removed_name,
            "restored_id": restored_id,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def _build_session_stats_sync(
    user_id: str,
    db: str,
    status: str,
):
    """
    Return aggregated usage stats per session for a user.

    Parses interactions.metadata JSON to extract:
      - input_tokens, output_tokens (from assistant roles)
      - duration_ms (from assistant roles — LLM call wall time)
      - cost (from assistant roles, when available)
      - turn count, message count, last active
    """
    # This function runs in a dedicated ThreadPoolExecutor. Unlike
    # ``asyncio.to_thread()``, ``executor.submit()`` does not copy ContextVars
    # from the request task, so the worker otherwise inherits the DB layer's
    # default ``admin`` identity. Explicitly bind the requested (and already
    # authorized) owner before resolving ``user.db`` so guests and other
    # non-admin users read their own per-user database.
    from app.db.local import set_db_user_context
    set_db_user_context(user_id)

    try:
        # WHEN-CHANGE-SESSION-STATS-READ: session_stats reads from the local mirror
        # when hybrid is on — same as the chat-header dropdown (/sessions). Both
        # views must agree, so both read from _open_read, not _open (which routes
        # to Postgres). The sync puller keeps the local mirror current.
        conn, _dialect = _open_read(db)
        if isinstance(conn, sqlite3.Connection):
            conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # Fetch sessions for user — optionally filtered by status
        try:
            _status_filter = ""
            _params = [user_id]
            if status == "active":
                _status_filter = " AND (status IS NULL OR status = 'active')"
            elif status == "recycled":
                _status_filter = " AND status = 'recycled'"
            cur.execute(
                'SELECT id, title, created_at, updated_at, status, COALESCE(pinned, 0) AS pinned, sort_order '
                'FROM sessions WHERE user_id = ?' + _status_filter + ' ORDER BY updated_at DESC NULLS LAST',
                _params
            )
            session_rows = cur.fetchall()
        except Exception:
            session_rows = []

        sessions_map = {
            r["id"]: {
                "title": r["title"] or r["id"][:12],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
                "pinned": bool(r["pinned"]),
                "sort_order": r["sort_order"],
            }
            for r in session_rows
        }

        # If no sessions table rows, fall back to distinct session_ids from the
        # interactions log so an orphaned transcript (no `sessions` row) is still
        # shown and can be cleared. These "phantom" sessions have no recycled
        # state — so only surface them in the active/all views, never in the bin
        # (otherwise the same orphans would show in both, since this fallback has
        # no status to filter on). Deleting one hard-removes its interactions
        # (see delete_session), so it truly disappears rather than reappearing.
        if not sessions_map and status != "recycled":
            # Skip any session that DOES have a row marked recycled — e.g. a real
            # session that lives under a different user_id (so the query above
            # found nothing for THIS user) but was already recycled. Without this,
            # recycling such a session wouldn't stick: the fallback re-derives it
            # straight from the interactions log on every reload.
            recycled_ids: set = set()
            try:
                for r in cur.execute("SELECT id FROM sessions WHERE status = 'recycled'"):
                    recycled_ids.add(r[0])
            except Exception:
                pass
            try:
                cur.execute(
                    'SELECT DISTINCT session_id FROM interactions ORDER BY created_at DESC'
                )
                for row in cur.fetchall():
                    sid = row[0]
                    if sid and sid not in sessions_map and sid not in recycled_ids:
                        sessions_map[sid] = {
                            "title": sid[:12], "created_at": None,
                            "updated_at": None, "pinned": False,
                            "sort_order": None,
                        }
            except Exception:
                pass

        if not sessions_map:
            conn.close()
            return {"sessions": [], "db": db}

        session_ids = list(sessions_map.keys())

        # Which sessions touched a genui or a web browser page? Derived from the
        # tool_executions log (logs.db) — a session is "linked" to a genui if it
        # ran any genui build/edit tool, and to a browser page if it drove the
        # live browser. No FK on the genui/browser rows is needed for this.
        _GENUI_TOOLS = [
            "render_visual", "edit_genui", "create_genui",
            "set_genui_data", "rename_genui", "screenshot_genui",
        ]
        _BROWSER_TOOLS = ["browser_action"]
        genui_sessions: set = set()
        browser_sessions_used: set = set()
        try:
            from app.db.logs_store import get_log_store
            # No user_id filter: tool_executions rows often carry a NULL user_id
            # (the loop's tool events don't stamp it). Correctness instead comes
            # from only flagging sessions that are in this user's session map.
            # This async-shaped store method contains synchronous SQLite work
            # and no awaits.  Run it to completion on this same summary worker
            # so its connection never touches the main event-loop thread.
            usage = asyncio.run(get_log_store().session_tool_usage(
                _GENUI_TOOLS + _BROWSER_TOOLS
            ))
            _genui_set = set(_GENUI_TOOLS)
            _browser_set = set(_BROWSER_TOOLS)
            for sid_used, tools in usage.items():
                if tools & _genui_set:
                    genui_sessions.add(sid_used)
                if tools & _browser_set:
                    browser_sessions_used.add(sid_used)
        except Exception:
            pass

        # ── Family grouping (mirror /sessions + the chat session list) ──────
        # Tag every session so the Sessions table can nest children under their
        # parent as an expandable tree, identical in shape to the chat session
        # list. A session is a parent only if it is an orchestrator (spawned
        # helpers in agent_spawns); a session is a child only if it is a
        # spawn-* helper row. Optimizer Planner ('optimizer-*') and Closer
        # ('closer-*') sessions are TOP-LEVEL sessions of their own — they are
        # not nested under the base session they ran on. Workers ('trial-*')
        # live in throwaway temp DBs and never reach user.db, so they're
        # naturally excluded.
        child_counts: dict = {}
        parent_of: dict = {}     # child sid -> parent sid
        child_role: dict = {}    # child sid -> 'spawn'
        try:
            for r in cur.execute(
                "SELECT orchestrator_session_id AS p, spawn_session_id AS c FROM agent_spawns"
            ).fetchall():
                _p, _c = r["p"], r["c"]
                if _p and _c:
                    child_counts[_p] = child_counts.get(_p, 0) + 1
                    parent_of[_c] = _p
                    child_role[_c] = "spawn"
        except Exception:
            pass

        # ── Authoritative per-session cost (usage_events) ───────────────────
        # The interactions.metadata 'cost' field is only populated on some rows,
        # so most sessions would otherwise show an em-dash. usage_events stores
        # the locked-in per-call cost_usd (published price × tokens) for every
        # LLM call and is the single source of truth for spend (see the
        # /session-cost endpoint). Sum it once, grouped by session, and overlay
        # it below — preferring it over the sparse interactions cost.
        # Usage events live in the CONTROL database (billing writes there via
        # get_control_db()), not the per-user database.
        cost_by_session: dict = {}
        if user_id:
            try:
                from app.db import get_app_db
                _cdb = get_app_db()
                if hasattr(_cdb, "_get_conn"):
                    _cdb_conn = _cdb._get_conn()
                    try:
                        _cdb_cur = _cdb_conn.cursor()
                        for r in _cdb_cur.execute(
                            "SELECT session_id, COALESCE(SUM(cost_usd),0) AS c "
                            "FROM usage_events WHERE user_id = ? GROUP BY session_id",
                            (user_id,),
                        ).fetchall():
                            _sid, _c = r["session_id"], r["c"]
                            if _sid is not None:
                                cost_by_session[_sid] = float(_c or 0)
                    finally:
                        _cdb_conn.close()
            except Exception:
                pass

        # Build stats per session. Agent authority lives in per-agent stores,
        # not alongside the user/session tables.  This worker records agent IDs;
        # the async route wrapper enriches them after the SQLite summary returns.
        results = []
        for sid in session_ids:
            try:
                cur.execute(
                    'SELECT role, metadata, created_at FROM interactions WHERE session_id = ? ORDER BY created_at ASC',
                    (sid,)
                )
                rows = cur.fetchall()
            except Exception:
                rows = []

            total_input_tokens = 0
            total_output_tokens = 0
            total_duration_ms = 0
            total_cost = 0.0
            turn_count = 0
            message_count = 0
            has_cost = False
            last_active = None

            for row in rows:
                message_count += 1
                ts = row["created_at"]
                if ts and (last_active is None or ts > last_active):
                    last_active = ts

                raw_meta = row["metadata"]
                if not raw_meta:
                    continue
                try:
                    meta = json.loads(raw_meta)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(meta, dict):
                    continue

                role = row["role"] or ""

                if role == "assistant":
                    in_tok = meta.get("input_tokens")
                    out_tok = meta.get("output_tokens")
                    dur = meta.get("duration_ms")
                    cost_val = meta.get("cost")
                    turn = meta.get("turn")

                    if in_tok is not None:
                        total_input_tokens += int(in_tok)
                    if out_tok is not None:
                        total_output_tokens += int(out_tok)
                    if dur is not None:
                        total_duration_ms += int(dur)
                    if cost_val is not None:
                        total_cost += float(cost_val)
                        has_cost = True
                    if turn is not None:
                        # Use max turn value for this session
                        turn_count = max(turn_count, int(turn) + 1)
                    else:
                        # Count assistant messages as turns
                        turn_count += 1

            # Count user+assistant turns that form "loops"
            total_tokens = total_input_tokens + total_output_tokens

            # Prefer the authoritative usage_events cost; fall back to the
            # interactions-derived cost only when there's no usage_events row.
            ue_cost = cost_by_session.get(sid)
            if ue_cost is not None and ue_cost > 0:
                resolved_cost = round(ue_cost, 6)
            elif has_cost:
                resolved_cost = round(total_cost, 6)
            else:
                resolved_cost = None

            entry = {
                "session_id": sid,
                "title": sessions_map[sid]["title"],
                "created_at": sessions_map[sid]["created_at"],
                "last_active": last_active or sessions_map[sid]["updated_at"] or sessions_map[sid]["created_at"],
                "pinned": sessions_map[sid]["pinned"],
                "sort_order": sessions_map[sid]["sort_order"],
                "message_count": message_count,
                "turn_count": turn_count,
                "total_input_tokens": total_input_tokens,
                "total_output_tokens": total_output_tokens,
                "total_tokens": total_tokens,
                "total_duration_ms": total_duration_ms,
                "total_cost": resolved_cost,
                "has_genui": sid in genui_sessions,
                "has_browser": sid in browser_sessions_used,
                "child_count": child_counts.get(sid, 0),
                "parent_session_id": parent_of.get(sid),
                "child_role": child_role.get(sid),
            }
            # Enrich with agent_name and run_status if available
            try:
                cur.execute('SELECT agent_id, metadata FROM sessions WHERE id = ?', (sid,))
                srow = cur.fetchone()
                # Which device this session ran on (stamped at creation; see
                # app/devices/) — surfaced as the Sessions page device badge.
                try:
                    if srow and srow["metadata"]:
                        _smeta = json.loads(srow["metadata"])
                        _dev = (_smeta or {}).get("device") if isinstance(_smeta, dict) else None
                        if isinstance(_dev, dict) and _dev.get("label"):
                            entry["device_id"] = _dev.get("id")
                            entry["device_label"] = _dev.get("label")
                except Exception:
                    pass
                if srow and srow["agent_id"]:
                    aid = srow["agent_id"]
                    entry["agent_id"] = aid
                    entry["agent_name"] = ""
                    entry["agent_icon"] = ""
                    entry["agent_engine"] = ""
                else:
                    entry["agent_id"] = None
                    entry["agent_name"] = ""

                # Run status
                try:
                    cur2 = conn.cursor()
                    cur2.execute(
                        'SELECT status, updated_at, stop_cause, resume_attempts, '
                        'max_resume_attempts FROM session_runs WHERE session_id = ?',
                        (sid,))
                    rrow = cur2.fetchone()
                    _rst = rrow["status"] if rrow else None
                    if rrow and _resume_pending({
                        "status": _rst, "stop_cause": rrow["stop_cause"],
                        "resume_attempts": rrow["resume_attempts"],
                        "max_resume_attempts": rrow["max_resume_attempts"],
                    }):
                        _rst = "resuming"
                    entry["run_status"] = _rst
                    entry["run_updated_at"] = rrow["updated_at"] if rrow else None
                except Exception:
                    entry["run_status"] = None
                    entry["run_updated_at"] = None
            except Exception:
                entry["agent_id"] = None
                entry["agent_name"] = ""
                entry["run_status"] = None
                entry["run_updated_at"] = None
            results.append(entry)

        # Unpinned follows activity; pinned then overlays its manual location.
        results.sort(key=lambda s: s["last_active"] or "", reverse=True)
        results.sort(key=lambda s: (
            not s["pinned"],
            (s["sort_order"] is None) if s["pinned"] else False,
            s["sort_order"] if s["pinned"] and s["sort_order"] is not None else 0,
        ))

        conn.close()
        return {"sessions": results, "db": db}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/session-stats")
async def session_stats(
    request: Request,
    user_id: str = Query(..., description="User ID"),
    db: str = Query("user.db", description="Database filename"),
    status: str = Query("active", description="Filter by session status: 'active', 'recycled', or 'all'"),
    agent_id: Optional[str] = Query(None, description="Return sessions belonging to this agent only"),
):
    """Return per-session usage summaries without blocking the request loop."""
    # Authorization remains on the request loop; only the post-authorization
    # database/JSON summary is handed to the dedicated bounded worker pool.
    if not _is_open_access_mode():
        from app.auth.identity import assert_caller_is
        await assert_caller_is(request, user_id)

    key = _session_stats_cache_key(user_id, db, status)
    result, cold_future = _cached_session_stats(key)
    if result is None:
        # asyncio.wrap_future lets all concurrent cold callers await the same
        # executor Future without submitting duplicate full-history scans.
        result = copy.deepcopy(await asyncio.wrap_future(cold_future))

    # The shared Agents-page viewer may be embedded in a specific agent card.
    # Scope that card's catalog before display enrichment, while preserving the
    # account-wide cached summary for the default All Agents view.
    # Direct callers of this route (including unit tests) receive FastAPI's
    # Query sentinel for omitted optional parameters; only a real non-empty
    # string represents an agent filter.
    if isinstance(agent_id, str) and agent_id:
        result["sessions"] = [
            row for row in result.get("sessions", [])
            if row.get("agent_id") == agent_id
        ]

    # Agent authority is async-shaped and may itself use a synchronous backend.
    # Keep those lookups off the request loop as well, then enrich only the plain
    # result dictionaries returned by the summary worker.
    agent_ids = {
        row.get("agent_id")
        for row in result.get("sessions", [])
        if row.get("agent_id")
    }
    agent_display: dict[str, dict] = {}
    if agent_ids:
        from app.db import get_db
        from app.db.offload import db_offload

        authority = get_db()
        for aid in agent_ids:
            try:
                agent = await db_offload(
                    lambda aid=aid: authority.get_agent_by_id(aid)
                ) or {}
            except Exception:
                agent = {}
            meta = agent.get("metadata") or {}
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except (json.JSONDecodeError, TypeError):
                    meta = {}
            if not isinstance(meta, dict):
                meta = {}
            agent_display[aid] = {
                "name": agent.get("name") or "",
                "icon": agent.get("icon") or meta.get("icon") or "",
                "engine": meta.get("engine") or agent.get("engine") or "",
            }

    for row in result.get("sessions", []):
        display = agent_display.get(row.get("agent_id"))
        if display:
            row["agent_name"] = display["name"]
            row["agent_icon"] = display["icon"]
            row["agent_engine"] = display["engine"]
    return result


@router.get("/storage-usage")
async def storage_usage(
    request: Request,
    user_id: str = Query(..., description="User ID"),
    db: str = Query("user.db", description="Database filename"),
):
    """Session storage used versus the configured per-user ceiling.

    Powers the Sessions footer meter on the Agents page (the Sessions table
    lives inside the Agents tab — see ui/main-panel/agents/sessions/).

    "Used" is the same SQLite main-file + live-WAL footprint the
    ``user_database_size_limit`` quota enforcer compares against its ceiling
    (``_sqlite_file_footprint``), so the meter a user reads and the pressure the
    cleanup worker reacts to can never disagree.

    Degrades honestly rather than inventing numbers:
      * the quota app function is switched off → ``limit_bytes`` is null and the
        meter shows used bytes with no ceiling;
      * a remote (Postgres) authority has no per-user file to measure →
        ``available`` false with ``reason`` "remote_backend".
    """
    if not _is_open_access_mode():
        from app.auth.identity import assert_caller_is
        await assert_caller_is(request, user_id)

    # `user.db` is a request-scoped alias for the authenticated caller's
    # data/user_data/<user_id>/<user_id>.db authority. Bind it before resolving
    # the path so this meter measures the same per-user interaction store as the
    # Sessions list and quota enforcer.
    if db == "user.db":
        from app.db.local import set_db_user_context
        set_db_user_context(user_id)

    limit_bytes = _user_database_limit_bytes()
    payload: dict = {
        "db": db,
        "available": False,
        "reason": None,
        "used_bytes": None,
        "limit_bytes": limit_bytes,
        "remaining_bytes": None,
        "used_percent": None,
        "limit_enabled": limit_bytes is not None,
    }
    try:
        db_path = _get_db_path(db)
    except HTTPException as exc:
        payload["reason"] = "invalid" if exc.status_code == 400 else "missing"
        return payload
    if not db_path.exists():
        payload["reason"] = "remote_backend" if _pg_conninfo_for(db) is not None else "missing"
        return payload
    # Two stat() calls on a known path — cheap enough to stay on the request
    # loop, and it keeps the reading consistent with the quota trigger.
    used_bytes = _sqlite_file_footprint(db_path)
    payload["available"] = True
    payload["used_bytes"] = used_bytes
    if limit_bytes:
        payload["remaining_bytes"] = max(0, limit_bytes - used_bytes)
        payload["used_percent"] = round(used_bytes / limit_bytes * 100.0, 1)
    return payload


@router.get("/stream/interactions")
async def stream_interactions(
    since: str = Query("", description="ISO timestamp — return rows with created_at > since"),
    db: str = Query("user.db", description="Database filename"),
    user_id: str = Query("", description="Filter by user_id (optional)"),
    session_id: str = Query("", description="Filter by session_id (optional)"),
):
    """Return interactions. Used by Stream tab and Loop visualizer."""
    # If session has a temp_db_path, route to that DB transparently
    if session_id:
        resolved_db = _resolve_session_db(session_id, db)
        if resolved_db != db:
            db = resolved_db
    db_path = _get_db_path(db)
    try:
        conn, _dialect = _open(db)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='interactions'")
        if not cur.fetchone():
            conn.close()
            return {"interactions": [], "db": db}

        where_parts = []
        params = []

        if session_id:
            # Direct session filter — most specific
            where_parts.append("session_id = ?")
            params.append(session_id)
        elif user_id:
            # User filter via sessions table
            where_parts.append("session_id IN (SELECT id FROM sessions WHERE user_id = ?)")
            params.append(user_id)

        if since:
            where_parts.append("created_at > ?")
            params.append(since)

        where_clause = " AND ".join(where_parts) if where_parts else "1=1"
        limit_clause = "" if since else "LIMIT 200"

        cur.execute(
            f'SELECT id, session_id, role, content, tool_name, metadata, output, created_at '
            f'FROM interactions WHERE {where_clause} ORDER BY created_at ASC {limit_clause}',
            params
        )

        rows = [dict(row) for row in cur.fetchall()]
        conn.close()
        return {"interactions": rows, "db": db}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class UpdateRowRequest(BaseModel):
    """Request body for updating a row."""
    db: str = "user.db"
    table: str
    # Column-value pairs to identify the row (typically PK columns)
    where: dict[str, object]
    # Column-value pairs to update
    values: dict[str, object]


@router.put("/update")
async def update_row(
    req: UpdateRowRequest,
    _auth: dict = Depends(require_admin),
):
    """Update a row in a table. Admin-only."""
    db_path = _get_db_path(req.db)
    try:
        conn, _dialect = _open(db)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # Verify table exists
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (req.table,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail=f"Table '{req.table}' not found")

        # Get columns
        cur.execute(f'PRAGMA table_info("{req.table}")')
        columns = {col[1] for col in cur.fetchall()}

        # Validate columns exist
        for col in list(req.where.keys()) + list(req.values.keys()):
            if col not in columns:
                raise HTTPException(status_code=400, detail=f"Column '{col}' not found in table '{req.table}'")

        if not req.where:
            raise HTTPException(status_code=400, detail="'where' clause cannot be empty")

        # Build UPDATE
        set_clause = ", ".join(f'"{col}" = ?' for col in req.values)
        where_clause = " AND ".join(f'"{col}" = ?' for col in req.where)
        params = list(req.values.values()) + list(req.where.values())

        query = f'UPDATE "{req.table}" SET {set_clause} WHERE {where_clause}'
        cur.execute(query, params)
        conn.commit()
        affected = cur.rowcount
        conn.close()

        logger.info(f"Updated {affected} row(s) in {req.table}")
        return {"affected": affected, "success": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class DeleteRowRequest(BaseModel):
    """Request body for deleting a row."""
    db: str = "user.db"
    table: str
    # Column-value pairs to identify the row (typically PK columns)
    where: dict[str, object]


@router.delete("/row")
async def delete_row(
    req: DeleteRowRequest,
    _auth: dict = Depends(require_admin),
):
    """Delete a single row from a table, identified by the `where` clause. Admin-only."""
    db_path = _get_db_path(req.db)
    try:
        conn, _dialect = _open(db)
        cur = conn.cursor()

        # Verify table exists
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (req.table,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail=f"Table '{req.table}' not found")

        # Get columns
        cur.execute(f'PRAGMA table_info("{req.table}")')
        columns = {col[1] for col in cur.fetchall()}

        # Validate columns exist
        for col in req.where.keys():
            if col not in columns:
                raise HTTPException(status_code=400, detail=f"Column '{col}' not found in table '{req.table}'")

        if not req.where:
            raise HTTPException(status_code=400, detail="'where' clause cannot be empty")

        where_clause = " AND ".join(
            f'"{col}" IS NULL' if val is None else f'"{col}" = ?'
            for col, val in req.where.items()
        )
        params = [v for v in req.where.values() if v is not None]

        query = f'DELETE FROM "{req.table}" WHERE {where_clause}'
        cur.execute(query, params)
        conn.commit()
        affected = cur.rowcount
        conn.close()

        logger.info(f"Deleted {affected} row(s) from {req.table}")
        return {"affected": affected, "success": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/reset")
async def reset_database(
    db: str = Query("user.db", description="Database filename"),
    exclude: list[str] = Query(default=["agent_templates", "agent_prompts", "auth_elements"], description="List of tables to exclude from reset"),
    _auth: dict = Depends(require_admin),
):
    """Delete ALL rows from ALL tables. Skips excluded tables. Admin-only."""
    db_path = _get_db_path(db)
    try:
        conn, _dialect = _open(db)
        cur = conn.cursor()

        # Get all user tables (not internal sqlite ones)
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '%_fts%' ORDER BY name")
        all_tables = [row[0] for row in cur.fetchall()]

        # Preserve template/protected tables
        # If a request sends ?exclude= (empty list via query param), it gets parsed as [""]
        # So we filter out empty strings to allow unchecking ALL tables
        exclude_set = set(e for e in exclude if e)
        to_truncate = [t for t in all_tables if t not in exclude_set]

        results = {}
        for table in to_truncate:
            cur.execute(f'SELECT COUNT(*) FROM "{table}"')
            before = cur.fetchone()[0]
            if before > 0:
                cur.execute(f'DELETE FROM "{table}"')
                results[table] = before

        conn.commit()
        conn.close()

        logger.info(f"Database reset: {len(results)} tables truncated ({sum(results.values())} rows total)")
        return {"success": True, "tables_truncated": results, "total_rows_deleted": sum(results.values()), "db": db}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/truncate")
async def truncate_table(
    table: str = Query(..., description="Table name to truncate"),
    db: str = Query("user.db", description="Database filename"),
    _auth: dict = Depends(require_admin),
):
    """Delete ALL rows from a table. Admin-only."""
    db_path = _get_db_path(db)
    try:
        conn, _dialect = _open(db)
        cur = conn.cursor()

        # Verify table exists
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail=f"Table '{table}' not found")

        # Count before delete
        cur.execute(f'SELECT COUNT(*) FROM "{table}"')
        before = cur.fetchone()[0]

        # Delete all rows
        cur.execute(f'DELETE FROM "{table}"')
        conn.commit()
        conn.close()

        logger.info(f"Truncated table '{table}': {before} rows deleted")
        return {"success": True, "table": table, "deleted": before, "db": db}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/column-values")
async def column_values(
    table: str = Query(..., description="Table name"),
    column: str = Query(..., description="Column name"),
    db: str = Query("user.db", description="Database filename"),
    search: str = Query("", description="Search term to filter distinct values"),
    _auth: dict = Depends(require_db_auth),
):
    """Get distinct values for a column (for filter popup)."""
    db_path = _get_db_path(db)
    try:
        conn, _dialect = _open(db)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # Verify table exists
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail=f"Table '{table}' not found")

        # Verify column exists
        cur.execute(f"PRAGMA table_info(\"{table}\")")
        columns = [col[1] for col in cur.fetchall()]
        if column not in columns:
            raise HTTPException(status_code=404, detail=f"Column '{column}' not found in table '{table}'")

        # Restrict to the caller's rows (admins see everything).
        user_id, is_admin = _get_caller(_auth)
        acl_clause, acl_params = _acl_clause(table, user_id, is_admin)

        where_parts = []
        params: list = []
        if acl_clause:
            where_parts.append(acl_clause)
            params.extend(acl_params)
        if search:
            where_parts.append(f'CAST("{column}" AS TEXT) LIKE ?')
            params.append(f"%{search}%")

        where_sql = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""
        query = f'SELECT DISTINCT "{column}" FROM "{table}"{where_sql} ORDER BY "{column}" ASC'
        cur.execute(query, params)
        values = [row[0] for row in cur.fetchall()]

        # Total distinct count, scoped to the caller's rows.
        count_where = (" WHERE " + acl_clause) if acl_clause else ""
        cur.execute(
            f'SELECT COUNT(DISTINCT "{column}") FROM "{table}"{count_where}',
            acl_params if acl_clause else [],
        )
        total = cur.fetchone()[0]

        conn.close()
        return {
            "table": table,
            "column": column,
            "values": values,
            "total": total,
            "db": db,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/query")
async def query_table(
    table: str = Query(..., description="Table name"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    order_by: Optional[str] = Query(None, description="Column to order by"),
    order_dir: str = Query("ASC", pattern="^(ASC|DESC)$"),
    filter_col: Optional[str] = Query(None, description="Column to filter on"),
    filter_op: str = Query("contains", pattern="^(contains|equals|starts|gt|lt|not_in)$"),
    filter_val: Optional[str] = Query(None, description="Filter value (comma-separated for not_in)"),
    filters_json: Optional[str] = Query(None, description="JSON array of {col, op, val} for multi-column filters"),
    db: str = Query("user.db", description="Database filename"),
    with_count: bool = Query(True, description="When false, skip SELECT COUNT(*) (total will be -1). Used by silent auto-refresh."),
    _auth: dict = Depends(require_db_auth),
):
    """Query rows from a table."""
    db_path = _get_db_path(db)
    try:
        conn, _dialect = _open(db)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # Verify table exists
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail=f"Table '{table}' not found")

        # Get columns
        cur.execute(f"PRAGMA table_info(\"{table}\")")
        columns = [col[1] for col in cur.fetchall()]

        # Collect filter specs
        filter_specs = []

        # Parse multi-column filters from JSON
        if filters_json:
            try:
                parsed = json.loads(filters_json)
                if isinstance(parsed, list):
                    for spec in parsed:
                        if isinstance(spec, dict) and spec.get("col") in columns:
                            filter_specs.append({
                                "col": spec["col"],
                                "op": spec.get("op", "contains"),
                                "val": spec.get("val", ""),
                                "include_null": spec.get("include_null", False)
                            })
            except (json.JSONDecodeError, TypeError):
                pass

        # Legacy single-filter support
        if not filter_specs and filter_col and filter_col in columns and filter_val:
            filter_specs.append({"col": filter_col, "op": filter_op, "val": filter_val})

        # Build WHERE clause. ACL goes first so it always applies, regardless
        # of any user-supplied filters.
        where_clauses = []
        where_params = []
        user_id, is_admin = _get_caller(_auth)
        acl_clause, acl_params = _acl_clause(table, user_id, is_admin)
        if acl_clause:
            where_clauses.append(acl_clause)
            where_params.extend(acl_params)
        for spec in filter_specs:
            col = spec["col"]
            op = spec["op"]
            val = spec["val"]
            if op == "not_in":
                vals = [v.strip() for v in val.split(",") if v.strip()]
                include_null = spec.get("include_null", False)
                if vals:
                    if vals == ["__ALL__"]:
                        where_clauses.append("1=0")
                    else:
                        placeholders = ",".join("?" for _ in vals)
                        clause = f'"{col}" NOT IN ({placeholders})'
                        if include_null:
                            clause = f'({clause} OR "{col}" IS NULL)'
                        where_clauses.append(clause)
                        where_params.extend(vals)
                else:
                    if not include_null:
                        where_clauses.append(f'"{col}" IS NOT NULL')
            elif op == "contains":
                where_clauses.append(f'"{col}" LIKE ?')
                where_params.append(f"%{val}%")
            elif op == "equals":
                where_clauses.append(f'"{col}" = ?')
                where_params.append(val)
            elif op == "starts":
                where_clauses.append(f'"{col}" LIKE ?')
                where_params.append(f"{val}%")
            elif op == "gt":
                where_clauses.append(f'"{col}" > ?')
                where_params.append(val)
            elif op == "lt":
                where_clauses.append(f'"{col}" < ?')
                where_params.append(val)

        where_clause = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

        # Build query
        query = f'SELECT * FROM "{table}"{where_clause}'
        if order_by and order_by in columns:
            query += f' ORDER BY "{order_by}" {order_dir}'
        elif "created_at" in columns:
            query += ' ORDER BY "created_at" DESC'
        query += f" LIMIT {limit} OFFSET {offset}"

        cur.execute(query, where_params)
        rows = [dict(row) for row in cur.fetchall()]

        # Skip COUNT(*) on silent refreshes — the client keeps the last total.
        if with_count:
            cur.execute(f'SELECT COUNT(*) FROM "{table}"{where_clause}', where_params)
            total = cur.fetchone()[0]
        else:
            total = -1

        conn.close()
        return {
            "table": table,
            "columns": columns,
            "rows": rows,
            "total": total,
            "limit": limit,
            "offset": offset,
            "db": db,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/download")
async def download_db(
    db: str = Query("user.db", description="Database filename"),
    _auth=Depends(require_db_auth),
):
    """Download the SQLite database file."""
    db_path = _get_db_path(db)
    return FileResponse(
        path=str(db_path),
        filename=db,
        media_type="application/octet-stream",
    )


@router.get("/list")
async def list_databases(
    _auth=Depends(require_db_auth),
):
    """List all .db files in the db directory."""
    files = sorted([
        f.name for f in _DB_FILES_DIR.iterdir()
        if f.suffix == ".db"
    ])
    return {"databases": files}


@router.delete("/file")
async def delete_database_file(
    db: str = Query(..., description="Database filename to delete"),
    _auth=Depends(require_admin),
):
    """Delete a .db file (plus sidecar -wal/-shm) from the db directory. Admin-only.

    Refuses to delete user.db (primary app database).
    """
    # Reject path-traversal / non-plain names
    if Path(db).name != db:
        raise HTTPException(status_code=400, detail="Database name must be a plain filename")
    if not db.endswith(".db"):
        raise HTTPException(status_code=400, detail="Only .db files may be deleted")
    if db in {"user.db", "app.db"}:
        raise HTTPException(status_code=400, detail=f"Refusing to delete {db}")

    db_path = _DB_FILES_DIR / db
    if not db_path.exists():
        raise HTTPException(status_code=404, detail=f"Database '{db}' not found")

    removed = []
    errors = {}
    for suffix in ("", "-wal", "-shm"):
        target = _DB_FILES_DIR / (db + suffix)
        if target.exists():
            try:
                target.unlink()
                removed.append(target.name)
            except OSError as e:
                errors[target.name] = str(e)

    if errors and not removed:
        raise HTTPException(status_code=500, detail=f"Failed to delete: {errors}")

    logger.info(f"Deleted database files: {removed} (errors: {errors})")
    return {"success": True, "deleted": removed, "errors": errors}
