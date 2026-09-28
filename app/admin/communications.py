"""
Admin endpoints for managing communication plugins.

- GET /admin/communications/plugins -- list all plugins with status
- POST /admin/communications/plugins/{name}/enable -- enable a plugin
- POST /admin/communications/plugins/{name}/disable -- disable a plugin
- PUT /admin/communications/webhook-url -- set public webhook base URL
- POST /admin/communications/plugins/{name}/token -- save bot token (Telegram)
- POST /admin/communications/plugins/{name}/credentials -- save arbitrary credentials
- GET /admin/communications/plugins/{name}/health -- live health info
- POST /admin/communications/plugins/{name}/test -- test connection (getMe)
- POST /admin/communications/plugins/reload -- re-discover plugins
"""

import json
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.communications.manager import get_plugin_manager, reload_plugins
from app.util.config_io import safe_write_json

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/communications", tags=["admin"])

_LOCAL_HINTS = ("localhost", "127.0.0.1", "0.0.0.0")


class WebhookUrlRequest(BaseModel):
    url: str


class PluginStatus(BaseModel):
    name: str
    enabled: bool
    has_token: bool


class CredentialsRequest(BaseModel):
    credentials: Dict[str, Any]


@router.get("/plugins")
async def list_plugins():
    """List all discovered plugins with their enabled status."""
    pm = get_plugin_manager()
    plugins = []
    for p in pm.get_all_plugins():
        has_token = False
        if hasattr(p, '_bot_token'):
            has_token = bool(p._bot_token)
        elif hasattr(p, 'has_token'):
            has_token = p.has_token
        plugins.append({
            "name": p.name,
            "enabled": p.enabled,
            "has_token": has_token,
            "webhook_path": p.webhook_path,
            "tool_count": len(p.get_tools()),
        })
    registry = getattr(pm, "_registry", {})
    return {
        "plugins": plugins,
        "webhook_base_url": registry.get("webhook_base_url", ""),
    }


@router.post("/plugins/{name}/enable")
async def enable_plugin(name: str):
    """Enable a plugin. Uses webhook if base URL is set, otherwise polls."""
    pm = get_plugin_manager()
    ok = pm.enable_plugin(name)
    if not ok:
        return {"status": "error", "message": f"Plugin '{name}' not found"}

    plugin = pm.get_plugin(name)
    if plugin and hasattr(plugin, 'set_webhook_url'):
        registry = getattr(pm, "_registry", {})
        base_url = registry.get("webhook_base_url", "")
        if base_url and not any(h in base_url for h in _LOCAL_HINTS):
            await plugin.set_webhook_url(base_url)
            logger.info("%s enabled with webhook at %s", name, base_url)
        else:
            if hasattr(plugin, 'start_polling'):
                await plugin.start_polling()
                logger.info("%s enabled with polling (no public webhook URL)", name)

    return {"status": "ok", "message": f"Plugin '{name}' enabled"}


@router.post("/plugins/{name}/disable")
async def disable_plugin(name: str):
    """Disable a plugin. Stops polling and removes webhook."""
    pm = get_plugin_manager()

    plugin = pm.get_plugin(name)
    if plugin and hasattr(plugin, 'stop_polling'):
        await plugin.stop_polling()
    if plugin and hasattr(plugin, 'delete_webhook_url'):
        await plugin.delete_webhook_url()

    ok = pm.disable_plugin(name)
    if not ok:
        return {"status": "error", "message": f"Plugin '{name}' not found"}
    return {"status": "ok", "message": f"Plugin '{name}' disabled"}


@router.put("/webhook-url")
async def set_webhook_url(req: WebhookUrlRequest):
    """Set the public webhook base URL and re-register all webhooks."""
    pm = get_plugin_manager()
    registry = getattr(pm, "_registry", {})
    registry["webhook_base_url"] = req.url.rstrip("/")

    from pathlib import Path
    reg_path = Path(__file__).resolve().parent.parent / "communications" / "registry.json"
    safe_write_json(reg_path, registry)

    results = {}
    for plugin in pm.get_enabled_plugins():
        if hasattr(plugin, 'set_webhook_url'):
            ok = await plugin.set_webhook_url(req.url)
            results[plugin.name] = "ok" if ok else "failed"

    return {"status": "ok", "webhook_base_url": req.url, "results": results}


