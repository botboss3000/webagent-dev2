"""my_card — My Card backend (copy of _template). See cards/README.md.

Contributes the snapshot section named in card.json `section`.
REMOVE-WHEN: the Dashboard tab is dropped from the Instances page.
"""

from __future__ import annotations

from typing import Any, Dict

from dashboard_server_lib import logger


async def build_section(ctx: Dict[str, Any]) -> Dict[str, Any]:
    # ctx: uid, window_s, rows (the shell's shared usage rows — never re-scan),
    # run_rows, db_health, storage, project_root, snapshot (grows as earlier-
    # `order` sections land, so sibling sections can be read from here).
    try:
        return {"my_value": 0}
    except Exception as e:
        logger.debug("dashboard my_card section failed: %s", e)
        return {}
