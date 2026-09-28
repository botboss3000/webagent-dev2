"""
PgPortableConnection — a sqlite3-compatible connection facade over Postgres.

The WebAgent storage layer (LocalBackend) is written against the sqlite3 API:
`conn = _get_conn(); conn.execute(sql, params).fetchone(); conn.commit()`, with
SQLite-dialect SQL (`?` placeholders, `INSERT OR IGNORE`, `datetime('now')`,
`json_each`, …). Rather than rewrite ~150 call sites + 60 backend methods, the
PostgresBackend reuses that exact code and swaps the connection: `_get_conn()`
returns one of these, which

  1. emulates the subset of the sqlite3 connection/cursor/Row API the codebase uses, and
  2. translates SQLite-dialect SQL → Postgres on the fly.

Connections are borrowed from a psycopg connection pool and returned on close().
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any, List, Optional, Sequence

logger = logging.getLogger(__name__)

# ── Per-query latency trace (TEMPORARY diagnostic) ────────────────────────────
# DIAGNOSTIC: when WEBAGENT_PERF_TRACE is on, log any pool-borrow (getconn) or
# query (execute) that takes longer than _PG_SLOW_MS to the diagnostics "perf"
# log (plaintext logs/perf.log). Lets us tell apart 'connection churn' (slow
# getconn) from 'high round-trip latency' (slow execute), and count how many
# round-trips one chat message really makes. REMOVE-WHEN: latency diagnosis done.
_PG_TRACE = (os.environ.get("WEBAGENT_PERF_TRACE", "1") or "1").strip().lower() not in ("0", "false", "no", "off")
try:
    _PG_SLOW_MS = int(os.environ.get("WEBAGENT_PG_SLOW_MS") or 150)
except ValueError:
    _PG_SLOW_MS = 150


def _pg_perf(mark: str, ms: int, **extra) -> None:
    if not _PG_TRACE:
        return
    try:
        from app.agent.diagnostics import record
        detail = {"mark": mark, "ms": ms}
        if extra:
            detail.update(extra)
        record("info", "perf", f"[pg] {mark} {ms}ms", source="pg.query", detail=detail)
    except Exception:
        pass


# ── Primary-key lookup (for INSERT OR REPLACE → ON CONFLICT) ──────────────────

def _build_pk_lookup() -> dict:
    """Map table name → list of primary-key column names, from the canonical schema."""
    from app.db.schema.tables import TABLES
    out: dict = {}
    for t in TABLES:
        pks = [c.name for c in t.columns if c.primary_key]
        if not pks:
            # composite PK declared as a constraint: "PRIMARY KEY (a, b)"
            for con in t.constraints:
                m = re.search(r"PRIMARY KEY\s*\(([^)]*)\)", con, re.I)
                if m:
                    pks = [c.strip() for c in m.group(1).split(",")]
                    break
        if pks:
            out[t.name] = pks
    return out


_PK_LOOKUP: Optional[dict] = None


def _pk_for(table: str) -> List[str]:
    global _PK_LOOKUP
    if _PK_LOOKUP is None:
        _PK_LOOKUP = _build_pk_lookup()
    return _PK_LOOKUP.get(table, ["id"])


# ── SQL dialect translation ──────────────────────────────────────────────────

_DATETIME_NOW_PG = "to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')"


def _convert_placeholders(sql: str) -> str:
    """
    Replace SQLite `?` placeholders with psycopg `%s`, and escape literal `%`
    as `%%` (psycopg interpolates % when params are bound; it un-escapes %% → %,
    so doubling is correct even inside string literals like LIKE '%x%').
    Quote-aware for `?` so a literal '?' inside a string is left alone.
    """
    sql = sql.replace("%", "%%")
    out: List[str] = []
    in_str = False
    for ch in sql:
        if ch == "'":
            in_str = not in_str
            out.append(ch)
        elif ch == "?" and not in_str:
            out.append("%s")
        else:
            out.append(ch)
    return "".join(out)


def _translate_json_each(sql: str) -> str:
    """
    SQLite: EXISTS (SELECT 1 FROM json_each(<col>) WHERE value = ?)
    Postgres: iterate a TEXT column holding a JSON array as a set of text values.
    `<col>` is a bare column name; guard NULL/'' → empty array.
    """
    def repl(m: "re.Match") -> str:
        col = m.group(1).strip()
        return (
            f"json_array_elements_text((CASE WHEN {col} IS NULL OR {col} = '' "
            f"THEN '[]' ELSE {col} END)::json) AS _je(value)"
        )
    return re.sub(r"json_each\(\s*([\w.]+)\s*\)", repl, sql, flags=re.I)


def _translate_insert_or(sql: str) -> str:
    """Translate INSERT OR IGNORE / INSERT OR REPLACE into Postgres upserts."""
    # INSERT OR IGNORE → INSERT ... ON CONFLICT DO NOTHING
    if re.search(r"\bINSERT\s+OR\s+IGNORE\b", sql, re.I):
        sql = re.sub(r"\bINSERT\s+OR\s+IGNORE\s+INTO\b", "INSERT INTO", sql, flags=re.I)
        if "on conflict" not in sql.lower():
            sql = _append_clause(sql, "ON CONFLICT DO NOTHING")
        return sql

    # INSERT OR REPLACE → INSERT ... ON CONFLICT (<pk>) DO UPDATE SET ...
    m = re.search(r"\bINSERT\s+OR\s+REPLACE\s+INTO\s+(\w+)\s*\(([^)]*)\)", sql, re.I)
    if m:
        table = m.group(1)
        cols = [c.strip() for c in m.group(2).split(",")]
        sql = re.sub(r"\bINSERT\s+OR\s+REPLACE\s+INTO\b", "INSERT INTO", sql, flags=re.I)
        if "on conflict" not in sql.lower():
            pk = _pk_for(table)
            update_cols = [c for c in cols if c not in pk]
            if update_cols:
                sets = ", ".join(f"{c}=EXCLUDED.{c}" for c in update_cols)
                clause = f"ON CONFLICT ({', '.join(pk)}) DO UPDATE SET {sets}"
            else:
                clause = f"ON CONFLICT ({', '.join(pk)}) DO NOTHING"
            sql = _append_clause(sql, clause)
    return sql


def _append_clause(sql: str, clause: str) -> str:
    """Append a clause, placed before a trailing RETURNING / ; if present."""
    stripped = sql.rstrip()
    trailing = sql[len(stripped):]
    ret = re.search(r"\bRETURNING\b", stripped, re.I)
    if ret:
        return stripped[:ret.start()].rstrip() + f" {clause} " + stripped[ret.start():] + trailing
    if stripped.endswith(";"):
        return stripped[:-1].rstrip() + f" {clause};" + trailing
    return stripped + f" {clause}" + trailing


def _translate_introspection(sql: str) -> str:
    """Translate SQLite catalog/PRAGMA introspection to Postgres equivalents.

    Used mainly by the admin DB viewer:
      - `... FROM sqlite_master WHERE type='table' ...` → a subquery exposing
        `name` + `type` columns from pg_tables (so the surrounding SELECT/WHERE
        keep working unchanged).
      - `PRAGMA table_info(<t>)` → a SELECT shaped like PRAGMA's output columns
        (cid, name, type, notnull, dflt_value, pk) so positional access still works.
      - bare `CURRENT_TIMESTAMP` → text timestamp (timestamp columns are TEXT on PG).
    """
    # PRAGMA table_info(X)  →  information_schema projection in PRAGMA column order
    m = re.search(r"PRAGMA\s+table_info\s*\(\s*[\"']?([A-Za-z0-9_]+)[\"']?\s*\)", sql, re.I)
    if m:
        tbl = m.group(1)
        return (
            "SELECT (ordinal_position-1) AS cid, column_name AS name, data_type AS type, "
            "(CASE WHEN is_nullable='NO' THEN 1 ELSE 0 END) AS notnull, "
            "column_default AS dflt_value, 0 AS pk "
            "FROM information_schema.columns "
            f"WHERE table_schema='public' AND table_name='{tbl}' ORDER BY ordinal_position"
        )
    # sqlite_master (table catalog) → pg_tables subquery aliased as sqlite_master
    if re.search(r"\bsqlite_master\b", sql, re.I):
        sql = re.sub(
            r"\bsqlite_master\b",
            "(SELECT tablename AS name, 'table' AS type FROM pg_tables "
            "WHERE schemaname='public') AS sqlite_master",
            sql, flags=re.I,
        )
    # bare CURRENT_TIMESTAMP → text (timestamp columns are TEXT on PG)
    sql = re.sub(r"\bCURRENT_TIMESTAMP\b", _DATETIME_NOW_PG, sql, flags=re.I)
    return sql


def translate_sql(sql: str, has_params: bool) -> str:
    """Full SQLite → Postgres translation for a single statement."""
    if re.search(r"\bsqlite_master\b|PRAGMA\s+table_info|\bCURRENT_TIMESTAMP\b", sql, re.I):
        sql = _translate_introspection(sql)
    # Idiom translations (independent of params)
    sql = re.sub(r"datetime\('now'\)", _DATETIME_NOW_PG, sql, flags=re.I)
    sql = re.sub(r"\bIFNULL\s*\(", "COALESCE(", sql, flags=re.I)
    if re.search(r"\bINSERT\s+OR\s+(IGNORE|REPLACE)\b", sql, re.I):
        sql = _translate_insert_or(sql)
    if re.search(r"\bjson_each\b", sql, re.I):
        sql = _translate_json_each(sql)
    # Placeholder/percent handling only when params are bound.
    if has_params:
        sql = _convert_placeholders(sql)
    return sql


# ── Row / Cursor / Connection facades ────────────────────────────────────────

class PgRow:
    """sqlite3.Row-like row: supports row[int], row['col'], keys(), dict(row), in, get()."""

    __slots__ = ("_keys", "_vals", "_map")

    def __init__(self, keys: Sequence[str], vals: Sequence[Any]):
        self._keys = list(keys)
        self._vals = list(vals)
        self._map = {k: v for k, v in zip(self._keys, self._vals)}

    def keys(self):
        return list(self._keys)

    def values(self):
        return list(self._vals)

    def get(self, key, default=None):
        return self._map.get(key, default)

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._vals[key]
        if isinstance(key, slice):
            return self._vals[key]
        return self._map[key]

    def __contains__(self, key):
        return key in self._map

    def __iter__(self):
        # sqlite3.Row iterates values; dict(row) uses keys() + __getitem__, not __iter__.
        return iter(self._vals)

    def __len__(self):
        return len(self._vals)

    def __repr__(self):
        return f"PgRow({self._map!r})"


class PgCursor:
    """Wraps a psycopg cursor, returning PgRow objects and honoring the sqlite3 API."""

    def __init__(self, raw_cursor):
        self._cur = raw_cursor

    @property
    def description(self):
        return self._cur.description

    @property
    def rowcount(self):
        return self._cur.rowcount

    @property
    def lastrowid(self):
        return None  # WebAgent uses UUID PKs, never lastrowid

    def _colnames(self) -> List[str]:
        if self._cur.description is None:
            return []
        return [d.name for d in self._cur.description]

    def execute(self, sql: str, params: Sequence[Any] = ()):
        # Empty params → pass None (NOT an empty tuple), so psycopg does not scan
        # the SQL for %-placeholders at all. With an empty tuple psycopg STILL
        # interpolates and chokes on a literal '%' (e.g. LIKE 'spawn-%'), while
        # translate_sql only escapes '%'→'%%' when params are bound (has_params) —
        # so the two must agree that "no params" == None. When they disagreed, any
        # param-less query containing a literal '%' raised, and the admin session
        # list (which filters `id NOT LIKE 'spawn-%'`) silently returned empty on
        # a Postgres backend.
        params = tuple(params) if params else None
        tsql = translate_sql(sql, has_params=bool(params))
        _t = time.monotonic()
        try:
            self._cur.execute(tsql, params)
        except Exception:
            logger.debug("PG execute failed for SQL: %s", tsql)
            raise
        finally:
            _ms = int((time.monotonic() - _t) * 1000)
            if _PG_TRACE and _ms >= _PG_SLOW_MS:
                _pg_perf("query_slow", _ms, sql=(sql or "")[:80].replace("\n", " "))
        return self

    def executemany(self, sql: str, seq_of_params: Sequence[Sequence[Any]]):
        seq = [tuple(p) for p in seq_of_params]
        has_params = bool(seq and seq[0])
        tsql = translate_sql(sql, has_params=has_params)
        self._cur.executemany(tsql, seq)
        return self

    def fetchone(self) -> Optional[PgRow]:
        row = self._cur.fetchone()
        if row is None:
            return None
        return PgRow(self._colnames(), row)

    def fetchall(self) -> List[PgRow]:
        cols = self._colnames()
        return [PgRow(cols, r) for r in self._cur.fetchall()]

    def fetchmany(self, size: int = 1) -> List[PgRow]:
        cols = self._colnames()
        return [PgRow(cols, r) for r in self._cur.fetchmany(size)]

    def __iter__(self):
        cols = self._colnames()
        for r in self._cur:
            yield PgRow(cols, r)

    def close(self):
        try:
            self._cur.close()
        except Exception:
            pass


class PgPortableConnection:
    """
    sqlite3.Connection-compatible facade over a pooled psycopg connection.

    Borrowed from the pool on construction; returned to the pool on close().
    Transactional like sqlite3 (explicit commit()); close() rolls back anything
    uncommitted (matches sqlite3 semantics and keeps pooled connections clean).
    """

    def __init__(self, pool=None, conn=None):
        self._pool = pool
        if conn is not None:
            # Standalone (non-pooled) connection — caller owns its lifecycle;
            # close() really closes it. Used by the admin DB viewer in autocommit
            # mode so per-statement errors don't poison a shared transaction.
            self._conn = conn
            self._standalone = True
        else:
            _t = time.monotonic()
            self._conn = pool.getconn()
            _ms = int((time.monotonic() - _t) * 1000)
            if _ms >= _PG_SLOW_MS:
                _pg_perf("getconn_slow", _ms)
            self._standalone = False
        self._closed = False
        # Accept callers that set row_factory = sqlite3.Row (no-op; we always
        # return PgRow which already supports key+index access).
        self.row_factory = None

    def execute(self, sql: str, params: Sequence[Any] = ()) -> PgCursor:
        cur = PgCursor(self._conn.cursor())
        return cur.execute(sql, params)

    def executemany(self, sql: str, seq_of_params: Sequence[Sequence[Any]]) -> PgCursor:
        cur = PgCursor(self._conn.cursor())
        return cur.executemany(sql, seq_of_params)

    def executescript(self, script: str) -> None:
        # Run a multi-statement script (each statement translated individually).
        for stmt in _split_statements(script):
            s = stmt.strip()
            if not s:
                continue
            self._conn.execute(translate_sql(s, has_params=False))

    def cursor(self) -> PgCursor:
        return PgCursor(self._conn.cursor())

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self._standalone:
            try:
                self._conn.close()
            except Exception:
                pass
            return
        try:
            # Discard any uncommitted work before returning to the pool.
            self._conn.rollback()
        except Exception:
            pass
        try:
            self._pool.putconn(self._conn)
        except Exception:
            try:
                self._conn.close()
            except Exception:
                pass

    # context-manager support: `with conn:` commits on success, rolls back on error
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        return False


def connect_standalone(conninfo: str, autocommit: bool = True) -> "PgPortableConnection":
    """Open a dedicated (non-pooled) PgPortableConnection. In autocommit mode each
    statement is independent, so a failing statement does not abort a shared
    transaction — used by the admin DB viewer, whose endpoints run several
    best-effort statements per request with per-statement error guards."""
    import psycopg
    conn = psycopg.connect(conninfo, autocommit=autocommit)
    try:
        from pgvector.psycopg import register_vector
        register_vector(conn)
    except Exception:
        pass
    return PgPortableConnection(conn=conn)


# ── Pooled autocommit connections for the DB-viewer / chat-panel endpoints ────
# `connect_standalone` opens a BRAND-NEW psycopg connection every call (~343ms TLS
# handshake to a remote pooler). The chat-panel read endpoints that stay
# on the remote authority (the live-reconcile tail, the related/family lookup) are
# POLLED, so that per-call handshake is pure waste. This keeps a small AUTOCOMMIT
# pool (separate from the transactional backend pool) so those calls reuse warm
# connections. Autocommit preserves the per-statement isolation the viewer relies
# on. Any pool failure falls back to a fresh standalone connection, so this can
# only ever make things faster, never break a request.
_VIEWER_POOL = None
_VIEWER_POOL_CONNINFO: Optional[str] = None
import threading as _threading  # noqa: E402
_VIEWER_POOL_LOCK = _threading.Lock()


def _viewer_pool(conninfo: str):
    """Lazily build (or rebuild on a conninfo change) the shared autocommit pool."""
    global _VIEWER_POOL, _VIEWER_POOL_CONNINFO
    if _VIEWER_POOL is not None and _VIEWER_POOL_CONNINFO == conninfo:
        return _VIEWER_POOL
    with _VIEWER_POOL_LOCK:
        if _VIEWER_POOL is not None and _VIEWER_POOL_CONNINFO == conninfo:
            return _VIEWER_POOL
        # A DB switch changed the conninfo → discard the stale pool.
        if _VIEWER_POOL is not None:
            try:
                _VIEWER_POOL.close()
            except Exception:
                pass
            _VIEWER_POOL = None
        from psycopg_pool import ConnectionPool
        def _configure(c):
            try:
                from pgvector.psycopg import register_vector
                register_vector(c)
            except Exception:
                pass
        try:
            _min = max(1, int(os.environ.get("WEBAGENT_PG_VIEWER_POOL_MIN") or 1))
        except ValueError:
            _min = 1
        try:
            _max = max(_min, int(os.environ.get("WEBAGENT_PG_VIEWER_POOL_MAX") or 4))
        except ValueError:
            _max = max(_min, 4)
        _VIEWER_POOL = ConnectionPool(
            conninfo, min_size=_min, max_size=_max, open=True,
            configure=_configure,
            # autocommit = per-statement isolation (the viewer's contract);
            # prepare_threshold=None keeps it compatible with the transaction
            # transaction pooler (6543), same as the backend pool.
            kwargs={"autocommit": True, "prepare_threshold": None},
        )
        _VIEWER_POOL_CONNINFO = conninfo
        return _VIEWER_POOL


def connect_viewer_pooled(conninfo: str) -> "PgPortableConnection":
    """Borrow a warm autocommit connection from the shared viewer pool (returned to
    the pool on ``.close()``). Falls back to a fresh standalone connection if the
    pool can't be built or is exhausted — so it never breaks a request."""
    try:
        return PgPortableConnection(_viewer_pool(conninfo))
    except Exception as e:
        logger.debug("viewer pool unavailable (%s); using standalone connection", e)
        return connect_standalone(conninfo)


def close_viewer_pool() -> None:
    """Close the shared viewer pool (app shutdown / DB switch). Best-effort."""
    global _VIEWER_POOL, _VIEWER_POOL_CONNINFO
    with _VIEWER_POOL_LOCK:
        if _VIEWER_POOL is not None:
            try:
                _VIEWER_POOL.close()
            except Exception:
                pass
        _VIEWER_POOL = None
        _VIEWER_POOL_CONNINFO = None


def _split_statements(script: str) -> List[str]:
    """Naive ';' split honoring single-quote string literals."""
    out: List[str] = []
    buf: List[str] = []
    in_str = False
    for ch in script:
        if ch == "'":
            in_str = not in_str
        if ch == ";" and not in_str:
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out
