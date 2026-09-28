"""Stable validated-cache canary selection and immediate rollback marker."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[2]
_ROLLBACK_MARKER = _ROOT / "data" / "config" / "browser_cache.rollback"


def rollback_active() -> bool:
    env = os.environ.get("WEBAGENT_BROWSER_CACHE_ROLLBACK", "")
    if env.strip().lower() in {"1", "true", "yes", "on"}:
        return True
    return _ROLLBACK_MARKER.exists()


def canary_percent() -> float:
    raw = os.environ.get("WEBAGENT_BROWSER_CACHE_CANARY_PERCENT", "100")
    try:
        return max(0.0, min(100.0, float(raw)))
    except (TypeError, ValueError):
        return 0.0


def cache_canary_eligible(user_id: str) -> bool:
    if rollback_active() or not user_id:
        return False
    percent = canary_percent()
    if percent <= 0:
        return False
    if percent >= 100:
        return True
    digest = hashlib.sha256(
        f"webagent-cache-canary:{user_id}".encode("utf-8")
    ).digest()
    bucket = int.from_bytes(digest[:8], "big") / float(2**64)
    return bucket < (percent / 100.0)


def set_rollback(active: bool) -> bool:
    _ROLLBACK_MARKER.parent.mkdir(parents=True, exist_ok=True)
    if active:
        _ROLLBACK_MARKER.write_text(
            "Validated browser cache rollback active.\n", encoding="utf-8"
        )
    else:
        try:
            _ROLLBACK_MARKER.unlink()
        except FileNotFoundError:
            pass
    return rollback_active()
