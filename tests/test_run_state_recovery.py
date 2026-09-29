import asyncio
import json

from app.agent.runner import _classify_resume, recovery_pending, resume_one
from app.db.local import LocalBackend


async def _resume_claim_clears_stale_op_and_stale_finisher_cannot_clobber(tmp_path, monkeypatch):
    async def no_status(*args, **kwargs):
        return None

    monkeypatch.setattr("app.db.local._emit_agent_run_status", no_status)
    db = LocalBackend(str(tmp_path / "recovery.db"), seed=False)
    session_id = "recovery-session"
    conn = db._get_conn()
    try:
        conn.execute(
            "INSERT INTO sessions (id, user_id, title, metadata) VALUES (?, ?, ?, ?)",
            ("recovery-session", "user", "recovery", "{}"),
        )
        conn.commit()
    finally:
        conn.close()
    await db.run_state_begin(
        session_id, "user", "agent", "turn", origin="web",
    )
    await db.run_state_set_op(session_id, '{"tool":"patch_source"}')
    await db.mark_orphans_for_resume()

    interrupted = await db.run_state_get(session_id)
    assert interrupted["status"] == "interrupted"
    assert interrupted["current_op"] is not None

    claimed = await db.run_state_claim_for_resume(
        session_id, "resume-owner", 120, 1, 3,
    )
    assert claimed is True
    resumed = await db.run_state_get(session_id)
    assert resumed["status"] == "running"
    assert resumed["current_op"] is None
    assert resumed["resume_attempts"] == 1

    stale_finished = await db.run_state_finish(
        session_id,
        status="interrupted",
        stop_cause="crash",
        expected_resume_attempts=0,
    )
    assert stale_finished is False
    assert (await db.run_state_get(session_id))["status"] == "running"

    current_finished = await db.run_state_finish(
        session_id,
        status="complete",
        stop_cause="complete",
        expected_owner_token="resume-owner",
        expected_resume_attempts=1,
    )
    assert current_finished is True
    assert (await db.run_state_get(session_id))["status"] == "complete"


def test_resume_claim_clears_stale_op_and_stale_finisher_cannot_clobber(tmp_path, monkeypatch):
    asyncio.run(_resume_claim_clears_stale_op_and_stale_finisher_cannot_clobber(tmp_path, monkeypatch))


async def _frozen_retry_ladder_reaches_final_budgeted_attempt(tmp_path, monkeypatch):
    async def no_status(*args, **kwargs):
        return None

    monkeypatch.setattr("app.db.local._emit_agent_run_status", no_status)
    db = LocalBackend(str(tmp_path / "frozen-recovery.db"), seed=False)
    session_id = "frozen-recovery-session"
    conn = db._get_conn()
    try:
        conn.execute(
            "INSERT INTO sessions (id, user_id, title, metadata) VALUES (?, ?, ?, ?)",
            (session_id, "user", "frozen recovery", "{}"),
        )
        conn.commit()
    finally:
        conn.close()

    await db.run_state_begin(session_id, "user", "agent", "turn", origin="web")
    # Each claimed recovery is interrupted by the same involuntary frozen cause.
    # The third claim must still launch; only the fourth is over budget.
    for attempt in (1, 2, 3):
        await db.run_state_set_cause(session_id, "frozen")
        await db.run_state_finish(session_id, status="interrupted", stop_cause="frozen")
        token = f"resume-owner-{attempt}"
        assert await db.run_state_claim_for_resume(
            session_id, token, 120, 1, 3,
        ) is True
        row = await db.run_state_get(session_id)
        assert row["status"] == "running"
        assert row["resume_attempts"] == attempt

    await db.run_state_finish(
        session_id, status="interrupted", stop_cause="frozen",
        expected_owner_token="resume-owner-3", expected_resume_attempts=3,
    )
    assert await db.run_state_claim_for_resume(
        session_id, "resume-owner-4", 120, 1, 3,
    ) is False


async def _provider_probe_defer_charges_recovery_budget(tmp_path, monkeypatch):
    async def no_status(*args, **kwargs):
        return None

    monkeypatch.setattr("app.db.local._emit_agent_run_status", no_status)
    db = LocalBackend(str(tmp_path / "probe-defer.db"), seed=False)
    session_id = "probe-defer-session"
    conn = db._get_conn()
    try:
        conn.execute(
            "INSERT INTO sessions (id, user_id, title, metadata) VALUES (?, ?, ?, ?)",
            (session_id, "user", "probe defer", "{}"),
        )
        conn.commit()
    finally:
        conn.close()

    await db.run_state_begin(session_id, "user", "agent", "turn", origin="web")
    await db.mark_orphans_for_resume()
    await db.run_state_defer_resume(session_id, 30, note="provider unavailable")

    row = await db.run_state_get(session_id)
    assert row["status"] == "interrupted"
    assert row["resume_attempts"] == 1
    assert row["next_resume_at"] is not None
    assert row["error"] == "provider unavailable"


