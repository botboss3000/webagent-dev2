"""
Admin endpoints for optimizer configuration and manual runs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Dict, Optional

from fastapi import APIRouter, Header, Query
from pydantic import BaseModel

from app.optimizer.config import load_config, save_config
from app.optimizer.runner import run_optimizer_async

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/settings", tags=["admin"])


class OptimizerConfigModel(BaseModel):
    mode: Optional[str] = None
    intensity: Optional[int] = None
    user_feedback: Optional[str] = None
    sessions: Optional[Dict[str, bool]] = None
    schedule: Optional[Dict[str, Any]] = None
    models: Optional[Dict[str, Optional[str]]] = None
    target_metrics: Optional[list] = None
    max_iterations: Optional[int] = None
    trials: Optional[Dict[str, Any]] = None
    app_wide: Optional[Dict[str, Any]] = None
    per_user: Optional[Dict[str, Any]] = None
    notifications: Optional[Dict[str, Any]] = None


@router.get("/optimizer")
async def get_optimizer_config():
    """Get current optimizer configuration."""
    cfg = load_config()
    return cfg


@router.post("/optimizer")
async def update_optimizer_config(body: OptimizerConfigModel, user_id: str = Header(default="test-user-1", alias="x-user-id")):
    """Update optimizer configuration. Merges with existing values."""
    current = load_config()
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    current.update(updates)
    if body.max_iterations is not None:
        try:
            from app.db import get_db
            db = get_db()
            conn = getattr(db, "_get_conn")()
            agent = conn.execute("SELECT id, metadata FROM agents WHERE user_id=? LIMIT 1", (user_id,)).fetchone()
            if agent:
                meta = json.loads(agent[1] or "{}")
                meta["optimizer_max_iterations"] = body.max_iterations
                conn.execute("UPDATE agents SET metadata=? WHERE id=?", (json.dumps(meta), agent[0]))
                conn.commit()
            conn.close()
        except Exception as e:
            logger.error("Failed to update agent max_iterations: %s", e)
    save_config(current)
    return {"status": "saved", "config": current}


# ──────────────────────────────────────────────────────────────────────────
# Optimizer Runs dashboard
#
# A "run" is one Planner → Worker → Closer pipeline. The runs table on the
# admin Optimizer page mirrors the Sessions / Automations tables: one flat,
# sortable, filterable row per run. Almost everything shown is DERIVED at read
# time from data that already persists in local.db — the run's Planner session
# id is the anchor:
#   • target session  ← planner session metadata ("target_session")
#   • closer session  ← sessions whose metadata.source_optimizer_session = planner sid
#   • cost / tokens   ← usage_events keyed by each session_id (real, measured)
#   • stage           ← status + whether a closer exists + proposals_deployed
# The ONLY thing that can't be derived (trial DBs are deleted after the trial)
# is the optimized trial metric — that is captured onto the run by
# run_worker_trials (see app/tools/optimizer_tools.py). The performance delta
# shown in the table is optimized_trial − baseline_target_session, both real.
# ──────────────────────────────────────────────────────────────────────────

# Extra columns captured by the worker-trial stage. Added lazily so an existing
# optimizer_runs table (created by the runner) gains them without a migration.
_RUN_EXTRA_COLS = [
    ("optimized_tokens", "INTEGER"),
    ("optimized_ms", "INTEGER"),
    ("trials_count", "INTEGER"),
]


def _ensure_runs_columns(conn) -> None:
    """Make sure optimizer_runs exists and carries the worker-trial columns."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS optimizer_runs "
        "(id TEXT PRIMARY KEY, status TEXT DEFAULT 'running', started_at TEXT, "
        "completed_at TEXT, skills_analyzed INTEGER DEFAULT 0, "
        "proposals_generated INTEGER DEFAULT 0, proposals_deployed INTEGER DEFAULT 0, "
        "errors TEXT, summary TEXT, config_snapshot TEXT, session_id TEXT)"
    )
    have = {r[1] for r in conn.execute("PRAGMA table_info(optimizer_runs)").fetchall()}
    for col, decl in _RUN_EXTRA_COLS:
        if col not in have:
            try:
                conn.execute(f"ALTER TABLE optimizer_runs ADD COLUMN {col} {decl}")
            except Exception:
                pass


