import asyncio
import importlib.util
import json
from pathlib import Path

PATH = Path(__file__).resolve().parents[1] / "plugins/abilities/Administrator/v3_build/v3_build.py"
spec = importlib.util.spec_from_file_location("v3_build_test", PATH)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

def test_discovery_and_schemas():
    from app import abilities
    abilities.reload()
    feature = abilities.ability_feature_with_skill("v3_build")
    assert feature and feature["display_name"] == "V3 Build"
    assert set(m.build_tools()) == set(m.TOOL_SCHEMAS)
    assert m.DESTRUCTIVE == {"login_as_current_user_v3", "v3_build_work", "v3_build_finish", "v3_source_apply"}
    descriptor = json.loads(PATH.with_suffix(".json").read_text())
    assert set(descriptor["tools"]) == set(m.TOOL_SCHEMAS)
    assert descriptor["tool_metadata"]["v3_build_finish"]["requires_confirmation"] is True
    assert descriptor["tool_metadata"]["v3_source_apply"]["requires_confirmation"] is True

def test_bridge_requires_mapped_administrator(monkeypatch):
    import app.auth.identity as identity
    calls = []
    async def request(method, path, body=None):
        calls.append((method, path, body))
        return {"open_count": 1, "own": []}
    async def admin(_): return True
    monkeypatch.setattr(m, "_worker_request", request)
    monkeypatch.setattr(identity, "_is_admin", admin)
    monkeypatch.setenv("V3_BROWSER_LOGIN_CALLER_ID", "fixture-admin")
    allowed = asyncio.run(m._bridge_request("fixture-admin", "v3_build_work", {"action": "status"}))
    denied = asyncio.run(m._bridge_request("other-user", "v3_build_work", {"action": "status"}))
    assert allowed["open_count"] == 1 and denied["status"] == "denied"
    assert calls == [("POST", "/v1/coordinator",
                      {"name": "v3_build_work", "arguments": {"action": "status"}})]


def test_cargo_request_keeps_http_open_for_broker_budget(monkeypatch):
    import httpx
    timeouts = []
    class Client:
        def __init__(self, *, timeout, **kwargs):
            timeouts.append(timeout)
        async def __aenter__(self): return self
        async def __aexit__(self, *unused): return False
        async def request(self, *unused, **kwargs):
            class Response:
                status_code = 200
                def json(self): return {"status": "completed"}
            return Response()
    monkeypatch.setattr(httpx, "AsyncClient", Client)
    monkeypatch.setenv("V3_BUILD_WORKER_URL", "http://127.0.0.1:1234")
    monkeypatch.setenv("V3_BUILD_WORKER_CLIENT_TOKEN", "fixture-token")
    for name in ("v3_build_work", "v3_cargo_task"):
        result = asyncio.run(m._worker_request("POST", "/v1/coordinator",
            {"name": name, "arguments": {}}))
        assert result["status"] == "completed"
    assert timeouts == [75, 675]


def test_lifecycle_and_apply_use_native_coordinator(monkeypatch):
    calls = []
    async def bridge(user, name, arguments):
        calls.append((name, arguments))
        if name == "v3_build_work" and arguments.get("action") == "finish":
            return {"status": "closed", "job_id": "job-1234"}
        if name == "v3_build_work" and arguments.get("action") == "status":
            return {"own": [{"task_id": "task-1234", "job_id": "job-1234"}]}
        if name == "v3_worker_job":
            return {"status": "completed", "job_id": "job-1234", "phase": "finished"}
        if name == "v3_build_status":
            return {"containers": {"api": {"state": "running", "health": "healthy"},
                                   "loop": {"state": "running", "health": "healthy"}}}
        return {"status": "ok"}
    monkeypatch.setattr(m, "_bridge_request", bridge)
    tools = m.build_tools(user_id="fixture-admin")
    assert "v3_worker_submit" not in tools
    assert json.loads(asyncio.run(tools["v3_build_work"]("finish")))["status"] == "error"
    assert json.loads(asyncio.run(tools["v3_build_work"]("begin", "task-1234", "Patch V3")))["status"] == "ok"
    assert json.loads(asyncio.run(tools["v3_source_apply"]("task-1234", "apply-1234", "patch", "a" * 64)))["status"] == "ok"
    result = json.loads(asyncio.run(tools["v3_build_finish"]("task-1234", "a" * 64)))
    assert result["status"] == "completed" and result["verified_healthy"] is True
    assert [name for name, _ in calls] == [
        "v3_build_work", "v3_source_apply", "v3_build_work", "v3_build_work",
        "v3_worker_job", "v3_build_status"]
    assert calls[2][1]["action"] == "finish"


