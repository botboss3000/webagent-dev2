"""Authenticated recorder API — capture leases, evidence, reports and approval.

Legacy /recordings reads remain available through legacy.py. New archives never
read session database files; scope verification uses the application DB facade.
Only these server-owned approval records can authorize execution.
"""
from __future__ import annotations
import asyncio
import base64
import io
import json
import time
from pathlib import Path
from typing import Any
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from app.api.db_viewer import require_admin
from . import legacy
from .config import settings, client_config
from .service import get_service
from .redaction import clean, html as clean_html

router=APIRouter(prefix="/api/v1/recordings",tags=["recorder"])

def owner(auth):
    value=auth.get("user_id") or auth.get("sub")
    if not value: raise HTTPException(403,"Authenticated owner required")
    return str(value)

def available():
    return Path(__file__).with_name("plugin.json").is_file() and settings()["enabled"] is True

def require_available():
    if not available(): raise HTTPException(409,"Recorder plugin unavailable")

async def owned(cid,uid):
    try:
        capture=await asyncio.to_thread(get_service().store.get,cid,uid)
    except PermissionError:
        raise HTTPException(404,"Recording not found")
    if not capture: raise HTTPException(404,"Recording not found")
    return capture

class LeaseRequest(BaseModel):
    enabled: bool = True
    client_id: str = Field(min_length=1,max_length=80)
    recording_id: str | None = Field(default=None,max_length=80)
    started_midrun: bool | None = None
    prebuffer: bool = False
    session_id: str | None = Field(default=None,max_length=80)
    agent_id: str | None = Field(default=None,max_length=80)

class CaptureProof(BaseModel):
    lease_token: str = ""
    client_id: str = Field(min_length=1,max_length=80)

async def proof(cid,body):
    require_available()
    lease=legacy._verify_capture_lease(body.lease_token)
    if lease.get("recording_id")!=cid or lease.get("client_id")!=body.client_id:
        raise HTTPException(403,"Recording lease mismatch")
    uid=str(lease["user_id"])
    if not await asyncio.to_thread(get_service().store.verify_document,cid,uid,body.client_id):
        raise HTTPException(403,"Recording document not registered")
    await owned(cid,uid)
    return uid

@router.get("/config")
async def config():
    return {"available":available(),"enabled":False,"scope":"tab","config":client_config() if available() else {}}

@router.get("/enabled")
async def enabled(_auth:dict=Depends(require_admin)):
    return await config()

@router.post("/enabled")
async def set_enabled(body:LeaseRequest,_auth:dict=Depends(require_admin)):
    require_available()
    if not body.enabled: return await config()
    uid=owner(_auth)
    if body.prebuffer: await verify_scope(uid,body.session_id)
    try:
        capture=await get_service().begin(uid,body.client_id,body.recording_id,time.time()+legacy.CAPTURE_LEASE_SECONDS)
    except (PermissionError,ValueError):
        raise HTTPException(409,"Recording cannot be resumed")
    if body.recording_id is None and body.started_midrun is not None:
        capture=await get_service().scope(capture["id"],uid,None,metadata={"started_midrun":body.started_midrun})
    if body.prebuffer and body.recording_id is None:
        await get_service().scope(capture["id"],uid,body.session_id,body.agent_id)
        capture=await get_service().promote_prebuffer(capture["id"],uid,body.client_id)
    lease=legacy._issue_capture_lease(_auth,client_id=body.client_id,recording_id=capture["id"])
    return {**await config(),**lease,"enabled":True,"capture":capture,"server_time_ms":time.time()*1000}

async def verify_scope(uid,session_id):
    if session_id:
        from app.db import get_db
        from app.db.offload import db_offload
        db=get_db()
        try:
            await db_offload(lambda:db.assert_session_owned(uid,session_id))
        except Exception:
            # A fresh browser-generated session UUID may precede its first send.
            # Permit that provisional binding only if the session does not yet
            # exist. Existing sessions must still pass ownership verification.
            try:
                exists = await db_offload(lambda: db.get_session_status(session_id))
            except Exception:
                raise HTTPException(409,"Session scope could not be verified")
            if exists is not None:
                raise HTTPException(403,"Session belongs to a different owner")
            import uuid
            try: uuid.UUID(session_id)
            except ValueError: raise HTTPException(422,"New session scope needs a UUID")

class ArmedRequest(BaseModel):
    enabled: bool = True
    client_id: str = Field(min_length=1,max_length=80)
    session_id: str | None = Field(default=None,max_length=80)
    agent_id: str | None = Field(default=None,max_length=80)

@router.post("/armed")
async def set_armed(body:ArmedRequest,_auth:dict=Depends(require_admin)):
    require_available()
    uid=owner(_auth)
    if body.enabled: await verify_scope(uid,body.session_id)
    return await get_service().arm(uid,body.client_id,body.enabled,body.session_id,body.agent_id)

