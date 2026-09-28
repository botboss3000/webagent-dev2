"""RECORDER-HOOK: removable five-second process memory.
Only leases/promotion acknowledgements cross workers, never armed payloads.
"""
from collections import deque
from contextlib import contextmanager
import fcntl
import json
import os
import threading
import time

WINDOW = 5.0
LIMIT = 16 * 1024 * 1024

class Prebuffer:
    def __init__(self, store, worker_id):
        self.path = store.path.with_name("armed.json")
        self.worker_id = worker_id
        self.lock = threading.RLock()
        self.stamp = None
        self.state = {"arms": {}, "promotions": {}}
        self.rings = {}
        self.pinned = {}
        self.done = set()
        self.lost = deque()

    @staticmethod
    def key(owner, client_id):
        return json.dumps([owner, client_id], separators=(",", ":"))

    @contextmanager
    def edit(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_suffix(".lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try: state = json.loads(self.path.read_text())
            except FileNotFoundError: state = {"arms": {}, "promotions": {}}
            stamp = time.time()
            state["arms"] = {k:v for k,v in state["arms"].items() if v["expires"] > stamp}
            state["promotions"] = {k:v for k,v in state["promotions"].items() if v["expires"] > stamp}
            yield state
            temporary = self.path.with_name("armed." + str(os.getpid()) + ".tmp")
            temporary.write_text(json.dumps(state, separators=(",", ":")))
            temporary.replace(self.path)

    def arm(self, owner, client_id, enabled, session_id=None, agent_id=None):
        key = self.key(owner, client_id)
        with self.edit() as state:
            if enabled:
                previous=state["arms"].get(key,{})
                scopes={k:v for k,v in previous.get("scopes",{}).items() if v>time.time()}
                if previous.get("session_id"): scopes[previous["session_id"]]=time.time()+5
                if session_id: scopes[session_id]=time.time()+30
                state["arms"][key] = {"scopes":scopes,"owner": owner, "client_id": client_id,
                    "session_id": session_id, "agent_id": agent_id, "expires": time.time()+30}
            else: state["arms"].pop(key, None)
        self.sync()
        return {"armed": enabled, "lease_expires": time.time()+30 if enabled else None,
                "requested_ms": 5000, "server_time_ms": time.time()*1000}

    def promote(self, cid, owner, client_id, workers):
        with self.edit() as state:
            state["promotions"][cid] = {"key": self.key(owner, client_id),
                "boundary": time.monotonic(), "expires": time.time()+30,
                "workers": workers, "results": {}}
        self.sync()

    def sync(self):
        with self.lock:
            try:
                stat = self.path.stat()
                stamp = (stat.st_mtime_ns, stat.st_size)
                if stamp != self.stamp:
                    state = json.loads(self.path.read_text())
                    if not isinstance(state,dict) or not all(isinstance(state.get(k),dict) for k in ("arms","promotions")):
                        raise ValueError("Invalid pre-buffer metadata")
                    for arm in state["arms"].values():
                        if not isinstance(arm,dict) or not isinstance(arm.get("expires"),(int,float)):
                            raise ValueError("Invalid armed lease")
                    for request in state["promotions"].values():
                        if not isinstance(request,dict) or not all(k in request for k in ("key","boundary","expires","workers","results")):
                            raise ValueError("Invalid promotion metadata")
                    self.state = state
                    self.stamp = stamp
            except (OSError,ValueError,TypeError):
                self.state = {"arms": {}, "promotions": {}}
            now = time.monotonic()
            for cid in list(self.pinned):
                request=self.state["promotions"].get(cid)
                if not request or request["expires"] <= time.time():
                    self.pinned.pop(cid)
                    self.lost.append((cid,"Backend pre-buffer promotion expired before persistence"))
            active = {k:v for k,v in self.state["arms"].items() if v["expires"] > time.time()}
            # Freeze before eviction; delayed workers report shortened coverage.
            for cid, request in self.state["promotions"].items():
                if self.worker_id not in request["workers"]: continue
                if cid in self.done or cid in self.pinned or request["expires"] <= time.time(): continue
                ring = self.rings.get(request["key"])
                boundary = request["boundary"]
                events = [event for ts,size,event in ring["events"] if boundary-WINDOW <= ts <= now] if ring else []
                begin = max(boundary-WINDOW, ring["since"]) if ring else boundary
                reasons = list(ring["reasons"]) if ring else ["worker_not_armed"]
                coverage = max(0, min(5000, (boundary-begin)*1000))
                if coverage < 4999 and not reasons: reasons.append("buffer_warming")
                sizes=[len(json.dumps(event,ensure_ascii=False).encode()) for event in events]
                reserved=sum(result.get("_bytes",0) for _,result in self.pinned.values())
                kept=sum(sizes)
                while events and reserved+kept>LIMIT:
                    events.pop(0)
                    kept-=sizes.pop(0)
                    coverage=0
                    if "promotion_memory_limit" not in reasons: reasons.append("promotion_memory_limit")
                self.pinned[cid] = (events, {"_bytes":kept,"available_ms": coverage,
                    "event_count": len(events), "limitations": reasons,
                    "channels": ["stream", "backend", "correlated_server"],
                    "uncorrelated_server_logs": "not_buffered"})
            for key in list(self.rings):
                if key not in active: del self.rings[key]
            for key, arm in active.items():
                ring = self.rings.get(key)
                if ring is None:
                    self.rings[key] = {"scope": arm.get("session_id"), "since": now,
                        "events": deque(), "bytes":0, "reasons": []}
            self.evict(now)
            self.done.intersection_update(self.state["promotions"])

    def evict(self, now):
        for ring in self.rings.values():
            while ring["events"] and ring["events"][0][0] < now-WINDOW:
                _,size,_ = ring["events"].popleft()
                ring["bytes"] -= size
            ring["since"] = max(ring["since"], now-WINDOW)
            if now-ring.get("last_drop",float("-inf"))>=WINDOW: ring["reasons"]=[]
        total = sum(ring["bytes"] for ring in self.rings.values())
        budget=max(0,LIMIT-sum(result.get("_bytes",0) for _,result in self.pinned.values()))
        while total > budget:
            ring = min((r for r in self.rings.values() if r["events"]), key=lambda r:r["events"][0][0])
            ts,size,_ = ring["events"].popleft()
            total -= size
            ring["bytes"] -= size
            ring["since"] = max(ring["since"], ts)
            ring["last_drop"] = now
            if "memory_limit" not in ring["reasons"]: ring["reasons"].append("memory_limit")

    def active(self):
        self.sync()
        return bool(self.rings)

    def observe(self, event, payload):
        # Uncorrelated logs could expose another user's activity.
        with self.lock:
            sid, parent = payload.get("session_id"), payload.get("parent_session_id")
            for key, ring in self.rings.items():
                arm = self.state["arms"].get(key, {})
                scopes={s for s,expires in arm.get("scopes",{}).items() if expires>time.time()}
                if not scopes.intersection(s for s in (sid,parent) if s): continue
                if payload.get("user_id") and str(payload["user_id"]) != arm.get("owner"): continue
                size = len(json.dumps(event, ensure_ascii=False).encode())
                ring["events"].append((time.monotonic(), size, event))
                ring["bytes"] += size
            self.evict(time.monotonic())

    def acknowledge(self, cid, result):
        with self.edit() as state:
            if cid in state["promotions"]:
                state["promotions"][cid]["results"][self.worker_id] = {k:v for k,v in result.items() if not k.startswith("_")}
        with self.lock:
            self.pinned.pop(cid, None)
            self.done.add(cid)

    def results(self, cid):
        self.sync()
        return self.state["promotions"].get(cid, {}).get("results", {})

    def clear(self, include_pins=True):
        with self.lock:
            self.rings.clear()
            if include_pins: self.pinned.clear()

    def take_lost(self):
        with self.lock:
            result=list(self.lost)
            self.lost.clear()
            return result

