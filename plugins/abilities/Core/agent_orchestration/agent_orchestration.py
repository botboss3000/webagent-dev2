"""Agent Orchestration ability — SELF-CONTAINED drop-in.

Everything this capability needs lives in this file (plus its sibling
``agent_orchestration.skill.md``): the FEATURE descriptor, the tool handlers, the
spawn/run/re-wake runtime, its own storage (a lazily-created ``agent_spawns``
table — no core schema edit, mirroring how the optimizer self-creates
``optimizer_runs``), and a background follow-up-timer poller. Delete this file
(and the .skill.md) and the whole capability is gone; nothing in core references
it by name.

It is discovered generically by core via three optional module hooks (see
app/abilities/__init__.py "Self-contained abilities"):
  • FEATURE                    — catalog + UI + which tool names it gates
  • build_tools(...)           — its tool handlers (app/tools/loader.py injects them)
  • start_background()/stop_background() — its poller (app/main.py runs it)

What it does: lets an agent act as an orchestrator — spawn purpose-built helper
agents in their own saved sessions, converse with them, run them blocking or
forked, and oversee forked work with durable follow-up timers. It also owns the
`delegate_to_agent` / `list_delegatable_agents` handlers IN-FILE (folded in from
the former app/tools/delegation.py — build_tools returns them alongside the
spawn/oversee tools, and the generic loader discovery injects them) and the
`run_optimizer` handler (folded in from the former core block in
app/tools/loader.py — same ability gate, same recursion guards).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════════════
# Storage — a self-created `agent_spawns` table (no core schema edit).
# One row per spawned helper: links the orchestrator session to the helper's own
# session, tracks lifecycle status, and folds in a durable follow-up timer
# (next_check_at + check_note). Portable DDL (no SQL-function defaults) so it
# works through both the SQLite and Postgres backends via db._get_conn().
# ════════════════════════════════════════════════════════════════════════════

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS agent_spawns (
    id                       TEXT PRIMARY KEY,
    user_id                  TEXT,
    orchestrator_session_id  TEXT,
    orchestrator_agent_id    TEXT,
    spawn_session_id         TEXT,
    spawn_agent_id           TEXT,
    name                     TEXT,
    task                     TEXT,
    status                   TEXT,
    result_summary           TEXT,
    next_check_at            TEXT,
    check_note               TEXT,
    resume_attempts          INTEGER,
    heartbeat_at             TEXT,
    claim_token              TEXT,
    completion_delivery      TEXT,
    delivery_state           TEXT,
    delivery_token           TEXT,
    kind                     TEXT,
    created_at               TEXT,
    updated_at               TEXT
)
"""
_INDEX_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_agent_spawns_orch ON agent_spawns(orchestrator_session_id)",
    "CREATE INDEX IF NOT EXISTS idx_agent_spawns_session ON agent_spawns(spawn_session_id)",
    "CREATE INDEX IF NOT EXISTS idx_agent_spawns_check ON agent_spawns(next_check_at)",
    "CREATE INDEX IF NOT EXISTS idx_agent_spawns_hb ON agent_spawns(status, heartbeat_at)",
]

_UPDATABLE = {"name", "task", "status", "result_summary", "next_check_at",
              "check_note", "resume_attempts", "heartbeat_at", "depth",
              "completion_delivery", "delivery_state", "delivery_token"}

# Default per-agent orchestration limits, overridden by the config JSON stored
# on the agent_orchestration connection row (config.ability_settings).
_ORCH_CONFIG_DEFAULTS = {
    # User-task helpers are limited by active work, not historical rows.
    "max_concurrent_spawns": 10,
    # Retained only for compatibility with stored older ability settings.
    "max_spawns_per_agent": 10,
    "max_spawns_per_spawn": 1,
    "max_spawn_depth": 2,
    "auto_stop_on_session_end": True,
    "default_spawn_mode": "fork",  # "fork" | "wait"
    "completion_delivery": "resume",  # "resume" | "notify"
    "delegation_level": "balanced",  # "conservative" | "balanced" | "eager"
}

# Serializes user-worker reservations in this application process. The durable
# pending row written by _create_spawn_unlocked remains the cross-task record;
# the lock closes the check-to-insert race for parallel tool calls.
_WORKER_SPAWN_CREATE_LOCK: Optional[asyncio.Lock] = None


def _worker_spawn_create_lock() -> asyncio.Lock:
    global _WORKER_SPAWN_CREATE_LOCK
    if _WORKER_SPAWN_CREATE_LOCK is None:
        _WORKER_SPAWN_CREATE_LOCK = asyncio.Lock()
    return _WORKER_SPAWN_CREATE_LOCK


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _retry_write(fn, attempts: int = 5, delay: float = 0.25):
    """Retry a DB write on transient SQLite 'locked' errors (no-op elsewhere)."""
    import time
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            if "locked" in str(e).lower() and i < attempts - 1:
                time.sleep(delay * (i + 1))
            else:
                raise


# The schema (table + indexes + late-added columns) never changes within a
# running process, so the DDL only needs to run ONCE — not on every spawn CRUD /
# poll tick. Re-running it each call was ~9 schema round-trips per operation on a
# remote Postgres (CREATE + 4 index + 4 ALTER), and the ALTERs — which fail once
# the columns exist — abort the connection's transaction on Postgres (autocommit
# off), poisoning the caller's very next statement. This flag collapses all that
# to a single guarded pass the first time the module touches the table.
_TABLE_READY = False
_TABLE_LOCK = threading.Lock()


def _ensure_table(conn) -> None:
    global _TABLE_READY
    if _TABLE_READY:
        return
    with _TABLE_LOCK:
        if _TABLE_READY:
            return
        # Commit/rollback each statement in isolation so a benign "already exists"
        # failure (indexes, ALTERs) can't leave the caller's transaction aborted
        # on Postgres. Nothing else is pending on this conn yet — _ensure_table is
        # always the first call in a _do() block — so committing here is safe.
        def _run(sql: str) -> None:
            try:
                conn.execute(sql)
                conn.commit()
            except Exception:  # noqa: BLE001
                try:
                    conn.rollback()
                except Exception:  # noqa: BLE001
                    pass
        _run(_CREATE_SQL)
        for stmt in _INDEX_SQL:
            _run(stmt)
        # Best-effort add of columns introduced after a table's first creation
        # (older installs created the table without them).
        for col, decl in (("resume_attempts", "INTEGER"),
                          ("heartbeat_at", "TEXT"), ("claim_token", "TEXT"),
                          ("completion_delivery", "TEXT"), ("delivery_state", "TEXT"),
                          ("delivery_token", "TEXT"),
                          ("depth", "INTEGER"), ("kind", "TEXT")):
            _run(f"ALTER TABLE agent_spawns ADD COLUMN {col} {decl}")
        _TABLE_READY = True


def _db_conn():
    from app.db import get_db
    db = get_db()
    raw = getattr(db, "_get_conn", None)
    return raw() if raw else None


