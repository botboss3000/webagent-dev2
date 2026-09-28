-- ── Migration 013: agent_templates.discoverable ─────────────────────────────
-- Adds a discoverable flag to agent_templates so admins can control which
-- templates appear in the "New Agent" creation dropdown.
--
-- Apply via Supabase SQL editor or psql.
-- The local SQLite backend (app/db/local.py) applies this automatically
-- at startup via its in-process migration runner.
-- ---------------------------------------------------------------------------

ALTER TABLE agent_templates ADD COLUMN IF NOT EXISTS discoverable INTEGER NOT NULL DEFAULT 0;

-- Seed: make the default template visible in the creation dropdown.
UPDATE agent_templates SET discoverable = 1 WHERE id = 'default';
