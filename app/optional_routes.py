"""Cheap manifest for API routers that are not needed by the interactive core.

Keep this module import-side-effect free.  In particular, do not import any
router implementation here: :mod:`app.main` imports this manifest on the cold
path and resolves each implementation later from the post-ready startup queue.
"""

from __future__ import annotations

from dataclasses import dataclass
import importlib
from typing import Any


@dataclass(frozen=True)
class OptionalRoute:
    label: str
    module: str
    attribute: str = "router"
    router_prefix: str | None = None


# Order matches the former eager registration order in app.main.  Keeping the
# order stable avoids changing FastAPI's first-match behaviour for overlapping
# paths while still removing these implementations from the readiness path.
OPTIONAL_ROUTES: tuple[OptionalRoute, ...] = (
    OptionalRoute("terminal", "app.api.terminal"),
    OptionalRoute("browser_stream", "app.api.browser_stream"),
    OptionalRoute("connector_websocket", "app.api.connector_ws"),
    OptionalRoute("uploads", "app.api.uploads"),
    OptionalRoute("transcription", "app.api.transcription"),
    OptionalRoute("tenant_database", "app.api.tenant_db"),
    OptionalRoute("diagnostics", "app.api.diagnostics"),
    OptionalRoute("kill_switch", "app.api.kill_switch"),
    OptionalRoute("browser_log", "app.api.browser_log"),
    OptionalRoute("features", "app.api.features"),
    OptionalRoute("admin_review", "app.admin.review"),
    OptionalRoute("admin_database_mode", "app.admin.db_mode"),
    OptionalRoute("admin_storage", "app.admin.storage"),
    OptionalRoute("admin_source", "plugins.admin.source"),
    OptionalRoute("admin_settings", "app.admin.settings"),
    OptionalRoute("admin_communications", "app.admin.communications"),
    OptionalRoute("admin_webhooks", "app.admin.webhooks_admin"),
    OptionalRoute("admin_events", "app.admin.events_admin"),
    OptionalRoute("admin_scheduler", "app.admin.scheduler_config"),
    OptionalRoute("files", "app.api.files"),
    OptionalRoute("admin_users", "app.admin.users"),
    OptionalRoute("admin_entitlements", "app.admin.entitlements"),
    OptionalRoute("billing", "plugins.billing.api"),
    OptionalRoute("feedback", "app.api.feedback"),
    OptionalRoute("social_auth", "app.api.social_auth"),
    OptionalRoute("generic_webhooks", "app.api.webhooks_generic"),
    OptionalRoute("communication_webhooks", "app.api.webhooks"),
    OptionalRoute("events", "app.api.events"),
    OptionalRoute("remote_access", "app.api.remote_access"),
    OptionalRoute("admin_remote_access", "app.admin.remote_access"),
    OptionalRoute("deploy", "app.api.deploy"),
    OptionalRoute("dns", "app.api.dns"),
    OptionalRoute("admin_tunnel_link", "app.admin.tunnel_link"),
    OptionalRoute("admin_integrations", "app.admin.integrations"),
    OptionalRoute("oauth", "app.api.oauth"),
    OptionalRoute("github", "app.api.github"),
    OptionalRoute("claude_auth", "app.api.claude_auth"),
    OptionalRoute("codex_auth", "app.api.codex_auth"),
    OptionalRoute("openai_subscription_auth", "app.api.openai_subscription_auth"),
    OptionalRoute("claude_skills", "app.api.claude_skills"),
    OptionalRoute("engines", "plugins.engines.api"),
    OptionalRoute("admin_optimizer", "app.admin.optimizer", router_prefix="/api/v1"),
    OptionalRoute("admin_tasks", "app.admin.tasks"),
    OptionalRoute("genui", "app.api.genui"),
    OptionalRoute("devices", "app.api.devices"),
    OptionalRoute("ability_delete", "app.api.ability_delete"),
    OptionalRoute("browser_storage", "app.api.browser_storage"),
    OptionalRoute("storage_routing", "app.api.storage_routing"),
    OptionalRoute("p2p_mirror", "app.p2p.server"),
    OptionalRoute("admin_p2p", "app.api.admin_p2p"),
)


def load_optional_router(spec: OptionalRoute) -> Any:
    """Import one optional implementation and return its FastAPI router."""
    module = importlib.import_module(spec.module)
    router = getattr(module, spec.attribute)
    if spec.router_prefix is not None:
        router.prefix = spec.router_prefix
    return router


def load_billing_extension_routers() -> list[Any]:
    """Resolve optional billing extensions after the base billing API exists."""
    from plugins.billing.extensions import load_billing_extensions

    return [
        router
        for module in load_billing_extensions()
        if (router := getattr(module, "router", None)) is not None
    ]
