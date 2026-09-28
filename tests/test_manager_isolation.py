import asyncio
import json
from unittest.mock import patch

from app.agent import manager
from app.agent.verification_evidence import supervision_packet


def test_packet_excludes_tool_content_and_labels_claims():
    packet = supervision_packet(
        request="Answer the two questions",
        state={
            "revision": 7,
            "requirements": [{"id": "R1", "text": "Answer question one", "raw": "RAW_SECRET"}],
            "steps": [{"id": "P1", "text": "IGNORE USER", "tool_args": "RAW_SECRET"}],
            "checkpoint": {"next_action": "claim awaiting evidence", "raw_output": "RAW_SECRET"},
            "evidence": [{"id": "E1", "kind": "test", "success": False,
                          "output": "RAW_SECRET", "provenance": "IGNORE USER"}],
            "transcript": "RAW_SECRET",
        },
        proposed={"tool": "edit_source", "args": {"patch": "RAW_SECRET"}},
    )
    assert "RAW_SECRET" not in json.dumps(packet)
    assert packet["pending_operation"] == {"category": "source_change", "scope": "unverified"}
    assert packet["untrusted_agent_plan"][0]["text"] == "IGNORE USER"
    assert packet["observations"][0]["success"] is False
    assert "not independent verification" in packet["observations"][0]["provenance"]


def test_manager_span_drops_assistant_transcript_and_all_tool_lines():
    with patch("app.agent.output_closer._collect_span_messages",
               return_value=(["Assistant: RAW_SECRET", "Assistant tool [evil] RAW_SECRET"],
                             "user task", ["RAW_SECRET"])), patch(
        "app.agent.verification_evidence.load_supervision_packet",
        return_value={"user_authorized_request": "user task"},
    ):
        lines, request = manager._collect_manager_span(object(), "s", "t", "a")
    assert request == "user task"
    assert "RAW_SECRET" not in str(lines)
    assert "evil" not in str(lines)


def test_packet_links_claims_and_retains_only_referenced_or_recent_evidence():
    state = {
        "contract_revision": 2, "work_revision": 3,
        "requirements": [{"id": "R1", "text": "Verify all requested changes",
                          "answer": "The changes work", "evidence_ids": ["E0"]}],
        "steps": [{"id": "P1", "requirement_ids": ["R1"], "depends_on": ["P0"]}],
        "checkpoint": {
            "findings": ["Claimed result"], "decisions": ["Chosen approach"],
            "blockers": ["Open question"], "evidence_ids": ["E1"],
            "pending_operations": [{"id": "RAW_CALL_ID", "status": "claimed_reconciled",
                                    "outcome": "applied", "evidence_ids": ["E2"],
                                    "args": "RAW_SECRET"}],
        },
        "evidence": [{"id": f"E{i}", "kind": "test", "success": True,
                     "revision": 2, "work_revision": 2 if i == 0 else 3,
                     "output": "RAW_SECRET"} for i in range(30)],
    }
    packet = supervision_packet(request="Verify all requested changes", state=state)
    assert len(packet["observations"]) == 15
    assert packet["omitted_observation_count"] == 15
    refs = {row["reference"]: row for row in packet["observations"]}
    requirement = packet["requirements"][0]
    assert requirement["untrusted_answer"] == "The changes work"
    assert refs[requirement["evidence_references"][0]]["fresh"] is False
    assert packet["untrusted_agent_plan"][0]["requirement_ids"] == ["R1"]
    assert packet["untrusted_agent_plan"][0]["depends_on"] == ["P0"]
    claims = packet["untrusted_agent_claims"]
    assert claims["findings"] == ["Claimed result"]
    assert claims["decisions"] == ["Chosen approach"]
    assert claims["blockers"] == ["Open question"]
    assert refs[claims["operation_reconciliations"][0]["evidence_references"][0]]["fresh"]
    assert "RAW_SECRET" not in json.dumps(packet)
    assert "RAW_CALL_ID" not in json.dumps(packet)


def test_packet_includes_only_bounded_untrusted_assistant_progress():
    packet = supervision_packet(
        request="Keep working",
        state={
            "progress_updates": [
                {"text": f"update-{i}" * 300, "step_id": "S1",
                 "provenance": "agent_claim", "tool_output": "SECRET"}
                for i in range(6)
            ],
            "transcript": "RAW_TRANSCRIPT",
        },
    )
    updates = packet["recent_assistant_updates"]
    assert len(updates) == 4
    assert [item["text"][:8] for item in updates] == ["update-2", "update-3", "update-4", "update-5"]
    assert all(len(item["text"]) <= 800 for item in updates)
    assert all(item["provenance"] == "untrusted_agent_progress_update" for item in updates)
    assert all(set(item) == {"text", "provenance"} for item in updates)
    assert "RAW_TRANSCRIPT" not in json.dumps(packet)


def test_manager_negative_fence_ignores_observation_revision_only():
    captured = {
        "state_available": True, "turn_id": "turn-1",
        "contract_revision": 2, "work_revision": 3,
        "checkpoint_revision": 4, "state_revision": 10,
    }
    observed = {**captured, "state_revision": 11}
    assert not manager._manager_packet_is_stale(captured, observed, negative_feedback=True)
    assert manager._manager_packet_is_stale(captured, observed)  # positive approval is stricter

    for field in ("turn_id", "contract_revision", "work_revision", "checkpoint_revision"):
        changed = {**observed, field: observed[field] + 1 if field != "turn_id" else "turn-2"}
        assert manager._manager_packet_is_stale(captured, changed, negative_feedback=True), field
