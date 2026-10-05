import asyncio
import hashlib
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

DIRECTORY = Path(__file__).resolve().parents[1] / "plugins/abilities/Administrator/git_control"
def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
transport = load("admin_transport", DIRECTORY / "transport.py")
askpass = load("admin_askpass", DIRECTORY / "askpass.py")
git = transport.adapter


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.repo = self.base / "webagent-v3"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.name", "Fixture"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.email", "fixture@example.test"], cwd=self.repo, check=True)
        (self.repo / "file").write_text("initial")
        subprocess.run(["git", "add", "file"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-qm", "initial"], cwd=self.repo, check=True)
        self.context = git._SANDBOX_ROOT.set(self.base)

    async def asyncTearDown(self):
        git._SANDBOX_ROOT.reset(self.context)
        self.temp.cleanup()

    def row(self, host="one.example.test", token="fixture-token-one"):
        return {"connections": [{"url": "https://" + host + "/team/repo", "username": "git", "token": token}]}

    async def test_credentials_are_call_scoped_under_concurrency(self):
        barrier = asyncio.Event()
        entered = 0
        async def command(repo, args, **kwargs):
            return ("https://" + str(repo) + "/team/repo").encode()
        async def execute(name, args, base):
            nonlocal entered
            entered += 1
            if entered == 2:
                barrier.set()
            await barrier.wait()
            env = await transport.authorize(base, "origin", "pull")
            await asyncio.sleep(0)
            return env
        with patch.object(git, "command", command), patch.object(git, "execute", execute):
            first, second = await asyncio.gather(
                transport.execute("git_tool", {}, "one.example.test", self.row()),
                transport.execute("git_tool", {}, "two.example.test", self.row("two.example.test", "fixture-token-two")))
        self.assertEqual("fixture-token-one", first["V3_GIT_TOKEN"])
        self.assertEqual("fixture-token-two", second["V3_GIT_TOKEN"])
        self.assertEqual((), transport._CONNECTIONS.get())

    async def test_exact_grant_revocation_and_no_ssh_fallback(self):
        async def command(*args, **kwargs):
            return b"https://other.example.test/team/repo"
        lease = transport._CONNECTIONS.set(transport.connections(self.row()))
        try:
            with patch.object(git, "command", command), self.assertRaisesRegex(git.Rejected, "not granted"):
                await transport.authorize(self.repo, "origin", "pull")
        finally:
            transport._CONNECTIONS.reset(lease)
        for raw in ("ssh://git@example.test/repo", "https://u:p@example.test/repo", "https://example.test/repo?token=x", "https://example.test/a/../b", "http://example.test/repo"):
            with self.assertRaises(git.Rejected):
                transport.normalized(raw)
        self.assertEqual((), transport.connections({"connections": [{**self.row()["connections"][0], "revoked": True}]}))
        with self.assertRaisesRegex(git.Rejected, "Ambiguous"):
            transport.connections({"connections": self.row()["connections"] * 2})

    def test_askpass_answers_only_exact_https_repository(self):
        env = {"V3_GIT_URL": "https://one.example.test/team/repo", "V3_GIT_USERNAME": "git", "V3_GIT_TOKEN": "fixture-token-one"}
        self.assertEqual("git", askpass.answer("Username for 'https://one.example.test/team/repo': ", env))
        self.assertEqual("fixture-token-one", askpass.answer("Password for 'https://git@one.example.test/team/repo': ", env))
        for prompt in ("Password for 'https://git@other.example.test/team/repo': ", "Password for 'http://git@one.example.test/team/repo': ",
                       "Password for 'https://evil@one.example.test/team/repo': ", "Password for 'https://git@one.example.test/other': ", "other prompt"):
            self.assertIsNone(askpass.answer(prompt, env))
        result = subprocess.run(["/usr/bin/python3", "-I", str(DIRECTORY / "askpass.py"), "untrusted prompt"], env=env, capture_output=True)
        self.assertEqual(1, result.returncode)
        self.assertEqual(b"", result.stdout)
        self.assertEqual(b"", result.stderr)

    def test_anchored_reads_survive_parent_swap_without_outside_bytes(self):
        nested = self.repo / "nested"
        nested.mkdir()
        (nested / "file").write_text("INSIDE")
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "file").write_text("PRIVATE-OUTSIDE")
        original = git._read_at
        def swap(parent, name, missing=False):
            nested.rename(self.repo / "saved")
            nested.symlink_to(outside, target_is_directory=True)
            return original(parent, name, missing)
        with patch.object(git, "_read_at", swap):
            data, _ = git.read_source(self.repo, "nested/file")
        self.assertEqual(b"INSIDE", data)
        with self.assertRaises(git.Rejected):
            git.read_source(self.repo, "nested/file")

    def test_atomic_replace_rejects_parent_swap_and_never_writes_outside(self):
        nested = self.repo / "nested"
        nested.mkdir()
        (nested / "file").write_text("original")
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "file").write_text("PRIVATE-OUTSIDE")
        def swap(repo, raw):
            nested.rename(self.repo / "saved")
            nested.symlink_to(outside, target_is_directory=True)
        expected = hashlib.sha256(b"original").hexdigest()
        with patch.object(git, "_before_publish", swap), self.assertRaises(git.Rejected):
            git.replace_source(self.repo, "nested/file", b"changed", expected)
        self.assertEqual("PRIVATE-OUTSIDE", (outside / "file").read_text())
        self.assertEqual(["file"], [p.name for p in outside.iterdir()])
        self.assertEqual(["file"], [p.name for p in (self.repo / "saved").iterdir()])
        self.assertEqual("original", (self.repo / "saved/file").read_text())

    async def test_mutation_output_guard_reports_uncertain_after_effect(self):
        name = "sk-" + "a" * 16
        subprocess.run(["git", "branch", name], cwd=self.repo, check=True)
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo, capture_output=True, check=True).stdout.decode().strip()
        result = await git.execute("git_tool", {"operation": "checkout", "ref": name, "expected_head": head}, self.base)
        self.assertTrue(result["uncertain"], result)
        self.assertEqual("git_effect_uncertain", result["error_kind"])
        current = subprocess.run(["git", "branch", "--show-current"], cwd=self.repo, capture_output=True, check=True).stdout.decode().strip()
        self.assertEqual(name, current)


if __name__ == "__main__":
    unittest.main()
