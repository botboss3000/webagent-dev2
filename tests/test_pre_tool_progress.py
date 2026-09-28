"""Pre-edit progress-message gate tests.

Load-bearing properties:

* **Strict tool-free boundary** — every AUTO-mode edit stays blocked until the
  agent sends a separate pre-edit progress message; no approval is requested.
* **Nothing judges message quality** — any non-empty, tool-free message passes;
  auditing/refining its prose remains a separate concern.
* **No mixed-step bypass** — a response that includes tools cannot publish or
  satisfy the gate, so the user-visible message contains no announced-work calls.
* **At most one message per run** — the published row carries a durable marker
  that the crash-resume latch reads.
* **Gate-off / Plan / Ask paths are untouched.**
* **Guidance only** — the descriptor body is the floor; the app-level and
  per-agent ``format`` layers stack on it and are never checked.
"""

import asyncio
import json
from pathlib import Path

import pytest

from app.agent import gate_registry, pre_tool_progress
from app.agent.contracts import (
    mark_tool_scheduled,
    mechanical_check,
    new_contract_state,
)

LOOP_SOURCE = (
    Path(__file__).resolve().parents[1] / "app" / "agent" / "loop.py"
).read_text(encoding="utf-8")


def _agent(gates=None, **extra):
    rec = {"id": "agent-1", "name": "Test",
           "metadata": {"gates": {"schema_version": 1, "gates": gates or []}}}
    rec.update(extra)
    return rec


@pytest.fixture(autouse=True)
def _fresh_registry():
    gate_registry.reload_gate_types()
    yield
    gate_registry.reload_gate_types()


# ── Descriptor ───────────────────────────────────────────────────────────────

def test_descriptor_declares_a_strict_pre_edit_message_boundary():
    descriptor = gate_registry.gate_types()["pre_tool_progress_message"]
    assert descriptor["id"] == "pre_tool_progress_message"
    assert descriptor["display_name"] == "Pre-edit progress message"
    # The registry normalises the view (a bool view gains param/default=False);
    # assert the projection the loop actually reads, not the raw descriptor dict.
    assert descriptor["view"]["key"] == "require_pre_tool_progress_message"
    assert descriptor["view"]["kind"] == "bool"
    # ``pre_tool``: the body asks for a REPLY, so it belongs to the message the
    # rule itself feeds back — never the standing ## Contract block.
    assert descriptor["inject"] == "pre_tool"
    assert descriptor["phase"] == "pre_tool"
    assert "tool-free" in descriptor["prompt_instruction"]


def test_pre_edit_message_instructions_are_editable_agent_guidance():
    params = gate_registry.gate_types()["pre_tool_progress_message"]["params"]
    assert set(params) == {"format"}
    assert params["format"]["multiline"] is True
    assert params["format"]["label"] == "Pre-edit progress message instructions"
    # The detailed, user-editable instruction ships in the agent-level field;
    # the descriptor body remains the backend's minimal floor.
    assert "what you found" in params["format"]["default"]


def test_pre_tool_type_never_reaches_the_standing_block():
    rec = _agent([{"type": "pre_tool_progress_message", "enabled": True, "message": "MARKER"}])
    assert gate_registry.prompt_instructions_for_agent(rec) == {}


# ── Guidance layering (mirrors closing_message) ──────────────────────────────

def test_assemble_instruction_stacks_floor_then_app_then_agent(monkeypatch):
    descriptor = gate_registry.gate_types()["pre_tool_progress_message"]
    floor = descriptor["prompt_instruction"]
    instance = {"type": "pre_tool_progress_message", "params": {"format": "AGENT-FORMAT"}}

    # The backend floor is followed by the descriptor's user-editable default.
    monkeypatch.setattr(pre_tool_progress, "_settings_block", lambda: {})
    default = descriptor["params"]["format"]["default"]
    assert pre_tool_progress.assemble_instruction({"params": {}}, descriptor) == f"{floor}\n{default}"

    # App-level guidance rides in, and the agent's own layer is appended last.
    monkeypatch.setattr(pre_tool_progress, "_settings_block", lambda: {
        "gate_settings": {"pre_tool_progress_message": {"format": "APP-FORMAT"}}})
    composed = pre_tool_progress.assemble_instruction(instance, descriptor)
    assert composed.index(floor) < composed.index("APP-FORMAT") < composed.index("AGENT-FORMAT")

    # The app layer alone (no per-agent value) still applies.
    assert "APP-FORMAT" in pre_tool_progress.assemble_instruction({"params": {}}, descriptor)


