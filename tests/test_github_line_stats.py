import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.api import github


class GithubLineStatsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        subprocess.run(["git", "init"], cwd=self.root, check=True, capture_output=True)
        (self.root / "source.py").write_text("source\n", encoding="utf-8")
        runtime = self.root / "data" / "agent_data" / "agent-1"
        runtime.mkdir(parents=True)
        (runtime / "agent-1.db").write_bytes(b"runtime")

    def tearDown(self):
        self.tempdir.cleanup()

    def test_payload_excludes_runtime_state_and_caches_git_scan(self):
        real_run = subprocess.run
        with (
            patch.object(github, "_PROJECT_ROOT", self.root),
            patch.object(github, "_line_stats_cache", None),
            patch.object(github, "_line_stats_cache_at", 0.0),
            patch.object(github.subprocess, "run", wraps=real_run) as run,
        ):
            first = github._project_line_stats_payload()
            second = github._project_line_stats_payload()

        self.assertIn("source.py", first["stats"])
        self.assertNotIn("data/agent_data/agent-1/agent-1.db", first["stats"])
        self.assertIs(first, second)
        self.assertEqual(run.call_count, 2)

    async def test_endpoint_offloads_git_scan_from_event_loop(self):
        payload = {"project_root": "repo", "stats": {}}
        with patch.object(github, "_run_git_job", new=AsyncMock(return_value=payload)) as offload:
            result = await github.get_line_stats(None)
        self.assertEqual(result, payload)
        offload.assert_awaited_once_with(github._project_line_stats_payload)


if __name__ == "__main__":
    unittest.main()
