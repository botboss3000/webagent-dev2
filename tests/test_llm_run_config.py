"""Provider resolution must remain isolated from process-global environment."""

import os
import unittest
from unittest.mock import AsyncMock, patch

from app.admin import settings


class ProviderRunConfigTests(unittest.IsolatedAsyncioTestCase):
    def test_custom_session_selection_survives_roster_reordering_by_entry_id(self):
        def entry(entry_id, model):
            return {
                "entry_id": entry_id, "provider": "openai",
                "base_url": "https://api.openai.com/v1", "api_key": entry_id,
                "model": model, "enabled": True, "text_capable": True,
            }

        selected = {
            "use_default": False, "selection_type": "custom",
            "entry_id": "model-b", "custom_position": 2,
        }
        for custom_order in (
            [entry("model-a", "a"), entry("model-b", "b")],
            [entry("model-b", "b"), entry("model-a", "a")],
        ):
            base = {
                **entry("standard", "standard"),
                "multi_providers": [entry("standard", "standard"), *custom_order],
            }
            with patch.object(settings, "_load_app_settings", return_value={
                "extend_llm_to_agents": True,
            }):
                resolved = settings._merge_agent_override(base, selected)
            self.assertEqual(resolved["entry_id"], "model-b")
            self.assertEqual(resolved["model"], "b")
            self.assertEqual(resolved["_slot_ref"], "entry:model-b")

    def test_selected_openrouter_free_slot_resolves_to_native_router(self):
        free = {
            "entry_id": "f1", "display_name": "F1", "provider": "openrouter",
            "base_url": "https://openrouter.ai/api/v1", "api_key": "free-key",
            "model": "openrouter/free", "routing_mode": "openrouter_free",
            "enabled": True, "text_capable": True,
        }
        selected = {
            "use_default": False, "selection_type": "role", "role": "standard",
        }
        with patch.object(settings, "_load_app_settings", return_value={"extend_llm_to_agents": True}):
            resolved = settings._merge_agent_override(
                {**free, "multi_providers": [free],
                 "role_assignments": {"standard": "f1"}},
                selected,
            )
        self.assertEqual(resolved["model"], "openrouter/free")
        self.assertEqual(resolved["routing_mode"], "openrouter_free")
        self.assertEqual(resolved["api_key"], "free-key")

    async def test_entitlement_clamp_keeps_the_callers_own_display_name(self):
        """MODEL-NAME-LEVELS: the clamp restricts WHICH entries run (identity,
        credentials, base URL). A display name is presentation, so the caller's own
        name for an entry must survive instead of being replaced by the catalog's."""
        base_row = {
            "entry_id": "model-a", "provider": "deepseek",
            "base_url": "https://api.deepseek.com", "api_key": "k",
            "model": "deepseek-v4", "display_name": "Catalog Name",
            "text_capable": True,
        }
        base = {"multi_providers": [base_row]}
        effective = {**base_row, "display_name": "V4 boyaa",
                     "multi_providers": [base_row]}
        capabilities = AsyncMock(return_value={"models": {"allow_byo": True}})

        with patch("app.entitlements.service.resolve_capabilities", new=capabilities):
            clamped, _ = await settings._clamp_config_to_entitlements("u1", base, effective)
            self.assertEqual(clamped["display_name"], "V4 boyaa")
            # Identity still comes from the entitled base row.
            self.assertEqual(clamped["model"], "deepseek-v4")

            # A caller with no name of their own still falls back to the catalog's.
            blank, _ = await settings._clamp_config_to_entitlements(
                "u1", base, {**effective, "display_name": ""})
            self.assertEqual(blank["display_name"], "Catalog Name")

            # A model that is not entitled must still fail closed.
            stray, _ = await settings._clamp_config_to_entitlements(
                "u1", base, {**effective, "entry_id": "nope", "model": "nope",
                             "display_name": "Spoofed"})
            self.assertEqual(stray["display_name"], "")
            self.assertEqual(stray["model"], "")


    def test_selected_model_never_borrows_another_roster_key(self):
        base = {
            "provider": "deepseek", "base_url": "https://api.deepseek.com/v1",
            "api_key": "deepseek-key", "model": "deepseek-v4-flash",
            "multi_providers": [
                {"provider": "abacus", "base_url": "https://routellm.abacus.ai/v1",
                 "api_key": "abacus-key", "model": "deepseek-ai/DeepSeek-V4-Pro", "high_effort_capable": True},
                {"provider": "deepseek", "base_url": "https://api.deepseek.com/v1",
                 "api_key": "deepseek-key", "model": "deepseek-v4-flash", "enabled": True},
            ],
        }
        override = {
            "provider": "deepseek", "base_url": "https://api.deepseek.com/v1",
            "api_key": "", "model": "deepseek-v4-flash",
            "multi_providers": [{"provider": "deepseek", "base_url": "https://api.deepseek.com/v1",
                                 "api_key": "", "model": "deepseek-v4-flash", "enabled": True}],
        }
        with patch.object(settings, "_load_app_settings", return_value={"extend_llm_to_agents": True}):
            resolved = settings._merge_agent_override(base, override)
        self.assertEqual(resolved["api_key"], "deepseek-key")

    async def test_non_env_resolution_keeps_each_provider_key_local(self):
        original = {key: os.environ.get(key) for key in ("LLM_API_KEY", "LLM_MODEL")}
        os.environ["LLM_API_KEY"] = "unrelated-process-key"
        os.environ["LLM_MODEL"] = "unrelated-process-model"
        configs = {
            "one": {
                "provider": "openai", "base_url": "https://api.openai.com/v1",
                "api_key": "key-one", "model": "gpt-one",
                "multi_providers": [{"provider": "openai", "base_url": "https://api.openai.com/v1",
                                     "api_key": "key-one", "model": "gpt-one",
                                     "enabled": True}],
            },
            "two": {
                "provider": "groq", "base_url": "https://api.groq.com/openai/v1",
                "api_key": "key-two", "model": "llama-two",
                "multi_providers": [{"provider": "groq", "base_url": "https://api.groq.com/openai/v1",
                                     "api_key": "key-two", "model": "llama-two",
                                     "enabled": True}],
            },
        }
        try:
            with patch.object(settings, "_resolve_user_config", AsyncMock(side_effect=lambda user: configs[user])), \
                 patch.object(settings, "_ensure_tool_capable", AsyncMock(side_effect=lambda config, _user: config)), \
                 patch.object(settings, "_load_session_override", AsyncMock(return_value=None)), \
                 patch("app.entitlements.service.resolve_capabilities", AsyncMock(return_value={
                     "models": {"allow_byo": True, "max_byo_entries": 1,
                                "allowed_entry_ids": [], "roster_id": "roster-free"},
                 })):
                first = await settings.apply_provider_for_run("one", apply_env=False)
                second = await settings.apply_provider_for_run("two", apply_env=False)

            self.assertEqual((first["base_url"], first["api_key"], first["model"]),
                             ("https://api.openai.com/v1", "key-one", "gpt-one"))
            self.assertEqual((second["base_url"], second["api_key"], second["model"]),
                             ("https://api.groq.com/openai/v1", "key-two", "llama-two"))
            self.assertEqual(os.environ["LLM_API_KEY"], "unrelated-process-key")
            self.assertEqual(os.environ["LLM_MODEL"], "unrelated-process-model")
        finally:
            for key, value in original.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    async def test_unavailable_session_model_fails_closed_after_all_override_layers(self):
        base = {
            "provider": "openai", "base_url": "https://api.openai.com/v1",
            "api_key": "standard-key", "model": "standard",
            "_platform_roster_id": "roster-free",
            "multi_providers": [{
                "entry_id": "free-standard", "provider": "openai",
                "base_url": "https://api.openai.com/v1", "api_key": "standard-key",
                "model": "standard",
            }],
        }
        forbidden = {"use_default": False, "provider": "openai", "api_key": "injected",
                     "model": "premium", "model_effort": {"premium": "high"}}
        capabilities = {"models": {
            "roster_id": "roster-free", "allowed_entry_ids": ["free-standard"],
            "allow_byo": True, "max_byo_entries": 1, "max_reasoning_effort": "medium",
        }}
        with patch.object(settings, "_resolve_user_config", AsyncMock(return_value=base)), \
             patch.object(settings, "_ensure_tool_capable", AsyncMock(side_effect=lambda config, _user: config)), \
             patch.object(settings, "_load_session_override", AsyncMock(return_value=forbidden)), \
             patch("app.entitlements.service.resolve_capabilities", AsyncMock(return_value=capabilities)):
            result = await settings.apply_provider_for_run("user", apply_env=False)

        self.assertEqual(result["model"], "")
        self.assertEqual(result["api_key"], "")
        self.assertNotEqual(result.get("reasoning_effort"), "high")

    async def test_deleted_agent_model_does_not_fallback_to_app_default(self):
        base = {
            "entry_id": "current-default", "provider": "openai",
            "base_url": "https://api.openai.com/v1", "api_key": "current-key",
            "model": "current-model", "_platform_roster_id": "roster-free",
            "multi_providers": [{
                "entry_id": "current-default", "provider": "openai",
                "base_url": "https://api.openai.com/v1", "api_key": "current-key",
                "model": "current-model",
            }],
        }
        agent = {"metadata": {"llm_config": {
            "use_default": False, "entry_id": "deleted-model",
            "provider": "openai", "base_url": "https://api.openai.com/v1",
            "api_key": "deleted-key", "model": "deleted-model",
            "multi_providers": [{
                "entry_id": "deleted-model", "provider": "openai",
                "base_url": "https://api.openai.com/v1", "api_key": "deleted-key",
                "model": "deleted-model",
            }],
        }}}
        capabilities = {"models": {
            "roster_id": "roster-free", "allowed_entry_ids": ["current-default"],
            "allow_byo": True, "max_byo_entries": 1,
        }}
        with patch.object(settings, "_resolve_user_config", AsyncMock(return_value=base)), \
             patch.object(settings, "_ensure_tool_capable", AsyncMock(side_effect=lambda config, _user: config)), \
             patch.object(settings, "_load_session_override", AsyncMock(return_value=None)), \
             patch("app.entitlements.service.resolve_capabilities", AsyncMock(return_value=capabilities)):
            result = await settings.apply_provider_for_run(
                "user", agent_rec=agent, apply_env=False,
            )
            picker = await settings.resolve_agent_models("user", agent_rec=agent)

        self.assertEqual(result["model"], "")
        self.assertEqual(result["provider"], "")
        self.assertEqual(result["base_url"], "")
        self.assertEqual(result["api_key"], "")
        self.assertEqual([slot["model"] for slot in picker["slots"]], ["current-model"])
        self.assertIsNone(picker["active_slot"])

    def test_stale_pinned_agent_model_does_not_repoint_to_first_survivor(self):
        base = {
            "provider": "openai", "base_url": "https://api.openai.com/v1",
            "api_key": "base-key", "model": "base-model",
            "multi_providers": [],
        }
        override = {
            "use_default": False, "provider": "openai",
            "base_url": "https://api.openai.com/v1", "api_key": "deleted-key",
            "model": "deleted-model",
            "multi_providers": [{
                "provider": "openai", "base_url": "https://api.openai.com/v1",
                "api_key": "survivor-key", "model": "survivor-model",
                "enabled": False,
            }],
        }
        with patch.object(settings, "_load_app_settings", return_value={
            "extend_llm_to_agents": False,
        }):
            result = settings._merge_agent_override(base, override)

        self.assertEqual(result["model"], "")
        self.assertEqual(result["provider"], "")
        self.assertEqual(result["base_url"], "")
        self.assertEqual(result["api_key"], "")

    def test_legacy_agent_default_without_roster_fails_closed(self):
        base = {
            "provider": "openai", "base_url": "https://api.openai.com/v1",
            "api_key": "base-key", "model": "base-model",
            "multi_providers": [],
        }
        override = {
            "use_default": False, "entry_id": "agent-model",
            "provider": "openai", "base_url": "https://api.openai.com/v1",
            "api_key": "agent-key", "model": "agent-model",
        }
        with patch.object(settings, "_load_app_settings", return_value={
            "extend_llm_to_agents": False,
        }):
            result = settings._effective_config(
                base, {"metadata": {"llm_config": override}},
            )

        self.assertEqual(result["entry_id"], "")
        self.assertEqual(result["model"], "")
        self.assertEqual(result["api_key"], "")

    def test_runtime_uses_only_a_literally_assigned_standard(self):
        """Only a row the user LITERALLY assigned to Standard may run automatically.

        A row that merely looks text-capable is not a Standard: with no literal
        assignment the roster resolves no automatic model at all.
        """
        roster = [
            {"entry_id": "custom", "provider": "openai", "api_key": "custom-key",
             "base_url": "https://api.openai.com/v1", "model": "custom",
             "enabled": False, "text_capable": True},
            {"entry_id": "standard", "provider": "openai", "api_key": "standard-key",
             "base_url": "https://api.openai.com/v1", "model": "standard",
             "enabled": True, "text_capable": True},
        ]
        stale_default = {
            "provider": "deepseek", "api_key": "stale-key",
            "base_url": "https://api.deepseek.com/v1", "model": "deleted-model",
            "multi_providers": roster,
        }

        unassigned = settings._runtime_config_from_roster(stale_default)
        self.assertEqual(unassigned["entry_id"], "")
        self.assertEqual(unassigned["model"], "")
        self.assertEqual(unassigned["api_key"], "")

        assigned = settings._runtime_config_from_roster({
            **stale_default, "role_assignments": {"standard": "standard"},
        })
        self.assertEqual(assigned["entry_id"], "standard")
        self.assertEqual(assigned["model"], "standard")
        self.assertEqual(assigned["api_key"], "standard-key")

    def test_runtime_with_no_standard_does_not_use_saved_default(self):
        result = settings._runtime_config_from_roster({
            "provider": "deepseek", "api_key": "stale-key",
            "model": "deleted-model",
            "multi_providers": [{
                "provider": "openai", "api_key": "custom-key",
                "model": "custom", "enabled": False,
            }],
        })

        self.assertEqual(result["provider"], "")
        self.assertEqual(result["model"], "")
        self.assertEqual(result["api_key"], "")

    def test_persisted_shape_keeps_model_ids_only_in_roster(self):
        result = settings._without_saved_default({
            "provider": "openai", "model": "legacy-default", "api_key": "root-key",
            "providers": {
                "openai": {
                    "base_url": "https://api.openai.com/v1",
                    "api_key": "draft-key", "model": "draft-model",
                },
            },
            "multi_providers": [{
                "provider": "openai", "model": "roster-model",
                "api_key": "roster-key", "enabled": True,
            }],
        })

        self.assertNotIn("model", result)
        self.assertNotIn("api_key", result)
        self.assertNotIn("model", result["providers"]["openai"])
        self.assertEqual(result["multi_providers"][0]["model"], "roster-model")

    async def test_model_picker_only_exposes_clamped_roster_and_effort(self):
        base = {
            "provider": "openai", "base_url": "https://api.openai.com/v1",
            "api_key": "k1", "model": "standard", "_platform_roster_id": "roster-free",
            "multi_providers": [
                {"entry_id": "standard-id", "provider": "openai", "base_url": "https://api.openai.com/v1", "api_key": "k1", "model": "standard"},
                {"entry_id": "premium-id", "provider": "openai", "base_url": "https://api.openai.com/v1", "api_key": "k2", "model": "premium", "high_effort_capable": True},
            ],
        }
        session = {"model_effort": {"role:standard": "high"}}
        capabilities = {"models": {
            "roster_id": "roster-free", "allowed_entry_ids": ["standard-id"],
            "allow_byo": True, "max_byo_entries": 1, "max_reasoning_effort": "medium",
        }}
        with patch.object(settings, "_resolve_user_config", AsyncMock(return_value=base)), \
             patch.object(settings, "_load_session_override", AsyncMock(return_value=session)), \
             patch("app.entitlements.service.resolve_capabilities", AsyncMock(return_value=capabilities)):
            result = await settings.resolve_agent_models("user")

        self.assertEqual([slot["model"] for slot in result["slots"]], ["standard"])
        self.assertEqual(result["model_effort"]["role:standard"], "medium")
        self.assertNotIn("api_key", result["slots"][0])

    async def test_unavailable_platform_roster_yields_no_runtime_or_picker_model(self):
        base = {
            "provider": "openai", "base_url": "https://api.openai.com/v1",
            "api_key": "platform-key", "model": "fallback",
            "_platform_roster_id": "roster-missing",
            "multi_providers": [{
                "entry_id": "fallback-id", "provider": "openai",
                "base_url": "https://api.openai.com/v1", "api_key": "platform-key",
                "model": "fallback",
            }],
        }
        capabilities = {"models": {
            "roster_id": "roster-missing", "available": False,
            "allowed_entry_ids": [], "allow_byo": True, "max_byo_entries": 1,
            "max_reasoning_effort": "medium",
        }}
        with patch.object(settings, "_resolve_user_config", AsyncMock(return_value=base)), \
             patch.object(settings, "_load_session_override", AsyncMock(return_value=None)), \
             patch.object(settings, "_ensure_tool_capable", AsyncMock(side_effect=lambda config, _user: config)), \
             patch("app.entitlements.service.resolve_capabilities", AsyncMock(return_value=capabilities)):
            runtime = await settings.apply_provider_for_run("user", apply_env=False)
            picker = await settings.resolve_agent_models("user")

        self.assertEqual(runtime["model"], "")
        self.assertEqual(runtime["api_key"], "")
        self.assertEqual(picker["slots"], [])

    def test_agent_without_own_rows_has_no_automatic_standard_model(self):
        """An EMPTY agent roster has no Standard model role assigned.

        It must NOT silently run on an app/catalog roster: no automatic model is
        resolved, and the rows stay offered so a user pick can resolve against
        them explicitly.
        """
        catalog = [
            {"entry_id": "catalog-text", "provider": "openai",
             "base_url": "https://api.openai.com/v1", "api_key": "key",
             "model": "catalog-text", "text_capable": True},
            {"entry_id": "catalog-image", "provider": "openai",
             "base_url": "https://api.openai.com/v1", "api_key": "key",
             "model": "catalog-image", "text_capable": False, "image_capable": True},
        ]
        with patch.object(settings, "_load_app_settings", return_value={"extend_llm_to_agents": True}):
            resolved = settings._merge_agent_override(
                {"multi_providers": catalog}, {"multi_providers": []},
            )
            picked_role = settings._merge_agent_override(
                {"multi_providers": catalog},
                {"selection_type": "role", "role": "standard"},
            )
            picked_entry = settings._merge_agent_override(
                {"multi_providers": catalog},
                {"selection_type": "custom", "entry_id": "catalog-image"},
            )

        # No automatic Standard: an empty roster resolves no model at all.
        self.assertEqual(resolved["model"], "")
        self.assertEqual(resolved["provider"], "")
        self.assertIsNone(
            settings._assign_slots(resolved["multi_providers"], [])["roles"]["standard"],
        )
        # EXPLICIT picks still resolve against the rows the user was offered...
        self.assertEqual(picked_entry["model"], "catalog-image")
        # ...but picking the STANDARD role is only meaningful once a Standard has
        # been assigned, so with no assignment it resolves nothing either.
        self.assertEqual(picked_role["model"], "")
        self.assertEqual(picked_entry["model"], "catalog-image")


if __name__ == "__main__":
    unittest.main()