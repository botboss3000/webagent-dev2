import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace

import app.abilities
import app.db
from app.agent import output_closer


class _AgentDB:
    def __init__(self, metadata):
        self.metadata = metadata
        self.agent_reads = 0
        self.insert_calls = 0

    async def get_agent_by_id(self, _agent_id):
        self.agent_reads += 1
        return {"id": "agent-1", "metadata": json.dumps(self.metadata)}

    async def get_session_execution_mode(self, _session_id):
        return None

    async def insert_interaction(self, *_args, **_kwargs):
        self.insert_calls += 1
        raise AssertionError("disabled Closer must not persist an interaction")


def _closing_gate(enabled: bool) -> dict:
    return {"gates": {"schema_version": 1, "gates": [{
        "type": "closing_message", "enabled": True,
        "params": {"output_closer_enabled": enabled},
    }]}}


def test_closing_message_gate_is_the_only_agent_activation_authority():
    assert not output_closer._closing_output_enabled(None, None)
    assert not output_closer._closing_output_enabled({"metadata": "{}"}, None)
    # Legacy Manager and Codex switches cannot enable or disable Output Closer.
    enabled = _closing_gate(True) | {"manager": {"enabled": False, "closer": {"enabled": False}},
                                     "codex_code": {"closer_enabled": False}}
    disabled = _closing_gate(False) | {"manager": {"enabled": True, "closer": {"enabled": True}},
                                       "codex_code": {"closer_enabled": True}}
    assert output_closer._closing_output_enabled(
        {"id": "agent-1", "metadata": json.dumps(enabled)}, None)
    assert not output_closer._closing_output_enabled(
        {"id": "agent-1", "metadata": json.dumps(disabled)}, None)


def test_disabled_agent_skips_live_closer_before_any_run_work(monkeypatch):
    db = _AgentDB(_closing_gate(False))
    monkeypatch.setattr(app.abilities, "app_function_enabled", lambda _name: True)
    monkeypatch.setattr(
        output_closer,
        "_run_stopped_by_user",
        lambda *_args: (_ for _ in ()).throw(AssertionError("run work started")),
    )

    asyncio.run(output_closer.run_output_closer(
        user_id="user-1",
        session_id="session-1",
        agent_id="agent-1",
        final_asst_id="assistant-1",
        db=db,
    ))

    assert db.agent_reads == 1
    assert db.insert_calls == 0


def test_disabling_during_llm_suppresses_summary_persistence(monkeypatch):
    class _LiveToggleDB(_AgentDB):
        async def get_agent_by_id(self, _agent_id):
            self.agent_reads += 1
            enabled = self.agent_reads == 1
            return {
                "id": "agent-1",
                "metadata": json.dumps({
                    **_closing_gate(enabled),
                }),
            }

    db = _LiveToggleDB({})

    async def _resolve(_user_id=None):
        return "model", "provider", object()

    async def _attempt(*_args, **_kwargs):
        return "finished summary", [], object()

    async def _note(*_args, **_kwargs):
        return None

    monkeypatch.setattr(app.abilities, "app_function_enabled", lambda _name: True)
    monkeypatch.setattr(output_closer, "_run_stopped_by_user", lambda *_args: False)
    monkeypatch.setattr(
        output_closer, "_resolve_original_parent", lambda _db, parent_id: parent_id)
    monkeypatch.setattr(
        output_closer,
        "_collect_span_messages",
        lambda *_args: (["Assistant: done"], "request", [{"role": "assistant"}]),
    )
    monkeypatch.setattr(output_closer, "_resolve_fast_llm", _resolve)
    monkeypatch.setattr(output_closer, "_resolve_audit_config", lambda _rec: (None, 0, False))
    monkeypatch.setattr(output_closer, "_attempt_closer_call", _attempt)
    monkeypatch.setattr(output_closer, "_emit_progress_note", _note)

    asyncio.run(output_closer.run_output_closer(
        user_id="user-1",
        session_id="session-1",
        agent_id="agent-1",
        final_asst_id="assistant-1",
        parent_interaction_id="user-message-1",
        db=db,
    ))

    assert db.agent_reads >= 2
    assert db.insert_calls == 0


