"""
Canonical WebAgent schema as Python data structures.

One source of truth for all SQL dialects (SQLite, Postgres, MySQL).
The legacy embedded DDL in `app/db/local.py` (SCHEMA_SQL) and the
`migrations/*.sql` files are derived views of this same structure.

Use `render_sqlite()`, `render_postgres()`, `render_mysql()` from
`ddl_renderer` to emit dialect-specific DDL strings.

This module is intentionally pure-Python and dialect-agnostic. Do not
import sqlite3 / asyncpg from here.
"""

from app.db.schema.tables import TABLES, INDEXES, TRIGGERS, FTS_TABLES
from app.db.schema.ddl_renderer import (
    render_sqlite,
    render_postgres,
    render_mysql,
    render_plane,
    ensure_sqlite_plane_columns,
    DIALECTS,
)

__all__ = [
    "TABLES",
    "INDEXES",
    "TRIGGERS",
    "FTS_TABLES",
    "render_sqlite",
    "render_postgres",
    "render_mysql",
    "render_plane",
    "ensure_sqlite_plane_columns",
    "DIALECTS",
]
