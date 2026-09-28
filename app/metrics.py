"""In-memory runtime metrics — the live pulse behind the admin Dashboard.

WHY THIS EXISTS
---------------
The admin Dashboard shows live health (CPU, DB latency, loop throughput). If it
read those from the database on a timer it would add the very DB load it is meant
to diagnose — ironic and self-defeating. So this module keeps a tiny, bounded,
in-memory record of runtime activity that the Dashboard's ``/admin/dashboard/
metrics`` endpoint reads with ZERO database round-trips.

WHAT IT RECORDS
---------------
* DB call latency — every ``db_offload`` round-trip's duration (app/db/offload.py),
  so we can surface avg / p95 latency and how DB slowness trends over time (the
  app's documented ongoing pain point).
* LLM calls — each model call's wall time + tokens in/out (app/agent/loop.py at
  ``llm_call_end``), giving loop throughput (calls & tokens per minute) and a
  cheap "where does the loop spend time" split (LLM vs DB).
* CPU — process CPU utilisation, sampled on demand (no background thread), from
  the stdlib only (``time.process_time``) so there is no psutil dependency.

DESIGN
------
Everything is a bounded ``deque`` (ring) plus a few running totals, guarded by one
lock. Writes come from BOTH worker threads (db_offload) and the main loop, so the
lock is required, but each op is O(1) and microseconds long. Lost on restart by
design — this is a live gauge, not durable history (durable history lives in the
``diagnostics`` / ``usage_events`` tables, which the Dashboard's heavier cards read
directly, cached).
"""
from __future__ import annotations

import collections
import os
import threading
import time
from typing import Any, Deque, Dict, List, Tuple

_LOCK = threading.Lock()
_MAX_SAMPLES = 1200  # ~ generous rolling window; each sample is tiny

# ── DB latency ring ─────────────────────────────────────────────────────────
_db_samples: Deque[Tuple[float, float]] = collections.deque(maxlen=_MAX_SAMPLES)  # (ts, ms)
_db_total_calls = 0
_db_total_ms = 0.0

# ── LLM call ring ───────────────────────────────────────────────────────────
_llm_samples: Deque[Tuple[float, float, int, int]] = collections.deque(maxlen=_MAX_SAMPLES)  # (ts, ms, in, out)
_llm_total_calls = 0
_llm_total_ms = 0.0

# ── Cache hit / miss counters ──────────────────────────────────────────────────
_cache_hits: Dict[str, int] = {}
_cache_misses: Dict[str, int] = {}

# ── CPU sampler state (on-demand, no background thread) ─────────────────────
_cpu_last_proc: float | None = None
_cpu_last_wall: float | None = None

# ── System (CPU% + RAM MB + active runs) history ring ───────────────────────
# CPU, memory and the active-run count have NO durable store (by design — see
# module docstring). The admin Dashboard's over-time chart needs a live trend
# for them, so we keep a small in-memory ring that the /admin/dashboard/metrics
# poll fills on each hit (record_system). Live-only: empty at boot, lost on restart.
_sys_samples: Deque[Tuple[float, float, float, float]] = collections.deque(maxlen=_MAX_SAMPLES)  # (ts, cpu%, mem_mb, active_runs)

# Process start — for an uptime card.
_STARTED_AT = time.time()


def uptime_seconds() -> int:
    """Process uptime in whole seconds since boot (module import)."""
    return int(time.time() - _STARTED_AT)


def record_db_latency(ms: float) -> None:
    """Record one DB round-trip duration (called from app/db/offload.py)."""
    global _db_total_calls, _db_total_ms
    try:
        with _LOCK:
            _db_samples.append((time.time(), float(ms)))
            _db_total_calls += 1
            _db_total_ms += float(ms)
    except Exception:
        pass  # metrics must never break the hot path