def _spawns_create(**f) -> dict:
    row_id = str(uuid.uuid4())
    now = _now_iso()

    def _do():
        conn = _db_conn()
        if conn is None:
            raise RuntimeError("backend has no raw connection")
        try:
            _ensure_table(conn)
            conn.execute(
                """INSERT INTO agent_spawns
                   (id, user_id, orchestrator_session_id, orchestrator_agent_id,
                    spawn_session_id, spawn_agent_id, name, task, status,
                    result_summary, next_check_at, check_note, completion_delivery,
                    delivery_state, delivery_token, depth, kind, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (row_id, f.get("user_id"), f.get("orchestrator_session_id"),
                 f.get("orchestrator_agent_id"), f.get("spawn_session_id"),
                 f.get("spawn_agent_id"), f.get("name") or "", f.get("task") or "",
                 f.get("status") or "pending",
                 None,                          # result_summary
                 None,                          # next_check_at
                 None,                          # check_note — was MISSING: 18 values for 19 placeholders
                 f.get("completion_delivery"),   # completion_delivery
                 None,                          # delivery_state
                 None,                          # delivery_token
                 f.get("depth"),                 # depth
                 f.get("kind"),                  # kind
                 now,                            # created_at
                 now),                           # updated_at
            )
            conn.commit()
            row = conn.execute("SELECT * FROM agent_spawns WHERE id = ?", (row_id,)).fetchone()
            return dict(row)
        finally:
            conn.close()

    return _retry_write(_do)


def _spawns_get(spawn_id: str) -> Optional[dict]:
    conn = _db_conn()
    if conn is None:
        return None
    try:
        _ensure_table(conn)
        row = conn.execute("SELECT * FROM agent_spawns WHERE id = ?", (spawn_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _spawns_list(orchestrator_session_id: str) -> list:
    conn = _db_conn()
    if conn is None:
        return []
    try:
        _ensure_table(conn)
        rows = conn.execute(
            "SELECT * FROM agent_spawns WHERE orchestrator_session_id = ? ORDER BY created_at ASC",
            (orchestrator_session_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _spawns_update(spawn_id: str, **fields) -> None:
    sets, params = [], []
    for k, v in fields.items():
        if k in _UPDATABLE:
            sets.append(f"{k} = ?")
            params.append(v)
    if not sets:
        return
    sets.append("updated_at = ?")
    params.append(_now_iso())
    params.append(spawn_id)

    def _do():
        conn = _db_conn()
        if conn is None:
            return
        try:
            _ensure_table(conn)
            conn.execute(f"UPDATE agent_spawns SET {', '.join(sets)} WHERE id = ?", params)
            conn.commit()
        finally:
            conn.close()

    _retry_write(_do)

def _spawns_claim_delivery(spawn_id: str) -> Optional[str]:
    """Atomically claim a terminal parent-delivery for one spawn."""
    token = uuid.uuid4().hex

    def _do():
        conn = _db_conn()
        if conn is None:
            return None
        try:
            _ensure_table(conn)
            conn.execute(
                """UPDATE agent_spawns
                   SET delivery_state = 'claimed', delivery_token = ?, updated_at = ?
                   WHERE id = ? AND (delivery_state IS NULL OR delivery_state = '')""",
                (token, _now_iso(), spawn_id),
            )
            conn.commit()
            row = conn.execute(
                "SELECT delivery_token FROM agent_spawns WHERE id = ?", (spawn_id,)
            ).fetchone()
            return token if row and row["delivery_token"] == token else None
        finally:
            conn.close()

    return _retry_write(_do)


def _spawns_finish_delivery(spawn_id: str, token: str, state: str) -> None:
    def _do():
        conn = _db_conn()
        if conn is None:
            return
        try:
            _ensure_table(conn)
            conn.execute(
                """UPDATE agent_spawns SET delivery_state = ?, updated_at = ?
                   WHERE id = ? AND delivery_token = ?""",
                (state, _now_iso(), spawn_id, token),
            )
            conn.commit()
        finally:
            conn.close()

    _retry_write(_do)


def _spawns_heartbeat(spawn_id: str, now_iso: Optional[str] = None) -> None:
    """Stamp a fresh ``heartbeat_at`` to prove this spawn's runner is alive. Called
    repeatedly by the runner while a spawn is queued/running. Cross-instance, this
    is the ONLY reliable liveness signal: a stale heartbeat means no live runner on
    any instance, so the row is safe to recover. Best-effort + quiet (a missed beat
    just shortens the liveness margin; the cutoff is many beats wide)."""
    ts = now_iso or _now_iso()

    def _do():
        conn = _db_conn()
        if conn is None:
            return
        try:
            conn.execute("UPDATE agent_spawns SET heartbeat_at = ? WHERE id = ?", (ts, spawn_id))
            conn.commit()
        finally:
            conn.close()

    try:
        _retry_write(_do)
    except Exception:  # noqa: BLE001 — a missed heartbeat is non-fatal
        pass


def _spawns_claim_due(now_iso: Optional[str] = None) -> list:
    """Rows whose follow-up timer elapsed; clear the timer so each fires once."""
    ts = now_iso or _now_iso()

    def _do():
        conn = _db_conn()
        if conn is None:
            return []
        try:
            _ensure_table(conn)
            rows = conn.execute(
                """SELECT * FROM agent_spawns
                   WHERE next_check_at IS NOT NULL AND next_check_at <= ?
                   ORDER BY next_check_at ASC""",
                (ts,),
            ).fetchall()
            claimed = [dict(r) for r in rows]
            for r in claimed:
                conn.execute(
                    "UPDATE agent_spawns SET next_check_at = NULL, updated_at = ? WHERE id = ?",
                    (ts, r["id"]),
                )
            conn.commit()
            return claimed
        finally:
            conn.close()

    return _retry_write(_do)


def _spawns_fail_orphans(now_iso: str, older_than_seconds: int, limit: int,
                         exclude_ids=()) -> list:
    """Fail — never re-run — forked spawns whose runner has died (e.g. a server
    restart/crash left them stuck 'running'/'queued'). A live runner proves itself
    by refreshing ``heartbeat_at`` every ``_HEARTBEAT_INTERVAL``s, so a row whose
    heartbeat is older than ``older_than_seconds`` has NO live runner on ANY
    instance and will never settle on its own.

    We deliberately flip such orphans to 'error' rather than re-executing their
    task: a re-run re-does everything the spawn already did, INCLUDING re-spawning
    the sub-helpers it created, so auto-re-running a self-orchestrating tree
    snowballs one restart into an exploding fan-out. The caller re-wakes the
    orchestrator instead, which can deliberately re-spawn anything it still needs.

    MULTI-INSTANCE SAFE: the flip is an atomic guarded UPDATE stamping a unique
    ``claim_token`` (``heartbeat_at <= cutoff`` re-checked at write time, and the
    status→'error' transition is itself the guard). Whoever's UPDATE lands first
    wins the row; every other instance's WHERE then misses. The token tells us
    which rows WE failed, so only we notify their orchestrator. ``exclude_ids`` is
    an extra in-process fast-skip for spawns with a live runner here; correctness
    rests on heartbeat + token. ``limit`` caps how many we fail per tick so a
    post-restart sweep doesn't fire a notification storm all at once."""
    cutoff = (datetime.fromisoformat(now_iso) - timedelta(seconds=older_than_seconds)).isoformat()
    excl = set(exclude_ids or ())
    token = uuid.uuid4().hex
    msg = ("This spawn was interrupted (most likely a server restart or crash) and "
           "did not finish. It was NOT automatically re-run, to avoid duplicating "
           "work it had already started. Re-spawn it if you still need the result; "
           "do not invent its output.")

    def _do():
        conn = _db_conn()
        if conn is None:
            return []
        try:
            _ensure_table(conn)
            # 1. Read a bounded batch of dead-runner candidates (cheap read).
            rows = conn.execute(
                """SELECT id FROM agent_spawns
                   WHERE status IN ('running', 'queued')
                     AND COALESCE(heartbeat_at, updated_at) <= ?
                   ORDER BY COALESCE(heartbeat_at, updated_at) ASC""",
                (cutoff,),
            ).fetchall()
            ids = [r["id"] for r in rows if r["id"] not in excl][:max(0, int(limit))]
            if not ids:
                return []
            # 2. Atomic fail-out: only rows STILL stale at write time get our token.
            ph = ",".join(["?"] * len(ids))
            conn.execute(
                f"""UPDATE agent_spawns
                    SET status = 'error', claim_token = ?, result_summary = ?, updated_at = ?
                    WHERE id IN ({ph})
                      AND status IN ('running', 'queued')
                      AND COALESCE(heartbeat_at, updated_at) <= ?""",
                (token, msg, now_iso, *ids, cutoff),
            )
            conn.commit()
            # 3. Fetch only what WE failed (our token).
            failed = conn.execute(
                "SELECT * FROM agent_spawns WHERE claim_token = ? AND status = 'error'", (token,)
            ).fetchall()
            return [dict(r) for r in failed]
        finally:
            conn.close()

    return _retry_write(_do)


# ════════════════════════════════════════════════════════════════════════════
# Runtime — create helper agents, run them (wait/fork), re-wake the orchestrator.
# A helper is "run" by POSTing to the local /api/v1/chat for its session, exactly
# like the optimizer's _kickstart_planner. The POST carries NO agent_id: chat.py
# routes the message to whatever agent the session is BOUND to (the helper agent
# for a spawn turn, the orchestrator agent for a re-wake). chat.py's agent
# resolution honors that binding before falling back to the user's default — if
# it didn't, an omitted agent_id would resolve to the default agent, mismatch the
# session's bound agent, and the endpoint's bind-mismatch guard would 500 (i.e.
# the spawn would never run). The loopback POST must also carry a JWT (see
# _internal_auth_headers) or chat.py's assert_caller_is rejects it 401 -> 500.
# ════════════════════════════════════════════════════════════════════════════

_SPAWN_PREAMBLE = (
    "You are a purpose-spawned helper agent created by an orchestrator agent to "
    "carry out a specific task. The 'user' in this conversation is the "
    "orchestrator, not a human. Work the task to completion, use your tools as "
    "needed, and report results clearly and concisely so the orchestrator can "
    "act on them. If you need a decision or more detail, ask in your reply.\n\n"
    "## Your task / directive\n"
)

# A delegation runs a REAL saved agent (its own persona/abilities/engine), so we
# never touch its prompt — instead the incoming task is framed inline as the first
# message. Kept short so it doesn't fight the agent's own identity.
_DELEGATION_PREAMBLE = (
    "[DELEGATED TASK] Another agent has handed you the task below to work on in "
    "this session. Carry it out with your own tools and judgement, then reply with "
    "the result clearly so it can be reported back. If you need a decision or more "
    "detail, say so in your reply.\n\n"
    "## Task\n"
)

# Tools that are inherently destructive regardless of an agent's own policy —
# mirror of app/agent/loop.py DESTRUCTIVE_TOOLS. Imported when available so the
# ceiling tracks core; the literal is the fallback if the import path changes.
try:  # pragma: no cover - import-shape guard
    from app.agent.loop import DESTRUCTIVE_TOOLS as _CORE_DESTRUCTIVE
    _HARDCODED_DESTRUCTIVE = set(_CORE_DESTRUCTIVE)
except Exception:  # noqa: BLE001
    _HARDCODED_DESTRUCTIVE = {"run_command", "restart_server"}


def _parse_json_list(raw) -> list:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            v = json.loads(raw)
            return v if isinstance(v, list) else []
        except Exception:  # noqa: BLE001
            return []
    return []


async def _compute_clone_grants(master_agent_id: str, requested_abilities, allow_destructive):
    """Compute what a clone is allowed, clamped so it can NEVER be looser than its
    master — the ceiling. Returns (granted_abilities, deny_tools, ask_tools).

    Two axes:
      • Ability axis — a clone only gets abilities the master itself has enabled;
        the requested list is intersected with the master's enabled abilities.
        Recursion is therefore opt-in: a clone gets the spawn tools only if the
        orchestrator explicitly lists 'agent_orchestration' (and has it itself).
      • Per-tool permission axis — for every tool the clone might run, its level is
        at most the master's level on the deny < ask < auto scale. A tool the
        master must CONFIRM (ask) can never be auto for the clone: it is blocked
        outright unless the orchestrator explicitly opts it in via allow_destructive
        (and even then it stays ask, never auto). A tool the master has blocked
        (deny) is blocked for the clone too. This is also the unattended-destructive
        policy: a clone runs with no human to confirm, so confirm-tools are off by
        default.
    """
    from app.db import get_db
    db = get_db()
    requested = {a.strip() for a in (requested_abilities or []) if isinstance(a, str) and a.strip()}
    opted_in = {t.strip() for t in (allow_destructive or []) if isinstance(t, str) and t.strip()}

    master = await db.get_agent_by_id(master_agent_id) or {}
    try:
        conns = await db.get_agent_connections(master_agent_id)
    except Exception:  # noqa: BLE001
        conns = []
    master_abilities = {
        c.get("connection_type") for c in conns
        if c.get("section") == "ability" and c.get("enabled")
    }
    granted = sorted(requested & master_abilities)

    master_deny = set(_parse_json_list(master.get("allowed_tools")))
    sp = master.get("safety_policy")
    if isinstance(sp, str):
        try:
            sp = json.loads(sp or "{}")
        except Exception:  # noqa: BLE001
            sp = {}
    master_ask = set(_parse_json_list((sp or {}).get("destructive_tools"))) | set(_HARDCODED_DESTRUCTIVE)

    clone_deny = sorted(master_deny | (master_ask - opted_in))
    clone_ask = sorted(master_ask & opted_in)
    return granted, clone_deny, clone_ask


async def _borrowed_prompt(from_agent: str) -> str:
    """Best-effort: assemble another agent/template's prompt text so the
    orchestrator can SEE and reuse it inside the clone's directive. Returns '' on
    any failure (the borrow is optional, never fatal)."""
    src = (from_agent or "").strip()
    if not src:
        return ""
    from app.db import get_db
    db = get_db()
    try:
        slots = await db.resolve_prompts(src, user_id=None)
    except Exception:  # noqa: BLE001
        return ""
    parts = []
    for s in slots or []:
        if s.get("slot_name") == "__skills__":
            continue
        c = (s.get("content") or "").strip()
        if c:
            parts.append(c)
    return "\n\n".join(parts).strip()


def _chat_url() -> str:
    return f"http://127.0.0.1:{os.environ.get('PORT', '8080')}/api/v1/chat"


def _internal_auth_headers(user_id: str) -> dict:
    """Mint a short-lived JWT so this loopback POST passes the chat endpoint's
    caller-identity check (`assert_caller_is`). Without it the request carries no
    token and is rejected as 401 "Not authenticated", which the chat handler then
    surfaces as a 500 — silently killing every spawn/re-wake. We act strictly on
    behalf of the session's own user, so minting that user's own token here is
    legitimate. Applies to BOTH agent types we drive: spawned worker sessions
    (via _run_spawn_turn) and the orchestrator's own session (via
    _rewake_orchestrator)."""
    try:
        from app.auth.jwt import create_access_token
        token = create_access_token(username=user_id, user_id=user_id)
        return {"Authorization": f"Bearer {token}"}
    except Exception as e:  # noqa: BLE001
        logger.warning("Could not mint internal auth token for %s: %s", user_id, e)
        return {}


async def _post_chat(user_id: str, session_id: str, message: str, timeout: float) -> str:
    import httpx
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            _chat_url(),
            json={"message": message, "user_id": user_id, "session_id": session_id},
            headers=_internal_auth_headers(user_id),
        )
        resp.raise_for_status()
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            return ""
        return (data.get("reply") or data.get("response") or "") if isinstance(data, dict) else ""


def _iso_minutes_from_now(minutes: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=max(0.0, minutes))).isoformat()


def _age_seconds(now_dt: datetime, iso: Optional[str]) -> Optional[int]:
    """Whole seconds between an ISO timestamp and now (None if unparseable)."""
    if not iso:
        return None
    try:
        return max(0, int((now_dt - datetime.fromisoformat(iso)).total_seconds()))
    except Exception:  # noqa: BLE001
        return None


def _spawn_health(status: Optional[str], since_heartbeat: Optional[int]) -> tuple:
    """Map (status, seconds-since-last-heartbeat) → (stalled?, human health line).

    `stalled` is True when a spawn that should have a live runner ('running' or
    'queued') has gone silent past the orphan window — i.e. no heartbeat from ANY
    instance — so it is almost certainly interrupted/dead, not progressing. The
    background recovery will re-run or fail it; meanwhile the orchestrator should
    treat it as NOT a result."""
    st = status or "unknown"
    stale = st in ("running", "queued") and since_heartbeat is not None and \
        since_heartbeat >= _ORPHAN_RUNNING_SECONDS
    if st == "done":
        return False, "done — finished; call quote_spawn for its actual result"
    if st == "error":
        return False, "failed — did not complete; there is no result to report"
    if st == "stopped":
        return False, "stopped — you halted it; transcript kept, no completed result"
    if st == "pending":
        return False, "pending — created but not started yet"
    if st == "queued":
        if stale:
            return True, (f"stalled — queued but no heartbeat for {since_heartbeat}s; its runner "
                          "is likely dead. Being auto-recovered/failed. NOT a result.")
        return False, "queued — waiting for a run slot (normal under load); not a result yet"
    if st == "running":
        if stale:
            return True, (f"stalled — 'running' but silent for {since_heartbeat}s; treat as "
                          "interrupted/failed, NOT progress. Being auto-recovered/failed.")
        return False, "running — actively working; not finished, not a result yet"
    return False, st


async def _load_orch_config(agent_id: str) -> dict:
    """Read per-agent orchestration limits from the agent_orchestration connection
    row's config JSON. Returns defaults for any missing keys."""
    if not agent_id:
        return dict(_ORCH_CONFIG_DEFAULTS)
    limits = dict(_ORCH_CONFIG_DEFAULTS)
    try:
        from app.db import get_db
        db = get_db()
        conns = await db.get_agent_connections(agent_id)
        for c in conns:
            if c.get("connection_type") == "agent_orchestration" and c.get("section") == "ability":
                raw = c.get("config")
                if isinstance(raw, str):
                    raw = json.loads(raw or "{}")
                if isinstance(raw, dict):
                    settings = raw.get("ability_settings") or {}
                    for k in _ORCH_CONFIG_DEFAULTS:
                        if k in settings:
                            v = settings[k]
                            # Type-coerce: the API stores everything as JSON, so
                            # numbers may come back as strings.
                            if k in ("max_concurrent_spawns", "max_spawns_per_agent",
                                     "max_spawns_per_spawn", "max_spawn_depth"):
                                try:
                                    v = int(v)
                                except (TypeError, ValueError):
                                    continue
                            elif k == "auto_stop_on_session_end":
                                v = bool(v)
                            elif k == "default_spawn_mode":
                                # UI stores a boolean (ON = wait, OFF = fork).
                                if isinstance(v, bool):
                                    v = "wait" if v else "fork"
                                elif isinstance(v, str):
                                    v = v.lower() if v.lower() in ("fork", "wait") else "fork"
                                else:
                                    v = "fork"
                            elif k == "completion_delivery":
                                v = str(v).lower().strip()
                                if v not in ("resume", "notify"):
                                    v = "resume"
                            elif k == "delegation_level":
                                v = str(v).lower().strip()
                                if v not in ("conservative", "balanced", "eager"):
                                    v = "balanced"
                            limits[k] = v
                break
    except Exception as e:
        logger.debug("Failed to load orchestration config for agent %s: %s", agent_id, e)
    return limits


async def _user_brain_models(user_id: str) -> dict:
    """Resolve the user's model roster for per-helper model selection.

    Returns {"available": [...enabled tool-capable brain ids...],
             "high_effort": [...premium-role ids...],
             "vision": [...image-capable ids...],
             "default": "<the app/agent default id>"}.

    Mirrors the Model Switcher's ``_enabled_brains`` so a per-spawn model is
    validated against exactly the same ladder the chat footer picker / set_model
    use (enabled + text + tool-capable). Empty lists on any failure — the caller
    treats an unmatched requested model as a hard error rather than silently
    inheriting. SISTER-SYNC: SESSION-MODEL-OVERRIDE (model_switcher.py)."""
    try:
        from app import model_catalog
        await model_catalog.ensure_fresh()
    except Exception:  # noqa: BLE001
        pass
    try:
        from app.admin.settings import (
            load_llm_capabilities_for_user, high_effort_targets, _is_tool_capable)
        caps = await load_llm_capabilities_for_user(user_id)
    except Exception as e:  # noqa: BLE001
        logger.debug("Could not load model caps for %s: %s", user_id, e)
        return {"available": [], "high_effort": [], "vision": [], "default": ""}
    d = caps.get("default") or {}
    entries = ([d] if d.get("model") else []) + [
        r for r in (caps.get("racers") or []) if r.get("enabled") and r.get("model")
    ]
    available = sorted({e["model"] for e in entries
                        if e.get("text_capable") and _is_tool_capable(e)})
    vision = sorted({e["model"] for e in entries
                     if e.get("model") and e.get("image_capable")})
    return {
        "available": available,
        "high_effort": list(high_effort_targets(caps)),
        "vision": vision,
        "default": d.get("model") or "",
    }


