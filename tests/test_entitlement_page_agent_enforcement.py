import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api import agents
from app.auth import identity
from app.entitlements.service import invalidate_capabilities, resolve_capabilities
from app.entitlements.tiers import load_tier_seeds


class FakeDB:
    def __init__(self, *, admins=(), assignments=None, templates=None, agents_rows=None):
        self.admins = set(admins)
        self.assignments = assignments or {}
        self.templates = templates or []
        self.agents_rows = agents_rows or []
        self.created = []
        self.tiers = {}
        self.rosters = {}
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

    async def is_user_admin(self, user_id):
        return user_id in self.admins

    async def get_active_user_tier_assignment(self, user_id):
        return self.assignments.get(user_id)

    async def get_experience_tier(self, value):
        return self.tiers.get(value)

    async def get_experience_tier_by_slug(self, value):
        return next((row for row in self.tiers.values() if row.get("slug") == value), None)

    async def get_model_roster(self, value):
        return self.rosters.get(value)

    async def get_model_roster_by_slug(self, value):
        return next((row for row in self.rosters.values() if row.get("slug") == value), None)

    async def list_agent_templates(self, include_admin=False, discoverable_only=False):
        rows = list(self.templates)
        if not include_admin:
            rows = [row for row in rows if row.get("access_level") != "admin_only"]
        if discoverable_only and not include_admin:
            rows = [row for row in rows if row.get("discoverable")]
        return rows

    async def list_agents_for_user(self, user_id, include_admin=False, view="active"):
        return list(self.agents_rows)

    async def create_custom_agent(self, **kwargs):
        row = {
            "id": "new-agent", "source": "custom", "metadata": json.dumps({
                "owner_user_id": kwargs["user_id"],
            }), **kwargs,
        }
        self.created.append(row)
        return row


