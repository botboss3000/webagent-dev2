"""Workspace adapter contract tests. No live services, databases, or credentials."""
import importlib.util
import json
from pathlib import Path
import threading
import urllib.error
import urllib.request
import uuid
import pytest

ROOT = Path(__file__).resolve().parents[1]

def module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec); spec.loader.exec_module(value)
    return value

HOST = module(ROOT / "scripts/v3_workspace_worker.py", "workspace_host_test")
CLIENT = module(ROOT / "plugins/abilities/Administrator/v3_workspace/v3_workspace.py", "workspace_client_test")

@pytest.fixture
def service(tmp_path):
    repo = tmp_path / "repo"; source = repo / "v3"; (source / "src").mkdir(parents=True)
    (source / "worker.py").write_bytes((ROOT / "v3/worker.py").read_bytes())
    (source / "src/main.rs").write_text('fn main() {}\n')
    instance = HOST.WorkspaceService(repo, tmp_path / "state")
    yield instance
    instance.lockfile.close()

@pytest.fixture
def workspace(service):
    wid = str(uuid.uuid4())
    action(service, wid, "seed", "seed")
    return wid

def action(service, wid, op, key, **args):
    return service.submit({"operation": op, "workspace_id": wid, "request_id": key, "arguments": args})

def patch():
    return 'diff --git a/src/main.rs b/src/main.rs\n--- a/src/main.rs\n+++ b/src/main.rs\n@@ -1 +1 @@\n-fn main() {}\n+fn main() { println!("hello"); }\n'

def test_retry_bound_to_exact_input(service, workspace):
    original = action(service, workspace, "review", "review-1")
    assert action(service, workspace, "review", "review-1") == original
    with pytest.raises(ValueError, match="different input"):
        action(service, workspace, "read", "review-1", path="src/main.rs")

def test_seed_does_not_change_live_source(service, workspace):
    assert (service.repo / "v3/src/main.rs").read_text() == 'fn main() {}\n'
    assert action(service, workspace, "review", "initial-review")["result"]["diff"] == ""

def test_patch_and_frozen_proposal(service, workspace):
    review = action(service, workspace, "review", "r1")["result"]
    assert action(service, workspace, "patch", "p1", patch=patch(), expected_review_sha256=review["review_sha256"])["status"] == "success"
    fresh = action(service, workspace, "review", "r2")["result"]
    result = action(service, workspace, "propose", "proposal", expected_review_sha256=fresh["review_sha256"], verification_summary="Source contract tested; compiler not run")
    assert result["status"] == "proposed" and not result["result"]["applied"]
    assert (service.repo / "v3/src/main.rs").read_text() == 'fn main() {}\n'
    assert json.loads((service.proposals / "proposal.json").read_text())["review"]["diff"] == fresh["diff"]

def test_stale_patch_does_not_apply(service, workspace):
    result = action(service, workspace, "patch", "stale", patch=patch(), expected_review_sha256="0" * 64)
    assert result["status"] == "uncertain"
    assert (service.base(workspace) / "webagent-v3/src/main.rs").read_text() == 'fn main() {}\n'

@pytest.mark.parametrize("path", ["../worker.py", "/etc/passwd", ".git/config", "src/../main.rs", "src\\main.rs"])
def test_read_path_rejected(service, workspace, path):
    assert action(service, workspace, "read", "bad-read", path=path)["status"] == "uncertain"

def test_symlink_rejected(service, workspace):
    (service.base(workspace) / "webagent-v3/src/link").symlink_to(service.repo / "v3/worker.py")
    assert action(service, workspace, "read", "link", path="src/link")["status"] == "uncertain"

def test_unknown_operation_and_arguments(service, workspace):
    for op, args in [("deploy", {}), ("review", {"command": "echo hi"})]:
        with pytest.raises(ValueError): action(service, workspace, op, "bad", **args)

