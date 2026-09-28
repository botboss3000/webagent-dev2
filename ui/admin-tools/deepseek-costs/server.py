"""DeepSeek cost calculator — admin-only drop-in backend.

Drop-in BACKEND for the "DeepSeek Costs" admin view. Discovered + mounted by the
page catalog (app/ui_pages.py discover_routers, via this folder's page.json
``router`` field) — so the API comes and goes with the folder, with NO edit to
app/main.py. Frontend: ui/admin-tools/deepseek-costs/deepseek-costs.js.

This page is a REPORTING tool, deliberately walled off from the rest of the app:
  * It recomputes per-call cost from the RAW token columns already stored in
    ``usage_events`` (cache hit/miss/write split + output + created_at), using an
    operator-tweakable rate table plus a peak/off-peak multiplier.
  * It NEVER writes to ``usage_events``, the model catalog, or any wallet — the
    recomputed figures exist only in this page's own responses and are shown
    nowhere else. Every other surface in the app keeps its own numbers.

Endpoints:
  GET  /api/v1/deepseek-costs/config   — the effective rate/peak config
  POST /api/v1/deepseek-costs/config   — save the config (admin-only)
  GET  /api/v1/deepseek-costs/report   — per-user token + cost roll-up (admin-only)

Rates are USD per 1M tokens. ``output`` already includes reasoning tokens in
DeepSeek's usage accounting (completion_tokens is the total), so reasoning is
deliberately NOT added on top — no double count. Peak windows are half-open
[start, end) hours in UTC, e.g. [1, 4) covers 01:00–03:59.

REMOVE-WHEN: the DeepSeek Costs view is dropped from the admin page catalog.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request

from app.api.github import _require_admin
from app.db import get_app_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/deepseek-costs", tags=["admin"])

# ui/admin-tools/deepseek-costs/server.py → repo root
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
CONFIG_FILE = _PROJECT_ROOT / "data" / "config" / "deepseek-costs.json"

# DeepSeek published rates (post 2026-08-16 16:00 UTC regime). Off-peak is the
# base; peak = base × multiplier. These are editable on the page and only affect
# THIS page's numbers.
DEFAULT_CONFIG = {
    "models": {
        "deepseek-v4-flash": {
            "cache_hit": 0.007,      # $ / 1M input tokens, cache hit
            "cache_miss": 0.22,      # $ / 1M input tokens, cache miss
            "cache_write": 0.0,      # $ / 1M tokens written to cache
            "output": 0.66,          # $ / 1M output tokens
        },
        "deepseek-v4-pro": {
            "cache_hit": 0.022,
            "cache_miss": 0.66,
            "cache_write": 0.0,
            "output": 1.98,
        },
    },
    "peak": {
        "enabled": True,
        "multiplier": 2.0,
        # UTC half-open [start, end) hour ranges: 01:00–04:00 and 06:00–10:00.
        "windows": [[1, 4], [6, 10]],
    },
}


# ── Config persistence ────────────────────────────────────────────────────────

def _load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    try:
        if CONFIG_FILE.is_file():
            saved = json.loads(CONFIG_FILE.read_text(encoding="utf-8")) or {}
            if isinstance(saved.get("models"), dict):
                cfg["models"].update(saved["models"])
            if isinstance(saved.get("peak"), dict):
                cfg["peak"].update(saved["peak"])
    except Exception as e:
        logger.warning("deepseek-costs: could not read config: %s", e)
    return cfg


def _save_config(cfg: dict) -> None:
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


# ── Cost math ─────────────────────────────────────────────────────────────────

def _match_model(model: str, rates: dict) -> Optional[str]:
    """Leniently map a usage row's model string onto a configured rate key."""
    m = (model or "").strip().lower()
    if not m:
        return None
    best_key, best_score = None, -1
    for key in rates:
        k = key.lower()
        if m == k:
            score = 4
        elif m.endswith("/" + k):
            score = 3
        elif m.rsplit("/", 1)[-1] == k:
            score = 2
        elif k in m:
            score = 1
        else:
            continue
        if score > best_score:
            best_key, best_score = key, score
    return best_key


