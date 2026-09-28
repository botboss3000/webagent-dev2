"""Admin Dashboard — BACKEND for /admin/dashboard/* (embedded in the Instances view)

Serves the movable/resizable metric grid that now lives as the "Dashboard" tab
inside the Instances page's "This device" tile (ui/main-panel/app/).
Three concerns, all admin-only (via ``resolve_admin_uid``, honours open mode):

  • metrics  — ONE composite snapshot the grid polls. The LIVE parts (CPU, DB
    latency, loop throughput) come from the in-memory recorder (app/metrics.py)
    with ZERO database round-trips — reading DB slowness from the DB would add
    the very load we're diagnosing. The heavier parts (token totals, cost,
    failures, devices) are cheap aggregates cached for a few seconds so a
    dashboard left open doesn't hammer the database. `/chart` feeds the
    full-width "Metrics over time" hero card: a shared time axis with per-model
    cost (stacked bars) + token/CPU/RAM lines, over a window OR a custom date
    range (durable series from usage_events; CPU/RAM live-only via metrics ring).
  • layout   — each admin's card arrangement (PER-USER), persisted to a
    gitignored JSON store so it survives reloads and restarts.
  • ai-card  — turn a plain-English request into a custom card spec, reusing the
    App Settings default-LLM resolver (app/agent/suggestions._resolve_default_llm).

This folder is NOT a top-level page — ui_pages discovery only scans
ui/admin-tools/<id>/ — so it is registered through the instances page.json
``routers`` list and mounted DIRECTLY on the app (keeping its own /admin/dashboard/*
prefix; see ui/main-panel/app/server.py + app/ui_pages/__init__.py).
`.py` files under ui/ are never served to the browser. The old standalone
Admin Tools → Dashboard page (ui/admin-tools/dashboard/) was removed; its
instance-switcher header endpoints (/admin/dashboard/instances/*) went with it —
only the embedded tab's routes remain here.

NOTE: no ``from __future__ import annotations`` here — this module is imported
under a synthetic name by the page-catalog loader, and stringised annotations
would leave the Pydantic request models unresolvable at request time (500).
"""
import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/dashboard", tags=["admin-dashboard"])

_PROJECT_ROOT = Path(__file__).resolve().parents[4]
_LAYOUT_FILE = _PROJECT_ROOT / "data" / "config" / "dashboard-layouts.json"

# Composite-snapshot cache: the DB-derived parts are recomputed at most every
# _CACHE_TTL seconds no matter how many admins/tabs poll, so an open dashboard
# adds negligible DB load. The live (in-memory) parts are always fresh.
_CACHE_TTL = 8.0
_cache: Dict[str, Any] = {"at": 0.0, "window": None, "data": None}


async def _require_admin(uid: str) -> None:
    from app.auth.identity import resolve_admin_uid
    if not await resolve_admin_uid(uid):
        raise HTTPException(status_code=403, detail="Admin required")


# ── time helpers ────────────────────────────────────────────────────────────
def _to_epoch(val: Any) -> float:
    """Parse a stored timestamp (ISO string or epoch number) to epoch seconds.
    Returns 0 on anything unparseable so it falls outside every window."""
    if val is None:
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip()
    if not s:
        return 0.0
    try:
        return float(s)
    except ValueError:
        pass
    try:
        s2 = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s2)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return 0.0


def _iso_since(window_s: float) -> str:
    return datetime.fromtimestamp(time.time() - window_s, tz=timezone.utc).isoformat()


