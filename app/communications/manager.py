"""
Plugin manager -- discovers, enables/disables communication plugins.

Reads registry.json for config. Provides tools to the agent loop.

DROP-IN — to add a channel, drop ONE file in app/communications/plugins/ that
exposes ``plugin_cls`` (a ``CommunicationPlugin`` subclass) and a ``FEATURE``
header (id/display_name/status). It is auto-discovered here; you register
nothing, and you do NOT edit the core. Delete the file to remove it. See
CLAUDE.md ("Core vs. plugins") and docs/claude/production-editions.md.
"""

import importlib
import json
import logging
from pathlib import Path
from typing import Optional

from app.communications.base import CommunicationPlugin

logger = logging.getLogger(__name__)

_PLUGINS_DIR = Path(__file__).resolve().parent / "plugins"
_REGISTRY_PATH = Path(__file__).resolve().parent / "registry.json"

DEFAULT_REGISTRY = {
    "webhook_base_url": "",
    "plugins": {
        # Off by default. A channel only goes live when the app admin enables it
        # in App Config → Channels (which writes the `channel_telegram` flag) AND
        # a bot token is present. Hard-enabling here meant a fresh install with a
        # stray token would start polling and log auth errors before anyone opted
        # in — the source of the "Telegram always errors" reports.
        "telegram": {"enabled": False},
    },
}


def _load_registry() -> dict:
    if _REGISTRY_PATH.exists():
        try:
            data = json.loads(_REGISTRY_PATH.read_text(encoding="utf-8"))
            # Merge with defaults so new fields appear
            merged = dict(DEFAULT_REGISTRY)
            merged.update(data)
            if "plugins" in data:
                merged["plugins"].update(data["plugins"])
            return merged
        except Exception as e:
            logger.warning("Failed to load registry.json: %s", e)
    return dict(DEFAULT_REGISTRY)


def _save_registry(registry: dict) -> None:
    _REGISTRY_PATH.write_text(json.dumps(registry, indent=2), encoding="utf-8")


