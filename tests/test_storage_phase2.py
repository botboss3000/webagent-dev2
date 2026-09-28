"""Phase 2 storage manifest, revocation, reservation, policy, and audit tests."""

from __future__ import annotations

import json
import multiprocessing
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.agent import turn_reservations
from app.auth import jwt, revocation
from app.db import browser_policy
from app.db.session_manifest import compute_session_manifest
from app.tools.execution_context import (
    ToolExecutionContext,
    current_tool_context,
    tool_execution_scope,
)
from app.tools.registry import _check_tool_code_safety
from scripts.audit_tool_db_handles import run_audit


INTERACTION_SCHEMA = """
CREATE TABLE interactions (
    id TEXT PRIMARY KEY, session_id TEXT, parent_id TEXT, role TEXT, content TEXT,
    tool_name TEXT, tool_call_id TEXT, channel TEXT, metadata TEXT, output TEXT,
    source TEXT, from_id TEXT, to_id TEXT, session_seq INTEGER, turn_id TEXT,
    turn_seq INTEGER, status TEXT, created_at TEXT
)
"""


def _reserve_in_process(path: str, start, output) -> None:
    from app.agent import turn_reservations as reservations

    reservations._DB_PATH = Path(path)
    start.wait(10)
    result = reservations.reserve_turn("u", "s", "multi", "hash")
    output.put(result.state)


class SessionManifestTests(unittest.TestCase):
    def test_manifest_is_stable_and_changes_on_append_or_edit(self):
        conn = sqlite3.connect(":memory:")
        conn.execute(INTERACTION_SCHEMA)
        conn.execute(
            "INSERT INTO interactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "one", "s", None, "user", "hello", None, None, "web", "{}", None,
                "user", "u", "a", 1, "turn", 0, "complete", "2026-01-01",
            ),
        )
        first = compute_session_manifest(conn, "s")
        self.assertEqual(first["authority_revision"], 1)
        self.assertEqual(first, compute_session_manifest(conn, "s"))
        conn.execute("UPDATE interactions SET content='changed' WHERE id='one'")
        edited = compute_session_manifest(conn, "s")
        self.assertGreater(edited["authority_revision"], first["authority_revision"])
        self.assertNotEqual(edited["content_hash"], first["content_hash"])
        conn.execute(
            "INSERT INTO interactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "two", "s", "one", "assistant", "reply", None, None, "web", "{}",
                None, "agent", "a", "u", 2, "turn", 1, "complete", "2026-01-02",
            ),
        )
        appended = compute_session_manifest(conn, "s")
        self.assertGreater(appended["authority_revision"], edited["authority_revision"])
        self.assertEqual(appended["interaction_count"], 2)
        conn.close()

    def test_split_database_manifest_is_created_in_user_store(self):
        with tempfile.TemporaryDirectory() as temp:
            main_path = Path(temp) / "main.db"
            user_path = Path(temp) / "user.db"
            conn = sqlite3.connect(main_path)
            conn.execute("ATTACH DATABASE ? AS _user", (str(user_path),))
            conn.execute(INTERACTION_SCHEMA.replace("CREATE TABLE interactions", "CREATE TABLE _user.interactions"))
            compute_session_manifest(conn, "s")
            self.assertIsNone(
                conn.execute(
                    "SELECT 1 FROM main.sqlite_master WHERE name='session_manifests'"
                ).fetchone()
            )
            self.assertIsNotNone(
                conn.execute(
                    "SELECT 1 FROM _user.sqlite_master WHERE name='session_manifests'"
                ).fetchone()
            )
            conn.close()


class TurnReservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path_patch = patch.object(
            turn_reservations, "_DB_PATH", Path(self.temp.name) / "turns.sqlite"
        )
        self.path_patch.start()

    def tearDown(self):
        self.path_patch.stop()
        self.temp.cleanup()

    def test_turn_reservation_replays_completion_and_rejects_changed_input(self):
        first = turn_reservations.reserve_turn("u", "s", "key", "hash")
        self.assertEqual(first.state, "acquired")
        busy = turn_reservations.reserve_turn("u", "s", "key", "hash")
        self.assertEqual(busy.state, "busy")
        self.assertTrue(turn_reservations.complete(first, {"status": "complete"}))
        replay = turn_reservations.reserve_turn("u", "s", "key", "hash")
        self.assertEqual(replay.state, "replay")
        self.assertEqual(replay.result, {"status": "complete"})
        conflict = turn_reservations.reserve_turn("u", "s", "key", "changed")
        self.assertEqual(conflict.state, "conflict")

    def test_expired_side_effecting_tool_becomes_uncertain(self):
        turn_key = turn_reservations.stable_key("turn")
        first = turn_reservations.reserve_tool(
            turn_key, "call", "send_email", {"to": "a"}, side_effecting=True
        )
        conn = sqlite3.connect(str(turn_reservations._DB_PATH))
        conn.execute(
            "UPDATE tool_reservations SET lease_expires_at=? WHERE reservation_key=?",
            (time.time() - 1, first.key),
        )
        conn.commit()
        conn.close()
        recovered = turn_reservations.reserve_tool(
            turn_key, "call", "send_email", {"to": "a"}, side_effecting=True
        )
        self.assertEqual(recovered.state, "uncertain")

    def test_expired_turn_requires_recovery_instead_of_reexecution(self):
        first = turn_reservations.reserve_turn(
            "u", "s", "expired", "hash", lease_seconds=1
        )
        conn = sqlite3.connect(str(turn_reservations._DB_PATH))
        conn.execute(
            "UPDATE turn_reservations SET lease_expires_at=? WHERE reservation_key=?",
            (time.time() - 1, first.key),
        )
        conn.commit()
        conn.close()
        recovered = turn_reservations.reserve_turn("u", "s", "expired", "hash")
        self.assertEqual(recovered.state, "uncertain")

    def test_only_one_process_acquires_the_same_turn(self):
        ctx = multiprocessing.get_context("spawn")
        start = ctx.Event()
        output = ctx.Queue()
        processes = [
            ctx.Process(
                target=_reserve_in_process,
                args=(str(turn_reservations._DB_PATH), start, output),
            )
            for _ in range(3)
        ]
        for process in processes:
            process.start()
        start.set()
        states = [output.get(timeout=20) for _ in processes]
        for process in processes:
            process.join(timeout=20)
            self.assertEqual(process.exitcode, 0)
        self.assertEqual(states.count("acquired"), 1)
        self.assertEqual(states.count("busy"), 2)


class RevocationEpochTests(unittest.TestCase):
    def test_revoke_invalidates_existing_token_but_not_new_login(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(
            revocation, "_DB_PATH", Path(temp) / "revocation.sqlite"
        ):
            token = jwt.create_access_token("person", "user-1")
            self.assertIsNotNone(jwt.decode_token(token))
            self.assertEqual(revocation.revoke_user("user-1"), 1)
            self.assertIsNone(jwt.decode_token(token))
            replacement = jwt.create_access_token("person", "user-1")
            self.assertEqual(jwt.decode_token(replacement)["rev"], 1)
            payload = jwt.decode_token(replacement)
            self.assertTrue(
                revocation.revoke_device("user-1", payload["device_id"])
            )
            self.assertIsNone(jwt.decode_token(replacement))


class BrowserPolicyTests(unittest.TestCase):
    def test_policy_save_is_clamped_atomic_and_does_not_enable_features(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(
            browser_policy, "_POLICY_PATH", Path(temp) / "policy.json"
        ), patch.dict("os.environ", {}, clear=False):
            policy = browser_policy.save_browser_storage_policy(
                {
                    "metadata_ttl_seconds": 1,
                    "max_cache_bytes": 10**20,
                    "telemetry_enabled": False,
                }
            )
            self.assertEqual(policy.metadata_ttl_seconds, 0)
            self.assertEqual(policy.max_cache_bytes, 2 * 1024 * 1024 * 1024)
            self.assertFalse(policy.telemetry_enabled)
            parsed = json.loads((Path(temp) / "policy.json").read_text())
            self.assertEqual(parsed["metadata_ttl_seconds"], 0)


class RawHandleAuditTests(unittest.TestCase):
    def test_every_raw_tool_handle_is_classified_and_context_bound(self):
        report = run_audit()
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["unclassified"], [])
        self.assertEqual(report["module_level_captures"], [])

    def test_tool_context_is_request_local_and_carries_idempotency(self):
        context = ToolExecutionContext(
            user_id="u",
            session_id="s",
            turn_key="turn",
            tool_name="send",
            tool_call_id="call",
            authority_mode="server",
            idempotency_key="receipt",
            side_effecting=True,
        )
        self.assertIsNone(current_tool_context())
        with tool_execution_scope(context):
            self.assertEqual(current_tool_context(), context)
        self.assertIsNone(current_tool_context())

    def test_dynamic_tools_cannot_import_or_open_application_database(self):
        self.assertIsNotNone(
            _check_tool_code_safety(
                "from app.db import get_db\nasync def run(): return get_db()._get_conn()"
            )
        )


if __name__ == "__main__":
    unittest.main()