async def _compute_spawn_depth(orchestrator_session_id: str) -> int:
    """Determine the depth of a new spawn by looking up its parent.

    Depth semantics:
      0 = the root orchestrator agent (main agent — NOT a spawn itself)
      1 = direct child of the root
      2 = grandchild (child of a depth-1 spawn), etc.

    A new spawn's depth = parent's depth + 1.  We find the parent's depth by
    looking up the spawn row whose spawn_session_id matches this orchestrator
    session id.  If none exists (the orchestrator is the root agent), depth is 0."""
    if not orchestrator_session_id or not orchestrator_session_id.startswith("spawn-"):
        return 0  # root agent
    # The parent is the spawn whose spawn_session_id IS this orchestrator session.
    conn = _db_conn()
    if conn is None:
        return 0
    try:
        _ensure_table(conn)
        row = conn.execute(
            "SELECT depth FROM agent_spawns WHERE spawn_session_id = ?",
            (orchestrator_session_id,)
        ).fetchone()
        if row and row["depth"] is not None:
            return int(row["depth"])
    except Exception:
        pass
    finally:
        conn.close()
    return 0  # fallback: treat as root


async def _enforce_spawn_limits(orchestrator_agent_id: str,
                                orchestrator_session_id: str,
                                parent_depth: int) -> Optional[str]:
    """Reserve capacity for user-task helpers owned by one working agent.

    Spawn history is intentionally not a quota. Only live user-task helpers in
    this orchestrator session count, so terminal rows immediately free a slot.
    Contract/manager workers do not call this function and therefore never
    consume or block user-task capacity.
    """
    cfg = await _load_orch_config(orchestrator_agent_id)
    max_depth = cfg.get("max_spawn_depth", 2)
    if parent_depth > 0 and parent_depth >= max_depth:
        return (f"Spawn depth limit reached: this spawn is at depth {parent_depth} "
                f"and max_spawn_depth is {max_depth} (spawns at depth "
                f"{parent_depth} cannot create further helpers).")

    # A pending row is the durable reservation written before the helper's
    # runner begins. Count it with queued/running/stopping rows so a burst of
    # tool calls cannot oversubscribe the ten-worker capacity. Completed,
    # failed, cancelled, and timed-out rows remain available for audit but no
    # longer consume a slot.
    max_concurrent = int(cfg.get("max_concurrent_spawns", 10) or 10)
    max_concurrent = max(1, max_concurrent)
    active_statuses = {"pending", "queued", "running", "stopping"}
    active_count = sum(
        1 for row in _spawns_list(orchestrator_session_id)
        if str(row.get("kind") or "") != "contract"
        and str(row.get("status") or "") in active_statuses
    )
    if active_count >= max_concurrent:
        return (f"Concurrent worker spawn limit reached: {active_count} active "
                f"subagents are already reserved, queued, or running for this "
                f"working agent (max {max_concurrent}). Wait for one to finish "
                f"before spawning another.")
    return None


async def _create_spawn_unlocked(*, user_id, orchestrator_session_id, orchestrator_agent_id,
                        task, name="", system_prompt="", from_agent="",
                        abilities=None, allow_destructive=None, output_contract="",
                        context_inherit=False, model="", forced_deny_tools=None,
                        kind="", enforce_worker_limits: bool = True) -> dict:
    """Materialize a CLONE helper + its own tagged session + an agent_spawns row.

    The helper is an ephemeral clone of the orchestrator (built from scratch on
    the app-global baseline), with abilities OFF except those explicitly granted
    and clamped to the orchestrator's own ceiling (see _compute_clone_grants).

    Enforces per-agent orchestration limits (spawn count, depth, per-spawn cap)
    before creating — returns an error dict instead of a spawn on breach."""
    from app.db import get_db
    db = get_db()

    spawn_name = (name or "").strip() or (task[:40].strip() if task else "Helper")

    # ── Enforce per-agent orchestration limits ──────────────────────────
    parent_depth = await _compute_spawn_depth(orchestrator_session_id)
    new_depth = parent_depth + 1
    if enforce_worker_limits:
        limit_error = await _enforce_spawn_limits(
            orchestrator_agent_id, orchestrator_session_id, parent_depth)
        if limit_error:
            return {"id": "__blocked__", "error": True,
                    "message": limit_error, "parent_depth": parent_depth,
                    "new_depth": new_depth}

    # ── Optional per-helper model override ──────────────────────────────
    # The orchestrator may put THIS helper on a specific model (e.g. a vision
    # model for a screenshot-reading helper, the high-effort model for a hard
    # reasoning helper, the cheap default for boilerplate). Validate it against
    # the SAME enabled tool-capable ladder set_model uses — and, like that path,
    # the clone keeps the orchestrator's provider account (base_url + key), so the
    # model must be reachable on it (the normal single-account case). Reject an
    # unknown id up front rather than silently inheriting, so a typo is obvious.
    spawn_model = (model or "").strip()
    if spawn_model and spawn_model.lower() in ("default", "inherit", "same"):
        spawn_model = ""
    if spawn_model:
        roster = await _user_brain_models(user_id)
        if spawn_model not in (roster.get("available") or []):
            return {"id": "__blocked__", "error": True,
                    "message": (f"Requested helper model '{spawn_model}' isn't one of your "
                                f"enabled, tool-capable models. Choose one of: "
                                f"{', '.join(roster.get('available') or []) or '(none configured)'} "
                                f"— or omit `model` to run the helper on your own model."),
                    "available_models": roster.get("available") or []}

    # Ceiling: clamp requested abilities + tool permissions to the orchestrator's.
    granted, clone_deny, clone_ask = ([], [], [])
    if orchestrator_agent_id:
        try:
            granted, clone_deny, clone_ask = await _compute_clone_grants(
                orchestrator_agent_id, abilities, allow_destructive)
        except Exception as e:  # noqa: BLE001
            logger.warning("Clone grant computation failed (defaulting to no abilities): %s", e)
    # Internal callers such as the subagent contract layer can make a broad
    # ability (notably codebase_admin) read-only by adding an immutable deny
    # ceiling.  The user-facing spawn tool never supplies this argument.
    clone_deny = sorted(set(clone_deny) | {
        str(tool).strip() for tool in (forced_deny_tools or [])
        if str(tool).strip()
    })
    clone_ask = sorted(set(clone_ask) - set(clone_deny))

    agent = await db.create_clone_agent(
        user_id=user_id, master_agent_id=orchestrator_agent_id or "",
        name=spawn_name,
        description=f"Clone helper for orchestrator session {orchestrator_session_id[:12]}",
        abilities=granted, allowed_tools=clone_deny, destructive_ask=clone_ask,
    )
    spawn_agent_id = agent["id"]

    # Apply the per-helper model override (validated above). create_clone_agent
    # inherited the orchestrator's model; overwrite just the clone's model id,
    # keeping the inherited provider/base_url/api_key so the override stays on the
    # same account. Best-effort: a failed UPDATE just leaves the inherited model.
    if spawn_model:
        try:
            conn = _db_conn()
            if conn is not None:
                conn.execute("UPDATE agents SET model = ?, updated_at = ? WHERE id = ?",
                             (spawn_model, _now_iso(), spawn_agent_id))
                conn.commit()
                conn.close()
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not set per-spawn model %s on clone %s: %s",
                           spawn_model, spawn_agent_id, e)

    # Build the clone's directive slot from (optional) borrowed prompt text, the
    # orchestrator's custom system_prompt, and an optional output contract. A
    # fire-and-forget worker with none of these gets no slot — it runs on the
    # app-global baseline + the task message alone.
    directive_parts = []
    borrowed = await _borrowed_prompt(from_agent) if (from_agent or "").strip() else ""
    if borrowed:
        directive_parts.append(
            f"## Reference identity borrowed from '{from_agent.strip()}'\n{borrowed}")
    if (system_prompt or "").strip():
        directive_parts.append(system_prompt.strip())
    if (output_contract or "").strip():
        directive_parts.append(
            "## Required output shape\n"
            "Return your result in exactly this shape/length:\n" + output_contract.strip())
    if directive_parts:
        try:
            await db.upsert_slot(
                agent_id=spawn_agent_id, slot_name="orchestrator_directive",
                order_index=0, lock=True, merge_mode="replace",
                content=_SPAWN_PREAMBLE + "\n\n".join(directive_parts), updated_by="orchestrator",
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not set spawn directive prompt: %s", e)

    spawn_session_id = f"spawn-{uuid.uuid4().hex[:10]}"
    meta = json.dumps({
        "source": "spawn",
        "orchestrator_session_id": orchestrator_session_id,
        "orchestrator_agent_id": orchestrator_agent_id,
        "spawn_name": spawn_name,
    })
    conn = _db_conn()
    if conn is not None:
        try:
            conn.execute(
                "INSERT INTO sessions (id, user_id, title, agent_id, metadata) VALUES (?, ?, ?, ?, ?)",
                (spawn_session_id, user_id, spawn_name[:60], spawn_agent_id, meta),
            )
            conn.commit()
        finally:
            conn.close()
    try:
        await db.bind_session_to_agent(spawn_session_id, spawn_agent_id)
    except Exception:  # noqa: BLE001
        pass

    # ── Context inheritance ────────────────────────────────────────────
    # Copy the orchestrator's full conversation history into the spawn's
    # session so the spawn knows everything the orchestrator knew at creation
    # time. The spawn's first turn sees the task message ON TOP of this
    # history. Context only flows one way: the spawn does not push changes
    # back to the orchestrator's session.
    if context_inherit:
        try:
            n = await _copy_orchestrator_context(
                db, user_id, orchestrator_session_id, spawn_session_id)
            if n > 0:
                spawn_name = f"{spawn_name} [ctx:{n}]"
                row = _spawns_create(
                    user_id=user_id, orchestrator_session_id=orchestrator_session_id,
                    orchestrator_agent_id=orchestrator_agent_id, spawn_session_id=spawn_session_id,
                    spawn_agent_id=spawn_agent_id, name=spawn_name, task=task or "", status="pending",
                    depth=new_depth,
                )
                row["granted_abilities"] = granted
                row["confirm_tools"] = clone_ask
                row["spawn_model"] = spawn_model
                return row
        except Exception as e:  # noqa: BLE001
            logger.warning("Context inheritance for spawn failed (non-fatal): %s", e)

    row = _spawns_create(
        user_id=user_id, orchestrator_session_id=orchestrator_session_id,
        orchestrator_agent_id=orchestrator_agent_id, spawn_session_id=spawn_session_id,
        spawn_agent_id=spawn_agent_id, name=spawn_name, task=task or "", status="pending",
        depth=new_depth, kind=kind or None,
    )
    # Echo the (clamped) grants so the orchestrator can see what the ceiling allowed.
    row["granted_abilities"] = granted
    row["confirm_tools"] = clone_ask
    row["spawn_model"] = spawn_model
    return row


async def _create_spawn(*, user_id, orchestrator_session_id, orchestrator_agent_id,
                        task, name="", system_prompt="", from_agent="",
                        abilities=None, allow_destructive=None, output_contract="",
                        context_inherit=False, model="", forced_deny_tools=None,
                        kind="", enforce_worker_limits: bool = True) -> dict:
    """Create a helper after atomically reserving user-worker capacity.

    Manager contract workers use an isolated process path and bypass this user
    worker reservation entirely.
    """
    kwargs = dict(
        user_id=user_id, orchestrator_session_id=orchestrator_session_id,
        orchestrator_agent_id=orchestrator_agent_id, task=task, name=name,
        system_prompt=system_prompt, from_agent=from_agent, abilities=abilities,
        allow_destructive=allow_destructive, output_contract=output_contract,
        context_inherit=context_inherit, model=model,
        forced_deny_tools=forced_deny_tools, kind=kind,
        enforce_worker_limits=enforce_worker_limits,
    )
    if not enforce_worker_limits:
        return await _create_spawn_unlocked(**kwargs)
    async with _worker_spawn_create_lock():
        return await _create_spawn_unlocked(**kwargs)


# Tools denied to source-inspecting contract workers.  The worker may receive
# codebase_admin for its read/search surface, but can never mutate files, execute
# arbitrary code, restart services, orchestrate children, or take external/git
# actions.  This is enforced in the clone record, not left to the role prompt.
_CONTRACT_SOURCE_READ_ONLY_DENY = {
    "db_query",
    "write_source", "edit_source", "patch_source", "delete_source",
    "run_command", "run_python", "restart_server", "terminal_open",
    "terminal_send", "terminal_close", "spawn_agent", "fork_self",
    "delegate_to_agent", "delegate_task_to_agent", "run_optimizer",
    "commit_and_push", "git_commit", "git_push", "git_checkout",
}


async def create_contract_worker(*, user_id: str, orchestrator_session_id: str,
                                 orchestrator_agent_id: str, name: str,
                                 system_prompt: str, output_contract: str,
                                 permission_profile: str = "tool_free",
                                 model: str = "") -> dict:
    """Create a role-locked worker for the runtime contract supervisor.

    This is a programmatic adapter over the same clone/session/storage path used
    by ``spawn_agent``.  It deliberately bypasses the user-facing tool permission
    prompt because the owning runtime trigger, not the model, invokes it.  Worker
    authority remains clamped to the parent and the selected fixed profile.
    """
    profile = str(permission_profile or "tool_free").strip().lower()
    if profile not in {"tool_free", "source_read_only"}:
        return {"id": "__blocked__", "error": True,
                "message": f"Unknown contract permission profile: {profile}"}
    abilities = ["codebase_admin"] if profile == "source_read_only" else []
    forced_deny = _CONTRACT_SOURCE_READ_ONLY_DENY if abilities else set()
    return await _create_spawn(
        user_id=user_id,
        orchestrator_session_id=orchestrator_session_id,
        orchestrator_agent_id=orchestrator_agent_id,
        task="",
        name=name,
        system_prompt=system_prompt,
        abilities=abilities,
        allow_destructive=[],
        output_contract=output_contract,
        context_inherit=False,
        model=model,
        forced_deny_tools=forced_deny,
        kind="contract",
        # Contract workers are manager infrastructure, not user-task helpers.
        # They use their own process lifecycle and must never consume the
        # working agent's active-worker reservations.
        enforce_worker_limits=False,
    )


async def send_contract_request(*, user_id: str, spawn_id: str,
                                spawn_session_id: str, request: str,
                                timeout: float = 60.0,
                                permission_profile: str = "tool_free",
                                generation: str = "",
                                agent_id: str = "") -> dict:
    """Run one contract turn in a killable subprocess.

    Ordinary orchestration keeps using ``_run_spawn_turn``.  Only contracts use
    this process boundary, so a slow provider/tool cannot delay the supervisor's
    wall-clock deadline or the API server's event loop.
    """
    profile = str(permission_profile or "tool_free").strip().lower()
    if profile not in {"tool_free", "source_read_only"}:
        raise ValueError(f"Unknown contract permission profile: {profile}")
    deadline = time.monotonic() + max(1.0, float(timeout))
    payload = {
        "user_id": user_id, "agent_id": agent_id,
        "spawn_id": spawn_id, "spawn_session_id": spawn_session_id,
        "request": request, "permission_profile": profile,
        "generation": generation,
        "deadline_monotonic": deadline,
    }
    root = Path(__file__).resolve().parents[4]
    env = os.environ.copy()
    env["WEBAGENT_CONTRACT_SUBPROCESS"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    proc_kwargs = {}
    if os.name == "nt":
        proc_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    python_executable = (
        getattr(sys, "_base_executable", sys.executable)
        if os.name == "nt" else sys.executable
    )
    if os.name == "nt":
        # Bypass the Windows venv redirector (which otherwise remains as a
        # wrapper process and launches the real interpreter as its child).
        # The child still receives this server's exact import environment.
        inherited_paths = [entry for entry in sys.path if entry]
        env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(inherited_paths))
    proc = await asyncio.create_subprocess_exec(
        python_executable, "-m", "app.agent.contract_worker_process",
        cwd=str(root), env=env,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, **proc_kwargs,
    )
    _CONTRACT_PROCESSES[spawn_id] = proc
    task = asyncio.current_task()
    if task is not None:
        _CONTRACT_PROCESS_TASKS[spawn_id] = task
    _LIVE_RUNS.add(spawn_id)
    _spawns_update(spawn_id, status="running")
    _spawns_heartbeat(spawn_id)
    heartbeat = asyncio.create_task(_heartbeat_loop(spawn_id))
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise asyncio.TimeoutError
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(json.dumps(payload).encode("utf-8")),
            timeout=remaining,
        )
    except asyncio.TimeoutError:
        await _terminate_contract_process(spawn_id, terminal_status="timeout")
        raise
    except asyncio.CancelledError:
        await asyncio.shield(
            _terminate_contract_process(spawn_id, terminal_status="stopped")
        )
        raise
    finally:
        heartbeat.cancel()
        try:
            await heartbeat
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        _CONTRACT_PROCESSES.pop(spawn_id, None)
        _CONTRACT_PROCESS_TASKS.pop(spawn_id, None)
        _LIVE_RUNS.discard(spawn_id)
    stderr_text = stderr.decode("utf-8", errors="replace")[-2000:]
    try:
        result = json.loads(stdout.decode("utf-8"))
    except Exception as exc:
        _spawns_update(spawn_id, status="error", result_summary=stderr_text or str(exc))
        return {"status": "error", "error": "contract subprocess returned invalid JSON"}
    if proc.returncode == 124 or result.get("status") == "timeout":
        error = str(result.get("error") or "contract subprocess deadline exceeded")
        _spawns_update(spawn_id, status="timeout", result_summary=error[:2000])
        raise asyncio.TimeoutError(error)
    if proc.returncode != 0 or result.get("status") != "done":
        error = str(result.get("error") or stderr_text or f"process exited {proc.returncode}")
        _spawns_update(spawn_id, status="error", result_summary=error[:2000])
        return {"status": "error", "error": error}
    reply = str(result.get("reply") or "")
    _spawns_update(spawn_id, status="done", result_summary=reply[:2000])
    return {"status": "done", "reply": reply,
            "duration_ms": int(result.get("duration_ms") or 0)}


