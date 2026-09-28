"""
Dialect-specific DDL renderers.

Translates the canonical Table/Column/Index/FtsTable structures into
CREATE TABLE / CREATE INDEX statements for SQLite, Postgres, or MySQL.

Type translation table (canonical → dialect):
    TEXT       → TEXT (sqlite), TEXT (postgres), TEXT (mysql)
    INTEGER    → INTEGER, INTEGER, INT
    REAL       → REAL, DOUBLE PRECISION, DOUBLE
    BLOB       → BLOB, BYTEA, BLOB
    TIMESTAMP  → TEXT, TIMESTAMPTZ, TIMESTAMP
    JSON       → TEXT, JSONB, JSON

Default translation:
    CURRENT_TIMESTAMP → datetime('now') (sqlite), now() (pg/mysql)
"""

from dataclasses import replace
from typing import List, Optional
from app.db.schema.tables import (
    Table, Column, Index, FtsTable, Trigger,
    TABLES, INDEXES, FTS_TABLES, TRIGGERS,
)


DIALECTS = ("sqlite", "postgres", "mysql")

# Bump this whenever the rendered SQL changes in a way worth tracking. It is
# stamped into the first comment line of every rendered script so a copied SQL
# blob can be identified at a glance (e.g. to tell a freshly-served script from a
# stale browser-cached copy). Format: YYYY.MM.DD-N.
SCHEMA_SQL_VERSION = "2026.06.29-2"


_TYPE_MAP = {
    "sqlite": {
        "TEXT": "TEXT", "INTEGER": "INTEGER", "REAL": "REAL",
        "BLOB": "BLOB", "TIMESTAMP": "TEXT", "JSON": "TEXT", "VECTOR": "BLOB",
    },
    # NB: TIMESTAMP and JSON map to TEXT on Postgres (not TIMESTAMPTZ/JSONB).
    # The Postgres backend reuses the SQLite-dialect backend code, which stores
    # and reads these as ISO strings / json.dumps text. Keeping them TEXT gives
    # exact behavioural parity (psycopg would otherwise hand back datetime/dict
    # objects and break json.loads / lexicographic time comparisons). VECTOR is
    # the one native type (pgvector) — embeddings are handled by dedicated code.
    "postgres": {
        "TEXT": "TEXT", "INTEGER": "INTEGER", "REAL": "DOUBLE PRECISION",
        "BLOB": "BYTEA", "TIMESTAMP": "TEXT", "JSON": "TEXT", "VECTOR": "vector",
    },
    "mysql": {
        "TEXT": "TEXT", "INTEGER": "INT", "REAL": "DOUBLE",
        "BLOB": "BLOB", "TIMESTAMP": "TIMESTAMP", "JSON": "JSON", "VECTOR": "BLOB",
    },
}


def _render_default(default: str, dialect: str) -> str:
    if default == "CURRENT_TIMESTAMP":
        if dialect == "sqlite":
            return "(datetime('now'))"
        if dialect == "postgres":
            # Timestamp columns are TEXT on Postgres (parity with SQLite). Emit a
            # text value in the exact same format SQLite's datetime('now') uses
            # ('YYYY-MM-DD HH:MM:SS', UTC) so default-populated timestamps sort and
            # compare identically across both backends.
            return "(to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS'))"
        return "CURRENT_TIMESTAMP"
    return default


def _render_column(c: Column, dialect: str) -> str:
    col_type = _TYPE_MAP[dialect][c.type]
    # pgvector: render the dimensionality, e.g. vector(384). Other dialects store
    # the raw float32 bytes as BLOB, so no dimension is emitted. The width tracks
    # the ACTIVE embedding source (app default is local bge-small = 384), so a
    # fresh Postgres install creates the column at the size the app will actually
    # write; c.vector_dim is only the static fallback. A later source/model switch
    # resizes existing columns via the reindex ALTER — this only sizes new installs.
    if c.type == "VECTOR" and dialect == "postgres":
        dim = c.vector_dim
        try:
            from app.agent.embed import embed_dim as _active_embed_dim
            dim = _active_embed_dim() or dim
        except Exception:
            pass  # embed module unavailable at render time → static fallback
        if dim:
            col_type = f"vector({dim})"
    parts = [c.name, col_type]
    # MySQL: TEXT columns cannot have a literal DEFAULT — strip in that case
    has_default = c.default is not None
    if dialect == "mysql" and c.type == "TEXT" and has_default:
        has_default = False  # silently drop; renderer NB: schema impacted only on mysql

    # Primary key handled inline only for single-PK case; composites via constraints
    if c.primary_key:
        parts.append("PRIMARY KEY")
    if not c.nullable and not c.primary_key:
        parts.append("NOT NULL")
    if c.unique:
        parts.append("UNIQUE")
    if has_default:
        parts.append(f"DEFAULT {_render_default(c.default, dialect)}")
    if c.references:
        ref = f"REFERENCES {c.references}"
        if c.on_delete:
            ref += f" ON DELETE {c.on_delete}"
        parts.append(ref)
    return " ".join(parts)


