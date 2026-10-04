import asyncio
import importlib.util
import json
from pathlib import Path
import threading
from unittest.mock import patch
import urllib.request
import urllib.error
import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("worker_test", ROOT / "scripts/v3_build_worker.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

@pytest.fixture
def worker(tmp_path):
    w = m.Worker(ROOT, tmp_path, "a" * 64)
    yield w
    w.lockfile.close()

def test_idempotency_and_conflict(worker):
    with patch.object(m.threading.Thread, "start"):
        request = {"operation": "build", "request_id": "build-1"}
        first = worker.submit(request)
        assert worker.submit(request)["id"] == first["id"]
        with pytest.raises(ValueError): worker.submit({**request, "operation": "deploy"})
        with pytest.raises(BlockingIOError): worker.submit({**request, "request_id": "build-2"})
    assert json.loads((worker.state / (first["id"] + ".json")).read_text())["status"] == "queued"

def test_reject_arbitrary_input(worker):
    for req in [{"operation": "shell", "request_id": "x"}, {"operation": "build", "request_id": "../x"},
                {"operation": "build", "request_id": "x", "command": "docker rm v2"},
                {"operation": "deploy", "request_id": "x", "build_job_id": "untrusted"}]:
        with pytest.raises(ValueError): worker.submit(req)

def test_restart_recovery(worker):
    with patch.object(m.threading.Thread, "start"):
        job = worker.submit({"operation": "build", "request_id": "x"})
    worker.lockfile.close()
    recovered = m.Worker(ROOT, worker.state, "a" * 64)
    assert recovered.jobs[job["id"]]["status"] == "interrupted"
    assert recovered.active is None
    recovered.lockfile.close()

def test_restart_refuses_active_job_and_preserves_receipt(worker):
    with patch.object(m.threading.Thread, "start"):
        worker.submit({"operation": "build", "request_id": "build-active"})
    with pytest.raises(BlockingIOError):
        worker.restart("restart-while-busy")
    assert not list(worker.restart_dir.glob("*.json"))
    worker.active = None
    previous, scheduled = worker.restart("restart-once")
    assert previous == m.os.getpid() and scheduled
    assert worker.restart("restart-once") == (previous, False)
    with pytest.raises(BlockingIOError):
        worker.submit({"operation": "build", "request_id": "blocked-while-draining"})
    worker.lockfile.close()
    recovered = m.Worker(ROOT, worker.state, worker.token)
    assert recovered.restart("restart-once") == (previous, False)
    recovered.lockfile.close()


def test_authenticated_http_restart_does_not_exit_in_test(worker):
    server = m.ThreadingHTTPServer(("127.0.0.1", 0), m.Handler); server.worker = worker
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    url = "http://127.0.0.1:%s/v1/restart" % server.server_port
    body = json.dumps({"request_id": "http-restart"}).encode()
    try:
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(urllib.request.Request(url, data=body, method="POST"))
        assert error.value.code == 401
        with patch.object(m.threading.Timer, "start") as start:
            request = urllib.request.Request(url, data=body, headers={"Authorization": "Bearer " + worker.token}, method="POST")
            with urllib.request.urlopen(request) as response:
                assert response.status == 202
                assert json.load(response)["previous_pid"] == m.os.getpid()
            with urllib.request.urlopen(request) as response:
                assert response.status == 202
            start.assert_called_once()
    finally:
        server.shutdown(); server.server_close(); thread.join()


def test_v3_broker_control_socket_can_request_restart(worker, tmp_path, monkeypatch):
    host_path = ROOT / "v3/plugins/abilities/Administrator/v3_build/host.py"
    spec = importlib.util.spec_from_file_location("host_build_test", host_path)
    host = importlib.util.module_from_spec(spec); spec.loader.exec_module(host)
    path = tmp_path / "control.sock"
    server = m.ThreadingUnixServer(str(path), m.Handler)
    server.worker = worker
    server.local_control = True
    path.chmod(0o600)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    monkeypatch.setattr(host, "WORKER_CONTROL", path)
    monkeypatch.setattr(worker, "command", lambda *args, **kwargs: {"output_tail": "fixture"})
    try:
        with patch.object(m.threading.Timer, "start") as start:
            result = host.worker_control("POST", "/v1/restart", {"request_id": "v3-control"})
            assert result["status"] == "restarting"
            status = host.worker_control("GET", "/v1/status")
            assert status["pid"] == result["previous_pid"]
            start.assert_called_once()
    finally:
        server.shutdown(); server.server_close(); thread.join()


def old_container():
    return {"Name": "/webagent-dev-v3", "State": {"Running": True},
            "Config": {"Image": "old", "Env": ["V3_MESSAGE_API_TOKEN=secret"], "Healthcheck": {"Test": ["CMD", "curl"]}},
            "HostConfig": {"NetworkMode": "bridge", "Memory": 1000, "ReadonlyRootfs": True, "RestartPolicy": {"Name": "unless-stopped"}},
            "Mounts": [{"Type": "bind", "Destination": "/state", "Source": str(ROOT / "v3/data")},
                       {"Type": "bind", "Destination": "/workspaces", "Source": str(ROOT / "v3/workspaces")} ]}

def deployment(worker):
    worker.jobs["build"] = {"status": "succeeded", "image_id": "sha256:new", "operation": "build"}
    return {"id": "deploy", "request": {"build_job_id": "build"}}

def test_deploy_preserves_config(worker, monkeypatch):
    calls = []
    old = old_container()
    def engine(method, path, body=None):
        calls.append((method, path, body))
        if path.startswith("/images/"): return {"Id": "sha256:new"}
        if method == "GET":
            return old if path.startswith("/containers/webagent-dev-v3/json") and len(calls) == 2 else {"State": {"Running": True, "Health": {"Status": "healthy"}}}
        return {}
    monkeypatch.setattr(m, "engine", engine)
    job = deployment(worker); worker.deploy(job)
    payload = next(body for method, path, body in calls if path.startswith("/containers/create"))
    assert payload["Env"] == old["Config"]["Env"]
    assert payload["HostConfig"] == old["HostConfig"]
    assert payload["Image"] == "sha256:new" and job["readiness"] == "healthy"
    assert all("v2" not in path for _, path, _ in calls)
    assert not any(method == "DELETE" for method, _, _ in calls)

def test_failed_readiness_rolls_back(worker, monkeypatch):
    calls = []
    def engine(method, path, body=None):
        calls.append((method, path, body))
        if path.startswith("/images/"): return {"Id": "sha256:new"}
        if method == "GET": return old_container() if len(calls) == 2 else {"State": {"Running": False}}
        return {}
    monkeypatch.setattr(m, "engine", engine)
    job = deployment(worker)
    with pytest.raises(RuntimeError, match="healthy"): worker.deploy(job)
    assert job["rollback"] == "restored"
    assert ("DELETE", "/containers/webagent-dev-v3?force=true", None) in calls
    assert calls[-1][:2] == ("POST", "/containers/webagent-dev-v3/start")

def test_failed_create_restores_without_deleting_target(worker, monkeypatch):
    calls = []
    def engine(method, path, body=None):
        calls.append((method, path, body))
        if path.startswith("/images/"): return {"Id": "sha256:new"}
        if method == "GET": return old_container()
        if path.startswith("/containers/create"): raise RuntimeError("create failed")
        return {}
    monkeypatch.setattr(m, "engine", engine)
    job = deployment(worker)
    with pytest.raises(RuntimeError): worker.deploy(job)
    assert job["rollback"] == "restored"
    assert not any(method == "DELETE" for method, _, _ in calls)

def test_http_auth_and_unknown_route(worker):
    server = m.ThreadingHTTPServer(("127.0.0.1", 0), m.Handler); server.worker = worker
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    url = "http://127.0.0.1:%s" % server.server_port
    try:
        for auth in (None, "Bearer wrong"):
            headers = {} if auth is None else {"Authorization": auth}
            with pytest.raises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(urllib.request.Request(url + "/v1/status", headers=headers))
            assert error.value.code == 401
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(urllib.request.Request(url + "/admin", headers={"Authorization": "Bearer " + worker.token}))
        assert error.value.code == 404
        with patch.object(worker, "execute", return_value=None):
            request = urllib.request.Request(url + "/v1/jobs", data=json.dumps({"operation": "build", "request_id": "http-1"}).encode(), headers={"Authorization": "Bearer " + worker.token}, method="POST")
            with urllib.request.urlopen(request) as response:
                assert response.status == 202
                job = json.load(response)
            poll = urllib.request.Request(url + "/v1/jobs/" + job["id"], headers={"Authorization": "Bearer " + worker.token})
            with urllib.request.urlopen(poll) as response:
                assert json.load(response)["id"] == job["id"]
    finally:
        server.shutdown(); server.server_close(); thread.join()

def test_client_disabled_and_input_validation(monkeypatch):
    build = m.load_build(ROOT)
    monkeypatch.delenv("V3_BUILD_WORKER_URL", raising=False)
    monkeypatch.delenv("V3_BUILD_WORKER_CLIENT_TOKEN", raising=False)
    assert asyncio.run(build._worker_request("GET", "/v1/status"))["status"] == "blocked"
    for key in ("../x", "x?token=secret"):
        assert json.loads(asyncio.run(build.build_tools()["v3_worker_job"](key)))["status"] == "error"

def test_worker_credential_not_in_build_environment(monkeypatch):
    build = m.load_build(ROOT)
    monkeypatch.setenv("V3_BUILD_WORKER_TOKEN", "secret")
    monkeypatch.setenv("V3_BUILD_WORKER_CLIENT_TOKEN", "secret")
    assert all("TOKEN" not in key for key in build._env())


def test_build_uses_directory_context(worker, monkeypatch):
    import tarfile
    import io
    def archive(file):
        with tarfile.open(fileobj=file, mode="w") as tar:
            info = tarfile.TarInfo("v3/Dockerfile")
            data = b"FROM scratch\n"
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        file.seek(0)
    async def run(argv, **kwargs):
        directory = Path(argv[-1])
        assert directory.is_dir()
        assert Path(argv[argv.index("-f") + 1]) == directory / "v3/Dockerfile"
        assert (directory / "v3/Dockerfile").read_text() == "FROM scratch\n"
        assert "stdin" not in kwargs
        return {"exit_code": 0, "output_tail": "built"}
    monkeypatch.setattr(worker.build, "_context_archive", archive)
    monkeypatch.setattr(worker.build, "_run", run)
    monkeypatch.setattr(m, "engine", lambda *args: {"Id": "sha256:test"})
    job = {"id": "directory-build", "operation": "build"}
    worker.execute(job)
    assert job["status"] == "succeeded"
    assert job["image_id"] == "sha256:test"
    assert len(job["context_sha256"]) == 64


def test_failed_build_preserves_snapshot_and_exit_metadata(worker, monkeypatch):
    import io
    import tarfile
    def archive(file):
        with tarfile.open(fileobj=file, mode="w") as tar:
            info = tarfile.TarInfo("v3/Dockerfile")
            data = b"FROM scratch\n"
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        file.seek(0)
    async def run(*args, **kwargs):
        return {"exit_code":137,"timed_out":False,"output_truncated":True,
                "output_tail":"failure " + worker.token}
    monkeypatch.setattr(worker.build,"_context_archive",archive)
    monkeypatch.setattr(worker.build,"_run",run)
    job = {"id":"failed-build","operation":"build"}
    worker.execute(job)
    saved = json.loads((worker.state / "failed-build.json").read_text())
    assert saved["status"] == "failed"
    assert saved["exit_code"] == 137 and not saved["timed_out"]
    assert saved["output_truncated"] and len(saved["context_sha256"]) == 64
    assert worker.token not in saved["output_tail"]


def test_missing_image_preflight_does_not_stop_app(worker, monkeypatch):
    calls = []
    def engine(method, path, body=None):
        calls.append((method, path))
        raise RuntimeError("Docker operation failed (HTTP 404)")
    monkeypatch.setattr(m, "engine", engine)
    with pytest.raises(RuntimeError, match="preflight image check"):
        worker.deploy(deployment(worker))
    assert calls == [("GET", "/images/sha256:new/json")]
