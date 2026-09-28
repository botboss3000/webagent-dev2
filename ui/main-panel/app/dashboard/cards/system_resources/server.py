"""System resources dashboard card.

Dependency-free and best-effort: every probe returns partial data rather than
breaking the dashboard. Linux reads host totals exposed through procfs/sysfs;
Windows uses the standard Win32 memory/CPU APIs. Inside Docker, CPU, physical
memory, disk and uptime are host-scoped on normal Linux Docker installations,
while ``/proc/net/dev`` belongs to the container network namespace; the payload
labels that distinction so the UI never overstates what it can see.
"""

import asyncio
import ctypes
import glob
import math
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from app.runtime_mode import data_root
from app.util.config_io import read_json, safe_write_json


_LOCK = threading.Lock()
_CPU_PREV: Optional[Tuple[int, int]] = None
_NET_PREV: Optional[Tuple[float, int, int]] = None
_IS_WINDOWS = sys.platform == "win32"
_IS_LINUX = sys.platform.startswith("linux")
_IN_CONTAINER = Path("/.dockerenv").exists() or bool(os.environ.get("container"))
_BASELINE_PATH = data_root() / "config" / "system-resources-baseline.json"
_BASELINE_LOCK = threading.Lock()


def _idle_baseline(signals: Dict[str, Optional[float]], *, capture: bool) -> Dict[str, Any]:
    """Load this server's idle floor and update it only with zero-workload samples."""
    try:
        saved = read_json(_BASELINE_PATH, {})
        baseline = saved if isinstance(saved, dict) else {}
    except Exception:
        baseline = {}
    values = baseline.get("signals") if isinstance(baseline.get("signals"), dict) else {}
    values = {key: value for key, value in values.items() if isinstance(value, (int, float))}
    changed = False

    if capture:
        for key, value in signals.items():
            if value is None or value < 0:
                continue
            prior = values.get(key)
            if prior is None or value < prior:
                values[key] = round(float(value), 2)
                changed = True
        if changed:
            baseline = {
                "version": 1,
                "signals": values,
                "captured_at": int(time.time()),
                "samples": int(baseline.get("samples") or 0) + 1,
            }
            try:
                with _BASELINE_LOCK:
                    safe_write_json(_BASELINE_PATH, baseline)
            except OSError:
                pass

    return {
        "signals": values,
        "samples": int(baseline.get("samples") or 0),
        "captured_at": baseline.get("captured_at"),
        "capturing": capture,
    }


def _cpu_sample() -> Optional[Tuple[int, int]]:
    if _IS_LINUX:
        try:
            values = [int(v) for v in Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0].split()[1:]]
            idle = values[3] + (values[4] if len(values) > 4 else 0)
            return idle, sum(values)
        except (OSError, ValueError, IndexError):
            return None
    if _IS_WINDOWS:
        try:
            idle = ctypes.c_ulonglong()
            kernel = ctypes.c_ulonglong()
            user = ctypes.c_ulonglong()
            ok = ctypes.windll.kernel32.GetSystemTimes(
                ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
            )
            return (idle.value, kernel.value + user.value) if ok else None
        except Exception:
            return None
    return None


def _cpu_percent() -> Optional[float]:
    global _CPU_PREV
    sample = _cpu_sample()
    if sample is None:
        return None
    with _LOCK:
        previous = _CPU_PREV
        _CPU_PREV = sample
    if previous is None:
        return None
    idle_delta = sample[0] - previous[0]
    total_delta = sample[1] - previous[1]
    if total_delta <= 0:
        return None
    return round(max(0.0, min(100.0, (1.0 - idle_delta / total_delta) * 100.0)), 1)


def _load_average() -> Dict[str, Optional[float]]:
    try:
        one, five, fifteen = os.getloadavg()
        return {"one": round(one, 2), "five": round(five, 2), "fifteen": round(fifteen, 2)}
    except (AttributeError, OSError):
        return {"one": None, "five": None, "fifteen": None}


