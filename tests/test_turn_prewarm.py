"""Correctness boundaries for the per-session chat prewarm cache."""

from app.agent import turn_prewarm


def setup_function() -> None:
    turn_prewarm._BUNDLES.clear()
    turn_prewarm._LAST_TURN_DONE.clear()


def test_new_task_reuses_reads_but_discards_message_agnostic_history() -> None:
    turn_prewarm.store(
        "session-1",
        sig=("user-1", "agent-1", "v1"),
        tools=[{"name": "read_file"}],
        history=[{"role": "tool", "content": "large prior-task result"}],
    )

    bundle = turn_prewarm.consume(
        "session-1",
        sig=("user-1", "agent-1", "v1"),
        pending_starts_new_task=True,
    )

    assert bundle is not None
    assert bundle["tools"] == [{"name": "read_file"}]
    assert bundle["history"] is None
    assert bundle["history_reusable"] is False

    # Consuming is non-destructive: a continuation/retry can still use the
    # original bundle until the completed turn invalidates it.
    continuation = turn_prewarm.consume(
        "session-1", sig=("user-1", "agent-1", "v1")
    )
    assert continuation is not None
    assert continuation["history"][0]["content"] == "large prior-task result"


def test_continuation_keeps_prewarmed_history() -> None:
    history = [{"role": "assistant", "content": "current task"}]
    turn_prewarm.store(
        "session-2",
        sig=("user-1", "agent-1", "v1"),
        tools=[],
        history=history,
    )

    bundle = turn_prewarm.consume(
        "session-2",
        sig=("user-1", "agent-1", "v1"),
        pending_starts_new_task=False,
    )

    assert bundle is not None
    assert bundle["history"] is history
