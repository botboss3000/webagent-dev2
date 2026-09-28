"""Delivery-channel enumeration for event-triggered runs.

A "delivery channel" is where the agent can reach the user when an event
fires — e.g. the original webchat session, a Telegram bot, an SMS number.
This module is the single source of truth for what's actually available
right now for a given (user, agent), and is consumed by:

  * `app.events.executor` — to build the `[DELIVERY CHANNELS]` overlay
    when an event-triggered agent run starts.
  * `app.tools.loader` — to expose `list_delivery_channels` as a chat
    tool so the agent stops hallucinating channels that aren't wired up.

A channel is "available" only when all three of these are true:
  1. The comms plugin is enabled at admin level
     (`get_channel_enabled_map`).
  2. The agent has a corresponding `agent_connections` row enabled.
  3. We know a recipient identifier for this user on that channel
     (otherwise the agent must ask).

`webchat` is special-cased and always returned first — it requires no
provider setup. If `current_session_id` is passed, it's stamped onto the
webchat entry so the chat agent can subscribe with that session as the
delivery target.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


async def list_available_channels(
    db,
    *,
    user_id: str,
    agent_id: str,
    current_session_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return the channels the agent can actually deliver on for this user."""
    out: List[Dict[str, Any]] = [{
        "channel": "webchat",
        "label": "Web chat",
        "recipient_hint": (
            f"this session (id={current_session_id})"
            if current_session_id else
            "ask the user which session, or default to a fresh one"
        ),
        "current_session_id": current_session_id,
        "available": True,
    }]
    try:
        from app.communications.manager import get_plugin_manager
        from app.admin.integrations import get_channel_enabled_map
        pm = get_plugin_manager()
        admin_map = await get_channel_enabled_map()
        agent_conns = await db.get_agent_connections(agent_id)
        agent_conn_map = {c["connection_type"]: c for c in agent_conns}

        for plugin in pm.get_all_plugins():
            if not plugin.enabled:
                continue
            if plugin.name in admin_map and not admin_map.get(plugin.name):
                continue
            ac = agent_conn_map.get(plugin.name)
            if not ac or not ac.get("enabled"):
                continue
            recipient = None
            try:
                cfg = ac.get("config")
                if isinstance(cfg, str):
                    cfg = json.loads(cfg or "{}")
                if isinstance(cfg, dict):
                    recipient = cfg.get("default_recipient") or cfg.get("chat_id") or cfg.get("phone")
            except Exception:
                recipient = None
            out.append({
                "channel": plugin.name,
                "label": plugin.name.capitalize(),
                "recipient_hint": recipient or "ask the user for their address",
                "recipient": recipient,  # raw recipient for engine-side delivery (None = ask)
                "available": True,
            })
    except Exception as e:
        logger.debug("Could not enumerate available channels: %s", e)
    return out
