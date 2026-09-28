-- Preserve provider-native cache/reasoning usage alongside each immutable call.
-- `cost_usd` remains the authoritative actual/provider or cache-aware cost.
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS cached_input_tokens INTEGER NOT NULL DEFAULT 0;
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS cache_write_tokens INTEGER NOT NULL DEFAULT 0;
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS uncached_input_tokens INTEGER;
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS reasoning_tokens INTEGER NOT NULL DEFAULT 0;
