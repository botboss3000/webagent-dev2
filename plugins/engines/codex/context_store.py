"""Durable task identity and context-snapshot telemetry for Codex wrapper runs.

The normal chat schema deliberately has sessions and turns but no persistent
task layer.  Wrapper-mode Codex needs stable task boundaries so a later history
rebuild cannot silently move old tool calls between context pools.  These
engine-owned tables are created lazily, keeping the capability removable with
the Codex engine plugin.
"""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


_SCHEMA = """
CREATE TABLE IF NOT EXISTS codex_context_tasks (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    root_interaction_id TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    objective TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'running',
    checkpoint_json TEXT,
    revision INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_codex_context_task_root
    ON codex_context_tasks(session_id, root_interaction_id);
CREATE INDEX IF NOT EXISTS idx_codex_context_task_session
    ON codex_context_tasks(session_id, updated_at);

CREATE TABLE IF NOT EXISTS codex_context_assignments (
    interaction_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    assignment_reason TEXT NOT NULL DEFAULT '',
    classifier_version TEXT NOT NULL DEFAULT 'task-grouping-v1',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_codex_context_assignment_task
    ON codex_context_assignments(task_id);

CREATE TABLE IF NOT EXISTS codex_context_snapshots (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    task_id TEXT,
    turn_id TEXT,
    packet_sha256 TEXT NOT NULL,
    packet_chars INTEGER NOT NULL,
    estimated_tokens INTEGER NOT NULL,
    included_json TEXT NOT NULL DEFAULT '[]',
    omitted_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_codex_context_snapshot_session
    ON codex_context_snapshots(session_id, created_at);

CREATE TABLE IF NOT EXISTS codex_session_context_modes (
    session_id TEXT PRIMARY KEY,
    context_mode TEXT NOT NULL,
    generation INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS codex_native_run_claims (
    session_id TEXT PRIMARY KEY,
    generation INTEGER NOT NULL,
    run_token TEXT NOT NULL,
    started_at TEXT NOT NULL
);
"""

_SCHEMA_LOCK = threading.Lock()


def _schema_owner(db: Any) -> Any:
    """Return the object whose connection owns these local auxiliary tables."""
    return getattr(db, "_local", None) or db


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _task_id(session_id: str, root_id: str) -> str:
    digest = hashlib.sha256(f"{session_id}\0{root_id}".encode("utf-8")).hexdigest()
    return f"codex-task-{digest[:24]}"


def _conn(db: Any):
    getter = getattr(db, "_get_conn", None)
    if getter is not None:
        return getter()
    # HybridBackend intentionally exposes the canonical local mirror as
    # ``_local``; engine-owned auxiliary tables live there because they are
    # execution telemetry, not part of the portable transcript contract.
    local = getattr(db, "_local", None)
    getter = getattr(local, "_get_conn", None) if local is not None else None
    return getter() if getter is not None else None


def ensure_schema(db: Any) -> bool:
    owner = _schema_owner(db)
    if getattr(owner, "_codex_context_schema_ready", False):
        return True
    # DDL takes SQLite's writer lock.  Do it once per live backend rather than
    # once for every context-store helper in a turn.
    with _SCHEMA_LOCK:
        if getattr(owner, "_codex_context_schema_ready", False):
            return True
        conn = _conn(owner)
        if conn is None:
            return False
        try:
            conn.executescript(_SCHEMA)
            conn.commit()
            try:
                setattr(owner, "_codex_context_schema_ready", True)
            except Exception:
                # Exotic immutable adapters remain correct; they merely miss
                # the process-local fast path.
                pass
            return True
        finally:
            conn.close()


