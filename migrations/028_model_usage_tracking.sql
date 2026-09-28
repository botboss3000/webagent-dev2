-- Track which model+provider each usage event used, so the Agent Settings
-- saved-models table can show real per-model token/cost totals.

ALTER TABLE usage_events ADD COLUMN model      TEXT;
ALTER TABLE usage_events ADD COLUMN provider   TEXT;