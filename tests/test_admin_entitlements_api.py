from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.admin import entitlements as api
from app.admin.users import _entitlement_summaries
from app.db.local import LocalBackend
from app.entitlements.tiers import seed_policy


@pytest.fixture
def app_db():
    with tempfile.TemporaryDirectory() as directory:
        yield LocalBackend(db_path=str(Path(directory) / "app.db"), seed=False, plane="app")


@pytest.fixture
def admin_api(monkeypatch, app_db):
    async def verified_admin(_claimed):
        return "verified-admin"

    monkeypatch.setattr(api, "resolve_admin_uid", verified_admin)
    monkeypatch.setattr(api, "get_app_db", lambda: app_db)
    return app_db


def admin_request(**values):
    return {"requesting_user_id": "untrusted-client-value", **values}


def run(coro):
    return asyncio.run(coro)


def test_admin_gate_rejects_unverified_caller(monkeypatch, app_db):
    async def no_admin(_claimed):
        return None

    monkeypatch.setattr(api, "resolve_admin_uid", no_admin)
    monkeypatch.setattr(api, "get_app_db", lambda: app_db)
    with pytest.raises(HTTPException) as exc:
        run(api.list_rosters("admin"))
    assert exc.value.status_code == 403


def test_editor_schema_is_derived_from_installed_descriptors(admin_api):
    schema = run(api.entitlement_schema("admin"))
    page_ids = {page["id"] for page in schema["pages"]}
    assert {"agents", "instances"}.issubset(page_ids)
    assert "max_agents" in schema["limits"]
    assert schema["limits"]["max_agents"]["unit"] == "agents"
    assert "chat_core" in schema["ability_groups"]


def test_roster_tier_publish_assignment_and_audit(admin_api, monkeypatch):
    invalidations = []
    monkeypatch.setattr(api, "invalidate_capabilities", lambda user_id=None: invalidations.append(user_id))

    roster = run(api.create_roster(api.RosterCreateRequest(**admin_request(
        id="roster-free", slug="roster-free", name="Free roster",
        entries=[{"entry_id": "small", "provider": "openai", "model": "small"}],
        default_entry_id="small",
    ))))
    assert roster["status"] == "draft"
    roster = run(api.publish_roster("roster-free", api.AdminRequest(**admin_request())))
    assert roster["status"] == "published"

    policy = seed_policy("free")
    policy["models"]["allowed_entry_ids"] = ["small"]
    tier = run(api.create_tier(api.TierCreateRequest(**admin_request(
        id="free", slug="free", name="Free", policy=policy,
        roster_id="roster-free",
    ))))
    assert tier["status"] == "draft" and tier["is_system"] is False
    tier = run(api.publish_tier("free", api.AdminRequest(**admin_request())))
    assert tier["status"] == "published"

    assignment = run(api.assign_user_tier("user-1", api.AssignmentCreateRequest(**admin_request(
        tier_id="free", reason="support grant",
    ))))
    assert assignment["assigned_by"] == "verified-admin"
    assert assignment["reason"] == "support grant"
    assert invalidations[-1] == "user-1"

    events = run(admin_api.list_entitlement_audit_events(subject_user_id="user-1"))
    assert events[0]["action"] == "assignment.created"
    assert events[0]["actor_user_id"] == "verified-admin"