def test_app_gate_values_is_tolerant_of_unconfigured_and_junk(monkeypatch):
    monkeypatch.setattr(pre_tool_progress, "_settings_block", lambda: {})
    assert pre_tool_progress.app_gate_values() == {}
    monkeypatch.setattr(pre_tool_progress, "_settings_block",
                        lambda: {"gate_settings": "not-a-dict"})
    assert pre_tool_progress.app_gate_values() == {}
    monkeypatch.setattr(pre_tool_progress, "_settings_block", lambda: {
        "gate_settings": {"pre_tool_progress_message": {"format": "  ", "x": None, "y": True,
                                               "z": ["nope"], "keep": " v "}}})
    assert pre_tool_progress.app_gate_values() == {"keep": "v"}


# ── Policy resolution ────────────────────────────────────────────────────────

def test_resolve_is_none_when_the_gate_is_off_or_missing():
    assert pre_tool_progress.resolve(_agent([]), "auto") is None
    assert pre_tool_progress.resolve(_agent([{"type": "pre_tool_progress_message",
                                          "enabled": False}]), "auto") is None
    assert pre_tool_progress.resolve(None, "auto") is None


def test_resolve_returns_the_assembled_announcement():
    policy = pre_tool_progress.resolve(
        _agent([{"type": "pre_tool_progress_message", "enabled": True}]), "auto")
    assert policy is not None
    assert pre_tool_progress.instruction(policy)


# ── Publishability (the ONLY requirement, by design) ─────────────────────────

def test_anything_the_agent_says_is_publishable():
    """No completeness check, no format check, no reviewer: a bare non-empty
    message passes, because the progress update is information for the user and a run must
    never be held back by the quality of one."""
    assert pre_tool_progress.is_publishable("x")
    assert pre_tool_progress.is_publishable("Here is my plan.")
    for empty in ("", "   ", "\n\t ", None):
        assert not pre_tool_progress.is_publishable(empty)


def test_presentation_records_that_a_plan_was_announced_never_how_good_it_was():
    record = pre_tool_progress.presentation("plan text")
    assert record["announced"] is True
    assert record["chars"] == len("plan text")
    assert record["format_configured"] is False
    # Non-judgemental: no score, no verdict, no requirement list.
    assert set(record) == {"announced", "chars", "format_configured"}


# ── The mechanical rule ──────────────────────────────────────────────────────

def test_mechanical_check_holds_every_edit_until_the_tool_free_message_is_published():
    state = new_contract_state()
    contract = {"require_pre_tool_progress_message": True}
    msg = mechanical_check(contract, state, tool_name="write_source", execution_mode="auto",
                           pre_tool_progress_message="ANNOUNCE-MARKER", turn=1)
    assert msg == "ANNOUNCE-MARKER"
    assert state["pre_tool_progress_message_pending"] is True
    assert state["pre_tool_progress_message_done"] is False

    # Same-turn siblings and later retries receive the instruction too. A tool
    # scheduling event cannot silently release the separate-message boundary.
    assert mechanical_check(contract, state, tool_name="write_source", execution_mode="auto", turn=1)
    mark_tool_scheduled(state)
    assert state["pre_tool_progress_message_pending"] is True
    assert mechanical_check(contract, state, tool_name="write_source", execution_mode="auto", turn=2)

    # The dedicated tool-free publisher is the only release path; simulate its
    # durable state transition and verify subsequent work is allowed.
    state["pre_tool_progress_message_pending"] = False
    state["pre_tool_progress_message_done"] = True
    assert mechanical_check(contract, state, tool_name="write_source", execution_mode="auto") is None


