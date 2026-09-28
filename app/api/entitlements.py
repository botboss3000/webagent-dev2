"""Secret-free user-facing experience-tier capability and BYO model APIs."""

import re
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.auth.identity import request_user_id
from app.entitlements.service import resolve_capabilities

router = APIRouter(prefix="/api/v1/entitlements", tags=["entitlements"])


@router.get("/me")
async def my_capabilities(request: Request):
    uid = request_user_id(request)
    return await resolve_capabilities(uid or "__anonymous__")


class ByoModelsRequest(BaseModel):
    entries: list[dict[str, Any]] = Field(default_factory=list)
    default_entry_id: str = ""


class UserModelCatalogRequest(BaseModel):
    """Editable user catalog: BYO rows plus opaque entitled platform IDs."""
    entries: list[dict[str, Any]] = Field(default_factory=list)
    platform_entry_ids: list[str] = Field(default_factory=list)
    # MODEL-NAME-LEVELS: the caller's own display name per adopted platform
    # entry, layered on top of the catalog's model name.
    platform_aliases: dict[str, str] = Field(default_factory=dict)
    default_entry_id: str = ""


async def _registered_caller(request: Request) -> str:
    uid = request_user_id(request)
    if not uid or str(uid).startswith("anon_"):
        raise HTTPException(status_code=401, detail={"code": "authentication_required"})
    return uid


_SAVED_MODEL_CREDENTIAL_SERVICE = "llm_saved_credential"
_SUBSCRIPTION_CREDENTIAL_SERVICE = "openai_subscription_oauth"
_CREDENTIAL_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._()-]{0,63}$")
_PROVIDER_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class SavedModelCredentialRequest(BaseModel):
    label: str = Field(min_length=1, max_length=64)
    provider: str = Field(min_length=1, max_length=64)
    base_url: str = Field(default="", max_length=512)
    api_key: str = Field(min_length=1, max_length=4096)


def _credential_label(value: str) -> str:
    label = str(value or "").strip()
    if not _CREDENTIAL_LABEL_RE.fullmatch(label):
        raise HTTPException(status_code=422, detail={
            "code": "invalid_credential_label",
            "message": "Credential names may contain letters, numbers, spaces, periods, underscores, hyphens, and parentheses.",
        })
    return label


def _credential_provider(value: str) -> str:
    provider = str(value or "").strip().lower()
    if not _PROVIDER_RE.fullmatch(provider) or provider == "openai_subscription":
        raise HTTPException(status_code=422, detail={"code": "invalid_credential_provider"})
    return provider


def _api_credential_projection(row: dict[str, Any]) -> dict[str, Any]:
    config = row.get("config") if isinstance(row.get("config"), dict) else {}
    return {
        "label": str(row.get("label") or ""),
        "kind": "api_key",
        "provider": str(config.get("provider") or ""),
        "base_url": str(config.get("base_url") or ""),
        "auth_mode": "api_key",
        "credential_configured": bool(row.get("secret_ref")),
        "credential_suffix": str(config.get("credential_suffix") or ""),
        "saved_at": int(config.get("saved_at") or 0),
    }


def _subscription_credential_projection(row: dict[str, Any]) -> dict[str, Any]:
    config = row.get("config") if isinstance(row.get("config"), dict) else {}
    account = config.get("account") if isinstance(config.get("account"), dict) else {}
    return {
        "label": str(row.get("label") or ""),
        "kind": "subscription",
        "provider": "openai_subscription",
        "auth_mode": "chatgpt",
        "credential_configured": bool(row.get("secret_ref")) and not bool(config.get("pending")),
        "credential_email": str(account.get("email") or config.get("email") or ""),
        "credential_plan_type": str(account.get("plan_type") or config.get("plan_type") or ""),
    }


@router.get("/me/model-credentials")
async def list_my_model_credentials(request: Request):
    """Return redacted, user-owned reusable API and subscription credentials."""
    uid = await _registered_caller(request)
    from app import model_credentials

    api_rows = await model_credentials.list_owned(
        "user", owner=uid, service=_SAVED_MODEL_CREDENTIAL_SERVICE, user_id=uid,
    )
    subscription_rows = await model_credentials.list_owned(
        "user", owner=uid, service=_SUBSCRIPTION_CREDENTIAL_SERVICE, user_id=uid,
    )
    credentials = [
        _api_credential_projection(row) for row in api_rows if isinstance(row, dict)
    ] + [
        _subscription_credential_projection(row)
        for row in subscription_rows if isinstance(row, dict)
    ]
    credentials.sort(key=lambda item: (item["kind"], item["provider"], item["label"].lower()))
    return {"credentials": credentials}


