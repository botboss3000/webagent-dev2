"""Move Gen UI pages between stores — filesystem → database.

Used when a user (or admin) switches Gen UI storage from **"this device only"**
(the filesystem store, pages saved as files on one machine's disk) to **"my
account — synced"** (the database store, whose ``genui`` table replicates across
every device via the hybrid sync engine / user-BYOD router). Without this
step, existing on-disk pages would simply be left behind on the old machine when
the store mode flips.

Everything is written through the destination store's TYPED methods
(``create_genui`` / ``save_genui_html`` / ``save_genui_data``), never raw SQL, so
the write lands on the correct plane in hybrid and user-BYOD setups (see the
``hybrid-getconn-routes-remote-gotcha`` note). The copy is non-destructive: an
already-present page in the destination is left untouched unless ``overwrite`` is
set, and the source files on disk are never deleted — the migration only ever
adds to the account store.
"""

import logging
from typing import Dict, List, Optional

from app import user_workspace as _ws
from app.genui_store.common import discover_genui_slugs
from app.genui_store.database import DatabaseGenuiStore
from app.genui_store.filesystem import FilesystemGenuiStore
from app.genui_store.interface import GenuiStore

logger = logging.getLogger(__name__)


def _discover_user_ids() -> List[str]:
    """Every user id that has an on-disk ``genui/`` folder under the user-data root.

    Directory names are already path-safe segments (``user_workspace.safe_segment``
    is idempotent), so they round-trip back through the store helpers unchanged.
    """
    ids: List[str] = []
    try:
        base = _ws.base_dir()
        if base.is_dir():
            for child in sorted(base.iterdir()):
                try:
                    if child.is_dir() and (child / "genui").is_dir():
                        ids.append(child.name)
                except OSError:
                    continue
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("Gen UI migrate: could not enumerate users: %s", e)
    return ids


async def migrate_user(
    user_id: str,
    source: Optional[GenuiStore] = None,
    dest: Optional[GenuiStore] = None,
    overwrite: bool = False,
) -> Dict:
    """Copy one user's filesystem Gen UI pages into the destination (database) store.

    Returns ``{user_id, copied:[slug…], skipped:[slug…], failed:[{slug,error}…]}``.
    ``skipped`` = a page with that slug already exists in the destination and
    ``overwrite`` was not requested.
    """
    source = source or FilesystemGenuiStore()
    dest = dest or DatabaseGenuiStore()
    result: Dict = {"user_id": user_id, "copied": [], "skipped": [], "failed": []}

    # One catalog read gives title / agent_context / agent_id per slug.
    try:
        src_entries = {e["slug"]: e for e in await source.list_genui(user_id)}
    except Exception as e:
        logger.warning("Gen UI migrate: list failed for user %s: %s", user_id, e)
        result["failed"].append({"slug": "*", "error": str(e)})
        return result

    for slug, entry in src_entries.items():
        try:
            html = await source.get_genui_html(user_id, slug)
            if html is None:
                continue  # a catalog entry with no body — nothing to carry over
            exists = await dest.get_genui_html(user_id, slug) is not None
            if exists and not overwrite:
                result["skipped"].append(slug)
                continue

            agent_id = entry.get("agent_id") or ""
            session_config = entry.get("session_config")
            if not isinstance(session_config, dict):
                session_config = None
            if exists:
                # Overwrite: replace the body (title/agent_context are preserved by
                # the database store's save_genui_html).
                await dest.save_genui_html(user_id, slug, html, agent_id=agent_id)
            else:
                await dest.create_genui(
                    user_id,
                    slug,
                    title=entry.get("title") or slug,
                    agent_context=entry.get("agent_context") or "",
                    initial_html=html,
                    agent_id=agent_id,
                    session_config=session_config,
                )

            # Carry the page's DATA object across too, when it has one.
            data = await source.get_genui_data(user_id, slug)
            if data is not None:
                await dest.save_genui_data(user_id, slug, data)

            result["copied"].append(slug)
        except Exception as e:
            logger.warning(
                "Gen UI migrate: failed slug %s for user %s: %s", slug, user_id, e
            )
            result["failed"].append({"slug": slug, "error": str(e)})

    return result


async def migrate_all(overwrite: bool = False) -> Dict:
    """Migrate every discoverable user's on-disk pages into the database store.

    Single-tenant only: the destination store resolves through the shared account
    database, and each row is scoped by ``user_id``. In user-BYOD mode every user
    already owns a separate database, so writes for a user other than the current
    caller would land in the wrong tenant — callers must migrate per-user in that
    mode instead.
    """
    source = FilesystemGenuiStore()
    dest = DatabaseGenuiStore()
    results: List[Dict] = []
    for uid in _discover_user_ids():
        results.append(await migrate_user(uid, source, dest, overwrite))
    totals = {
        "users": len(results),
        "copied": sum(len(r["copied"]) for r in results),
        "skipped": sum(len(r["skipped"]) for r in results),
        "failed": sum(len(r["failed"]) for r in results),
    }
    return {"totals": totals, "results": results}