def record_llm(ms: float, in_tokens: int = 0, out_tokens: int = 0) -> None:
    """Record one LLM call (called from app/agent/loop.py at llm_call_end)."""
    global _llm_total_calls, _llm_total_ms
    try:
        with _LOCK:
            _llm_samples.append((time.time(), float(ms), int(in_tokens or 0), int(out_tokens or 0)))
            _llm_total_calls += 1
            _llm_total_ms += float(ms)
    except Exception:
        pass


def record_cache_hit(cache_name: str = "session_messages") -> None:
    """Increment the hit counter for *cache_name*."""
    try:
        with _LOCK:
            _cache_hits[cache_name] = _cache_hits.get(cache_name, 0) + 1
    except Exception:
        pass


def record_cache_miss(cache_name: str = "session_messages") -> None:
    """Increment the miss counter for *cache_name*."""
    try:
        with _LOCK:
            _cache_misses[cache_name] = _cache_misses.get(cache_name, 0) + 1
    except Exception:
        pass


def record_system(cpu_pct: float, mem_mb: float, active_runs: float = 0.0) -> None:
    """Record one CPU%/RAM/active-run sample for the Dashboard's over-time chart.
    Called from the dashboard metrics poll (app-side, DB-free). Best-effort —
    never raises."""
    try:
        with _LOCK:
            _sys_samples.append((time.time(), float(cpu_pct or 0.0), float(mem_mb or 0.0),
                                 float(active_runs or 0.0)))
    except Exception:
        pass


