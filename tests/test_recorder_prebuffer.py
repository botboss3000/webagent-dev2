"""Behavioral coverage for process-local pre-trigger memory and worker promotion."""
import asyncio
import json
import os
import pytest
from plugins.recorder.store import CaptureStore
from plugins.recorder.prebuffer import Prebuffer
from plugins.recorder.service import RecorderService

def clock(monkeypatch):
    value=[100.]
    monkeypatch.setattr("plugins.recorder.prebuffer.time.monotonic",lambda:value[0])
    monkeypatch.setattr("plugins.recorder.prebuffer.time.time",lambda:value[0])
    return value

def test_five_seconds_scope_owner_and_no_archive(tmp_path,monkeypatch):
    t=clock(monkeypatch)
    store=CaptureStore(tmp_path/"recorder.sqlite3")
    ring=Prebuffer(store,"worker")
    ring.arm("alice","tab",True,"session")
    ring.observe({"id":"old"},{"session_id":"session","user_id":"alice"})
    t[0]=101
    ring.observe({"id":"keep"},{"parent_session_id":"session","user_id":"alice"})
    ring.observe({"id":"foreign"},{"session_id":"session","user_id":"bob"})
    ring.observe({"id":"global"},{"message":"global"})
    t[0]=105.5
    ring.sync()
    assert not store.path.exists()
    assert "keep" not in ring.path.read_text()
    ring.promote("capture","alice","tab",["worker"])
    events,coverage=ring.pinned["capture"]
    assert [e["id"] for e in events]==["keep"]
    assert coverage["available_ms"]==5000

def test_disarm_expiry_and_scope_switch(tmp_path,monkeypatch):
    t=clock(monkeypatch)
    ring=Prebuffer(CaptureStore(tmp_path/"recorder.sqlite3"),"worker")
    ring.arm("alice","tab",True,"first")
    t[0]=101
    ring.arm("alice","tab",True,"second")
    ring.observe({"id":"first"},{"session_id":"first"})
    ring.observe({"id":"second"},{"session_id":"second"})
    assert len(next(iter(ring.rings.values()))["events"])==2
    t[0]=107
    ring.sync()
    ring.observe({"id":"too-late"},{"session_id":"first"})
    assert len(next(iter(ring.rings.values()))["events"])==0
    ring.arm("alice","tab",False)
    assert not ring.active()
    ring.arm("alice","tab",True,"first")
    t[0]+=31
    assert not ring.active()

def test_memory_limit_shortens_coverage_not_integrity(tmp_path,monkeypatch):
    t=clock(monkeypatch)
    monkeypatch.setattr("plugins.recorder.prebuffer.LIMIT",100)
    ring=Prebuffer(CaptureStore(tmp_path/"recorder.sqlite3"),"worker")
    ring.arm("alice","tab",True,"session")
    t[0]+=2
    ring.observe({"id":"oversized","data":"x"*200},{"session_id":"session"})
    ring.promote("capture","alice","tab",["worker"])
    events,result=ring.pinned["capture"]
    assert not events
    assert result["available_ms"]==0
    assert result["limitations"]==["memory_limit"]

async def two_workers(tmp_path):
    store=CaptureStore(tmp_path/"recorder.sqlite3")
    first=RecorderService(store)
    second=RecorderService(CaptureStore(store.path))
    for worker in (first,second):
        store.register_worker(worker.worker_id,os.getpid())
    first.prebuffer.arm("alice","tab",True,"session")
    for worker in (first,second):
        worker.observe("stream.emit",{"session_id":"session","user_id":"alice",
            "content":"do not persist this raw content","type":"delta"})
    capture=store.begin("alice","tab")
    store.scope(capture["id"],"alice","session")
    store.publish_scopes()
    first.prebuffer.promote(capture["id"],"alice","tab",[first.worker_id,second.worker_id])
    for worker in (first,second):
        await worker.flush()
        await worker.flush()
    records=store.events(capture["id"])["events"]
    assert len(records)==2
    assert all("text_sha256" in e["detail"] and "content" not in e["detail"] for e in records)
    assert len(first.prebuffer.results(capture["id"]))==2
    assert all(e["worker_id"] in (first.worker_id,second.worker_id) for e in records)

async def failed_promotion(tmp_path,monkeypatch):
    store=CaptureStore(tmp_path/"recorder.sqlite3")
    worker=RecorderService(store)
    worker.prebuffer.arm("alice","tab",True,"session")
    worker.observe("cache.read",{"session_id":"session","status":"hit"})
    capture=store.begin("alice","tab")
    worker.prebuffer.promote(capture["id"],"alice","tab",[worker.worker_id])
    original=store.append
    def fail(*args): raise OSError("controlled outage")
    monkeypatch.setattr(store,"append",fail)
    await worker.flush()
    assert worker.prebuffer.pinned
    monkeypatch.setattr(store,"append",original)
    await worker.flush()
    assert not worker.prebuffer.pinned
    assert len(store.events(capture["id"])["events"])==1

