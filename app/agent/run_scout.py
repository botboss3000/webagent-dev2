"""Durable, tool-free intake companion for one logical user starter.

The Run Scout starts beside the main agent turn. It does not execute tools,
take authoritative action, or produce user-facing output. Its phase-1 job is to
turn one or more user messages into a durable internal run-intelligence artifact:
objective, constraints, questions, success criteria, plan, expected outputs, and
a title candidate. The main agent and Closer remain authoritative.

Rapid follow-up messages belong to the same logical starter when they replace
an active/recoverable run.  Appending a message increments ``revision`` and a
completion may update the row only when its captured revision is still current;
therefore a late result from an older Scout can never overwrite newer intent.

The table is deliberately independent of ``session_runs``.  ``session_runs`` is
one mutable liveness row per session, while a Scout artifact is durable task
history.  The implementation currently uses the server-side SQL connection,
matching the Manager/Closer persistence paths; backends without ``_get_conn``
fail off without affecting chat.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

SOURCE = "system:run_scout"
_LLM_TIMEOUT_SECONDS = 60.0
_MAX_TOKENS = 3072
_SWEEP_INTERVAL_SECONDS = 60
_SWEEP_STARTUP_DELAY_SECONDS = 8
_SWEEP_MIN_AGE_SECONDS = 10
_SWEEP_MAX_PER_TICK = 10
_SWEEP_MAX_ATTEMPTS = 3
_TASKS: Dict[Tuple[str, int], asyncio.Task] = {}
_TASK_TURNS: Dict[Tuple[str, int], Tuple[str, str]] = {}
_sweep_task: Optional[asyncio.Task] = None


def _kill_switch_engaged() -> bool:
    try:
        from app.kill_switch import is_engaged
        return bool(is_engaged())
    except Exception:
        return False

_SCHEMA = """
CREATE TABLE IF NOT EXISTS run_scout_artifacts (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    agent_id TEXT,
    root_interaction_id TEXT NOT NULL,
    active_turn_id TEXT NOT NULL,
    execution_mode TEXT NOT NULL DEFAULT 'ask',
    revision INTEGER NOT NULL DEFAULT 1,
    analysis_status TEXT NOT NULL DEFAULT 'queued',
    run_status TEXT NOT NULL DEFAULT 'running',
    stop_cause TEXT,
    source_messages_json TEXT NOT NULL DEFAULT '[]',
    combined_request TEXT NOT NULL DEFAULT '',
    artifact_json TEXT,
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_run_scout_session_updated
    ON run_scout_artifacts(session_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_run_scout_active_turn
    ON run_scout_artifacts(active_turn_id);
"""

_PROMPT = """You are the Run Scout, a tool-free intake companion running in
parallel with a separate main agent.  Analyze the user's combined starter.  Do
not claim that work has been performed and do not invent research evidence.

Return STRICT JSON only with this shape:
{
  "objective": "one concrete description of the desired outcome",
  "task_type": "answer|plan|research|diagnose|change|mixed",
  "constraints": ["explicit constraint"],
  "assumptions": ["reasonable but unverified assumption"],
  "questions": ["material unanswered question"],
  "success_criteria": [{"id": "C1", "text": "observable completion condition"}],
  "plan": [{"id": "P1", "text": "bounded step", "status": "pending"}],
  "expected_outputs": ["deliverable"],
  "risks": ["specific risk"],
  "title_candidate": "short session title",
  "relationship": "continue|new|branch|uncertain",
  "linked_prior_task_id": "prior task id or empty",
  "linked_capsule_id": "prior capsule id or empty",
  "relationship_confidence": 0.0,
  "relationship_reason": "short evidence-based reason"
}

Rules:
- Treat later messages as steering or additions to earlier messages unless they
  clearly replace them.
- Preserve explicit user constraints and requested sequencing.
- Success criteria must be auditable and plans must describe intended work,
  never completed work.
- This artifact is internal context for the separate main agent and is never
  rendered as a user-facing response.
- Keep the title concise and specific.
- Compare the starter to PRIOR RUN CAPSULES when supplied. Use continue only
  for the same objective, branch for a related but divergent objective, new
  for an unrelated task, and uncertain when the relationship is ambiguous.
"""



def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _conn(db: Any):
    getter = getattr(db, "_get_conn", None)
    if not callable(getter):
        raise RuntimeError("Run Scout requires a server-side SQL backend")
    return getter()


def ensure_schema(db: Any) -> bool:
    """Create the append-safe Scout store lazily.  Never raises to chat."""
    try:
        conn = _conn(db)
        try:
            conn.executescript(_SCHEMA)
            # Append-safe columns for Manager Loop Starter linkage. Existing
            # Scout databases predate handoff capsules, so migrate lazily.
            cols = {row[1] for row in conn.execute(
                "PRAGMA table_info(run_scout_artifacts)"
            ).fetchall()}
            if "prior_capsules_json" not in cols:
                conn.execute(
                    "ALTER TABLE run_scout_artifacts ADD COLUMN "
                    "prior_capsules_json TEXT NOT NULL DEFAULT '[]'"
                )
            if "starter_config_json" not in cols:
                conn.execute(
                    "ALTER TABLE run_scout_artifacts ADD COLUMN "
                    "starter_config_json TEXT NOT NULL DEFAULT '{}'"
                )
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.debug("run scout: schema unavailable: %s", exc)
        return False


def _loads(value: Any, default: Any) -> Any:
    if not value:
        return default
    try:
        parsed = json.loads(value)
        return parsed
    except Exception:
        return default


def _row_dict(row: Any) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    try:
        out = dict(row)
    except Exception:
        return None
    out["source_messages"] = _loads(out.get("source_messages_json"), [])
    out["artifact"] = _loads(out.get("artifact_json"), None)
    out["prior_capsules"] = _loads(out.get("prior_capsules_json"), [])
    out["starter_config"] = _loads(out.get("starter_config_json"), {})
    return out


def _combined(messages: List[Dict[str, str]]) -> str:
    if len(messages) == 1:
        return str(messages[0].get("content") or "").strip()
    blocks: List[str] = []
    for index, item in enumerate(messages, 1):
        label = "Opening message" if index == 1 else f"Follow-up {index - 1}"
        blocks.append(f"[{label}]\n{str(item.get('content') or '').strip()}")
    return "\n\n".join(blocks).strip()


def _latest_row(db: Any, session_id: str) -> Optional[Dict[str, Any]]:
    if not ensure_schema(db):
        return None
    conn = _conn(db)
    try:
        row = conn.execute(
            "SELECT * FROM run_scout_artifacts WHERE session_id=? "
            "ORDER BY updated_at DESC, created_at DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        return _row_dict(row)
    finally:
        conn.close()


def artifact_for_turn(db: Any, turn_id: str) -> Optional[Dict[str, Any]]:
    if not turn_id or not ensure_schema(db):
        return None
    conn = _conn(db)
    try:
        row = conn.execute(
            "SELECT * FROM run_scout_artifacts WHERE active_turn_id=? "
            "ORDER BY revision DESC LIMIT 1",
            (turn_id,),
        ).fetchone()
        if row is None:
            # A replaced turn remains in source_messages after active_turn_id
            # advances to its follow-up.  This fallback finds that membership.
            rows = conn.execute(
                "SELECT * FROM run_scout_artifacts ORDER BY updated_at DESC"
            ).fetchall()
            for candidate in rows:
                parsed = _row_dict(candidate)
                if any(m.get("interaction_id") == turn_id
                       for m in (parsed or {}).get("source_messages", [])):
                    return parsed
            return None
        return _row_dict(row)
    finally:
        conn.close()


def latest_artifact(db: Any, session_id: str) -> Optional[Dict[str, Any]]:
    return _latest_row(db, session_id)


def _starter_config(
    agent_rec: Optional[Dict[str, Any]], execution_mode: str,
) -> Dict[str, Any]:
    """Resolve canonical settings, retaining legacy Scout opt-in semantics."""
    try:
        from app.agent.manager_config import resolve_manager_loop
        cfg = resolve_manager_loop(agent_rec, execution_mode)
        starter = dict(cfg.get("starter") or {})
    except Exception:
        starter = {}
    raw_meta: Any = (agent_rec or {}).get("metadata", {})
    if isinstance(raw_meta, str):
        raw_meta = _loads(raw_meta, {})
    raw_manager = raw_meta.get("manager") if isinstance(raw_meta, dict) else None
    explicit = isinstance(raw_manager, dict) and "starter" in raw_manager
    starter["explicit"] = explicit
    # Before Manager Loop settings existed, Run Scout followed the app-level
    # run_manager ability. Preserve that for agents not yet configured.
    if not explicit:
        starter["enabled"] = _legacy_scout_enabled()
    return starter


async def begin_turn(
    db: Any,
    *,
    session_id: str,
    user_id: str,
    agent_id: Optional[str],
    turn_id: str,
    message: str,
    execution_mode: str,
    replaced: bool,
    agent_rec: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Create a starter or append this message to its recoverable predecessor."""
    if not session_id or not user_id or not turn_id or not ensure_schema(db):
        return None
    now = _now()
    starter_cfg = _starter_config(agent_rec, execution_mode)
    prior_capsules: List[Dict[str, Any]] = []
    if starter_cfg.get("inherit_prior_summary"):
        try:
            from app.agent.run_handoff import recent_capsules
            prior_capsules = recent_capsules(
                db, session_id, limit=3, exclude_turn_id=turn_id,
            )
        except Exception as exc:
            logger.debug("run scout: prior handoff lookup failed: %s", exc)
    conn = _conn(db)
    try:
        conn.execute("BEGIN IMMEDIATE")
        duplicate = conn.execute(
            "SELECT * FROM run_scout_artifacts WHERE active_turn_id=? LIMIT 1",
            (turn_id,),
        ).fetchone()
        if duplicate is not None:
            conn.commit()
            return _row_dict(duplicate)

        latest = conn.execute(
            "SELECT * FROM run_scout_artifacts WHERE session_id=? "
            "ORDER BY updated_at DESC, created_at DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        prior = _row_dict(latest)
        recoverable_prior = bool(prior and prior.get("run_status") in {
            "running", "replaced", "interrupted", "error", "resuming",
        })
        if prior and (replaced or recoverable_prior):
            messages = list(prior.get("source_messages") or [])
            if not any(m.get("interaction_id") == turn_id for m in messages):
                messages.append({
                    "interaction_id": turn_id,
                    "content": str(message or ""),
                    "received_at": now,
                })
            revision = int(prior.get("revision") or 0) + 1
            combined = _combined(messages)
            conn.execute(
                "UPDATE run_scout_artifacts SET agent_id=?, active_turn_id=?, "
                "execution_mode=?, revision=?, analysis_status='queued', "
                "run_status='running', stop_cause=NULL, source_messages_json=?, "
                "combined_request=?, artifact_json=NULL, error=NULL, updated_at=?, "
                "completed_at=NULL, prior_capsules_json=?, starter_config_json=? "
                "WHERE id=?",
                (agent_id, turn_id, execution_mode or "ask", revision,
                 json.dumps(messages, ensure_ascii=False), combined, now,
                 json.dumps(prior_capsules, ensure_ascii=False),
                 json.dumps(starter_cfg, ensure_ascii=False), prior["id"]),
            )
            artifact_id = str(prior["id"])
        else:
            artifact_id = f"scout-{uuid.uuid4()}"
            messages = [{
                "interaction_id": turn_id,
                "content": str(message or ""),
                "received_at": now,
            }]
            combined = _combined(messages)
            conn.execute(
                "INSERT INTO run_scout_artifacts "
                "(id, session_id, user_id, agent_id, root_interaction_id, "
                "active_turn_id, execution_mode, revision, analysis_status, "
                "run_status, source_messages_json, combined_request, created_at, updated_at, "
                "prior_capsules_json, starter_config_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 1, 'queued', 'running', ?, ?, ?, ?, ?, ?)",
                (artifact_id, session_id, user_id, agent_id, turn_id, turn_id,
                 execution_mode or "ask", json.dumps(messages, ensure_ascii=False),
                 combined, now, now, json.dumps(prior_capsules, ensure_ascii=False),
                 json.dumps(starter_cfg, ensure_ascii=False)),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return artifact_for_turn(db, turn_id)


def _legacy_scout_enabled() -> bool:
    try:
        from app.abilities import app_function_enabled
        return bool(app_function_enabled("run_manager"))
    except Exception:
        return False


def _scout_enabled(row: Optional[Dict[str, Any]] = None) -> bool:
    cfg = (row or {}).get("starter_config")
    if isinstance(cfg, dict) and "enabled" in cfg:
        return bool(cfg.get("enabled"))
    return _legacy_scout_enabled()


def _row_scout_enabled(row: Dict[str, Any]) -> bool:
    cfg = row.get("starter_config") if isinstance(row.get("starter_config"), dict) else {}
    if cfg.get("explicit"):
        return bool(cfg.get("enabled"))
    return _scout_enabled()


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    value = str(text or "").strip()
    if value.startswith("```"):
        value = value.split("\n", 1)[-1] if "\n" in value else value
        value = value.rsplit("```", 1)[0].strip()
    try:
        parsed = json.loads(value)
    except Exception:
        start, end = value.find("{"), value.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            parsed = json.loads(value[start:end + 1])
        except Exception:
            return None
    return parsed if isinstance(parsed, dict) else None


def _strings(value: Any, limit: int = 30) -> List[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip()[:1000] for item in value[:limit]
            if str(item).strip()]


def _steps(value: Any, prefix: str, *, plan: bool = False) -> List[Dict[str, str]]:
    if not isinstance(value, list):
        return []
    out: List[Dict[str, str]] = []
    for index, item in enumerate(value[:50], 1):
        if isinstance(item, dict):
            text = str(item.get("text") or "").strip()
            ident = str(item.get("id") or f"{prefix}{index}").strip()[:24]
        else:
            text = str(item).strip()
            ident = f"{prefix}{index}"
        if not text:
            continue
        row = {"id": ident, "text": text[:1200]}
        if plan:
            row["status"] = "pending"
        out.append(row)
    return out


def _normalize_artifact(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    objective = str(raw.get("objective") or "").strip()
    if not objective:
        return None
    task_type = str(raw.get("task_type") or "mixed").strip().lower()
    if task_type not in {"answer", "plan", "research", "diagnose", "change", "mixed"}:
        task_type = "mixed"
    relationship = str(raw.get("relationship") or "uncertain").strip().lower()
    if relationship not in {"continue", "new", "branch", "uncertain"}:
        relationship = "uncertain"
    try:
        confidence = max(0.0, min(1.0, float(raw.get("relationship_confidence") or 0.0)))
    except (TypeError, ValueError):
        confidence = 0.0
    return {
        "version": 1,
        "objective": objective[:4000],
        "task_type": task_type,
        "constraints": _strings(raw.get("constraints")),
        "assumptions": _strings(raw.get("assumptions")),
        "questions": _strings(raw.get("questions")),
        "success_criteria": _steps(raw.get("success_criteria"), "C"),
        "plan": _steps(raw.get("plan"), "P", plan=True),
        "expected_outputs": _strings(raw.get("expected_outputs")),
        "risks": _strings(raw.get("risks")),
        "title_candidate": str(raw.get("title_candidate") or "").strip()[:120],
        "relationship": relationship,
        "linked_prior_task_id": str(raw.get("linked_prior_task_id") or "").strip()[:200],
        "linked_capsule_id": str(raw.get("linked_capsule_id") or "").strip()[:200],
        "relationship_confidence": confidence,
        "relationship_reason": str(raw.get("relationship_reason") or "").strip()[:1000],
    }


async def _call_model(row: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Any, str, str]:
    # Managed-build agents use the same durable Scout row and CAS fencing, but
    # execute the intake contract in a locked tool-free orchestration worker.
    # Any transport/schema failure falls through to the existing bounded direct
    # call so optional orchestration cannot strand intake.
    try:
        from app.db import get_db
        from app.agent.subagent_contracts import ContractSupervisor
        _db = get_db()
        _agent = await _db.get_agent_by_id(str(row.get("agent_id") or ""))
        _supervisor = ContractSupervisor(
            db=_db, user_id=str(row.get("user_id") or ""),
            session_id=str(row.get("session_id") or ""),
            agent_id=str(row.get("agent_id") or ""), agent_rec=_agent,
            turn_id=str(row.get("active_turn_id") or row.get("root_interaction_id") or ""),
            generation=str(row.get("active_turn_id") or ""),
            execution_mode=str(row.get("execution_mode") or "ask"),
        )
        if await _supervisor.available():
            _contract_result = await _supervisor.run_scout(
                combined_request=str(row.get("combined_request") or ""),
                prior_capsules=list(row.get("prior_capsules") or [])[:3],
                revision=int(row.get("revision") or 1),
                execution_mode=str(row.get("execution_mode") or "ask"),
            )
            if (_contract_result.get("decision") == "pass"
                    and isinstance(_contract_result.get("payload"), dict)):
                _artifact = _normalize_artifact(_contract_result["payload"])
                if _artifact:
                    return _artifact, None, "contract-worker", "orchestration"
            # Contract mode is the selected Scout implementation. A timeout,
            # cancellation, malformed result, or infrastructure failure is the
            # visible advisory/inconclusive outcome; never launch the legacy
            # direct Scout afterward, because that would outlive the terminal
            # check and let a timed-out decision influence the parent run.
            return None, None, "contract-worker", "orchestration"
    except Exception as _contract_error:  # noqa: BLE001
        logger.debug("run scout contract fallback: %s", _contract_error)

    from app.agent.manager import _resolve_llm

    model, provider, client = await _resolve_llm(str(row.get("user_id") or ""))
    if client is None or not model:
        return None, None, model or "", provider or ""
    cfg = row.get("starter_config") if isinstance(row.get("starter_config"), dict) else {}
    custom_prompt = str(cfg.get("prompt") or "").strip()
    capsule_rows: List[Dict[str, Any]] = []
    for capsule in (row.get("prior_capsules") or [])[:3]:
        if not isinstance(capsule, dict):
            continue
        compact = {
            key: capsule.get(key) for key in (
                "id", "task_id", "turn_id", "status", "stop_cause",
                "execution_mode", "mode_history", "objective", "decisions",
                "blockers", "next_action", "summary", "updated_at",
            ) if capsule.get(key) not in (None, "", [])
        }
        if cfg.get("seed_plan"):
            compact["prior_next_action"] = capsule.get("next_action") or ""
        if cfg.get("seed_checklist"):
            compact["prior_completed"] = capsule.get("completed") or []
            compact["prior_open_requirements"] = capsule.get("open_requirements") or []
        capsule_rows.append(compact)
    system_prompt = _PROMPT
    if custom_prompt:
        system_prompt += "\n\nAGENT ADMINISTRATOR STARTER INSTRUCTIONS:\n" + custom_prompt
    prior_block = json.dumps(capsule_rows, ensure_ascii=False, default=str)
    from app.agent.provider_completion import complete_text
    response = await asyncio.wait_for(
        complete_text(
            client,
            model=model,
            messages=[{
                "role": "system",
                "content": system_prompt,
            }, {
                "role": "user",
                "content": (
                    "COMBINED STARTER:\n"
                    + str(row.get("combined_request") or "(empty)")
                    + "\n\nPRIOR RUN CAPSULES:\n"
                    + (prior_block if capsule_rows else "[]")
                    + "\n\nReturn the strict JSON Run Scout artifact now."
                ),
            }],
            temperature=0.1,
            max_tokens=_MAX_TOKENS,
            timeout=_LLM_TIMEOUT_SECONDS,
        ),
        timeout=_LLM_TIMEOUT_SECONDS,
    )
    text = response.choices[0].message.content if response.choices else ""
    parsed = _extract_json(text or "")
    return (_normalize_artifact(parsed) if parsed else None,
            response, model, provider)


def _mark_analysis_status(
    db: Any, artifact_id: str, revision: int, status: str, *, error: str = "",
) -> bool:
    if not ensure_schema(db):
        return False
    conn = _conn(db)
    try:
        attempt_sql = ", attempts=attempts+1" if status == "analyzing" else ""
        cur = conn.execute(
            "UPDATE run_scout_artifacts SET analysis_status=?, error=?, "
            f"updated_at=?{attempt_sql} WHERE id=? AND revision=?",
            (status, error[:1000] or None, _now(), artifact_id, revision),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def persist_analysis(
    db: Any, artifact_id: str, revision: int, artifact: Dict[str, Any],
) -> bool:
    """CAS write: an older Scout completion loses after any follow-up message."""
    normalized = _normalize_artifact(artifact) if "version" not in artifact else artifact
    if not normalized or not ensure_schema(db):
        return False
    now = _now()
    conn = _conn(db)
    try:
        cur = conn.execute(
            "UPDATE run_scout_artifacts SET artifact_json=?, "
            "analysis_status='complete', error=NULL, updated_at=?, completed_at=? "
            "WHERE id=? AND revision=?",
            (json.dumps(normalized, ensure_ascii=False), now, now,
             artifact_id, int(revision)),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


async def _analyze(db: Any, artifact_id: str, revision: int) -> None:
    if _kill_switch_engaged():
        return
    if not _mark_analysis_status(db, artifact_id, revision, "analyzing"):
        return
    row = _read_exact(db, artifact_id, revision)
    if not row:
        return
    if not _row_scout_enabled(row):
        _mark_analysis_status(db, artifact_id, revision, "disabled")
        return
    started = time.monotonic()
    try:
        artifact, response, model, provider = await _call_model(row)
        current = _read_exact(db, artifact_id, revision)
        if (_kill_switch_engaged() or not current
                or str(current.get("stop_cause") or "") in {"user_stop", "replaced", "global_stop"}
                or str(current.get("run_status") or "") in {"stopped", "replaced"}):
            logger.debug("run scout: fenced stale result %s:%s", artifact_id, revision)
            return
        if not artifact:
            _mark_analysis_status(db, artifact_id, revision, "failed",
                                  error="empty or invalid Scout completion")
            return
        if not persist_analysis(db, artifact_id, revision, artifact):
            logger.debug("run scout: stale revision %s:%s discarded", artifact_id, revision)
            return
        try:
            usage = getattr(response, "usage", None)
            if usage:
                from plugins.billing.usage import record_background_usage
                await record_background_usage(
                    model=model, provider=provider,
                    input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                    output_tokens=getattr(usage, "completion_tokens", 0) or 0,
                    label="run_scout", session_id=row.get("session_id"),
                    user_id=row.get("user_id"), agent_id=row.get("agent_id"),
                )
        except Exception:
            pass
        logger.debug("run scout: completed %s r%d in %.2fs",
                     artifact_id, revision, time.monotonic() - started)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        _mark_analysis_status(db, artifact_id, revision, "failed", error=str(exc))
        logger.debug("run scout: analysis failed: %s", exc)


def _read_exact(db: Any, artifact_id: str, revision: int) -> Optional[Dict[str, Any]]:
    if not ensure_schema(db):
        return None
    conn = _conn(db)
    try:
        row = conn.execute(
            "SELECT * FROM run_scout_artifacts WHERE id=? AND revision=?",
            (artifact_id, int(revision)),
        ).fetchone()
        return _row_dict(row)
    finally:
        conn.close()


def launch_analysis(db: Any, row: Optional[Dict[str, Any]]) -> Optional[asyncio.Task]:
    if not row or _kill_switch_engaged():
        return None
    if not _row_scout_enabled(row):
        _mark_analysis_status(db, str(row["id"]), int(row["revision"]), "disabled")
        return None
    key = (str(row["id"]), int(row["revision"]))
    existing = _TASKS.get(key)
    if existing and not existing.done():
        return existing
    task = asyncio.create_task(
        _analyze(db, key[0], key[1]),
        name=f"run-scout:{str(row.get('session_id') or '')[:12]}:r{key[1]}",
    )
    _TASKS[key] = task
    _TASK_TURNS[key] = (str(row.get("session_id") or ""),
                        str(row.get("active_turn_id") or ""))

    def _done(_task: asyncio.Task) -> None:
        _TASKS.pop(key, None)
        _TASK_TURNS.pop(key, None)
        try:
            _task.exception()
        except (asyncio.CancelledError, Exception):
            pass

    task.add_done_callback(_done)
    return task


async def stop_turn(db: Any, session_id: str, turn_id: str, cause: str) -> None:
    """Cancel only the Scout revision owned by the interrupted main turn."""
    for key, owner in list(_TASK_TURNS.items()):
        if owner == (session_id, turn_id):
            task = _TASKS.get(key)
            if task and not task.done():
                task.cancel()
    row = artifact_for_turn(db, turn_id)
    if not row or row.get("active_turn_id") != turn_id:
        return
    run_status = "stopped" if cause == "user_stop" else (
        "replaced" if cause == "replaced" else "interrupted"
    )
    analysis_status = row.get("analysis_status")
    if analysis_status in {"queued", "analyzing", "failed"}:
        analysis_status = "stopped" if cause == "user_stop" else "interrupted"
    conn = _conn(db)
    try:
        conn.execute(
            "UPDATE run_scout_artifacts SET run_status=?, analysis_status=?, "
            "stop_cause=?, updated_at=? WHERE id=? AND revision=? AND active_turn_id=?",
            (run_status, analysis_status, cause, _now(), row["id"],
             int(row["revision"]), turn_id),
        )
        conn.commit()
    finally:
        conn.close()


async def stop_all(db: Any, cause: str = "user_stop") -> int:
    """Cancel every live Scout and durably prevent its revision recovering."""
    targets = [task for task in list(_TASKS.values()) if not task.done()]
    for task in targets:
        task.cancel()
    if targets:
        await asyncio.gather(*targets, return_exceptions=True)
    if ensure_schema(db):
        conn = _conn(db)
        try:
            cur = conn.execute(
                "UPDATE run_scout_artifacts SET run_status='stopped', "
                "analysis_status=CASE WHEN analysis_status='complete' THEN analysis_status "
                "ELSE 'stopped' END, stop_cause=?, updated_at=? "
                "WHERE run_status NOT IN ('complete','stopped','replaced')",
                (cause, _now()),
            )
            conn.commit()
            return max(len(targets), int(cur.rowcount or 0))
        finally:
            conn.close()
    return len(targets)


async def mark_run_outcome(
    db: Any, turn_id: str, status: str, stop_cause: Optional[str] = None,
) -> None:
    row = artifact_for_turn(db, turn_id)
    if not row or row.get("active_turn_id") != turn_id:
        return
    run_status = {
        "complete": "complete", "error": "error", "interrupted": "interrupted",
    }.get(status, status or "interrupted")
    conn = _conn(db)
    try:
        conn.execute(
            "UPDATE run_scout_artifacts SET run_status=?, stop_cause=?, updated_at=? "
            "WHERE id=? AND revision=? AND active_turn_id=?",
            (run_status, stop_cause, _now(), row["id"], int(row["revision"]), turn_id),
        )
        conn.commit()
    finally:
        conn.close()
    try:
        from app.agent.run_handoff import persist_run_outcome
        await persist_run_outcome(db, turn_id, status, stop_cause)
    except Exception as exc:
        logger.debug("run scout: handoff outcome write failed: %s", exc)


def seeded_context_for_turn(db: Any, turn_id: str) -> Dict[str, Any]:
    """Return the Starter fields explicitly allowed to seed run context."""
    row = artifact_for_turn(db, turn_id) or {}
    artifact = row.get("artifact") if isinstance(row.get("artifact"), dict) else {}
    cfg = row.get("starter_config") if isinstance(row.get("starter_config"), dict) else {}
    return {
        "ready": row.get("analysis_status") == "complete",
        "revision": int(row.get("revision") or 0),
        "relationship": artifact.get("relationship") or "uncertain",
        "linked_prior_task_id": artifact.get("linked_prior_task_id") or "",
        "linked_capsule_id": artifact.get("linked_capsule_id") or "",
        "relationship_confidence": artifact.get("relationship_confidence") or 0.0,
        "relationship_reason": artifact.get("relationship_reason") or "",
        "objective": artifact.get("objective") or "",
        "plan": list(artifact.get("plan") or []) if cfg.get("seed_plan", True) else [],
        "checklist": (list(artifact.get("success_criteria") or [])
                      if cfg.get("seed_checklist", True) else []),
    }


async def await_write_ready(
    db: Any, turn_id: str, *, timeout_seconds: float = _LLM_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    """Wait only when configured for a Starter result before the first write.

    Terminal Scout failure/disable states fail open so a broken optional
    second opinion cannot deadlock the main run. User/global stops remain
    fenced by the main run lifecycle independently.
    """
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    while True:
        row = artifact_for_turn(db, turn_id) or {}
        cfg = row.get("starter_config") if isinstance(row.get("starter_config"), dict) else {}
        status = str(row.get("analysis_status") or "missing")
        if not cfg.get("wait_before_write") or status in {
            "complete", "failed", "disabled", "stopped", "interrupted", "missing",
        }:
            return seeded_context_for_turn(db, turn_id)
        if time.monotonic() >= deadline:
            result = seeded_context_for_turn(db, turn_id)
            result["timed_out"] = True
            return result
        await asyncio.sleep(0.05)


async def revive_turn(db: Any, turn_id: str) -> Optional[Dict[str, Any]]:
    """Reuse an existing starter after crash/freeze/zombie/server restart."""
    if _kill_switch_engaged():
        return None
    row = artifact_for_turn(db, turn_id)
    if not row:
        return None
    conn = _conn(db)
    try:
        conn.execute(
            "UPDATE run_scout_artifacts SET run_status='resuming', stop_cause=NULL, "
            "updated_at=? WHERE id=? AND revision=?",
            (_now(), row["id"], int(row["revision"])),
        )
        conn.commit()
    finally:
        conn.close()
    row = _read_exact(db, str(row["id"]), int(row["revision"]))
    if row and row.get("analysis_status") != "complete":
        launch_analysis(db, row)
    return row


def _parse_time(value: Any) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value or ""))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except Exception:
        return None


def _recovery_candidates(db: Any, limit: int = _SWEEP_MAX_PER_TICK) -> List[Dict[str, Any]]:
    """Find orphaned/inconclusive Scout analyses without reviving user stops."""
    if not ensure_schema(db):
        return []
    conn = _conn(db)
    try:
        rows = conn.execute(
            "SELECT * FROM run_scout_artifacts "
            "WHERE analysis_status IN ('queued', 'analyzing', 'interrupted', 'failed') "
            "AND run_status != 'stopped' AND attempts < ? "
            "ORDER BY updated_at ASC LIMIT ?",
            (_SWEEP_MAX_ATTEMPTS, max(1, int(limit))),
        ).fetchall()
        now = datetime.now(timezone.utc)
        out: List[Dict[str, Any]] = []
        for raw in rows:
            row = _row_dict(raw)
            updated = _parse_time((row or {}).get("updated_at"))
            if updated and (now - updated).total_seconds() < _SWEEP_MIN_AGE_SECONDS:
                continue
            out.append(row)
        return out
    finally:
        conn.close()


async def _sweep_once(db: Any = None) -> int:
    """Re-launch bounded orphan analyses; live task keys deduplicate races."""
    if _kill_switch_engaged():
        return 0
    if db is None:
        try:
            from app.db import get_db
            db = get_db()
        except Exception:
            return 0
    attempted = 0
    for row in _recovery_candidates(db):
        if not _row_scout_enabled(row):
            continue
        key = (str(row.get("id") or ""), int(row.get("revision") or 0))
        live = _TASKS.get(key)
        if live and not live.done():
            continue
        if launch_analysis(db, row) is not None:
            attempted += 1
    return attempted


async def _sweep_loop() -> None:
    await asyncio.sleep(_SWEEP_STARTUP_DELAY_SECONDS)
    while True:
        try:
            count = await _sweep_once()
            if count:
                logger.info("Run Scout recovery sweep revived %d analysis task(s)", count)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("Run Scout recovery sweep failed: %s", exc)
        await asyncio.sleep(_SWEEP_INTERVAL_SECONDS)


async def start_sweep() -> None:
    """Start the leader-owned orphan-Scout recovery sweep idempotently."""
    global _sweep_task
    if _sweep_task is not None and not _sweep_task.done():
        return
    _sweep_task = asyncio.create_task(_sweep_loop(), name="run_scout_sweep")
    logger.info("Run Scout recovery sweep started")


async def stop_sweep() -> None:
    global _sweep_task
    task, _sweep_task = _sweep_task, None
    if task is None:
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


def reset_task_registry_for_tests() -> None:
    global _sweep_task
    for task in list(_TASKS.values()):
        if not task.done():
            task.cancel()
    _TASKS.clear()
    _TASK_TURNS.clear()
    if _sweep_task is not None and not _sweep_task.done():
        _sweep_task.cancel()
    _sweep_task = None
