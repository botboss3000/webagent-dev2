"""Capture coordinator — connects leases, optional observation hooks and archive.

Stream/log callbacks only enqueue. One writer persists batches off the event loop;
per-capture subscriptions coexist and never mutate global diagnostics settings.
Lease expiry, restart and overflow become gaps, not fabricated passing reports.
"""
from __future__ import annotations
import asyncio
import collections
import hashlib
import logging
import os
import json
from pathlib import Path
import threading
import time
import uuid
from .store import CaptureStore, now
from .redaction import clean
from .config import settings
from .prebuffer import Prebuffer

class ProcessLogs(logging.Handler):
    def __init__(self, service):
        super().__init__(logging.INFO)
        self.service = service

    def emit(self, record):
        if record.name.startswith(("plugins.recorder","app.application_plugins","httpx","httpcore")):
            return
        if record.levelno < logging.WARNING and not record.name.startswith(("app.","uvicorn","watchfiles")):
            return
        if not self.service.active(): return
        self.service.observe("server.log", {
            "level":record.levelname.lower(), "source":record.name,
            "message":record.getMessage(), "line":record.lineno,
        })

class RecorderService:
    def __init__(self, store=None):
        self.store=store or CaptureStore()
        self.captures={}
        self.pending=collections.deque()
        self.pending_gaps=set()
        self.lock=threading.RLock()
        self.flush_lock=asyncio.Lock()
        self.task=None
        self.unsubscribe=None
        self.handler=None
        self.jobs={}
        self.last_audit={}
        self.loop=None
        self.worker_id=str(os.getpid())+"-"+str(uuid.uuid4())
        self._scope_stamp=None
        self._acked=set()
        self.prebuffer=Prebuffer(self.store,self.worker_id)
        self._available=True

    async def start(self):
        if self.task and not self.task.done(): return
        self._refresh_availability()
        self.loop=asyncio.get_running_loop()
        await asyncio.to_thread(self.store.register_worker,self.worker_id,os.getpid())
        await asyncio.to_thread(self.store.recover_dead_workers)
        self._sync_scopes()
        self._subscribe()
        self.task=asyncio.create_task(self._run(), name="recorder-writer")

    async def begin(self,owner,client_id,recording_id=None,expires=None):
        if not self.task or self.task.done(): await self.start()
        capture=await asyncio.to_thread(self.store.begin,owner,client_id,recording_id,expires)
        await asyncio.to_thread(self.store.publish_scopes)
        self._sync_scopes()
        self._subscribe()
        return capture

    def _subscribe(self):
        if self.unsubscribe: return
        from app.observation import subscribe
        self.unsubscribe=subscribe("recorder",self.observe,self.active)
        self.handler=ProcessLogs(self)
        logging.getLogger().addHandler(self.handler)

    def _release(self, force=False):
        # Every worker keeps a cheap listener so a remote worker's toggle can
        # activate it. No active snapshot means no event payload serialization.
        if not force: return
        if self.unsubscribe:
            self.unsubscribe()
            self.unsubscribe=None
        if self.handler:
            logging.getLogger().removeHandler(self.handler)
            self.handler=None

    async def scope(self,cid,owner,session_id,agent_id=None,metadata=None):
        capture=await asyncio.to_thread(self.store.scope,cid,owner,session_id,agent_id,metadata)
        await asyncio.to_thread(self.store.publish_scopes)
        self._sync_scopes()
        return capture

    def _sync_scopes(self):
        """Cheap mtime lookup; lifecycle writes are atomic and token-free."""
        try:
            stat=self.store.scope_path.stat()
            stamp=(stat.st_mtime_ns,stat.st_size)
            if stamp==self._scope_stamp: return
            scopes=json.loads(self.store.scope_path.read_text())
            with self.lock:
                self.captures={row["id"]:row for row in scopes}
                self._scope_stamp=stamp
        except (OSError,ValueError,KeyError):
            with self.lock:
                self.pending_gaps.update((cid,"Backend scope snapshot unavailable") for cid in self.captures)
                self.captures={}
                self._scope_stamp=None

    def _refresh_availability(self):
        # RECORDER-HOOK: configuration IO belongs to lifecycle ticks, never the
        # stream observation hot path. Ordinary active() reads cached state.
        self._available=settings()["enabled"] is True and Path(__file__).with_name("plugin.json").is_file()
        if not self._available: self.prebuffer.clear(include_pins=False)

    def active(self):
        self._sync_scopes()
        if not self._available: return False
        armed=self.prebuffer.active()
        return armed or any(c.get("state") != "finalizing" and (c.get("lease_expires") or 0)>=time.time() for c in self.captures.values())

    def observe(self,topic,payload):
        self._sync_scopes()
        if not self._available: return
        self.prebuffer.sync()
        if (not self.captures and not self.prebuffer.rings) or not isinstance(payload,dict): return
        sid=payload.get("session_id")
        parent=payload.get("parent_session_id")
        with self.lock:
            captures=tuple(self.captures.values())
        # Preserve semantically useful content fingerprints without duplicating
        # every streamed message/tool argument or placing secrets in log output.
        if topic=="stream.emit":
            detail={k:payload.get(k) for k in (
                "type","session_id","turn_id","interaction_id","id","session_seq",
                "interaction_seq","turn_seq","emit_time","status","step","tool",
                "parent_session_id","child_session_id","agent_id","replayed") if k in payload}
            content=payload.get("content") or payload.get("text") or payload.get("delta")
            if isinstance(content,str):
                detail["text_length"]=len(content.encode("utf-16-le"))//2
                detail["text_sha256"]=hashlib.sha256(content.encode()).hexdigest()
        else:
            detail=payload
        detail=clean(detail)
        event={"id":str(uuid.uuid4()),"kind":"trace","label":"DEBUG-TAG:"+topic,
               "ts":now(),"client_id":"server","seq":str(uuid.uuid4()),
               "session_id":sid,"turn_id":payload.get("turn_id"),
               "session_seq":payload.get("session_seq"),"worker_id":self.worker_id,"detail":detail}
        self.prebuffer.observe(event,payload)
        with self.lock:
            for capture in captures:
                current=self.captures.get(capture["id"]) or {}
                if current.get("state")=="finalizing" or (current.get("lease_expires") or 0)<time.time():
                    continue
                if topic=="stream.emit" and payload.get("user_id") and payload["user_id"] != capture["owner"]:
                    continue
                if topic!="server.log" and sid not in capture["sessions"] and parent not in capture["sessions"]:
                    continue
                if len(self.pending)>=20000:
                    self.pending_gaps.add((capture["id"],"Backend diagnostic queue overflow"))
                    continue
                if topic=="server.log":
                    event={**event,"detail":{**detail,"uncorrelated_process_context":True}}
                self.pending.append((capture["id"],event))

    async def arm(self,owner,client_id,enabled,session_id=None,agent_id=None):
        if not self.task or self.task.done(): await self.start()
        return await asyncio.to_thread(self.prebuffer.arm,owner,client_id,enabled,session_id,agent_id)

    async def promote_prebuffer(self,cid,owner,client_id):
        # RECORDER-HOOK: pinned prefixes survive failed archive writes; stable
        # event IDs make promotion/live overlap and retry idempotent.
        workers=await asyncio.to_thread(self.store.live_workers)
        await asyncio.to_thread(self.prebuffer.promote,cid,owner,client_id,workers)
        deadline=time.monotonic()+8
        while True:
            await self.flush()
            results=await asyncio.to_thread(self.prebuffer.results,cid)
            if set(workers).issubset(results) or time.monotonic()>=deadline: break
            await asyncio.sleep(.1)
        missing=sorted(set(workers)-set(results))
        metadata={"requested_ms":5000,"server":{"workers":[{"worker_id":key,**value} for key,value in results.items()],
            "available_ms":min((r["available_ms"] for r in results.values()),default=0) if not missing else 0,
            "missing_workers":missing,"boundary":"server_receipt",
            "limitations":["Network transit time separates browser click from server receipt."]}}
        return await asyncio.to_thread(self.store.save_prebuffer,cid,owner,metadata)

    async def _flush_prebuffer(self):
        self.prebuffer.sync()
        if not self._available: self.prebuffer.clear(include_pins=False)
        for cid,reason in self.prebuffer.take_lost():
            with self.lock: self.pending_gaps.add((cid,reason))
        for cid,(events,result) in tuple(self.prebuffer.pinned.items()):
            try:
                await asyncio.to_thread(self.store.append,cid,events)
            except (ValueError,KeyError):
                # One abandoned/finalized recording must not starve other
                # captures. Terminal failures release memory and report loss.
                result={**result,"available_ms":0,
                    "limitations":[*result["limitations"],"capture_not_accepting_promotion"]}
                with self.lock: self.pending_gaps.add((cid,"Backend pre-buffer could not be archived"))
            except Exception:
                # Keep this prefix for bounded retry, but allow unrelated work.
                continue
            await asyncio.to_thread(self.prebuffer.acknowledge,cid,result)

    async def flush(self):
        # A final stop must wait for a batch already dequeued by the background
        # writer, not merely observe an empty queue before its commit.
        async with self.flush_lock:
            await self._flush_prebuffer()
            return await self._flush_locked()

    async def _flush_locked(self):
        grouped={}
        with self.lock:
            while self.pending and sum(len(v) for v in grouped.values())<2000:
                cid,event=self.pending.popleft()
                grouped.setdefault(cid,[]).append(event)
            gaps=tuple(self.pending_gaps)
            self.pending_gaps.clear()
        for cid,events in grouped.items():
            try:
                await asyncio.to_thread(self.store.append,cid,events)
            except Exception:
                with self.lock:
                    for event in reversed(events):
                        self.pending.appendleft((cid,event))
                    self.pending_gaps.add((cid,"Backend archive write failed; batch retained for retry"))
                raise
        for cid,reason in gaps:
            await asyncio.to_thread(self.store.gap,cid,reason)

    async def audit(self,cid):
        from .rules import analyze
        capture=await asyncio.to_thread(self.store.get,cid)
        if not capture: return {}
        try:
            capture["status"]=capture["state"]
            records=await asyncio.to_thread(self.store.audit_records,cid)
            report=await asyncio.to_thread(analyze,records,capture)
        except OverflowError as exc:
            await asyncio.to_thread(self.store.gap,cid,str(exc))
            report={"result":"blocked","provisional":False,"checks":[],
                    "findings":[],"limitations":[str(exc)],"scope":"observed_transcript"}
        await asyncio.to_thread(self.store.save_report,cid,report,capture["state"])
        return report

    async def stop_capture(self,cid,owner,client_id,last_seq=None,gaps=None):
        for gap in (gaps or [])[:100]:
            await asyncio.to_thread(self.store.gap,cid,str(gap)[:500])
        # Publish the stop boundary to every process, then require each live
        # worker to commit its previously queued facts before final reporting.
        workers=await asyncio.to_thread(self.store.live_workers)
        await asyncio.to_thread(self.store.begin_finalization,cid,owner)
        self._sync_scopes()
        for _ in range(20):
            await self.flush()
            with self.lock:
                if not any(key==cid for key,_ in self.pending): break
        else:
            await asyncio.to_thread(self.store.gap,cid,"Backend queue still draining at stop")
        await asyncio.to_thread(self.store.acknowledge_flush,cid,self.worker_id)
        deadline=time.monotonic()+8
        while not await asyncio.to_thread(self.store.flush_acknowledged,cid,workers):
            if time.monotonic()>deadline:
                await asyncio.to_thread(self.store.gap,cid,"A backend worker did not acknowledge final flush")
                break
            await asyncio.sleep(.1)
        capture=await asyncio.to_thread(self.store.finish,cid,owner,client_id,last_seq)
        await asyncio.to_thread(self.store.publish_scopes)
        self._sync_scopes()
        report=await self.audit(cid)
        detail=await asyncio.to_thread(self.store.detail,cid,owner)
        if settings()["auto_investigate"] and (report.get("findings") or detail["markers"]):
            await self.investigate(cid,owner)
        return await asyncio.to_thread(self.store.detail,cid,owner)

    async def investigate(self,cid,owner):
        from .investigation import investigate
        prior=await asyncio.to_thread(self.store.get,cid,owner)
        if not prior: raise KeyError(cid)
        if cid in self.jobs and not self.jobs[cid].done():
            return prior["investigation"]
        state=prior["investigation"].get("state")
        if state in ("plan_ready","approved","executing","completed"):
            return prior["investigation"]
        if not await asyncio.to_thread(self.store.claim_investigation,cid,owner,self.worker_id):
            return (await asyncio.to_thread(self.store.get,cid,owner))["investigation"]
        task=asyncio.create_task(investigate(self.store,cid,owner),name="recorder-investigation")
        self.jobs[cid]=task
        def done(future):
            try: future.exception()
            except asyncio.CancelledError: pass
        task.add_done_callback(done)
        return {"state":"queued"}

    async def _run(self):
        ticks=0
        while True:
            try:
                await asyncio.sleep(2)
                self._refresh_availability()
                self._sync_scopes()
                await self.flush()
                for cid,capture in tuple(self.captures.items()):
                    if capture.get("state")=="finalizing" and cid not in self._acked:
                        # The ordinary writer commits at most 2,000 events.
                        # An ACK promises every pre-boundary event is durable.
                        for _ in range(20):
                            await self.flush()
                            with self.lock:
                                if not any(key==cid for key,_ in self.pending): break
                        else:
                            await asyncio.to_thread(self.store.gap,cid,"Backend queue still draining at stop")
                            continue
                        await asyncio.to_thread(self.store.acknowledge_flush,cid,self.worker_id)
                        self._acked.add(cid)
                ticks+=1
                if ticks%15==0:
                    await asyncio.to_thread(self.store.recover_dead_workers)
                if ticks%1800==0 and self.store.path.exists():
                    await asyncio.to_thread(self.store.expire_evidence,settings()["retention_days"])
                workers=await asyncio.to_thread(self.store.live_workers) if self.captures else []
                audit_worker=not workers or self.worker_id==min(workers)
                for cid,capture in tuple(self.captures.items()):
                    if settings()["enabled"] is not True or not (self.store.path.parents[2] / "plugins/recorder/plugin.json").exists():
                        await asyncio.to_thread(self.store.interrupt,cid,"Recorder plugin removed or disabled")
                        self.captures.pop(cid,None)
                        await asyncio.to_thread(self.store.publish_scopes)
                    elif ticks%15==0 and await asyncio.to_thread(self.store.expired,cid):
                        await asyncio.to_thread(self.store.interrupt,cid,"Capture lease expired without final acknowledgement")
                        self.captures.pop(cid,None)
                        await asyncio.to_thread(self.store.publish_scopes)
                        await self.audit(cid)
                    elif audit_worker and capture.get("state")!="finalizing" and time.monotonic()-self.last_audit.get(cid,0)>10:
                        await self.audit(cid)
                        self.last_audit[cid]=time.monotonic()
                self._release()
            except asyncio.CancelledError:
                break
            except Exception:
                # Preserve queue and signal unavailable storage on the next
                # successful write. Never print raw exceptions containing data.
                with self.lock:
                    self.pending_gaps.update((cid,"Recorder background processing failed") for cid in self.captures)

    async def shutdown(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task,return_exceptions=True)
            self.task=None
        await self.flush()
        for cid in tuple(self.captures):
            await asyncio.to_thread(self.store.gap,cid,"A backend worker stopped during capture")
        await asyncio.to_thread(self.store.unregister_worker,self.worker_id)
        self.captures.clear()
        self.prebuffer.clear()
        self._release(force=True)
        for task in self.jobs.values():
            if not task.done(): task.cancel()
        if self.jobs: await asyncio.gather(*self.jobs.values(),return_exceptions=True)

_service=None
def get_service():
    global _service
    if _service is None: _service=RecorderService()
    return _service
