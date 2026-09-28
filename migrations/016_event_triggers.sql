-- =====================================================================
-- Migration 016: agent_event_subscriptions
--
-- Generalized event-trigger abstraction. A subscription says "when SOURCE
-- emits EVENT_TYPE for OWNER, fire AGENT with PROMPT (and optionally
-- deliver via CHANNEL)". Sibling to agent_automations, which holds cron
-- triggers; events are the push/poll counterpart.
--
-- Each source plugin is responsible for the provider-side subscription
-- (Gmail watch, MS Graph /subscriptions, Drive channel, Dropbox webhook,
-- etc.) and for normalizing inbound payloads into the shared event shape
-- the router consumes.
--
-- Apply this to your remote DB (Supabase SQL editor, psql, etc.) once.
-- SQLite installs apply the same schema automatically on startup via
-- app/db/local.py SCHEMA_SQL.
-- =====================================================================

CREATE TABLE IF NOT EXISTS agent_event_subscriptions (
    id                      TEXT PRIMARY KEY,
    agent_id                TEXT NOT NULL,
    owner_user_id           TEXT NOT NULL,

    -- What we're listening for
    source                  TEXT NOT NULL,              -- e.g. 'gmail', 'google_calendar', 'slack'
    event_type              TEXT NOT NULL,              -- e.g. 'message_received', 'event_added'
    filter_json             TEXT NOT NULL DEFAULT '{}', -- source-specific filter dict

    -- What to do when it fires
    task_label              TEXT NOT NULL DEFAULT '',
    prompt                  TEXT NOT NULL DEFAULT '',
    trigger_natural         TEXT NOT NULL DEFAULT '',   -- original English phrase
    channel                 TEXT,                       -- preferred delivery channel, or null = ask user
    channel_recipient       TEXT,
    silent                  INTEGER NOT NULL DEFAULT 0,

    enabled                 INTEGER NOT NULL DEFAULT 1,

    -- Push subscription state (for sources that register externally)
    external_subscription_id    TEXT,                   -- e.g. Graph subscription id, Drive channel id
    external_resource_id        TEXT,                   -- secondary id (Drive resourceId, etc.)
    external_expiration_at      TEXT,                   -- ISO timestamp; null = no TTL
    external_metadata           TEXT NOT NULL DEFAULT '{}', -- JSON: Pub/Sub topic, history id, cursor, etc.

    -- Poll-source bookkeeping
    poll_cursor             TEXT,                       -- opaque "where I left off" marker
    poll_interval_seconds   INTEGER,                    -- per-subscription override (else source default)
    last_polled_at          TEXT,

    -- Observability
    last_event_at           TEXT,
    last_event_external_id  TEXT,                       -- dedup most-recent event
    last_status             TEXT,                       -- 'ok' | 'error' | 'pending'
    last_error              TEXT,
    last_session_id         TEXT,
    fire_count              INTEGER NOT NULL DEFAULT 0,

    -- Sync hash so the automation file can detect drift (same idea as agent_automations)
    source_hash             TEXT NOT NULL DEFAULT '',

    created_at              TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at              TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_evt_sub_agent ON agent_event_subscriptions(agent_id);
CREATE INDEX IF NOT EXISTS idx_evt_sub_owner ON agent_event_subscriptions(owner_user_id);
CREATE INDEX IF NOT EXISTS idx_evt_sub_source ON agent_event_subscriptions(source, event_type);
CREATE INDEX IF NOT EXISTS idx_evt_sub_ext ON agent_event_subscriptions(source, external_subscription_id);
CREATE INDEX IF NOT EXISTS idx_evt_sub_expiry ON agent_event_subscriptions(external_expiration_at);
CREATE INDEX IF NOT EXISTS idx_evt_sub_poll ON agent_event_subscriptions(source, last_polled_at);

CREATE UNIQUE INDEX IF NOT EXISTS idx_evt_sub_hash
    ON agent_event_subscriptions(agent_id, owner_user_id, source_hash);

-- ============================================================
-- event_deliveries: append-only log of fired events for dedup
-- + observability. Each row = one delivery attempt to an agent.
-- ============================================================

CREATE TABLE IF NOT EXISTS event_deliveries (
    id                  TEXT PRIMARY KEY,
    subscription_id     TEXT NOT NULL,
    source              TEXT NOT NULL,
    event_type          TEXT NOT NULL,
    event_external_id   TEXT NOT NULL,                  -- provider id (message id, event id, file id)
    owner_user_id       TEXT NOT NULL,
    agent_id            TEXT NOT NULL,
    session_id          TEXT,
    status              TEXT NOT NULL DEFAULT 'pending',  -- 'pending' | 'ok' | 'error' | 'duplicate'
    error               TEXT,
    payload_excerpt     TEXT,                           -- short JSON for debugging
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_evt_del_sub ON event_deliveries(subscription_id);
CREATE INDEX IF NOT EXISTS idx_evt_del_dedup
    ON event_deliveries(subscription_id, event_external_id);
CREATE INDEX IF NOT EXISTS idx_evt_del_created ON event_deliveries(created_at DESC);
