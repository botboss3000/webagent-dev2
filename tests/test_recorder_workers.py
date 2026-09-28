"""Cross-worker capture coordination using separate stores over one isolated archive."""
import asyncio
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from plugins.recorder.store import CaptureStore
from plugins.recorder.service import RecorderService


@pytest.fixture
def workers(tmp_path):
    root = tmp_path / "checkout"
    plugin = root / "plugins/recorder"
    plugin.mkdir(parents=True)
    (plugin / "plugin.json").write_text("{}")
    path = root / "data/recorder/recorder.sqlite3"
    first, second = CaptureStore(path), CaptureStore(path)
    return first, second, RecorderService(first), RecorderService(second)


def begin(store, *, owner="alice", client="tab-a", session="s1", expires=None):
    capture = store.begin(owner, client, expires=expires)
    store.scope(capture["id"], owner, session)
    store.publish_scopes()
    return capture["id"]


def emit(service, *, session="s1", owner="alice"):
    service.observe("stream.emit", {"session_id": session, "user_id": owner,
                                  "type": "delta", "content": "hello"})


def test_other_worker_observes_new_capture_and_session_scope_immediately(workers):
    a, b, service_a, service_b = workers
    cid = begin(a)
    emit(service_b)
    asyncio.run(service_b.flush())
    rows = b.audit_records(cid)
    assert len(rows) == 1
    assert rows[0]["detail"]["text_length"] == 5
    assert rows[0]["worker_id"] == service_b.worker_id
    assert "content" not in rows[0]["detail"]


def test_scope_change_visible_without_worker_database_poll(workers, monkeypatch):
    a, b, service_a, service_b = workers
    cid = begin(a)
    service_b._sync_scopes()
    a.scope(cid, "alice", "s2")
    a.publish_scopes()
    def forbidden(*args, **kwargs):
        raise AssertionError("No database read allowed in observe path")
    monkeypatch.setattr(b, "db", forbidden)
    emit(service_b, session="s2")
    assert len(service_b.pending) == 1


def test_finalizing_stops_new_events_but_preserves_pending_for_flush(workers):
    a, b, service_a, service_b = workers
    cid = begin(a)
    emit(service_b)
    a.begin_finalization(cid, "alice")
    emit(service_b)
    assert len(service_b.pending) == 1
    asyncio.run(service_b.flush())
    assert len(b.audit_records(cid)) == 1


def test_stopping_one_capture_keeps_other_capture_live(workers):
    a, b, service_a, service_b = workers
    first = begin(a)
    second = begin(a, client="tab-b")
    a.finish(first, "alice", "tab-a")
    a.publish_scopes()
    emit(service_b)
    asyncio.run(service_b.flush())
    assert b.audit_records(first) == []
    assert len(b.audit_records(second)) == 1


def test_expired_capture_does_not_accept_new_backend_observations(workers):
    a, b, service_a, service_b = workers
    cid = begin(a, expires=time.time()-1)
    emit(service_b)
    assert not service_b.pending
    assert b.expired(cid) is True


def test_wrong_owner_and_unrelated_session_are_not_captured(workers):
    a, b, service_a, service_b = workers
    cid = begin(a)
    emit(service_b, owner="mallory")
    emit(service_b, session="unrelated")
    assert not service_b.pending


def test_worker_shutdown_does_not_globally_interrupt_capture(workers):
    a, b, service_a, service_b = workers
    cid = begin(a)
    b.register_worker(service_b.worker_id, os.getpid())
    service_b._sync_scopes()
    asyncio.run(service_b.shutdown())
    record = a.get(cid, "alice")
    assert record["state"] in ("recording", "degraded")
    assert "A backend worker stopped during capture" in record["gaps"]
    assert service_b.worker_id not in a.live_workers()


def test_acknowledgements_are_shared_and_require_every_live_worker(workers):
    a, b, service_a, service_b = workers
    cid = begin(a)
    a.register_worker(service_a.worker_id, os.getpid())
    b.register_worker(service_b.worker_id, os.getpid())
    a.begin_finalization(cid, "alice")
    workers = a.live_workers()
    a.acknowledge_flush(cid, service_a.worker_id)
    assert b.flush_acknowledged(cid, workers) is False
    b.acknowledge_flush(cid, service_b.worker_id)
    assert a.flush_acknowledged(cid, workers) is True


def test_investigation_claim_is_unique_across_store_instances(workers):
    a, b, service_a, service_b = workers
    cid = begin(a)
    stores = [CaptureStore(a.path) for _ in range(12)]
    # Initialize schemas before simultaneous claims, so this targets claim isolation.
    for store in stores:
        store.get(cid, "alice")
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda store: store.claim_investigation(cid, "alice"), stores))
    assert results.count(True) == 1


