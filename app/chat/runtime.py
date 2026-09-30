"""Execution adapter: existing work engine, new durable publication authority."""
import asyncio
import contextlib
import json
import time
import traceback
from starlette.requests import Request
from .context import current
from .contracts import Conflict, Fenced, Send, TERMINAL
from .publication import PublicationService
from .repository import ChatRepository, uid

_tasks = {}
_worker = uid()
_shutting_down = False

def internal_request(owner):
    request=Request({"type":"http","method":"POST","path":"/api/v2/chat/internal",
                     "headers":[],"query_string":b"","server":("127.0.0.1",8080),
                     "client":("127.0.0.1",0),"scheme":"http"})
    request.state.auth_payload={"user_id":owner,"sub":owner}
    return request

def resolve_policy(agent, mode):
    from app.chat.closing import resolve
    policy=resolve(agent,mode)
    if policy is None:
        # Disabling optional closer requirements never disables publication.
        policy={"require_flag":False,"require_notification":False,"flags":[],
                "max_tokens":0,"format_enforcement_min_tokens":0,"max_attempts":0,
                "max_attempts_per_requirement":0}
    return policy

async def submit(request, actor, repo, session, command):
    from app.db import get_db
    from app.chat.execution import _require_chat_agent_access, _enforce_billing_access
    from app.api.rate_limit import enforce_tier_chat
    db=get_db()
    agent=await _require_chat_agent_access(db,session["agent_id"],actor)
    if not agent:
        raise Conflict("Session has no available agent")
    # A client can retry after the response was lost. The accepted command is
    # already metered, and must remain retrievable even if a rate window fills
    # or the account balance changes before the retry arrives.
    receipt=repo.receipt_for(session["id"],actor,command)
    if receipt is not None:
        schedule(repo.owner_id,receipt["run_id"])
        return {**receipt,"status":"accepted","session_id":session["id"]}
    if actor.startswith("anon_") and len(command.text)>32000:
        raise Conflict("Anonymous messages are limited to 32,000 characters")
    from app.chat.execution import _is_compact_command
    compact = _is_compact_command(command.text)
    if compact and (command.attachment_ids or command.app_control or command.display_text is not None):
        raise Conflict("Compaction does not accept attachments or app control data")
    await _enforce_billing_access(db,agent,actor,command.text)
    await enforce_tier_chat(actor,request,message=command.text,agent_id=agent["id"])
    for attachment_id in command.attachment_ids:
        attachment=await db.get_attachment(attachment_id)
        if not attachment or attachment.get("user_id")!=actor:
            raise Conflict("Attachment is unavailable")
    receipt=await repo.accept(session["id"],actor,command,resolve_policy(agent,command.execution_mode),
                              kind="compaction" if compact else "chat")
    schedule(repo.owner_id,receipt["run_id"])
    return {**receipt,"status":"accepted","session_id":session["id"]}

def schedule(owner, run_id):
    key=(owner,run_id)
    if key in _tasks and not _tasks[key].done():
        return
    task=asyncio.create_task(_drive(owner,run_id),name="chat-v2:"+run_id)
    _tasks[key]=task
    def finished(done):
        if _tasks.get(key) is done:
            _tasks.pop(key,None)
        if not done.cancelled():
            done.exception()  # exceptions are journalled by the driver
    task.add_done_callback(finished)

def cancel(owner,session_id):
    for (task_owner,_),task in list(_tasks.items()):
        if task_owner==owner and getattr(task,"chat_session_id",None)==session_id:
            task.cancel()

class Execution:
    def __init__(self,repo,run_id,generation):
        self.repo,self.run_id,self.generation=repo,run_id,generation
        self.publication=PublicationService(repo)
        self.chunks=[]
        self.chunk_bytes=0
        self.flushed_at=time.monotonic()

    async def flush(self):
        if not self.chunks:
            return
        chunks,self.chunks=self.chunks,[]
        self.chunk_bytes=0
        self.flushed_at=time.monotonic()
        await self.repo.journal(self.run_id,self.generation,"model_stream",{"chunks":chunks})

    async def observe(self,event):
        if event.get("_controlled_recorded"):
            return
        event["_controlled_recorded"]=True
        kind=str(event.get("type") or "unknown")
        if kind=="stream":
            self.chunks.append({k:v for k,v in event.items() if k!="_controlled_recorded"})
            self.chunk_bytes+=len(str(event.get("content") or "").encode())
            if self.chunk_bytes>=32768 or time.monotonic()-self.flushed_at>=0.2:
                await self.flush()
            return
        await self.flush()
        # Secret-entry payloads are interaction descriptors, never secret values.
        payload={k:v for k,v in event.items() if k!="_controlled_recorded"}
        await self.repo.journal(self.run_id,self.generation,kind,payload)
        if kind=="tool_result":
            result=event.get("result")
            try:
                result=json.loads(result) if isinstance(result,str) else result
            except (ValueError,TypeError):
                result=None
            if isinstance(result,dict) and result.get("kind")=="file_attachment":
                await self.publication.artifact(
                    self.run_id,self.generation,event.get("tool"),result)
            elif isinstance(result,dict) and event.get("tool") in {"generate_image","browser_action"}:
                await self.publication.media(self.run_id,self.generation,event.get("tool"),result)
            elif isinstance(result,dict) and result.get("ui")=="agent_auth":
                await self.publication.agent_auth(self.run_id,self.generation,result)
            elif isinstance(result,dict) and isinstance(result.get("ui"),str):
                await self.publication.notice(
                    self.run_id,self.generation,
                    "This interactive tool result is unavailable in controlled chat.")
        if kind=="response":
            render_kind=event.get("chat_render_kind")
            if render_kind=="notice" and event.get("mid_run"):
                await self.publication.notice(self.run_id,self.generation,event.get("content"))
            elif event.get("terminal_candidate") or render_kind=="promoted_answer":
                status=event.get("private_closer_status",event.get("closing_message")) or {}
                await self.publication.final(self.run_id,self.generation,event.get("content"),status)

    async def freeze_finalization(self,text,status):
        return await self.repo.freeze_finalization(self.run_id,self.generation,text,status)

    async def consume_inbox(self,db,session_id):
        with self.repo.transaction(write=False) as conn:
            run=self.repo._run(conn,self.run_id,self.generation,{"running","finalizing"})
        if run["state"]=="finalizing":
            return []
        rows=await self.repo.consume(self.run_id,self.generation)
        for row in rows:
            await db.insert_interaction(self.repo.owner_id,session_id,role="user",content=row["text"],
                                        metadata=json.dumps({"chat_message_id":row["id"]}),sender_id=row["author_id"])
        return [{"id":r["id"],"content":r["text"]} for r in rows]

