"""Agent-scoped hosting-provider connections and DNS-management tools.

Provider adapters are reused from :mod:`app.dns`; this ability owns only the
agent-scoped encrypted profiles, its Abilities-panel API, and tool boundary.
Adding a DNS provider to the drop-in registry therefore makes it available here
without another central provider list.
"""

from __future__ import annotations

import ipaddress
import json
import re
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.auth.identity import assert_caller_is
from app.dns.registry import get_provider, list_providers


ABILITY_ID = "host_management"
VAULT_USER = "admin"
VAULT_SERVICE = "ability_host_management"
_PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{5,63}$")

router = APIRouter(prefix="/api/v1/host-management", tags=["host-management"])
TOOL_SCHEMAS: Dict[str, dict] = {}
DESTRUCTIVE = {"host_upsert_dns_record"}


def _json(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


def _as_dict(raw: Any) -> dict:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            value = json.loads(raw)
            return dict(value) if isinstance(value, dict) else {}
        except Exception:
            return {}
    return {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _label(agent_id: str, profile_id: str) -> str:
    return f"agent:{agent_id}:{profile_id}"


def _profile_id(agent_id: str, label: str) -> str:
    prefix = f"agent:{agent_id}:"
    return label[len(prefix):] if label.startswith(prefix) else ""


def _public_provider(provider) -> dict:
    available, reason = provider.available()
    return {
        "id": provider.id,
        "display_name": provider.display_name,
        "icon": provider.icon,
        "summary": provider.summary,
        "requires": list(provider.requires or []),
        "manual": bool(getattr(provider, "manual", False)),
        "available": bool(available),
        "unavailable_reason": reason or "",
        "config_fields": list(provider.config_fields or []),
        "credential_fields": [
            {key: value for key, value in field.items() if key != "secret"}
            | {"secret": bool(field.get("secret"))}
            for field in (provider.credential_fields or [])
        ],
    }


async def _require_agent_admin(request: Request, agent_id: str, user_id: str) -> str:
    from app.api.agents import _is_agent_admin
    from app.db import get_db

    resolved = await assert_caller_is(request, user_id)
    if not await _is_agent_admin(get_db(), agent_id, resolved):
        raise HTTPException(status_code=403, detail="Only agent admins can manage hosting connections.")
    return resolved


async def _rows(agent_id: str) -> List[dict]:
    from app.db import get_db

    prefix = f"agent:{agent_id}:"
    rows = await get_db().auth_element_list(VAULT_USER, VAULT_SERVICE)
    return [row for row in (rows or []) if str(row.get("label") or "").startswith(prefix)]


def _public_profile(agent_id: str, row: dict) -> dict:
    config = _as_dict(row.get("config"))
    credentials = _as_dict(row.get("secret_ref"))
    provider = get_provider(str(config.get("provider") or ""))
    fields = list(provider.credential_fields or []) if provider else []
    return {
        **config,
        "id": _profile_id(agent_id, str(row.get("label") or "")),
        "provider_name": provider.display_name if provider else str(config.get("provider") or "Unknown"),
        "credentials_set": {
            str(field.get("key")): bool(str(credentials.get(str(field.get("key"))) or "").strip())
            for field in fields
        },
        "configured": bool(provider) and all(
            str(credentials.get(key) or "").strip()
            for key in (
                provider.credential_required
                if provider.credential_required is not None
                else [str(field.get("key")) for field in fields if field.get("required") or field.get("secret")]
            )
        ),
    }


async def _profiles(agent_id: str, *, include_secrets: bool = False) -> List[dict]:
    out = []
    for row in await _rows(agent_id):
        profile = _public_profile(agent_id, row)
        if include_secrets:
            profile["credentials"] = _as_dict(row.get("secret_ref"))
        out.append(profile)
    out.sort(key=lambda item: (str(item.get("name") or "").casefold(), str(item.get("id") or "")))
    return out


async def _find_profile(agent_id: str, profile_ref: str, *, include_secrets: bool = True) -> Optional[dict]:
    ref = str(profile_ref or "").strip()
    for profile in await _profiles(agent_id, include_secrets=include_secrets):
        if profile.get("id") == ref or str(profile.get("name") or "").casefold() == ref.casefold():
            return profile
    return None


def _field_keys(fields: list) -> set[str]:
    return {str(field.get("key")) for field in fields if field.get("key")}


def _normalize_profile(payload: dict, existing: Optional[dict] = None) -> tuple[dict, dict]:
    old = existing or {}
    provider_id = str(payload.get("provider") or old.get("provider") or "").strip().lower()
    provider = get_provider(provider_id)
    if not provider:
        raise HTTPException(status_code=422, detail="Choose an installed hosting provider.")
    available, reason = provider.available()
    if not available:
        raise HTTPException(status_code=422, detail=reason or "That provider is unavailable.")
    name = str(payload.get("name") or old.get("name") or provider.display_name).strip()[:80]
    if not name:
        raise HTTPException(status_code=422, detail="Connection name is required.")

    old_config = _as_dict(old.get("provider_config"))
    submitted_config = _as_dict(payload.get("provider_config"))
    config_keys = _field_keys(list(provider.config_fields or []))
    provider_config = {key: value for key, value in old_config.items() if key in config_keys}
    provider_config.update({key: value for key, value in submitted_config.items() if key in config_keys})

    old_credentials = _as_dict(old.get("credentials"))
    submitted_credentials = _as_dict(payload.get("credentials"))
    credential_keys = _field_keys(list(provider.credential_fields or []))
    credentials = {key: value for key, value in old_credentials.items() if key in credential_keys}
    for key, value in submitted_credentials.items():
        if key in credential_keys and str(value or "").strip():
            credentials[key] = value

    required = (
        provider.credential_required
        if provider.credential_required is not None
        else [str(field.get("key")) for field in (provider.credential_fields or []) if field.get("required")]
    )
    missing = [key for key in required if not str(credentials.get(key) or "").strip()]
    if missing and not getattr(provider, "manual", False):
        labels = {
            str(field.get("key")): str(field.get("label") or field.get("key"))
            for field in (provider.credential_fields or [])
        }
        raise HTTPException(status_code=422, detail="Missing: " + ", ".join(labels.get(key, key) for key in missing))

    now = _now()
    config = {
        "name": name,
        "provider": provider_id,
        "provider_config": provider_config,
        "last_test": old.get("last_test") if isinstance(old.get("last_test"), dict) else {},
        "created_at": old.get("created_at") or now,
        "updated_at": now,
    }
    return config, credentials


async def _save(agent_id: str, profile_id: str, config: dict, credentials: dict) -> None:
    from app.db import get_db

    await get_db().auth_element_set(
        user_id=VAULT_USER,
        service=VAULT_SERVICE,
        label=_label(agent_id, profile_id),
        config=config,
        secret_ref=_json(credentials),
    )


def _provider_for(profile: dict):
    provider = get_provider(str(profile.get("provider") or ""))
    if not provider:
        raise ValueError("The saved hosting provider is no longer installed.")
    available, reason = provider.available()
    if not available:
        raise ValueError(reason or "The saved hosting provider is unavailable.")
    return provider


class ConnectionPayload(BaseModel):
    user_id: str
    id: Optional[str] = None
    name: str
    provider: str
    provider_config: Dict[str, Any] = Field(default_factory=dict)
    credentials: Dict[str, Any] = Field(default_factory=dict)


@router.get("/providers")
async def providers_catalog():
    return {"providers": [_public_provider(provider) for provider in list_providers()]}


@router.get("/{agent_id}")
async def list_connections(agent_id: str, request: Request, user_id: str = Query(...)):
    await _require_agent_admin(request, agent_id, user_id)
    return {"connections": await _profiles(agent_id)}


@router.put("/{agent_id}")
async def put_connection(agent_id: str, body: ConnectionPayload, request: Request):
    await _require_agent_admin(request, agent_id, body.user_id)
    profile_id = str(body.id or "").strip().lower()
    if not _PROFILE_RE.fullmatch(profile_id):
        profile_id = "host_" + secrets.token_hex(8)
    existing = await _find_profile(agent_id, profile_id, include_secrets=True)
    config, credentials = _normalize_profile(body.model_dump(), existing)
    await _save(agent_id, profile_id, config, credentials)
    return {"connection": await _find_profile(agent_id, profile_id, include_secrets=False)}


@router.delete("/{agent_id}/{profile_id}")
async def delete_connection(agent_id: str, profile_id: str, request: Request, user_id: str = Query(...)):
    from app.db import get_db

    await _require_agent_admin(request, agent_id, user_id)
    deleted = await get_db().auth_element_delete(VAULT_USER, VAULT_SERVICE, _label(agent_id, profile_id))
    if not deleted:
        raise HTTPException(status_code=404, detail="Hosting connection not found.")
    return {"deleted": True}


@router.post("/{agent_id}/{profile_id}/test")
async def test_connection(agent_id: str, profile_id: str, request: Request, user_id: str = Query(...)):
    await _require_agent_admin(request, agent_id, user_id)
    profile = await _find_profile(agent_id, profile_id, include_secrets=True)
    if not profile:
        raise HTTPException(status_code=404, detail="Hosting connection not found.")
    try:
        provider = _provider_for(profile)
        result = await provider.test(profile.get("provider_config") or {}, profile.get("credentials") or {})
    except Exception as exc:
        result = {"ok": False, "detail": str(exc)[:400]}
    config = {key: value for key, value in profile.items() if key not in {"id", "provider_name", "credentials_set", "configured", "credentials"}}
    config["last_test"] = {**result, "tested_at": _now()}
    await _save(agent_id, profile_id, config, profile.get("credentials") or {})
    return result


@router.get("/{agent_id}/{profile_id}/zones")
async def connection_zones(agent_id: str, profile_id: str, request: Request, user_id: str = Query(...)):
    await _require_agent_admin(request, agent_id, user_id)
    profile = await _find_profile(agent_id, profile_id, include_secrets=True)
    if not profile:
        raise HTTPException(status_code=404, detail="Hosting connection not found.")
    provider = _provider_for(profile)
    return await provider.list_zones(profile.get("provider_config") or {}, profile.get("credentials") or {})


async def _tool_profile(agent_id: str, connection: str) -> tuple[dict, Any]:
    profile = await _find_profile(agent_id, connection, include_secrets=True)
    if not profile:
        raise ValueError("Hosting connection not found.")
    return profile, _provider_for(profile)


def build_tools(*, agent_id: str = "", **_ctx):
    async def host_list_connections() -> str:
        profiles = await _profiles(agent_id)
        return _json({"status": "ok", "connections": [
            {"id": p.get("id"), "name": p.get("name"), "provider": p.get("provider"),
             "provider_name": p.get("provider_name"), "configured": p.get("configured")}
            for p in profiles
        ]})

    async def host_list_zones(connection: str) -> str:
        try:
            profile, provider = await _tool_profile(agent_id, connection)
            result = await provider.list_zones(profile.get("provider_config") or {}, profile.get("credentials") or {})
            return _json({"status": "ok" if result.get("ok") else "error", "connection": profile.get("name"), **result})
        except Exception as exc:
            return _json({"status": "error", "message": str(exc)})

    async def host_list_dns_records(connection: str, zone_id: str, zone_name: str) -> str:
        try:
            profile, provider = await _tool_profile(agent_id, connection)
            result = await provider.list_records(
                profile.get("provider_config") or {}, profile.get("credentials") or {},
                {"id": zone_id, "name": zone_name},
            )
            return _json({"status": "ok" if result.get("ok") else "error", "connection": profile.get("name"), **result})
        except Exception as exc:
            return _json({"status": "error", "message": str(exc)})

    async def host_upsert_dns_record(connection: str, zone_id: str, zone_name: str,
                                     name: str, value: str, record_type: str = "A",
                                     ttl: int = 300, proxied: bool = False) -> str:
        try:
            if str(record_type or "A").upper() == "A":
                ipaddress.IPv4Address(str(value or "").strip())
            profile, provider = await _tool_profile(agent_id, connection)
            events = []
            result = {"ok": False, "message": "Provider returned no result."}
            record = {"name": name, "type": str(record_type or "A").upper(), "value": value,
                      "ttl": max(60, min(int(ttl), 86400)), "proxied": bool(proxied)}
            async for event in provider.upsert_record(
                profile.get("provider_config") or {}, profile.get("credentials") or {},
                {"id": zone_id, "name": zone_name}, record,
            ):
                if event.get("phase") == "done":
                    result = event.get("result") or result
                else:
                    events.append({key: event.get(key) for key in ("phase", "message", "level")})
            return _json({"status": "ok" if result.get("ok") else "error", "connection": profile.get("name"), "events": events, **result})
        except Exception as exc:
            return _json({"status": "error", "message": str(exc)})

    TOOL_SCHEMAS.clear()
    TOOL_SCHEMAS.update(_SCHEMAS)
    return {
        "host_list_connections": host_list_connections,
        "host_list_zones": host_list_zones,
        "host_list_dns_records": host_list_dns_records,
        "host_upsert_dns_record": host_upsert_dns_record,
    }


_CONNECTION = {"type": "string", "description": "Connection id or name from host_list_connections."}
_ZONE_ID = {"type": "string", "description": "Provider-specific zone id from host_list_zones."}
_ZONE_NAME = {"type": "string", "description": "Canonical zone name from host_list_zones, such as example.com."}
_SCHEMAS = {
    "host_list_connections": {"type": "object", "properties": {}, "required": []},
    "host_list_zones": {"type": "object", "properties": {"connection": _CONNECTION}, "required": ["connection"]},
    "host_list_dns_records": {
        "type": "object",
        "properties": {"connection": _CONNECTION, "zone_id": _ZONE_ID, "zone_name": _ZONE_NAME},
        "required": ["connection", "zone_id", "zone_name"],
    },
    "host_upsert_dns_record": {
        "type": "object",
        "properties": {
            "connection": _CONNECTION,
            "zone_id": _ZONE_ID,
            "zone_name": _ZONE_NAME,
            "name": {"type": "string", "description": "Record name, FQDN, or @ for the zone apex."},
            "value": {"type": "string", "description": "Record value; for A records this must be an IPv4 address."},
            "record_type": {"type": "string", "default": "A", "description": "DNS record type supported by the selected provider."},
            "ttl": {"type": "integer", "minimum": 60, "maximum": 86400, "default": 300},
            "proxied": {"type": "boolean", "default": False, "description": "Cloudflare orange-cloud proxy; ignored by other providers."}
        },
        "required": ["connection", "zone_id", "zone_name", "name", "value"],
    },
}