def test_two_workers_promote_redacted_evidence_once(tmp_path):
    asyncio.run(two_workers(tmp_path))

def test_failed_promotion_retains_original_ids(tmp_path,monkeypatch):
    asyncio.run(failed_promotion(tmp_path,monkeypatch))


def test_browser_metadata_cannot_replace_server_coverage(tmp_path):
    store=CaptureStore(tmp_path/"recorder.sqlite3")
    cid=store.begin("alice","tab")["id"]
    store.save_prebuffer(cid,"alice",{"requested_ms":5000,"server":{"available_ms":123}})
    result=store.scope(cid,"alice",None,metadata={"prebuffer":{"server":{"available_ms":5000}},
        "prebuffer_browser":{"available_ms":456}})
    assert result["prebuffer"]["server"]["available_ms"]==123
    assert result["prebuffer_browser"]["available_ms"]==456

def test_promotion_api_metadata_shape(tmp_path):
    async def run():
        store=CaptureStore(tmp_path/"recorder.sqlite3")
        worker=RecorderService(store)
        store.register_worker(worker.worker_id,os.getpid())
        worker.prebuffer.arm("alice","tab",True,"session")
        worker.observe("cache.read",{"session_id":"session","status":"hit"})
        cid=store.begin("alice","tab")["id"]
        result=await worker.promote_prebuffer(cid,"alice","tab")
        assert result["prebuffer"]["requested_ms"]==5000
        assert result["prebuffer"]["server"]["workers"][0]["worker_id"]==worker.worker_id
        assert result["prebuffer"]["server"]["missing_workers"]==[]
    asyncio.run(run())


def test_pending_pins_expire_and_share_memory_budget(tmp_path,monkeypatch):
    t=clock(monkeypatch)
    monkeypatch.setattr("plugins.recorder.prebuffer.LIMIT",250)
    ring=Prebuffer(CaptureStore(tmp_path/"recorder.sqlite3"),"worker")
    ring.arm("alice","tab",True,"session")
    ring.observe({"id":"one","text":"x"*100},{"session_id":"session"})
    ring.promote("first","alice","tab",["worker"])
    ring.observe({"id":"two","text":"y"*100},{"session_id":"session"})
    ring.promote("second","alice","tab",["worker"])
    resident=sum(r["bytes"] for r in ring.rings.values())+sum(r["_bytes"] for _,r in ring.pinned.values())
    assert resident<=250
    t[0]+=31
    ring.sync()
    assert not ring.pinned
    assert len(ring.take_lost())==2

def test_active_does_not_read_settings(tmp_path,monkeypatch):
    worker=RecorderService(CaptureStore(tmp_path/"recorder.sqlite3"))
    def forbidden(): raise AssertionError("hotpath must not parse settings")
    monkeypatch.setattr("plugins.recorder.service.settings",forbidden)
    for _ in range(1000): assert not worker.active()

def test_failed_capture_does_not_starve_promotion(tmp_path):
    async def run():
        store=CaptureStore(tmp_path/"recorder.sqlite3")
        worker=RecorderService(store)
        worker.prebuffer.arm("alice","tab",True,"session")
        worker.observe("cache.read",{"session_id":"session"})
        failed=store.begin("alice","tab")["id"]
        good=store.begin("alice","tab")["id"]
        worker.prebuffer.promote(failed,"alice","tab",[worker.worker_id])
        worker.prebuffer.promote(good,"alice","tab",[worker.worker_id])
        store.finish(failed,"alice","tab")
        await worker.flush()
        assert not worker.prebuffer.pinned
        assert len(store.events(good)["events"])==1
        assert "Backend pre-buffer could not be archived" in store.get(failed)["gaps"]
    asyncio.run(run())

def test_delayed_worker_reports_shortened_window(tmp_path,monkeypatch):
    t=clock(monkeypatch)
    store=CaptureStore(tmp_path/"recorder.sqlite3")
    first=Prebuffer(store,"first")
    second=Prebuffer(store,"second")
    first.arm("alice","tab",True,"session")
    second.sync()
    t[0]+=5
    second.observe({"id":"fact"},{"session_id":"session"})
    first.promote("capture","alice","tab",["first","second"])
    t[0]+=3
    # A worker whose previous tick already evicted a prefix cannot claim it.
    second.evict(t[0])
    second.sync()
    assert second.pinned["capture"][1]["available_ms"]==2000


@pytest.mark.parametrize("broken",["broken json","[]",'{"arms":{},"promotions":{"bad":{}}}'])
def test_invalid_metadata_fails_closed(tmp_path,broken):
    ring=Prebuffer(CaptureStore(tmp_path/"recorder.sqlite3"),"worker")
    ring.arm("alice","tab",True,"session")
    ring.path.write_text(broken)
    assert not ring.active()
