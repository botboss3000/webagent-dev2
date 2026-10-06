"""Native coordinator checks used by the Python V2 administrator bridge."""
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

from v3.tests.test_admin_build import FakeRunner

PATH = Path(__file__).resolve().parents[1] / "plugins/abilities/Administrator/v3_build/host.py"
spec = importlib.util.spec_from_file_location("bridge_host", PATH)
host = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host)

PATCH = ("diff --git a/README.md b/README.md\n"
         "--- a/README.md\n+++ b/README.md\n"
         "@@ -1 +1 @@\n-base\n+updated\n")


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "checkout" / "v3"
        self.source.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(self.source)], check=True)
        (self.source / "README.md").write_text("base\n")
        (self.source / "OTHER.md").write_text("preexisting\n")
        self.runner = FakeRunner()
        self.runner.source = self.root / "checkout"
        self.broker = host.Broker(self.root / "state", self.runner)
        self.trusted = {"user_id": "python-v2-admin",
                        "session_id": host.BRIDGE_WORKSPACE,
                        "lease_until": time.time() + 3600,
                        "access_fingerprint": "f" * 64,
                        "bridge_caller": True}

    def tearDown(self):
        self.broker.shutdown()
        self.temp.cleanup()

    def call(self, name, arguments):
        return self.broker.handle({"name": name, "arguments": arguments,
                                   "workspace": host.BRIDGE_WORKSPACE,
                                   "trusted": self.trusted})

    def test_checked_apply_is_single_use_and_keeps_other_changes(self):
        self.call("v3_build_work", {"action": "begin", "task_id": "python-task", "summary": "Patch V3"})
        digest = host.context_digest(self.runner.snapshot())[0]
        arguments = {"task_id": "python-task", "request_id": "apply-once",
                     "patch": PATCH, "expected_inputs_sha256": digest}
        with self.assertRaisesRegex(ValueError, "stale"):
            self.call("v3_source_apply", {**arguments, "expected_inputs_sha256": "0" * 64})
        first = self.call("v3_source_apply", arguments)
        self.assertEqual("success", first["status"])
        self.assertEqual(["README.md"], first["paths"])
        self.assertEqual("updated\n", (self.source / "README.md").read_text())
        self.assertEqual("preexisting\n", (self.source / "OTHER.md").read_text())
        self.assertEqual(first, self.call("v3_source_apply", arguments))
        self.assertEqual(first, self.call("v3_source_receipt", {
            "task_id": "python-task", "request_id": "apply-once"}))
        with self.assertRaisesRegex(ValueError, "reused"):
            self.call("v3_source_apply", {**arguments, "patch": PATCH + "\n"})
        self.assertFalse(self.broker.jobs)

    def test_python_finish_batches_one_native_job_in_fixture(self):
        self.call("v3_build_work", {"action": "begin", "task_id": "first-task", "summary": "First"})
        self.call("v3_build_work", {"action": "begin", "task_id": "second-task", "summary": "Second"})
        digest = host.context_digest(self.runner.snapshot())[0]
        early = self.call("v3_build_work", {
            "action": "finish", "task_id": "first-task", "expected_inputs_sha256": digest})
        self.assertIsNone(early["job_id"])
        self.assertFalse(any(call[0] in ("build", "stop") for call in self.runner.calls))
        last = self.call("v3_build_work", {
            "action": "finish", "task_id": "second-task", "expected_inputs_sha256": digest})
        self.assertIsNotNone(last["job_id"])
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = self.call("v3_worker_job", {"job_id": last["job_id"]})
            if result["status"] in {"completed", "failed", "uncertain"}:
                break
            time.sleep(.01)
        self.assertEqual("completed", result["status"])
        self.assertEqual(last["job_id"], self.call("v3_build_work", {
            "action": "status"})["own"][0]["job_id"])
        self.assertEqual(2, len([call for call in self.runner.calls if call[0] == "build"]))

    def test_failed_combined_job_scrubs_bridge_receipt(self):
        self.runner.fail_create = True
        self.call("v3_build_work", {"action": "begin", "task_id": "failed-task", "summary": "Failure"})
        digest = host.context_digest(self.runner.snapshot())[0]
        queued = self.call("v3_build_work", {
            "action": "finish", "task_id": "failed-task", "expected_inputs_sha256": digest})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = self.call("v3_worker_job", {"job_id": queued["job_id"]})
            if result["status"] in {"completed", "failed", "uncertain"}:
                break
            time.sleep(.01)
        self.assertEqual("uncertain", result["status"])
        self.assertNotIn("NEVER-EXPOSE-THIS", str(result))
        self.assertIn("[redacted]", result["reason"])

    def test_unregistered_and_unsafe_paths_are_rejected(self):
        digest = host.context_digest(self.runner.snapshot())[0]
        args = {"task_id": "missing-task", "request_id": "apply-missing",
                "patch": PATCH, "expected_inputs_sha256": digest}
        with self.assertRaisesRegex(ValueError, "registered open"):
            self.call("v3_source_apply", args)
        self.call("v3_build_work", {"action": "begin", "task_id": "python-task", "summary": "Patch V3"})
        with self.assertRaisesRegex(ValueError, "mode"):
            self.call("v3_source_apply", {**args, "task_id": "python-task",
                "patch": "diff --git a/link b/link\nnew file mode 120000\n"})
        self.assertEqual("base\n", (self.source / "README.md").read_text())

    def test_bridge_identity_is_distinct_from_local_operator(self):
        self.call("v3_build_work", {"action": "begin", "task_id": "python-task", "summary": "Patch V3"})
        local = {"user_id": "codex-local", "session_id": host.LOCAL_WORKSPACE,
                 "lease_until": time.time() + 3600, "access_fingerprint": "f" * 64,
                 "local_caller": True}
        value = self.broker.handle({"name": "v3_build_work", "arguments": {"action": "status"},
                                    "workspace": host.LOCAL_WORKSPACE, "trusted": local})
        self.assertEqual([], value["own"])
        self.assertEqual("python-task", self.call("v3_build_work", {"action": "status"})["own"][0]["task_id"])


if __name__ == "__main__":
    unittest.main()
