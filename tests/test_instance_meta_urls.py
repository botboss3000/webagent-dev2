from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from app.db.instance_meta import track_endpoint_url


class InstanceMetaUrlTests(unittest.IsolatedAsyncioTestCase):
    async def test_tracking_merges_trailing_slash_variant(self) -> None:
        existing = {
            "metadata": {
                "urls": {
                    "https://agent.example.com/": {
                        "last_seen": "old",
                        "https_auto": False,
                        "hidden": True,
                    }
                }
            }
        }
        with (
            patch("app.db.instance_meta.get_instance", AsyncMock(return_value=existing)),
            patch("app.db.instance_meta.upsert_instance", AsyncMock(return_value=True)) as upsert,
        ):
            self.assertTrue(
                await track_endpoint_url("device-1", " https://agent.example.com/ ")
            )

        urls = upsert.await_args.kwargs["metadata"]["urls"]
        self.assertEqual(list(urls), ["https://agent.example.com"])
        self.assertTrue(urls["https://agent.example.com"]["hidden"])
        self.assertTrue(urls["https://agent.example.com"]["last_seen"])


if __name__ == "__main__":
    unittest.main()
