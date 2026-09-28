import asyncio
import json
from unittest.mock import AsyncMock

import app.abilities
from app.agent import output_closer


def test_builtin_closer_prompt_preserves_requested_information():
    prompt = output_closer._FALLBACK_CLOSER_PROMPT
    assert "INFORMATION PRESERVATION (highest priority)" in prompt
    assert "Never summarize away" in prompt
    assert "even when the user did not explicitly request 'detail'" in prompt
    custom = output_closer._enforce_closer_response_format("Use a custom agent voice.")
    assert "Use a custom agent voice." in custom
    assert "INFORMATION PRESERVATION (highest priority)" in custom
    assert "Never summarize away" in custom


def test_legacy_compact_custom_prompt_receives_preservation_override():
    legacy = "Use at most 12 words per bullet and no more than 5 bullets."
    enforced = output_closer._enforce_closer_response_format(legacy)
    assert enforced.startswith(legacy)
    assert "INFORMATION PRESERVATION (highest priority)" in enforced
    assert "Concision applies only to unrequested framing" in enforced


def test_lightweight_conversation_gate_is_narrow():
    for request in ("hi", "Hello!", "good morning", "thank you", "sounds good"):
        assert output_closer._is_lightweight_conversation(request)

    for request in ("hi, can you fix this?", "thanks, now deploy it", "good morning report"):
        assert not output_closer._is_lightweight_conversation(request)


def test_reuses_the_only_assistant_response():
    answer = "Hello! How can I help?"
    assert output_closer._reusable_final_response("hi", [answer]) == answer


def test_reuses_explicit_summary_after_progress_messages():
    final = "What changed\n\n- The timeout is now configurable.\n- Tests pass."
    assert output_closer._reusable_final_response(
        "Make the timeout configurable",
        ["I’m locating the configuration path.", "I’m running the tests now.", final],
    ) == final

    inline_summary = "Summary: the timeout is configurable and the tests pass."
    assert output_closer._reusable_final_response(
        "Make the timeout configurable",
        ["I’m updating it now.", inline_summary],
    ) == inline_summary


def test_reuses_self_contained_findings_and_user_handoffs():
    findings = "Findings\n\n- The timeout comes from the worker configuration."
    assert output_closer._reusable_final_response(
        "Investigate the timeout", ["I’m tracing it.", findings]) == findings

    handoff = "Please attach the source workbook so I can preserve its formulas."
    assert output_closer._reusable_final_response(
        "Update my workbook", ["I’m checking the available files.", handoff]) == handoff


def test_does_not_reuse_ordinary_multi_message_progress():
    assert output_closer._reusable_final_response(
        "Investigate the timeout",
        ["I’m checking the worker.", "The worker has a 30 second timeout."],
    ) is None


def test_reuses_a_closing_envelope_verbatim_even_for_a_long_task():
    """A ``closing_message`` run already ends with the WORKING agent's own
    promoted message (statement + FLAG line + call-to-action). The closer lacks
    that context, so a valid envelope must be returned verbatim — never
    paraphrased — while ordinary progress stays eligible for the rewrite.
    (Envelope grammar: app/agent/closing_message.parse_envelope.)"""
    final = "Landed the configurable timeout.\n\nFLAG: complete\nReady to test"
    assert output_closer._reusable_final_response(
        "Make the timeout configurable and deploy it",
        ["I’m locating the configuration path.", "I’m running the tests now.", final],
    ) == final
    assert output_closer._reusable_final_response(
        "Make the timeout configurable and deploy it",
        ["I’m locating the configuration path.", "I’m running the tests now.",
         "The worker timeout comes from the config file."],
    ) is None


def test_fallback_uses_latest_nonempty_working_response():
    assert output_closer._fallback_working_response([
        "Working update.", "", "Final working response.",
    ]) == "Final working response."
    assert output_closer._fallback_working_response(["", "  "]) is None

