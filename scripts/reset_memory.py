"""Targeted reset of the cross-session knowledge store (memories only).

Unlike the full app reset (TUI ``reset.py``, which wipes the whole user-data DB),
this clears ONLY the memory engine: it deletes every ``memories`` + ``memory_chunks``
row and drops the retired ``memory_links`` / ``memory_timeline`` tables. Everything
else (sessions, interactions, agents, credentials) is untouched.

Use it once when retiring the old memory feature so the new cross-session
knowledge engine starts from a clean slate. Safe to re-run (idempotent).

Backend-aware (mirrors ``app/db_connection.json`` / env selection, same as the
reset tool):

* **SQLite** (the default when no Postgres is configured) — clears every local
  ``*.db`` that has a ``memories`` table. No external deps.
* **Postgres-family** — connects through the app's own config resolution and
  clears the active schema. Requires the project venv (psycopg).

    python scripts/reset_memory.py [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_PG_PROVIDERS = {"postgres", "neon", "gcp_cloud_sql"}

# Current split-storage authorities that can own memory tables.
_SQLITE_CANDIDATES = [
    str(path.relative_to(ROOT))
    for path in (ROOT / "data" / "user_data").glob("*/*.db")
    if not path.name.endswith(".user-secrets.db")
]

_DEAD_TABLES = ("memory_links", "memory_timeline")


def _active_provider() -> str:
    """Which backend the app will use — mirrors the reset tool's detection."""
    if os.environ.get("WEBAGENT_CONFIG_SOURCE", "").lower() == "env":
        return os.environ.get("WEBAGENT_DB_PROVIDER", "sqlite") or "sqlite"
    try:
        data = json.loads((ROOT / "app" / "db_connection.json").read_text(encoding="utf-8"))
        return str(data.get("provider") or "sqlite")
    except (OSError, json.JSONDecodeError, ValueError):
        return "sqlite"


def _reset_sqlite(dry_run: bool) -> int:
    touched = 0
    for rel in _SQLITE_CANDIDATES:
        path = ROOT / rel
        if not path.exists():
            continue
        conn = sqlite3.connect(str(path))
        try:
            tabs = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if "memories" not in tabs:
                continue
            mem = conn.execute("SELECT count(*) FROM memories").fetchone()[0]
            chunks = (conn.execute("SELECT count(*) FROM memory_chunks").fetchone()[0]
                      if "memory_chunks" in tabs else 0)
            dead_present = [t for t in _DEAD_TABLES if t in tabs]
            print(f"{rel}: memories={mem} chunks={chunks} dead_tables={dead_present or 'none'}")
            if dry_run:
                touched += 1
                continue
            # memory_chunks cascades from memories, but delete explicitly too in
            # case foreign keys aren't enforced on this connection.
            if "memory_chunks" in tabs:
                conn.execute("DELETE FROM memory_chunks")
            conn.execute("DELETE FROM memories")
            for t in dead_present:
                conn.execute(f"DROP TABLE IF EXISTS {t}")
            conn.commit()
            print(f"  -> cleared {rel}")
            touched += 1
        finally:
            conn.close()
    if touched == 0:
        print("No SQLite database with a memories table found.")
    return touched


def _reset_postgres(dry_run: bool) -> int:
    try:
        import psycopg  # noqa
        from app.db.connection_config import load_config
        from app.db import _resolve_pg_password
        from app.db.postgres_backend import make_conninfo
    except Exception as e:  # pragma: no cover
        print(f"ERROR: Postgres reset needs the project venv (psycopg + app): {e}")
        return 0
    cfg = load_config()
    schema = getattr(cfg, "schema", None) or "public"
    conninfo = make_conninfo(cfg, _resolve_pg_password(cfg))
    import psycopg
    with psycopg.connect(conninfo, autocommit=True) as conn:
        def _count(t):
            r = conn.execute("SELECT to_regclass(%s)", (f"{schema}.{t}",)).fetchone()[0]
            if not r:
                return None
            return conn.execute(f'SELECT count(*) FROM "{schema}"."{t}"').fetchone()[0]
        mem, chunks = _count("memories"), _count("memory_chunks")
        dead_present = [t for t in _DEAD_TABLES
                        if conn.execute("SELECT to_regclass(%s)", (f"{schema}.{t}",)).fetchone()[0]]
        print(f"postgres {schema}: memories={mem} chunks={chunks} dead_tables={dead_present or 'none'}")
        if dry_run:
            return 1
        if chunks is not None:
            conn.execute(f'DELETE FROM "{schema}"."memory_chunks"')
        if mem is not None:
            conn.execute(f'DELETE FROM "{schema}"."memories"')
        for t in dead_present:
            conn.execute(f'DROP TABLE IF EXISTS "{schema}"."{t}" CASCADE')
        print("  -> cleared postgres memory store")
    return 1


def main() -> None:
    ap = argparse.ArgumentParser(description="Clear the memory engine (memories only)")
    ap.add_argument("--dry-run", action="store_true", help="Report only, change nothing")
    args = ap.parse_args()

    provider = _active_provider()
    print(f"Active backend: {provider}")
    if provider in _PG_PROVIDERS:
        _reset_postgres(args.dry_run)
    else:
        _reset_sqlite(args.dry_run)
    print("Done." + (" (dry run -- nothing changed)" if args.dry_run else ""))


if __name__ == "__main__":
    main()