def _linux_memory() -> Tuple[Dict[str, Any], Dict[str, Any]]:
    values: Dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, _, raw = line.partition(":")
            parts = raw.strip().split()
            if parts:
                values[key] = int(parts[0]) * 1024
    except (OSError, ValueError):
        return {}, {}
    total = values.get("MemTotal") or 0
    available = values.get("MemAvailable", values.get("MemFree", 0))
    used = max(0, total - available)
    swap_total = values.get("SwapTotal") or 0
    swap_free = values.get("SwapFree") or 0
    swap_used = max(0, swap_total - swap_free)
    return (
        {
            "total_bytes": total,
            "available_bytes": available,
            "used_bytes": used,
            "percent": round(used / total * 100.0, 1) if total else None,
        },
        {
            "label": "Swap",
            "total_bytes": swap_total,
            "free_bytes": swap_free,
            "used_bytes": swap_used,
            "percent": round(swap_used / swap_total * 100.0, 1) if swap_total else 0.0,
        },
    )


def _windows_memory() -> Tuple[Dict[str, Any], Dict[str, Any]]:
    class MemoryStatus(ctypes.Structure):
        _fields_ = [
            ("length", ctypes.c_ulong),
            ("memory_load", ctypes.c_ulong),
            ("total_physical", ctypes.c_ulonglong),
            ("available_physical", ctypes.c_ulonglong),
            ("total_pagefile", ctypes.c_ulonglong),
            ("available_pagefile", ctypes.c_ulonglong),
            ("total_virtual", ctypes.c_ulonglong),
            ("available_virtual", ctypes.c_ulonglong),
            ("available_extended_virtual", ctypes.c_ulonglong),
        ]

    try:
        status = MemoryStatus()
        status.length = ctypes.sizeof(MemoryStatus)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return {}, {}
        used = status.total_physical - status.available_physical
        page_used = status.total_pagefile - status.available_pagefile
        return (
            {
                "total_bytes": status.total_physical,
                "available_bytes": status.available_physical,
                "used_bytes": used,
                "percent": float(status.memory_load),
            },
            {
                "label": "Pagefile",
                "total_bytes": max(0, status.total_pagefile - status.total_physical),
                "free_bytes": status.available_pagefile,
                "used_bytes": max(0, page_used - used),
                "percent": round(max(0, page_used - used) / max(1, status.total_pagefile - status.total_physical) * 100.0, 1),
            },
        )
    except Exception:
        return {}, {}


def _memory() -> Tuple[Dict[str, Any], Dict[str, Any]]:
    if _IS_LINUX:
        return _linux_memory()
    if _IS_WINDOWS:
        return _windows_memory()
    return {}, {}


def _disk(path: Path) -> Dict[str, Any]:
    try:
        usage = shutil.disk_usage(str(path))
        used = usage.total - usage.free
        return {
            "path": str(path),
            "total_bytes": usage.total,
            "used_bytes": used,
            "free_bytes": usage.free,
            "percent": round(used / usage.total * 100.0, 1) if usage.total else None,
        }
    except OSError:
        return {}


