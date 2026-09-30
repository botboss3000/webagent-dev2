"""Tenant-scoped, row-owned ChatGPT subscription authorization."""
import json
import uuid

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app import openai_subscription
from app.auth.identity import request_user_id

router = APIRouter(prefix="/api/v1/openai-subscription")


async def _authorized_scope(request: Request, scope: str, agent_id: str) -> dict[str, str]:
    scope = str(scope or "platform").strip().lower()
    uid = request_user_id(request)
    if scope == "platform":
        from app.api.claude_auth import _require_admin
        await _require_admin(request)
        return {"scope": scope}
    if not uid or uid.startswith("anon_"):
        raise HTTPException(status_code=401, detail="Sign in to connect a model account.")
    if scope == "agent":
        if not agent_id:
            raise HTTPException(status_code=422, detail="agent_id is required.")
        from app.api.agents import _is_agent_admin
        from app.db import get_db
        if not await _is_agent_admin(get_db(), agent_id, uid):
            raise HTTPException(status_code=403, detail="Only agent admins can connect this account.")
        return {"scope": scope, "agent_id": agent_id, "_caller_id": uid}
    if scope == "user":
        from app.entitlements.service import resolve_capabilities
        capabilities = await resolve_capabilities(uid)
        if not (capabilities.get("models") or {}).get("allow_byo"):
            raise HTTPException(status_code=403, detail={"code": "byo_not_allowed"})
        return {"scope": scope, "user_id": uid}
    raise HTTPException(status_code=422, detail="Invalid credential scope.")


async def _scope_roster(target: dict[str, str]) -> list[dict]:
    scope = target["scope"]
    if scope in {"platform", "user"}:
        from app.admin.settings import PLATFORM_LLM_OWNER, _load_own_llm_config
        owner = PLATFORM_LLM_OWNER if scope == "platform" else target["user_id"]
        config = await _load_own_llm_config(owner) or {}
        return [row for row in config.get("multi_providers") or [] if isinstance(row, dict)]
    from app.db import get_db
    agent = await get_db().get_agent_by_id(target["agent_id"])
    if not agent:
        return []
    metadata = agent.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata or "{}")
        except (TypeError, ValueError):
            metadata = {}
    llm = metadata.get("llm_config") if isinstance(metadata, dict) else {}
    return [row for row in (llm or {}).get("multi_providers") or [] if isinstance(row, dict)]


async def _authorize_entry(request: Request, scope: str, agent_id: str,
                           entry_id: str, *, allow_reserved: bool = False) -> dict[str, str]:
    target = await _authorized_scope(request, scope, agent_id)
    entry_id = str(entry_id or "").strip()
    if not entry_id:
        raise HTTPException(status_code=422, detail="entry_id is required.")
    roster = await _scope_roster(target)
    row = next((item for item in roster if str(item.get("entry_id") or "") == entry_id), None)
    subscription_row = bool(row and (
        row.get("provider") == "openai_subscription"
        or row.get("auth_mode") == "chatgpt"
    ))
    reserved = False
    if not subscription_row and allow_reserved:
        reserved = await openai_subscription.entry_is_reserved(
            **_transport_target({**target, "entry_id": entry_id})
        )
    if not subscription_row and not reserved:
        raise HTTPException(status_code=404, detail="Subscription model row not found.")
    target["entry_id"] = entry_id
    return target



def _transport_target(target: dict[str, str]) -> dict[str, str]:
    return {key: value for key, value in target.items() if not key.startswith("_")}


async def _disable_entry_roles(target: dict[str, str], entry_id: str) -> None:
    role_fields = (
        "enabled", "high_effort_capable", "use_for_image", "use_for_image_out",
        "use_for_system", "use_for_voice",
    )
    scope = target["scope"]
    if scope in {"platform", "user"}:
        from app.admin.settings import (
            PLATFORM_LLM_OWNER, _load_own_llm_config, _persist_llm_config,
        )
        owner = PLATFORM_LLM_OWNER if scope == "platform" else target["user_id"]
        config = await _load_own_llm_config(owner) or {}
        for row in config.get("multi_providers") or []:
            if isinstance(row, dict) and str(row.get("entry_id") or "") == entry_id:
                for field in role_fields:
                    row[field] = False
        await _persist_llm_config(owner, config)
        return

    from app.db import get_db
    db = get_db()
    agent = await db.get_agent_by_id(target["agent_id"])
    if not agent:
        return
    metadata = agent.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata or "{}")
        except (TypeError, ValueError):
            metadata = {}
    metadata = dict(metadata) if isinstance(metadata, dict) else {}
    llm = dict(metadata.get("llm_config") or {})
    for row in llm.get("multi_providers") or []:
        if isinstance(row, dict) and str(row.get("entry_id") or "") == entry_id:
            for field in role_fields:
                row[field] = False
    metadata["llm_config"] = llm
    updated = await db.update_agent_fields(
        target["agent_id"], target.get("_caller_id", ""), {"metadata": metadata},
    )
    if updated is None:
        raise RuntimeError("Could not disable disconnected model roles")