def test_disabled_agent_suppresses_audit_send_back(monkeypatch):
    class _AuditDB(_AgentDB):
        async def next_session_seq(self, *_args):
            raise AssertionError("disabled audit must not allocate a sequence")

    db = _AuditDB(_closing_gate(False))

    asyncio.run(output_closer._send_audit_back(
        db=db,
        user_id="user-1",
        session_id="session-1",
        agent_id="agent-1",
        channel=None,
        final_asst_id="assistant-1",
        feedback="finish it",
        missing=["tests"],
        round_no=1,
        max_rounds=2,
    ))

    assert db.agent_reads == 1
    assert db.insert_calls == 0


def test_subscription_standard_role_resolves_closer_transport(monkeypatch):
    from app.admin import settings

    async def _config(_user_id):
        return {"multi_providers": []}

    standard = {
        "provider": "openai_subscription",
        "model": "gpt-5.6-terra",
        "credential_scope": "platform",
        "entry_id": "entry-1",
    }
    monkeypatch.setattr(settings, "_resolve_user_config", _config)
    monkeypatch.setattr(settings, "_assign_slots", lambda _entries: {
        "roles": {"standard": standard},
    })
    monkeypatch.setattr(output_closer, "_CONFIG_CACHE", None)

    model, provider, client = asyncio.run(output_closer._resolve_fast_llm("user-1"))

    assert model == "gpt-5.6-terra"
    assert provider == "openai_subscription"
    from app.agent.provider_completion import SubscriptionResponseClient
    assert isinstance(client, SubscriptionResponseClient)
    assert client.entry_id == "entry-1"
    assert client.user_id == "user-1"


def test_subscription_closer_completion_uses_responses_transport(monkeypatch):
    from app import openai_subscription
    from app.agent import response_chaining

    captured = {}

    class _Stream:
        def __init__(self):
            self.closed = False
            self.chunks = [SimpleNamespace(choices=[SimpleNamespace(
                delta=SimpleNamespace(content="subscription summary"),
            )])]

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.chunks:
                return self.chunks.pop(0)
            raise StopAsyncIteration

        async def close(self):
            self.closed = True

    stream = _Stream()

    async def _auth(**kwargs):
        captured["auth"] = kwargs
        return {"access_token": "token", "account_id": "account", "residency": "us"}

    async def _open(**kwargs):
        captured["open"] = kwargs
        return stream

    monkeypatch.setattr(openai_subscription, "transport_auth", _auth)
    monkeypatch.setattr(response_chaining, "open_responses_stream", _open)

    from app.agent.provider_completion import SubscriptionResponseClient, complete_text
    result = asyncio.run(complete_text(
        SubscriptionResponseClient(
            scope="platform", entry_id="entry-1", user_id="user-1", agent_id=""),
        model="gpt-5.6-terra", messages=[{"role": "user", "content": "Summarize this"}],
        max_tokens=100, temperature=0.2, timeout=30.0,
    ))

    assert result.choices[0].message.content == "subscription summary"
    assert stream.closed is True
    assert captured["auth"]["entry_id"] == "entry-1"
    assert captured["open"]["request_url"] == openai_subscription.RESPONSES_ENDPOINT
    assert captured["open"]["body"]["store"] is False
    assert "max_output_tokens" not in captured["open"]["body"]
    assert captured["open"]["extra_headers"]["ChatGPT-Account-Id"] == "account"


def test_recovery_sweep_skips_disabled_agents(monkeypatch):
    db = _AgentDB(_closing_gate(False))
    closer_calls = []

    async def _resolve(_user_id=None):
        return "model", "provider", object()

    async def _run(**kwargs):
        closer_calls.append(kwargs)

    monkeypatch.setattr(app.db, "get_db", lambda: db)
    monkeypatch.setattr(output_closer, "_resolve_fast_llm", _resolve)
    monkeypatch.setattr(output_closer, "run_output_closer", _run)
    monkeypatch.setattr(output_closer, "_find_final_rows_without_summary", lambda *_args: [{
        "id": "assistant-1",
        "session_id": "session-1",
        "user_id": "user-1",
        "agent_id": "agent-1",
        "parent_id": "user-message-1",
        "metadata": "{}",
    }])

    attempted = asyncio.run(output_closer._sweep_once())

    assert attempted == 0
    assert db.agent_reads == 1
    assert closer_calls == []


