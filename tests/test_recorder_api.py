"""Recorder route boundaries using only a temporary diagnostic archive.

No live app lifespan, model, session DB or global observation subscriptions run.
API authorization, real signed document leases, intake redaction and persisted
approval consumption are exercised through FastAPI's public routes.
"""
import base64
import io
import json
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from plugins.recorder import api, service, investigation, legacy
from plugins.recorder.store import CaptureStore


@pytest.fixture
def recorder(tmp_path, monkeypatch):
    store = CaptureStore(tmp_path / "recorder.sqlite3")
    coordinator = service.RecorderService(store)

    async def begin(uid, client_id, recording_id=None, expires=None):
        return store.begin(uid, client_id, recording_id, expires)

    async def forbidden_investigation(*args, **kwargs):
        raise AssertionError("Tests must never invoke a model or execution adapter")

    async def execute(archive, cid, uid, approved, **kwargs):
        calls.append({"cid": cid, "uid": uid, "approved": approved, **kwargs})
        return archive.investigation(cid, state="execution_started")

    calls = []
    coordinator.begin = begin
    coordinator.investigate = forbidden_investigation
    monkeypatch.setattr(api, "get_service", lambda: coordinator)
    monkeypatch.setattr(api, "available", lambda: True)
    monkeypatch.setattr(service, "settings", lambda: {"auto_investigate": False})
    monkeypatch.setattr(legacy, "_lease_signing_key", lambda: "test-only-signing-key-never-from-runtime-1234567890")
    monkeypatch.setattr(investigation, "execute", execute)
    app = FastAPI()

    async def auth(request: Request):
        value = request.headers.get("x-test-user")
        if value is None:
            raise HTTPException(403, "Test authentication required")
        return {"user_id": value}

    app.dependency_overrides[api.require_admin] = auth
    app.include_router(api.router)
    with TestClient(app, raise_server_exceptions=False) as client:
        client.headers["x-test-user"] = "alice"
        yield SimpleNamespace(client=client, store=store, service=coordinator, calls=calls, app=app)


def start(recorder, client_id="tab-a"):
    response = recorder.client.post("/api/v1/recordings/enabled", json={"enabled": True, "client_id": client_id})
    assert response.status_code == 200, response.text
    return response.json()


def proof(capture):
    return {"lease_token": capture["lease_token"], "client_id": capture["client_id"]}


def record(capture, seq=0, **extra):
    return {"recording_id": capture["recording_id"], "client_id": capture["client_id"],
            "seq": seq, "kind": "checkpoint", "detail": {}, **extra}


def post_records(recorder, capture, rows):
    return recorder.client.post("/api/v1/recordings", json={"lease_token": capture["lease_token"], "records": rows})


def test_admin_auth_and_cross_owner_archive_access(recorder):
    capture = start(recorder)
    cid = capture["recording_id"]
    recorder.client.headers.pop("x-test-user")
    assert recorder.client.get("/api/v1/recordings/captures").status_code == 403
    assert recorder.client.post("/api/v1/recordings/enabled", json={"client_id": "tab"}).status_code == 403
    recorder.client.headers["x-test-user"] = "mallory"
    assert recorder.client.get("/api/v1/recordings/captures").json()["captures"] == []
    for tail in ("", "/events", "/artifacts/missing"):
        assert recorder.client.get(f"/api/v1/recordings/captures/{cid}{tail}").status_code == 404
    assert recorder.client.post(f"/api/v1/recordings/captures/{cid}/approval", json={"plan_revision": 1, "decision": "approve"}).status_code == 404


def test_bad_lease_and_entire_mixed_document_batch_rejected(recorder):
    capture = start(recorder)
    assert recorder.client.post("/api/v1/recordings", json={"lease_token": "invalid", "records": []}).status_code == 403
    response = post_records(recorder, capture, [record(capture), record(capture, 1, client_id="other-tab")])
    assert response.status_code == 403
    assert recorder.store.events(capture["recording_id"])["events"] == []
    cid = capture["recording_id"]
    response = recorder.client.post(f"/api/v1/recordings/captures/{cid}/stop", json={**proof(capture), "client_id": "other-tab"})
    assert response.status_code == 403
    unregistered = legacy._issue_capture_lease({"user_id": "alice"}, client_id="not-registered", recording_id=cid)
    assert recorder.client.post(f"/api/v1/recordings/captures/{cid}/stop", json=proof(unregistered)).status_code == 403


