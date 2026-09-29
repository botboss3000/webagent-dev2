"""Admin-only, secret-free management API for tiers and model rosters."""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.auth.identity import resolve_admin_uid
from app.db import get_app_db
from app.entitlements.policy import (
    KNOWN_ABILITY_GROUPS, KNOWN_FEATURES, LIMIT_SPECS, POLICY_SCHEMA_VERSION,
    REASONING_LEVELS, PolicyError, installed_page_ids, normalize_policy,
)
from app.entitlements.rosters import (
    RosterError,
    assign_entry_ids,
    delete_roster_entry_credential,
    load_platform_roster_config,
    roster_credential_states,
    set_roster_entry_credential,
    validate_roster_entries,
)
from app.entitlements.service import invalidate_capabilities

router = APIRouter(prefix="/admin/entitlements", tags=["admin-entitlements"])
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:[a-z0-9-]*[a-z0-9])?$")
_SECRET_FIELDS = frozenset({
    "api_key", "apikey", "access_token", "refresh_token", "token", "secret",
    "client_secret", "password", "credential", "credentials",
})


class AdminRequest(BaseModel):
    requesting_user_id: str


class RosterCreateRequest(AdminRequest):
    id: Optional[str] = None
    slug: str
    name: str
    description: str = ""
    entries: list[dict[str, Any]] = Field(default_factory=list)
    default_entry_id: Optional[str] = None


class RosterUpdateRequest(AdminRequest):
    slug: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None
    entries: Optional[list[dict[str, Any]]] = None
    default_entry_id: Optional[str] = None


class RosterActionRequest(AdminRequest):
    expected_revision: Optional[int] = Field(default=None, ge=1)
    reason: str = Field(default="", max_length=1000)


class RosterRollbackRequest(AdminRequest):
    revision: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=1000)


class RosterCredentialRequest(AdminRequest):
    credential: str = Field(min_length=1, max_length=65536)
    reason: str = Field(default="", max_length=1000)


class TierCreateRequest(AdminRequest):
    id: Optional[str] = None
    slug: str
    name: str
    description: str = ""
    policy: dict[str, Any]
    roster_id: Optional[str] = None
    is_system: bool = False
    is_locked: bool = False


class TierUpdateRequest(AdminRequest):
    slug: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None
    policy: Optional[dict[str, Any]] = None
    roster_id: Optional[str] = None
    is_locked: Optional[bool] = None
    expected_revision: Optional[int] = Field(default=None, ge=1)


class TierActionRequest(AdminRequest):
    expected_revision: Optional[int] = Field(default=None, ge=1)
    reason: str = Field(default="", max_length=1000)


class TierRollbackRequest(AdminRequest):
    revision: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=1000)


class TierCreditResetRequest(AdminRequest):
    scope_kind: Literal["account", "network"]
    principal_key: str = Field(min_length=1, max_length=512)
    tier_id: Optional[str] = None
    reason: str = Field(min_length=1, max_length=1000)


class AssignmentCreateRequest(AdminRequest):
    tier_id: str
    reason: str = Field(min_length=1, max_length=1000)
    starts_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    source: Literal["manual", "billing", "import", "default", "system"] = "manual"


async def _admin_actor(claimed_user_id: str) -> str:
    actor = await resolve_admin_uid(claimed_user_id)
    if not actor:
        raise HTTPException(status_code=403, detail="Admin access required.")
    return actor


def _parse_json(value: Any, fallback: Any) -> Any:
    if not isinstance(value, str):
        return value if value is not None else fallback
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def _contains_secret(value: Any) -> bool:
    if isinstance(value, dict):
        return any(str(key).lower() in _SECRET_FIELDS or _contains_secret(item)
                   for key, item in value.items())
    return isinstance(value, list) and any(_contains_secret(item) for item in value)


def _remove_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _remove_secrets(item) for key, item in value.items()
                if str(key).lower() not in _SECRET_FIELDS}
    if isinstance(value, list):
        return [_remove_secrets(item) for item in value]
    return value


