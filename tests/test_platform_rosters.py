import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import UUID

from app.entitlements.rosters import (
    PLATFORM_ROSTER_OWNER,
    PLATFORM_ROSTER_SERVICE,
    RosterError,
    config_to_roster,
    export_platform_rosters,
    import_platform_rosters,
    provision_system_rosters,
    roster_to_config,
    resolve_platform_roster_config,
    roster_credential_states,
    set_roster_entry_credential,
    delete_roster_entry_credential,
    sync_legacy_platform_config,
    validate_roster_entries,
)
from app.admin import bootstrap_bundle, settings
from app.db.local import LocalBackend
from app.db.router import PlaneRouterBackend


class FakeRosterDB:
    def __init__(self):
        self.rosters = {}
        self.auth = {}

    async def get_model_roster(self, roster_id):
        return self.rosters.get(roster_id)

    async def get_model_roster_by_slug(self, slug):
        return next((row for row in self.rosters.values() if row["slug"] == slug), None)

    async def list_model_rosters(self, status=None):
        rows = list(self.rosters.values())
        return [row for row in rows if not status or row.get("status") == status]

    async def upsert_model_roster(self, roster_id, **fields):
        row = dict(self.rosters.get(roster_id) or {"id": roster_id, "revision": 1})
        if roster_id in self.rosters and "revision" not in fields:
            row["revision"] = int(row.get("revision") or 1) + 1
        row.update(fields)
        if not isinstance(row.get("entries_json"), str):
            row["entries_json"] = json.dumps(row.get("entries_json") or [])
        self.rosters[roster_id] = row
        return row

    async def auth_element_get(self, user_id, service, label="default"):
        return self.auth.get((user_id, service, label))

    async def auth_element_set(self, *, user_id, service, config, secret_ref, label="default"):
        row = {
            "user_id": user_id,
            "service": service,
            "label": label,
            "config": config,
            "secret_ref": secret_ref,
        }
        self.auth[(user_id, service, label)] = row
        return row


def legacy_config():
    return {
        "provider": "openai",
        "base_url": "https://api.openai.com/v1",
        "api_key": "standard-secret",
        "model": "gpt-standard",
        "providers": {},
        "multi_providers": [
            {
                "provider": "openai",
                "base_url": "https://api.openai.com/v1",
                "api_key": "standard-secret",
                "model": "gpt-standard",
                "text_capable": True,
                "enabled": True,
                "stateful_responses": "disabled",
            },
            {
                "provider": "openrouter",
                "base_url": "https://openrouter.ai/api/v1",
                "api_key": "premium-secret",
                "model": "vendor/gpt-premium",
                "high_effort_capable": True,
            },
        ],
    }


