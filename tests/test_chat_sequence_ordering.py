import sqlite3
import unittest

from app.api.db_viewer import (
    _interaction_cursor_values,
    _interaction_order_key,
    _interaction_order_by,
    _interaction_message_phase,
    _message_type_of,
)


class ChatSequenceOrderingTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute(
            "CREATE TABLE interactions ("
            "id TEXT PRIMARY KEY, session_id TEXT, session_seq INTEGER, "
            "created_at TEXT)"
        )
        # Insert timestamps/rowids in an order that disagrees with event order.
        self.conn.executemany(
            "INSERT INTO interactions VALUES (?, 's', ?, ?)",
            [
                ("legacy", None, "2025-01-01 00:00:00"),
                ("third", 30, "2025-01-01 00:00:01"),
                ("first", 10, "2025-01-01 00:00:03"),
                ("second", 20, "2025-01-01 00:00:02"),
            ],
        )
        self.order_key = _interaction_order_key("rowid")
        self.order_desc = _interaction_order_by("rowid", "DESC")

    def tearDown(self):
        self.conn.close()

    def test_sequence_is_authoritative_over_timestamp_and_insert_order(self):
        rows = self.conn.execute(
            f"SELECT id FROM interactions ORDER BY {self.order_key}"
        ).fetchall()
        self.assertEqual([row[0] for row in rows], ["legacy", "first", "second", "third"])

    def test_cursor_pages_without_duplicates_or_omissions(self):
        anchor = self.conn.execute(
            "SELECT session_seq, created_at, rowid FROM interactions WHERE id='second'"
        ).fetchone()
        newer = self.conn.execute(
            f"SELECT id FROM interactions WHERE ({self.order_key}) > (?, ?, ?, ?) "
            f"ORDER BY {self.order_key}",
            _interaction_cursor_values(*anchor),
        ).fetchall()
        older = self.conn.execute(
            f"SELECT id FROM interactions WHERE ({self.order_key}) < (?, ?, ?, ?) "
            f"ORDER BY {self.order_key}",
            _interaction_cursor_values(*anchor),
        ).fetchall()
        self.assertEqual([row[0] for row in older], ["legacy", "first"])
        self.assertEqual([row[0] for row in newer], ["third"])

    def test_explicit_message_phase_survives_persistence_projection(self):
        self.assertEqual(
            _interaction_message_phase(
                '{"message_phase":"main"}', "assistant", "complete", "{}"
            ),
            "main",
        )
        self.assertEqual(
            _interaction_message_phase(
                '{"message_phase":"final"}', "assistant", "complete", "{}"
            ),
            "final",
        )
        self.assertEqual(
            _interaction_message_phase(
                '{"message_phase":"progress"}', "assistant", "complete", "{}"
            ),
            "progress",
        )

    def test_legacy_tool_assistant_is_progress_not_final(self):
        output = '{"tool_calls":[{"id":"call-1"}]}'
        self.assertEqual(
            _interaction_message_phase(None, "assistant", "complete", output),
            "progress",
        )

    def test_stopped_status_classifies_as_system(self):
        self.assertEqual(
            _interaction_message_phase(
                '{"message_phase":"pending"}', "assistant", "error", "{}"
            ),
            "system",
        )

    def test_system_role_classifies_as_system(self):
        self.assertEqual(
            _interaction_message_phase(None, "system", "complete", "{}"),
            "system",
        )

    def test_legacy_terminal_phase_normalizes_to_system(self):
        self.assertEqual(
            _interaction_message_phase(
                '{"message_phase":"terminal"}', "assistant", "complete", "{}"
            ),
            "system",
        )

    def test_newest_window_reverses_every_order_component(self):
        newest = self.conn.execute(
            f"SELECT id FROM interactions ORDER BY {self.order_desc} LIMIT 2"
        ).fetchall()
        self.assertEqual([row[0] for row in newest], ["third", "second"])

    def test_order_key_rejects_untrusted_identifier(self):
        with self.assertRaises(ValueError):
            _interaction_order_key("rowid; DROP TABLE interactions")

    def test_message_type_promotes_only_explicit_main_final_or_streaming(self):
        """The closing gate's verdict is the ONLY promotion authority. A durable
        row is `main` iff its persisted phase is an explicit main/final; an
        in-flight streaming row is the live reply. ``pending``, ``progress`` and
        phase-less rows are activity only — the implicit main default is gone."""
        self.assertEqual(
            _message_type_of('{"message_phase":"main"}', "assistant", "complete", "{}", ""),
            "main",
        )
        self.assertEqual(
            _message_type_of('{"message_phase":"final"}', "assistant", "complete", "{}", ""),
            "main",
        )
        self.assertEqual(
            _message_type_of(None, "assistant", "streaming", "{}", ""),
            "main",
        )
        self.assertEqual(
            _message_type_of('{"message_phase":"pending"}', "assistant", "complete", "{}", ""),
            "progress",
        )
        self.assertEqual(
            _message_type_of('{"message_phase":"progress"}', "assistant", "complete", "{}", ""),
            "progress",
        )
        self.assertEqual(
            _message_type_of(None, "assistant", "complete", "{}", ""),
            "progress",
        )

    def test_phase_less_text_row_is_not_promoted(self):
        """A substantive-looking pre-phase row (no tool calls, real text) used to
        default to `main`. It is activity now — nothing but an explicit main/final
        promotes a row out of the run's activity group."""
        output = '{"text":"A substantive answer with no tool calls."}'
        self.assertEqual(
            _message_type_of(None, "assistant", "complete", output, ""),
            "progress",
        )
        self.assertIsNone(
            _interaction_message_phase(None, "assistant", "complete", output),
        )


    def test_timestamp_recovery_order_uses_created_at_when_marked(self):
        rows = self.conn.execute(
            f"SELECT id FROM interactions ORDER BY {_interaction_order_by('rowid', timestamp_recovery=True)}"
        ).fetchall()
        self.assertEqual([row[0] for row in rows], ["legacy", "third", "second", "first"])

    def test_small_timestamp_skew_keeps_sequence_order(self):
        rows = self.conn.execute(
            f"SELECT id FROM interactions ORDER BY {_interaction_order_by('rowid')}"
        ).fetchall()
        self.assertEqual([row[0] for row in rows], ["legacy", "first", "second", "third"])

