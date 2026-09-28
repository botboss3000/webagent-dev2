import asyncio
import json
import sqlite3

import pytest

from plugins.engines.codex import portal
from plugins.engines.codex import portal_store
from plugins.engines.codex.portal_stats import task_stats
from plugins.engines.codex.app_server import CodexAppServer, CodexAppServerError, _find_codex_executable


def test_portal_normalizes_native_threads_and_items():
    result = {
        "thread": {
            "id": "thread-1",
            "turns": [{
                "id": "turn-1",
                "items": [
                    {"id": "u1", "type": "userMessage", "content": [{"type": "text", "text": "hello"}]},
                    {"id": "a1", "type": "agentMessage", "text": "hi"},
                    {"id": "c1", "type": "commandExecution", "command": "pwd", "aggregatedOutput": "repo"},
                ],
            }],
        }
    }
    messages = portal.messages_from_thread(result, "thread-1")
    assert [(row["role"], row["content"]) for row in messages[:2]] == [("user", "hello"), ("assistant", "hi")]
    assert messages[2]["tool_name"] == "command"
    assert messages[2]["output"] == "repo"
    assert json.loads(messages[2]["metadata"])["args"]["command"] == "pwd"
    assert [row["session_seq"] for row in messages] == [1, 2, 3]
    assert all(row["session_id"] == "codex:thread-1" for row in messages)


def test_portal_stats_reads_codex_projections_and_latest_usage(monkeypatch, tmp_path):
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    rollout = codex_home / "rollout.jsonl"
    rollout.write_text("\n".join([
        json.dumps({"type": "event_msg", "payload": {
            "type": "token_count", "info": {
                "total_token_usage": {"input_tokens": 1200, "output_tokens": 45},
                "last_token_usage": {"input_tokens": 321},
                "model_context_window": 258400,
            },
        }}),
        json.dumps({"type": "response_item", "payload": {"type": "message"}}),
    ]), encoding="utf-8")
    with sqlite3.connect(codex_home / "state_5.sqlite") as conn:
        conn.execute("CREATE TABLE threads(id TEXT, rollout_path TEXT, tokens_used INTEGER)")
        conn.execute("INSERT INTO threads VALUES(?,?,?)", ("thread-1", str(rollout), 1245))
    with sqlite3.connect(codex_home / "thread_history_1.sqlite") as conn:
        conn.execute("CREATE TABLE thread_items(thread_id TEXT, item_type TEXT)")
        conn.execute("CREATE TABLE thread_turns(thread_id TEXT, duration_ms INTEGER)")
        conn.executemany("INSERT INTO thread_items VALUES(?,?)", [
            ("thread-1", "userMessage"), ("thread-1", "agentMessage"),
            ("thread-1", "commandExecution"),
        ])
        conn.executemany("INSERT INTO thread_turns VALUES(?,?)", [
            ("thread-1", 1200), ("thread-1", 800),
        ])
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    row = task_stats(["thread-1"])["thread-1"]
    assert row == {
        "total_tokens": 1245,
        "message_count": 2,
        "total_duration_ms": 2000,
        "total_input_tokens": 1200,
        "total_output_tokens": 45,
        "context_tokens": 321,
        "model_context_limit": 258400,
    }


def test_portal_thread_metrics_aggregate_visible_messages_and_duration():
    metrics = portal.thread_metrics({"thread": {"turns": [
        {"durationMs": 125, "items": [
            {"type": "userMessage"}, {"type": "reasoning"}, {"type": "agentMessage"},
        ]},
        {"durationMs": 75, "items": [{"type": "agentMessage"}]},
    ]}})
    assert metrics == {"message_count": 3, "total_duration_ms": 200}


