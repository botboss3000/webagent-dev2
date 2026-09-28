from __future__ import annotations

import functools
import http.server
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from app.db.schema import render_plane


ROOT = Path(__file__).resolve().parents[1]


class FirstTurnAttachmentSchemaTests(unittest.TestCase):
    def test_user_plane_schema_allows_attachment_before_session_exists(self):
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.executescript(render_plane("user", "sqlite"))
            conn.execute(
                """INSERT INTO attachments
                   (id, user_id, session_id, original_name, mime_type,
                    size_bytes, storage_path, storage_provider, metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "att-first",
                    "user-one",
                    "session-not-created-yet",
                    "first.png",
                    "image/png",
                    3,
                    "user-one/uploads/first.png",
                    "local",
                    "{}",
                ),
            )
        finally:
            conn.close()

    def test_plane_scoped_backend_removes_legacy_attachment_session_fk(self):
        from app.db.local import LocalBackend

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "user.db"
            conn = sqlite3.connect(path)
            conn.executescript(
                """
                PRAGMA foreign_keys=ON;
                CREATE TABLE sessions (id TEXT PRIMARY KEY, user_id TEXT NOT NULL);
                CREATE TABLE attachments (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    session_id TEXT REFERENCES sessions(id),
                    original_name TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    storage_path TEXT NOT NULL,
                    storage_provider TEXT NOT NULL DEFAULT 'local',
                    metadata TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
                """
            )
            conn.close()

            LocalBackend(str(path), seed=False, plane="user")

            conn = sqlite3.connect(path)
            try:
                session_fks = [
                    row for row in conn.execute("PRAGMA foreign_key_list(attachments)")
                    if row[3] == "session_id"
                ]
                self.assertEqual(session_fks, [])
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute(
                    """INSERT INTO attachments
                       (id, user_id, session_id, original_name, mime_type,
                        size_bytes, storage_path, storage_provider, metadata)
                       VALUES ('att-migrated', 'user-one', 'future-session',
                               'first.png', 'image/png', 3,
                               'user-one/uploads/first.png', 'local', '{}')"""
                )
            finally:
                conn.close()

    def test_chat_send_preflights_attachments_before_outbox_and_composer_clear(self):
        source = (ROOT / "ui/chat/js/chat-send.js").read_text(encoding="utf-8")

        upload = source.index("await addAttachmentsToMessage(base)")
        outbox = source.index("_addToOutbox(outboxEntry)")
        composer_clear = source.index("app.chatInput.value = '';", outbox)

        self.assertLess(upload, outbox)
        self.assertLess(upload, composer_clear)


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        return


class TestAttachmentUploadFailureBrowser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise unittest.SkipTest(f"Playwright unavailable: {exc}")

        handler = functools.partial(_QuietHandler, directory=str(ROOT))
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch(headless=True)
        except Exception as exc:
            cls.playwright.stop()
            cls.server.shutdown()
            raise unittest.SkipTest(f"Chromium unavailable: {exc}")
        cls.page = cls.browser.new_page()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        cls.page.goto(f"{cls.base}/ui/diagnostics.html")

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "browser"):
            cls.browser.close()
        if hasattr(cls, "playwright"):
            cls.playwright.stop()
        if hasattr(cls, "server"):
            cls.server.shutdown()
            cls.server.server_close()

    def test_failed_upload_rejects_and_keeps_pending_file_for_retry(self):
        result = self.page.evaluate(
            """async base => {
              const {app} = await import(base + '/ui/shared/js/state.js');
              const attachments = await import(base + '/ui/shared/js/attachments.js');
              app.currentUserId = 'user-one';
              app.currentSessionId = 'future-session';
              const pending = attachments.getPendingAttachments();
              pending.length = 0;
              const file = new File(['png'], 'first.png', {type: 'image/png'});
              pending.push({_file: file, _objectUrl: URL.createObjectURL(file),
                            original_name: file.name, mime_type: file.type});
              const originalFetch = window.fetch;
              window.fetch = async () => new Response(
                JSON.stringify({detail: 'Failed to store attachment metadata'}),
                {status: 500, headers: {'Content-Type': 'application/json'}}
              );
              let rejected = false;
              let message = '';
              try {
                await attachments.uploadPendingAttachments(pending);
              } catch (error) {
                rejected = true;
                message = error.message;
              } finally {
                window.fetch = originalFetch;
              }
              const state = {rejected, message, count: pending.length,
                             hasFile: !!pending[0]?._file,
                             hasObjectUrl: !!pending[0]?._objectUrl};
              pending.length = 0;
              return state;
            }""",
            self.base,
        )
        self.assertTrue(result["rejected"])
        self.assertIn("Failed to store attachment metadata", result["message"])
        self.assertEqual(result["count"], 1)
        self.assertTrue(result["hasFile"])
        self.assertTrue(result["hasObjectUrl"])


if __name__ == "__main__":
    unittest.main()