def test_assign_tier_synchronizes_pending_approval_state(admin_api, monkeypatch):
    """Pending retains awaiting approval; every other tier promotes the account."""
    import types
    monkeypatch.setattr(api, "invalidate_capabilities", lambda user_id=None: None)

    approvals = []
    state = {"is_approved": False}

    async def fake_get_user_by_id(user_id):
        return types.SimpleNamespace(username="pat", is_approved=state["is_approved"])

    async def fake_set_user_approval(username, is_approved):
        state["is_approved"] = is_approved
        approvals.append((username, is_approved))
        return None

    monkeypatch.setattr("app.auth.users.get_user_by_id", fake_get_user_by_id)
    monkeypatch.setattr("app.auth.users.set_user_approval", fake_set_user_approval)

    for roster_id in ("roster-free", "roster-anonymous"):
        run(api.create_roster(api.RosterCreateRequest(**admin_request(
            id=roster_id, slug=roster_id, name=roster_id,
            entries=[{"entry_id": "s", "provider": "openai", "model": "small"}],
            default_entry_id="s",
        ))))
        run(api.publish_roster(roster_id, api.AdminRequest(**admin_request())))

    for tier_id in ("free", "pending"):
        policy = seed_policy(tier_id)
        if tier_id == "free":
            policy["models"]["allowed_entry_ids"] = ["s"]
        run(api.create_tier(api.TierCreateRequest(**admin_request(
            id=tier_id, slug=tier_id, name=tier_id.title(), policy=policy,
            roster_id=policy["models"]["roster_id"],
        ))))
        run(api.publish_tier(tier_id, api.AdminRequest(**admin_request())))

    run(api.assign_user_tier("pending-user", api.AssignmentCreateRequest(**admin_request(
        tier_id="free", reason="promote",
    ))))
    run(api.assign_user_tier("pending-user", api.AssignmentCreateRequest(**admin_request(
        tier_id="pending", reason="return to review",
    ))))
    run(api.assign_user_tier("pending-user", api.AssignmentCreateRequest(**admin_request(
        tier_id="pending", reason="remain in review",
    ))))

    assert approvals == [("pat", True), ("pat", False)]
    assert state["is_approved"] is False


def test_roster_api_rejects_and_never_returns_secrets(admin_api):
    with pytest.raises(HTTPException) as exc:
        run(api.create_roster(api.RosterCreateRequest(**admin_request(
            slug="unsafe", name="Unsafe",
            entries=[{"entry_id": "one", "model": "m", "api_key": "super-secret"}],
            default_entry_id="one",
        ))))
    assert exc.value.status_code == 422

    run(admin_api.upsert_model_roster(
        "legacy", slug="legacy", name="Legacy", status="draft",
        entries_json=[{"entry_id": "one", "model": "m", "api_key": "super-secret"}],
        default_entry_id="one",
    ))
    response = run(api.list_rosters("admin"))
    encoded = json.dumps(response)
    assert "super-secret" not in encoded and "api_key" not in encoded
    assert response["rosters"][0]["credential_configured"] is False


def test_referenced_resources_cannot_be_deleted(admin_api):
    run(admin_api.upsert_model_roster(
        "roster", slug="roster", name="Roster", status="published",
        entries_json=[{"entry_id": "one", "model": "m"}], default_entry_id="one",
    ))
    policy = seed_policy("free")
    policy["models"]["roster_id"] = "roster"
    run(admin_api.upsert_experience_tier(
        "tier", slug="tier", name="Tier", status="published",
        roster_id="roster", policy_json=policy,
    ))
    with pytest.raises(HTTPException) as roster_exc:
        run(api.delete_roster("roster", "admin"))
    assert roster_exc.value.status_code == 409

    run(admin_api.upsert_user_tier_assignment(
        "assignment", user_id="user-1", tier_id="tier", source="manual",
    ))
    with pytest.raises(HTTPException) as tier_exc:
        run(api.delete_tier("tier", "admin"))
    assert tier_exc.value.status_code == 409


def test_publish_validation_rejects_missing_default(admin_api):
    run(admin_api.upsert_model_roster(
        "bad", slug="bad", name="Bad", status="draft",
        entries_json=[{"entry_id": "one", "model": "m"}],
    ))
    with pytest.raises(HTTPException) as exc:
        run(api.publish_roster("bad", api.AdminRequest(**admin_request())))
    assert exc.value.status_code == 422


