"""Deployment acceptance tests; run inside v3 container as uid 1100."""
import asyncio
import json
import os
import unittest
import uuid
from pathlib import Path

SOCKET = "/run/v3/worker/tools.sock"


class ContainerWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_identity_and_state_isolation(self):
        if os.getuid() != 1100:
            self.skipTest("run as application uid 1100 inside v3 container")
        app_pid = next(int(p.name) for p in Path("/proc").iterdir() if p.name.isdigit() and (p / "comm").read_text().strip() == "webagent-v3")
        reader, writer = await asyncio.open_unix_connection(SOCKET)
        request = {"id": "isolation-test", "workspace": str(uuid.uuid4()),
                   "name": "python", "arguments": {"code": (
                       "import os,json; "
                       "result={'uid':os.getuid(),'groups':os.getgroups()}; "
                       "\ntry: os.listdir('/state'); result['state_access']=True"
                       "\nexcept PermissionError: result['state_access']=False"
                       "\ntry: os.close(os.open('/run/v3/control/shutoff',os.O_WRONLY)); result['control_write']=True"
                       "\nexcept PermissionError: result['control_write']=False"
                       f"\ntry: open('/proc/{app_pid}/environ','rb'); result['app_environ_access']=True"
                       "\nexcept PermissionError: result['app_environ_access']=False"
                       "\nresult['secret_env']=any(k.startswith('V3_ADMIN') for k in os.environ)"
                       "\nprint(json.dumps(result))")},
                   "timeout_seconds": 10}
        writer.write((json.dumps(request) + "\n").encode())
        await writer.drain()
        response = json.loads(await reader.readline())
        writer.close()
        await writer.wait_closed()
        self.assertEqual(response["status"], "success", response)
        result = json.loads(response["output"])
        self.assertEqual(result["uid"], 1101)
        self.assertEqual(result["groups"], [])
        self.assertFalse(result["state_access"])
        self.assertFalse(result["control_write"])
        self.assertFalse(result["secret_env"])
        self.assertFalse(result["app_environ_access"])

    async def test_non_application_peer_rejected(self):
        if os.getuid() == 1100:
            self.skipTest("run separately as worker uid 1101")
        reader, writer = await asyncio.open_unix_connection(SOCKET)
        writer.write((json.dumps({"id":"forbidden","name":"shell",
            "workspace":str(uuid.uuid4()),"arguments":{"command":"printf bypass"}}) + "\n").encode())
        try:
            await writer.drain()
            response = await reader.readline()
            self.assertEqual(response, b"")
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionResetError, BrokenPipeError):
                pass


if __name__ == "__main__":
    unittest.main()
