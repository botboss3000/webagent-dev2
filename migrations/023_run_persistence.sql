-- 023_run_persistence.sql
-- Durable, connection-independent agent runs.
--
-- 1. interactions.status — lifecycle of a message row. Existing rows are all
--    finished, so they default to 'complete'. New assistant rows are written
--    'streaming' and flipped to 'complete' / 'interrupted' / 'error' by the
--    agent loop as the answer is generated, so the partial answer is durable
--    (survives RunBuffer eviction and server restarts).
--
-- 2. session_runs — one row per session describing the in-flight (or recently
--    finished) turn. Lets a cold device / a second device / a process that just
--    restarted learn from the DB alone that a run is in progress and where the
--    live stream is up to, without depending on the in-memory RunBuffer.
--
-- Apply on Supabase via the SQL editor. Local SQLite auto-migrates via
-- app/db/local.py. (NOTE: full run-state tracking is implemented for
-- LocalBackend; on Supabase the run still works and degrades to RAM-only live
-- replay until the SupabaseBackend methods are ported.)

ALTER TABLE interactions ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'complete';

CREATE TABLE IF NOT EXISTS session_runs (
    session_id TEXT PRIMARY KEY REFERENCES sessions(id),
    user_id TEXT NOT NULL,
    agent_id TEXT,
    turn_id TEXT,
    assistant_interaction_id TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    latest_session_seq INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_session_runs_user_status
    ON session_runs (user_id, status);
