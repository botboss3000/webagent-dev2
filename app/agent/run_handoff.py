"""Cheap durable handoff capsules between supervised agent turns.

Capsules are machine-facing state, not another model conversation.  The
Closer enriches the row it owns after its existing one-shot completes, while
the run lifecycle may write a deterministic partial capsule for recoverable
errors.  All writes use the shared turn-id generation fence; user/global stops
never create or update a capsule.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

_SILENT_CAUSES = {"user_stop", "global_stop", "replaced"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS run_handoff_capsules (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    user_id TEXT,
    agent_id TEXT,
    status TEXT NOT NULL,
    stop_cause TEXT,
    execution_mode TEXT NOT NULL DEFAULT 'ask',
    mode_history_json TEXT NOT NULL DEFAULT '[]',
    objective TEXT NOT NULL DEFAULT '',
    completed_json TEXT NOT NULL DEFAULT '[]',
    open_requirements_json TEXT NOT NULL DEFAULT '[]',
    decisions_json TEXT NOT NULL DEFAULT '[]',
    changed_paths_json TEXT NOT NULL DEFAULT '[]',
    verification_json TEXT NOT NULL DEFAULT '[]',
    blockers_json TEXT NOT NULL DEFAULT '[]',
    next_action TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'lifecycle',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(session_id, turn_id)
);
CREATE INDEX IF NOT EXISTS idx_run_handoff_session_updated
    ON run_handoff_capsules(session_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_run_handoff_task_updated
    ON run_handoff_capsules(task_id, updated_at DESC);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _conn(db: Any):
    getter = getattr(db, "_get_conn", None)
    if not callable(getter):
        raise RuntimeError("Run handoff requires a server-side SQL backend")
    return getter()


def ensure_schema(db: Any) -> bool:
    try:
        conn = _conn(db)
        try:
            conn.executescript(_SCHEMA)
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.debug("run handoff: schema unavailable: %s", exc)
        return False


def _loads(value: Any, default: Any) -> Any:
    try:
        return json.loads(value) if value else default
    except Exception:
        return default


def _strings(values: Optional[Iterable[Any]], *, limit: int = 100) -> List[str]:
    if isinstance(values, str):
        values = [values]
    if not values:
        return []
    result: List[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text[:2000])
        if len(result) >= limit:
            break
    return result


_JSON_COLUMNS = {
    "mode_history_json": "mode_history",
    "completed_json": "completed",
    "open_requirements_json": "open_requirements",
    "decisions_json": "decisions",
    "changed_paths_json": "changed_paths",
    "verification_json": "verification",
    "blockers_json": "blockers",
}


def _row_dict(row: Any) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    try:
        out = dict(row)
    except Exception:
        return None
    for column, name in _JSON_COLUMNS.items():
        out[name] = _loads(out.get(column), [])
    return out


def recent_capsules(
    db: Any, session_id: str, *, limit: int = 3,
    exclude_turn_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return compact recent capsules for one Starter prompt."""
    if not session_id or not ensure_schema(db):
        return []
    conn = _conn(db)
    try:
        sql = "SELECT * FROM run_handoff_capsules WHERE session_id=?"
        args: List[Any] = [session_id]
        if exclude_turn_id:
            sql += " AND turn_id != ?"
            args.append(exclude_turn_id)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        args.append(max(1, min(int(limit), 10)))
        return [parsed for row in conn.execute(sql, args).fetchall()
                if (parsed := _row_dict(row)) is not None]
    finally:
        conn.close()


def capsule_for_turn(db: Any, session_id: str, turn_id: str) -> Optional[Dict[str, Any]]:
    if not session_id or not turn_id or not ensure_schema(db):
        return None
    conn = _conn(db)
    try:
        return _row_dict(conn.execute(
            "SELECT * FROM run_handoff_capsules WHERE session_id=? AND turn_id=?",
            (session_id, turn_id),
        ).fetchone())
    finally:
        conn.close()


