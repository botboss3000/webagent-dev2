import asyncio
import importlib.util
import fcntl
import threading
import hashlib
import os
import sys
import subprocess
import tempfile
import unittest
from unittest import mock
import uuid
from pathlib import Path

spec = importlib.util.spec_from_file_location("worker", Path(__file__).parents[1] / "worker.py")
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        worker.ROOT = Path(self.tmp.name) / "workspaces"
        worker.ROOT.mkdir()
        worker.TEMPLATE = Path(self.tmp.name) / "source-template"
        worker.TEMPLATE.mkdir()
        (worker.TEMPLATE / "Cargo.toml").write_text("[package]\nname = \"test\"\nversion = \"0.1.0\"\n")
        (worker.TEMPLATE / "src").mkdir()
        (worker.TEMPLATE / "src/main.rs").write_text("fn main() {}\n")
        worker.CONTROL = Path(self.tmp.name) / "control"
        worker.CONTROL.mkdir()
        (worker.CONTROL / "dispatch.lock").touch()
        (worker.CONTROL / "shutoff").write_text("0")
        self.session = str(uuid.uuid4())

    def tearDown(self):
        self.tmp.cleanup()

    async def run_tool(self, name, args, timeout=120):
        return await worker.execute({"workspace": self.session, "name": name,
                                     "arguments": args, "timeout_seconds": timeout})

    async def test_file_write_prepublication_failures_preserve_original(self):
        base = worker.workspace_path(self.session)
        target = base / "source.py"
        target.write_text("original")
        target.chmod(0o751)
        for operation in ("fsync", "replace"):
            with self.subTest(operation=operation):
                with mock.patch.object(worker.os, operation, side_effect=OSError("injected failure")):
                    with self.assertRaises(OSError):
                        await self.run_tool("file_write", {"path": "source.py", "content": "replacement"})
                self.assertEqual(target.read_text(), "original")
                self.assertEqual(target.stat().st_mode & 0o777, 0o751)
                self.assertEqual(list(base.glob(".v3-write-*")), [])

    async def test_file_write_syncs_content_before_publish_then_directory(self):
        base = worker.workspace_path(self.session)
        target = base / "source.py"
        target.write_text("original")
        target.chmod(0o751)
        events = []
        real_sync, real_replace = os.fsync, os.replace

        def sync(fd):
            import stat
            events.append("directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
            return real_sync(fd)

        def replace(*args, **kwargs):
            events.append("publish")
            return real_replace(*args, **kwargs)

        with mock.patch.object(worker.os, "fsync", side_effect=sync), mock.patch.object(worker.os, "replace", side_effect=replace):
            result = await self.run_tool("file_write", {"path": "source.py", "content": "replacement"})
        self.assertEqual(result["status"], "success")
        self.assertEqual(events, ["file", "publish", "directory"])
        self.assertEqual(target.read_text(), "replacement")
        self.assertEqual(target.stat().st_mode & 0o777, 0o751)
        self.assertEqual(list(base.glob(".v3-write-*")), [])

    async def test_file_write_postpublication_sync_failure_is_uncertain(self):
        base = worker.workspace_path(self.session)
        target = base / "source.py"
        target.write_text("original")
        real_sync = os.fsync

        def sync(fd):
            import stat
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("directory sync failed")
            return real_sync(fd)

        with mock.patch.object(worker.os, "fsync", side_effect=sync):
            result = await self.run_tool("file_write", {"path": "source.py", "content": "replacement"})
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(target.read_text(), "replacement")
        self.assertEqual(list(base.glob(".v3-write-*")), [])

    async def test_file_write_creation_refuses_concurrent_destination(self):
        base = worker.workspace_path(self.session)
        target = base / "new.txt"
        real_link = os.link

        def competing_create(*args, **kwargs):
            target.write_text("other writer")
            return real_link(*args, **kwargs)

        with mock.patch.object(worker.os, "link", side_effect=competing_create):
            with self.assertRaises(FileExistsError):
                await self.run_tool("file_write", {"path": "new.txt", "content": "replacement"})
        self.assertEqual(target.read_text(), "other writer")
        self.assertEqual(list(base.glob(".v3-write-*")), [])

    async def test_file_write_follows_symlink_and_rejects_oversized_content(self):
        base = worker.workspace_path(self.session)
        target = base / "safe.txt"
        target.write_text("original")
        (base / "alias").symlink_to(target)
        await self.run_tool("file_write", {"path": "alias", "content": "replacement"})
        with self.assertRaises(ValueError):
            await self.run_tool("file_write", {"path": "safe.txt", "content": "x" * (worker.LIMIT + 1)})
        self.assertEqual(target.read_text(), "replacement")
        self.assertTrue((base / "alias").is_symlink())
        self.assertEqual(list(base.glob(".v3-write-*")), [])

    async def test_source_patch_applies_reviewed_multifile_diff(self):
        await self.run_tool("workspace_seed", {})
        source = worker.ROOT / self.session / "webagent-v3"
        review = await self.run_tool("source_review", {})
        patch = (
            "diff --git a/src/main.rs b/src/main.rs\n"
            "--- a/src/main.rs\n"
            "+++ b/src/main.rs\n"
            "@@ -1 +1 @@\n"
            "-fn main() {}\n"
            "+fn main() { println!(\"changed\"); }\n"
            "diff --git a/new.txt b/new.txt\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/new.txt\n"
            "@@ -0,0 +1 @@\n"
            "+new source\n"
        )
        result = await self.run_tool("source_patch", {
            "patch": patch, "expected_review_sha256": review["review_sha256"]})
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["paths"], ["src/main.rs", "new.txt"])
        self.assertIn("changed", (source / "src/main.rs").read_text())
        self.assertEqual((source / "new.txt").read_text(), "new source\n")
        self.assertEqual(result["review_sha256"],
                         (await self.run_tool("source_review", {}))["review_sha256"])

    async def test_source_patch_rejects_stale_or_invalid_patch_without_partial_edit(self):
        await self.run_tool("workspace_seed", {})
        source = worker.ROOT / self.session / "webagent-v3"
        original = (source / "src/main.rs").read_text()
        review = await self.run_tool("source_review", {})
        patch = (
            "diff --git a/src/main.rs b/src/main.rs\n"
            "--- a/src/main.rs\n"
            "+++ b/src/main.rs\n"
            "@@ -1 +1 @@\n"
            "-fn main() {}\n"
            "+fn main() { println!(\"changed\"); }\n"
            "diff --git a/Cargo.toml b/Cargo.toml\n"
            "--- a/Cargo.toml\n"
            "+++ b/Cargo.toml\n"
            "@@ -1 +1 @@\n"
            "-not present\n"
            "+changed\n"
        )
        with self.assertRaisesRegex(ValueError, "review"):
            await self.run_tool("source_patch", {
                "patch": patch, "expected_review_sha256": "0" * 64})
        with self.assertRaises(ValueError):
            await self.run_tool("source_patch", {
                "patch": patch, "expected_review_sha256": review["review_sha256"]})
        self.assertEqual((source / "src/main.rs").read_text(), original)

    async def test_source_patch_review_token_is_bound_to_baseline(self):
        await self.run_tool("workspace_seed", {})
        source = worker.ROOT / self.session / "webagent-v3"
        before = await self.run_tool("source_review", {})
        subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@localhost",
             "commit", "--allow-empty", "-qm", "new baseline"],
            cwd=source, check=True)
        after = await self.run_tool("source_review", {})
        self.assertEqual(before["diff_sha256"], after["diff_sha256"])
        self.assertNotEqual(before["review_sha256"], after["review_sha256"])
        patch = (
            "diff --git a/new.txt b/new.txt\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/new.txt\n"
            "@@ -0,0 +1 @@\n"
            "+new\n"
        )
        with self.assertRaisesRegex(ValueError, "review"):
            await self.run_tool("source_patch", {
                "patch": patch, "expected_review_sha256": before["review_sha256"]})
        self.assertFalse((source / "new.txt").exists())

    async def test_source_patch_rejects_git_metadata_and_symlink_paths(self):
        await self.run_tool("workspace_seed", {})
        source = worker.ROOT / self.session / "webagent-v3"
        config = (source / ".git/config").read_bytes()
        outside = Path(self.tmp.name) / "escape.txt"
        (source / "src/link").symlink_to(self.tmp.name, target_is_directory=True)
        review = await self.run_tool("source_review", {})
        for path in (".git/config", "../escape.txt", "src/link/escape.txt"):
            patch = (
                f"diff --git a/{path} b/{path}\n"
                "new file mode 100644\n"
                "--- /dev/null\n"
                f"+++ b/{path}\n"
                "@@ -0,0 +1 @@\n"
                "+unsafe\n"
            )
            with self.assertRaises(ValueError):
                await self.run_tool("source_patch", {
                    "patch": patch, "expected_review_sha256": review["review_sha256"]})
        self.assertEqual((source / ".git/config").read_bytes(), config)
        self.assertFalse(outside.exists())

    async def test_source_patch_obeys_global_shutoff(self):
        await self.run_tool("workspace_seed", {})
        source = worker.ROOT / self.session / "webagent-v3"
        review = await self.run_tool("source_review", {})
        patch = (
            "diff --git a/blocked.txt b/blocked.txt\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/blocked.txt\n"
            "@@ -0,0 +1 @@\n"
            "+blocked\n"
        )
        (worker.CONTROL / "shutoff").write_text("1")
        with self.assertRaisesRegex(ValueError, "disabled"):
            await self.run_tool("source_patch", {
                "patch": patch, "expected_review_sha256": review["review_sha256"]})
        self.assertFalse((source / "blocked.txt").exists())

    async def test_source_patch_apply_error_is_uncertain(self):
        await self.run_tool("workspace_seed", {})
        review = await self.run_tool("source_review", {})
        patch = (
            "diff --git a/uncertain.txt b/uncertain.txt\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/uncertain.txt\n"
            "@@ -0,0 +1 @@\n"
            "+uncertain\n"
        )
        original = worker.git_capture

        def fail_during_apply(source, arguments, *args):
            if arguments[:2] == ["apply", "--whitespace=error"]:
                raise OSError("simulated worker loss")
            return original(source, arguments, *args)

        with mock.patch.object(worker, "git_capture", side_effect=fail_during_apply):
            result = await self.run_tool("source_patch", {
                "patch": patch, "expected_review_sha256": review["review_sha256"]})
        self.assertEqual(result["status"], "uncertain")
        self.assertIn("review", result["output"])

    async def test_source_review_includes_tracked_and_new_files_without_changing_index(self):
        await self.run_tool("workspace_seed", {})
        source = worker.ROOT / self.session / "webagent-v3"
        (source / "src/main.rs").write_text("fn main() { println!(\"changed\"); }\n")
        (source / "new.txt").write_text("new source\n")
        before = subprocess.check_output(["git", "status", "--porcelain", "-z"], cwd=source)
        result = await self.run_tool("source_review", {})
        after = subprocess.check_output(["git", "status", "--porcelain", "-z"], cwd=source)
        self.assertEqual(result["status"], "success")
        self.assertFalse(result["truncated"])
        self.assertEqual(result["diff_sha256"], hashlib.sha256(result["diff"].encode()).hexdigest())
        self.assertEqual(result["review_sha256"], hashlib.sha256(
            result["baseline_commit"].encode() + b"\0" + result["diff"].encode()).hexdigest())
        self.assertIn("changed", result["diff"])
        self.assertIn("new source", result["diff"])
        self.assertIn("src/main.rs", result["status_output"])
        self.assertIn("new.txt", result["status_output"])
        self.assertEqual(before, after)
        subprocess.run(["git", "restore", "--worktree", "src/main.rs"], cwd=source, check=True)
        (source / "new.txt").unlink()
        subprocess.run(["git", "apply", "--check", "-"], cwd=source,
                       input=result["diff"].encode(), check=True)

    async def test_source_review_reports_oversized_untracked_file(self):
        await self.run_tool("workspace_seed", {})
        source = worker.ROOT / self.session / "webagent-v3"
        (source / "large.txt").write_bytes(b"x" * (worker.LIMIT + 1))
        result = await self.run_tool("source_review", {})
        self.assertTrue(result["truncated"])
        self.assertIsNone(result["diff_sha256"])
        self.assertIn("large.txt", result["status_output"])
        self.assertLessEqual(len(result["diff"].encode()), worker.LIMIT)

    async def test_workspace_seed_creates_clean_source_baseline(self):
        result = await self.run_tool("workspace_seed", {})
        path = worker.ROOT / self.session / "webagent-v3"
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["path"], "webagent-v3")
        self.assertEqual((path / "src/main.rs").read_text(), "fn main() {}\n")
        self.assertEqual(len(result["baseline_commit"]), 40)
        status = subprocess.check_output(["git", "status", "--porcelain"], cwd=path)
        self.assertEqual(status, b"")
        (path / "src/main.rs").write_text("fn main() { println!(\"edited\"); }\n")
        with self.assertRaisesRegex(ValueError, "already initialized"):
            await self.run_tool("workspace_seed", {})
        self.assertIn("edited", (path / "src/main.rs").read_text())

    async def test_workspace_seed_obeys_shutoff_before_publish(self):
        original = worker._seed_finish
        def stop_before_publish(spec, raw):
            (worker.CONTROL / "shutoff").write_text("1")
            return original(spec, raw)
        with mock.patch.object(worker, "_seed_finish", side_effect=stop_before_publish):
            with self.assertRaisesRegex(ValueError, "execution disabled"):
                await self.run_tool("workspace_seed", {})
        self.assertFalse((worker.ROOT / self.session / "webagent-v3").exists())

    async def test_seed_cancelled_spawn_is_reaped_without_publication(self):
        original = asyncio.create_subprocess_exec
        launched, release = asyncio.Event(), asyncio.Event()
        children = []
        async def delayed(*args, **kwargs):
            process = await original(*args, **kwargs)
            children.append(process); launched.set()
            await release.wait()
            return process
        with mock.patch.object(worker.asyncio, "create_subprocess_exec", delayed):
            task = asyncio.create_task(self.run_tool("workspace_seed", {}))
            await launched.wait(); task.cancel(); release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 8)
        self.assertIsNotNone(children[0].returncode)
        base = worker.ROOT / self.session
        self.assertFalse((base / "webagent-v3").exists())
        self.assertEqual([], list(base.glob(".v3-seed-*")))

    async def test_seed_child_rejects_raced_stage_directory_escape(self):
        base = worker.workspace_path(self.session)
        outside = Path(self.tmp.name) / "outside"; outside.mkdir()
        (outside / "main.rs").write_text("OUTSIDE")
        script = r"""
import importlib.util,runpy,sys,json,shutil
from pathlib import Path
workerpath,base,template,outside=sys.argv[1:]
spec=importlib.util.spec_from_file_location("seedworker",workerpath)
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
real=shutil.copytree
def race(src,dst,*args,**kwargs):
    if Path(src)==Path(template):
        (Path(dst)/"src").symlink_to(outside,target_is_directory=True)
    return real(src,dst,*args,**kwargs)
m.shutil.copytree=race
runpy.run_path(str(Path(workerpath).with_name("sandbox.py")))["restrict"](base,read_paths=(template,))
try:
    m._seed_inner({"base":base,"template":template,"stage":".v3-seed-fixture"})
    print("UNSAFE_SUCCESS")
except Exception:
    print("REJECTED")
"""
        result = await asyncio.to_thread(subprocess.run,
            [sys.executable, "-I", "-c", script, str(Path(worker.__file__).resolve()), str(base), str(worker.TEMPLATE), str(outside)],
            capture_output=True, timeout=8, check=True)
        self.assertEqual(b"REJECTED\n", result.stdout)
        self.assertEqual("OUTSIDE", (outside / "main.rs").read_text())

    async def test_seed_child_ignores_raced_precommit_hook(self):
        base = worker.workspace_path(self.session)
        outside = Path(self.tmp.name) / "hook-outside"
        script = r"""
import importlib.util,runpy,sys,subprocess
from pathlib import Path
workerpath,base,template,outside=sys.argv[1:]
spec=importlib.util.spec_from_file_location("seedworker",workerpath)
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
real=subprocess.Popen
stage=Path(base)/".v3-seed-fixture"
def race(argv,*args,**kwargs):
    if "commit" in argv:
        hook=stage/".git/hooks/pre-commit"
        hook.write_text("#!/bin/sh\nprintf UNSAFE > "+outside+"\n")
        hook.chmod(0o755)
    return real(argv,*args,**kwargs)
m.subprocess.Popen=race
runpy.run_path(str(Path(workerpath).with_name("sandbox.py")))["restrict"](base,read_paths=(template,))
result=m._seed_inner({"base":base,"template":template,"stage":".v3-seed-fixture"})
print("SEEDED" if result["baseline_commit"] else "FAILED")
"""
        result = await asyncio.to_thread(subprocess.run,
            [sys.executable, "-I", "-c", script, str(Path(worker.__file__).resolve()), str(base), str(worker.TEMPLATE), str(outside)],
            capture_output=True, timeout=8, check=True)
        self.assertEqual(b"SEEDED\n", result.stdout)
        self.assertFalse(outside.exists())

    async def test_workspace_seed_rejects_template_symlink(self):
        (worker.TEMPLATE / "escape").symlink_to("/etc/passwd")
        with self.assertRaisesRegex(ValueError, "invalid source template"):
            await self.run_tool("workspace_seed", {})
        self.assertFalse((worker.ROOT / self.session / "webagent-v3").exists())

    async def test_files_follow_container_paths(self):
        await self.run_tool("file_write", {"path": "a/file.txt", "content": "hello"})
        self.assertEqual((await self.run_tool("file_read", {"path": "a/file.txt"}))["output"], "hello")
        self.assertIn("a/file.txt:1:hello", (await self.run_tool("file_search", {"query": "hello"}))["output"])
        outside = Path(self.tmp.name) / "outside.txt"
        outside.write_text("outside")
        self.assertEqual((await self.run_tool("file_read", {
            "path": "../../outside.txt"}))["output"], "outside")
        (worker.ROOT / self.session / "escape").symlink_to(Path(self.tmp.name))
        self.assertEqual((await self.run_tool("file_read", {
            "path": "escape/outside.txt"}))["output"], "outside")
        checkout = Path(self.tmp.name) / "checkout"
        checkout.mkdir()
        (checkout / "v3").mkdir()
        (checkout / "v3/README.md").write_text("live checkout")
        with mock.patch.object(worker, "LIVE_CHECKOUT", checkout):
            self.assertEqual((await self.run_tool("file_read", {
                "path": "v3/README.md"}))["output"], "live checkout")

    async def test_absolute_container_path_can_be_read_and_edited(self):
        target = Path(self.tmp.name) / "live-source.rs"
        target.write_text("old")
        read = await self.run_tool("file_read", {"path": str(target)})
        self.assertEqual(read["output"], "old")
        edited = await self.run_tool("file_edit", {
            "path": str(target), "old_text": "old", "new_text": "new"})
        self.assertEqual(edited["status"], "success")
        self.assertEqual(target.read_text(), "new")

    async def test_file_read_pages_beyond_first_megabyte(self):
        base = worker.workspace_path(self.session)
        target = base / "deep.rs"
        target.write_text("".join(f"line-{index:05}: source context\n" for index in range(70000)))
        self.assertGreater(target.stat().st_size, worker.LIMIT)
        page = await self.run_tool("file_read", {
            "path": "deep.rs", "offset": 60001, "limit": 3})
        self.assertEqual(page["output"], "".join(
            f"line-{index:05}: source context\n" for index in range(60000, 60003)))
        self.assertEqual(page["total_lines"], 70000)
        self.assertEqual(page["next_offset"], 60004)
        self.assertTrue(page["truncated"])
        next_page = await self.run_tool("file_read", {
            "path": "deep.rs", "offset": page["next_offset"], "limit": 2})
        self.assertIn("line-60003", next_page["output"])
        self.assertEqual(next_page["next_offset"], 60006)
        with self.assertRaisesRegex(ValueError, "offset"):
            await self.run_tool("file_read", {"path": "deep.rs", "offset": 0})
        with self.assertRaisesRegex(ValueError, "limit"):
            await self.run_tool("file_read", {"path": "deep.rs", "limit": 2001})

    async def test_file_search_regex_glob_comments_and_outline(self):
        base = worker.workspace_path(self.session)
        (base / "src").mkdir()
        (base / "target").mkdir()
        (base / ".git").mkdir()
        (base / "src/main.rs").write_text(
            "// TODO: inspect parser\nfn main() {}\nlet text = \"TODO hidden\";\n")
        (base / "src/other.py").write_text("# TODO: Python\n")
        (base / "target/generated.rs").write_text("// TODO: ignored\n")
        (base / ".git/config.rs").write_text("// TODO: ignored\n")
        outside = Path(self.tmp.name) / "external.rs"
        outside.write_text("// TODO: external\n")
        (base / "src/escape.rs").symlink_to(outside)
        regex = await self.run_tool("file_search", {
            "path": "src", "query": r"fn\s+main", "regex": True,
            "file_glob": "*.rs"})
        self.assertIn("src/main.rs:2:fn main()", regex["output"])
        comments = await self.run_tool("file_search", {
            "query": "TODO", "comments_only": True, "file_glob": "*.rs"})
        self.assertIn("src/main.rs:1:// TODO: inspect parser", comments["output"])
        self.assertIn("target/generated.rs:1:// TODO: ignored", comments["output"])
        self.assertIn(".git/config.rs:1:// TODO: ignored", comments["output"])
        outline = await self.run_tool("file_search", {
            "path": "src/main.rs", "comments_only": True})
        self.assertEqual(outline["output"], "src/main.rs:1:// TODO: inspect parser")
        linked = await self.run_tool("file_search", {
            "path": "src/escape.rs", "query": "external"})
        self.assertIn("src/escape.rs:1:// TODO: external", linked["output"])
        target = await self.run_tool("file_search", {
            "path": "target", "query": "TODO"})
        self.assertIn("target/generated.rs:1:// TODO: ignored", target["output"])
        with self.assertRaisesRegex(ValueError, "directory search"):
            await self.run_tool("file_search", {"path": "src", "comments_only": True})

    async def test_file_search_reports_output_truncation(self):
        base = worker.workspace_path(self.session)
        (base / "many.rs").write_text("hit\n" * 200000)
        result = await self.run_tool("file_search", {"query": "hit"})
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(result["output"].encode()), worker.LIMIT)

    async def test_file_edit_exact_match_and_hash(self):
        path = worker.workspace_path(self.session) / "source.py"
        path.write_text("first = 1\nsecond = 2\n")
        original = path.read_bytes()
        result = await self.run_tool("file_edit", {
            "path": "source.py", "old_text": "second = 2", "new_text": "second = 3",
            "expected_sha256": hashlib.sha256(original).hexdigest()})
        self.assertEqual(result["status"], "success")
        self.assertEqual(path.read_text(), "first = 1\nsecond = 3\n")
        self.assertEqual(result["previous_sha256"], hashlib.sha256(original).hexdigest())
        self.assertEqual(result["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        with self.assertRaisesRegex(ValueError, "changed since inspection"):
            await self.run_tool("file_edit", {
                "path": "source.py", "old_text": "second = 3", "new_text": "second = 4",
                "expected_sha256": result["previous_sha256"]})
        self.assertEqual(path.read_text(), "first = 1\nsecond = 3\n")

    async def test_file_edit_rejects_ambiguous_and_accepts_container_paths(self):
        path = worker.workspace_path(self.session) / "source.py"
        path.write_text("same\nsame\n")
        with self.assertRaisesRegex(ValueError, "exactly once"):
            await self.run_tool("file_edit", {
                "path": "source.py", "old_text": "same", "new_text": "different"})
        outside = worker.ROOT / "outside.py"
        outside.write_text("first")
        await self.run_tool("file_edit", {
            "path": "../outside.py", "old_text": "first", "new_text": "second"})
        (worker.ROOT / self.session / "escape").symlink_to(worker.ROOT)
        await self.run_tool("file_edit", {
            "path": "escape/outside.py", "old_text": "second", "new_text": "third"})
        self.assertEqual(outside.read_text(), "third")
        self.assertEqual(path.read_text(), "same\nsame\n")

    async def test_file_edit_rejects_oversized_result(self):
        path = worker.workspace_path(self.session) / "source.py"
        path.write_text("start")
        with self.assertRaisesRegex(ValueError, "exceeds limit"):
            await self.run_tool("file_edit", {
                "path": "source.py", "old_text": "start", "new_text": "x" * (worker.LIMIT + 1)})
        self.assertEqual(path.read_text(), "start")

    async def test_python_shell_and_environment(self):
        os.environ["V3_TEST_SECRET"] = "private"
        result = await self.run_tool("python", {"code": "import os; print(os.getenv('V3_TEST_SECRET', 'absent'))"})
        self.assertEqual(result["output"].strip(), "absent")
        result = await self.run_tool("shell", {"command": "printf working"})
        self.assertEqual(result["output"], "working")
        checkout = Path(self.tmp.name) / "checkout"
        checkout.mkdir()
        with mock.patch.object(worker, "LIVE_CHECKOUT", checkout):
            result = await self.run_tool("shell", {"command": "pwd"})
        self.assertEqual(result["output"].strip(), str(checkout))

    async def test_timeout(self):
        result = await self.run_tool("shell", {"command": "sleep 30"}, 1)
        self.assertEqual(result["status"], "timeout")

    async def test_cancel_kills_descendant(self):
        task = asyncio.create_task(self.run_tool("shell", {"command": "sleep 2; touch survived"}))
        await asyncio.sleep(0.15)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.sleep(2.1)
        self.assertFalse((worker.ROOT / self.session / "survived").exists())

    async def test_cancel_kills_double_fork_new_session(self):
        code = (
            "import os,time\n"
            "if os.fork()==0:\n"
            " os.setsid()\n"
            " if os.fork()!=0: os._exit(0)\n"
            " time.sleep(2); open('detached-survived','w').write('bad'); os._exit(0)\n"
            "time.sleep(30)\n"
        )
        task = asyncio.create_task(self.run_tool("python", {"code": code}))
        await asyncio.sleep(0.2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.sleep(2.1)
        self.assertFalse((worker.ROOT / self.session / "detached-survived").exists())

    async def test_completion_cleans_detached_child(self):
        code = "import os,time\nif os.fork()==0:\n os.setsid(); time.sleep(2); open('orphan-survived','w').write('bad')\n"
        result = await self.run_tool("python", {"code": code})
        self.assertEqual(result["status"], "success")
        await asyncio.sleep(2.1)
        self.assertFalse((worker.ROOT / self.session / "orphan-survived").exists())

    async def test_search_total_scan_budget(self):
        base = worker.workspace_path(self.session)
        for index in range(34):
            (base / f"{index:02}.txt").write_bytes(b"x" * worker.LIMIT)
        result = await self.run_tool("file_search", {"query": "missing"})
        self.assertTrue(result["truncated"])

    async def test_listing_entry_budget(self):
        base = worker.workspace_path(self.session)
        for index in range(10001):
            (base / str(index)).touch()
        result = await self.run_tool("file_list", {})
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(result["output"].splitlines()), 10000)

    async def test_control_lock_released_while_file_operation_runs(self):
        started, release = threading.Event(), threading.Event()
        original = worker.file_operation

        def delayed(*args):
            started.set()
            release.wait(2)
            return original(*args)

        worker.file_operation = delayed
        task = asyncio.create_task(self.run_tool("file_list", {}))
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            with (worker.CONTROL / "dispatch.lock").open("rb") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                (worker.CONTROL / "shutoff").write_text("1")
                fcntl.flock(lock, fcntl.LOCK_UN)
            self.assertFalse(task.done())
        finally:
            release.set()
            await task
            worker.file_operation = original

    async def test_shutoff(self):
        (worker.CONTROL / "shutoff").write_text("1")
        with self.assertRaisesRegex(ValueError, "disabled"):
            await self.run_tool("shell", {"command": "touch forbidden"})

    async def test_approved_shell_can_read_outside_session_workspace(self):
        outside = Path(self.tmp.name) / "live-source.txt"
        outside.write_text("LIVE_SOURCE")
        result = await self.run_tool("shell", {"command": "cat " + str(outside)})
        self.assertEqual(result["status"], "success", result)
        self.assertEqual(result["output"], "LIVE_SOURCE")

    async def test_output_is_bounded(self):
        result = await self.run_tool("python", {"code": "print('x' * 2000000)"})
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(result["output"]), worker.LIMIT)


if __name__ == "__main__":
    unittest.main()
