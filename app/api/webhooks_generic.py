"""
Generic inbound webhook endpoint.

External services POST to:
  POST /api/v1/webhooks/generic/{webhook_id}

The webhook_id maps to a registration in the database.
The payload is forwarded to the agent loop as a user message,
and the agent's response is returned as the HTTP response.

Also exposes:
  PATCH /api/v1/webhooks/generic/{webhook_id} — edit registration
  GET   /api/v1/webhooks/generic/{webhook_id}/log — event log
"""

import json
import logging
import time

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel
from typing import Optional

from app.db import get_db
from app.agent.loop import run_agent_loop_buffered
from app.agent.prompts import build_system_prompt, CONTEXT_SECTION_TYPES
from app.agent.session_history import build_openai_history_from_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/webhooks/generic", tags=["webhooks"])


class UpdateWebhookBody(BaseModel):
    user_id: str
    name: Optional[str] = None
    instructions: Optional[str] = None
    active: Optional[bool] = None


@router.patch("/{webhook_id}")
async def patch_webhook(request: Request, webhook_id: str, body: UpdateWebhookBody):
    """Edit a webhook registration (name, instructions, active toggle)."""
    from app.auth.identity import assert_caller_is
    uid = await assert_caller_is(request, body.user_id)
    db = get_db()
    reg = await db.get_webhook(webhook_id)
    if not reg:
        raise HTTPException(status_code=404, detail="Webhook not found.")
    if reg.get("user_id") != uid and not await db.is_user_admin(uid):
        raise HTTPException(status_code=403, detail="Not the owner of this webhook.")
    fields = {}
    if body.name is not None:
        fields["name"] = body.name
    if body.instructions is not None:
        fields["instructions"] = body.instructions
    if body.active is not None:
        fields["active"] = 1 if body.active else 0
    if not fields:
        return {"webhook": reg}
    updated = await db.update_webhook(webhook_id, uid, **fields)
    return {"webhook": updated}


@router.get("/{webhook_id}/log")
async def get_webhook_log(request: Request, webhook_id: str, user_id: str = Query(...)):
    """Return recent event log entries for a webhook."""
    from app.auth.identity import assert_caller_is
    uid = await assert_caller_is(request, user_id)
    db = get_db()
    reg = await db.get_webhook(webhook_id)
    if not reg:
        raise HTTPException(status_code=404, detail="Webhook not found.")
    if reg.get("user_id") != uid and not await db.is_user_admin(uid):
        raise HTTPException(status_code=403, detail="Not the owner of this webhook.")
    events = await db.get_webhook_logs(webhook_id)
    return {"events": events}


class UpdateWebhookBody(BaseModel):
    user_id: str
    name: str | None = None
    instructions: str | None = None
    active: bool | None = None


@router.patch("/{webhook_id}")
async def patch_webhook(request: Request, webhook_id: str, body: UpdateWebhookBody):
    """Edit a generic webhook registration (name, instructions, active toggle)."""
    from app.auth.identity import assert_caller_is
    uid = await assert_caller_is(request, body.user_id)
    db = get_db()
    reg = await db.get_webhook(webhook_id)
    if not reg:
        raise HTTPException(status_code=404, detail="Webhook not found.")
    if reg.get("user_id") != uid and not await db.is_user_admin(uid):
        raise HTTPException(status_code=403, detail="Not the owner of this webhook.")

    fields = {}
    if body.name is not None:
        fields["name"] = body.name
    if body.instructions is not None:
        fields["instructions"] = body.instructions
    if body.active is not None:
        fields["active"] = 1 if body.active else 0

    if not fields:
        return {"webhook": reg}

    conn = db._get_conn()
    try:
        sets = ", ".join(f"{k} = ?" for k in fields)
        vals = list(fields.values()) + [_now_iso(), webhook_id]
        conn.execute(f"UPDATE webhook_registrations SET {sets}, updated_at = ? WHERE id = ?", vals)
        conn.commit()
    finally:
        conn.close()

    fresh = await db.get_webhook(webhook_id)
    return {"webhook": fresh}


@router.get("/{webhook_id}/log")
async def get_webhook_log(request: Request, webhook_id: str, user_id: str = Query(...)):
    """Return recent event log entries for a generic webhook."""
    from app.auth.identity import assert_caller_is
    uid = await assert_caller_is(request, user_id)
    db = get_db()
    reg = await db.get_webhook(webhook_id)
    if not reg:
        raise HTTPException(status_code=404, detail="Webhook not found.")
    if reg.get("user_id") != uid and not await db.is_user_admin(uid):
        raise HTTPException(status_code=403, detail="Not the owner of this webhook.")

    conn = db._get_conn()
    try:
        rows = conn.execute(
            """SELECT id, method, headers, payload, response_status,
                      response_body, duration_ms, created_at
               FROM webhook_event_log
               WHERE webhook_id = ?
               ORDER BY created_at DESC
               LIMIT 20""",
            (webhook_id,),
        ).fetchall()
        events = []
        for r in rows:
            d = dict(r)
            d["payload"] = (d.get("payload") or "")[:500]
            d["response_body"] = (d.get("response_body") or "")[:500]
            events.append(d)
        return {"events": events}
    finally:
        conn.close()


