"""Edition gating — decide whether a feature is switched on for this build.

The active edition (``full`` by default) decides which features load. This
module is the single gate the load sites consult. **It is a no-op for the
``full`` edition** — `is_enabled(...)` short-circuits to True before doing any
work, so the default build behaves exactly as before and there is zero overhead
and zero risk unless an operator sets ``WEBAGENT_EDITION`` to something else.

Resolution is by the feature's maturity *status* (stable/beta/experimental),
matched against the edition rule (see ``app/features/editions.py``). A feature
the catalog doesn't recognise is **never hidden** (fail-open), so gating can
only ever trim known-immature features, never silently drop something unknown.
"""

from __future__ import annotations

import importlib
import logging
from typing import Optional

from app.features.editions import get_active_edition, edition_includes

logger = logging.getLogger(__name__)


def _full() -> bool:
    return get_active_edition() == "full"


def is_enabled(status: Optional[str], feature_id: str) -> bool:
    """Core decision: would the active edition include a feature of this status?

    Fail-open: unknown edition or missing status → enabled.
    """
    if _full():
        return True
    if not status or not feature_id:
        return True
    try:
        return edition_includes(status, feature_id, get_active_edition())
    except Exception:
        return True


def module_enabled(module_name: str, *, default_id: str = "") -> bool:
    """Gate a feature by reading the ``FEATURE`` header off its module.

    Used by load sites that have the providing module in hand (integrations,
    event sources, channels). Fail-open on any error.
    """
    if _full():
        return True
    try:
        mod = importlib.import_module(module_name)
        feature = getattr(mod, "FEATURE", None)
        if not isinstance(feature, dict):
            return True  # no header → unknown → don't hide
        fid = str(feature.get("id") or default_id or module_name)
        return is_enabled(feature.get("status"), fid)
    except Exception:
        return True


def feature_enabled(feature: Optional[dict], default_id: str = "") -> bool:
    """Gate using a ``FEATURE`` dict already in hand (no import). Fail-open."""
    if _full():
        return True
    if not isinstance(feature, dict):
        return True
    fid = str(feature.get("id") or default_id)
    return is_enabled(feature.get("status"), fid)


def ability_enabled(ability_id: str) -> bool:
    """Gate an agent ability by its catalogued status."""
    if _full():
        return True
    try:
        from app.features.catalog import _ABILITIES
        for aid, _display, status, _summary in _ABILITIES:
            if aid == ability_id:
                return is_enabled(status, ability_id)
        return True  # unknown ability → don't hide
    except Exception:
        return True
