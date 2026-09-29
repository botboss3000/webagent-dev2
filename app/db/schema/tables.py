"""
Canonical table definitions for WebAgent.

Each table is represented as a dict:
{
    "name": str,
    "columns": [Column(name, type, nullable, default, primary_key, unique, references)],
    "constraints": [str]  # raw constraint strings (e.g. CHECK, UNIQUE composite)
}

Column.type is dialect-neutral (e.g. "TEXT", "INTEGER", "REAL", "JSON", "BLOB",
"TIMESTAMP"). The renderer translates per-dialect.

Indexes and triggers live in separate lists keyed by table name.
"""

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class Column:
    name: str
    type: str
    nullable: bool = True
    default: Optional[str] = None  # raw SQL default expression (e.g. "'{}'", "0", "now()")
    primary_key: bool = False
    unique: bool = False
    references: Optional[str] = None  # e.g. "sessions(id)"
    on_delete: Optional[str] = None   # e.g. "CASCADE"
    # For type == "VECTOR": embedding dimensionality. Postgres renders
    # vector(<dim>) (pgvector); SQLite/MySQL fall back to BLOB (raw float32 bytes).
    vector_dim: Optional[int] = None


@dataclass
class Table:
    name: str
    columns: List[Column] = field(default_factory=list)
    constraints: List[str] = field(default_factory=list)  # composite UNIQUE, CHECK


@dataclass
class Index:
    name: str
    table: str
    columns: str  # raw column list, e.g. "user_id, slug" or "title COLLATE NOCASE"
    unique: bool = False


@dataclass
class FtsTable:
    """SQLite FTS5 virtual table. Postgres uses tsvector instead (rendered as TEXT + GIN)."""
    name: str
    content_table: str
    indexed_columns: List[str]
    unindexed_columns: List[str] = field(default_factory=list)


@dataclass
class Trigger:
    """SQLite-only trigger (FTS sync). Skipped by Postgres/MySQL renderers."""
    name: str
    body: str  # raw SQL body, SQLite syntax


# ── Tables ──────────────────────────────────────────────────────────────────
#
# ⚠ DROP-IN POLICY — this list is for CORE, app-wide tables only.
# Do NOT add a table here for a drop-in plugin capability (an ability,
# integration, connector, event source, …). A self-contained plugin owns its
# OWN storage: create it lazily with `CREATE TABLE IF NOT EXISTS` on first use
# from inside the plugin file (worked example: the `agent_spawns` table in
# plugins/abilities/agent_orchestration.py; precedent: optimizer_runs). That way
# deleting the plugin file removes the whole capability, storage included.
# See CLAUDE.md "Core vs. plugins" and plugins/abilities/_TEMPLATE.py.

