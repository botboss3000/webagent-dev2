"""
Per-user "connect your own database" API (User BYOD / bring-your-own-database).

Lets a signed-in user point WebAgent at THEIR OWN Postgres, so their interaction
data (chats, memories, agents, secrets) lives in a database they own instead of
the shared central one. Only meaningful when App Settings → User BYOD is
ON; when it's off these endpoints still work but routing ignores the record.

Endpoints (all scoped to the verified caller — a user only ever manages their own
database, never another user's):
  GET    /api/v1/tenant-db          → current status (configured? state? MT on?)
  POST   /api/v1/tenant-db/test     → test a connection without saving
  POST   /api/v1/tenant-db          → test, then save + bootstrap the schema
  DELETE /api/v1/tenant-db          → forget the personal DB (data is left intact)

Reuses the existing DB plumbing: DBConnectionConfig (app/db/connection_config.py),
test_connection() (app/db/postgres_backend.py), the tenant registry
(app/db/tenant_registry.py) and the per-user resolver cache (app/db/tenant.py).
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/tenant-db", tags=["tenant-db"])

# Personal databases must be a Postgres-family endpoint (the per-user backend is
# built by build_postgres_backend). Mirrors the _PG_PROVIDERS set in app/db.
_ALLOWED_PROVIDERS = ("postgres", "neon", "gcp_cloud_sql", "aws_rds", "azure_postgres")


class TenantDBBody(BaseModel):
    provider: str = "postgres"
    host: Optional[str] = None
    port: Optional[int] = 5432
    database: str = ""
    username: Optional[str] = None
    password: Optional[str] = None      # write-only; stored in the credential store
    ssl_mode: str = "require"
    schema_name: str = "public"         # 'schema' collides with BaseModel internals
    options: dict = {}


def _caller_uid(request: Request) -> str:
    """The verified id of the signed-in caller (set by CallerIdentityMiddleware).
    A user may only manage their OWN personal database."""
    from app.auth.identity import get_verified_caller_uid
    uid = get_verified_caller_uid()
    if not uid:
        raise HTTPException(status_code=401, detail="Sign in to manage your database")
    return uid


def _cfg_from_body(body: TenantDBBody):
    from app.db.connection_config import DBConnectionConfig
    if body.provider not in _ALLOWED_PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported provider '{body.provider}'. Use one of: {', '.join(_ALLOWED_PROVIDERS)}",
        )
    return DBConnectionConfig(
        provider=body.provider,
        host=body.host,
        port=body.port,
        database=body.database or "",
        username=body.username,
        ssl_mode=body.ssl_mode or "require",
        schema=body.schema_name or "public",
        options=body.options or {},
    )


@router.get("")
async def get_tenant_db(request: Request):
    """Status of the caller's personal database: whether multi-tenant is on,
    whether they've configured one, and the current connection state."""
    uid = _caller_uid(request)
    from app.admin.settings import get_user_byod_enabled
    from app.db.tenant_registry import get_tenant_record
    from app.db.tenant import tenant_health

    rec = get_tenant_record(uid) or {}
    return {
        "user_byod_enabled": get_user_byod_enabled(),
        "configured": bool(rec),
        # Non-secret connection details only (never the password).
        "connection": {
            k: rec.get(k) for k in
            ("provider", "host", "port", "database", "username", "ssl_mode", "schema", "status", "last_ok_at")
        } if rec else None,
        "health": tenant_health(uid),
    }


@router.post("/test")
async def test_tenant_db(request: Request, body: TenantDBBody):
    """Try the connection without saving. Returns {ok, server_version|error}."""
    _caller_uid(request)
    from app.db.postgres_backend import test_connection
    cfg = _cfg_from_body(body)
    if not body.password:
        raise HTTPException(status_code=400, detail="Password is required to test the connection")
    result = await test_connection(cfg, body.password)
    return result


@router.post("")
async def set_tenant_db(request: Request, body: TenantDBBody):
    """Test, then persist the caller's personal-database record and bootstrap its
    schema. On success their data routes to this database from the next request."""
    uid = _caller_uid(request)
    from app.db.postgres_backend import test_connection
    from app.db import tenant_registry
    from app.db import tenant as tenant_mod

    cfg = _cfg_from_body(body)
    if not body.password:
        raise HTTPException(status_code=400, detail="Password is required")

    # 1) Verify we can actually reach it before saving anything.
    probe = await test_connection(cfg, body.password)
    if not probe.get("ok"):
        raise HTTPException(status_code=400, detail=f"Could not connect: {probe.get('error')}")

    # 2) Persist the record + password, and drop any stale cached backend.
    tenant_registry.set_tenant_db(uid, cfg, body.password)
    tenant_mod.evict(uid)

    # 3) Build the backend once now so the schema (+ default templates) is
    #    bootstrapped up front and the first chat isn't slowed by cold setup.
    #    build_postgres_backend runs the fingerprint-gated _init_db(seed=True).
    bootstrapped = False
    try:
        import asyncio
        await asyncio.to_thread(tenant_mod.get_tenant_backend, uid)
        tenant_registry.update_status(uid, "active", "Connected", probe.get("database"))
        bootstrapped = True
    except Exception as e:  # noqa: BLE001
        logger.warning("Tenant DB bootstrap failed for %s: %s", uid, e)
        tenant_registry.update_status(uid, "error", str(e)[:300])

    return {
        "ok": True,
        "bootstrapped": bootstrapped,
        "server_version": probe.get("server_version"),
        "database": probe.get("database"),
    }


@router.delete("")
async def delete_tenant_db(request: Request):
    """Forget the caller's personal database (removes the pointer + stored
    password and closes the pool). Does NOT delete any data inside that database
    — the user still owns it. Afterwards they fall back to the shared database."""
    uid = _caller_uid(request)
    from app.db import tenant_registry
    from app.db import tenant as tenant_mod
    existed = tenant_registry.delete_tenant_db(uid)
    tenant_mod.evict(uid)
    return {"ok": True, "removed": existed}
