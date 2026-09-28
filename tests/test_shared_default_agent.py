from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sqlite3
import tempfile
from unittest.mock import AsyncMock, Mock, patch

from app.api import agents as agents_api
from app.db.local import LocalBackend
from app.db.router import PlaneRouterBackend


def run(coro):
    return asyncio.run(coro)


def test_shared_default_creation_uses_one_fixed_admin_owned_id():
    db = Mock()
    db.get_agent_by_id = AsyncMock(return_value=None)
    db.create_agent_for_user = AsyncMock(
        return_value={"id": agents_api.SHARED_DEFAULT_AGENT_ID}
    )
    agents_api._provision_locks.clear()

    with patch(
        "app.admin.settings.shared_default_agent_enabled", return_value=True
    ):
        first = run(agents_api.provision_default_agent(db, "alice"))

    assert first["id"] == agents_api.SHARED_DEFAULT_AGENT_ID
    db.create_agent_for_user.assert_awaited_once_with(
        "admin", agent_id=agents_api.SHARED_DEFAULT_AGENT_ID
    )


def test_every_user_resolves_the_same_existing_shared_agent():
    shared = {"id": agents_api.SHARED_DEFAULT_AGENT_ID}
    db = Mock()
    db.get_agent_by_id = AsyncMock(return_value=shared)
    db.create_agent_for_user = AsyncMock()
    agents_api._provision_locks.clear()

    with patch(
        "app.admin.settings.shared_default_agent_enabled", return_value=True
    ):
        alice = run(agents_api.provision_default_agent(db, "alice"))
        bob = run(agents_api.provision_default_agent(db, "bob"))

    assert alice["id"] == bob["id"] == agents_api.SHARED_DEFAULT_AGENT_ID
    db.create_agent_for_user.assert_not_awaited()


def test_shared_default_provision_migrates_legacy_roles_once():
    shared = {
        "id": agents_api.SHARED_DEFAULT_AGENT_ID,
        "metadata": json.dumps({
            "llm_config": {
                "use_default": False,
                "multi_providers": [{
                    "provider": "deepseek",
                    "base_url": "https://api.deepseek.com/v1",
                    "model": "deepseek-v4-flash",
                    "enabled": True,
                    "use_for_system": True,
                }],
            },
        }),
    }
    db = Mock()
    db.get_agent_by_id = AsyncMock(return_value=shared)
    db.update_agent_fields = AsyncMock(side_effect=lambda *_args, **_kwargs: {
        **shared, "metadata": _args[2]["metadata"],
    })
    agents_api._provision_locks.clear()

    async def load_config(_agent_id, config):
        return config

    async def persist_config(_agent_id, config):
        return config

    with patch(
        "app.admin.settings.shared_default_agent_enabled", return_value=True
    ), patch(
        "app.admin.settings._load_agent_llm_config", new=load_config
    ), patch(
        "app.admin.settings._persist_agent_llm_config", new=persist_config
    ):
        result = run(agents_api.provision_default_agent(db, "alice"))

    config = result["metadata"]["llm_config"]
    entry = config["multi_providers"][0]
    assert entry["model"] == "deepseek-v4-flash"
    assert entry["entry_id"]
    assert "enabled" not in entry
    assert "use_for_system" not in entry
    assert config["role_assignments"] == {
        "standard": entry["entry_id"],
        "system": entry["entry_id"],
    }
    db.update_agent_fields.assert_awaited_once_with(
        agents_api.SHARED_DEFAULT_AGENT_ID,
        "",
        {"metadata": result["metadata"]},
        allow_install_admin=True,
    )


