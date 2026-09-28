"""Durable capture archive — owns ONLY data/recorder/recorder.sqlite3.

API/service callers use asyncio.to_thread; no database work runs on render or
stream callbacks. This is diagnostic storage, never a session database. Batch
identity is (capture, document, seq), making acknowledgement/retry idempotent.
Reports and approval revisions live here; GenUI data is not authorization.
"""
from __future__ import annotations
import contextlib
import json
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
def now():
    return datetime.now(timezone.utc).isoformat()

def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)

class CaptureStore:
    def __init__(self, path=None):
        self.path = Path(path or ROOT / "data/recorder/recorder.sqlite3")
        self.lock = threading.RLock()
        self.ready = False

    @contextlib.contextmanager
    def db(self):
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path, timeout=10)
            conn.row_factory = sqlite3.Row
            try:
                if not self.ready:
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.executescript("""
                    CREATE TABLE IF NOT EXISTS captures(
                      id TEXT PRIMARY KEY, owner TEXT NOT NULL, state TEXT NOT NULL,
                      started_at TEXT NOT NULL, updated_at TEXT NOT NULL, meta TEXT NOT NULL,
                      report TEXT, investigation TEXT NOT NULL DEFAULT '{}');
                    CREATE TABLE IF NOT EXISTS documents(
                      capture_id TEXT NOT NULL, client_id TEXT NOT NULL, expires REAL NOT NULL,
                      PRIMARY KEY(capture_id,client_id));
                    CREATE TABLE IF NOT EXISTS events(
                      cursor INTEGER PRIMARY KEY AUTOINCREMENT, capture_id TEXT NOT NULL,
                      client_id TEXT NOT NULL, seq TEXT NOT NULL, event TEXT NOT NULL,
                      UNIQUE(capture_id,client_id,seq));
                    CREATE INDEX IF NOT EXISTS events_capture ON events(capture_id,cursor);
                    CREATE TABLE IF NOT EXISTS workers(
                      id TEXT PRIMARY KEY, pid INTEGER NOT NULL);
                    CREATE TABLE IF NOT EXISTS flush_acks(
                      capture_id TEXT NOT NULL, worker_id TEXT NOT NULL,
                      PRIMARY KEY(capture_id,worker_id));
                    CREATE TABLE IF NOT EXISTS markers(
                      id TEXT PRIMARY KEY, capture_id TEXT NOT NULL, data TEXT NOT NULL,
                      mime TEXT, image BLOB);
                    """)
                    self.ready = True
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def _capture(self, row):
        if row is None:
            return None
        item = dict(row)
        item.update(json.loads(item.pop("meta")))
        report = json.loads(item.pop("report") or "{}")
        item["investigation"] = json.loads(item["investigation"])
        item["report_summary"] = {k: report.get(k) for k in ("result", "provisional", "rule_set_version")}
        item["report_summary"]["findings"] = len(report.get("findings") or [])
        for result in ("pass","fail","blocked","not_run"):
            item["report_summary"][result] = sum(c.get("result")==result for c in report.get("checks",[]))
        return item

    def begin(self, owner, client_id, recording_id=None, expires=None):
        stamp = now()
        with self.db() as db:
            cid = recording_id or str(uuid.uuid4())
            row = db.execute("SELECT * FROM captures WHERE id=?", (cid,)).fetchone()
            if row:
                if row["owner"] != owner:
                    raise PermissionError("Recording owner mismatch")
                if row["state"] not in ("recording", "degraded", "interrupted"):
                    raise ValueError("Recording has ended")
                meta = json.loads(row["meta"])
            elif recording_id:
                raise ValueError("Unknown recording")
            else:
                meta = {"sessions": [], "gaps": [], "started_midrun": True,
                        "channels": {"browser": "pending", "stream": "active",
                          "cache": "pending", "backend": "active", "server": "active",
                          "screenshots": "optional", "host": "unavailable"},
                        "limitations": ["Host/container/tunnel logs are not attached; application-process server logs are captured."],
                        "schema_version": 1}
                db.execute("INSERT INTO captures(id,owner,state,started_at,updated_at,meta) VALUES(?,?,?,?,?,?)",
                           (cid, owner, "recording", stamp, stamp, encode(meta)))
            db.execute("INSERT INTO documents VALUES(?,?,?) ON CONFLICT(capture_id,client_id) DO UPDATE SET expires=excluded.expires",
                       (cid, client_id, expires or time.time()+600))
            db.execute("UPDATE captures SET updated_at=?,state=? WHERE id=?",
                       (stamp, "degraded" if meta.get("gaps") else "recording", cid))
        return self.get(cid, owner)

    def get(self, cid, owner=None):
        with self.db() as db:
            row = db.execute("SELECT * FROM captures WHERE id=?", (cid,)).fetchone()
            if row and owner is not None and row["owner"] != owner:
                raise PermissionError("Recording owner mismatch")
            return self._capture(row)

    def detail(self, cid, owner):
        with self.db() as db:
            row = db.execute("SELECT * FROM captures WHERE id=? AND owner=?", (cid,owner)).fetchone()
            if not row:
                raise KeyError(cid)
            return {"capture": self._capture(row),
                    "report": json.loads(row["report"] or "{}"),
                    "investigation": json.loads(row["investigation"]),
                    "markers": [json.loads(r["data"]) for r in db.execute(
                        "SELECT data FROM markers WHERE capture_id=? ORDER BY rowid", (cid,))]}

    def list(self, owner, limit=100):
        with self.db() as db:
            return [self._capture(r) for r in db.execute(
                "SELECT * FROM captures WHERE owner=? ORDER BY started_at DESC LIMIT ?", (owner,limit))]

    def active(self):
        with self.db() as db:
            return [self._capture(r) for r in db.execute(
                "SELECT * FROM captures WHERE state IN ('recording','degraded','finalizing')")]

    def verify_document(self, cid, owner, client_id):
        with self.db() as db:
            return db.execute("""SELECT 1 FROM documents d JOIN captures c ON c.id=d.capture_id
              WHERE d.capture_id=? AND c.owner=? AND d.client_id=?""", (cid,owner,client_id)).fetchone() is not None

    def scope(self, cid, owner, session_id, agent_id=None, metadata=None):
        with self.db() as db:
            row = db.execute("SELECT * FROM captures WHERE id=? AND owner=?", (cid,owner)).fetchone()
            if not row:
                raise KeyError(cid)
            meta = json.loads(row["meta"])
            if session_id and session_id not in meta["sessions"]:
                meta["sessions"].append(session_id)
            if agent_id:
                meta.setdefault("agents", [])
                if agent_id not in meta["agents"]:
                    meta["agents"].append(agent_id)
            # Only observational metadata; never permissions, owner or approval.
            for key in ("viewport","theme","storage_mode","cache_generation","started_midrun","prebuffer_browser"):
                if metadata and key in metadata:
                    meta[key] = metadata[key]
            db.execute("UPDATE captures SET meta=?,updated_at=? WHERE id=?", (encode(meta),now(),cid))
        return self.get(cid,owner)

    def save_prebuffer(self,cid,owner,metadata):
        # Server-owned coverage is separate from observational browser metadata.
        with self.db() as db:
            row=db.execute("SELECT meta FROM captures WHERE id=? AND owner=?",(cid,owner)).fetchone()
            if not row: raise KeyError(cid)
            meta=json.loads(row["meta"])
            meta["prebuffer"]=metadata
            db.execute("UPDATE captures SET meta=?,updated_at=? WHERE id=?",(encode(meta),now(),cid))
        return self.get(cid,owner)

    def gap(self, cid, message):
        with self.db() as db:
            row = db.execute("SELECT meta,state FROM captures WHERE id=?", (cid,)).fetchone()
            if not row: return
            meta = json.loads(row["meta"])
            if message not in meta["gaps"]:
                meta["gaps"].append(message)
            state = "degraded" if row["state"] in ("recording","degraded") else row["state"]
            db.execute("UPDATE captures SET meta=?,state=?,updated_at=? WHERE id=?", (encode(meta),state,now(),cid))

    def append(self, cid, records):
        accepted = duplicates = 0
        with self.db() as db:
            row = db.execute("SELECT meta,state FROM captures WHERE id=?", (cid,)).fetchone()
            if not row: raise KeyError(cid)
            if row["state"] not in ("recording","degraded","interrupted","finalizing"):
                raise ValueError("Capture already finalized")
            meta = json.loads(row["meta"])
            for raw in records:
                record = dict(raw)
                record.setdefault("id", str(uuid.uuid4()))
                record.setdefault("ts",now())
                record["recording_id"] = cid
                client = str(record.get("client_id") or "server")
                seq = str(record.get("seq") if record.get("seq") is not None else record["id"])
                record["client_id"] = client
                # Server-side dedup does not conflate transport and interaction cursors.
                cursor = db.execute("INSERT OR IGNORE INTO events(capture_id,client_id,seq,event) VALUES(?,?,?,?)",
                                    (cid,client,seq,encode(record)))
                if cursor.rowcount:
                    accepted += 1
                    if record.get("kind") in ("snapshot","mutation","checkpoint"):
                        meta["channels"]["browser"] = "active"
                    if record.get("kind") == "trace" and "cache" in str(record.get("label","")):
                        meta["channels"]["cache"] = "active"
                    detail = record.get("detail") or {}
                    if isinstance(detail,dict) and record.get("kind") == "meta":
                        if record.get("label") in ("capture-start","DEBUG-TAG:capture-start") and "started_midrun" in detail:
                            meta["started_midrun"] = bool(detail["started_midrun"])
                        if "gap" in str(record.get("label")) or detail.get("truncated"):
                            reason = str(detail.get("reason") or record.get("label"))
                            if reason not in meta["gaps"]: meta["gaps"].append(reason)
                else:
                    duplicates += 1
            db.execute("UPDATE captures SET meta=?,updated_at=? WHERE id=?", (encode(meta),now(),cid))
        return {"accepted": accepted, "duplicates": duplicates, "dropped": 0, "persisted": True}

    def events(self, cid, after=0, limit=100):
        with self.db() as db:
            rows = db.execute("SELECT cursor,event FROM events WHERE capture_id=? AND cursor>? ORDER BY cursor LIMIT ?",
                              (cid,after,limit+1)).fetchall()
            return {"events":[{**json.loads(r["event"]),"cursor":r["cursor"]} for r in rows[:limit]],
                    "next_cursor":rows[min(len(rows),limit)-1]["cursor"] if rows else after,
                    "has_more":len(rows)>limit}

    def audit_records(self, cid, limit=100000):
        with self.db() as db:
            # Mutations can be large; rules consume structured facts/checkpoints.
            rows=db.execute("SELECT event FROM events WHERE capture_id=? ORDER BY cursor LIMIT ?",
                            (cid,limit+1)).fetchall()
            if len(rows)>limit:
                raise OverflowError("Audit evidence limit exceeded; report requires segmented analysis")
            records=[]
            for r in rows:
                event=json.loads(r["event"])
                event.pop("html",None)
                records.append(event)
            return records

    def save_report(self, cid, report, expected_state=None):
        with self.db() as db:
            # A periodic audit started while recording must not overwrite the
            # final audit after another worker completed the capture.
            if expected_state is not None:
                result=db.execute("UPDATE captures SET report=?,updated_at=? WHERE id=? AND state=?",
                                  (encode(report),now(),cid,expected_state))
            else:
                result=db.execute("UPDATE captures SET report=?,updated_at=? WHERE id=?", (encode(report),now(),cid))
            return bool(result.rowcount)

    def finish(self, cid, owner, client_id, last_seq=None):
        with self.db() as db:
            row=db.execute("SELECT * FROM captures WHERE id=? AND owner=?", (cid,owner)).fetchone()
            if not row: raise KeyError(cid)
            meta=json.loads(row["meta"])
            if last_seq is not None:
                count=db.execute("SELECT COUNT(*) FROM events WHERE capture_id=? AND client_id=? AND CAST(seq AS INTEGER) BETWEEN 0 AND ?",
                                 (cid,client_id,int(last_seq))).fetchone()[0]
                if count != int(last_seq)+1:
                    message="Unacknowledged browser events at finalization"
                    if message not in meta["gaps"]: meta["gaps"].append(message)
            # Reloads use a new document ID. Account for the previous document
            # too: its persisted pagehide record is its acknowledged boundary.
            previous=db.execute("SELECT client_id FROM documents WHERE capture_id=? AND client_id!=?",(cid,client_id)).fetchall()
            for document in previous:
                doc=document["client_id"]
                boundary=db.execute("""SELECT MAX(CAST(seq AS INTEGER)) FROM events
                    WHERE capture_id=? AND client_id=? AND json_extract(event,'$.label')='document-pagehide'""",(cid,doc)).fetchone()[0]
                count=db.execute("SELECT COUNT(*) FROM events WHERE capture_id=? AND client_id=? AND CAST(seq AS INTEGER) BETWEEN 0 AND ?",(cid,doc,boundary if boundary is not None else -1)).fetchone()[0]
                if boundary is None or count!=boundary+1:
                    message="Previous browser document has an unacknowledged boundary or missing events"
                    if message not in meta["gaps"]: meta["gaps"].append(message)
            state="incomplete" if meta["gaps"] else "completed"
            meta["finished_at"]=now()
            db.execute("UPDATE captures SET state=?,meta=?,updated_at=? WHERE id=?", (state,encode(meta),now(),cid))
        return self.get(cid,owner)

    def interrupt(self, cid, reason):
        self.gap(cid,reason)
        with self.db() as db:
            db.execute("UPDATE captures SET state='interrupted',updated_at=? WHERE id=?", (now(),cid))

    def expired(self, cid):
        with self.db() as db:
            expiry=db.execute("SELECT MAX(expires) FROM documents WHERE capture_id=?", (cid,)).fetchone()[0]
            return expiry is None or expiry < time.time()

    def marker(self, cid, data, image=None, mime=None):
        # Stable markers survive lost-ack retries even after finalization.
        # BEGIN IMMEDIATE serializes duplicate writes and approval/evidence
        # decisions across workers. New evidence invalidates an unapproved
        # analysis only; this method never enqueues another model call.
        mid = str(data.get("marker_id") or uuid.uuid4())
        data = {**data, "id":mid, "recording_id":cid, "created_at":now()}
        if image:
            data["artifact_id"]=mid
            data["artifact_url"]="/api/v1/recordings/captures/"+cid+"/artifacts/"+mid
        with self.db() as db:
            if not db.in_transaction: db.execute("BEGIN IMMEDIATE")
            existing=db.execute("SELECT capture_id,data FROM markers WHERE id=?", (mid,)).fetchone()
            if existing:
                if existing["capture_id"] != cid:
                    raise ValueError("Marker identifier already belongs to another capture")
                return json.loads(existing["data"])
            row=db.execute("SELECT state,investigation FROM captures WHERE id=?", (cid,)).fetchone()
            if not row or row["state"] not in ("recording","degraded","completed","incomplete"):
                raise ValueError("Capture is not accepting new issue markers")
            investigation=json.loads(row["investigation"])
            if (investigation.get("approval") or {}).get("decision")=="approve" or investigation.get("state") in (
                "approved","executing","dispatching","execution_started","completed"):
                raise ValueError("Approved investigation evidence is locked; start a new recording")
            db.execute("INSERT INTO markers VALUES(?,?,?,?,?)",(mid,cid,encode(data),mime,image))
            if investigation:
                revision=max(int(investigation.get("plan_revision") or 0),int((investigation.get("plan") or {}).get("revision") or 0))
                investigation.update(state="evidence_changed",plan=None,approval=None,
                                     fingerprint=None,job_id=None,plan_revision=revision,
                                     error="New issue evidence was added. Investigate again before reviewing a plan.")
            db.execute("UPDATE captures SET investigation=?,updated_at=? WHERE id=?",
                       (encode(investigation),now(),cid))
        return data

    def artifact(self, cid, artifact_id):
        with self.db() as db:
            row=db.execute("SELECT mime,image FROM markers WHERE capture_id=? AND id=?", (cid,artifact_id)).fetchone()
            if not row or not row["image"]: raise KeyError(artifact_id)
            return row["mime"],row["image"]

    def investigation(self, cid, **updates):
        with self.db() as db:
            row=db.execute("SELECT investigation FROM captures WHERE id=?", (cid,)).fetchone()
            if not row: raise KeyError(cid)
            data=json.loads(row["investigation"])
            data.update(updates)
            db.execute("UPDATE captures SET investigation=?,updated_at=? WHERE id=?", (encode(data),now(),cid))
            return data

    def approve(self, cid, owner, revision, decision, comment="", execution_agent_id=None):
        with self.db() as db:
            row=db.execute("SELECT investigation FROM captures WHERE id=? AND owner=?", (cid,owner)).fetchone()
            if not row: raise KeyError(cid)
            data=json.loads(row["investigation"])
            plan=data.get("plan") or {}
            if plan.get("revision") != revision or data.get("state") not in ("plan_ready","approved","rejected","execution_blocked"):
                raise ValueError("Plan revision is stale or not ready")
            data.update(state="approved" if decision=="approve" else "rejected",
                        approval={"revision":revision,"decision":decision,"by":owner,"at":now(),"comment":comment,"execution_agent_id":execution_agent_id})
            db.execute("UPDATE captures SET investigation=?,updated_at=? WHERE id=?", (encode(data),now(),cid))
            return data

    def claim_execution(self, cid, owner, revision):
        with self.db() as db:
            row=db.execute("SELECT investigation FROM captures WHERE id=? AND owner=?", (cid,owner)).fetchone()
            if not row: raise KeyError(cid)
            data=json.loads(row["investigation"])
            approval=data.get("approval") or {}
            if data.get("state")!="approved" or approval.get("decision")!="approve" or approval.get("revision")!=revision or (data.get("plan") or {}).get("revision")!=revision:
                raise PermissionError("This plan revision has no unconsumed human approval")
            data.update(state="executing",execution_started_at=now())
            db.execute("UPDATE captures SET investigation=?,updated_at=? WHERE id=?", (encode(data),now(),cid))
            return data

    def expire_evidence(self, retention_days=30):
        """Expire only this plugin's old evidence; retain report metadata with a gap."""
        from datetime import timedelta
        cutoff=(datetime.now(timezone.utc)-timedelta(days=max(1,int(retention_days)))).isoformat()
        with self.db() as db:
            rows=db.execute("SELECT id,meta,report FROM captures WHERE state IN ('completed','incomplete','interrupted') AND updated_at<?",(cutoff,)).fetchall()
            for row in rows:
                meta=json.loads(row["meta"])
                reason="Raw evidence expired under recorder retention policy"
                meta["gaps"].append(reason)
                report=json.loads(row["report"] or "{}")
                report["original_result"]=report.get("result")
                report.update(result="blocked",evidence_expired=True)
                report.setdefault("limitations",[]).append(reason)
                db.execute("DELETE FROM events WHERE capture_id=?",(row["id"],))
                db.execute("UPDATE markers SET image=NULL WHERE capture_id=?",(row["id"],))
                db.execute("UPDATE captures SET state='expired',meta=?,report=? WHERE id=?",(encode(meta),encode(report),row["id"]))
            return len(rows)

    @property
    def scope_path(self):
        return self.path.with_name("active.json")

    def publish_scopes(self):
        """Publish a token-free, atomic scope snapshot for all Uvicorn workers.

        Rare lifecycle writes, not per-event database queries. A file lock prevents
        an older publisher from overwriting a newer worker's committed scope.
        """
        import fcntl
        import os
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.path.with_name("active.lock").open("a") as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            with self.db() as db:
                rows=db.execute("""SELECT c.*,MAX(d.expires) AS lease_expires
                     FROM captures c JOIN documents d ON d.capture_id=c.id
                     WHERE c.state IN ('recording','degraded','finalizing')
                     GROUP BY c.id""").fetchall()
                scopes=[]
                for row in rows:
                    item=self._capture(row)
                    scopes.append({key:item.get(key) for key in
                        ("id","owner","sessions","state","lease_expires")})
            temporary=self.scope_path.with_name("active."+str(os.getpid())+".tmp")
            temporary.write_text(encode(scopes))
            temporary.replace(self.scope_path)

    def register_worker(self, worker_id, pid):
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO workers VALUES(?,?)",(worker_id,pid))

    def unregister_worker(self, worker_id):
        with self.db() as db:
            db.execute("DELETE FROM workers WHERE id=?",(worker_id,))

    def live_workers(self):
        import os
        with self.db() as db:
            rows=db.execute("SELECT * FROM workers").fetchall()
        alive=[]
        for row in rows:
            try: os.kill(row["pid"],0)
            except (ProcessLookupError,PermissionError): continue
            alive.append(row["id"])
        return alive

    def begin_finalization(self, cid, owner):
        with self.db() as db:
            row=db.execute("SELECT state FROM captures WHERE id=? AND owner=?",(cid,owner)).fetchone()
            if not row: raise KeyError(cid)
            if row["state"] in ("recording","degraded","interrupted"):
                db.execute("DELETE FROM flush_acks WHERE capture_id=?",(cid,))
                db.execute("UPDATE captures SET state='finalizing',updated_at=? WHERE id=?",(now(),cid))
        self.publish_scopes()

    def acknowledge_flush(self,cid,worker_id):
        with self.db() as db:
            db.execute("INSERT OR IGNORE INTO flush_acks VALUES(?,?)",(cid,worker_id))

    def flush_acknowledged(self,cid,workers):
        with self.db() as db:
            ack={r[0] for r in db.execute("SELECT worker_id FROM flush_acks WHERE capture_id=?",(cid,))}
        return set(workers).issubset(ack)

    def claim_investigation(self,cid,owner,worker_id=None):
        with self.db() as db:
            row=db.execute("SELECT investigation FROM captures WHERE id=? AND owner=?",(cid,owner)).fetchone()
            if not row: raise KeyError(cid)
            data=json.loads(row["investigation"])
            if data.get("worker_lost") or data.get("state") in ("queued","collecting","analyzing","plan_ready","approved","executing","dispatching","execution_started","completed"):
                return False
            data.update(state="queued",worker_id=worker_id,claimed_at=now())
            db.execute("UPDATE captures SET investigation=?,updated_at=? WHERE id=?",(encode(data),now(),cid))
            return True


    @staticmethod
    def _worker_alive(pid):
        """Probe only process existence; permission denial means unknown/live."""
        import os
        try:
            os.kill(int(pid), 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def recover_dead_workers(self):
        """Account for lost process evidence without restarting paid model work.

        Called by each service on startup/maintenance. One SQLite transaction
        claims recovery across processes. Jobs lacking worker ownership are
        left unchanged; plan-ready/approved/execution states are never rewritten.
        A recovered analysis stays blocked (worker_lost) until explicit future
        recovery handling is added; claim_investigation cannot silently retry it.
        """
        with self.db() as db:
            workers = db.execute("SELECT id,pid FROM workers").fetchall()
            registered = {row["id"] for row in workers}
            dead = {row["id"] for row in workers if not self._worker_alive(row["pid"])}
            recovered = []
            rows = db.execute("SELECT id,investigation FROM captures").fetchall()
            for row in rows:
                data = json.loads(row["investigation"])
                worker = data.get("worker_id")
                if (worker and (worker in dead or worker not in registered)
                    and data.get("state") in ("queued","collecting","analyzing")):
                    data.update(state="blocked",worker_lost=True,
                                error="Investigation worker stopped; no automatic model retry was made.",
                                error_type="WorkerLost",recovered_at=now())
                    db.execute("UPDATE captures SET investigation=?,updated_at=? WHERE id=?",
                               (encode(data),now(),row["id"]))
                    recovered.append(row["id"])
            affected = []
            if dead:
                captures = db.execute("SELECT id,state,meta FROM captures WHERE state IN ('recording','degraded','finalizing')").fetchall()
                for row in captures:
                    meta = json.loads(row["meta"])
                    reason = "Backend worker disappeared during capture; some process diagnostics may be missing"
                    if reason not in meta.setdefault("gaps",[]):
                        meta["gaps"].append(reason)
                    state = "degraded" if row["state"] in ("recording","degraded") else row["state"]
                    db.execute("UPDATE captures SET state=?,meta=?,updated_at=? WHERE id=?",
                               (state,encode(meta),now(),row["id"]))
                    affected.append(row["id"])
                db.executemany("DELETE FROM workers WHERE id=?", [(worker,) for worker in dead])
                db.executemany("DELETE FROM flush_acks WHERE worker_id=?", [(worker,) for worker in dead])
        if affected:
            self.publish_scopes()
        return {"dead_workers":sorted(dead),"blocked_investigations":recovered,
                "affected_captures":affected}

    def finish_investigation(self, cid, job_id, fingerprint, **updates):
        """Publish a plan only against the exact report/marker revision analyzed."""
        import hashlib
        with self.db() as db:
            row = db.execute("SELECT investigation,report FROM captures WHERE id=?", (cid,)).fetchone()
            if not row:
                raise KeyError(cid)
            data = json.loads(row["investigation"])
            if data.get("job_id") != job_id:
                return data
            material = {"report":json.loads(row["report"] or "{}"),
                        "markers":[json.loads(r["data"]) for r in db.execute(
                            "SELECT data FROM markers WHERE capture_id=? ORDER BY rowid",(cid,))]}
            current = hashlib.sha256(json.dumps(material,sort_keys=True,default=str).encode()).hexdigest()
            if current != fingerprint or data.get("state") not in ("collecting","analyzing"):
                data.update(state="evidence_changed",plan=None,approval=None,fingerprint=None,
                            error="Evidence changed during analysis; review the new evidence before investigating again.")
            else:
                data.update(updates)
            db.execute("UPDATE captures SET investigation=?,updated_at=? WHERE id=?",
                       (encode(data),now(),cid))
            return data
