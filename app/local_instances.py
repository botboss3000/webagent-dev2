"""Local Instances registry — run SEVERAL WebAgent checkouts side by side.

The whole app is one process serving one repo folder on one port (:8080). This
small, self-contained module lets that one running app also **launch, watch and
stop OTHER WebAgent repo folders as background servers**, each on its OWN port
(8081, 8082, …). A browser then "switches repos" simply by pointing at the other
instance's port — no reverse proxy, no shared state: each instance is a complete
app with its own ``data/`` folder, database, scheduler and WebSocket feed (all
derived from its repo-folder location), so nothing collides.

Two kinds of entry:

- **This app (the hub)** is always present and *synthesized on the fly* (never
  persisted): its folder is the project root and its port is this process's port
  (``WEBAGENT_PORT`` or 8080). It is always "running" (it's us), can't be
  started/stopped/removed here, and is the instance you're looking at right now.
- **Registered instances** live in ``data/config/local-instances.json``
  (gitignored) with their own ``folder`` + ``port``. Each is a separate WebAgent
  checkout on disk.

Why launching is safe. Each sibling is started with ``WEBAGENT_PORT=<its port>``,
and the port-aware ``run.py`` guards + binds only THAT port — so a sibling on
8081 never kills the 8080 hub. As a belt-and-braces guard we REFUSE to launch a
checkout whose ``run.py`` is too old to read ``WEBAGENT_PORT`` (it would fall
back to 8080 and fight the hub); the fix is to update that repo.

Nothing here is wired into the agent core; it is a self-contained admin-tools
extension, mirroring ``app/git_repos.py`` and ``app/production_mirror.py``. Its
caller is the **Deploy card's "Current deployment" list** (``app/api/deploy.py``
→ ``/admin/deploy/instances/*``), which lists this hub + registered sibling
checkouts and starts/stops them. It previously also powered the
**instance-switcher header** on the standalone Admin Dashboard
(``/admin/dashboard/instances/*``, UI ``ui/admin-tools/dashboard/
instances-header.js``); that header and its endpoints were removed when the
Dashboard became a tab inside the Instances view, so the registry is now
reachable only through the Deploy card's wrappers.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

from app.util.config_io import read_json, safe_write_json

logger = logging.getLogger(__name__)

# ── Project + config locations ──────────────────────────────────────────────
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_PATH = _PROJECT_ROOT / "data" / "config" / "local-instances.json"
_LOG_DIR = _PROJECT_ROOT / "data" / "local-instances"

# The hub (this-app) entry's fixed id + label.
BUILTIN_ID = "self"
BUILTIN_LABEL = "This app (hub)"

_IS_WIN = sys.platform == "win32"
_NO_WINDOW = 0x08000000       # CREATE_NO_WINDOW
_NEW_GROUP = 0x00000200       # CREATE_NEW_PROCESS_GROUP
_HEALTH_TIMEOUT = 2.5         # per health probe
_BOOT_WAIT = 45.0             # how long to wait for a freshly-launched sibling to answer
_STOP_WAIT = 12.0             # how long to wait for a stopped sibling's port to free


def hub_port() -> int:
    """This running process's port. WEBAGENT_PORT wins (so a SIBLING launched with
    an explicit port reports its own), then a saved hub port (set from the Deploy
    card's "change current deployment port"), then 8080. This mirrors run.py's own
    _resolve_port, so what the UI shows matches what the process actually binds
    after a restart."""
    raw = (os.environ.get("WEBAGENT_PORT") or "").strip()
    if raw:
        try:
            p = int(raw)
            if 1 <= p <= 65535:
                return p
        except ValueError:
            pass
    sp = saved_hub_port()
    return sp if sp else 8080


def saved_hub_port() -> Optional[int]:
    """The hub port persisted in the registry (data/config/local-instances.json
    ``hub_port``), or None. This is the port the *hub* (this-app, launched with no
    WEBAGENT_PORT) rebinds to on its next start — run.py reads the same key."""
    try:
        v = int((_read_raw().get("hub_port") or 0))
    except (TypeError, ValueError):
        return None
    return v if 1024 <= v <= 65535 else None


# ── Helpers ─────────────────────────────────────────────────────────────────
def _project_root_str() -> str:
    return str(_PROJECT_ROOT).replace("\\", "/")


def _norm_folder(path_str: str) -> str:
    """Absolute, forward-slashed folder path (or '' when blank/unresolvable)."""
    s = (path_str or "").strip().strip('"')
    if not s:
        return ""
    try:
        return str(Path(s).resolve()).replace("\\", "/")
    except Exception:  # noqa: BLE001
        return s.replace("\\", "/")


def _run_py(folder: str) -> Path:
    return Path(folder) / "run.py"


def _is_webagent_checkout(folder: str) -> bool:
    """A folder is a WebAgent checkout if it has both run.py and app/main.py."""
    p = Path(folder)
    return (p / "run.py").is_file() and (p / "app" / "main.py").is_file()


def _run_py_is_port_aware(folder: str) -> bool:
    """True when the folder's run.py reads WEBAGENT_PORT — i.e. it's safe to launch
    on a custom port. An older run.py hard-codes 8080 and would fight the hub, so
    we refuse to launch it until it's updated."""
    try:
        text = _run_py(folder).read_text(encoding="utf-8", errors="ignore")
        return "WEBAGENT_PORT" in text
    except Exception:  # noqa: BLE001
        return False


# ── Port / process helpers (self-contained; mirrors run.py's guard) ─────────
def _port_listeners(port: int) -> List[int]:
    """PIDs with a LISTENING socket on *port* (best-effort, never raises)."""
    me = os.getpid()
    pids: List[int] = []
    try:
        if _IS_WIN:
            out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True,
                                 timeout=10, creationflags=_NO_WINDOW).stdout
            for line in out.splitlines():
                if f":{port}" not in line or "LISTENING" not in line:
                    continue
                parts = line.split()
                if parts and parts[-1].isdigit():
                    pid = int(parts[-1])
                    if pid != me and pid not in pids:
                        pids.append(pid)
        else:
            out = subprocess.run(["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
                                 capture_output=True, text=True, timeout=10).stdout
            for tok in out.split():
                if tok.isdigit() and int(tok) != me and int(tok) not in pids:
                    pids.append(int(tok))
    except (OSError, subprocess.SubprocessError):
        pass
    return pids


def _kill(pid: int) -> None:
    try:
        if _IS_WIN:
            subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"],
                           capture_output=True, creationflags=_NO_WINDOW)
        else:
            import signal
            os.kill(pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass


def _port_open(port: int) -> bool:
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1.0)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    except OSError:
        return False
    finally:
        s.close()


