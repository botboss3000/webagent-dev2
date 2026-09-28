"""Focused tests for deterministic task-boundary grouping and diagnostics."""

import json
import sqlite3

from app.admin.tasks import (
    _decide_boundary,
    _load_turns,
    boundary_diagnostic,
)


def test_anaphoric_followup_stays_in_current_task() -> None:
    is_new, reason, signals = _decide_boundary({
        "prompt": "What does that imply for someone setting up the project locally?",
    })

    assert is_new is False
    assert reason == "anaphoric continuation"
    assert signals["is_anaphoric_continuation"] is True


def test_explicit_unrelated_request_still_opens_new_task() -> None:
    is_new, reason, signals = _decide_boundary({
        "prompt": "Write an unrelated poem about the sea.",
    })

    assert is_new is True
    assert reason == "new request"
    assert signals["ambiguous"] is True


def test_boundary_diagnostic_marks_ambiguous_decisions_provisional() -> None:
    diagnostic = boundary_diagnostic("Write an unrelated poem about the sea.")

    assert diagnostic["is_new"] is True
    assert diagnostic["provisional"] is True
    assert diagnostic["signals"]["ambiguous"] is True


def test_persisted_boundary_diagnostic_is_exposed_on_reconstructed_turn() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE interactions (
               id TEXT, role TEXT, content TEXT, tool_name TEXT, metadata TEXT,
               created_at TEXT, session_seq INTEGER, source TEXT, session_id TEXT
           )"""
    )
    diagnostic = boundary_diagnostic("What does that imply?")
    conn.execute(
        "INSERT INTO interactions VALUES (?,?,?,?,?,?,?,?,?)",
        (
            "u1", "user", "What does that imply?", None,
            json.dumps({"task_boundary": diagnostic}),
            "2026-08-25T12:00:00Z", 1, None, "s1",
        ),
    )

    turns = _load_turns(conn, "s1")

    assert turns[0]["boundary_diagnostic"] == diagnostic
