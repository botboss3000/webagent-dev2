-- MODEL-ROW-USAGE: attribute each usage event to the exact saved
-- model-connection row (roster entry_id) that served it. model+provider can
-- repeat across rows — the same model saved twice with a different credential,
-- reasoning effort, or prompt injections — so per-row usage must key on the
-- stable entry_id. Existing rows stay NULL and are attributed by the legacy
-- model/provider fallback; they are never guessed onto a specific row.
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS entry_id TEXT;

CREATE INDEX IF NOT EXISTS idx_usage_events_entry_created
    ON usage_events(entry_id, created_at);