async def _terminate_contract_process(spawn_id: str, *, terminal_status: str) -> bool:
    """Terminate and reap a live contract process within five seconds."""
    proc = _CONTRACT_PROCESSES.get(spawn_id)
    if proc is None or proc.returncode is not None:
        _spawns_update(spawn_id, status=terminal_status)
        return True
    _spawns_update(spawn_id, status="stopping")
    try:
        proc.terminate()
    except ProcessLookupError:
        await proc.wait()
        _spawns_update(spawn_id, status=terminal_status)
        return True
    try:
        await asyncio.wait_for(proc.wait(), timeout=4.5)
    except asyncio.TimeoutError:
        proc.kill()
        try:
            await asyncio.wait_for(proc.wait(), timeout=0.25)
        except asyncio.TimeoutError:
            _spawns_update(spawn_id, status="stop_timeout")
            return False
    _spawns_update(spawn_id, status=terminal_status)
    return True


async def stop_contract_worker(*, spawn_id: str, spawn_session_id: str,
                               terminal_status: str = "stopped") -> None:
    """Best-effort stop for a contract worker owned by a completed/stale turn."""
    try:
        from app.db import get_db
        await get_db().set_interrupt(spawn_session_id)
    except Exception:
        pass
    terminal = terminal_status if terminal_status in {"stopped", "timeout"} else "stopped"
    await _terminate_contract_process(spawn_id, terminal_status=terminal)
    owner = _CONTRACT_PROCESS_TASKS.get(spawn_id)
    current = asyncio.current_task()
    if owner is not None and owner is not current and not owner.done():
        owner.cancel()


# Stable internal adapter vocabulary used by the contract supervisor. Keep the
# contract-prefixed functions above as compatibility aliases for early callers.
async def create_worker(**kwargs) -> dict:
    return await create_contract_worker(**kwargs)


async def send_worker_request(**kwargs) -> dict:
    return await send_contract_request(**kwargs)


async def run_workers_parallel(requests: list[dict]) -> list[dict]:
    return list(await asyncio.gather(*(
        send_worker_request(**request) for request in requests
    )))


async def stop_worker(**kwargs) -> None:
    await stop_contract_worker(**kwargs)


async def _create_fork(*, user_id, orchestrator_session_id, orchestrator_agent_id,
                        task, name="") -> dict:
    """Fork the orchestrator's OWN session as a 'fork_self' spawn.

    Unlike _create_spawn (fresh clone + empty session), a fork REUSES the same
    agent (no clone record, no prompt rebuild) and forks the existing session via
    the built-in POST /sessions/{id}/fork endpoint.  The forked session inherits
    the full conversation prefix — every interaction up to the fork point — so
    the LLM sees an IDENTICAL prefix for KV-cache hits on the system prompt AND
    the conversation history.  Only the new task message at the end is novel."""
    import httpx
    # ── Enforce same per-agent orchestration limits ──────────────────────
    parent_depth = await _compute_spawn_depth(orchestrator_session_id)
    new_depth = parent_depth + 1
    limit_error = await _enforce_spawn_limits(
        orchestrator_agent_id, orchestrator_session_id, parent_depth)
    if limit_error:
        return {"id": "__blocked__", "error": True,
                "message": limit_error, "parent_depth": parent_depth,
                "new_depth": new_depth}

    # ── Find the latest interaction in the orchestrator's session ───────
    from app.db import get_db
    db = get_db()
    try:
        records = await db.fetch_interactions(user_id, orchestrator_session_id)
    except Exception:
        records = []
    if not records:
        return {"id": "__blocked__", "error": True,
                "message": "Cannot fork: orchestrator session has no interactions yet."}

    last_interaction_id = None
    for r in records:
        rid = (getattr(r, "id", None) if hasattr(r, "id")
               else (r.get("id") if isinstance(r, dict) else None))
        if rid:
            last_interaction_id = str(rid)
    if not last_interaction_id:
        return {"id": "__blocked__", "error": True,
                "message": "Cannot fork: no valid interaction ID in the orchestrator session."}

    # ── Call the existing session-fork API ──────────────────────────────
    fork_url = f"http://127.0.0.1:{os.environ.get('PORT', '8080')}/api/v1/db/sessions/{orchestrator_session_id}/fork"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                fork_url,
                json={"up_to_interaction_id": last_interaction_id, "user_id": user_id},
                headers=_internal_auth_headers(user_id),
            )
            resp.raise_for_status()
            fork_result = resp.json()
    except Exception as e:
        logger.warning("Session fork for fork_self failed: %s", e)
        return {"id": "__blocked__", "error": True,
                "message": f"Session fork request failed: {e}"}

    fork_session_id = fork_result.get("session_id") if isinstance(fork_result, dict) else None
    if not fork_session_id:
        return {"id": "__blocked__", "error": True,
                "message": f"Session fork returned no session_id: {fork_result}"}

    fork_name = (name or "").strip() or (task[:40].strip() if task else "Fork helper")

    # ── Create agent_spawns ledger row ──────────────────────────────────
    # Same agent (not a clone) — the forked session runs as the orchestrator.
    row = _spawns_create(
        user_id=user_id, orchestrator_session_id=orchestrator_session_id,
        orchestrator_agent_id=orchestrator_agent_id,
        spawn_session_id=fork_session_id,
        spawn_agent_id=orchestrator_agent_id,
        name=fork_name, task=task or "", status="pending",
        depth=new_depth, kind="fork_self",
    )
    return row


async def _copy_orchestrator_context(db, user_id: str, from_session_id: str,
                                    to_session_id: str) -> int:
    """Copy all interactions from the orchestrator's session into the spawn's
    session so the spawn inherits the full conversation context at creation time.

    Re-maps all interaction ids and parent_ids so the parent-child chain remains
    intact within the spawn's session. Returns the number of interactions copied.
    """
    from app.db.local import _uuid
    try:
        records = await db.fetch_interactions(user_id, from_session_id)
    except Exception:
        return 0
    if not records:
        return 0
    # Build old-id → new-id map
    id_map: dict = {}
    for r in records:
        rid = getattr(r, "id", None) if hasattr(r, "id") else (r.get("id") if isinstance(r, dict) else None)
        if rid:
            id_map[str(rid)] = _uuid()
    # Build rows for batch insert, remapping ids and parent_ids
    rows: list = []
    for r in records:
        # Pydantic model → dict; plain dict passes through
        if hasattr(r, "model_dump"):
            d = r.model_dump()
        elif hasattr(r, "dict"):
            d = r.dict()
        elif isinstance(r, dict):
            d = r
        else:
            continue
        new_id = id_map.get(str(d.get("id", "")))
        if not new_id:
            continue
        old_parent = d.get("parent_id")
        new_parent = id_map.get(str(old_parent)) if old_parent else None
        rows.append({
            "id": new_id,
            "session_id": to_session_id,
            "parent_id": new_parent,
            "role": d.get("role", ""),
            "content": d.get("content", "") or "",
            "tool_name": d.get("tool_name"),
            "tool_call_id": d.get("tool_call_id"),
            "channel": d.get("channel"),
            "metadata": d.get("metadata"),
            "output": d.get("output"),
            "source": d.get("source"),
            "from_id": d.get("from_id"),
            "to_id": d.get("to_id"),
            "created_at": str(d.get("created_at")) if d.get("created_at") else None,
        })
    if rows:
        try:
            await db.insert_interactions_batch(rows)
        except Exception as e:
            logger.warning("Context copy for spawn failed (batch insert): %s", e)
            return 0
    return len(rows)


async def _resolve_delegate_target(user_id: str, ref: str) -> Optional[dict]:
    """Resolve a delegation target among the user's OWN saved agents.

    Unlike a clone spawn (built fresh + clamped to the orchestrator's ceiling), a
    delegation runs a REAL agent the user deliberately built — with its own
    persona, abilities and runtime engine (e.g. a Local Claude Code agent whose
    metadata.engine == 'claude_code'). We therefore resolve against the user's
    actual agent roster, not the template list, so the specific configured
    instance (its working folder, model, etc.) is the one that runs.

    `ref` may be an agent id (exact) or a name (case-insensitive). Pipeline agents
    and ephemeral clones are never delegation targets. Returns the agent dict
    (with 'engine' folded in for display) or None if nothing matches."""
    from app.db import get_db
    db = get_db()
    ref = (ref or "").strip()
    if not ref:
        return None
    try:
        agents = await db.list_agents_for_user(user_id, include_admin=True)
    except Exception as e:  # noqa: BLE001
        logger.warning("delegate target lookup failed: %s", e)
        return None

    def _engine_of(a: dict) -> str:
        meta = a.get("metadata")
        if isinstance(meta, str):
            try:
                meta = json.loads(meta or "{}")
            except Exception:  # noqa: BLE001
                meta = {}
        return str((meta or {}).get("engine") or "").strip() if isinstance(meta, dict) else ""

    eligible = [a for a in (agents or [])
                if not a.get("is_pipeline") and a.get("status") not in ("clone", "clone_trashed")]
    # id match first (unambiguous), then exact-name (case-insensitive).
    match = next((a for a in eligible if str(a.get("id")) == ref), None)
    if not match:
        match = next((a for a in eligible
                      if str(a.get("name") or "").strip().lower() == ref.lower()), None)
    if not match:
        return None
    match = dict(match)
    match["engine"] = _engine_of(match)
    return match


