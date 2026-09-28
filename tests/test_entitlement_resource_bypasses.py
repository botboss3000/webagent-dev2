import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.entitlements import resources


def run(coro):
    return asyncio.run(coro)


class ResourceDB:
    def __init__(self):
        self.agents = []
        self.connections = {}

    async def list_agent_templates(self, **_kwargs):
        return [{"id": "default"}, {"id": "public-template"}]

    async def list_agents_for_user(self, _user_id, **_kwargs):
        return list(self.agents)

    async def get_agent_connections(self, agent_id):
        return list(self.connections.get(agent_id, []))


def capabilities(*, max_agents=1, max_connections=1, groups=None):
    return {
        "subject": {"class": "registered", "is_admin": False},
        "features": {"agent_create": True, "connectors": True},
        "agent_templates": ["*"],
        "ability_groups": groups or ["personal_integrations"],
        "limits": {"max_agents": max_agents, "max_connections": max_connections},
    }


def test_shared_agent_gate_blocks_tool_style_materialization_at_quota(monkeypatch):
    db = ResourceDB()
    db.agents = [{
        "id": "a1", "source": "custom", "status": "active",
        "metadata": {"owner_user_id": "u1"},
    }]
    monkeypatch.setattr(resources, "resolve_capabilities", AsyncMock(return_value=capabilities()))

    with pytest.raises(resources.ResourceEntitlementError) as exc:
        run(resources.enforce_agent_materialization(db, "u1", template_id="default"))
    assert exc.value.detail() == {
        "code": "quota_exceeded", "limit": "max_agents", "maximum": 1,
    }


def test_shared_connection_gate_counts_new_enabled_rows_but_allows_update(monkeypatch):
    db = ResourceDB()
    db.agents = [{
        "id": "a1", "source": "custom", "status": "active",
        "metadata": {"owner_user_id": "u1"},
    }]
    db.connections["a1"] = [{"connection_type": "google", "enabled": True}]
    monkeypatch.setattr(resources, "resolve_capabilities", AsyncMock(return_value=capabilities()))

    # Updating the already-enabled row does not consume another slot.
    run(resources.enforce_connection_change(db, "u1", "a1", "google", enabling=True))
    with pytest.raises(resources.ResourceEntitlementError) as exc:
        run(resources.enforce_connection_change(db, "u1", "a1", "slack", enabling=True))
    assert exc.value.limit == "max_connections"


def test_unknown_connection_ability_fails_restrictive(monkeypatch):
    db = ResourceDB()
    monkeypatch.setattr(resources, "resolve_capabilities", AsyncMock(return_value=capabilities()))
    with pytest.raises(resources.ResourceEntitlementError) as exc:
        run(resources.enforce_connection_change(db, "u1", "a1", "new_unreviewed", enabling=True))
    assert exc.value.detail()["resource"] == "ability_group"


def test_oauth_scope_ability_inherits_provider_entitlement_group(monkeypatch):
    async def resolved(*_args, **_kwargs):
        return capabilities(groups={"personal_integrations"})

    monkeypatch.setattr(resources, "resolve_capabilities", resolved)
    run(resources.enforce_ability_group(ResourceDB(), "u1", "google.gmail_read"))


def test_dynamic_tools_are_gated_at_materialization(monkeypatch):
    from app.tools import loader as loader_module

    tool_loader = object.__new__(loader_module.ToolLoader)

    async def rows(_user_id):
        return [{"id": "custom-tool", "name": "custom_exec", "parameters": {"type": "object"}}]

    async def groups(*_args, **_kwargs):
        return {"ability_groups": ["chat_core"]}

    tool_loader._fetch_user_tools = rows
    tool_loader._make_handler = lambda *_args: (lambda: None)
    tool_loader._inject_builtin_tools = lambda *_args, **_kwargs: None

    monkeypatch.setattr("app.integrations.gather_enabled_providers", AsyncMock(return_value=set()))
    monkeypatch.setattr("app.agent.ability_access.filter_abilities_for_caller", AsyncMock(return_value=set()))
    monkeypatch.setattr("app.entitlements.service.resolve_capabilities", groups)
    monkeypatch.setattr("app.integrations.inject_integration_tools", lambda *_args, **_kwargs: None)

    loaded = run(tool_loader.load_tools("u1", agent_id="a1", gate_caller_access=True))
    assert "custom_exec" not in loaded


def test_automation_execution_rechecks_live_entitlement(monkeypatch):
    from app.automation import runner
    from app.automation import entitlements

    class DB:
        async def update_automation(self, _row_id, **fields):
            self.fields = fields

    db = DB()
    denial = entitlements.AutomationEntitlementError(
        "demoted", code="automations_not_allowed",
    )
    monkeypatch.setattr(entitlements, "enforce_automation_entitlement", AsyncMock(side_effect=denial))
    result = run(runner.run_one(db, {
        "id": "auto-1", "agent_id": "agent-1", "owner_user_id": "u1",
    }))
    assert result == {"ok": False, "skipped": "automations_not_allowed"}
    assert db.fields["last_status"] == "skipped"


def test_wiki_direct_route_gate_has_stable_reason(monkeypatch):
    from app.api import wiki

    monkeypatch.setattr(wiki, "caller_may_access_page", AsyncMock(return_value=False))
    with pytest.raises(HTTPException) as exc:
        run(wiki._require_wiki_page(SimpleNamespace()))
    assert exc.value.status_code == 403
    assert exc.value.detail == {"code": "page_access_denied", "page_id": "wiki"}


def test_transcription_requires_verified_identity_before_provider_use(monkeypatch):
    from app.api import transcription

    monkeypatch.setattr(
        "app.auth.identity.assert_caller_is",
        AsyncMock(side_effect=HTTPException(status_code=401, detail="Not authenticated")),
    )
    upload = SimpleNamespace()
    with pytest.raises(HTTPException) as exc:
        run(transcription.transcribe_audio(SimpleNamespace(), upload, "victim", ""))
    assert exc.value.status_code == 401