async def _heartbeat(execution):
    while True:
        await asyncio.sleep(15)
        execution.repo.renew(execution.run_id,execution.generation,_worker)


async def _settle_execution(execution):
    """V2 alone settles an unfinished attempt; consumed effects are never replayed."""
    state = await execution.repo.run(execution.run_id)
    if state["state"] not in TERMINAL and not _shutting_down:
        if state["kind"]=="compaction":
            reason = ("Compaction stopped before its result was confirmed. Its checkpoint may already have changed; "
                      "review the session before retrying.")
        else:
            reason = ("Execution stopped with an unconfirmed tool result. Review activity before continuing."
                      if await execution.repo.has_uncertain_tool_result(execution.run_id) else None)
        await execution.repo.fail_execution(execution.run_id, execution.generation,
                                           reason=reason)


async def _drive(owner,run_id):
    from app.auth.workspace import workspace_scope, system_user_context
    from app.db import get_db
    from app.models.schemas import ChatRequest
    from .engine_adapter import prepare_controlled_turn, execute_controlled_turn
    repo=ChatRepository(owner)
    execution=None
    token=None
    heartbeat=None
    with workspace_scope(system_user_context(owner,"controlled chat worker")):
        try:
            run=await repo.run(run_id)
            asyncio.current_task().chat_session_id=run["session_id"]
            from app.agent.compaction import wait_for_automatic_compaction
            await wait_for_automatic_compaction(owner,run["session_id"])
            while True:
                try:
                    generation=repo.claim(run_id,_worker)
                    break
                except Fenced:
                    return
                except Conflict:
                    await asyncio.sleep(0.25)
            execution=Execution(repo,run_id,generation)
            token=current.set(execution)
            heartbeat=asyncio.create_task(_heartbeat(execution))
            driver=asyncio.current_task()
            def lease_finished(done):
                if not done.cancelled() and done.exception() is not None:
                    driver.cancel()
            heartbeat.add_done_callback(lease_finished)
            if str(run.get("stop_reason") or "").startswith("finalization_retry:"):
                candidate_id=run["stop_reason"].split(":",1)[1]
                source=await repo.retry_source(run["session_id"],candidate_id)
                with repo.transaction(write=False) as conn:
                    session=repo._session(conn,run["session_id"])
                from .finalization import repair_callback
                repair=await repair_callback(execution,session["agent_id"],source["user_request"])
                draft=source["candidate"]
                await execution.publication.final(
                    run_id,generation,draft.get("text"),draft.get("status"),
                    repair=repair,manual_retry=True)
                return
            rows=await repo.consume(run_id,generation)
            if not rows:
                raise RuntimeError("Run has no accepted input")
            with repo.transaction(write=False) as conn:
                session=repo._session(conn,run["session_id"])
            request=ChatRequest(
                user_id=owner,session_id=run["session_id"],agent_id=session["agent_id"],
                message="\n\n".join(r["text"] for r in rows),
                client_msg_id=rows[0]["client_message_id"],
                attachment_ids=[a for r in rows for a in r.get("attachment_ids",[])],
                execution_mode=rows[-1]["execution_mode"],
                app_control=rows[-1].get("app_control"),
            )
            db=get_db()
            if run["kind"]=="compaction":
                from .compaction import run_manual_compaction
                if not repo.begin_finalization(run_id,generation):
                    raise RuntimeError("Compaction run received unexpected input")
                await run_manual_compaction(execution,db,session)
                return
            agent,interaction_id,first=await prepare_controlled_turn(db,request,rows)
            await execute_controlled_turn(db,request,agent,interaction_id,first=first)
            await execution.flush()
            state=await repo.run(run_id)
            if state["state"] not in TERMINAL:
                await _settle_execution(execution)
        except asyncio.CancelledError:
            if execution:
                await execution.flush()
            raise
        except Exception as error:
            if execution:
                await execution.repo.journal(run_id,execution.generation,"execution_error",
                                             {"error_type":type(error).__name__,"incomplete":True,"stack":[{"file":f.filename,"line":f.lineno,"function":f.name} for f in traceback.extract_tb(error.__traceback__)],"sqlite_error":getattr(error,"sqlite_errorname",None)})
                with contextlib.suppress(Fenced):
                    await _settle_execution(execution)
        finally:
            if heartbeat:
                heartbeat.cancel()
                with contextlib.suppress(asyncio.CancelledError,Fenced):
                    await heartbeat
            if token is not None:
                current.reset(token)