def test_duplicate_batch_is_idempotent_and_owner_cannot_be_spoofed(recorder):
    capture = start(recorder)
    rows = [record(capture, 0, user_id="mallory"), record(capture, 1)]
    first = post_records(recorder, capture, rows)
    second = post_records(recorder, capture, rows)
    assert first.status_code == second.status_code == 200
    assert first.json()["accepted"] == 2
    assert second.json()["accepted"] == 0 and second.json()["duplicates"] == 2
    events = recorder.store.events(capture["recording_id"])["events"]
    assert len(events) == 2 and all(e["user_id"] == "alice" for e in events)


@pytest.mark.parametrize("seq", [True, -1, "1", 0.5, None])
def test_browser_sequences_are_integral(recorder, seq):
    capture = start(recorder)
    assert post_records(recorder, capture, [record(capture, seq)]).status_code == 422


def test_stop_requires_complete_sequence_and_blocks_late_writes(recorder):
    capture = start(recorder)
    cid = capture["recording_id"]
    assert post_records(recorder, capture, [record(capture, 0), record(capture, 2)]).status_code == 200
    response = recorder.client.post(f"/api/v1/recordings/captures/{cid}/stop", json={**proof(capture), "last_seq": 2, "acknowledged": True})
    assert response.status_code == 200, response.text
    assert response.json()["capture"]["state"] == "incomplete"
    assert "Unacknowledged browser events at finalization" in response.json()["capture"]["gaps"]
    assert post_records(recorder, capture, [record(capture, 1)]).status_code == 409
    again = recorder.client.post(f"/api/v1/recordings/captures/{cid}/stop", json=proof(capture))
    assert again.status_code == 200
    assert again.json()["capture"]["state"] == "incomplete"


@pytest.mark.parametrize("image", ["data:image/svg+xml;base64,PHN2Zz4=", "data:image/png;base64,not-base64!", "data:image/png;base64,YWJj"])
def test_invalid_screenshot_never_archived(recorder, image):
    capture = start(recorder)
    cid = capture["recording_id"]
    response = recorder.client.post(f"/api/v1/recordings/captures/{cid}/markers", json={**proof(capture), "screenshot_data_url": image})
    assert response.status_code == 422
    assert recorder.store.detail(cid, "alice")["markers"] == []


def test_approval_revision_agent_binding_and_single_execution(recorder):
    capture = start(recorder)
    cid = capture["recording_id"]
    recorder.store.investigation(cid, state="plan_ready", plan={"revision": 2, "steps": ["Review ordering"]})
    root = f"/api/v1/recordings/captures/{cid}"
    assert recorder.client.post(root + "/execute", json={"plan_revision": 2}).status_code == 403
    stale = recorder.client.post(root + "/approval", json={"plan_revision": 1, "decision": "approve", "execution_agent_id": "dev-agent"})
    assert stale.status_code == 409, stale.text
    approved = recorder.client.post(root + "/approval", json={"plan_revision": 2, "decision": "approve", "execution_agent_id": "dev-agent"})
    assert approved.status_code == 200, approved.text
    assert approved.json()["investigation"]["approval"]["execution_agent_id"] == "dev-agent"
    assert recorder.client.post(root + "/execute", json={"plan_revision": 2, "execution_agent_id": "other-agent"}).status_code == 409
    assert recorder.client.post(root + "/execute", json={"plan_revision": 1, "execution_agent_id": "dev-agent"}).status_code == 403
    response = recorder.client.post(root + "/execute", json={"plan_revision": 2, "execution_agent_id": "dev-agent"})
    assert response.status_code == 200, response.text
    assert recorder.calls[0]["execution_agent_id"] == "dev-agent"
    assert recorder.client.post(root + "/execute", json={"plan_revision": 2, "execution_agent_id": "dev-agent"}).status_code == 403
    assert len(recorder.calls) == 1


