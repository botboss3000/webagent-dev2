"""Service-level entitlement checks for automation writers.

The automation UI, prompt-slot synchronizer, and Core ability all reach the
database through different routes.  Keeping the quota calculation here makes
those routes agree on what counts as an automation: active scheduled tasks and
active event subscriptions owned by the user.
"""

from __future__ import annotations

from typing import Any, Optional


class AutomationEntitlementError(PermissionError):
    """Raised before an automation write that the effective tier disallows."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


async def active_automation_rows(db, user_id: str) -> tuple[list[dict], list[dict]]:
    """Return all non-recycled task and subscription rows for ``user_id``."""
    tasks = await db.list_automations(owner_user_id=user_id)
    subscriptions = await db.list_event_subscriptions(owner_user_id=user_id)
    return list(tasks or []), list(subscriptions or [])


async def enforce_automation_entitlement(
    db,
    user_id: str,
    *,
    additional: int = 0,
    projected_count: Optional[int] = None,
) -> dict[str, Any]:
    """Require automation access and capacity for a prospective write.

    ``additional`` is useful for imperative creates. ``projected_count`` is
    used by slot reconciliation, where old slot-managed rows are replaced and
    a simple current-count-plus-one check would reject valid edits at the cap.
    """
    from app.entitlements.service import resolve_capabilities

    capabilities = await resolve_capabilities(user_id, db=db)
    if not bool((capabilities.get("features") or {}).get("automations")):
        raise AutomationEntitlementError(
            "Automations are not available for this user's experience tier.",
            code="automations_not_allowed",
        )

    limit = (capabilities.get("limits") or {}).get("max_automations")
    if limit is None:
        return capabilities

    try:
        max_automations = max(0, int(limit))
    except (TypeError, ValueError):
        max_automations = 0

    if projected_count is None:
        tasks, subscriptions = await active_automation_rows(db, user_id)
        prospective = len(tasks) + len(subscriptions) + max(0, int(additional))
    else:
        prospective = max(0, int(projected_count))

    if prospective > max_automations:
        raise AutomationEntitlementError(
            f"Automation limit reached: this tier allows {max_automations} "
            f"active automation{'s' if max_automations != 1 else ''}.",
            code="max_automations_reached",
        )
    return capabilities


async def enforce_slot_automation_projection(
    db,
    user_id: str,
    agent_id: str,
    *,
    task_hashes: set[str],
    event_hashes: set[str],
) -> None:
    """Validate the post-reconciliation count without mutating the database."""
    tasks, subscriptions = await active_automation_rows(db, user_id)
    replaced_tasks = sum(
        1 for row in tasks
        if row.get("agent_id") == agent_id and row.get("origin") == "slot"
    )
    replaced_subscriptions = sum(
        1 for row in subscriptions
        if row.get("agent_id") == agent_id and row.get("origin") == "slot"
    )
    projected = (
        len(tasks) + len(subscriptions)
        - replaced_tasks - replaced_subscriptions
        + len(task_hashes) + len(event_hashes)
    )
    await enforce_automation_entitlement(
        db,
        user_id,
        projected_count=projected,
    )
