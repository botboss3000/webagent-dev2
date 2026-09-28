-- 019_pages_table.sql
--
-- Adds the `pages` table backing the Canvas page-builder workspace.
-- Used by DatabasePageStore (full HTML in `html`) and HybridPageStore
-- (metadata-only rows, `html` stays NULL — body lives on disk).
--
-- Apply on Supabase via the SQL editor. Local SQLite auto-migrates via
-- app/db/local.py (SCHEMA_SQL).

CREATE TABLE IF NOT EXISTS pages (
    id            TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL,
    slug          TEXT NOT NULL,
    title         TEXT NOT NULL,
    agent_context TEXT NOT NULL DEFAULT '',
    html          TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(user_id, slug)
);

CREATE INDEX IF NOT EXISTS idx_pages_user ON pages(user_id);
