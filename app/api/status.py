"""Lightweight service status endpoint — GET /api/v1/status.

A cheap, DB-free, unauthenticated status probe (mounted publicly alongside the
/health probes — see PUBLIC_PATHS in app/auth/middleware.py) returning process
uptime and the app version. Intentionally contains no auth or DB logic; the
deeper /health/ready readiness check lives in app/main.py.
"""

from __future__ import annotations

import importlib.metadata
import os

from fastapi import APIRouter

from app.metrics import uptime_seconds

router = APIRouter(prefix="/api/v1", tags=["status"])

_VERSION_CACHE: dict[str, str] = {}


def _resolve_version() -> str:
    """Resolve the app version once per process: WEBAGENT_VERSION env var first,
    then installed distribution metadata, then "unknown"."""
    cached = _VERSION_CACHE.get("v")
    if cached:
        return cached
    version = os.environ.get("WEBAGENT_VERSION") or ""
    if not version:
        try:
            version = importlib.metadata.version("webagent")
        except Exception:
            version = ""
    if not version:
        version = "unknown"
    _VERSION_CACHE["v"] = version
    return version


@router.get("/status")
async def status():
    """Return {"status": "ok", "uptime_seconds": ..., "version": ...}."""
    return {
        "status": "ok",
        "uptime_seconds": uptime_seconds(),
        "version": _resolve_version(),
    }