@router.put("/me/model-credentials")
async def save_my_model_credential(body: SavedModelCredentialRequest, request: Request):
    """Save an API key under the authenticated user's vault owner."""
    uid = await _registered_caller(request)
    label = _credential_label(body.label)
    provider = _credential_provider(body.provider)
    api_key = str(body.api_key or "").strip()
    if not api_key:
        raise HTTPException(status_code=422, detail={"code": "missing_api_key"})
    from app import model_credentials

    saved = await model_credentials.set_owned(
        "user", owner=uid, service=_SAVED_MODEL_CREDENTIAL_SERVICE, label=label,
        user_id=uid, secret_ref=api_key,
        config={
            "provider": provider,
            "base_url": str(body.base_url or "").strip(),
            "credential_suffix": api_key[-4:] if len(api_key) >= 4 else "",
            "saved_at": int(time.time()),
        },
    )
    return {"credential": _api_credential_projection(saved)}


@router.get("/me/model-credentials/{label}")
async def load_my_model_credential(label: str, request: Request):
    """Load one API key for its verified owner; subscription tokens are never loadable."""
    uid = await _registered_caller(request)
    label = _credential_label(label)
    from app import model_credentials

    row = await model_credentials.get_owned(
        "user", owner=uid, service=_SAVED_MODEL_CREDENTIAL_SERVICE, label=label, user_id=uid,
    )
    if not row or not row.get("secret_ref"):
        raise HTTPException(status_code=404, detail={"code": "credential_not_found"})
    return {"credential": _api_credential_projection(row), "api_key": str(row["secret_ref"])}


@router.delete("/me/model-credentials/{label}")
async def delete_my_model_credential(label: str, request: Request):
    uid = await _registered_caller(request)
    label = _credential_label(label)
    from app import model_credentials

    if not await model_credentials.delete_owned(
        "user", owner=uid, service=_SAVED_MODEL_CREDENTIAL_SERVICE, label=label, user_id=uid,
    ):
        raise HTTPException(status_code=404, detail={"code": "credential_not_found"})
    return {"status": "deleted"}


# A user catalog is deliberately a projection, not a runtime/provider config.
# The app catalog owns purchasable entitlement rows; the caller owns BYO rows;
# agent rosters alone own role positions.  Keep this allow-list narrow so a
# future credential/config field cannot accidentally reach a browser catalog.
_CATALOG_ENTRY_FIELDS = frozenset({
    "entry_id", "display_name", "provider", "auth_mode", "base_url", "model",
    "text_capable", "image_capable", "image_out_capable", "voice_capable",
    "video_capable", "stateful_responses", "routing_mode",
    "credit_schedule", "credit_input_per_million", "credit_output_per_million",
})


_PLATFORM_CATALOG_FIELDS = frozenset({
    # MODEL-NAME-LEVELS: catalog_name is the catalog's own alias for the model;
    # display_name is the caller's display name layered on top of it.
    "entry_id", "display_name", "catalog_name", "provider", "model", "text_capable", "image_capable",
    "image_out_capable", "voice_capable", "video_capable",
    "credit_schedule", "credit_input_per_million", "credit_output_per_million",
})


