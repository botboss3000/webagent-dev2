-- Migration 030: rename pages table to canvases
-- The "Pages" workspace has been renamed to "Canvas".
-- This renames the table + index and updates all references.

-- SQLite
ALTER TABLE pages RENAME TO canvases;
DROP INDEX IF EXISTS idx_pages_user;
CREATE INDEX IF NOT EXISTS idx_canvases_user ON canvases(user_id);
