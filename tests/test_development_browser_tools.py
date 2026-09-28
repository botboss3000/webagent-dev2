"""Contract tests for the Diagnostics-owned development browser facade."""
from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
DIAGNOSTICS = ROOT / "plugins/abilities/Administrator/diagnostics/diagnostics.py"


def _module():
    spec = importlib.util.spec_from_file_location("development_browser_diagnostics", DIAGNOSTICS)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _identity(user_id="admin"):
    return {
        "http_status": 200, "username": "admin", "user_id": user_id,
        "stored_user_id": user_id, "active_user_id": user_id,
    }


def _tools(monkeypatch, *, backend="server", resolver_error=None, recorder_available=True, recorder_degraded=False):
    mod = _module()
    import app.auth.jwt as jwt
    import app.auth.users as users
    import app.remote_access.netinfo as netinfo
    import app.tools.browser as browser

    state = {"backend": backend, "recording": False, "calls": [],
             "recorder_available": recorder_available, "recorder_degraded": recorder_degraded}

    async def fake_user(_user_id):
        return SimpleNamespace(username="admin", user_id="admin")

    def resolve(*_args):
        if resolver_error:
            raise resolver_error
        return "browser-1"

    async def action(bs_id, action, **kwargs):
        assert bs_id == "browser-1"
        state["calls"].append((action, kwargs))
        if action == "evaluate":
            js = kwargs.get("js", "")
            if "auth/me" in js:
                return {"success": True, "result": json.dumps(_identity())}
            if "#render-recorder-toggle" in js:
                if not state["recorder_available"]:
                    return {"success": True, "result": json.dumps({"error": "Render Recorder toggle unavailable"})}
                state["recording"] = "!== true" in js
                return {"success": True, "result": json.dumps({"enabled": state["recording"], "recording_id": "rec-1"})}
            if "__renderRecorderState" in js:
                return {"success": True, "result": json.dumps({
                    "enabled": state["recording"], "recording_id": "rec-1",
                    "phase": "degraded" if state["recorder_degraded"] else "recording",
                    "gaps": ["network-gap"] if state["recorder_degraded"] else [],
                })}
        return {"success": True, "result": "ok", "url": kwargs.get("url", "http://localhost:8080/")}

    async def server(_bs_id):
        state["backend"] = "server"
        return {"success": True}

    monkeypatch.setattr(users, "get_user_by_id", fake_user)
    monkeypatch.setattr(jwt, "create_access_token", lambda *_args, **_kwargs: "never-return-this-token")
    monkeypatch.setattr(netinfo, "get_port", lambda: 8080)
    monkeypatch.setattr(browser, "resolve_agent_session", resolve)
    monkeypatch.setattr(browser, "browser_action", action)
    monkeypatch.setattr(browser, "open_server_browser", server)
    monkeypatch.setattr(browser, "backend_status", lambda _bs_id: {"backend": state["backend"]})
    return mod.build_tools(user_id="admin", agent_id="agent", session_id="chat"), state


def test_development_tools_gate_in_ask_mode_without_locking_auto_permission():
    source = DIAGNOSTICS.read_text(encoding="utf-8")
    loop = (ROOT / "app/agent/loop.py").read_text(encoding="utf-8")
    assert '"dev_browser_action", "dev_browser_record"' in source
    assert '"dev_browser_url"' in source
    assert "open_development_browser" not in source
    assert "development_browser_action" not in source
    assert "development_browser_url" not in source
    assert "REQUIRES_CONFIRMATION: set = set()" in source
    assert "def _is_safe_dev_browser_action" in loop
    assert 'tool_name == "dev_browser_action"' in loop


def test_dev_browser_action_open_authenticates_hands_off_and_hides_token(monkeypatch):
    tools, state = _tools(monkeypatch, backend="headless")
    result = json.loads(asyncio.run(tools["dev_browser_action"]("open")))

    assert result["status"] == "ok"
    assert result["backend"] == "server"
    assert result["origin"] == "http://localhost:8080"
    assert result["verified"]["user_id"] == "admin"
    assert "never-return-this-token" not in json.dumps(result)
    assert any(action == "navigate" for action, _kwargs in state["calls"])


def test_dev_browser_url_uses_chat_route_session_parameter(monkeypatch):
    tools, _state = _tools(monkeypatch)

    class _Db:
        async def assert_session_owned(self, user_id, session_id):
            assert (user_id, session_id) == ("admin", "chat session")

    import app.db
    monkeypatch.setattr(app.db, "get_db", lambda: _Db())

    result = json.loads(asyncio.run(
        tools["dev_browser_url"](chat_session_id="chat session")
    ))

    assert result["status"] == "ok"
    assert result["path"] == "/app?session=chat%20session"
    assert result["url"] == "http://localhost:8080/app?session=chat%20session"


def test_development_action_is_same_origin_and_requires_server_identity(monkeypatch):
    tools, state = _tools(monkeypatch)
    allowed = json.loads(asyncio.run(tools["dev_browser_action"]("navigate", path="/app?tab=app")))
    assert allowed["status"] == "ok"
    assert any(kwargs.get("url") == "http://localhost:8080/app?tab=app" for action, kwargs in state["calls"] if action == "navigate")

    rejected = json.loads(asyncio.run(tools["dev_browser_action"]("navigate", path="https://example.com/")))
    assert rejected["status"] == "error"

    state["backend"] = "headless"
    unready = json.loads(asyncio.run(tools["dev_browser_action"]("get_text")))
    assert unready["status"] == "error"
    assert 'dev_browser_action(action="open")' in unready["message"]


def test_development_recorder_is_explicit_and_reports_start_stop(monkeypatch):
    tools, state = _tools(monkeypatch)
    assert state["recording"] is False

    started = json.loads(asyncio.run(tools["dev_browser_record"]("start")))
    assert started["status"] == "ok"
    assert started["recorder"]["enabled"] is True

    stopped = json.loads(asyncio.run(tools["dev_browser_record"]("stop")))
    assert stopped["status"] == "ok"
    assert stopped["recorder"]["enabled"] is False


def test_development_recorder_reports_unavailable_and_degraded_states(monkeypatch):
    unavailable, _state = _tools(monkeypatch, recorder_available=False)
    result = json.loads(asyncio.run(unavailable["dev_browser_record"]("start")))
    assert result["status"] == "error"
    assert "unavailable" in result["message"]

    degraded, _state = _tools(monkeypatch, recorder_degraded=True)
    result = json.loads(asyncio.run(degraded["dev_browser_record"]("status")))
    assert result["status"] == "ok"
    assert result["recorder"]["phase"] == "degraded"
    assert result["recorder"]["gaps"] == ["network-gap"]


def test_dev_browser_action_open_fails_closed_on_browser_session_ownership(monkeypatch):
    tools, _state = _tools(monkeypatch, resolver_error=PermissionError("browser session not found"))
    result = json.loads(asyncio.run(tools["dev_browser_action"]("open")))
    assert result["status"] == "error"
    assert result["message"] == "browser session not found"


@pytest.mark.parametrize("path", ["", "//other.example/app", "https://other.example/app"])
def test_development_paths_reject_non_relative_origins(path):
    mod = _module()
    assert mod._development_path("http://localhost:8080", path) is None