def _resolved_platform_credit_rates(
    entry: dict[str, Any], platform_entries: list[dict[str, Any]],
) -> tuple[float | None, float | None, str]:
    """Return the platform row's explicit or existing runtime-equivalent rates.

    The App Catalog editor presents Flash/Pro reference defaults without writing
    them until an administrator changes the control.  The runtime already uses
    the same fallback while charging.  Projecting it here makes the catalog
    search truthful without silently mutating roster or billing data.
    """
    explicit_input = entry.get("credit_input_per_million")
    explicit_output = entry.get("credit_output_per_million")
    schedule = str(entry.get("credit_schedule") or (
        "pro" if entry.get("high_effort_capable") else "flash"
    )).lower()
    if schedule not in {"flash", "pro", "custom"}:
        schedule = "pro" if entry.get("high_effort_capable") else "flash"

    # Preserve every explicitly configured side, including a configured zero.
    if explicit_input is not None and explicit_output is not None:
        return float(explicit_input), float(explicit_output), schedule

    reference = next((candidate for candidate in platform_entries if
                      "deepseek" in " ".join(str(candidate.get(key) or "")
                                              for key in ("display_name", "label", "model")).lower()
                      and schedule in " ".join(str(candidate.get(key) or "")
                                                   for key in ("display_name", "label", "model")).lower()), None)
    source = reference or entry
    try:
        from app import model_catalog
        model = str(source.get("model") or "")
        provider = str(source.get("provider") or "")
        input_cost, input_source = model_catalog.cost_for(model, 1_000_000, 0, provider)
        output_cost, output_source = model_catalog.cost_for(model, 0, 1_000_000, provider)
        if input_source != "unknown" or output_source != "unknown":
            calculated_input = round(max(0.0, float(input_cost)) * 1000, 6)
            calculated_output = round(max(0.0, float(output_cost)) * 1000, 6)
            return (
                float(explicit_input) if explicit_input is not None else calculated_input,
                float(explicit_output) if explicit_output is not None else calculated_output,
                schedule,
            )
    except (TypeError, ValueError, AttributeError):
        pass
    return (
        float(explicit_input) if explicit_input is not None else None,
        float(explicit_output) if explicit_output is not None else None,
        schedule,
    )


def _user_catalog_entry(
    entry: dict[str, Any], origin: str, *,
    platform_entries: list[dict[str, Any]] | None = None, alias: str = "",
) -> dict[str, Any]:
    """Return a role-free, credential-free catalog row.

    Platform rows are intentionally opaque: provider, model and base URL never
    cross this API boundary.  The opaque entry_id is resolved server-side when
    the user adds it to an agent roster.
    """
    fields = _PLATFORM_CATALOG_FIELDS if origin == "platform" else _CATALOG_ENTRY_FIELDS
    safe = {key: value for key, value in dict(entry or {}).items() if key in fields}
    if not safe.get("entry_id"):
        return {}
    if origin == "platform":
        provider = str(safe.get("provider") or "").strip()
        model = str(safe.get("model") or "").strip()
        # An explicit alias remains preferred, but a blank alias must still
        # identify the model by its real provider/model name in the user picker.
        safe["actual_model_name"] = " / ".join(part for part in (provider, model) if part)
        # MODEL-NAME-LEVELS: the catalog's own alias is the model NAME; the
        # caller's display name is a second level layered on top of it. Blank
        # means the caller has set none, so the catalog alias is the name shown.
        catalog_name = str(safe.get("display_name") or "").strip()
        safe["catalog_name"] = catalog_name
        safe["display_name"] = str(alias or "").strip() or catalog_name
        rate_input, rate_output, schedule = _resolved_platform_credit_rates(
            dict(entry or {}), platform_entries or [],
        )
        safe["credit_schedule"] = schedule
        if rate_input is not None:
            safe["credit_input_per_million"] = rate_input
        if rate_output is not None:
            safe["credit_output_per_million"] = rate_output
    elif not safe.get("model"):
        return {}
    safe["catalog_origin"] = origin
    return safe