class Batch(BaseModel):
    records:list[dict]=Field(default_factory=list,max_length=200)
    lease_token:str=""

@router.post("")
async def ingest(body:Batch):
    require_available()
    lease=legacy._verify_capture_lease(body.lease_token)
    cid=str(lease["recording_id"]); uid=str(lease["user_id"])
    legacy._validate_lease_batch(body.records,lease)
    capture=await owned(cid,uid)
    if capture["state"] not in ("recording","degraded","finalizing","interrupted"):
        raise HTTPException(409,"Capture already finalized")
    if not await asyncio.to_thread(get_service().store.verify_document,cid,uid,str(lease["client_id"])):
        raise HTTPException(403,"Recording document not registered")
    if len(json.dumps(body.records,ensure_ascii=False).encode())>4_000_000:
        await asyncio.to_thread(get_service().store.gap,cid,"Browser batch exceeded intake limit")
        raise HTTPException(413,"Split recording batch into smaller chunks")
    records=[]
    for raw in body.records:
        if raw.get("seq") is None or isinstance(raw.get("seq"),bool):
            raise HTTPException(422,"Each browser event needs a sequence number")
        if not isinstance(raw["seq"],int) or raw["seq"]<0:
            raise HTTPException(422,"Invalid browser sequence")
        record=clean(raw)
        record["user_id"]=uid
        if raw.get("html") is not None: record["html"]=clean_html(raw["html"])
        detail=record.get("detail")
        if not isinstance(detail,dict): record["detail"]={"value":detail}
        records.append(record)
    try: result=await asyncio.to_thread(get_service().store.append,cid,records)
    except ValueError: raise HTTPException(409,"Capture already finalized")
    return {"status":"ok",**result,"server_time_ms":time.time()*1000}

class Scope(CaptureProof):
    session_id:str|None=Field(default=None,max_length=80)
    agent_id:str|None=Field(default=None,max_length=80)
    started_midrun:bool|None=None
    metadata:dict=Field(default_factory=dict)

@router.post("/captures/{cid}/scope")
async def scope(cid:str,body:Scope):
    uid=await proof(cid,body)
    capture=await owned(cid,uid)
    if capture["state"] not in ("recording","degraded"):
        raise HTTPException(409,"Capture is not active")
    await verify_scope(uid,body.session_id)
    metadata=clean(body.metadata)
    # Mid-run refers to recording start, not subsequent session switches.
    result=await get_service().scope(cid,uid,body.session_id,body.agent_id,metadata)
    return {"capture":result,"server_time_ms":time.time()*1000}

class Stop(CaptureProof):
    last_seq:int|None=Field(default=None,ge=-1)
    gaps:list[Any]=Field(default_factory=list,max_length=100)
    acknowledged:bool|None=None

@router.post("/captures/{cid}/stop")
async def stop(cid:str,body:Stop):
    uid=await proof(cid,body)
    capture=await owned(cid,uid)
    if capture["state"] in ("completed","incomplete"):
        return await asyncio.to_thread(get_service().store.detail,cid,uid)
    gaps=clean(body.gaps)
    if body.acknowledged is False:
        gaps.append("Browser reported unacknowledged recording uploads")
    return await get_service().stop_capture(cid,uid,body.client_id,body.last_seq,gaps)

class Marker(CaptureProof):
    marker_id:str|None=Field(default=None,min_length=1,max_length=80,pattern=r"^[A-Za-z0-9_-]+$")
    elapsed_ms:float=Field(default=0,ge=0)
    screenshot_at_ms:float|None=None
    note:str=Field(default="",max_length=4000)
    screenshot_data_url:str|None=Field(default=None,max_length=12_000_000)
    session_id:str|None=None
    event_seq:int|None=None

@router.post("/captures/{cid}/markers")
async def marker(cid:str,body:Marker):
    uid=await proof(cid,body)
    await owned(cid,uid)
    # A lost acknowledgement retries the stable browser marker ID. Return the
    # original before image decoding or final-state checks; retries cannot amend
    # evidence, invalidate a plan, or create another screenshot artifact.
    if body.marker_id:
        detail=await asyncio.to_thread(get_service().store.detail,cid,uid)
        existing=next((m for m in detail["markers"] if m["id"]==body.marker_id),None)
        if existing is not None: return {"marker":existing}
    image=None; mime=None
    if body.screenshot_data_url:
        prefix,separator,encoded=body.screenshot_data_url.partition(",")
        allowed={"data:image/png;base64":"image/png","data:image/jpeg;base64":"image/jpeg","data:image/webp;base64":"image/webp"}
        if not separator or prefix not in allowed: raise HTTPException(422,"Unsupported screenshot format")
        try:
            image=base64.b64decode(encoded,validate=True)
            if len(image)>8_000_000: raise ValueError()
            from PIL import Image
            with Image.open(io.BytesIO(image)) as im:
                if im.width*im.height>25_000_000: raise ValueError()
                im.verify()
            mime=allowed[prefix]
        except Exception:
            raise HTTPException(422,"Invalid or oversized screenshot")
    data=clean({"marker_id":body.marker_id,"client_id":body.client_id,"elapsed_ms":body.elapsed_ms,
                "screenshot_at_ms":body.screenshot_at_ms,"note":body.note,
                "session_id":body.session_id,"event_seq":body.event_seq})
    try: saved=await asyncio.to_thread(get_service().store.marker,cid,data,image,mime)
    except ValueError as exc: raise HTTPException(409,str(exc))
    return {"marker":saved}

