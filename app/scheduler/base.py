"""Abstract scheduler interface.

Implementations either run jobs in-process (``LocalScheduler``) or push them
to an external service (the ``google_cloud`` provider, ``GoogleCloudScheduler``).
Callers go through ``app.scheduler.get_scheduler()``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional


class SchedulerBackend(ABC):
    name: str = "abstract"

    @abstractmethod
    async def start(self) -> None:
        """Begin firing scheduled tasks. Called once on app startup."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop firing tasks. Called on app shutdown."""

    @abstractmethod
    async def sync_tasks(self, agent_id: Optional[str] = None) -> None:
        """Reload tasks for the given agent (or all agents if None)."""

    @abstractmethod
    async def run_now(self, automation_id: str) -> dict:
        """Fire a specific automation row immediately. Returns status dict."""

    async def get_status(self) -> dict:
        """Lightweight health info. Backends may override."""
        return {"provider": self.name, "running": False}
