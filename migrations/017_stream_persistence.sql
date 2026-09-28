-- 017_stream_persistence.sql
-- Adds session_seq, turn_id, turn_seq columns to interactions for
-- end-of-turn batch persistence of streamed events. Old rows keep NULL
-- session_seq; callers fall back to created_at when session_seq IS NULL.
--
-- Apply on Supabase via SQL editor. Local SQLite auto-migrates via
-- app/db/local.py.

ALTER TABLE interactions ADD COLUMN IF NOT EXISTS session_seq INTEGER;
ALTER TABLE interactions ADD COLUMN IF NOT EXISTS turn_id TEXT;
ALTER TABLE interactions ADD COLUMN IF NOT EXISTS turn_seq INTEGER;

CREATE INDEX IF NOT EXISTS idx_interactions_session_seq
    ON interactions (session_id, session_seq);

CREATE INDEX IF NOT EXISTS idx_interactions_turn
    ON interactions (turn_id);