async def _create_delegation(*, user_id, orchestrator_session_id, orchestrator_agent_id,
                             target_agent_id, target_name, task, name="",
                             context_inherit=False) -> dict:
    """Materialize a DELEGATION: a spawn-family sub-session bound to an EXISTING
    saved agent (not a fresh clone), plus its agent_spawns ledger row tagged
    kind='delegate'.

    Reuses the spawn family plumbing wholesale — same ledger, same run/re-wake
    path, same follow-up timers, same tab-bar surfacing (the /related endpoint
    groups on orchestrator_session_id) — so a delegated agent behaves like any
    other family member. The ONE deliberate difference from _create_spawn: no
    clone is built and no ability/permission clamping happens. The target runs as
    its real self (its persona, abilities and metadata.engine), because it is an
    agent the user owns and configured. Because the sub-session is bound to that
    real agent, the loop's per-agent engine dispatch routes a Local Claude Code
    target through the `claude` CLI automatically on its first turn.

    Still enforces the per-agent orchestration limits (a delegation counts toward
    the same spawn caps, so it can't flood the account)."""
    # ── Enforce the same per-agent orchestration limits as a spawn ──────────
    parent_depth = await _compute_spawn_depth(orchestrator_session_id)
    new_depth = parent_depth + 1
    limit_error = await _enforce_spawn_limits(
        orchestrator_agent_id, orchestrator_session_id, parent_depth)
    if limit_error:
        return {"id": "__blocked__", "error": True,
                "message": limit_error, "parent_depth": parent_depth, "new_depth": new_depth}

    from app.db import get_db
    db = get_db()
    deleg_name = (name or "").strip() or (target_name or "").strip() \
        or (task[:40].strip() if task else "Delegate")

    spawn_session_id = f"spawn-{uuid.uuid4().hex[:10]}"
    meta = json.dumps({
        "source": "delegation",
        "orchestrator_session_id": orchestrator_session_id,
        "orchestrator_agent_id": orchestrator_agent_id,
        "delegate_agent_id": target_agent_id,
        "spawn_name": deleg_name,
    })
    conn = _db_conn()
    if conn is not None:
        try:
            conn.execute(
                "INSERT INTO sessions (id, user_id, title, agent_id, metadata) VALUES (?, ?, ?, ?, ?)",
                (spawn_session_id, user_id, deleg_name[:60], target_agent_id, meta),
            )
            conn.commit()
        finally:
            conn.close()
    try:
        await db.bind_session_to_agent(spawn_session_id, target_agent_id)
    except Exception:  # noqa: BLE001
        pass

    if context_inherit:
        try:
            n = await _copy_orchestrator_context(
                db, user_id, orchestrator_session_id, spawn_session_id)
            if n > 0:
                deleg_name = f"{deleg_name} [ctx:{n}]"
        except Exception as e:  # noqa: BLE001
            logger.warning("Context inheritance for delegation failed (non-fatal): %s", e)

    row = _spawns_create(
        user_id=user_id, orchestrator_session_id=orchestrator_session_id,
        orchestrator_agent_id=orchestrator_agent_id, spawn_session_id=spawn_session_id,
        spawn_agent_id=target_agent_id, name=deleg_name, task=task or "", status="pending",
        depth=new_depth, kind="delegate",
    )
    return row


async def _notify_orchestrator(user_id: str, orchestrator_session_id: str, note: str) -> None:
    """Deliver spawn status without creating a chat turn or resuming an agent."""
    try:
        from app.api.chat import notify_user
        await notify_user(user_id, {
            "type": "spawn_status",
            "session_id": orchestrator_session_id,
            "message": note,
        })
    except Exception as e:  # noqa: BLE001
        logger.warning("Spawn-status notification for %s failed (non-fatal): %s",
                       orchestrator_session_id, e)

_PARENT_RESUME_LOCKS: dict[str, asyncio.Lock] = {}


def _parent_resume_lock(session_id: str) -> asyncio.Lock:
    lock = _PARENT_RESUME_LOCKS.get(session_id)
    if lock is None:
        lock = asyncio.Lock()
        _PARENT_RESUME_LOCKS[session_id] = lock
    return lock


async def _deliver_terminal_event(spawn: dict, note: str) -> None:
    """Deliver one terminal child event, resuming its parent only when configured."""
    spawn_id = str(spawn.get("id") or "")
    user_id = str(spawn.get("user_id") or "")
    session_id = str(spawn.get("orchestrator_session_id") or "")
    if not spawn_id or not user_id or not session_id:
        return
    token = _spawns_claim_delivery(spawn_id)
    if not token:
        return
    mode = str(spawn.get("completion_delivery") or "").strip().lower()
    if not mode:
        cfg = await _load_orch_config(str(spawn.get("orchestrator_agent_id") or ""))
        mode = str(cfg.get("completion_delivery") or "resume")
    if mode == "notify":
        await _notify_orchestrator(user_id, session_id, note)
        _spawns_finish_delivery(spawn_id, token, "notified")
        return
    try:
        async with _parent_resume_lock(session_id):
            await _post_chat(user_id, session_id, note, timeout=300.0)
        _spawns_finish_delivery(spawn_id, token, "resumed")
    except Exception as e:  # noqa: BLE001
        logger.warning("Parent resume for spawn %s failed: %s", spawn_id, e)
        await _notify_orchestrator(
            user_id, session_id,
            note + "\n\nAutomatic parent resume failed; review this completion manually.",
        )
        _spawns_finish_delivery(spawn_id, token, "resume_failed_notified")

async def _heartbeat_loop(spawn_id: str) -> None:
    """Refresh a spawn's heartbeat every _HEARTBEAT_INTERVAL while its runner is
    alive (covers the whole life of the runner coroutine: queue-wait + run), so a
    live spawn is never recovered as 'dead' — on this OR any other instance."""
    try:
        while True:
            _spawns_heartbeat(spawn_id)
            await asyncio.sleep(_HEARTBEAT_INTERVAL)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        logger.debug("heartbeat loop for spawn %s stopped: %s", spawn_id, e)


async def _run_spawn_turn(*, user_id, spawn_id, spawn_session_id, message, wait,
                         notify_on_done=True, wait_timeout=300.0) -> dict:
    """WAIT: await the turn inline (bypasses the run-queue — see _run_slots),
    return the reply. FORK: return immediately as 'queued'; a background task
    waits for a run slot, executes the turn, settles the spawn, and (if
    notify_on_done) re-wakes the orchestrator with the result."""
    if wait:
        # Blocking runs execute inline under the caller and do NOT take a run
        # slot: they don't multiply concurrency, and throttling them would
        # deadlock a deep blocking chain. Tracked as live so recovery won't
        # double-run a legitimately long one.
        _spawns_update(spawn_id, status="running")
        _LIVE_RUNS.add(spawn_id)
        _spawns_heartbeat(spawn_id)
        hb = asyncio.create_task(_heartbeat_loop(spawn_id))
        try:
            reply = await _post_chat(user_id, spawn_session_id, message, wait_timeout)
            _spawns_update(spawn_id, status="done", result_summary=(reply or "")[:2000])
            return {"status": "done", "reply": reply or ""}
        except Exception as e:  # noqa: BLE001
            logger.warning("Spawn %s wait-run failed: %s", spawn_id, e)
            _spawns_update(spawn_id, status="error", result_summary=str(e)[:2000])
            return {"status": "error", "error": str(e)}
        finally:
            hb.cancel()
            try:
                await hb
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            _LIVE_RUNS.discard(spawn_id)

    spawn = _spawns_get(spawn_id) or {}
    orch_session = spawn.get("orchestrator_session_id")
    spawn_name = spawn.get("name") or spawn_id
    # A delegation (real saved agent) reads as "Delegation" in re-wake events; a
    # clone helper stays "Spawn". Both are managed with the same helper tools.
    kind = spawn.get("kind")
    noun = {"delegate": "Delegation", "fork_self": "Fork"}.get(kind, "Spawn")

    # Mark queued up-front: it has a live runner (below) but may sit waiting for a
    # run slot under heavy fan-out. 'queued' (not 'running') keeps the orphan
    # sweep from mistaking a slot-waiting spawn for a dead one.
    _spawns_update(spawn_id, status="queued")

    async def _bg():
        _LIVE_RUNS.add(spawn_id)
        _spawns_heartbeat(spawn_id)  # prove life immediately, even while queued
        hb = asyncio.create_task(_heartbeat_loop(spawn_id))
        try:
            # Drain through the global run-queue: unlimited spawns may be queued,
            # but only _MAX_CONCURRENT_SPAWN_RUNS execute their (heavy) loop at
            # once. The slot is held ONLY for the spawn's own turn — released
            # before the re-wake so the orchestrator's reaction isn't gated by it.
            async with _run_slots():
                _spawns_update(spawn_id, status="running")
                reply = await _post_chat(user_id, spawn_session_id, message, wait_timeout)
            _spawns_update(spawn_id, status="done", result_summary=(reply or "")[:2000])
            if notify_on_done and orch_session:
                await _deliver_terminal_event(spawn, (
                    f"[ORCHESTRATION EVENT] {noun} '{spawn_name}' (id {spawn_id}) finished.\n\n"
                    f"Result:\n{(reply or '').strip()[:2000]}\n\n"
                    "Verify the completed result before reporting it."
                ))
        except Exception as e:  # noqa: BLE001
            logger.warning("Spawn %s fork-run failed: %s", spawn_id, e)
            _spawns_update(spawn_id, status="error", result_summary=str(e)[:2000])
            if notify_on_done and orch_session:
                await _deliver_terminal_event(
                    spawn,
                    f"[ORCHESTRATION EVENT] {noun} '{spawn_name}' (id {spawn_id}) errored: {e}",
                )
        finally:
            hb.cancel()
            try:
                await hb
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            _LIVE_RUNS.discard(spawn_id)

    asyncio.create_task(_bg())
    return {"status": "queued"}


# ════════════════════════════════════════════════════════════════════════════
# Background service — durable follow-up-timer poller (discovered by app/main.py).
# Sibling in spirit to the automation scheduler: wakes every POLL_INTERVAL, claims
# spawns whose next_check_at elapsed, and re-wakes their orchestrator. DB-backed
# so timers survive a restart.
# ════════════════════════════════════════════════════════════════════════════

_POLL_INTERVAL = 20
_REMOTE_POLL_INTERVAL = 60   # remote DB: every tick is a network round-trip AND
                             # runs on the main loop, so poll far less often there


def _poll_interval() -> float:
    """Follow-up poll cadence. On a remote DB each tick's reads are costly network
    round-trips on the shared event loop, so we poll a third as often; a queued
    follow-up just fires up to a minute later, which is fine for human-scale
    check-ins. Local SQLite keeps the snappy 20s floor."""
    try:
        from app.db import is_remote_db
        if is_remote_db():
            return _REMOTE_POLL_INTERVAL
    except Exception:
        pass
    return _POLL_INTERVAL

# ── Spawn self-healing tunables ──────────────────────────────────────────────
# The core run self-healing (session_runs + boot resume) does NOT cover spawn
# turns: forked spawns run via the plain /api/v1/chat path and never register a
# session_runs row, so a restart orphans them with no recovery. This ability
# therefore recovers its OWN forked spawns from the same poller — but recovery
# here means FAIL-AND-NOTIFY, never re-run. A spawn proven dead (a stale heartbeat
# ⇒ no live runner on any instance) is flipped to 'error' and its orchestrator is
# re-woken so it can deliberately re-spawn the work. We never re-execute the task
# automatically: a re-run re-does everything the spawn already did, INCLUDING
# re-spawning the sub-helpers it created, so auto-re-running a self-orchestrating
# tree snowballs one restart into an exploding fan-out (observed: 6 → 120+ spawns).
_ORPHAN_RUNNING_SECONDS = 180   # 'running'/'queued' + heartbeat idle this long ⇒ runner is dead
_RECOVER_PER_TICK = 3           # fail at most this many orphans per tick (avoid a notify storm)

# ── Spawn run-queue (concurrency throttle) ───────────────────────────────────
# Spawns can spawn — self-orchestration is unlimited in DEPTH and BREADTH (an
# agent may fork as many helpers as it likes, and those helpers may fork their
# own). What is bounded is how many forked spawn loops EXECUTE at once. Each
# forked spawn runs a full agent loop (its own LLM calls + parallel tool batches)
# via a loopback into this same server process; letting dozens run simultaneously
# exhausts memory and hard-kills the process (observed: ~6 crashes/16min under a
# heavy fan-out). So forked execution drains through a global semaphore: requests
# are never capped, but only this many run concurrently — the rest sit 'queued'
# and start as slots free up. Blocking (wait=True) spawns BYPASS the semaphore:
# they run inline under their caller, so they don't multiply concurrency, and
# throttling them would deadlock a deep blocking chain (each level would hold a
# slot waiting for a deeper level that can't get one). Tune via env.
_MAX_CONCURRENT_SPAWN_RUNS = max(1, int(os.environ.get("WEBAGENT_SPAWN_CONCURRENCY", "10") or 10))
_RUN_SLOTS: Optional[asyncio.Semaphore] = None
# How often a live runner refreshes its heartbeat. Must be comfortably smaller
# than _ORPHAN_RUNNING_SECONDS so a healthy spawn beats several times within the
# window and is never mistaken for dead.
_HEARTBEAT_INTERVAL = max(5, int(os.environ.get("WEBAGENT_SPAWN_HEARTBEAT", "20") or 20))
# spawn_ids whose runner coroutine is alive THIS process (queued-and-waiting or
# actively running). Recovery skips these: a spawn that's stuck 'running'/'queued'
# but NOT in this set has no live runner (e.g. a restart killed it) ⇒ orphaned.
# This also prevents wrongly re-running a legitimately long live spawn.
_LIVE_RUNS: set = set()
_CONTRACT_PROCESSES: dict[str, asyncio.subprocess.Process] = {}
_CONTRACT_PROCESS_TASKS: dict[str, asyncio.Task] = {}


def _run_slots() -> asyncio.Semaphore:
    """Lazily create the run-queue semaphore so it binds to the running loop."""
    global _RUN_SLOTS
    if _RUN_SLOTS is None:
        _RUN_SLOTS = asyncio.Semaphore(_MAX_CONCURRENT_SPAWN_RUNS)
    return _RUN_SLOTS


_poll_task: Optional[asyncio.Task] = None
_poll_running = False


async def _resume_orphaned_spawns(now_iso: str) -> None:
    """Recover forked spawns whose runner died (server restart, crash): they sit at
    'running'/'queued' with a stale heartbeat and no live coroutine. We FAIL them
    honestly and re-wake their orchestrator — we never re-run the task, because a
    re-run re-does everything the spawn already did (including re-spawning its
    sub-helpers), which snowballs one restart into an exploding fan-out. The
    orchestrator can deliberately re-spawn anything it still needs."""
    live = set(_LIVE_RUNS)
    try:
        # Run the blocking DB sweep in a worker thread so a remote round-trip
        # doesn't freeze the event loop (and any in-flight chat stream) mid-tick.
        failed = await asyncio.to_thread(
            _spawns_fail_orphans, now_iso, _ORPHAN_RUNNING_SECONDS,
            _RECOVER_PER_TICK, exclude_ids=live)
    except Exception as e:  # noqa: BLE001
        logger.debug("orphan fail-out sweep failed: %s", e)
        return
    for dead in failed:
        orch = dead.get("orchestrator_session_id")
        logger.info("Failed orphaned spawn %s '%s' (interrupted; not re-run)",
                    dead.get("id"), dead.get("name"))
        # Contract recovery is owned by ContractSupervisor's durable request
        # fence. Never wake the primary agent from the generic orchestration
        # path or blindly replay a potentially side-effecting parent turn.
        if dead.get("kind") == "contract":
            continue
        if not orch:
            continue
        try:
            await _deliver_terminal_event(dead, (
                f"[ORCHESTRATION EVENT] Spawn '{dead.get('name') or dead.get('id')}' "
                f"(id {dead.get('id')}) FAILED to complete — it was interrupted (likely a "
                "server restart) and was NOT automatically re-run. Re-spawn it if you still "
                "need it, or report it as failed. Do not invent its result."))
        except Exception as e:  # noqa: BLE001
            logger.warning("Re-wake after fail-out failed for spawn %s: %s", dead.get("id"), e)


