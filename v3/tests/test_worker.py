import asyncio
import importlib.util
import fcntl
import threading
import os
import tempfile
import unittest
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

    async def test_files_and_escape(self):
        await self.run_tool("file_write", {"path": "a/file.txt", "content": "hello"})
        self.assertEqual((await self.run_tool("file_read", {"path": "a/file.txt"}))["output"], "hello")
        self.assertIn("a/file.txt:1:hello", (await self.run_tool("file_search", {"query": "hello"}))["output"])
        with self.assertRaises(ValueError):
            await self.run_tool("file_read", {"path": "../../etc/passwd"})
        (worker.ROOT / self.session / "escape").symlink_to("/etc")
        with self.assertRaises((ValueError, OSError)):
            await self.run_tool("file_read", {"path": "escape/passwd"})

    async def test_python_shell_and_environment(self):
        os.environ["V3_TEST_SECRET"] = "private"
        result = await self.run_tool("python", {"code": "import os; print(os.getenv('V3_TEST_SECRET', 'absent'))"})
        self.assertEqual(result["output"].strip(), "absent")
        result = await self.run_tool("shell", {"command": "printf working"})
        self.assertEqual(result["output"], "working")

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

    async def test_output_is_bounded(self):
        result = await self.run_tool("python", {"code": "print('x' * 2000000)"})
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(result["output"]), worker.LIMIT)


if __name__ == "__main__":
    unittest.main()
