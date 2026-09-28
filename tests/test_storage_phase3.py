"""Phase 3 provider, purge, lifecycle-policy, and canary contract tests."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import time
import unittest
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from unittest.mock import AsyncMock

from app.agent import turn_reservations
from app.agent.browser_history_cache import (
    BrowserHistoryCache,
    BrowserTurnReplayCache,
)
from app import auth
from app.auth import jwt, revocation
from app.api import db_viewer
from app.db import browser_canary, browser_policy
from app.db import user_store
from app.db import session_manifest
from app.db.session_manifest import POSTGRES_MANIFEST_DDL, compute_session_manifest
from app.db.user_lifecycle import erase_user_data, export_user_data
from app.db.router import TenantRouterBackend
from app.tools.execution_context import ToolExecutionContext, tool_execution_scope
from app.tools.provider_idempotency import (
    google_calendar_create,
    microsoft_calendar_create,
)


class ProviderManifestTests(unittest.TestCase):
    _INTERACTIONS_SCHEMA = """
        CREATE TABLE interactions(
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            parent_id TEXT,
            role TEXT,
            content TEXT,
            tool_name TEXT,
            tool_call_id TEXT,
            channel TEXT,
            metadata TEXT,
            output TEXT,
            source TEXT,
            from_id TEXT,
            to_id TEXT,
            session_seq INTEGER,
            turn_id TEXT,
            turn_seq INTEGER,
            status TEXT,
            created_at TEXT
        )
    """

    @staticmethod
    def _insert_interaction(
        conn: sqlite3.Connection,
        interaction_id: str,
        session_id: str,
        sequence: int,
        content: str = "content",
    ) -> None:
        conn.execute(
            """INSERT INTO interactions(
                   id,session_id,role,content,session_seq,status,created_at
               ) VALUES (?,?,?,?,?,'complete',?)""",
            (
                interaction_id,
                session_id,
                "user",
                content,
                sequence,
                f"2026-07-30T00:00:{sequence:02d}Z",
            ),
        )

    def test_postgres_trigger_advances_revision_and_marks_dirty(self):
        self.assertIn("AFTER INSERT OR UPDATE OR DELETE ON interactions", POSTGRES_MANIFEST_DDL)
        self.assertIn("authority_revision + 1", POSTGRES_MANIFEST_DDL)
        self.assertIn("dirty = 1", POSTGRES_MANIFEST_DDL)
        migration = (
            Path(__file__).parents[1]
            / "supabase"
            / "migrations"
            / "20260730000000_session_manifest_maintenance.sql"
        ).read_text(encoding="utf-8")
        self.assertIn("webagent_mark_session_manifest_dirty", migration)

    def test_provider_path_does_not_poison_transaction_with_sqlite_probe(self):
        class Cursor:
            rowcount = 0

            def __init__(self, row=None):
                self._row = row

            def fetchone(self):
                return self._row

        class ProviderConnection:
            def __init__(self):
                self.statements = []

            def execute(self, sql, params=()):
                self.statements.append(sql)
                if "LIMIT 1" in sql:
                    return Cursor(None)
                if "content_hash" in sql and "WHERE session_id" in sql:
                    return Cursor((7, "provider-hash", 2, 2, 0))
                raise AssertionError(f"Unexpected provider SQL: {sql}")

        connection = ProviderConnection()
        manifest = compute_session_manifest(connection, "session")
        self.assertEqual(manifest["content_hash"], "provider-hash")
        self.assertFalse(
            any("sqlite_version" in sql for sql in connection.statements)
        )

    def test_rebuild_retries_when_trigger_advances_revision_during_hashing(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript(self._INTERACTIONS_SCHEMA)
        self._insert_interaction(conn, "i1", "s1", 1, "before")
        compute_session_manifest(conn, "s1")
        conn.execute("UPDATE interactions SET content='dirty' WHERE id='i1'")
        conn.commit()

        original = session_manifest.manifest_from_rows
        injected = False

        def mutate_after_snapshot(rows):
            nonlocal injected
            result = original(rows)
            if not injected:
                injected = True
                self._insert_interaction(conn, "i2", "s1", 2, "racing write")
            return result

        with patch.object(
            session_manifest,
            "manifest_from_rows",
            side_effect=mutate_after_snapshot,
        ):
            rebuilt = compute_session_manifest(conn, "s1")

        durable = conn.execute(
            "SELECT interaction_count,dirty,content_hash "
            "FROM session_manifests WHERE session_id='s1'"
        ).fetchone()
        self.assertTrue(injected)
        self.assertEqual(rebuilt["interaction_count"], 2)
        self.assertEqual(durable[0], 2)
        self.assertEqual(durable[1], 0)
        self.assertEqual(durable[2], rebuilt["content_hash"])
        conn.close()

    def test_sqlite_session_move_dirties_old_and_new_manifests(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript(self._INTERACTIONS_SCHEMA)
        self._insert_interaction(conn, "i1", "old-session", 1)
        compute_session_manifest(conn, "old-session")
        compute_session_manifest(conn, "new-session")

        conn.execute(
            "UPDATE interactions SET session_id='new-session' WHERE id='i1'"
        )
        conn.commit()
        states = dict(
            conn.execute(
                "SELECT session_id,dirty FROM session_manifests "
                "WHERE session_id IN ('old-session','new-session')"
            ).fetchall()
        )
        self.assertEqual(states, {"new-session": 1, "old-session": 1})
        self.assertEqual(
            compute_session_manifest(conn, "old-session")["interaction_count"],
            0,
        )
        self.assertEqual(
            compute_session_manifest(conn, "new-session")["interaction_count"],
            1,
        )
        conn.close()


class ProviderIdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path_patch = patch.object(
            turn_reservations, "_DB_PATH", Path(self.temp.name) / "turns.sqlite"
        )
        self.path_patch.start()

    def tearDown(self):
        self.path_patch.stop()
        self.temp.cleanup()

    def test_calendar_adapters_persist_native_reconciliation_ids_and_allow_safe_retry(self):
        turn = turn_reservations.reserve_turn("u", "s", "request-key", "hash")
        tool = turn_reservations.reserve_tool(
            turn.key,
            "call",
            "gcal_create_event",
            {"summary": "demo"},
            side_effecting=True,
            lease_seconds=1,
        )
        context = ToolExecutionContext(
            user_id="u",
            session_id="s",
            turn_key=turn.key,
            tool_name="gcal_create_event",
            tool_call_id="call",
            authority_mode="server",
            idempotency_key=tool.key,
            side_effecting=True,
        )
        with tool_execution_scope(context):
            google = google_calendar_create()
            microsoft = microsoft_calendar_create()
        self.assertRegex(google.resource_id, r"^[0-9a-v]{5,1024}$")
        self.assertEqual(len(microsoft.resource_id), 36)
        hint = turn_reservations.get_provider_reconciliation(tool.key)
        self.assertTrue(hint["provider_idempotent"])

        conn = sqlite3.connect(str(turn_reservations._DB_PATH))
        conn.execute(
            "UPDATE tool_reservations SET lease_expires_at=? WHERE reservation_key=?",
            (time.time() - 1, tool.key),
        )
        conn.commit()
        conn.close()
        retried = turn_reservations.reserve_tool(
            turn.key,
            "new-call-id",
            "gcal_create_event",
            {"summary": "demo"},
            side_effecting=True,
        )
        self.assertEqual(retried.state, "acquired")

    def test_user_receipts_are_deletable_by_pseudonymous_owner(self):
        turn = turn_reservations.reserve_turn("u", "s", "key", "hash")
        turn_reservations.reserve_tool(
            turn.key, "call", "send", {"x": 1}, side_effecting=True
        )
        self.assertEqual(turn_reservations.delete_user_reservations("u"), 2)


class ProviderReconciliationTests(unittest.IsolatedAsyncioTestCase):
    async def test_google_duplicate_create_reconciles_existing_event(self):
        from app.integrations import calendar_tools

        calls = AsyncMock(
            side_effect=[
                {
                    "status": "error",
                    "http_status": 409,
                    "body": {"error": {"message": "The requested identifier already exists."}},
                    "url": "insert",
                },
                {
                    "status": "ok",
                    "http_status": 200,
                    "body": {"id": "stableeventid", "summary": "demo"},
                    "url": "get",
                },
            ]
        )
        with patch.object(
            calendar_tools,
            "google_calendar_create",
            return_value=SimpleNamespace(resource_id="stableeventid"),
        ), patch.object(calendar_tools, "oauth_api_call", calls):
            payload = json.loads(
                await calendar_tools.gcal_create_event(
                    "u",
                    "agent",
                    "demo",
                    "2026-07-30T10:00:00Z",
                    "2026-07-30T11:00:00Z",
                )
            )

        self.assertEqual(payload["status"], "ok")
        self.assertTrue(payload["reconciled"])
        self.assertEqual(payload["body"]["id"], "stableeventid")
        self.assertEqual(calls.await_count, 2)
        first, second = calls.await_args_list
        self.assertEqual(first.args[3], "POST")
        self.assertEqual(first.kwargs["json_body"]["id"], "stableeventid")
        self.assertEqual(second.args[3], "GET")
        self.assertTrue(second.args[4].endswith("/events/stableeventid"))

    async def test_google_conflict_without_provider_id_is_not_reclassified(self):
        from app.integrations import calendar_tools

        calls = AsyncMock(
            return_value={
                "status": "error",
                "http_status": 409,
                "body": {"error": "conflict"},
            }
        )
        with patch.object(
            calendar_tools, "google_calendar_create", return_value=None
        ), patch.object(calendar_tools, "oauth_api_call", calls):
            payload = json.loads(
                await calendar_tools.gcal_create_event(
                    "u",
                    "agent",
                    "demo",
                    "2026-07-30T10:00:00Z",
                    "2026-07-30T11:00:00Z",
                )
            )

        self.assertEqual(payload["status"], "error")
        self.assertEqual(calls.await_count, 1)


class DevicePurgeTests(unittest.TestCase):
    def test_bulk_revoke_is_scoped_atomic_and_requires_every_target_to_purge(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(
            revocation, "_DB_PATH", Path(temp) / "revocation.sqlite"
        ):
            first = jwt.create_access_token(
                "person", "user-1", device_id="first-device"
            )
            second = jwt.create_access_token(
                "person", "user-1", device_id="second-device"
            )
            untouched = jwt.create_access_token(
                "person", "user-1", device_id="untouched-device"
            )

            revoked = revocation.revoke_devices(
                "user-1", ["second-device", "first-device", "second-device"]
            )

            self.assertEqual(revoked, ["second-device", "first-device"])
            self.assertIsNone(jwt.decode_token(first))
            self.assertIsNone(jwt.decode_token(second))
            self.assertIsNotNone(jwt.decode_token(untouched))
            self.assertTrue(
                revocation.device_purge_status("user-1", "first-device")[
                    "purge_required"
                ]
            )
            self.assertTrue(
                revocation.device_purge_status("user-1", "second-device")[
                    "purge_required"
                ]
            )

    def test_revoked_signed_device_can_ack_purge_without_becoming_authenticated(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(
            revocation, "_DB_PATH", Path(temp) / "revocation.sqlite"
        ):
            token = jwt.create_access_token("person", "user-1")
            payload = jwt.decode_token(token)
            revocation.revoke_user("user-1")
            self.assertIsNone(jwt.decode_token(token))
            self.assertEqual(jwt.decode_signed_token(token)["device_id"], payload["device_id"])
            state = revocation.device_purge_status("user-1", payload["device_id"])
            self.assertTrue(state["purge_required"])
            self.assertTrue(
                revocation.acknowledge_device_purge("user-1", payload["device_id"])
            )
            self.assertTrue(
                revocation.device_purge_status("user-1", payload["device_id"])[
                    "purge_acknowledged"
                ]
            )

    def test_bound_remember_token_dies_with_revoked_device(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(
            revocation, "_DB_PATH", Path(temp) / "revocation.sqlite"
        ):
            jwt.create_access_token(
                "person", "user-1", device_id="remembered-device"
            )
            self.assertTrue(
                revocation.bind_remember_token(
                    "user-1", "remembered-device", "remember-secret"
                )
            )
            self.assertEqual(
                revocation.remember_token_device("user-1", "remember-secret"),
                "remembered-device",
            )
            conn = sqlite3.connect(str(revocation._DB_PATH))
            stored_hash = conn.execute(
                "SELECT remember_token_hash FROM auth_devices "
                "WHERE device_id='remembered-device'"
            ).fetchone()[0]
            conn.close()
            self.assertNotEqual(stored_hash, "remember-secret")
            self.assertTrue(
                revocation.revoke_device("user-1", "remembered-device")
            )
            self.assertIsNone(
                revocation.remember_token_device("user-1", "remember-secret")
            )
            self.assertIsNone(
                revocation.claim_legacy_remember_token(
                    "user-1", "remember-secret"
                )
            )
            reminted = jwt.create_access_token(
                "person", "user-1", device_id="remembered-device"
            )
            self.assertIsNone(jwt.decode_token(reminted))
            self.assertTrue(
                revocation.device_purge_status(
                    "user-1", "remembered-device"
                )["purge_required"]
            )

    def test_device_gc_keeps_old_unacknowledged_purge(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(
            revocation, "_DB_PATH", Path(temp) / "revocation.sqlite"
        ):
            jwt.create_access_token(
                "person", "user-1", device_id="offline-device"
            )
            self.assertTrue(revocation.revoke_device("user-1", "offline-device"))
            conn = sqlite3.connect(str(revocation._DB_PATH))
            conn.execute(
                "UPDATE auth_devices SET last_seen=? WHERE device_id=?",
                (int(time.time()) - 100 * 24 * 3600, "offline-device"),
            )
            conn.commit()
            conn.close()

            # Device registration runs the registry's 90-day garbage collection.
            jwt.create_access_token(
                "person", "other-user", device_id="other-device"
            )
            self.assertTrue(
                revocation.device_purge_status(
                    "user-1", "offline-device"
                )["purge_required"]
            )

    def test_signed_in_device_list_hides_revoked_rows_and_includes_login_context(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(
            revocation, "_DB_PATH", Path(temp) / "revocation.sqlite"
        ):
            jwt.create_access_token("person", "user-1", device_id="active-device")
            jwt.create_access_token("person", "user-1", device_id="old-device")
            self.assertTrue(
                revocation.update_device_metadata(
                    "user-1",
                    "active-device",
                    ip_address="203.0.113.10",
                    location="New York, New York, United States",
                    user_agent="Test Browser",
                )
            )
            self.assertTrue(revocation.revoke_device("user-1", "old-device"))

            devices = revocation.list_devices("user-1")

            self.assertEqual([row["device_id"] for row in devices], ["active-device"])
            self.assertEqual(devices[0]["ip_address"], "203.0.113.10")
            self.assertEqual(
                devices[0]["location"], "New York, New York, United States"
            )
            self.assertEqual(devices[0]["user_agent"], "Test Browser")

    def test_explicit_reauthentication_can_reuse_a_revoked_stable_device(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(
            revocation, "_DB_PATH", Path(temp) / "revocation.sqlite"
        ):
            old_token = jwt.create_access_token(
                "person", "user-1", device_id="stable-browser"
            )
            self.assertTrue(revocation.revoke_device("user-1", "stable-browser"))
            self.assertIsNone(jwt.decode_token(old_token))

            passive_token = jwt.create_access_token(
                "person", "user-1", device_id="stable-browser"
            )
            self.assertIsNone(jwt.decode_token(passive_token))
            login_token = jwt.create_access_token(
                "person",
                "user-1",
                device_id="stable-browser",
                reauthenticated=True,
            )
            self.assertIsNotNone(jwt.decode_token(login_token))
            self.assertIsNone(jwt.decode_token(old_token))
            self.assertIsNone(jwt.decode_token(passive_token))


class DeviceLogoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_socket_revocation_closes_only_selected_device(self):
        from app.api import chat

        selected_socket = SimpleNamespace(
            send_text=AsyncMock(), close=AsyncMock()
        )
        other_socket = SimpleNamespace(send_text=AsyncMock(), close=AsyncMock())
        chat._user_listeners["user-1"] = [
            (selected_socket, "selected-device"),
            (other_socket, "other-device"),
        ]
        event = {"type": "device_revoked", "device_ids": ["selected-device"]}
        try:
            await chat.revoke_user_device_connections(
                "user-1", ["selected-device"], event
            )
            selected_socket.send_text.assert_awaited_once()
            selected_socket.close.assert_awaited_once_with(
                code=4401, reason="Device session revoked"
            )
            other_socket.send_text.assert_not_awaited()
            other_socket.close.assert_not_awaited()
            self.assertEqual(chat._user_listeners["user-1"], [
                (other_socket, "other-device")
            ])
        finally:
            chat._user_listeners.pop("user-1", None)

    async def test_bulk_endpoint_pushes_immediate_revocation_to_connected_devices(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(
            revocation, "_DB_PATH", Path(temp) / "revocation.sqlite"
        ):
            jwt.create_access_token("person", "user-1", device_id="first-device")
            jwt.create_access_token("person", "user-1", device_id="second-device")
            disconnect = AsyncMock()
            with patch("app.auth._require_auth", return_value=("person", "user-1")), patch(
                "app.api.chat.revoke_user_device_connections", new=disconnect
            ):
                response = await auth.revoke_my_devices(
                    SimpleNamespace(headers={}),
                    auth.RevokeDevicesRequest(
                        device_ids=["first-device", "second-device"]
                    ),
                )

            self.assertEqual(response["revoked_count"], 2)
            self.assertEqual(
                response["revoked_device_ids"], ["first-device", "second-device"]
            )
            disconnect.assert_awaited_once_with(
                "user-1",
                ["first-device", "second-device"],
                {
                    "type": "device_revoked",
                    "device_ids": ["first-device", "second-device"],
                    "purge_required": True,
                },
            )

    async def test_logout_revokes_only_calling_device(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(
            revocation, "_DB_PATH", Path(temp) / "revocation.sqlite"
        ):
            first_token = jwt.create_access_token(
                "person", "user-1", device_id="first-device"
            )
            second_token = jwt.create_access_token(
                "person", "user-1", device_id="second-device"
            )
            request = SimpleNamespace(
                headers={"Authorization": f"Bearer {first_token}"},
                query_params={},
            )

            response = await auth.logout(request)

            self.assertEqual(response["message"], "Logged out on this device.")
            self.assertEqual(response["device_id"], "first-device")
            self.assertTrue(response["revoked"])
            self.assertIsNone(jwt.decode_token(first_token))
            self.assertIsNotNone(jwt.decode_token(second_token))
            self.assertEqual(revocation.current_epoch("user-1"), 0)


class DeviceLoginTests(unittest.IsolatedAsyncioTestCase):
    async def test_social_login_state_carries_stable_device_id(self):
        from app.auth_providers import flow

        state = flow.make_state("example", device_id="stable-browser")
        payload = flow.read_state_payload(state, "example")

        self.assertIsNotNone(payload)
        self.assertEqual(payload["device_id"], "stable-browser")
        self.assertIsNone(flow.read_state_payload(state, "different-provider"))

    async def test_login_reuses_browser_id_and_records_ip_location_and_user_agent(self):
        from app.auth.users import User

        user = User(
            username="person@example.com",
            password_hash="",
            user_id="user-1",
            display_name="Person",
            auto_renew=False,
        )
        profile_db = SimpleNamespace(upsert_user_profile=AsyncMock())
        request = SimpleNamespace(
            headers={"user-agent": "Test Browser on Test OS"},
            client=SimpleNamespace(host="127.0.0.1"),
        )
        with tempfile.TemporaryDirectory() as temp, patch.object(
            revocation, "_DB_PATH", Path(temp) / "revocation.sqlite"
        ), patch(
            "app.auth.authenticate", new=AsyncMock(return_value=user)
        ), patch(
            "app.admin.settings.get_access_mode", return_value="public_registered"
        ), patch(
            "app.auth.get_db", return_value=profile_db
        ), patch(
            "app.auth._record_auth_event"
        ):
            response = await auth.login(
                request,
                auth.LoginRequest(
                    email=user.username,
                    password="password",
                    device_id="stable-browser",
                ),
            )

            payload = jwt.decode_token(response.access_token)
            devices = revocation.list_devices(user.user_id)
            self.assertEqual(payload["device_id"], "stable-browser")
            self.assertEqual(len(devices), 1)
            self.assertEqual(devices[0]["location"], "Local network")
            self.assertEqual(devices[0]["ip_address"], "127.0.0.1")
            self.assertEqual(devices[0]["user_agent"], "Test Browser on Test OS")


class RememberRecallTests(unittest.IsolatedAsyncioTestCase):
    async def test_recall_reuses_bound_device_id(self):
        from app.auth import RecallRequest, recall
        from app.auth.users import User

        user = User(
            username="person@example.com",
            password_hash="",
            user_id="user-1",
            display_name="Person",
            remember_token="remember-secret",
        )
        profile_db = SimpleNamespace(upsert_user_profile=AsyncMock())
        with tempfile.TemporaryDirectory() as temp, patch.object(
            revocation, "_DB_PATH", Path(temp) / "revocation.sqlite"
        ), patch(
            "app.auth.resolve_remember_token",
            new=AsyncMock(return_value=user),
        ), patch("app.auth.get_db", return_value=profile_db):
            jwt.create_access_token(
                user.username, user.user_id, device_id="remembered-device"
            )
            revocation.bind_remember_token(
                user.user_id, "remembered-device", "remember-secret"
            )

            response = await recall(
                RecallRequest(remember_token="remember-secret")
            )
            payload = jwt.decode_token(response.access_token)
            self.assertEqual(payload["device_id"], "remembered-device")
            self.assertEqual(len(revocation.list_devices(user.user_id)), 1)

    async def test_recall_does_not_mint_for_revoked_bound_device(self):
        from fastapi import HTTPException
        from app.auth import RecallRequest, recall
        from app.auth.users import User

        user = User(
            username="person@example.com",
            password_hash="",
            user_id="user-1",
            display_name="Person",
            remember_token="remember-secret",
        )
        with tempfile.TemporaryDirectory() as temp, patch.object(
            revocation, "_DB_PATH", Path(temp) / "revocation.sqlite"
        ), patch(
            "app.auth.resolve_remember_token",
            new=AsyncMock(return_value=user),
        ):
            jwt.create_access_token(
                user.username, user.user_id, device_id="remembered-device"
            )
            revocation.bind_remember_token(
                user.user_id, "remembered-device", "remember-secret"
            )
            revocation.revoke_device(user.user_id, "remembered-device")

            with patch("app.auth.create_access_token") as mint:
                with self.assertRaises(HTTPException) as raised:
                    await recall(RecallRequest(remember_token="remember-secret"))
                self.assertEqual(raised.exception.status_code, 401)
                mint.assert_not_called()


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_delete_and_export_cover_transcript_memory_attachment_and_account(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE sessions(id TEXT PRIMARY KEY,user_id TEXT);
            CREATE TABLE interactions(id TEXT,session_id TEXT);
            CREATE TABLE session_manifests(session_id TEXT);
            CREATE TABLE attachments(
                id TEXT,session_id TEXT,user_id TEXT,storage_path TEXT,
                storage_provider TEXT,mime_type TEXT
            );
            CREATE TABLE memories(id TEXT,user_id TEXT);
            CREATE TABLE memory_chunks(id TEXT,memory_id TEXT);
            CREATE TABLE user_profiles(user_id TEXT);
            CREATE TABLE user_accounts(
                user_id TEXT,password_hash TEXT,remember_token TEXT
            );
            CREATE TABLE browser_sync_receipts(
                mutation_id TEXT,user_id TEXT,session_id TEXT,
                request_hash TEXT,result_json TEXT,created_at TEXT
            );
            CREATE TABLE agent_automations(
                id TEXT,owner_user_id TEXT,task_label TEXT,fire_token TEXT
            );
            """
        )
        conn.execute("INSERT INTO sessions VALUES ('s','u')")
        conn.execute("INSERT INTO interactions VALUES ('i','s')")
        conn.execute("INSERT INTO session_manifests VALUES ('s')")
        conn.execute(
            "INSERT INTO attachments VALUES "
            "('a','s','u','indexeddb://a','browser','text/plain')"
        )
        conn.execute("INSERT INTO memories VALUES ('m','u')")
        conn.execute("INSERT INTO memory_chunks VALUES ('c','m')")
        conn.execute("INSERT INTO user_profiles VALUES ('u')")
        conn.execute("INSERT INTO user_accounts VALUES ('u','secret','remember')")
        conn.execute(
            "INSERT INTO browser_sync_receipts VALUES "
            "('receipt','u','s','hash','{}','now')"
        )
        conn.execute(
            "INSERT INTO agent_automations VALUES "
            "('automation','u','demo','bearer-secret')"
        )
        conn.commit()

        exported = await export_user_data(conn, "u")
        self.assertEqual(len(exported["server_authority"]["interactions"]), 1)
        account = exported["server_authority"]["user_accounts"][0]
        self.assertNotIn("password_hash", account)
        self.assertNotIn("remember_token", account)
        automation = exported["server_authority"]["agent_automations"][0]
        self.assertNotIn("fire_token", automation)
        self.assertTrue(exported["attachment_blobs"][0]["browser_local"])

        deleted = await erase_user_data(conn, "u")
        self.assertEqual(deleted["interactions"], 1)
        self.assertEqual(deleted["memory_chunks"], 1)
        self.assertEqual(deleted["browser_sync_receipts"], 1)
        self.assertEqual(deleted["user_accounts"], 1)
        conn.close()

    async def test_sidecar_export_and_removal_include_sync_receipts(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(
            user_store, "USER_DATA_DIR", temp
        ):
            store = user_store.get_user_store("user-1")
            conn = store._get_conn()
            conn.execute(
                "INSERT INTO browser_sync_receipts VALUES (?,?,?,?,?,?)",
                ("receipt", "user-1", "session", "hash", "{}", "now"),
            )
            conn.commit()
            exported = await user_store.export_user_store_data("user-1")
            self.assertEqual(
                len(exported["server_authority"]["browser_sync_receipts"]), 1
            )
            path = Path(store._db_path)
            erased = await user_store.purge_user_store_files(
                "user-1", remove_files=False
            )
            self.assertEqual(erased["browser_sync_receipts"], 1)
            self.assertTrue(path.exists())
            removed = await user_store.purge_user_store_files("user-1")
            self.assertGreaterEqual(removed["sidecar_files"], 1)
            self.assertFalse(path.exists())

    async def test_process_local_browser_caches_purge_by_owner(self):
        history = BrowserHistoryCache()
        replay = BrowserTurnReplayCache()
        await history.put("u", "s", revision=1, history=[{"role": "user"}])
        await history.put("other", "s", revision=1, history=[])
        await replay.put("u", "s", "key", "hash", [{"type": "done"}])
        self.assertEqual(await history.purge_user("u"), 1)
        self.assertEqual(await replay.purge_user("u"), 1)
        self.assertIsNone(
            await replay.get("u", "s", "key", "hash")
        )
        self.assertEqual(await history.purge_user("u"), 0)

    async def test_tenant_lifecycle_visits_personal_and_control_planes(self):
        class Backend:
            def __init__(self, name):
                self.name = name
                self.erased = []

            async def erase_user_owned_data(self, user_id):
                self.erased.append(user_id)
                return {"sessions": 1}

            async def export_user_data(self, user_id):
                return {"user_id": user_id, "source": self.name}

        control = Backend("control")
        personal = Backend("personal")
        router = TenantRouterBackend(control)
        with patch(
            "app.db.tenant.resolve_data_backend", return_value=personal
        ):
            counts = await router.erase_user_owned_data("u")
            exported = await router.export_user_data("u")
        self.assertEqual(personal.erased, ["u"])
        self.assertEqual(control.erased, ["u"])
        self.assertEqual(counts["control:sessions"], 1)
        self.assertEqual(exported["source"], "personal")
        self.assertEqual(exported["control_plane"]["source"], "control")

    def test_policy_guards(self):
        disabled = browser_policy.BrowserStoragePolicy(
            export_enabled=False, delete_enabled=False
        )
        with patch.object(browser_policy, "load_browser_storage_policy", return_value=disabled):
            with self.assertRaises(PermissionError):
                browser_policy.require_export_enabled()
            with self.assertRaises(PermissionError):
                browser_policy.require_delete_enabled()


class CanaryTests(unittest.TestCase):
    def test_canary_is_stable_and_rollback_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(
            browser_canary, "_ROLLBACK_MARKER", Path(temp) / "rollback"
        ), patch.dict(
            os.environ,
            {"WEBAGENT_BROWSER_CACHE_CANARY_PERCENT": "25"},
            clear=False,
        ):
            first = browser_canary.cache_canary_eligible("user-1")
            self.assertEqual(first, browser_canary.cache_canary_eligible("user-1"))
            browser_canary.set_rollback(True)
            self.assertFalse(browser_canary.cache_canary_eligible("user-1"))
            browser_canary.set_rollback(False)
            self.assertEqual(first, browser_canary.cache_canary_eligible("user-1"))

    def test_manifest_validation_rechecks_runtime_rollback(self):
        manifest = {"authority_revision": 7, "content_hash": "current"}
        with tempfile.TemporaryDirectory() as temp, patch.object(
            browser_canary, "_ROLLBACK_MARKER", Path(temp) / "rollback"
        ), patch.object(
            db_viewer, "rollback_active", side_effect=browser_canary.rollback_active
        ):
            self.assertTrue(
                db_viewer._manifest_cache_not_modified(7, "current", manifest)
            )
            browser_canary.set_rollback(True)
            self.assertFalse(
                db_viewer._manifest_cache_not_modified(7, "current", manifest)
            )


if __name__ == "__main__":
    unittest.main()
