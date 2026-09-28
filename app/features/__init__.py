"""Feature catalog + editions.

This package implements the **discovery layer** described in
`docs/claude/production-editions.md`: every capability in the app is a
self-describing *feature* (an optional module-level ``FEATURE`` dict), and this
package scans the plugin folders to build one **catalog** of what code is
available, plus the **edition** logic that (eventually) decides which features a
given build switches on.

Phase 1 is **report-only** — nothing is gated. The default edition is ``full``,
so every feature loads exactly as before.

Public surface:
    from app.features import build_catalog, get_active_edition, edition_includes
"""

from app.features.descriptor import FeatureDescriptor, read_feature
from app.features.editions import (
    get_active_edition,
    list_editions,
    edition_includes,
    edition_reason,
)
from app.features.catalog import build_catalog

__all__ = [
    "FeatureDescriptor",
    "read_feature",
    "get_active_edition",
    "list_editions",
    "edition_includes",
    "edition_reason",
    "build_catalog",
]
