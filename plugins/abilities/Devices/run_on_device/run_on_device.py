"""Multi-Device Control ability — hand a task to ANOTHER of the user's devices.

Drop-in (see plugins/abilities/_TEMPLATE.py): exposes two tools —
  • list_my_devices — discover the other WebAgent instances sharing this database
  • run_on_device   — enqueue a job for one of them; that device's worker
                      (app/devices/worker.py) claims and runs it with ITS OWN
                      local tools / browser / files.

Requires a SHARED database across the devices. On a single, unshared instance
there are simply no other devices to target (list_my_devices shows only itself).
"""
from __future__ import annotations

TOOL_SCHEMAS: dict = {}
DESTRUCTIVE: set = set()


def build_tools(*, user_id: str = "", session_id: str = "", agent_id: str = "",
                agent_template_id: str = "", enabled_providers=None, **_ctx):
    async def list_my_devices():
        """List the user's WebAgent devices (instances sharing this database):
        each one's id, friendly label, whether it's online now, and what it can do."""
        from app.devices import list_devices
        devices = await list_devices(online_within_seconds=60)
        return {"devices": [
            {"instance_id": d.get("instance_id"),
             "label": d.get("label"),
             "online": d.get("online"),
             "capabilities": d.get("capabilities"),
             "last_seen": d.get("last_seen")}
            for d in devices
        ]}

    async def run_on_device(prompt: str, target: str = "", run_agent_id: str = ""):
        """Hand a task to another device to run with its own local tools.

        prompt: what the other device's agent should do.
        target: the device's instance_id OR its friendly label; empty or "any"
                lets any available device take it.
        run_agent_id: which agent runs it on the target (defaults to this agent).
        """
        from app.devices import enqueue, list_devices
        tgt = (target or "").strip()
        target_instance = None
        target_label = None
        if tgt and tgt.lower() != "any":
            devices = await list_devices(online_within_seconds=3600)
            match = next(
                (d for d in devices
                 if d.get("instance_id") == tgt
                 or (d.get("label") or "").lower() == tgt.lower()),
                None,
            )
            if not match:
                return {"ok": False,
                        "error": f"No device matches {tgt!r}. Call list_my_devices first."}
            target_instance = match.get("instance_id")
            target_label = match.get("label")
        job_id = await enqueue(
            owner_user_id=user_id,
            prompt=prompt,
            agent_id=(run_agent_id or agent_id or None),
            target_instance=target_instance,
            target_label=target_label,
        )
        return {"ok": True, "job_id": job_id,
                "target": target_instance or "any",
                "note": "Queued. The target device will claim and run it shortly."}

    TOOL_SCHEMAS.clear()
    TOOL_SCHEMAS.update({
        "list_my_devices": {"type": "object", "properties": {}, "required": []},
        "run_on_device": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string",
                           "description": "What the other device's agent should do."},
                "target": {"type": "string",
                           "description": "Target device instance_id or label; empty or 'any' = any available device."},
                "run_agent_id": {"type": "string",
                                 "description": "Agent to run on the target device (defaults to this agent)."},
            },
            "required": ["prompt"],
        },
    })
    DESTRUCTIVE.clear()
    DESTRUCTIVE.add("run_on_device")
    return {"list_my_devices": list_my_devices, "run_on_device": run_on_device}
