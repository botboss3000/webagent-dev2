-- 021_billing_platform.sql
-- PLATFORM (marketplace) billing tier — OPTIONAL. Apply only on a deployment that
-- ships the plugins/admin/billing add-on. A public / agent-only edition omits
-- this file entirely (stripped by scripts/build_edition.py), so its schema never
-- exists there and the core billing schema (020_billing.sql) carries no platform
-- columns or tables.
--
-- Adds 3 platform-only tables: platform_billing_config, payment_accounts,
-- platform_ledger. The SQLite build self-installs the same tables at runtime via
-- plugins/admin/billing/store.ensure_schema(); this file is the Supabase/Postgres
-- equivalent (apply via the SQL editor).

-- Platform-wide pricing policy + the platform fee. Exactly one row (id='platform').
CREATE TABLE IF NOT EXISTS platform_billing_config (
    id                         TEXT    PRIMARY KEY DEFAULT 'platform',
    strategy                   TEXT    NOT NULL DEFAULT 'free',
    allowed_strategies         TEXT    NOT NULL DEFAULT '[]',
    allowed_processors         TEXT    NOT NULL DEFAULT '[]',
    rate_card_default_llm      TEXT    NOT NULL DEFAULT '{}',
    rate_card_byo_llm          TEXT    NOT NULL DEFAULT '{}',
    platform_fee_pct           REAL    NOT NULL DEFAULT 0,
    platform_fee_flat_cents    INTEGER NOT NULL DEFAULT 0,
    trial_config               TEXT    NOT NULL DEFAULT '{}',
    subscription_price_cents   INTEGER NOT NULL DEFAULT 0,
    currency                   TEXT    NOT NULL DEFAULT 'usd',
    updated_at                 TIMESTAMP,
    updated_by                 TEXT
);

-- Per-agent-admin payout (Connect) onboarding state, one row per processor.
CREATE TABLE IF NOT EXISTS payment_accounts (
    user_id              TEXT NOT NULL,
    processor            TEXT NOT NULL,
    external_account_id  TEXT,
    onboarding_complete  INTEGER NOT NULL DEFAULT 0,
    metadata             TEXT NOT NULL DEFAULT '{}',
    created_at           TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at           TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, processor)
);

-- The platform's cut, per charge. One row per usage_event the platform took a
-- cut on (the agent tier records the charge itself in usage_events; this is the
-- platform-side split, kept entirely out of the core schema).
CREATE TABLE IF NOT EXISTS platform_ledger (
    id                     TEXT PRIMARY KEY,
    usage_event_id         TEXT,
    agent_id               TEXT,
    user_id                TEXT,
    end_user_charge_cents  INTEGER NOT NULL DEFAULT 0,
    platform_fee_cents     INTEGER NOT NULL DEFAULT 0,
    agent_earnings_cents   INTEGER NOT NULL DEFAULT 0,
    interaction_id         TEXT,
    created_at             TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_platform_ledger_agent ON platform_ledger (agent_id);
CREATE INDEX IF NOT EXISTS idx_platform_ledger_event ON platform_ledger (usage_event_id);
