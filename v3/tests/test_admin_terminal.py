import asyncio
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

PATH = Path(__file__).resolve().parents[1] / "plugins/abilities/Administrator/terminal_control/adapter.py"
spec = importlib.util.spec_from_file_location("admin_terminal", PATH)
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)


class TerminalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.other = self.base / "other"
        self.other.mkdir()
        # Distinct workspace root, not nested under the permitted root.
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()

    async def asyncTearDown(self):
        await adapter.close_all(self.workspace)
        adapter.configure(None)
        self.temp.cleanup()

    async def call(self, name, **args):
        return await adapter.execute(name, args, self.workspace)

    async def read_until(self, ident, text):
        output = ""
        for _ in range(30):
            result = await self.call("terminal_wait", terminal_id=ident, timeout_seconds=1)
            result = await self.call("terminal_read", terminal_id=ident, cursor=0)
            output = result["output"]
            if text in output or not result["running"]:
                return result
        self.fail("terminal output did not arrive: " + output)

    async def test_interactive_roundtrip_and_fresh_observation(self):
        opened = await self.call("terminal_open", command="printf 'READY\\n'; read answer; printf 'RESULT:%s\\n' \"$answer\"")
        ident = opened["terminal_id"]
        with self.assertRaisesRegex(ValueError, "read before"):
            await self.call("terminal_send", terminal_id=ident, expected_sequence=0, text="unsafe")
        observed = await self.read_until(ident, "READY")
        await self.call("terminal_send", terminal_id=ident, expected_sequence=observed["sequence"], text="hello", keys=[])
        with self.assertRaisesRegex(ValueError, "read before"):
            await self.call("terminal_send", terminal_id=ident, expected_sequence=observed["sequence"], text="duplicate")
        observed = await self.read_until(ident, "hello")
        with self.assertRaisesRegex(ValueError, "exactly one"):
            await self.call("terminal_send", terminal_id=ident, expected_sequence=observed["sequence"], text="bad", keys=["enter"])
        await self.call("terminal_send", terminal_id=ident, expected_sequence=observed["sequence"], text="", keys=["enter"])
        result = await self.read_until(ident, "RESULT:hello")
        self.assertIn("RESULT:hello", result["output"])
        closed = await self.call("terminal_close", terminal_id=ident)
        self.assertTrue(closed["closed"])
        with self.assertRaisesRegex(ValueError, "never replays"):
            await self.call("terminal_read", terminal_id=ident)

    async def test_terminal_ownership_with_unconfined_dev_shell(self):
        secret = self.other / "private"
        secret.write_text("PRIVATE")
        opened = await self.call("terminal_open", command="cat " + str(secret) + "; printf 'DONE\\n'")
        result = await self.read_until(opened["terminal_id"], "DONE")
        self.assertIn("PRIVATE", result["output"])
        with self.assertRaisesRegex(ValueError, "missing in this workspace"):
            await adapter.execute("terminal_read", {"terminal_id": opened["terminal_id"]}, self.other)
        self.assertEqual([], (await adapter.execute("terminal_list", {}, self.other))["terminals"])

    async def test_bounded_output_cursor_and_limit(self):
        opened = await self.call("terminal_open", command="python3 -c 'print(\"x\"*300000)'; printf 'DONE\\n'")
        result = await self.read_until(opened["terminal_id"], "DONE")
        self.assertLessEqual(len(result["output"].encode()), adapter.LIMIT)
        result = await self.call("terminal_read", terminal_id=opened["terminal_id"], cursor=0)
        self.assertTrue(result["truncated"])
        with self.assertRaises(ValueError):
            await self.call("terminal_read", terminal_id=opened["terminal_id"], cursor=result["cursor"] + 1)

    async def test_double_fork_cleanup_and_restart_eof(self):
        # A detached child survives shell exit unless the supervisor subreaps it.
        command = "python3 -c 'import os,time; r,w=os.pipe(); p=os.fork(); os.close(w) if p else os.close(r); os.read(r,1) if p else None; os._exit(0) if p else None; os.setsid(); q=os.fork(); os._exit(0) if q else None; open(\"detached.pid\",\"w\").write(str(os.getpid())); os.write(w,b\"1\"); os.close(w); time.sleep(120)'"
        opened = await self.call("terminal_open", command=command)
        for _ in range(100):
            if (self.workspace / "detached.pid").exists():
                break
            await asyncio.sleep(.02)
        pid = int((self.workspace / "detached.pid").read_text())
        terminal = adapter._TERMINALS[opened["terminal_id"]]
        terminal.process.stdin.close()  # Worker restart closes the control pipe.
        await asyncio.wait_for(terminal.process.wait(), 5)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    async def test_terminal_cap_idle_cleanup_and_exit_status(self):
        opened = []
        for _ in range(adapter.MAX_TERMINALS):
            opened.append(await self.call("terminal_open", command="sleep 120"))
        with self.assertRaisesRegex(ValueError, "limit"):
            await self.call("terminal_open", command="true")
        adapter._TERMINALS[opened[0]["terminal_id"]].updated -= adapter.IDLE_SECONDS + 1
        listed = await self.call("terminal_list")
        self.assertEqual(adapter.MAX_TERMINALS - 1, len(listed["terminals"]))
        exited = await self.call("terminal_open", command="exit 7")
        result = await self.read_until(exited["terminal_id"], "never printed")
        for _ in range(100):
            if not result["running"]:
                break
            await asyncio.sleep(.01)
            result = await self.call("terminal_read", terminal_id=exited["terminal_id"])
        self.assertEqual(7, result["exit_code"])

    async def test_revocation_fences_pending_spawn(self):
        original = asyncio.create_subprocess_exec
        launched, release = asyncio.Event(), asyncio.Event()
        children = []
        async def delayed(*args, **kwargs):
            process = await original(*args, **kwargs)
            children.append(process)
            launched.set()
            await release.wait()
            return process
        with patch.object(adapter.asyncio, "create_subprocess_exec", delayed):
            opening = asyncio.create_task(self.call("terminal_open", command="sleep 120"))
            await launched.wait()
            revoked = asyncio.create_task(adapter.close_all(self.workspace))
            await asyncio.sleep(0)
            release.set()
            with self.assertRaisesRegex(ValueError, "authority changed"):
                await opening
            await asyncio.wait_for(revoked, 4)
        self.assertIsNotNone(children[0].returncode)
        self.assertEqual([], (await self.call("terminal_list"))["terminals"])

    async def test_cancelled_spawn_reaps_child(self):
        original = asyncio.create_subprocess_exec
        launched, release = asyncio.Event(), asyncio.Event()
        children = []
        async def delayed(*args, **kwargs):
            process = await original(*args, **kwargs)
            children.append(process)
            launched.set()
            await release.wait()
            return process
        with patch.object(adapter.asyncio, "create_subprocess_exec", delayed):
            opening = asyncio.create_task(self.call("terminal_open", command="sleep 120"))
            await launched.wait()
            opening.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(opening, 4)
        self.assertIsNotNone(children[0].returncode)
        self.assertEqual([], (await self.call("terminal_list"))["terminals"])

    async def test_nonreading_pty_close_is_bounded(self):
        opened = await self.call("terminal_open", command="stty -icanon -echo; printf READY; sleep 120")
        await self.read_until(opened["terminal_id"], "READY")
        terminal = adapter._TERMINALS[opened["terminal_id"]]
        terminal.process.stdin.write(b"x" * 1048576)
        await asyncio.wait_for(self.call("terminal_close", terminal_id=terminal.id), 4)
        self.assertIsNotNone(terminal.process.returncode)

    async def test_only_trusted_allocated_ports_enter_environment(self):
        ports = [24001, 24002, 24003, 24004]
        adapter.configure(lambda base: ports if base == self.workspace else [])
        opened = await self.call("terminal_open", command="printf '%s' \"$V3_ALLOWED_LISTEN_PORTS\"")
        self.assertEqual(ports, opened["preview_ports"])
        result = await self.read_until(opened["terminal_id"], "24001,24002,24003,24004")
        self.assertIn("24001,24002,24003,24004", result["output"])
        listed = await self.call("terminal_list")
        self.assertEqual(ports, listed["terminals"][0]["preview_ports"])
        with self.assertRaises(ValueError):
            await self.call("terminal_open", command="true", preview_ports=[8080])
        adapter.configure(lambda base: [8080, 24002, 24003, 24004])
        with self.assertRaisesRegex(ValueError, "trusted workspace port"):
            await self.call("terminal_open", command="true")

    async def test_argument_validation(self):
        for args in ({"command": ""}, {"command": "true", "cols": True}, {"command": "true", "rows": 1000}):
            with self.assertRaises(ValueError):
                await adapter.execute("terminal_open", args, self.workspace)
        with self.assertRaises(ValueError):
            await self.call("terminal_list", arbitrary="value")


if __name__ == "__main__":
    unittest.main()