def request():
    return Request({"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b""})


def run(coro):
    return asyncio.run(coro)


def _catalog():
    def page(page_id):
        return {
            "id": page_id,
            "visibility": "all",
            "required_backend_capability": (
                "role:platform_admin" if page_id in {"admin-tools", "explorer", "instances"}
                else f"page:{page_id}"
            ),
        }
    return {
        "main": [page(page_id) for page_id in (
            "admin-tools", "agents", "automations", "browser", "explorer", "genui", "instances", "wiki"
        )],
        "admin": [page("database"), page("features")],
        "splash": [page("splash-page")],
    }


@pytest.mark.parametrize(
    ("uid", "assignment", "is_admin", "expected_main", "admin_count"),
    [
        ("", None, False, {"agents", "wiki"}, 0),
        ("free-user", None, False, {"agents", "browser", "wiki"}, 0),
        ("pro-user", {"tier_id": "pro", "source": "manual"}, False,
         {"agents", "automations", "browser", "genui", "wiki"}, 0),
        ("admin-user", None, True,
         {"admin-tools", "agents", "automations", "browser", "explorer", "genui", "instances", "wiki"}, 2),
    ],
)
def test_page_catalog_intersects_operator_visibility_and_tier(
    monkeypatch, uid, assignment, is_admin, expected_main, admin_count,
):
    db = FakeDB(
        admins={uid} if is_admin else set(),
        assignments={uid: assignment} if assignment else {},
    )
    invalidate_capabilities()
    monkeypatch.setattr(agents, "get_db", lambda: db)
    monkeypatch.setattr(agents, "request_user_id", lambda _request: uid)
    monkeypatch.setattr("app.ui_pages.reload", lambda: None)
    monkeypatch.setattr("app.ui_pages.ui_catalog", _catalog)
    monkeypatch.setattr("app.admin.page_config.reload", lambda: None)
    monkeypatch.setattr("app.ui_pages.effective_visibility", lambda kind, page_id: "all")
    monkeypatch.setattr(identity, "_is_admin", AsyncMock(side_effect=lambda value: value in db.admins))

    result = run(agents.get_pages_catalog(request()))

    assert {page["id"] for page in result["main"]} == expected_main
    assert len(result["admin"]) == admin_count
    assert [page["id"] for page in result["splash"]] == ["splash-page"]


def test_direct_page_gate_requires_operator_and_tier(monkeypatch):
    db = FakeDB(assignments={"pro-user": {"tier_id": "pro", "source": "manual"}})
    free = run(resolve_capabilities("free-user", db=db, use_cache=False))
    pro = run(resolve_capabilities("pro-user", db=db, use_cache=False))
    monkeypatch.setattr(identity, "_is_admin", AsyncMock(return_value=False))
    monkeypatch.setattr("app.ui_pages.effective_visibility", lambda kind, page_id: "all")

    assert not run(identity.user_may_access_page("free-user", "main", "genui", capabilities=free))
    assert run(identity.user_may_access_page("pro-user", "main", "genui", capabilities=pro))

    monkeypatch.setattr("app.ui_pages.effective_visibility", lambda kind, page_id: "off")
    assert not run(identity.user_may_access_page("pro-user", "main", "genui", capabilities=pro))


def test_direct_instances_page_gate_requires_platform_admin(monkeypatch):
    capabilities = {"pages": {"instances": True}}
    monkeypatch.setattr(identity, "_is_admin", AsyncMock(return_value=False))
    monkeypatch.setattr("app.ui_pages.effective_visibility", lambda kind, page_id: "all")
    monkeypatch.setattr(
        "app.ui_pages.page_entry",
        lambda kind, page_id: {"required_backend_capability": "role:platform_admin"},
    )

    assert not run(identity.user_may_access_page(
        "registered-user", "main", "instances", capabilities=capabilities,
    ))


def test_template_discovery_filters_by_entitlement(monkeypatch):
    templates = [
        {"id": "default", "name": "Default", "discoverable": 1},
        {"id": "premium", "name": "Premium", "discoverable": 1},
        {"id": "ops", "name": "Ops", "access_level": "admin_only"},
    ]
    db = FakeDB(admins={"boss"}, templates=templates)
    monkeypatch.setattr(agents, "get_db", lambda: db)
    monkeypatch.setattr(agents, "assert_caller_is", AsyncMock(side_effect=lambda _request, uid: uid))

    free = run(agents.list_agent_templates(
        request(), user_id="free-user", include_admin=False, discoverable_only=False,
    ))
    admin = run(agents.list_agent_templates(
        request(), user_id="boss", include_admin=True, discoverable_only=False,
    ))

    assert [row["id"] for row in free["templates"]] == ["default", "premium"]
    assert [row["id"] for row in admin["templates"]] == ["default", "premium", "ops"]


def test_create_rejects_disallowed_template(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(agents, "get_db", lambda: db)
    monkeypatch.setattr(agents, "assert_caller_is", AsyncMock(return_value="free-user"))
    req = agents.CreateAgentRequest(user_id="free-user", name="Agent", template_id="premium")

    with pytest.raises(HTTPException) as exc:
        run(agents.create_agent(req, request()))

    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "upgrade_required"
    assert not db.created


def test_anonymous_create_requires_registration_before_materialization(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(agents, "get_db", lambda: db)
    monkeypatch.setattr(agents, "assert_caller_is", AsyncMock(return_value="anon_preview"))
    req = agents.CreateAgentRequest(user_id="anon_preview", name="Preview Agent")

    with pytest.raises(HTTPException) as exc:
        run(agents.create_agent(req, request()))

    assert exc.value.status_code == 403
    assert exc.value.detail == {
        "code": "registration_required",
        "feature": "agent_create",
        "message": "Register or sign in to create and manage agents.",
    }
    assert not db.created


def test_create_uses_the_persisted_update_schema_for_initial_config(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(agents, "get_db", lambda: db)
    monkeypatch.setattr(agents, "assert_caller_is", AsyncMock(return_value="free-user"))
    apply_update = AsyncMock(return_value={
        "agent": {"id": "new-agent", "name": "Configured", "source": "custom"},
    })
    monkeypatch.setattr(agents, "update_agent", apply_update)
    monkeypatch.setattr("app.api.chat.notify_user", AsyncMock())
    req = agents.CreateAgentRequest(
        user_id="free-user",
        name="Configured",
        template_id="default",
        default_execution_mode="plan",
        max_turn_count=7,
        chat_ui={"show_tool_calls": False},
    )

    result = run(agents.create_agent(req, request()))

    assert result["agent"]["id"] == "new-agent"
    update_req = apply_update.await_args.args[1]
    assert isinstance(update_req, agents.UpdateAgentRequest)
    assert update_req.user_id == "free-user"
    assert update_req.default_execution_mode == "plan"
    assert update_req.max_turn_count == 7
    assert update_req.chat_ui == {"show_tool_calls": False}


def test_create_request_schema_is_update_schema_plus_creation_selectors():
    update_fields = set(agents.UpdateAgentRequest.model_fields)
    create_fields = set(agents.CreateAgentRequest.model_fields)

    assert update_fields <= create_fields
    assert create_fields - update_fields == {
        "template_id", "capability_profile", "capability_extensions",
    }
    assert agents.CreateAgentRequest.model_fields["name"].is_required()


def test_create_enforces_owned_agent_limit(monkeypatch):
    db = FakeDB(agents_rows=[{
        "id": "owned", "source": "custom", "status": "active",
        "metadata": json.dumps({"owner_user_id": "free-user"}),
    }, {
        "id": "member", "source": "custom", "status": "active",
        "metadata": json.dumps({"owner_user_id": "someone-else"}),
    }])
    monkeypatch.setattr(agents, "get_db", lambda: db)
    monkeypatch.setattr(agents, "assert_caller_is", AsyncMock(return_value="free-user"))
    req = agents.CreateAgentRequest(user_id="free-user", name="Agent", template_id="default")

    with pytest.raises(HTTPException) as exc:
        run(agents.create_agent(req, request()))

    assert exc.value.status_code == 403
    assert exc.value.detail == {
        "code": "quota_exceeded", "limit": "max_agents", "maximum": 1,
    }
    assert not db.created
