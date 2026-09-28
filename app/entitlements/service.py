"""Database-backed resolution of secret-free capability documents."""

from __future__ import annotations

import json
import hashlib
import logging
import time
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Optional

from urllib.parse import urlsplit, urlunsplit

from .policy import LIMIT_SPECS, KNOWN_FEATURES, PolicyError, compose_policy, installed_page_ids, system_policy

logger = logging.getLogger(__name__)
_CACHE_TTL_SECONDS = 3.0
_cache: dict[tuple[str, bool], tuple[float, dict[str, Any]]] = {}


def invalidate_capabilities(user_id: Optional[str] = None) -> None:
    if user_id is None:
        _cache.clear()
        return
    uid = str(user_id)
    for key in [candidate for candidate in _cache if candidate[0] == uid]:
        _cache.pop(key, None)


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value or "{}")
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _active(row: dict[str, Any], now: datetime) -> bool:
    def parse(value):
        if not value:
            return None
        if isinstance(value, datetime):
            return value
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except Exception:
            return False
    starts, expires = parse(row.get("starts_at")), parse(row.get("expires_at"))
    if starts is False or expires is False:
        return False
    if starts and starts > (now if starts.tzinfo else now.replace(tzinfo=None)):
        return False
    if expires and expires <= (now if expires.tzinfo else now.replace(tzinfo=None)):
        return False
    return True


async def _call(db, name: str, *args, **kwargs):
    fn = getattr(db, name, None)
    return await fn(*args, **kwargs) if fn is not None else None


async def _audit(db, user_id: str, reason: str, detail: str) -> None:
    try:
        fn = getattr(db, "append_entitlement_audit_event", None)
        if fn is None:
            return
        try:
            await fn(
                str(uuid.uuid4()), subject_user_id=user_id or None,
                actor_user_id="system", action="resolution_fallback",
                entity_type="experience_tier", entity_id=None,
                new_json={"detail": detail[:500]}, reason=reason,
            )
        except TypeError:
            # Compatibility for small test/legacy adapters that accepted one row.
            await fn({
                "subject_user_id": user_id, "actor_user_id": "system",
                "action": "resolution_fallback", "reason": reason,
                "metadata": {"detail": detail[:500]},
            })
    except Exception:
        logger.debug("Could not persist entitlement fallback audit", exc_info=True)


async def _record(db, primary: str, secondary: str, value: str):
    row = await _call(db, primary, value)
    return row or await _call(db, secondary, value)


