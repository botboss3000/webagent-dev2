import asyncio
import importlib
from unittest.mock import AsyncMock, patch

from app import abilities
from app.admin import ability_config
from app.agent.context_control import (
    apply_tool_evidence_override,
    get_context_settings,
    normalize_tool_evidence_settings,
)
from app.api.agents import get_ability_config_schema


def test_automatic_and_memory_guard_triggers_share_one_finalizer():
    strategy = importlib.import_module(
        "plugins.abilities.Memory.context_control.context_control"
    )

    async def run(trigger):
        info = {
            "tokens_before": 8_000,
            "tokens_after": 4_000,
            "summarised_rows": 2,
        }
        persist = AsyncMock()
        with (
            patch("app.agent.compaction.maybe_compact", AsyncMock(return_value=info)),
            patch("app.agent.compaction.persist_compaction_success", persist),
        ):
            settings = {"enabled": True}
            if trigger:
                settings["_compaction_trigger"] = trigger
            result = await strategy.CONTEXT_COMPACT(
                object(), "user", "session", settings,
            )
        return result, persist

    automatic, automatic_persist = asyncio.run(run(None))
    guarded, guarded_persist = asyncio.run(run("memory_guard"))

    assert automatic["summarised_rows"] == 2
    automatic_persist.assert_awaited_once()
    assert automatic_persist.await_args.kwargs["trigger"] == "automatic"
    guarded_persist.assert_awaited_once()
    assert guarded_persist.await_args.kwargs["trigger"] == "memory_guard"


def test_context_control_has_agent_and_app_surfaces():
    catalog = abilities.ui_catalog()
    agent_meta = catalog["abilities"]["context_control"]
    app_meta = next(
        item for item in catalog["app_functions"]
        if item["id"] == "context_control"
    )

    assert agent_meta["group"] == "memory"
    assert agent_meta["agent_configurable"] is True
    assert app_meta["locked_on"] is True
    knowledge = next(group for group in catalog["groups"] if group["id"] == "memory")
    assert "context_control" in knowledge["members"]


def test_context_control_schema_is_scoped_for_each_surface():
    agent_schema = asyncio.run(
        get_ability_config_schema("context_control", scope="agent"))
    app_schema = asyncio.run(
        get_ability_config_schema("context_control", scope="app"))

    agent_fields = {field["key"]: field for field in agent_schema["settings"]}
    app_fields = {field["key"]: field for field in app_schema["settings"]}

    assert set(agent_fields) == {
        "self_compaction_posture", "compact_target_tokens", "verbatim_tail_tokens",
        "tool_evidence_policy", "full_evidence_runs",
        "full_evidence_token_budget",
    }
    assert agent_fields["compact_target_tokens"]["type"] == "range"
    assert agent_fields["compact_target_tokens"]["display"] == "tokens"
    assert agent_fields["compact_target_tokens"]["default"] == 100000
    assert agent_fields["verbatim_tail_tokens"]["type"] == "range"
    assert agent_fields["verbatim_tail_tokens"]["display"] == "tokens"
    assert "token_limit" not in agent_fields
    assert agent_fields["tool_evidence_policy"]["default"] == "current_task"
    assert agent_fields["full_evidence_runs"]["default"] == 2

    assert app_fields["token_limit"]["default"] == 1_050_000
    assert app_fields["token_limit"]["ceiling"] == "max"
    assert "self_compaction_posture" not in app_fields


def test_tool_evidence_settings_are_normalized_and_profile_ready():
    base = normalize_tool_evidence_settings({
        "tool_evidence_policy": "hybrid",
        "full_evidence_runs": "3",
        "full_evidence_token_budget": "25000",
    })
    assert base == {
        "tool_evidence_policy": "hybrid",
        "full_evidence_runs": 3,
        "full_evidence_token_budget": 25_000,
    }

    member = apply_tool_evidence_override(base, {
        "tool_evidence_policy": "recent_runs",
        "full_evidence_runs": 1,
    })
    assert member["tool_evidence_policy"] == "recent_runs"
    assert member["full_evidence_runs"] == 1
    assert member["full_evidence_token_budget"] == 25_000


def test_model_window_is_capped_by_app_maximum():
    class FakeDb:
        async def get_agent_connections(self, agent_id):
            return [{
                "section": "ability",
                "connection_type": "context_control",
                "enabled": True,
                "config": {"ability_settings": {
                    "compact_target_tokens": "100000",
                    "verbatim_tail_tokens": "30000",
                }},
            }]

    def effective(_ability_id, per_agent):
        return {
            "auto_context_limit": True,
            "token_limit": 1_050_000,
            **per_agent,
        }

    with (
        patch("app.abilities.ability_is_locked_on", return_value=True),
        patch("app.admin.ability_config.effective_ability_config", side_effect=effective),
        patch(
            "app.agent.context_control._resolve_model_limit",
            new=AsyncMock(return_value=1_000_000),
        ),
    ):
        settings = asyncio.run(get_context_settings(
            FakeDb(), "agent", session_id=None, user_id="user",
        ))

    assert settings["token_limit"] == 1_000_000
    assert settings["compact_target_tokens"] == 100_000
    assert settings["verbatim_tail_tokens"] == 30_000


def test_agent_token_budgets_are_clamped_by_global_limit():
    with patch.object(ability_config, "get_ability_config", return_value={
        "token_limit": 1_050_000,
        "compact_target_tokens": 100_000,
        "verbatim_tail_tokens": 30_000,
    }):
        effective = ability_config.effective_ability_config(
            "context_control",
            {
                "compact_target_tokens": 2_000_000,
                "verbatim_tail_tokens": 1_500_000,
            },
        )

    assert effective["compact_target_tokens"] == 1_050_000
    assert effective["verbatim_tail_tokens"] == 1_050_000


def test_legacy_agent_fractions_convert_to_tokens():
    class FakeDb:
        async def get_agent_connections(self, agent_id):
            return [{
                "section": "ability",
                "connection_type": "context_control",
                "enabled": True,
                "config": {"ability_settings": {
                    "compact_threshold": "0.5",
                    "tail_fraction": "0.2",
                }},
            }]

    with (
        patch("app.abilities.ability_is_locked_on", return_value=True),
        patch("app.agent.context_control._resolve_model_limit", new=AsyncMock(
            return_value=1_000_000)),
    ):
        settings = asyncio.run(get_context_settings(
            FakeDb(), "agent", session_id=None, user_id="user",
        ))

    assert settings["compact_target_tokens"] == 500_000
    assert settings["verbatim_tail_tokens"] == 200_000
