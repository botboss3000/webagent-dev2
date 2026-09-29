"""Authenticated public conversation API; private diagnostics are separately gated."""
import asyncio
import json
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import Field
from app.auth.identity import assert_caller_is
from .contracts import Conflict, Fenced, NotFound, Send, SessionCreate, StrictModel
from .repository import ChatRepository, uid

router = APIRouter(prefix="/api/v2/chat", tags=["controlled-chat"])

async def scope(request, session_id=None, owner_id=None):
    actor = await assert_caller_is(request,None)
    owner = owner_id or actor
    repo = ChatRepository(owner)
    if owner != actor:
        from app.db import get_user_db
        if not session_id or not await get_user_db(owner).is_session_participant(session_id,actor,"user"):
            raise HTTPException(404,"Session not found")
    if session_id:
        try:
            with repo.transaction(write=False) as conn:
                session = repo._session(conn,session_id)
        except NotFound:
            raise HTTPException(404,"Session not found")
    else:
        session = None
    return actor,repo,session

@router.get("/identity")
async def identity(request: Request):
    actor,repo,_ = await scope(request)
    from app.db.storage_layout import read_layout
    return {"user_id":actor,"instance_id":read_layout()["manifest"]["instance_id"],"schema_version":1}

@router.get("/sessions")
async def sessions(request: Request):
    actor,repo,_ = await scope(request)
    with repo.transaction(write=False) as conn:
        rows=[dict(r) for r in conn.execute("""SELECT id,title,agent_id,pinned,created_at,updated_at
            FROM sessions WHERE user_id=? AND status='active' ORDER BY updated_at DESC LIMIT 200""",(actor,))]
    return {"sessions":rows}

@router.post("/sessions",status_code=201)
async def create_session(body: SessionCreate,request: Request):
    actor,repo,_ = await scope(request)
    from app.db import get_db
    from app.chat.execution import _require_chat_agent_access
    agent = await _require_chat_agent_access(get_db(),body.agent_id,actor)
    if agent is None:
        raise HTTPException(404,"Agent not found")
    sid=body.id or uid()
    with repo.transaction() as conn:
        existing=conn.execute("SELECT user_id,agent_id,title,status FROM sessions WHERE id=?",(sid,)).fetchone()
        if existing:
            if existing["user_id"] != actor or existing["agent_id"] != body.agent_id or existing["status"] != "active":
                raise HTTPException(409,"Session identity is already in use")
            title=existing["title"]
        else:
            conn.execute("INSERT INTO sessions(id,user_id,title,agent_id,status) VALUES (?,?,?,?,'active')",
                         (sid,actor,body.title,body.agent_id))
            title=body.title
    return {"id":sid,"agent_id":body.agent_id,"title":title}

@router.post("/sessions/{session_id}/messages",status_code=202)
async def send(session_id: str,body: Send,request: Request,owner_id: str | None=None):
    actor,repo,session = await scope(request,session_id,owner_id)
    from .runtime import submit
    try:
        return await submit(request,actor,repo,session,body)
    except Conflict as error:
        raise HTTPException(409,str(error))

class ContinueCommand(StrictModel):
    client_message_id: str = Field(min_length=1,max_length=128)

@router.post("/sessions/{session_id}/continue",status_code=202)
async def continue_run(session_id: str,body: ContinueCommand,request: Request,owner_id: str | None=None):
    # Typed Continue and the button enter exactly the same acceptance command.
    return await send(session_id,Send(client_message_id=body.client_message_id,text="Continue"),request,owner_id)

class FinalizationRetry(StrictModel):
    candidate_id: str = Field(min_length=1,max_length=128)

@router.post("/sessions/{session_id}/finalization-retry",status_code=202)
async def finalization_retry(session_id: str,body: FinalizationRetry,request: Request,
                             owner_id: str | None=None):
    actor,repo,session=await scope(request,session_id,owner_id)
    from app.db import get_db
    from app.chat.execution import _require_chat_agent_access, _enforce_billing_access
    from app.api.rate_limit import enforce_tier_chat
    from .runtime import schedule
    agent=await _require_chat_agent_access(get_db(),session["agent_id"],actor)
    if not agent:
        raise HTTPException(404,"Agent not found")
    try:
        source=await repo.retry_source(session_id,body.candidate_id)
        previous=repo.finalization_retry_receipt(source["parent_run_id"],body.candidate_id)
        if previous:
            schedule(repo.owner_id,previous["run_id"])
            return previous
        await _enforce_billing_access(get_db(),agent,actor,"Retry final response")
        await enforce_tier_chat(actor,request,message="Retry final response",agent_id=agent["id"])
        receipt=await repo.start_finalization_retry(session_id,body.candidate_id)
    except Conflict as error:
        raise HTTPException(409,str(error))
    schedule(repo.owner_id,receipt["run_id"])
    return receipt

@router.get("/sessions/{session_id}/components")
async def components(session_id: str,request: Request):
    # Elements are owner-only session resources (including private scratchpad
    # rows), not raw execution events or participant-visible transcript items.
    actor,repo,_ = await scope(request,session_id)
    from app.chat_components import list_components, get_panel_ui
    rows = await list_components(actor,session_id)
    ui = await get_panel_ui(actor,session_id)
    await scope(request,session_id)  # Recheck access after asynchronous reads.
    return {"session_id":session_id,"components":rows,"ui":ui}

