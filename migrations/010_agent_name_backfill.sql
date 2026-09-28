-- ── Migration 010: Agent name backfill ───────────────────────────────────────
-- Ensures every user-owned agent row has a non-empty name.
-- Agents created before this migration (name = '') are given the default
-- display name 'autoAgent'. New agents are named at creation time.
--
-- Safe to run multiple times (WHERE guard prevents double-updates).
--
-- Apply via Supabase SQL editor or psql.
-- The local SQLite backend applies this automatically at startup.
-- ---------------------------------------------------------------------------

UPDATE agents
SET name = 'autoAgent'
WHERE (name IS NULL OR name = '')
  AND owner_user_id IS NOT NULL;