def materialize_session_tasks(db: Any, session_id: str) -> Optional[str]:
    """Assign every interaction to a stable task and return the current task id.

    Existing assignments always win. New user turns use the same text rule and
    memoised LLM verdict as the runtime history reducer; once written, a verdict
    is deliberately not recomputed on later runs.
    """
    if not session_id or not ensure_schema(db):
        return None
    from app.admin.tasks import (
        _decide_boundary,
        _is_synthetic_user,
        cached_llm_verdict,
    )

    conn = _conn(db)
    if conn is None:
        return None
    try:
        rows = conn.execute(
            "SELECT id, role, content, source, created_at FROM interactions "
            "WHERE session_id=? AND (status IS NULL OR status != 'deleted') "
            "ORDER BY COALESCE(session_seq, 999999999), created_at, id",
            (session_id,),
        ).fetchall()
        existing = {
            r["interaction_id"]: r["task_id"]
            for r in conn.execute(
                "SELECT interaction_id, task_id FROM codex_context_assignments "
                "WHERE session_id=?", (session_id,)
            ).fetchall()
        }
        current: Optional[str] = None
        prior_users: List[str] = []
        prior_assistant: List[str] = []
        now = _now()

        for row in rows:
            iid = str(row["id"])
            role = str(row["role"] or "")
            content = str(row["content"] or "")
            synthetic = role == "user" and _is_synthetic_user(content, row["source"])
            assigned = existing.get(iid)
            reason = "inherits current task"

            if role == "user" and not synthetic:
                if assigned:
                    current = assigned
                    reason = "existing durable assignment"
                else:
                    is_new, reason, sig = _decide_boundary({"prompt": content})
                    if current is None:
                        is_new, reason = True, "session start"
                    elif is_new and sig.get("ambiguous"):
                        if cached_llm_verdict(prior_users, prior_assistant, content) is True:
                            is_new, reason = False, "llm: same task"
                    if is_new:
                        current = _task_id(session_id, iid)
                        title = " ".join(content.strip().split())[:120]
                        conn.execute(
                            "INSERT OR IGNORE INTO codex_context_tasks "
                            "(id, session_id, root_interaction_id, title, objective, "
                            "status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, "
                            "'running', ?, ?)",
                            (current, session_id, iid, title, content[:2000], now, now),
                        )
                prior_users.append(content)
                prior_assistant.append("")
            elif assigned:
                current = assigned
            elif role == "assistant" and content.strip() and prior_assistant:
                prior_assistant[-1] = content

            if current:
                conn.execute(
                    "INSERT OR IGNORE INTO codex_context_assignments "
                    "(interaction_id, session_id, task_id, assignment_reason, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (iid, session_id, current, reason, now),
                )
                conn.execute(
                    "UPDATE codex_context_tasks SET updated_at=? WHERE id=?",
                    (now, current),
                )
        conn.commit()
        return current
    finally:
        conn.close()


def current_checkpoint(db: Any, session_id: str) -> Optional[Dict[str, Any]]:
    if not ensure_schema(db):
        return None
    task_id = materialize_session_tasks(db, session_id)
    if not task_id:
        return None
    conn = _conn(db)
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT id, status, checkpoint_json, revision FROM codex_context_tasks "
            "WHERE id=?", (task_id,)
        ).fetchone()
        if not row:
            return None
        try:
            body = json.loads(row["checkpoint_json"] or "{}")
        except (TypeError, ValueError):
            body = {}
        return {"task_id": row["id"], "status": row["status"],
                "revision": int(row["revision"] or 0), "checkpoint": body}
    finally:
        conn.close()


def task_state_for_interaction(
    db: Any, session_id: str, interaction_id: str,
) -> Optional[Dict[str, Any]]:
    """Materialize durable assignments and return the interaction's task state.

    Callers use the returned revision as a compare-and-swap fence before doing
    slow background work.  Looking up the task by interaction (rather than by
    the session's latest task) keeps a delayed Closer attached to the run it is
    actually closing when a newer user task has already started.
    """
    if not session_id or not interaction_id:
        return None
    materialize_session_tasks(db, session_id)
    conn = _conn(db)
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT t.id, t.objective, t.status, t.revision, i.session_seq "
            "FROM codex_context_assignments a "
            "JOIN codex_context_tasks t ON t.id=a.task_id "
            "JOIN interactions i ON i.id=a.interaction_id "
            "WHERE a.session_id=? AND a.interaction_id=? LIMIT 1",
            (session_id, interaction_id),
        ).fetchone()
        if not row:
            return None
        return {
            "task_id": str(row["id"]),
            "objective": str(row["objective"] or ""),
            "status": str(row["status"] or "running"),
            "revision": int(row["revision"] or 0),
            "final_session_seq": (
                int(row["session_seq"]) if row["session_seq"] is not None else None
            ),
        }
    finally:
        conn.close()