def _usage_for_session(conn, sid: str):
    """Real measured (tokens, cost_usd) for one session from usage_events."""
    if not sid:
        return 0, 0.0
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(input_tokens + output_tokens), 0), "
            "COALESCE(SUM(cost_usd), 0) FROM usage_events WHERE session_id = ?",
            (sid,),
        ).fetchone()
        return int(row[0] or 0), float(row[1] or 0.0)
    except Exception:
        return 0, 0.0


def _session_meta(conn, sid: str) -> dict:
    try:
        row = conn.execute("SELECT metadata FROM sessions WHERE id = ?", (sid,)).fetchone()
        if row and row[0]:
            return json.loads(row[0])
    except Exception:
        pass
    return {}


def _find_closer_session(conn, planner_sid: str):
    """The closer-* session spawned from this planner session (id, title) or None."""
    try:
        rows = conn.execute(
            "SELECT id, title, metadata FROM sessions WHERE id LIKE 'closer-%'"
        ).fetchall()
        for r in rows:
            try:
                meta = json.loads(r[2] or "{}")
            except Exception:
                meta = {}
            if meta.get("source_optimizer_session") == planner_sid:
                return {"session_id": r[0], "title": r[1]}
    except Exception:
        pass
    return None


def _derive_stage(status: str, has_closer: bool, deployed: int, optimized_tokens) -> str:
    """optimizer → worker → closer → deployed, overlaid on the run status."""
    if deployed and deployed > 0:
        return "deployed"
    if has_closer:
        return "closer"
    if optimized_tokens:
        return "worker"
    return "optimizer"


