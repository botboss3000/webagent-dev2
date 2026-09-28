"""Cross-device dispatch — enqueue a job for another device, and nudge it awake.

``enqueue()`` writes a pending row into the shared ``device_jobs`` queue. The
targeted device's worker (app/devices/worker.py) claims and runs it with ITS OWN
local tools. ``nudge()`` is the "knock" so the target acts within seconds instead
of waiting for its next poll:

  • Target is THIS instance (or a broadcast job): kick the local worker now.
  • Target is another instance: the target's short poll picks the job up shortly
    (the responsiveness floor). A direct push to a known endpoint is a documented
    future upgrade — see ``nudge`` — but is never required for correctness.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from app.devices.identity import device_id

logger = logging.getLogger(__name__)


async def enqueue(*, owner_user_id: str, prompt: str, agent_id: Optional[str] = None,
                  target_instance: Optional[str] = None, target_label: Optional[str] = None,
                  payload: Optional[dict] = None, nudge_target: bool = True) -> str:
    """Queue a job to run on another device. Returns the job id.

    target_instance=None means "any device may take it" (a broadcast job)."""
    from app.db import get_db
    db = get_db()
    job_id = await db.enqueue_device_job(
        owner_user_id=owner_user_id,
        prompt=prompt,
        agent_id=agent_id,
        target_instance=target_instance,
        target_label=target_label,
        created_by_instance=device_id(),
        payload=payload,
    )
    if nudge_target:
        try:
            await nudge(target_instance)
        except Exception as e:
            logger.debug("nudge after enqueue failed (job still queued): %s", e)
    return job_id


async def list_devices(online_within_seconds: int = 60) -> List[Dict[str, Any]]:
    """Presence registry: all known devices, each tagged online/offline."""
    from app.db import get_db
    db = get_db()
    try:
        return await db.list_devices(online_within_seconds=online_within_seconds)
    except Exception as e:
        logger.debug("list_devices failed: %s", e)
        return []


async def remove_device(instance_id: str) -> bool:
    """Drop a device's presence row from the shared registry (admin unlink of a
    stale/offline device). This clears the RECORD only — it never reaches the
    machine, so a still-running device pointed at this database will re-appear on
    its next heartbeat. Returns True if the delete was issued without error."""
    from app.db import get_db
    db = get_db()
    try:
        await db.delete_device(instance_id)
        return True
    except Exception as e:
        logger.debug("remove_device failed: %s", e)
        return False


async def set_device_override(instance_id: str, *, label: Optional[str] = None,
                              icon: Optional[str] = None) -> bool:
    """Set the admin's custom display name / icon for a device in the shared
    registry (the Instances-page rename). Overrides the self-reported hostname +
    platform icon, lives in the shared DB (so every device shows the same), and
    is never overwritten by the heartbeat. Per field: None = leave unchanged,
    "" = clear the override, else set it. Returns True when a row was updated."""
    from app.db import get_db
    db = get_db()
    try:
        return bool(await db.set_device_override(instance_id, label=label, icon=icon))
    except Exception as e:
        logger.debug("set_device_override failed: %s", e)
        return False


async def resolve_target(target: str, *, online_within_seconds: int = 60
                         ) -> Optional[Dict[str, Any]]:
    """Look a stored target up in the presence registry. Matches by instance_id
    first (the canonical, unambiguous claim key the UI stores) and falls back to
    a label match. Returns the device dict (carrying an ``online`` flag) or None
    if no device with that id/label has ever been seen. A None result does NOT
    mean "can't run" — a 'wait' job for an unseen id simply sits queued until a
    device with that id heartbeats and claims it."""
    target = (target or "").strip()
    if not target:
        return None
    devices = await list_devices(online_within_seconds=online_within_seconds)
    for d in devices:
        if d.get("instance_id") == target:
            return d
    for d in devices:
        if (d.get("label") or "") == target:
            return d
    return None


async def nudge(target_instance: Optional[str]) -> None:
    """Best-effort wake of the target so it claims the job now, not on next poll.

    A missed nudge only ever costs poll latency, never correctness — the target's
    worker will claim the job on its next tick regardless. Today this wakes the
    LOCAL worker immediately (covers self-dispatch and same-box multi-instance);
    a remote device is reached via its short poll. A direct cross-network push
    (POST to the target's endpoint) is the next increment and slots in here."""
    from app.devices import worker as _worker
    if not target_instance or target_instance == device_id():
        _worker.kick()
        return
    # Remote target: rely on its fast poll. (Future: if we know its reachable
    # endpoint from the presence row, POST a wake to it here.)
    logger.debug("Remote device %s will claim on its next poll", target_instance)
