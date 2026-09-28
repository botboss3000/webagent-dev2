-- Migration 033: rename the canvases table to genui
-- The "Canvas" workspace has been renamed to "Gen UI".
-- This renames the table + index. (The SQLite backend self-applies this in
-- app/db/local.py via a guarded rename; this file covers Postgres/Supabase.)

ALTER TABLE IF EXISTS canvases RENAME TO genui;
ALTER INDEX IF EXISTS idx_canvases_user RENAME TO idx_genui_user;
