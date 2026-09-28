-- Migration 032: link each canvas to its owning agent.
-- Adds canvases.agent_id — the agent that created/manages the canvas (mirrors
-- browser_sessions.agent_id). Nullable: a user-made canvas has none until an
-- agent renders into it, in which case the Canvas footer falls back to the
-- default WebAgent. The SQLite backend self-applies this in app/db/local.py
-- (PRAGMA-guarded ALTER TABLE); this file covers the Postgres/Supabase backend.

ALTER TABLE canvases ADD COLUMN IF NOT EXISTS agent_id TEXT;