def test_portal_links_live_only_in_plugin_sidecar(monkeypatch, tmp_path):
    sidecar = tmp_path / "engine_state" / "codex" / "portal_links.sqlite"
    monkeypatch.setattr(portal_store, "_DB_PATH", sidecar)
    assert portal_store.add_links("user", "agent", ["one", "two", "one"]) == 2
    assert portal_store.has_link("user", "agent", "one")
    assert {row["thread_id"] for row in portal_store.list_links("user", "agent")} == {"one", "two"}
    assert portal_store.update_link("user", "agent", "one", {"pinned": True, "hidden": True, "title": "ignored"})
    updated = next(row for row in portal_store.list_links("user", "agent") if row["thread_id"] == "one")
    assert updated["pinned"] == 1 and updated["hidden"] == 1
    with sqlite3.connect(sidecar) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == {"portal_links"}
    assert portal_store.remove_link("user", "agent", "one")


def test_portal_engine_guard_prevents_normal_persistence_path():
    from plugins.engines.codex import codex
    cfg = codex._cfg({"metadata": '{"codex_code":{"context_mode":"codex_portal"}}'})
    assert cfg["context_mode"] == "codex_portal"


def test_app_server_reads_paginated_transcript(monkeypatch):
    server = CodexAppServer()
    calls = []

    async def request(method, params, **_kwargs):
        calls.append((method, params))
        if method == "thread/read":
            return {"thread": {"id": "thread-1", "historyMode": "paginated"}}
        return {
            "data": [{
                "id": "turn-1",
                "startedAt": 1_700_000_000,
                "status": "completed",
                "items": [{"id": "a1", "type": "agentMessage", "text": "done"}],
            }],
            "nextCursor": None,
        }

    monkeypatch.setattr(server, "request", request)
    result = asyncio.run(server.read_thread("thread-1"))
    assert result["thread"]["turns"][0]["items"][0]["text"] == "done"
    assert calls[1] == ("thread/turns/list", {
        "threadId": "thread-1", "limit": 100, "sortDirection": "asc", "itemsView": "full",
    })


def test_app_server_reads_thread_summary_without_turns(monkeypatch):
    server = CodexAppServer()
    captured = {}

    async def request(method, params, **_kwargs):
        captured.update({"method": method, "params": params})
        return {"thread": {"id": "thread-1", "title": "Native task"}}

    monkeypatch.setattr(server, "request", request)
    result = asyncio.run(server.read_thread_summary("thread-1"))
    assert result["thread"]["title"] == "Native task"
    assert captured == {
        "method": "thread/read",
        "params": {"threadId": "thread-1", "includeTurns": False},
    }


def test_app_server_pages_the_native_task_catalog(monkeypatch):
    server = CodexAppServer()
    captured = {}

    async def request(method, params, **_kwargs):
        captured.update({"method": method, "params": params})
        return {"data": [], "nextCursor": None}

    monkeypatch.setattr(server, "request", request)
    asyncio.run(server.list_threads(200, cursor="next-page"))
    assert captured == {
        "method": "thread/list",
        "params": {
            "limit": 200,
            "sortKey": "updated_at",
            "sortDirection": "desc",
            "archived": False,
            "cursor": "next-page",
        },
    }


def test_portal_catalog_forwards_cursor_and_marks_promoted_tasks(monkeypatch, tmp_path):
    from plugins.engines import api as engines_api
    from plugins.engines.codex.app_server import app_server

    monkeypatch.setattr(portal_store, "_DB_PATH", tmp_path / "portal-links.sqlite")
    portal_store.add_links("user", "agent", ["thread-1"])

    async def require_admin(_request, user_id):
        return user_id

    async def agent_config(_user_id, _agent_id):
        return {"context_mode": "codex_portal"}

    async def list_threads(limit, cursor=None):
        assert (limit, cursor) == (200, "next-page")
        return {
            "data": [{"id": "thread-1", "title": "Native task"}],
            "nextCursor": "last-page",
        }

    monkeypatch.setattr(engines_api, "_require_portal_admin", require_admin)
    monkeypatch.setattr(engines_api, "_portal_agent_config", agent_config)
    monkeypatch.setattr(app_server, "list_threads", list_threads)
    result = asyncio.run(engines_api.codex_portal_candidates(
        object(), "user", "agent", 200, "next-page"
    ))
    assert result["threads"][0]["id"] == "codex:thread-1"
    assert result["threads"][0]["linked"] is True
    assert result["next_cursor"] == "last-page"