# ── The durable latch ────────────────────────────────────────────────────────

def test_already_sent_is_best_effort():
    async def scenario():
        # A backend without the probe, and an unreadable transcript, both report
        # "not sent" — the latch can never fail a run.
        assert await pre_tool_progress.already_sent(object(), "u", "s") is False

        class _Boom:
            async def has_pre_tool_progress_message(self, user_id, session_id):
                raise RuntimeError("db down")

        assert await pre_tool_progress.already_sent(_Boom(), "u", "s") is False

        class _Hit:
            async def has_pre_tool_progress_message(self, user_id, session_id):
                return True

        assert await pre_tool_progress.already_sent(_Hit(), "u", "s") is True

    asyncio.run(scenario())


async def _latch_reads_the_durable_marker(tmp_path, monkeypatch):
    """``has_pre_tool_progress_message`` must read the durable ``pre_tool_progress_message_sent`` marker
    within the CURRENT run (rows after the session's newest user turn) and
    nothing else, so a crash-resume neither re-asks for a progress update nor posts a
    second one."""
    from app.db.local import LocalBackend

    async def no_status(*args, **kwargs):
        return None

    monkeypatch.setattr("app.db.local._emit_agent_run_status", no_status)
    db = LocalBackend(str(tmp_path / "plan-latch.db"), seed=False)
    session_id = "plan-latch-session"
    conn = db._get_conn()
    try:
        conn.execute(
            "INSERT INTO sessions (id, user_id, title, metadata) VALUES (?, ?, ?, ?)",
            (session_id, "user", "latch", "{}"),
        )
        conn.commit()
    finally:
        conn.close()

    # No user turn → no run boundary → never "announced".
    assert await db.has_pre_tool_progress_message("user", session_id) is False

    await db.insert_interaction("user", session_id, role="user", content="go")
    # A progress row is an activity attempt, not a published plan.
    await db.insert_interaction(
        "user", session_id, role="assistant", content="working…",
        metadata=json.dumps({"message_phase": "progress"}),
    )
    assert await db.has_pre_tool_progress_message("user", session_id) is False

    # The published plan trips the latch.
    await db.insert_interaction(
        "user", session_id, role="assistant", content="the plan",
        metadata=json.dumps({"message_phase": "main", "pre_tool_progress_message_sent": True}),
    )
    assert await db.has_pre_tool_progress_message("user", session_id) is True

    # A promoted CLOSING terminal is not a progress update — the markers stay independent.
    await db.insert_interaction("user", session_id, role="user", content="again")
    await db.insert_interaction(
        "user", session_id, role="assistant", content="answer",
        metadata=json.dumps({"message_phase": "main", "closing_promoted": True}),
    )
    assert await db.has_pre_tool_progress_message("user", session_id) is False


def test_latch_reads_the_durable_marker(tmp_path, monkeypatch):
    asyncio.run(_latch_reads_the_durable_marker(tmp_path, monkeypatch))


# ── Loop wiring (source invariants) ──────────────────────────────────────────

def _publish_helper() -> str:
    return LOOP_SOURCE[LOOP_SOURCE.index("async def _publish_pre_tool_progress_message"):
                       LOOP_SOURCE.index("async def _persist_stream_progress")]


def _interception() -> str:
    start = LOOP_SOURCE.index("# ── Pre-tool progress-message interception ──")
    return LOOP_SOURCE[start:LOOP_SOURCE.index("# ── No tool calls → final response ──")]


def test_published_plan_is_main_phase_and_durably_marked():
    helper = _publish_helper()
    assert 'message_phase="main"' in helper
    assert 'meta["pre_tool_progress_message_sent"] = True' in helper
    # This row is NOT the run's answer, so it must not consume the one-promotion
    # latch — the run still promotes its own terminal message later.
    # No string-literal write of the one-promotion latch — a docstring mention is
    # fine, an assignment would not be.
    assert '"closing_promoted"' not in helper
    assert 'status="complete"' in helper