class PlatformRosterConversionTests(unittest.TestCase):
    def test_closed_entry_schema_rejects_nested_secret_metadata(self):
        with self.assertRaisesRegex(RosterError, "unsupported fields"):
            validate_roster_entries([{
                "entry_id": "one", "provider": "openai", "model": "gpt",
                "headers": {"Authorization": "secret"},
            }])

    def test_platform_catalog_strips_all_role_position_fields(self):
        catalog = validate_roster_entries([{
            "entry_id": "catalog", "provider": "openai", "model": "gpt-catalog",
            "text_capable": True,
            "enabled": True,
            "high_effort_capable": True,
            "use_for_image": True,
            "use_for_image_out": True,
            "use_for_system": True,
            "use_for_voice": True,
        }])

        for field in (
            "enabled", "high_effort_capable", "use_for_image", "use_for_image_out",
            "use_for_system", "use_for_voice",
        ):
            self.assertNotIn(field, catalog[0])
        self.assertTrue(catalog[0]["text_capable"])

    def test_untrusted_byo_url_rejects_private_targets_and_url_credentials(self):
        for base_url in ("http://127.0.0.1:8000/v1", "https://key@example.com/v1"):
            with self.subTest(base_url=base_url), self.assertRaises(RosterError):
                validate_roster_entries([{
                    "entry_id": "one", "provider": "custom", "model": "gpt",
                    "base_url": base_url,
                }], untrusted_urls=True)

    def test_entry_ids_survive_reorder_and_keys_follow_ids(self):
        config = legacy_config()
        entries, default_id, secret = config_to_roster(config, "roster-free")
        reversed_config = dict(config, multi_providers=list(reversed(config["multi_providers"])))
        reversed_entries, _, _ = config_to_roster(reversed_config, "roster-free")

        by_model = {entry["model"]: entry["entry_id"] for entry in entries}
        reversed_by_model = {entry["model"]: entry["entry_id"] for entry in reversed_entries}
        self.assertEqual(by_model, reversed_by_model)
        self.assertEqual(default_id, by_model["gpt-standard"])
        self.assertNotIn("api_key", json.dumps(entries))

        restored = roster_to_config(
            {"id": "roster-free", "entries_json": entries, "default_entry_id": default_id},
            secret,
        )
        keys = {entry["model"]: entry["api_key"] for entry in restored["multi_providers"]}
        self.assertEqual(keys["gpt-standard"], "standard-secret")
        self.assertEqual(keys["vendor/gpt-premium"], "premium-secret")
        self.assertEqual(restored["stateful_responses"], "disabled")

    def test_first_text_catalog_entry_wins_over_legacy_default_entry_id(self):
        entries, _default_id, secret = config_to_roster(legacy_config(), "roster-free")
        catalog_default = next(entry for entry in entries if entry.get("text_capable"))
        nondefault = next(entry for entry in entries if entry["entry_id"] != catalog_default["entry_id"])

        restored = roster_to_config(
            {
                "id": "roster-free",
                "entries_json": entries,
                "default_entry_id": nondefault["entry_id"],
            },
            secret,
        )

        self.assertEqual(restored["_platform_default_entry_id"], catalog_default["entry_id"])
        self.assertEqual(restored["model"], catalog_default["model"])

    def test_openrouter_free_slot_is_canonical_and_keeps_its_vault_key(self):
        entries = validate_roster_entries([{
            "entry_id": "f1", "display_name": "F1", "routing_mode": "openrouter_free",
            "enabled": True, "text_capable": True,
        }], require_provider_model=True)
        self.assertEqual(entries[0]["provider"], "openrouter")
        self.assertEqual(entries[0]["base_url"], "https://openrouter.ai/api/v1")
        self.assertEqual(entries[0]["model"], "openrouter/free")
        self.assertEqual(entries[0]["routing_mode"], "openrouter_free")

        restored = roster_to_config(
            {"id": "roster-free", "entries_json": entries, "default_entry_id": "f1"},
            json.dumps({"v": 2, "entries": {"f1": "openrouter-key"}}),
        )
        self.assertEqual(restored["model"], "openrouter/free")
        self.assertEqual(restored["routing_mode"], "openrouter_free")
        self.assertEqual(restored["multi_providers"][0]["api_key"], "openrouter-key")

    def test_openrouter_free_slot_rejects_static_pin_or_other_provider(self):
        with self.assertRaisesRegex(RosterError, "cannot select a static model"):
            validate_roster_entries([{
                "routing_mode": "openrouter_free", "provider": "openrouter", "model": "meta-llama/llama-3",
            }])
        with self.assertRaisesRegex(RosterError, "requires provider openrouter"):
            validate_roster_entries([{
                "routing_mode": "openrouter_free", "provider": "openai",
            }])

    def test_stateful_responses_accepts_supported_modes_only(self):
        self.assertEqual(
            validate_roster_entries([{"entry_id": "one", "stateful_responses": "auto"}])[0]
            ["stateful_responses"],
            "auto",
        )
        with self.assertRaisesRegex(RosterError, "must be auto or disabled"):
            validate_roster_entries([{"entry_id": "one", "stateful_responses": "enabled"}])

    def test_duplicate_explicit_entry_ids_are_rejected(self):
        config = legacy_config()
        for entry in config["multi_providers"]:
            entry["entry_id"] = "same-id"
        with self.assertRaisesRegex(RosterError, "duplicate roster entry_id"):
            config_to_roster(config, "roster-free")


class PlatformRosterServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_env_provision_creates_keyless_rows_and_platform_vault_secrets(self):
        db = FakeRosterDB()
        with patch("app.admin.settings._load_own_llm_config", new=AsyncMock(return_value=None)):
            result = await provision_system_rosters(db=db, env_config=legacy_config())

        self.assertEqual(result, {"created": 0, "migrated": 4})
        self.assertEqual(len(db.rosters), 4)
        for roster_id, row in db.rosters.items():
            self.assertNotIn("standard-secret", row["entries_json"])
            vault_row = db.auth[(PLATFORM_ROSTER_OWNER, PLATFORM_ROSTER_SERVICE, roster_id)]
            self.assertEqual(json.loads(vault_row["secret_ref"])["v"], 2)

        # A second startup is an idempotent no-op.
        with patch("app.admin.settings._load_own_llm_config", new=AsyncMock(return_value=None)):
            self.assertEqual(
                await provision_system_rosters(db=db, env_config=legacy_config()),
                {"created": 0, "migrated": 0},
            )

    async def test_export_import_round_trip_preserves_entry_key_mapping(self):
        source = FakeRosterDB()
        with patch("app.admin.settings._load_own_llm_config", new=AsyncMock(return_value=None)):
            await provision_system_rosters(db=source, env_config=legacy_config())
        payload = await export_platform_rosters(db=source)

        target = FakeRosterDB()
        self.assertEqual(await import_platform_rosters(payload, db=target), 4)
        restored = await export_platform_rosters(db=target)
        source_free = next(row for row in payload["rosters"] if row["id"] == "roster-free")
        target_free = next(row for row in restored["rosters"] if row["id"] == "roster-free")
        self.assertEqual(source_free["entries"], target_free["entries"])
        self.assertEqual(source_free["credential_bundle"], target_free["credential_bundle"])

    async def test_shape_only_export_does_not_include_credentials(self):
        db = FakeRosterDB()
        with patch("app.admin.settings._load_own_llm_config", new=AsyncMock(return_value=None)):
            await provision_system_rosters(db=db, env_config=legacy_config())
        payload = await export_platform_rosters(db=db, strip_secrets=True)
        self.assertTrue(payload["rosters"])
        self.assertTrue(all(not row["credential_bundle"] for row in payload["rosters"]))

    async def test_named_credential_write_delete_is_id_keyed(self):
        db = FakeRosterDB()
        await db.upsert_model_roster(
            "custom", slug="custom", name="Custom", status="draft",
            entries_json=[{"entry_id": "a", "model": "one"}, {"entry_id": "b", "model": "two"}],
            default_entry_id="a",
        )
        await set_roster_entry_credential("custom", "b", "secret-b", db=db)
        self.assertEqual(await roster_credential_states("custom", db=db), {"b": True})
        bundle = json.loads(db.auth[(PLATFORM_ROSTER_OWNER, PLATFORM_ROSTER_SERVICE, "custom")]["secret_ref"])
        self.assertEqual(bundle["entries"], {"b": "secret-b"})
        await delete_roster_entry_credential("custom", "b", db=db)
        self.assertEqual(await roster_credential_states("custom", db=db), {})

    async def test_legacy_bridge_never_overwrites_admin_claimed_system_roster(self):
        db = FakeRosterDB()
        await db.upsert_model_roster(
            "roster-free", slug="roster-free", name="Admin Free", status="published",
            source="admin", entries_json=[{"entry_id": "kept", "model": "kept"}],
            default_entry_id="kept",
        )
        changed = await sync_legacy_platform_config(legacy_config(), db=db)
        self.assertEqual(changed, 3)
        self.assertIn("kept", db.rosters["roster-free"]["entries_json"])
        self.assertNotIn("gpt-standard", db.rosters["roster-free"]["entries_json"])

    async def test_invalid_tier_roster_never_falls_back_to_free(self):
        db = FakeRosterDB()
        entries, default_id, secret = config_to_roster(legacy_config(), "roster-free")
        await db.upsert_model_roster(
            "roster-free", slug="roster-free", name="Free", status="published",
            entries_json=entries, default_entry_id=default_id,
        )
        await db.auth_element_set(
            user_id=PLATFORM_ROSTER_OWNER, service=PLATFORM_ROSTER_SERVICE,
            label="roster-free", config={}, secret_ref=secret,
        )
        with patch(
            "app.entitlements.service.resolve_capabilities",
            new=AsyncMock(return_value={"models": {"roster_id": "does-not-exist"}}),
        ):
            self.assertIsNone(await resolve_platform_roster_config("user", db=db))


