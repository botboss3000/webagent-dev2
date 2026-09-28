"""Multi-device coordination.

A SHARED database lets one user's several WebAgent instances ("devices") see one
another and hand work back and forth — "run this on the laptop", "let the phone
handle that". This package adds:

  • identity   — a stable, per-machine device id kept OUTSIDE the shared data tree
                 (so a shared DB doesn't make every device think it's the same box)
  • presence   — a registry (device_presence): who's online + what each can do
  • dispatch   — a queue (device_jobs): enqueue a job for a device; EXACTLY ONE
                 device claims and runs it (atomic claim, same idiom as the leader
                 lock in app/coordination/leader.py)
  • worker     — a per-instance loop that claims jobs addressed to THIS device and
                 runs them locally, so a job uses this machine's own tools

Unlike app/coordination/leader.py (which elects ONE worker to run global
singletons), the device worker runs on EVERY instance and is addressed by id.
The agent-facing surface is the drop-in "Multi-Device Control" ability
(plugins/abilities/Devices/run_on_device/).
"""
from __future__ import annotations

from app.devices.identity import capabilities, device_id, device_label
from app.devices.dispatch import enqueue, list_devices, nudge
from app.devices.worker import (
    get_worker,
    kick,
    start_device_worker,
    stop_device_worker,
)

__all__ = [
    "device_id", "device_label", "capabilities",
    "enqueue", "list_devices", "nudge",
    "start_device_worker", "stop_device_worker", "get_worker", "kick",
]
