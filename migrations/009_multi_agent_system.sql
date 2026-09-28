-- ── Migration 009: Multi-Agent System ──────────────────────────────────────
-- Adds the user_profiles table, extends agent_templates and agents with
-- multi-agent fields, and seeds the admin-agent template row.
--
-- Apply via Supabase SQL editor or psql.
-- The local SQLite backend (app/db/local.py) applies these automatically
-- at startup via its in-process migration runner.
-- ---------------------------------------------------------------------------

-- ── user_profiles ──────────────────────────────────────────────────────────
-- Tracks per-user admin flag and preferred default agent.
CREATE TABLE IF NOT EXISTS user_profiles (
    user_id          TEXT PRIMARY KEY,
    is_admin         INTEGER NOT NULL DEFAULT 0,   -- 1 = admin
    default_agent_id TEXT,                          -- template id or agents.id UUID
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ── agent_templates: new multi-agent columns ───────────────────────────────
ALTER TABLE agent_templates ADD COLUMN IF NOT EXISTS name               TEXT NOT NULL DEFAULT '';
ALTER TABLE agent_templates ADD COLUMN IF NOT EXISTS description        TEXT NOT NULL DEFAULT '';
ALTER TABLE agent_templates ADD COLUMN IF NOT EXISTS icon               TEXT NOT NULL DEFAULT '';
ALTER TABLE agent_templates ADD COLUMN IF NOT EXISTS can_be_default     INTEGER NOT NULL DEFAULT 1;
ALTER TABLE agent_templates ADD COLUMN IF NOT EXISTS is_system          INTEGER NOT NULL DEFAULT 0;
ALTER TABLE agent_templates ADD COLUMN IF NOT EXISTS is_pipeline        INTEGER NOT NULL DEFAULT 0;
ALTER TABLE agent_templates ADD COLUMN IF NOT EXISTS access_level       TEXT NOT NULL DEFAULT 'all';
ALTER TABLE agent_templates ADD COLUMN IF NOT EXISTS trigger_description TEXT NOT NULL DEFAULT '';

-- ── agents: new multi-agent / ownership columns ────────────────────────────
ALTER TABLE agents ADD COLUMN IF NOT EXISTS owner_user_id   TEXT;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS is_user_default INTEGER NOT NULL DEFAULT 0;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS name            TEXT NOT NULL DEFAULT '';
ALTER TABLE agents ADD COLUMN IF NOT EXISTS description     TEXT NOT NULL DEFAULT '';

-- ── Backfill: mark existing default agents as user-owned ───────────────────
UPDATE agents
SET owner_user_id = user_id
WHERE owner_user_id IS NULL
  AND (template_id = 'default' OR template_id IS NULL)
  AND user_id NOT LIKE 'opt_%';

UPDATE agents
SET is_user_default = 1
WHERE (template_id = 'default' OR template_id IS NULL)
  AND user_id NOT LIKE 'opt_%'
  AND is_user_default = 0;

-- ── Seed: update existing template rows with new metadata ──────────────────
UPDATE agent_templates SET
    name        = 'Default',
    description = 'General-purpose assistant. Starting agent for all new sessions.',
    icon        = '🤖',
    can_be_default = 1,
    is_system   = 1,
    is_pipeline = 0,
    access_level = 'all'
WHERE id = 'default';

UPDATE agent_templates SET
    name        = 'Optimizer Planner',
    description = 'Pipeline agent — plans and structures optimizer passes.',
    icon        = '📋',
    can_be_default = 0,
    is_system   = 1,
    is_pipeline = 1,
    access_level = 'all'
WHERE id = 'opt_planner';

UPDATE agent_templates SET
    name        = 'Optimizer Closer',
    description = 'Pipeline agent — evaluates trial results and closes out the optimization.',
    icon        = '🔒',
    can_be_default = 0,
    is_system   = 1,
    is_pipeline = 1,
    access_level = 'all'
WHERE id = 'opt_closer';

-- ── Seed: admin-agent template (insert if not present) ─────────────────────
-- system_prompt and bootstrap_tools are intentionally left blank here;
-- the full content is loaded from app/context/agents/admin-agent.json at
-- server startup by the JSON seeder.
INSERT INTO agent_templates (
    id, name, description, icon,
    can_be_default, is_system, is_pipeline, access_level,
    system_prompt, bootstrap_tools,
    max_turn_count, model, provider, temperature, max_tokens,
    agent_prompt, user_prompt, skills_prompt, tasks_prompt, misc_prompt,
    metadata, created_at, updated_at
)
SELECT
    'admin-agent', 'Admin', 'Privileged management agent for codebase and server operations.', '🔧',
    0, 1, 0, 'admin_only',
    '', '',
    20, '', '', 0.3, 8192,
    '', '', '', '', '',
    '{}', datetime('now'), datetime('now')
WHERE NOT EXISTS (SELECT 1 FROM agent_templates WHERE id = 'admin-agent');
