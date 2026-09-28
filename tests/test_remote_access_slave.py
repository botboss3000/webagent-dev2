from __future__ import annotations

import asyncio
import os
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from app.remote_access.slave import (
    TunnelSlave,
    parse_public_url,
    read_status,
    status_path,
    write_status,
)


class TunnelSlaveTests(unittest.TestCase):
    def test_instances_tunnel_start_rejects_non_windows_device(self):
        from app.devices.actions import _start_slave_tunnel

        async def run():
            with patch("sys.platform", "linux"):
                await _start_slave_tunnel(job={}, db=None, payload={})

        with self.assertRaisesRegex(RuntimeError, "only on Windows"):
            asyncio.run(run())

    def test_parses_provider_urls_from_child_output(self) -> None:
        self.assertEqual(
            parse_public_url("cloudflare", "INF Visit https://quiet-river-12.trycloudflare.com now"),
            "https://quiet-river-12.trycloudflare.com",
        )
        self.assertEqual(
            parse_public_url("ngrok", 'url=https://example-123.ngrok-free.app'),
            "https://example-123.ngrok-free.app",
        )

    def test_fake_child_output_updates_the_slave_url(self) -> None:
        slave = TunnelSlave(port=54320, token="test", provider="cloudflare", quick=True)
        snapshots = []
        slave.persist = lambda **_kwargs: snapshots.append(slave.snapshot())  # type: ignore[method-assign]
        with patch("builtins.print"):
            slave._inspect_provider_log(
                "INF connecting\nINF https://calm-field.trycloudflare.com\n"
            )
        self.assertEqual(slave.url, "https://calm-field.trycloudflare.com")
        self.assertTrue(any(item["state"] == "running" for item in snapshots))

    def test_named_cloudflare_connection_uses_configured_url(self) -> None:
        slave = TunnelSlave(
            port=54320,
            token="test",
            provider="cloudflare",
            quick=False,
            name="named-tunnel",
            public_url="https://agent.example.com/",
        )
        slave.url = slave.configured_url
        snapshots = []
        slave.persist = lambda **_kwargs: snapshots.append(slave.snapshot())  # type: ignore[method-assign]
        with patch("builtins.print"):
            slave._inspect_provider_log(
                "INF Registered tunnel connection connIndex=0 connection=abc\n"
            )
        self.assertEqual(slave.url, "https://agent.example.com")
        self.assertTrue(any(item["state"] == "running" for item in snapshots))

    def test_cloudflare_inherits_visible_console_and_writes_parser_log(self) -> None:
        slave = TunnelSlave(
            port=54320, token="test", provider="cloudflare", quick=True,
            bin_path=r"C:\cloudflared.exe",
        )
        fake_proc = MagicMock()
        fake_proc.pid = 1234
        fake_proc.poll.return_value = None
        fake_job = MagicMock()
        with (
            patch("app.remote_access.slave.subprocess.Popen", return_value=fake_proc) as popen,
            patch("app.remote_access.slave._WindowsKillJob", return_value=fake_job),
            patch("app.remote_access.slave.threading.Thread") as thread,
            patch.object(slave, "persist"),
            patch("app.remote_access.slave.Path.unlink"),
            patch("builtins.print"),
        ):
            slave.start_child()
        kwargs = popen.call_args.kwargs
        self.assertNotIn("stdout", kwargs)
        self.assertNotIn("stderr", kwargs)
        argv = popen.call_args.args[0]
        self.assertIn("--logfile", argv)
        fake_job.assign.assert_called_once_with(fake_proc)
        thread.assert_called_once()

    def test_named_cloudflare_options_precede_tunnel_name(self) -> None:
        slave = TunnelSlave(
            port=54320, token="test", provider="cloudflare", quick=False,
            name="my-tunnel", bin_path=r"C:\cloudflared.exe",
        )
        argv = slave._argv()
        self.assertEqual(argv[-1], "my-tunnel")
        self.assertLess(argv.index("--logfile"), argv.index("my-tunnel"))

    def test_status_file_round_trip_and_freshness(self) -> None:
        port = 50000 + (os.getpid() % 10000)
        path = status_path(port)
        path.unlink(missing_ok=True)
        try:
            payload = {"state": "running", "url": "https://example.test", "pid": 42, "ts": time.time()}
            write_status(port, payload)
            self.assertEqual(read_status(port), payload)
            payload["ts"] = time.time() - 61
            write_status(port, payload)
            self.assertIsNone(read_status(port))
        finally:
            path.unlink(missing_ok=True)

    def test_control_server_is_live_before_provider_start(self) -> None:
        class FakeServer:
            def __init__(self, *_args, **_kwargs) -> None:
                self.started = threading.Event()
                self.closed = threading.Event()

            def serve_forever(self, **_kwargs) -> None:
                self.started.set()
                self.closed.wait(timeout=2.0)

            def shutdown(self) -> None:
                self.closed.set()

            def server_close(self) -> None:
                return

        slave = TunnelSlave(port=54320, token="test", provider="cloudflare", quick=True)

        def fake_start() -> None:
            self.assertTrue(slave._server.started.wait(timeout=1.0))  # type: ignore[union-attr]
            slave._quitting.set()

        with (
            patch("app.remote_access.slave.ThreadingHTTPServer", FakeServer),
            patch.object(slave, "start_child", side_effect=fake_start) as start_child,
            patch.object(slave, "stop_child"),
            patch.object(slave, "_refresh_loop"),
            patch("builtins.print"),
        ):
            slave.serve()

        start_child.assert_called_once()


if __name__ == "__main__":
    unittest.main()
