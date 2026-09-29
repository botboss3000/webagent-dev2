"""Kill-switch HTTP API.

Admin-only control for the header kill-switch button. ``GET`` reports the
current state; ``POST`` engages or disengages the switch. Engaging cancels all
live runs and stops every background service; disengaging restarts them.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from app.auth.identity import require_platform_admin

router = APIRouter(
    prefix="/api/v1",
    tags=["kill-switch"],
    dependencies=[Depends(require_platform_admin)],
)


@router.get("/kill-switch")
async def get_kill_switch():
    from app.kill_switch import status
    return status()


@router.post("/kill-switch")
async def set_kill_switch(request: Request):
    from app.auth.identity import request_user_id
    from app.db import get_db

    caller_id = request_user_id(request)
    if not caller_id or not await get_db().is_user_admin(caller_id):
        raise HTTPException(status_code=403, detail="Admin access required")

    try:
        body = await request.json()
    except Exception:
        body = {}
    engaged = body.get("engaged", False) is True

    from app import kill_switch
    if engaged:
        result = await kill_switch.engage(app=request.app)
    else:
        result = await kill_switch.disengage(app=request.app)
    # Fleet propagation: ask every OTHER known device (multi-instance installs
    # sharing one DB) to do the same via the device-dispatch action queue.
    # Best-effort — a broadcast failure must never undo the local kill.
    try:
        result["fleet_targets"] = await kill_switch.broadcast_to_fleet(engaged, caller_id)
    except Exception:
        result["fleet_targets"] = 0
    # Tell every OTHER browser/tab of this user about the toggle so their
    # session lists clear spinners right away (agentWs re-dispatches this as
    # kill-switch-changed). Best-effort, never raises.
    try:
        from app.events.delivery import notify_user
        await notify_user(caller_id, {"type": "kill_switch", "engaged": engaged})
    except Exception:
        pass
    return result
