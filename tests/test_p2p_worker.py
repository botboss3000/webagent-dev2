import json
import unittest
from unittest.mock import AsyncMock, patch

from app.p2p.worker import P2PWorker


class P2PWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_peer_store_is_offloaded_and_backed_off(self):
        worker = P2PWorker()
        with patch("app.p2p.worker.asyncio.to_thread", new=AsyncMock(return_value=[])) as offload:
            await worker._tick()

        offload.assert_awaited_once()
        self.assertGreater(worker._last_heartbeat, 0)
        self.assertEqual(worker._last_sync, worker._last_heartbeat)
        self.assertGreater(worker._next_wake(), 29)

    async def test_replica_push_uses_scoped_encrypted_transport(self):
        worker = P2PWorker()
        worker._transport.is_connected = AsyncMock(return_value=True)
        worker._transport.send = AsyncMock(return_value=json.dumps({
            "ok": True,
            "app_rows": 3,
            "secret_rows": 2,
            "config_files": 1,
        }).encode())
        peer = {
            "id": "peer-1",
            "name": "replica",
            "sync_options": {"app_db": True, "app_secrets": True, "app_configs": True},
            "sync": {},
        }
        payload = {"version": 1, "app_rows": {}, "vault_rows": [], "configs": {}}

        with patch("app.p2p.bootstrap.build_payload", new=AsyncMock(return_value=payload)), \
                patch("app.p2p.worker.peer_store.set_sync_state") as set_state:
            await worker._push_replica(peer)

        args = worker._transport.send.await_args.args
        self.assertEqual(args[:3], ("peer-1", "POST", "/api/v1/p2p/bootstrap/apply"))
        set_state.assert_called_once()
        self.assertEqual(set_state.call_args.kwargs["status"], "insync")
        self.assertEqual(set_state.call_args.kwargs["last_sync_files"], 6)
        self.assertTrue(set_state.call_args.kwargs["last_replica_hash"])


if __name__ == "__main__":
    unittest.main()
