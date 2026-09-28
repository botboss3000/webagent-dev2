-- Migration 012: Configurable Guardrails and Tool Safety
-- Adds per-agent safety_policy column and per-tool requires_confirmation flag.
--
-- safety_policy (agents): JSON object controlling guardrail behaviour.
--   {
--     "destructive_tools": ["tool_name", ...],  -- tools added on top of the hardcoded baseline
--     "auto_confirm": false,                    -- skip confirmation gate (automation agents)
--     "blocked_imports": ["requests", ...],     -- extra modules blocked in create_tool
--     "max_concurrent_tools": 5                -- cap parallel tool execution (0 = unlimited)
--   }
--
-- requires_confirmation (tools): boolean flag on user-created tools.
--   If 1, the tool is treated as destructive and triggers the guardrail confirmation
--   check regardless of the agent's safety_policy.

-- NOTE: The local SQLite backend applies these automatically via ALTER TABLE
-- migrations in app/db/local.py (_init_schema). This file is provided for
-- Supabase (cloud mode) deployments — apply it via the Supabase SQL editor.

ALTER TABLE agents ADD COLUMN IF NOT EXISTS safety_policy JSONB NOT NULL DEFAULT '{}';
ALTER TABLE tools  ADD COLUMN IF NOT EXISTS requires_confirmation BOOLEAN NOT NULL DEFAULT false;
