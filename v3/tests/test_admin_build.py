import copy
import asyncio
import os
import socket
import subprocess
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

PATH = Path(__file__).resolve().parents[1] / "plugins/abilities/Administrator/v3_build/host.py"
spec = importlib.util.spec_from_file_location("admin_build", PATH)
host = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host)


def context(version="one", extra=None):
    files = {"v3/Dockerfile": "FROM test " + version,
             "v3/Cargo.toml": "fixture", "v3/Cargo.lock": "locked",
             "v3/data/config/gates/message_visibility_v1.json": "{}",
             "v3/data/config/gates/closing_status_v1.json": "{}"}
    files.update(extra or {})
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for name, body in files.items():
            info = tarfile.TarInfo(name)
            data = body.encode()
            info.size, info.mode = len(data), 0o644
            archive.addfile(info, io.BytesIO(data))
    return stream.getvalue()


class FakeRunner:
    def __init__(self):
        self.blob = context()
        self.secrets = ["NEVER-EXPOSE-THIS"]
        self.calls = []
        self.containers = {name: {"Image": "sha256:" + "1" * 64,
                           "State": {"Status": "running", "Health": {"Status": "healthy"}},
                           "Config": {"Env": ["TOKEN=NEVER-EXPOSE-THIS"]}, "HostConfig": {"Memory": 5}}
                           for name in host.TARGETS}
        self.change_during_build = False
        self.low_space = False
        self.block = None
        self.fail_create = False
        self.require_both_started = False
        self.export_entered = None
        self.export_release = None
        self.stop_entered = None
        self.stop_release = None
        self.hot_assets = False
        self.asset_id = None
        self.staged_assets = set()
        self.fail_verify_ui = False

    def check_space(self, minimum):
        self.calls.append(("space", minimum))
        if self.low_space:
            raise ValueError("development build needs more disk space")
        return minimum

    def prepare_build_space(self):
        self.calls.append(("prepare_space", host.MIN_SEQUENCE_FREE))
        return self.check_space(host.MIN_SEQUENCE_FREE)

    def snapshot(self):
        return self.blob

    def inspect(self, name):
        self.calls.append(("inspect", name))
        return copy.deepcopy(self.containers[name])

    def build(self, blob, job, role):
        self.calls.append(("build", role, host.context_digest(blob)[0]))
        if self.block:
            self.block.wait(5)
        if self.change_during_build:
            self.blob = context("changed")
        return "sha256:" + ("a" if role == "api" else "b") * 64, "TOKEN=NEVER-EXPOSE-THIS\nAuthorization: Bearer hidden\n"

    def export(self, images, destination):
        self.calls.append(("export", images))
        if self.export_entered:
            self.export_entered.set()
            self.export_release.wait(5)
        destination.write_bytes(b"rollback image fixture")

    def stop(self, name):
        self.calls.append(("stop", name))
        self.containers[name]["State"]["Status"] = "exited"
        if self.stop_entered:
            self.stop_entered.set()
            self.stop_release.wait(5)

    def rename(self, name, target):
        self.calls.append(("rename", name, target))
        self.containers[target] = self.containers.pop(name)

    def create(self, name, image, previous, release_root=None):
        self.calls.append(("create", name, image, release_root))
        if self.fail_create:
            raise RuntimeError("TOKEN=NEVER-EXPOSE-THIS create failure")
        self.containers[name] = copy.deepcopy(previous)
        self.containers[name]["Image"] = image
        if name == host.TARGETS[0] and release_root is not None:
            self.hot_assets = True

    def start(self, name):
        self.calls.append(("start", name))
        self.containers[name]["State"]["Status"] = "running"

    def healthy(self, name, expected):
        self.calls.append(("healthy", name))
        if self.require_both_started:
            started = {call[1] for call in self.calls if call[0] == "start"}
            if not set(host.TARGETS) <= started:
                return False
        return self.containers[name]["Image"] == expected and self.containers[name]["State"]["Status"] == "running"

    def promote_checkout(self, before, after, changed):
        self.calls.append(("promote", tuple(changed)))
        self.blob = after

    def checkout_matches(self, blob, changed):
        return host.release.entries(self.blob, host.context_digest) == host.release.entries(blob, host.context_digest)

    def stage_assets(self, blob, job_id):
        self.calls.append(("stage_assets", job_id))
        self.staged_assets.add(job_id)

    def validate_ui(self, job_id, changed):
        self.calls.append(("validate_ui", tuple(changed)))

    def current_asset_id(self):
        return self.asset_id

    def activate_assets(self, job_id):
        if job_id not in self.staged_assets:
            raise ValueError("unstaged asset fixture")
        self.calls.append(("activate_assets", job_id))
        self.asset_id = job_id

    def ui_hot_swappable(self):
        return self.hot_assets

    def verify_ui(self, changed=()):
        self.calls.append(("verify_ui", tuple(changed)))
        if self.fail_verify_ui:
            raise RuntimeError("UI response mismatch")


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name) / "jobs"
        self.runner = FakeRunner()
        self.broker = host.Broker(self.state, self.runner)
        self.workspace = str(uuid.uuid4())
        self.trusted = {"user_id": "fixture-user", "session_id": self.workspace, "lease_until": time.time() + 30, "access_fingerprint": "f" * 64}

    def tearDown(self):
        self.broker.shutdown()
        self.temp.cleanup()

    def call(self, name, args=None, trusted=None, workspace=None):
        return self.broker.handle({"id": "fixture", "name": name, "arguments": args or {}, "timeout_seconds": 60,
                                  "trusted": trusted or self.trusted, "workspace": workspace or self.workspace})

    def build(self, request_id="build-fixture"):
        digest = host.context_digest(self.runner.snapshot())[0]
        return self.call("v3_build_image", {"request_id": request_id, "expected_inputs_sha256": digest})

    def test_dashboard_is_admin_scoped_and_reports_confirmed_milestones(self):
        job_id = self.build()["job_id"]
        terminal = self.wait({"job_id": job_id})
        self.assertEqual("completed", terminal["status"])
        dashboard = {"user_id": "admin", "session_id": "00000000-0000-0000-0000-000000000000",
                     "lease_until": time.time() + 30, "access_fingerprint": "a" * 64}
        view = self.call("v3_build_dashboard", trusted=dashboard,
                         workspace=dashboard["session_id"])
        self.assertEqual(job_id, view["jobs"][0]["job_id"])
        self.assertEqual("passed", view["jobs"][0]["rust_tests"]["status"])
        self.broker.jobs[job_id].pop("rust_tests")
        legacy = self.call("v3_build_dashboard", trusted=dashboard,
                           workspace=dashboard["session_id"])
        self.assertEqual("passed", legacy["jobs"][0]["rust_tests"]["status"])
        self.assertIn("detailed test output was not retained", legacy["jobs"][0]["rust_tests"]["summary"])
        self.assertIn("building_api", [item["phase"] for item in view["jobs"][0]["phase_history"]])
        self.assertEqual("healthy", view["containers"]["api"]["health"])
        self.assertNotIn("NEVER-EXPOSE-THIS", json.dumps(view))
        self.assertNotIn("access_fingerprint", view["jobs"][0])
        with self.assertRaises(PermissionError):
            self.call("v3_build_dashboard", trusted=self.trusted, workspace=self.workspace)
        with self.assertRaises(PermissionError):
            self.call("v3_build_dashboard", {"unexpected": True}, trusted=dashboard,
                      workspace=dashboard["session_id"])

    def wait(self, job):
        for _ in range(100):
            value = self.call("v3_worker_job", {"job_id": job["job_id"]})
            if value["status"] not in ("queued", "running"):
                # _run finally releases the busy marker after durable result.
                while self.broker.active:
                    time.sleep(.001)
                return value
            time.sleep(.01)
        self.fail("job did not finish")

    def deploy(self, source_job, request_id="deploy-fixture"):
        return self.call("v3_worker_submit", {"operation": "deploy", "job_id": source_job["job_id"],
                "request_id": request_id, "expected_inputs_sha256": source_job["inputs_sha256"]})

    def test_checked_patch_changes_nested_task_workspace(self):
        root = self.state / "nested-repo"
        root.mkdir()
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        source = root / "v3/build-jobs/coordination/trees/task/v3"
        source.mkdir(parents=True)
        target = source / "README.md"
        target.write_text("before\n")
        patch_text = ("diff --git a/README.md b/README.md\n"
                      "--- a/README.md\n+++ b/README.md\n"
                      "@@ -1 +1 @@\n-before\n+after\n")
        with patch.object(host, "SOURCE", root):
            with self.assertRaisesRegex(ValueError, "hunk line counts mismatch"):
                host.inspect_source_patch(source, patch_text + "+ignored trailing line\n")
            self.assertEqual(["README.md"], host.inspect_source_patch(source, patch_text))
            host.source_git(source, ["apply", "--whitespace=error", "-"], patch_text.encode())
        self.assertEqual("after\n", target.read_text())
        with patch.object(host, "SOURCE", root), self.assertRaisesRegex(ValueError, "README.md:1"):
            host.source_git(source, ["apply", "--check", "-"], patch_text.encode())

    def test_release_mounts_pin_source_and_keep_state_writable(self):
        root = self.state / "release-fixture"
        for name in ("v3/ui", "v3/config", "v3/plugins/abilities",
                     "ui/shared/css", "ui/main-panel/agents", "ui/vendor/fonts",
                     "ui/vendor/lucide"):
            (root / name).mkdir(parents=True)
        (root / "ui/favicon.svg").write_text("svg")
        api = {"Mounts": [
            {"Type": "bind", "Source": "/old", "Target": "/live-v3"},
            {"Type": "bind", "Source": "/checkout", "Target": "/live-checkout"},
            {"Type": "bind", "Source": "/old/ui", "Target": "/opt/v3/ui"},
            {"Type": "bind", "Source": "/state", "Target": "/state"}]}
        mounted = host.release.release_mounts(api, root)["Mounts"]
        by_target = {item["Target"]: item for item in mounted}
        self.assertEqual(str(root / "v3"), by_target["/live-v3"]["Source"])
        self.assertTrue(by_target["/live-v3"]["ReadOnly"])
        self.assertEqual("/checkout", by_target["/live-checkout"]["Source"])
        self.assertEqual("/state", by_target["/state"]["Source"])
        loop = {"Mounts": [{"Type": "bind", "Source": "/old/abilities",
                            "Target": "/opt/v3/plugins/abilities"}]}
        target = host.release.release_mounts(loop, root)["Mounts"][0]
        self.assertEqual(str(root / "v3/plugins/abilities"), target["Source"])

    def test_stable_asset_mount_and_atomic_pointer(self):
        root = self.state / "asset-fixture"
        blob = context(extra={"v3/ui/index.html": "<main>ready</main>",
                              "v3/ui/app.js": "export {}",
                              "ui/shared/css/app1.css": "body {}"})
        first = str(uuid.uuid4())
        second = str(uuid.uuid4())
        release = host.release
        release.stage_assets(root, first, blob, host.context_digest)
        self.assertIsNone(release.current_asset_id(root))
        self.assertIsNone(release.activate_assets(root, first))
        self.assertEqual(first, release.current_asset_id(root))
        release.stage_assets(root, second, blob, host.context_digest)
        self.assertEqual(first, release.activate_assets(root, second))
        self.assertEqual(second, release.current_asset_id(root))
        release.stage_assets(root, second, blob, host.context_digest)
        (root / "releases" / second / "v3/ui/app.js").write_text("changed")
        with self.assertRaisesRegex(ValueError, "frozen UI asset"):
            release.stage_assets(root, second, blob, host.context_digest)

        snapshot = self.state / "release-fixture"
        (snapshot / "v3").mkdir(parents=True)
        api = {"Mounts": [
            {"Type": "bind", "Source": "/old", "Target": "/live-v3"},
            {"Type": "bind", "Source": "/checkout", "Target": "/live-checkout"},
            {"Type": "bind", "Source": "/old/ui", "Target": "/opt/v3/ui"},
            {"Type": "bind", "Source": "/state", "Target": "/state"}]}
        mounted = release.release_mounts(api, snapshot, root)["Mounts"]
        targets = {item["Target"]: item for item in mounted}
        self.assertNotIn("/opt/v3/ui", targets)
        self.assertEqual(str(root), targets["/opt/v3/assets"]["Source"])
        self.assertTrue(targets["/opt/v3/assets"]["ReadOnly"])
        self.assertEqual("/state", targets["/state"]["Target"])

    def test_checkout_promotion_checks_conflicts_and_is_idempotent(self):
        root = self.state / "checkout"
        path = root / "v3/README.md"
        path.parent.mkdir(parents=True)
        path.write_text("before")
        before = context(extra={"v3/README.md": "before"})
        after = context(extra={"v3/README.md": "after"})
        promote = host.release.promote_checkout
        self.assertEqual(1, promote(root, before, after, ["v3/README.md"], host.context_digest))
        self.assertEqual("after", path.read_text())
        self.assertEqual(0, promote(root, before, after, ["v3/README.md"], host.context_digest))
        path.write_text("unrelated edit")
        with self.assertRaisesRegex(ValueError, "checkout changed during release"):
            promote(root, before, after, ["v3/README.md"], host.context_digest)
        self.assertEqual("unrelated edit", path.read_text())

    def test_isolated_source_read_is_task_scoped_and_bounded(self):
        task = self.call("v3_build_work", {"action": "begin", "task_id": "read-task",
            "summary": "Read", "mode": "isolated"})
        value = self.call("v3_source_read", {"task_id": "read-task",
            "path": "Cargo.toml", "offset": 1, "limit": 20})
        self.assertEqual(["fixture"], value["lines"])
        self.assertEqual(task["workspace_inputs_sha256"], value["workspace_inputs_sha256"])
        with self.assertRaises(ValueError):
            self.call("v3_source_read", {"task_id": "read-task", "path": "data/config/gates/closing_status_v1.json"})
        with self.assertRaises(ValueError):
            self.call("v3_source_read", {"task_id": "read-task", "path": "../Cargo.toml"})
        with self.assertRaises(ValueError):
            self.call("v3_source_read", {"task_id": "read-task", "path": "Cargo.toml", "limit": 1000})

    def test_task_search_and_batched_reads_keep_caller_scope(self):
        task = self.call("v3_build_work", {"action": "begin", "task_id": "search-task",
            "summary": "Search", "mode": "isolated"})
        source = Path(task["workspace_path"])
        (source / "src").mkdir(exist_ok=True)
        (source / "src" / "sample.rs").write_text("needle first\nother\nneedle second\n")
        found = self.call("v3_source_search", {"task_id": "search-task", "query": "needle"})
        self.assertEqual([1, 3], [entry["line"] for entry in found["matches"]])
        batch = self.call("v3_source_read_batch", {"task_id": "search-task",
            "requests": [{"path": "src/sample.rs", "offset": 1, "limit": 1},
                         {"path": "src/sample.rs", "offset": 3, "limit": 1}]})
        self.assertEqual(["needle first", "needle second"],
                         [entry["lines"][0] for entry in batch["results"]])
        with self.assertRaisesRegex(ValueError, "task"):
            self.call("v3_source_search", {"task_id": "other-task", "query": "needle"})
        with self.assertRaisesRegex(ValueError, "source read"):
            self.call("v3_source_read_batch", {"task_id": "search-task",
                "requests": [{"path": "../secret"}]})

    def test_task_cargo_check_verifies_source_digest(self):
        task = self.call("v3_build_work", {"action": "begin", "task_id": "cargo-task",
            "summary": "Check", "mode": "isolated"})
        digest = task["workspace_inputs_sha256"]
        with patch.object(self.runner, "cargo_task", return_value="check passed", create=True) as command:
            result = self.call("v3_cargo_task", {"task_id": "cargo-task", "task": "check",
                "expected_inputs_sha256": digest, "timeout_seconds": 30})
        self.assertEqual("completed", result["status"])
        self.assertEqual(digest, result["inputs_sha256"])
        self.assertIn("check passed", result["log_excerpt"])
        self.assertEqual(30, command.call_args.args[-1])
        with self.assertRaisesRegex(ValueError, "stale"):
            self.call("v3_cargo_task", {"task_id": "cargo-task", "task": "check",
                "expected_inputs_sha256": "0" * 64})
        with self.assertRaisesRegex(ValueError, "open isolated"):
            self.call("v3_cargo_task", {"task_id": "other-task", "task": "check",
                "expected_inputs_sha256": digest})
        def change_during_check(*unused):
            Path(task["workspace_path"], "Cargo.toml").write_text("changed")
            return "check passed"
        with patch.object(self.runner, "cargo_task", side_effect=change_during_check, create=True):
            stale = self.call("v3_cargo_task", {"task_id": "cargo-task", "task": "check",
                "expected_inputs_sha256": digest})
        self.assertEqual("stale", stale["status"])

    def test_isolated_tasks_release_in_order_while_other_work_remains_open(self):
        first = self.call("v3_build_work", {"action": "begin", "task_id": "isolated-one",
            "summary": "First", "mode": "isolated"})
        second = self.call("v3_build_work", {"action": "begin", "task_id": "isolated-two",
            "summary": "Second", "mode": "isolated"})
        Path(first["workspace_path"], "src").mkdir()
        Path(first["workspace_path"], "src/first.rs").write_text("first")
        Path(second["workspace_path"], "src").mkdir()
        Path(second["workspace_path"], "src/second.rs").write_text("second")
        status = self.call("v3_build_work", {"action": "status"})["own"]
        selected = self.call("v3_build_work", {"action": "status", "task_id": "isolated-one"})["own"]
        self.assertEqual(["isolated-one"], [item["task_id"] for item in selected])
        with self.assertRaisesRegex(ValueError, "bounded task_id"):
            self.call("v3_build_work", {"action": "status", "task_id": "../wrong"})
        digests = {item["task_id"]: item["workspace_inputs_sha256"] for item in status}
        one = self.call("v3_build_work", {"action": "finish", "task_id": "isolated-one",
            "expected_inputs_sha256": digests["isolated-one"]})
        self.assertIsNotNone(one["job_id"])
        self.assertEqual(1, one["open_count"])
        self.assertEqual("completed", self.wait(one)["status"])
        two = self.call("v3_build_work", {"action": "finish", "task_id": "isolated-two",
            "expected_inputs_sha256": digests["isolated-two"]})
        self.assertEqual("completed", self.wait(two)["status"])
        final = host.release.entries(
            (self.state / two["job_id"] / "candidate.tar").read_bytes(), host.context_digest)
        self.assertIn("v3/src/first.rs", final)
        self.assertIn("v3/src/second.rs", final)
        self.assertEqual(4, len([call for call in self.runner.calls if call[0] == "build"]))
        self.assertEqual(2, len([call for call in self.runner.calls if call[0] == "promote"]))
        self.assertEqual(two["job_id"], self.call("v3_build_work", {
            "action": "finish", "task_id": "isolated-two",
            "expected_inputs_sha256": digests["isolated-two"]})["job_id"])

    def test_isolated_finish_queues_during_another_build(self):
        first = self.call("v3_build_work", {"action": "begin", "task_id": "queued-one",
            "summary": "First", "mode": "isolated"})
        second = self.call("v3_build_work", {"action": "begin", "task_id": "queued-two",
            "summary": "Second", "mode": "isolated"})
        for item, filename in ((first, "one.rs"), (second, "two.rs")):
            folder = Path(item["workspace_path"], "src")
            folder.mkdir()
            (folder / filename).write_text(filename)
        digests = {item["task_id"]: item["workspace_inputs_sha256"]
                   for item in self.call("v3_build_work", {"action": "status"})["own"]}
        self.runner.block = threading.Event()
        one = self.call("v3_build_work", {"action": "finish", "task_id": "queued-one",
            "expected_inputs_sha256": digests["queued-one"]})
        two = self.call("v3_build_work", {"action": "finish", "task_id": "queued-two",
            "expected_inputs_sha256": digests["queued-two"]})
        self.assertEqual("queued", self.call("v3_worker_job", {
            "job_id": two["job_id"]})["status"])
        self.runner.block.set()
        self.assertEqual("completed", self.wait(one)["status"])
        self.assertEqual("completed", self.wait(two)["status"])
        self.assertEqual(4, len([call for call in self.runner.calls if call[0] == "build"]))

    def test_ui_only_release_reuses_images_and_skips_rust_build(self):
        task = self.call("v3_build_work", {"action": "begin", "task_id": "ui-only-task",
            "summary": "UI asset", "mode": "isolated"})
        asset = Path(task["workspace_path"], "ui/fast-lane.css")
        asset.parent.mkdir(exist_ok=True)
        asset.write_text("body { color: red; }")
        digest = self.call("v3_build_work", {"action": "status", "task_id": "ui-only-task"})["own"][0]["workspace_inputs_sha256"]
        receipt = self.call("v3_build_work", {"action": "finish", "task_id": "ui-only-task",
            "expected_inputs_sha256": digest})
        job = self.wait(receipt)
        self.assertEqual("ui_deploy", job["operation"])
        self.assertEqual("completed", job["status"])
        self.assertEqual([], [call for call in self.runner.calls if call[0] == "build"])
        self.assertEqual({"api": "sha256:" + "1" * 64, "loop": "sha256:" + "1" * 64}, job["images"])
        self.assertEqual("not_required", self.broker.jobs[receipt["job_id"]]["rust_tests"]["status"])
        released = host.release.entries(
            (self.state / receipt["job_id"] / "candidate.tar").read_bytes(), host.context_digest)
        self.assertEqual(b"body { color: red; }", released["v3/ui/fast-lane.css"][1])

    def test_ui_release_switches_assets_without_replacing_containers(self):
        first = self.call("v3_build_work", {"action": "begin", "task_id": "runtime-first",
            "summary": "Runtime migration", "mode": "isolated"})
        source = Path(first["workspace_path"], "src/feature.rs")
        source.parent.mkdir(exist_ok=True)
        source.write_text("feature")
        digest = self.call("v3_build_work", {"action": "status", "task_id": "runtime-first"})["own"][0]["workspace_inputs_sha256"]
        full = self.call("v3_build_work", {"action": "finish", "task_id": "runtime-first",
            "expected_inputs_sha256": digest})
        self.assertEqual("completed", self.wait(full)["status"])
        self.assertTrue(self.runner.hot_assets)
        self.assertEqual(full["job_id"], self.runner.asset_id)
        prior_lifecycle = len([call for call in self.runner.calls if call[0] in ("stop", "create")])

        ui = self.call("v3_build_work", {"action": "begin", "task_id": "asset-second",
            "summary": "UI fast lane", "mode": "isolated"})
        asset = Path(ui["workspace_path"], "ui/hot.css")
        asset.parent.mkdir(exist_ok=True)
        asset.write_text("body {}")
        digest = self.call("v3_build_work", {"action": "status", "task_id": "asset-second"})["own"][0]["workspace_inputs_sha256"]
        receipt = self.call("v3_build_work", {"action": "finish", "task_id": "asset-second",
            "expected_inputs_sha256": digest})
        result = self.wait(receipt)
        self.assertEqual("completed", result["status"])
        self.assertEqual("ui_deploy", result["operation"])
        self.assertEqual(receipt["job_id"], self.runner.asset_id)
        self.assertEqual(prior_lifecycle, len([call for call in self.runner.calls
                                               if call[0] in ("stop", "create")]))
        self.assertEqual(2, len([call for call in self.runner.calls if call[0] == "build"]))
        self.assertIn(("verify_ui", ("v3/ui/hot.css",)), self.runner.calls)

    def test_hot_ui_verification_failure_is_uncertain_and_blocks_queue(self):
        self.runner.hot_assets = True
        task = self.call("v3_build_work", {"action": "begin", "task_id": "failed-hot-ui",
            "summary": "UI verification", "mode": "isolated"})
        asset = Path(task["workspace_path"], "ui/fail.css")
        asset.parent.mkdir(exist_ok=True)
        asset.write_text("body {}")
        digest = self.call("v3_build_work", {"action": "status", "task_id": "failed-hot-ui"})["own"][0]["workspace_inputs_sha256"]
        self.runner.fail_verify_ui = True
        receipt = self.call("v3_build_work", {"action": "finish", "task_id": "failed-hot-ui",
            "expected_inputs_sha256": digest})
        result = self.wait(receipt)
        self.assertEqual("uncertain", result["status"])
        self.assertEqual(receipt["job_id"], self.runner.asset_id)
        self.broker.shutdown()
        self.runner.fail_verify_ui = False
        self.broker = host.Broker(self.state, self.runner)
        self.assertEqual("uncertain", self.broker.jobs[receipt["job_id"]]["status"])
        next_task = self.call("v3_build_work", {"action": "begin", "task_id": "after-uncertain",
            "summary": "Later runtime", "mode": "isolated"})
        source = Path(next_task["workspace_path"], "src/later.rs")
        source.parent.mkdir(exist_ok=True)
        source.write_text("later")
        later_digest = self.call("v3_build_work", {"action": "status", "task_id": "after-uncertain"})["own"][0]["workspace_inputs_sha256"]
        later = self.call("v3_build_work", {"action": "finish", "task_id": "after-uncertain",
            "expected_inputs_sha256": later_digest})
        self.assertEqual("queued", self.call("v3_worker_job", {"job_id": later["job_id"]})["status"])

    def test_ui_switches_during_build_and_runtime_rebases_before_deploy(self):
        bootstrap = self.call("v3_build_work", {"action": "begin", "task_id": "bootstrap-ui",
            "summary": "Migrate asset mount", "mode": "isolated"})
        first_file = Path(bootstrap["workspace_path"], "src/bootstrap.rs")
        first_file.parent.mkdir(exist_ok=True)
        first_file.write_text("bootstrap")
        digest = self.call("v3_build_work", {"action": "status", "task_id": "bootstrap-ui"})["own"][0]["workspace_inputs_sha256"]
        first = self.call("v3_build_work", {"action": "finish", "task_id": "bootstrap-ui",
            "expected_inputs_sha256": digest})
        self.assertEqual("completed", self.wait(first)["status"])

        runtime = self.call("v3_build_work", {"action": "begin", "task_id": "running-runtime",
            "summary": "Runtime work", "mode": "isolated"})
        ui = self.call("v3_build_work", {"action": "begin", "task_id": "parallel-ui",
            "summary": "UI work", "mode": "isolated"})
        runtime_file = Path(runtime["workspace_path"], "src/runtime.rs")
        runtime_file.parent.mkdir(exist_ok=True)
        runtime_file.write_text("runtime")
        ui_file = Path(ui["workspace_path"], "ui/parallel.css")
        ui_file.parent.mkdir(exist_ok=True)
        ui_file.write_text("body {}")
        digests = {item["task_id"]: item["workspace_inputs_sha256"]
                   for item in self.call("v3_build_work", {"action": "status"})["own"]}
        self.runner.block = threading.Event()
        full = self.call("v3_build_work", {"action": "finish", "task_id": "running-runtime",
            "expected_inputs_sha256": digests["running-runtime"]})
        for _ in range(100):
            if self.broker.jobs[full["job_id"]]["phase"] == "building_api":
                break
            time.sleep(.01)
        self.assertEqual("building_api", self.broker.jobs[full["job_id"]]["phase"])
        fast = self.call("v3_build_work", {"action": "finish", "task_id": "parallel-ui",
            "expected_inputs_sha256": digests["parallel-ui"]})
        self.assertEqual("running", self.broker.jobs[full["job_id"]]["status"])
        self.assertTrue(self.broker.jobs[fast["job_id"]].get("parallel_ui"))
        for _ in range(100):
            if self.call("v3_worker_job", {"job_id": fast["job_id"]})["status"] == "completed":
                break
            time.sleep(.01)
        self.assertEqual("completed", self.call("v3_worker_job", {"job_id": fast["job_id"]})["status"])
        self.assertEqual("running", self.call("v3_worker_job", {"job_id": full["job_id"]})["status"])
        self.assertEqual(fast["job_id"], self.runner.asset_id)
        self.runner.block.set()
        final = self.wait(full)
        self.assertEqual("completed", final["status"])
        self.assertIn("built_inputs_sha256", self.broker.jobs[full["job_id"]])
        combined = host.release.entries(
            (self.state / full["job_id"] / "candidate.tar").read_bytes(), host.context_digest)
        self.assertIn("v3/ui/parallel.css", combined)
        self.assertIn("v3/src/runtime.rs", combined)

    def test_ui_release_passes_queued_build_and_later_build_inherits_assets(self):
        tasks = []
        for name in ("first", "second", "ui-third"):
            tasks.append(self.call("v3_build_work", {"action": "begin", "task_id": name + "-task",
                "summary": name, "mode": "isolated"}))
        for task, path in zip(tasks, ("src/first.rs", "src/second.rs", "ui/fast-lane.css")):
            asset = Path(task["workspace_path"], path)
            asset.parent.mkdir(exist_ok=True)
            asset.write_text(path)
        digests = {item["task_id"]: item["workspace_inputs_sha256"]
                   for item in self.call("v3_build_work", {"action": "status"})["own"]}
        self.runner.block = threading.Event()
        receipts = [self.call("v3_build_work", {"action": "finish", "task_id": name + "-task",
            "expected_inputs_sha256": digests[name + "-task"]})
            for name in ("first", "second", "ui-third")]
        self.runner.block.set()
        for receipt in receipts:
            self.assertEqual("completed", self.wait(receipt)["status"])
        promotions = [call[1] for call in self.runner.calls if call[0] == "promote"]
        self.assertEqual([("v3/src/first.rs",), ("v3/ui/fast-lane.css",),
                          ("v3/src/second.rs",)], promotions)
        released = host.release.entries(
            (self.state / receipts[1]["job_id"] / "candidate.tar").read_bytes(), host.context_digest)
        self.assertIn("v3/ui/fast-lane.css", released)
        self.assertEqual(4, len([call for call in self.runner.calls if call[0] == "build"]))

    def test_compatible_queued_tasks_share_one_tested_image_build(self):
        tasks = []
        for name in ("running-one", "queued-two", "queued-three"):
            tasks.append(self.call("v3_build_work", {"action": "begin", "task_id": name,
                "summary": name, "mode": "isolated"}))
        for task, name in zip(tasks, ("running.rs", "second.rs", "third.rs")):
            path = Path(task["workspace_path"], "src", name)
            path.parent.mkdir(exist_ok=True)
            path.write_text(name)
        digests = {item["task_id"]: item["workspace_inputs_sha256"]
                   for item in self.call("v3_build_work", {"action": "status"})["own"]}
        self.runner.block = threading.Event()
        receipts = [self.call("v3_build_work", {"action": "finish", "task_id": name,
            "expected_inputs_sha256": digests[name]})
            for name in ("running-one", "queued-two", "queued-three")]
        self.runner.block.set()
        results = [self.wait(receipt) for receipt in receipts]
        self.assertEqual(["completed"] * 3, [item["status"] for item in results])
        self.assertEqual(receipts[1]["job_id"], results[2]["batch_parent"])
        self.assertEqual([receipts[2]["job_id"]], results[1]["batch_members"])
        self.assertEqual(results[1]["inputs_sha256"], results[2]["inputs_sha256"])
        self.assertEqual(4, len([call for call in self.runner.calls if call[0] == "build"]))
        self.assertEqual(2, len([call for call in self.runner.calls if call[0] == "promote"]))
        released = host.release.entries(
            (self.state / receipts[1]["job_id"] / "candidate.tar").read_bytes(), host.context_digest)
        self.assertIn("v3/src/second.rs", released)
        self.assertIn("v3/src/third.rs", released)

    def test_isolated_conflict_fails_without_second_deployment(self):
        first = self.call("v3_build_work", {"action": "begin", "task_id": "conflict-one",
            "summary": "First", "mode": "isolated"})
        second = self.call("v3_build_work", {"action": "begin", "task_id": "conflict-two",
            "summary": "Second", "mode": "isolated"})
        for item, value in ((first, "one"), (second, "two")):
            Path(item["workspace_path"], "Cargo.toml").write_text(value)
        digests = {item["task_id"]: item["workspace_inputs_sha256"]
                   for item in self.call("v3_build_work", {"action": "status"})["own"]}
        one = self.call("v3_build_work", {"action": "finish", "task_id": "conflict-one",
            "expected_inputs_sha256": digests["conflict-one"]})
        self.assertEqual("completed", self.wait(one)["status"])
        two = self.call("v3_build_work", {"action": "finish", "task_id": "conflict-two",
            "expected_inputs_sha256": digests["conflict-two"]})
        result = self.wait(two)
        self.assertEqual("failed", result["status"])
        self.assertIn("source conflict", result["reason"])
        self.assertEqual(2, len([call for call in self.runner.calls if call[0] == "build"]))

    def test_coordinated_work_batches_one_build_and_deployment(self):
        first = self.call("v3_build_work", {"action": "begin", "task_id": "first-task", "summary": "First change"})
        second = self.call("v3_build_work", {"action": "begin", "task_id": "second-task", "summary": "Second change"})
        self.assertEqual(2, second["open_count"])
        with self.assertRaisesRegex(ValueError, "work remains open"):
            self.build()
        digest = host.context_digest(self.runner.snapshot())[0]
        closed = self.call("v3_build_work", {"action": "finish", "task_id": "first-task",
                                              "expected_inputs_sha256": digest})
        self.assertIsNone(closed["job_id"])
        closed = self.call("v3_build_work", {"action": "finish", "task_id": "second-task",
                                              "expected_inputs_sha256": digest})
        self.assertEqual("completed", self.wait({"job_id": closed["job_id"]})["status"])
        duplicate = self.call("v3_build_work", {"action": "finish", "task_id": "second-task",
                                                 "expected_inputs_sha256": digest})
        self.assertEqual(closed["job_id"], duplicate["job_id"])
        first_duplicate = self.call("v3_build_work", {"action": "finish", "task_id": "first-task",
                                                       "expected_inputs_sha256": digest})
        self.assertEqual(closed["job_id"], first_duplicate["job_id"])
        self.assertEqual(2, len([c for c in self.runner.calls if c[0] == "build"]))
        self.assertEqual(2, len([c for c in self.runner.calls if c[0] == "stop"]))
        self.assertEqual(2, len([c for c in self.runner.calls if c[0] == "healthy"]))
        self.assertEqual(0, self.call("v3_build_work", {"action": "status"})["open_count"])

    def test_coordinated_work_rejects_stale_inputs_without_replacement(self):
        self.call("v3_build_work", {"action": "begin", "task_id": "stale-task", "summary": "Change"})
        old = host.context_digest(self.runner.snapshot())[0]
        self.runner.blob = context("changed")
        with self.assertRaisesRegex(ValueError, "stale work inputs"):
            self.call("v3_build_work", {"action": "finish", "task_id": "stale-task",
                                         "expected_inputs_sha256": old})
        self.assertEqual(1, self.call("v3_build_work", {"action": "status"})["open_count"])
        self.assertFalse(any(c[0] == "stop" for c in self.runner.calls))

    def test_coordinated_build_changed_inputs_never_stops_live_containers(self):
        self.call("v3_build_work", {"action": "begin", "task_id": "moving-task", "summary": "Moving"})
        self.runner.change_during_build = True
        digest = host.context_digest(self.runner.snapshot())[0]
        job = self.call("v3_build_work", {"action": "finish", "task_id": "moving-task",
                                           "expected_inputs_sha256": digest})
        result = self.wait({"job_id": job["job_id"]})
        self.assertEqual("failed", result["status"])
        self.assertFalse(any(c[0] == "stop" for c in self.runner.calls))

    def test_coordinated_work_survives_broker_restart(self):
        self.call("v3_build_work", {"action": "begin", "task_id": "saved-task", "summary": "Saved"})
        self.broker.shutdown()
        self.broker = host.Broker(self.state, self.runner)
        self.assertEqual(1, self.call("v3_build_work", {"action": "status"})["open_count"])
        with self.assertRaisesRegex(ValueError, "work remains open"):
            self.build()

    def test_worker_restart_uses_fixed_local_control_and_refuses_busy_broker(self):
        calls = []
        def control(method, path, body=None):
            calls.append((method, path, body))
            return {"status": "restarting", "previous_pid": 10} if method == "POST" else {"pid": 11}
        with patch.object(host, "worker_control", control), patch.object(host.time, "sleep"):
            value = self.call("v3_build_worker_restart", {"request_id": "stable-restart"})
        self.assertEqual(value["status"], "ok")
        self.assertEqual(calls, [("POST", "/v1/restart", {"request_id": "stable-restart"}),
                                 ("GET", "/v1/status", None)])
        with self.assertRaises(ValueError):
            self.call("v3_build_worker_restart", {"request_id": "../bad"})
        self.broker.active = "another-job"
        with self.assertRaisesRegex(ValueError, "busy"):
            self.call("v3_build_worker_restart", {"request_id": "another-restart"})
        self.broker.active = None
        self.assertEqual(len(calls), 2)

    def test_low_disk_fails_before_build_or_container_replacement(self):
        self.runner.low_space = True
        result = self.wait(self.build())
        self.assertEqual("failed", result["status"])
        self.assertFalse(any(call[0] == "build" for call in self.runner.calls))
        self.assertFalse(any(call[0] == "stop" for call in self.runner.calls))
        self.assertIn(("space", host.MIN_SEQUENCE_FREE), self.runner.calls)

    def test_frozen_build_idempotence_and_secret_scrub(self):
        value = self.wait(self.build())
        self.assertEqual("completed", value["status"])
        self.assertEqual({"api", "loop"}, set(value["images"]))
        self.assertNotIn("NEVER-EXPOSE-THIS", json.dumps(value))
        duplicate = self.build()
        self.assertEqual(value["job_id"], duplicate["job_id"])
        self.assertEqual(2, len([c for c in self.runner.calls if c[0] == "build"]))
        with self.assertRaisesRegex(ValueError, "changed body"):
            self.call("v3_build_image", {"request_id": "build-fixture", "expected_inputs_sha256": "0" * 64})

    def test_changed_inputs_mark_stale_and_block_deploy(self):
        self.runner.change_during_build = True
        value = self.wait(self.build())
        self.assertEqual("stale", value["status"])
        with self.assertRaisesRegex(ValueError, "stale build inputs"):
            self.deploy(value)
        self.assertFalse(any(c[0] == "stop" for c in self.runner.calls))

    def test_busy_and_scope_rejections(self):
        self.runner.block = threading.Event()
        submitted = self.build()
        with self.assertRaisesRegex(ValueError, "busy"):
            self.build("second-build")
        other = str(uuid.uuid4())
        with self.assertRaisesRegex(ValueError, "not available"):
            self.call("v3_worker_job", {"job_id": submitted["job_id"]}, {"user_id": "other", "session_id": other}, other)
        with self.assertRaises(ValueError):
            self.call("v3_build_status", {"source": "/home/ar/webagent"})
        with self.assertRaisesRegex(ValueError, "unsupported"):
            self.call("v3_worker_submit", {"operation": "shell", "command": "docker rm production"})
        self.runner.block.set()
        self.wait(submitted)

    def test_deploy_preserves_rollback_and_private_configuration(self):
        built = self.wait(self.build())
        deployed = self.wait(self.deploy(built))
        self.assertEqual("completed", deployed["status"])
        archive = self.state / deployed["job_id"] / "rollback-images.tar"
        self.assertTrue(archive.exists())
        for rollback in deployed["rollback"].values():
            self.assertEqual("exited", self.runner.containers[rollback]["State"]["Status"])
        self.assertNotIn("NEVER-EXPOSE-THIS", json.dumps(deployed))
        private = self.state / deployed["job_id"] / "container-config.json"
        self.assertEqual(0o600, private.stat().st_mode & 0o777)
        self.assertIn("NEVER-EXPOSE-THIS", private.read_text())
        self.assertFalse(any("webagent-dev-v2" in str(c) for c in self.runner.calls))

    def test_deploy_starts_both_before_health_checks_requiring_loop_heartbeat(self):
        built = self.wait(self.build())
        self.runner.require_both_started = True
        deployed = self.wait(self.deploy(built))
        self.assertEqual("completed", deployed["status"])
        starts = [i for i, call in enumerate(self.runner.calls) if call[0] == "start"]
        checks = [i for i, call in enumerate(self.runner.calls) if call[0] == "healthy"]
        self.assertEqual(2, len(starts))
        self.assertEqual(2, len(checks))
        self.assertLess(max(starts), min(checks))
        self.assertEqual(set(host.TARGETS), {self.runner.calls[i][1] for i in checks})

    def test_superseded_uncertain_release_does_not_hold_queue(self):
        old = {"job_id": str(uuid.uuid4()), "status": "uncertain",
               "operation": "build_deploy", "isolated": True,
               "effects_started": True, "created_at": 1, "finished_at": 2}
        self.broker.jobs[old["job_id"]] = old
        self.assertTrue(self.broker._has_unresolved_release())
        interrupted = {"job_id": str(uuid.uuid4()), "status": "interrupted",
                       "operation": "build_deploy", "created_at": 3}
        self.broker.jobs[interrupted["job_id"]] = interrupted
        self.assertTrue(self.broker._has_unresolved_release())
        completed = {"job_id": str(uuid.uuid4()), "status": "completed",
                     "operation": "build_deploy", "isolated": True,
                     "effects_started": True, "created_at": 4, "finished_at": 5}
        self.broker.jobs[completed["job_id"]] = completed
        self.assertFalse(self.broker._has_unresolved_release())


    def test_uncertain_external_effect_is_not_replayed_on_restart(self):
        built = self.wait(self.build())
        self.runner.fail_create = True
        deployed = self.wait(self.deploy(built))
        self.assertEqual("uncertain", deployed["status"])
        mutations = len([c for c in self.runner.calls if c[0] in ("stop", "rename", "create", "start")])
        self.broker.shutdown()
        self.broker = host.Broker(self.state, self.runner)
        self.assertEqual("uncertain", self.call("v3_worker_job", {"job_id": deployed["job_id"]})["status"])
        duplicate = self.deploy(built)
        self.assertEqual(deployed["job_id"], duplicate["job_id"])
        self.assertEqual(mutations, len([c for c in self.runner.calls if c[0] in ("stop", "rename", "create", "start")]))

    def test_operator_adopts_only_matching_healthy_uncertain_release(self):
        item = self.call("v3_build_work", {"action": "begin", "task_id": "adopt-release",
            "summary": "Adopt release", "mode": "isolated"})
        source = Path(item["workspace_path"], "src", "adopt.rs")
        source.parent.mkdir()
        source.write_text("adopt")
        digest = self.call("v3_build_work", {"action": "status",
            "task_id": "adopt-release"})["own"][0]["workspace_inputs_sha256"]
        def conflict(*_args):
            raise ValueError("checkout changed during release")
        self.runner.promote_checkout = conflict
        receipt = self.call("v3_build_work", {"action": "finish",
            "task_id": "adopt-release", "expected_inputs_sha256": digest})
        result = self.wait(receipt)
        self.assertEqual("uncertain", result["status"])
        with self.assertRaises(PermissionError):
            self.call("v3_build_adopt_release", {"job_id": receipt["job_id"]})
        local = {"user_id": "codex-local", "session_id": host.LOCAL_WORKSPACE,
                 "local_caller": True, "lease_until": time.time() + 30,
                 "access_fingerprint": "a" * 64}
        self.runner.containers[host.TARGETS[0]]["Image"] = "sha256:" + "0" * 64
        with self.assertRaisesRegex(ValueError, "does not match healthy runtime"):
            self.call("v3_build_adopt_release", {"job_id": receipt["job_id"]},
                      trusted=local, workspace=host.LOCAL_WORKSPACE)
        self.runner.containers[host.TARGETS[0]]["Image"] = result["images"]["api"]
        accepted = self.call("v3_build_adopt_release", {"job_id": receipt["job_id"]},
                             trusted=local, workspace=host.LOCAL_WORKSPACE)
        self.assertTrue(accepted["operator_adopted"])
        self.assertEqual("uncertain", self.broker.jobs[receipt["job_id"]]["status"])
        self.assertFalse(self.broker._has_unresolved_release())
        self.broker.shutdown()
        self.broker = host.Broker(self.state, self.runner)
        self.assertFalse(self.broker._has_unresolved_release())
        self.assertEqual(result["inputs_sha256"],
                         host.context_digest(self.broker._current_release_blob())[0])


    def test_operator_adopts_healthy_hot_ui_checkout_conflict(self):
        self.runner.hot_assets = True
        task = self.call("v3_build_work", {"action": "begin", "task_id": "adopt-hot-ui",
            "summary": "Adopt hot UI", "mode": "isolated"})
        asset = Path(task["workspace_path"], "ui/hot-adopt.css")
        asset.parent.mkdir(exist_ok=True)
        asset.write_text("body {}")
        digest = self.call("v3_build_work", {"action": "status"})["own"][0]["workspace_inputs_sha256"]
        self.runner.promote_checkout = lambda *_: (_ for _ in ()).throw(ValueError("checkout changed"))
        receipt = self.call("v3_build_work", {"action": "finish",
            "task_id": "adopt-hot-ui", "expected_inputs_sha256": digest})
        result = self.wait(receipt)
        self.assertEqual("uncertain", result["status"])
        self.assertEqual(receipt["job_id"], self.runner.asset_id)
        local = {"user_id": "codex-local", "session_id": host.LOCAL_WORKSPACE,
                 "local_caller": True, "lease_until": time.time() + 30,
                 "access_fingerprint": "a" * 64}
        self.runner.fail_verify_ui = True
        with self.assertRaisesRegex(ValueError, "does not match healthy runtime"):
            self.call("v3_build_adopt_release", {"job_id": receipt["job_id"]},
                      trusted=local, workspace=host.LOCAL_WORKSPACE)
        self.runner.fail_verify_ui = False
        accepted = self.call("v3_build_adopt_release", {"job_id": receipt["job_id"]},
                             trusted=local, workspace=host.LOCAL_WORKSPACE)
        self.assertTrue(accepted["operator_adopted"])
        self.assertFalse(self.broker._has_unresolved_release())


    def test_recovery_reconciles_matching_healthy_deployment(self):
        built = self.wait(self.build())
        deployed = self.wait(self.deploy(built))
        job = self.broker.jobs[deployed["job_id"]]
        job["status"] = "running"
        self.broker._save(job)
        self.broker.shutdown()
        self.broker = host.Broker(self.state, self.runner)
        recovered = self.call("v3_worker_job", {"job_id": deployed["job_id"]})
        self.assertEqual("completed", recovered["status"])
        self.assertIn("recovered", recovered["reason"])

    def test_only_one_broker_and_allowlisted_tar(self):
        with self.assertRaisesRegex(ValueError, "already running"):
            host.Broker(self.state, self.runner)
        for extra in ({"v3/data/session.db": "private"}, {"v3/src/../../data/secrets": "private"}):
            with self.assertRaisesRegex(ValueError, "allowlist"):
                host.context_digest(context(extra=extra))
        with self.assertRaisesRegex(ValueError, "cache"):
            host.context_digest(context(extra={"v3/tests/__pycache__/generated.pyc": "bytecode"}))
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as archive:
            member = tarfile.TarInfo("v3/src/link")
            member.type, member.linkname = tarfile.SYMTYPE, "/state"
            archive.addfile(member)
        with self.assertRaisesRegex(ValueError, "symlink"):
            host.context_digest(stream.getvalue())

    def test_pending_deploy_revocation_prevents_first_external_mutation(self):
        built = self.wait(self.build())
        self.runner.export_entered = threading.Event()
        self.runner.export_release = threading.Event()
        submitted = self.deploy(built)
        self.assertTrue(self.runner.export_entered.wait(5))
        revoked = self.call("administrator_host_revoke")
        self.assertEqual(1, revoked["updated"])
        self.runner.export_release.set()
        result = self.wait(submitted)
        self.assertEqual("cancelled", result["status"])
        self.assertFalse(any(c[0] in ("stop", "rename", "create", "start") for c in self.runner.calls))
        self.assertTrue((self.state / result["job_id"] / "rollback-images.tar").is_file())

    def test_self_replacement_keeps_bounded_commit_lease(self):
        built = self.wait(self.build())
        self.runner.stop_entered = threading.Event()
        self.runner.stop_release = threading.Event()
        submitted = self.deploy(built, "commit-lease")
        self.assertTrue(self.runner.stop_entered.wait(5))
        before = self.broker.jobs[submitted["job_id"]]["lease_until"]
        self.assertGreater(before - time.time(), 100)
        self.call("administrator_host_lease", trusted={**self.trusted,
                  "lease_until": time.time() + 30})
        self.assertEqual(before, self.broker.jobs[submitted["job_id"]]["lease_until"])
        self.runner.stop_release.set()
        self.assertEqual("completed", self.wait(submitted)["status"])

    def test_revocation_after_first_stop_is_uncertain_without_further_effects(self):
        built = self.wait(self.build())
        self.runner.stop_entered = threading.Event()
        self.runner.stop_release = threading.Event()
        submitted = self.deploy(built)
        self.assertTrue(self.runner.stop_entered.wait(5))
        self.call("administrator_host_revoke")
        self.runner.stop_release.set()
        result = self.wait(submitted)
        self.assertEqual("uncertain", result["status"])
        self.assertEqual(1, len([c for c in self.runner.calls if c[0] == "stop"]))
        self.assertFalse(any(c[0] in ("rename", "create", "start") for c in self.runner.calls))

    def test_changed_fingerprint_stops_job_and_expired_lease_cannot_revive(self):
        self.runner.block = threading.Event()
        submitted = self.build()
        changed = {**self.trusted, "access_fingerprint": "e" * 64, "lease_until": time.time() + 30}
        result = self.call("administrator_host_lease", trusted=changed)
        self.assertEqual(1, result["updated"])
        self.runner.block.set()
        self.assertEqual("cancelled", self.wait(submitted)["status"])
        with self.assertRaisesRegex(ValueError, "lease"):
            self.call("administrator_host_lease", trusted={**self.trusted, "lease_until": time.time() - 1})

    def test_public_log_and_failure_reason_fit_model_budget(self):
        value = self.wait(self.build())
        job = self.broker.jobs[value["job_id"]]
        job["log"] = "あ" * 100000
        job["reason"] = "あ" * 100000 + "final failure"
        view = self.call("v3_worker_job", {"job_id": value["job_id"]})
        self.assertLessEqual(len(view["log"].encode()), host.PUBLIC_LOG)
        self.assertLessEqual(len(view["reason"].encode()), 4096)
        self.assertTrue(view["reason"].endswith("final failure"))
        self.assertTrue(view["log_truncated"])
        self.assertEqual(300000, view["log_bytes"])
        self.assertLess(len(json.dumps(view, ensure_ascii=False).encode()), 22000)

    def test_atomic_export_is_unpublished_until_command_succeeds(self):
        runner = host.Runner()
        destination = Path(self.temp.name) / "rollback-images.tar"
        def command(argv, **kwargs):
            self.assertFalse(destination.exists())
            Path(argv[argv.index("-o") + 1]).write_bytes(b"image archive")
            return ""
        with patch.object(runner, "command", command):
            runner.export(["sha256:" + "a" * 64], destination)
        self.assertEqual(b"image archive", destination.read_bytes())
        self.assertEqual(0o600, destination.stat().st_mode & 0o777)
        def fail(argv, **kwargs):
            Path(argv[argv.index("-o") + 1]).write_bytes(b"incomplete")
            raise RuntimeError("export failed")
        with patch.object(runner, "command", fail), self.assertRaises(RuntimeError):
            runner.export(["sha256:" + "a" * 64], destination)
        self.assertEqual(b"image archive", destination.read_bytes())
        self.assertFalse(list(destination.parent.glob(".rollback-*")))

    def test_shutdown_revokes_active_job_without_replaying(self):
        self.runner.block = threading.Event()
        submitted = self.build()
        self.broker.request_shutdown()
        self.runner.block.set()
        self.assertEqual("cancelled", self.wait(submitted)["status"])
        with self.assertRaisesRegex(PermissionError, "stopping"):
            self.build("after-shutdown")

    def test_replacement_adopts_defaults_keeps_explicit_credentials(self):
        old = {"Env": ["MODE=old", "TOKEN=default"], "Cmd": ["old"], "Labels": {"image": "old"}}
        new = {"Env": ["MODE=new", "ADDED=yes"], "Cmd": ["new"], "Labels": {"image": "new"}}
        current = {"Env": ["MODE=old", "TOKEN=private", "CUSTOM=yes"], "Cmd": ["old"], "Labels": {"image": "old", "custom": "yes"}}
        value = host.replacement_config(current, old, new)
        self.assertEqual(["new"], value["Cmd"])
        self.assertEqual({"MODE=new", "ADDED=yes", "TOKEN=private", "CUSTOM=yes"}, set(value["Env"]))
        self.assertEqual({"image": "new", "custom": "yes"}, value["Labels"])