async def _health(port: int) -> bool:
    """True when a WebAgent server on *port* answers /health as healthy."""
    url = f"http://127.0.0.1:{port}/health"
    try:
        import httpx
        async with httpx.AsyncClient(timeout=_HEALTH_TIMEOUT) as client:
            r = await client.get(url)
        return r.status_code < 500 and "status" in (r.text or "")
    except Exception:  # noqa: BLE001
        return False


async def _probe_status(port: int) -> str:
    """'running' (WebAgent answering) | 'busy' (port taken, not WebAgent/booting)
    | 'stopped' (nothing there)."""
    if await _health(port):
        return "running"
    return "busy" if _port_open(port) else "stopped"


# ── Config store (registered instances only — the hub is synthesized) ───────
def _read_raw() -> Dict[str, Any]:
    """The registry file as-is (``{hub_port?, instances}``). Never raises."""
    return read_json(_CONFIG_PATH, {}) or {}


def load_config() -> Dict[str, Any]:
    """Read the registry, normalised. Never raises. Shape:
    ``{"instances": [{id, label, folder, port}]}``."""
    data = _read_raw()
    instances: List[Dict[str, Any]] = []
    seen_ports = set()
    for r in (data.get("instances") or []):
        if not isinstance(r, dict):
            continue
        rid = str(r.get("id") or "").strip()
        folder = _norm_folder(r.get("folder") or "")
        try:
            port = int(r.get("port") or 0)
        except (TypeError, ValueError):
            port = 0
        if not rid or not folder or not (1 <= port <= 65535) or port in seen_ports:
            continue
        seen_ports.add(port)
        instances.append({
            "id": rid,
            "label": (str(r.get("label") or "").strip() or folder.rsplit("/", 1)[-1]),
            "folder": folder,
            "port": port,
        })
    return {"instances": instances}


