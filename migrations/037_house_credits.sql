-- Immutable house-credit metering for new usage events. Existing rows remain zero.
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS credit_units DOUBLE PRECISION NOT NULL DEFAULT 0;
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS credit_source TEXT;