def test_unlocked_system_tier_can_be_drafted_but_not_deleted(admin_api):
    run(admin_api.upsert_model_roster(
        "system-roster", slug="system-roster", name="System roster", status="draft",
    ))
    policy = seed_policy("free")
    policy["models"]["roster_id"] = "system-roster"
    run(admin_api.upsert_experience_tier(
        "system-free", slug="system-free", name="System Free", status="draft",
        roster_id="system-roster", policy_json=policy, is_system=True, is_locked=False,
    ))
    updated = run(api.update_tier(
        "system-free", api.TierUpdateRequest(**admin_request(name="Admin draft")),
    ))
    assert updated["name"] == "Admin draft"
    with pytest.raises(HTTPException) as exc:
        run(api.delete_tier("system-free", "admin"))
    assert exc.value.status_code == 409


def test_published_roster_stays_live_during_draft_and_supports_preview_history_rollback(admin_api):
    created = run(api.create_roster(api.RosterCreateRequest(**admin_request(
        id="versioned", slug="versioned", name="Versioned",
        entries=[{"entry_id": "one", "provider": "openai", "model": "one"}],
        default_entry_id="one",
    ))))
    published = run(api.publish_roster(
        "versioned", api.RosterActionRequest(**admin_request(expected_revision=created["draft_revision"]))
    ))
    assert published["published_revision"] == published["draft_revision"]

    draft = run(api.update_roster("versioned", api.RosterUpdateRequest(**admin_request(
        entries=[{"entry_id": "two", "provider": "openai", "model": "two"}],
        default_entry_id="two",
    ))))
    assert draft["status"] == "published" and draft["has_draft"] is True
    live = run(admin_api.get_published_model_roster("versioned"))
    assert "one" in live["entries_json"] and "two" not in live["entries_json"]

    preview = run(api.preview_roster("versioned", api.AdminRequest(**admin_request())))
    assert preview["validation"]["valid"] is True
    assert "entries" in preview["diff"]
    history = run(api.roster_history("versioned", "admin"))
    assert history["revisions"][0]["revision"] == published["published_revision"]

    republished = run(api.publish_roster(
        "versioned", api.RosterActionRequest(**admin_request(expected_revision=draft["draft_revision"]))
    ))
    rolled = run(api.rollback_roster("versioned", api.RosterRollbackRequest(**admin_request(
        revision=published["published_revision"], reason="restore known-good",
    ))))
    assert rolled["published_revision"] > republished["published_revision"]
    live = run(admin_api.get_published_model_roster("versioned"))
    assert "one" in live["entries_json"]


def test_published_tier_stays_live_during_draft_and_supports_impact_history_rollback(
    admin_api, monkeypatch,
):
    invalidations = []
    monkeypatch.setattr(api, "invalidate_capabilities", lambda user_id=None: invalidations.append(user_id))
    run(admin_api.upsert_model_roster(
        "tier-roster", slug="tier-roster", name="Tier roster", status="draft",
        entries_json=[{"entry_id": "small", "provider": "openai", "model": "small"}],
        default_entry_id="small",
    ))
    run(admin_api.publish_model_roster("tier-roster", actor_user_id="admin"))
    policy = seed_policy("free")
    policy["models"]["roster_id"] = "tier-roster"
    policy["models"]["allowed_entry_ids"] = ["small"]
    created = run(api.create_tier(api.TierCreateRequest(**admin_request(
        id="versioned-tier", slug="versioned-tier", name="Versioned tier",
        policy=policy, roster_id="tier-roster",
    ))))
    published = run(api.publish_tier(
        "versioned-tier", api.TierActionRequest(**admin_request(
            expected_revision=created["draft_revision"], reason="launch",
        )),
    ))
    changed_policy = json.loads(json.dumps(policy))
    changed_policy["features"].remove("model_picker")
    draft = run(api.update_tier(
        "versioned-tier", api.TierUpdateRequest(**admin_request(
            policy=changed_policy, expected_revision=published["draft_revision"],
        )),
    ))
    assert draft["status"] == "published" and draft["has_draft"] is True
    live = run(admin_api.get_published_experience_tier("versioned-tier"))
    assert "model_picker" in json.loads(live["policy_json"])["features"]

    run(admin_api.upsert_user_tier_assignment(
        "tier-impact", user_id="affected-user", tier_id="versioned-tier", source="manual",
    ))
    preview = run(api.preview_tier("versioned-tier", api.AdminRequest(**admin_request())))
    assert preview["validation"]["valid"] is True
    assert preview["impact"] == {"user_count": 1, "user_ids": ["affected-user"]}
    history = run(api.tier_history("versioned-tier", "admin"))
    assert history["revisions"][0]["revision"] == published["published_revision"]

    republished = run(api.publish_tier(
        "versioned-tier", api.TierActionRequest(**admin_request(
            expected_revision=draft["draft_revision"], reason="publish draft",
        )),
    ))
    rolled = run(api.rollback_tier(
        "versioned-tier", api.TierRollbackRequest(**admin_request(
            revision=published["published_revision"], reason="restore",
        )),
    ))
    assert rolled["published_revision"] > republished["published_revision"]
    assert invalidations
    events = run(admin_api.list_entitlement_audit_events(entity_id="versioned-tier"))
    assert {event["action"] for event in events} >= {
        "tier.created", "tier.updated", "tier.published", "tier.rolled_back",
    }


