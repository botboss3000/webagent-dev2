import asyncio

from app.tools import browser_connector


def test_connector_reports_device_metadata_and_accepts_remote_lifecycle():
    sent = []

    async def send_json(message):
        sent.append(message)

    async def scenario():
        conn = browser_connector.ConnectorConn("connector-lifecycle-user", send_json)
        browser_connector.register(conn)
        try:
            browser_connector.handle_incoming(
                conn,
                {
                    "type": "hello",
                    "version": "0.2.0",
                    "installation_id": "install-1",
                    "browser": "Chromium",
                    "browser_version": "140.0",
                    "platform": "win",
                    "mobile": False,
                    "capabilities": {"screenshot": True},
                    "settings": {"paused": False, "auto_connect": True},
                },
            )
            info = browser_connector.connector_info(conn.user_id)
            assert info["connected"] is True
            assert info["installation_id"] == "install-1"
            assert info["browser_version"] == "140.0"
            assert info["settings"]["auto_connect"] is True

            updated = await browser_connector.control_connectors(
                conn.user_id, "settings", {"paused": True}
            )
            assert updated == 1
            assert sent[-1] == {
                "type": "control",
                "action": "settings",
                "settings": {"paused": True},
            }

            stopped = await browser_connector.stop_connectors(conn.user_id)
            assert stopped == 1
            assert sent[-1] == {"type": "control", "action": "shutdown"}
            assert browser_connector.connector_info(conn.user_id) == {"connected": False}
        finally:
            browser_connector.unregister(conn)

    asyncio.run(scenario())
