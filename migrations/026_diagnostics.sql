-- 026_diagnostics.sql
-- Diagnostic flight-recorder durable store (see app/agent/diagnostics.py).
--
-- A rolling, auto-pruned log of the server's and agent loop's internal
-- activity, so an operator — or a diagnostic AI agent via the read_diagnostics
-- tool — can inspect what has been happening and what went wrong after the fact:
--
--   • server — WARNING/ERROR log records (with tracebacks) from stdlib logging
--   • loop   — agent-loop pipeline problems (guardrail trips, stalls, errors)
--   • run    — run lifecycle outcomes (complete / interrupted / error / resume)
--   • tool   — tool execution errors
--
-- The recorder keeps a fast in-memory ring AND writes warnings/errors/run
-- outcomes here for durability. A background pruner caps rows + age.
--
-- session_id / agent_id are plain TEXT (NOT foreign keys) on purpose: a
-- diagnostic record must outlive the session/agent it referenced, and deleting a
-- session must never cascade its diagnostics away.
--
-- Apply on Supabase via the SQL editor. Local SQLite auto-migrates via
-- app/db/local.py. (On Supabase the recorder degrades to RAM-only until the
-- SupabaseBackend diagnostics methods are ported.)

CREATE TABLE IF NOT EXISTS diagnostics (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    level TEXT NOT NULL,
    category TEXT NOT NULL,
    source TEXT,
    message TEXT NOT NULL,
    detail TEXT,
    session_id TEXT,
    turn_id TEXT,
    agent_id TEXT,
    user_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_diagnostics_ts ON diagnostics (ts);
CREATE INDEX IF NOT EXISTS idx_diagnostics_level ON diagnostics (level);
CREATE INDEX IF NOT EXISTS idx_diagnostics_category ON diagnostics (category);
CREATE INDEX IF NOT EXISTS idx_diagnostics_session ON diagnostics (session_id);
