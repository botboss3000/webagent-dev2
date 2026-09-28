"""
Raw Postgres backend (asyncpg).

The live Postgres backend for all providers whose dialect is "postgres"
(postgres, aws_rds, gcp_cloud_sql, azure_postgres, neon). Subclasses
LocalBackend and overrides connection + schema bootstrap; every data method
is inherited and runs through the PgPortableConnection translation layer.
"""

import asyncio
import logging
import os
import shutil
import tempfile
from typing import Optional

from app.db.connection_config import DBConnectionConfig

logger = logging.getLogger(__name__)


class PostgresConnectionError(Exception):
    pass


# ── Connection-string + DDL helpers (shared by the live backend) ─────────────

def make_conninfo(cfg: DBConnectionConfig, password: str) -> str:
    """Build a libpq conninfo string from a DBConnectionConfig + password."""
    import psycopg
    meta_port = cfg.port or 5432
    return psycopg.conninfo.make_conninfo(
        host=cfg.host or "localhost",
        port=meta_port,
        dbname=cfg.database or "postgres",
        user=cfg.username or "postgres",
        password=password or "",
        sslmode=cfg.ssl_mode or "prefer",
    )


def _strip_comment_lines(stmt: str) -> str:
    return "\n".join(
        ln for ln in stmt.splitlines() if not ln.strip().startswith("--")
    ).strip()


def _sqlite_type_to_pg(decl: str) -> str:
    d = (decl or "").upper()
    if "INT" in d:
        return "INTEGER"
    if any(x in d for x in ("REAL", "FLOA", "DOUB")):
        return "DOUBLE PRECISION"
    if "BLOB" in d:
        return "BYTEA"
    return "TEXT"


async def test_connection(cfg: DBConnectionConfig, password: Optional[str]) -> dict:
    """
    Try to connect with the given config + password. Returns
        {"ok": True, "server_version": "...", "database": "..."}
    or
        {"ok": False, "error": "..."}
    """
    try:
        import asyncpg
    except ImportError:
        return {"ok": False, "error": "asyncpg not installed. Add `asyncpg` to requirements.txt."}

    url = cfg.build_url(password=password or "")
    # asyncpg consumes the raw DSN (strip the +asyncpg dialect tag for asyncpg.connect)
    dsn = url.replace("postgresql+asyncpg://", "postgresql://")

    try:
        conn = await asyncpg.connect(dsn, timeout=10)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    try:
        version = await conn.fetchval("SELECT version()")
        dbname = await conn.fetchval("SELECT current_database()")
        return {"ok": True, "server_version": version, "database": dbname}
    finally:
        await conn.close()


async def bootstrap_schema(cfg: DBConnectionConfig, password: Optional[str]) -> dict:
    """
    Connect and run the rendered Postgres DDL.

    Idempotent (uses CREATE TABLE IF NOT EXISTS). Returns
        {"ok": True, "statements_run": N}
    or
        {"ok": False, "error": "..."}
    """
    try:
        import asyncpg
    except ImportError:
        return {"ok": False, "error": "asyncpg not installed."}

    from app.db.schema import render_postgres
    ddl = render_postgres()

    url = cfg.build_url(password=password or "")
    dsn = url.replace("postgresql+asyncpg://", "postgresql://")

    try:
        conn = await asyncpg.connect(dsn, timeout=15)
    except Exception as e:
        return {"ok": False, "error": f"connect failed: {e}"}

    statements_run = 0
    errors: list = []
    try:
        # Ensure pgvector is available before any table that uses a vector column.
        try:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        except Exception as e:
            logger.warning("Could not create pgvector extension: %s", e)
            errors.append({"stmt": "CREATE EXTENSION vector", "error": str(e)})

        # Statements are split + comment-stripped by _iter_ddl_statements. The
        # DDL renderer produces simple statements separated by ';' (no PL/pgSQL
        # bodies) in dependency order, so a single pass works.
        for s in _iter_ddl_statements(ddl):
            try:
                await conn.execute(s)
                statements_run += 1
            except Exception as e:
                logger.warning("Bootstrap statement failed: %s :: %s", e, s[:120])
                errors.append({"stmt": s[:160], "error": str(e)})
                # continue — IF NOT EXISTS makes most failures recoverable
        return {"ok": len(errors) == 0, "statements_run": statements_run, "errors": errors}
    finally:
        await conn.close()