def test_shared_default_provision_preserves_explicit_role_assignments():
    entry_id = "shared-default-deepseek-v4-flash"
    shared = {
        "id": agents_api.SHARED_DEFAULT_AGENT_ID,
        "metadata": {
            "llm_config": {
                "use_default": False,
                "multi_providers": [{
                    "entry_id": entry_id,
                    "provider": "deepseek",
                    "model": "deepseek-v4-flash",
                }],
                "role_assignments": {
                    "standard": entry_id,
                    "system": entry_id,
                },
            },
        },
    }
    db = Mock()
    db.get_agent_by_id = AsyncMock(return_value=shared)
    db.update_agent_fields = AsyncMock()
    agents_api._provision_locks.clear()

    with patch(
        "app.admin.settings.shared_default_agent_enabled", return_value=True
    ):
        result = run(agents_api.provision_default_agent(db, "alice"))

    assert result is shared
    db.update_agent_fields.assert_not_awaited()


def test_shared_default_seed_declares_deepseek_standard_and_system():
    seed_path = Path(__file__).parents[1] / "app" / "defaults" / "agents" / "default.json"
    config = json.loads(seed_path.read_text(encoding="utf-8"))["metadata"]["llm_config"]

    entry = config["multi_providers"][0]
    assert entry["model"] == "deepseek-v4-flash"
    assert config["role_assignments"] == {
        "standard": entry["entry_id"],
        "system": entry["entry_id"],
    }
    assert "model" not in config
    assert "provider" not in config


def test_disabled_shared_default_does_not_create_a_per_user_clone():
    db = Mock()
    db.get_agent_by_id = AsyncMock()
    db.create_agent_for_user = AsyncMock()
    db.create_custom_agent = AsyncMock()
    agents_api._provision_locks.clear()

    with patch(
        "app.admin.settings.shared_default_agent_enabled", return_value=False
    ):
        result = run(agents_api.provision_default_agent(db, "alice"))

    assert result is None
    db.get_agent_by_id.assert_not_awaited()
    db.create_agent_for_user.assert_not_awaited()
    db.create_custom_agent.assert_not_awaited()


def test_generic_default_session_resolution_uses_shared_authority():
    with tempfile.TemporaryDirectory() as tmp:
        db = LocalBackend(f"{tmp}/local.db")
        conn = db._get_conn()
        conn.execute(
            "INSERT INTO sessions (id,user_id,title) VALUES (?,?,?)",
            ("session-1", "alice", "New Session"),
        )
        conn.commit()
        conn.close()

        with patch(
            "app.admin.settings.shared_default_agent_enabled", return_value=True
        ), patch("app.db.storage_layout.is_layout_active", return_value=False):
            agent = run(
                db.get_or_resolve_session_agent("session-1", "alice", "default")
            )

        conn = db._get_conn()
        agent_ids = [row[0] for row in conn.execute("SELECT id FROM agents")]
        bound_id = conn.execute(
            "SELECT agent_id FROM sessions WHERE id='session-1'"
        ).fetchone()[0]
        conn.close()

    assert agent["id"] == agents_api.SHARED_DEFAULT_AGENT_ID
    assert agent_ids == [agents_api.SHARED_DEFAULT_AGENT_ID]
    assert bound_id == agents_api.SHARED_DEFAULT_AGENT_ID


def test_plane_router_preserves_explicit_singleton_agent_id():
    app_db = Mock()
    app_db.set_user_default_agent = AsyncMock()
    authority = Mock()
    authority.create_agent_for_user = AsyncMock(
        return_value={"id": agents_api.SHARED_DEFAULT_AGENT_ID}
    )
    router = PlaneRouterBackend(app_db)

    with patch.object(router, "_agent", return_value=authority), patch.object(
        router, "_refresh_catalog", new=AsyncMock()
    ) as refresh:
        result = run(
            router.create_agent_for_user(
                "admin", agent_id=agents_api.SHARED_DEFAULT_AGENT_ID
            )
        )

    assert result["id"] == agents_api.SHARED_DEFAULT_AGENT_ID
    authority.create_agent_for_user.assert_awaited_once_with(
        "admin", agent_id=agents_api.SHARED_DEFAULT_AGENT_ID
    )
    refresh.assert_awaited_once_with(agents_api.SHARED_DEFAULT_AGENT_ID, parent_id=None)
    app_db.set_user_default_agent.assert_awaited_once_with(
        "admin", agents_api.SHARED_DEFAULT_AGENT_ID
    )