def bind_interaction_to_task(
    db: Any, *, session_id: str, interaction_id: str, task_id: str,
    reason: str = "closer checkpoint",
) -> bool:
    """Durably bind a generated row to a known task before later replay."""
    if not session_id or not interaction_id or not task_id or not ensure_schema(db):
        return False
    conn = _conn(db)
    if conn is None:
        return False
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO codex_context_assignments "
            "(interaction_id, session_id, task_id, assignment_reason, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (interaction_id, session_id, task_id, reason, _now()),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def bounded_tool_evidence(
    db: Any, task_id: str, *, limit: int = 20,
) -> List[Dict[str, Any]]:
    """Return payload-free descriptors for the newest tool results in a task.

    Raw tool content can contain huge documents, base64 data, or credentials.
    Checkpoints therefore retain only identity, outcome metadata, and a hash
    that lets a future run deliberately retrieve the authoritative DB row.
    """
    if not task_id or not ensure_schema(db):
        return []
    conn = _conn(db)
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT i.id, i.tool_name, i.tool_call_id, i.status, i.content, "
            "i.metadata FROM codex_context_assignments a "
            "JOIN interactions i ON i.id=a.interaction_id "
            "WHERE a.task_id=? AND i.role='tool' "
            "AND (i.status IS NULL OR i.status!='deleted') "
            "ORDER BY COALESCE(i.session_seq, 999999999) DESC, i.created_at DESC "
            "LIMIT ?",
            (task_id, max(0, min(int(limit), 50))),
        ).fetchall()
        evidence: List[Dict[str, Any]] = []
        safe_keys = {"success", "exit_code", "status", "error", "error_type"}
        for row in reversed(rows):
            content = str(row["content"] or "")
            item: Dict[str, Any] = {
                "interaction_id": str(row["id"]),
                "tool": str(row["tool_name"] or "tool"),
                "tool_call_id": str(row["tool_call_id"] or ""),
                "status": str(row["status"] or "complete"),
                "content_chars": len(content),
                "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            }
            try:
                meta = json.loads(row["metadata"] or "{}")
            except (TypeError, ValueError):
                meta = {}
            if isinstance(meta, dict):
                outcome = {
                    str(k): v for k, v in meta.items()
                    if k in safe_keys and isinstance(v, (str, int, float, bool))
                    and len(str(v)) <= 160
                }
                if outcome:
                    item["outcome"] = outcome
            evidence.append(item)
        return evidence
    finally:
        conn.close()


def save_checkpoint(
    db: Any, task_id: str, checkpoint: Dict[str, Any], *,
    expected_revision: Optional[int] = None,
) -> bool:
    """Atomically update a task checkpoint without letting an older turn win.

    The revision remains the normal compare-and-swap fence.  Two Closers may,
    however, capture the same revision before either slow model call finishes.
    In that case the final response's durable ``session_seq`` is the ordering
    fence: a newer turn may supersede the first writer, while an older (or
    unorderable) result is rejected.
    """
    if not task_id or not ensure_schema(db):
        return False
    status = str(checkpoint.get("status") or "running")
    if status not in {"running", "needs_work", "needs_input", "blocked", "complete", "cancelled", "error"}:
        status = "running"
    conn = _conn(db)
    if conn is None:
        return False
    try:
        # Serialize the read/decision/write.  A plain UPDATE CAS cannot decide
        # whether the revision winner represented an older or newer turn.
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT revision, checkpoint_json FROM codex_context_tasks WHERE id=?",
            (task_id,),
        ).fetchone()
        if not row:
            conn.rollback()
            return False
        current_revision = int(row["revision"] or 0)
        revision_matches = (
            expected_revision is None or current_revision == int(expected_revision)
        )
        try:
            existing = json.loads(row["checkpoint_json"] or "{}")
        except (TypeError, ValueError):
            existing = {}

        def _final_seq(value: Any) -> Optional[int]:
            if not isinstance(value, dict):
                return None
            refs = value.get("references")
            raw = refs.get("final_session_seq") if isinstance(refs, dict) else None
            if raw is None or isinstance(raw, bool):
                return None
            try:
                return int(raw)
            except (TypeError, ValueError):
                return None

        incoming_seq = _final_seq(checkpoint)
        existing_seq = _final_seq(existing)
        if incoming_seq is not None and existing_seq is not None:
            # Turn ordering is stronger than revision ordering, including when
            # a delayed older Closer only starts after the newer one committed.
            allowed = incoming_seq > existing_seq
        else:
            allowed = revision_matches
        if not allowed:
            conn.rollback()
            return False
        cur = conn.execute(
            "UPDATE codex_context_tasks SET checkpoint_json=?, status=?, "
            "revision=revision+1, updated_at=? WHERE id=?",
            (json.dumps(checkpoint, ensure_ascii=False), status, _now(), task_id),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def record_snapshot(
    db: Any, *, session_id: str, task_id: Optional[str], turn_id: Optional[str],
    packet: str, included_ids: Optional[List[str]] = None,
    omitted_ids: Optional[List[str]] = None,
) -> Optional[str]:
    if not ensure_schema(db):
        return None
    digest = hashlib.sha256(packet.encode("utf-8")).hexdigest()
    stamp = _now()
    sid = "codex-snapshot-" + hashlib.sha256(
        f"{session_id}\0{turn_id or ''}\0{digest}\0{stamp}".encode("utf-8")
    ).hexdigest()[:24]
    conn = _conn(db)
    if conn is None:
        return None
    try:
        conn.execute(
            "INSERT INTO codex_context_snapshots "
            "(id, session_id, task_id, turn_id, packet_sha256, packet_chars, "
            "estimated_tokens, included_json, omitted_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (sid, session_id, task_id, turn_id, digest, len(packet),
             max(1, len(packet) // 4), json.dumps(included_ids or []),
             json.dumps(omitted_ids or []), stamp),
        )
        conn.commit()
        return sid
    finally:
        conn.close()


def note_session_mode(db: Any, session_id: str, context_mode: str) -> Optional[str]:
    """Persist the effective mode and return the previous mode on transition.

    The engine uses this fence to ensure wrapper -> native never resumes the
    stale native thread that missed wrapper-mode turns. ``None`` means the mode
    is unchanged (including the first recorded turn).
    """
    if context_mode not in {"native_codex", "webagent_wrapper"}:
        raise ValueError(f"unsupported Codex context mode: {context_mode}")
    if not ensure_schema(db):
        return None
    conn = _conn(db)
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT context_mode FROM codex_session_context_modes WHERE session_id=?",
            (session_id,),
        ).fetchone()
        previous = str(row["context_mode"]) if row else None
        now = _now()
        if row is None:
            conn.execute(
                "INSERT INTO codex_session_context_modes "
                "(session_id, context_mode, generation, updated_at) VALUES (?, ?, 1, ?)",
                (session_id, context_mode, now),
            )
        elif previous != context_mode:
            conn.execute(
                "UPDATE codex_session_context_modes SET context_mode=?, "
                "generation=generation+1, updated_at=? WHERE session_id=?",
                (context_mode, now, session_id),
            )
            if previous == "webagent_wrapper" and context_mode == "native_codex":
                # Invalidate the native thread in the SAME transaction as the
                # mode generation. A crash can therefore leave either the old
                # wrapper mode or a fresh-native requirement, never a native
                # mode marker pointing at stale thread state.
                srow = conn.execute(
                    "SELECT metadata FROM sessions WHERE id=?", (session_id,)
                ).fetchone()
                if srow:
                    try:
                        smeta = json.loads(srow["metadata"] or "{}")
                    except (TypeError, ValueError):
                        smeta = {}
                    if not isinstance(smeta, dict):
                        smeta = {}
                    smeta.pop("codex_thread_id", None)
                    conn.execute(
                        "UPDATE sessions SET metadata=?, updated_at=? WHERE id=?",
                        (json.dumps(smeta), now, session_id),
                    )
        conn.commit()
        return previous if previous and previous != context_mode else None
    finally:
        conn.close()


