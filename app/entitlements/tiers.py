"""Validated, insert-only bootstrap seeds for built-in experience tiers.

The JSON files are shipped definitions.  Once imported, the database row is the
live authority.  The one exception is a versioned, locked system tier: a higher
shipped revision is a product/security migration and is published as a new
immutable revision.  Operator-created or unlocked tiers are never overwritten.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping, Optional

from .policy import POLICY_SCHEMA_VERSION, PolicyError, normalize_policy

_SEED_DIR = Path(__file__).resolve().parents[1] / "defaults" / "experience-tiers"
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:[a-z0-9-]*[a-z0-9])?$")
_ALLOWED_FIELDS = {
    "id", "slug", "name", "description", "status", "revision",
    "is_system", "is_locked", "policy_schema_version", "policy",
}


class TierSeedError(ValueError):
    """A shipped tier seed is malformed or unsafe to import."""


def normalize_seed(raw: Mapping[str, Any], *, source: str = "tier seed") -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise TierSeedError(f"{source} must contain an object")
    unknown = set(raw) - _ALLOWED_FIELDS
    if unknown:
        raise TierSeedError(f"{source} has unknown fields: {sorted(unknown)}")
    tier_id = str(raw.get("id") or "").strip()
    slug = str(raw.get("slug") or "").strip().lower()
    name = str(raw.get("name") or "").strip()
    if not tier_id or not slug or not name:
        raise TierSeedError(f"{source} requires id, slug, and name")
    if not _SLUG_RE.fullmatch(slug):
        raise TierSeedError(f"{source} has an invalid slug")
    status = str(raw.get("status") or "published").strip().lower()
    if status not in {"draft", "published", "retired"}:
        raise TierSeedError(f"{source} has invalid status {status!r}")
    try:
        policy = normalize_policy(raw.get("policy") or {})
    except PolicyError as exc:
        raise TierSeedError(f"{source} has invalid policy: {exc}") from exc
    schema_version = int(raw.get("policy_schema_version") or POLICY_SCHEMA_VERSION)
    if schema_version != policy["schema_version"]:
        raise TierSeedError(f"{source} policy schema versions disagree")
    roster_id = str(policy["models"]["roster_id"])
    return {
        "id": tier_id,
        "slug": slug,
        "name": name,
        "description": str(raw.get("description") or ""),
        "status": status,
        "revision": max(1, int(raw.get("revision") or 1)),
        "is_system": bool(raw.get("is_system", True)),
        "is_locked": bool(raw.get("is_locked", True)),
        "policy_schema_version": schema_version,
        "roster_id": roster_id,
        "policy": policy,
    }


def load_tier_seeds() -> list[dict[str, Any]]:
    seeds: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_slugs: set[str] = set()
    for path in sorted(_SEED_DIR.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TierSeedError(f"could not read {path.name}: {exc}") from exc
        seed = normalize_seed(raw, source=path.name)
        if seed["id"] in seen_ids or seed["slug"] in seen_slugs:
            raise TierSeedError(f"duplicate tier id or slug in {path.name}")
        seen_ids.add(seed["id"])
        seen_slugs.add(seed["slug"])
        seeds.append(seed)
    required = {"anonymous", "pending", "free", "pro"}
    missing = required - seen_slugs
    if missing:
        raise TierSeedError(f"missing required built-in tier seeds: {sorted(missing)}")
    return seeds


def seed_policy(slug: str) -> dict[str, Any]:
    """Return a validated shipped policy for tests/bootstrap tooling, not runtime."""
    wanted = str(slug or "").strip().lower()
    for seed in load_tier_seeds():
        if seed["slug"] == wanted:
            return dict(seed["policy"])
    raise TierSeedError(f"unknown shipped tier {wanted!r}")


def _legacy_anonymous_controls() -> dict[str, int]:
    """Snapshot existing operator defaults into the new Anonymous-tier block."""
    from app.admin.settings import (
        get_anon_chat_ip_max, get_anon_chat_ip_window,
        get_anon_chat_max, get_anon_chat_window,
        get_anon_daily_chat_max, get_anon_daily_chat_window,
    )
    return {
        "identity_messages": get_anon_chat_max(),
        "identity_window_seconds": get_anon_chat_window(),
        "daily_messages": get_anon_daily_chat_max(),
        "daily_window_seconds": get_anon_daily_chat_window(),
        "network_messages": get_anon_chat_ip_max(),
        "network_window_seconds": get_anon_chat_ip_window(),
    }


async def _backfill_anonymous_tier_controls(db, existing: Mapping[str, Any]) -> bool:
    """Persist legacy app-wide anonymous allowances into an un-migrated tier.

    The operation is intentionally idempotent: a present ``anonymous`` object,
    even an operator-selected one, is authoritative and is never overwritten.
    """
    raw = existing.get("policy_json") or existing.get("policy") or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return False
    if not isinstance(raw, Mapping) or isinstance(raw.get("anonymous"), Mapping):
        return False
    migrated = dict(raw)
    migrated["anonymous"] = _legacy_anonymous_controls()
    try:
        policy = normalize_policy(migrated)
    except PolicyError:
        return False
    tier_id = str(existing.get("id") or "anonymous")
    current_revision = int(existing.get("revision") or 1)
    saved = await db.upsert_experience_tier(
        tier_id, expected_revision=current_revision,
        policy_json=policy, policy_schema_version=POLICY_SCHEMA_VERSION,
        roster_id=policy["models"]["roster_id"], status="draft",
        revision=current_revision + 1, updated_by="anonymous-tier-backfill",
    )
    publisher = getattr(db, "publish_experience_tier", None)
    if callable(publisher):
        await publisher(
            tier_id, actor_user_id="anonymous-tier-backfill",
            expected_revision=int(saved.get("revision") or current_revision + 1),
            action="anonymous-tier-backfill",
        )
    return True


async def provision_system_tiers(*, db=None) -> dict[str, int]:
    """Insert missing built-ins and maintain shipped system-tier ownership.

    A locked built-in is not operator-owned configuration.  Keeping an old
    published revision forever makes a shipped access-policy repair inert on
    existing installations.  A shipped locked-to-editable transition preserves
    the live policy while handing future revisions to the operator.  Publishing
    a locked security update preserves the immutable
    audit trail; all other existing rows retain insert-only behavior.
    """
    if db is None:
        from app.db import get_app_db
        db = get_app_db()
    created = skipped = 0
    for seed in load_tier_seeds():
        existing = await db.get_experience_tier(seed["id"])
        if not existing:
            existing = await db.get_experience_tier_by_slug(seed["slug"])
        if existing:
            current_revision = int(existing.get("revision") or 1)
            is_locked_system = bool(existing.get("is_system")) and bool(existing.get("is_locked"))
            publisher = getattr(db, "publish_experience_tier", None)
            if is_locked_system and seed["is_system"] and not seed["is_locked"]:
                await db.upsert_experience_tier(
                    str(existing.get("id") or seed["id"]),
                    expected_revision=current_revision,
                    is_locked=False,
                    revision=current_revision + 1,
                    updated_by="system-seed-unlock",
                )
            if seed["slug"] == "anonymous":
                # Re-read after a possible locked→editable transition so the
                # backfill observes the current revision and remains idempotent.
                current = await db.get_experience_tier(str(existing.get("id") or seed["id"]))
                if current:
                    await _backfill_anonymous_tier_controls(db, dict(current))
            if (is_locked_system and seed["is_system"] and seed["is_locked"]
                    and current_revision < seed["revision"] and callable(publisher)):
                await db.upsert_experience_tier(
                    seed["id"], expected_revision=current_revision,
                    slug=seed["slug"], name=seed["name"], description=seed["description"],
                    policy_json=seed["policy"], policy_schema_version=seed["policy_schema_version"],
                    roster_id=seed["roster_id"], is_system=True, is_locked=True,
                    status="draft", revision=seed["revision"], updated_by="system-seed",
                )
                await publisher(
                    seed["id"], actor_user_id="system-seed",
                    expected_revision=seed["revision"], action="system-seed-upgrade",
                )
            skipped += 1
            continue
        publisher = getattr(db, "publish_experience_tier", None)
        await db.upsert_experience_tier(
            seed["id"],
            slug=seed["slug"],
            name=seed["name"],
            description=seed["description"],
            policy_json=seed["policy"],
            policy_schema_version=seed["policy_schema_version"],
            roster_id=seed["roster_id"],
            is_system=seed["is_system"],
            is_locked=seed["is_locked"],
            status="draft" if publisher is not None else seed["status"],
            revision=seed["revision"],
            created_by="system-seed",
            updated_by="system-seed",
        )
        if publisher is not None and seed["status"] == "published":
            await publisher(
                seed["id"], actor_user_id="system-seed",
                expected_revision=seed["revision"], action="system-seed",
            )
        created += 1
    return {"created": created, "skipped": skipped}


async def export_experience_tiers(*, db=None) -> dict[str, Any]:
    """Return portable tier definitions without assignments, audit, or secrets."""
    if db is None:
        from app.db import get_app_db
        db = get_app_db()
    exported: list[dict[str, Any]] = []
    for raw in await db.list_experience_tiers():
        row = dict(raw)
        try:
            policy = json.loads(row.get("policy_json") or "{}")
        except (TypeError, ValueError) as exc:
            raise TierSeedError(f"tier {row.get('id')!r} has unreadable policy JSON") from exc
        policy = normalize_policy(policy)
        schema_version = int(row.get("policy_schema_version") or POLICY_SCHEMA_VERSION)
        roster_id = str(row.get("roster_id") or policy["models"]["roster_id"])
        if schema_version != policy["schema_version"]:
            raise TierSeedError(f"tier {row.get('id')!r} policy schema versions disagree")
        if roster_id != str(policy["models"]["roster_id"]):
            raise TierSeedError(f"tier {row.get('id')!r} roster references disagree")
        exported.append({
            "id": row.get("id"),
            "slug": row.get("slug"),
            "name": row.get("name"),
            "description": row.get("description") or "",
            "status": row.get("status") or "draft",
            "revision": int(row.get("revision") or 1),
            "published_revision": (
                int(row["published_revision"])
                if row.get("published_revision") is not None else None
            ),
            "is_system": bool(row.get("is_system")),
            "is_locked": bool(row.get("is_locked")),
            "policy_schema_version": schema_version,
            "roster_id": roster_id,
            "policy": policy,
        })
    return {"v": 1, "tiers": exported}


async def import_experience_tiers(
    payload: Mapping[str, Any], *, db=None, overwrite: bool = False,
) -> int:
    """Validate and import portable tiers, preserving existing rows by default.

    Publication history and user assignments are intentionally not portable. A
    published imported definition is snapshotted locally as a new immutable
    publication revision.
    """
    if db is None:
        from app.db import get_app_db
        db = get_app_db()
    tiers = payload.get("tiers") if isinstance(payload, Mapping) else None
    if not isinstance(tiers, list):
        raise TierSeedError("experience tier bundle must contain a tiers list")
    applied = 0
    for index, raw in enumerate(tiers):
        if not isinstance(raw, Mapping):
            raise TierSeedError("experience tier bundle entries must be objects")
        tier_id = str(raw.get("id") or "").strip()
        slug = str(raw.get("slug") or tier_id).strip().lower()
        name = str(raw.get("name") or slug).strip()
        if not tier_id or not slug or not name or not _SLUG_RE.fullmatch(slug):
            raise TierSeedError(f"imported tier at index {index} has invalid id, slug, or name")
        status = str(raw.get("status") or "draft").strip().lower()
        if status not in {"draft", "published", "retired"}:
            raise TierSeedError(f"imported tier {tier_id!r} has invalid status")
        policy_raw = raw.get("policy") or raw.get("policy_json") or {}
        if isinstance(policy_raw, str):
            try:
                policy_raw = json.loads(policy_raw)
            except (TypeError, ValueError) as exc:
                raise TierSeedError(
                    f"imported tier {tier_id!r} has unreadable policy JSON"
                ) from exc
        try:
            policy = normalize_policy(policy_raw)
        except PolicyError as exc:
            raise TierSeedError(f"imported tier {tier_id!r} has invalid policy: {exc}") from exc
        schema_version = int(raw.get("policy_schema_version") or POLICY_SCHEMA_VERSION)
        if schema_version != policy["schema_version"]:
            raise TierSeedError(f"imported tier {tier_id!r} policy schema versions disagree")
        roster_id = str(raw.get("roster_id") or policy["models"]["roster_id"]).strip()
        if roster_id != str(policy["models"]["roster_id"]):
            raise TierSeedError(f"imported tier {tier_id!r} roster references disagree")
        if not await db.get_model_roster(roster_id):
            raise TierSeedError(
                f"imported tier {tier_id!r} references missing model roster {roster_id!r}"
            )

        existing_id = await db.get_experience_tier(tier_id)
        existing_slug = await db.get_experience_tier_by_slug(slug)
        existing = existing_id or existing_slug
        if existing and not overwrite:
            continue
        if existing_slug and str(existing_slug.get("id")) != tier_id:
            raise TierSeedError(
                f"imported tier slug {slug!r} belongs to a different stable tier id"
            )

        fields: dict[str, Any] = {
            "slug": slug,
            "name": name,
            "description": str(raw.get("description") or ""),
            "policy_json": policy,
            "policy_schema_version": schema_version,
            "roster_id": roster_id,
            "is_system": bool(raw.get("is_system", False)),
            "is_locked": bool(raw.get("is_locked", False)),
            "updated_by": "bootstrap-import",
        }
        if not existing:
            fields.update({
                "status": "draft" if status == "published" else status,
                "revision": max(1, int(raw.get("revision") or 1)),
                "created_by": "bootstrap-import",
            })
        elif status == "retired":
            fields["status"] = "retired"
        row = await db.upsert_experience_tier(tier_id, **fields)
        if status == "published":
            publish = getattr(db, "publish_experience_tier", None)
            if publish is not None:
                await publish(
                    tier_id, actor_user_id="bootstrap-import",
                    expected_revision=int(row.get("revision") or 1),
                    action="bootstrap-import",
                )
            else:
                await db.upsert_experience_tier(tier_id, status="published")
        applied += 1
    return applied
