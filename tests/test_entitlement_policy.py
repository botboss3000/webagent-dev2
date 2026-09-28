import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from app.entitlements.policy import PolicyError, compose_policy, normalize_policy, system_policy
from app.entitlements.service import invalidate_capabilities, resolve_capabilities
from app.entitlements.tiers import load_tier_seeds, seed_policy


class FakeDB:
    def __init__(self):
        self.admins, self.tiers, self.rosters, self.audit = set(), {}, {}, []
        self.assignment = None
        for seed in load_tier_seeds():
            self.tiers[seed["id"]] = {
                "id": seed["id"], "slug": seed["slug"], "status": "published",
                "revision": seed["revision"], "policy_json": seed["policy"],
            }
            self.rosters[seed["roster_id"]] = {
                "id": seed["roster_id"], "status": "published", "revision": 1,
                "default_entry_id": "standard",
                "entries_json": [{"entry_id": "standard", "provider": "test", "model": "test-model"}],
            }
    async def is_user_admin(self, user_id): return user_id in self.admins
    async def list_user_tier_assignments(self, user_id=None): return [self.assignment] if self.assignment else []
    async def get_experience_tier(self, value): return self.tiers.get(value)
    async def get_experience_tier_by_slug(self, value): return self.tiers.get(value)
    async def get_model_roster(self, value): return self.rosters.get(value)
    async def get_model_roster_by_slug(self, value): return self.rosters.get(value)
    async def append_entitlement_audit_event(self, row): self.audit.append(row)


def run(coro): return asyncio.run(coro)


def test_shipped_policies_are_valid_and_python_only_has_emergency_anonymous():
    assert all(seed_policy(slug)["schema_version"] == 1 for slug in ("anonymous", "pending", "free", "pro"))
    assert seed_policy("pending") == seed_policy("anonymous")
    assert system_policy("anonymous")["limits"]["max_agents"] == 0
    with pytest.raises(PolicyError):
        system_policy("pro")
    assert seed_policy("pro")["models"]["max_byo_entries"] is None


def test_policy_rejects_unknown_fields_and_negative_limits():
    raw = seed_policy("free"); raw["typo_grant"] = True
    with pytest.raises(PolicyError): normalize_policy(raw)
    raw = seed_policy("free"); raw["limits"]["max_agents"] = -1
    with pytest.raises(PolicyError): normalize_policy(raw)
    raw = seed_policy("anonymous"); raw["anonymous"]["identity_messages"] = -1
    with pytest.raises(PolicyError): normalize_policy(raw)
    raw = seed_policy("anonymous"); raw["anonymous"]["typo"] = 1
    with pytest.raises(PolicyError): normalize_policy(raw)


def test_legacy_policy_receives_restrictive_anonymous_defaults_and_composes_down():
    raw = seed_policy("anonymous"); raw.pop("anonymous")
    normalized = normalize_policy(raw)
    assert normalized["anonymous"]["identity_messages"] == 5
    assert normalized["anonymous"]["daily_messages"] == 10
    restricted = seed_policy("anonymous")
    restricted["anonymous"]["identity_messages"] = 2
    assert compose_policy(seed_policy("anonymous"), restrictive_policy=restricted)["anonymous"]["identity_messages"] == 2


def test_admin_overlay_is_explicit_but_installation_can_disable_page():
    policy = compose_policy(seed_policy("free"), is_admin=True,
                            installation_pages={"agents", "admin-tools"})
    assert policy["pages"] == ["admin-tools", "agents"]
    explicit_catalog = compose_policy(seed_policy("free"), is_admin=True,
                                      installation_pages={"agents"})
    assert explicit_catalog["pages"] == ["admin-tools", "agents"]
    assert "platform_admin" in policy["ability_groups"]
    assert policy["limits"]["max_agents"] is None


def test_drop_in_page_id_requires_no_central_python_registration():
    policy = seed_policy("free")
    policy["pages"].append("plugin-reports")
    normalized = normalize_policy(policy)
    assert "plugin-reports" in normalized["pages"]


def test_restrictive_policy_intersects_grants_and_limits():
    policy = compose_policy(seed_policy("pro"), restrictive_policy=seed_policy("free"))
    assert "genui" not in policy["pages"] and "image_generation" not in policy["features"]
    assert policy["limits"]["max_agents"] == 1
    assert policy["models"]["max_reasoning_effort"] == "medium"


def test_registered_defaults_free_and_response_has_no_key():
    db = FakeDB(); invalidate_capabilities()
    result = run(resolve_capabilities("user-1", db=db, use_cache=False))
    assert result["tier"]["slug"] == "free" and result["pages"]["agents"] is True
    assert "api_key" not in json.dumps(result)


