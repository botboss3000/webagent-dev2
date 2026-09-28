from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from app.db.schema import render_plane
from app.db.schema import render_sqlite
from app.db.schema.ownership import (
    StoragePlane,
    TABLE_POLICIES,
    tables_for_plane,
    validate_table_policies,
)
from app.db.schema.tables import TABLES
from app.db.storage_layout import (
    activate_layout,
    begin_layout,
    is_layout_active,
    mark_plane_status,
    read_layout,
)
from app.db.migrate_storage_layout import StorageLayoutMigrator


def _async(coro):
    import asyncio

    return asyncio.run(coro)


class StorageOwnershipTests(unittest.TestCase):
    def test_every_core_table_has_exactly_one_policy(self):
        validate_table_policies(table.name for table in TABLES)
        self.assertEqual(len(TABLE_POLICIES), len(TABLES))

    def test_expected_authorities(self):
        self.assertIn("sessions", tables_for_plane(StoragePlane.USER))
        self.assertIn("agents", tables_for_plane(StoragePlane.AGENT))
        self.assertIn("user_accounts", tables_for_plane(StoragePlane.APP))
        self.assertIn("auth_elements", tables_for_plane(StoragePlane.SECRETS))
        self.assertIn("diagnostics", tables_for_plane(StoragePlane.TELEMETRY))

    def test_plane_schema_contains_only_owned_tables(self):
        app_sql = render_plane("app")
        user_sql = render_plane("user")
        agent_sql = render_plane("agent")

        self.assertIn("CREATE TABLE IF NOT EXISTS agent_catalog", app_sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS sessions", user_sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS agents", agent_sql)
        self.assertNotIn("CREATE TABLE IF NOT EXISTS sessions", app_sql)
        self.assertNotIn("CREATE TABLE IF NOT EXISTS agents", user_sql)
        self.assertNotIn("CREATE TABLE IF NOT EXISTS user_accounts", agent_sql)

    def test_cross_plane_foreign_keys_are_not_rendered(self):
        agent_sql = render_plane("agent")
        self.assertIn("agent_data_sources", agent_sql)
        self.assertNotIn("REFERENCES data_sources", agent_sql)

    def test_user_plane_schema_executes_standalone(self):
        conn = sqlite3.connect(":memory:")
        try:
            conn.executescript(render_plane("user"))
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        finally:
            conn.close()
        self.assertIn("sessions", tables)
        self.assertIn("session_notifications", tables)
        self.assertIn("browser_sync_receipts", tables)
        self.assertNotIn("agents", tables)

    def test_plane_scoped_local_backend_does_not_create_catch_all_schema(self):
        from app.db.local import LocalBackend

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "app.db"
            backend = LocalBackend(str(path), seed=False, plane="app")
            conn = sqlite3.connect(path)
            try:
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
            finally:
                conn.close()
            self.assertIn("user_accounts", tables)
            self.assertIn("agent_catalog", tables)
            self.assertNotIn("sessions", tables)
            self.assertNotIn("agents", tables)
            self.assertIsNotNone(backend)


class StorageLayoutActivationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "app.db"

    def tearDown(self):
        self.tempdir.cleanup()

    def test_file_existence_never_activates_layout(self):
        sqlite3.connect(self.path).close()
        self.assertFalse(is_layout_active(path=self.path))

    def test_activation_requires_every_plane_and_verified_migrations(self):
        begin_layout(
            path=self.path,
            manifest={"plane_status": {"app": "preparing", "user": "pending", "agent": "pending"}},
        )
        self.assertFalse(is_layout_active(path=self.path))
        with self.assertRaisesRegex(RuntimeError, "unverified planes"):
            activate_layout(path=self.path)

        for plane in ("app", "user", "agent"):
            mark_plane_status(plane, "verified", path=self.path)

        conn = sqlite3.connect(self.path)
        try:
            conn.execute(
                """INSERT INTO storage_migrations
                   (migration_id,source_ref,target_ref,table_name,state)
                   VALUES ('test','legacy','app','app_meta','verified')"""
            )
            conn.commit()
        finally:
            conn.close()

        activated = activate_layout(path=self.path)
        self.assertEqual(activated["state"], "active")
        self.assertTrue(activated["manifest"]["verified"])
        self.assertTrue(is_layout_active(path=self.path))

    def test_manifest_is_durable(self):
        begin_layout(path=self.path, manifest={"legacy_files_retained": True})
        row = read_layout(path=self.path)
        self.assertEqual(row["layout_version"], 2)
        self.assertTrue(row["manifest"]["legacy_files_retained"])