class PluginManager:
    """Discovers plugins, provides aggregated tools, routes webhooks."""

    def __init__(self):
        self._registry = _load_registry()
        self._plugins: dict[str, CommunicationPlugin] = {}
        self._discover_plugins()

    def _discover_plugins(self) -> None:
        """Scan the plugins directory and instantiate each plugin module."""
        _PLUGINS_DIR.mkdir(parents=True, exist_ok=True)

        for fpath in sorted(_PLUGINS_DIR.iterdir()):
            if fpath.suffix != ".py" or fpath.name.startswith("_"):
                continue
            mod_name = fpath.stem
            try:
                spec = importlib.util.spec_from_file_location(
                    f"app.communications.plugins.{mod_name}", fpath
                )
                if spec is None or spec.loader is None:
                    continue
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)

                plugin_cls = getattr(mod, "plugin_cls", None)
                if plugin_cls is None:
                    logger.debug("Plugin %s has no plugin_cls, skipping", mod_name)
                    continue

                plugin = plugin_cls(registry=self._registry)
                # Stash the module's FEATURE header so edition gating can read it.
                try:
                    plugin._feature = getattr(mod, "FEATURE", None)
                except Exception:
                    plugin._feature = None
                self._plugins[plugin.name] = plugin
                logger.info("Discovered plugin: %s", plugin.name)
            except Exception as e:
                logger.error("Failed to load plugin %s: %s", mod_name, e)

    def get_plugin(self, name: str) -> Optional[CommunicationPlugin]:
        return self._plugins.get(name)

    def get_enabled_plugins(self) -> list[CommunicationPlugin]:
        """Return only enabled plugins the active edition includes."""
        try:
            from app.features.gating import feature_enabled
        except Exception:
            feature_enabled = None
        out = []
        for p in self._plugins.values():
            if not p.enabled:
                continue
            if feature_enabled is not None and not feature_enabled(getattr(p, "_feature", None), p.name):
                continue
            out.append(p)
        return out

    def get_all_plugins(self) -> list[CommunicationPlugin]:
        return list(self._plugins.values())

    def get_all_tools(self) -> list[dict]:
        """Aggregate tool definitions from all enabled plugins."""
        tools = []
        for plugin in self.get_enabled_plugins():
            tools.extend(plugin.get_tools())
        return tools

    def enable_plugin(self, name: str) -> bool:
        """Enable a plugin by name. Returns True if found."""
        if name not in self._plugins:
            return False
        self._registry.setdefault("plugins", {})
        self._registry["plugins"].setdefault(name, {})
        self._registry["plugins"][name]["enabled"] = True
        _save_registry(self._registry)
        return True

    def disable_plugin(self, name: str) -> bool:
        """Disable a plugin by name. Returns True if found."""
        if name not in self._plugins:
            return False
        self._registry.setdefault("plugins", {})
        self._registry["plugins"].setdefault(name, {})
        self._registry["plugins"][name]["enabled"] = False
        _save_registry(self._registry)
        return True

    def reload(self) -> None:
        """Re-discover plugins (after adding a new plugin file)."""
        self._registry = _load_registry()
        self._plugins = {}
        self._discover_plugins()

    async def start_polling_for_offline_plugins(self) -> None:
        """Start polling for all enabled plugins without a reachable webhook URL.
        Called on server startup. Also loads per-agent Telegram connections."""
        base_url = self._registry.get("webhook_base_url", "")
        _local_hints = ("localhost", "127.0.0.1", "0.0.0.0")
        is_offline = not base_url or any(h in base_url for h in _local_hints)
        if not is_offline:
            logger.info("Webhook base URL is set (%s), skipping auto-polling", base_url)
            return
        admin_enabled = await _admin_enabled_channels()
        for plugin in self.get_enabled_plugins():
            if not hasattr(plugin, "start_polling"):
                continue
            if plugin.name not in admin_enabled:
                logger.info("Skipping polling for %s — channel not enabled in App Config", plugin.name)
                continue
            await plugin.start_polling()
            logger.info("Auto-started polling for %s", plugin.name)
        # Also start polling for any per-agent Telegram tokens not in registry
        await self._start_agent_connection_polling()

    async def _start_agent_connection_polling(self) -> None:
        """Load per-agent Telegram connections from DB and start polling for unique tokens."""
        admin_enabled = await _admin_enabled_channels()
        if "telegram" not in admin_enabled:
            logger.info("Telegram channel not enabled in App Config — skipping per-agent pollers")
            return
        try:
            from app.db import get_db
            db = get_db()
            rows = await db.get_all_connections_by_type("telegram")
        except Exception as e:
            logger.warning("Could not load agent Telegram connections: %s", e)
            return

        # Collect unique tokens not already covered by the global registry token
        global_token = (
            self._registry.get("plugins", {}).get("telegram", {}).get("bot_token", "")
        )
        seen_tokens = {global_token} if global_token else set()

        for row in rows:
            try:
                cfg = json.loads(row.get("config") or "{}")
                token = cfg.get("bot_token", "").strip()
                if not token or token in seen_tokens:
                    continue
                seen_tokens.add(token)
                await self._start_extra_telegram_poller(token, row["agent_id"])
            except Exception as e:
                # A bad/expired per-agent token is a user config issue, not a
                # server fault — warn (don't ERROR into the diagnostics recorder).
                logger.warning("Skipping agent Telegram poller for agent %s: %s",
                               row.get("agent_id"), e)

    async def _start_extra_telegram_poller(self, token: str, agent_id: str) -> None:
        """Spin up an additional TelegramPlugin poller for a per-agent bot token."""
        try:
            from app.communications.plugins.telegram import TelegramPlugin
            key = f"telegram:{token[-6:]}"

            # Preserve the last acknowledged update_id so we don't re-deliver old
            # messages when the poller is restarted (e.g. after a connection save).
            last_update_id = 0
            existing = self._plugins.get(key)
            if existing and hasattr(existing, "_last_update_id"):
                last_update_id = existing._last_update_id
                logger.info("Preserving update offset %d for token ...%s",
                            last_update_id, token[-4:])

            if existing and hasattr(existing, "stop_polling"):
                await existing.stop_polling()
                logger.info("Stopped existing poller for token ...%s before reload", token[-4:])

            extra_registry = dict(self._registry)
            extra_registry["plugins"] = dict(self._registry.get("plugins", {}))
            extra_registry["plugins"]["telegram"] = {"enabled": True, "bot_token": token}
            plugin = TelegramPlugin(registry=extra_registry)
            plugin._agent_id = agent_id  # so the polling loop routes messages to this agent
            self._plugins[key] = plugin
            if hasattr(plugin, "start_polling"):
                await plugin.start_polling(initial_update_id=last_update_id)
                logger.info("Started extra Telegram poller for agent %s (token ...%s, offset=%d)",
                            agent_id, token[-4:], last_update_id)
        except Exception as e:
            # Invalid token / network blip — warn rather than ERROR so a single
            # misconfigured agent can't spam the flight recorder on every boot.
            logger.warning("Could not start extra Telegram poller: %s", e)

    async def reload_agent_connections(self) -> None:
        """Reload per-agent Telegram connections (called after UI saves a new token).

        Only starts polling when running in offline/local mode.  In webhook mode the
        inbound webhook endpoint already handles messages, so starting a concurrent
        polling loop would cause the same message to be processed twice.
        """
        base_url = self._registry.get("webhook_base_url", "")
        _local_hints = ("localhost", "127.0.0.1", "0.0.0.0")
        if base_url and not any(h in base_url for h in _local_hints):
            logger.info(
                "reload_agent_connections: webhook mode (registry url=%s) -- skipping polling",
                base_url,
            )
            return
        await self._start_agent_connection_polling()

    async def stop_all_polling(self) -> int:
        """Stop every communications plugin poller (base channels + per-agent
        variants). Used by the kill switch. Returns the number of pollers stopped.
        Pollers are NOT removed from the registry so a later start (server boot or
        kill-switch disengage) can restart them cleanly."""
        stopped = 0
        for key, plugin in list(self._plugins.items()):
            if plugin and hasattr(plugin, "stop_polling"):
                try:
                    await plugin.stop_polling()
                    stopped += 1
                    logger.info("Stopped poller for %s", key)
                except Exception as e:
                    logger.warning("Failed to stop poller for %s: %s", key, e)
        return stopped

    async def stop_channel_pollers(self, channel: str) -> int:
        """Stop the base plugin and every per-agent poller for a channel.

        Called when the admin disables a channel in App Config so we don't
        keep listening on Telegram (or other channel) after revocation.
        Returns the number of plugins stopped.
        """
        stopped = 0
        # Base plugin (e.g. "telegram") and any keyed variants ("telegram:abcd12")
        keys_to_stop = [name for name in list(self._plugins.keys())
                        if name == channel or name.startswith(f"{channel}:")]
        for key in keys_to_stop:
            plugin = self._plugins.get(key)
            if not plugin:
                continue
            if hasattr(plugin, "stop_polling"):
                try:
                    await plugin.stop_polling()
                    stopped += 1
                    logger.info("Stopped poller for %s", key)
                except Exception as e:
                    logger.warning("Failed to stop poller for %s: %s", key, e)
            # Remove per-agent variants entirely; keep base plugin so it can
            # be re-started cleanly if the channel is re-enabled.
            if key != channel:
                self._plugins.pop(key, None)
        return stopped


async def _admin_enabled_channels() -> set[str]:
    """Read which communication channels the admin has enabled in App Config.

    Returns an empty set on any error so a misconfigured DB doesn't accidentally
    enable channels that should be off.
    """
    try:
        from app.admin.integrations import get_channel_enabled_map
        m = await get_channel_enabled_map()
        return {name for name, on in m.items() if on}
    except Exception as e:
        logger.warning("Could not read admin channel enablement: %s", e)
        return set()


# -- Global singleton --
_plugin_manager: Optional[PluginManager] = None


def get_plugin_manager() -> PluginManager:
    global _plugin_manager
    if _plugin_manager is None:
        _plugin_manager = PluginManager()
    return _plugin_manager


def reload_plugins() -> None:
    get_plugin_manager().reload()
