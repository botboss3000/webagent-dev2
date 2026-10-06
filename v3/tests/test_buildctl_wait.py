"""Read-only shared build result polling for the Codex client."""
import importlib.util
import unittest
from pathlib import Path

PATH = Path(__file__).resolve().parents[1] / "plugins/abilities/Administrator/v3_build/buildctl.py"
spec = importlib.util.spec_from_file_location("buildctl", PATH)
client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client)


class FakeBroker:
    def __init__(self, receipts, jobs=(), healthy=True):
        self.receipts = list(receipts)
        self.jobs = list(jobs)
        self.healthy = healthy
        self.calls = []

    def __call__(self, name, arguments):
        self.calls.append((name, arguments))
        if name == "v3_build_work":
            return {"own": [self.receipts.pop(0) if len(self.receipts) > 1 else self.receipts[0]]}
        if name == "v3_worker_job":
            value = self.jobs.pop(0) if len(self.jobs) > 1 else self.jobs[0]
            if isinstance(value, Exception):
                raise value
            return value
        if name == "v3_build_status":
            health = "healthy" if self.healthy else "unhealthy"
            return {"containers": {role: {"state": "running", "health": health}
                                   for role in ("api", "loop")}}
        raise AssertionError("polling attempted to create or deploy work")


def receipt(job_id=None):
    return {"task_id": "task-1", "status": "closed", "job_id": job_id}


def job(status="completed", phase="finished", reason="inputs unchanged", log=""):
    return {"job_id": "job-1", "status": status, "phase": phase,
            "reason": reason, "log": log}


class WaitTests(unittest.TestCase):
    def assert_read_only(self, broker):
        self.assertTrue(all(name in {"v3_build_work", "v3_worker_job", "v3_build_status"}
                            for name, _ in broker.calls))
        self.assertTrue(all(args == {"action": "status"} for name, args in broker.calls
                            if name == "v3_build_work"))

    def test_early_finisher_discovers_shared_job_on_next_turn(self):
        broker = FakeBroker([receipt(), receipt("job-1")], [job()])
        pending, code = client.wait_task("task-1", 1800, request=broker)
        self.assertEqual(3, code)
        self.assertEqual("waiting for other registered tasks", pending["reason"])
        self.assertEqual(["v3_build_work"], [name for name, _ in broker.calls])
        complete, code = client.wait_task("task-1", 0, request=broker)
        self.assertEqual(0, code)
        self.assertEqual(("job-1", "completed", "finished"),
                         (complete["job_id"], complete["status"], complete["phase"]))
        self.assertEqual("success", complete["result"])
        self.assert_read_only(broker)

    def test_last_finisher_waits_for_terminal_success(self):
        broker = FakeBroker([receipt("job-1")],
                            [job("queued", "queued"), job("running", "building"), job()])
        now = [0]
        delays = []

        def clock():
            return now[0]

        def pause(seconds):
            delays.append(seconds)
            now[0] += seconds

        result, code = client.wait_task("task-1", 30, request=broker, clock=clock, pause=pause)
        self.assertEqual(0, code)
        self.assertEqual([15, 15], delays)
        self.assertEqual("success", result["result"])
        self.assert_read_only(broker)

    def test_failed_job_is_terminal_and_excerpt_is_bounded(self):
        broker = FakeBroker([receipt("job-1")],
                            [job("failed", "building", "error", "x" * 2000)])
        result, code = client.wait_task("task-1", 0, request=broker)
        self.assertEqual(1, code)
        self.assertEqual(("failed", "building"), (result["status"], result["phase"]))
        self.assertLessEqual(len(result["log_excerpt"].encode()), 1024)
        self.assertNotIn("v3_build_status", [name for name, _ in broker.calls])
        self.assert_read_only(broker)

    def test_disconnect_then_resume_from_same_task(self):
        broker = FakeBroker([receipt("job-1")],
                            [ConnectionError("socket closed"), job()])
        pending, code = client.wait_task("task-1", 0, request=broker)
        self.assertEqual((3, "still_pending"), (code, pending["result"]))
        result, code = client.wait_task("task-1", 0, request=broker)
        self.assertEqual((0, "success"), (code, result["result"]))
        self.assert_read_only(broker)

    def test_timeout_and_duplicate_polling_do_not_schedule(self):
        broker = FakeBroker([receipt("job-1")], [job("running", "building")])
        for _ in range(2):
            result, code = client.wait_task("task-1", 0, request=broker)
            self.assertEqual((3, "still_pending"), (code, result["result"]))
            self.assertEqual("job-1", result["job_id"])
        self.assert_read_only(broker)

    def test_completed_job_requires_both_healthy(self):
        broker = FakeBroker([receipt("job-1")], [job()], healthy=False)
        result, code = client.wait_task("task-1", 0, request=broker)
        self.assertEqual((1, "failed"), (code, result["result"]))
        self.assert_read_only(broker)


if __name__ == "__main__":
    unittest.main()
