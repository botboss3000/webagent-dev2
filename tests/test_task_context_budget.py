from app.agent.context_control import history_budget, bound_history, estimate_tokens, context_composition, ContextBudgetExceeded
from app.agent.session_history import trim_history_for_resume, build_reseed_context
import pytest


def test_request_budget_reserves_instructions_schemas_state_and_response():
    assert history_budget({"token_limit": 8000, "compact_target_tokens": 8000},
                          mandatory_tokens=1000, instruction_tokens=500,
                          tool_schema_tokens=500, response_tokens=1000) == 5000
    with pytest.raises(ContextBudgetExceeded):
        history_budget({"token_limit": 1000}, mandatory_tokens=1000)


def test_history_selection_preserves_task_and_whole_exchange():
    state = {"role": "system", "name": "webagent_task_state", "content": "requirements and next step"}
    history = [state, {"role": "user", "content": "old " * 1000},
               {"role": "assistant", "content": "", "tool_calls": [{"id": "c", "function": {"name": "read", "arguments": "{}"}}]},
               {"role": "tool", "tool_call_id": "c", "content": "result " * 1000},
               {"role": "user", "content": "continue"}]
    selected = bound_history(history, 200)
    assert state in selected
    assert selected[-1]["content"] == "continue"
    assert not any(m.get("tool_calls") or m.get("role") == "tool" for m in selected)
    assert estimate_tokens(selected) <= 200


def test_resume_and_reseed_preserve_all_mandatory_state():
    state = {"role": "system", "name": "webagent_task_state", "content": "REQ " * 2000}
    summary = {"role": "system", "content": "old summary"}
    history = [state, summary] + [{"role": "user", "content": str(i)} for i in range(50)]
    assert trim_history_for_resume(history, max_tail_messages=2)[:2] == [state, summary]
    assert state["content"] in build_reseed_context(history)


def test_required_request_is_never_silently_cut():
    with pytest.raises(ContextBudgetExceeded):
        bound_history([{"role": "user", "content": "x" * 1000}], 20)


def test_persisted_ask_contract_recovers_without_any_transcript(tmp_path):
    import asyncio
    import sqlite3
    from app.agent.task_state import begin_task, update_checkpoint
    from app.agent.session_history import build_openai_history_from_session
    from plugins.engines.codex.context_packet import build_context_packet

    class DB:
        def _get_conn(self):
            return sqlite3.connect(tmp_path / "synthetic-task.db")

        async def count_interactions(self, user_id, session_id):
            return 0

        async def fetch_interactions(self, user_id, session_id):
            return []

    db = DB()
    state = begin_task(db, session_id="s", turn_id="t", task_id="task",
                       source_messages=[{"interaction_id": "request", "content": "Explain the cache and its invalidation."}], mode="ask")
    update_checkpoint(db, "s", "t", state["revision"], {
        "steps": [{"id": "P1", "text": "Explain both behaviors", "requirement_ids": ["R1"],
                   "verification": "Answer covers cache and invalidation", "status": "in_progress"}],
        "active_step": "P1", "next_action": "Verify invalidation behavior",
    })
    history = asyncio.run(build_openai_history_from_session(db, "u", "s"))
    assert len(history) == 1
    assert history[0]["name"] == "webagent_task_state"
    assert "Explain the cache and its invalidation." in history[0]["content"]
    assert "Verify invalidation behavior" in history[0]["content"]
    packet = asyncio.run(build_context_packet(db, "u", "s", history=[], max_chars=4000))
    assert "Explain the cache and its invalidation." in packet.text
    assert "Verify invalidation behavior" in packet.text


def test_rolling_summary_is_optional_supporting_memory():
    from app.agent.compaction import render_segment_message
    summary = render_segment_message({"summary": "old " * 1000, "end_index": 20}, 1, 1)
    assert summary["name"] == "webagent_history_summary"
    state = {"role": "system", "name": "webagent_task_state", "content": "Required next step"}
    result = bound_history([state, summary, {"role": "user", "content": "continue"}], 100)
    assert state in result
    assert summary not in result


def test_reasoning_replay_is_counted_and_exposed_separately():
    messages = [
        {"role": "system", "content": "base " * 20},
        {"role": "system", "name": "webagent_task_state", "content": "state " * 20},
        {"role": "assistant", "content": "answer", "reasoning_content": "think " * 400},
        {"role": "user", "content": "continue"},
    ]
    without_reasoning = [dict(m) for m in messages]
    without_reasoning[2].pop("reasoning_content")
    assert estimate_tokens(messages) > estimate_tokens(without_reasoning)
    composition = context_composition(messages, verbatim_tail_tokens=20)
    assert composition["base_prompt_tokens"] > 0
    assert composition["runtime_state_tokens"] > 0
    assert composition["reasoning_tokens"] > 0
    assert composition["estimate_total"] == sum(composition[key] for key in (
        "base_prompt_tokens", "runtime_state_tokens", "reasoning_tokens",
        "compactable_tokens", "verbatim_tokens"))