TABLES: List[Table] = [
    Table("sessions", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("user_id", "TEXT", nullable=False),
        Column("title", "TEXT"),
        Column("metadata", "TEXT"),
        Column("agent_id", "TEXT"),
        Column("participants", "TEXT", default="'[]'"),
        Column("pinned", "INTEGER", nullable=False, default="0"),
        Column("sort_order", "INTEGER"),
        # Recycling-bin lifecycle: 'active' = live, 'recycled' = soft-deleted from
        # the chat header (hidden, kept until its agent is permanently emptied).
        Column("status", "TEXT", nullable=False, default="'active'"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ]),

    # Browser sessions — first-class, persistent browser TABS that live BESIDE
    # chat. Agent-created tabs are scoped to the chat session that created them
    # so concurrent chats never drive the same Playwright page.
    # owns: its own cookie jar (storage_state) so logins survive restarts, an
    # optional linked agent, and a `shared` flag (0 = private to the user,
    # 1 = visible to the linked agent). The Web tab and the agent's browser_action
    # both address a tab by THIS id, so they reliably drive the same Playwright
    # page. agent_id / user_id are free TEXT (NOT foreign keys) so a tab is
    # independent of any chat session and outlives it. See app/tools/browser.py
    # and app/api/browser_stream.py.
    Table("browser_sessions", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("user_id", "TEXT", nullable=False),
        Column("agent_id", "TEXT"),                          # linked agent (nullable)
        Column("chat_session_id", "TEXT"),                   # owning chat (nullable for user-created tabs)
        Column("title", "TEXT"),
        Column("url", "TEXT"),
        Column("shared", "INTEGER", nullable=False, default="0"),   # 0 private, 1 shared with agent
        Column("status", "TEXT", nullable=False, default="'active'"),  # active | idle | closed
        Column("storage_state", "JSON"),                    # Playwright cookies/localStorage
        Column("position", "INTEGER", nullable=False, default="0"),  # tab ordering (future multi-tab UI)
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ]),

    Table("browser_sync_receipts", [
        Column("mutation_id", "TEXT", nullable=False, primary_key=True),
        Column("user_id", "TEXT", nullable=False),
        Column("session_id", "TEXT", nullable=False),
        Column("request_hash", "TEXT", nullable=False),
        Column("result_json", "TEXT", nullable=False),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ]),

    Table("interactions", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("session_id", "TEXT", nullable=False, references="sessions(id)"),
        Column("parent_id", "TEXT"),
        Column("role", "TEXT", nullable=False),
        Column("content", "TEXT", nullable=False),
        Column("tool_name", "TEXT"),
        Column("tool_call_id", "TEXT"),
        Column("channel", "TEXT"),
        Column("metadata", "TEXT"),
        Column("output", "TEXT"),
        Column("source", "TEXT"),
        Column("from_id", "TEXT"),
        Column("to_id", "TEXT"),
        # Stream-persistence ordering (added 2026-05-24). NULL on legacy rows;
        # callers fall back to created_at ordering when session_seq IS NULL.
        Column("session_seq", "INTEGER"),
        Column("turn_id", "TEXT"),
        Column("turn_seq", "INTEGER"),
        Column("chat_render_kind", "TEXT", nullable=False, default="'activity'"),
        # Message lifecycle (added 2026-05-29). Legacy + finished rows are
        # 'complete'; an assistant answer is 'streaming' while being generated,
        # then 'complete' / 'interrupted' / 'error'. Makes the partial answer
        # durable (survives RunBuffer eviction and server restarts).
        Column("status", "TEXT", nullable=False, default="'complete'"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ]),

    # Collapsed transcript activity, materialized as a projection over ranges of
    # real interactions (never synthetic rows). group_id + the boundary columns let
    # the projector open/split/repair a group from its authoritative
    # start_seq..end_seq range; they must stay in step with the canonical CREATE in
    # app/db/session_activity.py (whose ensure_* migrates older DBs).
    Table("session_activity_summaries", [
        Column("session_id", "TEXT", nullable=False, references="sessions(id)"),
        Column("group_id", "TEXT"),
        Column("start_seq", "INTEGER", nullable=False),
        Column("end_seq", "INTEGER", nullable=False),
        Column("first_interaction_id", "TEXT", nullable=False),
        Column("owner_turn_id", "TEXT", nullable=False, default="''"),
        Column("preceding_boundary_id", "TEXT"),
        Column("following_boundary_id", "TEXT"),
        Column("created_at", "TIMESTAMP", nullable=False),
        Column("tool_count", "INTEGER", nullable=False, default="0"),
        Column("progress_count", "INTEGER", nullable=False, default="0"),
        Column("mode_count", "INTEGER", nullable=False, default="0"),
        Column("model_count", "INTEGER", nullable=False, default="0"),
        Column("compaction_count", "INTEGER", nullable=False, default="0"),
        Column("activity_count", "INTEGER", nullable=False, default="1"),
        Column("is_open", "INTEGER", nullable=False, default="0"),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ], constraints=["PRIMARY KEY (session_id, start_seq)"]),

    # Materialized transcript revision/hash. SQLite marks this dirty through
    # triggers and rebuilds the canonical hash once after a mutation, making
    # subsequent browser-cache validations O(1). Remote backends may populate it
    # in their write adapter before enabling validated persistent cache.
    Table("session_manifests", [
        Column("session_id", "TEXT", nullable=False, primary_key=True),
        Column("authority_revision", "INTEGER", nullable=False, default="0"),
        Column("content_hash", "TEXT", nullable=False, default="''"),
        Column("interaction_count", "INTEGER", nullable=False, default="0"),
        Column("max_session_seq", "INTEGER", nullable=False, default="0"),
        Column("dirty", "INTEGER", nullable=False, default="1"),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ]),

    # Per-session durable run state — the in-flight (or recently finished) agent
    # turn. Lets a cold/second device (even after a server restart) discover an
    # active run from the DB and where the live stream is up to, independent of
    # the in-memory RunBuffer. See app/agent/run_manager.py.
    Table("session_runs", [
        Column("session_id", "TEXT", nullable=False, primary_key=True, references="sessions(id)"),
        Column("user_id", "TEXT", nullable=False),
        Column("agent_id", "TEXT"),
        Column("turn_id", "TEXT"),
        Column("assistant_interaction_id", "TEXT"),
        Column("status", "TEXT", nullable=False, default="'running'"),
        Column("latest_session_seq", "INTEGER", nullable=False, default="0"),
        Column("error", "TEXT"),
        Column("started_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        # Self-healing / auto-resume bookkeeping (see migrations/025_run_self_healing.sql).
        Column("stop_cause", "TEXT"),
        Column("origin", "TEXT"),
        Column("resume_attempts", "INTEGER", nullable=False, default="0"),
        Column("max_resume_attempts", "INTEGER"),
        # Per-error-type resume tally for the run (JSON: {"crash": 2, ...}).
        # Reset by run_state_begin on a fresh run; see migrations/025_run_self_healing.sql.
        Column("resume_cause_counts", "TEXT"),
        Column("heartbeat_at", "TIMESTAMP"),
        Column("next_resume_at", "TIMESTAMP"),
        Column("owner_token", "TEXT"),
        Column("lease_expires_at", "TIMESTAMP"),
        Column("relaunch_ctx", "TEXT"),
        # Durable snapshot of the in-flight operation (JSON: tool/turn/note) so a
        # refresh can re-show the live "in-process" indicator. Cleared on finish.
        Column("current_op", "TEXT"),
    ]),

    # Versioned subagent contract state and append-only review evidence. The
    # runtime also creates these lazily so installations can enable the feature
    # without waiting for a process-wide migration pass.
    Table("run_contract_state", [
        Column("session_id", "TEXT", nullable=False, references="sessions(id)", on_delete="CASCADE"),
        Column("turn_id", "TEXT", nullable=False),
        Column("generation", "TEXT"),
        Column("mission_digest", "TEXT"),
        Column("plan_revision", "INTEGER", nullable=False, default="0"),
        Column("reviewer_spawn_id", "TEXT"),
        Column("reviewer_session_id", "TEXT"),
        Column("block_count", "INTEGER", nullable=False, default="0"),
        Column("close_round", "INTEGER", nullable=False, default="0"),
        Column("status", "TEXT", nullable=False, default="'active'"),
        Column("config_json", "TEXT", nullable=False, default="'{}'"),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ], constraints=["PRIMARY KEY(session_id, turn_id)"]),

    Table("run_contract_checks", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("request_id", "TEXT", nullable=False, unique=True),
        Column("session_id", "TEXT", nullable=False, references="sessions(id)", on_delete="CASCADE"),
        Column("turn_id", "TEXT", nullable=False),
        Column("contract_id", "TEXT", nullable=False),
        Column("contract_version", "INTEGER", nullable=False),
        Column("status", "TEXT", nullable=False),
        Column("decision", "TEXT"),
        Column("worker_spawn_id", "TEXT"),
        Column("worker_session_id", "TEXT"),
        Column("request_hash", "TEXT", nullable=False),
        Column("result_hash", "TEXT"),
        Column("evidence_json", "TEXT", nullable=False, default="'{}'"),
        Column("result_json", "TEXT"),
        Column("error", "TEXT"),
        Column("started_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("completed_at", "TIMESTAMP"),
    ]),

    # Diagnostic flight-recorder (see app/agent/diagnostics.py). Rolling,
    # auto-pruned log of server warnings/errors, agent-loop pipeline problems and
    # run outcomes. session_id/agent_id are free TEXT (NOT foreign keys) so a
    # record outlives the row it referenced and a delete never cascades it away.
    Table("diagnostics", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("ts", "TEXT", nullable=False),
        Column("level", "TEXT", nullable=False),
        Column("category", "TEXT", nullable=False),
        Column("source", "TEXT"),
        Column("message", "TEXT", nullable=False),
        Column("detail", "TEXT"),
        Column("session_id", "TEXT"),
        Column("turn_id", "TEXT"),
        Column("agent_id", "TEXT"),
        Column("user_id", "TEXT"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ]),

    # Client render recorder (see app/agent/render_recorder.py + ui/js/recorder.js).
    # Rolling, auto-pruned record of what the BROWSER rendered and felt — HTML
    # snapshots, DOM-mutation deltas, lag/long-task metrics, JS errors, console
    # warnings and failed network calls. Every row carries session_seq, the same
    # monotonic per-session counter on WS events / interactions, so a render
    # moment joins exactly to the interaction / diagnostics row beside it.
    Table("render_recordings", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("ts", "TEXT", nullable=False),
        Column("recv_ts", "TEXT"),
        Column("kind", "TEXT", nullable=False),
        Column("session_id", "TEXT"),
        Column("turn_id", "TEXT"),
        Column("session_seq", "INTEGER"),
        Column("user_id", "TEXT"),
        Column("agent_id", "TEXT"),
        Column("client_id", "TEXT"),
        Column("seq", "INTEGER"),
        Column("url", "TEXT"),
        Column("level", "TEXT"),
        Column("label", "TEXT"),
        Column("value_num", "REAL"),
        Column("detail", "TEXT"),
        Column("html", "TEXT"),
        Column("html_bytes", "INTEGER"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ]),

    Table("session_summaries", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("user_id", "TEXT", nullable=False),
        Column("session_id", "TEXT", nullable=False, unique=True, references="sessions(id)"),
        Column("title", "TEXT"),
        Column("summary", "TEXT", nullable=False),
        Column("message_count", "INTEGER", nullable=False, default="0"),
        # Compaction marker: how many leading interactions (created_at order) are
        # folded into `summary`. 0 = no rolling summary yet. The verbatim tail is
        # everything after this count. See app/agent/compaction.py.
        Column("covered_count", "INTEGER", nullable=False, default="0"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ]),

    # Segmented compaction "train": one row per FROZEN summary car. Replaces the
    # single rolling summary above with an ordered list of cars, each summarising
    # one contiguous span of raw turns exactly once and then never re-summarised
    # (the far-back merge is the only exception — it folds the oldest cars into a
    # tier=1 block). The verbatim hot tail is every interaction after the largest
    # `end_index`. The raw turns a car covers stay in `interactions` and are
    # retrievable verbatim via [start_index, end_index). See app/agent/compaction.py.
    Table("session_summary_segments", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("user_id", "TEXT", nullable=False),
        Column("session_id", "TEXT", nullable=False, references="sessions(id)"),
        # Order of this car within the session's train (0 = oldest).
        Column("seq", "INTEGER", nullable=False, default="0"),
        # Half-open range of covered interactions in created_at order: [start, end).
        Column("start_index", "INTEGER", nullable=False, default="0"),
        Column("end_index", "INTEGER", nullable=False, default="0"),
        Column("summary", "TEXT", nullable=False),
        # Rough token size of `summary` (for the fill gauge / assembly maths).
        Column("token_estimate", "INTEGER", nullable=False, default="0"),
        # Short human-facing label of what this span was about (retrieval hint).
        Column("topic", "TEXT"),
        # 0 = normal car (one peeled span). 1 = far-back merged block (several
        # oldest cars folded together once the car count passed the cap).
        Column("tier", "INTEGER", nullable=False, default="0"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ], constraints=["UNIQUE(session_id, seq)"]),

    Table("agent_templates", [
        Column("id", "TEXT", nullable=False, primary_key=True, default="'default'"),
        Column("name", "TEXT", nullable=False, default="''"),
        Column("description", "TEXT", nullable=False, default="''"),
        Column("max_turn_count", "INTEGER", nullable=False, default="0"),
        Column("max_wall_seconds", "REAL"),
        Column("max_identical_tool_calls", "INTEGER", nullable=False, default="0"),
        Column("max_stall_strikes", "INTEGER", nullable=False, default="0"),
        Column("model", "TEXT"),
        Column("provider", "TEXT"),
        Column("temperature", "REAL", nullable=False, default="0.0"),
        Column("max_tokens", "INTEGER", nullable=False, default="8096"),
        Column("metadata", "TEXT", nullable=False, default="'{}'"),
        Column("trigger_type", "TEXT", nullable=False, default="'user_input'"),
        Column("trigger_key", "TEXT"),
        Column("loop_logic", "TEXT", nullable=False, default="'[]'"),
        Column("abilities", "TEXT"),
        Column("icon", "TEXT"),
        Column("can_be_default", "INTEGER", nullable=False, default="1"),
        Column("is_system", "INTEGER", nullable=False, default="0"),
        Column("is_pipeline", "INTEGER", nullable=False, default="0"),
        Column("access_level", "TEXT", nullable=False, default="'user'"),
        Column("trigger_description", "TEXT"),
        Column("discoverable", "INTEGER", nullable=False, default="1"),
        Column("is_admin_agent", "INTEGER", nullable=False, default="0"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ]),

    Table("agents", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("template_id", "TEXT"),
        Column("name", "TEXT", nullable=False, default="''"),
        Column("description", "TEXT", nullable=False, default="''"),
        Column("is_user_default", "INTEGER", nullable=False, default="0"),
        Column("max_turn_count", "INTEGER", nullable=False, default="0"),
        Column("max_wall_seconds", "REAL"),
        Column("max_identical_tool_calls", "INTEGER", nullable=False, default="0"),
        Column("max_stall_strikes", "INTEGER", nullable=False, default="0"),
        Column("model", "TEXT"),
        Column("provider", "TEXT"),
        Column("temperature", "REAL", nullable=False, default="0.0"),
        Column("max_tokens", "INTEGER", nullable=False, default="8096"),
        Column("status", "TEXT", nullable=False, default="'active'"),
        Column("metadata", "TEXT", nullable=False, default="'{}'"),
        Column("default_execution_mode", "TEXT", nullable=False, default="'ask'"),
        Column("allowed_tools", "TEXT", nullable=False, default="'[]'"),
        Column("custom_tool_ids", "TEXT", nullable=False, default="'[]'"),
        Column("safety_policy", "TEXT", nullable=False, default="'{}'"),
        Column("trigger_type", "TEXT", nullable=False, default="'user_input'"),
        Column("trigger_key", "TEXT"),
        Column("loop_logic", "TEXT", nullable=False, default="'[]'"),
        # Self-healing is opt-out: new agents resume recoverable interrupted runs by default.
        Column("auto_resume", "INTEGER", nullable=False, default="1"),
        Column("is_admin_agent", "INTEGER", nullable=False, default="0"),
        Column("icon", "TEXT"),
        Column("current_context_id", "TEXT"),
        Column("assigned_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("turn_count", "INTEGER", nullable=False, default="0"),
        Column("admin_users", "TEXT", nullable=False, default="'[]'"),
        Column("member_users", "TEXT", nullable=False, default="'[]'"),
        Column("authorized_users", "TEXT", nullable=False, default="'[]'"),
        Column("user_mode", "TEXT", nullable=False, default="'anonymous'"),
        Column("sort_order", "INTEGER"),
    ]),

    Table("agent_prompts", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("agent_id", "TEXT", nullable=False),
        Column("slot_name", "TEXT", nullable=False),
        Column("user_id", "TEXT"),
        Column("order_index", "INTEGER"),
        Column("lock", "INTEGER"),
        Column("merge_mode", "TEXT"),
        Column("content", "TEXT", nullable=False, default="''"),
        # Origin version this row was cloned from (in agent_prompt_templates).
        # NULL on user-override rows and on legacy rows pre-versioning.
        Column("template_version", "INTEGER"),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("updated_by", "TEXT"),
    ]),

    # Canonical prompt-slot templates. Source of truth for slot defaults.
    # JSON files in data/agents/*.json seed this table; admin edits
    # promoted here are protected from JSON re-seed via source='admin'.
    # When a new agent is created from a template, rows here are cloned
    # into agent_prompts under that agent's id.
    Table("agent_prompt_templates", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("template_id", "TEXT", nullable=False),
        Column("slot_name", "TEXT", nullable=False),
        Column("order_index", "INTEGER", nullable=False, default="0"),
        Column("lock", "INTEGER", nullable=False, default="0"),
        Column("merge_mode", "TEXT", nullable=False, default="'replace'"),
        Column("content", "TEXT", nullable=False, default="''"),
        Column("version", "INTEGER", nullable=False, default="1"),
        # 'json' = seeded from JSON file (re-seed may overwrite).
        # 'admin' = promoted from admin UI edit (re-seed SKIPS this row).
        Column("source", "TEXT", nullable=False, default="'json'"),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("updated_by", "TEXT", nullable=False, default="'system'"),
    ], constraints=[
        "UNIQUE(template_id, slot_name)",
        "CHECK (source IN ('json','admin'))",
    ]),

    # Generic key/value store for cross-cutting runtime metadata
    # (manifest hashes, schema versions, feature toggles, etc.).
    # Use sparingly — prefer typed tables for domain data.
    Table("app_meta", [
        Column("key", "TEXT", nullable=False, primary_key=True),
        Column("value", "TEXT", nullable=False, default="''"),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ]),

    Table("agent_connections", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("agent_id", "TEXT", nullable=False, references="agents(id)", on_delete="CASCADE"),
        Column("connection_type", "TEXT", nullable=False),
        Column("section", "TEXT", nullable=False, default="'channel'"),
        Column("enabled", "INTEGER", nullable=False, default="0"),
        Column("config", "TEXT", nullable=False, default="'{}'"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ], constraints=["UNIQUE(agent_id, connection_type)"]),

    Table("agent_abilities", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("agent_id", "TEXT", nullable=False, references="agents(id)", on_delete="CASCADE"),
        Column("ability_id", "TEXT", nullable=False),
        Column("source", "TEXT", nullable=False, default="'platform'"),
        Column("enabled", "INTEGER", nullable=False, default="0"),
        Column("byo_client_id", "TEXT", nullable=False, default="''"),
        Column("byo_client_secret_ref", "TEXT", nullable=False, default="''"),
        Column("config", "TEXT", nullable=False, default="'{}'"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ], constraints=["UNIQUE(agent_id, ability_id)"]),

    # Per-agent, database-backed abilities.  Unlike host abilities these do not
    # load Python modules: they contribute a skill and a constrained declaration
    # of which already-granted tools a workflow may use.
    Table("agent_soft_abilities", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("agent_id", "TEXT", nullable=False, references="agents(id)", on_delete="CASCADE"),
        Column("slug", "TEXT", nullable=False),
        Column("display_name", "TEXT", nullable=False),
        Column("description", "TEXT", nullable=False, default="''"),
        Column("icon", "TEXT", nullable=False, default="'sparkles'"),
        Column("enabled", "INTEGER", nullable=False, default="1"),
        Column("skill_summary", "TEXT", nullable=False, default="''"),
        Column("skill_body", "TEXT", nullable=False, default="''"),
        Column("workflow", "TEXT", nullable=False, default="'{}'"),
        Column("allowed_tools", "TEXT", nullable=False, default="'[]'"),
        Column("credential_schema", "TEXT", nullable=False, default="'[]'"),
        Column("policy", "TEXT", nullable=False, default="'{}'"),
        Column("status", "TEXT", nullable=False, default="'draft'"),
        Column("version", "INTEGER", nullable=False, default="1"),
        Column("created_by", "TEXT", nullable=False),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ], constraints=[
        "UNIQUE(agent_id, slug)",
        "CHECK (status IN ('draft','ready','disabled','error'))",
    ]),

    Table("soft_ability_runs", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("ability_id", "TEXT", nullable=False, references="agent_soft_abilities(id)", on_delete="CASCADE"),
        Column("agent_id", "TEXT", nullable=False, references="agents(id)", on_delete="CASCADE"),
        Column("user_id", "TEXT", nullable=False),
        Column("session_id", "TEXT", nullable=False, default="''"),
        Column("ability_version", "INTEGER", nullable=False),
        Column("status", "TEXT", nullable=False),
        Column("tools", "TEXT", nullable=False, default="'[]'"),
        Column("elapsed_ms", "INTEGER", nullable=False, default="0"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ]),

    Table("memories", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("user_id", "TEXT", nullable=False),
        Column("slug", "TEXT", nullable=False),
        Column("page_type", "TEXT", nullable=False),
        Column("title", "TEXT", nullable=False),
        Column("compiled_truth", "TEXT", nullable=False, default="''"),
        Column("timeline", "TEXT", nullable=False, default="''"),
        Column("frontmatter", "TEXT", nullable=False, default="'{}'"),
        # Cross-session knowledge engine fields (Context Control):
        #   origin       — 'deliberate' (user/agent chose to save) vs 'distilled' (system harvested)
        #   pinned       — protected from auto-merge/trim by the consolidation pass
        #   provenance   — JSON list of source session/interaction ids the fact came from
        #   needs_review — flagged when new evidence touches it, so consolidation only revisits changed facts
        Column("origin", "TEXT", nullable=False, default="'distilled'"),
        Column("pinned", "INTEGER", nullable=False, default="0"),
        Column("provenance", "TEXT", nullable=False, default="'[]'"),
        Column("needs_review", "INTEGER", nullable=False, default="0"),
        Column("content_hash", "TEXT"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ], constraints=[
        "UNIQUE(user_id, slug)",
        "CHECK (page_type IN ('person','company','deal','meeting','project','idea','concept','writing','program','personal','media','inbox','archive'))",
    ]),

    Table("memory_chunks", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("memory_id", "TEXT", nullable=False, references="memories(id)", on_delete="CASCADE"),
        Column("chunk_index", "INTEGER", nullable=False),
        Column("chunk_text", "TEXT", nullable=False),
        Column("chunk_source", "TEXT", nullable=False, default="'compiled_truth'"),
        Column("embedding", "VECTOR", vector_dim=1536),
        Column("token_count", "INTEGER"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ], constraints=[
        "UNIQUE(memory_id, chunk_index)",
        "CHECK (chunk_source IN ('compiled_truth', 'timeline'))",
    ]),

    Table("tools", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("name", "TEXT", nullable=False),
        Column("code", "TEXT", nullable=False),
        Column("description", "TEXT", nullable=False),
        Column("parameters", "TEXT", nullable=False, default="'{}'"),
        Column("language", "TEXT", nullable=False, default="'python'"),
        Column("status", "TEXT", nullable=False, default="'active'"),
        Column("created_by", "TEXT", nullable=False),
        Column("stages", "TEXT", nullable=False, default="'[]'"),
        Column("destructive", "INTEGER", nullable=False, default="0"),
        Column("agent_types", "TEXT", nullable=False, default="'[]'"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ]),

    Table("agent_credentials", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("user_id", "TEXT", nullable=False),
        Column("tool_name", "TEXT", nullable=False),
        Column("credential_type", "TEXT", nullable=False),
        Column("encrypted_data", "TEXT", nullable=False),
        Column("display_name", "TEXT"),
        Column("expires_at", "TEXT"),
        Column("scopes", "TEXT", default="'[]'"),
        Column("last_used_at", "TEXT"),
        Column("use_count", "INTEGER", nullable=False, default="0"),
        Column("is_active", "INTEGER", nullable=False, default="1"),
        Column("requires_renewal", "INTEGER", nullable=False, default="0"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ]),

    Table("skills", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("user_id", "TEXT", nullable=False),
        Column("name", "TEXT", nullable=False),
        Column("description", "TEXT"),
        Column("code", "TEXT"),
        Column("version", "INTEGER", nullable=False, default="1"),
        Column("base_skill_id", "TEXT"),
        Column("is_official", "INTEGER", nullable=False, default="0"),
        Column("tags", "TEXT", nullable=False, default="'[]'"),
        Column("is_active", "INTEGER", nullable=False, default="1"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ]),

    Table("skill_executions", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("skill_id", "TEXT", nullable=False, references="skills(id)", on_delete="CASCADE"),
        Column("user_id", "TEXT", nullable=False),
        Column("session_id", "TEXT", nullable=False, references="sessions(id)"),
        Column("interaction_id", "TEXT"),
        Column("success", "INTEGER", nullable=False, default="1"),
        Column("duration_ms", "INTEGER", nullable=False, default="0"),
        Column("steps_to_complete", "INTEGER", nullable=False, default="1"),
        Column("error_message", "TEXT"),
        Column("input_params", "TEXT", default="'{}'"),
        Column("output_summary", "TEXT"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ]),

    Table("skill_feedback", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("skill_id", "TEXT", nullable=False, references="skills(id)", on_delete="CASCADE"),
        Column("execution_id", "TEXT", references="skill_executions(id)", on_delete="SET NULL"),
        Column("user_id", "TEXT", nullable=False),
        Column("feedback_type", "TEXT", nullable=False),
        Column("message", "TEXT"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ], constraints=["CHECK (feedback_type IN ('positive','negative','correction'))"]),

    Table("session_interrupts", [
        Column("session_id", "TEXT", nullable=False, primary_key=True, references="sessions(id)", on_delete="CASCADE"),
        # Ownership column. Every interrupt row is scoped to the owning user so it
        # can never leak across tenants, and LocalBackend.set_interrupt always
        # writes it. It was missing from the canonical schema, so
        # render_plane/ensure_sqlite_plane_columns created legacy 3-column tables
        # and never reconciled an existing one — set_interrupt then raised
        # "table session_interrupts has no column named user_id", the stop flag
        # never landed, and the footer Stop button was a no-op (the run kept
        # executing). ``default=''`` (never a real user id) keeps the column
        # additively reconcilable; _migrate_session_interrupt_ownership rebuilds
        # the row with the true owner.
        Column("user_id", "TEXT", nullable=False, default="''"),
        Column("interrupt_requested", "INTEGER", nullable=False, default="1"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ]),

    Table("session_notifications", [
        Column("session_id", "TEXT", nullable=False, primary_key=True, references="sessions(id)", on_delete="CASCADE"),
        Column("user_id", "TEXT", nullable=False),
        Column("title", "TEXT", nullable=False, default="''"),
        Column("dismissed", "INTEGER", nullable=False, default="0"),
        Column("dismissed_at", "TEXT"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ]),

    Table("attachments", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("user_id", "TEXT", nullable=False),
        Column("session_id", "TEXT"),
        Column("original_name", "TEXT", nullable=False),
        Column("mime_type", "TEXT", nullable=False),
        Column("size_bytes", "INTEGER", nullable=False),
        Column("storage_path", "TEXT", nullable=False),
        Column("storage_provider", "TEXT", nullable=False, default="'local'"),
        Column("metadata", "TEXT", nullable=False, default="'{}'"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ]),

    Table("channel_identities", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("channel", "TEXT", nullable=False),
        Column("external_id", "TEXT", nullable=False),
        Column("user_id", "TEXT", nullable=False),
        Column("user_tier", "TEXT", nullable=False, default="'anonymous'"),
        Column("display_name", "TEXT", nullable=False, default="''"),
        Column("email", "TEXT", nullable=False, default="''"),
        Column("email_verified", "INTEGER", nullable=False, default="0"),
        Column("linked_user_id", "TEXT"),
        Column("metadata", "TEXT", nullable=False, default="'{}'"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ], constraints=[
        "UNIQUE(channel, external_id)",
        "CHECK (user_tier IN ('anonymous','channel_verified','full'))",
    ]),

    Table("linking_codes", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("code", "TEXT", nullable=False, unique=True),
        Column("source_user_id", "TEXT", nullable=False),
        Column("target_channel", "TEXT", nullable=False, default="''"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("expires_at", "TEXT", nullable=False),
        Column("used", "INTEGER", nullable=False, default="0"),
    ]),

    Table("webhook_registrations", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("user_id", "TEXT", nullable=False),
        Column("name", "TEXT", nullable=False),
        Column("instructions", "TEXT", nullable=False, default="''"),
        Column("active", "INTEGER", nullable=False, default="1"),
        Column("deleted_at", "TEXT"),  # NULL = active; set = in the Automations recycling bin
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ]),

    Table("webhook_event_log", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("webhook_id", "TEXT", nullable=False, references="webhook_registrations(id)", on_delete="CASCADE"),
        Column("method", "TEXT", nullable=False),
        Column("headers", "TEXT", nullable=False, default="'{}'"),
        Column("payload", "TEXT", nullable=False, default="''"),
        Column("response_status", "INTEGER", nullable=False, default="200"),
        Column("response_body", "TEXT", nullable=False, default="''"),
        Column("duration_ms", "INTEGER", nullable=False, default="0"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ]),

    Table("auth_elements", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("user_id", "TEXT", nullable=False),
        Column("service", "TEXT", nullable=False),
        Column("label", "TEXT", nullable=False, default="'default'"),
        Column("config", "TEXT", nullable=False, default="'{}'"),
        Column("secret_ref", "TEXT", nullable=False, default="''"),
        Column("is_active", "INTEGER", nullable=False, default="1"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ]),

    Table("user_profiles", [
        Column("user_id", "TEXT", nullable=False, primary_key=True),
        Column("is_admin", "INTEGER", nullable=False, default="0"),
        Column("default_agent_id", "TEXT"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("last_login_at", "TEXT"),
        Column("appearance", "TEXT"),
    ]),

    # Personal cross-device ordering for the compact mobile Agents sidebar. Kept
    # separate from shared agent records and user_profiles so a small preference
    # can evolve without broad profile writes or tenant-plane coupling.
    Table("user_agent_sidebar_preferences", [
        Column("user_id", "TEXT", nullable=False, primary_key=True),
        Column("favorite_agent_ids_json", "TEXT", nullable=False, default="'[]'"),
        Column("revision", "INTEGER", nullable=False, default="0"),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ]),

    # Personal cross-device ordering for Agent-card subpage tabs. This is
    # deliberately independent from mobile sidebar agent favorites.
    Table("user_agent_page_preferences", [
        Column("user_id", "TEXT", nullable=False, primary_key=True),
        Column("favorite_page_ids_json", "TEXT", nullable=False, default="'[]'"),
        Column("revision", "INTEGER", nullable=False, default="0"),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ]),

    # Personal cross-device ordering for the My Agents (All Agents) card's subpage
    # tabs. Separate from user_agent_page_preferences because the id sets differ.
    Table("user_my_agents_page_preferences", [
        Column("user_id", "TEXT", nullable=False, primary_key=True),
        Column("favorite_page_ids_json", "TEXT", nullable=False, default="'[]'"),
        Column("revision", "INTEGER", nullable=False, default="0"),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ]),

    # The account/identity plane — login credentials. Central (never per-tenant
    # routed): see app/db/router.py CONTROL_METHODS. Was formerly the per-instance
    # file app/auth/users.json; moved into the shared DB so every instance sees
    # one identical account list (multi-device / multi-instance login). The
    # password is a bcrypt hash — never plaintext. Sibling of user_profiles
    # (same user_id key); kept a SEPARATE table so the auth secrets stay apart
    # from the per-user preferences/admin-flag plane.
    # App-wide product entitlements. These records live in the installation
    # control plane and remain independent from identity assurance and roles.
    Table("model_rosters", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("slug", "TEXT", nullable=False, unique=True),
        Column("name", "TEXT", nullable=False),
        Column("description", "TEXT", nullable=False, default="''"),
        Column("entries_json", "TEXT", nullable=False, default="'[]'"),
        Column("default_entry_id", "TEXT"),
        Column("status", "TEXT", nullable=False, default="'draft'"),
        Column("revision", "INTEGER", nullable=False, default="1"),
        Column("published_revision", "INTEGER"),
        Column("source", "TEXT", nullable=False, default="'admin'"),
        Column("created_by", "TEXT"),
        Column("updated_by", "TEXT"),
        Column("published_at", "TIMESTAMP"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ], constraints=[
        "CHECK (status IN ('draft','published','retired'))",
        "CHECK (revision >= 1)",
    ]),

    Table("anonymous_guest_credentials", [
        Column("credential_hash", "TEXT", nullable=False, primary_key=True),
        Column("user_id", "TEXT", nullable=False),
        Column("channel", "TEXT", nullable=False),
        Column("external_id", "TEXT", nullable=False),
        Column("agent_id", "TEXT", nullable=False, default="''"),
        Column("expires_at", "TEXT", nullable=False),
        Column("last_used_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ]),

    Table("workspace_artifacts", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("user_id", "TEXT", nullable=False),
        Column("storage_path", "TEXT", nullable=False),
        Column("storage_provider", "TEXT", nullable=False, default="'local'"),
        Column("original_name", "TEXT", nullable=False, default="''"),
        Column("mime_type", "TEXT", nullable=False, default="'application/octet-stream'"),
        Column("size_bytes", "INTEGER", nullable=False, default="0"),
        Column("source_type", "TEXT", nullable=False, default="'file'"),
        Column("source_id", "TEXT"),
        Column("created_by", "TEXT"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("deleted_at", "TIMESTAMP"),
    ]),

    Table("model_roster_revisions", [
        Column("roster_id", "TEXT", nullable=False,
               references="model_rosters(id)"),
        Column("revision", "INTEGER", nullable=False),
        Column("payload_json", "TEXT", nullable=False),
        Column("action", "TEXT", nullable=False, default="'published'"),
        Column("created_by", "TEXT"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ], constraints=["PRIMARY KEY (roster_id, revision)", "CHECK (revision >= 1)"]),

    Table("experience_tiers", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("slug", "TEXT", nullable=False, unique=True),
        Column("name", "TEXT", nullable=False),
        Column("description", "TEXT", nullable=False, default="''"),
        Column("policy_json", "TEXT", nullable=False, default="'{}'"),
        Column("policy_schema_version", "INTEGER", nullable=False, default="1"),
        Column("roster_id", "TEXT", references="model_rosters(id)"),
        Column("is_system", "INTEGER", nullable=False, default="0"),
        Column("is_locked", "INTEGER", nullable=False, default="0"),
        Column("status", "TEXT", nullable=False, default="'draft'"),
        Column("revision", "INTEGER", nullable=False, default="1"),
        Column("published_revision", "INTEGER"),
        Column("created_by", "TEXT"),
        Column("updated_by", "TEXT"),
        Column("published_at", "TIMESTAMP"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ], constraints=[
        "CHECK (status IN ('draft','published','retired'))",
        "CHECK (policy_schema_version >= 1)",
        "CHECK (revision >= 1)",
    ]),

    Table("experience_tier_revisions", [
        Column("tier_id", "TEXT", nullable=False,
               references="experience_tiers(id)"),
        Column("revision", "INTEGER", nullable=False),
        Column("payload_json", "TEXT", nullable=False),
        Column("action", "TEXT", nullable=False, default="'published'"),
        Column("created_by", "TEXT"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ], constraints=["PRIMARY KEY (tier_id, revision)", "CHECK (revision >= 1)"]),

    # Multiple rows per user are intentional: manual, billing, and imported
    # assignments may overlap and are resolved by explicit policy precedence.
    Table("user_tier_assignments", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("user_id", "TEXT", nullable=False),
        Column("tier_id", "TEXT", nullable=False, references="experience_tiers(id)"),
        Column("source", "TEXT", nullable=False),
        Column("starts_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("expires_at", "TIMESTAMP"),
        Column("assigned_by", "TEXT"),
        Column("reason", "TEXT", nullable=False, default="''"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ], constraints=[
        "CHECK (source IN ('default','manual','billing','import','system'))",
    ]),

    # Audit events survive subject account deletion; lifecycle erasure
    # pseudonymizes their user identifiers instead of deleting the evidence.
    Table("entitlement_audit_events", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("subject_user_id", "TEXT"),
        Column("actor_user_id", "TEXT"),
        Column("action", "TEXT", nullable=False),
        Column("entity_type", "TEXT", nullable=False),
        Column("entity_id", "TEXT"),
        Column("previous_json", "TEXT"),
        Column("new_json", "TEXT"),
        Column("reason", "TEXT", nullable=False, default="''"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ]),

    Table("user_accounts", [
        Column("user_id", "TEXT", nullable=False, primary_key=True),
        Column("username", "TEXT", nullable=False),          # login name / email; unique
        Column("password_hash", "TEXT", nullable=False, default="''"),  # bcrypt
        Column("display_name", "TEXT", nullable=False, default="''"),
        Column("remember_token", "TEXT", nullable=False, default="''"),
        Column("is_approved", "INTEGER", nullable=False, default="1"),
        Column("session_lifetime_minutes", "INTEGER", nullable=False, default="43200"),
        Column("auto_renew", "INTEGER", nullable=False, default="1"),
        Column("social_links", "TEXT", nullable=False, default="'{}'"),  # JSON {provider: external_id}
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ], constraints=[
        "UNIQUE(username)",
    ]),

    Table("data_sources", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("user_id", "TEXT", nullable=False),
        Column("name", "TEXT", nullable=False),
        Column("type", "TEXT", nullable=False),
        Column("config", "TEXT", nullable=False, default="'{}'"),
        Column("auth_element_id", "TEXT"),
        Column("schema_cache", "TEXT", nullable=False, default="'{}'"),
        Column("safety_policy", "TEXT", nullable=False, default="'{}'"),
        Column("status", "TEXT", nullable=False, default="'unverified'"),
        Column("last_test_message", "TEXT"),
        Column("last_tested_at", "TIMESTAMP"),
        Column("last_introspected_at", "TIMESTAMP"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ], constraints=[
        "UNIQUE(user_id, name)",
        # No CHECK on `type`: connector types are validated in code by
        # CONNECTOR_REGISTRY (drop-in), so a new connector file needs no schema edit.
        "CHECK (status IN ('unverified','active','error','disabled'))",
    ]),

    Table("agent_data_sources", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("agent_id", "TEXT", nullable=False, references="agents(id)", on_delete="CASCADE"),
        Column("data_source_id", "TEXT", nullable=False, references="data_sources(id)", on_delete="CASCADE"),
        Column("tool_alias", "TEXT"),
        Column("enabled", "INTEGER", nullable=False, default="1"),
        Column("inject_schema_in_prompt", "INTEGER", nullable=False, default="1"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ], constraints=["UNIQUE(agent_id, data_source_id)"]),

    Table("doc_chunks", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("data_source_id", "TEXT", nullable=False, references="data_sources(id)", on_delete="CASCADE"),
        Column("source_ref", "TEXT", nullable=False, default="''"),
        Column("chunk_index", "INTEGER", nullable=False, default="0"),
        Column("chunk_text", "TEXT", nullable=False),
        Column("content_hash", "TEXT"),
        Column("embedding", "VECTOR", vector_dim=1536),
        Column("token_count", "INTEGER"),
        Column("metadata", "TEXT", nullable=False, default="'{}'"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ]),

    # Tenant key metadata. NO key material lives here — wrapped DEKs live in
    # the configured SecretsBackend at "wa:dek:<user_id>:v<key_version>".
    # This table only tracks which versions exist and which is active per tenant.
    Table("tenant_key_meta", [
        Column("user_id", "TEXT", nullable=False),
        Column("key_version", "INTEGER", nullable=False),
        Column("algo", "TEXT", nullable=False, default="'fernet'"),
        Column("status", "TEXT", nullable=False, default="'active'"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("retired_at", "TIMESTAMP"),
    ], constraints=[
        "PRIMARY KEY (user_id, key_version)",
        "CHECK (status IN ('active','retired'))",
    ]),

    # ── Billing / monetization ──
    # Per-agent pricing config. scope: 'agent:<agent_id>'.
    Table("billing_configs", [
        Column("scope", "TEXT", nullable=False, primary_key=True),
        Column("strategy", "TEXT", nullable=False, default="'free'"),
        Column("allowed_strategies", "TEXT", nullable=False, default="'[]'"),
        Column("allowed_processors", "TEXT", nullable=False, default="'[]'"),
        Column("rate_card_default_llm", "TEXT", nullable=False, default="'{}'"),
        Column("rate_card_byo_llm", "TEXT", nullable=False, default="'{}'"),
        # Cost-based pricing: charge = provider cost × multiplier (floored at
        # min_charge_cents); flat_image_cost_usd estimates per-image cost for
        # providers that report no usage. NULL = inherit the platform default;
        # the agent row only overrides when a value is explicitly set.
        Column("cost_multiplier", "REAL"),
        Column("min_charge_cents", "INTEGER"),
        Column("flat_image_cost_usd", "REAL"),
        Column("trial_config", "TEXT", nullable=False, default="'{}'"),
        Column("subscription_price_cents", "INTEGER", nullable=False, default="0"),
        Column("currency", "TEXT", nullable=False, default="'usd'"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("updated_by", "TEXT"),
    ]),

    Table("usage_events", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("agent_id", "TEXT", nullable=False),
        Column("user_id", "TEXT", nullable=False),
        Column("interaction_id", "TEXT"),
        Column("input_tokens", "INTEGER", nullable=False, default="0"),
        Column("output_tokens", "INTEGER", nullable=False, default="0"),
        Column("provider_cost_cents", "INTEGER", nullable=False, default="0"),
        Column("end_user_charge_cents", "INTEGER", nullable=False, default="0"),
        Column("agent_admin_earnings_cents", "INTEGER", nullable=False, default="0"),
        Column("strategy", "TEXT", nullable=False, default="'free'"),
        Column("is_byo_llm", "INTEGER", nullable=False, default="0"),
        Column("is_trial", "INTEGER", nullable=False, default="0"),
        Column("is_exempt", "INTEGER", nullable=False, default="0"),
        Column("model", "TEXT"),
        Column("provider", "TEXT"),
        # Locked-in published-price cost of this single call in USD (model rate ×
        # this call's tokens, computed at save time). Summing it gives accurate
        # session / agent / global cost even when the session switches models.
        Column("cost_usd", "REAL", nullable=False, default="0"),
        Column("cost_source", "TEXT"),
        Column("credit_units", "REAL", nullable=False, default="0"),
        Column("credit_source", "TEXT"),
        Column("cached_input_tokens", "INTEGER", nullable=False, default="0"),
        Column("cache_write_tokens", "INTEGER", nullable=False, default="0"),
        Column("uncached_input_tokens", "INTEGER"),
        Column("reasoning_tokens", "INTEGER", nullable=False, default="0"),
        # Direct session attribution for chat rows (NULL for background rows).
        Column("session_id", "TEXT"),
        # 'chat' for agent turns, 'background' for non-chat LLM calls.
        Column("source", "TEXT", nullable=False, default="'chat'"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ]),

    Table("wallets", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("owner_type", "TEXT", nullable=False),
        Column("owner_id", "TEXT", nullable=False),
        Column("balance_cents", "INTEGER", nullable=False, default="0"),
        Column("hold_cents", "INTEGER", nullable=False, default="0"),
        Column("currency", "TEXT", nullable=False, default="'usd'"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ], constraints=[
        "UNIQUE(owner_type, owner_id, currency)",
        "CHECK (owner_type IN ('user','agent_admin'))",
    ]),

    Table("wallet_transactions", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("wallet_id", "TEXT", nullable=False),
        Column("delta_cents", "INTEGER", nullable=False),
        Column("kind", "TEXT", nullable=False),
        Column("ref_id", "TEXT"),
        Column("note", "TEXT"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ], constraints=[
        "CHECK (kind IN ('purchase','usage','refund','earnings','hold','release'))",
    ]),

    Table("subscriptions", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("user_id", "TEXT", nullable=False),
        Column("agent_id", "TEXT", nullable=False),
        Column("processor", "TEXT", nullable=False),
        Column("external_subscription_id", "TEXT"),
        Column("status", "TEXT", nullable=False, default="'pending'"),
        Column("current_period_end", "TIMESTAMP"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ], constraints=[
        "UNIQUE(user_id, agent_id)",
        "CHECK (status IN ('pending','active','past_due','cancelled','expired'))",
    ]),

    Table("trials", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("user_id", "TEXT", nullable=False),
        Column("agent_id", "TEXT", nullable=False),
        Column("started_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("expires_at", "TIMESTAMP"),
        # The trial is a credit grant: credit_cents is the original allotment,
        # remaining_cents burns down with the same cost × multiplier charges as
        # purchased credits. (messages_remaining/tokens_remaining kept for
        # legacy rows; clean cutover — the engine reads only the credit fields.)
        Column("credit_cents", "INTEGER"),
        Column("remaining_cents", "INTEGER"),
        Column("messages_remaining", "INTEGER"),
        Column("tokens_remaining", "INTEGER"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ], constraints=[
        "UNIQUE(user_id, agent_id)",
    ]),

    Table("payments", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("processor", "TEXT", nullable=False),
        Column("external_payment_id", "TEXT"),
        Column("user_id", "TEXT", nullable=False),
        Column("agent_id", "TEXT"),
        Column("amount_cents", "INTEGER", nullable=False, default="0"),
        Column("currency", "TEXT", nullable=False, default="'usd'"),
        Column("kind", "TEXT", nullable=False),
        Column("status", "TEXT", nullable=False, default="'pending'"),
        Column("metadata", "TEXT", nullable=False, default="'{}'"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ], constraints=[
        "CHECK (kind IN ('purchase','subscription','one_off'))",
        "CHECK (status IN ('pending','completed','failed','refunded'))",
    ]),

    # Exemption rules. Three kinds:
    #   'agent'           — whole agent is free (agent_id set, user_id NULL)
    #   'user'            — user is exempt globally (user_id set, agent_id NULL)
    #   'user_for_agent'  — user is exempt for one agent (both set)
    Table("billing_exemptions", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("kind", "TEXT", nullable=False),
        Column("agent_id", "TEXT"),
        Column("user_id", "TEXT"),
        Column("granted_by_user_id", "TEXT"),
        Column("reason", "TEXT"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ], constraints=[
        "CHECK (kind IN ('agent','user','user_for_agent'))",
    ]),

    # ── Automation / events (cron + push/poll triggers; see migration 016) ──
    Table("agent_automations", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("agent_id", "TEXT", nullable=False),
        Column("owner_user_id", "TEXT", nullable=False),
        Column("task_label", "TEXT", nullable=False, default="''"),
        Column("prompt", "TEXT", nullable=False, default="''"),
        Column("schedule_cron", "TEXT", nullable=False, default="''"),
        Column("schedule_natural", "TEXT", nullable=False, default="''"),
        Column("timezone", "TEXT", nullable=False, default="'UTC'"),
        Column("channel", "TEXT"),
        Column("channel_recipient", "TEXT"),
        Column("silent", "INTEGER", nullable=False, default="0"),
        Column("enabled", "INTEGER", nullable=False, default="1"),
        Column("next_run_at", "TEXT"),
        Column("last_run_at", "TEXT"),
        Column("last_status", "TEXT"),
        Column("last_error", "TEXT"),
        Column("last_session_id", "TEXT"),
        Column("source_hash", "TEXT", nullable=False, default="''"),
        Column("fire_token", "TEXT"),
        Column("external_job_id", "TEXT"),
        Column("external_provider", "TEXT"),
        # ── Feature-rich automation (see migration 029) ──
        Column("schedule_kind", "TEXT", nullable=False, default="'cron'"),   # cron | once
        Column("delivery_json", "TEXT", nullable=False, default="'{}'"),     # unified delivery spec
        Column("run_mode", "TEXT", nullable=False, default="'inline'"),      # inline|fresh_clone|dedicated_clone|headless
        Column("runner_agent_id", "TEXT"),                                   # dedicated clone id
        Column("clone_abilities", "TEXT", nullable=False, default="'[]'"),   # abilities to grant a clone runner
        Column("max_per_day", "INTEGER"),                                    # guardrail: max fires per day
        Column("runs_today", "INTEGER", nullable=False, default="0"),
        Column("runs_today_date", "TEXT"),                                   # YYYY-MM-DD the counter is for
        Column("fail_count", "INTEGER", nullable=False, default="0"),        # consecutive failures
        Column("disable_after_failures", "INTEGER"),                         # auto-disable threshold
        Column("expires_at", "TEXT"),                                        # ISO; past = stop
        Column("retry_max", "INTEGER", nullable=False, default="0"),
        Column("retry_backoff_seconds", "INTEGER", nullable=False, default="0"),
        Column("next_retry_at", "TEXT"),
        Column("memory_json", "TEXT", nullable=False, default="'{}'"),       # per-automation persistent state
        Column("origin", "TEXT", nullable=False, default="'slot'"),          # slot | tool | dashboard | timer
        # ── Cross-device targeting (see app/devices/) ──
        Column("target_device", "TEXT"),                                     # device instance-id to run on; NULL/'' = run on the firing device locally
        Column("target_offline", "TEXT", default="'wait'"),                  # when the target is offline at fire time: 'wait' (queue until it wakes) | 'skip'
        Column("deleted_at", "TEXT"),  # NULL = active; set = in the Automations recycling bin (also enabled=0)
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ]),

    Table("agent_event_subscriptions", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("agent_id", "TEXT", nullable=False),
        Column("owner_user_id", "TEXT", nullable=False),
        Column("source", "TEXT", nullable=False),
        Column("event_type", "TEXT", nullable=False),
        Column("filter_json", "TEXT", nullable=False, default="'{}'"),
        Column("task_label", "TEXT", nullable=False, default="''"),
        Column("prompt", "TEXT", nullable=False, default="''"),
        Column("trigger_natural", "TEXT", nullable=False, default="''"),
        Column("channel", "TEXT"),
        Column("channel_recipient", "TEXT"),
        Column("silent", "INTEGER", nullable=False, default="0"),
        Column("enabled", "INTEGER", nullable=False, default="1"),
        Column("external_subscription_id", "TEXT"),
        Column("external_resource_id", "TEXT"),
        Column("external_expiration_at", "TEXT"),
        Column("external_metadata", "TEXT", nullable=False, default="'{}'"),
        Column("poll_cursor", "TEXT"),
        Column("poll_interval_seconds", "INTEGER"),
        Column("last_polled_at", "TEXT"),
        Column("last_event_at", "TEXT"),
        Column("last_event_external_id", "TEXT"),
        Column("last_status", "TEXT"),
        Column("last_error", "TEXT"),
        Column("last_session_id", "TEXT"),
        Column("fire_count", "INTEGER", nullable=False, default="0"),
        Column("source_hash", "TEXT", nullable=False, default="''"),
        # ── Feature-rich automation (see migration 029) ──
        Column("delivery_json", "TEXT", nullable=False, default="'{}'"),     # unified delivery spec
        Column("run_mode", "TEXT", nullable=False, default="'inline'"),      # inline|fresh_clone|dedicated_clone|headless
        Column("runner_agent_id", "TEXT"),                                   # dedicated clone id
        Column("clone_abilities", "TEXT", nullable=False, default="'[]'"),   # abilities to grant a clone runner
        Column("max_per_day", "INTEGER"),
        Column("runs_today", "INTEGER", nullable=False, default="0"),
        Column("runs_today_date", "TEXT"),
        Column("fail_count", "INTEGER", nullable=False, default="0"),
        Column("disable_after_failures", "INTEGER"),
        Column("expires_at", "TEXT"),
        Column("retry_max", "INTEGER", nullable=False, default="0"),
        Column("retry_backoff_seconds", "INTEGER", nullable=False, default="0"),
        Column("next_retry_at", "TEXT"),
        Column("memory_json", "TEXT", nullable=False, default="'{}'"),
        Column("origin", "TEXT", nullable=False, default="'slot'"),          # slot | tool | dashboard
        Column("deleted_at", "TEXT"),  # NULL = active; set = in the Automations recycling bin (also enabled=0)
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ]),

    Table("automation_runs", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("kind", "TEXT", nullable=False, default="'schedule'"),        # schedule|event|timer|manual
        Column("automation_id", "TEXT"),                                     # set for cron/once/timer/manual
        Column("subscription_id", "TEXT"),                                   # set for event runs
        Column("agent_id", "TEXT", nullable=False),
        Column("owner_user_id", "TEXT", nullable=False),
        Column("runner_agent_id", "TEXT"),                                   # clone id if a clone ran it
        Column("run_mode", "TEXT", nullable=False, default="'inline'"),
        Column("session_id", "TEXT"),
        Column("status", "TEXT", nullable=False, default="'running'"),       # running|ok|error|delivered|delivery_failed
        Column("started_at", "TEXT"),
        Column("finished_at", "TEXT"),
        Column("reply_excerpt", "TEXT"),
        Column("delivery_json", "TEXT", nullable=False, default="'{}'"),     # attempts + per-target results
        Column("error", "TEXT"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ]),

    Table("event_deliveries", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("subscription_id", "TEXT", nullable=False),
        Column("source", "TEXT", nullable=False),
        Column("event_type", "TEXT", nullable=False),
        Column("event_external_id", "TEXT", nullable=False),
        Column("owner_user_id", "TEXT", nullable=False),
        Column("agent_id", "TEXT", nullable=False),
        Column("session_id", "TEXT"),
        Column("status", "TEXT", nullable=False, default="'pending'"),
        Column("error", "TEXT"),
        Column("payload_excerpt", "TEXT"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ]),

    # Visualizer workspace genui (see migration 019 / 030 / 032).
    Table("genui", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("user_id", "TEXT", nullable=False),
        Column("slug", "TEXT", nullable=False),
        Column("title", "TEXT", nullable=False),
        Column("agent_context", "TEXT", nullable=False, default="''"),
        # Owning agent — the agent that created/manages this genui (mirrors
        # browser_sessions.agent_id). Nullable: a user-made genui has none until
        # an agent renders into it; the footer falls back to the default WebAgent.
        Column("agent_id", "TEXT"),
        Column("html", "TEXT"),
        # Per-genui DATA bag (JSON object) — the content the page renders, kept
        # separate from `html` so the agent updates data without rewriting markup.
        Column("data", "TEXT"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ], constraints=[
        "UNIQUE(user_id, slug)",
    ]),

    # ── Background singleton leadership (see app/coordination/leader.py) ──
    # A TTL'd lock so exactly ONE instance across a shared DB runs the singleton
    # background services (schedulers, orphan-resume, etc.). Every instance polls
    # to acquire/renew this lease; only the holder is "leader". MUST exist on
    # Postgres too — without it the leader poll fails and retry-storms the DB.
    Table("background_leader", [
        Column("lock_key", "TEXT", nullable=False, primary_key=True),
        Column("holder_id", "TEXT"),
        Column("heartbeat_at", "TEXT"),
        Column("expires_at", "TEXT"),
    ]),

    # ── Multi-device coordination (see app/devices/) ──
    # Presence registry + cross-device dispatch queue. A shared DB lets one user's
    # several instances ("devices") see each other and hand work back and forth.
    # Every device runs its own worker and claims only jobs addressed to its own
    # instance_id (or broadcast jobs, target_instance NULL) via an atomic claim.
    Table("device_presence", [
        Column("instance_id", "TEXT", nullable=False, primary_key=True),
        Column("label", "TEXT"),                                              # friendly name (hostname; self-reported by the device)
        Column("custom_label", "TEXT"),                                       # admin's chosen display name (overrides label); never touched by the heartbeat
        Column("custom_icon", "TEXT"),                                        # admin's chosen icon (Lucide name; overrides the platform icon)
        Column("capabilities", "TEXT", nullable=False, default="'{}'"),       # JSON: platform, has_browser, …
        Column("endpoint", "TEXT"),                                           # reachable base URL for nudges
        Column("last_seen", "TEXT"),                                          # ISO heartbeat; stale = offline
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ]),

    Table("device_jobs", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("created_by_instance", "TEXT"),                               # which device enqueued it
        Column("target_instance", "TEXT"),                                   # NULL = any device may claim
        Column("target_label", "TEXT"),                                      # friendly target for the UI
        Column("owner_user_id", "TEXT", nullable=False),
        Column("agent_id", "TEXT"),                                          # agent to run on the target
        Column("prompt", "TEXT", nullable=False, default="''"),
        Column("payload", "TEXT", nullable=False, default="'{}'"),           # JSON extras
        Column("status", "TEXT", nullable=False, default="'pending'"),       # pending|claimed|done|error
        Column("claimed_by", "TEXT"),                                        # instance that won the claim
        Column("claimed_at", "TEXT"),
        Column("lease_expires_at", "TEXT"),                                  # TTL'd; expired+claimed = reclaimable
        Column("result_excerpt", "TEXT"),
        Column("error", "TEXT"),
        Column("session_id", "TEXT"),                                        # session the run created on the target
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ]),

    # ── Instance metadata (shared identity store) ──────────────────────────────
    # Persistent facts about any instance (cloud VMs, fleet devices, local
    # checkouts, Cloud Run services, manual servers). One row per instance, keyed
    # by ``ref``. ``metadata`` is a JSON bag for everything optional or
    # kind-specific — adding a new field never needs a migration.
    # SEPARATE from ``device_presence`` (heartbeat/liveness) and ``device_jobs``
    # (dispatch queue).
    Table("instances", [
        Column("ref", "TEXT", nullable=False, primary_key=True),
        Column("kind", "TEXT", nullable=False),
        Column("display_name", "TEXT"),
        Column("provider", "TEXT"),
        Column("status", "TEXT"),
        Column("ip", "TEXT"),
        Column("endpoint", "TEXT"),
        Column("platform", "TEXT"),
        Column("zone", "TEXT"),
        Column("machine_type", "TEXT"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("metadata", "TEXT", nullable=False, default="'{}'"),
    ]),

    # Minimal control-plane projection used to locate and authorize an agent
    # without duplicating its complete per-agent authority record in app.db.
    Table("agent_catalog", [
        Column("agent_id", "TEXT", nullable=False, primary_key=True),
        Column("name", "TEXT", nullable=False, default="''"),
        Column("icon", "TEXT"),
        Column("status", "TEXT", nullable=False, default="'active'"),
        Column("template_id", "TEXT"),
        Column("owner_user_id", "TEXT"),
        Column("admin_users", "TEXT", nullable=False, default="'[]'"),
        Column("member_users", "TEXT", nullable=False, default="'[]'"),
        Column("authorized_users", "TEXT", nullable=False, default="'[]'"),
        Column("storage_ref", "TEXT", nullable=False, default="''"),
        Column("authority_revision", "INTEGER", nullable=False, default="0"),
        Column("deleted_at", "TEXT"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ]),

    # Durable activation marker.  Merely finding app.db/global.db on disk is
    # never sufficient to activate a partially completed storage migration.
    Table("storage_layout", [
        Column("id", "INTEGER", nullable=False, primary_key=True),
        Column("layout_version", "INTEGER", nullable=False),
        Column("state", "TEXT", nullable=False, default="'preparing'"),
        Column("manifest_json", "TEXT", nullable=False, default="'{}'"),
        Column("started_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("activated_at", "TEXT"),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ], constraints=["CHECK (state IN ('preparing','verified','active','failed','retired'))"]),

    Table("storage_migrations", [
        Column("migration_id", "TEXT", nullable=False, primary_key=True),
        Column("source_ref", "TEXT", nullable=False),
        Column("target_ref", "TEXT", nullable=False),
        Column("table_name", "TEXT", nullable=False),
        Column("state", "TEXT", nullable=False, default="'pending'"),
        Column("source_count", "INTEGER", nullable=False, default="0"),
        Column("target_count", "INTEGER", nullable=False, default="0"),
        Column("source_hash", "TEXT", nullable=False, default="''"),
        Column("target_hash", "TEXT", nullable=False, default="''"),
        Column("detail", "TEXT", nullable=False, default="'{}'"),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ], constraints=["CHECK (state IN ('pending','copied','verified','failed'))"]),

    # OpenRouter free-model ranking.  These rows never contain provider secrets.
    Table("free_model_ranking_config", [
        Column("user_id", "TEXT", nullable=False, primary_key=True),
        Column("enabled", "INTEGER", nullable=False, default="0"),
        Column("cadence_hours", "REAL", nullable=False, default="1"),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ]),
    Table("free_model_ranking_observations", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("user_id", "TEXT", nullable=False),
        Column("model_id", "TEXT", nullable=False),
        Column("ok", "INTEGER", nullable=False),
        Column("latency_ms", "REAL"),
        Column("ttft_ms", "REAL"),
        Column("tokens_per_second", "REAL"),
        Column("status_code", "INTEGER"),
        Column("error", "TEXT", nullable=False, default="''"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ]),
    Table("free_model_ranking_aggregates", [
        Column("user_id", "TEXT", nullable=False),
        Column("model_id", "TEXT", nullable=False),
        Column("display_name", "TEXT", nullable=False, default="''"),
        Column("score", "REAL", nullable=False, default="0"),
        Column("eligible", "INTEGER", nullable=False, default="0"),
        Column("reliability", "REAL"),
        Column("latency_ms", "REAL"),
        Column("tokens_per_second", "REAL"),
        Column("manual_rating", "INTEGER"),
        Column("disabled", "INTEGER", nullable=False, default="0"),
        Column("reason", "TEXT", nullable=False, default="''"),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ], constraints=["PRIMARY KEY (user_id, model_id)"]),
    Table("free_model_ranking_leases", [
        Column("user_id", "TEXT", nullable=False, primary_key=True),
        Column("holder", "TEXT", nullable=False),
        Column("expires_at", "TIMESTAMP", nullable=False),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ]),
]

# NOTE: the company-wide Wiki lives in its OWN dedicated SQLite file
# (data/wiki.db, see app/wiki/db.py) — deliberately NOT in this schema or the
# main backend. Keep wiki tables out of here.


# ── Indexes ─────────────────────────────────────────────────────────────────

INDEXES: List[Index] = [
    Index("idx_browser_sessions_user", "browser_sessions", "user_id"),
    Index("idx_browser_sessions_agent", "browser_sessions", "agent_id"),
    Index("idx_browser_sessions_chat", "browser_sessions", "chat_session_id"),
    Index("idx_interactions_session", "interactions", "session_id"),
    Index("idx_interactions_created", "interactions", "created_at"),
    # The transcript hot path filters one session and renders it chronologically.
    # This avoids an extra sort (and growing per-session work) on long chats.
    Index("idx_interactions_session_created", "interactions", "session_id, created_at"),
    Index("idx_interactions_session_seq", "interactions", "session_id, session_seq"),
    Index("idx_interactions_turn", "interactions", "turn_id"),
    Index("idx_diagnostics_ts", "diagnostics", "ts"),
    Index("idx_diagnostics_level", "diagnostics", "level"),
    Index("idx_diagnostics_category", "diagnostics", "category"),
    Index("idx_diagnostics_session", "diagnostics", "session_id"),
    Index("idx_render_rec_ts", "render_recordings", "ts"),
    Index("idx_render_rec_session", "render_recordings", "session_id"),
    Index("idx_render_rec_kind", "render_recordings", "kind"),
    Index("idx_render_rec_seq", "render_recordings", "session_seq"),
    Index("idx_summaries_user", "session_summaries", "user_id"),
    Index("idx_summary_segments_session", "session_summary_segments", "session_id, seq"),
    # Session-list/live-recovery polling asks for a user's active runs.
    Index("idx_session_runs_user_status", "session_runs", "user_id, status"),
    Index("idx_session_runs_status_heartbeat", "session_runs", "status, heartbeat_at"),
    Index("idx_run_contract_checks_turn", "run_contract_checks", "session_id, turn_id, started_at"),
    Index("idx_agent_prompts_agent", "agent_prompts", "agent_id"),
    Index("idx_agent_prompts_user", "agent_prompts", "user_id"),
    Index("idx_agent_prompt_templates_tpl", "agent_prompt_templates", "template_id"),
    Index("idx_agent_conn_agent", "agent_connections", "agent_id"),
    Index("idx_agent_conn_type", "agent_connections", "connection_type"),
    Index("idx_agent_soft_abilities_agent", "agent_soft_abilities", "agent_id"),
    Index("idx_soft_ability_runs_ability", "soft_ability_runs", "ability_id, created_at"),
    Index("idx_agent_ability_agent", "agent_abilities", "agent_id"),
    Index("idx_agent_ability_id", "agent_abilities", "ability_id"),
    Index("idx_memories_user", "memories", "user_id"),
    Index("idx_memories_type", "memories", "page_type"),
    Index("idx_memories_updated", "memories", "updated_at DESC"),
    Index("idx_chunks_memory", "memory_chunks", "memory_id"),
    Index("idx_chunks_source", "memory_chunks", "chunk_source"),
    Index("idx_tools_status", "tools", "status"),
    Index("idx_tools_creator", "tools", "created_by"),
    Index("idx_skills_user", "skills", "user_id"),
    Index("idx_skills_name", "skills", "name"),
    Index("idx_skills_official", "skills", "is_official"),
    Index("idx_exec_skill", "skill_executions", "skill_id"),
    Index("idx_exec_user", "skill_executions", "user_id"),
    Index("idx_exec_created", "skill_executions", "created_at"),
    Index("idx_exec_success", "skill_executions", "success"),
    Index("idx_feedback_skill", "skill_feedback", "skill_id"),
    Index("idx_feedback_user", "skill_feedback", "user_id"),
    Index("idx_feedback_type", "skill_feedback", "feedback_type"),
    Index("idx_attachments_user", "attachments", "user_id"),
    Index("idx_attachments_session", "attachments", "session_id"),
    Index("idx_workspace_artifacts_user", "workspace_artifacts", "user_id, created_at"),
    Index("idx_workspace_artifacts_source", "workspace_artifacts", "source_type, source_id"),
    Index("idx_channel_user", "channel_identities", "user_id"),
    Index("idx_channel_channel_ext", "channel_identities", "channel, external_id"),
    Index("idx_guest_credentials_user", "anonymous_guest_credentials", "user_id"),
    Index("idx_linking_codes_code", "linking_codes", "code"),
    Index("idx_webhook_reg_user", "webhook_registrations", "user_id"),
    Index("idx_webhook_log_hook", "webhook_event_log", "webhook_id"),
    Index("idx_webhook_log_created", "webhook_event_log", "created_at DESC"),
    # Unique
    Index("idx_tools_name", "tools", "name", unique=True),
    Index("idx_auth_elements_user_service_label", "auth_elements", "user_id, service, label", unique=True),
    # Entitlements
    Index("idx_model_rosters_status", "model_rosters", "status"),
    Index("idx_model_roster_revisions_created", "model_roster_revisions", "roster_id, created_at DESC"),
    Index("idx_experience_tiers_status", "experience_tiers", "status"),
    Index("idx_experience_tiers_roster", "experience_tiers", "roster_id"),
    Index("idx_experience_tier_revisions_created", "experience_tier_revisions", "tier_id, created_at DESC"),
    Index("idx_tier_assignments_user", "user_tier_assignments", "user_id, starts_at DESC"),
    Index("idx_tier_assignments_tier", "user_tier_assignments", "tier_id"),
    Index("idx_entitlement_audit_subject", "entitlement_audit_events", "subject_user_id, created_at DESC"),
    Index("idx_entitlement_audit_entity", "entitlement_audit_events", "entity_type, entity_id, created_at DESC"),
    # data sources
    Index("idx_data_sources_user", "data_sources", "user_id"),
    Index("idx_data_sources_type", "data_sources", "type"),
    Index("idx_agent_data_sources_agent", "agent_data_sources", "agent_id"),
    Index("idx_agent_data_sources_source", "agent_data_sources", "data_source_id"),
    Index("idx_tenant_key_meta_active", "tenant_key_meta", "user_id, status"),
    Index("idx_doc_chunks_source", "doc_chunks", "data_source_id"),
    Index("idx_doc_chunks_hash", "doc_chunks", "content_hash"),
    # Billing
    Index("idx_usage_events_agent_created", "usage_events", "agent_id, created_at"),
    Index("idx_usage_events_user_created", "usage_events", "user_id, created_at"),
    Index("idx_usage_events_model_created", "usage_events", "model, created_at"),
    Index("idx_usage_events_session", "usage_events", "session_id"),
    Index("idx_wallet_tx_wallet", "wallet_transactions", "wallet_id"),
    Index("idx_payments_user", "payments", "user_id"),
    Index("idx_payments_external", "payments", "processor, external_payment_id"),
    Index("idx_subscriptions_user", "subscriptions", "user_id"),
    Index("idx_subscriptions_agent", "subscriptions", "agent_id"),
    Index("idx_trials_user_agent", "trials", "user_id, agent_id"),
    Index("idx_exemptions_agent", "billing_exemptions", "agent_id"),
    Index("idx_exemptions_user", "billing_exemptions", "user_id"),
    Index("idx_exemptions_kind", "billing_exemptions", "kind"),
    # Automation / events
    Index("idx_agent_automations_agent", "agent_automations", "agent_id"),
    Index("idx_agent_automations_owner", "agent_automations", "owner_user_id"),
    Index("idx_agent_automations_next", "agent_automations", "next_run_at"),
    Index("idx_agent_automations_hash", "agent_automations", "agent_id, owner_user_id, source_hash", unique=True),
    Index("idx_evt_sub_agent", "agent_event_subscriptions", "agent_id"),
    Index("idx_evt_sub_owner", "agent_event_subscriptions", "owner_user_id"),
    Index("idx_evt_sub_source", "agent_event_subscriptions", "source, event_type"),
    Index("idx_evt_sub_ext", "agent_event_subscriptions", "source, external_subscription_id"),
    Index("idx_evt_sub_expiry", "agent_event_subscriptions", "external_expiration_at"),
    Index("idx_evt_sub_poll", "agent_event_subscriptions", "source, last_polled_at"),
    Index("idx_evt_sub_hash", "agent_event_subscriptions", "agent_id, owner_user_id, source_hash", unique=True),
    Index("idx_evt_del_sub", "event_deliveries", "subscription_id"),
    Index("idx_evt_del_dedup", "event_deliveries", "subscription_id, event_external_id"),
    Index("idx_evt_del_created", "event_deliveries", "created_at DESC"),
    Index("idx_automation_runs_auto", "automation_runs", "automation_id, created_at DESC"),
    Index("idx_automation_runs_sub", "automation_runs", "subscription_id, created_at DESC"),
    Index("idx_automation_runs_owner", "automation_runs", "owner_user_id, created_at DESC"),
    Index("idx_genui_user", "genui", "user_id"),
    # Multi-device coordination
    Index("idx_device_jobs_claim", "device_jobs", "status, target_instance"),
    Index("idx_device_jobs_created", "device_jobs", "created_at"),
    Index("idx_device_presence_seen", "device_presence", "last_seen"),
    # Instance metadata
    Index("idx_instances_kind", "instances", "kind"),
]


# ── FTS tables (SQLite-only; Postgres/MySQL renderer translates to tsvector/FULLTEXT) ──

FTS_TABLES: List[FtsTable] = [
    FtsTable(
        name="memories_fts",
        content_table="memories",
        indexed_columns=["title", "compiled_truth", "timeline"],
        unindexed_columns=["slug"],
    ),
    FtsTable(
        name="doc_chunks_fts",
        content_table="doc_chunks",
        indexed_columns=["chunk_text"],
        unindexed_columns=["source_ref"],
    ),
]


# ── Triggers (SQLite-only — FTS sync) ───────────────────────────────────────

TRIGGERS: List[Trigger] = [
    Trigger("trg_memories_fts_insert", """
        AFTER INSERT ON memories BEGIN
        INSERT INTO memories_fts(rowid, slug, title, compiled_truth, timeline)
        VALUES (new.rowid, new.slug, new.title, new.compiled_truth, new.timeline);
        END
    """),
    Trigger("trg_memories_fts_delete", """
        AFTER DELETE ON memories BEGIN
        INSERT INTO memories_fts(memories_fts, rowid, slug, title, compiled_truth, timeline)
        VALUES ('delete', old.rowid, old.slug, old.title, old.compiled_truth, old.timeline);
        END
    """),
    Trigger("trg_memories_fts_update", """
        AFTER UPDATE ON memories BEGIN
        INSERT INTO memories_fts(memories_fts, rowid, slug, title, compiled_truth, timeline)
        VALUES ('delete', old.rowid, old.slug, old.title, old.compiled_truth, old.timeline);
        INSERT INTO memories_fts(rowid, slug, title, compiled_truth, timeline)
        VALUES (new.rowid, new.slug, new.title, new.compiled_truth, new.timeline);
        END
    """),
    Trigger("trg_doc_chunks_fts_insert", """
        AFTER INSERT ON doc_chunks BEGIN
        INSERT INTO doc_chunks_fts(rowid, chunk_text, source_ref)
        VALUES (new.rowid, new.chunk_text, new.source_ref);
        END
    """),
    Trigger("trg_doc_chunks_fts_delete", """
        AFTER DELETE ON doc_chunks BEGIN
        INSERT INTO doc_chunks_fts(doc_chunks_fts, rowid, chunk_text, source_ref)
        VALUES ('delete', old.rowid, old.chunk_text, old.source_ref);
        END
    """),
    Trigger("trg_doc_chunks_fts_update", """
        AFTER UPDATE ON doc_chunks BEGIN
        INSERT INTO doc_chunks_fts(doc_chunks_fts, rowid, chunk_text, source_ref)
        VALUES ('delete', old.rowid, old.chunk_text, old.source_ref);
        INSERT INTO doc_chunks_fts(rowid, chunk_text, source_ref)
        VALUES (new.rowid, new.chunk_text, new.source_ref);
        END
    """),
]


# Dependency-order list for migration / bootstrap / delete operations.
# Children before parents on delete; parents before children on insert.
from app.chat.schema import definitions as _chat_definitions
_chat_tables, _chat_indexes = _chat_definitions(Column, Table, Index)
TABLES.extend(_chat_tables)
INDEXES.extend(_chat_indexes)


TABLE_ORDER = [t.name for t in TABLES]
