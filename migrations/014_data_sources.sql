-- ── Migration 014: per-agent external data source connectors ───────────────
-- Adds three tables and an optional pgvector-backed hybrid search RPC.
--
-- Apply via Supabase SQL editor or psql.
-- The local SQLite backend (app/db/local.py) applies the equivalent schema
-- automatically at startup via its in-process bootstrap.
-- ----------------------------------------------------------------------------

-- Optional: pgvector for embedding-backed doc search. Skip if you don't plan
-- to use the doc_store connector in cloud mode.
CREATE EXTENSION IF NOT EXISTS vector;

-- ── data_sources ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS data_sources (
    id                    TEXT PRIMARY KEY,
    user_id               TEXT NOT NULL,
    name                  TEXT NOT NULL,
    type                  TEXT NOT NULL CHECK (type IN (
                              'sql_postgres','sql_mysql','rest_api',
                              'doc_store','web_search_domain',
                              'notion','confluence','shopify',
                              'airtable','google_sheets'
                          )),
    config                JSONB NOT NULL DEFAULT '{}'::jsonb,
    auth_element_id       TEXT,
    schema_cache          JSONB NOT NULL DEFAULT '{}'::jsonb,
    safety_policy         JSONB NOT NULL DEFAULT '{}'::jsonb,
    status                TEXT NOT NULL DEFAULT 'unverified'
                          CHECK (status IN ('unverified','active','error','disabled')),
    last_test_message     TEXT,
    last_tested_at        TIMESTAMPTZ,
    last_introspected_at  TIMESTAMPTZ,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, name)
);

CREATE INDEX IF NOT EXISTS idx_data_sources_user ON data_sources(user_id);
CREATE INDEX IF NOT EXISTS idx_data_sources_type ON data_sources(type);

-- ── agent_data_sources (many-to-many link) ──────────────────────────────────
CREATE TABLE IF NOT EXISTS agent_data_sources (
    id                       TEXT PRIMARY KEY,
    agent_id                 TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    data_source_id           TEXT NOT NULL REFERENCES data_sources(id) ON DELETE CASCADE,
    tool_alias               TEXT,
    enabled                  BOOLEAN NOT NULL DEFAULT TRUE,
    inject_schema_in_prompt  BOOLEAN NOT NULL DEFAULT TRUE,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (agent_id, data_source_id)
);

CREATE INDEX IF NOT EXISTS idx_agent_data_sources_agent  ON agent_data_sources(agent_id);
CREATE INDEX IF NOT EXISTS idx_agent_data_sources_source ON agent_data_sources(data_source_id);

-- ── doc_chunks (mirror of memory_chunks, scoped to a data source) ───────────
CREATE TABLE IF NOT EXISTS doc_chunks (
    id              TEXT PRIMARY KEY,
    data_source_id  TEXT NOT NULL REFERENCES data_sources(id) ON DELETE CASCADE,
    source_ref      TEXT NOT NULL DEFAULT '',
    chunk_index     INTEGER NOT NULL DEFAULT 0,
    chunk_text      TEXT NOT NULL,
    content_hash    TEXT,
    embedding       vector(1536),
    token_count     INTEGER,
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_doc_chunks_source ON doc_chunks(data_source_id);
CREATE INDEX IF NOT EXISTS idx_doc_chunks_hash   ON doc_chunks(content_hash);

-- Full-text + vector indexes — choose lists tuned to your corpus size.
CREATE INDEX IF NOT EXISTS idx_doc_chunks_fts
    ON doc_chunks USING gin (to_tsvector('english', chunk_text));
CREATE INDEX IF NOT EXISTS idx_doc_chunks_embed
    ON doc_chunks USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);

-- ── Optional: hybrid search RPC consumed by SupabaseBackend.doc_chunk_search
-- This is best-effort: app falls back to ILIKE if the RPC is missing.
CREATE OR REPLACE FUNCTION doc_chunks_hybrid_search(
    p_data_source_id TEXT,
    p_query TEXT,
    p_limit INTEGER DEFAULT 5
)
RETURNS TABLE (
    id           TEXT,
    source_ref   TEXT,
    chunk_index  INTEGER,
    chunk_text   TEXT,
    rank         REAL
)
LANGUAGE sql STABLE AS $$
    SELECT id, source_ref, chunk_index, chunk_text,
           ts_rank_cd(to_tsvector('english', chunk_text), plainto_tsquery('english', p_query)) AS rank
    FROM doc_chunks
    WHERE data_source_id = p_data_source_id
      AND to_tsvector('english', chunk_text) @@ plainto_tsquery('english', p_query)
    ORDER BY rank DESC
    LIMIT p_limit;
$$;
