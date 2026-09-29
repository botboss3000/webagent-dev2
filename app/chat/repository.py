"""Transactional owner-scoped storage. No network delivery occurs before commit."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import time
import uuid
from .contracts import Conflict, Fenced, KINDS, NotFound, TERMINAL

def uid():
    return str(uuid.uuid4())

def now():
    return datetime.now(timezone.utc).isoformat()

def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()

class ChatRepository:
    def __init__(self, owner_id, *, connect=None, cipher=None):
        self.owner_id = owner_id
        if connect is None:
            from app.db import get_user_db
            connect = get_user_db(owner_id)._get_conn
        if cipher is None:
            from app.encryption import get_encryption
            cipher = get_encryption()
            if cipher.level == "none":
                raise RuntimeError("Controlled chat requires encrypted payload storage")
        self.connect = connect
        self.cipher = cipher

    async def encode(self, value):
        return await self.cipher.encrypt(self.owner_id, canonical(value))

    async def decode(self, value):
        plain = await self.cipher.decrypt(self.owner_id, value)
        return json.loads(plain)

    @contextmanager
    def transaction(self, *, write=True):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _session(self, conn, session_id):
        row = conn.execute("SELECT * FROM sessions WHERE id=? AND user_id=?", (session_id, self.owner_id)).fetchone()
        if row is None or row["status"] != "active":
            raise NotFound("Session not found")
        return dict(row)

    def _run(self, conn, run_id, generation=None, states=None):
        row = conn.execute("SELECT * FROM chat_runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise NotFound("Run not found")
        self._session(conn, row["session_id"])
        if generation is not None and (row["generation"] != generation or row["lease_until"] <= time.time()):
            raise Fenced("Execution lease expired or superseded")
        if states is not None and row["state"] not in states:
            raise Fenced("Run no longer accepts this operation")
        return dict(row)

    def _publish(self, conn, *, session_id, run_id, kind, payload, item_id=None,
                 terminal=False, candidate_id=None, timestamp=None):
        if kind not in KINDS:
            raise ValueError("Unknown public item kind")
        self._session(conn, session_id)
        item_id = item_id or uid()
        timestamp = timestamp or now()
        conn.execute("INSERT INTO chat_counters(session_id,position) VALUES (?,0) ON CONFLICT DO NOTHING", (session_id,))
        conn.execute("UPDATE chat_counters SET position=position+1 WHERE session_id=?", (session_id,))
        position = conn.execute("SELECT position FROM chat_counters WHERE session_id=?", (session_id,)).fetchone()[0]
        conn.execute("""INSERT INTO chat_publications
            (id,session_id,run_id,terminal_run_id,position,kind,payload,candidate_id,created_at)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (item_id, session_id, run_id, run_id if terminal else None, position, kind, payload, candidate_id, timestamp))
        # Delivery retains the exact encrypted revision, not a pointer to mutable content.
        cursor = conn.execute("""INSERT INTO chat_delivery(session_id,item_id,revision,payload,created_at)
            VALUES (?,?,1,?,?)""", (session_id, item_id, payload, timestamp)).lastrowid
        conn.execute("UPDATE chat_segments SET closed=1 WHERE session_id=? AND closed=0", (session_id,))
        if run_id and not terminal:
            conn.execute("INSERT INTO chat_segments(id,session_id,run_id,boundary_item_id) VALUES (?,?,?,?)",
                         (uid(), session_id, run_id, item_id))
        return {"id": item_id, "position": position, "cursor": cursor, "revision": 1}

    def receipt_for(self, session_id, author_id, command):
        """Return a committed acceptance before metering a transport retry.

        Authorization still happens in the caller. A conflicting reuse of the
        client key is rejected even when the original send has already run.
        """
        payload = command.model_dump() if hasattr(command, "model_dump") else dict(command)
        fingerprint = digest(payload)
        with self.transaction(write=False) as conn:
            self._session(conn, session_id)
            old = conn.execute("""SELECT id,digest FROM chat_messages
                WHERE session_id=? AND author_id=? AND client_message_id=?""",
                (session_id, author_id, payload["client_message_id"])).fetchone()
            if old is None:
                return None
            if old["digest"] != fingerprint:
                raise Conflict("client_message_id already used with different content")
            run = conn.execute("SELECT run_id FROM chat_inbox WHERE message_id=?", (old["id"],)).fetchone()
            event = conn.execute("SELECT cursor FROM chat_delivery WHERE item_id=?", (old["id"],)).fetchone()
            return {"message_id": old["id"], "run_id": run["run_id"],
                    "cursor": event["cursor"], "duplicate": True}

    async def accept(self, session_id, author_id, command, policy):
        payload = command.model_dump() if hasattr(command, "model_dump") else dict(command)
        cmid = payload["client_message_id"]
        fingerprint = digest(payload)
        encrypted = await self.encode(payload)
        public = await self.encode({"text": payload.get("display_text") if payload.get("display_text") is not None
                                    else payload["text"], "attachment_ids": payload.get("attachment_ids", []),
                                    "author_id": author_id, "client_message_id": cmid})
        frozen_policy = await self.encode(policy)
        with self.transaction() as conn:
            self._session(conn, session_id)
            old = conn.execute("""SELECT * FROM chat_messages
                WHERE session_id=? AND author_id=? AND client_message_id=?""", (session_id, author_id, cmid)).fetchone()
            if old:
                if old["digest"] != fingerprint:
                    raise Conflict("client_message_id already used with different content")
                row = conn.execute("SELECT run_id FROM chat_inbox WHERE message_id=?", (old["id"],)).fetchone()
                cursor = conn.execute("SELECT cursor FROM chat_delivery WHERE item_id=?", (old["id"],)).fetchone()[0]
                return {"message_id": old["id"], "run_id": row["run_id"], "cursor": cursor, "duplicate": True}
            message_id, stamp = uid(), now()
            active = conn.execute("""SELECT * FROM chat_runs WHERE session_id=? AND state IN ('queued','running')
                ORDER BY created_at DESC LIMIT 1""", (session_id,)).fetchone()
            run_id = active["id"] if active else uid()
            conn.execute("INSERT INTO chat_messages VALUES (?,?,?,?,?,?,?)",
                         (message_id, session_id, author_id, cmid, fingerprint, encrypted, stamp))
            if not active:
                parent=None
                if payload["text"].strip().casefold()=="continue" and not payload.get("attachment_ids"):
                    previous=conn.execute("""SELECT id FROM chat_runs WHERE session_id=?
                        AND state IN ('completed','cancelled','failed') ORDER BY created_at DESC LIMIT 1""",
                        (session_id,)).fetchone()
                    parent=previous["id"] if previous else None
                conn.execute("""INSERT INTO chat_runs(id,session_id,message_id,parent_run_id,state,policy,created_at,updated_at)
                    VALUES (?,?,?,?,'queued',?,?,?)""", (run_id,session_id,message_id,parent,frozen_policy,stamp,stamp))
            result = self._publish(conn, session_id=session_id, run_id=run_id, kind="user",
                                   payload=public, item_id=message_id, timestamp=stamp)
            conn.execute("INSERT INTO chat_inbox VALUES (?,?,?,0,?)",
                         (message_id,session_id,run_id,result["position"]))
            return {"message_id":message_id, "run_id":run_id, "cursor":result["cursor"], "duplicate":False}

    def claim(self, run_id, worker_id, *, ttl=60):
        with self.transaction() as conn:
            row = self._run(conn, run_id, states={"queued"})
            occupied = conn.execute("""SELECT 1 FROM chat_runs WHERE session_id=? AND id!=?
                AND state IN ('running','finalizing','stopping')""", (row["session_id"],run_id)).fetchone()
            if occupied:
                raise Conflict("Another run owns this session")
            generation = row["generation"] + 1
            conn.execute("""UPDATE chat_runs SET state='running', generation=?, lease_owner=?,
                lease_until=?,updated_at=? WHERE id=?""", (generation,worker_id,time.time()+ttl,now(),run_id))
            return generation

    def renew(self, run_id, generation, worker_id, *, ttl=60):
        with self.transaction() as conn:
            row = self._run(conn,run_id,generation,{"running","finalizing"})
            if row["lease_owner"] != worker_id:
                raise Fenced("Worker does not own lease")
            conn.execute("UPDATE chat_runs SET lease_until=?,updated_at=? WHERE id=?",
                         (time.time()+ttl,now(),run_id))

    async def consume(self, run_id, generation):
        with self.transaction() as conn:
            self._run(conn,run_id,generation,{"running"})
            rows = [dict(r) for r in conn.execute("""SELECT m.* FROM chat_inbox i JOIN chat_messages m ON m.id=i.message_id
                WHERE i.run_id=? AND i.consumed=0 ORDER BY i.ordinal""",(run_id,))]
            conn.execute("UPDATE chat_inbox SET consumed=1 WHERE run_id=?",(run_id,))
        return [{"id":row["id"], "author_id":row["author_id"], **await self.decode(row["payload"])} for row in rows]

    async def journal(self, run_id, generation, kind, payload, *, event_id=None):
        encrypted = await self.encode(payload)
        with self.transaction() as conn:
            run = self._run(conn,run_id)
            # Stale output is diagnostic only. Mark it rather than extending a live segment.
            stale = run["generation"] != generation or run["state"] in TERMINAL or run["lease_until"] <= time.time()
            kind = "stale:" + kind if stale else kind
            row = conn.execute("""INSERT INTO chat_execution(event_id,session_id,run_id,generation,kind,payload,created_at)
                VALUES (?,?,?,?,?,?,?) ON CONFLICT(event_id) DO NOTHING""",
                (event_id or uid(),run["session_id"],run_id,generation,kind,encrypted,now()))
            if row.rowcount and not stale:
                updated = conn.execute("""UPDATE chat_segments SET first_seq=COALESCE(first_seq,?),last_seq=?
                    WHERE run_id=? AND closed=0""",(row.lastrowid,row.lastrowid,run_id))
                if not updated.rowcount:
                    # A different public item can close this run's segment
                    # while the run is still finalizing. Place later work after
                    # that item rather than reattaching it to the older group.
                    boundary = conn.execute("""SELECT id FROM chat_publications
                        WHERE session_id=? ORDER BY position DESC LIMIT 1""",
                        (run["session_id"],)).fetchone()
                    if boundary:
                        prior = conn.execute("SELECT 1 FROM chat_segments WHERE run_id=? LIMIT 1",
                                             (run_id,)).fetchone()
                        segment_id = ("diagnostic:" + run_id
                                      if not prior and (run["stop_reason"] or "").startswith("finalization_retry:")
                                      else uid())
                        conn.execute("""INSERT INTO chat_segments
                            (id,session_id,run_id,boundary_item_id,first_seq,last_seq)
                            VALUES (?,?,?,?,?,?)""",
                            (segment_id,run["session_id"],run_id,boundary["id"],
                             row.lastrowid,row.lastrowid))
            return row.lastrowid

    async def freeze_finalization(self, run_id, generation, text, status):
        """Freeze the first final draft and policy before any repair request."""
        frozen_policy=(await self.run(run_id))["policy"]
        policy_digest=digest(frozen_policy)
        payload=await self.encode({"text":text,"status":status})
        candidate_id=uid()
        with self.transaction() as conn:
            run=self._run(conn,run_id,generation,{"running","finalizing"})
            if run["state"]=="finalizing":
                return True
            if conn.execute("SELECT 1 FROM chat_inbox WHERE run_id=? AND consumed=0",(run_id,)).fetchone():
                return False
            conn.execute("""INSERT INTO chat_candidates
                (id,session_id,run_id,generation,kind,payload,decision,reason,policy_digest,created_at)
                VALUES (?,?,?,?,?,?,'pending','[]',?,?)""",
                (candidate_id,run["session_id"],run_id,generation,"answer",payload,
                 policy_digest,now()))
            conn.execute("UPDATE chat_runs SET state='finalizing',candidate_id=?,updated_at=? WHERE id=?",
                         (candidate_id,now(),run_id))
        return True

    def begin_finalization(self, run_id, generation):
        with self.transaction() as conn:
            self._run(conn,run_id,generation,{"running"})
            if conn.execute("SELECT 1 FROM chat_inbox WHERE run_id=? AND consumed=0",(run_id,)).fetchone():
                return False
            conn.execute("UPDATE chat_runs SET state='finalizing',updated_at=? WHERE id=?",(now(),run_id))
            return True

    async def decide(self, run_id, generation, kind, payload, *, accepted, reasons,
                     policy_digest, terminal=False, outcome="completed", candidate_id=None):
        encrypted = await self.encode(payload)
        candidate_id = candidate_id or uid()
        with self.transaction() as conn:
            run = self._run(conn,run_id,generation,{"finalizing"} if terminal else {"running"})
            existing = conn.execute("SELECT * FROM chat_candidates WHERE id=?",(candidate_id,)).fetchone()
            if existing:
                raise Conflict("Candidate already decided")
            conn.execute("""INSERT INTO chat_candidates
                (id,session_id,run_id,generation,kind,payload,decision,reason,policy_digest,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (candidate_id,run["session_id"],run_id,generation,kind,encrypted,
                 "accepted" if accepted else "rejected",canonical(reasons),policy_digest,now()))
            conn.execute("UPDATE chat_runs SET candidate_id=? WHERE id=?",(candidate_id,run_id))
            if not accepted:
                return {"candidate_id":candidate_id,"accepted":False}
            result = self._publish(conn,session_id=run["session_id"],run_id=run_id,kind=kind,
                                   payload=encrypted,terminal=terminal,candidate_id=candidate_id)
            if terminal:
                if outcome not in TERMINAL:
                    raise ValueError("Invalid terminal outcome")
                conn.execute("UPDATE chat_runs SET state=?,lease_until=0,updated_at=? WHERE id=?",
                             (outcome,now(),run_id))
            return {**result,"candidate_id":candidate_id,"accepted":True}

    async def stop(self, session_id):
        payload = await self.encode({"text":"Stopped.","flag":"partial","system_outcome":"cancelled"})
        with self.transaction() as conn:
            self._session(conn,session_id)
            runs = list(conn.execute("SELECT * FROM chat_runs WHERE session_id=? AND state IN ('queued','running','finalizing','stopping')",(session_id,)))
            for run in runs:
                conn.execute("UPDATE chat_runs SET state='cancelled',generation=generation+1,lease_until=0,stop_reason='user_stop',updated_at=? WHERE id=?",(now(),run["id"]))
                self._publish(conn,session_id=session_id,run_id=run["id"],kind="notice",payload=payload,terminal=True)
            return [row["id"] for row in runs]

    async def run(self, run_id):
        with self.transaction(write=False) as conn:
            row = self._run(conn,run_id)
        row["policy"] = await self.decode(row["policy"])
        return row

    async def retry_source(self, session_id, candidate_id):
        """Read only the rejected draft named by a committed failure notice."""
        with self.transaction(write=False) as conn:
            self._session(conn,session_id)
            candidate=conn.execute("""SELECT * FROM chat_candidates
                WHERE id=? AND session_id=? AND kind='answer' AND decision='rejected'""",
                (candidate_id,session_id)).fetchone()
            if candidate is None:
                raise Conflict("No rejected finalization candidate")
            parent=self._run(conn,candidate["run_id"])
            failure=conn.execute("""SELECT payload FROM chat_publications
                WHERE terminal_run_id=? AND kind='failure'""",(parent["id"],)).fetchone()
            messages=[]
            source_run=parent
            seen=set()
            while source_run and source_run["id"] not in seen:
                seen.add(source_run["id"])
                messages=[row["payload"] for row in conn.execute("""SELECT m.payload FROM chat_inbox i
                    JOIN chat_messages m ON m.id=i.message_id WHERE i.run_id=? ORDER BY i.ordinal""",
                    (source_run["id"],))]
                if messages:
                    break
                source_run=(self._run(conn,source_run["parent_run_id"])
                            if source_run["parent_run_id"] else None)
            if parent["state"]!="failed" or failure is None:
                raise Conflict("Candidate has no failed terminal publication")
        failure_payload=await self.decode(failure["payload"])
        if failure_payload.get("retry")!={"kind":"finalization","candidate_id":candidate_id}:
            raise Conflict("Candidate is not the offered finalization retry")
        requests=[(await self.decode(row)).get("text","") for row in messages]
        return {"parent_run_id":parent["id"],"message_id":parent["message_id"],
                "policy":parent["policy"],"candidate":await self.decode(candidate["payload"]),
                "user_request":"\n\n".join(requests)}

    def finalization_retry_receipt(self, parent_run_id, candidate_id):
        marker="finalization_retry:"+candidate_id
        with self.transaction(write=False) as conn:
            parent=self._run(conn,parent_run_id,states={"failed"})
            row=conn.execute("""SELECT id FROM chat_runs
                WHERE parent_run_id=? AND stop_reason=?""",(parent["id"],marker)).fetchone()
        return ({"run_id":row["id"],"parent_run_id":parent_run_id,"duplicate":True}
                if row else None)

    async def start_finalization_retry(self, session_id, candidate_id):
        source=await self.retry_source(session_id,candidate_id)
        marker="finalization_retry:"+candidate_id
        with self.transaction() as conn:
            self._session(conn,session_id)
            parent=self._run(conn,source["parent_run_id"],states={"failed"})
            existing=conn.execute("""SELECT id FROM chat_runs
                WHERE parent_run_id=? AND stop_reason=?""",(parent["id"],marker)).fetchone()
            if existing:
                return {"run_id":existing["id"],"parent_run_id":parent["id"],"duplicate":True}
            run_id,stamp=uid(),now()
            conn.execute("""INSERT INTO chat_runs
                (id,session_id,message_id,parent_run_id,state,policy,candidate_id,stop_reason,created_at,updated_at)
                VALUES (?,?,?,?,'queued',?,?,?,?,?)""",
                (run_id,session_id,source["message_id"],parent["id"],source["policy"],
                 candidate_id,marker,stamp,stamp))
            return {"run_id":run_id,"parent_run_id":parent["id"],"duplicate":False}

    async def fail_execution(self, run_id, generation):
        """Terminally close an execution error without offering draft-only retry."""
        payload=await self.encode({
            "text":"Execution stopped before a final answer was available. You can continue in a new run.",
            "system_outcome":"failed",
        })
        with self.transaction() as conn:
            run=self._run(conn,run_id,generation,{"running","finalizing"})
            result=self._publish(conn,session_id=run["session_id"],run_id=run_id,
                                 kind="failure",payload=payload,terminal=True)
            conn.execute("UPDATE chat_runs SET state='failed',lease_until=0,updated_at=? WHERE id=?",
                         (now(),run_id))
            return result

    async def snapshot(self, session_id, *, before=None, limit=100):
        limit = max(1,min(limit,200))
        with self.transaction(write=False) as conn:
            self._session(conn,session_id)
            cursor = conn.execute("SELECT COALESCE(MAX(cursor),0) FROM chat_delivery WHERE session_id=?",(session_id,)).fetchone()[0]
            rows = [dict(r) for r in conn.execute("""SELECT * FROM chat_publications WHERE session_id=?
                AND (? IS NULL OR position<?) ORDER BY position DESC LIMIT ?""",(session_id,before,before,limit+1))]
            runs = [dict(r) for r in conn.execute("""SELECT id,state,generation,message_id,parent_run_id FROM chat_runs
                WHERE session_id=? ORDER BY created_at""",(session_id,))]
        items = [await self._item(row) for row in reversed(rows[:limit])]
        return {"items":items,"cursor":cursor,"has_more":len(rows)>limit,"runs":runs,"schema_version":1}

    async def changes(self, session_id, after=0, *, limit=200):
        with self.transaction(write=False) as conn:
            self._session(conn,session_id)
            rows = [dict(r) for r in conn.execute("""SELECT p.*,d.cursor,d.payload AS event_payload,d.revision AS event_revision
                FROM chat_delivery d JOIN chat_publications p ON p.id=d.item_id
                WHERE d.session_id=? AND d.cursor>? ORDER BY d.cursor LIMIT ?""",(session_id,after,min(limit,200)))]
        events=[]
        for row in rows:
            row["payload"],row["revision"]=row.pop("event_payload"),row.pop("event_revision")
            events.append({"cursor":row["cursor"],"item":await self._item(row)})
        return events

    async def _item(self, row):
        return {**{k:row[k] for k in ("id","session_id","run_id","position","revision","schema_version","kind","created_at")},
                "payload":await self.decode(row["payload"]),"deleted":bool(row["deleted"]),
                "terminal":row["terminal_run_id"] is not None}

    async def activity(self, session_id, *, after=0, limit=100):
        with self.transaction(write=False) as conn:
            self._session(conn,session_id)
            rows=[dict(r) for r in conn.execute("SELECT * FROM chat_execution WHERE session_id=? AND seq>? ORDER BY seq LIMIT ?",
                                              (session_id,after,min(limit,200)))]
        return [{**r,"payload":await self.decode(r["payload"])} for r in rows]

    async def latest_notification(self, session_id):
        """Project the most recent terminal publication, never private output."""
        with self.transaction(write=False) as conn:
            self._session(conn,session_id)
            row=conn.execute("""SELECT p.id,p.run_id,p.kind,p.payload,d.cursor
                FROM chat_publications p JOIN chat_delivery d ON d.item_id=p.id
                WHERE p.session_id=? AND p.terminal_run_id IS NOT NULL
                ORDER BY p.position DESC,d.revision DESC LIMIT 1""",(session_id,)).fetchone()
        if row is None:
            return None
        payload=await self.decode(row["payload"])
        status=payload.get("status") if isinstance(payload.get("status"),dict) else {}
        flag=status.get("flag") or payload.get("flag") or {
            "failed":"error","cancelled":"partial"}.get(payload.get("system_outcome"))
        message=status.get("cta") or ("Response ready" if row["kind"]=="answer"
                                      else payload.get("text") or "")
        return {"session_id":session_id,"item_id":row["id"],"run_id":row["run_id"],
                "cursor":row["cursor"],"kind":row["kind"],"flag":flag,
                "message":message[:200]}

    def notification_ack(self, session_id):
        with self.transaction(write=False) as conn:
            row=conn.execute("SELECT cursor,manual_unread FROM chat_acknowledgements WHERE session_id=?",
                             (session_id,)).fetchone()
        return {"cursor":row["cursor"],"manual_unread":bool(row["manual_unread"])} if row else {
            "cursor":0,"manual_unread":False}

    def is_notification_cursor(self, session_id, cursor):
        with self.transaction(write=False) as conn:
            self._session(conn,session_id)
            return conn.execute("""SELECT 1 FROM chat_delivery d JOIN chat_publications p ON p.id=d.item_id
                WHERE d.session_id=? AND d.cursor=? AND p.terminal_run_id IS NOT NULL""",
                (session_id,cursor)).fetchone() is not None

    def acknowledge(self, session_id, cursor, *, unread=False):
        with self.transaction() as conn:
            # The caller's repository stores their acknowledgement; authorization
            # against the conversation owner is performed by the service.
            conn.execute("""INSERT INTO chat_acknowledgements VALUES (?,?,?)
                ON CONFLICT(session_id) DO UPDATE SET cursor=MAX(cursor,excluded.cursor),manual_unread=excluded.manual_unread""",
                         (session_id,cursor,int(unread)))

    async def recover_orphaned(self):
        """Fence work owned by the previous process and make it runnable again.

        This is deliberately a boot-only transition. Unlike ``recover_expired``,
        which conservatively fails execution with an uncertain outcome, process
        startup proves that no old worker can still own the lease. Runs whose
        accepted input was already consumed are tagged for the v1 transcript
        resumer; runs that never reached execution return to the normal queue.
        """
        recovered = []
        with self.transaction() as conn:
            rows = list(conn.execute("""SELECT r.* FROM chat_runs r JOIN sessions s ON s.id=r.session_id
                WHERE s.user_id=? AND s.status='active'
                AND r.state IN ('running','finalizing','stopping')
                ORDER BY r.created_at""", (self.owner_id,)))
            for row in rows:
                consumed = conn.execute("""SELECT 1 FROM chat_inbox
                    WHERE run_id=? AND consumed=1 LIMIT 1""", (row["id"],)).fetchone()
                conn.execute("""UPDATE chat_runs SET state='queued',generation=generation+1,
                    lease_owner=NULL,lease_until=0,candidate_id=NULL,stop_reason=?,updated_at=?
                    WHERE id=?""", ("server_restart_resume" if consumed else None,
                                    now(), row["id"]))
                recovered.append(row["id"])
        return recovered

    async def recover_expired(self):
        """Fence abandoned attempts; only never-consumed messages may run again."""
        payload = await self.encode({
            "text":"Execution was interrupted. Some tool results may be unknown; review the activity before continuing.",
            "system_outcome":"failed",
        })
        recovered = []
        with self.transaction() as conn:
            expired = list(conn.execute("""SELECT r.* FROM chat_runs r JOIN sessions s ON s.id=r.session_id
                WHERE s.user_id=? AND s.status='active' AND r.state IN ('running','finalizing','stopping')
                AND r.lease_until<=?""",(self.owner_id,time.time())))
            for row in expired:
                conn.execute("""UPDATE chat_runs SET state='failed',generation=generation+1,lease_until=0,
                    stop_reason='lease_expired',updated_at=? WHERE id=?""",(now(),row["id"]))
                self._publish(conn,session_id=row["session_id"],run_id=row["id"],
                              kind="failure",payload=payload,terminal=True)
                pending = conn.execute("""SELECT message_id FROM chat_inbox
                    WHERE run_id=? AND consumed=0 ORDER BY ordinal LIMIT 1""",(row["id"],)).fetchone()
                if pending:
                    # Preserve accepted input that never reached the execution engine.
                    next_id,stamp = uid(),now()
                    conn.execute("""INSERT INTO chat_runs
                        (id,session_id,message_id,parent_run_id,state,policy,created_at,updated_at)
                        VALUES (?,?,?,?,'queued',?,?,?)""",
                        (next_id,row["session_id"],pending["message_id"],row["id"],row["policy"],stamp,stamp))
                    conn.execute("UPDATE chat_inbox SET run_id=? WHERE run_id=? AND consumed=0",
                                 (next_id,row["id"]))
                recovered.append(row["id"])
        return recovered

    def pending_runs(self):
        with self.transaction(write=False) as conn:
            return [r[0] for r in conn.execute("""SELECT r.id FROM chat_runs r
                JOIN sessions s ON s.id=r.session_id WHERE s.user_id=? AND s.status='active'
                AND r.state='queued' ORDER BY r.created_at""",(self.owner_id,))]