async def _poll_tick() -> None:
    now = _now_iso()
    # Self-heal forked spawns orphaned by a dead runner (e.g. a server restart).
    try:
        await _resume_orphaned_spawns(now)
    except Exception as e:  # noqa: BLE001
        logger.debug("orphaned-spawn recovery failed: %s", e)
    try:
        due = await asyncio.to_thread(_spawns_claim_due, now_iso=now)
    except Exception as e:  # noqa: BLE001
        logger.debug("spawn check claim failed (table not ready?): %s", e)
        return
    for spawn in due:
        try:
            name = spawn.get("name") or spawn.get("id")
            status = spawn.get("status") or "unknown"
            summary = (spawn.get("result_summary") or "").strip()[:1500]
            check_note = (spawn.get("check_note") or "").strip()
            note = (
                f"[ORCHESTRATION EVENT] Follow-up timer for spawn '{name}' "
                f"(id {spawn.get('id')}).\nCurrent status: {status}.\n"
            )
            if summary:
                note += f"Latest result/summary:\n{summary}\n"
            if check_note:
                note += f"\nYour reminder: {check_note}\n"
            note += ("\nDecide what to do: read_spawn for the full transcript, "
                     "message_spawn to nudge or redirect it, schedule_spawn_check to set "
                     "another timer, stop_spawn to halt it, or report back.")
            await _notify_orchestrator(spawn.get("user_id"),
                                       spawn.get("orchestrator_session_id"), note)
        except Exception as e:  # noqa: BLE001
            logger.warning("Follow-up re-wake failed for spawn %s: %s", spawn.get("id"), e)


async def _poll_loop() -> None:
    await asyncio.sleep(3)
    while _poll_running:
        try:
            await _poll_tick()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.exception("Orchestration poller tick failed: %s", e)
        try:
            await asyncio.sleep(_poll_interval())
        except asyncio.CancelledError:
            raise


async def start_background() -> None:
    """Discovered + called by app/main.py at startup."""
    global _poll_task, _poll_running
    if _poll_task and not _poll_task.done():
        return
    _poll_running = True
    _poll_task = asyncio.create_task(_poll_loop(), name="orchestration_followup_loop")
    logger.info("Orchestration follow-up poller started (every %ss)", _POLL_INTERVAL)


async def stop_background() -> None:
    global _poll_running
    _poll_running = False
    if _poll_task:
        _poll_task.cancel()
        try:
            await _poll_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    if _CONTRACT_PROCESSES:
        await asyncio.gather(*(
            _terminate_contract_process(spawn_id, terminal_status="stopped")
            for spawn_id in list(_CONTRACT_PROCESSES)
        ), return_exceptions=True)
    logger.info("Orchestration follow-up poller stopped")


# ════════════════════════════════════════════════════════════════════════════
# Tools — build_tools() is discovered + called by app/tools/loader.py for every
# agent that has this ability enabled. Returns the spawn-and-oversee handlers.
# ════════════════════════════════════════════════════════════════════════════

_PIPELINE_TEMPLATES = ("opt_planner", "opt_closer")
_MAX_TRANSCRIPT = 40

# Sentinel key the agent loop watches for in a tool result to switch agents
# mid-session (folded in from the former app/tools/delegation.py — keep the value
# identical so the loop's detection still matches).
DELEGATE_SENTINEL = "__delegate__"


def _err(msg: str, **extra) -> str:
    out = {"status": "error", "error": msg}
    out.update(extra)
    return json.dumps(out)


def _delegation_signal(target_template_id: str, target_agent_id: str,
                       target_name: str, context: str) -> str:
    """Return the sentinel JSON string the loop detects to hand off the session."""
    return json.dumps({
        DELEGATE_SENTINEL: True,
        "target_template_id": target_template_id,
        "target_agent_id":    target_agent_id,
        "target_name":        target_name,
        "context":            context,
    })


