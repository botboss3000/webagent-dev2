"""Authoritative entitlement checks for mutable, quota-bearing resources.

API routes and agent tools both call these helpers before touching storage so
an alternate materialization path cannot evade the same tier decision.
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from typing import Any, Optional

from app.entitlements.abilities import ability_group
from app.entitlements.service import resolve_capabilities


class ResourceEntitlementError(PermissionError):
    def __init__(self, message: str, *, code: str, resource: str = "",
                 limit: str = "", maximum: Optional[int] = None):
        super().__init__(message)
        self.code = code
        self.resource = resource
        self.limit = limit
        self.maximum = maximum

    def detail(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code}
        if self.resource:
            out["resource"] = self.resource
        if self.limit:
            out["limit"] = self.limit
        if self.maximum is not None:
            out["maximum"] = self.maximum
        return out


_agent_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
_connection_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


def _template_allowed(capabilities: dict, template_id: str) -> bool:
    allowed = set(capabilities.get("agent_templates") or [])
    return "*" in allowed or template_id in allowed


def _owned_agent_count(rows: list[dict], user_id: str) -> int:
    count = 0
    for row in rows:
        if row.get("source") != "custom" or row.get("status") in {"clone", "trashed"}:
            continue
        metadata = row.get("metadata") or {}
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata or "{}")
            except Exception:
                metadata = {}
        if isinstance(metadata, dict) and metadata.get("owner_user_id") == user_id:
            count += 1
    return count


async def enforce_agent_materialization(
    db, user_id: str, *, template_id: str = "default", restoring: bool = False,
) -> dict:
    """Validate agent feature, template, discoverability, and owned-agent cap."""
    capabilities = await resolve_capabilities(user_id, db=db, use_cache=False)
    features = capabilities.get("features") or {}
    if not features.get("agent_create"):
        subject = (capabilities.get("subject") or {}).get("class")
        raise ResourceEntitlementError(
            "Agent creation is not available for this account.",
            code="authentication_required" if subject == "anonymous" else "upgrade_required",
            resource="agent_create",
        )
    template_id = str(template_id or "default")
    if not _template_allowed(capabilities, template_id):
        raise ResourceEntitlementError(
            "This agent template is not available for this account.",
            code="upgrade_required", resource="agent_template",
        )
    if template_id not in {"", "none", "default"} and not (capabilities.get("subject") or {}).get("is_admin"):
        templates = await db.list_agent_templates(include_admin=False, discoverable_only=True)
        if not any(str(row.get("id") or "") == template_id for row in templates or []):
            raise ResourceEntitlementError(
                "This agent template is not discoverable.",
                code="upgrade_required", resource="agent_template",
            )
    maximum = (capabilities.get("limits") or {}).get("max_agents")
    if maximum is not None:
        rows = await db.list_agents_for_user(
            user_id,
            include_admin=bool((capabilities.get("subject") or {}).get("is_admin")),
            view="active",
        )
        if _owned_agent_count(list(rows or []), user_id) >= int(maximum):
            raise ResourceEntitlementError(
                "The account's agent limit has been reached.", code="quota_exceeded",
                limit="max_agents", maximum=int(maximum),
            )
    return capabilities


def agent_resource_lock(user_id: str) -> asyncio.Lock:
    """Serialize process-local count+create operations for one owner."""
    return _agent_locks[str(user_id)]


async def enforce_ability_group(db, user_id: str, ability_id: str, *, capabilities=None) -> dict:
    capabilities = capabilities or await resolve_capabilities(user_id, db=db, use_cache=False)
    # Fine-grained OAuth abilities use ``provider.scope`` IDs while product
    # entitlements classify the provider's drop-in ability descriptor.
    classified_id = str(ability_id or "").split(".", 1)[0]
    group = ability_group(classified_id)
    if group not in set(capabilities.get("ability_groups") or []):
        raise ResourceEntitlementError(
            "This ability is not available for this account.",
            code="upgrade_required", resource="ability_group",
        )
    return capabilities


async def _enabled_connection_count(db, user_id: str, *, capabilities: dict) -> int:
    rows = await db.list_agents_for_user(
        user_id,
        include_admin=bool((capabilities.get("subject") or {}).get("is_admin")),
        view="active",
    )
    count = 0
    for agent in rows or []:
        if agent.get("source") != "custom":
            continue
        for connection in await db.get_agent_connections(str(agent.get("id") or "")) or []:
            if connection.get("enabled"):
                count += 1
    return count


async def enforce_connection_change(
    db, user_id: str, agent_id: str, connection_type: str, *, enabling: bool,
) -> dict:
    """Enforce connector feature/group and quota only for a new enabled row."""
    capabilities = await resolve_capabilities(user_id, db=db, use_cache=False)
    if enabling:
        if not (capabilities.get("features") or {}).get("connectors"):
            raise ResourceEntitlementError(
                "Connections are not available for this account.",
                code="upgrade_required", resource="connectors",
            )
        await enforce_ability_group(
            db, user_id, connection_type, capabilities=capabilities,
        )
        existing = next(
            (row for row in await db.get_agent_connections(agent_id) or []
             if row.get("connection_type") == connection_type),
            None,
        )
        if not existing or not existing.get("enabled"):
            maximum = (capabilities.get("limits") or {}).get("max_connections")
            if maximum is not None:
                used = await _enabled_connection_count(db, user_id, capabilities=capabilities)
                if used >= int(maximum):
                    raise ResourceEntitlementError(
                        "The account's connection limit has been reached.",
                        code="quota_exceeded", limit="max_connections",
                        maximum=int(maximum),
                    )
    return capabilities


def connection_resource_lock(user_id: str) -> asyncio.Lock:
    return _connection_locks[str(user_id)]
