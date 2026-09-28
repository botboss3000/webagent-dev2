-- 025_run_self_healing.sql
-- Self-healing / auto-resume bookkeeping on session_runs.
--
-- Extends the durable run record (023_run_persistence.sql) so a run that stops
-- INVOLUNTARILY — a server restart, a task that vanished, or a frozen await —
-- can be detected and re-ignited from durable conversation history, with no
-- dependence on any user WebSocket. The boot recovery and the liveness watchdog
-- (app/agent/runner.py + app/agent/watchdog.py) read these columns; the loop
-- (app/agent/loop.py) writes heartbeat_at while alive.
--
-- Column meanings:
--   stop_cause   machine taxonomy of WHY a run ended (error holds the human reason):
--                complete | user_stop | replaced | server_restart | zombie |
--                frozen | crash | failed | needs_manual_resume   (NULL while running)
--   origin       launch source — drives resume eligibility + how to relaunch:
--                web | automation | event | inbound | webhook | sandbox | optimizer
--   resume_attempts / max_resume_attempts   retry budget (per-row cap; NULL = global default)
--   resume_cause_counts (JSON)   per-error-type resume tally {cause: count} so a
--                single repeating cause can't burn the whole total budget
--   heartbeat_at         last time the live loop proved it is alive (distinct from updated_at)
--   next_resume_at       backoff gate — do not resume before this
--   owner_token / lease_expires_at   lease so a second worker can't double-run a live turn
--   relaunch_ctx (JSON)  recipe to rebuild a turn headlessly (no new user message)
--
-- Apply on Supabase via the SQL editor. Local SQLite auto-migrates via
-- app/db/local.py. (NOTE: full run-state + self-healing is implemented for the
-- LocalBackend, which is the production backend; on Supabase these columns are
-- added for schema parity but the SupabaseBackend run_state_* methods are not
-- yet ported, so self-healing degrades to a no-op there.)

ALTER TABLE session_runs ADD COLUMN IF NOT EXISTS stop_cause          TEXT;
ALTER TABLE session_runs ADD COLUMN IF NOT EXISTS origin              TEXT;
ALTER TABLE session_runs ADD COLUMN IF NOT EXISTS resume_attempts     INTEGER NOT NULL DEFAULT 0;
ALTER TABLE session_runs ADD COLUMN IF NOT EXISTS max_resume_attempts INTEGER;
ALTER TABLE session_runs ADD COLUMN IF NOT EXISTS resume_cause_counts TEXT;
ALTER TABLE session_runs ADD COLUMN IF NOT EXISTS heartbeat_at        TIMESTAMPTZ;
ALTER TABLE session_runs ADD COLUMN IF NOT EXISTS next_resume_at      TIMESTAMPTZ;
ALTER TABLE session_runs ADD COLUMN IF NOT EXISTS owner_token         TEXT;
ALTER TABLE session_runs ADD COLUMN IF NOT EXISTS lease_expires_at    TIMESTAMPTZ;
ALTER TABLE session_runs ADD COLUMN IF NOT EXISTS relaunch_ctx        TEXT;

CREATE INDEX IF NOT EXISTS idx_session_runs_status_heartbeat
    ON session_runs (status, heartbeat_at);

-- Per-agent opt-out: 0 = never auto-resume this agent's runs (hold for a
-- one-click manual resume instead). Default 1 = auto-resume eligible.
ALTER TABLE agents ADD COLUMN IF NOT EXISTS auto_resume INTEGER NOT NULL DEFAULT 1;
