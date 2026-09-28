"""Regression test: the ``restart_server`` agent tool must restart IN-PROCESS.

``POST /api/v1/restart`` is gated by ``require_platform_admin``, so the agent
path — which has no token to present from localhost — used to receive 401 on
every call and the restart silently never happened. The handler must delegate to
``app.relauncher.trigger_restart`` directly, exactly like the admin UI paths
(``app/admin/storage.py``), and must never fall back to an HTTP call.
"""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from plugins.admin.source_tools import inject_source_tools


def _restart_handler():
    tools: dict = {}
    inject_source_tools(tools, "admin")
    return tools["restart_server"].handler


class RestartToolInProcessTests(unittest.TestCase):
    def test_tool_restarts_through_the_relauncher_without_http(self):
        result = {
            "status": "restarting",
            "auto_restart": True,
            "mode": "multiprocess",
            "message": "Server is restarting; it will be back in a few seconds.",
        }
        no_http = AssertionError(
            "restart_server must not POST the admin-gated /api/v1/restart route"
        )
        with (
            patch("app.relauncher.trigger_restart", return_value=result) as relaunch,
            patch("httpx.AsyncClient", side_effect=no_http),
        ):
            output = asyncio.run(_restart_handler()())

        self.assertEqual(output, "Server restarting: " + result["message"])
        relaunch.assert_called_once_with()

    def test_tool_reports_unavailable_host_and_relauncher_errors(self):
        with patch(
            "app.relauncher.trigger_restart",
            return_value={
                "status": "unavailable",
                "auto_restart": False,
                "reason": "run.py not found",
            },
        ):
            self.assertEqual(
                asyncio.run(_restart_handler()()),
                "Server restart unavailable: run.py not found",
            )

        with patch("app.relauncher.trigger_restart", side_effect=OSError("no fork")):
            self.assertEqual(
                asyncio.run(_restart_handler()()),
                "Could not request server restart: no fork",
            )


if __name__ == "__main__":
    unittest.main()