def test_base_roster_publishes_without_legacy_default_and_activates_only_base(
    admin_api, monkeypatch,
):
    from app.admin import settings as admin_settings

    run(admin_api.upsert_model_roster(
        "roster-admin", slug="roster-admin", name="Base — Admin only", status="draft",
        entries_json=[{
            "entry_id": "standard", "provider": "openai_subscription",
            "auth_mode": "chatgpt", "base_url": "codex://chatgpt",
            "model": "gpt-standard", "enabled": True, "text_capable": True,
        }],
        default_entry_id=None,
    ))
    persisted = {}

    async def load_active(_roster_id, *, db=None):
        return {
            "provider": "openai_subscription", "auth_mode": "chatgpt",
            "base_url": "codex://chatgpt", "model": "gpt-standard",
            "multi_providers": [{"entry_id": "standard", "enabled": True}],
            "providers": {},
        }

    async def load_existing(_actor):
        return {"model_directives": {"standard": "keep"}, "providers": {"saved": {"model": "m"}}}

    async def persist(_actor, config, *, sync_named_rosters=True):
        persisted.update(config)
        persisted["sync_named_rosters"] = sync_named_rosters

    monkeypatch.setattr(api, "load_platform_roster_config", load_active)
    monkeypatch.setattr(admin_settings, "_load_own_llm_config", load_existing)
    monkeypatch.setattr(admin_settings, "_persist_llm_config", persist)

    published = run(api.publish_roster(
        "roster-admin", api.RosterActionRequest(**admin_request(expected_revision=1)),
    ))

    assert published["status"] == "published"
    assert persisted["model"] == "gpt-standard"
    assert persisted["model_directives"] == {"standard": "keep"}
    assert persisted["providers"]["saved"] == {"model": "m"}
    assert persisted["sync_named_rosters"] is False


def test_base_roster_rollback_activates_and_retirement_is_rejected(admin_api, monkeypatch):
    from app.admin import settings as admin_settings

    activated = []

    async def load_active(_roster_id, *, db=None):
        return {"provider": "openai", "model": "first", "providers": {}}

    async def load_existing(_actor): return {"providers": {}}
    async def persist(_actor, config, *, sync_named_rosters=True): activated.append((config, sync_named_rosters))

    monkeypatch.setattr(api, "load_platform_roster_config", load_active)
    monkeypatch.setattr(admin_settings, "_load_own_llm_config", load_existing)
    monkeypatch.setattr(admin_settings, "_persist_llm_config", persist)
    run(admin_api.upsert_model_roster(
        "roster-admin", slug="roster-admin", name="Base — Admin only", status="draft",
        entries_json=[{"entry_id": "one", "provider": "openai", "model": "first", "enabled": True}],
    ))
    run(api.publish_roster("roster-admin", api.RosterActionRequest(**admin_request(expected_revision=1))))
    changed = run(api.update_roster("roster-admin", api.RosterUpdateRequest(**admin_request(
        entries=[{"entry_id": "two", "provider": "openai", "model": "second", "enabled": True}],
    ))))
    republished = run(api.publish_roster("roster-admin", api.RosterActionRequest(**admin_request(
        expected_revision=changed["draft_revision"],
    ))))

    rolled = run(api.rollback_roster("roster-admin", api.RosterRollbackRequest(**admin_request(
        revision=1, reason="restore base",
    ))))
    assert rolled["published_revision"] > republished["published_revision"]
    assert activated and activated[-1][1] is False

    with pytest.raises(HTTPException) as exc:
        run(api.retire_roster("roster-admin", api.RosterActionRequest(**admin_request(reason="no"))))
    assert exc.value.status_code == 409
    assert "cannot be retired" in str(exc.value.detail)


