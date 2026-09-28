"""Regression coverage for the hybrid remote transcript skeleton."""

import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.api.db_viewer import (
    _compact_sqlite_for_quota,
    _enforce_user_database_size_limit,
    _hard_delete_family,
    list_session_notifications,
)
from app.db.sync.engine import SYNCED_SPECS, SyncEngine
from app.db.hybrid import HybridBackend


class _CaptureTable:
    def __init__(self):
        self.payload = None

    def upsert(self, payload, on_conflict):
        self.payload = (payload, on_conflict)
        return self

    def execute(self):
        return self


class _CaptureClient:
    def __init__(self):
        self.table_api = _CaptureTable()

    def table(self, name):
        self.name = name
        return self.table_api


class _CaptureRemote:
    def __init__(self):
        self.client = _CaptureClient()

    def get_raw_client(self):
        return self.client


class _RowsTable:
    def __init__(self, rows):
        self.rows = rows
        self.upserts = []

    def select(self, _cols):
        return self

    def eq(self, _column, _value):
        return self

    def upsert(self, row, on_conflict):
        self.upserts.append((row, on_conflict))
        return self

    def execute(self):
        return type("Result", (), {"data": self.rows})()


class _RowsBackend:
    def __init__(self, rows):
        self.table_api = _RowsTable(rows)

    def get_raw_client(self):
        return self

    def table(self, _name):
        return self.table_api


