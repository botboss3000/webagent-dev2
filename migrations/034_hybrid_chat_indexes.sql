-- Long-session hybrid chat indexes.
-- The canonical schema applies these automatically for local SQLite and raw
-- Postgres installs; this migration covers already-provisioned Supabase projects.

CREATE INDEX IF NOT EXISTS idx_interactions_session_created
    ON interactions (session_id, created_at);

CREATE INDEX IF NOT EXISTS idx_session_runs_user_status
    ON session_runs (user_id, status);

CREATE INDEX IF NOT EXISTS idx_session_runs_status_heartbeat
    ON session_runs (status, heartbeat_at);