@router.post("/plugins/{name}/token")
async def set_plugin_token(name: str, req: WebhookUrlRequest, http_request: Request):
    """Save a bot token for a plugin. Auto-detects server URL, registers webhook
    or starts polling. Returns foreign_webhook_url if bot was connected elsewhere."""
    pm = get_plugin_manager()
    plugin = pm.get_plugin(name)
    if not plugin:
        return {"status": "error", "message": f"Plugin '{name}' not found"}

    token = req.url  # reuse WebhookUrlRequest for a single string field

    # Save to registry
    registry = getattr(pm, "_registry", {})
    registry.setdefault("plugins", {}).setdefault(name, {})
    registry["plugins"][name]["bot_token"] = token

    from pathlib import Path
    reg_path = Path(__file__).resolve().parent.parent / "communications" / "registry.json"

    # Auto-detect server URL from the incoming request if base URL not set
    server_url = registry.get("webhook_base_url", "")
    if not server_url:
        scheme = str(http_request.url.scheme)
        host = str(http_request.url.hostname)
        port = http_request.url.port
        if port and port not in (80, 443):
            server_url = f"{scheme}://{host}:{port}"
        else:
            server_url = f"{scheme}://{host}"
        # Respect X-Forwarded-Proto for TLS-terminating proxies (Cloud Run etc.)
        forwarded_proto = http_request.headers.get("x-forwarded-proto", "")
        if forwarded_proto and server_url.startswith("http://"):
            from app.admin.integrations import _is_trusted_proxy
            if _is_trusted_proxy(http_request):
                server_url = "https://" + server_url[len("http://"):]
        registry["webhook_base_url"] = server_url
        logger.info("Auto-detected server URL: %s", server_url)

    safe_write_json(reg_path, registry)

    # Re-init plugin so it picks up the new token
    plugin._bot_token = token

    # Auto-enable
    pm.enable_plugin(name)

    is_local = not server_url or any(h in server_url for h in _LOCAL_HINTS)
    foreign_webhook_url = None
    webhook_ok = False

    if is_local:
        # Delete any existing webhook to avoid 409 conflict, then start polling
        if hasattr(plugin, 'delete_webhook_url'):
            await plugin.delete_webhook_url()
        if hasattr(plugin, 'start_polling'):
            await plugin.start_polling()
            logger.info("Telegram polling started (server URL is localhost)")
    else:
        # Check if the bot is already registered to a different URL
        if hasattr(plugin, 'get_webhook_info'):
            info = await plugin.get_webhook_info()
            registered_url = info.get("url", "")
            expected_url = f"{server_url.rstrip('/')}/api/v1/webhooks/{name}"
            if registered_url and registered_url != expected_url:
                foreign_webhook_url = registered_url
                logger.info("Bot was at %s, taking over with %s", registered_url, expected_url)

        # Stop any running polling loop, then register webhook
        if hasattr(plugin, 'stop_polling'):
            await plugin.stop_polling()
        if hasattr(plugin, 'set_webhook_url'):
            webhook_ok = await plugin.set_webhook_url(server_url)
            logger.info("Webhook registration: %s", "ok" if webhook_ok else "failed")

    mode = "polling" if is_local else "webhook"
    return {
        "status": "ok",
        "message": f"Token saved for plugin '{name}', mode: {mode}",
        "has_token": True,
        "webhook_base_url": server_url,
        "webhook_registered": webhook_ok,
        "mode": mode,
        "foreign_webhook_url": foreign_webhook_url,
    }


@router.post("/plugins/{name}/credentials")
async def set_plugin_credentials(name: str, req: CredentialsRequest, http_request: Request):
    """Save arbitrary credentials for any plugin. Auto-enables and registers webhooks."""
    pm = get_plugin_manager()
    plugin = pm.get_plugin(name)
    if not plugin:
        return {"status": "error", "message": f"Plugin '{name}' not found"}

    registry = getattr(pm, "_registry", {})
    plugin_cfg = registry.setdefault("plugins", {}).setdefault(name, {})
    plugin_cfg.update(req.credentials)

    from pathlib import Path
    reg_path = Path(__file__).resolve().parent.parent / "communications" / "registry.json"

    server_url = registry.get("webhook_base_url", "")
    if not server_url:
        scheme = str(http_request.url.scheme)
        host = str(http_request.url.hostname)
        port = http_request.url.port
        if port and port not in (80, 443):
            server_url = f"{scheme}://{host}:{port}"
        else:
            server_url = f"{scheme}://{host}"
        forwarded_proto = http_request.headers.get("x-forwarded-proto", "")
        if forwarded_proto and server_url.startswith("http://"):
            from app.admin.integrations import _is_trusted_proxy
            if _is_trusted_proxy(http_request):
                server_url = "https://" + server_url[len("http://"):]
        registry["webhook_base_url"] = server_url

    safe_write_json(reg_path, registry)

    plugin._registry = registry
    pm.enable_plugin(name)

    is_local = not server_url or any(h in server_url for h in _LOCAL_HINTS)
    webhook_registered = False
    foreign_webhook_url = None
    mode = "webhook"

    if is_local:
        if hasattr(plugin, 'delete_webhook_url'):
            await plugin.delete_webhook_url()
        if hasattr(plugin, 'start_polling'):
            await plugin.start_polling()
        mode = "polling"
    else:
        if hasattr(plugin, 'get_webhook_info'):
            info = await plugin.get_webhook_info()
            registered_url = info.get("url", "")
            expected_url = f"{server_url.rstrip('/')}/api/v1/webhooks/{name}"
            if registered_url and registered_url != expected_url:
                foreign_webhook_url = registered_url

        if hasattr(plugin, 'stop_polling'):
            await plugin.stop_polling()
        if hasattr(plugin, 'set_webhook_url'):
            webhook_registered = await plugin.set_webhook_url(server_url)
        mode = "webhook"

    return {
        "status": "ok",
        "message": f"Credentials saved for '{name}', mode: {mode}",
        "plugin": name,
        "webhook_base_url": server_url,
        "webhook_registered": webhook_registered,
        "mode": mode,
        "foreign_webhook_url": foreign_webhook_url,
    }