def test_finalization_ack_is_not_ahead_of_pending_batches(workers, monkeypatch):
    a, b, service_a, service_b = workers
    cid = begin(a)
    b.register_worker(service_b.worker_id, os.getpid())
    for _ in range(2100):
        emit(service_b)
    a.begin_finalization(cid, "alice")
    iterations = 0
    async def one_tick(*args):
        nonlocal iterations
        iterations += 1
        if iterations > 1:
            raise asyncio.CancelledError()
    monkeypatch.setattr("plugins.recorder.service.asyncio.sleep", one_tick)
    asyncio.run(service_b._run())
    if b.flush_acknowledged(cid, [service_b.worker_id]):
        assert not service_b.pending
        assert len(a.audit_records(cid)) == 2100
    else:
        assert service_b.pending


def test_execution_approval_is_consumed_once_across_workers(workers):
    a, b, service_a, service_b = workers
    cid = begin(a)
    a.investigation(cid, state="plan_ready", plan={"revision": 1})
    a.approve(cid, "alice", 1, "approve")
    stores = [CaptureStore(a.path) for _ in range(8)]
    for store in stores:
        store.get(cid, "alice")
    def claim(store):
        try:
            return store.claim_execution(cid, "alice", 1)["state"] == "executing"
        except PermissionError:
            return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(claim, stores)).count(True) == 1


def test_dead_worker_recovery_blocks_analysis_without_retry_and_preserves_live_jobs(workers, monkeypatch):
    a, b, service_a, service_b = workers
    dead_capture = begin(a)
    live_capture = begin(a, client="tab-live")
    a.register_worker("dead-worker", 123)
    b.register_worker("live-worker", 456)
    a.claim_investigation(dead_capture, "alice", worker_id="dead-worker")
    b.claim_investigation(live_capture, "alice", worker_id="live-worker")
    a.investigation(dead_capture, state="analyzing")
    monkeypatch.setattr(CaptureStore, "_worker_alive", staticmethod(lambda pid: pid == 456))
    result = b.recover_dead_workers()
    assert result["dead_workers"] == ["dead-worker"]
    assert result["blocked_investigations"] == [dead_capture]
    failed = a.get(dead_capture, "alice")["investigation"]
    assert failed["state"] == "blocked"
    assert failed["worker_lost"] is True
    assert a.claim_investigation(dead_capture, "alice", worker_id="live-worker") is False
    assert a.get(live_capture, "alice")["investigation"]["state"] == "queued"
    assert a.get(dead_capture, "alice")["gaps"]
    assert b.recover_dead_workers()["dead_workers"] == []


def test_missing_worker_recovery_preserves_ready_plan_and_unowned_jobs(workers):
    a, b, service_a, service_b = workers
    ready = begin(a)
    unowned = begin(a, client="unowned")
    orphan = begin(a, client="orphan")
    a.investigation(ready, state="plan_ready", worker_id="gone", plan={"revision": 2})
    a.claim_investigation(unowned, "alice")
    a.claim_investigation(orphan, "alice", worker_id="gone")
    result = b.recover_dead_workers()
    assert result["blocked_investigations"] == [orphan]
    assert a.get(ready, "alice")["investigation"]["plan"] == {"revision": 2}
    assert a.get(unowned, "alice")["investigation"]["state"] == "queued"

def test_late_provisional_audit_cannot_replace_final_report(workers):
    a, b, _, _ = workers
    cid = begin(a)
    observed_state = a.get(cid)["state"]
    b.finish(cid, "alice", "tab-a")
    final = {"result": "blocked", "provisional": False, "checks": []}
    assert b.save_report(cid, final, "completed") is True
    assert a.save_report(cid, {"result": "pass", "provisional": True}, observed_state) is False
    assert a.detail(cid, "alice")["report"] == final

def test_reload_checks_previous_document_boundary_and_sequence(workers):
    a, b, _, _ = workers
    cid = begin(a)
    a.append(cid, [{"client_id":"tab-a","seq":0,"kind":"meta","label":"capture-start","detail":{}},
                   {"client_id":"tab-a","seq":2,"kind":"meta","label":"document-pagehide","detail":{}}])
    b.begin("alice", "reloaded-tab", cid)
    result = b.finish(cid,"alice","reloaded-tab",-1)
    assert result["state"] == "incomplete"
    assert any("Previous browser document" in gap for gap in result["gaps"])


def test_generic_capture_start_preserves_known_start_scope(workers):
    a, _, _, _ = workers
    cid = begin(a)
    a.scope(cid,"alice",None,metadata={"started_midrun":False})
    a.append(cid,[{"client_id":"tab-a","seq":0,"kind":"meta","label":"capture-start","detail":{"root":"chat"}}])
    assert a.get(cid)["started_midrun"] is False
