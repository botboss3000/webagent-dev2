"""Integration tool package — flat layout, grouped by capability.

Mirrors the `app/admin/` "self-contained, deletable" pattern: drop a tool's
file (or just remove its entry from TOOLS) and the agent loses that capability.

DROP-IN — add an integration by copying `app/integrations/_TEMPLATE.py` to a new
file here (TOOLS list + a FEATURE header). It is auto-discovered; do NOT wire it
into app/tools/loader.py or any core file. See CLAUDE.md ("Core vs. plugins")
and docs/claude/production-editions.md.

Layout
------
    app/integrations/
      __init__.py          — discovery + injection (this file)
      oauth_helper.py      — shared token store + refresh + generic oauth_api_call
      email_tools.py       — gmail_*; Outlook / Yahoo Mail go here later
      calendar_tools.py    — gcal_*; Outlook Calendar later
      (Cloud-file retrieval lives in the scoped Document Libraries ability.)
      (social_tools.py)    — Twitter / LinkedIn / Meta / Reddit / TikTok later

Module contract
---------------
Each tool-defining module exposes a top-level `TOOLS` list. Each entry:

    {
      "name":            unique tool name surfaced to the LLM,
      "provider":        OAuth provider key (must match agent_connections.connection_type)
                         e.g. "google", "microsoft", "dropbox", "twitter".
                         Tools without a provider are always-on (no gating).
      "handler":         async callable; called as handler(user_id=..., **tool_args).
      "parameters":      JSON Schema for the tool.
      "stages":          list of loop node IDs (usually ["execute_tools"]).
      "destructive":     bool — writes / sends / mutates remote state.
      "requires_confirmation":  bool — defaults to destructive when omitted.
    }

Registration is gated per tool: a tool registers only when its provider is
enabled in agent_connections for the active agent. The generic
`oauth_api_call` tool is always registered (works against any connected
provider).
"""

import importlib
import json
import logging
import pkgutil
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# Modules under app/integrations/ that should NOT be scanned for TOOLS.
# (Helpers + the package itself.)
_NON_TOOL_MODULES = {"oauth_helper"}


_DISCOVERED: Optional[List[dict]] = None


def _discover_tool_specs() -> List[dict]:
    """Walk `app.integrations.*` modules and return a flat list of tool dicts."""
    global _DISCOVERED
    if _DISCOVERED is not None:
        return _DISCOVERED

    specs: List[dict] = []
    for _finder, name, is_pkg in pkgutil.iter_modules(__path__):  # noqa: F821
        if is_pkg or name in _NON_TOOL_MODULES or name.startswith("_"):
            continue
        try:
            mod = importlib.import_module(f"app.integrations.{name}")
        except Exception as e:
            logger.warning("Failed to import integration module %s: %s", name, e)
            continue
        tools = getattr(mod, "TOOLS", None)
        if not isinstance(tools, list):
            continue
        for spec in tools:
            if not isinstance(spec, dict) or "name" not in spec or "handler" not in spec:
                continue
            specs.append({**spec, "_module": name})
    _DISCOVERED = specs
    return specs


def integration_tool_metadata() -> Dict[str, Dict]:
    """Metadata block consumed by BUILTIN_TOOL_METADATA."""
    out: Dict[str, Dict] = {
        "oauth_api_call": {
            "stages": ["execute_tools"],
            "destructive": True,
            "requires_confirmation": True,
            "agent_types": [],
        },
    }
    for spec in _discover_tool_specs():
        out[spec["name"]] = {
            "stages": spec.get("stages", ["execute_tools"]),
            "destructive": bool(spec.get("destructive", False)),
            "requires_confirmation": bool(spec.get("requires_confirmation", spec.get("destructive", False))),
            "agent_types": spec.get("agent_types", []),
        }
    return out


async def gather_enabled_providers(agent_id: str, user_id: Optional[str] = None) -> set:
    """Return connection_types that are both enabled on this agent AND configured.

    Intersecting with the configured set prevents stale agent_connections rows
    from leaking integration awareness to the LLM after admin OAuth creds (or
    per-user browser_session cookies) are removed. `user_id` is needed to
    decide whether `browser_session` is set up for the active user.
    """
    if not agent_id:
        return set()
    try:
        from app.db import get_db
        from app.admin.integrations import get_admin_configured_providers
        rows = await get_db().get_agent_connections(agent_id)
        enabled = {r["connection_type"] for r in rows if r.get("enabled")}
        return enabled & await get_admin_configured_providers(user_id)
    except Exception as e:
        logger.warning("Could not read agent_connections for %s: %s", agent_id, e)
        return set()


