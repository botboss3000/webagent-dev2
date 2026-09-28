import asyncio
import pytest
from datetime import datetime, timezone

from app.models.schemas import InteractionRecord
from plugins.engines.codex.context_packet import (
    PACKET_MAX_CHARS,
    TOOL_ARGUMENT_MAX_CHARS,
    TOOL_RESULT_MAX_CHARS,
    build_context_packet,
    render_context_packet,
)


def test_compaction_car_and_task_hidden_tool_input_are_reduced():
    huge_old_input = "secret-old-input-" + "x" * 20_000
    messages = [
        {"role": "system", "content": "Compaction car: bounded earlier context"},
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "old-call", "type": "function", "function": {
                "name": "read_source", "arguments": huge_old_input,
            },
        }]},
        {"role": "tool", "tool_call_id": "old-call", "content":
            "[tool result hidden — completed in an earlier task]\nTool: read_source"},
        {"role": "user", "content": "continue current task"},
    ]

    packet = render_context_packet(messages)

    assert "Compaction car: bounded earlier context" in packet.text
    assert "[input hidden with completed earlier task]" in packet.text
    assert huge_old_input not in packet.text
    assert "continue current task" in packet.text


def test_large_tool_inputs_results_and_packet_are_bounded():
    messages = []
    for index in range(40):
        messages.extend([
            {"role": "assistant", "content": "", "tool_calls": [{
                "id": f"call-{index}", "type": "function", "function": {
                    "name": "huge", "arguments": "a" * 50_000,
                },
            }]},
            {"role": "tool", "tool_call_id": f"call-{index}",
             "content": "r" * 50_000},
        ])

    packet = render_context_packet(messages)

    assert len(packet.text) <= PACKET_MAX_CHARS + 100
    assert "omitted base64 payload" in packet.text
    assert ("a" * (TOOL_ARGUMENT_MAX_CHARS + 1)) not in packet.text
    assert ("r" * (TOOL_RESULT_MAX_CHARS + 1)) not in packet.text


def test_durable_checkpoint_is_injected_as_machine_context():
    packet = render_context_packet(
        [{"role": "user", "content": "continue"}],
        checkpoint={
            "task_id": "task-1", "status": "needs_input", "revision": 2,
            "checkpoint": {"objective": "ship it", "remaining": ["approval"]},
        },
    )
    assert "Durable task checkpoint" in packet.text
    assert '"task_id":"task-1"' in packet.text
    assert '"remaining":["approval"]' in packet.text


def test_tool_payloads_redact_secrets_and_replace_blobs_with_descriptors():
    base64_blob = "A" * 2_000
    html_blob = "<!doctype html><html><body>" + ("private page " * 100) + "</body></html>"
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "write", "type": "function", "function": {
                "name": "write_file", "arguments": {
                    "path": "notes.txt",
                    "api_key": "sk-do-not-forward",
                    "content": "ordinary but bulky " * 200,
                    "image": "data:image/png;base64," + base64_blob,
                },
            },
        }]},
        {"role": "tool", "tool_call_id": "write", "content": html_blob},
        {"role": "tool", "tool_call_id": "auth", "content":
         "authorization: Bearer bearer-token-do-not-forward password=hunter2"},
    ]

    packet = render_context_packet(messages)

    assert "notes.txt" in packet.text
    assert "sk-do-not-forward" not in packet.text
    assert "hunter2" not in packet.text
    assert "bearer-token-do-not-forward" not in packet.text
    assert base64_blob[:200] not in packet.text
    assert html_blob[:200] not in packet.text
    assert "[REDACTED]" in packet.text
    assert "omitted data URI" in packet.text
    assert "omitted HTML payload" in packet.text
    assert "sha256=" in packet.text


def test_checkpoint_budget_preserves_newest_message_and_exact_telemetry():
    messages = [
        {"role": "user", "content": f"old-{index}-" + "x" * 700}
        for index in range(12)
    ]
    messages.append({"role": "user", "content": "NEWEST-MUST-SURVIVE"})
    packet = render_context_packet(
        messages,
        max_chars=4_000,
        checkpoint={
            "task_id": "task-budget", "status": "working", "revision": 3,
            "checkpoint": {"objective": "finish the request"},
        },
    )

    assert len(packet.text) <= 4_000
    assert "NEWEST-MUST-SURVIVE" in packet.text
    assert len(messages) - 1 in packet.included_message_indexes
    for index in packet.included_message_indexes:
        assert ("NEWEST-MUST-SURVIVE" if index == len(messages) - 1 else f"old-{index}-") in packet.text
    for index in packet.omitted_message_indexes:
        assert f"old-{index}-" not in packet.text


def test_db_builder_excludes_current_user_row():
    now = datetime.now(timezone.utc)
    rows = [
        InteractionRecord(id="old", session_id="session", parent_id=None,
                          role="user", content="prior user", created_at=now),
        InteractionRecord(id="current", session_id="session", parent_id=None,
                          role="user", content="current user", created_at=now),
    ]

    class _Db:
        async def count_interactions(self, _user_id, _session_id):
            return len(rows)

        async def fetch_interactions(self, _user_id, _session_id):
            return rows

    packet = asyncio.run(build_context_packet(
        _Db(), "user", "session", exclude_interaction_ids={"current"},
    ))

    assert "prior user" in packet.text
    assert "current user" not in packet.text


def test_oversized_checkpoint_reports_budget_conflict_without_truncation():
    from app.agent.context_control import ContextBudgetExceeded
    with pytest.raises(ContextBudgetExceeded, match="checkpoint"):
        render_context_packet([], max_chars=4000,
                              checkpoint={"checkpoint": {"objective": "x" * 20000}})


def test_shared_state_survives_fresh_packet_and_overrides_legacy_checkpoint():
    required = "REQUIREMENT-END " * 1000
    packet = render_context_packet([
        {"role": "system", "name": "webagent_task_state", "content": required},
        {"role": "user", "content": "continue"},
    ], max_chars=20000, checkpoint={"checkpoint": {"objective": "stale"}})
    assert required in packet.text
    assert "stale" not in packet.text


def test_packet_reserves_external_request_overhead():
    packet = render_context_packet([
        {"role": "user", "content": "recent " * 1000},
    ], max_chars=10000, reserved_chars=4000)
    assert len(packet.text) <= 6000