@router.get("/me/model-catalog")
async def get_my_model_catalog(request: Request):
    """Return the caller's entitled platform and owned-BYO model catalog.

    The response is read-only metadata: no API keys, credential hints, or
    agent-role fields.  Stable source entry IDs are retained so an agent may
    explicitly adopt one of these rows into its own independent roster.
    """
    uid = await _registered_caller(request)
    from app.admin.settings import _load_own_llm_config
    from app.db import get_db
    from app.entitlements.rosters import resolve_platform_roster_config

    db = get_db()
    platform = await resolve_platform_roster_config(uid, db=db) or {}
    owned = await _load_own_llm_config(uid) or {}
    platform_entries = [
        row for row in (platform.get("multi_providers") or []) if isinstance(row, dict)
    ]
    platform_by_id = {
        str(row.get("entry_id") or ""): row
        for row in platform_entries
    }
    selected_platform_ids = {
        str(value) for value in (owned.get("user_catalog_platform_entry_ids") or []) if str(value)
    }
    # MODEL-NAME-LEVELS: the caller's own display name for an adopted platform
    # entry, layered on top of that entry's catalog model name.
    platform_aliases = {
        str(key): str(value).strip()
        for key, value in (owned.get("user_catalog_platform_aliases") or {}).items()
        if str(key) and str(value).strip()
    }
    # Entry ID is the canonical model identity. A platform selection wins over
    # a stale/provider-backed copy with the same ID; the latter was created by
    # the former inheritance migration and is not a distinct BYO model.
    entries_by_id: dict[str, dict[str, Any]] = {}

    def add(raw: dict[str, Any], origin: str):
        entry_id_hint = str(raw.get("entry_id") or "")
        item = _user_catalog_entry(
            raw, origin, platform_entries=platform_entries,
            alias=platform_aliases.get(entry_id_hint, "") if origin == "platform" else "",
        )
        entry_id = str(item.get("entry_id") or "")
        if not item or not entry_id:
            return
        existing = entries_by_id.get(entry_id)
        if existing is None or origin == "platform":
            entries_by_id[entry_id] = item

    for entry_id in selected_platform_ids:
        if entry_id in platform_by_id:
            add(platform_by_id[entry_id], "platform")
    for raw in owned.get("multi_providers") or []:
        if isinstance(raw, dict):
            # A selected app entry must never also be returned as a BYO row.
            if str(raw.get("entry_id") or "") in selected_platform_ids:
                continue
            add(raw, "byo")
    # Existing agent choices are canonical user availability too. Their source
    # is intentionally not returned; an agent-selected model is indistinguishable
    # from one selected directly in Available models. Include admin-marked agents
    # a caller can access and hydrate the exact roster used by the chat footer.
    from app.admin.settings import _load_agent_llm_config
    for agent in await db.list_agents_for_user(uid, include_admin=True, include_templates=False):
        metadata = agent.get("metadata") or {}
        if isinstance(metadata, str):
            try:
                import json
                metadata = json.loads(metadata)
            except Exception:
                metadata = {}
        raw_llm = metadata.get("llm_config") if isinstance(metadata, dict) else {}
        llm = await _load_agent_llm_config(str(agent.get("id") or ""), raw_llm or {})
        for raw in (llm.get("multi_providers") or []) if isinstance(llm, dict) else []:
            if not isinstance(raw, dict):
                continue
            entry_id = str(raw.get("entry_id") or "")
            # An agent row is platform only after that opaque app entry has been
            # explicitly adopted into the user's catalog. Otherwise it remains a
            # BYO row, even when a legacy ID happens to collide with a platform ID.
            if entry_id in selected_platform_ids and entry_id in platform_by_id:
                add(platform_by_id[entry_id], "platform")
            elif entry_id not in entries_by_id:
                # An agent roster is an availability projection, not account-owned
                # BYO configuration. Keep it visible, but let the UI avoid
                # replaying it through the account catalog write lane.
                add(raw, "byo")
                if entry_id in entries_by_id:
                    entries_by_id[entry_id]["catalog_entry_source"] = "agent"
    available_platform_models = [
        _user_catalog_entry(
            row, "platform", platform_entries=platform_entries,
            alias=platform_aliases.get(str(row.get("entry_id") or ""), ""),
        )
        for row in platform_by_id.values()
    ]
    entries = list(entries_by_id.values())
    default_entry_id = str(owned.get("user_catalog_default_entry_id") or "").strip()
    available_ids = {str(row.get("entry_id") or "") for row in entries}
    if default_entry_id not in available_ids:
        # Existing BYO catalog configurations predate an explicit catalog default.
        # Preserve their enabled text model as the harmless compatibility fallback.
        default_entry_id = next((
            str(row.get("entry_id") or "") for row in entries
            if row.get("catalog_origin") == "byo" and row.get("text_capable") is not False
        ), "")
    return {
        "entries": entries,
        "available_platform_models": available_platform_models,
        "default_entry_id": default_entry_id,
    }