def _scout_context(db: Any, turn_id: str) -> Dict[str, Any]:
    try:
        from app.agent.run_scout import artifact_for_turn
        row = artifact_for_turn(db, turn_id) or {}
    except Exception:
        row = {}
    artifact = row.get("artifact") if isinstance(row.get("artifact"), dict) else {}
    plan = artifact.get("plan") if isinstance(artifact.get("plan"), list) else []
    completed = [str(item.get("text") or "").strip() for item in plan
                 if isinstance(item, dict) and item.get("status") == "complete"]
    opened = [str(item.get("text") or "").strip() for item in plan
              if isinstance(item, dict) and item.get("status") != "complete"]
    return {
        "task_id": str(row.get("id") or turn_id),
        "user_id": row.get("user_id"),
        "agent_id": row.get("agent_id"),
        "execution_mode": str(row.get("execution_mode") or "ask"),
        "objective": str(artifact.get("objective") or row.get("combined_request") or "").strip(),
        "completed": _strings(completed),
        "open_requirements": _strings(opened),
    }


async def _mode_history(db: Any, session_id: str, fallback: str) -> List[str]:
    try:
        values = await db.get_session_execution_mode_history(session_id)
    except Exception:
        values = []
    result = _strings(values, limit=20)
    if fallback and fallback not in result:
        result.append(fallback)
    return result