def _tokens(row: dict) -> tuple:
    """Return (total_in, cached, uncached, cache_write, output) for one row."""
    total_in = max(0, int(row.get("input_tokens") or 0))
    cached = max(0, int(row.get("cached_input_tokens") or 0))
    write = max(0, int(row.get("cache_write_tokens") or 0))
    uncached_raw = row.get("uncached_input_tokens")
    if uncached_raw is not None:
        uncached = max(0, int(uncached_raw or 0))
    else:
        uncached = max(0, total_in - cached)
    out = max(0, int(row.get("output_tokens") or 0))
    return total_in, cached, uncached, write, out


def _parse_ts(created_at: str) -> Optional[datetime]:
    if not created_at:
        return None
    try:
        return datetime.fromisoformat(str(created_at))
    except Exception:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(str(created_at), fmt)
        except Exception:
            continue
    return None


def _in_peak(ts: Optional[datetime], windows: list) -> bool:
    if ts is None:
        return False
    h = ts.hour
    for w in windows or []:
        if not (isinstance(w, (list, tuple)) and len(w) == 2):
            continue
        try:
            s, e = int(w[0]), int(w[1])
        except (TypeError, ValueError):
            continue
        if s == e:
            continue
        if e > s:
            if s <= h < e:
                return True
        else:  # window wraps midnight
            if h >= s or h < e:
                return True
    return False


def _row_cost(row: dict, rates: dict, peak: dict) -> tuple:
    """Return (cost_usd, matched_rate_key) for one usage row, or (None, None)."""
    key = _match_model(row.get("model"), rates)
    if key is None:
        return None, None
    r = rates[key]
    _in, cached, uncached, write, out = _tokens(row)
    base = (uncached * float(r.get("cache_miss", 0))
            + cached * float(r.get("cache_hit", 0))
            + write * float(r.get("cache_write", 0))
            + out * float(r.get("output", 0))) / 1_000_000.0
    if peak.get("enabled") and _in_peak(_parse_ts(row.get("created_at")), peak.get("windows")):
        base *= float(peak.get("multiplier") or 1.0)
    return round(base, 8), key


