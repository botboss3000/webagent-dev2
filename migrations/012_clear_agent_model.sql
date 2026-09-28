-- ── Migration 012: Clear hardcoded model from user-owned agents ──────────────
-- The backend ignores agents.model at runtime (always uses the user's
-- configured LLM provider). The UI now shows "default" when model IS NULL.
-- Clear any stored model string so existing agents display "default".
--
-- Apply via Supabase SQL editor or psql.
-- The local SQLite backend (app/db/local.py) applies this automatically
-- at startup via its in-process migration runner.
-- ---------------------------------------------------------------------------

UPDATE agents
SET model = NULL
WHERE model IS NOT NULL;
