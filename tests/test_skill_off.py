"""Off is a skill availability boundary, independent of tool visibility."""
import asyncio
import json
from unittest.mock import AsyncMock

from app.tools.tool_modes import normalize_skill_mode, normalize_visibility, resolve_skill_mode
from app.agent.ability_skills import ability_skill_payload


def test_skill_off_and_legacy_fallback():
    assert normalize_skill_mode("OFF") == "off"
    assert normalize_visibility("off") is None
    assert resolve_skill_mode("demo:core", {"demo": "off"}) == "off"
    assert resolve_skill_mode("demo:core", {"demo": "off", "demo:core": "visible"}) == "visible"
    assert resolve_skill_mode("demo:other", {"demo": "off"}) == "visible"
    assert resolve_skill_mode("demo:other", {"demo:other": "off"}) == "off"


def test_payload_withholds_root_but_keeps_tool_skills(monkeypatch):
    from app import abilities
    monkeypatch.setattr(abilities, "ability_feature_with_skill", lambda aid: {
        "id": aid, "skill": "ROOT SECRET", "skill_tool_sections": {"example": "TOOL GUIDE"}})
    payload = ability_skill_payload("demo", ["example"], {"demo:core": "off"})
    assert payload["body"] == ""
    assert payload["handle"] is None
    assert payload["summary"] == ""
    assert payload["deferred"] == "TOOL GUIDE"


def test_collection_and_direct_load_exclude_off(monkeypatch):
    from app import abilities, integrations, db
    from app.agent.ability_skills import collect_ability_skills
    from app.tools.core_tools import list_skills, load_skill
    store = AsyncMock()
    store.get_agent_skill_modes.return_value = {"demo": "off", "demo:extra": "off"}
    store.get_agent_soft_abilities.return_value = []
    store.get_agent_skills.return_value = []
    monkeypatch.setattr(db, "get_db", lambda: store)
    monkeypatch.setattr(integrations, "gather_enabled_providers", AsyncMock(return_value={"demo"}))
    monkeypatch.setattr(integrations, "_discover_tool_specs", lambda: [])
    monkeypatch.setattr("app.agent.ability_skills._agent_ability_settings", AsyncMock(return_value={}))
    monkeypatch.setattr(abilities, "ability_feature_with_skill", lambda aid: {"id": aid, "skill": "SECRET"})
    monkeypatch.setattr(abilities, "ability_general_skills", lambda aid: [
        {"skill_id": sid, "skill_key": f"demo:{sid}", "body": "SECRET"} for sid in ["core", "extra"]])
    monkeypatch.setattr(abilities, "all_raw", lambda: {})
    assert asyncio.run(collect_ability_skills("a")) == []
    assert json.loads(asyncio.run(list_skills(agent_id="a", db=store)))["count"] == 0
    result = json.loads(asyncio.run(load_skill("demo", agent_id="a", db=store)))
    assert result["status"] == "error"
    assert "SECRET" not in str(result)


def test_bundled_skills_default_visible():
    from app.abilities import all_raw, ability_general_skills
    assert resolve_skill_mode("demo:core", {}) == "visible"
    assert resolve_skill_mode("demo:core", {"demo": "discoverable"}) == "discoverable"
    for aid in all_raw():
        for skill in ability_general_skills(aid):
            assert resolve_skill_mode(skill["skill_key"], {}, skill.get("default_mode")) == "visible", skill["skill_key"]
