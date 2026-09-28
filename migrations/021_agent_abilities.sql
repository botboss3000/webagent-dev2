-- 021_agent_abilities.sql
-- Three-tier OAuth ability system. Each agent can opt into fine-grained
-- OAuth capabilities (e.g. google.gmail_read, google.gmail_send) and pick
-- whether to use the platform's OAuth credentials or supply their own (BYO).
--
-- Apply on Supabase via the SQL editor. Local SQLite picks this up from
-- app/db/local.py:SCHEMA_SQL on next startup.

CREATE TABLE IF NOT EXISTS agent_abilities (
    id                    TEXT PRIMARY KEY,
    agent_id              TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    ability_id            TEXT NOT NULL,
    source                TEXT NOT NULL DEFAULT 'platform',
    enabled               INTEGER NOT NULL DEFAULT 0,
    byo_client_id         TEXT NOT NULL DEFAULT '',
    byo_client_secret_ref TEXT NOT NULL DEFAULT '',
    config                TEXT NOT NULL DEFAULT '{}',
    created_at            TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at            TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(agent_id, ability_id)
);

CREATE INDEX IF NOT EXISTS idx_agent_ability_agent ON agent_abilities(agent_id);
CREATE INDEX IF NOT EXISTS idx_agent_ability_id    ON agent_abilities(ability_id);