@router.get("/captures")
async def captures(limit:int=Query(100,ge=1,le=200),_auth:dict=Depends(require_admin)):
    return {"captures":await asyncio.to_thread(get_service().store.list,owner(_auth),limit)}

@router.get("/captures/{cid}")
async def detail(cid:str,_auth:dict=Depends(require_admin)):
    uid=owner(_auth); await owned(cid,uid)
    return await asyncio.to_thread(get_service().store.detail,cid,uid)

@router.get("/captures/{cid}/events")
async def events(cid:str,after:int=Query(0,ge=0),limit:int=Query(100,ge=1,le=500),_auth:dict=Depends(require_admin)):
    await owned(cid,owner(_auth))
    return await asyncio.to_thread(get_service().store.events,cid,after,limit)

@router.get("/captures/{cid}/artifacts/{aid}")
async def artifact(cid:str,aid:str,_auth:dict=Depends(require_admin)):
    await owned(cid,owner(_auth))
    try: mime,image=await asyncio.to_thread(get_service().store.artifact,cid,aid)
    except KeyError: raise HTTPException(404,"Screenshot not found")
    return Response(image,media_type=mime,headers={"Cache-Control":"private, no-store","X-Content-Type-Options":"nosniff"})

@router.post("/captures/{cid}/investigate")
async def investigate(cid:str,_auth:dict=Depends(require_admin)):
    require_available()
    uid=owner(_auth); capture=await owned(cid,uid)
    if capture["state"] in ("recording","degraded","finalizing"):
        raise HTTPException(409,"Finish recording before investigation")
    return {"investigation":await get_service().investigate(cid,uid)}

class Approval(BaseModel):
    plan_revision:int=Field(ge=1)
    decision:str
    comment:str=Field(default="",max_length=4000)
    execution_agent_id:str|None=Field(default=None,max_length=80)

@router.post("/captures/{cid}/approval")
async def approval(cid:str,body:Approval,_auth:dict=Depends(require_admin)):
    require_available()
    if body.decision not in ("approve","reject"): raise HTTPException(422,"Invalid decision")
    uid=owner(_auth); capture=await owned(cid,uid)
    if capture["state"]=="expired": raise HTTPException(409,"Recording evidence expired")
    try:
        data=await asyncio.to_thread(get_service().store.approve,cid,uid,body.plan_revision,body.decision,body.comment,body.execution_agent_id)
    except ValueError as exc: raise HTTPException(409,str(exc))
    return {"investigation":data}

class Execute(BaseModel):
    plan_revision:int=Field(ge=1)
    execution_agent_id:str|None=Field(default=None,max_length=80)

@router.post("/captures/{cid}/execute")
async def execute(cid:str,body:Execute,request:Request,_auth:dict=Depends(require_admin)):
    require_available()
    uid=owner(_auth); capture=await owned(cid,uid)
    if capture["state"]=="expired": raise HTTPException(409,"Recording evidence expired")
    choice=(capture.get("investigation",{}).get("approval") or {}).get("execution_agent_id")
    if body.execution_agent_id and body.execution_agent_id!=choice:
        raise HTTPException(409,"Execution agent differs from approved choice; approve again")
    try:
        approved=await asyncio.to_thread(get_service().store.claim_execution,cid,uid,body.plan_revision)
    except PermissionError as exc: raise HTTPException(403,str(exc))
    from .investigation import execute as run
    return {"investigation":await run(get_service().store,cid,uid,approved,request=request,execution_agent_id=choice)}

@router.get("/execution-agents")
async def execution_agents(_auth:dict=Depends(require_admin)):
    from app.db import get_db
    from app.db.offload import db_offload
    uid=owner(_auth)
    agents=await db_offload(lambda:get_db().list_agents_for_user(uid,include_admin=True))
    # Native agents use the app's canonical permission/dispatch path. Engine
    # adapters are outside this plugin's scope and never inspected or modified.
    return {"agents":[{"id":a["id"],"name":a.get("name") or a["id"]}
                      for a in (agents or []) if not a.get("engine") or a.get("engine") in ("default","native","webagent")]}

# Keep historical read/stats/delete routes; new intake and lease lifecycle win.
for route in legacy.router.routes:
    if route.path in ("/api/v1/recordings/config","/api/v1/recordings/enabled"):
        continue
    if route.path=="/api/v1/recordings" and "POST" in route.methods:
        continue
    router.routes.append(route)