def _split_sql(text: str) -> list:
    """Naive ';' split. Renderer output has no embedded ';' inside literals."""
    out = []
    buf = []
    for ch in text:
        buf.append(ch)
        if ch == ";":
            out.append("".join(buf))
            buf = []
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out


def _iter_ddl_statements(ddl: str):
    """Yield each non-empty, comment-stripped statement from rendered DDL.

    Shared by both bootstrap paths (async asyncpg `bootstrap_schema` and sync
    psycopg `_bootstrap_pg_schema`): split on ';', drop full-line `--` comments
    (so a leading banner comment glued to the first CREATE doesn't swallow it),
    and skip anything left blank. Each caller executes the statements itself.
    """
    for raw in _split_sql(ddl):
        stmt = _strip_comment_lines(raw)
        if stmt:
            yield stmt


# ── Live Postgres backend ────────────────────────────────────────────────────

class PostgresBackend:  # placeholder replaced below by the real subclass
    pass


def _reference_sqlite_columns() -> dict:
    """
    Build {table: {column: declared_type}} from a throwaway, no-seed SQLite
    backend. This reflects the CURRENT code's full schema (base + all ALTER
    migrations), so Postgres can be reconciled to exact column parity without
    hand-maintaining a duplicate column list.
    """
    from app.db.local import LocalBackend
    tmpdir = tempfile.mkdtemp(prefix="wa_refschema_")
    out: dict = {}
    try:
        ref = LocalBackend(db_path=os.path.join(tmpdir, "ref.db"), seed=False)
        conn = ref._get_conn()
        try:
            tables = [
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
            ]
            for t in tables:
                info = conn.execute(f"PRAGMA table_info({t})").fetchall()
                out[t] = {row[1]: row[2] for row in info}  # name -> declared type
        finally:
            conn.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return out


