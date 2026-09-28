-- ── Migration 011: Add login tracking ──────────────────────────────────────
-- Adds last_login_at column to user_profiles table to track user login times.
--
-- created_at: Set on first login, immutable (first login timestamp)
-- last_login_at: Updated on every login (most recent login timestamp)
--
-- Safe to run multiple times. Existing rows get last_login_at = NULL.
--
-- The local SQLite backend applies this automatically at startup.
-- ---------------------------------------------------------------------------

ALTER TABLE user_profiles ADD COLUMN last_login_at TEXT;