def _parse_ts_ms(ts: str):
    """Best-effort parse of a stored timestamp into epoch-ms (UTC). None on miss."""
    if not ts:
        return None
    from datetime import datetime, timezone
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(ts, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
        except Exception:
            continue
    return None


def _enrich_run(conn, r: dict) -> dict:
    """Turn a raw optimizer_runs row into the rich object the dashboard renders."""
    planner_sid = r.get("session_id") or ""
    pmeta = _session_meta(conn, planner_sid)
    target_sid = pmeta.get("target_session") or ""

    # Target (the real session being optimized) — title + owning agent.
    target_title, target_agent = "", ""
    if target_sid:
        try:
            trow = conn.execute(
                "SELECT title, agent_id FROM sessions WHERE id = ?", (target_sid,)
            ).fetchone()
            if trow:
                target_title = trow[0] or ""
                if trow[1]:
                    arow = conn.execute(
                        "SELECT name FROM agents WHERE id = ?", (trow[1],)
                    ).fetchone()
                    target_agent = (arow[0] if arow else "") or ""
        except Exception:
            pass

    closer = _find_closer_session(conn, planner_sid)

    # Real spend on this run's agent conversations (optimizer + closer).
    p_tok, p_cost = _usage_for_session(conn, planner_sid)
    c_tok, c_cost = _usage_for_session(conn, closer["session_id"]) if closer else (0, 0.0)
    run_cost = round(p_cost + c_cost, 6)

    # Baseline = the target session's measured footprint (what we optimised away from).
    base_tok, base_cost = _usage_for_session(conn, target_sid)

    optimized_tokens = r.get("optimized_tokens")
    optimized_ms = r.get("optimized_ms")

    # Token delta: optimized trial vs baseline. Negative = fewer tokens = better.
    tokens_delta = None
    if optimized_tokens is not None and base_tok:
        tokens_delta = int(optimized_tokens) - int(base_tok)

    # Projected cost of the optimised agent (same per-token price as the baseline run).
    optimized_cost, cost_delta = None, None
    if optimized_tokens is not None and base_tok and base_cost:
        per_tok = base_cost / base_tok
        optimized_cost = round(int(optimized_tokens) * per_tok, 6)
        cost_delta = round(optimized_cost - base_cost, 6)

    started_ms = _parse_ts_ms(r.get("started_at"))
    completed_ms = _parse_ts_ms(r.get("completed_at"))
    duration_ms = (completed_ms - started_ms) if (started_ms and completed_ms) else None

    return {
        "id": r.get("id"),
        "status": r.get("status") or "running",
        "stage": _derive_stage(
            r.get("status") or "", bool(closer),
            r.get("proposals_deployed") or 0, optimized_tokens,
        ),
        "summary": r.get("summary") or "",
        "started_at": r.get("started_at"),
        "completed_at": r.get("completed_at"),
        "duration_ms": duration_ms,
        # Pipeline sessions
        "planner_session_id": planner_sid,
        "closer_session_id": closer["session_id"] if closer else None,
        "target_session": target_sid,
        "target_title": target_title,
        "target_agent": target_agent,
        # Counts
        "proposals_generated": r.get("proposals_generated") or 0,
        "proposals_deployed": r.get("proposals_deployed") or 0,
        "trials_count": r.get("trials_count") or 0,
        # Performance metrics (current value + delta vs baseline)
        "tokens": optimized_tokens if optimized_tokens is not None else base_tok,
        "tokens_delta": tokens_delta,
        "baseline_tokens": base_tok,
        "cost": optimized_cost if optimized_cost is not None else run_cost,
        "cost_delta": cost_delta,
        "run_cost": run_cost,
        "duration_trial_ms": optimized_ms,
    }


@router.get("/optimizer/runs")
async def get_optimizer_runs(limit: int = Query(50, ge=1, le=200)):
    """Recent optimizer runs, enriched for the dashboard table."""
    try:
        from app.db import get_db
        db = get_db()
        raw = getattr(db, '_get_conn', None)
        if raw:
            conn = raw()
            try:
                _ensure_runs_columns(conn)
                rows = conn.execute(
                    "SELECT * FROM optimizer_runs ORDER BY started_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
                return [_enrich_run(conn, dict(r)) for r in rows]
            finally:
                conn.close()
    except Exception as e:
        logger.error("Failed to fetch optimizer runs: %s", e)
    return []


@router.get("/optimizer/runs/{run_id}")
async def get_optimizer_run_detail(run_id: str):
    """One run plus its per-stage breakdown (for the expandable row).

    The Optimizer is a real, openable chat session. Worker trials are
    ephemeral (their temp DBs are deleted), so that stage is a measured summary.
    """
    try:
        from app.db import get_db
        db = get_db()
        raw = getattr(db, '_get_conn', None)
        if not raw:
            return {"error": "no db"}
        conn = raw()
        try:
            _ensure_runs_columns(conn)
            row = conn.execute(
                "SELECT * FROM optimizer_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if not row:
                return {"error": "not found"}
            run = _enrich_run(conn, dict(row))

            stages = []
            # ── Optimizer ──
            p_tok, p_cost = _usage_for_session(conn, run["planner_session_id"])
            stages.append({
                "key": "optimizer", "label": "Optimizer",
                "session_id": run["planner_session_id"],
                "openable": True,
                "tokens": p_tok, "cost": round(p_cost, 6),
                "detail": "Analysed the session, proposed changes, ran trials, judged results, and deployed",
            })
            # ── Worker trials (measured summary, not openable) ──
            if run.get("trials_count") or run.get("tokens"):
                stages.append({
                    "key": "worker", "label": "Worker trials",
                    "session_id": None, "openable": False,
                    "tokens": run.get("tokens"),
                    "trials_count": run.get("trials_count") or 0,
                    "duration_ms": run.get("duration_trial_ms"),
                    "detail": f"{run.get('trials_count') or 0} simulated trial(s)",
                })
            if run["closer_session_id"]:
                stages.append({
                    "key": "deploy", "label": "Deployed",
                    "session_id": run["closer_session_id"],
                    "openable": True,
                    "detail": "Changes deployed",
                })

            return {"run": run, "stages": stages}
        finally:
            conn.close()
    except Exception as e:
        logger.error("Failed to fetch optimizer run %s: %s", run_id, e)
        return {"error": str(e)}


@router.post("/optimizer/run")
async def trigger_optimizer_run(
    user_id: str = Query("_system"),
    session_id: str = Query("manual"),
    feedback: str = Query(""),
):
    """Start an interactive optimizer session. Creates chat session with Optimizer agent."""
    try:
        from app.admin.settings import load_provider_for_user
        import uuid, json, sqlite3
        from app.db import get_db
        
        await load_provider_for_user(user_id)
        
        # Create optimizer session ID
        opt_sid = f"optimizer-{user_id[:8]}-{str(uuid.uuid4())[:8]}"
        
        db = get_db()
        raw = getattr(db, '_get_conn', None)
        conn = raw()
        
        # Ensure session exists with optimizer role
        conn.execute("INSERT OR IGNORE INTO sessions (id,user_id,title,metadata,created_at,updated_at) VALUES (?,?,?,?,datetime('now'),datetime('now'))",
                     (opt_sid, user_id, f"Optimizer - {opt_sid[:12]}", '{"opt_role": "optimizer"}'))
        
        # Get prefilter data (session stats) and inject as first message
        from app.optimizer.prefilter import prefilter
        # If session_id is empty, find the most recent session for this user
        if not session_id:
            from app.db import get_db as _get_db
            _conn = _get_db()._get_conn()
            _row = _conn.execute(
                "SELECT id FROM sessions WHERE user_id=? AND id NOT LIKE 'optimizer-%' ORDER BY created_at DESC LIMIT 1",
                (user_id,)
            ).fetchone()
            if _row:
                session_id = _row[0]
            _conn.close()
        pf = await prefilter(user_id, session_id)
        turns = pf.get("turns", 1)
        tokens = pf.get("tokens", 100)
        
        # Build enriched init message so Optimizer has everything without discovery tools
        parts = []
        parts.append(f"Session stats: {turns} assistant turns, ~{tokens} tokens.")
        parts.append(f"User feedback: {feedback or '(none)'}")
        
        original_message = pf.get("original_message", "")
        if original_message:
            parts.append(f"Original user message: {original_message}")
        
        tool_defs = pf.get("tool_definitions", [])
        if tool_defs:
            parts.append("\nTools used in session:")
            for td in tool_defs:
                parts.append(f"  - {td['name']}: {td.get('description', 'N/A')[:200]}")
        
        context_docs = pf.get("context_docs", [])
        if context_docs:
            parts.append("\nYour active context documents:")
            for cd in context_docs:
                parts.append(f"  - [{cd['type']}] {cd['title']}: {cd.get('excerpt', '')[:200]}")
        
        full_transcript = pf.get("full_transcript", [])
        parts.append("\nFull transcript:")
        for line in full_transcript:
            parts.append(line)
        
        init_content = "\n".join(parts)
        conn.execute(
            "INSERT INTO interactions (id,session_id,role,content,source,channel,created_at) VALUES (?,?,'system',?,'optimizer:init','optimizer',datetime('now'))",
            (str(uuid.uuid4()), opt_sid, init_content)
        )
        conn.commit()
        conn.close()

        # Fire-and-forget: auto-trigger the Optimizer to respond
        async def _auto_trigger_optimizer():
            try:
                import httpx
                async with httpx.AsyncClient(timeout=60.0) as hclient:
                    await hclient.post(
                        f"http://127.0.0.1:{os.environ.get('PORT', '8080')}/api/v1/chat",
                        json={
                            "message": f"I need help optimizing this session. User feedback: {feedback or 'No specific feedback.'}",
                            "session_id": opt_sid,
                            "user_id": user_id
                        },
                    )
            except Exception as trigger_e:
                logger.warning("Auto-trigger Optimizer first response failed (non-fatal): %s", trigger_e)
        asyncio.create_task(_auto_trigger_optimizer())

        return {"status": "session_created", "optimizer_session_id": opt_sid}
    except Exception as e:
        logger.error("Manual optimizer run failed: %s", e)
        return {"status": "error", "message": str(e)}
