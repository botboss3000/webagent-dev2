import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.agent import session_changes
from app.api import github
from app.db.local import LocalBackend
from fastapi import HTTPException


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


class SessionChangeCaptureTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_broad_capture_excludes_runtime_databases(self):
        repo = self.root / "repo-runtime"
        repo.mkdir()
        _git(repo, "init")
        (repo / "app.py").write_text("dirty\n", encoding="utf-8")
        runtime = repo / "data" / "agent_data" / "agent-1"
        runtime.mkdir(parents=True)
        (runtime / "agent-1.db").write_bytes(b"runtime")

        with patch.object(session_changes, "_PROJECT_ROOT", repo):
            state = session_changes.capture_tool_state("run_python", {})

        self.assertIn("app.py", state)
        self.assertNotIn("data/agent_data/agent-1/agent-1.db", state)

    def test_runtime_sqlite_sidecars_are_not_source_changes(self):
        for path in (
            "data/engine_state/codex/portal_links.sqlite-wal",
            "data/engine_state/codex/portal_links.sqlite-shm",
            "plugins/data/db/index.db-wal",
            "plugins/data/db/index.db-shm",
            "cache/state.sqlite-journal",
        ):
            self.assertFalse(session_changes._trackable_path(path), path)

    def test_normal_source_and_sqlite_named_source_remain_trackable(self):
        self.assertTrue(session_changes._trackable_path("app/agent/loop.py"))
        self.assertTrue(session_changes._trackable_path("migrations/001_add_sqlite.py"))

    def test_fingerprint_does_not_read_dirty_file_contents(self):
        target = self.root / "large.py"
        target.write_bytes(b"x" * 1024)
        with patch.object(session_changes, "_PROJECT_ROOT", self.root):
            with patch.object(Path, "open", side_effect=AssertionError("content read")):
                fingerprint = session_changes._content_fingerprint("large.py", " M")
        self.assertIn(":1024:", fingerprint)


class SessionChangeClaimTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.db = LocalBackend(
            db_path=str(self.root / "claims.db"),
            seed=False,
        )
        conn = self.db._get_conn()
        try:
            conn.executemany(
                "INSERT INTO sessions (id, user_id, title, metadata) VALUES (?, ?, ?, ?)",
                [
                    ("session-1", "admin", "one", "{}"),
                    ("session-2", "admin", "two", "{}"),
                    ("session-3", "admin", "three", "{}"),
                ],
            )
            conn.commit()
        finally:
            conn.close()

    async def asyncTearDown(self):
        self.tempdir.cleanup()

    async def test_collisions_are_retained_until_one_session_releases(self):
        await self.db.update_session_change_claims(
            "session-1", claimed=["app.py"], reset=["app.py"]
        )
        await self.db.update_session_change_claims(
            "session-2", claimed=["app.py"]
        )

        owners = await self.db.get_session_change_owners(["app.py"])
        self.assertEqual(set(owners["app.py"]), {"session-1", "session-2"})

        await self.db.clear_session_change_claims("session-1", ["app.py"])
        owners = await self.db.get_session_change_owners(["app.py"])
        self.assertEqual(owners["app.py"], ["session-2"])

    async def test_clean_to_dirty_generation_removes_stale_owners(self):
        await self.db.update_session_change_claims(
            "session-1", claimed=["app.py"]
        )
        await self.db.update_session_change_claims(
            "session-3", claimed=["app.py"], reset=["app.py"]
        )

        owners = await self.db.get_session_change_owners(["app.py"])
        self.assertEqual(owners["app.py"], ["session-3"])

    async def test_git_delta_records_clean_and_already_dirty_edits(self):
        repo = self.root / "repo"
        repo.mkdir()
        _git(repo, "init")
        _git(repo, "config", "user.name", "Test")
        _git(repo, "config", "user.email", "test@example.com")
        target = repo / "app.py"
        target.write_text("one\n", encoding="utf-8")
        _git(repo, "add", "app.py")
        _git(repo, "commit", "-m", "initial")

        with (
            patch.object(session_changes, "_PROJECT_ROOT", repo),
            patch.object(session_changes, "get_db", return_value=self.db),
        ):
            before = session_changes.capture_tool_state(
                "write_source", {"path": "app.py"}
            )
            target.write_text("two\n", encoding="utf-8")
            after = session_changes.capture_tool_state(
                "write_source", {"path": "app.py"}
            )
            changed = await session_changes.record_tool_delta(
                "session-1", before, after
            )
            self.assertEqual(changed, ["app.py"])

            before = after
            target.write_text("three\n", encoding="utf-8")
            after = session_changes.capture_tool_state(
                "write_source", {"path": "app.py"}
            )
            changed = await session_changes.record_tool_delta(
                "session-2", before, after
            )
            self.assertEqual(changed, ["app.py"])

        owners = await self.db.get_session_change_owners(["app.py"])
        self.assertEqual(set(owners["app.py"]), {"session-1", "session-2"})

    async def test_action_validation_rejects_foreign_and_shared_paths(self):
        request = object()
        with (
            patch.object(github, "get_db", return_value=self.db),
            patch.object(github, "_get_user_id_from_request", return_value="admin"),
        ):
            foreign = github.PathScopedChangesRequest(
                session_id="session-1", paths=["foreign.py"]
            )
            with self.assertRaises(HTTPException) as caught:
                await github._owned_action_paths(foreign, request)
            self.assertEqual(caught.exception.status_code, 409)

            await self.db.update_session_change_claims(
                "session-1", claimed=["shared.py"]
            )
            await self.db.update_session_change_claims(
                "session-2", claimed=["shared.py"]
            )
            shared = github.PathScopedChangesRequest(
                session_id="session-1", paths=["shared.py"]
            )
            with self.assertRaises(HTTPException) as caught:
                await github._owned_action_paths(shared, request)
            self.assertEqual(caught.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main()