def test_anonymous_is_restrictive():
    result = run(resolve_capabilities("anon_123", db=FakeDB(), use_cache=False))
    assert result["tier"]["slug"] == "anonymous"
    assert {page_id for page_id, allowed in result["pages"].items() if allowed} == {"agents", "wiki"}
    assert result["ability_groups"] == []
    assert result["models"]["allow_byo"] is False
    assert "attachments" not in result["features"]
    assert result["limits"]["max_attachment_bytes"] == 0
    assert result["limits"]["max_storage_bytes"] == 0
    assert result["anonymous"]["identity_messages"] == 5


def test_unknown_assignment_fails_down_and_audits():
    db = FakeDB(); db.assignment = {"tier_id": "missing", "source": "manual"}
    result = run(resolve_capabilities("user-1", db=db, use_cache=False))
    assert result["tier"]["slug"] == "anonymous"
    assert result["tier"]["fallback_reason"] == "tier_unknown" and db.audit


def test_missing_default_database_tier_uses_emergency_anonymous():
    db = FakeDB()
    db.tiers.pop("free")
    result = run(resolve_capabilities("user-1", db=db, use_cache=False))
    assert result["tier"]["slug"] == "anonymous"
    assert result["tier"]["source"] == "emergency"
    assert result["tier"]["fallback_reason"] == "tier_missing"
    assert result["limits"]["max_attachment_bytes"] == 0


def test_expired_direct_assignment_is_ignored_for_free_default():
    db = FakeDB()
    db.get_active_user_tier_assignment = AsyncMock(return_value={
        "id": "old", "tier_id": "pro", "source": "billing",
        "expires_at": "2020-01-01T00:00:00+00:00",
    })
    result = run(resolve_capabilities("user-1", db=db, use_cache=False))
    assert result["tier"]["slug"] == "free"
    assert result["assignment"] is None


def test_admin_overlay_preserves_underlying_free_tier():
    db = FakeDB(); db.admins.add("boss")
    result = run(resolve_capabilities("boss", db=db, use_cache=False))
    assert result["tier"]["slug"] == "free" and result["subject"]["is_admin"] is True
    assert result["pages"]["admin-tools"] is True


def test_roster_expands_safe_ids_without_leaking_entries():
    db = FakeDB(); db.rosters["roster-free"] = {
        "id": "roster-free", "status": "published", "revision": 4,
        "default_entry_id": "cheap",
        "entries_json": json.dumps([
            {"entry_id": "cheap", "api_key": "nope", "provider": "vendor",
             "model": "small", "label": "Small", "base_url": "https://user:pass@example.test/v1?token=nope"},
            {"entry_id": "vision", "model": "vision", "image_capable": True},
        ]),
    }
    result = run(resolve_capabilities("user-1", db=db, use_cache=False))
    assert result["models"]["allowed_entry_ids"] == ["cheap", "vision"]
    assert "nope" not in json.dumps(result)
    assert result["models"]["default_entry_id"] == "cheap"
    assert result["models"]["entries"][0]["base_url"] == "https://example.test/v1"
    first_revision = result["evaluation"]["revision"]
    db.rosters["roster-free"]["revision"] = 5
    changed = run(resolve_capabilities("user-1", db=db, use_cache=False))
    assert changed["evaluation"]["revision"] != first_revision


def test_stable_tier_id_and_slug_are_distinct_and_assignment_is_reported():
    db = FakeDB()
    policy = seed_policy("pro")
    db.tiers["tier-uuid"] = {
        "id": "tier-uuid", "slug": "professional", "status": "published",
        "revision": 7, "policy_json": policy,
    }
    db.assignment = {
        "id": "assignment-1", "tier_id": "tier-uuid", "source": "billing",
        "starts_at": "2026-01-01T00:00:00+00:00", "expires_at": "2099-01-01T00:00:00+00:00",
        "reason": "active subscription",
    }
    result = run(resolve_capabilities("user-1", db=db, use_cache=False))
    assert result["tier"]["id"] == "tier-uuid"
    assert result["tier"]["slug"] == "professional"
    assert result["assignment"]["id"] == "assignment-1"
    assert result["evaluation"]["revision"]


def test_unpublished_roster_is_explicitly_unavailable():
    db = FakeDB()
    db.rosters["roster-free"]["status"] = "retired"
    result = run(resolve_capabilities("user-1", db=db, use_cache=False))
    assert result["models"]["available"] is False
    assert result["models"]["entries"] == []
    assert result["models"]["fallback_reason"] == "model_roster_not_published"
