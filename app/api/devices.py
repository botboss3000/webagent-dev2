"""Device presence API — lists the WebAgent instances ("devices") that share
this database, for the target-device picker used by the chat pill, the
automations "Run on" field, and the sessions device badge.

Read-only. Actual cross-device dispatch happens elsewhere (the agent tools in
plugins/abilities/Devices/, the automation runner's firing seam, and the chat
send path) — this endpoint only answers "which devices exist and which are
online right now?". See app/devices/ for the engine.
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Query, Request

from app.auth.identity import assert_caller_is

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/devices", tags=["devices"])


def _parse_caps(caps):
    if isinstance(caps, str):
        try:
            return json.loads(caps or "{}")
        except Exception:
            return {}
    return caps or {}


@router.get("")
async def list_devices_endpoint(
    request: Request,
    user_id: str = Query(...),
    online_within_seconds: int = Query(60),
):
    """Every device sharing this DB, each tagged online/offline. The caller's own
    device is flagged ``is_self`` so the picker can label it "This device"."""
    await assert_caller_is(request, user_id)
    from app.devices import list_devices, identity

    me = identity.device_id()
    devices = await list_devices(online_within_seconds=online_within_seconds)
    out = []
    for d in devices:
        iid = d.get("instance_id")
        # The admin's Instances-page rename (custom_label, shared in the DB) wins over
        # the self-reported hostname, so every picker surface shows the chosen name.
        custom = (d.get("custom_label") or "").strip()
        out.append({
            "instance_id": iid,
            "label": custom or d.get("label") or iid,
            "custom_label": custom,
            "online": bool(d.get("online")),
            "last_seen": d.get("last_seen"),
            "is_self": iid == me,
            "capabilities": _parse_caps(d.get("capabilities")),
        })
    # Surface THIS device even before its first heartbeat lands, so the picker is
    # never empty on a fresh boot (the worker heartbeats a few seconds in). No
    # custom_label yet — a device must check in once before it can be renamed.
    if not any(x["is_self"] for x in out):
        out.append({
            "instance_id": me,
            "label": identity.device_label(),
            "custom_label": "",
            "online": True,
            "last_seen": None,
            "is_self": True,
            "capabilities": await identity.capabilities_async(),
        })
    # Order every picker surface consistently: THIS device first, then the rest
    # alphabetical by display name (case-insensitive).
    out.sort(key=lambda x: (0 if x["is_self"] else 1, (x.get("label") or "").lower()))
    return {"devices": out, "self": me}
