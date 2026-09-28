-- 018_agent_prompt_templates.sql
--
-- Splits canonical prompt-slot templates out of agent_prompts into a dedicated
-- agent_prompt_templates table. Adds versioning + source tracking so JSON
-- re-seed never silently overwrites admin edits.
--
-- Also adds the app_meta key/value store used for the manifest-hash short-circuit
-- in the seeder and other cross-cutting runtime flags.
--
-- Apply on Supabase via the SQL editor. Local SQLite auto-migrates via
-- app/db/local.py (migrations 025 + 026).

-- 1. Canonical prompt-slot templates ──────────────────────────────────────
CREATE TABLE IF NOT EXISTS agent_prompt_templates (
    id              TEXT PRIMARY KEY,
    template_id     TEXT NOT NULL,
    slot_name       TEXT NOT NULL,
    order_index     INTEGER NOT NULL DEFAULT 0,
    lock            INTEGER NOT NULL DEFAULT 0,
    merge_mode      TEXT NOT NULL DEFAULT 'replace',
    content         TEXT NOT NULL DEFAULT '',
    version         INTEGER NOT NULL DEFAULT 1,
    source          TEXT NOT NULL DEFAULT 'json',
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by      TEXT NOT NULL DEFAULT 'system',
    UNIQUE (template_id, slot_name),
    CHECK (source IN ('json','admin'))
);

CREATE INDEX IF NOT EXISTS idx_agent_prompt_templates_tpl
    ON agent_prompt_templates(template_id);

-- 2. Generic key/value runtime metadata store ─────────────────────────────
CREATE TABLE IF NOT EXISTS app_meta (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL DEFAULT '',
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 3. Track which template version a per-agent slot row was cloned from ────
ALTER TABLE agent_prompts ADD COLUMN IF NOT EXISTS template_version INTEGER;

-- 4. One-shot copy: move admin-base slot rows into the new template table ─
-- Pre-refactor, agent_prompts double-dutied as both per-agent rows AND
-- template defaults (the latter identified by user_id IS NULL + agent_id
-- matching agent_templates.id). Move them once, then drop from agent_prompts.
-- ON CONFLICT skip preserves any rows already there (idempotent re-run).
INSERT INTO agent_prompt_templates
    (id, template_id, slot_name, order_index, lock, merge_mode, content,
     version, source, updated_at, updated_by)
SELECT
    -- gen_random_uuid() requires pgcrypto on older Supabase projects;
    -- md5 of inputs is a stable fallback that needs no extension.
    md5(ap.agent_id || '|' || ap.slot_name) AS id,
    ap.agent_id   AS template_id,
    ap.slot_name,
    COALESCE(ap.order_index, 0),
    COALESCE(ap.lock, 0),
    COALESCE(ap.merge_mode, 'replace'),
    COALESCE(ap.content, ''),
    1            AS version,
    'json'       AS source,
    COALESCE(ap.updated_at, now()),
    COALESCE(ap.updated_by, 'migration')
FROM agent_prompts ap
INNER JOIN agent_templates t ON t.id = ap.agent_id
WHERE ap.user_id IS NULL
ON CONFLICT (template_id, slot_name) DO NOTHING;

DELETE FROM agent_prompts
WHERE user_id IS NULL
  AND agent_id IN (SELECT id FROM agent_templates);

-- 5. Mark migration as applied (idempotent flag) ─────────────────────────
INSERT INTO app_meta (key, value, updated_at)
VALUES ('admin_base_migrated', '1', now())
ON CONFLICT (key) DO UPDATE
    SET value = excluded.value,
        updated_at = excluded.updated_at;
