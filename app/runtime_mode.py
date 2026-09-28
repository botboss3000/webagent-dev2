"""Fail-closed runtime settings for isolated performance-test instances."""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_ROOT = (PROJECT_ROOT / "data").resolve()
_TRUTHY = {"1", "true", "yes", "on"}


def performance_test_mode() -> bool:
    return os.environ.get("WEBAGENT_PERF_TEST_MODE", "").strip().lower() in _TRUTHY


def data_root() -> Path:
    """Return the runtime data root, rejecting unsafe performance-test setup."""
    if performance_test_mode():
        raw = os.environ.get("WEBAGENT_PERF_TEST_DATA_DIR", "").strip()
        if not raw:
            raise RuntimeError(
                "WEBAGENT_PERF_TEST_MODE requires WEBAGENT_PERF_TEST_DATA_DIR"
            )
        target = Path(raw).expanduser().resolve()
        if target == DEFAULT_DATA_ROOT or DEFAULT_DATA_ROOT in target.parents:
            raise RuntimeError("Performance-test data must be outside the live data directory")
        target.mkdir(parents=True, exist_ok=True)
        return target

    override = os.environ.get("WEBAGENT_DATA_ROOT", "").strip()
    return Path(override).expanduser().resolve() if override else DEFAULT_DATA_ROOT


def background_services_enabled() -> bool:
    return not performance_test_mode()
