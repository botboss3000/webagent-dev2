"""Instance metadata — persistent facts about any instance (cloud VM, fleet device,
local checkout, Cloud Run service, manual server, …).

Stored in its OWN narrow table (``instances``) so the payload is always a single
row read by the primary key (``ref``) — no joins, no scans. The ``metadata`` JSON
column holds everything that varies by kind (domains, repo, custom_icon, tunnel, …)
so adding a new field never needs a migration.

``device_presence`` (heartbeat/liveness) and ``device_jobs`` (dispatch queue) are
SEPARATE tables — their access patterns are fundamentally different (constant
upserts, atomic claims, TTL'd leases). Never merge them in here.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


async def get_instance(ref: str) -> Optional[Dict[str, Any]]:
    """Read one instance row by its ``ref``. Returns None when absent."""
    from app.db import get_app_db
    db = get_app_db()
    raw = db.get_raw_client()
    try:
        row = raw.table("instances").select("*").eq("ref", ref).execute()
    except Exception as e:
        logger.debug("get_instance(%s) error: %s", ref, e)
        return None
    data = (row.data or []) if hasattr(row, "data") else []
    if not data:
        return None
    inst = dict(data[0])
    # Parse the JSON metadata column back into a dict so callers never touch raw JSON.
    try:
        inst["metadata"] = _normalize_metadata(json.loads(inst.get("metadata") or "{}"))
    except Exception:
        inst["metadata"] = {}
    return inst


async def upsert_instance(ref: str, **fields) -> bool:
    """Create or update an instance row. Only the passed fields are written;
    every other column is left unchanged. The ``metadata`` field is auto-merged
    with the existing metadata dict (so passing ``domains`` doesn't wipe ``repo``).

    Special keys:
      • ``metadata`` — a dict that is MERGED into the existing JSON (not replaced).
      • ``kind``, ``display_name``, ``provider``, ``status``, ``ip``, ``endpoint``,
        ``platform``, ``zone``, ``machine_type``, ``created_at``, ``updated_at`` —
        written directly (None clears them).
    """
    from app.db import get_app_db
    db = get_app_db()
    now = _now_iso()

    # Separate the metadata dict from the column fields.
    meta_update = fields.pop("metadata", None) or {}

    # Read the existing row so we can merge metadata and keep missing fields unchanged.
    existing = await get_instance(ref)

    if existing:
        # Merge new column values over existing
        row: Dict[str, Any] = dict(existing)
        for k in _COLUMNS:
            if k in fields:
                row[k] = fields[k]
        # Merge metadata
        row["metadata"] = _merge_meta(existing.get("metadata") or {}, meta_update)
    else:
        row = {
            "ref": ref,
            "kind": fields.get("kind", ""),
            "display_name": fields.get("display_name", ""),
            "provider": fields.get("provider", ""),
            "status": fields.get("status", ""),
            "ip": fields.get("ip", ""),
            "endpoint": fields.get("endpoint", ""),
            "platform": fields.get("platform", ""),
            "zone": fields.get("zone", ""),
            "machine_type": fields.get("machine_type", ""),
            "created_at": fields.get("created_at") or now,
            "metadata": meta_update,
        }

    row["updated_at"] = now
    # Serialise metadata to JSON for the DB.
    row_out = dict(row)
    row_out["metadata"] = json.dumps(row.get("metadata") or {}, ensure_ascii=False)

    try:
        raw = db.get_raw_client()
        raw.table("instances").upsert(row_out, on_conflict="ref").execute()
        return True
    except Exception as e:
        logger.warning("upsert_instance(%s) error: %s", ref, e)
        return False


async def list_instances_by_kind(kind: str) -> List[Dict[str, Any]]:
    """All instances of a given ``kind`` (e.g. ``"cloud_vm"``)."""
    from app.db import get_app_db
    db = get_app_db()
    raw = db.get_raw_client()
    try:
        res = raw.table("instances").select("*").eq("kind", kind).execute()
    except Exception as e:
        logger.debug("list_instances_by_kind(%s) error: %s", kind, e)
        return []
    data = (res.data or []) if hasattr(res, "data") else []
    out = []
    for d in data:
        inst = dict(d)
        try:
            inst["metadata"] = json.loads(inst.get("metadata") or "{}")
        except Exception:
            inst["metadata"] = {}
        out.append(inst)
    return out


# ── helpers ────────────────────────────────────────────────────────────────────

_COLUMNS = {
    "kind", "display_name", "provider", "status", "ip", "endpoint",
    "platform", "zone", "machine_type", "created_at", "updated_at", "metadata",
}


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _normalize_metadata(metadata: dict) -> dict:
    """Migrate retired instance-prewarm fields without losing tile layout.

    ``prewarm`` never had an execution path and is discarded. The old
    ``prewarm_order`` UI preference becomes the neutral ``tile_order`` key.
    ``upsert_instance`` persists this normalized form on the next write.
    """
    normalized = dict(metadata or {})
    old_order = normalized.pop("prewarm_order", None)
    normalized.pop("prewarm", None)
    if "tile_order" not in normalized:
        try:
            if int(old_order or 0) > 0:
                normalized["tile_order"] = int(old_order)
        except (TypeError, ValueError):
            pass
    return normalized


def _merge_meta(existing: dict, update: dict) -> dict:
    """Merge ``update`` into ``existing`` shallowly — a key set to None or
    empty-string is removed from the result (clearing that field)."""
    merged = _normalize_metadata(existing)
    for k, v in update.items():
        if v is None or (isinstance(v, str) and v == ""):
            merged.pop(k, None)
        else:
            merged[k] = v
    return merged


async def track_endpoint_url(ref: str, url: str, https_auto: bool = False) -> bool:
    """Record that ``url`` is a live endpoint for instance ``ref``, stamping
    ``last_seen`` to now. Existing URLs for this instance are preserved (deep
    merge), so all URLs the instance has ever reported remain visible.

    Each URL entry: ``{"last_seen": "<ISO>", "https_auto": bool}``. An entry the
    admin flagged ``hidden`` keeps that flag — re-detecting a URL must not
    resurface it in the overview."""
    # Request-derived base URLs and tunnel-provider output differ in whether they
    # include a trailing slash. Store one canonical key so learning the live host
    # after launch updates the address announced by the tunnel instead of adding
    # a visually duplicate row.
    url = str(url or "").strip().rstrip("/")
    if not url:
        return False
    existing = await get_instance(ref)
    urls: Dict[str, Dict[str, Any]] = {}
    if existing:
        urls = dict(existing.get("metadata", {}).get("urls") or {})
    legacy_slash_url = url + "/"
    prev = urls.get(url) or urls.get(legacy_slash_url) or {}
    urls.pop(legacy_slash_url, None)
    urls[url] = {"last_seen": _now_iso(), "https_auto": https_auto}
    if prev.get("hidden"):
        urls[url]["hidden"] = True
    return await upsert_instance(ref, metadata={"urls": urls})


async def clear_endpoint_url(ref: str, url: str) -> bool:
    """Remove a single URL from instance ``ref``'s URL list."""
    existing = await get_instance(ref)
    if not existing:
        return False
    urls: Dict[str, Dict[str, Any]] = dict(existing.get("metadata", {}).get("urls") or {})
    if url not in urls:
        return False
    del urls[url]
    return await upsert_instance(ref, metadata={"urls": urls})


async def add_local_net_urls(ref: str, urls: List[str]) -> List[str]:
    """Add auto-detected LOCAL-NETWORK URLs to instance ``ref``'s URL list.

    These are the machine's own LAN addresses (``http://192.168.x.x:<port>/``),
    detected by the Instances page backend and listed by default in the Overview —
    they work for anyone on the same network even when no heartbeat signal has
    ever been seen and nobody is using them. Each entry is stored WITHOUT a
    ``last_seen`` (there is no signal to stamp) and flagged ``{"local_net": True}``
    so the UI can label it as such instead of showing a stale age.

    Existing entries are preserved untouched (a real ``last_seen`` from a signal
    or an admin ``hidden`` flag wins), so re-running discovery on every Overview
    open only ever ADDS genuinely new addresses. Returns the URLs that were added.
    """
    existing = await get_instance(ref)
    cur: Dict[str, Dict[str, Any]] = {}
    if existing:
        cur = dict(existing.get("metadata", {}).get("urls") or {})
    added: List[str] = []
    for url in urls:
        if not url or url in cur:
            continue
        cur[url] = {"local_net": True}
        added.append(url)
    if added:
        await upsert_instance(ref, metadata={"urls": cur})
    return added


async def set_endpoint_url_hidden(ref: str, url: str, hidden: bool) -> bool:
    """Mark a single URL on instance ``ref`` as hidden (or visible again).

    Hidden URLs stay in the list (their ``last_seen`` history survives) but the
    overview UI renders them collapsed/dimmed behind a "Hidden" group so they
    don't clutter the URL section. Each URL entry gains ``{"hidden": bool}``.

    A URL that isn't tracked yet is ADDED as hidden (stamped now) so the admin
    can hide the fallback URL (e.g. the self tile's localhost) before the device
    has ever reported it. The instance row is created on first use."""
    existing = await get_instance(ref)
    urls: Dict[str, Dict[str, Any]] = {}
    if existing:
        urls = dict(existing.get("metadata", {}).get("urls") or {})
    entry = dict(urls.get(url) or {})
    entry["hidden"] = bool(hidden)
    if hidden and not entry.get("last_seen"):
        entry["last_seen"] = _now_iso()
    urls[url] = entry
    return await upsert_instance(ref, metadata={"urls": urls})
