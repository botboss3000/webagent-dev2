-- 024_row_ordering.sql
-- Manual row ordering for the agents and sessions dropdowns.
--
-- sort_order is a nullable integer holding the user's drag-to-reorder position
-- (0 = top). NULL means "never manually ordered" and the row falls back to its
-- natural order (agents: created_at ASC; unpinned sessions: latest interaction
-- activity DESC). Only pinned sessions consume manual sort_order.
-- The agent ordering is consumed by /api/v1/agents (and mirrored on the Agents
-- page); the session ordering is consumed by /api/v1/db/sessions.
--
-- Apply on Supabase via the SQL editor. Local SQLite auto-migrates via
-- app/db/local.py. (NOTE: the multi-agent system currently runs on the
-- LocalBackend only; the agents column is included here for schema parity.)

ALTER TABLE agents   ADD COLUMN IF NOT EXISTS sort_order INTEGER;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS sort_order INTEGER;
