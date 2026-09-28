"""Discover, hold, and expose registered ``EventSource`` plugins."""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import logging
from pathlib import Path
from typing import Dict, List, Optional

from app.events.base import EventSource

logger = logging.getLogger(__name__)

_SOURCES_DIR = Path(__file__).resolve().parent / "sources"


class EventSourceManager:
    """Singleton registry. Discovers source plugins under ``app/events/sources/``.

    DROP-IN — to add an event source, drop ONE file in app/events/sources/ that
    exposes ``source_cls`` (an ``EventSource`` subclass) and a ``FEATURE`` header
    (id/display_name/status). It is auto-discovered here; you register nothing.
    Delete the file to remove it. Do NOT wire sources into the core. See
    CLAUDE.md ("Core vs. plugins") and docs/claude/production-editions.md.
    """

    def __init__(self) -> None:
        self._sources: Dict[str, EventSource] = {}
        self._poller_task: Optional[asyncio.Task] = None
        self._renewer_task: Optional[asyncio.Task] = None
        self._discover()

    # ── Discovery ────────────────────────────────────────────────────────

    def _discover(self) -> None:
        _SOURCES_DIR.mkdir(parents=True, exist_ok=True)
        for fpath in sorted(_SOURCES_DIR.iterdir()):
            if fpath.suffix != ".py" or fpath.name.startswith("_"):
                continue
            mod_name = fpath.stem
            try:
                spec = importlib.util.spec_from_file_location(
                    f"app.events.sources.{mod_name}", fpath
                )
                if spec is None or spec.loader is None:
                    continue
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                cls = getattr(mod, "source_cls", None)
                if cls is None:
                    logger.debug("Event source %s has no source_cls, skipping", mod_name)
                    continue
                inst = cls()
                try:
                    inst._feature = getattr(mod, "FEATURE", None)
                except Exception:
                    inst._feature = None
                self._sources[inst.name] = inst
                logger.info("Discovered event source: %s (push=%s)", inst.name, inst.supports_push)
            except Exception as e:
                logger.exception("Failed to load event source %s: %s", mod_name, e)

    # ── Accessors ────────────────────────────────────────────────────────

    def get(self, name: str) -> Optional[EventSource]:
        return self._sources.get(name)

    def all(self) -> List[EventSource]:
        return list(self._sources.values())

    def enabled(self) -> List[EventSource]:
        try:
            from app.features.gating import feature_enabled
        except Exception:
            feature_enabled = None
        out = []
        for s in self._sources.values():
            if not s.enabled:
                continue
            if feature_enabled is not None and not feature_enabled(getattr(s, "_feature", None), s.name):
                continue
            out.append(s)
        return out

    def push_sources(self) -> List[EventSource]:
        return [s for s in self.enabled() if s.supports_push]

    def poll_sources(self) -> List[EventSource]:
        return [s for s in self.enabled() if s.supports_poll]

    def manifest(self) -> List[dict]:
        """List of parser-schema dicts for all enabled sources."""
        return [s.parser_schema() for s in self.enabled()]

    # ── Background loops ─────────────────────────────────────────────────

    async def start_poller(self) -> None:
        if self._poller_task and not self._poller_task.done():
            return
        from app.events.poller import EventPoller
        poller = EventPoller(self)
        self._poller_task = asyncio.create_task(poller.run(), name="event_poller")
        logger.info("Event poller started")

    async def stop_poller(self) -> None:
        if self._poller_task and not self._poller_task.done():
            self._poller_task.cancel()
            try:
                await self._poller_task
            except (asyncio.CancelledError, Exception):
                pass
        self._poller_task = None

    async def start_renewer(self) -> None:
        if self._renewer_task and not self._renewer_task.done():
            return
        from app.events.renewer import SubscriptionRenewer
        renewer = SubscriptionRenewer(self)
        self._renewer_task = asyncio.create_task(renewer.run(), name="event_renewer")
        logger.info("Event subscription renewer started")

    async def stop_renewer(self) -> None:
        if self._renewer_task and not self._renewer_task.done():
            self._renewer_task.cancel()
            try:
                await self._renewer_task
            except (asyncio.CancelledError, Exception):
                pass
        self._renewer_task = None