def claim_native_run(db: Any, session_id: str, run_token: str) -> Optional[int]:
    """Claim the current native-mode generation for one headless invocation.

    A positive generation is a durable claim, ``0`` means this adapter has no
    local context store (legacy/test fallback), and ``None`` means another run
    already owns the current generation.
    """
    if not ensure_schema(db):
        return 0
    conn = _conn(db)
    if conn is None:
        return 0
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT context_mode, generation FROM codex_session_context_modes "
            "WHERE session_id=?", (session_id,),
        ).fetchone()
        if not row or str(row["context_mode"]) != "native_codex":
            conn.rollback()
            return None
        generation = int(row["generation"] or 0)
        existing = conn.execute(
            "SELECT generation FROM codex_native_run_claims WHERE session_id=?",
            (session_id,),
        ).fetchone()
        if existing and int(existing["generation"] or 0) == generation:
            conn.rollback()
            return None
        conn.execute(
            "INSERT INTO codex_native_run_claims "
            "(session_id, generation, run_token, started_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET generation=excluded.generation, "
            "run_token=excluded.run_token, started_at=excluded.started_at",
            (session_id, generation, run_token, _now()),
        )
        conn.commit()
        return generation
    finally:
        conn.close()


def persist_native_thread_for_run(
    db: Any, session_id: str, generation: int, run_token: str, thread_id: str,
) -> Optional[bool]:
    """Atomically persist a thread only while this run still owns its generation."""
    if generation <= 0:
        return None
    conn = _conn(db)
    if conn is None:
        return None
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT m.context_mode, m.generation, c.run_token "
            "FROM codex_session_context_modes m "
            "LEFT JOIN codex_native_run_claims c ON c.session_id=m.session_id "
            "AND c.generation=m.generation WHERE m.session_id=?",
            (session_id,),
        ).fetchone()
        if (not row or str(row["context_mode"]) != "native_codex"
                or int(row["generation"] or 0) != generation
                or str(row["run_token"] or "") != run_token):
            conn.rollback()
            return False
        srow = conn.execute(
            "SELECT metadata FROM sessions WHERE id=?", (session_id,),
        ).fetchone()
        if not srow:
            conn.rollback()
            return False
        try:
            metadata = json.loads(srow["metadata"] or "{}")
        except (TypeError, ValueError):
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        metadata["codex_thread_id"] = thread_id
        conn.execute(
            "UPDATE sessions SET metadata=?, updated_at=? WHERE id=?",
            (json.dumps(metadata), _now(), session_id),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def release_native_run(
    db: Any, session_id: str, generation: int, run_token: str,
) -> None:
    if generation <= 0:
        return
    conn = _conn(db)
    if conn is None:
        return
    try:
        conn.execute(
            "DELETE FROM codex_native_run_claims WHERE session_id=? "
            "AND generation=? AND run_token=?",
            (session_id, generation, run_token),
        )
        conn.commit()
    finally:
        conn.close()


def invalidate_native_thread(db: Any, session_id: str) -> bool:
    """Forget a stale native thread before a wrapper -> native reseed."""
    conn = _conn(db)
    if conn is None:
        return False
    try:
        row = conn.execute("SELECT metadata FROM sessions WHERE id=?", (session_id,)).fetchone()
        if not row:
            return False
        try:
            meta = json.loads(row["metadata"] or "{}")
        except (TypeError, ValueError):
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        meta.pop("codex_thread_id", None)
        conn.execute(
            "UPDATE sessions SET metadata=?, updated_at=? WHERE id=?",
            (json.dumps(meta), _now(), session_id),
        )
        conn.commit()
        return True
    finally:
        conn.close()