def _safe_roster(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    entries = _parse_json(result.pop("entries_json", result.get("entries", [])), [])
    result.pop("entries", None)
    result["entries"] = _remove_secrets(entries if isinstance(entries, list) else [])
    draft_revision = int(result.get("revision") or 1)
    published_revision = result.get("published_revision")
    published_revision = int(published_revision) if published_revision is not None else None
    result["draft_revision"] = draft_revision
    result["published_revision"] = published_revision
    result["has_draft"] = published_revision != draft_revision
    return result


async def _safe_roster_with_credentials(db, row: dict[str, Any]) -> dict[str, Any]:
    safe = _safe_roster(row)
    states = await roster_credential_states(str(row.get("id") or ""), db=db)
    state_map: dict[str, str] = {}
    for entry in safe["entries"]:
        entry_id = str(entry.get("entry_id") or "")
        configured = bool(states.get(entry_id))
        entry["credential_configured"] = configured
        state_map[entry_id] = "configured" if configured else "missing"
    safe["credential_state_by_entry"] = state_map
    safe["credential_configured"] = any(value == "configured" for value in state_map.values())
    return safe


def _safe_revision(row: dict[str, Any]) -> dict[str, Any]:
    result = {key: row.get(key) for key in (
        "roster_id", "revision", "action", "created_by", "created_at",
    )}
    payload = _parse_json(row.get("payload_json"), {})
    if isinstance(payload, dict):
        result["payload"] = {**_roster_shape(payload), "source": payload.get("source")}
    else:
        result["payload"] = {}
    return result


def _safe_tier(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    policy = _parse_json(result.pop("policy_json", result.get("policy", {})), {})
    result.pop("policy", None)
    result["policy"] = policy if isinstance(policy, dict) else {}
    result["is_system"] = bool(result.get("is_system"))
    result["is_locked"] = bool(result.get("is_locked"))
    draft_revision = int(result.get("revision") or 1)
    published_revision = result.get("published_revision")
    published_revision = int(published_revision) if published_revision is not None else None
    result["draft_revision"] = draft_revision
    result["published_revision"] = published_revision
    result["has_draft"] = published_revision != draft_revision
    return result


def _safe_tier_revision(row: dict[str, Any]) -> dict[str, Any]:
    result = {key: row.get(key) for key in (
        "tier_id", "revision", "action", "created_by", "created_at",
    )}
    payload = _parse_json(row.get("payload_json"), {})
    result["payload"] = _tier_shape(payload if isinstance(payload, dict) else {})
    return result


def _safe_audit(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    for field in ("previous_json", "new_json"):
        if result.get(field) is not None:
            result[field] = _remove_secrets(_parse_json(result[field], {}))
    return result


def _slug(value: str) -> str:
    result = str(value or "").strip().lower()
    if not _SLUG_RE.fullmatch(result):
        raise HTTPException(status_code=422, detail="slug must contain lowercase letters, numbers, and hyphens")
    return result


async def _assign_roster_entry_ids(
    entries: list[dict[str, Any]], existing_entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Accept only server-owned identities and issue UUIDs for genuinely new rows."""
    from app import openai_subscription

    accepted = await openai_subscription.existing_entries_with_reservations(
        entries, existing_entries, scope="platform",
    )
    try:
        return assign_entry_ids(entries, existing_entries=accepted)
    except RosterError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _entries(value: Any, default_id: Optional[str], *, publishing: bool) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise HTTPException(status_code=422, detail="entries must be a list")
    if _contains_secret(value):
        raise HTTPException(status_code=422, detail="Store roster credentials in the platform vault, not roster metadata")
    try:
        validated = validate_roster_entries(value, require_provider_model=publishing)
    except RosterError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    seen, result = set(), []
    for index, item in enumerate(validated):
        if not isinstance(item, dict):
            raise HTTPException(status_code=422, detail=f"entries[{index}] must be an object")
        entry_id = str(item.get("entry_id") or "").strip()
        if not entry_id or entry_id in seen:
            detail = f"entries[{index}].entry_id is required" if not entry_id else f"Duplicate entry_id: {entry_id}"
            raise HTTPException(status_code=422, detail=detail)
        seen.add(entry_id)
        normalized = dict(item, entry_id=entry_id)
        if normalized.get("provider") == "openai_subscription" or normalized.get("auth_mode") == "chatgpt":
            normalized["auth_mode"] = "chatgpt"
            normalized["base_url"] = "codex://chatgpt"
            normalized["credential_scope"] = "platform"
        result.append(normalized)
    if publishing and not result:
        raise HTTPException(status_code=422, detail="A published roster must contain an entry")
    # Standard is the primary/default chat position.  default_entry_id remains
    # accepted only as a compatibility hint for historical roster revisions;
    # it is no longer a publication requirement or an admin-facing concept.
    if default_id and default_id not in seen:
        raise HTTPException(status_code=422, detail="default_entry_id must reference a roster entry")
    return result


def _policy(value: Any) -> dict[str, Any]:
    try:
        return normalize_policy(value)
    except PolicyError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


async def _audit(db, actor: str, action: str, entity_type: str, entity_id: str,
                 *, previous=None, new=None, reason="", subject=None) -> None:
    await db.append_entitlement_audit_event(
        str(uuid.uuid4()), action=action, entity_type=entity_type, entity_id=entity_id,
        subject_user_id=subject, actor_user_id=actor, previous_json=previous,
        new_json=new, reason=reason,
    )


async def _roster(db, value: str) -> dict[str, Any]:
    row = await db.get_model_roster(value) or await db.get_model_roster_by_slug(value)
    if not row:
        raise HTTPException(status_code=404, detail="Model roster not found")
    return dict(row)


@router.get("/schema")
async def entitlement_schema(requesting_user_id: str = Query(...)):
    """Return editor metadata derived from installed descriptors and code schema."""
    await _admin_actor(requesting_user_id)
    from app.ui_pages import ui_catalog

    catalog = ui_catalog()
    pages = [
        {"id": str(page.get("id") or ""), "label": str(page.get("label") or page.get("id") or "")}
        for kind in ("main", "admin") for page in (catalog.get(kind) or [])
        if str(page.get("id") or "") in installed_page_ids()
    ]
    return {
        "policy_schema_version": POLICY_SCHEMA_VERSION,
        "pages": pages,
        "features": sorted(KNOWN_FEATURES),
        "ability_groups": sorted(KNOWN_ABILITY_GROUPS),
        "reasoning_levels": list(REASONING_LEVELS),
        "limits": {key: dict(value) for key, value in LIMIT_SPECS.items()},
    }


async def _published_roster(db, row: dict[str, Any]) -> Optional[dict[str, Any]]:
    getter = getattr(db, "get_published_model_roster", None)
    live = await getter(str(row.get("id") or "")) if getter is not None else None
    return dict(live) if live else None


def _roster_shape(row: Optional[dict[str, Any]]) -> dict[str, Any]:
    if not row:
        return {}
    return {
        "slug": row.get("slug"), "name": row.get("name"),
        "description": row.get("description"),
        "entries": _remove_secrets(_parse_json(row.get("entries_json"), [])),
        "default_entry_id": row.get("default_entry_id"),
    }


def _shape_diff(live: dict[str, Any], draft: dict[str, Any]) -> dict[str, Any]:
    keys = sorted(set(live) | set(draft))
    return {
        key: {"published": live.get(key), "draft": draft.get(key)}
        for key in keys if live.get(key) != draft.get(key)
    }


async def _tier(db, value: str) -> dict[str, Any]:
    row = await db.get_experience_tier(value) or await db.get_experience_tier_by_slug(value)
    if not row:
        raise HTTPException(status_code=404, detail="Experience tier not found")
    return dict(row)


async def _published_tier(db, row: dict[str, Any]) -> Optional[dict[str, Any]]:
    getter = getattr(db, "get_published_experience_tier", None)
    live = await getter(str(row.get("id") or "")) if getter is not None else None
    return dict(live) if live else None


def _tier_shape(row: Optional[dict[str, Any]]) -> dict[str, Any]:
    if not row:
        return {}
    return {
        "slug": row.get("slug"), "name": row.get("name"),
        "description": row.get("description"),
        "policy": _parse_json(row.get("policy_json", row.get("policy")), {}),
        "policy_schema_version": row.get("policy_schema_version"),
        "roster_id": row.get("roster_id"),
        "is_system": bool(row.get("is_system")),
        "is_locked": bool(row.get("is_locked")),
    }


@router.get("/rosters")
async def list_rosters(requesting_user_id: str = Query(...), status: Optional[str] = None):
    await _admin_actor(requesting_user_id)
    if status and status not in {"draft", "published", "retired"}:
        raise HTTPException(status_code=422, detail="Invalid roster status")
    db = get_app_db()
    return {"rosters": [
        await _safe_roster_with_credentials(db, dict(row))
        for row in await db.list_model_rosters(status=status)
    ]}


@router.get("/rosters/{roster_id}")
async def get_roster(roster_id: str, requesting_user_id: str = Query(...)):
    await _admin_actor(requesting_user_id)
    db = get_app_db()
    return await _safe_roster_with_credentials(db, await _roster(db, roster_id))


@router.post("/rosters", status_code=201)
async def create_roster(req: RosterCreateRequest):
    actor, db, roster_id = await _admin_actor(req.requesting_user_id), get_app_db(), str(req.id or uuid.uuid4())
    assigned_entries = await _assign_roster_entry_ids(req.entries, [])
    try:
        row = await db.upsert_model_roster(
            roster_id, slug=_slug(req.slug), name=req.name.strip(), description=req.description,
            entries_json=_entries(assigned_entries, req.default_entry_id, publishing=False),
            default_entry_id=req.default_entry_id, status="draft", source="admin",
            created_by=actor, updated_by=actor,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=409, detail=f"Could not create roster: {exc}") from exc
    safe = await _safe_roster_with_credentials(db, dict(row))
    await _audit(db, actor, "roster.created", "model_roster", roster_id, new=safe)
    invalidate_capabilities()
    return safe


@router.put("/rosters/{roster_id}")
async def update_roster(roster_id: str, req: RosterUpdateRequest):
    actor, db = await _admin_actor(req.requesting_user_id), get_app_db()
    previous = await _roster(db, roster_id)
    fields = req.model_dump(exclude={"requesting_user_id"}, exclude_unset=True)
    if "slug" in fields:
        fields["slug"] = _slug(fields["slug"])
    if "entries" in fields:
        default_id = fields.get("default_entry_id", previous.get("default_entry_id"))
        submitted_entries = fields.pop("entries")
        previous_entries = _parse_json(previous.get("entries_json"), [])
        assigned_entries = await _assign_roster_entry_ids(submitted_entries, previous_entries)
        fields["entries_json"] = _entries(assigned_entries, default_id, publishing=False)
    if fields:
        fields["source"] = "admin"
    fields["updated_by"] = actor
    try:
        row = await db.upsert_model_roster(previous["id"], **fields)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=409, detail=f"Could not update roster: {exc}") from exc
    safe = await _safe_roster_with_credentials(db, dict(row))
    await _audit(db, actor, "roster.updated", "model_roster", previous["id"],
                 previous=_safe_roster(previous), new=safe)
    invalidate_capabilities()
    return safe


async def _activate_published_base_roster(actor: str, db) -> None:
    """Activate Base's published snapshot without resyncing tier rosters."""
    active = await load_platform_roster_config("roster-admin", db=db)
    if not active:
        return
    from app.admin.settings import _load_own_llm_config, _persist_llm_config

    existing = await _load_own_llm_config(actor)
    merged = {**existing, **active}
    merged["providers"] = {
        **dict(existing.get("providers") or {}),
        **dict(active.get("providers") or {}),
    }
    await _persist_llm_config(actor, merged, sync_named_rosters=False)


@router.post("/rosters/{roster_id}/publish")
async def publish_roster(roster_id: str, req: RosterActionRequest):
    actor, db = await _admin_actor(req.requesting_user_id), get_app_db()
    previous = await _roster(db, roster_id)
    _entries(_parse_json(previous.get("entries_json"), []), previous.get("default_entry_id"), publishing=True)
    try:
        row = await db.publish_model_roster(
            previous["id"], actor_user_id=actor,
            expected_revision=getattr(req, "expected_revision", None), action="published",
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={"code": "revision_conflict"}) from exc
    if str(previous.get("id") or "") == "roster-admin":
        await _activate_published_base_roster(actor, db)

    safe = await _safe_roster_with_credentials(db, dict(row))
    await _audit(db, actor, "roster.published", "model_roster", previous["id"],
                 previous=_safe_roster(previous), new=safe, reason=getattr(req, "reason", ""))
    invalidate_capabilities()
    return safe


@router.post("/rosters/{roster_id}/validate")
async def validate_roster(roster_id: str, req: AdminRequest):
    await _admin_actor(req.requesting_user_id)
    db = get_app_db()
    row = await _roster(db, roster_id)
    try:
        entries = _entries(
            _parse_json(row.get("entries_json"), []),
            row.get("default_entry_id"), publishing=True,
        )
    except HTTPException as exc:
        return {"valid": False, "errors": [exc.detail], "warnings": []}
    states = await roster_credential_states(str(row["id"]), db=db)
    missing = [entry["entry_id"] for entry in entries if not states.get(entry["entry_id"])]
    return {
        "valid": True, "errors": [],
        "warnings": ([{"code": "credential_missing", "entry_ids": missing}] if missing else []),
    }


@router.post("/rosters/{roster_id}/preview")
async def preview_roster(roster_id: str, req: AdminRequest):
    await _admin_actor(req.requesting_user_id)
    db = get_app_db()
    row = await _roster(db, roster_id)
    live = await _published_roster(db, row)
    tiers = [dict(item) for item in await db.list_experience_tiers()]
    referencing = [tier for tier in tiers if str(tier.get("roster_id") or "") == str(row["id"])]
    tier_ids = {str(tier.get("id") or "") for tier in referencing}
    assignments = [dict(item) for item in await db.list_user_tier_assignments()]
    users = {str(item.get("user_id") or "") for item in assignments
             if str(item.get("tier_id") or "") in tier_ids and item.get("user_id")}
    validation = await validate_roster(roster_id, req)
    return {
        "validation": validation,
        "diff": _shape_diff(_roster_shape(live), _roster_shape(row)),
        "impact": {
            "tier_count": len(referencing), "user_count": len(users),
            "tier_ids": sorted(tier_ids),
        },
        "published_revision": row.get("published_revision"),
        "draft_revision": int(row.get("revision") or 1),
    }


@router.get("/rosters/{roster_id}/history")
async def roster_history(roster_id: str, requesting_user_id: str = Query(...)):
    await _admin_actor(requesting_user_id)
    db = get_app_db()
    row = await _roster(db, roster_id)
    revisions = await db.list_model_roster_revisions(str(row["id"]))
    return {
        "roster_id": row["id"], "published_revision": row.get("published_revision"),
        "revisions": [_safe_revision(dict(item)) for item in revisions],
    }


@router.post("/rosters/{roster_id}/retire")
async def retire_roster(roster_id: str, req: RosterActionRequest):
    actor, db = await _admin_actor(req.requesting_user_id), get_app_db()
    previous = await _roster(db, roster_id)
    if str(previous.get("id") or "") == "roster-admin":
        raise HTTPException(status_code=409, detail="Base — Admin only cannot be retired")
    if req.expected_revision is not None and int(previous.get("revision") or 1) != req.expected_revision:
        raise HTTPException(status_code=409, detail={"code": "revision_conflict"})
    row = await db.upsert_model_roster(
        previous["id"], status="retired", source="admin", updated_by=actor,
    )
    safe = await _safe_roster_with_credentials(db, dict(row))
    await _audit(db, actor, "roster.retired", "model_roster", previous["id"],
                 previous=_safe_roster(previous), new=safe, reason=req.reason)
    invalidate_capabilities()
    return safe


@router.post("/rosters/{roster_id}/rollback")
async def rollback_roster(roster_id: str, req: RosterRollbackRequest):
    actor, db = await _admin_actor(req.requesting_user_id), get_app_db()
    previous = await _roster(db, roster_id)
    snapshot = await db.get_model_roster_revision(previous["id"], req.revision)
    if not snapshot:
        raise HTTPException(status_code=404, detail="Model roster revision not found")
    payload = _parse_json(snapshot.get("payload_json"), {})
    _entries(_parse_json(payload.get("entries_json"), []), payload.get("default_entry_id"), publishing=True)
    row = await db.rollback_model_roster(
        previous["id"], req.revision, actor_user_id=actor,
    )
    if str(previous.get("id") or "") == "roster-admin":
        await _activate_published_base_roster(actor, db)
    safe = await _safe_roster_with_credentials(db, dict(row))
    await _audit(db, actor, "roster.rolled_back", "model_roster", previous["id"],
                 previous=_safe_roster(previous), new=safe, reason=req.reason)
    invalidate_capabilities()
    return safe


@router.put("/rosters/{roster_id}/credentials/{entry_id}")
async def put_roster_credential(roster_id: str, entry_id: str, req: RosterCredentialRequest):
    actor, db = await _admin_actor(req.requesting_user_id), get_app_db()
    row = await _roster(db, roster_id)
    before = await roster_credential_states(str(row["id"]), db=db)
    try:
        after = await set_roster_entry_credential(
            str(row["id"]), entry_id, req.credential, db=db,
        )
    except RosterError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    claim = getattr(db, "claim_model_roster", None)
    if claim is not None:
        row = await claim(str(row["id"]), actor_user_id=actor)
    await _audit(
        db, actor, "roster.credential_set", "model_roster", str(row["id"]),
        previous={"entry_id": entry_id, "configured": bool(before.get(entry_id))},
        new={"entry_id": entry_id, "configured": bool(after.get(entry_id))},
        reason=req.reason,
    )
    return await _safe_roster_with_credentials(db, dict(row))


@router.delete("/rosters/{roster_id}/credentials/{entry_id}")
async def delete_roster_credential(
    roster_id: str, entry_id: str, requesting_user_id: str = Query(...),
    reason: str = Query("", max_length=1000),
):
    actor, db = await _admin_actor(requesting_user_id), get_app_db()
    row = await _roster(db, roster_id)
    before = await roster_credential_states(str(row["id"]), db=db)
    try:
        after = await delete_roster_entry_credential(str(row["id"]), entry_id, db=db)
    except RosterError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    claim = getattr(db, "claim_model_roster", None)
    if claim is not None:
        row = await claim(str(row["id"]), actor_user_id=actor)
    await _audit(
        db, actor, "roster.credential_deleted", "model_roster", str(row["id"]),
        previous={"entry_id": entry_id, "configured": bool(before.get(entry_id))},
        new={"entry_id": entry_id, "configured": bool(after.get(entry_id))},
        reason=reason,
    )
    return await _safe_roster_with_credentials(db, dict(row))


@router.delete("/rosters/{roster_id}")
async def delete_roster(roster_id: str, requesting_user_id: str = Query(...)):
    actor, db = await _admin_actor(requesting_user_id), get_app_db()
    previous = await _roster(db, roster_id)
    if any(str(row.get("roster_id") or "") == str(previous["id"])
           for row in await db.list_experience_tiers()):
        raise HTTPException(status_code=409, detail="Roster is referenced by an experience tier")
    try:
        deleted = await db.delete_model_roster(previous["id"])
    except Exception as exc:
        raise HTTPException(status_code=409, detail=f"Could not delete roster: {exc}") from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="Model roster not found")
    await _audit(db, actor, "roster.deleted", "model_roster", previous["id"], previous=_safe_roster(previous))
    invalidate_capabilities()
    return {"id": previous["id"], "deleted": True}


@router.post("/tier-credit-resets")
async def reset_tier_credit(req: TierCreditResetRequest):
    actor, db = await _admin_actor(req.requesting_user_id), get_app_db()
    if req.scope_kind == "network" and not str(req.principal_key).startswith("sha256:"):
        raise HTTPException(status_code=422, detail="Anonymous reset requires a pseudonymous network hash")
    if req.tier_id:
        await _tier(db, req.tier_id)
    reset = await db.reset_tier_credit_allowances(
        scope_kind=req.scope_kind, principal_key=req.principal_key, tier_id=req.tier_id,
        actor_user_id=actor, reason=req.reason,
    )
    await _audit(db, actor, "tier_credit.reset", "tier_credit_allowance", str(req.tier_id or "all"),
                 subject=req.principal_key, new=reset, reason=req.reason)
    return reset


@router.get("/tiers")
async def list_tiers(requesting_user_id: str = Query(...), status: Optional[str] = None):
    await _admin_actor(requesting_user_id)
    if status and status not in {"draft", "published", "retired"}:
        raise HTTPException(status_code=422, detail="Invalid tier status")
    return {"tiers": [_safe_tier(dict(row)) for row in await get_app_db().list_experience_tiers(status=status)]}


@router.get("/tiers/{tier_id}")
async def get_tier(tier_id: str, requesting_user_id: str = Query(...)):
    await _admin_actor(requesting_user_id)
    return _safe_tier(await _tier(get_app_db(), tier_id))


@router.post("/tiers", status_code=201)
async def create_tier(req: TierCreateRequest):
    actor, db, tier_id = await _admin_actor(req.requesting_user_id), get_app_db(), str(req.id or uuid.uuid4())
    policy = _policy(req.policy)
    roster_id = str(req.roster_id or policy["models"]["roster_id"])
    if roster_id != policy["models"]["roster_id"]:
        raise HTTPException(status_code=422, detail="roster_id must match policy.models.roster_id")
    await _roster(db, roster_id)
    try:
        row = await db.upsert_experience_tier(
            tier_id, slug=_slug(req.slug), name=req.name.strip(), description=req.description,
            policy_json=policy, policy_schema_version=POLICY_SCHEMA_VERSION, roster_id=roster_id,
            is_system=req.is_system, is_locked=req.is_locked, status="draft",
            created_by=actor, updated_by=actor,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=409, detail=f"Could not create tier: {exc}") from exc
    safe = _safe_tier(dict(row))
    await _audit(db, actor, "tier.created", "experience_tier", tier_id, new=safe)
    invalidate_capabilities()
    return safe


@router.put("/tiers/{tier_id}")
async def update_tier(tier_id: str, req: TierUpdateRequest):
    actor, db = await _admin_actor(req.requesting_user_id), get_app_db()
    previous = await _tier(db, tier_id)
    if previous.get("is_locked"):
        raise HTTPException(status_code=409, detail="Locked system tiers cannot be edited")
    fields = req.model_dump(
        exclude={"requesting_user_id", "expected_revision"}, exclude_unset=True,
    )
    if "slug" in fields:
        fields["slug"] = _slug(fields["slug"])
    policy = _policy(fields.pop("policy")) if "policy" in fields else _policy(_parse_json(previous.get("policy_json"), {}))
    roster_id = str(fields.get("roster_id") or previous.get("roster_id") or policy["models"]["roster_id"])
    if roster_id != policy["models"]["roster_id"]:
        raise HTTPException(status_code=422, detail="roster_id must match policy.models.roster_id")
    await _roster(db, roster_id)
    fields.update(policy_json=policy, policy_schema_version=POLICY_SCHEMA_VERSION,
                  roster_id=roster_id, updated_by=actor)
    try:
        row = await db.upsert_experience_tier(
            previous["id"], expected_revision=req.expected_revision, **fields,
        )
    except HTTPException:
        raise
    except ValueError as exc:
        if "revision conflict" in str(exc):
            raise HTTPException(status_code=409, detail={"code": "revision_conflict"}) from exc
        raise HTTPException(status_code=409, detail=f"Could not update tier: {exc}") from exc
    except Exception as exc:
        raise HTTPException(status_code=409, detail=f"Could not update tier: {exc}") from exc
    safe = _safe_tier(dict(row))
    await _audit(db, actor, "tier.updated", "experience_tier", previous["id"],
                 previous=_safe_tier(previous), new=safe)
    invalidate_capabilities()
    return safe


@router.post("/tiers/{tier_id}/publish")
async def publish_tier(tier_id: str, req: TierActionRequest):
    actor, db = await _admin_actor(req.requesting_user_id), get_app_db()
    previous = await _tier(db, tier_id)
    policy = _policy(_parse_json(previous.get("policy_json"), {}))
    roster_id = str(previous.get("roster_id") or policy["models"]["roster_id"])
    if roster_id != policy["models"]["roster_id"]:
        raise HTTPException(status_code=422, detail="roster_id must match policy.models.roster_id")
    roster = await _roster(db, roster_id)
    live_roster = await _published_roster(db, roster)
    if not live_roster:
        raise HTTPException(status_code=409, detail="Publish the tier's model roster first")
    roster_entries = _entries(
        _parse_json(live_roster.get("entries_json"), []),
        live_roster.get("default_entry_id"), publishing=True,
    )
    allowed = set(policy["models"]["allowed_entry_ids"])
    entry_ids = {entry["entry_id"] for entry in roster_entries}
    if allowed and allowed != {"*"} and not allowed.issubset(entry_ids):
        raise HTTPException(status_code=422, detail="Tier allows entries absent from its roster")
    try:
        row = await db.publish_experience_tier(
            previous["id"], actor_user_id=actor,
            expected_revision=getattr(req, "expected_revision", None), action="published",
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={"code": "revision_conflict"}) from exc
    safe = _safe_tier(dict(row))
    await _audit(db, actor, "tier.published", "experience_tier", previous["id"],
                 previous=_safe_tier(previous), new=safe, reason=getattr(req, "reason", ""))
    invalidate_capabilities()
    return safe


@router.post("/tiers/{tier_id}/validate")
async def validate_tier(tier_id: str, req: AdminRequest):
    await _admin_actor(req.requesting_user_id)
    db = get_app_db()
    row = await _tier(db, tier_id)
    errors: list[Any] = []
    warnings: list[Any] = []
    try:
        policy = _policy(_parse_json(row.get("policy_json"), {}))
    except HTTPException as exc:
        return {"valid": False, "errors": [exc.detail], "warnings": []}
    roster_id = str(row.get("roster_id") or policy["models"]["roster_id"])
    if roster_id != policy["models"]["roster_id"]:
        errors.append("roster_id must match policy.models.roster_id")
        return {"valid": False, "errors": errors, "warnings": warnings}
    try:
        roster = await _roster(db, roster_id)
    except HTTPException:
        return {"valid": False, "errors": ["Referenced model roster does not exist"], "warnings": []}
    live_roster = await _published_roster(db, roster)
    if not live_roster:
        return {"valid": False, "errors": ["Referenced model roster is not published"], "warnings": []}
    try:
        entries = _entries(
            _parse_json(live_roster.get("entries_json"), []),
            live_roster.get("default_entry_id"), publishing=True,
        )
    except HTTPException as exc:
        return {"valid": False, "errors": [exc.detail], "warnings": []}
    allowed = set(policy["models"]["allowed_entry_ids"])
    entry_ids = {entry["entry_id"] for entry in entries}
    missing = sorted(allowed - entry_ids) if allowed != {"*"} else []
    if missing:
        errors.append({"code": "model_entries_missing", "entry_ids": missing})
    return {"valid": not errors, "errors": errors, "warnings": warnings}


@router.post("/tiers/{tier_id}/preview")
async def preview_tier(tier_id: str, req: AdminRequest):
    await _admin_actor(req.requesting_user_id)
    db = get_app_db()
    row = await _tier(db, tier_id)
    live = await _published_tier(db, row)
    assignments = [dict(item) for item in await db.list_user_tier_assignments()]
    affected = {str(item.get("user_id") or "") for item in assignments
                if str(item.get("tier_id") or "") == str(row["id"]) and item.get("user_id")}
    return {
        "validation": await validate_tier(tier_id, req),
        "diff": _shape_diff(_tier_shape(live), _tier_shape(row)),
        "impact": {"user_count": len(affected), "user_ids": sorted(affected)},
        "published_revision": row.get("published_revision"),
        "draft_revision": int(row.get("revision") or 1),
    }


@router.get("/tiers/{tier_id}/history")
async def tier_history(tier_id: str, requesting_user_id: str = Query(...)):
    await _admin_actor(requesting_user_id)
    db = get_app_db()
    row = await _tier(db, tier_id)
    revisions = await db.list_experience_tier_revisions(str(row["id"]))
    return {
        "tier_id": row["id"], "published_revision": row.get("published_revision"),
        "revisions": [_safe_tier_revision(dict(item)) for item in revisions],
    }


@router.post("/tiers/{tier_id}/retire")
async def retire_tier(tier_id: str, req: TierActionRequest):
    actor, db = await _admin_actor(req.requesting_user_id), get_app_db()
    previous = await _tier(db, tier_id)
    if previous.get("is_locked"):
        raise HTTPException(status_code=409, detail="Locked system tiers cannot be retired")
    try:
        row = await db.upsert_experience_tier(
            previous["id"], expected_revision=req.expected_revision,
            status="retired", updated_by=actor,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={"code": "revision_conflict"}) from exc
    safe = _safe_tier(dict(row))
    await _audit(db, actor, "tier.retired", "experience_tier", previous["id"],
                 previous=_safe_tier(previous), new=safe, reason=req.reason)
    invalidate_capabilities()
    return safe


@router.post("/tiers/{tier_id}/rollback")
async def rollback_tier(tier_id: str, req: TierRollbackRequest):
    actor, db = await _admin_actor(req.requesting_user_id), get_app_db()
    previous = await _tier(db, tier_id)
    if previous.get("is_locked"):
        raise HTTPException(status_code=409, detail="Locked system tiers cannot be rolled back")
    snapshot = await db.get_experience_tier_revision(previous["id"], req.revision)
    if not snapshot:
        raise HTTPException(status_code=404, detail="Experience tier revision not found")
    payload = _parse_json(snapshot.get("payload_json"), {})
    _policy(_parse_json(payload.get("policy_json"), {}))
    row = await db.rollback_experience_tier(
        previous["id"], req.revision, actor_user_id=actor,
    )
    safe = _safe_tier(dict(row))
    await _audit(db, actor, "tier.rolled_back", "experience_tier", previous["id"],
                 previous=_safe_tier(previous), new=safe, reason=req.reason)
    invalidate_capabilities()
    return safe


@router.delete("/tiers/{tier_id}")
async def delete_tier(tier_id: str, requesting_user_id: str = Query(...)):
    actor, db = await _admin_actor(requesting_user_id), get_app_db()
    previous = await _tier(db, tier_id)
    if previous.get("is_system") or previous.get("is_locked"):
        raise HTTPException(status_code=409, detail="System tiers cannot be deleted")
    if any(str(row.get("tier_id") or "") == str(previous["id"])
           for row in await db.list_user_tier_assignments()):
        raise HTTPException(status_code=409, detail="Tier has user assignments")
    try:
        deleted = await db.delete_experience_tier(previous["id"])
    except Exception as exc:
        raise HTTPException(status_code=409, detail=f"Could not delete tier: {exc}") from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="Experience tier not found")
    await _audit(db, actor, "tier.deleted", "experience_tier", previous["id"], previous=_safe_tier(previous))
    invalidate_capabilities()
    return {"id": previous["id"], "deleted": True}


@router.get("/assignments")
async def list_assignments(requesting_user_id: str = Query(...), user_id: Optional[str] = None):
    await _admin_actor(requesting_user_id)
    return {"assignments": [dict(row) for row in await get_app_db().list_user_tier_assignments(user_id=user_id)]}


@router.post("/users/{user_id}/tier", status_code=201)
async def assign_user_tier(user_id: str, req: AssignmentCreateRequest):
    actor, db = await _admin_actor(req.requesting_user_id), get_app_db()
    tier = await _tier(db, req.tier_id)
    if str(tier.get("status") or "draft") != "published":
        raise HTTPException(status_code=409, detail="Only published tiers may be assigned")
    starts, expires = req.starts_at or datetime.now(timezone.utc), req.expires_at
    starts_cmp = starts if starts.tzinfo else starts.replace(tzinfo=timezone.utc)
    expires_cmp = expires if not expires or expires.tzinfo else expires.replace(tzinfo=timezone.utc)
    if expires_cmp and expires_cmp <= starts_cmp:
        raise HTTPException(status_code=422, detail="expires_at must be after starts_at")
    assignment_id = str(uuid.uuid4())
    row = await db.upsert_user_tier_assignment(
        assignment_id, user_id=user_id, tier_id=tier["id"], source=req.source,
        starts_at=starts.isoformat(), expires_at=expires.isoformat() if expires else None,
        assigned_by=actor, reason=req.reason.strip(),
    )
    await _audit(db, actor, "assignment.created", "user_tier_assignment", assignment_id,
                 new=dict(row), reason=req.reason.strip(), subject=user_id)
    invalidate_capabilities(user_id)
    # Tier assignment owns the approval transition in both directions. Pending
    # shares Anonymous's restrictive policy but remains distinguishable through
    # the account's awaiting-approval state; every other tier authorizes it.
    from app.auth.users import get_user_by_id as _get_user_by_id
    from app.auth.users import set_user_approval as _set_user_approval
    target = await _get_user_by_id(user_id)
    should_approve = str(tier.get("slug") or tier.get("id") or "").lower() != "pending"
    if target is not None and target.is_approved != should_approve:
        await _set_user_approval(target.username, should_approve)
    return dict(row)


@router.delete("/assignments/{assignment_id}")
async def delete_assignment(assignment_id: str, requesting_user_id: str = Query(...),
                            reason: str = Query(..., min_length=1, max_length=1000)):
    actor, db = await _admin_actor(requesting_user_id), get_app_db()
    previous = await db.get_user_tier_assignment(assignment_id)
    if not previous:
        raise HTTPException(status_code=404, detail="Tier assignment not found")
    if not await db.delete_user_tier_assignment(assignment_id):
        raise HTTPException(status_code=404, detail="Tier assignment not found")
    await _audit(db, actor, "assignment.deleted", "user_tier_assignment", assignment_id,
                 previous=dict(previous), reason=reason, subject=previous.get("user_id"))
    invalidate_capabilities(previous.get("user_id"))
    return {"id": assignment_id, "deleted": True}


@router.get("/audit")
async def list_audit_events(requesting_user_id: str = Query(...),
                            subject_user_id: Optional[str] = None,
                            entity_type: Optional[str] = None,
                            entity_id: Optional[str] = None,
                            limit: int = Query(100, ge=1, le=1000)):
    await _admin_actor(requesting_user_id)
    rows = await get_app_db().list_entitlement_audit_events(
        subject_user_id=subject_user_id, entity_type=entity_type, entity_id=entity_id, limit=limit,
    )
    return {"events": [_safe_audit(dict(row)) for row in rows]}
