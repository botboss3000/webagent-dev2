-- =====================================================================
-- Migration 015: tenant_key_meta
--
-- Encryption support. METADATA ONLY — no key material is stored here.
-- The wrapped DEK for (user_id, key_version) lives in the configured
-- SecretsBackend at "wa:dek:<user_id>:v<key_version>".
--
-- One "active" row per user at any time; old versions stay "retired"
-- until purged after a successful rotation loop.
--
-- Apply this to your remote DB (Supabase SQL editor, psql, etc.) once.
-- SQLite installs apply the same schema automatically on startup via
-- app/db/local.py SCHEMA_SQL.
-- =====================================================================

CREATE TABLE IF NOT EXISTS tenant_key_meta (
    user_id      TEXT      NOT NULL,
    key_version  INTEGER   NOT NULL,
    algo         TEXT      NOT NULL DEFAULT 'fernet',
    status       TEXT      NOT NULL DEFAULT 'active'
                          CHECK (status IN ('active','retired')),
    created_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    retired_at   TIMESTAMP,
    PRIMARY KEY (user_id, key_version)
);

CREATE INDEX IF NOT EXISTS idx_tenant_key_meta_active
    ON tenant_key_meta(user_id, status);
