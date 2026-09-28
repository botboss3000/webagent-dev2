-- 027_render_recordings.sql
-- Client render recorder durable store (see app/agent/render_recorder.py +
-- ui/js/recorder.js).
--
-- A rolling, auto-pruned record of what the BROWSER actually rendered and felt —
-- the client-side blind spot the server logs (diagnostics) can never see:
--
--   • snapshot — full HTML of the app root at a moment (Level 1)
--   • mutation — DOM-mutation deltas for replay (Level 2, same column)
--   • lag      — long-task stalls / render latency
--   • js_error — window.onerror + unhandledrejection with stack traces
--   • console  — console.warn / console.error captured client-side
--   • network  — failed or slow fetch calls
--   • nav/meta — navigation timing + session markers
--
-- Every row carries session_seq — the same monotonic per-session counter stamped
-- on WS events and interactions — so a render moment joins exactly to the
-- interaction / diagnostics row beside it. session_id / agent_id are plain TEXT
-- (NOT foreign keys) so a record outlives the row it referenced.
--
-- A background pruner caps rows + age. Recording is OFF in every tab by default;
-- an admin starts it only in a specific browser tab's header — an investigation tool.
--
-- Apply on Supabase via the SQL editor. Local SQLite + the portable Postgres
-- backend auto-create this from the canonical schema (app/db/schema/tables.py).

CREATE TABLE IF NOT EXISTS render_recordings (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    recv_ts TEXT,
    kind TEXT NOT NULL,
    session_id TEXT,
    turn_id TEXT,
    session_seq INTEGER,
    user_id TEXT,
    agent_id TEXT,
    client_id TEXT,
    seq INTEGER,
    url TEXT,
    level TEXT,
    label TEXT,
    value_num DOUBLE PRECISION,
    detail TEXT,
    html TEXT,
    html_bytes INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_render_rec_ts ON render_recordings (ts);
CREATE INDEX IF NOT EXISTS idx_render_rec_session ON render_recordings (session_id);
CREATE INDEX IF NOT EXISTS idx_render_rec_kind ON render_recordings (kind);
CREATE INDEX IF NOT EXISTS idx_render_rec_seq ON render_recordings (session_seq);