@pytest.mark.parametrize("extra,secret", [
    ({"detail": {"request": {"headers": {"authorization": "nested-header-secret"}}}}, "nested-header-secret"),
    ({"detail": {"url": "https://host.test/path?access_token=query-secret"}}, "query-secret"),
    ({"detail": {"message": "Authorization: Bearer log-secret"}}, "log-secret"),
    ({"html": '<input type="password" value="html-input-secret">'}, "html-input-secret"),
    ({"detail": {"url": "https://person:userinfo-secret@host.test/path"}}, "userinfo-secret"),
    ({"detail": {"message": "failed request secret=log-assignment-secret"}}, "log-assignment-secret"),
    ({"detail": {"html": '<input type="password" value="nested-input-secret">'}}, "nested-input-secret"),
    ({"html": '<textarea name="password">textarea-secret</textarea>'}, "textarea-secret"),
])
def test_credentials_do_not_enter_archive(recorder, extra, secret):
    capture = start(recorder)
    response = post_records(recorder, capture, [record(capture, **extra)])
    assert response.status_code == 200, response.text
    stored = recorder.store.events(capture["recording_id"])["events"]
    assert secret not in json.dumps(stored), "Credential content reached persisted evidence"


def test_routes_have_one_new_intake_and_no_double_prefix(recorder):
    paths = [(route.path, method) for route in api.router.routes for method in getattr(route, "methods", ())]
    assert paths.count(("/api/v1/recordings", "POST")) == 1
    assert paths.count(("/api/v1/recordings/enabled", "POST")) == 1
    assert not any("/api/v1/recordings/api/" in path for path, _ in paths)


def test_expired_document_lease_cannot_write(recorder):
    import jwt
    capture = start(recorder)
    payload = legacy._verify_capture_lease(capture["lease_token"])
    payload["exp"] = time.time() - 1
    capture["lease_token"] = jwt.encode(payload, legacy._lease_signing_key(), algorithm="HS256")
    assert post_records(recorder, capture, [record(capture)]).status_code == 403
    assert recorder.store.events(capture["recording_id"])["events"] == []


def test_screenshot_bytes_are_authenticated_and_capture_scoped(recorder):
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", (2, 2)).save(out, format="PNG")
    image = out.getvalue()
    capture = start(recorder)
    cid = capture["recording_id"]
    response = recorder.client.post(f"/api/v1/recordings/captures/{cid}/markers", json={
        **proof(capture), "note": "Ordering issue", "screenshot_data_url": "data:image/png;base64," + base64.b64encode(image).decode()})
    assert response.status_code == 200, response.text
    marker = response.json()["marker"]
    assert "screenshot_data_url" not in marker
    url = f"/api/v1/recordings/captures/{cid}/artifacts/{marker['artifact_id']}"
    result = recorder.client.get(url)
    assert result.status_code == 200 and result.content == image
    assert result.headers["cache-control"] == "private, no-store"
    other = start(recorder, "tab-b")["recording_id"]
    assert recorder.client.get(url.replace(cid, other)).status_code == 404
    recorder.client.headers["x-test-user"] = "mallory"
    assert recorder.client.get(url).status_code == 404


def test_session_scope_uses_owned_session_facade(recorder, monkeypatch):
    import app.db
    capture = start(recorder)
    seen = []
    class FakeDB:
        async def assert_session_owned(self, user, session):
            seen.append((user, session))
            if session != "owned-session":
                raise PermissionError("Not owned")
    monkeypatch.setattr(app.db, "get_db", lambda: FakeDB())
    path = f"/api/v1/recordings/captures/{capture['recording_id']}/scope"
    assert recorder.client.post(path, json={**proof(capture), "session_id": "other-session"}).status_code == 409
    response = recorder.client.post(path, json={**proof(capture), "session_id": "owned-session", "metadata": {"owner": "mallory", "theme": "dark"}})
    assert response.status_code == 200, response.text
    assert response.json()["capture"]["sessions"] == ["owned-session"]
    assert response.json()["capture"]["owner"] == "alice"
    assert seen == [("alice", "other-session"), ("alice", "owned-session")]