class HybridSyncPayloadTests(unittest.TestCase):
    def _push(self, row):
        engine = SyncEngine.__new__(SyncEngine)
        engine._remote = _CaptureRemote()
        engine._push_row("interactions", SYNCED_SPECS["interactions"], row)
        return engine._remote.client.table_api.payload[0]

    def test_tool_result_is_always_remote_placeholder(self):
        payload = self._push({
            "id": "tool-1", "role": "tool", "content": "small result",
            "metadata": '{"trace":"abc"}', "output": "sensitive output",
        })
        self.assertEqual(
            payload["content"],
            "[tool call processed on local device; recall unavailable remotely]",
        )
        self.assertIsNone(payload["output"])
        self.assertEqual(payload["metadata"], json.dumps({
            "remote_placeholder": True, "local_payload": "tool_execution",
        }, separators=(",", ":")))

    def test_user_text_is_kept_for_handoff(self):
        content = "x" * 4096
        payload = self._push({"id": "user-1", "role": "user", "content": content})
        self.assertEqual(payload["content"], content)

    def test_legacy_interaction_input_is_never_pushed(self):
        payload = self._push({
            "id": "user-1", "role": "user", "content": "Current message",
            "input": "legacy duplicate payload",
        })
        self.assertNotIn("input", payload)

    def test_tool_call_payload_is_always_local_only(self):
        payload = self._push({
            "id": "assistant-1", "role": "assistant",
            "output": json.dumps({"tool_calls": [{
                "id": "call-1", "type": "function",
                "function": {"name": "read_file", "arguments": '{"path":"secret.txt"}'},
            }]}),
        })
        out = json.loads(payload["output"])
        self.assertTrue(out["_remote_placeholder"])
        self.assertEqual(out["local_payload"], "tool_calls")
        self.assertEqual(out["tool_calls_processed_locally"], 1)
        self.assertNotIn("tool_calls", out)

    def test_session_metadata_refresh_applies_only_newer_remote_rows(self):
        hybrid = HybridBackend.__new__(HybridBackend)
        hybrid._remote = _RowsBackend([{
            "id": "remote-new", "user_id": "user-1", "title": "Renamed elsewhere",
            "pinned": 1, "updated_at": "2026-07-17T12:01:00+00:00",
        }, {
            "id": "local-newer", "user_id": "user-1", "title": "Stays local",
            "pinned": 0, "updated_at": "2026-07-17T12:00:00+00:00",
        }])
        hybrid._local = _RowsBackend([{
            "id": "remote-new", "user_id": "user-1", "title": "Old name",
            "pinned": 0, "updated_at": "2026-07-17 12:00:00",
        }, {
            "id": "local-newer", "user_id": "user-1", "title": "Stays local",
            "pinned": 1, "updated_at": "2026-07-17 12:02:00",
        }])

        changed = hybrid._refresh_session_metadata_sync("user-1")

        self.assertEqual(changed, ["remote-new"])
        self.assertEqual(len(hybrid._local.table_api.upserts), 1)
        self.assertEqual(hybrid._local.table_api.upserts[0][0]["title"], "Renamed elsewhere")

    def test_hard_session_delete_cascades_session_state_and_spawn_ledger(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript("""
            CREATE TABLE sessions (id TEXT PRIMARY KEY);
            CREATE TABLE interactions (session_id TEXT);
            CREATE TABLE session_summaries (session_id TEXT);
            CREATE TABLE session_summary_segments (session_id TEXT);
            CREATE TABLE pipeline_events (session_id TEXT);
            CREATE TABLE session_runs (session_id TEXT);
            CREATE TABLE messages (session_id TEXT);
            CREATE TABLE browser_sessions (session_id TEXT);
            CREATE TABLE session_notifications (session_id TEXT PRIMARY KEY);
            CREATE TABLE agent_spawns (orchestrator_session_id TEXT, spawn_session_id TEXT);
        """)
        conn.execute("INSERT INTO sessions VALUES ('session-1')")
        for table in ("interactions", "session_summaries", "session_summary_segments",
                      "pipeline_events", "session_runs", "messages", "browser_sessions"):
            conn.execute(f"INSERT INTO {table} VALUES ('session-1')")
        conn.execute("INSERT INTO agent_spawns VALUES ('session-1', 'child-1')")
        conn.execute("INSERT INTO agent_spawns VALUES ('parent-1', 'session-1')")
        conn.execute("INSERT INTO session_notifications VALUES ('session-1')")
        conn.execute("INSERT INTO session_notifications VALUES ('other-session')")

        _hard_delete_family(conn, ["session-1"])

        for table in ("sessions", "interactions", "session_summaries", "session_summary_segments",
                      "pipeline_events", "session_runs", "messages", "browser_sessions", "agent_spawns"):
            self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
        self.assertEqual(
            conn.execute("SELECT session_id FROM session_notifications").fetchall(),
            [("other-session",)],
        )


    def test_notification_list_hides_orphaned_session_rows(self):
        with tempfile.TemporaryDirectory() as tempdir:
            db_path = Path(tempdir) / "user.db"
            conn = sqlite3.connect(db_path)
            try:
                conn.executescript("""
                    CREATE TABLE sessions (id TEXT PRIMARY KEY, user_id TEXT NOT NULL);
                    CREATE TABLE session_notifications (
                        session_id TEXT PRIMARY KEY,
                        user_id TEXT NOT NULL,
                        title TEXT NOT NULL DEFAULT '',
                        dismissed INTEGER NOT NULL DEFAULT 0,
                        dismissed_at TEXT,
                        created_at TEXT NOT NULL DEFAULT (datetime('now')),
                        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
                    );
                """)
                conn.execute("INSERT INTO sessions VALUES ('live-session', 'user-1')")
                conn.execute(
                    "INSERT INTO session_notifications (session_id, user_id, title) VALUES (?, ?, ?)",
                    ('live-session', 'user-1', 'Live session'),
                )
                conn.execute(
                    "INSERT INTO session_notifications (session_id, user_id, title) VALUES (?, ?, ?)",
                    ('orphan-session', 'user-1', 'Deleted session'),
                )
                conn.commit()
            finally:
                conn.close()

            def open_read(_db):
                read_conn = sqlite3.connect(db_path)
                read_conn.row_factory = sqlite3.Row
                return read_conn, "sqlite"

            with (
                patch("app.api.db_viewer._caller_user_id", return_value="user-1"),
                patch("app.api.db_viewer._open_read", side_effect=open_read),
            ):
                result = asyncio.run(list_session_notifications(None))

        self.assertEqual(len(result["notifications"]), 1)
        self.assertEqual(result["notifications"][0]["session_id"], "live-session")
        self.assertEqual(result["notifications"][0]["title"], "Live session")


class UserDatabaseQuotaTests(unittest.IsolatedAsyncioTestCase):
    """The quota worker operates on a real SQLite file so VACUUM is exercised."""

    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "user.db"
        conn = sqlite3.connect(self.db_path)
        conn.executescript("""
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL, status TEXT NOT NULL,
                pinned INTEGER NOT NULL DEFAULT 0, created_at TEXT, updated_at TEXT
            );
            CREATE TABLE interactions (
                id TEXT PRIMARY KEY, session_id TEXT, parent_id TEXT, role TEXT,
                content TEXT, tool_name TEXT, tool_call_id TEXT, metadata TEXT,
                output TEXT, status TEXT NOT NULL DEFAULT 'complete', created_at TEXT
            );
            CREATE TABLE session_runs (session_id TEXT PRIMARY KEY, status TEXT);
            CREATE TABLE agent_spawns (
                orchestrator_session_id TEXT, spawn_session_id TEXT
            );
            CREATE TABLE memories (
                id TEXT PRIMARY KEY, user_id TEXT, slug TEXT, origin TEXT,
                pinned INTEGER NOT NULL DEFAULT 0, compiled_truth TEXT,
                timeline TEXT, frontmatter TEXT, provenance TEXT,
                created_at TEXT, updated_at TEXT
            );
            CREATE TABLE memory_chunks (
                id TEXT PRIMARY KEY, memory_id TEXT, chunk_text TEXT,
                embedding BLOB
            );
        """)
        conn.close()

    async def asyncTearDown(self):
        self.tempdir.cleanup()

    def _add_session(
        self, session_id, status, stamp, content_bytes=4 * 1024 * 1024,
        *, pinned=False, running=False,
    ):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO sessions VALUES (?, 'user-1', ?, ?, ?, ?)",
                (session_id, status, int(pinned), stamp, stamp),
            )
            conn.execute(
                """INSERT INTO interactions
                   (id, session_id, role, content, created_at)
                   VALUES (?, ?, 'assistant', ?, ?)""",
                (f"interaction-{session_id}", session_id, "x" * content_bytes, stamp),
            )
            if running:
                conn.execute(
                    "INSERT INTO session_runs VALUES (?, 'running')", (session_id,)
                )
            conn.commit()
        finally:
            conn.close()

    async def _enforce(self, protected_ids, limit_mb=10):
        def _open(_db):
            return sqlite3.connect(self.db_path)

        with (
            patch("app.api.db_viewer._open_local_sqlite", side_effect=_open),
            patch(
                "app.api.db_viewer._user_database_limit_bytes",
                return_value=limit_mb * 1024 * 1024,
            ),
            patch(
                "app.api.db_viewer._compact_sqlite_for_quota",
                wraps=_compact_sqlite_for_quota,
            ) as compact,
        ):
            result = await _enforce_user_database_size_limit(
                "user.db", self.db_path, "user-1", protected_ids, None, None,
            )
        self.compact_calls = compact.call_count
        return result

    def _session_ids(self):
        conn = sqlite3.connect(self.db_path)
        try:
            return {row[0] for row in conn.execute("SELECT id FROM sessions")}
        finally:
            conn.close()

    def _add_tool_result(self, session_id, stamp, result_bytes):
        conn = sqlite3.connect(self.db_path)
        try:
            assistant_id = f"assistant-tool-{session_id}"
            conn.execute(
                """INSERT INTO interactions
                   (id, session_id, role, content, output, created_at)
                   VALUES (?, ?, 'assistant', '', ?, ?)""",
                (assistant_id, session_id, json.dumps({"tool_calls": [{
                    "id": f"call-{session_id}", "type": "function",
                    "function": {"name": "search", "arguments": "{\"q\":\"example\"}"},
                }]}), stamp),
            )
            conn.execute(
                """INSERT INTO interactions
                   (id, session_id, parent_id, role, content, tool_name,
                    tool_call_id, metadata, output, status, created_at)
                   VALUES (?, ?, ?, 'tool', ?, 'search', ?, ?, ?, 'complete', ?)""",
                (f"tool-{session_id}", session_id, assistant_id, "z" * result_bytes,
                 f"call-{session_id}", json.dumps({"duration_ms": 12, "args": {"q": "example"}}),
                 "z" * result_bytes, stamp),
            )
            conn.commit()
        finally:
            conn.close()

    async def test_oldest_recycled_session_is_purged_before_active_history(self):
        self._add_session("old-bin", "recycled", "2026-01-01T00:00:00Z")
        self._add_session("active", "active", "2026-01-02T00:00:00Z")
        self._add_session("new-bin", "recycled", "2026-01-03T00:00:00Z")

        result = await self._enforce(["new-bin"])

        self.assertEqual(self._session_ids(), {"active", "new-bin"})
        self.assertEqual(result["purged_recycled"], 1)
        self.assertEqual(result["purged_active"], 0)
        self.assertLessEqual(result["size_after_bytes"], result["limit_bytes"])
        self.assertEqual(self.compact_calls, 1)

    async def test_active_history_is_purged_only_when_no_older_bin_session_exists(self):
        self._add_session("old-active", "active", "2026-01-01T00:00:00Z")
        self._add_session("newer-active", "active", "2026-01-02T00:00:00Z")
        self._add_session("new-bin", "recycled", "2026-01-03T00:00:00Z")

        result = await self._enforce(["new-bin"])

        self.assertEqual(self._session_ids(), {"newer-active", "new-bin"})
        self.assertEqual(result["purged_recycled"], 0)
        self.assertEqual(result["purged_active"], 1)

    async def test_unpinned_history_is_purged_before_pinned_history(self):
        self._add_session("old-pinned", "active", "2026-01-01T00:00:00Z", pinned=True)
        self._add_session("new-unpinned", "active", "2026-01-02T00:00:00Z")
        self._add_session("protected-bin", "recycled", "2026-01-03T00:00:00Z")

        result = await self._enforce(["protected-bin"])

        self.assertEqual(self._session_ids(), {"old-pinned", "protected-bin"})
        self.assertEqual(result["purged_unpinned"], 1)
        self.assertEqual(result["purged_pinned"], 0)

    async def test_oldest_pinned_history_is_last_resort(self):
        self._add_session("old-pinned", "active", "2026-01-01T00:00:00Z", pinned=True)
        self._add_session("new-pinned", "active", "2026-01-02T00:00:00Z", pinned=True)
        self._add_session("protected-bin", "recycled", "2026-01-03T00:00:00Z")

        result = await self._enforce(["protected-bin"])

        self.assertEqual(self._session_ids(), {"new-pinned", "protected-bin"})
        self.assertEqual(result["purged_unpinned"], 0)
        self.assertEqual(result["purged_pinned"], 1)

    async def test_running_session_is_protected_from_quota_cleanup(self):
        self._add_session(
            "old-running", "active", "2026-01-01T00:00:00Z", running=True,
        )
        self._add_session("eligible", "active", "2026-01-02T00:00:00Z")
        self._add_session("protected-bin", "recycled", "2026-01-03T00:00:00Z")

        result = await self._enforce(["protected-bin"])

        self.assertEqual(self._session_ids(), {"old-running", "protected-bin"})
        self.assertEqual(result["protected_families"], 2)
        self.assertEqual(result["purged_unpinned"], 1)

    async def test_spawn_family_is_deleted_as_one_candidate(self):
        self._add_session("old-bin", "recycled", "2026-01-01T00:00:00Z", 3 * 1024 * 1024)
        self._add_session("old-child", "recycled", "2026-01-02T00:00:00Z", 3 * 1024 * 1024)
        self._add_session("protected-bin", "recycled", "2026-01-03T00:00:00Z", 5 * 1024 * 1024)
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("INSERT INTO agent_spawns VALUES ('old-bin', 'old-child')")
            conn.commit()
        finally:
            conn.close()

        result = await self._enforce(["protected-bin"])

        self.assertEqual(self._session_ids(), {"protected-bin"})
        self.assertEqual(result["purged_recycled"], 2)
        self.assertEqual(self.compact_calls, 1)

    async def test_free_pages_are_compacted_without_deleting_history(self):
        self._add_session("keep", "active", "2026-01-01T00:00:00Z", 1 * 1024 * 1024)
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("CREATE TABLE disposable (payload TEXT)")
            conn.execute("INSERT INTO disposable VALUES (?)", ("x" * 12 * 1024 * 1024,))
            conn.commit()
            conn.execute("DELETE FROM disposable")
            conn.commit()
        finally:
            conn.close()

        result = await self._enforce([], limit_mb=10)

        self.assertEqual(self._session_ids(), {"keep"})
        self.assertEqual(result["purged_sessions"], 0)
        self.assertEqual(self.compact_calls, 1)
        self.assertLessEqual(result["size_after_bytes"], result["limit_bytes"])

    async def test_tool_output_is_evicted_before_session_history(self):
        self._add_session("old", "active", "2026-01-01T00:00:00Z", 1024)
        self._add_tool_result("old", "2026-01-01T00:01:00Z", 12 * 1024 * 1024)
        self._add_session("protected-bin", "recycled", "2026-01-02T00:00:00Z", 1024)

        result = await self._enforce(["protected-bin"])

        self.assertEqual(self._session_ids(), {"old", "protected-bin"})
        self.assertEqual(result["evicted_tool_outputs"], 1)
        self.assertEqual(result["purged_sessions"], 0)
        conn = sqlite3.connect(self.db_path)
        try:
            tool = conn.execute(
                "SELECT content, metadata, output, status FROM interactions WHERE id='tool-old'"
            ).fetchone()
            request = conn.execute(
                "SELECT output FROM interactions WHERE id='assistant-tool-old'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertIn("Stored tool output removed", tool[0])
        self.assertEqual(json.loads(tool[1])["payload_state"], "evicted_local")
        self.assertIsNone(tool[2])
        self.assertEqual(tool[3], "complete")
        self.assertEqual(json.loads(request)["tool_calls"][0]["function"]["name"], "search")

    async def test_memory_vector_chunks_are_reclaimed_before_pages(self):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """INSERT INTO memories VALUES
                   ('memory-1', 'user-1', 'project/example', 'deliberate', 0,
                    'keep this truth', '', '{}', '[]', ?, ?)""",
                ("2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
            )
            conn.execute(
                "INSERT INTO memory_chunks VALUES ('chunk-1', 'memory-1', ?, ?)",
                ("x" * 1024, b"z" * 12 * 1024 * 1024),
            )
            conn.commit()
        finally:
            conn.close()

        result = await self._enforce([])

        conn = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM memory_chunks").fetchone()[0], 0)
        finally:
            conn.close()
        self.assertEqual(result["memory_cache_pages_reclaimed"], 1)
        self.assertEqual(result["purged_sessions"], 0)

    async def test_session_purge_removes_only_its_memory_evidence(self):
        self._add_session("old-bin", "recycled", "2026-01-01T00:00:00Z", 6 * 1024 * 1024)
        self._add_session("protected-bin", "recycled", "2026-01-02T00:00:00Z", 6 * 1024 * 1024)
        conn = sqlite3.connect(self.db_path)
        try:
            shared = json.dumps([
                {"session_id": "old-bin", "interaction_id": "old"},
                {"session_id": "protected-bin", "interaction_id": "new"},
            ])
            old_only = json.dumps([
                {"session_id": "old-bin", "interaction_id": "old"},
            ])
            conn.execute(
                """INSERT INTO memories VALUES
                   ('shared', 'user-1', 'chat/shared', 'distilled', 0, 'shared truth',
                    '', '{}', ?, ?, ?)""",
                (shared, "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"),
            )
            conn.execute(
                """INSERT INTO memories VALUES
                   ('old-only', 'user-1', 'chat/old', 'distilled', 0, 'old truth',
                    '', '{}', ?, ?, ?)""",
                (old_only, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
            )
            conn.commit()
        finally:
            conn.close()

        result = await self._enforce(["protected-bin"])

        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute("SELECT id, provenance FROM memories ORDER BY id").fetchall()
        finally:
            conn.close()
        self.assertEqual([row[0] for row in rows], ["shared"])
        self.assertEqual(json.loads(rows[0][1])[0]["session_id"], "protected-bin")
        self.assertEqual(result["memory_links_removed"], 2)
        self.assertEqual(result["memory_pages_deleted"], 1)


if __name__ == "__main__":
    unittest.main()