def test_task_inspection_and_cargo_use_native_identity(monkeypatch):
    calls = []

    async def bridge(user, name, arguments):
        calls.append((user, name, arguments))
        return {"status": "ok"}

    monkeypatch.setattr(m, "_bridge_request", bridge)
    tools = m.build_tools(user_id="fixture-admin")
    asyncio.run(tools["v3_source_search"]("task-1234", "needle", 5))
    asyncio.run(tools["v3_source_read_batch"]("task-1234", [{"path": "src/main.rs"}]))
    asyncio.run(tools["v3_cargo_task"]("task-1234", "check", "a" * 64, 30))
    assert [name for _, name, _ in calls] == [
        "v3_source_search", "v3_source_read_batch", "v3_cargo_task"]
    assert all(user == "fixture-admin" for user, _, _ in calls)
    assert calls[-1][2]["timeout_seconds"] == 30


def test_v3_login_only_in_build_ability():
    from plugins.abilities.Administrator.diagnostics import diagnostics
    assert "login_as_current_user_v3" not in diagnostics.build_tools()
    assert "login_as_current_user_v3" not in diagnostics.TOOL_SCHEMAS
    assert "login_as_current_user_v3" in m.build_tools(user_id="fixture-admin")

def test_v3_login_missing_identity_and_configuration(monkeypatch):
    result = json.loads(asyncio.run(m.build_tools()["login_as_current_user_v3"]()))
    assert result["status"] == "error" and "signed-in" in result["message"]
    monkeypatch.delenv("V3_BROWSER_LOGIN_TOKEN", raising=False)
    monkeypatch.delenv("V3_BROWSER_LOGIN_CALLER_ID", raising=False)
    result = json.loads(asyncio.run(m.build_tools(user_id="fixture-admin")["login_as_current_user_v3"]()))
    assert result["status"] == "error" and "not configured" in result["message"]

def test_v3_login_mapping_and_origin(monkeypatch):
    import pytest
    monkeypatch.setenv("V3_BROWSER_LOGIN_TOKEN", "x" * 40)
    monkeypatch.setenv("V3_BROWSER_LOGIN_CALLER_ID", "fixture-admin")
    monkeypatch.setenv("V3_DIAGNOSTICS_ORIGIN", "https://dev.webagent.live")
    assert m._v3_login_config("fixture-admin")[0] == "https://dev.webagent.live"
    with pytest.raises(ValueError, match="Current user"):
        m._v3_login_config("other-user")
    for origin in ["http://remote.example", "https://user:pass@example.com", "https://example.com/path"]:
        monkeypatch.setenv("V3_DIAGNOSTICS_ORIGIN", origin)
        with pytest.raises(ValueError, match="HTTPS origin"):
            m._v3_login_config("fixture-admin")

def test_v3_login_rejects_external_route(monkeypatch):
    monkeypatch.setenv("V3_BROWSER_LOGIN_TOKEN", "x" * 40)
    monkeypatch.setenv("V3_BROWSER_LOGIN_CALLER_ID", "fixture-admin")
    monkeypatch.setenv("V3_DIAGNOSTICS_ORIGIN", "https://dev.webagent.live")
    for path in ["https://other.example/app", "//other.example/app", "/app\\bad", "/app\n"]:
        result = json.loads(asyncio.run(m.build_tools(user_id="fixture-admin")["login_as_current_user_v3"](path=path)))
        assert result["status"] == "error" and "relative route" in result["message"]