def test_clean_stop_is_final_and_metadata_cannot_fabricate_approval(recorder):
    capture = start(recorder)
    cid = capture["recording_id"]
    path = f"/api/v1/recordings/captures/{cid}"
    response = recorder.client.post(path + "/scope", json={**proof(capture), "metadata": {
        "investigation": {"state": "approved", "approval": {"decision": "approve", "revision": 1}},
        "plan": {"revision": 1}, "owner": "mallory"}})
    assert response.status_code == 200
    assert response.json()["capture"]["investigation"] == {}
    assert post_records(recorder, capture, [record(capture)]).status_code == 200
    stopped = recorder.client.post(path + "/stop", json={**proof(capture), "last_seq": 0, "acknowledged": True})
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["capture"]["state"] == "completed"
    assert stopped.json()["report"]["provisional"] is False
    assert recorder.client.post(path + "/execute", json={"plan_revision": 1}).status_code == 403


def test_stop_waits_for_dequeued_backend_write(recorder, monkeypatch):
    import asyncio
    import threading
    archive = recorder.store
    coordinator = recorder.service
    capture = archive.begin("alice", "tab-a")
    cid = capture["id"]
    entered, release = threading.Event(), threading.Event()
    append = archive.append

    def paused_append(*args):
        entered.set()
        assert release.wait(3), "Test release timed out"
        return append(*args)

    monkeypatch.setattr(archive, "append", paused_append)
    coordinator.pending.append((cid, {"id": "backend-fact", "client_id": "server", "kind": "trace", "seq": "server-seq", "detail": {}}))

    async def scenario():
        writer = asyncio.create_task(coordinator.flush())
        assert await asyncio.to_thread(entered.wait, 2)
        stopping = asyncio.create_task(coordinator.stop_capture(cid, "alice", "tab-a", -1))
        try:
            await asyncio.sleep(0.1)
            assert not stopping.done(), "Stop finalized before an already-dequeued backend write was durable"
        finally:
            release.set()
            await asyncio.gather(writer, stopping)
        assert archive.events(cid)["events"][0]["id"] == "backend-fact"

    asyncio.run(scenario())


def test_provisional_session_uuid_and_existing_foreign_session(recorder, monkeypatch):
    import app.db
    fresh = "89b6b79d-9093-44c3-8683-a6647b7e6d27"
    foreign = "7f702084-c18e-44b2-8e1a-e94e06aaeb65"
    class FakeDB:
        async def assert_session_owned(self, uid, sid):
            raise PermissionError("Not currently owned")
        async def get_session_status(self, sid):
            return "active" if sid == foreign else None
    monkeypatch.setattr(app.db, "get_db", lambda: FakeDB())
    capture = start(recorder)
    path = f"/api/v1/recordings/captures/{capture['recording_id']}/scope"
    assert recorder.client.post(path, json={**proof(capture), "session_id": foreign}).status_code == 403
    assert recorder.client.post(path, json={**proof(capture), "session_id": "not-a-uuid"}).status_code == 422
    response = recorder.client.post(path, json={**proof(capture), "session_id": fresh})
    assert response.status_code == 200, response.text
    assert response.json()["capture"]["sessions"] == [fresh]


def test_foreign_stream_cannot_enter_provisional_scope(recorder):
    import asyncio
    capture = recorder.store.begin("alice", "tab")
    cid = capture["id"]
    capture = recorder.store.scope(cid, "alice", "provisional-session")
    recorder.store.publish_scopes()
    recorder.service.observe("stream.emit", {"session_id": "provisional-session", "user_id": "mallory", "type": "response", "content": "foreign content"})
    recorder.service.observe("stream.emit", {"session_id": "provisional-session", "user_id": "alice", "type": "response", "content": "owner content"})
    asyncio.run(recorder.service.flush())
    rows = recorder.store.events(cid)["events"]
    assert len(rows) == 1
    assert rows[0]["detail"]["text_length"] == len("owner content")
    assert "foreign content" not in json.dumps(rows)