def ensure_sqlite_plane_columns(conn, plane) -> None:
    """Add canonical non-key columns missing from an existing plane database."""
    from app.db.schema.ownership import tables_for_plane

    wanted = set(tables_for_plane(plane))
    for table in (item for item in TABLES if item.name in wanted):
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table.name,)
        ).fetchone()
        if not exists:
            continue
        current = {
            row[1] for row in conn.execute(f'PRAGMA table_info("{table.name}")').fetchall()
        }
        for column in table.columns:
            if column.name in current:
                continue
            if column.primary_key or column.unique:
                raise RuntimeError(
                    f"Cannot add missing key column {table.name}.{column.name} additively"
                )
            additive = replace(column, references=None, on_delete=None)
            conn.execute(
                f'ALTER TABLE "{table.name}" ADD COLUMN {_render_column(additive, "sqlite")}'
            )


def _render_table(t: Table, dialect: str, idempotent: bool = True) -> str:
    col_lines = [f"    {_render_column(c, dialect)}" for c in t.columns]
    for con in t.constraints:
        col_lines.append(f"    {con}")
    body = ",\n".join(col_lines)
    # `idempotent=False` drops the `IF NOT EXISTS` guard. This exists ONLY for the
    # Supabase SQL-Editor copy: its pre-run linter naively reads the word AFTER
    # "CREATE TABLE" as the table name, so "CREATE TABLE IF NOT EXISTS x" makes it
    # think the table is literally named "IF", fail to find RLS for "IF", and warn
    # ("...may be able to access IF"). Dropping IF NOT EXISTS lets it read the real
    # name and match the ENABLE ROW LEVEL SECURITY line. Every EXECUTION path keeps
    # idempotent=True (re-runnable; tolerates an already-created schema).
    exists = "IF NOT EXISTS " if idempotent else ""
    return f"CREATE TABLE {exists}{t.name} (\n{body}\n);"


def _render_rls(t: Table, idempotent: bool = True) -> str:
    """Postgres-only: lock a table to privileged roles AND satisfy Supabase's linters.

    Per table:
      1. ``ENABLE ROW LEVEL SECURITY`` — turns RLS on. Clears Supabase's
         'RLS disabled in public' error. The role WebAgent connects with bypasses
         RLS (Supabase 'service_role'; a self-hosted table owner bypasses by
         default), so the app keeps full access and is unaffected.
      2. ``CREATE POLICY … USING (false)`` — a deny-all policy for the public role
         group (anon/authenticated). Adding ANY policy clears Supabase's softer
         'RLS enabled, no policy' note, and ``USING (false)`` grants those keys
         nothing, so the lockdown is fully preserved. service_role/owner ignore
         the policy (they bypass RLS).

    ``idempotent=True`` (every EXECUTION path) prepends ``DROP POLICY IF EXISTS``
    so a re-run doesn't fail on ``CREATE POLICY`` (which has no IF NOT EXISTS).
    ``idempotent=False`` (the Supabase SQL-Editor copy) OMITS the DROP: Supabase's
    pre-run linter flags any ``DROP`` as a "destructive operation", and a one-time
    setup script on a fresh project has no policy to drop anyway.
    """
    pol = f"{t.name}_no_public_access"
    lines = [f"ALTER TABLE {t.name} ENABLE ROW LEVEL SECURITY;"]
    if idempotent:
        lines.append(f"DROP POLICY IF EXISTS {pol} ON {t.name};")
    lines.append(
        f"CREATE POLICY {pol} ON {t.name} AS PERMISSIVE FOR ALL\n"
        f"    TO public USING (false) WITH CHECK (false);"
    )
    return "\n".join(lines)


def _render_index(idx: Index, dialect: str, idempotent: bool = True) -> str:
    unique = "UNIQUE " if idx.unique else ""
    cols = idx.columns
    # SQLite tolerates COLLATE NOCASE / DESC inline; postgres/mysql do too for basic forms.
    if dialect == "mysql":
        # MySQL requires explicit length on TEXT; skip those indexes that target plain text columns
        # with no length spec. The schema doesn't use any such indexes today; if added later,
        # encode the length in the columns string (e.g. "name(64)").
        pass
    # idempotent=False (Supabase copy) drops `IF NOT EXISTS` here too: a one-time
    # script must contain NO `IF NOT EXISTS` anywhere, because Supabase's pre-run
    # linter misreads the `IF` token (even in CREATE INDEX) as a table name and
    # warns "...may be able to access IF".
    exists = "IF NOT EXISTS " if idempotent else ""
    return f"CREATE {unique}INDEX {exists}{idx.name} ON {idx.table}({cols});"


