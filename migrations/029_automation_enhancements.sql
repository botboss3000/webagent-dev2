-- =====================================================================
-- Migration 029: feature-rich automation backend
--
-- Adds, to BOTH trigger tables (agent_automations + agent_event_subscriptions):
--   * unified delivery target (delivery_json)
--   * run mode + clone runner (run_mode, runner_agent_id, clone_abilities)
--   * guardrails (max_per_day, runs_today[_date], fail_count,
--     disable_after_failures, expires_at, retry_max, retry_backoff_seconds,
--     next_retry_at)
--   * per-automation memory (memory_json)
--   * origin (slot | tool | dashboard | timer) so slot reconciliation only
--     deletes slot-declared rows and never touches imperative ones.
-- agent_automations additionally gets schedule_kind (cron | once) for
-- first-class one-shot timers.
--
-- Plus a new automation_runs table: per-run history powering the dashboard.
--
-- Apply this to your remote DB (Supabase SQL editor, psql, etc.) once.
-- SQLite installs apply the same columns automatically on startup via
-- app/db/local.py (SCHEMA_SQL + the migration 029 ALTER block).
-- =====================================================================

-- ── agent_automations ────────────────────────────────────────────────
ALTER TABLE agent_automations ADD COLUMN IF NOT EXISTS schedule_kind          TEXT NOT NULL DEFAULT 'cron';
ALTER TABLE agent_automations ADD COLUMN IF NOT EXISTS delivery_json          TEXT NOT NULL DEFAULT '{}';
ALTER TABLE agent_automations ADD COLUMN IF NOT EXISTS run_mode               TEXT NOT NULL DEFAULT 'inline';
ALTER TABLE agent_automations ADD COLUMN IF NOT EXISTS runner_agent_id        TEXT;
ALTER TABLE agent_automations ADD COLUMN IF NOT EXISTS clone_abilities        TEXT NOT NULL DEFAULT '[]';
ALTER TABLE agent_automations ADD COLUMN IF NOT EXISTS max_per_day            INTEGER;
ALTER TABLE agent_automations ADD COLUMN IF NOT EXISTS runs_today             INTEGER NOT NULL DEFAULT 0;
ALTER TABLE agent_automations ADD COLUMN IF NOT EXISTS runs_today_date        TEXT;
ALTER TABLE agent_automations ADD COLUMN IF NOT EXISTS fail_count             INTEGER NOT NULL DEFAULT 0;
ALTER TABLE agent_automations ADD COLUMN IF NOT EXISTS disable_after_failures INTEGER;
ALTER TABLE agent_automations ADD COLUMN IF NOT EXISTS expires_at             TEXT;
ALTER TABLE agent_automations ADD COLUMN IF NOT EXISTS retry_max              INTEGER NOT NULL DEFAULT 0;
ALTER TABLE agent_automations ADD COLUMN IF NOT EXISTS retry_backoff_seconds  INTEGER NOT NULL DEFAULT 0;
ALTER TABLE agent_automations ADD COLUMN IF NOT EXISTS next_retry_at          TEXT;
ALTER TABLE agent_automations ADD COLUMN IF NOT EXISTS memory_json            TEXT NOT NULL DEFAULT '{}';
ALTER TABLE agent_automations ADD COLUMN IF NOT EXISTS origin                 TEXT NOT NULL DEFAULT 'slot';

-- ── agent_event_subscriptions (same set minus schedule_kind) ─────────
ALTER TABLE agent_event_subscriptions ADD COLUMN IF NOT EXISTS delivery_json          TEXT NOT NULL DEFAULT '{}';
ALTER TABLE agent_event_subscriptions ADD COLUMN IF NOT EXISTS run_mode               TEXT NOT NULL DEFAULT 'inline';
ALTER TABLE agent_event_subscriptions ADD COLUMN IF NOT EXISTS runner_agent_id        TEXT;
ALTER TABLE agent_event_subscriptions ADD COLUMN IF NOT EXISTS clone_abilities        TEXT NOT NULL DEFAULT '[]';
ALTER TABLE agent_event_subscriptions ADD COLUMN IF NOT EXISTS max_per_day            INTEGER;
ALTER TABLE agent_event_subscriptions ADD COLUMN IF NOT EXISTS runs_today             INTEGER NOT NULL DEFAULT 0;
ALTER TABLE agent_event_subscriptions ADD COLUMN IF NOT EXISTS runs_today_date        TEXT;
ALTER TABLE agent_event_subscriptions ADD COLUMN IF NOT EXISTS fail_count             INTEGER NOT NULL DEFAULT 0;
ALTER TABLE agent_event_subscriptions ADD COLUMN IF NOT EXISTS disable_after_failures INTEGER;
ALTER TABLE agent_event_subscriptions ADD COLUMN IF NOT EXISTS expires_at             TEXT;
ALTER TABLE agent_event_subscriptions ADD COLUMN IF NOT EXISTS retry_max              INTEGER NOT NULL DEFAULT 0;
ALTER TABLE agent_event_subscriptions ADD COLUMN IF NOT EXISTS retry_backoff_seconds  INTEGER NOT NULL DEFAULT 0;
ALTER TABLE agent_event_subscriptions ADD COLUMN IF NOT EXISTS next_retry_at          TEXT;
ALTER TABLE agent_event_subscriptions ADD COLUMN IF NOT EXISTS memory_json            TEXT NOT NULL DEFAULT '{}';
ALTER TABLE agent_event_subscriptions ADD COLUMN IF NOT EXISTS origin                 TEXT NOT NULL DEFAULT 'slot';

-- ── automation_runs: per-run history ─────────────────────────────────
CREATE TABLE IF NOT EXISTS automation_runs (
    id                  TEXT PRIMARY KEY,
    kind                TEXT NOT NULL DEFAULT 'schedule',  -- schedule | event | timer | manual
    automation_id       TEXT,
    subscription_id     TEXT,
    agent_id            TEXT NOT NULL,
    owner_user_id       TEXT NOT NULL,
    runner_agent_id     TEXT,
    run_mode            TEXT NOT NULL DEFAULT 'inline',
    session_id          TEXT,
    status              TEXT NOT NULL DEFAULT 'running',    -- running|ok|error|delivered|delivery_failed
    started_at          TEXT,
    finished_at         TEXT,
    reply_excerpt       TEXT,
    delivery_json       TEXT NOT NULL DEFAULT '{}',
    error               TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_automation_runs_auto  ON automation_runs(automation_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_automation_runs_sub   ON automation_runs(subscription_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_automation_runs_owner ON automation_runs(owner_user_id, created_at DESC);