def test_unpromoted_native_task_can_open_in_agent_catalog(monkeypatch):
    from plugins.engines import api as engines_api
    from plugins.engines.codex.app_server import app_server

    async def require_admin(_request, user_id):
        return user_id

    async def agent_config(_user_id, _agent_id):
        return {"context_mode": "codex_portal"}

    async def read_thread(thread_id):
        assert thread_id == "thread-1"
        return {"thread": {"id": thread_id, "turns": []}}

    monkeypatch.setattr(engines_api, "_require_portal_admin", require_admin)
    monkeypatch.setattr(engines_api, "_portal_agent_config", agent_config)
    monkeypatch.setattr(
        engines_api, "_ensure_link",
        lambda *_args: (_ for _ in ()).throw(AssertionError("catalog reads must not require promotion")),
    )
    monkeypatch.setattr(app_server, "read_thread", read_thread)
    result = asyncio.run(engines_api.codex_portal_messages(
        "codex:thread-1", object(), "user", "agent"
    ))
    assert result["messages"] == []
    assert result["manifest"]["external_authority"] == "codex"


def test_portal_turn_reports_native_queue_acceptance(monkeypatch):
    from plugins.engines import api as engines_api
    from plugins.engines.codex.app_server import app_server

    async def require_admin(_request, user_id):
        return user_id

    async def agent_config(_user_id, _agent_id):
        return {"context_mode": "codex_portal"}

    async def run_turn(*_args, **_kwargs):
        return {"queued": True, "queue": {"queuedSubmission": {"id": "queued-1"}}}

    async def read_thread(thread_id):
        return {"thread": {"id": thread_id, "turns": []}}

    monkeypatch.setattr(engines_api, "_require_portal_admin", require_admin)
    monkeypatch.setattr(engines_api, "_portal_agent_config", agent_config)
    monkeypatch.setattr(app_server, "run_turn", run_turn)
    monkeypatch.setattr(app_server, "read_thread", read_thread)
    body = engines_api.CodexPortalTurnRequest(
        user_id="user", agent_id="agent", message="follow up"
    )
    result = asyncio.run(engines_api.codex_portal_turn(
        "codex:thread-1", body, object()
    ))
    assert result["status"] == "queued"


def test_app_server_starts_native_portal_thread(monkeypatch):
    server = CodexAppServer()
    captured = {}

    async def request(method, params, **_kwargs):
        captured.update({"method": method, "params": params})
        return {"thread": {"id": "thread-1"}}

    monkeypatch.setattr(server, "request", request)
    asyncio.run(server.start_thread(cwd="C:/repo", model="gpt-test", execution_mode="wkspc"))
    assert captured["method"] == "thread/start"
    assert captured["params"]["sandbox"] == "workspace-write"
    assert captured["params"]["historyMode"] == "paginated"


def test_first_portal_turn_does_not_resume_thread_without_rollout(monkeypatch):
    server = CodexAppServer()
    calls = []

    async def request(method, params, **_kwargs):
        calls.append((method, params))
        if method == "turn/start":
            asyncio.get_running_loop().call_soon(
                server._thread_events["thread-1"][0].put_nowait,
                {"method": "turn/completed", "params": {"threadId": "thread-1", "turn": {"id": "turn-1"}}},
            )
            return {"turn": {"id": "turn-1"}}
        if method == "thread/unsubscribe":
            return {"status": "unsubscribed"}
        raise AssertionError(method)

    monkeypatch.setattr(server, "request", request)
    asyncio.run(server.run_turn("thread-1", "hello"))
    assert [method for method, _params in calls] == ["turn/start", "thread/unsubscribe"]


