-- 020_billing.sql
-- Agent-tier billing: an agent admin charges its users; the agent keeps 100%.
-- Adds 8 tables: billing_configs, usage_events, wallets, wallet_transactions,
-- subscriptions, trials, payments, billing_exemptions.
--
-- Apply on Supabase via the SQL editor. Local SQLite picks these up from
-- app/db/local.py:SCHEMA_SQL on next startup.

-- Per-agent pricing config, keyed by scope 'agent:<id>'.
CREATE TABLE IF NOT EXISTS billing_configs (
    scope                      TEXT    PRIMARY KEY,
    strategy                   TEXT    NOT NULL DEFAULT 'free',
    allowed_strategies         TEXT    NOT NULL DEFAULT '[]',
    allowed_processors         TEXT    NOT NULL DEFAULT '[]',
    rate_card_default_llm      TEXT    NOT NULL DEFAULT '{}',
    rate_card_byo_llm          TEXT    NOT NULL DEFAULT '{}',
    trial_config               TEXT    NOT NULL DEFAULT '{}',
    subscription_price_cents   INTEGER NOT NULL DEFAULT 0,
    currency                   TEXT    NOT NULL DEFAULT 'usd',
    created_at                 TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at                 TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_by                 TEXT,
    CHECK (strategy IN ('free','credits','per_message','per_token','subscription','trial'))
);

CREATE TABLE IF NOT EXISTS usage_events (
    id                          TEXT PRIMARY KEY,
    agent_id                    TEXT NOT NULL,
    user_id                     TEXT NOT NULL,
    interaction_id              TEXT,
    input_tokens                INTEGER NOT NULL DEFAULT 0,
    output_tokens               INTEGER NOT NULL DEFAULT 0,
    provider_cost_cents         INTEGER NOT NULL DEFAULT 0,
    end_user_charge_cents       INTEGER NOT NULL DEFAULT 0,
    agent_admin_earnings_cents  INTEGER NOT NULL DEFAULT 0,
    strategy                    TEXT NOT NULL DEFAULT 'free',
    is_byo_llm                  INTEGER NOT NULL DEFAULT 0,
    is_trial                    INTEGER NOT NULL DEFAULT 0,
    is_exempt                   INTEGER NOT NULL DEFAULT 0,
    created_at                  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_usage_events_agent_created
    ON usage_events (agent_id, created_at);
CREATE INDEX IF NOT EXISTS idx_usage_events_user_created
    ON usage_events (user_id, created_at);

CREATE TABLE IF NOT EXISTS wallets (
    id            TEXT PRIMARY KEY,
    owner_type    TEXT NOT NULL,
    owner_id      TEXT NOT NULL,
    balance_cents INTEGER NOT NULL DEFAULT 0,
    hold_cents    INTEGER NOT NULL DEFAULT 0,
    currency      TEXT NOT NULL DEFAULT 'usd',
    created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(owner_type, owner_id, currency),
    CHECK (owner_type IN ('user','agent_admin'))
);

CREATE TABLE IF NOT EXISTS wallet_transactions (
    id          TEXT PRIMARY KEY,
    wallet_id   TEXT NOT NULL,
    delta_cents INTEGER NOT NULL,
    kind        TEXT NOT NULL,
    ref_id      TEXT,
    note        TEXT,
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (kind IN ('purchase','usage','refund','earnings','hold','release'))
);

CREATE INDEX IF NOT EXISTS idx_wallet_tx_wallet ON wallet_transactions (wallet_id);

CREATE TABLE IF NOT EXISTS subscriptions (
    id                       TEXT PRIMARY KEY,
    user_id                  TEXT NOT NULL,
    agent_id                 TEXT NOT NULL,
    processor                TEXT NOT NULL,
    external_subscription_id TEXT,
    status                   TEXT NOT NULL DEFAULT 'pending',
    current_period_end       TIMESTAMP,
    created_at               TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at               TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, agent_id),
    CHECK (status IN ('pending','active','past_due','cancelled','expired'))
);

CREATE INDEX IF NOT EXISTS idx_subscriptions_user  ON subscriptions (user_id);
CREATE INDEX IF NOT EXISTS idx_subscriptions_agent ON subscriptions (agent_id);

CREATE TABLE IF NOT EXISTS trials (
    id                  TEXT PRIMARY KEY,
    user_id             TEXT NOT NULL,
    agent_id            TEXT NOT NULL,
    started_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at          TIMESTAMP,
    messages_remaining  INTEGER,
    tokens_remaining    INTEGER,
    created_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, agent_id)
);

CREATE INDEX IF NOT EXISTS idx_trials_user_agent ON trials (user_id, agent_id);

CREATE TABLE IF NOT EXISTS payments (
    id                  TEXT PRIMARY KEY,
    processor           TEXT NOT NULL,
    external_payment_id TEXT,
    user_id             TEXT NOT NULL,
    agent_id            TEXT,
    amount_cents        INTEGER NOT NULL DEFAULT 0,
    currency            TEXT NOT NULL DEFAULT 'usd',
    kind                TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'pending',
    metadata            TEXT NOT NULL DEFAULT '{}',
    created_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (kind IN ('purchase','subscription','one_off')),
    CHECK (status IN ('pending','completed','failed','refunded'))
);

CREATE INDEX IF NOT EXISTS idx_payments_user     ON payments (user_id);
CREATE INDEX IF NOT EXISTS idx_payments_external ON payments (processor, external_payment_id);

CREATE TABLE IF NOT EXISTS billing_exemptions (
    id                  TEXT PRIMARY KEY,
    kind                TEXT NOT NULL,
    agent_id            TEXT,
    user_id             TEXT,
    granted_by_user_id  TEXT,
    reason              TEXT,
    created_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (kind IN ('agent','user','user_for_agent'))
);

CREATE INDEX IF NOT EXISTS idx_exemptions_agent ON billing_exemptions (agent_id);
CREATE INDEX IF NOT EXISTS idx_exemptions_user  ON billing_exemptions (user_id);
CREATE INDEX IF NOT EXISTS idx_exemptions_kind  ON billing_exemptions (kind);