def _make_postgres_backend_class():
    """Build the PostgresBackend subclass lazily (so importing this module does
    not require LocalBackend / psycopg unless a PG backend is actually used)."""
    from app.db.local import LocalBackend
    from app.db.schema import render_postgres

    class _PostgresBackend(LocalBackend):
        """
        Live Postgres backend. Subclasses LocalBackend and overrides only the
        connection + schema bootstrap; every data method is inherited and runs
        through the PgPortableConnection translation layer (see pg_portable.py).
        """

        def __init__(self, cfg: DBConnectionConfig, password: Optional[str] = None, seed: bool = True):
            self._cfg = cfg
            self._password = password if password is not None else os.environ.get("WEBAGENT_DB_PASSWORD", "")
            self._conninfo = make_conninfo(cfg, self._password)
            self._db_path = "<postgres>"  # sentinel; never used to open SQLite
            # Cross-thread/loop write guard (see _DbWriteLock in local.py): lets
            # offloaded write coroutines run in worker threads without the
            # loop-bound-lock error that froze chat on remote PG.
            from app.db.local import _DbWriteLock
            self._write_lock = _DbWriteLock()
            self._seed_on_init = seed
            self._scan_sibling_dbs = False
            self._pool = None
            # The `vector` extension must exist BEFORE the pool opens, otherwise
            # register_vector() fails on the pool's initial connections and numpy
            # arrays cannot be adapted to the vector type.
            self._ensure_extension()
            self._open_pool()
            self._init_db()

        # ---- connection / pool ----

        def _ensure_extension(self):
            import psycopg
            try:
                # prepare_threshold=None disables server-side prepared statements:
                # REQUIRED on a pgBouncer-style transaction-mode pooler (port 6543), which
                # multiplexes client connections across shared backends. Without it
                # psycopg auto-prepares a statement (e.g. "_pg3_0") that lingers on a
                # backend and collides on the next client ("prepared statement already
                # exists"), stranding boot on the local SQLite fallback. Mirrors the
                # pool config in _open_pool().
                with psycopg.connect(self._conninfo, autocommit=True,
                                     prepare_threshold=None) as conn:
                    conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            except Exception as e:
                logger.warning("Could not ensure pgvector extension: %s", e)

        def _open_pool(self):
            from psycopg_pool import ConnectionPool

            def _configure(conn):
                try:
                    from pgvector.psycopg import register_vector
                    register_vector(conn)
                    conn.rollback()  # clear the txn opened while reading the type OID
                except Exception as e:  # pragma: no cover
                    logger.warning("pgvector register failed on pooled conn: %s", e)

            # Pool sizing — IMPORTANT with a SESSION-mode pooler (port
            # 5432), which caps TOTAL client connections (default 15) and holds
            # one server connection per pooled client for its whole life. Each
            # running instance keeps min_size connections open permanently, so a
            # large min across several instances exhausts the cap ("max clients
            # reached in session mode"). Keep the defaults modest and leave
            # headroom; raise via env only on the transaction-mode pooler (6543)
            # or a direct connection, which don't have the same per-client cap.
            # Defaults assume the TRANSACTION-mode pooler (6543) or a direct
            # connection (no per-client cap): min 4 keeps enough warm connections
            # that a turn's concurrent read fan-out + the live stream + background
            # pollers don't each pay a cold connection open. If you are pinned to
            # the SESSION-mode pooler (5432, cap ~15) AND run several instances,
            # lower these via env so the instances don't exhaust the cap.
            try:
                _min = max(1, int(os.environ.get("WEBAGENT_PG_POOL_MIN") or 4))
            except ValueError:
                _min = 4
            # max 24 (was 16): the page-boot burst fires ~15-20 remote-touching
            # reads at once (each through the db-worker pool, default 16) while the
            # background sync engine also wants connections; 24 gives that burst
            # room + headroom for the live stream. Safe on the TRANSACTION-mode
            # pooler (6543, no per-client cap) and direct; if pinned to the
            # SESSION-mode pooler (5432, cap ~15) with several instances, lower via
            # WEBAGENT_PG_POOL_MAX so the instances don't exhaust the cap.
            try:
                _max = max(_min, int(os.environ.get("WEBAGENT_PG_POOL_MAX") or 24))
            except ValueError:
                _max = max(_min, 24)
            self._pool = ConnectionPool(
                self._conninfo,
                min_size=_min,
                max_size=_max,
                open=True,
                configure=_configure,
                # prepare_threshold=None disables server-side prepared statements:
                # required for a pgBouncer-style transaction-mode pooler and
                # harmless on session mode / direct, so a user can switch the
                # connection to the faster multiplexing pooler with no code change.
                kwargs={"autocommit": False, "prepare_threshold": None},
            )

        def _get_conn(self):
            from app.db.pg_portable import PgPortableConnection
            return PgPortableConnection(self._pool)

        # ---- schema bootstrap (overrides the SQLite _init_db entirely) ----

        def _init_db(self) -> None:
            # Schema bootstrap + column reconcile are a ONE-TIME job, but they are
            # expensive over a remote link: ~294 DDL statements + ~59 information_schema
            # probes, each a separate round-trip. Re-running them on every boot is the
            # bulk of a remote Postgres's slow startup. Gate them behind a fingerprint
            # of the current schema definition, stored in the DB itself: on a boot where
            # the schema hasn't changed we do 2 round-trips (ensure marker table + read)
            # instead of ~353. Fresh/other databases lack the marker, so they still get
            # a full bootstrap exactly as before. Mirrors the manifest-hash short-circuit
            # the agent-template seed already uses.
            if self._schema_is_current():
                logger.info("PostgresBackend schema up-to-date (fingerprint match) — "
                            "skipping bootstrap/reconcile")
            else:
                self._bootstrap_pg_schema()
                self._reconcile_columns()
                self._record_schema_fingerprint()
            if getattr(self, "_seed_on_init", True):
                conn = self._get_conn()
                try:
                    self._seed_agent_templates_from_json_files(conn)
                except Exception as e:
                    logger.warning("Postgres seed step failed: %s", e)
                finally:
                    conn.close()
            logger.info("PostgresBackend initialized (host=%s db=%s)", self._cfg.host, self._cfg.database)

        def _bootstrap_pg_schema(self) -> None:
            import psycopg
            from app.db.session_manifest import install_postgres_manifest_maintenance
            ddl = render_postgres()
            # prepare_threshold=None: see _ensure_extension — no server-side prepared
            # statements on the transaction pooler.
            with psycopg.connect(self._conninfo, autocommit=True,
                                 prepare_threshold=None) as conn:
                try:
                    conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
                except Exception as e:
                    logger.warning("CREATE EXTENSION vector failed: %s", e)
                for stmt in _iter_ddl_statements(ddl):
                    try:
                        conn.execute(stmt)
                    except Exception as e:
                        logger.warning("DDL statement failed: %s :: %s", e, stmt[:100])
                try:
                    install_postgres_manifest_maintenance(conn)
                except Exception as e:
                    logger.warning("Session manifest trigger bootstrap failed: %s", e)

        def _reconcile_columns(self) -> None:
            """Add any columns present in the reference SQLite schema but missing
            in Postgres (migration-proof parity). Tables empty at bootstrap so
            ADD COLUMN is always safe."""
            import psycopg
            try:
                ref = _reference_sqlite_columns()
            except Exception as e:
                logger.warning("Could not build reference schema for reconcile: %s", e)
                return
            # prepare_threshold=None: CRITICAL here — this loop runs the SAME
            # information_schema query once per table, so psycopg would auto-prepare
            # it after 5 rounds and that "_pg3_0" prepared statement collides across
            # the transaction pooler's shared backends. See _ensure_extension.
            with psycopg.connect(self._conninfo, autocommit=True,
                                 prepare_threshold=None) as conn:
                for table, cols in ref.items():
                    rows = conn.execute(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema='public' AND table_name=%s",
                        (table,),
                    ).fetchall()
                    existing = {r[0] for r in rows}
                    if not existing:
                        continue  # not a Postgres table (e.g. FTS shadow tables)
                    for col, decl in cols.items():
                        if col not in existing:
                            pgt = _sqlite_type_to_pg(decl)
                            try:
                                conn.execute(
                                    f'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS "{col}" {pgt}'
                                )
                                logger.info("Reconcile: added %s.%s (%s)", table, col, pgt)
                            except Exception as e:
                                logger.warning("Reconcile ADD %s.%s failed: %s", table, col, e)

        # ---- schema fingerprint gate (skip bootstrap/reconcile when unchanged) ----

        _SCHEMA_META_TABLE = "_webagent_schema_meta"
        _SCHEMA_FP_KEY = "schema_fingerprint"

        def _schema_fingerprint(self) -> str:
            """A stable hash of the SCHEMA DEFINITION this build would create — the
            Postgres DDL plus the reference column set the reconcile step derives.
            Any change to a table, index, or column changes the hash, forcing a
            re-bootstrap; an unchanged schema hashes identically every boot. Purely
            local — no network."""
            import hashlib
            import json as _json
            from app.db.schema import render_postgres
            from app.db.session_manifest import POSTGRES_MANIFEST_DDL
            ddl = render_postgres()
            try:
                ref = _reference_sqlite_columns()
            except Exception:
                ref = {}
            ref_blob = _json.dumps(ref, sort_keys=True, default=str)
            h = hashlib.sha256()
            h.update(ddl.encode("utf-8", "replace"))
            h.update(b"\x00")
            h.update(ref_blob.encode("utf-8", "replace"))
            h.update(b"\x00")
            h.update(POSTGRES_MANIFEST_DDL.encode("utf-8", "replace"))
            return h.hexdigest()

        def _schema_is_current(self) -> bool:
            """True when the remote DB already carries our exact schema fingerprint,
            so the full bootstrap + reconcile can be skipped. Costs one connection and
            two statements (ensure the tiny marker table exists, then read it). Any
            failure returns False so we conservatively fall back to a full bootstrap —
            the same behaviour as before this gate existed."""
            import psycopg
            try:
                with psycopg.connect(self._conninfo, autocommit=True,
                                     prepare_threshold=None) as conn:
                    conn.execute(
                        f"CREATE TABLE IF NOT EXISTS {self._SCHEMA_META_TABLE} "
                        f"(key TEXT PRIMARY KEY, value TEXT)"
                    )
                    row = conn.execute(
                        f"SELECT value FROM {self._SCHEMA_META_TABLE} WHERE key = %s",
                        (self._SCHEMA_FP_KEY,),
                    ).fetchone()
                stored = row[0] if row else None
                return bool(stored) and stored == self._schema_fingerprint()
            except Exception as e:
                logger.warning("Schema fingerprint check failed (%s) — will bootstrap", e)
                return False

        def _record_schema_fingerprint(self) -> None:
            """Persist the current schema fingerprint after a successful bootstrap +
            reconcile, so the next boot's check matches and skips the heavy work. Never
            fatal — if the write fails we simply pay the bootstrap cost again next boot."""
            import psycopg
            try:
                with psycopg.connect(self._conninfo, autocommit=True,
                                     prepare_threshold=None) as conn:
                    conn.execute(
                        f"CREATE TABLE IF NOT EXISTS {self._SCHEMA_META_TABLE} "
                        f"(key TEXT PRIMARY KEY, value TEXT)"
                    )
                    conn.execute(
                        f"INSERT INTO {self._SCHEMA_META_TABLE} (key, value) VALUES (%s, %s) "
                        f"ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                        (self._SCHEMA_FP_KEY, self._schema_fingerprint()),
                    )
            except Exception as e:
                logger.warning("Could not record schema fingerprint: %s", e)

        def close(self):
            if self._pool is not None:
                try:
                    self._pool.close()
                except Exception:
                    pass

        # ---- memory: embeddings (pgvector) + full-text (tsquery) overrides ----

        async def _embed_and_store_chunks(self, conn, memory_id, text, source):
            import json as _json  # noqa
            import uuid as _uuidm
            import numpy as np
            from app.agent.embed import embed_text
            chunks = self._chunk_text(text, max_chars=500)
            conn.execute(
                "DELETE FROM memory_chunks WHERE memory_id = ? AND chunk_source = ?",
                (memory_id, source),
            )
            for i, chunk in enumerate(chunks):
                if not chunk.strip():
                    continue
                emb = None
                try:
                    emb = np.array(await embed_text(chunk), dtype=np.float32)
                except Exception as e:
                    logger.warning("Chunk embed failed (idx=%d mem=%s): %s", i, memory_id, e)
                conn.execute(
                    """INSERT OR REPLACE INTO memory_chunks
                       (id, memory_id, chunk_index, chunk_text, chunk_source, embedding, token_count)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (str(_uuidm.uuid4()), memory_id, i, chunk, source, emb, len(chunk.split())),
                )

        async def _fts5_search(self, user_id, query, limit=10):
            import json as _json
            if not query or not query.strip():
                return []
            expr = ("to_tsvector('english', coalesce(m.title,'') || ' ' || "
                    "coalesce(m.compiled_truth,'') || ' ' || coalesce(m.timeline,''))")
            conn = self._get_conn()
            try:
                rows = conn.execute(
                    f"""SELECT m.*, ts_rank({expr}, plainto_tsquery('english', ?)) AS rank
                        FROM memories m
                        WHERE m.user_id = ? AND {expr} @@ plainto_tsquery('english', ?)
                        ORDER BY rank DESC LIMIT ?""",
                    (query, user_id, query, limit),
                ).fetchall()
                out = []
                for r in rows:
                    d = dict(r)
                    d["frontmatter"] = _json.loads(d.get("frontmatter") or "{}")
                    out.append(d)
                return out
            finally:
                conn.close()

        async def _vector_search(self, user_id, query_text, limit=10):
            import json as _json
            import numpy as np
            from app.agent.embed import embed_text
            try:
                qv = np.array(await embed_text(query_text), dtype=np.float32)
            except Exception as e:
                logger.warning("Query embed failed, skipping vector search: %s", e)
                return []
            conn = self._get_conn()
            try:
                rows = conn.execute(
                    """SELECT m.slug, m.title, m.compiled_truth, m.timeline, m.page_type,
                              m.frontmatter, m.created_at, m.updated_at,
                              MIN(mc.embedding <=> ?) AS dist
                       FROM memory_chunks mc JOIN memories m ON m.id = mc.memory_id
                       WHERE m.user_id = ? AND mc.embedding IS NOT NULL
                       GROUP BY m.id, m.slug, m.title, m.compiled_truth, m.timeline,
                                m.page_type, m.frontmatter, m.created_at, m.updated_at
                       ORDER BY dist ASC LIMIT ?""",
                    (qv, user_id, limit),
                ).fetchall()
            finally:
                conn.close()
            out = []
            for r in rows:
                d = dict(r)
                dist = d.pop("dist", None)
                d["frontmatter"] = _json.loads(d.get("frontmatter") or "{}")
                d["rank"] = round(1.0 - float(dist), 4) if dist is not None else 0.0
                out.append(d)
            return out

        async def reindex_embeddings(self, *, tables=("memory_chunks", "doc_chunks"),
                                     batch=64) -> dict:
            """Re-embed every stored chunk with the CURRENT model, resizing the
            fixed-width pgvector column to the new model's width first.

            Unlike SQLite (raw BLOBs, any width), pgvector enforces the column's
            declared dimension, so a model switch that changes width must ALTER the
            column. pgvector won't change a column type while it still holds vectors
            of the old width, so the sequence per table is: drop the ANN index →
            NULL out the old vectors → ALTER the column to vector(N) → re-embed →
            rebuild the index at the new width. Embeds run with no DB txn held; the
            writes go in short committed bursts."""
            import psycopg
            import numpy as np
            from app.agent.embed import embed_text, embed_dim, embed_model_name

            target_dim = embed_dim()
            out: dict = {"model": embed_model_name(), "dim": target_dim, "tables": {}}
            for table in tables:
                idx = f"idx_{table}_embedding_hnsw"
                # 1. Resize the column (index dropped up front, rebuilt in step 3).
                try:
                    with psycopg.connect(self._conninfo, autocommit=True,
                                         prepare_threshold=None) as c:
                        c.execute(f"DROP INDEX IF EXISTS {idx}")
                        c.execute(f"UPDATE {table} SET embedding = NULL")
                        c.execute(f"ALTER TABLE {table} "
                                  f"ALTER COLUMN embedding TYPE vector({target_dim})")
                except Exception as e:
                    logger.warning("reindex: could not resize %s to vector(%d): %s",
                                   table, target_dim, e)
                    out["tables"][table] = {"error": str(e)}
                    continue

                # 2. Read rows, then re-embed with no DB connection held.
                conn = self._get_conn()
                try:
                    rows = conn.execute(f"SELECT id, chunk_text FROM {table}").fetchall()
                finally:
                    conn.close()

                written = failed = 0
                pending: list = []  # (np_embedding, id)

                async def _flush(items, _table=table):
                    if not items:
                        return
                    c = self._get_conn()
                    try:
                        for emb, rid in items:
                            c.execute(
                                f"UPDATE {_table} SET embedding = ? WHERE id = ?",
                                (emb, rid),
                            )
                        c.commit()
                    finally:
                        c.close()

                for r in rows:
                    txt = (r["chunk_text"] or "")
                    if not txt.strip():
                        continue
                    try:
                        emb = np.array(await embed_text(txt), dtype=np.float32)
                    except Exception as e:
                        failed += 1
                        logger.warning("reindex embed failed (%s id=%s): %s",
                                       table, r["id"], e)
                        continue
                    pending.append((emb, r["id"]))
                    written += 1
                    if len(pending) >= batch:
                        await _flush(pending)
                        pending = []
                await _flush(pending)

                # 3. Rebuild the ANN index at the new width.
                try:
                    with psycopg.connect(self._conninfo, autocommit=True,
                                         prepare_threshold=None) as c:
                        c.execute(f"CREATE INDEX IF NOT EXISTS {idx} ON {table} "
                                  f"USING hnsw (embedding vector_cosine_ops)")
                except Exception as e:
                    logger.warning("reindex: rebuild index %s failed: %s", idx, e)

                out["tables"][table] = {"rows": len(rows), "written": written, "failed": failed}
                logger.info("reindex %s (pg): %d rows, %d embedded, %d failed (dim=%d)",
                            table, len(rows), written, failed, target_dim)
            return out

        # ---- doc_chunks: embeddings + full-text overrides ----

        async def doc_chunk_upsert(self, data_source_id, source_ref, chunk_index,
                                   chunk_text, content_hash=None, embedding=None, metadata=None):
            import json as _json
            import uuid as _uuidm
            import numpy as np
            chunk_id = str(_uuidm.uuid4())
            emb = np.array(embedding, dtype=np.float32) if embedding is not None else None
            async with self._write_lock:
                conn = self._get_conn()
                try:
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
                        (chunk_id, data_source_id, source_ref, chunk_index, chunk_text,
                         content_hash, emb, len(chunk_text.split()) if chunk_text else 0,
                         _json.dumps(metadata or {})),
                    )
                    conn.commit()
                    return chunk_id
                finally:
                    conn.close()

        async def _doc_fts5_search(self, data_source_id, query, limit):
            if not query or not query.strip():
                return []
            expr = "to_tsvector('english', coalesce(dc.chunk_text,''))"
            conn = self._get_conn()
            try:
                rows = conn.execute(
                    f"""SELECT dc.id, dc.source_ref, dc.chunk_index, dc.chunk_text
                        FROM doc_chunks dc
                        WHERE dc.data_source_id = ? AND {expr} @@ plainto_tsquery('english', ?)
                        ORDER BY ts_rank({expr}, plainto_tsquery('english', ?)) DESC LIMIT ?""",
                    (data_source_id, query, query, limit),
                ).fetchall()
                return [
                    {"id": r["id"], "source_ref": r["source_ref"],
                     "chunk_index": r["chunk_index"], "chunk_text": r["chunk_text"]}
                    for r in rows
                ]
            finally:
                conn.close()

        async def _doc_vector_search(self, data_source_id, query_text, limit):
            import numpy as np
            from app.agent.embed import embed_text
            try:
                qv = np.array(await embed_text(query_text), dtype=np.float32)
            except Exception as e:
                logger.warning("doc_chunks query embed failed: %s", e)
                return []
            conn = self._get_conn()
            try:
                rows = conn.execute(
                    """SELECT id, source_ref, chunk_index, chunk_text, (embedding <=> ?) AS dist
                       FROM doc_chunks
                       WHERE data_source_id = ? AND embedding IS NOT NULL
                       ORDER BY dist ASC LIMIT ?""",
                    (qv, data_source_id, limit),
                ).fetchall()
                return [
                    {"id": r["id"], "source_ref": r["source_ref"],
                     "chunk_index": r["chunk_index"], "chunk_text": r["chunk_text"]}
                    for r in rows
                ]
            finally:
                conn.close()

    return _PostgresBackend


def build_postgres_backend(cfg: DBConnectionConfig, password: Optional[str] = None, seed: bool = True):
    """Instantiate the live Postgres backend. seed=False builds the schema only
    (used by the SQLite→Postgres migration, where SQLite rows are the source of truth)."""
    cls = _make_postgres_backend_class()
    return cls(cfg, password=password, seed=seed)