def test_non_base_roster_can_retire_with_revision_guard(admin_api):
    run(admin_api.upsert_model_roster(
        "retire-safe", slug="retire-safe", name="Retire safe", status="draft",
        entries_json=[{"entry_id": "one", "provider": "openai", "model": "safe", "enabled": True}],
    ))
    current = run(api.retire_roster("retire-safe", api.RosterActionRequest(**admin_request(
        expected_revision=1, reason="lifecycle test",
    ))))
    assert current["status"] == "retired"
    with pytest.raises(HTTPException) as exc:
        run(api.retire_roster("retire-safe", api.RosterActionRequest(**admin_request(
            expected_revision=1, reason="stale",
        ))))
    assert exc.value.status_code == 409


def test_named_roster_credential_api_returns_only_state(admin_api, monkeypatch):
    run(admin_api.upsert_model_roster(
        "credentials", slug="credentials", name="Credentials", status="draft",
        entries_json=[{"entry_id": "one", "model": "m"}], default_entry_id="one",
    ))
    state = {}

    async def read_states(_roster_id, *, db=None):
        return dict(state)

    async def set_secret(_roster_id, entry_id, credential, *, db=None):
        assert credential == "super-secret"
        state[entry_id] = True
        return dict(state)

    async def delete_secret(_roster_id, entry_id, *, db=None):
        state.pop(entry_id, None)
        return dict(state)

    monkeypatch.setattr(api, "roster_credential_states", read_states)
    monkeypatch.setattr(api, "set_roster_entry_credential", set_secret)
    monkeypatch.setattr(api, "delete_roster_entry_credential", delete_secret)

    response = run(api.put_roster_credential(
        "credentials", "one", api.RosterCredentialRequest(**admin_request(
            credential="super-secret", reason="configure provider",
        )),
    ))
    assert response["credential_state_by_entry"] == {"one": "configured"}
    assert "super-secret" not in json.dumps(response)
    response = run(api.delete_roster_credential("credentials", "one", "admin", "rotate"))
    assert response["credential_state_by_entry"] == {"one": "missing"}


def test_users_summary_reports_tier_source_and_expiry(app_db):
    run(app_db.upsert_model_roster("r", slug="r", name="R", status="published"))
    run(app_db.upsert_experience_tier(
        "free", slug="free", name="Free", roster_id="r", status="published",
    ))
    run(app_db.upsert_experience_tier(
        "pro", slug="pro", name="Professional", roster_id="r", status="published",
    ))
    run(app_db.upsert_user_tier_assignment(
        "billing", user_id="person", tier_id="pro", source="billing",
        starts_at="2026-01-01T00:00:00+00:00", expires_at="2099-01-01T00:00:00+00:00",
    ))
    run(app_db.upsert_user_tier_assignment(
        "manual", user_id="person", tier_id="free", source="manual",
        starts_at="2026-01-02T00:00:00+00:00", expires_at="2098-01-01T00:00:00+00:00",
    ))
    summary = run(_entitlement_summaries(app_db))["person"]
    assert summary["tier_slug"] == "free"
    assert summary["tier_name"] == "Free"
    assert summary["tier_source"] == "manual"
    assert summary["tier_expires_at"] == "2098-01-01T00:00:00+00:00"