def _render_fts_sqlite(f: FtsTable) -> str:
    cols: List[str] = []
    for c in f.unindexed_columns:
        cols.append(f"{c} UNINDEXED")
    cols.extend(f.indexed_columns)
    cols_sql = ",\n    ".join(cols)
    return (
        f"CREATE VIRTUAL TABLE IF NOT EXISTS {f.name} USING fts5(\n"
        f"    {cols_sql},\n"
        f"    content='{f.content_table}',\n"
        f"    content_rowid='rowid',\n"
        f"    tokenize='porter unicode61'\n"
        f");"
    )


def _render_fts_postgres(f: FtsTable, idempotent: bool = True) -> str:
    # tsvector column lives on the content table; query layer uses to_tsvector inline.
    # Render as GIN expression index instead of a separate FTS table.
    expr = " || ' ' || ".join(f"coalesce({c}, '')" for c in f.indexed_columns)
    exists = "IF NOT EXISTS " if idempotent else ""  # see _render_index (Supabase copy = none)
    return (
        f"-- {f.name} (postgres GIN FTS index on {f.content_table})\n"
        f"CREATE INDEX {exists}{f.name}_gin ON {f.content_table}\n"
        f"    USING GIN (to_tsvector('english', {expr}));"
    )


def _render_fts_mysql(f: FtsTable) -> str:
    cols = ", ".join(f.indexed_columns)
    return (
        f"-- {f.name} (mysql FULLTEXT index on {f.content_table})\n"
        f"CREATE FULLTEXT INDEX {f.name}_ft ON {f.content_table}({cols});"
    )


def _render_trigger_sqlite(t: Trigger) -> str:
    return f"CREATE TRIGGER IF NOT EXISTS {t.name}\n{t.body.strip()};"


def _sort_tables_by_deps(tables: List[Table]) -> List[Table]:
    """
    Order tables so every foreign-key target is created before the table that
    references it. SQLite tolerates any order (FK targets are resolved lazily),
    but Postgres/MySQL require the referenced table to already exist at
    CREATE TABLE time. The canonical TABLES list is authoring-ordered, not
    dependency-ordered, so we topologically sort here.

    Self-references and references to tables outside this list are ignored.
    Stable: preserves original order among independent tables. Falls back to
    original order if a cycle is detected (none exist today).
    """
    by_name = {t.name: t for t in tables}
    deps = {
        t.name: {
            c.references.split("(")[0]
            for c in t.columns
            if c.references and c.references.split("(")[0] in by_name and c.references.split("(")[0] != t.name
        }
        for t in tables
    }
    ordered: List[Table] = []
    placed = set()
    remaining = [t.name for t in tables]  # preserves authoring order
    while remaining:
        progressed = False
        next_remaining = []
        for name in remaining:
            if deps[name] <= placed:
                ordered.append(by_name[name])
                placed.add(name)
                progressed = True
            else:
                next_remaining.append(name)
        remaining = next_remaining
        if not progressed:
            # Cycle / unresolvable — emit the rest in original order.
            ordered.extend(by_name[n] for n in remaining)
            break
    return ordered