def _uptime_seconds() -> Optional[int]:
    if _IS_LINUX:
        try:
            return int(float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0]))
        except (OSError, ValueError, IndexError):
            return None
    if _IS_WINDOWS:
        try:
            return int(ctypes.windll.kernel32.GetTickCount64() // 1000)
        except Exception:
            return None
    return None


def _temperature_c() -> Optional[float]:
    if not _IS_LINUX:
        return None
    readings = []
    paths = glob.glob("/sys/class/thermal/thermal_zone*/temp")
    paths += glob.glob("/sys/class/hwmon/hwmon*/temp*_input")
    for raw_path in paths:
        try:
            raw = float(Path(raw_path).read_text(encoding="utf-8").strip())
            value = raw / 1000.0 if raw > 1000 else raw
            if 0.0 < value < 150.0:
                readings.append(value)
        except (OSError, ValueError):
            continue
    return round(max(readings), 1) if readings else None


def _network_totals() -> Optional[Tuple[int, int]]:
    if _IS_LINUX:
        received = transmitted = 0
        try:
            lines = Path("/proc/net/dev").read_text(encoding="utf-8").splitlines()[2:]
            for line in lines:
                name, _, raw = line.partition(":")
                if name.strip() == "lo":
                    continue
                fields = raw.split()
                received += int(fields[0])
                transmitted += int(fields[8])
            return received, transmitted
        except (OSError, ValueError, IndexError):
            return None
    if _IS_WINDOWS:
        # ``netstat -e`` is built into Windows and exposes cumulative interface
        # byte totals without adding psutil to the application's dependencies.
        try:
            result = subprocess.run(
                ["netstat", "-e"], capture_output=True, text=True, timeout=3,
                creationflags=0x08000000,
            )
            for line in (result.stdout or "").splitlines():
                parts = line.split()
                if len(parts) >= 3 and parts[0].lower() == "bytes":
                    return int(parts[1]), int(parts[2])
        except (OSError, subprocess.SubprocessError, ValueError):
            return None
    return None


def _network() -> Dict[str, Any]:
    global _NET_PREV
    totals = _network_totals()
    if totals is None:
        return {"scope": "unavailable"}
    now = time.monotonic()
    with _LOCK:
        previous = _NET_PREV
        _NET_PREV = (now, totals[0], totals[1])
    rx_rate = tx_rate = None
    if previous is not None and now > previous[0]:
        elapsed = now - previous[0]
        rx_rate = max(0.0, (totals[0] - previous[1]) / elapsed)
        tx_rate = max(0.0, (totals[1] - previous[2]) / elapsed)
    return {
        "scope": "webagent-container" if _IN_CONTAINER else "host",
        "received_bytes": totals[0],
        "transmitted_bytes": totals[1],
        "received_bytes_per_s": round(rx_rate, 1) if rx_rate is not None else None,
        "transmitted_bytes_per_s": round(tx_rate, 1) if tx_rate is not None else None,
    }


def _descriptor_status() -> Dict[str, Any]:
    """Return process descriptor utilization where the operating system exposes it."""
    open_count = limit = None
    try:
        open_count = len(os.listdir("/proc/self/fd"))
    except OSError:
        pass
    try:
        import resource
        limit = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
    except (ImportError, OSError, ValueError):
        pass
    percent = round(open_count / limit * 100, 1) if open_count is not None and limit and limit > 0 else None
    return {"open": open_count, "limit": limit, "percent": percent}


async def _event_loop_yield_ms() -> float:
    """Measure one cooperative event-loop yield without adding polling delay."""
    started = time.perf_counter()
    await asyncio.sleep(0)
    return round((time.perf_counter() - started) * 1000.0, 2)


def _capacity_estimate(
    active_users: int, cpu_percent: Optional[float], memory_percent: Optional[float],
    db_p95_ms: Optional[float], descriptor_percent: Optional[float], session_cap: int,
    baseline: Dict[str, Optional[float]],
) -> Dict[str, Any]:
    """Project capacity from per-user load above this server's idle floor."""
    if active_users <= 0:
        return {"estimated_users": None, "limiting_metric": None, "sample_users": 0}

    candidates = []
    projections = []
    scalable = (
        ("CPU", "percent", cpu_percent, baseline.get("cpu_percent"), 100.0),
        ("Memory", "percent", memory_percent, baseline.get("memory_percent"), 100.0),
        ("Open descriptors", "percent", descriptor_percent, baseline.get("descriptor_percent"), 100.0),
        ("Database p95 target", "milliseconds", db_p95_ms, baseline.get("db_p95_ms"), 500.0),
    )
    for label, unit, current, idle_floor, limit in scalable:
        if current is None or idle_floor is None:
            continue
        incremental = max(0.0, float(current) - float(idle_floor))
        if incremental <= 0:
            continue
        projected_users = max(0, math.floor(active_users / (incremental / limit)))
        projection = {
            "metric": label,
            "unit": unit,
            "current": round(float(current), 2),
            "baseline": round(float(idle_floor), 2),
            "incremental": round(incremental, 2),
            "limit": limit,
            "estimated_users": projected_users,
        }
        projections.append(projection)
        candidates.append((projected_users, label))
    if session_cap > 0:
        projections.append({
            "metric": "Chat concurrency cap",
            "unit": "users",
            "current": active_users,
            "baseline": None,
            "incremental": None,
            "limit": session_cap,
            "estimated_users": session_cap,
        })
        candidates.append((session_cap, "Chat concurrency cap"))
    if not candidates:
        return {
            "estimated_users": None,
            "limiting_metric": None,
            "sample_users": active_users,
            "awaiting_baseline": not bool(baseline),
            "projections": projections,
        }
    estimated, limiting_metric = min(candidates, key=lambda candidate: candidate[0])
    return {
        "estimated_users": estimated,
        "limiting_metric": limiting_metric,
        "sample_users": active_users,
        "awaiting_baseline": False,
        "projections": projections,
    }


async def build_section(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Return one live, dependency-free host resource snapshot."""
    project_root = Path(ctx.get("project_root") or Path.cwd())
    memory, swap = _memory()
    cpu_percent = _cpu_percent()
    active_runs = len(ctx.get("run_rows") or [])
    holders = queued = session_cap = 0
    browser: Dict[str, Any] = {}
    connections: Dict[str, Any] = {}
    scheduler: Dict[str, Any] = {}
    try:
        from app.agent.session_gate import stats
        holders, queued = stats()
    except Exception:
        pass
    try:
        from app.admin.settings import get_max_active_sessions
        session_cap = int(get_max_active_sessions() or 0)
    except Exception:
        pass
    try:
        from app.tools.browser import resource_status
        browser = resource_status()
    except Exception:
        pass
    try:
        from app.api.chat import user_listener_stats
        connections = user_listener_stats()
    except Exception:
        pass
    try:
        from app.scheduler import get_scheduler
        scheduler = await get_scheduler().get_status()
    except Exception:
        pass

    live = (ctx.get("snapshot") or {}).get("live") or {}
    database = live.get("db") or {}
    descriptors = _descriptor_status()
    active_users = max(active_runs, int(connections.get("users") or 0))
    idle_baseline = _idle_baseline(
        {
            "cpu_percent": cpu_percent,
            "memory_percent": memory.get("percent"),
            "db_p95_ms": database.get("p95_ms"),
            "descriptor_percent": descriptors.get("percent"),
        },
        capture=active_users == 0,
    )
    capacity = _capacity_estimate(
        active_users, cpu_percent, memory.get("percent"), database.get("p95_ms"),
        descriptors.get("percent"), session_cap, idle_baseline["signals"],
    )
    return {
        "generated_at": int(time.time()),
        "platform": platform.system() or sys.platform,
        "containerized": _IN_CONTAINER,
        "cpu": {"percent": cpu_percent, "logical_cores": os.cpu_count(), "load": _load_average()},
        "memory": memory,
        "swap": swap,
        "disk": _disk(project_root),
        "network": _network(),
        "workload": {
            "active_chat_runs": active_runs,
            "holders": holders,
            "queued": queued,
            "session_cap": session_cap,
            "active_chromium_sessions": int(browser.get("active_sessions") or 0),
            "live_chromium_sessions": int(browser.get("headless_sessions") or 0),
            "attached_chromium_sessions": int(browser.get("attached_sessions") or 0),
            "database": database,
            "llm": live.get("llm") or {},
            "event_loop_yield_ms": await _event_loop_yield_ms(),
            "connections": connections,
            "scheduler": scheduler,
            "descriptors": descriptors,
            "idle_baseline": idle_baseline,
            "capacity": capacity,
        },
        "uptime_s": _uptime_seconds(),
        "temperature_c": _temperature_c(),
    }
