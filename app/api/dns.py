"""Domains admin API: /admin/dns/*

Backs the Domains card in App Config → Data Settings. Connects to the admin's DNS
provider (Cloudflare, Namecheap, …) and points a domain at a server. ALL endpoints
are admin-only — editing DNS is an admin power, exactly like Deploy and Remote
Access — and provider keys ride into the encrypted vault (app/dns/credentials.py),
never returned to the browser. The point / verify actions stream NDJSON progress
(like the Deploy + Commit ⭐ streams) so the panel shows a live log.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.dns import manager
from app.dns.registry import get_provider

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/dns", tags=["admin-dns"])


async def _require_admin(uid: str) -> None:
    from app.auth.identity import resolve_admin_uid
    if not await resolve_admin_uid(uid):
        raise HTTPException(status_code=403, detail="Admin required")


# ── Models ──
class UserBody(BaseModel):
    requesting_user_id: str = ""


class ProviderBody(UserBody):
    provider: str


class ConnectBody(UserBody):
    provider: str
    values: dict = {}


class ZoneBody(UserBody):
    provider: str
    zone_id: str = ""
    zone_name: str = ""


class PointBody(UserBody):
    provider: str
    zone_id: str = ""
    zone_name: str = ""
    host: str = ""
    ip: str = ""
    ttl: int = 300
    proxied: bool = False
    also_www: bool = False


class VerifyBody(UserBody):
    provider: str
    host: str = ""
    ip: str = ""


# ── Catalog ──
@router.get("/catalog")
async def catalog(requesting_user_id: str = ""):
    await _require_admin(requesting_user_id)
    return await manager.build_catalog()


# ── Which provider is selected ──
@router.post("/select")
async def select(body: ProviderBody):
    await _require_admin(body.requesting_user_id)
    if not get_provider(body.provider):
        raise HTTPException(status_code=400, detail="Unknown provider")
    from app.dns import store
    store.set_active_provider(body.provider)
    return {"ok": True, "active_provider": body.provider}


# ── Connect / disconnect / test ──
@router.post("/connect")
async def connect(body: ConnectBody):
    await _require_admin(body.requesting_user_id)
    if not get_provider(body.provider):
        raise HTTPException(status_code=400, detail="Unknown provider")
    res = await manager.save_connection(body.provider, body.values or {})
    if not res.get("ok"):
        raise HTTPException(status_code=500, detail=res.get("detail", "Could not save."))
    return res


@router.post("/disconnect")
async def disconnect(body: ProviderBody):
    await _require_admin(body.requesting_user_id)
    if not get_provider(body.provider):
        raise HTTPException(status_code=400, detail="Unknown provider")
    return await manager.disconnect(body.provider)


@router.post("/test")
async def test(body: ProviderBody):
    await _require_admin(body.requesting_user_id)
    if not get_provider(body.provider):
        raise HTTPException(status_code=400, detail="Unknown provider")
    return await manager.test(body.provider)


# ── Domains + records ──
@router.post("/zones")
async def zones(body: ProviderBody):
    await _require_admin(body.requesting_user_id)
    if not get_provider(body.provider):
        raise HTTPException(status_code=400, detail="Unknown provider")
    return await manager.list_zones(body.provider)


@router.post("/records")
async def records(body: ZoneBody):
    await _require_admin(body.requesting_user_id)
    if not get_provider(body.provider):
        raise HTTPException(status_code=400, detail="Unknown provider")
    return await manager.list_records(body.provider, {"id": body.zone_id, "name": body.zone_name})


# ── Verify (non-streaming: a quick resolution re-check) ──
@router.post("/verify")
async def verify_endpoint(body: VerifyBody):
    await _require_admin(body.requesting_user_id)
    if not get_provider(body.provider):
        raise HTTPException(status_code=400, detail="Unknown provider")
    return await manager.run_verify(body.provider, body.host, body.ip)


# ── Point a domain at a server (streaming NDJSON) ──
def _stream(agen):
    async def _gen():
        try:
            async for evt in agen:
                yield json.dumps(evt) + "\n"
        except Exception as e:  # never leave the client's reader hanging
            logger.exception("dns stream crashed")
            yield json.dumps({"phase": "done", "result": {"ok": False, "message": str(e) or "failed"}}) + "\n"
    return StreamingResponse(
        _gen(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


@router.post("/point")
async def point(body: PointBody):
    await _require_admin(body.requesting_user_id)
    if not get_provider(body.provider):
        raise HTTPException(status_code=400, detail="Unknown provider")
    return _stream(manager.run_point(body.provider, {
        "zone_id": body.zone_id, "zone_name": body.zone_name, "host": body.host,
        "ip": body.ip, "ttl": body.ttl, "proxied": body.proxied, "also_www": body.also_www,
    }))


PROVIDER_TEMPLATE_HINT = "Add a provider: drop a file in app/dns/providers/. See _TEMPLATE.py."
