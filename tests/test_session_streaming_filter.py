from app.api.db_viewer import _filter_orphan_streaming_messages


def test_orphan_streaming_rows_are_hidden_when_run_is_terminal():
    messages = [
        {"id": "old", "status": "streaming", "content": "Please"},
        {"id": "done", "status": "complete", "content": "Done"},
    ]

    assert _filter_orphan_streaming_messages(
        messages, {"active": False, "assistant_interaction_id": "old"},
    ) == [messages[1]]


def test_only_current_running_assistant_may_stream():
    messages = [
        {"id": "old", "status": "streaming", "content": "Please"},
        {"id": "live", "status": "streaming", "content": "Working"},
        {"id": "done", "status": "complete", "content": "Done"},
    ]

    assert _filter_orphan_streaming_messages(
        messages, {"active": True, "assistant_interaction_id": "live"},
    ) == [messages[1], messages[2]]


def test_legacy_session_without_run_state_hides_streaming_drafts():
    messages = [
        {"id": "old", "status": "streaming", "content": "Please"},
        {"id": "done", "status": "complete", "content": "Done"},
    ]

    assert _filter_orphan_streaming_messages(messages, None) == [messages[1]]