def test_protects_tables_images_code_links_and_rich_directives_verbatim():
    table = "| Name | Score |\n| --- | ---: |\n| Ada | 98 |\n| Lin | 97 |"
    code = "```python\nprint('exact')\n```"
    image = "![Generated chart](/absolute/chart.png)"
    directive = '::artifact{kind="interactive-table" id="sales-1"}'
    message = (
        f"The analysis is ready.\n\n{table}\n\n{code}\n\n{image}\n\n"
        f"[Download CSV](/absolute/data.csv)\n\n{directive}"
    )

    lines, messages, blocks = output_closer._protect_verbatim_content(
        [f"Assistant: {message}"], [message])

    assert len(blocks) == 5
    assert all(block["token"] in lines[0] for block in blocks)
    assert all(block["token"] in messages[0] for block in blocks)
    assert table not in messages[0]
    assert {block["kind"] for block in blocks} == {
        "data table", "fenced block", "generated image", "link", "rich directive",
    }


def test_restores_verbatim_blocks_once_and_appends_omissions():
    blocks = [
        {"token": "[[CLOSER_VERBATIM_001]]", "kind": "data table", "content": "| A |\n|---|\n| 1 |"},
        {"token": "[[CLOSER_VERBATIM_002]]", "kind": "generated image", "content": "![x](/x.png)"},
    ]
    restored = output_closer._restore_verbatim_content(
        "Key result:\n\n[[CLOSER_VERBATIM_001]]\n[[CLOSER_VERBATIM_001]]",
        blocks,
    )

    assert restored.count("| A |") == 1
    assert restored.count("![x](/x.png)") == 1
    assert "CLOSER_VERBATIM" not in restored


class _ReuseDB:
    def __init__(self):
        self.inserted = []

    async def get_agent_by_id(self, _agent_id):
        return {
            "id": "agent-1",
            "metadata": json.dumps({
                "gates": {"schema_version": 1, "gates": [{
                    "type": "closing_message", "enabled": True,
                    "params": {"output_closer_enabled": True},
                }]},
                "audit_checklist": ["Run the full implementation checklist"],
            }),
        }

    async def insert_interaction(self, *_args, **kwargs):
        self.inserted.append(kwargs)
        return "closer-row-1"


def test_lightweight_turn_reuses_final_without_model_or_auditor(monkeypatch):
    db = _ReuseDB()

    async def _unexpected_model(*_args, **_kwargs):
        raise AssertionError("reused final response must not resolve or call an LLM")

    async def _enabled(*_args, **_kwargs):
        return True

    async def _noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(app.abilities, "app_function_enabled", lambda _name: True)
    monkeypatch.setattr(output_closer, "_run_stopped_by_user", lambda *_args: False)
    monkeypatch.setattr(output_closer, "_resolve_original_parent", lambda _db, parent: parent)
    monkeypatch.setattr(
        output_closer,
        "_collect_span_messages",
        lambda *_args: (
            ["User: hi", "Assistant: I’m getting ready.", "Assistant: Hi!"],
            "hi",
            ["I’m getting ready.", "Hi!"],
        ),
    )
    monkeypatch.setattr(output_closer, "_prepare_codex_checkpoint_target", lambda *_args: None)
    monkeypatch.setattr(output_closer, "_resolve_fast_llm", _unexpected_model)
    monkeypatch.setattr(output_closer, "_attempt_combined_call", _unexpected_model)
    monkeypatch.setattr(output_closer, "_attempt_closer_call", _unexpected_model)
    monkeypatch.setattr(output_closer, "_closing_output_enabled_live", _enabled)
    monkeypatch.setattr(output_closer, "_next_session_seq", lambda *_args: 3)
    monkeypatch.setattr(output_closer, "_final_row_meta", lambda *_args: ("turn-1", 1, 2))
    monkeypatch.setattr(output_closer, "_save_codex_closer_checkpoint", _noop)

    asyncio.run(output_closer.run_output_closer(
        user_id="user-1",
        session_id="session-1",
        agent_id="agent-1",
        final_asst_id="assistant-1",
        parent_interaction_id="user-message-1",
        db=db,
        audit_eligible=True,
    ))

    assert len(db.inserted) == 1
    assert db.inserted[0]["content"] == "Hi!"
    metadata = json.loads(db.inserted[0]["metadata"])
    assert metadata["audit"] == ""
    assert metadata["reused_final_response"] is True