def test_marker_retry_returns_original_after_finalization_and_approval(recorder):
    capture = start(recorder)
    cid = capture["recording_id"]
    path = f"/api/v1/recordings/captures/{cid}/markers"
    payload = {**proof(capture), "marker_id": "stable-marker", "note": "Original issue"}
    original = recorder.client.post(path, json=payload)
    assert original.status_code == 200, original.text
    recorder.store.finish(cid, "alice", "tab-a", -1)
    recorder.store.investigation(cid, state="plan_ready", plan={"revision": 1})
    recorder.store.approve(cid, "alice", 1, "approve", execution_agent_id="dev-agent")
    # Deliberately changed retry data cannot replace the original or invalidate approval.
    retry = recorder.client.post(path, json={**payload, "note": "Changed retry", "screenshot_data_url": "invalid-image"})
    assert retry.status_code == 200 and retry.json() == original.json()
    detail = recorder.store.detail(cid, "alice")
    assert len(detail["markers"]) == 1
    assert detail["investigation"]["state"] == "approved"


@pytest.mark.parametrize("terminal", ["completed", "incomplete"])
def test_late_marker_invalidates_only_unapproved_plan(recorder, terminal):
    capture = start(recorder)
    cid = capture["recording_id"]
    if terminal == "incomplete":
        recorder.store.gap(cid, "Missing frame")
    recorder.store.finish(cid, "alice", "tab-a", -1)
    recorder.store.investigation(cid, state="plan_ready", plan={"revision": 4}, job_id="old-job", fingerprint="old-evidence")
    response = recorder.client.post(f"/api/v1/recordings/captures/{cid}/markers", json={**proof(capture), "marker_id": "late-marker", "note": "Screenshot upload arrived after stop"})
    assert response.status_code == 200, response.text
    detail = recorder.store.detail(cid, "alice")
    assert detail["capture"]["state"] == terminal
    assert detail["investigation"]["state"] == "evidence_changed"
    assert detail["investigation"]["plan"] is None and detail["investigation"]["approval"] is None
    assert detail["investigation"]["plan_revision"] == 4
    assert detail["investigation"]["job_id"] is None
    assert recorder.service.jobs == {}


@pytest.mark.parametrize("protected", ["approved", "executing", "dispatching", "execution_started"])
def test_new_marker_cannot_change_approved_evidence(recorder, protected):
    capture = start(recorder)
    cid = capture["recording_id"]
    recorder.store.finish(cid, "alice", "tab-a", -1)
    recorder.store.investigation(cid, state=protected, plan={"revision": 2}, approval={"decision": "approve", "revision": 2})
    response = recorder.client.post(f"/api/v1/recordings/captures/{cid}/markers", json={**proof(capture), "marker_id": "new-late-marker", "note": "New issue"})
    assert response.status_code == 409, response.text
    assert recorder.store.detail(cid, "alice")["markers"] == []
    assert recorder.store.get(cid)["investigation"]["state"] == protected


def test_shared_workers_deduplicate_marker_transaction(recorder):
    from concurrent.futures import ThreadPoolExecutor
    capture = start(recorder)
    cid = capture["recording_id"]
    second = CaptureStore(recorder.store.path)
    data = {"marker_id": "same-marker", "client_id": "tab-a", "note": "Same issue"}
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(recorder.store.marker, cid, data)
        b = pool.submit(second.marker, cid, data)
        assert a.result() == b.result()
    assert len(recorder.store.detail(cid, "alice")["markers"]) == 1
    other = recorder.store.begin("alice", "tab-b")
    with pytest.raises(ValueError):
        second.marker(other["id"], data)