@router.get("/plugins/{name}/health")
async def get_plugin_health(name: str):
    """Return live health info: mode, conflict status, last message, webhook registration."""
    pm = get_plugin_manager()
    plugin = pm.get_plugin(name)
    if not plugin:
        return {"status": "error", "message": f"Plugin '{name}' not found"}

    is_polling = getattr(plugin, 'is_polling', False)
    mode = "polling" if is_polling else ("webhook" if plugin.enabled else "disabled")

    result = {
        "name": name,
        "enabled": plugin.enabled,
        "mode": mode,
        "is_polling": is_polling,
        "conflict_detected": getattr(plugin, 'conflict_detected', False),
        "last_message_at": getattr(plugin, 'last_message_at', None),
        "webhook_info": None,
    }

    if hasattr(plugin, 'get_webhook_info') and plugin.enabled:
        try:
            result["webhook_info"] = await plugin.get_webhook_info()
        except Exception as e:
            logger.warning("Could not fetch webhook info for %s: %s", name, e)

    return result


@router.post("/plugins/{name}/test")
async def test_plugin_connection(name: str):
    """Test a plugin's credentials with a lightweight API call.
    For Telegram: calls getMe and returns the bot name and username."""
    pm = get_plugin_manager()
    plugin = pm.get_plugin(name)
    if not plugin:
        return {"status": "error", "message": f"Plugin '{name}' not found"}

    if not hasattr(plugin, 'get_me'):
        return {"status": "error", "message": f"Test not supported for plugin '{name}'"}

    try:
        result = await plugin.get_me()
        if result.get("ok"):
            bot = result.get("result", {})
            return {
                "status": "ok",
                "bot_name": bot.get("first_name", ""),
                "bot_username": bot.get("username", ""),
                "bot_id": bot.get("id"),
            }
        else:
            desc = result.get("description", "Token is invalid or bot was deleted")
            return {"status": "error", "message": desc}
    except Exception as e:
        return {"status": "error", "message": str(e)}


class TokenTestRequest(BaseModel):
    token: Optional[str] = None
    agent_id: Optional[str] = None
    connection_type: Optional[str] = None


@router.post("/plugins/{name}/test-token")
async def test_plugin_token(name: str, req: TokenTestRequest):
    """Test a specific bot token directly (for per-agent connections).
    Does not require the token to be saved; calls getMe and returns bot info.
    If token is masked (contains •) or absent, fetches real token from DB using agent_id."""
    token = req.token or ""
    if not token or "•" in token:
        if not req.agent_id:
            return {"status": "error", "message": "No token provided"}
        try:
            from app.db import get_db
            db = get_db()
            conn_type = req.connection_type or name
            rows = await db.get_agent_connections(req.agent_id)
            row = next((r for r in rows if r["connection_type"] == conn_type), None)
            if not row:
                return {"status": "error", "message": "No saved token found"}
            cfg = json.loads(row.get("config") or "{}")
            token = cfg.get("bot_token", "")
            if not token:
                return {"status": "error", "message": "No saved token found"}
        except Exception as e:
            return {"status": "error", "message": f"Could not retrieve token: {e}"}
    try:
        from app.communications.plugins.telegram import get_me
        result = await get_me(token)
        if result.get("ok"):
            bot = result.get("result", {})
            return {
                "status": "ok",
                "bot_name": bot.get("first_name", ""),
                "bot_username": bot.get("username", ""),
                "bot_id": bot.get("id"),
            }
        else:
            desc = result.get("description", "Token is invalid or bot was deleted")
            return {"status": "error", "message": desc}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.post("/plugins/reload")
async def reload_plugins_endpoint():
    """Re-discover plugins from the filesystem."""
    reload_plugins()
    pm = get_plugin_manager()
    return {
        "status": "ok",
        "plugins": [{"name": p.name, "enabled": p.enabled} for p in pm.get_all_plugins()],
    }