class StoragePlaneRoutingTests(unittest.TestCase):
    def tearDown(self):
        from app.db import reset_db_instance

        reset_db_instance()

    def test_secret_calls_stay_on_encrypted_app_handle(self):
        from app.db.router import PlaneRouterBackend

        app = Mock()
        app.auth_element_get = AsyncMock(return_value={"secret_ref": "decrypted"})
        user = Mock()
        user.auth_element_get = AsyncMock(return_value={"secret_ref": "enc:v1:ciphertext"})
        router = PlaneRouterBackend(app)

        with patch.object(router, "_user", return_value=user):
            row = _async(router.auth_element_get("admin", "llm", "default"))

        self.assertEqual(row["secret_ref"], "decrypted")
        app.auth_element_get.assert_awaited_once_with("admin", "llm", "default")
        user.auth_element_get.assert_not_awaited()

    def test_explicit_user_handle_is_encryption_wrapped(self):
        import app.db as db_module
        from app.db.interface import EncryptedStorageBackend

        inner = Mock()
        wrapped = Mock(spec=EncryptedStorageBackend)
        with patch("app.db.local.LocalBackend", return_value=inner), \
                patch("app.db.user_store._user_db_path", return_value="user.db"), \
                patch.object(db_module, "_maybe_wrap_encryption", return_value=wrapped) as wrap:
            first = db_module.get_user_db("alice")
            second = db_module.get_user_db("alice")

        self.assertIs(first, wrapped)
        self.assertIs(second, wrapped)
        wrap.assert_called_once_with(inner)
    def test_control_db_cache_uses_declared_app_cache(self):
        import app.db as db_module

        backend = Mock()

        with patch("app.db.connection_config.load_config", return_value=SimpleNamespace(provider="sqlite")), \
                patch("app.db.storage_layout.is_layout_active", return_value=True), \
                patch.object(db_module, "_build_local_control_backend", return_value=backend) as build, \
                patch.object(db_module, "_maybe_wrap_encryption", side_effect=lambda value: value):
            first = db_module.get_control_db()
            second = db_module.get_control_db()

        self.assertIs(first, backend)
        self.assertIs(second, backend)
        self.assertIs(db_module._app_db_instance, backend)
        build.assert_called_once_with()


    def test_inactive_layout_rejects_existing_legacy_database(self):
        import app.db as db_module

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_path = root / "app.db"
            legacy_path = root / "local.db"
            sqlite3.connect(app_path).close()
            legacy = sqlite3.connect(legacy_path)
            legacy.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY)")
            legacy.close()

            with (
                patch(
                    "app.db.connection_config.load_config",
                    return_value=SimpleNamespace(provider="sqlite"),
                ),
                patch("app.db.storage_layout.APP_DB_PATH", app_path),
                patch("app.db.storage_layout.is_layout_active", return_value=False),
                patch("app.db.local.LocalBackend") as local_backend,
            ):
                with self.assertRaisesRegex(
                    db_module.StorageLayoutInactiveError, "not activated"
                ):
                    db_module.get_control_db()

            local_backend.assert_not_called()

    def test_inactive_layout_without_legacy_database_fails_closed(self):
        import app.db as db_module

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_path = root / "app.db"
            sqlite3.connect(app_path).close()

            with (
                patch(
                    "app.db.connection_config.load_config",
                    return_value=SimpleNamespace(provider="sqlite"),
                ),
                patch("app.db.storage_layout.APP_DB_PATH", app_path),
                patch("app.db.storage_layout.is_layout_active", return_value=False),
                patch("app.db.local.LocalBackend") as local_backend,
            ):
                with self.assertRaisesRegex(
                    db_module.StorageLayoutInactiveError, "not activated"
                ):
                    db_module.get_control_db()

            local_backend.assert_not_called()

    def test_session_ui_never_targets_retired_database_files(self):
        root = Path(__file__).resolve().parents[1]
        paths = (root / "ui").rglob("*.js")
        legacy_references = {
            str(path.relative_to(root)): marker
            for path in paths
            for marker in ("db=local.db", "db=global.db")
            if marker in path.read_text(encoding="utf-8")
        }
        self.assertEqual(legacy_references, {})

    def test_session_picker_lists_user_sessions_without_colocated_agents_table(self):
        from starlette.requests import Request
        from app.api.db_viewer import list_sessions
        from app.db.local import get_db_user_context

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "admin.db"
            conn = sqlite3.connect(path)
            conn.executescript(render_plane("user"))
            conn.execute(
                "INSERT INTO sessions (id,user_id,title,agent_id,participants) "
                "VALUES ('session-1','admin','Split session','agent-1','[]')"
            )
            conn.executemany(
                "INSERT INTO interactions (id,session_id,role,content,created_at) "
                "VALUES (?, 'session-1', ?, 'hello', ?)",
                [
                    ('user-1', 'user', '2026-08-21 10:00:00'),
                    ('assistant-1', 'assistant', '2026-08-21 10:00:01'),
                    ('tool-1', 'tool', '2026-08-21 10:00:02'),
                    ('user-2', 'user', '2026-08-21 10:00:03'),
                ],
            )
            conn.commit()
            conn.close()

            authority = Mock()
            authority.get_agent_by_id = AsyncMock(return_value={
                "id": "agent-1",
                "name": "Split Agent",
                "icon": "bot",
                "metadata": {"engine": "standard"},
            })
            request = Request({
                "type": "http",
                "method": "GET",
                "path": "/api/v1/db/sessions",
                "query_string": b"",
                "headers": [(b"authorization", b"Bearer test")],
            })

            resolved_user_ids = []
            opened_user_ids = []

            def resolve_user_db(user_id):
                resolved_user_ids.append(user_id)
                return str(path)

            def open_user(_db):
                opened_user_ids.append(get_db_user_context())
                return sqlite3.connect(path), "sqlite"

            with patch("app.api.db_viewer._open_read", side_effect=open_user), \
                    patch("app.db.user_store._user_db_path", side_effect=resolve_user_db), \
                    patch("app.api.db_viewer.compute_session_manifest", return_value={
                        "authority_revision": 1,
                        "content_hash": "test",
                        "interaction_count": 0,
                        "max_session_seq": 0,
                        "dirty": False,
                    }), \
                    patch("app.api.db_viewer.decode_token", return_value={
                        "user_id": "admin", "sub": "admin"
                    }), \
                    patch("app.db.get_db", return_value=authority):
                result = _async(list_sessions(
                    request=request,
                    user_id="admin",
                    db="user.db",
                    agent_id=None,
                    limit=0,
                    include_hidden=False,
                    q=None,
                    include_recycled=False,
                ))

        self.assertEqual([row["id"] for row in result["sessions"]], ["session-1"])
        self.assertEqual(resolved_user_ids, ["admin"])
        self.assertTrue(opened_user_ids)
        self.assertEqual(set(opened_user_ids), {"admin"})
        self.assertEqual(result["sessions"][0]["agent_name"], "Split Agent")
        self.assertEqual(result["sessions"][0]["agent_engine"], "standard")
        self.assertEqual(result["sessions"][0]["activity_count"], 2)

    def test_session_picker_query_failure_is_not_reported_as_empty(self):
        from fastapi import HTTPException
        from starlette.requests import Request
        from app.api.db_viewer import list_sessions

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.db"
            sqlite3.connect(path).close()  # deliberately lacks the sessions table
            request = Request({
                "type": "http",
                "method": "GET",
                "path": "/api/v1/db/sessions",
                "query_string": b"",
                "headers": [(b"authorization", b"Bearer test")],
            })

            def open_user(_db):
                return sqlite3.connect(path), "sqlite"

            with patch("app.api.db_viewer._open_read", side_effect=open_user), \
                    patch("app.api.db_viewer._get_db_path", return_value=path), \
                    patch("app.api.db_viewer.decode_token", return_value={
                        "user_id": "admin", "sub": "admin"
                    }):
                with self.assertRaises(HTTPException) as raised:
                    _async(list_sessions(
                        request=request,
                        user_id="admin",
                        db="user.db",
                        agent_id=None,
                        limit=0,
                        include_hidden=False,
                        q=None,
                        include_recycled=False,
                    ))

        self.assertEqual(raised.exception.status_code, 500)

    def test_session_stats_resolves_agent_icon_from_agent_authority(self):
        from starlette.requests import Request
        from app.api.db_viewer import session_stats

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "admin.db"
            conn = sqlite3.connect(path)
            conn.executescript(render_plane("user"))
            conn.execute(
                "INSERT INTO sessions (id,user_id,title,agent_id,participants) "
                "VALUES ('session-1','admin','External session','external-agent','[]')"
            )
            conn.commit()
            conn.close()

            authority = Mock()
            authority.get_agent_by_id = AsyncMock(return_value={
                "id": "external-agent",
                "name": "External Agent",
                "metadata": '{"icon":"rocket","engine":"external"}',
            })
            request = Request({
                "type": "http",
                "method": "GET",
                "path": "/api/v1/db/session-stats",
                "query_string": b"",
                "headers": [],
            })

            def open_user(_db):
                return sqlite3.connect(path), "sqlite"

            with patch("app.api.db_viewer._open_read", side_effect=open_user), \
                    patch("app.api.db_viewer._get_db_path", return_value=path), \
                    patch("app.api.db_viewer._is_open_access_mode", return_value=True), \
                    patch("app.db.get_db", return_value=authority):
                result = _async(session_stats(
                    request=request,
                    user_id="admin",
                    db="user.db",
                    status="active",
                ))

        self.assertEqual(len(result["sessions"]), 1)
        row = result["sessions"][0]
        self.assertEqual(row["agent_name"], "External Agent")
        self.assertEqual(row["agent_icon"], "rocket")
        self.assertEqual(row["agent_engine"], "external")