def test_provider_probe_defer_charges_recovery_budget(tmp_path, monkeypatch):
    asyncio.run(_provider_probe_defer_charges_recovery_budget(tmp_path, monkeypatch))

def test_frozen_retry_ladder_reaches_final_budgeted_attempt(tmp_path, monkeypatch):
    asyncio.run(_frozen_retry_ladder_reaches_final_budgeted_attempt(tmp_path, monkeypatch))


async def _recovery_pending_holds_until_the_run_is_retired(tmp_path, monkeypatch):
    """The busy signal (list spinner / "re-assessing…" note) must outlive the
    retry budget.

    A run that spent its last attempt stays parked with its resumable cause
    until the next watchdog tick retires it as ``failed``. Clearing the
    indicator inside that window is the reported idle → working flicker, so
    ``recovery_pending`` keys on the durable "gave up" cause, never the count.
    """
    async def no_status(*args, **kwargs):
        return None

    monkeypatch.setattr("app.db.local._emit_agent_run_status", no_status)
    db = LocalBackend(str(tmp_path / "recovery-hold.db"), seed=False)
    session_id = "recovery-hold-session"
    conn = db._get_conn()
    try:
        conn.execute(
            "INSERT INTO sessions (id, user_id, title, metadata) VALUES (?, ?, ?, ?)",
            (session_id, "user", "recovery hold", "{}"),
        )
        conn.commit()
    finally:
        conn.close()

    await db.run_state_begin(session_id, "user", "agent", "turn", origin="web")
    # Spend the whole budget: three claims, each interrupted by the same
    # involuntary cause — exactly the state the watchdog leaves behind.
    for attempt in (1, 2, 3):
        await db.run_state_finish(session_id, status="interrupted", stop_cause="crash")
        assert await db.run_state_claim_for_resume(
            session_id, f"resume-owner-{attempt}", 120, 1, 3,
        ) is True
    await db.run_state_finish(session_id, status="interrupted", stop_cause="crash")

    row = await db.run_state_get(session_id)
    assert row["resume_attempts"] == 3
    # The budget is spent — a fourth claim is refused...
    assert await db.run_state_claim_for_resume(
        session_id, "resume-owner-4", 120, 1, 3,
    ) is False
    # ...but the run is not retired yet, so the in-progress signal must hold.
    assert recovery_pending(row) is True

    # Only retirement clears it, and retirement is durable.
    await db.run_state_mark_failed(session_id, None)
    retired = await db.run_state_get(session_id)
    assert retired["stop_cause"] == "failed"
    assert recovery_pending(retired) is False


def test_recovery_pending_holds_until_the_run_is_retired(tmp_path, monkeypatch):
    asyncio.run(_recovery_pending_holds_until_the_run_is_retired(tmp_path, monkeypatch))


def test_recovery_pending_only_holds_resumable_stops():
    # A live or completed run is working/finished, never "re-assessing".
    assert recovery_pending({"status": "running", "stop_cause": None}) is False
    assert recovery_pending({"status": "complete", "stop_cause": "complete"}) is False
    assert recovery_pending({}) is False
    # Every involuntary cause holds, at any attempt depth.
    for cause in ("crash", "server_restart", "frozen", "zombie", "empty_response"):
        assert recovery_pending({"status": "interrupted", "stop_cause": cause}) is True
    # Voluntary stops are terminal: a user stop (incl. the global stop, which
    # tags cancelled runs 'user_stop') and the manual-resume hold never revive.
    assert recovery_pending({"status": "interrupted", "stop_cause": "user_stop"}) is False
    assert recovery_pending({"status": "interrupted", "stop_cause": "needs_manual_resume"}) is False
    assert recovery_pending({"status": "error", "stop_cause": "provider_error"}) is False


# ── One promoted terminal per run ─────────────────────────────────────────────
# A run that died AFTER its closing message was promoted still looks like an
# orphan to the boot sweep: the terminal row is durable, but the run never
# recorded a terminal status. Resuming it re-runs the whole turn and promotes a
# SECOND answer into the same activity group. The gate promotes one terminal per
# run, so a turn that already has its promoted closer is closed out, not revived.
# ``closing_promoted`` is written only by the loop's terminal promotion block,
# so an ordinary progress row as the destination means "never answered".