def _build_generic_oauth_tool(user_id: str, agent_id: str, allowed_providers: List[str]) -> "tuple[Callable, dict]":
    """Generic OAuth call tool — handler + JSON Schema.

    `allowed_providers` is the sorted list of OAuth providers enabled for the
    active agent. It is embedded in the schema so the LLM only ever sees the
    providers it is permitted to call. The (user_id, agent_id) pair is baked
    into the closure so this tool always uses per-agent OAuth tokens.
    """
    from app.integrations.oauth_helper import oauth_api_call, normalize_provider

    def _is_scoped_knowledge_endpoint(provider: str, url: str) -> bool:
        """Keep document APIs behind Document Libraries' approved scopes."""
        from urllib.parse import urlparse

        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        path = (parsed.path or "").lower()
        if provider == "google":
            return (
                (host == "www.googleapis.com" and "/drive/" in path)
                or host in {
                    "drive.googleapis.com", "driveactivity.googleapis.com",
                    "docs.googleapis.com", "sheets.googleapis.com", "slides.googleapis.com",
                }
            )
        if provider == "microsoft" and host == "graph.microsoft.com":
            return (
                "/drive" in path or path.startswith("/v1.0/sites/")
                or path.startswith("/beta/sites/") or path.endswith("/search/query")
            )
        if provider == "dropbox":
            return host in {"api.dropboxapi.com", "content.dropboxapi.com"}
        return False

    async def _handler(
        provider: str,
        method: str = "GET",
        url: str = "",
        params: Optional[dict] = None,
        json_body: Optional[dict] = None,
        headers: Optional[dict] = None,
    ) -> str:
        if not url:
            return json.dumps({"status": "error", "message": "url is required"})
        normalized = normalize_provider(provider)
        if normalized not in allowed_providers:
            return json.dumps({
                "status": "not_enabled",
                "message": f"Provider '{provider}' is not enabled for this agent.",
            })
        if _is_scoped_knowledge_endpoint(normalized, url):
            return json.dumps({
                "status": "blocked",
                "message": (
                    "Document-provider endpoints must be accessed through the "
                    "Document Libraries knowledge ability so the configured "
                    "folder, site, drive, and path scope is enforced."
                ),
            })
        result = await oauth_api_call(
            user_id, agent_id, normalized, method or "GET", url,
            params=params, json_body=json_body, headers=headers,
        )
        return json.dumps(result)

    schema = {
        "type": "object",
        "properties": {
            "provider":  {"type": "string", "enum": allowed_providers,
                          "description": "Connected OAuth provider enabled for this agent."},
            "method":    {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"], "default": "GET"},
            "url":       {"type": "string", "description": "Full HTTPS provider API URL. Drive, OneDrive, SharePoint, and Dropbox file endpoints are restricted to Document Libraries."},
            "params":    {"type": "object", "description": "Optional query-string parameters."},
            "json_body": {"type": "object", "description": "Optional JSON request body."},
            "headers":   {"type": "object", "description": "Optional extra request headers (Authorization is injected for you)."},
        },
        "required": ["provider", "url"],
    }
    return _handler, schema


def inject_integration_tools(
    tools: dict,
    user_id: str,
    agent_id: str,
    enabled_providers: Optional[set] = None,
    tool_info_cls=None,
) -> None:
    """Register integration tools into the loader's tools dict.

    - tool_info_cls: caller passes app.tools.loader.ToolInfo to avoid circular imports.
    - enabled_providers: pre-fetched set of agent_connections.connection_type
      values that are enabled. Caller should await gather_enabled_providers()
      and pass the result so we don't spin up a fresh event loop here.
    """
    if tool_info_cls is None:
        from app.tools.loader import ToolInfo as tool_info_cls

    if enabled_providers is None:
        enabled_providers = set()  # Without async context we cannot fetch; skip gating.

    # ── Generic OAuth call — only registered if at least one OAuth provider
    # is enabled. Its provider enum is restricted to those providers so the
    # LLM cannot discover capabilities it isn't supposed to have.
    # Non-OAuth providers (telegram, scraper, browser_session) don't apply here.
    _NON_OAUTH_PROVIDERS = {
        "telegram", "scraper", "browser_session",
        # Direct credential-backed Commerce APIs. Their dedicated adapters read
        # agent-scoped credentials from the vault; oauth_api_call cannot.
        "woocommerce", "bigcommerce", "square", "stripe", "paypal", "btcpay",
    }
    _OAUTH_ENABLED = sorted(p for p in enabled_providers if p not in _NON_OAUTH_PROVIDERS)
    if _OAUTH_ENABLED:
        handler, schema = _build_generic_oauth_tool(user_id, agent_id, _OAUTH_ENABLED)
        tools["oauth_api_call"] = tool_info_cls(
            name="oauth_api_call",
            handler=handler,
            parameters=schema,
            requires_confirmation=True,
        )

    # Edition gate: in a non-`full` build, skip integration modules whose
    # maturity the active edition excludes (no-op for `full`).
    try:
        from app.features.gating import module_enabled as _module_enabled
    except Exception:
        _module_enabled = None
    _gate_cache: dict = {}

    for spec in _discover_tool_specs():
        provider = spec.get("provider", "")
        # Tools that declare no provider are always-on (none today, but reserved).
        if provider and provider not in enabled_providers:
            continue

        if _module_enabled is not None:
            _mod = spec.get("_module", "")
            if _mod not in _gate_cache:
                _gate_cache[_mod] = _module_enabled(f"app.integrations.{_mod}", default_id=_mod)
            if not _gate_cache[_mod]:
                continue

        name = spec["name"]
        base_handler = spec["handler"]

        def _wrap(_h):
            async def _bound(**kwargs):
                return await _h(user_id=user_id, agent_id=agent_id, **kwargs)
            return _bound

        tools[name] = tool_info_cls(
            name=name,
            handler=_wrap(base_handler),
            parameters=spec.get("parameters", {"type": "object", "properties": {}, "required": []}),
            requires_confirmation=bool(spec.get("requires_confirmation", spec.get("destructive", False))),
        )