class _IsolatedMigrator(StorageLayoutMigrator):
    async def _refresh_agent_bundles(self, report):
        # AgentStore intentionally resolves the real application workspace;
        # this isolated fixture has no agents and verifies the plane directly.
        mark_plane_status(
            "agent",
            "verified",
            path=self.app_db,
            summary={"expected": 0, "migrated": 0, "failed": 0},
        )


class StorageMigratorTests(unittest.TestCase):
    def test_activate_existing_layout_verifies_split_schemas(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_dir = root / "data" / "db"
            user_dir = root / "data" / "user_data" / "admin"
            db_dir.mkdir(parents=True)
            user_dir.mkdir(parents=True)

            app_path = db_dir / "app.db"
            app = sqlite3.connect(app_path)
            app.executescript(render_plane("app"))
            app.commit()
            app.close()

            user_path = user_dir / "admin.db"
            user = sqlite3.connect(user_path)
            user.executescript(render_plane("user"))
            user.commit()
            user.close()

            activated = StorageLayoutMigrator(root, app_db=app_path).activate_existing()

            self.assertEqual(activated["state"], "active")
            self.assertTrue(is_layout_active(path=app_path))
            app = sqlite3.connect(app_path)
            try:
                self.assertEqual(
                    app.execute(
                        "SELECT COUNT(*) FROM storage_migrations WHERE state='verified'"
                    ).fetchone()[0],
                    3,
                )
            finally:
                app.close()

    def test_activate_existing_layout_rejects_incomplete_user_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_dir = root / "data" / "db"
            user_dir = root / "data" / "user_data" / "admin"
            db_dir.mkdir(parents=True)
            user_dir.mkdir(parents=True)

            app_path = db_dir / "app.db"
            app = sqlite3.connect(app_path)
            app.executescript(render_plane("app"))
            app.commit()
            app.close()
            sqlite3.connect(user_dir / "admin.db").close()

            with self.assertRaisesRegex(RuntimeError, "user.*missing"):
                StorageLayoutMigrator(root, app_db=app_path).activate_existing()
            self.assertFalse(is_layout_active(path=app_path))

    def test_container_healthcheck_uses_database_readiness(self):
        dockerfile = Path(__file__).resolve().parents[1] / "Dockerfile"
        healthcheck = dockerfile.read_text(encoding="utf-8")
        self.assertIn("http://localhost:${APP_PORT}/health/ready", healthcheck)

    def test_apply_is_additive_and_keeps_legacy_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_dir = root / "data" / "db"
            user_dir = root / "data" / "user_data" / "admin"
            db_dir.mkdir(parents=True)
            user_dir.mkdir(parents=True)

            local_path = db_dir / "local.db"
            local = sqlite3.connect(local_path)
            local.executescript(render_sqlite())
            local.execute(
                "INSERT INTO sessions (id,user_id,title) VALUES ('s1','admin','hello')"
            )
            local.execute(
                """INSERT INTO interactions (id,session_id,role,content)
                   VALUES ('i1','s1','user','hello')"""
            )
            local.execute(
                "INSERT INTO channel_identities (id,channel,external_id,user_id) "
                "VALUES ('c1','web','external','admin')"
            )
            local.commit()
            local.close()

            global_path = db_dir / "global.db"
            global_conn = sqlite3.connect(global_path)
            global_conn.executescript(
                """CREATE TABLE agent_templates (
                       id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT, updated_at TEXT
                   );
                   CREATE TABLE agent_prompt_templates (
                       id TEXT PRIMARY KEY, template_id TEXT, slot_name TEXT, content TEXT
                   );
                   CREATE TABLE app_meta (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);
                   CREATE TABLE tools (id TEXT PRIMARY KEY, name TEXT);
                   CREATE TABLE agents (id TEXT PRIMARY KEY, name TEXT, status TEXT);"""
            )
            global_conn.execute(
                "INSERT INTO agent_templates (id,name,created_at,updated_at) "
                "VALUES ('t1','Default','2026-01-01','2026-01-01')"
            )
            global_conn.commit()
            global_conn.close()

            admin_path = user_dir / "admin.db"
            admin = sqlite3.connect(admin_path)
            admin.executescript(
                """CREATE TABLE user_profiles (
                       user_id TEXT PRIMARY KEY, is_admin INTEGER, default_agent_id TEXT,
                       created_at TEXT, updated_at TEXT, last_login_at TEXT,
                       appearance TEXT
                   );
                   CREATE TABLE user_accounts (
                       user_id TEXT PRIMARY KEY, username TEXT UNIQUE, password_hash TEXT,
                       display_name TEXT, remember_token TEXT, is_approved INTEGER,
                       session_lifetime_minutes INTEGER, auto_renew INTEGER, social_links TEXT,
                       created_at TEXT, updated_at TEXT
                   );"""
            )
            admin.execute(
                "INSERT INTO user_profiles (user_id,is_admin,updated_at) VALUES ('admin',1,'2026-01-02')"
            )
            admin.execute(
                """INSERT INTO user_accounts
                   (user_id,username,password_hash,is_approved,session_lifetime_minutes,
                    auto_renew,created_at,updated_at)
                   VALUES ('admin','admin','hash',1,60,1,'2026-01-01','2026-01-02')"""
            )
            admin.commit()
            admin.close()

            app_path = db_dir / "app.db"
            report = _IsolatedMigrator(root, app_db=app_path).run(apply=True)
            self.assertFalse(report.dry_run)
            self.assertTrue(local_path.exists())
            self.assertTrue(global_path.exists())

            app = sqlite3.connect(app_path)
            try:
                self.assertEqual(app.execute("SELECT COUNT(*) FROM agent_templates").fetchone()[0], 1)
                self.assertEqual(app.execute("SELECT COUNT(*) FROM channel_identities").fetchone()[0], 1)
                self.assertEqual(app.execute("SELECT is_admin FROM user_profiles WHERE user_id='admin'").fetchone()[0], 1)
            finally:
                app.close()

            user = sqlite3.connect(admin_path)
            try:
                self.assertEqual(user.execute("SELECT COUNT(*) FROM sessions WHERE id='s1'").fetchone()[0], 1)
                self.assertEqual(user.execute("SELECT COUNT(*) FROM interactions WHERE id='i1'").fetchone()[0], 1)
            finally:
                user.close()


if __name__ == "__main__":
    unittest.main()
