import unittest
from unittest.mock import AsyncMock, Mock, patch

from app.deploy.providers.google_vm import GoogleVMProvider


class GoogleVMInstanceNameTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_custom_name_fails_before_cloud_authentication(self):
        provider = GoogleVMProvider()
        events = []
        async for event in provider.deploy(
            {"project_id": "example-project", "instance_name": "Not Valid!"},
            {},
        ):
            events.append(event)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["phase"], "done")
        self.assertFalse(events[0]["result"]["ok"])
        self.assertIn("Instance name", events[0]["result"]["message"])

    async def _drain(self, agen):
        """Consume _poll_app_health's async generator and return the state it
        reports in its terminal "verify-result" event."""
        state = None
        async for event in agen:
            if event.get("phase") == "verify-result":
                state = event.get("state")
        return state

    async def test_health_probe_uses_app_api_instead_of_caddy_root(self):
        provider = GoogleVMProvider()
        response = Mock(status_code=404, text="not found")
        response.json.return_value = {}
        client = AsyncMock()
        client.get.return_value = response
        context = AsyncMock()
        context.__aenter__.return_value = client

        with patch("app.deploy.providers.google_vm.httpx.AsyncClient", return_value=context), \
                patch("asyncio.sleep", new=AsyncMock()), \
                patch("app.deploy.providers.google_vm.time.time", side_effect=[0, 0, 2]):
            state = await self._drain(
                provider._poll_app_health("203.0.113.10", timeout_s=1))

        self.assertEqual(state, "installing")
        self.assertEqual(client.get.await_args.args[0], "http://203.0.113.10/api/v1/boot")

    async def test_new_instance_health_requires_scoped_p2p_capability(self):
        provider = GoogleVMProvider()
        response = Mock(status_code=200, text="{}")
        response.json.return_value = {
            "protocol_version": 1,
            "capabilities": {"scoped_bootstrap": True},
        }
        client = AsyncMock()
        client.get.return_value = response
        context = AsyncMock()
        context.__aenter__.return_value = client

        with patch("app.deploy.providers.google_vm.httpx.AsyncClient", return_value=context), \
                patch("asyncio.sleep", new=AsyncMock()), \
                patch("app.deploy.providers.google_vm.time.time", side_effect=[0, 0]):
            state = await self._drain(
                provider._poll_app_health(
                    "203.0.113.10", timeout_s=1, require_scoped_p2p=True))

        self.assertEqual(state, "running")
        self.assertEqual(
            client.get.await_args.args[0],
            "http://203.0.113.10/api/v1/p2p/status",
        )


if __name__ == "__main__":
    unittest.main()