def test_plane_router_default_session_resolution_reuses_shared_authority():
    app_db = Mock()
    authority = Mock()
    shared = {"id": agents_api.SHARED_DEFAULT_AGENT_ID, "status": "active"}
    authority.get_agent_by_id = AsyncMock(return_value=shared)
    authority.fetch_agent_by_id_with_context = AsyncMock(return_value=shared)
    user_db = Mock()
    user_db.get_session_agent_id = AsyncMock(return_value=None)
    user_db.bind_session_to_agent = AsyncMock()
    user_db.is_session_participant = AsyncMock(return_value=False)
    user_db.add_session_participant = AsyncMock()
    router = PlaneRouterBackend(app_db)
    router.create_custom_agent = AsyncMock()

    with patch.object(router, "_agent", return_value=authority), patch.object(
        router, "_user", return_value=user_db
    ), patch(
        "app.admin.settings.shared_default_agent_enabled", return_value=True
    ):
        agent = run(
            router.get_or_resolve_session_agent("session-1", "alice", "default")
        )

    assert agent["id"] == agents_api.SHARED_DEFAULT_AGENT_ID
    router.create_custom_agent.assert_not_awaited()
    user_db.bind_session_to_agent.assert_awaited_once_with(
        "session-1", agents_api.SHARED_DEFAULT_AGENT_ID
    )


def test_plane_router_skips_one_unavailable_authority_in_roster():
    app_db = Mock()
    app_db.get_user_profile = AsyncMock(return_value={})
    router = PlaneRouterBackend(app_db)
    catalog = {
        "agent_id": "missing-agent",
        "admin_users": '["alice"]',
        "member_users": "[]",
        "authorized_users": "[]",
        "status": "active",
    }

    with patch.object(router, "_catalog_rows", return_value=[catalog]), patch.object(
        router,
        "_catalog_agent",
        new=AsyncMock(side_effect=OSError("authority file missing")),
    ):
        result = run(
            router.list_agents_for_user(
                "alice", include_templates=False, view="active"
            )
        )

    assert result == []


def test_catalog_projection_records_app_admin_as_shared_owner():
    with tempfile.TemporaryDirectory() as tmp:
        path = f"{tmp}/app.db"
        conn = sqlite3.connect(path)
        conn.execute(
            """CREATE TABLE agent_catalog (
                agent_id TEXT PRIMARY KEY, name TEXT, icon TEXT, status TEXT,
                template_id TEXT, owner_user_id TEXT, admin_users TEXT,
                member_users TEXT, authorized_users TEXT, storage_ref TEXT,
                authority_revision INTEGER, created_at TEXT, updated_at TEXT
            )"""
        )
        conn.close()

        app_db = Mock()
        app_db._get_conn.side_effect = lambda: sqlite3.connect(path)
        authority = Mock(_db_path="shared.db")
        authority.get_agent_by_id = AsyncMock(
            return_value={
                "id": agents_api.SHARED_DEFAULT_AGENT_ID,
                "name": "WebAgent",
                "status": "active",
                "template_id": "default",
                "admin_users": '["admin"]',
                "member_users": "[]",
                "authorized_users": "[]",
                "created_at": "now",
                "updated_at": "now",
            }
        )
        router = PlaneRouterBackend(app_db)

        with patch.object(router, "_agent", return_value=authority):
            run(router._refresh_catalog(agents_api.SHARED_DEFAULT_AGENT_ID))

        conn = sqlite3.connect(path)
        row = conn.execute(
            "SELECT owner_user_id FROM agent_catalog WHERE agent_id=?",
            (agents_api.SHARED_DEFAULT_AGENT_ID,),
        ).fetchone()
        conn.close()

    assert row == ("admin",)