@router.get("/sessions/{session_id}")
async def snapshot(session_id: str,request: Request,before: int | None=None,limit: int=100,owner_id: str | None=None):
    _,repo,_ = await scope(request,session_id,owner_id)
    return await repo.snapshot(session_id,before=before,limit=limit)

@router.get("/sessions/{session_id}/messages/{message_id}/row-details")
async def row_details(session_id: str, message_id: str, request: Request):
    # Owner-only diagnostics: never expose private row metadata to participants.
    _, repo, _ = await scope(request, session_id)
    with repo.transaction(write=False) as conn:
        row = conn.execute(
            "SELECT * FROM chat_publications WHERE session_id=? AND id=?",
            (session_id, message_id)).fetchone()
        if row is None:
            raise HTTPException(404, "Message not found")
        row = dict(row)
    # Preserve the exact plaintext JSON, including whitespace, for inspection.
    raw_payload = await repo.cipher.decrypt(repo.owner_id, row["payload"])
    row["payload"] = raw_payload
    await scope(request, session_id)
    return {"table": "chat_publications", "render_type": row["kind"],
            "row": row, "parsed_payload": json.loads(raw_payload)}

@router.get("/sessions/{session_id}/events")
async def events(session_id: str,request: Request,after: int=Query(0,ge=0),owner_id: str | None=None):
    _,repo,_ = await scope(request,session_id,owner_id)
    # Each bounded SSE response replays committed changes. The client reconnects
    # at its durable cursor, so a lost response can be requested again.
    deadline=asyncio.get_running_loop().time()+1.0
    while True:
        try:
            await scope(request,session_id,owner_id)
            changes=await repo.changes(session_id,after)
            with repo.transaction(write=False) as conn:
                runs=[dict(r) for r in conn.execute("""SELECT id,state,message_id,parent_run_id FROM chat_runs
                    WHERE session_id=? ORDER BY created_at""",(session_id,))]
        except (HTTPException,NotFound):
            body="event: revoked\ndata: {}\n\n"
            break
        active=any(run["state"] in {"queued","running","finalizing","stopping"} for run in runs)
        if changes or active or asyncio.get_running_loop().time()>=deadline:
            frames=["id: "+str(change["cursor"])+"\nevent: publication\ndata: "+json.dumps(change)+"\n\n"
                    for change in changes]
            if changes or active:
                frames.append("event: state\ndata: "+json.dumps({"runs":runs})+"\n\n")
            body="".join(frames) or ": idle\n\n"
            break
        await asyncio.sleep(0.2)
    return Response(body,media_type="text/event-stream",
                    headers={"Cache-Control":"no-store","X-Accel-Buffering":"no"})

@router.post("/sessions/{session_id}/stop")
async def stop(session_id: str,request: Request,owner_id: str | None=None):
    _,repo,_=await scope(request,session_id,owner_id)
    stopped=await repo.stop(session_id)
    from .runtime import cancel
    cancel(repo.owner_id,session_id)
    return {"stopped":stopped}

@router.get("/sessions/{session_id}/activity")
async def activity(session_id: str,request: Request,after: int=Query(0,ge=0),owner_id: str | None=None):
    actor,repo,_=await scope(request,session_id,owner_id)
    from app.db import get_app_db
    if not await get_app_db().is_user_admin(actor):
        raise HTTPException(403,"Diagnostic access required")
    from .activity import activity_page
    return await activity_page(repo,session_id,after=after)

class Ack(StrictModel):
    cursor: int = Field(ge=0)
    unread: bool = False

@router.get("/sessions/{session_id}/notification")
async def notification(session_id: str,request: Request,owner_id: str | None=None):
    actor,repo,_=await scope(request,session_id,owner_id)
    latest=await repo.latest_notification(session_id)
    if latest is None:
        return {"notification":None}
    ack=ChatRepository(actor).notification_ack(session_id)
    return {"notification":{**latest,
        "unread":ack["manual_unread"] or latest["cursor"]>ack["cursor"]}}

@router.get("/notifications")
async def notifications(request: Request, after: str = "", limit: int = Query(200, ge=1, le=200)):
    actor,repo,_=await scope(request)
    # Cursor names the last scanned session, including one without a terminal item.
    with repo.transaction(write=False) as conn:
        session_ids=[row[0] for row in conn.execute(
            "SELECT id FROM sessions WHERE user_id=? AND status='active' AND id>? ORDER BY id LIMIT ?",
            (actor,after,limit+1))]
    rows=[]
    for session_id in session_ids[:limit]:
        latest=await repo.latest_notification(session_id)
        if latest is not None:
            ack=repo.notification_ack(session_id)
            rows.append({**latest,"unread":ack["manual_unread"] or latest["cursor"]>ack["cursor"]})
    more=len(session_ids)>limit
    return {"notifications":rows,"has_more":more,
            "next_after":session_ids[limit-1] if more else None}

@router.post("/sessions/{session_id}/acknowledgement")
async def acknowledge(session_id: str,body: Ack,request: Request,owner_id: str | None=None):
    actor,owner_repo,_=await scope(request,session_id,owner_id)
    if not owner_repo.is_notification_cursor(session_id,body.cursor):
        raise HTTPException(409,"Acknowledgement must name a published notification revision")
    ChatRepository(actor).acknowledge(session_id,body.cursor,unread=body.unread)
    return {"cursor":body.cursor,"unread":body.unread}