def build_tools(*, user_id: str = "", session_id: str = "", agent_id: str = "",
                agent_template_id: Optional[str] = None, **_ctx):
    """Return this session's orchestration tools, subject to configured limits."""
    if not session_id:
        return {}
    if agent_template_id in _PIPELINE_TEMPLATES:
        return {}

    async def _owned_spawn(spawn_id: str) -> Optional[dict]:
        spawn = _spawns_get(spawn_id)
        if not spawn or spawn.get("orchestrator_session_id") != session_id:
            return None
        return spawn

    async def spawn_agent(task: str, name: str = "", system_prompt: str = "",
                          from_agent: str = "", wait: bool = None,
                          check_back_minutes: float = 0, abilities: Optional[list] = None,
                          allow_destructive: Optional[list] = None,
                          output_contract: str = "",
                          context_inherit: bool = False, model: str = "") -> str:
        """Spawn a fresh, least-privileged clone for a self-contained task.

        Clones start without abilities and may receive only permissions you hold.
        Use `wait=True` for a needed-now result; otherwise fork and handle its
        one terminal event. Use `image_vision` to inspect images, not a vision
        model as the helper brain. `output_contract` keeps results concise.
        """
        if not (task or "").strip() and not (system_prompt or "").strip():
            return _err("Provide a task (and/or a system_prompt) for the spawn.")
        try:
            spawn = await _create_spawn(
                user_id=user_id, orchestrator_session_id=session_id,
                orchestrator_agent_id=agent_id or None, task=task or "",
                name=name or "", system_prompt=system_prompt or "", from_agent=from_agent or "",
                abilities=abilities or [], allow_destructive=allow_destructive or [],
                output_contract=output_contract or "",
                context_inherit=bool(context_inherit), model=model or "",
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("spawn_agent create failed: %s", e)
            return _err(f"Could not create spawn: {e}")

        # If _create_spawn returned an error (limit breach), surface it.
        if spawn.get("error"):
            return json.dumps({"status": "error", "error": spawn.get("message" or "Limit reached."),
                               "detail": spawn})

        spawn_id = spawn["id"]
        if check_back_minutes and float(check_back_minutes) > 0:
            try:
                _spawns_update(spawn_id, next_check_at=_iso_minutes_from_now(float(check_back_minutes)),
                               check_note=f"Scheduled check-in on spawn '{spawn['name']}'.")
            except Exception:  # noqa: BLE001
                pass

        first_message = (task or system_prompt or "Begin your task.").strip()
        # If the orchestrator didn't explicitly pick a mode, use the per-agent default.
        if wait is None:
            cfg = await _load_orch_config(agent_id)
            wait = cfg.get("default_spawn_mode") == "wait"
        result = await _run_spawn_turn(user_id=user_id, spawn_id=spawn_id,
                                       spawn_session_id=spawn["spawn_session_id"],
                                       message=first_message, wait=bool(wait))
        out = {"status": "ok", "spawn_id": spawn_id, "name": spawn["name"],
               "spawn_session_id": spawn["spawn_session_id"],
               "granted_abilities": spawn.get("granted_abilities", []),
               "confirm_tools": spawn.get("confirm_tools", []),
               "model": spawn.get("spawn_model") or "(your model)",
               "mode": "wait" if wait else "fork", "run_status": result.get("status")}
        if wait:
            out["reply"] = result.get("reply", "")
            if result.get("status") == "error":
                out["error"] = result.get("error")
        else:
            out["note"] = ("Spawn forked — it's queued in the run-queue and will run on its "
                           "own (status goes queued → running → done). You'll be re-woken with "
                           "its result when it finishes; no need to wait or poll. 'queued' just "
                           "means it's waiting its turn under load — that's normal, not a "
                           "failure. Check anytime with list_spawns.")
        return json.dumps(out)

    async def fork_self(task: str, name: str = "", wait: bool = None,
                         check_back_minutes: float = 0) -> str:
        """Fork your own session into a sub-agent that continues as YOU.

        Unlike spawn_agent (fresh clone from scratch), this is a TRUE fork: same
        agent, same prompt slots, same abilities, same conversation prefix —
        everything inherited from THIS session.  The LLM sees an identical prefix
        so KV-cache hit rates are near-100%.

        - ``task`` — what the fork should work on (required).
        - ``name`` — optional label for the fork's session tab.
        - ``wait=True`` blocks for the reply; ``wait=False`` forks and re-wakes
          you when it finishes.  Leave unset to use the agent's configured default.
        - ``check_back_minutes`` — optional follow-up timer.

        Returns the fork_id."""
        if not (task or "").strip():
            return _err("Provide a task for the fork.")
        try:
            fork = await _create_fork(
                user_id=user_id, orchestrator_session_id=session_id,
                orchestrator_agent_id=agent_id or None, task=task or "",
                name=name or "",
            )
        except Exception as e:
            logger.warning("fork_self create failed: %s", e)
            return _err(f"Could not create fork: {e}")

        if fork.get("error"):
            return json.dumps({"status": "error",
                               "error": fork.get("message", "Limit reached."),
                               "detail": fork})

        fork_id = fork["id"]
        if check_back_minutes and float(check_back_minutes) > 0:
            try:
                _spawns_update(fork_id,
                               next_check_at=_iso_minutes_from_now(float(check_back_minutes)),
                               check_note=f"Scheduled check-in on fork '{fork['name']}'.")
            except Exception:
                pass

        first_message = task.strip()
        if wait is None:
            cfg = await _load_orch_config(agent_id)
            wait = cfg.get("default_spawn_mode") == "wait"
        result = await _run_spawn_turn(user_id=user_id, spawn_id=fork_id,
                                        spawn_session_id=fork["spawn_session_id"],
                                        message=first_message, wait=bool(wait))
        out = {"status": "ok", "fork_id": fork_id, "name": fork["name"],
               "fork_session_id": fork["spawn_session_id"],
               "mode": "wait" if wait else "fork",
               "run_status": result.get("status")}
        if wait:
            out["reply"] = result.get("reply", "")
            if result.get("status") == "error":
                out["error"] = result.get("error")
        else:
            out["note"] = ("Fork forked — it's queued in the run-queue and will run on "
                           "its own. You'll be re-woken with its result when it finishes.")
        return json.dumps(out)

    async def message_spawn(spawn_id: str, message: str, wait: bool = False) -> str:
        """Send a follow-up message into a spawn's session to continue the
        conversation. wait=True blocks for the reply; wait=False forks."""
        spawn = await _owned_spawn(spawn_id)
        if not spawn:
            return _err(f"No spawn '{spawn_id}' owned by this session.")
        if not (message or "").strip():
            return _err("message is required.")
        result = await _run_spawn_turn(user_id=user_id, spawn_id=spawn_id,
                                       spawn_session_id=spawn["spawn_session_id"],
                                       message=message, wait=bool(wait))
        out = {"status": "ok", "spawn_id": spawn_id, "mode": "wait" if wait else "fork",
               "run_status": result.get("status")}
        if wait:
            out["reply"] = result.get("reply", "")
            if result.get("status") == "error":
                out["error"] = result.get("error")
        return json.dumps(out)

    async def read_spawn(spawn_id: str, limit: int = 20) -> str:
        """Read a spawn's transcript so far (most recent messages)."""
        spawn = await _owned_spawn(spawn_id)
        if not spawn:
            return _err(f"No spawn '{spawn_id}' owned by this session.")
        from app.db import get_db
        db = get_db()
        try:
            records = await db.fetch_interactions(user_id, spawn["spawn_session_id"])
        except Exception as e:  # noqa: BLE001
            return _err(f"Could not read spawn transcript: {e}")
        try:
            limit = max(1, min(int(limit), _MAX_TRANSCRIPT))
        except (TypeError, ValueError):
            limit = 20
        transcript = []
        for r in records[-limit:]:
            entry = {"role": getattr(r, "role", "") or "",
                     "content": (getattr(r, "content", "") or "")[:1500]}
            tn = getattr(r, "tool_name", None)
            if tn:
                entry["tool"] = tn
            transcript.append(entry)
        return json.dumps({"status": "ok", "spawn_id": spawn_id, "name": spawn.get("name"),
                           "spawn_status": spawn.get("status"), "transcript": transcript})

    async def quote_spawn(spawn_id: str, max_chars: int = 8000) -> str:
        """Return the helper's ACTUAL final answer, verbatim and untouched.

        Use this — never your memory — to report what a spawn produced. It pulls
        the helper's last real reply straight from its saved session and returns
        it word-for-word, plus a content fingerprint and the session id so the
        result is verifiable and can't be silently embellished. If the spawn
        produced nothing (stalled / interrupted / stopped), it says so explicitly
        so you don't invent a result.
        """
        spawn = await _owned_spawn(spawn_id)
        if not spawn:
            return _err(f"No spawn '{spawn_id}' owned by this session.")
        from app.db import get_db
        db = get_db()
        try:
            records = await db.fetch_interactions(user_id, spawn["spawn_session_id"])
        except Exception as e:  # noqa: BLE001
            return _err(f"Could not read spawn output: {e}")

        answers = [(getattr(r, "content", "") or "") for r in records
                   if getattr(r, "role", "") == "assistant"
                   and (getattr(r, "content", "") or "").strip()]
        spawn_status = spawn.get("status")
        if not answers:
            return json.dumps({
                "status": "ok", "spawn_id": spawn_id, "name": spawn.get("name"),
                "spawn_status": spawn_status, "produced_output": False,
                "spawn_session_id": spawn["spawn_session_id"], "final_answer": "",
                "guidance": ("This helper produced NO answer (it likely stalled, was "
                             "interrupted, or was stopped). Do NOT invent, summarize, "
                             "or guess a result for it. Re-run it (spawn again or "
                             "message_spawn) or report this spawn to the human as "
                             "FAILED / no output."),
            })

        final = answers[-1]
        try:
            cap = max(500, min(int(max_chars), 16000))
        except (TypeError, ValueError):
            cap = 8000
        full_len = len(final)
        body = final if full_len <= cap else (
            final[:cap] + f"\n\n[TRUNCATED — {full_len - cap} more characters. "
            "Open spawn_session_id to read the rest verbatim.]")
        fp = hashlib.sha256(final.encode("utf-8")).hexdigest()[:12]
        return json.dumps({
            "status": "ok", "spawn_id": spawn_id, "name": spawn.get("name"),
            "spawn_status": spawn_status, "produced_output": True,
            "spawn_session_id": spawn["spawn_session_id"], "answer_count": len(answers),
            "final_answer": body, "char_count": full_len, "fingerprint": f"sha256:{fp}",
            "guidance": ("This is the helper's ACTUAL words. When you report its result "
                         "to the human, quote from THIS text verbatim and attribute it "
                         "to the helper — do not paraphrase its numbers, counts, names, "
                         "severities, or claims from memory. The human can open "
                         "spawn_session_id to verify what you quote."),
        })

    async def list_spawns() -> str:
        """List the helper agents you've spawned in this session, with status,
        a health read (is it alive / stalled / done?), age, last result summary,
        and any pending follow-up timer."""
        try:
            rows = _spawns_list(session_id)
        except Exception as e:  # noqa: BLE001
            return _err(f"Could not list spawns: {e}")
        now_dt = datetime.now(timezone.utc)
        spawns = []
        for r in rows:
            status = r.get("status")
            # Prefer the heartbeat (refreshed during a run); fall back to updated_at
            # for legacy rows with no heartbeat — same basis the recovery uses.
            since_hb = _age_seconds(now_dt, r.get("heartbeat_at") or r.get("updated_at"))
            stalled, health = _spawn_health(status, since_hb)
            spawns.append({
                "spawn_id": r.get("id"), "name": r.get("name"), "status": status,
                "kind": r.get("kind") or "spawn",
                "health": health, "stalled": stalled,
                "seconds_since_heartbeat": since_hb,
                "seconds_in_state": _age_seconds(now_dt, r.get("updated_at")),
                "age_seconds": _age_seconds(now_dt, r.get("created_at")),
                "task": (r.get("task") or "")[:200],
                "result_summary": (r.get("result_summary") or "")[:400],
                "next_check_at": r.get("next_check_at"),
                "depth": r.get("depth"),
            })
        return json.dumps({
            "status": "ok", "count": len(spawns), "spawns": spawns,
            "guidance": ("`health`/`stalled` tell you a spawn's liveness. Only a 'done' spawn "
                         "has a result (read it with quote_spawn). 'running'/'queued' are "
                         "in-progress — never report them as results or narrate what they're "
                         "'doing'. A 'stalled' spawn is effectively dead — it's being "
                         "auto-recovered or failed; don't wait on it, and don't invent its output."),
        })

    async def stop_spawn(spawn_id: str) -> str:
        """Interrupt a running spawn and mark it stopped (transcript is kept)."""
        spawn = await _owned_spawn(spawn_id)
        if not spawn:
            return _err(f"No spawn '{spawn_id}' owned by this session.")
        from app.db import get_db
        db = get_db()
        try:
            await db.set_interrupt(spawn["spawn_session_id"])
        except Exception as e:  # noqa: BLE001
            logger.debug("set_interrupt on spawn failed: %s", e)
        _spawns_update(spawn_id, status="stopped")
        return json.dumps({"status": "ok", "spawn_id": spawn_id, "spawn_status": "stopped"})

    async def schedule_spawn_check(spawn_id: str, minutes: float, note: str = "") -> str:
        """Set a durable follow-up timer on a spawn. After ``minutes`` you're
        re-woken to check on it — even if still running — with your ``note``.
        Survives a server restart; fires once; replaces any pending timer."""
        spawn = await _owned_spawn(spawn_id)
        if not spawn:
            return _err(f"No spawn '{spawn_id}' owned by this session.")
        try:
            mins = float(minutes)
            if mins <= 0:
                return _err("minutes must be greater than 0.")
        except (TypeError, ValueError):
            return _err("minutes must be a number.")
        _spawns_update(spawn_id, next_check_at=_iso_minutes_from_now(mins),
                       check_note=(note or "").strip() or f"Scheduled check-in on spawn '{spawn['name']}'.")
        return json.dumps({"status": "ok", "spawn_id": spawn_id, "check_in_minutes": mins,
                           "note": "You'll be re-woken to check on this spawn when the timer elapses."})

    async def promote_spawn(spawn_id: str, name: str = "", description: str = "") -> str:
        """Promote a throwaway clone into a PERMANENT agent so it survives the
        cleanup that normally reaps clones with this session. Use it when a clone
        you spawned turned out genuinely reusable and you want to keep its prompt +
        abilities as a real fleet agent. Keeps the clone's directive, granted
        abilities and transcript; flips it out of 'clone' status so it appears in
        the agent roster and is no longer cascade-deleted."""
        spawn = await _owned_spawn(spawn_id)
        if not spawn:
            return _err(f"No spawn '{spawn_id}' owned by this session.")
        spawn_agent_id = spawn.get("spawn_agent_id")
        if not spawn_agent_id:
            return _err("This spawn has no agent to promote.")
        from app.db import get_db
        db = get_db()
        try:
            agent = await db.get_agent_by_id(spawn_agent_id) or {}
            if agent.get("status") != "clone":
                return _err("That spawn's agent is not a clone (already promoted or gone).")
            try:
                meta = json.loads(agent.get("metadata") or "{}")
            except Exception:  # noqa: BLE001
                meta = {}
            meta["kind"] = "promoted_clone"  # keep clone_of for lineage
            meta["owner_user_id"] = user_id
            new_name = (name or "").strip() or (spawn.get("name") or agent.get("name") or "Promoted clone")
            new_desc = (description or "").strip() or "Promoted from a spawned clone."
            conn = _db_conn()
            if conn is None:
                return _err("Backend unavailable.")
            try:
                conn.execute(
                    "UPDATE agents SET status='active', name=?, description=?, metadata=?, updated_at=? "
                    "WHERE id=? AND status='clone'",
                    (new_name, new_desc, json.dumps(meta), _now_iso(), spawn_agent_id),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception as e:  # noqa: BLE001
            logger.warning("promote_spawn failed: %s", e)
            return _err(f"Could not promote spawn: {e}")
        return json.dumps({"status": "ok", "spawn_id": spawn_id, "agent_id": spawn_agent_id,
                           "name": new_name,
                           "note": "Clone promoted to a permanent agent — it now appears in the "
                                   "roster and will NOT be reaped when this session is deleted."})

    async def list_abilities() -> str:
        """List the abilities you can switch on for a clone — the ones YOU have
        enabled (your ceiling). A clone starts with everything OFF; pass the ids
        you need in spawn_agent(abilities=[...]). You can't grant what you lack."""
        from app.db import get_db
        db = get_db()
        try:
            conns = await db.get_agent_connections(agent_id) if agent_id else []
        except Exception:  # noqa: BLE001
            conns = []
        mine = {c.get("connection_type") for c in conns
                if c.get("section") == "ability" and c.get("enabled")}
        try:
            from app.abilities import ui_catalog
            cat = ui_catalog() or {}
        except Exception:  # noqa: BLE001
            cat = {}
        meta_map = cat.get("abilities") or {}
        out = []
        for aid in sorted(mine):
            meta = meta_map.get(aid) or {}
            out.append({
                "id": aid,
                "display_name": meta.get("display_name") or aid,
                "description": (meta.get("description") or meta.get("summary") or "")[:300],
                "tools": meta.get("tools") or [],
            })
        try:
            models = await _user_brain_models(user_id)
        except Exception:  # noqa: BLE001
            models = {"available": [], "high_effort": [], "vision": [], "default": ""}
        try:
            _cfg = await _load_orch_config(agent_id)
            _level = _cfg.get("delegation_level", "balanced")
        except Exception:  # noqa: BLE001
            _level = "balanced"
        _posture = {
            "conservative": "Your delegation posture is CONSERVATIVE — prefer doing the work yourself; spawn only when truly necessary.",
            "balanced": "Your delegation posture is BALANCED — spawn when it clearly helps, otherwise do it yourself.",
            "eager": "Your delegation posture is EAGER — delegation is your DEFAULT. Break the task into units and spawn a helper for EACH one; keep a piece yourself only if it truly can't be handed off. Don't quietly do it all solo. When in doubt, spawn.",
        }.get(_level, "")
        return json.dumps({
            "status": "ok", "count": len(out), "grantable_abilities": out,
            "models": models,
            "delegation_posture": _posture,
            "guidance": ("These are the only abilities you can switch on for a clone — you "
                         "can't grant what you don't have. Pass the ids you need in "
                         "spawn_agent(abilities=[...]); everything is OFF unless you list it. "
                         "Include 'agent_orchestration' ONLY if the clone must spawn its own "
                         "sub-helpers (recursion is opt-in). `models` lists the models you may "
                         "put a helper's BRAIN on via spawn_agent(model=...): `available` = brains "
                         "you can use, `high_effort` = the premium/Eff tier (use for hard reasoning), "
                         "`default` = your cheap baseline. `vision` is NOT a brain — to let a helper "
                         "SEE images, grant it abilities=['image_vision'] (it reads them with "
                         "process_image, powered by the vision model); never pass a vision model in "
                         "model=. Match the brain to the job; omit model to inherit yours."),
        })

    # ── Delegation — hand the whole session off to a different agent ──────────
    # Folded in from the former app/tools/delegation.py. These are gated by this
    # same ability (a normal agent can't auto-discover and hand off to / loop on
    # other agents) and are excluded for pipeline sub-agents by the early return
    # above — exactly the gating the old core loader block applied.
    async def delegate_to_agent(agent_template_id: str, context: str = "") -> str:
        """
        Hand off this session to a different agent.

        Args:
            agent_template_id: The template id of the target agent
                                (e.g. 'admin-agent', 'purchase-agent').
            context: Optional context message to pass to the new agent
                     so it understands why it was called.

        Returns:
            A delegation signal the loop acts on to switch agents.
        """
        from app.db import get_db
        db = get_db()
        try:
            # Verify template exists and is not a pipeline agent
            templates = await db.list_agent_templates(include_admin=True)
            target_tpl = next(
                (t for t in templates if t["id"] == agent_template_id), None
            )
            if not target_tpl:
                return json.dumps({
                    "error": f"Agent template '{agent_template_id}' not found."
                })
            if target_tpl.get("is_pipeline"):
                return json.dumps({
                    "error": f"Cannot delegate to pipeline agent '{agent_template_id}'."
                })

            # Resolve or create the agent instance for this user
            agents = await db.list_agents_for_user(user_id, include_admin=True)
            target_agent = next(
                (a for a in agents if a.get("template_id") == agent_template_id), None
            )
            if not target_agent:
                # Create the agent instance on the fly
                from app.entitlements.resources import (
                    ResourceEntitlementError, agent_resource_lock,
                    enforce_agent_materialization,
                )
                async with agent_resource_lock(user_id):
                    try:
                        await enforce_agent_materialization(
                            db, user_id, template_id=agent_template_id,
                        )
                    except ResourceEntitlementError as exc:
                        return json.dumps({"error": str(exc), **exc.detail()})
                    target_agent = await db.create_custom_agent(
                        user_id=user_id,
                        name=target_tpl.get("name", agent_template_id),
                        description=target_tpl.get("description", ""),
                        template_id=agent_template_id,
                    )

            target_name = target_tpl.get("name") or agent_template_id
            logger.info(
                "Delegation requested: user=%s template=%s agent=%s",
                user_id, agent_template_id, target_agent["id"]
            )
            return _delegation_signal(
                target_template_id=agent_template_id,
                target_agent_id=target_agent["id"],
                target_name=target_name,
                context=context,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("delegate_to_agent error: %s", e)
            return json.dumps({"error": str(e)})

    async def list_delegatable_agents() -> str:
        """
        List agents you can delegate to, with each agent's trigger description.
        Use this to decide which agent to hand off to.
        """
        from app.db import get_db
        db = get_db()
        try:
            templates = await db.list_agent_templates(include_admin=True)
            available = []
            for t in templates:
                if t.get("is_pipeline"):
                    continue
                available.append({
                    "agent_template_id":  t["id"],
                    "name":               t.get("name") or t["id"],
                    "description":        t.get("description", ""),
                    "trigger_description": t.get("trigger_description", ""),
                    "icon":               t.get("icon", ""),
                })
            return json.dumps({"agents": available})
        except Exception as e:  # noqa: BLE001
            logger.warning("list_delegatable_agents error: %s", e)
            return json.dumps({"error": str(e)})

    # ── Task delegation to a saved agent (spawn-family member, NOT a handoff) ──
    # Sibling of spawn_agent, but the worker is one of the user's REAL saved
    # agents (its own persona/abilities/engine — e.g. a Local Claude Code agent),
    # run in its own sub-session and surfaced as a tab like any spawn. Distinct
    # from delegate_to_agent, which hands the WHOLE current session over in place.
    async def list_task_delegatable_agents() -> str:
        """List the saved agents you can hand a discrete TASK to with
        delegate_task_to_agent. These are the user's real agents (each runs as
        itself — its own persona, abilities and runtime engine), NOT ephemeral
        clones. The `engine` field tells you how each one runs; `is_claude_code`
        marks a Local Claude Code agent (answered by the machine's `claude` CLI)."""
        from app.db import get_db
        db = get_db()
        try:
            agents = await db.list_agents_for_user(user_id, include_admin=True)
        except Exception as e:  # noqa: BLE001
            logger.warning("list_task_delegatable_agents error: %s", e)
            return _err(str(e))
        available = []
        for a in agents or []:
            if a.get("is_pipeline") or a.get("status") in ("clone", "clone_trashed"):
                continue
            if str(a.get("id")) == str(agent_id):
                continue  # don't offer the orchestrator itself
            meta = a.get("metadata")
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta or "{}")
                except Exception:  # noqa: BLE001
                    meta = {}
            engine = str((meta or {}).get("engine") or "").strip() if isinstance(meta, dict) else ""
            available.append({
                "agent_id":      a.get("id"),
                "name":          a.get("name") or a.get("id"),
                "description":   a.get("description", ""),
                "engine":        engine or "default",
                "is_claude_code": engine == "claude_code",
                "source":        a.get("source", ""),
            })
        return json.dumps({"agents": available})

    async def delegate_task_to_agent(agent: str, task: str, name: str = "",
                                     wait: bool = None, check_back_minutes: float = 0,
                                     context_inherit: bool = False) -> str:
        """Hand a discrete TASK to one of your saved agents, which runs it as a
        family member you can watch as a tab — NOT a whole-session handoff.

        Unlike spawn_agent (which builds a locked-down clone of you), the target
        here is a REAL agent you own: it runs as itself, with its own persona,
        abilities and runtime engine. Use this to route work to a specialist you
        built — including a Local Claude Code agent, whose task genuinely runs
        through the machine's `claude` CLI. Call list_task_delegatable_agents
        first to see valid targets.

        - ``agent`` — which saved agent to hand the task to (its name or id).
        - ``task`` — the self-contained task for it to carry out.
        - ``name`` — optional short label for the delegation's tab/session.
        - ``wait`` — True = block and return its reply now; False = fork and be
          re-woken when it finishes. Omit to use your configured default.
        - ``check_back_minutes`` — optional follow-up timer to re-check it.
        - ``context_inherit`` — if True, copy your whole conversation so far into
          the delegate's session so it knows the background (one-way copy).

        The delegated agent appears in the chat sub-header next to spawns; manage
        it with the same tools (message_spawn / read_spawn / quote_spawn /
        stop_spawn / schedule_spawn_check). Returns the delegation's spawn_id."""
        if not (task or "").strip():
            return _err("Provide a task to delegate.")
        target = await _resolve_delegate_target(user_id, agent)
        if not target:
            return _err(f"No saved agent matches '{agent}'. Call "
                        "list_task_delegatable_agents to see valid names/ids.")
        if str(target.get("id")) == str(agent_id):
            return _err("That target is you — pick a different agent to delegate to.")
        try:
            deleg = await _create_delegation(
                user_id=user_id, orchestrator_session_id=session_id,
                orchestrator_agent_id=agent_id or None,
                target_agent_id=target["id"], target_name=target.get("name") or "",
                task=task, name=name or "", context_inherit=bool(context_inherit))
        except Exception as e:  # noqa: BLE001
            logger.warning("delegate_task_to_agent create failed: %s", e)
            return _err(f"Could not create delegation: {e}")
        if deleg.get("error"):
            return json.dumps({"status": "error",
                               "error": deleg.get("message") or "Limit reached.",
                               "detail": deleg})

        spawn_id = deleg["id"]
        if check_back_minutes and float(check_back_minutes) > 0:
            try:
                _spawns_update(spawn_id, next_check_at=_iso_minutes_from_now(float(check_back_minutes)),
                               check_note=f"Scheduled check-in on delegation '{deleg['name']}'.")
            except Exception:  # noqa: BLE001
                pass

        first_message = _DELEGATION_PREAMBLE + (task or "").strip()
        if wait is None:
            cfg = await _load_orch_config(agent_id)
            wait = cfg.get("default_spawn_mode") == "wait"
        result = await _run_spawn_turn(user_id=user_id, spawn_id=spawn_id,
                                       spawn_session_id=deleg["spawn_session_id"],
                                       message=first_message, wait=bool(wait))
        out = {"status": "ok", "spawn_id": spawn_id, "name": deleg["name"],
               "delegated_to": target.get("name") or target["id"],
               "agent_id": target["id"], "engine": target.get("engine") or "default",
               "spawn_session_id": deleg["spawn_session_id"],
               "mode": "wait" if wait else "fork", "run_status": result.get("status")}
        if wait:
            out["reply"] = result.get("reply", "")
            if result.get("status") == "error":
                out["error"] = result.get("error")
        else:
            out["note"] = ("Delegation forked — the agent runs on its own and you'll be "
                           "re-woken with its result. Track it with list_spawns; talk to it "
                           "with message_spawn; read its work with read_spawn; get its final "
                           "answer with quote_spawn.")
        return json.dumps(out)

    # ── run_optimizer — start an interactive optimizer session ────────────────
    # Folded in from the former core block in app/tools/loader.py. Same gating:
    # the ability must be enabled (build_tools only runs then) and optimizer
    # sub-agents never get it (recursion guard — their user ids start "opt_",
    # and pipeline templates already returned {} above).
    async def run_optimizer(feedback: str = "", skill_name: str = "", criteria: str = "") -> str:
        """Start an interactive optimizer session. User chats with the Planner agent."""
        from app.db import get_db as _get_db
        # Safety check: if we're already in an optimizer session, don't create another
        _dbc = _get_db()._get_conn()
        _recent = _dbc.execute(
            "SELECT metadata FROM sessions WHERE user_id=? AND id LIKE 'optimizer-%' ORDER BY created_at DESC LIMIT 1",
            (user_id,)
        ).fetchone()
        _dbc.close()
        if _recent and _recent[0]:
            _meta = json.loads(_recent[0])
            if _meta.get('opt_role'):
                # We're being called from within an optimizer session — skip
                return json.dumps({"status": "skipped", "message": "Already in an optimizer session."})
        import httpx
        try:
            async with httpx.AsyncClient(timeout=15.0) as hclient:
                resp = await hclient.post(
                    f"http://127.0.0.1:{os.environ.get('PORT', '8080')}/admin/settings/optimizer/run",
                    params={"user_id": user_id, "session_id": "", "feedback": feedback},
                )
                result = resp.json()
            if result.get("status") == "session_created":
                return json.dumps({
                    "status": "completed",
                    "optimizer_session_id": result["optimizer_session_id"],
                    "message": "Optimization session ready. Go to the optimizer session to talk to the Planner."
                })
            return json.dumps({"status": "error", "message": result.get("message", "Unknown error")})
        except Exception:  # noqa: BLE001
            from app.admin.settings import load_provider_for_user
            await load_provider_for_user(user_id)
            opt_sid = f"optimizer-{user_id[:8]}-{str(uuid.uuid4())[:8]}"
            db_conn = _get_db()._get_conn()
            db_conn.execute(
                "INSERT OR IGNORE INTO sessions (id,user_id,title,metadata,created_at,updated_at) VALUES (?,?,?,?,datetime('now'),datetime('now'))",
                (opt_sid, user_id, f"Optimizer - {opt_sid[:12]}", json.dumps({"opt_role": "planner"}))
            )
            db_conn.execute(
                "INSERT INTO interactions (id,session_id,role,content,source,channel,created_at) VALUES (?,?,'user',?,'optimizer:trigger','optimizer',datetime('now'))",
                (str(uuid.uuid4()), opt_sid, f"I need help optimizing this session. Feedback: {feedback or 'General optimization'}.")
            )
            db_conn.commit()
            db_conn.close()
            return json.dumps({"status": "session_created", "optimizer_session_id": opt_sid,
                               "message": "Optimization session created. Go to the optimizer session to talk to the Planner."})

    out = {
        "spawn_agent": spawn_agent,
        "fork_self": fork_self,
        "message_spawn": message_spawn,
        "read_spawn": read_spawn,
        "quote_spawn": quote_spawn,
        "list_spawns": list_spawns,
        "stop_spawn": stop_spawn,
        "schedule_spawn_check": schedule_spawn_check,
        "list_abilities": list_abilities,
        "promote_spawn": promote_spawn,
        "delegate_to_agent": delegate_to_agent,
        "list_delegatable_agents": list_delegatable_agents,
        "delegate_task_to_agent": delegate_task_to_agent,
        "list_task_delegatable_agents": list_task_delegatable_agents,
    }
    if not user_id.startswith("opt_"):
        out["run_optimizer"] = run_optimizer
    return out


# Launching autonomous work confirms in Ask/Plan mode: spawn_agent / delegate /
# run_optimizer each start ANOTHER agent that runs on its own and spends tokens, so
# the user is asked before money + automation is committed. The lighter helper-
# management tools (message / read / quote / stop / promote / schedule-check /
# lists) stay frictionless. NOTE this gate is belt-and-braces: a HELPER itself
# still runs under its own session's tool gating, and a clone starts with NO
# destructive permissions — so the real side effects were already guarded.
# (Auto execution mode runs everything without a pause.)
DESTRUCTIVE: set = {"spawn_agent", "fork_self", "delegate_to_agent", "delegate_task_to_agent",
                    "run_optimizer"}

TOOL_SCHEMAS = {
    "spawn_agent": {
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "Self-contained helper task."},
            "name": {"type": "string", "description": "Optional helper label."},
            "system_prompt": {"type": "string", "description": "Optional clone directive."},
            "abilities": {"type": "array", "items": {"type": "string"}, "description": "Abilities to grant; defaults to none and is clamped to yours."},
            "allow_destructive": {"type": "array", "items": {"type": "string"}, "description": "Confirmed tools to allow within your permission ceiling."},
            "output_contract": {"type": "string", "description": "Optional required result shape or length."},
            "from_agent": {"type": "string", "description": "Optional template identity reference."},
            "wait": {"type": "boolean", "description": "True waits; false forks; omit for the agent default."},
            "check_back_minutes": {"type": "number", "description": "Optional follow-up timer in minutes."},
            "context_inherit": {"type": "boolean", "description": "Copy current conversation into the clone; default false."},
            "model": {"type": "string", "description": "Optional enabled tool-capable helper model; omit to inherit."},
        },
        "required": ["task"],
    },
    "fork_self": {
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "What the forked copy of yourself should work on."},
            "name": {"type": "string", "description": "Optional short label for the fork's session tab."},
            "wait": {"type": "boolean", "description": "True = block and return the fork's reply now. False = fork and be re-woken when it finishes. Leave unset to use the agent's configured default (see Agent Orchestration settings)."},
            "check_back_minutes": {"type": "number", "description": "Optional: also set a follow-up timer (minutes) to re-check the fork even if still running."},
        },
        "required": ["task"],
    },
    "list_abilities": {"type": "object", "properties": {}, "required": []},
    "promote_spawn": {
        "type": "object",
        "properties": {
            "spawn_id": {"type": "string", "description": "The spawn whose clone agent to keep permanently."},
            "name": {"type": "string", "description": "Optional name for the promoted agent (defaults to the spawn's name)."},
            "description": {"type": "string", "description": "Optional description for the promoted agent."},
        },
        "required": ["spawn_id"],
    },
    "message_spawn": {
        "type": "object",
        "properties": {
            "spawn_id": {"type": "string", "description": "The spawn to message (from spawn_agent / list_spawns)."},
            "message": {"type": "string", "description": "The message to send into the spawn's session."},
            "wait": {"type": "boolean", "description": "True = block for the reply. False = fork and be re-woken. Leave unset to use the agent's configured default."},
        },
        "required": ["spawn_id", "message"],
    },
    "read_spawn": {
        "type": "object",
        "properties": {
            "spawn_id": {"type": "string", "description": "The spawn whose transcript to read."},
            "limit": {"type": "integer", "description": "How many recent messages to return (default 20, max 40)."},
        },
        "required": ["spawn_id"],
    },
    "quote_spawn": {
        "type": "object",
        "properties": {
            "spawn_id": {"type": "string", "description": "The spawn whose actual final answer you want, verbatim."},
            "max_chars": {"type": "integer", "description": "Max characters of the answer to return (default 8000, max 16000; longer answers are marked truncated)."},
        },
        "required": ["spawn_id"],
    },
    "list_spawns": {"type": "object", "properties": {}, "required": []},
    "stop_spawn": {
        "type": "object",
        "properties": {"spawn_id": {"type": "string", "description": "The spawn to interrupt and mark stopped."}},
        "required": ["spawn_id"],
    },
    "schedule_spawn_check": {
        "type": "object",
        "properties": {
            "spawn_id": {"type": "string", "description": "The spawn to set a follow-up timer on."},
            "minutes": {"type": "number", "description": "Minutes from now to be re-woken to check on it."},
            "note": {"type": "string", "description": "Optional reminder to yourself, delivered when the timer fires."},
        },
        "required": ["spawn_id", "minutes"],
    },
    # ── Delegation (folded in from app/tools/delegation.py) — schemas are
    #    verbatim those the former core loader block applied, so behavior is
    #    identical. Neither is destructive (kept out of DESTRUCTIVE above). ──
    "delegate_to_agent": {
        "type": "object",
        "properties": {
            "agent_template_id": {"type": "string", "description": "Template ID of the agent to delegate to (e.g. 'admin-agent')."},
            "context": {"type": "string", "description": "Context or reason for the delegation — passed to the new agent."},
        },
        "required": ["agent_template_id"],
    },
    "list_delegatable_agents": {
        "type": "object",
        "properties": {},
        "required": [],
    },
    # ── Task delegation to a saved agent (spawn-family member; NOT a handoff) ──
    "list_task_delegatable_agents": {
        "type": "object",
        "properties": {},
        "required": [],
    },
    "delegate_task_to_agent": {
        "type": "object",
        "properties": {
            "agent": {"type": "string", "description": "Which of your saved agents to hand the task to — its name or id (see list_task_delegatable_agents). Can be a Local Claude Code agent; its task then runs through the machine's `claude` CLI."},
            "task": {"type": "string", "description": "The self-contained task for that agent to carry out."},
            "name": {"type": "string", "description": "Optional short label for the delegation's tab/session."},
            "wait": {"type": "boolean", "description": "True = block and return its reply now. False = fork and be re-woken when it finishes. Leave unset to use the agent's configured default."},
            "check_back_minutes": {"type": "number", "description": "Optional: set a follow-up timer (minutes) to re-check the delegated agent even if still running."},
            "context_inherit": {"type": "boolean", "description": "If True, copy your entire conversation so far into the delegate's session so it knows the background. One-way copy (its later turns don't flow back). Default False."},
        },
        "required": ["agent", "task"],
    },
    # ── run_optimizer (folded in from the former core loader block) — schema is
    #    verbatim what the loader applied, so behavior is identical. ──
    "run_optimizer": {
        "type": "object",
        "properties": {
            "skill_name": {"type": "string", "description": "Optional: specific skill to optimize (e.g. 'send_email'). If blank, analyzes all skills."},
            "feedback": {"type": "string", "description": "Optional: what to improve. E.g. 'make it use the API instead of scraping' or 'response was too verbose'"},
            "criteria": {"type": "string", "description": "Optional: which metric to optimize. 'turns' (fewer back-and-forths), 'tokens' (cheaper), or 'time' (faster). If blank, balances all."},
        },
        "required": [],
    },
}