async def _run_that_died_with_destination(tmp_path, monkeypatch, *, name, meta):
    async def no_status(*args, **kwargs):
        return None

    monkeypatch.setattr("app.db.local._emit_agent_run_status", no_status)
    db = LocalBackend(str(tmp_path / f"{name}.db"), seed=False)
    session_id = f"{name}-session"
    conn = db._get_conn()
    try:
        conn.execute(
            "INSERT INTO sessions (id, user_id, title, metadata) VALUES (?, ?, ?, ?)",
            (session_id, "user", name, "{}"),
        )
        conn.commit()
    finally:
        conn.close()

    await db.run_state_begin(session_id, "user", "agent", "turn", origin="web")
    destination = await db.insert_interaction(
        "user", session_id, role="assistant", content="The closing message.",
        metadata=json.dumps(meta),
        chat_render_kind="promoted_answer" if meta.get("closing_promoted") else "activity",
    )
    await db.run_state_set_assistant(session_id, destination)
    # The process dies before recording a terminal status → the boot sweep
    # re-tags the still-'running' row as an orphan resume candidate.
    await db.mark_orphans_for_resume()
    return db, session_id


async def _promoted_turn_is_closed_not_resumed(tmp_path, monkeypatch):
    db, session_id = await _run_that_died_with_destination(
        tmp_path, monkeypatch, name="promoted",
        meta={"message_phase": "main", "closing_promoted": True},
    )
    row = await db.run_state_get(session_id)
    assert row["status"] == "interrupted"  # it *looks* resumable
    decision, why = await _classify_resume(db, row)
    assert decision == "complete"
    assert "promoted terminal" in why


def test_promoted_turn_is_closed_not_resumed(tmp_path, monkeypatch):
    asyncio.run(_promoted_turn_is_closed_not_resumed(tmp_path, monkeypatch))


async def _unanswered_turn_still_resumes(tmp_path, monkeypatch):
    # A mid-run death leaves an ordinary progress row as the destination: the
    # turn never answered, so recovery must still re-run it.
    db, session_id = await _run_that_died_with_destination(
        tmp_path, monkeypatch, name="unanswered",
        meta={"message_phase": "progress"},
    )
    row = await db.run_state_get(session_id)
    assert await _classify_resume(db, row) == ("ok", "")


def test_unanswered_turn_still_resumes(tmp_path, monkeypatch):
    asyncio.run(_unanswered_turn_still_resumes(tmp_path, monkeypatch))


async def _resume_one_closes_a_promoted_run(tmp_path, monkeypatch):
    db, session_id = await _run_that_died_with_destination(
        tmp_path, monkeypatch, name="closed",
        meta={"message_phase": "main", "closing_promoted": True},
    )

    class _RunManager:
        def is_running(self, _sid):
            return False

    monkeypatch.setattr("app.db.get_db", lambda: db)
    monkeypatch.setattr("app.agent.run_manager.get_run_manager", lambda: _RunManager())
    monkeypatch.setattr("app.kill_switch.is_engaged", lambda: False)

    # No turn is launched (that would promote a second answer) and the orphaned
    # row is retired so the watchdog stops picking it up.
    assert await resume_one(session_id, reason="boot") is False
    closed = await db.run_state_get(session_id)
    assert closed["status"] == "complete"
    assert closed["resume_attempts"] == 0


def test_resume_one_closes_a_promoted_run(tmp_path, monkeypatch):
    asyncio.run(_resume_one_closes_a_promoted_run(tmp_path, monkeypatch))


# ── One promoted terminal per run ─────────────────────────────────────────────
# A run that died AFTER its closing message was promoted still looks like an
# orphan to the boot sweep: the promoted row is durable but the run never
# recorded a terminal status. Resuming it re-runs the whole turn and promotes a
# SECOND answer into the same activity group. The closing gate promotes exactly
# one terminal per run, so a turn that already has its promoted closer is closed
# out instead of revived.

async def _promoted_run_db(tmp_path, monkeypatch, *, name, destination_meta):
    """A run whose destination row carries ``destination_meta``, orphaned by a
    restart before it could record a terminal status."""

    async def no_status(*args, **kwargs):
        return None

    monkeypatch.setattr("app.db.local._emit_agent_run_status", no_status)
    db = LocalBackend(str(tmp_path / f"{name}.db"), seed=False)
    session_id = f"{name}-session"
    conn = db._get_conn()
    try:
        conn.execute(
            "INSERT INTO sessions (id, user_id, title, metadata) VALUES (?, ?, ?, ?)",
            (session_id, "user", name, "{}"),
        )
        conn.commit()
    finally:
        conn.close()

    await db.run_state_begin(session_id, "user", "agent", "turn", origin="web")
    destination = await db.insert_interaction(
        "user", session_id, role="assistant", content="The closing message.",
        metadata=json.dumps(destination_meta),
        chat_render_kind="promoted_answer" if destination_meta.get("closing_promoted") else "activity",
    )
    await db.run_state_set_assistant(session_id, destination)
    await db.mark_orphans_for_resume()
    return db, session_id


