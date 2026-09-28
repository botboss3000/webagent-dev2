from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.db.local import LocalBackend, _vault_for
from app.db.schema import render_plane
from app.db.schema.ownership import StoragePlane, policy_for


class EntitlementSchemaTests(unittest.TestCase):
    def test_entitlement_tables_are_app_plane_authorities(self):
        app_sql = render_plane("app")
        user_sql = render_plane("user")
        for table in (
            "model_rosters",
            "model_roster_revisions",
            "experience_tiers",
            "experience_tier_revisions",
            "user_tier_assignments",
            "entitlement_audit_events",
        ):
            self.assertEqual(policy_for(table).plane, StoragePlane.APP)
            self.assertIn(f"CREATE TABLE IF NOT EXISTS {table}", app_sql)
            self.assertNotIn(f"CREATE TABLE IF NOT EXISTS {table}", user_sql)

    def test_platform_roster_secret_routes_to_app_vault(self):
        self.assertEqual(_vault_for("_platform", "llm_roster", "roster-free"), "vault_app")
        self.assertNotEqual(_vault_for("person", "llm_roster", "roster-free"), "vault_app")


class EntitlementLocalBackendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = LocalBackend(
            db_path=str(Path(self.temp.name) / "app.db"),
            seed=False,
            plane="app",
        )

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_tier_list_uses_canonical_product_order(self):
        for tier_id, slug, name in (
            ("zeta", "zeta", "Zeta"),
            ("free", "free", "Free"),
            ("pending", "pending", "Pending"),
            ("anonymous", "anonymous", "Anonymous"),
            ("admin", "admin", "Base"),
            ("alpha", "alpha", "Alpha"),
        ):
            await self.db.upsert_model_roster(
                f"roster-{slug}", slug=f"roster-{slug}", name=f"{name} roster",
                entries_json=[], status="published",
            )
            await self.db.upsert_experience_tier(
                tier_id,
                slug=slug,
                name=name,
                policy_json={},
                roster_id=f"roster-{slug}",
                status="published",
            )
        rows = await self.db.list_experience_tiers()
        self.assertEqual(
            [row["slug"] for row in rows],
            ["admin", "anonymous", "pending", "free", "alpha", "zeta"],
        )

    async def test_crud_revision_assignment_and_audit(self):
        roster = await self.db.upsert_model_roster(
            "roster-free",
            slug="roster-free",
            name="Free roster",
            entries_json=[{"entry_id": "standard", "model": "small"}],
            default_entry_id="standard",
            status="published",
            source="seed",
        )
        self.assertEqual(json.loads(roster["entries_json"])[0]["entry_id"], "standard")
        self.assertEqual((await self.db.get_model_roster_by_slug("roster-free"))["id"], "roster-free")

        updated = await self.db.upsert_model_roster("roster-free", description="safe defaults")
        self.assertEqual(updated["revision"], 2)

        tier = await self.db.upsert_experience_tier(
            "free",
            slug="free",
            name="Free",
            policy_json={"features": {"model_picker": True}},
            roster_id="roster-free",
            is_system=True,
            status="published",
        )
        self.assertEqual(tier["is_system"], 1)
        self.assertTrue(json.loads(tier["policy_json"])["features"]["model_picker"])

        assignment = await self.db.upsert_user_tier_assignment(
            "assignment-1",
            user_id="user-1",
            tier_id="free",
            source="manual",
            assigned_by="admin",
            reason="support grant",
        )
        self.assertEqual(assignment["tier_id"], "free")
        self.assertEqual(len(await self.db.list_user_tier_assignments("user-1")), 1)

        event = await self.db.append_entitlement_audit_event(
            "audit-1",
            subject_user_id="user-1",
            actor_user_id="admin",
            action="assignment.created",
            entity_type="user_tier_assignment",
            entity_id="assignment-1",
            new_json=assignment,
            reason="support grant",
        )
        self.assertEqual(event["action"], "assignment.created")
        self.assertEqual(len(await self.db.list_entitlement_audit_events(subject_user_id="user-1")), 1)

        with self.assertRaises(ValueError):
            await self.db.upsert_model_roster("bad", slug="bad", name="Bad", entries_json="not json")

    async def test_assignment_exports_erases_and_audit_is_deidentified(self):
        await self.db.upsert_model_roster(
            "roster-free", slug="roster-free", name="Free", status="published"
        )
        await self.db.upsert_experience_tier(
            "free", slug="free", name="Free", roster_id="roster-free", status="published"
        )
        await self.db.upsert_user_tier_assignment(
            "assignment-1", user_id="user-1", tier_id="free", source="billing"
        )
        await self.db.append_entitlement_audit_event(
            "audit-1",
            subject_user_id="user-1",
            actor_user_id="user-1",
            action="assignment.created",
            entity_type="user_tier_assignment",
            entity_id="assignment-1",
        )

        exported = await self.db.export_user_data("user-1")
        self.assertEqual(len(exported["server_authority"]["user_tier_assignments"]), 1)
        self.assertEqual(len(exported["server_authority"]["entitlement_audit_events"]), 1)

        erased = await self.db.erase_user_owned_data("user-1")
        self.assertEqual(erased["user_tier_assignments"], 1)
        self.assertEqual(erased["entitlement_audit_events_pseudonymized"], 1)
        self.assertEqual(await self.db.list_user_tier_assignments("user-1"), [])
        retained = await self.db.list_entitlement_audit_events(entity_id="assignment-1")
        self.assertEqual(len(retained), 1)
        self.assertIsNone(retained[0]["subject_user_id"])
        self.assertIsNone(retained[0]["actor_user_id"])

    async def test_experience_tier_publication_is_immutable_and_rollback_is_new_revision(self):
        await self.db.upsert_model_roster(
            "roster-free", slug="roster-free", name="Free roster", status="draft",
        )
        await self.db.upsert_experience_tier(
            "free", slug="free", name="Free", status="draft",
            policy_json={"version": 1}, roster_id="roster-free",
        )
        first = await self.db.publish_experience_tier(
            "free", actor_user_id="admin", expected_revision=1,
        )
        self.assertEqual(first["published_revision"], 1)

        draft = await self.db.upsert_experience_tier(
            "free", expected_revision=1, policy_json={"version": 2},
        )
        self.assertEqual(draft["status"], "published")
        self.assertEqual(draft["revision"], 2)
        live = await self.db.get_published_experience_tier("free")
        self.assertEqual(json.loads(live["policy_json"])["version"], 1)

        with self.assertRaises(ValueError):
            await self.db.publish_experience_tier(
                "free", actor_user_id="admin", expected_revision=1,
            )
        second = await self.db.publish_experience_tier(
            "free", actor_user_id="admin", expected_revision=2,
        )
        rolled = await self.db.rollback_experience_tier(
            "free", 1, actor_user_id="admin",
        )
        self.assertGreater(rolled["published_revision"], second["published_revision"])
        live = await self.db.get_published_experience_tier("free")
        self.assertEqual(json.loads(live["policy_json"])["version"], 1)

    async def test_legacy_published_tier_is_backfilled_to_immutable_snapshot(self):
        legacy_path = Path(self.temp.name) / "legacy-tier.db"
        conn = sqlite3.connect(legacy_path)
        conn.execute(
            """CREATE TABLE experience_tiers (
               id TEXT PRIMARY KEY, slug TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
               description TEXT NOT NULL DEFAULT '', policy_json TEXT NOT NULL DEFAULT '{}',
               policy_schema_version INTEGER NOT NULL DEFAULT 1, roster_id TEXT,
               is_system INTEGER NOT NULL DEFAULT 0, is_locked INTEGER NOT NULL DEFAULT 0,
               status TEXT NOT NULL DEFAULT 'draft', revision INTEGER NOT NULL DEFAULT 1,
               created_by TEXT, updated_by TEXT, published_at TEXT, created_at TEXT,
               updated_at TEXT)"""
        )
        conn.execute(
            """INSERT INTO experience_tiers
               (id, slug, name, policy_json, status, revision, created_at, updated_at)
               VALUES ('legacy', 'legacy', 'Legacy', '{"legacy":true}', 'published', 7,
                       datetime('now'), datetime('now'))"""
        )
        conn.commit()
        conn.close()

        db = LocalBackend(db_path=str(legacy_path), seed=False, plane="app")
        container = await db.get_experience_tier("legacy")
        self.assertEqual(container["published_revision"], 7)
        live = await db.get_published_experience_tier("legacy")
        self.assertEqual(json.loads(live["policy_json"]), {"legacy": True})
        history = await db.list_experience_tier_revisions("legacy")
        self.assertEqual(history[0]["action"], "compatibility-backfill")


if __name__ == "__main__":
    unittest.main()