@pytest.mark.parametrize("missing_error", [
    "thread not loaded: thread-1",
    "{'code': -32600, 'message': 'thread not found: thread-1'}",
])
def test_existing_unloaded_portal_thread_resumes_then_retries(monkeypatch, missing_error):
    server = CodexAppServer()
    calls = []
    starts = 0

    async def request(method, params, **_kwargs):
        nonlocal starts
        calls.append((method, params))
        if method == "turn/start":
            starts += 1
            if starts == 1:
                raise CodexAppServerError(missing_error)
            asyncio.get_running_loop().call_soon(
                server._thread_events["thread-1"][0].put_nowait,
                {"method": "turn/completed", "params": {"threadId": "thread-1", "turn": {"id": "turn-2"}}},
            )
            return {"turn": {"id": "turn-2"}}
        if method == "thread/resume":
            return {"thread": {"id": "thread-1"}}
        if method == "thread/unsubscribe":
            return {"status": "unsubscribed"}
        raise AssertionError(method)

    monkeypatch.setattr(server, "request", request)
    asyncio.run(server.run_turn("thread-1", "hello"))
    assert [method for method, _params in calls] == ["turn/start", "thread/resume", "turn/start", "thread/unsubscribe"]
    assert calls[1][1] == {"threadId": "thread-1", "excludeTurns": True}


def test_active_writer_portal_thread_queues_follow_up(monkeypatch):
    server = CodexAppServer()
    calls = []

    async def request(method, params, **_kwargs):
        calls.append((method, params))
        if method == "turn/start":
            raise CodexAppServerError("thread not found: thread-1")
        if method == "thread/resume":
            raise CodexAppServerError("thread thread-1 already has an active writer")
        if method == "thread/queue/add":
            return {"queuedSubmission": {"id": "queued-1"}}
        raise AssertionError(method)

    monkeypatch.setattr(server, "request", request)
    result = asyncio.run(server.run_turn("thread-1", "follow up"))
    assert result["queued"] is True
    assert [method for method, _params in calls] == [
        "turn/start", "thread/resume", "thread/queue/add",
    ]
    assert calls[2][1]["threadId"] == "thread-1"
    assert calls[2][1]["input"] == [{"type": "text", "text": "follow up"}]
    assert calls[2][1]["clientUserMessageId"]


def test_desktop_codex_is_preferred_over_path(monkeypatch, tmp_path):
    desktop = tmp_path / "OpenAI" / "Codex" / "bin" / "current" / "codex.exe"
    desktop.parent.mkdir(parents=True)
    desktop.write_bytes(b"codex")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.delenv("WEBAGENT_CODEX_PATH", raising=False)
    monkeypatch.setattr("plugins.engines.codex.app_server.shutil.which", lambda _name: "old-codex")
    assert _find_codex_executable() == str(desktop)


def test_app_server_launches_with_code_mode_host(monkeypatch):
    server = CodexAppServer()
    launched = []

    class Stream:
        async def readline(self):
            await asyncio.sleep(3600)

    class Process:
        returncode = None
        stdin = object()
        stdout = Stream()
        stderr = Stream()

    async def create_subprocess_exec(*args, **kwargs):
        launched.append((args, kwargs))
        return Process()

    async def request(method, params, **_kwargs):
        assert method == "initialize"
        return {}

    async def notify(method, params, **_kwargs):
        assert method == "initialized"

    monkeypatch.setattr("plugins.engines.codex.app_server._find_codex_executable", lambda: "codex")
    monkeypatch.setattr("plugins.engines.codex.app_server.asyncio.create_subprocess_exec", create_subprocess_exec)
    monkeypatch.setattr(server, "request", request)
    monkeypatch.setattr(server, "notify", notify)

    async def run():
        await server._ensure_started()
        server._reader_task.cancel()
        server._stderr_task.cancel()
        await asyncio.gather(server._reader_task, server._stderr_task, return_exceptions=True)

    asyncio.run(run())
    assert launched[0][0] == ("codex", "-c", "features.code_mode_host=true", "app-server")