@router.get("/auth/status")
async def auth_status(request: Request, scope: str = "platform", agent_id: str = "",
                      entry_id: str = ""):
    target = await _authorize_entry(
        request, scope, agent_id, entry_id, allow_reserved=True,
    )
    return await openai_subscription.status(**_transport_target(target))


@router.post("/login/start")
async def login_start(request: Request, scope: str = "platform", agent_id: str = "",
                      entry_id: str = ""):
    created = not str(entry_id or "").strip()
    if created:
        target = await _authorized_scope(request, scope, agent_id)
        target["entry_id"] = str(uuid.uuid4())
        await openai_subscription.reserve_entry(**_transport_target(target))
    else:
        target = await _authorize_entry(
            request, scope, agent_id, entry_id, allow_reserved=True,
        )
    try:
        result = await openai_subscription.start_device_login(**_transport_target(target))
    except Exception as exc:
        if created:
            await openai_subscription.sign_out(**_transport_target(target))
        raise HTTPException(
            status_code=500, detail=f"Could not start ChatGPT sign-in: {exc}"
        ) from exc
    return {
        "ok": True,
        "entry_id": target["entry_id"],
        **result,
        "message": "Open the verification page and enter the one-time code.",
    }


@router.post("/login/reset")
async def login_reset(request: Request, scope: str = "platform", agent_id: str = "",
                      entry_id: str = ""):
    target = await _authorize_entry(
        request, scope, agent_id, entry_id, allow_reserved=True,
    )
    try:
        reservation_removed = await openai_subscription.reset_device_login(
            **_transport_target(target)
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"Could not reset ChatGPT sign-in: {exc}"
        ) from exc
    return {"ok": True, "reservation_removed": reservation_removed}


@router.post("/auth/signout")
async def auth_signout(request: Request, scope: str = "platform", agent_id: str = "",
                       entry_id: str = ""):
    target = await _authorize_entry(
        request, scope, agent_id, entry_id, allow_reserved=True,
    )
    try:
        await openai_subscription.sign_out(**_transport_target(target))
        await _disable_entry_roles(target, entry_id)
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"Could not sign out of ChatGPT: {exc}"
        ) from exc
    return {"ok": True}


@router.get("/models")
async def subscription_models(request: Request, scope: str = "platform", agent_id: str = ""):
    await _authorized_scope(request, scope, agent_id)
    return {"models": openai_subscription.models()}


_SAVED_SUBSCRIPTION_SERVICE = "saved_model_subscription"


class SubscriptionCredentialRequest(BaseModel):
    label: str = Field(min_length=1, max_length=64)


@router.get("/credentials")
async def saved_subscription_credentials(request: Request, scope: str = "platform", agent_id: str = ""):
    from app.api.entitlements import _saved_credential_target
    vault = await _saved_credential_target(request, scope, agent_id)
    from app import model_credentials
    from app.api.entitlements import _subscription_credential_projection
    rows = await model_credentials.list_owned(
        vault["scope"], owner=vault["owner"], service=_SAVED_SUBSCRIPTION_SERVICE,
        user_id=vault.get("user_id", ""), agent_id=vault.get("agent_id", ""),
    )
    return {"credentials": [_subscription_credential_projection(row) for row in rows
                            if row.get("secret_ref") and not (row.get("config") or {}).get("pending")]}


