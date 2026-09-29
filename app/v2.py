"""Isolated V2 entry point. No current-development data or services are inherited."""
import os
from pathlib import Path

os.environ.setdefault("WEBAGENT_ENCRYPTION_LEVEL", "field")
os.environ.setdefault("WEBAGENT_SECRETS_PROVIDER", "inline_db")
os.environ.setdefault("WEBAGENT_DB_MODE", "local")
os.environ["WEBAGENT_V2_ISOLATED"] = "1"

from app.chat.bootstrap import initialize
INSTANCE_ID = initialize()
from app.main import app
from app.chat.api import router as chat_router
app.include_router(chat_router)

@app.on_event("startup")
async def start_chat_recovery():
    import asyncio
    from app.chat.recovery import recover_once, supervise
    await recover_once(startup=True)
    app.state.chat_recovery_task = asyncio.create_task(supervise(),name="controlled-chat-recovery")

@app.on_event("shutdown")
async def stop_chat_recovery():
    from app.chat.recovery import shutdown
    await shutdown(app.state.chat_recovery_task)

@app.middleware("http")
async def isolation_boundary(request, call_next):
    if request.url.path.startswith(("/api/v1/webhook", "/webhook", "/api/v1/connector")):
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail":"External ingestion is disabled on this test instance"}, status_code=503)
    response = await call_next(request)
    response.headers["X-WebAgent-Instance"] = INSTANCE_ID
    if request.url.path.startswith(("/ui/", "/app", "/api/v2/chat")):
        response.headers["Cache-Control"] = "no-store"
    return response

