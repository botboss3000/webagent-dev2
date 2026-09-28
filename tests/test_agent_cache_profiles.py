from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile

from app.agent.cache_profiles import (
    PROFILE_ABILITIES,
    ordered_ability_ids,
    profile_abilities,
    profile_from_metadata,
    profile_layer_blocks,
    with_cache_profile,
)
from app.agent.prompts import append_skills_section, build_system_prompt_parts
from app.agent.loop import _build_layered_messages, _filter_history_for_available_tools
from app.db.local import LocalBackend
from app.tools.tool_modes import render_ability_index


def run(coro):
    return asyncio.run(coro)


def test_profiles_are_nested_prefixes():
    simple = PROFILE_ABILITIES["simple"]
    standard = PROFILE_ABILITIES["standard"]
    advanced = PROFILE_ABILITIES["advanced"]

    assert standard[: len(simple)] == simple
    assert advanced[: len(standard)] == standard
    assert profile_abilities("advanced") == list(advanced)


def test_profile_layers_are_exact_nested_message_prefixes():
    simple = profile_layer_blocks("simple")
    standard = profile_layer_blocks("standard")
    advanced = profile_layer_blocks("advanced")
    assert standard[: len(simple)] == simple
    assert advanced[: len(standard)] == standard


def test_profile_layers_can_hide_every_forbidden_ability():
    assert profile_layer_blocks("advanced", allowed_abilities=set()) == []
    blocks = profile_layer_blocks("advanced", allowed_abilities={"web_access"})
    assert len(blocks) == 1
    assert "web_access" in blocks[0]
    assert "codebase_admin" not in blocks[0]


def test_profile_order_is_stable_and_extensions_follow_profile():
    unordered = {
        "automation",
        "visualizer",
        "web_access",
        "browser_control",
        "email",
    }
    ordered = ordered_ability_ids(unordered)
    assert ordered[:4] == [
        "web_access",
        "visualizer",
        "browser_control",
        "automation",
    ]
    assert ordered[-1] == "email"


def test_ability_index_ignores_message_ranking_for_shared_cache_block():
    entries = [
        {"id": "automation", "name": "Automation", "desc": "Schedule work."},
        {"id": "web_access", "name": "Web Access", "desc": "Search the web."},
        {"id": "visualizer", "name": "Visualizer", "desc": "Render a UI."},
    ]
    first = render_ability_index(entries, order=["automation", "visualizer", "web_access"])
    second = render_ability_index(entries, order=["visualizer", "web_access", "automation"])
    assert first == second
    assert first.index("`web_access`") < first.index("`visualizer`") < first.index("`automation`")


def test_prompt_parts_keep_brain_context_out_of_shared_prefix(monkeypatch):
    monkeypatch.setattr(
        "app.admin.settings.get_global_system_prompt",
        lambda: "GLOBAL CORE",
    )
    parts = run(build_system_prompt_parts(
        [{"context_type": "agent", "content": "AGENT PERSONA"}],
        brain_context="USER MEMORY",
    ))
    assert parts.shared_core == "GLOBAL CORE"
    assert "AGENT PERSONA" in parts.agent_context
    assert "USER MEMORY" in parts.turn_context
    assert "USER MEMORY" not in parts.shared_core


def test_anonymous_prompt_excludes_shared_agent_docs_skills_and_memory():
    docs = [{"context_type": "agent", "content": "omnipotent codebase admin"}]
    parts = run(build_system_prompt_parts(
        docs,
        brain_context="private memory",
        user_id="anon_test",
        agent_id="shared_default",
    ))
    rendered = parts.render().lower()
    assert "helpful conversational assistant" in rendered
    assert "codebase" not in rendered
    assert "private memory" not in rendered
    assert run(append_skills_section(
        parts.agent_context,
        {"id": "shared_default"},
        caller_user_id="anon_test",
    )) == parts.agent_context


def test_layered_messages_put_turn_context_after_history():
    messages = _build_layered_messages(
        shared_system="PLATFORM",
        capability_parts=["SIMPLE", "STANDARD"],
        agent_system="PERSONA",
        turn_parts=["MEMORY", "ROUTING HINT"],
        history=[
            {"role": "user", "content": "earlier"},
            {"role": "assistant", "content": "reply"},
        ],
        user_message="current",
    )
    assert [m["role"] for m in messages] == [
        "system", "system", "system", "system", "user", "assistant", "system", "user",
    ]
    assert messages[0]["content"] == "PLATFORM"
    assert messages[1]["content"] == "SIMPLE"
    assert messages[2]["content"] == "STANDARD"
    assert messages[-2]["content"] == "MEMORY\n\nROUTING HINT"