class InheritedSocketTests(unittest.IsolatedAsyncioTestCase):
    async def test_codex_and_webagent_share_work_registry(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "broker.sock")
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(path)
            listener.listen(4)
            broker = host.Broker(Path(temp) / "jobs", FakeRunner())
            service = asyncio.create_task(host.serve(broker, listener))

            async def local(name, arguments):
                reader, writer = await asyncio.open_unix_connection(path)
                writer.write(json.dumps({"id": "local-test", "name": name, "arguments": arguments}).encode() + b"\n")
                await writer.drain()
                response = json.loads(await reader.readline())
                writer.close()
                await writer.wait_closed()
                return response

            try:
                await asyncio.sleep(.02)
                own_session = str(uuid.uuid4())
                trusted = {"user_id": "v3-user", "session_id": own_session,
                           "lease_until": time.time() + 30, "access_fingerprint": "f" * 64}
                def app(action, task_id, **more):
                    return broker.handle({"name": "v3_build_work", "workspace": own_session,
                                          "trusted": trusted, "arguments": {"action": action,
                                          "task_id": task_id, **more}})
                self.assertEqual("success", (await local("v3_build_work", {
                    "action": "begin", "task_id": "codex-task", "summary": "Codex change"}))["status"])
                app("begin", "webagent-task", summary="WebAgent change")
                self.assertEqual(2, (await local("v3_build_work", {"action": "status"}))["output"]["open_count"])
                forbidden = await local("v3_worker_submit", {"operation": "deploy"})
                self.assertEqual("failed", forbidden["status"])
                digest = host.context_digest(broker.runner.snapshot())[0]
                first = await local("v3_build_work", {"action": "finish", "task_id": "codex-task",
                                                       "expected_inputs_sha256": digest})
                self.assertIsNone(first["output"]["job_id"])
                second = app("finish", "webagent-task", expected_inputs_sha256=digest)
                job_id = second["job_id"]
                self.assertEqual("success", (await local("v3_worker_job", {"job_id": job_id}))["status"])
                for _ in range(100):
                    result = broker.handle({"name": "v3_worker_job", "workspace": own_session,
                                            "trusted": trusted, "arguments": {"job_id": job_id}})
                    if result["status"] not in ("queued", "running"):
                        break
                    await asyncio.sleep(.01)
                self.assertEqual("completed", result["status"])
                self.assertEqual(2, len([c for c in broker.runner.calls if c[0] == "build"]))
            finally:
                service.cancel()
                try:
                    await service
                except asyncio.CancelledError:
                    pass
                listener.close()
                broker.shutdown()

    async def test_root_fd_address_needs_no_child_path_cleanup(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = os.open(temp, os.O_RDONLY | os.O_DIRECTORY)
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind("/proc/self/fd/" + str(directory) + "/broker.sock")
            listener.listen(1)
            os.close(directory)  # Closed across the broker's privilege-drop exec.
            broker = type("FixtureBroker", (), {"runner": FakeRunner()})()
            task = asyncio.create_task(host.serve(broker, listener))
            try:
                await asyncio.sleep(.02)
                self.assertFalse(task.done(), "inherited listener failed to start")
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                listener.close()


class RunnerSpaceTests(unittest.TestCase):
    def test_task_cargo_test_uses_binary_test_target(self):
        runner = host.Runner()
        with tempfile.TemporaryDirectory() as root, patch.object(runner, "command", return_value="ok") as command:
            self.assertEqual("ok", runner.cargo_task(Path(root), Path(root) / "checks", "test", 60))
        argv = command.call_args.args[0]
        self.assertEqual(["cargo", "test", "--locked", "--", "--test-threads=1"], argv[-5:])

    def test_preflight_accepts_adequate_space_without_pruning(self):
        runner = host.Runner()
        with patch.object(host.shutil, "disk_usage", return_value=type("Usage", (), {"free": host.MIN_SEQUENCE_FREE})()), \
                patch.object(runner, "command") as command:
            self.assertEqual(host.MIN_SEQUENCE_FREE, runner.prepare_build_space())
        command.assert_not_called()

    def test_preflight_rejects_inadequate_space_without_pruning(self):
        runner = host.Runner()
        with patch.object(host.shutil, "disk_usage", return_value=type("Usage", (), {"free": host.MIN_SEQUENCE_FREE - 1})()), \
                patch.object(runner, "command") as command:
            with self.assertRaisesRegex(ValueError, "needs 16 GiB free"):
                runner.prepare_build_space()
        command.assert_not_called()


if __name__ == "__main__":
    unittest.main()
