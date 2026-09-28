import asyncio
from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.auth.workspace import WorkspaceContext, require_user_workspace, workspace_scope
from app.content_access import (
    decode_content_grant,
    issue_path_grant,
    validate_local_storage_path,
)
from app.agent.code_runner import assert_trusted_in_process_path


def _request(user_id: str, headers=None):
    raw_headers = []
    for key, value in (headers or {}).items():
        raw_headers.append((key.lower().encode(), value.encode()))
    return Request({
        "type": "http", "method": "GET", "path": "/", "query_string": b"",
        "headers": raw_headers, "state": {"auth_payload": {"user_id": user_id, "sub": user_id}},
    })


def test_content_grants_are_opaque_bound_and_tamper_evident():
    url = issue_path_grant(
        user_id="alice", storage_path="alice/uploads/secret.txt",
        original_name="secret.txt", mime_type="text/plain",
    )
    token = url.rsplit("/", 1)[-1]
    assert "alice" not in token and "secret" not in token
    payload = decode_content_grant(token)
    assert payload["user_id"] == "alice"
    with pytest.raises(HTTPException) as exc:
        decode_content_grant(token[:-1] + ("A" if token[-1] != "A" else "B"))
    assert exc.value.status_code == 404


def test_local_content_paths_cannot_escape_the_user_home(monkeypatch, tmp_path):
    import app.user_workspace as workspace
    monkeypatch.setattr(workspace, "_DEFAULT_BASE", tmp_path / "user_data")
    validate_local_storage_path("alice", "alice/uploads/file.txt")
    with pytest.raises(HTTPException) as exc:
        validate_local_storage_path("alice", "bob/uploads/file.txt")
    assert exc.value.status_code == 404


def test_own_workspace_needs_no_admin_override():
    context = asyncio.run(require_user_workspace(_request("alice"), "alice", write=True))
    assert context.subject_id == "alice"
    context.require("user:write")


def test_cross_workspace_is_hidden_from_every_account():
    with pytest.raises(HTTPException) as exc:
        asyncio.run(require_user_workspace(_request("alice"), "bob"))
    assert exc.value.status_code == 404
    with pytest.raises(HTTPException) as exc:
        asyncio.run(require_user_workspace(_request("admin"), "bob"))
    assert exc.value.status_code == 404


def test_forged_admin_workspace_context_cannot_change_the_subject():
    forged_context = WorkspaceContext(
        actor_user_id="admin", subject_kind="user", subject_id="bob",
        capabilities=frozenset({"user:read", "user:write"}),
    )
    with workspace_scope(forged_context):
        resolved = asyncio.run(require_user_workspace(_request("admin"), "admin"))
        assert resolved.subject_id == "admin"
        with pytest.raises(HTTPException) as exc:
            asyncio.run(require_user_workspace(
                _request("admin", {"X-WebAgent-Workspace-Session": "forged"}), "bob"
            ))
        assert exc.value.status_code == 404


def test_in_process_runner_rejects_runtime_data(monkeypatch, tmp_path):
    data = tmp_path / "data"
    code = data / "agent_data" / "a" / "plugin.py"
    code.parent.mkdir(parents=True)
    code.write_text("print('no')", encoding="utf-8")
    monkeypatch.setattr("app.agent.code_runner.Path", Path)
    monkeypatch.setattr("app.runtime_mode.data_root", lambda: data)
    with pytest.raises(PermissionError):
        assert_trusted_in_process_path(code)


def test_agent_ui_path_is_contained_and_never_serves_python(monkeypatch, tmp_path):
    import app.agent_workspace as workspace
    monkeypatch.setattr(workspace, "_DEFAULT_BASE", tmp_path / "agent_data")
    index = workspace.agent_ui_dir("agent-1") / "index.html"
    index.write_text("<h1>safe</h1>", encoding="utf-8")
    python_file = index.with_name("server.py")
    python_file.write_text("raise SystemExit", encoding="utf-8")
    assert workspace.resolve_ui_asset("agent-1", "index.html") == index.resolve()
    assert workspace.resolve_ui_asset("agent-1", "../agent-1.db") is None
    assert workspace.resolve_ui_asset("agent-1", "server.py") is None


def test_user_data_has_no_static_mount():
    from app.main import app
    assert not any(getattr(route, "path", None) == "/user_data" for route in app.routes)


def test_screenshots_have_no_shared_storage_fallback():
    source = (Path(__file__).parents[1] / "app" / "tools" / "browser.py").read_text(
        encoding="utf-8"
    )
    assert 'uri = f"/screenshots/{filename}"' not in source
    assert "Screenshot workspace could not be resolved" in source


def test_no_admin_workspace_impersonation_routes_exist():
    from app.main import app
    assert not any(
        str(getattr(route, "path", "")).startswith("/api/v1/admin/workspace-sessions")
        for route in app.routes
    )