@router.put("/me/model-catalog")
async def set_my_model_catalog(body: UserModelCatalogRequest, request: Request):
    """Persist the caller's BYO rows and opaque entitled platform selections."""
    uid = await _registered_caller(request)
    from app.db import get_db
    from app.admin.settings import _load_own_llm_config, _persist_llm_config
    from app.entitlements.rosters import resolve_platform_roster_config

    platform = await resolve_platform_roster_config(uid, db=get_db()) or {}
    allowed = {
        str(row.get("entry_id") or "")
        for row in (platform.get("multi_providers") or []) if isinstance(row, dict)
    }
    selected = []
    for entry_id in body.platform_entry_ids:
        entry_id = str(entry_id or "").strip()
        if not entry_id or entry_id not in allowed:
            raise HTTPException(status_code=422, detail={"code": "unknown_catalog_entry"})
        if entry_id not in selected:
            selected.append(entry_id)
    # MODEL-NAME-LEVELS: the caller's own display name per adopted platform
    # entry, layered on top of the catalog's model name. Only entitled entries
    # may carry one; a blank value clears it.
    aliases: dict[str, str] = {}
    for raw_id, raw_alias in (body.platform_aliases or {}).items():
        entry_id = str(raw_id or "").strip()
        if not entry_id or entry_id not in allowed:
            raise HTTPException(status_code=422, detail={"code": "unknown_catalog_entry"})
        alias = str(raw_alias or "").strip()
        if len(alias) > 120:
            raise HTTPException(status_code=422, detail={"code": "display_name_too_long"})
        if alias:
            aliases[entry_id] = alias
    default_entry_id = str(body.default_entry_id or "").strip()
    # A catalog default may point at either a selected platform row or a BYO row.
    # Platform rows are opaque to the BYO write lane, so validate and retain that
    # choice here while continuing to use the established credential-safe path.
    if default_entry_id and default_entry_id not in selected:
        submitted_byo_ids = {
            str(row.get("entry_id") or "") for row in body.entries if isinstance(row, dict)
        }
        if default_entry_id not in submitted_byo_ids:
            raise HTTPException(status_code=422, detail={
                "code": "invalid_default_entry", "entry_id": default_entry_id,
            })
    await set_my_byo_models(ByoModelsRequest(
        entries=body.entries,
        default_entry_id="" if default_entry_id in selected else default_entry_id,
    ), request)
    current = await _load_own_llm_config(uid) or {}
    current["user_catalog_platform_entry_ids"] = selected
    current["user_catalog_platform_aliases"] = aliases
    if default_entry_id:
        current["user_catalog_default_entry_id"] = default_entry_id
    else:
        current.pop("user_catalog_default_entry_id", None)
    await _persist_llm_config(uid, current)
    return await get_my_model_catalog(request)


@router.get("/me/byo-models")
async def get_my_byo_models(request: Request):
    uid = await _registered_caller(request)
    from app.admin.settings import _load_own_llm_config, _public_provider_config

    config = await _load_own_llm_config(uid) or {}
    public = _public_provider_config(config)
    entries = public.get("multi_providers") or []
    standard = next(
        (entry for entry in entries
         if entry.get("enabled") is not False and entry.get("text_capable") is not False),
        {},
    )
    return {
        "entries": entries,
        "default_entry_id": standard.get("entry_id") or "",
        "credential_state": "configured" if any(
            entry.get("credential_configured") for entry in public.get("multi_providers") or []
        ) else "missing",
    }