@router.post("/{webhook_id}")
async def generic_webhook_handler(webhook_id: str, request: Request):
    """
    Receive an inbound webhook from any external service.

    Looks up the webhook registration, parses the payload,
    creates an interaction, runs the agent loop with the
    registration's instructions, and returns the agent's response.
    """
    db = get_db()

    # 1. Look up registration
    registration = await db.get_webhook(webhook_id)
    if not registration:
        logger.warning("Webhook %s not found", webhook_id)
        return Response(content='{"error":"webhook not found"}', status_code=404)

    if not registration.get("active", True):
        logger.warning("Webhook %s is disabled", webhook_id)
        return Response(content='{"error":"webhook disabled"}', status_code=410)

    user_id = registration["user_id"]
    instructions = registration.get("instructions", "")

    # 2. Read request
    method = request.method
    in_headers = dict(request.headers)
    try:
        body_bytes = await request.body()
        body_str = body_bytes.decode("utf-8", errors="replace")
    except Exception:
        body_str = ""

    # 3. Build the user message from the webhook data
    content_type = in_headers.get("content-type", "")
    if content_type.startswith("application/json") and body_str:
        try:
            parsed = json.loads(body_str)
            user_message = f"[WEBHOOK] {registration['name']}\n\nPayload:\n```json\n{json.dumps(parsed, indent=2)[:8000]}\n```"
        except json.JSONDecodeError:
            user_message = f"[WEBHOOK] {registration['name']}\n\nRaw body:\n{body_str[:8000]}"
    else:
        user_message = f"[WEBHOOK] {registration['name']}\n\nRaw body:\n{body_str[:8000]}"

    start = time.time()

    try:
        # 4. Ensure session exists, then save incoming interaction
        session_id = user_id
        try:
            await db.assert_session_owned(user_id, session_id)
        except PermissionError:
            now = _now_iso()
            client = db.get_raw_client()
            client.table("sessions").insert({
                "id": session_id,
                "user_id": user_id,
                "title": "Webhook Session",
                "created_at": now,
                "updated_at": now,
            }).execute()
        await db.insert_interaction(
            user_id, session_id, role="user", content=user_message,
            channel="webhook",
            metadata=json.dumps({
                "source": f"webhook/generic/{webhook_id}",
                "webhook_name": registration["name"],
                "method": method,
            }),
        )

        # 5. Look up the user's agent
        agent = await db.get_agent_for_user(user_id)
        if agent is None:
            logger.warning("webhook %s: user %s has no agent assigned; skipping run", webhook_id, user_id)
            return Response(content='{"error":"no agent assigned for user"}', status_code=400, media_type="application/json")

        # 6. Fetch agent + context docs
        agent_with_ctx = await db.fetch_agent_with_context(user_id, CONTEXT_SECTION_TYPES)
        if agent_with_ctx:
            agent = agent_with_ctx
        if not agent.get("context_documents"):
            copied = await db.copy_defaults_to_agent(agent["id"])
            if copied > 0:
                agent = await db.fetch_agent_with_context(user_id, CONTEXT_SECTION_TYPES)
        context_docs = agent.get("context_documents", [])

        # 7. Build system prompt
        if instructions:
            webhook_block = (
                "## [WEBHOOK INSTRUCTIONS]\n"
                "This message arrived via webhook. Follow these instructions:\n"
                f"{instructions}\n"
            )
            context_docs = [{"id": "_webhook_overlay", "content": webhook_block}] + context_docs
        system_prompt = await build_system_prompt(context_docs, brain_context=None, user_id=user_id, session_id=session_id)

        # 8. History
        history = await build_openai_history_from_session(db, user_id, session_id, exclude_interaction_ids=None)
        if len(history) > 20:
            history = history[-20:]

        # 9. Run agent loop
        from app.agent.runner import run_supervised_turn, RunOutcome
        _wh_agent_id = agent.get("id")
        _relaunch_ctx = {
            "origin": "webhook", "session_id": session_id, "user_id": user_id,
            "agent_id": _wh_agent_id, "channel": "webhook", "timeout_seconds": 600,
        }

        async def _build_webhook_turn(replaced: bool) -> RunOutcome:
            _reply = await run_agent_loop_buffered(
                user_id=user_id, session_id=session_id, user_message=user_message,
                system_prompt=system_prompt, history=history,
                max_turns=agent.get("max_turn_count", 0), channel="webhook",
            )
            if not (_reply or "").strip():
                # The loop errored (it now returns "" instead of a fabricated
                # "I encountered an error: …"). Record it as a resumable
                # failure, not a fake 'complete'.
                return RunOutcome(status="error", stop_cause="empty_response",
                                  error="agent loop produced no final reply")
            return RunOutcome(status="complete", stop_cause="complete", reply=_reply)

        _outcome = await run_supervised_turn(
            session_id=session_id, user_id=user_id, agent_id=_wh_agent_id,
            origin="webhook", channel="webhook", relaunch_ctx=_relaunch_ctx,
            build_turn=_build_webhook_turn, await_result=True, result_timeout=620,
        )
        reply = (_outcome.reply if _outcome else "") or ""
        duration_ms = int((time.time() - start) * 1000)

        # 10. Log the event
        try:
            await db.log_webhook_event(
                webhook_id=webhook_id,
                method=method,
                headers=json.dumps({k: v for k, v in in_headers.items()
                                   if k.lower() not in ("authorization", "cookie", "x-api-key")}),
                payload=body_str[:5000], response_status=200, response_body=reply[:5000],
                duration_ms=duration_ms,
            )
        except Exception as e:
            logger.warning("Failed to log webhook event: %s", e)

        return Response(
            content=json.dumps({"status": "ok", "reply": reply, "webhook_id": webhook_id, "duration_ms": duration_ms}),
            media_type="application/json", status_code=200,
        )

    except Exception as e:
        logger.error("Webhook handler error for %s: %s", webhook_id, e, exc_info=True)
        return Response(content=json.dumps({"status": "error", "message": str(e)}), media_type="application/json", status_code=500)