def test_canonical_task_cannot_complete_from_reused_answer_without_review(monkeypatch):
    from app.agent import task_state
    db = _ReuseDB()
    reviews = []

    async def enabled(*_args, **_kwargs):
        return True

    async def no_model(*_args, **_kwargs):
        return "", "", None

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(app.abilities, "app_function_enabled", lambda _name: True)
    monkeypatch.setattr(output_closer, "_run_stopped_by_user", lambda *_args: False)
    monkeypatch.setattr(output_closer, "_resolve_original_parent", lambda _db, parent: parent)
    monkeypatch.setattr(output_closer, "_collect_span_messages",
                        lambda *_args: (["Assistant: Done"], "Answer both questions", ["Done"]))
    monkeypatch.setattr(output_closer, "_prepare_codex_checkpoint_target", lambda *_args: None)
    monkeypatch.setattr(output_closer, "_resolve_fast_llm", no_model)
    monkeypatch.setattr(output_closer, "_closing_output_enabled_live", enabled)
    monkeypatch.setattr(output_closer, "_next_session_seq", lambda *_args: 3)
    monkeypatch.setattr(output_closer, "_final_row_meta", lambda *_args: ("turn-1", 1, 2))
    monkeypatch.setattr(output_closer, "_save_codex_closer_checkpoint", noop)
    monkeypatch.setattr(task_state, "current_state", lambda *_args: {
        "turn_id": "turn-1", "revision": 7,
        "requirements": [{"id": "R1", "text": "Answer both questions"}],
    })
    monkeypatch.setattr(task_state, "completion_gaps", lambda state: [])
    monkeypatch.setattr(task_state, "accept_review",
                        lambda *args: reviews.append(args) or True)
    asyncio.run(output_closer.run_output_closer(
        user_id="user-1", session_id="session-1", agent_id="agent-1",
        final_asst_id="assistant-1", parent_interaction_id="user-message-1",
        db=db, audit_eligible=True,
    ))
    assert reviews[0][2:5] == ("turn-1", 7, "inconclusive")
    assert json.loads(db.inserted[0]["metadata"])["audit"] == "inconclusive"


def test_inconclusive_contract_review_persists_working_response(monkeypatch):
    db = _ReuseDB()
    calls = []

    class _InconclusiveSupervisor:
        def __init__(self, **_kwargs):
            self.config = {"close_review": {"enabled": True, "policy": "blocking"}}

        async def available(self):
            calls.append("available")
            return True

        async def review_close(self, **_kwargs):
            calls.append("review_close")
            return {"decision": "inconclusive", "reason": "reviewer capacity unavailable"}

    async def enabled(*_args, **_kwargs):
        return True

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(app.abilities, "app_function_enabled", lambda _name: True)
    monkeypatch.setattr(output_closer, "_run_stopped_by_user", lambda *_args: False)
    monkeypatch.setattr(output_closer, "_resolve_original_parent", lambda _db, parent: parent)
    monkeypatch.setattr(
        output_closer, "_collect_span_messages",
        lambda *_args: (["User: ship it", "Assistant: Completed the requested change."],
                        "ship it", ["Completed the requested change."]),
    )
    monkeypatch.setattr(output_closer, "_prepare_codex_checkpoint_target", lambda *_args: None)
    monkeypatch.setattr(output_closer, "_closing_output_enabled_live", enabled)
    monkeypatch.setattr(output_closer, "_next_session_seq", lambda *_args: 3)
    monkeypatch.setattr(output_closer, "_final_row_meta", lambda *_args: ("turn-1", 1, 2))
    monkeypatch.setattr(output_closer, "_save_codex_closer_checkpoint", noop)
    monkeypatch.setattr(output_closer, "_emit_progress_note", noop)
    monkeypatch.setattr(output_closer, "_audit_rounds_used", lambda *_args: 0)
    monkeypatch.setattr(
        "app.agent.subagent_contracts.ContractSupervisor", _InconclusiveSupervisor,
    )
    monkeypatch.setattr(
        "app.agent.run_fence.side_effects_allowed", AsyncMock(return_value=True),
    )

    asyncio.run(output_closer.run_output_closer(
        user_id="user-1", session_id="session-1", agent_id="agent-1",
        final_asst_id="assistant-1", parent_interaction_id="user-message-1",
        db=db, audit_eligible=True,
    ))

    assert calls == ["available", "review_close"]
    assert len(db.inserted) == 1
    assert db.inserted[0]["content"] == "Completed the requested change."
    assert json.loads(db.inserted[0]["metadata"])["contract_status"] == "review_skipped"
