"""
Webhook router for inbound messages from communication channels.

Delegates to plugins for parsing, then routes to auth or agent loop.
"""

import json
import logging

from fastapi import APIRouter, Request, Response

from app.communications.manager import get_plugin_manager
from app.communications.processor import process_channel_message

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/webhooks", tags=["webhooks"])


@router.post("/{plugin_name}")
async def webhook_handler(plugin_name: str, request: Request):
    """
    Generic webhook handler. Routes to the right plugin,
    verifies request, then delegates to the shared message processor.
    """
    pm = get_plugin_manager()
    plugin = pm.get_plugin(plugin_name)

    if not plugin:
        logger.warning("Webhook for unknown plugin: %s", plugin_name)
        return Response(content='{"error":"unknown plugin"}', status_code=404)

    if not plugin.enabled:
        logger.warning("Webhook for disabled plugin: %s", plugin_name)
        return Response(content='{"error":"plugin disabled"}', status_code=503)

    # Channel must also be enabled at the admin level (App Config → Agent Abilities).
    try:
        from app.admin.integrations import get_channel_enabled_map
        channel_map = await get_channel_enabled_map()
    except Exception:
        channel_map = {}
    if plugin_name in channel_map and not channel_map.get(plugin_name, False):
        logger.warning("Webhook for admin-disabled channel: %s", plugin_name)
        return Response(content='{"error":"channel disabled by admin"}', status_code=503)

    # 1. Verify request authenticity
    if not await plugin.verify_request(request):
        return Response(content='{"error":"invalid request"}', status_code=403)

    # 2. Extract identity
    external_id = plugin.extract_external_id(request)
    if not external_id:
        return Response(content='{"ok":true}', status_code=200)  # non-message update

    message_text = plugin.extract_text(request)
    if not message_text:
        return Response(content='{"ok":true}', status_code=200)  # empty message

    # 3. Delegate to shared processor (handles auth, session, agent loop, reply)
    await process_channel_message(plugin.name, external_id, message_text, plugin)

    return Response(content='{"ok":true}', status_code=200)