def _safe_base_url(value: Any) -> str:
    """Return origin/path metadata without credentials, query, or fragment."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return ""
        host = parsed.hostname
        if parsed.port:
            host = f"{host}:{parsed.port}"
        return urlunsplit((parsed.scheme, host, parsed.path or "", "", ""))
    except (TypeError, ValueError):
        return ""


def _safe_roster_entry(item: dict[str, Any], credential_ids: set[str]) -> dict[str, Any]:
    entry_id = str(item.get("entry_id") or item.get("id") or "").strip()
    safe = {
        "entry_id": entry_id,
        "provider": str(item.get("provider") or ""),
        # Authentication routing is server-side configuration, not browser-safe
        # capability metadata.  In particular, never expose an API-key marker.
        "model": str(item.get("model") or ""),
        "label": str(item.get("label") or item.get("display_label") or item.get("model") or entry_id),
        "credential_configured": entry_id in credential_ids,
    }
    base_url = _safe_base_url(item.get("base_url"))
    if base_url:
        safe["base_url"] = base_url
    for field in (
        "text_capable", "image_capable", "image_out_capable", "voice_capable",
        "high_effort_capable", "tool_capable",
    ):
        if field in item:
            safe[field] = bool(item.get(field))
    for field in ("input_modalities", "output_modalities", "capabilities"):
        values = item.get(field)
        if isinstance(values, list):
            safe[field] = [str(value) for value in values if isinstance(value, str)]
    return safe


async def _credential_ids(db, roster_id: str) -> set[str]:
    try:
        row = await _call(db, "auth_element_get", "_platform", "llm_roster", roster_id)
        payload = json.loads(str((row or {}).get("secret_ref") or "{}"))
        entries = payload.get("entries") if isinstance(payload, dict) else None
        return {str(key) for key, value in (entries or {}).items() if value}
    except Exception:
        return set()


def _evaluation_revision(parts: dict[str, Any]) -> str:
    encoded = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:24]


async def _assignment(db, user_id: str):
    direct = await _call(db, "get_active_user_tier_assignment", user_id)
    if direct and _active(dict(direct), datetime.now(timezone.utc)):
        return direct
    rows = await _call(db, "list_user_tier_assignments", user_id=user_id) or []
    active = [dict(row) for row in rows if _active(dict(row), datetime.now(timezone.utc))]
    if not active:
        return None
    # Manual safety/grant rows win over billing/import/default; latest row wins
    # within a source.  The DB method may later perform this selection itself.
    source_rank = {"manual": 4, "billing": 3, "import": 2, "default": 1, "system": 0}
    return max(active, key=lambda row: (source_rank.get(str(row.get("source") or ""), 0),
                                        str(row.get("updated_at") or row.get("created_at") or "")))


async def resolve_capabilities(user_id: Optional[str], *, db=None, use_cache: bool = True,
                               installation_pages: Optional[set[str]] = None,
                               installation_features: Optional[set[str]] = None) -> dict[str, Any]:
    """Resolve one browser-safe policy without ever reading roster secrets."""
    uid = str(user_id or "")
    try:
        from app.agent.member_workspace import is_agent_member_subject
        agent_native = is_agent_member_subject(uid)
    except Exception:
        agent_native = False
    # An agent-local login proves membership to one agent, not registration in
    # the hosting application. App pages/resources therefore see the anonymous
    # installation tier; the agent runtime separately applies its own profile.
    anonymous = not uid or uid.startswith("anon_") or uid == "__anonymous__" or agent_native
    if db is None:
        from app.db import get_db
        db = get_db()
    try:
        is_admin = bool(uid) and bool(await db.is_user_admin(uid))
    except Exception:
        is_admin = False
    cacheable = installation_pages is None and installation_features is None
    cache_key, now_mono = (uid, is_admin), time.monotonic()
    if use_cache and cacheable:
        cached = _cache.get(cache_key)
        if cached and now_mono < cached[0]:
            return deepcopy(cached[1])

    requested = "anonymous" if anonymous else "free"
    slug, source = requested, ("system" if anonymous else "default")
    assignment, fallback_reason = None, ""
    if not anonymous:
        try:
            assignment = await _assignment(db, uid)
        except Exception as exc:
            fallback_reason = "assignment_lookup_failed"
            logger.warning("Tier assignment lookup failed for %s: %s", uid[:12], exc)
        if assignment:
            requested = str(assignment.get("tier_id") or assignment.get("tier_slug") or "").strip()
            slug = requested
            source = str(assignment.get("source") or "manual")

    tier_id, tier_revision, raw_policy = requested, 1, None
    try:
        published_tier_getter = getattr(db, "get_published_experience_tier", None)
        if requested and published_tier_getter is not None:
            record = await published_tier_getter(requested)
            tier_container = None if record else await _record(
                db, "get_experience_tier", "get_experience_tier_by_slug", requested
            )
        else:
            record = await _record(
                db, "get_experience_tier", "get_experience_tier_by_slug", requested
            ) if requested else None
            tier_container = record
    except Exception as exc:
        record, tier_container = None, None
        fallback_reason = fallback_reason or "tier_lookup_failed"
        logger.warning("Tier lookup failed for %s: %s", requested, exc)
    if record:
        if str(record.get("status") or "published").lower() != "published":
            fallback_reason = "tier_not_published"
        else:
            tier_id = str(record.get("id") or requested)
            slug = str(record.get("slug") or requested)
            tier_revision = int(record.get("revision") or 1)
            raw_policy = _as_dict(record.get("policy_json") or record.get("policy"))
    else:
        fallback_reason = fallback_reason or (
            "tier_not_published" if tier_container else
            "tier_unknown" if assignment else "tier_missing"
        )
    if fallback_reason:
        slug = tier_id = "anonymous"
        tier_revision, source, raw_policy = 0, "emergency", None
        await _audit(db, uid, fallback_reason, "restrictive fallback")
    try:
        effective = compose_policy(raw_policy if raw_policy is not None else system_policy("anonymous"), is_admin=is_admin,
                                   installation_pages=installation_pages,
                                   installation_features=installation_features)
    except PolicyError as exc:
        await _audit(db, uid, "policy_invalid", str(exc))
        slug = tier_id = "anonymous"
        tier_revision, source = 0, "emergency"
        effective = compose_policy(system_policy("anonymous"), is_admin=is_admin,
                                   installation_pages=installation_pages,
                                   installation_features=installation_features)

    models, roster_revision = dict(effective["models"]), 0
    allowed_entry_ids: list[str] = []
    safe_entries: list[dict[str, Any]] = []
    roster_default: Optional[str] = None
    roster_reason: Optional[str] = None
    default_reason: Optional[str] = None
    roster_record_id = str(models.get("roster_id") or "")
    roster_slug = roster_record_id
    roster_name = ""
    try:
        published_getter = getattr(db, "get_published_model_roster", None)
        if published_getter is not None:
            roster = await published_getter(models["roster_id"])
            container = None if roster else await _record(
                db, "get_model_roster", "get_model_roster_by_slug", models["roster_id"]
            )
        else:
            roster = await _record(db, "get_model_roster", "get_model_roster_by_slug", models["roster_id"])
            container = roster
        if not roster:
            roster_reason = "model_roster_not_published" if container else "model_roster_missing"
        elif str(roster.get("status") or "").lower() != "published":
            roster_reason = "model_roster_not_published"
        else:
            roster_record_id = str(roster.get("id") or models["roster_id"])
            roster_slug = str(roster.get("slug") or roster_record_id)
            roster_name = str(roster.get("name") or roster_slug)
            roster_revision = int(roster.get("published_revision") or roster.get("revision") or 1)
            entries = roster.get("entries_json") or roster.get("entries") or []
            entries = json.loads(entries or "[]") if isinstance(entries, str) else entries
            if not isinstance(entries, list):
                raise ValueError("roster entries must be a list")
            ids = [str(item.get("entry_id") or item.get("id")) for item in entries
                   if isinstance(item, dict) and (item.get("entry_id") or item.get("id"))]
            if len(ids) != len(entries) or len(set(ids)) != len(ids) or not ids:
                roster_reason = "model_roster_invalid"
            else:
                policy_ids = list(models.get("allowed_entry_ids") or [])
                allowed_entry_ids = ids if not policy_ids or policy_ids == ["*"] else [item for item in ids if item in set(policy_ids)]
                if not allowed_entry_ids:
                    roster_reason = "model_roster_has_no_allowed_entries"
                else:
                    credential_ids = await _credential_ids(db, roster_record_id)
                    safe_entries = [
                        _safe_roster_entry(item, credential_ids)
                        for item in entries if str(item.get("entry_id") or item.get("id")) in set(allowed_entry_ids)
                    ]
                    # Standard is the primary/default chat position.  Fall back
                    # to the historical default_entry_id only for old revisions,
                    # then to the first allowed entry for compatibility.
                    standard_entry = next(
                        (
                            item for item in entries
                            if str(item.get("entry_id") or item.get("id")) in allowed_entry_ids
                            and item.get("enabled")
                            and item.get("text_capable", True)
                        ),
                        None,
                    )
                    roster_default = str(
                        (standard_entry or {}).get("entry_id")
                        or (standard_entry or {}).get("id")
                        or roster.get("default_entry_id")
                        or ""
                    )
                    if roster_default not in allowed_entry_ids:
                        roster_default = allowed_entry_ids[0]
                        default_reason = "model_default_stale"
    except Exception as exc:
        roster_reason = "model_roster_invalid"
        logger.debug("Could not enrich safe roster metadata: %s", exc, exc_info=True)

    available = bool(safe_entries and roster_default and not roster_reason)
    configured = sum(1 for item in safe_entries if item.get("credential_configured"))
    credential_state = (
        "unavailable" if not safe_entries else
        "configured" if configured == len(safe_entries) else
        "partial" if configured else "missing"
    )

    page_names = set(installation_pages or installed_page_ids())
    # The admin shell maps its dynamic views to this stable entitlement gate.
    page_names.add("admin-tools")
    evaluated_at = datetime.now(timezone.utc).isoformat()
    assignment_safe = None if not assignment else {
        "id": assignment.get("id"), "source": assignment.get("source"),
        "starts_at": assignment.get("starts_at"), "expires_at": assignment.get("expires_at"),
        "reason": assignment.get("reason") or None,
    }
    revision = _evaluation_revision({
        "tier_id": tier_id, "tier_revision": tier_revision,
        "roster_id": roster_record_id, "roster_revision": roster_revision,
        "assignment": assignment_safe, "admin": is_admin,
        "installation_pages": sorted(installation_pages) if installation_pages is not None else None,
        "installation_features": sorted(installation_features) if installation_features is not None else None,
        "roster_reason": roster_reason,
        "anonymous": effective.get("anonymous", {}),
    })
    result = {
        "subject": {"class": "anonymous" if anonymous else "registered", "is_admin": is_admin},
        "tier": {"id": tier_id, "slug": slug, "revision": tier_revision, "source": source,
                 "fallback_reason": fallback_reason or None},
        "pages": {name: name in effective["pages"] for name in sorted(page_names)},
        "features": {name: True for name in effective["features"]},
        "ability_groups": list(effective["ability_groups"]),
        "agent_templates": list(effective["agent_templates"]),
        "limits": dict(effective["limits"]),
        "anonymous": dict(effective.get("anonymous") or {}),
        "limit_definitions": deepcopy(LIMIT_SPECS),
        "models": {"roster_id": roster_record_id, "roster_slug": roster_slug,
                   "roster_name": roster_name, "revision": roster_revision,
                   "allowed_entry_ids": allowed_entry_ids, "allow_byo": bool(models["allow_byo"]),
                   "max_byo_entries": models["max_byo_entries"],
                   "max_reasoning_effort": models["max_reasoning_effort"],
                   "available": available, "default_entry_id": roster_default,
                   "entries": safe_entries, "credential_state": credential_state,
                   "fallback_reason": roster_reason, "selection_reason": default_reason},
        "assignment": assignment_safe,
        "decisions": {
            "pages": {name: {"allowed": name in effective["pages"],
                              "reason": "allowed" if name in effective["pages"] else "tier_denied"}
                      for name in sorted(page_names)},
            "features": {name: {"allowed": name in effective["features"],
                                 "reason": "allowed" if name in effective["features"] else "tier_denied"}
                         for name in sorted(KNOWN_FEATURES)},
        },
        "evaluation": {"revision": revision, "evaluated_at": evaluated_at},
        "evaluated_at": evaluated_at,
    }
    if cacheable:
        deadline = now_mono + _CACHE_TTL_SECONDS
        if assignment_safe and assignment_safe.get("expires_at"):
            try:
                expiry = datetime.fromisoformat(
                    str(assignment_safe["expires_at"]).replace("Z", "+00:00")
                )
                if expiry.tzinfo is None:
                    expiry = expiry.replace(tzinfo=timezone.utc)
                deadline = min(
                    deadline,
                    now_mono + max(0.0, (expiry - datetime.now(timezone.utc)).total_seconds()),
                )
            except (TypeError, ValueError):
                deadline = now_mono
        _cache[cache_key] = (deadline, deepcopy(result))
    return result
