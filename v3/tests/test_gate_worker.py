"""Gate protocol tests; no application state or credentials are required."""
import asyncio
import contextlib
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

SOURCE = Path(__file__).parents[1] / "gate_worker.py"
SPEC = importlib.util.spec_from_file_location("gate_worker", SOURCE)
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


def request(text_bytes=12):
    return {
        "protocol_version": 1, "request_id": "a" * 64,
        "module_id": "publication_size_guard_v1",
        "checkpoint": "publication", "run_id": "run",
        "generation": "generation", "policy_revision": "b" * 64,
        "input_digest": "c" * 64, "turn": 1, "call_index": 0,
        "facts": {"text_bytes": text_bytes},
    }


class GateTests(unittest.IsolatedAsyncioTestCase):
    async def test_fixed_limit_and_no_payload(self):
        self.assertEqual(gate.evaluate(request(8192))["decision"], "pass")
        result = gate.evaluate(request(8193))
        self.assertEqual(result["decision"], "reject")
        self.assertEqual(result["reason_code"], "publication_size_exceeded")
        self.assertNotIn("text", json.dumps(result))

    async def test_unknown_or_malformed_fields_rejected(self):
        for change in (
            {"protocol_version": 2},
            {"checkpoint": "pre_tool"},
            {"module_id": "other"},
            {"facts": {"text_bytes": True}},
            {"facts": {"text_bytes": 1, "text": "secret"}},
            {"request_id": "bad"},
        ):
            value = request()
            value.update(change)
            with self.assertRaises(ValueError):
                gate.evaluate(value)

    async def test_socket_response_and_peer_uid(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "gate.sock")
            server = await asyncio.start_unix_server(
                lambda r, w: gate.client(r, w, expected_uid=os.getuid()), path,
                limit=gate.MAX_FRAME + 1,
            )
            async with server:
                reader, writer = await asyncio.open_unix_connection(path)
                writer.write((json.dumps(request(8193)) + "\n").encode())
                await writer.drain()
                response = json.loads(await reader.readline())
                self.assertEqual(response["decision"], "reject")
                writer.close()
                with contextlib.suppress(ConnectionResetError):
                    await writer.wait_closed()
            server.close()
            await server.wait_closed()

    async def test_wrong_peer_receives_no_response(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "gate.sock")
            server = await asyncio.start_unix_server(
                lambda r, w: gate.client(r, w, expected_uid=2**31), path,
            )
            async with server:
                reader, writer = await asyncio.open_unix_connection(path)
                writer.write((json.dumps(request()) + "\n").encode())
                await writer.drain()
                try:
                    self.assertEqual(await asyncio.wait_for(reader.read(), 1), b"")
                except ConnectionResetError:
                    pass
                writer.close()
                with contextlib.suppress(ConnectionResetError):
                    await writer.wait_closed()
            server.close()
            await server.wait_closed()


if __name__ == "__main__":
    unittest.main()