def _render_all(
    dialect: str,
    idempotent: bool = True,
    *,
    tables: Optional[List[Table]] = None,
    indexes: Optional[List[Index]] = None,
    fts_tables: Optional[List[FtsTable]] = None,
    triggers: Optional[List[Trigger]] = None,
) -> str:
    selected_tables = TABLES if tables is None else tables
    selected_indexes = INDEXES if indexes is None else indexes
    selected_fts = FTS_TABLES if fts_tables is None else fts_tables
    selected_triggers = TRIGGERS if triggers is None else triggers
    parts: List[str] = []
    # NB: keep this text free of the literal tokens a naive SQL linter scans for
    # (no "CREATE TABLE", no "IF"+"EXISTS", no "DROP") so the header comment can't
    # itself trip Supabase's pre-run check.
    variant = ("re-runnable / idempotent — safe to run repeatedly"
               if idempotent else
               "one-time setup — Supabase SQL-Editor friendly")
    parts.append(
        f"-- WebAgent schema SQL  |  version {SCHEMA_SQL_VERSION}  |  "
        f"dialect: {dialect}  |  {variant}\n"
    )

    ordered = _sort_tables_by_deps(selected_tables)

    # Postgres: any embedding column uses pgvector's `vector` type, which does
    # not exist until the extension is enabled. Emit the enable FIRST (before any
    # CREATE TABLE that declares a vector column) so a fresh database / the
    # Supabase SQL Editor doesn't fail with `type "vector" does not exist`.
    # IF NOT EXISTS is the standard, linter-safe form for extensions (Supabase's
    # own docs use it) — the pre-run linter only mis-parses it on CREATE TABLE.
    if dialect == "postgres" and any(
        c.type == "VECTOR" for t in ordered for c in t.columns
    ):
        parts.append("CREATE EXTENSION IF NOT EXISTS vector;")

    for t in ordered:
        parts.append(_render_table(t, dialect, idempotent))
        # Postgres: immediately after each table, enable RLS + a deny-all policy
        # (see _render_rls). Co-located with the CREATE so Supabase's editor sees
        # the lock right next to the table it guards. Harmless self-hosted (the
        # owner bypasses RLS); the app is unaffected on both backends.
        if dialect == "postgres":
            parts.append(_render_rls(t, idempotent))

    for idx in selected_indexes:
        parts.append(_render_index(idx, dialect, idempotent))

    # Postgres: an ANN index for every pgvector column so similarity search is a
    # server-side index scan, not a sequential distance-to-every-row scan. HNSW
    # (vs ivfflat) needs no training pass and works on an empty/growing table, so
    # it fits tables that fill up incrementally (memory_chunks, doc_chunks). The
    # `vector_cosine_ops` opclass must match the `<=>` operator used by the search
    # queries, or the planner ignores the index. Best-effort: if the running
    # pgvector predates HNSW (<0.5.0) the CREATE fails and the bootstrap's
    # per-statement try/except simply leaves that column on the sequential scan.
    if dialect == "postgres":
        exists = "IF NOT EXISTS " if idempotent else ""
        for t in ordered:
            for c in t.columns:
                if c.type == "VECTOR":
                    parts.append(
                        f"-- ANN index for {t.name}.{c.name} (pgvector HNSW, cosine)\n"
                        f"CREATE INDEX {exists}idx_{t.name}_{c.name}_hnsw\n"
                        f"    ON {t.name} USING hnsw ({c.name} vector_cosine_ops);"
                    )

    for f in selected_fts:
        if dialect == "sqlite":
            parts.append(_render_fts_sqlite(f))
        elif dialect == "postgres":
            parts.append(_render_fts_postgres(f, idempotent))
        elif dialect == "mysql":
            parts.append(_render_fts_mysql(f))

    if dialect == "sqlite":
        for trig in selected_triggers:
            parts.append(_render_trigger_sqlite(trig))

    return "\n\n".join(parts) + "\n"


def render_sqlite() -> str:
    return _render_all("sqlite")


def render_postgres(idempotent: bool = True) -> str:
    """Render the Postgres schema.

    ``idempotent=True`` (default; every execution path) → re-runnable DDL
    (CREATE TABLE IF NOT EXISTS + DROP-guarded policies). ``idempotent=False`` →
    a clean one-time-setup script for the Supabase SQL Editor whose pre-run
    linter chokes on IF NOT EXISTS and DROP (see _render_table / _render_rls).
    """
    return _render_all("postgres", idempotent)


def render_mysql() -> str:
    return _render_all("mysql")


def render_plane(
    plane: str,
    dialect: str = "sqlite",
    *,
    idempotent: bool = True,
) -> str:
    """Render only the tables owned by one storage plane.

    Foreign keys that cross a plane boundary are removed from the physical DDL.
    The referenced IDs remain normal columns and are validated by the service
    layer/outbox reconciliation; neither SQLite ATTACH nor separate Postgres
    databases can provide a portable cross-plane FK.
    """
    from app.db.schema.ownership import StoragePlane, tables_for_plane

    selected_plane = StoragePlane(plane)
    names = set(tables_for_plane(selected_plane))
    selected: List[Table] = []
    for table in TABLES:
        if table.name not in names:
            continue
        columns = []
        for column in table.columns:
            target = column.references.split("(", 1)[0] if column.references else None
            columns.append(
                replace(column, references=None, on_delete=None)
                if target and target not in names
                else column
            )
        selected.append(replace(table, columns=columns))

    selected_indexes = [idx for idx in INDEXES if idx.table in names]
    selected_fts = [fts for fts in FTS_TABLES if fts.content_table in names]
    selected_triggers = [
        trigger
        for trigger in TRIGGERS
        if any(trigger.name.startswith(f"trg_{fts.content_table}_") for fts in selected_fts)
    ]
    return _render_all(
        dialect,
        idempotent,
        tables=selected,
        indexes=selected_indexes,
        fts_tables=selected_fts,
        triggers=selected_triggers,
    )


def render(dialect: str, idempotent: bool = True) -> str:
    if dialect not in DIALECTS:
        raise ValueError(f"Unknown dialect: {dialect}. Must be one of {DIALECTS}")
    return _render_all(dialect, idempotent)
