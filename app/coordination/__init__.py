"""Cross-worker / cross-instance coordination primitives.

Currently: a single-leader election (``leader.py``) so the singleton background
loops (scheduler, event runtime, ability pollers, watchdog, remote access, boot
orphan-resume) run in exactly ONE worker even under ``gunicorn --workers N`` or
multiple instances — backed by a TTL'd lock row in the shared DB.
"""

from .leader import get_leader, BackgroundLeader

__all__ = ["get_leader", "BackgroundLeader"]