def _save(cfg: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {"instances": cfg.get("instances") or []}
    hp = saved_hub_port()          # preserve the persisted hub port across edits
    if hp:
        out["hub_port"] = hp
    safe_write_json(_CONFIG_PATH, out)
    return cfg


def set_hub_port(port: int) -> int:
    """Persist the port the *hub* (this-app) should bind on its next start. run.py
    reads the same ``hub_port`` key, so a restart brings the app up on this port.
    The caller triggers the restart (app.relauncher). Rejects a port already taken
    by a registered sibling so two instances can't collide."""
    try:
        port = int(port)
    except (TypeError, ValueError):
        raise ValueError("A port number is required.")
    if not (1024 <= port <= 65535):
        raise ValueError("Pick a port between 1024 and 65535.")
    for r in load_config()["instances"]:
        if r["port"] == port:
            raise ValueError("Another instance already uses that port — pick a different one.")
    safe_write_json(_CONFIG_PATH, {"hub_port": port, "instances": load_config()["instances"]})
    return port


def _builtin_entry() -> Dict[str, Any]:
    """The always-present hub: this repo, this process's port."""
    return {
        "id": BUILTIN_ID,
        "label": BUILTIN_LABEL,
        "folder": _project_root_str(),
        "port": hub_port(),
        "builtin": True,
    }


def _find(instance_id: str) -> Optional[Dict[str, Any]]:
    for r in load_config()["instances"]:
        if r["id"] == instance_id:
            return r
    return None


# ── Validation ──────────────────────────────────────────────────────────────
def validate_folder(path: str) -> Dict[str, Any]:
    """Check *path* is a WebAgent checkout the Add form can register, and whether
    its run.py is new enough to launch on a custom port."""
    folder = _norm_folder(path)
    p = Path(folder) if folder else None
    if not p or not p.exists():
        return {"is_webagent": False, "exists": False, "folder": folder,
                "port_aware": False, "message": "Folder not found."}
    if not p.is_dir():
        return {"is_webagent": False, "exists": True, "folder": folder,
                "port_aware": False, "message": "That path is not a folder."}
    if not _is_webagent_checkout(folder):
        return {"is_webagent": False, "exists": True, "folder": folder,
                "port_aware": False,
                "message": "That folder isn't a WebAgent checkout (no run.py + app/main.py)."}
    port_aware = _run_py_is_port_aware(folder)
    return {
        "is_webagent": True, "exists": True, "folder": folder,
        "port_aware": port_aware,
        "message": "ok" if port_aware else
                   "This checkout's run.py can't run on a custom port yet — update "
                   "it (git pull) before starting it, or it will try to take over "
                   "port 8080 and stop this app.",
    }


def _validate_port(port: int, *, exclude_id: str = "") -> None:
    if not (1024 <= port <= 65535):
        raise ValueError("Pick a port between 1024 and 65535.")
    if port == hub_port():
        raise ValueError("That's this app's own port — pick a different one (e.g. 8081).")
    for r in load_config()["instances"]:
        if r["id"] != exclude_id and r["port"] == port:
            raise ValueError("Another instance already uses that port.")


# ── Public surface: registry CRUD ───────────────────────────────────────────
def add_instance(label: str = "", folder: str = "", port: int = 0) -> Dict[str, Any]:
    """Register another WebAgent checkout to run on its own port."""
    folder_n = _norm_folder(folder)
    if not folder_n:
        raise ValueError("A local folder is required.")
    if folder_n == _project_root_str():
        raise ValueError("That's this app's own folder — it's already the hub.")
    if not _is_webagent_checkout(folder_n):
        raise ValueError("That folder isn't a WebAgent checkout (no run.py + app/main.py).")
    try:
        port = int(port)
    except (TypeError, ValueError):
        raise ValueError("A port number is required.")
    _validate_port(port)
    cfg = load_config()
    if any(r["folder"] == folder_n for r in cfg["instances"]):
        raise ValueError("That folder is already in the list.")
    entry = {
        "id": uuid.uuid4().hex[:12],
        "label": (label or "").strip() or folder_n.rsplit("/", 1)[-1],
        "folder": folder_n,
        "port": port,
    }
    cfg["instances"].append(entry)
    _save(cfg)
    return entry


def update_instance(instance_id: str, *, label=None, folder=None, port=None) -> Dict[str, Any]:
    """Edit a registered instance. The hub can't be edited here."""
    rid = (instance_id or "").strip()
    if rid == BUILTIN_ID:
        raise ValueError("This app (the hub) can't be edited here.")
    cfg = load_config()
    for r in cfg["instances"]:
        if r["id"] != rid:
            continue
        if label is not None and label.strip():
            r["label"] = label.strip()
        if folder is not None:
            fn = _norm_folder(folder)
            if fn and fn != r["folder"]:
                if not _is_webagent_checkout(fn):
                    raise ValueError("That folder isn't a WebAgent checkout.")
                if any(o["folder"] == fn and o["id"] != rid for o in cfg["instances"]):
                    raise ValueError("That folder is already in the list.")
                r["folder"] = fn
        if port is not None:
            try:
                pn = int(port)
            except (TypeError, ValueError):
                raise ValueError("A port number is required.")
            if pn != r["port"]:
                _validate_port(pn, exclude_id=rid)
                r["port"] = pn
        _save(cfg)
        return r
    raise ValueError("Unknown instance.")


def remove_instance(instance_id: str) -> Dict[str, Any]:
    """Forget a registered instance (does NOT stop it if it's running). The hub
    can't be removed."""
    rid = (instance_id or "").strip()
    if rid == BUILTIN_ID:
        raise ValueError("This app (the hub) can't be removed.")
    cfg = load_config()
    before = len(cfg["instances"])
    cfg["instances"] = [r for r in cfg["instances"] if r["id"] != rid]
    if len(cfg["instances"]) == before:
        raise ValueError("Unknown instance.")
    _save(cfg)
    return cfg


# ── Public surface: live listing (with status) ──────────────────────────────
async def list_instances() -> List[Dict[str, Any]]:
    """Every instance, hub first, each with a live ``status`` and a ``url``. Ports
    are probed concurrently so a long list stays fast."""
    entries = [_builtin_entry()] + [
        {**r, "builtin": False} for r in load_config()["instances"]
    ]
    # The hub is us — always running; probe the rest in parallel.
    async def _status_for(e: Dict[str, Any]) -> str:
        if e.get("builtin"):
            return "running"
        return await _probe_status(e["port"])

    statuses = await asyncio.gather(*[_status_for(e) for e in entries])
    out: List[Dict[str, Any]] = []
    for e, st in zip(entries, statuses):
        out.append({
            "id": e["id"],
            "label": e["label"],
            "folder": e["folder"],
            "port": e["port"],
            "builtin": bool(e.get("builtin")),
            "status": st,
            "url": f"http://localhost:{e['port']}/",
            # Only registered, port-aware checkouts can be launched here.
            "port_aware": True if e.get("builtin") else _run_py_is_port_aware(e["folder"]),
            "is_checkout": True if e.get("builtin") else _is_webagent_checkout(e["folder"]),
        })
    return out


# ── Public surface: start / stop (streaming async generators) ───────────────
def _ev(message: str, *, phase: str = "step", level: str = "info") -> Dict[str, Any]:
    return {"phase": phase, "message": message, "level": level}


def _done(ok: bool, message: str = "") -> Dict[str, Any]:
    return {"phase": "done", "result": {"ok": ok, "message": message}}


def _launch_cmd(folder: str) -> Tuple[List[str], str]:
    """The command + a human label for how we launch. Prefer ``uv`` (resolves the
    checkout's own venv/deps, exactly like WebAgent.bat) and fall back to the
    current interpreter."""
    uv = shutil.which("uv")
    if uv:
        return [uv, "run", "--extra", "encryption", "python", "run.py"], "uv"
    return [sys.executable, "run.py"], "python"


def _spawn(folder: str, port: int) -> None:
    """Spawn a detached sibling server on *port* from *folder*. Output is captured
    to data/local-instances/<port>.log so a failed launch can be diagnosed."""
    cmd, _how = _launch_cmd(folder)
    env = {**os.environ, "WEBAGENT_PORT": str(port)}
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        out: Any = open(_LOG_DIR / f"{port}.log", "ab")
    except OSError:
        out = subprocess.DEVNULL
    kwargs: Dict[str, Any] = dict(cwd=folder, env=env, stdin=subprocess.DEVNULL,
                                  stdout=out, stderr=subprocess.STDOUT, close_fds=True)
    if _IS_WIN:
        kwargs["creationflags"] = _NO_WINDOW | _NEW_GROUP
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(cmd, **kwargs)


async def start_instance(instance_id: str) -> AsyncIterator[Dict[str, Any]]:
    """Launch a registered instance as a background server and stream progress."""
    r = _find(instance_id)
    if not r:
        yield _done(False, "Unknown instance.")
        return
    folder, port, label = r["folder"], r["port"], r["label"]

    if not _is_webagent_checkout(folder):
        yield _done(False, "That folder is no longer a WebAgent checkout — check the path.")
        return
    if not _run_py_is_port_aware(folder):
        yield _ev(f"{folder}/run.py can't run on a custom port.", level="err")
        yield _done(False, "This checkout is too old to launch safely — update it "
                           "(git pull) so its run.py reads WEBAGENT_PORT, then retry.")
        return

    yield _ev(f"Starting “{label}” on port {port}…")
    if await _health(port):
        yield _done(True, f"Already running on port {port}.")
        return
    if _port_open(port):
        yield _done(False, f"Port {port} is already in use by something else — "
                           f"free it or pick another port.")
        return

    cmd, how = _launch_cmd(folder)
    yield _ev(f"Launching with {how}…")
    try:
        _spawn(folder, port)
    except OSError as e:
        yield _done(False, f"Could not launch: {e}")
        return

    # Wait for /health to come up. First boot (deps sync, schema) can take a while.
    deadline = time.monotonic() + _BOOT_WAIT
    announced = False
    while time.monotonic() < deadline:
        if await _health(port):
            yield _ev(f"“{label}” is up.", level="ok")
            yield _done(True, f"Running on http://localhost:{port}/")
            return
        if not announced and _port_open(port):
            yield _ev("Booting… (first start can take a minute)")
            announced = True
        await asyncio.sleep(1.5)

    yield _done(False, f"Launched, but it hasn't answered on port {port} yet. "
                       f"It may still be building — check data/local-instances/{port}.log.")


async def stop_instance(instance_id: str) -> AsyncIterator[Dict[str, Any]]:
    """Stop a registered instance by killing whatever is listening on its port."""
    if (instance_id or "").strip() == BUILTIN_ID:
        yield _done(False, "This is the app you're using — stop it from the server "
                           "window / Server Manager, not here.")
        return
    r = _find(instance_id)
    if not r:
        yield _done(False, "Unknown instance.")
        return
    port, label = r["port"], r["label"]

    yield _ev(f"Stopping “{label}” (port {port})…")
    pids = _port_listeners(port)
    if not pids:
        yield _done(True, "It wasn't running.")
        return
    for pid in pids:
        _kill(pid)
    deadline = time.monotonic() + _STOP_WAIT
    while time.monotonic() < deadline:
        if not _port_open(port):
            yield _ev("Stopped.", level="ok")
            yield _done(True, f"“{label}” stopped.")
            return
        await asyncio.sleep(0.5)
    yield _done(False, f"Sent the stop signal, but port {port} is still busy. It may "
                       f"take a moment, or another process is holding it.")