def test_history_drops_forbidden_tool_calls_and_their_advertising_prose():
    history = [
        {"role": "user", "content": "write a file"},
        {"role": "assistant", "content": "I will load coding.", "tool_calls": [
            {"id": "forbidden", "type": "function", "function": {"name": "load_skill", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "forbidden", "name": "load_skill", "content": "Codebase Admin"},
        {"role": "assistant", "content": "I cannot perform that action."},
    ]
    filtered = _filter_history_for_available_tools(history, {"calculate"})
    assert filtered == [history[0], history[-1]]


def test_history_keeps_canonical_nameless_tool_results_for_allowed_calls():
    history = [
        {"role": "user", "content": "find the wiki"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call-1", "type": "function", "function": {
                "name": "search_source", "arguments": '{"pattern":"Wiki"}',
            }},
            {"id": "call-2", "type": "function", "function": {
                "name": "search_source", "arguments": '{"pattern":"sidebar"}',
            }},
        ]},
        # Canonical OpenAI tool messages have no name; the call id binds each
        # result to the named function in the assistant message above.
        {"role": "tool", "tool_call_id": "call-1", "content": "first result"},
        {"role": "tool", "tool_call_id": "call-2", "content": "second result"},
    ]

    filtered = _filter_history_for_available_tools(history, {"search_source"})

    assert filtered == history


def test_custom_agent_profile_persists_metadata_and_exact_abilities():
    with tempfile.TemporaryDirectory() as tmp:
        db = LocalBackend(db_path=f"{tmp}/cache-profile.db")
        agent = run(db.create_custom_agent(
            user_id="user-1",
            name="Standard Agent",
            template_id="none",
            seed_abilities=False,
            capability_profile="standard",
            capability_extensions=["browser_control"],
        ))

        metadata = json.loads(agent["metadata"])
        assert profile_from_metadata(metadata) == "standard"
        assert metadata["discovery_default"] == "discoverable"
        assert metadata["capability_extensions"] == ["browser_control"]

        conn = sqlite3.connect(f"{tmp}/cache-profile.db")
        rows = conn.execute(
            """SELECT connection_type FROM agent_connections
               WHERE agent_id = ? AND section = 'ability' AND enabled = 1""",
            (agent["id"],),
        ).fetchall()
        conn.close()
        enabled = {row[0] for row in rows}
        assert enabled == set(PROFILE_ABILITIES["standard"]) | {"browser_control"}


def test_catalog_seed_uses_shared_default_only_when_user_has_no_explicit_default(monkeypatch):
    from app.api.agents import _catalog_seed_for_new_agent

    async def own_config(_user_id):
        return {}

    async def entitled_config(_user_id, *, db):
        return {"multi_providers": [{
            "entry_id": "catalog-flash", "provider": "deepseek", "model": "flash",
        }]}

    class Db:
        async def get_agent_by_id(self, agent_id):
            assert agent_id == "shared_default"
            return {"metadata": {"llm_config": {
                "multi_providers": [{
                    "entry_id": "shared-flash", "provider": "deepseek", "model": "flash",
                }],
                "role_assignments": {"standard": "shared-flash"},
            }}}

    monkeypatch.setattr("app.admin.settings._load_own_llm_config", own_config)
    monkeypatch.setattr("app.entitlements.rosters.resolve_platform_roster_config", entitled_config)

    seed = run(_catalog_seed_for_new_agent(Db(), "user-a"))
    assert seed == {
        "multi_providers": [{"entry_id": "catalog-flash"}],
        "role_assignments": {"standard": "catalog-flash"},
    }


def test_default_webagent_is_provisioned_as_exact_advanced_profile():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = f"{tmp}/default-profile.db"
        db = LocalBackend(db_path=db_path)
        agent = run(db.create_agent_for_user("new-user"))

        metadata = json.loads(agent["metadata"])
        assert profile_from_metadata(metadata) == "advanced"
        assert metadata["pre_enabled_connections"] == list(PROFILE_ABILITIES["advanced"])

        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            """SELECT connection_type FROM agent_connections
               WHERE agent_id = ? AND section = 'ability' AND enabled = 1""",
            (agent["id"],),
        ).fetchall()
        conn.close()
        assert {row[0] for row in rows} == set(PROFILE_ABILITIES["advanced"])


def test_default_profile_metadata_is_advanced_and_discoverable():
    metadata = with_cache_profile({"pre_enabled_connections": ["*"]}, "advanced")
    assert metadata["capability_profile"] == "advanced"
    assert metadata["discovery_default"] == "discoverable"
    assert metadata["pre_enabled_connections"] == list(PROFILE_ABILITIES["advanced"])