def test_restart_does_not_replay(service):
    HOST.durable_json(service.receipts / "interrupted.json", {"status": "running", "request_id": "interrupted"})
    service.lockfile.close()
    again = HOST.WorkspaceService(service.repo, service.state)
    try: assert again.receipt("interrupted")["status"] == "uncertain"
    finally: again.lockfile.close()

def test_shutoff(service, workspace):
    (service.control / "shutoff").write_text("1")
    with pytest.raises(ValueError, match="disabled"): action(service, workspace, "review", "disabled")

def test_http_auth_and_no_apply(service):
    token = "x" * 48
    server = HOST.serve(service, "127.0.0.1", 0, token)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    url = "http://127.0.0.1:%s" % server.server_port
    try:
        with pytest.raises(urllib.error.HTTPError) as error: urllib.request.urlopen(url + "/v1/status")
        assert error.value.code == 401
        req = urllib.request.Request(url + "/v1/status", headers={"Authorization": "Bearer " + token})
        status = json.load(urllib.request.urlopen(req))
        assert status["live_apply"] is False and "deploy" not in status["operations"]
    finally: server.shutdown(); server.server_close(); thread.join()

def test_client_config_is_explicit(monkeypatch):
    for key in ("V3_WORKSPACE_WORKER_URL", "V3_WORKSPACE_WORKER_CLIENT_TOKEN", "V3_WORKSPACE_WORKER_CALLER_ID"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(ValueError, match="Configure"): CLIENT.configuration("admin")
    monkeypatch.setenv("V3_WORKSPACE_WORKER_URL", "http://127.0.0.1:8093")
    monkeypatch.setenv("V3_WORKSPACE_WORKER_CLIENT_TOKEN", "a" * 48)
    monkeypatch.setenv("V3_WORKSPACE_WORKER_CALLER_ID", "admin")
    with pytest.raises(ValueError, match="mapped"): CLIENT.configuration("other")
    assert CLIENT.configuration("admin")[0] == "http://127.0.0.1:8093"
    monkeypatch.setenv("V3_WORKSPACE_WORKER_URL", "http://user:pass@localhost/secret")
    with pytest.raises(ValueError, match="origin"): CLIENT.configuration("admin")


def test_drop_in_discovery_and_tool_contracts():
    from app import abilities
    abilities.reload()
    feature = abilities.ability_feature_with_skill("v3_workspace")
    assert feature and feature["display_name"] == "V3 Workspace"
    folder = ROOT / "plugins/abilities/Administrator/v3_workspace"
    descriptor = json.loads((folder / "v3_workspace.json").read_text())
    assert set(descriptor["tools"]) == set(CLIENT.TOOL_SCHEMAS) == set(CLIENT.build_tools())
    for name, schema in CLIENT.TOOL_SCHEMAS.items():
        assert schema["type"] == "object" and schema["additionalProperties"] is False
        assert set(schema.get("required", [])) <= set(schema["properties"])
        assert (folder / "tools" / (name + ".skill.md")).read_text().strip()
    assert CLIENT.DESTRUCTIVE == {"v3_workspace_action"}

def test_missing_cargo_is_scoped_blocker(service, workspace, monkeypatch):
    review = action(service, workspace, "review", "cargo-review")["result"]
    monkeypatch.setattr(HOST.shutil, "which", lambda _: None)
    value = action(service, workspace, "check", "cargo-check", expected_review_sha256=review["review_sha256"])
    assert value["status"] == "blocked"


def test_browser_paths_are_fixed_and_session_scoped():
    sid = str(uuid.uuid4())
    assert CLIENT.browser_read_path("status") == "/api/v3/auth/me"
    assert CLIENT.browser_read_path("review", sid) == f"/api/v3/sessions/{sid}/source-review"
    assert CLIENT.browser_read_path("receipt", sid, "proposal-1") == f"/api/v3/sessions/{sid}/promotions/proposal-1"
    for kind, session, receipt in [("patch", sid, ""), ("review", "../", ""), ("receipt", sid, "../secrets")]:
        with pytest.raises(ValueError): CLIENT.browser_read_path(kind, session, receipt)


def test_browser_mode_blocks_mutations_without_login(monkeypatch):
    import asyncio
    monkeypatch.delenv("V3_WORKSPACE_WORKER_URL", raising=False)
    for operation in ("seed", "patch", "check", "test", "propose", "read"):
        result = json.loads(asyncio.run(CLIENT.build_tools(user_id="admin")["v3_workspace_action"](operation, str(uuid.uuid4()), "r1", {})))
        assert result["status"] == "unsupported"


def test_browser_review_preserves_bound_identity(monkeypatch):
    import asyncio
    monkeypatch.delenv("V3_WORKSPACE_WORKER_URL", raising=False)
    seen = []
    async def fake(ctx, kind, workspace_id="", request_id=""):
        seen.append((ctx, kind, workspace_id))
        return {"status": "ok"}
    monkeypatch.setattr(CLIENT, "browser_read", fake)
    sid = str(uuid.uuid4())
    tools = CLIENT.build_tools(user_id="caller", session_id="owned", agent_id="agent")
    assert json.loads(asyncio.run(tools["v3_workspace_action"]("review", sid, "r1", {})))["status"] == "ok"
    assert seen == [({"user_id":"caller", "session_id":"owned", "agent_id":"agent"}, "review", sid)]


def test_configured_worker_does_not_fallback(monkeypatch):
    import asyncio
    monkeypatch.setenv("V3_WORKSPACE_WORKER_URL", "https://worker.invalid")
    async def fake(*args, **kwargs): return {"status": "blocked"}
    monkeypatch.setattr(CLIENT, "request", fake)
    assert json.loads(asyncio.run(CLIENT.build_tools()["v3_workspace_status"]()))["status"] == "blocked"


def test_failed_login_never_fetches(monkeypatch):
    import asyncio
    from plugins.abilities.Administrator.v3_build import v3_build
    from app.tools import browser
    async def login(**kwargs): return json.dumps({"status":"error"})
    monkeypatch.setattr(v3_build, "build_tools", lambda **ctx: {"login_as_current_user_v3":login})
    async def forbidden(*args, **kwargs): raise AssertionError("No browser read after failed login")
    monkeypatch.setattr(browser, "browser_action", forbidden)
    assert asyncio.run(CLIENT.browser_read({"user_id":"caller"}, "status"))["status"] == "blocked"


def test_browser_transport_validates_and_closes(monkeypatch):
    import asyncio
    from plugins.abilities.Administrator.v3_build import v3_build
    from plugins.abilities.Administrator.diagnostics import diagnostics
    from app.tools import browser
    async def login(**kwargs):
        return json.dumps({"status":"ok", "application":"v3", "backend":"server", "browser_session_id":"owned",
            "origin":"https://dev.webagent.live", "verified":{"http_status":200,"user_id":"admin","is_admin":True}})
    monkeypatch.setattr(v3_build, "build_tools", lambda **ctx: {"login_as_current_user_v3":login})
    calls=[]
    async def action(sid, **kwargs):
        calls.append((sid, kwargs))
        return {"success":True}
    monkeypatch.setattr(browser, "browser_action", action)
    monkeypatch.setattr(diagnostics, "_decode_browser_result", lambda result: {"http_status":200,"data":{"status":"failed","output":"No workspace"}})
    result=asyncio.run(CLIENT.browser_read({"user_id":"caller"}, "review", str(uuid.uuid4())))
    assert result["status"] == "failed" and result["result"]["output"] == "No workspace"
    assert calls[-1] == ("owned", {"action":"close"})
    assert "redirect:'error'" in calls[0][1]["js"]
    assert "https://dev.webagent.live" in calls[0][1]["js"]