def _normalize_date(raw: Optional[str], time_part: str, fallback: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return fallback
    if len(raw) == 10:  # YYYY-MM-DD
        return f"{raw} {time_part}"
    return raw


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/config")
async def get_config(request: Request):
    _require_admin(request)
    return _load_config()


@router.post("/config")
async def set_config(request: Request):
    _require_admin(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    models = body.get("models")
    if not isinstance(models, dict) or not models:
        raise HTTPException(status_code=400, detail="models must be a non-empty object")

    cleaned_models: dict = {}
    for raw_key, m in models.items():
        if not isinstance(m, dict):
            continue
        key = str(raw_key).strip()
        if not key:
            continue
        try:
            cleaned_models[key] = {
                "cache_hit": float(m.get("cache_hit", 0)),
                "cache_miss": float(m.get("cache_miss", 0)),
                "cache_write": float(m.get("cache_write", 0)),
                "output": float(m.get("output", 0)),
            }
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"model '{key}' rates must be numbers")
    if not cleaned_models:
        raise HTTPException(status_code=400, detail="at least one valid model is required")

    cfg = _load_config()
    peak = body.get("peak") if isinstance(body.get("peak"), dict) else {}
    raw_windows = peak.get("windows", cfg["peak"].get("windows", []))
    cleaned_windows: list = []
    for w in raw_windows:
        if isinstance(w, (list, tuple)) and len(w) == 2:
            try:
                s, e = int(w[0]), int(w[1])
                if 0 <= s <= 23 and 0 <= e <= 23:
                    cleaned_windows.append([s, e])
            except (TypeError, ValueError):
                continue
    try:
        mult = float(peak.get("multiplier", cfg["peak"].get("multiplier", 1.0)))
    except (TypeError, ValueError):
        mult = 1.0
    mult = max(0.0, min(mult, 100.0))

    new_cfg = {
        "models": cleaned_models,
        "peak": {
            "enabled": bool(peak.get("enabled", cfg["peak"].get("enabled", True))),
            "multiplier": mult,
            "windows": cleaned_windows,
        },
    }
    _save_config(new_cfg)
    return new_cfg


@router.get("/report")
async def report(
    request: Request,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = Query(None),
):
    _require_admin(request)
    cfg = _load_config()
    rates = cfg["models"]
    peak = cfg["peak"]

    from_ts = _normalize_date(from_, "00:00:00", "1970-01-01 00:00:00")
    to_ts = _normalize_date(to, "23:59:59", "9999-12-31 23:59:59")

    db = get_app_db()
    rows: list = []
    if hasattr(db, "_get_conn"):
        conn = db._get_conn()
        try:
            cur = conn.execute(
                "SELECT user_id, model, input_tokens, output_tokens, "
                "cached_input_tokens, cache_write_tokens, uncached_input_tokens, "
                "created_at FROM usage_events "
                "WHERE created_at >= ? AND created_at <= ? "
                "ORDER BY user_id, created_at",
                (from_ts, to_ts),
            )
            rows = [{k: r[k] for k in r.keys()} for r in cur.fetchall()]
        finally:
            conn.close()

    # ── Aggregate per user + per model ──
    per_user: dict = {}
    unconfigured_models: dict = {}
    totals = {
        "calls": 0, "input_tokens": 0, "output_tokens": 0,
        "cached_input_tokens": 0, "uncached_input_tokens": 0,
        "cache_write_tokens": 0, "cost_usd": 0.0, "unconfigured_calls": 0,
    }

    for r in rows:
        uid = r["user_id"] or "system"
        u = per_user.setdefault(uid, {
            "user_id": uid, "display_name": uid, "calls": 0,
            "input_tokens": 0, "output_tokens": 0,
            "cached_input_tokens": 0, "uncached_input_tokens": 0,
            "cache_write_tokens": 0, "cost_usd": 0.0,
            "unconfigured_calls": 0, "models": {},
        })
        cost, key = _row_cost(r, rates, peak)
        _in, cached, uncached, write, out = _tokens(r)

        u["calls"] += 1
        u["input_tokens"] += _in
        u["output_tokens"] += out
        u["cached_input_tokens"] += cached
        u["uncached_input_tokens"] += uncached
        u["cache_write_tokens"] += write
        totals["calls"] += 1
        totals["input_tokens"] += _in
        totals["output_tokens"] += out
        totals["cached_input_tokens"] += cached
        totals["uncached_input_tokens"] += uncached
        totals["cache_write_tokens"] += write

        if key is None:
            u["unconfigured_calls"] += 1
            totals["unconfigured_calls"] += 1
            model = r["model"] or "(unknown)"
            unconfigured_models[model] = unconfigured_models.get(model, 0) + 1
            continue

        u["cost_usd"] = round(u["cost_usd"] + cost, 8)
        totals["cost_usd"] = round(totals["cost_usd"] + cost, 8)
        m = u["models"].setdefault(key, {
            "model": key, "calls": 0, "input_tokens": 0, "output_tokens": 0,
            "cached_input_tokens": 0, "uncached_input_tokens": 0,
            "cache_write_tokens": 0, "cost_usd": 0.0,
        })
        m["calls"] += 1
        m["input_tokens"] += _in
        m["output_tokens"] += out
        m["cached_input_tokens"] += cached
        m["uncached_input_tokens"] += uncached
        m["cache_write_tokens"] += write
        m["cost_usd"] = round(m["cost_usd"] + cost, 8)

    # ── Best-effort display-name resolution ──
    names: dict = {}
    uids = list(per_user.keys())
    if uids and hasattr(db, "_get_conn"):
        conn = db._get_conn()
        try:
            ph = ",".join("?" * len(uids))
            for r in conn.execute(
                f"SELECT user_id, display_name FROM channel_identities "
                f"WHERE user_id IN ({ph})",
                uids,
            ).fetchall():
                if r["display_name"]:
                    names[r["user_id"]] = r["display_name"]
        except Exception as e:
            logger.debug("deepseek-costs: name lookup skipped: %s", e)
        finally:
            conn.close()

    users = []
    for uid, u in per_user.items():
        u["display_name"] = names.get(uid, uid)
        u["models"] = sorted(u["models"].values(), key=lambda m: m["cost_usd"], reverse=True)
        users.append(u)
    users.sort(key=lambda u: u["cost_usd"], reverse=True)

    return {
        "from": from_ts,
        "to": to_ts,
        "config": cfg,
        "users": users,
        "unconfigured_models": unconfigured_models,
        "totals": totals,
    }