@router.put("/me/byo-models")
async def set_my_byo_models(body: ByoModelsRequest, request: Request):
    uid = await _registered_caller(request)
    from app.db import get_db
    from app.admin.settings import _load_own_llm_config, _persist_llm_config
    from app.entitlements.rosters import (
        RosterError, assign_entry_ids, validate_live_voice_assignments,
        validate_roster_entries,
    )

    db = get_db()
    capabilities = await resolve_capabilities(uid, db=db, use_cache=False)
    model_policy = capabilities.get("models") or {}
    if not model_policy.get("allow_byo"):
        raise HTTPException(status_code=403, detail={"code": "byo_not_allowed"})
    maximum = model_policy.get("max_byo_entries")
    if maximum is not None and len(body.entries) > int(maximum):
        raise HTTPException(status_code=409, detail={
            "code": "quota_exceeded", "limit": "max_byo_entries", "maximum": int(maximum),
        })
    # Roster normalization deliberately removes secrets for platform metadata.
    # Preserve each caller-owned credential separately and splice it back only
    # into the vault-bound config after stable IDs have been assigned.
    try:
        validated_entries = validate_roster_entries(
            body.entries, allow_credentials=True, require_provider_model=True,
            untrusted_urls=True,
        )
    except RosterError as exc:
        raise HTTPException(status_code=422, detail={
            "code": "invalid_model_entry", "message": str(exc),
        }) from exc
    incoming_credentials = [
        str(entry.get("api_key") or "") if isinstance(entry, dict) else ""
        for entry in validated_entries
    ]
    existing = await _load_own_llm_config(uid) or {}
    from app import openai_subscription
    allowed_existing = await openai_subscription.existing_entries_with_reservations(
        validated_entries, existing.get("multi_providers") or [],
        scope="user", user_id=uid,
    )
    try:
        entries = assign_entry_ids(
            validated_entries,
            existing_entries=allowed_existing,
        )
    except RosterError as exc:
        raise HTTPException(status_code=422, detail={
            "code": "invalid_model_entry", "message": str(exc),
        }) from exc
    try:
        validate_live_voice_assignments(entries)
    except RosterError as exc:
        raise HTTPException(status_code=422, detail={
            "code": "invalid_model_entry", "message": str(exc),
        }) from exc
    existing_by_id = {
        str(entry.get("entry_id") or ""): entry
        for entry in existing.get("multi_providers") or [] if isinstance(entry, dict)
    }
    features = capabilities.get("features") or {}
    for index, entry in enumerate(entries):
        if not entry.get("provider") or not entry.get("model"):
            raise HTTPException(status_code=422, detail="Each BYO model needs provider and model.")
        if entry.get("image_out_capable") and not features.get("image_generation"):
            raise HTTPException(status_code=403, detail={"code": "upgrade_required", "feature": "image_generation"})
        if entry.get("voice_capable") and not features.get("voice_llm"):
            raise HTTPException(status_code=403, detail={"code": "upgrade_required", "feature": "voice_llm"})
        subscription = (
            entry.get("provider") == "openai_subscription"
            or entry.get("auth_mode") == "chatgpt"
        )
        if subscription:
            entry["api_key"] = ""
            entry["base_url"] = "codex://chatgpt"
            entry["auth_mode"] = "chatgpt"
            entry["credential_scope"] = "user"
            from app import openai_subscription
            try:
                state = await openai_subscription.status(
                    scope="user", user_id=uid, entry_id=str(entry.get("entry_id") or ""),
                )
            except Exception:
                state = {}
            if not state.get("chatgpt_authorized"):
                for field in (
                    "enabled", "high_effort_capable", "use_for_image", "use_for_image_out",
                    "use_for_system", "use_for_voice",
                ):
                    entry[field] = False
        elif incoming_credentials[index]:
            entry["api_key"] = incoming_credentials[index]
        elif entry.get("entry_id") in existing_by_id:
            entry["api_key"] = existing_by_id[entry["entry_id"]].get("api_key", "")
    if any(not entry.get("api_key") for entry in entries
           if entry.get("provider") != "openai_subscription" and entry.get("auth_mode") != "chatgpt"):
        raise HTTPException(status_code=422, detail="Each API-key BYO model needs a credential.")
    default = next((entry for entry in entries if entry["entry_id"] == body.default_entry_id), None)
    if body.default_entry_id and default is None:
        raise HTTPException(status_code=422, detail={
            "code": "invalid_default_entry", "entry_id": body.default_entry_id,
        })
    default = default or (entries[0] if entries else {})
    if default:
        # Keep the compatibility request field, but encode the choice solely as
        # the roster's Standard role instead of saving a separate default ID.
        for entry in entries:
            entry["enabled"] = entry["entry_id"] == default["entry_id"]
        entries.sort(key=lambda entry: entry["entry_id"] != default["entry_id"])
    config = {
        **existing,
        "multi_providers": entries,
        "providers": {},
    }
    incoming_ids = {str(entry.get("entry_id") or "") for entry in entries}
    removed_subscription_ids = [
        str(entry.get("entry_id") or "")
        for entry in existing.get("multi_providers") or [] if isinstance(entry, dict)
        and str(entry.get("entry_id") or "") not in incoming_ids
        and (entry.get("provider") == "openai_subscription" or entry.get("auth_mode") == "chatgpt")
    ]
    await _persist_llm_config(uid, config)
    if removed_subscription_ids:
        from app import openai_subscription
        for entry_id in removed_subscription_ids:
            await openai_subscription.sign_out(
                scope="user", user_id=uid, entry_id=entry_id,
            )
    from app.admin.settings import _public_provider_config
    public = _public_provider_config({"multi_providers": entries})
    return {
        "status": "ok", "count": len(entries),
        "default_entry_id": default.get("entry_id", ""),
        "entries": public.get("multi_providers", []),
    }


@router.delete("/me/byo-models")
async def delete_my_byo_models(request: Request):
    uid = await _registered_caller(request)
    from app.admin.settings import _persist_llm_config, DEFAULT_PROVIDER

    await _persist_llm_config(uid, dict(DEFAULT_PROVIDER))
    return {"status": "deleted"}