class PlatformRosterPublicationTests(unittest.IsolatedAsyncioTestCase):
    async def test_plane_init_backfills_legacy_published_roster_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.db"
            conn = sqlite3.connect(path)
            conn.execute(
                """CREATE TABLE model_rosters (
                   id TEXT PRIMARY KEY, slug TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
                   description TEXT NOT NULL DEFAULT '', entries_json TEXT NOT NULL DEFAULT '[]',
                   default_entry_id TEXT, status TEXT NOT NULL DEFAULT 'draft',
                   revision INTEGER NOT NULL DEFAULT 1, source TEXT NOT NULL DEFAULT 'admin',
                   created_by TEXT, updated_by TEXT, published_at TEXT, created_at TEXT,
                   updated_at TEXT)"""
            )
            conn.execute(
                """INSERT INTO model_rosters
                   (id, slug, name, entries_json, default_entry_id, status, revision,
                    created_at, updated_at)
                   VALUES ('legacy', 'legacy', 'Legacy',
                           '[{"entry_id":"one","provider":"openai","model":"gpt"}]',
                           'one', 'published', 7, datetime('now'), datetime('now'))"""
            )
            conn.commit()
            conn.close()

            db = LocalBackend(db_path=str(path), seed=False, plane="app")
            container = await db.get_model_roster("legacy")
            self.assertEqual(container["published_revision"], 7)
            live = await db.get_published_model_roster("legacy")
            self.assertIn("gpt", live["entries_json"])
            history = await db.list_model_roster_revisions("legacy")
            self.assertEqual(history[0]["action"], "compatibility-backfill")

    async def test_draft_edit_does_not_change_live_snapshot_and_rollback_is_new_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            db = LocalBackend(db_path=str(Path(directory) / "app.db"), seed=False, plane="app")
            await db.upsert_model_roster(
                "r", slug="r", name="Roster", status="draft",
                entries_json=[{"entry_id": "one", "model": "model-one"}],
                default_entry_id="one", source="admin",
            )
            first = await db.publish_model_roster("r", actor_user_id="admin")
            self.assertEqual(first["published_revision"], 1)
            await db.upsert_model_roster(
                "r", entries_json=[{"entry_id": "two", "model": "model-two"}],
                default_entry_id="two", source="admin",
            )
            working = await db.get_model_roster("r")
            live = await db.get_published_model_roster("r")
            self.assertEqual(working["revision"], 2)
            self.assertEqual(live["revision"], 1)
            self.assertIn("model-one", live["entries_json"])
            self.assertNotIn("model-two", live["entries_json"])

            second = await db.publish_model_roster("r", actor_user_id="admin")
            self.assertEqual(second["published_revision"], 2)
            rolled = await db.rollback_model_roster("r", 1, actor_user_id="admin")
            self.assertGreater(rolled["published_revision"], 2)
            live = await db.get_published_model_roster("r")
            self.assertIn("model-one", live["entries_json"])
            revisions = await db.list_model_roster_revisions("r")
            self.assertEqual([row["action"] for row in revisions[:2]], ["rollback", "published"])


class PlatformRosterBootstrapTests(unittest.IsolatedAsyncioTestCase):
    async def test_selecting_llm_export_also_carries_named_rosters(self):
        named = {"v": 1, "rosters": [{"id": "roster-free"}]}
        tiers = {"v": 1, "tiers": [{"id": "free"}]}
        with patch.object(
            bootstrap_bundle, "_gather_llm", new=AsyncMock(return_value=legacy_config())
        ), patch.object(
            bootstrap_bundle, "_gather_model_rosters", new=AsyncMock(return_value=named)
        ), patch.object(
            bootstrap_bundle, "_gather_experience_tiers", new=AsyncMock(return_value=tiers)
        ):
            bundle = await bootstrap_bundle.gather_bundle([bootstrap_bundle.SECTION_LLM])
        self.assertEqual(bundle["v"], 2)
        self.assertIn(bootstrap_bundle.SECTION_LLM, bundle["sections"])
        self.assertEqual(bundle["sections"][bootstrap_bundle.SECTION_MODEL_ROSTERS], named)
        self.assertEqual(bundle["sections"][bootstrap_bundle.SECTION_EXPERIENCE_TIERS], tiers)

    async def test_v1_llm_only_bundle_remains_applicable(self):
        with patch.object(
            bootstrap_bundle, "_apply_llm", new=AsyncMock(return_value="legacy applied")
        ) as apply_llm:
            result = await bootstrap_bundle.apply_bundle(
                {"v": 1, "sections": {bootstrap_bundle.SECTION_LLM: legacy_config()}},
                {bootstrap_bundle.SECTION_LLM: bootstrap_bundle.MERGE_OVERWRITE},
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["results"][bootstrap_bundle.SECTION_LLM], "legacy applied")
        apply_llm.assert_awaited_once()


class PlatformRosterSettingsBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_masked_reordered_save_preserves_credentials_by_entry_id(self):
        existing = legacy_config()
        public = settings._public_provider_config(existing)["multi_providers"]
        incoming = [dict(entry, api_key="") for entry in reversed(public)]
        persist = AsyncMock()
        with patch.object(
            settings, "_require_provider_admin", new=AsyncMock(return_value="admin-user")
        ), patch.object(
            settings, "_load_own_llm_config", new=AsyncMock(return_value=existing)
        ), patch.object(settings, "_persist_llm_config", new=persist):
            await settings.set_multi_providers(
                object(), settings.MultiProvidersRequest(providers=incoming)
            )

        saved = persist.await_args.args[1]
        keys = {entry["model"]: entry["api_key"] for entry in saved["multi_providers"]}
        self.assertEqual(keys["gpt-standard"], "standard-secret")
        self.assertEqual(keys["vendor/gpt-premium"], "premium-secret")
        for field in settings._LEGACY_DEFAULT_FIELDS:
            self.assertNotIn(field, saved)

    async def test_new_roster_save_returns_server_assigned_entry_id(self):
        existing = {"providers": {}, "multi_providers": []}
        persist = AsyncMock()
        body = settings.MultiProvidersRequest(providers=[{
            "provider": "openai_subscription",
            "auth_mode": "chatgpt",
            "base_url": "codex://chatgpt",
            "model": "gpt-new",
            "enabled": False,
        }])

        with patch.object(
            settings, "_require_provider_admin", new=AsyncMock(return_value="admin-user")
        ), patch.object(
            settings, "_load_own_llm_config", new=AsyncMock(return_value=existing)
        ), patch.object(
            settings, "_persist_llm_config", new=persist
        ), patch(
            "app.openai_subscription.status", new=AsyncMock(return_value={"chatgpt_authorized": False})
        ):
            result = await settings.set_multi_providers(object(), body)

        self.assertEqual(len(result["providers"]), 1)
        returned = result["providers"][0]
        UUID(returned["entry_id"])
        self.assertEqual(returned["model"], "gpt-new")
        self.assertEqual(returned["api_key"], "")
        saved = persist.await_args.args[1]["multi_providers"][0]
        self.assertEqual(saved["entry_id"], returned["entry_id"])

    async def test_global_delete_prunes_matching_rows_from_every_agent_roster(self):
        existing = legacy_config()
        incoming = [dict(settings._roster_entries_with_ids(existing["multi_providers"])[1], api_key="")]
        persist = AsyncMock()
        prune = AsyncMock(return_value=3)

        class FakeDB:
            prune_agent_model_rosters = prune

        with patch.object(
            settings, "_require_provider_admin", new=AsyncMock(return_value="admin-user")
        ), patch.object(
            settings, "_load_own_llm_config", new=AsyncMock(return_value=existing)
        ), patch.object(
            settings, "_persist_llm_config", new=persist
        ), patch("app.db.get_db", return_value=FakeDB()):
            result = await settings.set_multi_providers(
                object(), settings.MultiProvidersRequest(providers=incoming)
            )

        prune.assert_awaited_once()
        removed = prune.await_args.args[0]
        self.assertEqual(len(removed), 1)
        self.assertEqual(removed[0]["model"], "gpt-standard")
        self.assertTrue(removed[0]["entry_id"])
        self.assertEqual(result["pruned_agent_models"], 3)

    def test_agent_roster_prune_requires_the_same_entry_id(self):
        entries = settings._roster_entries_with_ids(
            legacy_config()["multi_providers"]
        )
        removed = entries[:1]
        same_model_different_id = dict(removed[0], entry_id="agent-owned-row")
        roster = [
            same_model_different_id,
            dict(removed[0]),
            dict(entries[1]),
        ]

        kept = settings._prune_roster_entries(roster, removed)

        self.assertEqual(
            [entry["entry_id"] for entry in kept],
            ["agent-owned-row", entries[1]["entry_id"]],
        )

    async def test_plane_router_prunes_agent_authority_and_preserves_other_metadata(self):
        roster = settings._roster_entries_with_ids(
            legacy_config()["multi_providers"]
        )
        removed = roster[:1]
        backend = AsyncMock()
        backend.get_agent_by_id.return_value = {
            "id": "agent-one",
            "metadata": json.dumps({
                "icon": "spark",
                "llm_config": {
                    "use_default": False,
                    "multi_providers": roster,
                },
            }),
        }
        backend.update_agent_fields.return_value = {"id": "agent-one"}
        router = object.__new__(PlaneRouterBackend)
        refresh = AsyncMock()

        with patch.object(
            PlaneRouterBackend, "_catalog_rows",
            return_value=[{"agent_id": "agent-one"}],
        ), patch.object(
            PlaneRouterBackend, "_agent", return_value=backend,
        ), patch.object(
            PlaneRouterBackend, "_refresh_catalog", new=refresh,
        ):
            count = await router.prune_agent_model_rosters(removed)

        self.assertEqual(count, 1)
        update = backend.update_agent_fields.await_args.args[2]["metadata"]
        self.assertEqual(update["icon"], "spark")
        self.assertFalse(update["llm_config"]["use_default"])
        self.assertEqual(
            [entry["model"] for entry in update["llm_config"]["multi_providers"]],
            ["vendor/gpt-premium"],
        )
        refresh.assert_awaited_once_with("agent-one")

    def test_shared_model_table_preserves_and_creates_roster_entry_ids(self):
        source = Path("ui/shared/js/model-table.js").read_text(encoding="utf-8")

        self.assertNotIn("entry_id: randomUUID()", source)
        self.assertIn("entry_id: p.entry_id || ''", source)
        self.assertIn("entry_id: p.entry_id || ''", Path(
            "ui/main-panel/app/settings/agent-settings/agent-settings.js"
        ).read_text(encoding="utf-8"))

    def test_shared_model_table_exposes_automatic_openrouter_free_slots(self):
        source = Path("ui/shared/js/model-table.js").read_text(encoding="utf-8")
        scoped = Path("ui/main-panel/app/settings/agent-settings/agent-settings.js").read_text(encoding="utf-8")

        self.assertIn("OpenRouter Free (Auto)", source)
        self.assertIn("Automatically routed:", source)
        self.assertIn("openrouter/free", source)
        self.assertIn("routing_mode: free ? 'openrouter_free'", source)
        self.assertIn("routing_mode: provider.routing_mode", scoped)

    def test_settings_models_has_scoped_draft_publish_toolbar_before_table(self):
        markup = Path("ui/main-panel/app/settings/pages/models/page.html").read_text(encoding="utf-8")
        script = Path(
            "ui/main-panel/app/settings/agent-settings/agent-settings.js"
        ).read_text(encoding="utf-8")

        self.assertLess(markup.index('class="model-scope-toolbar"'), markup.index('id="ac-model-host"'))
        self.assertIn('Base — Admin only', markup)
        self.assertIn('id="ac-model-save-draft"', markup)
        self.assertIn('id="ac-model-publish"', markup)
        self.assertNotIn('id="ac-model-default-entry"', markup)
        self.assertIn("labels = { published: 'Published', 'saved-draft': 'Saved draft', unsaved: 'Unsaved'", script)
        self.assertIn('adapter: _scopedAdminModelAdapter()', script)
        self.assertIn("const catalogDefault = entries.find(entry => entry.text_capable !== false)", script)
        self.assertIn('data-model-lifecycle="validate"', markup)
        self.assertIn('data-model-lifecycle="preview"', markup)
        self.assertIn('data-model-lifecycle="history"', markup)
        self.assertIn('data-model-lifecycle="retire"', markup)
        self.assertIn('id="ac-model-lifecycle-result"', markup)
        self.assertIn('_runModelLifecycle', script)
        self.assertIn('_rollbackModelScope', script)
        self.assertIn('canDeleteCredential', script)
        self.assertIn('deleteCredential', script)
        self.assertIn('Remove credential', Path("ui/shared/js/model-table.js").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