async def _promoted_turn_is_not_resumed(tmp_path, monkeypatch):
    db, session_id = await _promoted_run_db(
        tmp_path, monkeypatch, name="promoted",
        destination_meta={"message_phase": "main", "closing_promoted": True},
    )
    decision, why = await _classify_resume(db, await db.run_state_get(session_id))
    assert decision == "complete"
    assert "promoted terminal" in why

    # A mid-run death leaves an ordinary progress row as the destination — the
    # turn never answered, so recovery must still re-run it.
    db2, sid2 = await _promoted_run_db(
        tmp_path, monkeypatch, name="midrun",
        destination_meta={"message_phase": "progress"},
    )
    assert await _classify_resume(db2, await db2.run_state_get(sid2)) == ("ok", "")


def test_promoted_turn_is_not_resumed(tmp_path, monkeypatch):
    asyncio.run(_promoted_turn_is_not_resumed(tmp_path, monkeypatch))


async def _resume_one_closes_a_promoted_turn(tmp_path, monkeypatch):
    db, session_id = await _promoted_run_db(
        tmp_path, monkeypatch, name="closer",
        destination_meta={"message_phase": "main", "closing_promoted": True},
    )

    class _RunManager:
        def is_running(self, _sid):
            return False

    monkeypatch.setattr("app.db.get_db", lambda: db)
    monkeypatch.setattr("app.agent.run_manager.get_run_manager", lambda: _RunManager())
    monkeypatch.setattr("app.kill_switch.is_engaged", lambda: False)

    assert await resume_one(session_id, reason="boot") is False
    closed = await db.run_state_get(session_id)
    assert closed["status"] == "complete"
    assert closed["resume_attempts"] == 0


def test_resume_one_closes_a_promoted_turn(tmp_path, monkeypatch):
    asyncio.run(_resume_one_closes_a_promoted_turn(tmp_path, monkeypatch))


# ── The durable latch itself (the run's ONE promoted terminal) ────────────────

async def _promotion_latch_reads_the_durable_marker(tmp_path, monkeypatch):
    """``has_promoted_closing_terminal`` is the probe the loop's terminal block
    consults before promoting. It must read the durable ``closing_promoted``
    marker within the CURRENT run (rows after the session's newest user turn) and
    nothing else — a progress row, an earlier run's promotion, or a session with
    no user turn must all report "not promoted"."""

    async def no_status(*args, **kwargs):
        return None

    monkeypatch.setattr("app.db.local._emit_agent_run_status", no_status)
    db = LocalBackend(str(tmp_path / "latch.db"), seed=False)
    session_id = "latch-session"
    conn = db._get_conn()
    try:
        conn.execute(
            "INSERT INTO sessions (id, user_id, title, metadata) VALUES (?, ?, ?, ?)",
            (session_id, "user", "latch", "{}"),
        )
        conn.commit()
    finally:
        conn.close()

    # No user turn → no run boundary → never "promoted".
    assert await db.has_promoted_closing_terminal("user", session_id) is False

    await db.insert_interaction("user", session_id, role="user", content="go")
    # A progress row is an activity attempt, not the run's promoted answer.
    await db.insert_interaction(
        "user", session_id, role="assistant", content="working…",
        metadata=json.dumps({"message_phase": "progress"}),
    )
    assert await db.has_promoted_closing_terminal("user", session_id) is False

    # The promoted closer trips the latch — a re-entered run must not promote a
    # second terminal.
    await db.insert_interaction(
        "user", session_id, role="assistant", content="done",
        metadata=json.dumps({"message_phase": "main", "closing_promoted": True}),
        chat_render_kind="promoted_answer",
    )
    assert await db.has_promoted_closing_terminal("user", session_id) is True

    # A NEW user turn opens a fresh run: the previous run's promotion no longer
    # blocks this run's own closer.
    await db.insert_interaction("user", session_id, role="user", content="again")
    assert await db.has_promoted_closing_terminal("user", session_id) is False


def test_promotion_latch_reads_the_durable_marker(tmp_path, monkeypatch):
    asyncio.run(_promotion_latch_reads_the_durable_marker(tmp_path, monkeypatch))