def test_recovery_sweep_does_not_preflight_raw_api_key_client(monkeypatch):
    db = _AgentDB(_closing_gate(True))
    closer_calls = []

    async def _must_not_resolve(*_args, **_kwargs):
        raise AssertionError("recovery must defer model resolution to the closer")

    async def _run(**kwargs):
        closer_calls.append(kwargs)

    monkeypatch.setattr(app.db, "get_db", lambda: db)
    monkeypatch.setattr(output_closer, "_resolve_fast_llm", _must_not_resolve)
    monkeypatch.setattr(output_closer, "run_output_closer", _run)
    monkeypatch.setattr(output_closer, "_find_final_rows_without_summary", lambda *_args: [{
        "id": "assistant-1",
        "session_id": "session-1",
        "user_id": "user-1",
        "agent_id": "agent-1",
        "parent_id": "user-message-1",
        "metadata": "{}",
    }])

    attempted = asyncio.run(output_closer._sweep_once())

    assert attempted == 1
    assert [call["final_asst_id"] for call in closer_calls] == ["assistant-1"]


class _DurableToggleDB(_AgentDB):
    def __init__(self, path):
        super().__init__(_closing_gate(False))
        self.path = str(path)

    def _get_conn(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn


def _seed_recovery_candidate(db):
    conn = db._get_conn()
    conn.executescript(
        """
        CREATE TABLE sessions (id TEXT PRIMARY KEY, user_id TEXT, agent_id TEXT);
        CREATE TABLE session_runs (
          assistant_interaction_id TEXT, stop_cause TEXT
        );
        CREATE TABLE interactions (
          id TEXT PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,
          parent_id TEXT, channel TEXT, metadata TEXT, created_at TEXT,
          source TEXT, status TEXT
        );
        INSERT INTO sessions VALUES ('session-1','user-1','agent-1');
        INSERT INTO interactions VALUES (
          'assistant-1','session-1','assistant','done','user-1',NULL,
          '{"message_phase": "final"}',datetime('now','-1 hour'),
          'agent','complete'
        );
        """
    )
    conn.commit()
    conn.close()


def test_recovery_finder_skips_cooldown_rows_before_limit(tmp_path):
    db = _DurableToggleDB(tmp_path / "closer-cooldown.db")
    _seed_recovery_candidate(db)
    conn = db._get_conn()
    now = datetime.now(timezone.utc).isoformat()
    cooldown_metadata = json.dumps({
        "message_phase": "final",
        "summary_attempt_at": now,
    })
    conn.execute(
        "UPDATE interactions SET metadata = ? WHERE id = 'assistant-1'",
        (cooldown_metadata,),
    )
    for index in range(9):
        conn.execute(
            """INSERT INTO interactions VALUES (?, 'session-1', 'assistant', 'done',
               'user-1', NULL, ?, datetime('now','-1 hour'), 'agent', 'complete')""",
            (
                f"cooldown-{index:02d}",
                json.dumps({
                    "message_phase": "final",
                    "summary_attempt_at": now,
                }),
            ),
        )
    conn.execute(
        """INSERT INTO interactions VALUES ('eligible-1', 'session-1', 'assistant', 'done',
           'user-1', NULL, '{\"message_phase\": \"final\"}',
           datetime('now','-1 hour'), 'agent', 'complete')"""
    )
    conn.commit()
    conn.close()

    candidates = output_closer._find_final_rows_without_summary(db, 1)

    assert [row["id"] for row in candidates] == ["eligible-1"]


def test_disabled_recovery_candidate_is_durably_ineligible_after_reenable(
    monkeypatch, tmp_path,
):
    db = _DurableToggleDB(tmp_path / "closer-disabled.db")
    _seed_recovery_candidate(db)

    async def _resolve(_user_id=None):
        return "model", "provider", object()

    real_finder = output_closer._find_final_rows_without_summary
    monkeypatch.setattr(app.db, "get_db", lambda: db)
    monkeypatch.setattr(output_closer, "_resolve_fast_llm", _resolve)
    monkeypatch.setattr(output_closer, "_find_final_rows_without_summary", lambda *_args: [{
        "id": "assistant-1",
        "session_id": "session-1",
        "user_id": "user-1",
        "agent_id": "agent-1",
        "parent_id": "user-1",
        "metadata": '{"message_phase": "final"}',
    }])

    assert asyncio.run(output_closer._sweep_once()) == 0
    conn = db._get_conn()
    stamped = json.loads(conn.execute(
        "SELECT metadata FROM interactions WHERE id='assistant-1'"
    ).fetchone()["metadata"])
    conn.close()
    assert stamped["closer_skipped_disabled"] is True

    # Once enabled, the real recovery query still excludes the intentional
    # no-Closer turn, rather than backfilling it later.
    db.metadata = _closing_gate(True)
    assert real_finder(db, 10) == []
