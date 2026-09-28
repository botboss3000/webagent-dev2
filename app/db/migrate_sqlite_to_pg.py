"""
One-time (idempotent) data migration: SQLite → Postgres.

Copies every row of every table that exists in BOTH the SQLite database and the
Postgres schema, in foreign-key dependency order. BLOB embedding columns are
converted to pgvector. Foreign-key checks are disabled during the bulk load
(SET session_replication_role = replica) so orphaned legacy rows don't block it.

Usage (programmatic):
    from app.db.connection_config import DBConnectionConfig
    from app.db.postgres_backend import build_postgres_backend, make_conninfo
    from app.db.migrate_sqlite_to_pg import migrate
    cfg = DBConnectionConfig(provider="postgres", host="localhost", port=5432,
                             database="webagent", username="webagent")
    build_postgres_backend(cfg, password="...", seed=False)   # ensure schema exists
    report = migrate("data/db/app.db", make_conninfo(cfg, "..."))
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Optional

logger = logging.getLogger(__name__)

# Columns that hold packed float32 embedding BLOBs in SQLite and pgvector in PG.
_EMBED_COLS = {"memory_chunks": "embedding", "doc_chunks": "embedding"}


def migrate(sqlite_path: str, conninfo: str, embed_dim: int = 1536) -> dict:
    import psycopg
    import numpy as np
    from pgvector.psycopg import register_vector
    from app.db.schema.tables import TABLES
    from app.db.schema.ddl_renderer import _sort_tables_by_deps

    sconn = sqlite3.connect(sqlite_path)
    sconn.row_factory = sqlite3.Row

    pconn = psycopg.connect(conninfo, autocommit=False)
    try:
        pconn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        pconn.commit()
    except Exception:
        pconn.rollback()
    register_vector(pconn)

    # Disable FK/triggers for the bulk load (requires table-owner/superuser).
    try:
        pconn.execute("SET session_replication_role = replica")
    except Exception as e:
        logger.warning("Could not disable FK checks (%s); inserting in dependency order only", e)

    # PK lookup so we can backfill NULL single-column primary keys (legacy rows).
    import uuid as _uuid
    pk_lookup: dict = {}
    for t in TABLES:
        pks = [c.name for c in t.columns if c.primary_key]
        if len(pks) == 1:
            pk_lookup[t.name] = pks[0]

    order = [t.name for t in _sort_tables_by_deps(TABLES)]
    report: dict = {}

    def _clean(v):
        # PostgreSQL TEXT cannot contain NUL (0x00); SQLite allows it.
        if isinstance(v, str) and "\x00" in v:
            return v.replace("\x00", "")
        return v

    for table in order:
        scols = [r[1] for r in sconn.execute(f"PRAGMA table_info({table})").fetchall()]
        if not scols:
            continue
        prows = pconn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name=%s",
            (table,),
        ).fetchall()
        pcols = {r[0] for r in prows}
        if not pcols:
            continue
        common = [c for c in scols if c in pcols]
        if not common:
            continue

        rows = sconn.execute(f"SELECT {', '.join(common)} FROM {table}").fetchall()
        emb_col = _EMBED_COLS.get(table)
        pk_col = pk_lookup.get(table)
        collist = ", ".join(f'"{c}"' for c in common)
        placeholders = ", ".join(["%s"] * len(common))
        sql = f'INSERT INTO {table} ({collist}) VALUES ({placeholders}) ON CONFLICT DO NOTHING'

        cur = pconn.cursor()
        inserted = 0
        failed = 0
        for r in rows:
            vals = []
            for c in common:
                v = r[c]
                if emb_col and c == emb_col and v is not None:
                    try:
                        arr = np.frombuffer(v, dtype=np.float32)
                        v = arr if arr.size == embed_dim else None
                    except Exception:
                        v = None
                elif c == pk_col and v is None:
                    v = str(_uuid.uuid4())  # backfill missing PK on legacy rows
                else:
                    v = _clean(v)
                vals.append(v)
            # Per-row savepoint so one bad row never rolls back the whole table.
            cur.execute("SAVEPOINT row_sp")
            try:
                cur.execute(sql, vals)
                if cur.rowcount and cur.rowcount > 0:
                    inserted += cur.rowcount
                cur.execute("RELEASE SAVEPOINT row_sp")
            except Exception as e:
                failed += 1
                cur.execute("ROLLBACK TO SAVEPOINT row_sp")
                logger.warning("Row insert failed for %s: %s", table, e)
        pconn.commit()
        report[table] = {"sqlite_rows": len(rows), "inserted": inserted, "failed": failed}
        logger.info("Migrated %s: %d rows, %d inserted, %d failed", table, len(rows), inserted, failed)

    try:
        pconn.execute("SET session_replication_role = DEFAULT")
        pconn.commit()
    except Exception:
        pass
    sconn.close()
    pconn.close()
    return report


def _main():
    """CLI: python -m app.db.migrate_sqlite_to_pg [sqlite_path]

    Reads Postgres target from WEBAGENT_DB_* env vars (host/port/name/user/
    password/sslmode). Builds the schema (no seed) then copies all rows.
    """
    import os
    import sys
    from app.db.connection_config import DBConnectionConfig
    from app.db.postgres_backend import build_postgres_backend, make_conninfo

    if len(sys.argv) <= 1:
        raise SystemExit(
            "Pass an explicit authority database path (app, user, or agent); "
            "there is no combined SQLite database after layout v2"
        )
    sqlite_path = sys.argv[1]
    cfg = DBConnectionConfig(
        provider="postgres",
        host=os.environ.get("WEBAGENT_DB_HOST", "localhost"),
        port=int(os.environ.get("WEBAGENT_DB_PORT", "5432")),
        database=os.environ.get("WEBAGENT_DB_NAME", "webagent"),
        username=os.environ.get("WEBAGENT_DB_USER", "webagent"),
        ssl_mode=os.environ.get("WEBAGENT_DB_SSLMODE", "require"),
    )
    password = os.environ.get("WEBAGENT_DB_PASSWORD", "")
    logging.basicConfig(level=logging.INFO)
    print(f"Building Postgres schema (no seed) at {cfg.host}:{cfg.port}/{cfg.database} …")
    be = build_postgres_backend(cfg, password=password, seed=False)
    be.close()
    print(f"Copying rows from {sqlite_path} …")
    report = migrate(sqlite_path, make_conninfo(cfg, password))
    total = sum(v["inserted"] for v in report.values())
    failed = sum(v.get("failed", 0) for v in report.values())
    print(f"Done. {total} rows inserted across {len(report)} tables; {failed} rows failed.")


if __name__ == "__main__":
    _main()