def _iso_from_epoch(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _sql_ts(epoch: float) -> str:
    """A lower-bound string that matches how ``created_at`` is STORED — space
    separated UTC, no ``T`` and no offset (e.g. ``2026-07-01 18:11:25``). This
    matters for the coarse DB-side ``.gte`` pre-filter: stored timestamps look
    like ``2026-07-01 19:02:28`` and comparisons are lexical on SQLite (text) and
    string-cast on Postgres. An ISO bound with a ``T`` (0x54) sorts AFTER a
    space (0x20), so a ``T``-formatted bound silently drops every row on the same
    calendar day — which is exactly why a 5m/1h window came back empty. The
    caller still re-filters precisely by epoch, so this bound only needs to sort
    correctly, not be exact."""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ── DB-derived aggregate sections (each best-effort, degrade to safe defaults) ─
# The usage_events rows for the window are fetched ONCE per snapshot rebuild
# (_fetch_usage_rows — hybrid-aware) and shared by the tokens / agents /
# sessions / models sections, so adding cards did not add extra 20k-row scans.
def _tokens_section(rows: List[Dict[str, Any]], window_s: float) -> Dict[str, Any]:
    """Token in/out + cost over the window, plus a small per-agent breakdown,
    aggregated from the shared usage_events rows."""
    out = {"in": 0, "out": 0, "cost_usd": 0.0, "by_agent": [], "calls": 0}
    try:
        cutoff = time.time() - window_s
        by_agent: Dict[str, Dict[str, float]] = {}
        for r in rows:
            if _to_epoch(r.get("created_at")) < cutoff:
                continue
            ti = int(r.get("input_tokens") or 0)
            to = int(r.get("output_tokens") or 0)
            cost = float(r.get("cost_usd") or 0.0)
            out["in"] += ti
            out["out"] += to
            out["cost_usd"] += cost
            out["calls"] += 1
            aid = r.get("agent_id") or "—"
            a = by_agent.setdefault(aid, {"in": 0, "out": 0, "cost": 0.0})
            a["in"] += ti
            a["out"] += to
            a["cost"] += cost
        top = sorted(by_agent.items(), key=lambda kv: kv[1]["in"] + kv[1]["out"], reverse=True)[:6]
        out["by_agent"] = [
            {"agent": k, "in": v["in"], "out": v["out"], "cost_usd": round(v["cost"], 4)}
            for k, v in top
        ]
        out["cost_usd"] = round(out["cost_usd"], 4)
    except Exception as e:
        logger.debug("dashboard tokens section failed: %s", e)
    return out


async def _db_health_section() -> Dict[str, Any]:
    """Which backend is live and whether it silently degraded to local SQLite."""
    from app.db import get_connection_health, get_mode, is_remote_db
    from app.db.hybrid import hybrid_enabled
    from app.db.connection_config import load_config
    try:
        health = get_connection_health()
        cfg = load_config()
        prov = cfg.provider
        host = cfg.host or None
    except Exception:
        health = {}
        prov = "unknown"
        host = None
    try:
        hybrid = bool(hybrid_enabled())
    except Exception:
        hybrid = False
    return {
        "provider": prov,
        "host": host,
        "hybrid": hybrid,
        "mode": get_mode(),
        "actual": health.get("actual") or get_mode(),
        "intended": health.get("intended"),
        "degraded": bool(health.get("degraded")),
        "remote": bool(is_remote_db()),
        "ok": bool(health.get("ok", True)),
        "message": health.get("message"),
    }


async def _active_run_rows() -> List[Dict[str, Any]]:
    """All currently-running run-state rows (all users) — the count feeds the
    Active Runs card; the rows feed the Sessions & runs monitor."""
    try:
        from app.db import get_db
        from app.db.offload import db_offload
        db = get_db()
        return await db_offload(lambda: db.run_state_list_active_all()) or []
    except Exception as e:
        logger.debug("dashboard active-runs failed: %s", e)
        return []

def _storage_section() -> Dict[str, Any]:
    out: Dict[str, Any] = {"files": []}
    data_dir = _PROJECT_ROOT / "data"
    for name in ("local.db", "logs.db", "vault.db", "recordings.db"):
        p = data_dir / name
        try:
            if p.is_file():
                out["files"].append({"name": name, "mb": round(p.stat().st_size / 1e6, 1)})
        except Exception:
            pass
    try:
        import shutil
        usage = shutil.disk_usage(str(_PROJECT_ROOT))
        out["disk_free_gb"] = round(usage.free / 1e9, 1)
        out["disk_total_gb"] = round(usage.total / 1e9, 1)
    except Exception:
        pass
    return out


def _memory_mb() -> Optional[float]:
    try:
        from app.agent.loop import _process_memory_mb
        v = _process_memory_mb()
        return round(v, 1) if v else None
    except Exception:
        return None


# ── Card plugin system ──────────────────────────────────────────────
# Each dashboard CARD is a self-contained folder under cards/<id>/ (card.json +
# card.js + optional server.py / card.css) — the same drop-in philosophy as the
# ui/admin-tools page folders. The layout JSON (order + x/y/w/h) decides what
# renders; the catalog below is built by scanning the folders. A card with a
# `server` module contributes a snapshot section (build_section(ctx)).
# DO NOT add new section builders to _build_snapshot() below — that is
# hardcoding. Create a card folder with a server.py instead (see cards/README.md).
_CARDS_DIR = Path(__file__).resolve().parent / "cards"

# server-lib helpers for card backends: load once and register under a stable
# name so each card's server.py can `from dashboard_server_lib import ...`
# regardless of how this module was imported (synthetic catalog name).
try:
    import importlib.util as _ilu, sys as _sys
    _lib_spec = _ilu.spec_from_file_location("dashboard_server_lib", _CARDS_DIR / "_lib" / "server-lib.py")
    _lib_mod = _ilu.module_from_spec(_lib_spec)
    if _lib_spec.loader is not None:
        _lib_spec.loader.exec_module(_lib_mod)
        _sys.modules["dashboard_server_lib"] = _lib_mod
except Exception as _lib_err:
    logger.warning("dashboard card server-lib failed to load: %s", _lib_err)

_CARD_CATALOG: Optional[List[Dict[str, Any]]] = None


def _card_catalog() -> List[Dict[str, Any]]:
    """Scan cards/<id>/card.json into descriptors for GET /cards and the
    snapshot builder. Cached; a dropped folder is picked up on restart."""
    global _CARD_CATALOG
    if _CARD_CATALOG is not None:
        return _CARD_CATALOG
    out: List[Dict[str, Any]] = []
    if _CARDS_DIR.is_dir():
        for folder in sorted(_CARDS_DIR.iterdir()):
            if not folder.is_dir() or folder.name.startswith("_"):
                continue
            jf = folder / "card.json"
            if not jf.is_file():
                continue
            try:
                d = json.loads(jf.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(d, dict) or not d.get("id"):
                continue
            out.append({
                "id": d["id"],
                "label": d.get("label") or d["id"],
                "icon": d.get("icon") or "layout-dashboard",
                "w": int(d.get("w") or 3), "h": int(d.get("h") or 2),
                "order": int(d.get("order") or 999),
                "live": bool(d.get("live")),
                "sections": [str(s) for s in (d.get("sections") or [])],
                "chart": d.get("chart"),
                "section": d.get("section"),
                "hasServer": (folder / (d.get("server") or "server.py")).is_file(),
                "hasCss": (folder / "card.css").is_file(),
                "aiHint": d.get("aiHint"),
            })
    _CARD_CATALOG = out
    return out


_CARD_BACKENDS: Optional[Dict[str, Any]] = None


def _card_backends() -> Dict[str, Any]:
    """Load each card's server.py (once) → {section: module}. Isolated: a
    broken backend logs and is skipped, never failing the dashboard."""
    global _CARD_BACKENDS
    if _CARD_BACKENDS is not None:
        return _CARD_BACKENDS
    backends: Dict[str, Any] = {}
    for d in _card_catalog():
        section = d.get("section")
        if not section or not d.get("hasServer"):
            continue
        mod_path = _CARDS_DIR / d["id"] / "server.py"
        mod_name = f"webagent_dashboard_card_{d['id']}".replace("-", "_")
        try:
            import importlib.util as _ilu
            spec = _ilu.spec_from_file_location(mod_name, mod_path)
            if spec is None or spec.loader is None:
                continue
            mod = _ilu.module_from_spec(spec)
            spec.loader.exec_module(mod)
            if callable(getattr(mod, "build_section", None)):
                backends[section] = mod
        except Exception as e:
            logger.warning("Dashboard card backend %s failed to load: %s", d["id"], e)
    _CARD_BACKENDS = backends
    return backends


async def _plugin_sections(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Run every installed card backend's build_section(ctx), merged by section.
    ctx carries the shell's shared rows (ONE usage_events fetch), db_health,
    storage and the growing snapshot — so backends never re-scan usage_events,
    and a card (health_board) can read a sibling's section (devices)."""
    merged: Dict[str, Any] = {}
    for d in sorted(_card_catalog(), key=lambda x: x["order"]):
        section = d.get("section")
        mod = _card_backends().get(section) if section else None
        if not mod:
            continue
        try:
            merged[section] = await mod.build_section(ctx)
        except Exception as e:
            logger.warning("Dashboard card section %s failed: %s", d["id"], e)
            merged.setdefault(section, {})
    return merged


async def _live_snapshot(window_s: float) -> Dict[str, Any]:
    """The FAST first fill for the dashboard: only the sections that need NO
    database scan — the in-memory live gauges (app/metrics.py), process memory,
    the connection-health read (in-memory + file config), and the filesystem
    storage stat. Returns in milliseconds even against a cold/slow remote DB, so
    the grid's live/db-health/storage cards populate immediately while the heavier
    DB-backed cards keep spinning until the full ``/metrics`` poll lands. Marked
    ``partial`` so callers know the DB-derived sections are absent (not empty)."""
    from app import metrics
    live = metrics.snapshot(window_s=min(window_s, 300.0))
    db_health = await _db_health_section()
    memory_mb = _memory_mb()
    # Feed the live ring so the over-time chart keeps trending between full polls.
    metrics.record_system((live or {}).get("cpu_percent"), memory_mb, 0)
    return {
        "generated_at": int(time.time()),
        "window_s": window_s,
        "partial": True,
        "live": live,
        "memory_mb": memory_mb,
        "db_health": db_health,
        "storage": _storage_section(),
    }


async def _build_snapshot(uid: str, window_s: float) -> Dict[str, Any]:
    from app import metrics
    # Live section is always fresh (in-memory, DB-free). Clamp the live window to
    # something sensible (5 min) so a "7 days" aggregate window doesn't dilute the
    # live gauges — aggregates honour the full window; live gauges stay recent.
    live = metrics.snapshot(window_s=min(window_s, 300.0))
    # ONE usage_events fetch (hybrid-aware) + one active-runs read feed the
    # tokens section AND every card backend (via ctx), so adding cards does not
    # add extra 20k-row scans.
    usage_rows, run_rows = await asyncio.gather(
        _usage_rows(time.time() - window_s),
        _active_run_rows(),
    )
    db_health, storage = await asyncio.gather(
        _db_health_section(),
        asyncio.to_thread(_storage_section),
    )
    # Plugin card sections (cards/<id>/server.py → build_section). ctx grows as
    # sections land so a card can read a sibling's output (health_board reads
    # devices / storage / db_health).
    ctx: Dict[str, Any] = {
        "uid": uid, "window_s": window_s,
        "rows": usage_rows, "run_rows": run_rows,
        "db_health": db_health, "storage": storage,
        "project_root": _PROJECT_ROOT,
        "snapshot": {
            "live": live, "memory_mb": _memory_mb(), "active_runs": len(run_rows),
            "db_health": db_health, "storage": storage,
        },
    }
    sections = await _plugin_sections(ctx)
    ctx["snapshot"].update(sections)
    snap = dict(ctx["snapshot"])
    snap.update({
        "generated_at": int(time.time()),
        "window_s": window_s,
        "tokens": _tokens_section(usage_rows, window_s),
    })
    return snap


@router.get("/metrics")
async def metrics_endpoint(
    requesting_user_id: str = Query(""),
    window: float = Query(3600.0),
    live: int = Query(0),
):
    await _require_admin(requesting_user_id)
    window = max(60.0, min(float(window), 7 * 86400.0))
    # Fast, DB-free partial for the grid's first paint (see _live_snapshot). Does
    # NOT touch the composite cache — it's cheap enough to always recompute.
    if live:
        return await _live_snapshot(window)
    now = time.time()
    if _cache["data"] is not None and _cache["window"] == window and (now - _cache["at"]) < _CACHE_TTL:
        # Refresh only the always-cheap live section on a cache hit so CPU/DB
        # gauges keep ticking between the heavier aggregate refreshes.
        from app import metrics
        cached = dict(_cache["data"])
        cached["live"] = metrics.snapshot(window_s=min(window, 300.0))
        cached["memory_mb"] = _memory_mb()
        cached["cached"] = True
        # Feed the live CPU/RAM/active-run history ring so the over-time chart can
        # trend them (live-only, going forward — none of the three has a durable store).
        metrics.record_system((cached["live"] or {}).get("cpu_percent"), cached["memory_mb"],
                              cached.get("active_runs") or 0)
        return cached
    data = await _build_snapshot(requesting_user_id, window)
    _cache.update(at=now, window=window, data=data)
    from app import metrics
    metrics.record_system((data.get("live") or {}).get("cpu_percent"), data.get("memory_mb"),
                          data.get("active_runs") or 0)
    return data


@router.get("/metrics/timeseries")
async def timeseries_endpoint(
    requesting_user_id: str = Query(""),
    kind: str = Query("db"),
    window: float = Query(1800.0),
    buckets: int = Query(30),
):
    await _require_admin(requesting_user_id)
    from app import metrics
    kind = "db" if kind not in ("db", "llm") else kind
    buckets = max(6, min(int(buckets), 120))
    window = max(300.0, min(float(window), 7 * 86400.0))
    return {"kind": kind, "points": metrics.timeseries(kind, buckets=buckets, window_s=window)}


@router.get("/cards")
async def cards_catalog(requesting_user_id: str = Query("")):
    """The installed card-plugin catalog — built by scanning cards/<id>/card.json.
    The frontend uses it for the picker, default sizes and section-needs, so a
    dropped folder shows up with zero shell edits."""
    await _require_admin(requesting_user_id)
    return {"cards": _card_catalog()}


# ── the full-width "Metrics over time" chart ─────────────────────────────────
# ONE endpoint feeding the hero chart card: a shared time X axis with several
# selectable series. Durable series (tokens in/out, per-model cost) come from
# usage_events and support any window OR a custom date range. In hybrid mode the
# rows are a UNION of the remote authority (history) + the local hot store (the
# freshest, not-yet-synced tail), deduped by id — see _fetch_usage_rows — so a
# frequent poll reflects live activity immediately. Live series (CPU, RAM) come
# from the in-memory ring (app/metrics.py) and only trend forward.
_CHART_LINE_META = {
    # Durable (usage_events / diagnostics) — full history, any window or range.
    "tokens_in":  {"label": "Tokens in",   "unit": "tok",   "src": "usage"},
    "tokens_out": {"label": "Tokens out",  "unit": "tok",   "src": "usage"},
    "llm_calls":  {"label": "LLM calls",   "unit": "calls", "src": "usage"},
    "errors":     {"label": "Errors",      "unit": "calls", "src": "diag"},
    # Live (in-memory rings, app/metrics.py) — trend forward from boot only.
    "cpu":         {"label": "CPU",              "unit": "%",    "src": "live", "kind": "cpu"},
    "ram":         {"label": "RAM",              "unit": "MB",   "src": "live", "kind": "ram"},
    "active_runs": {"label": "Active runs",      "unit": "runs", "src": "live", "kind": "runs"},
    "db_avg_ms":   {"label": "DB latency (avg)", "unit": "ms",   "src": "live", "kind": "db"},
    "db_p95_ms":   {"label": "DB latency (p95)", "unit": "ms",   "src": "live", "kind": "db_p95"},
}
_CHART_MAX_MODELS = 8  # top-N models by cost get their own bar; the rest fold into "Other"
# Shared by the chart AND the snapshot sections (tokens / agents / sessions /
# models) — agent_id + session_id ride along so one fetch serves them all.
_USAGE_COLS = "id,input_tokens,output_tokens,cost_usd,model,agent_id,session_id,created_at"


def _select_usage(raw, start: float) -> List[Dict[str, Any]]:
    """Run the usage_events query on ONE raw client since ``start``. The lower
    bound uses the stored timestamp format (see ``_sql_ts``) so the coarse
    DB-side pre-filter is correct; the caller re-filters precisely by epoch."""
    q = (raw.table("usage_events").select(_USAGE_COLS)
         .gte("created_at", _sql_ts(start)).order("created_at", desc=True).limit(20000))
    return getattr(q.execute(), "data", None) or []


async def _fetch_usage_rows(start: float) -> List[Dict[str, Any]]:
    """Usage rows for the chart since ``start``.

    In HYBRID mode the remote Postgres is the authority (full history) but the
    background sync means its newest rows can be a minute-to-hours behind; the
    local SQLite hot store holds those freshest rows first. So we UNION both,
    deduped by row ``id`` — remote supplies history, local overlays the live
    tail — which is what lets a frequent poll show activity the instant it
    happens instead of waiting for the push to remote. Non-hybrid installs just
    query the single active backend. Either side failing degrades to the other
    (never hard-fails the chart).

    Reads the CONTROL (central) database — usage_events is the central billing
    plane in user-BYOD mode (see app/agent/loop.py). No-op difference in
    single-tenant mode."""
    from app.db import get_control_db
    db = get_control_db()
    local = getattr(db, "local", None)
    remote = getattr(db, "remote", None)
    if local is not None and remote is not None:
        merged: Dict[Any, Dict[str, Any]] = {}
        # Remote first (authority/history); local second so its fresher copy of a
        # shared id wins and its not-yet-synced tail is added on top.
        for who, backend in (("remote", remote), ("local", local)):
            try:
                rows = await asyncio.to_thread(_select_usage, backend.get_raw_client(), start)
                for r in rows:
                    merged[r.get("id") or id(r)] = r
            except Exception as e:
                logger.debug("dashboard chart usage fetch (%s) failed: %s", who, e)
        return list(merged.values())
    try:
        return await asyncio.to_thread(_select_usage, db.get_raw_client(), start)
    except Exception as e:
        logger.debug("dashboard chart usage fetch failed: %s", e)
        return []


# ── shared usage-rows fetch cache (de-dupes the biggest cost on page open) ────
# Opening the dashboard fires BOTH /metrics (→ _build_snapshot) and /chart at the
# same instant, and each one independently scans up to 20k usage_events rows —
# in hybrid mode that is a remote AND a local query per call, so up to FOUR big
# scans against the slow remote Postgres for a single page open. Every consumer
# already re-filters the rows precisely by epoch, so a slightly-wider/older row
# set is always safe to reuse. This wrapper therefore (1) serves a fresh cached
# set for a few seconds, and (2) coalesces concurrent callers onto ONE in-flight
# fetch — so the two page-open requests share a single scan instead of racing.
# Windows are bucketed to 10s so /metrics' and /chart's near-identical start
# times collide; distinct windows (5m vs 7d) key separately and never share.
_USAGE_TTL = 6.0
_usage_cache: Dict[int, Dict[str, Any]] = {}      # bucket -> {"at", "rows"}
_usage_inflight: Dict[int, "asyncio.Task"] = {}   # bucket -> in-flight fetch


def _usage_bucket(start: float) -> int:
    return int(start // 10)


async def _usage_rows(start: float) -> List[Dict[str, Any]]:
    """Cached, concurrency-coalesced wrapper over ``_fetch_usage_rows``."""
    bucket = _usage_bucket(start)
    now = time.time()
    hit = _usage_cache.get(bucket)
    if hit is not None and (now - hit["at"]) < _USAGE_TTL:
        return hit["rows"]
    task = _usage_inflight.get(bucket)
    if task is not None:
        return await task            # a sibling request is already fetching this window
    task = asyncio.ensure_future(_fetch_usage_rows(start))
    _usage_inflight[bucket] = task
    try:
        rows = await task
    finally:
        _usage_inflight.pop(bucket, None)
    _usage_cache[bucket] = {"at": time.time(), "rows": rows}
    # Prune stale buckets so a long-lived process doesn't accrete windows.
    for b in [b for b, v in _usage_cache.items() if (time.time() - v["at"]) > _USAGE_TTL * 3]:
        _usage_cache.pop(b, None)
    return rows


def _resolve_range(window: float, frm: Optional[str], to: Optional[str]) -> tuple:
    """Return (start_epoch, end_epoch) from either a preset window (seconds ending
    now) or an explicit from/to. Date-only strings (YYYY-MM-DD) span whole days:
    `from` = start of that day, `to` = end of that day."""
    now = time.time()
    if frm or to:
        start = _to_epoch(frm) if frm else (now - window)
        if to:
            end = _to_epoch(to)
            if end and len(str(to).strip()) == 10 and "T" not in str(to):
                end += 86400.0  # date-only upper bound → include the whole day
        else:
            end = now
    else:
        window = max(60.0, min(float(window), 30 * 86400.0))
        start, end = now - window, now
    if not start:
        start = now - 3600.0
    if not end or end <= start:
        end = start + 60.0
    return start, end


@router.get("/chart")
async def chart_endpoint(
    requesting_user_id: str = Query(""),
    window: float = Query(3600.0),
    frm: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = Query(None, alias="to"),
    buckets: int = Query(48),
    series: str = Query("tokens_in,tokens_out,cost"),
):
    await _require_admin(requesting_user_id)
    from app import metrics
    buckets = max(6, min(int(buckets), 200))
    start, end = _resolve_range(window, frm, to)
    span = (end - start) / buckets
    want = {s.strip() for s in (series or "").split(",") if s.strip()}
    now = time.time()

    def _bucket_idx(ts: float) -> int:
        return int((ts - start) / span) if span > 0 else 0

    lines: List[Dict[str, Any]] = []
    cost_models: List[Dict[str, Any]] = []

    # Durable token/cost series from usage_events (bucketed in Python, like
    # _tokens_section — the raw builder has no .lt(), so the upper bound is a
    # Python filter). Fetch once, only when a durable series is requested.
    need_usage = bool(want & {"tokens_in", "tokens_out", "cost", "llm_calls"})
    if need_usage:
        tin = [0.0] * buckets
        tout = [0.0] * buckets
        lcalls = [0.0] * buckets
        per_model: Dict[str, List[float]] = {}
        model_totals: Dict[str, float] = {}
        try:
            rows = await _usage_rows(start)
            for r in rows:
                ts = _to_epoch(r.get("created_at"))
                if ts < start or ts >= end:
                    continue
                bi = _bucket_idx(ts)
                if bi < 0 or bi >= buckets:
                    continue
                tin[bi] += int(r.get("input_tokens") or 0)
                tout[bi] += int(r.get("output_tokens") or 0)
                lcalls[bi] += 1
                cost = float(r.get("cost_usd") or 0.0)
                if cost:
                    mdl = r.get("model") or "unknown"
                    per_model.setdefault(mdl, [0.0] * buckets)[bi] += cost
                    model_totals[mdl] = model_totals.get(mdl, 0.0) + cost
        except Exception as e:
            logger.debug("dashboard chart usage fetch failed: %s", e)

        if "tokens_in" in want:
            lines.append({"id": "tokens_in", "label": "Tokens in", "unit": "tok", "values": [round(v) for v in tin]})
        if "tokens_out" in want:
            lines.append({"id": "tokens_out", "label": "Tokens out", "unit": "tok", "values": [round(v) for v in tout]})
        if "llm_calls" in want:
            lines.append({"id": "llm_calls", "label": "LLM calls", "unit": "calls", "values": [round(v) for v in lcalls]})
        if "cost" in want:
            top = sorted(model_totals.items(), key=lambda kv: kv[1], reverse=True)
            keep = {m for m, _ in top[:_CHART_MAX_MODELS]}
            other = [0.0] * buckets
            for mdl, vals in per_model.items():
                if mdl in keep:
                    cost_models.append({"model": mdl, "values": [round(v, 4) for v in vals]})
                else:
                    for i, v in enumerate(vals):
                        other[i] += v
            if any(other):
                cost_models.append({"model": "Other", "values": [round(v, 4) for v in other]})
            # Preserve cost order (largest total first) for stable stacking/colours.
            order = {m: i for i, (m, _) in enumerate(top)}
            cost_models.sort(key=lambda cm: order.get(cm["model"], 10_000))

    # Durable error series from the diagnostics store (count per bucket).
    if "errors" in want:
        errs = [0.0] * buckets
        try:
            from app.db import get_db
            db = get_db()
            from app.db.offload import db_offload
            rows = await db_offload(lambda: db.query_diagnostics(
                levels=["error", "critical"], since=_iso_from_epoch(start), limit=2000)) or []
            for r in rows:
                ts = _to_epoch(r.get("created_at") or r.get("ts"))
                bi = _bucket_idx(ts)
                if start <= ts < end and 0 <= bi < buckets:
                    errs[bi] += 1
        except Exception as e:
            logger.debug("dashboard chart errors fetch failed: %s", e)
        lines.append({"id": "errors", "label": "Errors", "unit": "calls", "values": [round(v) for v in errs]})

    # Live series (CPU / RAM / active runs / DB latency) from the in-memory rings.
    # Only meaningful up to "now", so for a past-ending custom range they come
    # back empty (live-only, by design).
    for sid, meta in _CHART_LINE_META.items():
        if meta.get("src") != "live" or sid not in want:
            continue
        if end >= now - span:
            pts = metrics.timeseries(meta["kind"], buckets=buckets, window_s=max(now - start, span))
            values = [p.get("avg_ms") or 0.0 for p in pts][:buckets]
            values += [0.0] * (buckets - len(values))
        else:
            values = [0.0] * buckets
        lines.append({"id": sid, "label": meta["label"], "unit": meta["unit"], "values": values})

    return {
        "start": int(start),
        "end": int(end),
        "buckets": [int(start + (b + 1) * span) for b in range(buckets)],
        "lines": lines,
        "cost_models": cost_models,
    }


# ── admin actions (the operational cards' buttons) ──────────────────────────
class StopRunBody(BaseModel):
    requesting_user_id: str = ""
    session_id: str = ""


@router.post("/runs/stop")
async def stop_run(body: StopRunBody):
    """Gracefully interrupt a running agent session from the Sessions & runs
    monitor card (admin Stop button). Same mechanism as the chat Stop button
    (run_manager.interrupt), but admin-gated so an admin can stop ANY user's
    stuck or runaway run."""
    await _require_admin(body.requesting_user_id)
    sid = (body.session_id or "").strip()
    if not sid:
        raise HTTPException(status_code=400, detail="session_id required")
    try:
        from app.agent.run_manager import get_run_manager
        from app.db import get_db
        was_running = await get_run_manager().interrupt(sid, get_db())
        return {"ok": True, "was_running": bool(was_running)}
    except Exception as e:
        logger.warning("dashboard stop-run failed: %s", e)
        raise HTTPException(status_code=500, detail="Could not stop the run")


# ── per-user layout ─────────────────────────────────────────────────────────
class LayoutBody(BaseModel):
    requesting_user_id: str = ""
    cards: List[Dict[str, Any]] = []


def _load_layouts() -> Dict[str, Any]:
    from app.util.config_io import read_json
    data = read_json(_LAYOUT_FILE, {})
    return data if isinstance(data, dict) else {}


@router.get("/layout")
async def get_layout(requesting_user_id: str = Query("")):
    await _require_admin(requesting_user_id)
    layouts = _load_layouts()
    entry = layouts.get(requesting_user_id or "_")
    if isinstance(entry, dict) and isinstance(entry.get("cards"), list):
        return {"cards": entry["cards"], "default": False}
    return {"cards": _default_cards(), "default": True}


@router.put("/layout")
async def put_layout(body: LayoutBody):
    await _require_admin(body.requesting_user_id)
    from app.util.config_io import read_json, safe_write_json
    layouts = read_json(_LAYOUT_FILE, {})
    if not isinstance(layouts, dict):
        layouts = {}
    layouts[body.requesting_user_id or "_"] = {"cards": body.cards, "saved_at": int(time.time())}
    safe_write_json(_LAYOUT_FILE, layouts)
    return {"ok": True, "count": len(body.cards)}


class ResetBody(BaseModel):
    requesting_user_id: str = ""


@router.post("/layout/reset")
async def reset_layout(body: ResetBody):
    """Drop this admin's saved arrangement so the grid returns to the seed layout.
    Returns the default cards so the caller can render them without a second fetch."""
    await _require_admin(body.requesting_user_id)
    from app.util.config_io import read_json, safe_write_json
    layouts = read_json(_LAYOUT_FILE, {})
    if isinstance(layouts, dict) and (body.requesting_user_id or "_") in layouts:
        layouts.pop(body.requesting_user_id or "_", None)
        safe_write_json(_LAYOUT_FILE, layouts)
    return {"cards": _default_cards(), "default": True}


@router.post("/layout/default")
async def save_as_default(body: LayoutBody):
    """Make the posted arrangement the NEW default for everyone — the cards a fresh
    admin, or anyone who hits "reset to default", will get. Writes the runtime
    override (data/config/dashboard-default.json) so the bundled factory seed
    (app/defaults/dashboard.json) stays pristine."""
    await _require_admin(body.requesting_user_id)
    from app.util.config_io import safe_write_json
    from app.util.paths import dashboard_default_override_path
    cards = body.cards if isinstance(body.cards, list) else []
    safe_write_json(dashboard_default_override_path(), {"version": 1, "cards": cards})
    return {"ok": True, "count": len(cards)}


# The seed layout a fresh admin sees comes from a JSON file (like the default
# agent template) — the bundled app/defaults/dashboard.json, or a runtime override
# an admin saved via "Save as default" (data/config/dashboard-default.json), which
# wins. Resolved through app.util.paths so relocation is a one-file edit. A tiny
# in-code list is kept ONLY as a last-ditch fallback if the JSON is missing/corrupt
# so the dashboard never comes up empty.
_FALLBACK_CARDS: List[Dict[str, Any]] = [
    {"id": "c-chart", "type": "metric_chart", "x": 0, "y": 0, "w": 12, "h": 5,
     "series": ["tokens_in", "tokens_out", "cost"], "range": {"window": 3600}},
    {"id": "c-dbmode", "type": "db_mode", "x": 0, "y": 5, "w": 3, "h": 2},
    {"id": "c-dblat", "type": "db_latency", "x": 3, "y": 5, "w": 6, "h": 3},
    {"id": "c-add", "type": "add_card", "x": 0, "y": 8, "w": 4, "h": 3},
]


def _default_cards() -> List[Dict[str, Any]]:
    """Load the current default layout (override-or-bundled JSON). Read fresh each
    call so a just-saved "Save as default" takes effect immediately, with no server
    restart. Falls back to the in-code list if the file is unreadable."""
    try:
        from app.util.config_io import read_json
        from app.util.paths import dashboard_default_path
        data = read_json(dashboard_default_path(), {})
        cards = data.get("cards") if isinstance(data, dict) else None
        if isinstance(cards, list) and cards:
            return cards
    except Exception as e:
        logger.debug("dashboard default-cards load failed, using fallback: %s", e)
    return _FALLBACK_CARDS


# ── AI card generation ──────────────────────────────────────────────────────
class AiCardBody(BaseModel):
    requesting_user_id: str = ""
    prompt: str = ""


# The metric fields an AI-generated custom card may reference — the language the
# LLM is allowed to speak. Keep in sync with the frontend renderer's FIELD map.
# Plugin sections (cards/<id>/server.py → build_section) may be listed here so
# AI cards can read them too.
_AI_FIELDS = [
    "live.cpu_percent", "memory_mb", "active_runs",
    "live.db.avg_ms", "live.db.p95_ms", "live.db.rate_per_min", "live.db.calls_total",
    "live.llm.rate_per_min", "live.llm.tokens_per_min", "live.llm.calls_total",
    "live.loop_split.llm_pct", "live.loop_split.db_pct", "live.uptime_s",
    "tokens.in", "tokens.out", "tokens.cost_usd", "tokens.calls",
    "db_health.actual", "db_health.degraded", "db_health.provider", "db_health.host", "db_health.hybrid", "failures.count",
    "devices", "storage.disk_free_gb", "context.max_input",
    "agents.total", "users.total", "users.admins", "users.pending", "users.new_in_window",
    "sessions.active", "tool_usage.tool_calls", "tool_usage.tool_failures",
    "security.signins", "security.failed",
]


@router.post("/ai-card")
async def ai_card(body: AiCardBody):
    await _require_admin(body.requesting_user_id)
    prompt = (body.prompt or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="Describe the card you want")
    try:
        from app.agent.suggestions import _resolve_default_llm
        from openai import AsyncOpenAI
    except Exception:
        raise HTTPException(status_code=503, detail="AI is not configured")

    llm = await _resolve_default_llm(body.requesting_user_id)
    if not llm.get("api_key"):
        raise HTTPException(status_code=503, detail="No LLM key configured")

    sys = (
        "You design a single metric card for a self-hosted AI-agent admin dashboard. "
        "Return ONLY compact JSON, no prose. Shape: "
        '{"title": str, "icon": <lucide-icon-name>, "viz": "stat"|"list"|"bars", '
        '"unit": str, "fields": [ {"label": str, "path": <one of the allowed paths>, '
        '"unit": str} ] }. '
        "viz 'stat' shows 1-2 big numbers; 'list'/'bars' show several field rows. "
        "Choose ONLY from these allowed data paths: " + ", ".join(_AI_FIELDS) + ". "
        "Pick paths that genuinely match the user's request; keep to 1-4 fields."
    )
    try:
        client = AsyncOpenAI(base_url=llm["base_url"], api_key=llm["api_key"], timeout=45.0)
        resp = await client.chat.completions.create(
            model=llm["model"],
            messages=[{"role": "system", "content": sys},
                      {"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=400,
        )
        text = (resp.choices[0].message.content or "").strip()
        if text.startswith("```"):
            text = text.strip("`")
            text = text[text.find("{"):]
        spec = json.loads(text[text.find("{"): text.rfind("}") + 1])
    except Exception as e:
        logger.warning("dashboard ai-card failed: %s", e)
        raise HTTPException(status_code=502, detail="Could not generate a card")

    # Validate: keep only allowed paths.
    fields = []
    for f in (spec.get("fields") or []):
        if isinstance(f, dict) and f.get("path") in _AI_FIELDS:
            fields.append({"label": str(f.get("label") or f["path"]),
                           "path": f["path"], "unit": str(f.get("unit") or "")})
    if not fields:
        raise HTTPException(status_code=422, detail="AI picked no valid metrics — try rephrasing")
    return {
        "type": "custom",
        "title": str(spec.get("title") or prompt[:40]),
        "icon": str(spec.get("icon") or "sparkles"),
        "viz": spec.get("viz") if spec.get("viz") in ("stat", "list", "bars") else "list",
        "fields": fields,
    }