@router.post("/credentials/save")
async def save_subscription_credential(body: SubscriptionCredentialRequest, request: Request,
                                       scope: str = "platform", agent_id: str = "", entry_id: str = ""):
    target = await _authorize_entry(request, scope, agent_id, entry_id, allow_reserved=True)
    from app.api.entitlements import _saved_credential_target
    vault = await _saved_credential_target(request, scope, agent_id)
    from app import model_credentials
    from app.api.entitlements import _credential_label, _subscription_credential_projection
    label = _credential_label(body.label)
    transport = _transport_target(target)
    owner = openai_subscription.credential_owner(
        transport["scope"], user_id=transport.get("user_id", ""), agent_id=transport.get("agent_id", ""),
    )
    try:
        await openai_subscription.transport_auth(**transport)
    except Exception:
        raise HTTPException(status_code=409, detail="Validate the subscription connection before saving.")
    tokens = await openai_subscription._load_tokens(
        owner, entry_id, scope=transport["scope"],
        user_id=transport.get("user_id", ""), agent_id=transport.get("agent_id", ""),
    )
    if not tokens.get("access_token") or not tokens.get("refresh_token"):
        raise HTTPException(status_code=409, detail="Connect the subscription before saving its credential.")
    source = await model_credentials.get_owned(
        transport["scope"], owner=owner, service="openai_subscription_oauth", label=entry_id,
        user_id=transport.get("user_id", ""), agent_id=transport.get("agent_id", ""),
    ) or {}
    source_config = source.get("config") or {}
    identity = (source_config.get("saved_credential_id")
                if source_config.get("saved_credential_label") == label else None) or str(uuid.uuid4())
    saved = await model_credentials.set_owned(
        vault["scope"], owner=vault["owner"], service=_SAVED_SUBSCRIPTION_SERVICE, label=label,
        user_id=vault.get("user_id", ""), agent_id=vault.get("agent_id", ""),
        config={**openai_subscription.token_metadata(tokens), "saved_credential_id": identity},
        secret_ref=json.dumps(tokens, separators=(",", ":")),
    )
    await model_credentials.set_owned(
        vault["scope"], owner=vault["owner"], service="saved_model_subscription_tokens", label=identity,
        user_id=vault.get("user_id", ""), agent_id=vault.get("agent_id", ""),
        config=openai_subscription.token_metadata(tokens),
        secret_ref=json.dumps(tokens, separators=(",", ":")),
    )
    await model_credentials.set_owned(
        transport["scope"], owner=owner, service="openai_subscription_oauth", label=entry_id,
        user_id=transport.get("user_id", ""), agent_id=transport.get("agent_id", ""),
        config={**openai_subscription.token_metadata(tokens),
                "saved_credential_label": label, "saved_credential_id": identity},
        secret_ref=json.dumps(tokens, separators=(",", ":")),
    )
    return {"credential": _subscription_credential_projection(saved)}


@router.post("/credentials/load")
async def load_subscription_credential(body: SubscriptionCredentialRequest, request: Request,
                                       scope: str = "platform", agent_id: str = ""):
    # Always issue a new row identity. Loading in the add-model composer must
    # never replace authorization on an existing saved model.
    target = await _authorized_scope(request, scope, agent_id)
    from app.api.entitlements import _saved_credential_target
    vault = await _saved_credential_target(request, scope, agent_id)
    from app import model_credentials
    from app.api.entitlements import _credential_label, _subscription_credential_projection
    saved = await model_credentials.get_owned(
        vault["scope"], owner=vault["owner"], service=_SAVED_SUBSCRIPTION_SERVICE,
        label=_credential_label(body.label),
        user_id=vault.get("user_id", ""), agent_id=vault.get("agent_id", ""),
    )
    if not saved or not saved.get("secret_ref"):
        raise HTTPException(status_code=404, detail="Saved subscription credential not found.")
    try:
        tokens = json.loads(saved["secret_ref"])
    except (TypeError, ValueError):
        tokens = {}
    if not isinstance(tokens, dict) or not tokens.get("access_token") or not tokens.get("refresh_token"):
        raise HTTPException(status_code=409, detail="Saved subscription must be connected again.")
    target["entry_id"] = str(uuid.uuid4())
    transport = _transport_target(target)
    owner = openai_subscription.credential_owner(
        transport["scope"], user_id=transport.get("user_id", ""), agent_id=transport.get("agent_id", ""),
    )
    await model_credentials.set_owned(
        transport["scope"], owner=owner, service="openai_subscription_oauth", label=target["entry_id"],
        user_id=transport.get("user_id", ""), agent_id=transport.get("agent_id", ""),
        config={**openai_subscription.token_metadata(tokens),
                "saved_credential_label": saved["label"],
                "saved_credential_id": (saved.get("config") or {}).get("saved_credential_id")},
        secret_ref=json.dumps(tokens, separators=(",", ":")),
    )
    try:
        await openai_subscription.transport_auth(**transport)
    except Exception:
        await openai_subscription.sign_out(**transport)
        raise HTTPException(status_code=409, detail="Saved subscription must be validated again.")
    return {"entry_id": target["entry_id"], "credential": _subscription_credential_projection(saved)}