if __name__ == "__main__":
    unittest.main()


class TranscriptTimestampRecoveryStampTests(unittest.IsolatedAsyncioTestCase):
    """Storage-level recovery policy boundaries for late sequence stamps."""

    async def asyncSetUp(self):
        import asyncio
        import tempfile

        from app.db.local import LocalBackend

        self._tmp = tempfile.TemporaryDirectory()
        self._db_path = str(Path(self._tmp.name) / "ordering.sqlite3")
        conn = sqlite3.connect(self._db_path)
        conn.executescript("""
            CREATE TABLE sessions (id TEXT PRIMARY KEY, metadata TEXT);
            CREATE TABLE interactions (
                id TEXT PRIMARY KEY,
                session_id TEXT,
                created_at TEXT,
                metadata TEXT,
                session_seq INTEGER,
                turn_id TEXT,
                turn_seq INTEGER
            );
        """)
        conn.execute("INSERT INTO sessions (id, metadata) VALUES (?, ?)", ("s1", "{}"))
        conn.execute(
            "INSERT INTO interactions (id, session_id, created_at, metadata, session_seq) VALUES (?, ?, ?, ?, ?)",
            ("prior", "s1", "2026-01-01T00:00:00Z", "{}", 1),
        )
        conn.commit()
        conn.close()

        self.backend = object.__new__(LocalBackend)
        self.backend._write_lock = asyncio.Lock()
        self.backend._get_conn = lambda: sqlite3.connect(self._db_path)

    async def asyncTearDown(self):
        self._tmp.cleanup()

    def _insert(self, interaction_id, created_at):
        conn = sqlite3.connect(self._db_path)
        conn.execute(
            "INSERT INTO interactions (id, session_id, created_at, metadata, session_seq) VALUES (?, ?, ?, ?, NULL)",
            (interaction_id, "s1", created_at, "{}"),
        )
        conn.commit()
        conn.close()

    def _metadata(self, table, row_id):
        conn = sqlite3.connect(self._db_path)
        row = conn.execute(f"SELECT metadata FROM {table} WHERE id=?", (row_id,)).fetchone()
        conn.close()
        return json.loads(row[0] or "{}")

    async def test_recovery_starts_only_above_configured_threshold(self):
        from unittest.mock import patch

        self._insert("at-threshold", "2026-01-01T00:01:00Z")
        with patch("app.admin.settings.get_transcript_timestamp_order_recovery_enabled", return_value=True), \
             patch("app.admin.settings.get_transcript_timestamp_order_recovery_threshold_seconds", return_value=60):
            await self.backend.stamp_interaction_seq("at-threshold", 2)

        self.assertNotIn("timestamp_order_anomaly", self._metadata("interactions", "at-threshold"))
        self.assertNotIn("transcript_timestamp_order_recovery", self._metadata("sessions", "s1"))

        self._insert("over-threshold", "2026-01-01T00:02:01Z")
        with patch("app.admin.settings.get_transcript_timestamp_order_recovery_enabled", return_value=True), \
             patch("app.admin.settings.get_transcript_timestamp_order_recovery_threshold_seconds", return_value=60):
            await self.backend.stamp_interaction_seq("over-threshold", 3)

        self.assertTrue(self._metadata("interactions", "over-threshold")["timestamp_order_anomaly"])
        self.assertTrue(self._metadata("sessions", "s1")["transcript_timestamp_order_recovery"])

    async def test_disabled_recovery_never_marks_anomaly_or_session(self):
        from unittest.mock import patch

        self._insert("disabled", "2026-01-01T00:10:00Z")
        with patch("app.admin.settings.get_transcript_timestamp_order_recovery_enabled", return_value=False), \
             patch("app.admin.settings.get_transcript_timestamp_order_recovery_threshold_seconds", return_value=60):
            await self.backend.stamp_interaction_seq("disabled", 2)

        self.assertNotIn("timestamp_order_anomaly", self._metadata("interactions", "disabled"))
        self.assertNotIn("transcript_timestamp_order_recovery", self._metadata("sessions", "s1"))

# Imports used by TranscriptTimestampRecoveryStampTests.
import json
import sqlite3
from pathlib import Path