def test_interception_publishes_then_continues_before_the_work_policy_branch():
    block = _interception()
    assert '_contract_state.get("pre_tool_progress_message_pending")' in block
    assert "_publish_pre_tool_progress_message(_final_content)" in block
    # The window must close BEFORE publishing so the gate can never re-ask.
    assert block.index('pre_tool_progress_message_done"] = True') < block.index(
        "_publish_pre_tool_progress_message(_final_content)")
    # The run continues: the progress update is the notice the gate asked for, not an answer.
    assert "continue" in block
    assert "Do not wait for approval" in block
    # Placed before the prose-is-a-protocol-violation branches, or the progress update would
    # be read as a violation instead of a progress update.
    # Compare against the NEXT use after the marker: the helper is defined earlier
    # in the file, so a first-occurrence check would be meaningless.
    _intercept_at = LOOP_SOURCE.index("# ── Pre-tool progress-message interception ──")
    assert _intercept_at < LOOP_SOURCE.index("_contains_unparsed_tool_call", _intercept_at)


def test_cleanup_final_stands_down_for_a_pending_progress_update():
    """The announcement asks for a TOOL-FREE reply, which the cleanup-final path
    would publish as the run's answer and then end the run on. It must stand
    down, exactly as it already does for the closing gate."""
    assert 'cleanup_final and _contract_state.get("pre_tool_progress_message_pending")' in LOOP_SOURCE


def test_loop_consults_the_durable_latch_before_asking():
    assert "async def _pre_tool_progress_already_sent" in LOOP_SOURCE
    assert "already_sent(" in LOOP_SOURCE
    assert "has_pre_tool_progress_message" in LOOP_SOURCE
    # The latch is consulted only while the rule can still fire this run.
    assert 'not _contract_state.get("pre_tool_progress_message_done")' in LOOP_SOURCE


def test_announcement_is_resolved_and_handed_to_the_rule():
    assert "pre_tool_progress_message=_pre_tool_progress_message" in LOOP_SOURCE
    assert "from app.agent.pre_tool_progress import (" in LOOP_SOURCE


def test_tool_carrying_response_cannot_publish_or_satisfy_the_gate():
    """The dedicated tool-free interception is the sole opening publisher.

    A response that contains a progress sentence plus any tool call remains an
    activity step. It cannot write the durable opening mark or release pending
    edits in the same response.
    """
    assert "_plan_publish" not in LOOP_SOURCE
    assert "PLAN-ANNOUNCE-STEP" not in LOOP_SOURCE
    assert "Pre-tool progress message on a tool-carrying step" not in LOOP_SOURCE
    helper = _publish_helper()
    assert 'meta["pre_tool_progress_message_sent"] = True' in helper
    assert '_publish_pre_tool_progress_message(_final_content)' in _interception()
    assert "_contract_state.get(\"pre_tool_progress_message_pending\") and _final_content" in _interception()


def test_the_rule_is_given_the_assistant_turn_for_observability_only():
    """The loop continues to pass the turn for diagnostics and compatibility;
    strict gate behavior no longer treats same-turn edits as an exemption."""
    assert "pre_tool_progress_message=_pre_tool_progress_message" in _mechanical_check_call()
    assert "turn=turn_count" in _mechanical_check_call()


def _mechanical_check_call() -> str:
    """The whole ``mechanical_check(...)`` call, paren-balanced — a naive
    first-``)`` slice stops inside the ``execution_mode`` conditional."""
    at = LOOP_SOURCE.index("_contract_msg = mechanical_check(")
    depth = 0
    for i in range(LOOP_SOURCE.index("(", at), len(LOOP_SOURCE)):
        if LOOP_SOURCE[i] == "(":
            depth += 1
        elif LOOP_SOURCE[i] == ")":
            depth -= 1
            if depth == 0:
                return LOOP_SOURCE[at:i + 1]
    raise AssertionError("unterminated mechanical_check call")



