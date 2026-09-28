-- ── Migration 031: Add appearance to user_profiles ─────────────────────────
-- Stores a per-user theme override as a JSON blob — a SPARSE patch of the same
-- appearance keys as data/config/app-settings.json (palette tokens, fonts,
-- border, background, …). When the admin turns on `allow_user_appearance`,
-- GET /api/v1/auth/ui-config layers this on top of the global appearance for
-- the signed-in caller (any token the user hasn't set inherits the global
-- theme). Empty/NULL = the user follows the global theme.
--
-- Safe to run multiple times. Existing rows get appearance = NULL, which the
-- server treats as "use the global theme".
--
-- The local SQLite backend applies this automatically at startup (see the
-- equivalent in-code block in app/db/local.py).
-- ---------------------------------------------------------------------------

ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS appearance TEXT;
