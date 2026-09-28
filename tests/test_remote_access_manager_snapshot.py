from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.remote_access import manager


class TunnelSnapshotTests(unittest.TestCase):
    def _patches(self, *, control_status):
        cfg = {
            "active_method": "cloudflare",
            "cloudflare": {"quick": True, "bin_path": ""},
        }
        return (
            patch("app.remote_access.manager.store.load_config", return_value=cfg),
            patch(
                "app.remote_access.manager.store.load_slave_link",
                return_value={"running": True, "url": "https://remembered.example"},
            ),
            patch("app.remote_access.manager.netinfo.get_port", return_value=8080),
            patch("app.remote_access.manager.tunnels.cloudflared_available", return_value=True),
            patch("app.remote_access.manager.get_manager", return_value=SimpleNamespace(_tunnel=None)),
            patch(
                "app.remote_access.slave.read_status",
                return_value={"state": "starting", "url": "", "pid": 123, "ts": 1},
            ),
            patch("app.remote_access.slave.probe_control", return_value=control_status),
        )

    def test_fresh_file_does_not_make_dead_controller_look_running(self) -> None:
        patches = self._patches(control_status=None)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
            snapshot = manager.tunnel_snapshot()

        self.assertFalse(snapshot["running"])
        self.assertEqual(snapshot["headful_url"], "")

    def test_live_controller_is_adopted_after_server_restart(self) -> None:
        live = {
            "kind": "webagent-tunnel-slave",
            "state": "running",
            "url": "https://live.trycloudflare.com",
            "provider": "cloudflare",
            "started_at": 123.0,
        }
        patches = self._patches(control_status=live)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
            snapshot = manager.tunnel_snapshot()

        self.assertTrue(snapshot["running"])
        self.assertEqual(snapshot["public_url"], live["url"])
        self.assertEqual(snapshot["headful_url"], live["url"])


if __name__ == "__main__":
    unittest.main()