def _percentile(values: List[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * (pct / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    frac = k - lo
    return s[lo] + (s[hi] - s[lo]) * frac


def cpu_percent() -> float:
    """Process CPU utilisation since the previous call, normalised to 0–100% of
    total machine capacity. Stdlib only: (Δ process CPU time / Δ wall time) /
    core count. First call seeds the baseline and returns 0."""
    global _cpu_last_proc, _cpu_last_wall
    now_proc = time.process_time()
    now_wall = time.perf_counter()
    with _LOCK:
        last_proc, last_wall = _cpu_last_proc, _cpu_last_wall
        _cpu_last_proc, _cpu_last_wall = now_proc, now_wall
    if last_proc is None or last_wall is None:
        return 0.0
    d_wall = now_wall - last_wall
    if d_wall <= 0:
        return 0.0
    d_proc = now_proc - last_proc
    ncpu = os.cpu_count() or 1
    return max(0.0, min(100.0, (d_proc / d_wall) * 100.0 / ncpu))


def _rate_per_min(samples: Deque, window_s: float) -> float:
    cutoff = time.time() - window_s
    n = sum(1 for s in samples if s[0] >= cutoff)
    minutes = max(window_s / 60.0, 1e-9)
    return n / minutes


def snapshot(window_s: float = 300.0) -> Dict[str, Any]:
    """A cheap, DB-free view of live runtime activity over the last ``window_s``.

    Returns db-latency stats, LLM throughput/token rates, a loop time split
    (LLM vs DB), CPU%, and uptime. Heavier, durable metrics (token totals,
    failures, device list) are composed by the Dashboard backend on top of this."""
    cutoff = time.time() - window_s
    with _LOCK:
        db_win = [ms for (ts, ms) in _db_samples if ts >= cutoff]
        llm_win = [(ts, ms, i, o) for (ts, ms, i, o) in _llm_samples if ts >= cutoff]
        db_total_calls = _db_total_calls
        db_total_ms = _db_total_ms
        llm_total_calls = _llm_total_calls
        db_samples_copy = collections.deque(_db_samples)
        llm_samples_copy = collections.deque(_llm_samples)

    db_win_ms_sum = sum(db_win)
    llm_win_ms_sum = sum(ms for (_ts, ms, _i, _o) in llm_win)
    tokens_in_win = sum(i for (_ts, _ms, i, _o) in llm_win)
    tokens_out_win = sum(o for (_ts, _ms, _i, o) in llm_win)

    # Loop time split: of the wall time we can attribute, how much is LLM vs DB.
    split_total = db_win_ms_sum + llm_win_ms_sum
    if split_total > 0:
        pct_llm = round(llm_win_ms_sum / split_total * 100, 1)
        pct_db = round(db_win_ms_sum / split_total * 100, 1)
    else:
        pct_llm = pct_db = 0.0

    return {
        "window_s": window_s,
        "cpu_percent": round(cpu_percent(), 1),
        "uptime_s": int(time.time() - _STARTED_AT),
        "db": {
            "calls_window": len(db_win),
            "calls_total": db_total_calls,
            "avg_ms": round(db_win_ms_sum / len(db_win), 1) if db_win else 0.0,
            "p50_ms": round(_percentile(db_win, 50), 1),
            "p95_ms": round(_percentile(db_win, 95), 1),
            "max_ms": round(max(db_win), 1) if db_win else 0.0,
            "lifetime_avg_ms": round(db_total_ms / db_total_calls, 1) if db_total_calls else 0.0,
            "rate_per_min": round(_rate_per_min(db_samples_copy, window_s), 1),
        },
        "llm": {
            "calls_window": len(llm_win),
            "calls_total": llm_total_calls,
            "avg_ms": round(llm_win_ms_sum / len(llm_win), 1) if llm_win else 0.0,
            "rate_per_min": round(_rate_per_min(llm_samples_copy, window_s), 1),
            "tokens_in_window": tokens_in_win,
            "tokens_out_window": tokens_out_win,
            "tokens_per_min": round((tokens_in_win + tokens_out_win) / max(window_s / 60.0, 1e-9), 0),
        },
        "cache": {
            name: {
                "hits": _cache_hits.get(name, 0),
                "misses": _cache_misses.get(name, 0),
                "hit_rate": round(
                    _cache_hits.get(name, 0) / max(_cache_hits.get(name, 0) + _cache_misses.get(name, 0), 1) * 100, 1
                ),
            }
            for name in sorted(set(list(_cache_hits.keys()) + list(_cache_misses.keys())))
        },
        "loop_split": {"llm_pct": pct_llm, "db_pct": pct_db},
    }


def timeseries(kind: str, buckets: int = 30, window_s: float = 1800.0) -> List[Dict[str, Any]]:
    """Bucketed history for a line card. ``kind`` is 'db', 'db_p95', 'llm', 'cpu',
    'ram' or 'runs'. Returns ``buckets`` evenly-spaced points over the last
    ``window_s`` with the aggregated value and sample count in each bucket. For
    'db'/'llm' the value is average latency (``avg_ms``), 'db_p95' the per-bucket
    p95 latency; 'cpu'/'ram'/'runs' average the sampled CPU%/RAM MB/active runs."""
    now = time.time()
    start = now - window_s
    span = window_s / buckets
    with _LOCK:
        if kind in ("db", "db_p95"):
            src = [(ts, ms) for (ts, ms) in _db_samples if ts >= start]
        elif kind == "cpu":
            src = [(ts, cpu) for (ts, cpu, _mem, _r) in _sys_samples if ts >= start]
        elif kind == "ram":
            src = [(ts, mem) for (ts, _cpu, mem, _r) in _sys_samples if ts >= start]
        elif kind == "runs":
            src = [(ts, runs) for (ts, _cpu, _mem, runs) in _sys_samples if ts >= start]
        else:
            src = [(ts, ms) for (ts, ms, _i, _o) in _llm_samples if ts >= start]
    out: List[Dict[str, Any]] = []
    for b in range(buckets):
        b0 = start + b * span
        b1 = b0 + span
        vals = [v for (ts, v) in src if b0 <= ts < b1]
        if not vals:
            agg = 0.0
        elif kind == "db_p95":
            agg = _percentile(vals, 95)
        else:
            agg = sum(vals) / len(vals)
        out.append({
            "t": int(b1),
            "avg_ms": round(agg, 1),
            "count": len(vals),
        })
    return out
