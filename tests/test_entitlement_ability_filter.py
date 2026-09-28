import asyncio

import app.agent.ability_access as access
import app.entitlements.abilities as entitlement_abilities


class FakeDB:
    def __init__(self, access_map=None):
        self.access_map = access_map or {}

    async def get_agent_ability_access(self, _agent_id):
        return self.access_map

    async def is_user_admin(self, _user_id):
        return False


def test_anonymous_only_gets_chat_core(monkeypatch):
    async def capabilities(_user_id, **_kwargs):
        return {"ability_groups": ["chat_core"]}

    monkeypatch.setattr(access, "resolve_capabilities", capabilities)
    result = asyncio.run(access.filter_abilities_for_caller(
        "agent", {"base", "web_access", "automation", "diagnostics"}, None, FakeDB()
    ))
    assert result == {"base"}


def test_free_tier_excludes_automation_and_admin(monkeypatch):
    async def capabilities(_user_id, **_kwargs):
        return {"ability_groups": ["chat_core", "web_read"]}

    monkeypatch.setattr(access, "resolve_capabilities", capabilities)
    result = asyncio.run(access.filter_abilities_for_caller(
        "agent", {"base", "web_scraper", "automation", "ui_admin"}, "user-1", FakeDB()
    ))
    assert result == {"base", "web_scraper"}


def test_admin_group_allows_known_and_unknown_admin_abilities(monkeypatch):
    async def capabilities(_user_id, **_kwargs):
        return {"ability_groups": ["platform_admin"]}

    monkeypatch.setattr(access, "resolve_capabilities", capabilities)
    result = asyncio.run(access.filter_abilities_for_caller(
        "agent", {"diagnostics", "new_unreviewed_ability"}, "admin", FakeDB()
    ))
    assert result == {"diagnostics", "new_unreviewed_ability"}


def test_agent_access_map_still_restricts_with_tier(monkeypatch):
    async def capabilities(_user_id, **_kwargs):
        return {"ability_groups": ["chat_core", "web_read"]}

    monkeypatch.setattr(access, "resolve_capabilities", capabilities)
    db = FakeDB({"web_access": "admin"})
    result = asyncio.run(access.filter_abilities_for_caller(
        "agent", {"base", "web_access"}, "user-1", db
    ))
    assert result == {"base"}


def test_entitlement_failure_falls_back_to_chat_core(monkeypatch):
    async def capabilities(_user_id, **_kwargs):
        raise RuntimeError("backend unavailable")

    monkeypatch.setattr(access, "resolve_capabilities", capabilities)
    result = asyncio.run(access.filter_abilities_for_caller(
        "agent", {"base", "github", "automation"}, "user-1", FakeDB()
    ))
    assert result == {"base"}


def test_descriptor_entitlement_group_is_authoritative(monkeypatch):
    monkeypatch.setattr(
        "app.abilities.ability_entry",
        lambda slug: {"entitlement_group": "browser_control"} if slug == "web_access" else None,
    )
    assert entitlement_abilities.ability_group("web_access") == "browser_control"


def test_invalid_descriptor_entitlement_group_fails_restrictive(monkeypatch):
    monkeypatch.setattr(
        "app.abilities.ability_entry",
        lambda _slug: {"entitlement_group": "unreviewed-superuser"},
    )
    assert entitlement_abilities.ability_group("web_access") == "platform_admin"