def _tool_evidence(db: Any, session_id: str, turn_id: str) -> Dict[str, List[Any]]:
    """Collect sanitized receipts only where the interaction store has them."""
    changed: List[str] = []
    verification: List[Dict[str, Any]] = []
    try:
        conn = _conn(db)
        try:
            rows = conn.execute(
                "SELECT tool_name, metadata FROM interactions "
                "WHERE session_id=? AND turn_id=? AND role='tool' ORDER BY created_at",
                (session_id, turn_id),
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return {"changed_paths": [], "verification": []}
    try:
        from app.agent.contracts import is_verify_tool
    except Exception:
        is_verify_tool = lambda _name, _args: False  # type: ignore[assignment]
    for row in rows:
        meta = _loads(row["metadata"], {})
        if not isinstance(meta, dict):
            continue
        changed.extend(_strings(meta.get("changed_paths"), limit=100))
        name = str(row["tool_name"] or "")
        args = meta.get("input_params") if isinstance(meta.get("input_params"), dict) else {}
        if is_verify_tool(name, args):
            verification.append({
                "tool": name,
                "success": bool(meta.get("success")),
                "duration_ms": int(meta.get("duration_ms") or 0),
            })
    return {
        "changed_paths": _strings(changed, limit=200),
        "verification": verification[-30:],
    }


async def persist_capsule(
    db: Any,
    *,
    session_id: str,
    turn_id: str,
    run_id: Optional[str] = None,
    status: str,
    stop_cause: Optional[str] = None,
    user_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    execution_mode: Optional[str] = None,
    objective: Optional[str] = None,
    completed: Optional[Iterable[Any]] = None,
    open_requirements: Optional[Iterable[Any]] = None,
    decisions: Optional[Iterable[Any]] = None,
    changed_paths: Optional[Iterable[Any]] = None,
    verification: Optional[List[Dict[str, Any]]] = None,
    blockers: Optional[Iterable[Any]] = None,
    next_action: Optional[str] = None,
    summary: Optional[str] = None,
    source: str = "lifecycle",
) -> Optional[Dict[str, Any]]:
    """Generation-fenced upsert for one run's machine handoff."""
    cause = str(stop_cause or "").strip().lower()
    if not session_id or not turn_id or cause in _SILENT_CAUSES:
        return None
    from app.agent.run_fence import side_effects_allowed
    if not await side_effects_allowed(db, session_id, expected_turn_id=turn_id):
        return None
    if not ensure_schema(db):
        return None

    from app.agent.task_state import current_state
    task = current_state(db, session_id, turn_id)
    if task:
        status = task["status"]
        objective = "\n".join(r["text"] for r in task["requirements"])
        completed = [r["id"] for r in task["requirements"] if r["status"] == "satisfied"]
        open_requirements = [r["text"] for r in task["requirements"] if r["status"] != "satisfied"]
        decisions = task["checkpoint"]["decisions"]
        next_action = task["checkpoint"]["next_action"]
        blockers = task["checkpoint"]["blockers"]
        verification = task["evidence"]
    scout = _scout_context(db, turn_id)
    mode = str(execution_mode or scout.get("execution_mode") or "ask")
    modes = await _mode_history(db, session_id, mode)
    evidence = _tool_evidence(db, session_id, turn_id)
    completed_items = _strings(completed) if completed is not None else scout["completed"]
    open_items = (_strings(open_requirements) if open_requirements is not None
                  else scout["open_requirements"])
    changed = (_strings(changed_paths, limit=200) if changed_paths is not None
               else evidence["changed_paths"])
    checks = verification if verification is not None else evidence["verification"]
    now = _now()
    existing = capsule_for_turn(db, session_id, turn_id)
    capsule_id = str((existing or {}).get("id") or f"handoff-{uuid.uuid4()}")
    task_id = str((existing or {}).get("task_id") or scout["task_id"] or turn_id)
    values = (
        capsule_id, session_id, task_id, str(run_id or (existing or {}).get("run_id") or turn_id),
        turn_id, user_id or scout.get("user_id"), agent_id or scout.get("agent_id"),
        status or "unknown", stop_cause, mode, json.dumps(modes, ensure_ascii=False),
        str(objective if objective is not None else scout.get("objective") or "")[:4000],
        json.dumps(completed_items, ensure_ascii=False),
        json.dumps(open_items, ensure_ascii=False),
        json.dumps(_strings(decisions), ensure_ascii=False),
        json.dumps(changed, ensure_ascii=False),
        json.dumps(checks or [], ensure_ascii=False, default=str),
        json.dumps(_strings(blockers), ensure_ascii=False),
        str(next_action or "")[:2000], str(summary or "")[:12000], source,
        (existing or {}).get("created_at") or now, now,
    )
    if not await side_effects_allowed(db, session_id, expected_turn_id=turn_id):
        return None
    conn = _conn(db)
    try:
        conn.execute(
            "INSERT INTO run_handoff_capsules "
            "(id,session_id,task_id,run_id,turn_id,user_id,agent_id,status,stop_cause,"
            "execution_mode,mode_history_json,objective,completed_json,"
            "open_requirements_json,decisions_json,changed_paths_json,verification_json,"
            "blockers_json,next_action,summary,source,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(session_id,turn_id) DO UPDATE SET "
            "run_id=excluded.run_id,user_id=excluded.user_id,agent_id=excluded.agent_id,"
            "status=excluded.status,stop_cause=excluded.stop_cause,"
            "execution_mode=excluded.execution_mode,mode_history_json=excluded.mode_history_json,"
            "objective=excluded.objective,completed_json=excluded.completed_json,"
            "open_requirements_json=excluded.open_requirements_json,"
            "decisions_json=excluded.decisions_json,changed_paths_json=excluded.changed_paths_json,"
            "verification_json=excluded.verification_json,blockers_json=excluded.blockers_json,"
            "next_action=excluded.next_action,summary=excluded.summary,source=excluded.source,"
            "updated_at=excluded.updated_at",
            values,
        )
        conn.commit()
    finally:
        conn.close()
    return capsule_for_turn(db, session_id, turn_id)


async def persist_run_outcome(
    db: Any, turn_id: str, status: str, stop_cause: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Persist a no-LLM partial/terminal capsule where lifecycle permits."""
    cause = str(stop_cause or "").strip().lower()
    if cause in _SILENT_CAUSES:
        return None
    scout = _scout_context(db, turn_id)
    try:
        from app.agent.run_scout import artifact_for_turn
        row = artifact_for_turn(db, turn_id) or {}
    except Exception:
        row = {}
    session_id = str(row.get("session_id") or "")
    if not session_id:
        return None
    blockers = [cause] if status != "complete" and cause else []
    next_action = ("Resume from the durable checkpoint." if status != "complete" else "")
    return await persist_capsule(
        db, session_id=session_id, turn_id=turn_id, run_id=turn_id,
        status=status, stop_cause=stop_cause,
        user_id=row.get("user_id"), agent_id=row.get("agent_id"),
        execution_mode=scout.get("execution_mode"), blockers=blockers,
        next_action=next_action, source="lifecycle",
    )

