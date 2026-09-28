import asyncio
import json
import tempfile
from pathlib import Path

import pytest

from app.db.local import LocalBackend
from app.entitlements.tiers import (
    TierSeedError,
    export_experience_tiers,
    import_experience_tiers,
    load_tier_seeds,
    provision_system_tiers,
    seed_policy,
)


class FakeTierDB:
    def __init__(self):
        self.rows = {}

    async def get_experience_tier(self, tier_id):
        return self.rows.get(tier_id)

    async def get_experience_tier_by_slug(self, slug):
        return next((row for row in self.rows.values() if row.get("slug") == slug), None)

    async def upsert_experience_tier(self, tier_id, **fields):
        row = {**self.rows.get(tier_id, {}), "id": tier_id, **fields}
        self.rows[tier_id] = row
        return row

    async def publish_experience_tier(self, tier_id, **_kwargs):
        self.rows[tier_id]["status"] = "published"
        self.rows[tier_id]["published_revision"] = self.rows[tier_id]["revision"]
        return self.rows[tier_id]


def test_seed_files_are_valid_and_complete():
    seeds = load_tier_seeds()
    assert {seed["slug"] for seed in seeds} == {"anonymous", "pending", "free", "pro"}
    assert all(seed["status"] == "published" for seed in seeds)
    assert all(seed["roster_id"] == seed["policy"]["models"]["roster_id"] for seed in seeds)
    renewals = {seed["slug"]: seed["policy"]["limits"]["weekly_credit_renews"] for seed in seeds}
    assert renewals == {"anonymous": False, "pending": False, "free": True, "pro": True}


def test_provision_is_insert_only_and_preserves_existing_admin_data():
    db = FakeTierDB()
    db.rows["free"] = {
        "id": "free", "slug": "free", "name": "Operator Free",
        "status": "published", "revision": 41, "updated_by": "admin-user",
    }
    first = asyncio.run(provision_system_tiers(db=db))
    assert first == {"created": 3, "skipped": 1}
    assert db.rows["free"]["name"] == "Operator Free"
    assert db.rows["free"]["revision"] == 41

    snapshot = {key: dict(value) for key, value in db.rows.items()}
    second = asyncio.run(provision_system_tiers(db=db))
    assert second == {"created": 0, "skipped": 4}
    assert db.rows == snapshot


def test_existing_slug_under_another_stable_id_is_not_overwritten():
    db = FakeTierDB()
    db.rows["custom-free-id"] = {
        "id": "custom-free-id", "slug": "free", "name": "Migrated Free",
        "status": "published", "revision": 8,
    }
    result = asyncio.run(provision_system_tiers(db=db))
    assert result == {"created": 3, "skipped": 1}
    assert "free" not in db.rows
    assert db.rows["custom-free-id"]["name"] == "Migrated Free"


def test_locked_anonymous_tier_becomes_operator_editable_without_policy_replacement():
    db = FakeTierDB()
    operator_policy = {"schema_version": 1, "pages": ["wiki"]}
    db.rows["anonymous"] = {
        "id": "anonymous", "slug": "anonymous", "name": "Anonymous",
        "status": "published", "revision": 1, "published_revision": 1,
        "is_system": 1, "is_locked": 1,
        "policy_json": operator_policy,
    }

    result = asyncio.run(provision_system_tiers(db=db))

    assert result == {"created": 3, "skipped": 1}
    assert db.rows["anonymous"]["is_locked"] is False
    assert db.rows["anonymous"]["revision"] == 2
    assert db.rows["anonymous"]["published_revision"] == 1
    assert db.rows["anonymous"]["policy_json"] == operator_policy
    assert db.rows["anonymous"]["updated_by"] == "system-seed-unlock"

    snapshot = dict(db.rows["anonymous"])
    asyncio.run(provision_system_tiers(db=db))
    assert db.rows["anonymous"] == snapshot


def test_legacy_anonymous_tier_backfills_allowance_block_once():
    db = FakeTierDB()
    policy = seed_policy("anonymous")
    policy.pop("anonymous")
    db.rows["anonymous"] = {
        "id": "anonymous", "slug": "anonymous", "name": "Anonymous",
        "status": "published", "revision": 4, "published_revision": 4,
        "is_system": 1, "is_locked": 0, "policy_json": policy,
    }

    asyncio.run(provision_system_tiers(db=db))
    migrated = db.rows["anonymous"]
    assert migrated["policy_json"]["anonymous"]["identity_messages"] == 5
    assert migrated["policy_json"]["anonymous"]["daily_messages"] == 10
    snapshot = json.loads(json.dumps(migrated))
    asyncio.run(provision_system_tiers(db=db))
    assert db.rows["anonymous"] == snapshot


def test_tier_bootstrap_is_validated_secret_free_and_preserves_existing_by_default():
    async def scenario():
        with tempfile.TemporaryDirectory() as directory:
            source = LocalBackend(
                db_path=str(Path(directory) / "source.db"), seed=False, plane="app",
            )
            target = LocalBackend(
                db_path=str(Path(directory) / "target.db"), seed=False, plane="app",
            )
            policy = seed_policy("free")
            policy["models"]["roster_id"] = "portable-roster"
            for db in (source, target):
                await db.upsert_model_roster(
                    "portable-roster", slug="portable-roster", name="Portable roster",
                    status="draft", entries_json=[],
                )
            await source.upsert_experience_tier(
                "portable", slug="portable", name="Portable", status="draft",
                policy_json=policy, roster_id="portable-roster",
            )
            await source.publish_experience_tier("portable", actor_user_id="admin")
            await source.upsert_user_tier_assignment(
                "private-assignment", user_id="private-user", tier_id="portable",
                source="manual", reason="private-reason",
            )
            await source.append_entitlement_audit_event(
                "private-audit", subject_user_id="private-user", actor_user_id="admin",
                action="assignment.created", entity_type="assignment",
                entity_id="private-assignment", reason="private-reason",
            )
            payload = await export_experience_tiers(db=source)
            encoded = json.dumps(payload)
            assert "private-user" not in encoded
            assert "private-reason" not in encoded
            assert "assignments" not in encoded and "audit" not in encoded

            await target.upsert_experience_tier(
                "portable", slug="portable", name="Keep me", status="draft",
                policy_json=policy, roster_id="portable-roster",
            )
            assert await import_experience_tiers(payload, db=target) == 0
            assert (await target.get_experience_tier("portable"))["name"] == "Keep me"
            assert await import_experience_tiers(payload, db=target, overwrite=True) == 1
            live = await target.get_published_experience_tier("portable")
            assert live["name"] == "Portable"

            invalid = json.loads(json.dumps(payload))
            invalid["tiers"][0]["policy"]["limits"]["not_a_limit"] = 1
            with pytest.raises(TierSeedError):
                await import_experience_tiers(invalid, db=target, overwrite=True)

    asyncio.run(scenario())
