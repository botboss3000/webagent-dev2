"""Supervisor-cooperative server self-restart.

Lets the running WebAgent server restart **itself** from the UI without depending
on an external supervisor being present — so a settings change that needs a fresh
process (e.g. switching the secrets vault for a clean cutover) can be applied with
one click instead of asking the user to restart manually.

Two roles, one file:

* **Detached relauncher** (run as a standalone script — see ``trigger_restart``):
  it waits for the old server to stop answering ``/health``, then gives any
  external supervisor (the Server-Manager guardian, a ``WebAgent.bat`` loop,
  systemd/launchd…) a short head-start to revive the server, and ONLY if nothing
  does within that window does it relaunch ``run.py`` itself. So:
    - on a supervised host the supervisor wins the race → no duplicate server;
    - on an unsupervised host the relauncher brings it back → never stranded.
  If a duplicate ever does slip through, the guardian's reaper collapses it to the
  one process actually serving :8080.

* **Helpers** imported by the admin API: ``auto_restart_available()`` (can we
  self-revive on this host?) and ``trigger_restart()`` (spawn the relauncher,
  then stop the active standalone process or supervised server topology so the
  port frees). A multiprocess restart also RECONCILES the worker population back
  to the supervisor's own ``--workers N`` (``_trim_surplus_workers``), so a
  supervisor that drifted above its launch count cannot leave a permanent surplus
  worker — the "stuck pid" — behind.

Intentionally STDLIB-ONLY and spawned by FILE PATH (not ``-m app.relauncher``), so
the tiny relauncher process never imports the heavy ``app`` package.
"""

from __future__ import annotations

import argparse
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

# Default port for self-restart. Reads WEBAGENT_PORT so a SIBLING instance (one a
# different repo folder started on 8081/8082 via the Local Instances page) that
# self-restarts probes + relaunches on ITS own port, not the 8080 hub. The hub is
# started with no WEBAGENT_PORT, so it stays 8080. The relaunched `run.py` inherits
# this same env, so it re-binds the correct port automatically.
def _resolve_port() -> int:
    raw = (os.environ.get("WEBAGENT_PORT") or "").strip()
    if raw:
        try:
            p = int(raw)
            if 1 <= p <= 65535:
                return p
        except ValueError:
            pass
    return 8080


PORT = _resolve_port()

_IS_WIN = sys.platform == "win32"
def _single_worker_supervisor_pid() -> int | None:
    if _IS_WIN:
        return None
    parent = os.getppid()
    try:
        with open(f"/proc/{parent}/cmdline", "rb") as stream:
            argv = [v.decode(errors="replace") for v in stream.read().split(b"\0") if v]
    except OSError:
        return None
    return parent if parent > 1 and "app.server_supervisor" in argv else None
_NO_WINDOW = 0x08000000      # CREATE_NO_WINDOW   — hidden console (background daemon)
_NEW_GROUP = 0x00000200      # CREATE_NEW_PROCESS_GROUP — detach from parent's Ctrl+C

# ── cadence / windows (seconds) ──────────────────────────────────────────────
_DOWN_WAIT = 30.0            # how long to wait for the OLD server to stop answering
_COOP_WAIT = 12.0            # head-start given to an external supervisor to revive it
_PORT_WAIT = 20.0            # if the port is held but unhealthy, wait this long for it
                            # to either become healthy (someone else won) or free up
_UP_WAIT = 180.0            # after WE launch, how long to wait for /health before exit
_PROBE_TIMEOUT = 3.0

# Multiprocess worker reload (SIGHUP → verify → force). See _mp_reload.
_MP_TURNOVER_WAIT = 25.0    # after SIGHUP, how long to wait for the OLD workers to retire
_MP_ESCALATE_WAIT = 30.0    # after a forced worker kill, how long to wait for the new set


def _project_root() -> str:
    """The WebAgent checkout root — the parent of this file's ``app/`` dir."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run_py(root: str) -> str:
    return os.path.join(root, "run.py")


def auto_restart_available(root: str | None = None) -> tuple[bool, str]:
    """Whether this host can revive itself after a self-restart.

    True unless we can't even find the entrypoint to relaunch — in which case the
    UI should fall back to asking the user to restart manually rather than risk
    stranding them with a dead server."""
    root = root or _project_root()
    if not os.path.isfile(_run_py(root)):
        return False, "run.py not found — cannot auto-relaunch on this host."
    if not sys.executable:
        return False, "No Python interpreter resolved — cannot auto-relaunch."
    return True, ""


def _detached_kwargs(log_path: str | None) -> dict:
    try:
        out: object = open(log_path, "ab") if log_path else subprocess.DEVNULL
    except OSError:
        out = subprocess.DEVNULL
    kwargs: dict = dict(stdin=subprocess.DEVNULL, stdout=out,
                        stderr=subprocess.STDOUT, close_fds=True)
    if _IS_WIN:
        kwargs["creationflags"] = _NO_WINDOW | _NEW_GROUP
    else:
        kwargs["start_new_session"] = True
    return kwargs


def _reload_supervisor_active() -> bool:
    """Whether this worker is owned by Uvicorn's development reloader."""
    argv = [str(value) for value in sys.argv]
    if "--reload" in argv and any("uvicorn" in value for value in argv):
        return True
    if _IS_WIN:
        return False
    try:
        with open(f"/proc/{os.getppid()}/cmdline", "rb") as stream:
            parent_argv = [
                value.decode(errors="replace")
                for value in stream.read().split(b"\0")
                if value
            ]
    except OSError:
        return False
    return "--reload" in parent_argv and any("uvicorn" in value for value in parent_argv)


def _process_argv(pid: int) -> list[str]:
    """Return a Linux process command line without adding runtime dependencies."""
    if _IS_WIN:
        return []
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as stream:
            return [
                value.decode(errors="replace")
                for value in stream.read().split(b"\0")
                if value
            ]
    except OSError:
        return []


def _worker_count(argv: list[str]) -> int | None:
    """Read Uvicorn's --workers value from either supported CLI spelling."""
    for index, value in enumerate(argv):
        raw: str | None = None
        if value == "--workers" and index + 1 < len(argv):
            raw = argv[index + 1]
        elif value.startswith("--workers="):
            raw = value.partition("=")[2]
        if raw is not None:
            try:
                return int(raw)
            except ValueError:
                return None
    return None


def _uvicorn_multiprocess_supervisor_pid() -> int | None:
    """Return the Uvicorn supervisor PID for a multi-worker child.

    Uvicorn's spawn workers inherit the process group led by the master. Requiring
    all three identities (Uvicorn parent, parent as group leader, and this worker
    in that group) lets callers safely signal only the supervisor without touching
    Tini or relying on a container restart policy.
    """
    if _IS_WIN:
        return None
    parent_pid = os.getppid()
    if parent_pid <= 1:
        return None
    parent_argv = _process_argv(parent_pid)
    if not any("uvicorn" in value for value in parent_argv):
        return None
    if (_worker_count(parent_argv) or 0) <= 1:
        return None
    try:
        parent_group = os.getpgid(parent_pid)
        worker_group = os.getpgrp()
    except OSError:
        return None
    if parent_group != parent_pid or worker_group != parent_group:
        return None
    return parent_pid


def _proc_ppid(pid: int) -> int | None:
    """A pid's parent from ``/proc`` — no dependencies, Windows returns None."""
    if _IS_WIN:
        return None
    try:
        with open(f"/proc/{pid}/stat", "rb") as stream:
            data = stream.read().decode(errors="replace")
    except OSError:
        return None
    # ``comm`` may contain spaces and parentheses, so only trust the fields AFTER
    # the last ')': state, ppid, ...
    try:
        return int(data.rsplit(")", 1)[1].split()[1])
    except (IndexError, ValueError):
        return None


def _worker_children(master_pid: int) -> set[int]:
    """The Uvicorn worker processes owned by ``master_pid`` (``/proc`` only).

    Identified by the ``multiprocessing.spawn`` / ``--multiprocessing-fork``
    command line so the multiprocessing resource tracker and any other child stay
    untouched — the parent still needs them to reap the workers we replace.
    """
    workers: set[int] = set()
    if _IS_WIN:
        return workers
    try:
        entries = os.listdir("/proc")
    except OSError:
        return workers
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        if _proc_ppid(pid) != master_pid:
            continue
        argv = _process_argv(pid)
        if any(
            "multiprocessing.spawn" in value or "--multiprocessing-fork" in value
            for value in argv
        ):
            workers.add(pid)
    return workers


def _await_retirement(master_pid: int, before: set[int], timeout: float) -> bool:
    """True once no worker from ``before`` is still alive — a real turnover.

    A replacement appearing is NOT success: Uvicorn spawns the new worker before
    it retires the old one, so only the retirement proves the supervisor is
    actually progressing rather than parked in ``Process.join()``.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = _worker_children(master_pid)
        if before:
            if not (before & current):
                return True
        elif current:
            return True
        time.sleep(0.5)
    return False


def _intended_worker_count(master_pid: int) -> int | None:
    """The worker population a Uvicorn supervisor MEANT to run — its ``--workers N``.

    The master's own argv is the only trustworthy source. Uvicorn's ``handle_ttin``
    increments ``processes_num`` on a stray SIGTTIN and NOTHING ever decrements it
    except ``handle_ttou``, so a supervisor can drift ABOVE the count it was
    launched with — and the surplus is permanent on its own, because
    ``keep_subprocess_alive`` respawns any tracked worker that dies. Returns None
    when the pid is not a multi-worker Uvicorn supervisor, so callers never trim a
    topology they do not recognise.
    """
    if _IS_WIN or master_pid <= 1:
        return None
    argv = _process_argv(master_pid)
    if not any("uvicorn" in value for value in argv):
        return None
    return _worker_count(argv)


def _await_population(master_pid: int, target: int, timeout: float) -> bool:
    """True once the live worker population has shrunk to ``target`` or below."""
    deadline = time.monotonic() + timeout
    while True:
        if len(_worker_children(master_pid)) <= target:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.5)


def _trim_surplus_workers(master_pid: int, timeout: float = _MP_ESCALATE_WAIT) -> int:
    """Retire the workers a supervisor is running ABOVE its own ``--workers N``.

    A surplus worker cannot be removed by killing it: ``keep_subprocess_alive``
    replaces every tracked worker that dies, so the population never converges.
    Uvicorn's ONLY supported way to shrink the set is SIGTTOU — ``handle_ttou``
    pops the last tracked Process and retires it — so that is what we send, one
    signal per surplus worker, each verified by the population actually dropping so
    a wedged ``join()`` can never make us fire blindly.

    Only the supervisor is signalled: Tini and the container stay put, so the
    listen socket is never left unowned. Returns the number of workers retired.
    """
    if _IS_WIN or master_pid <= 1:
        return 0
    intended = _intended_worker_count(master_pid)
    if not intended:
        return 0
    retired = 0
    while True:
        live = len(_worker_children(master_pid))
        if live <= intended:
            return retired
        try:
            os.kill(master_pid, signal.SIGTTOU)
        except OSError:
            return retired
        if not _await_population(master_pid, live - 1, timeout):
            return retired
        retired += 1


def _mp_reload(master_pid: int, delay: float = 0.6) -> int:
    """Replace a Uvicorn multiprocess supervisor's workers, robustly.

    The supported path is SIGHUP: Uvicorn's ``restart_all()`` spawns a worker's
    replacement, waits until it is ready, then retires the old worker. That
    retirement calls ``Process.join()`` — and Uvicorn's worker shutdown waits
    forever when ``timeout_graceful_shutdown`` is unset and a long-lived
    connection or task never drains. A worker that never exits parks the
    supervisor's ONLY signal-draining thread in ``waitpid``, so every later SIGHUP
    is queued and ignored and the whole restart is silently dead.

    So: ask nicely, confirm the old workers actually retired, and if they did not,
    force the turnover by killing the workers. That lets the blocked ``join()``
    return and the supervisor respawn a fresh set (or its ``keep_subprocess_alive``
    reaps and replaces them). The supervisor itself is never signalled — nor Tini
    nor the container — so the listen socket is never left unowned.
    """
    if _IS_WIN or master_pid <= 1:
        return 0
    time.sleep(max(0.1, delay))
    before = _worker_children(master_pid)
    try:
        os.kill(master_pid, signal.SIGHUP)
    except OSError:
        return 0
    if not before:
        # No worker children were identifiable (e.g. a /proc-scoped sandbox):
        # there is nothing to verify, and a targeted kill we cannot bound would
        # be worse than the signal alone.
        return 0
    if _await_retirement(master_pid, before, _MP_TURNOVER_WAIT):
        # A turnover alone does not fix a population that has drifted above the
        # supervisor's own --workers N (see _trim_surplus_workers); reconcile both
        # in one restart so the "stuck pid" never survives a reload.
        _trim_surplus_workers(master_pid)
        return 0
    # The supervisor accepted SIGHUP but never turned its workers over — it is
    # wedged in Process.join(). Force it: kill the workers, then let the
    # supervisor (whose blocked join now returns) bring up replacements.
    self_pid = os.getpid()
    for pid in sorted(_worker_children(master_pid), key=lambda p: (p == self_pid, p)):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    _await_retirement(master_pid, before, _MP_ESCALATE_WAIT)
    _trim_surplus_workers(master_pid)
    return 0


def _request_supervised_reload(root: str) -> dict:
    """Ask Uvicorn's watcher to replace this worker without orphaning its parent."""
    trigger_path = os.path.join(root, "app", "main.py")
    try:
        os.utime(trigger_path, None)
    except OSError as exc:
        return {
            "status": "error",
            "auto_restart": False,
            "reason": f"Could not notify the reload supervisor: {exc}",
        }
    return {
        "status": "restarting",
        "auto_restart": True,
        "mode": "reload",
        "message": "Server is restarting — it will be back in a few seconds.",
    }


def trigger_restart(port: int = PORT, delay: float = 0.6) -> dict:
    """Restart the active server topology after the HTTP response can flush.

    Supervised Uvicorn modes reload in place. A standalone process uses the
    detached relauncher, which waits for the old server to stop and launches
    ``run.py`` only when no external supervisor revives it."""
    supervisor_pid = _single_worker_supervisor_pid()
    if supervisor_pid is not None:
        def _restart_worker() -> None:
            time.sleep(max(0.1, delay))
            try:
                os.kill(supervisor_pid, signal.SIGHUP)
            except OSError:
                pass
        threading.Thread(target=_restart_worker, daemon=True).start()
        return {
            "status": "restarting",
            "auto_restart": True,
            "mode": "single-worker",
            "message": "Application worker is restarting; the container stays running.",
        }
    root = _project_root()
    if _reload_supervisor_active():
        # Exiting only the worker strands Uvicorn's reload parent holding the port.
        # Touching a watched source file lets that parent perform a clean replacement.
        return _request_supervised_reload(root)

    multiprocess_supervisor_pid = _uvicorn_multiprocess_supervisor_pid()
    if multiprocess_supervisor_pid is not None:
        # Signal the supervisor from a background thread, IN-PROCESS: the HTTP
        # response can flush before the workers roll, and the container and Tini
        # keep the listen socket owned (never exit here and never spawn a rival
        # server). A bare SIGHUP is not enough on its own — see _mp_reload:
        # Uvicorn's restart_all() retires the old worker with Process.join(), which
        # parks the supervisor's only signal-draining thread in waitpid forever
        # when a worker will not drain, so later SIGHUPs queue and are ignored.
        # _mp_reload confirms the turnover happened and forces it when it did not.
        def _reload_workers() -> None:
            _mp_reload(multiprocess_supervisor_pid, max(0.1, delay))

        threading.Thread(target=_reload_workers, daemon=True).start()
        return {
            "status": "restarting",
            "auto_restart": True,
            "mode": "multiprocess",
            "message": "Server is restarting; it will be back in a few seconds.",
        }

    ok, reason = auto_restart_available(root)
    if not ok:
        return {"status": "unavailable", "auto_restart": False, "reason": reason}

    log_path = os.path.join(root, "data", "relauncher.log")
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
    except OSError:
        log_path = None

    args = [
        sys.executable, os.path.abspath(__file__),
        "--port", str(port),
        "--root", root,
        "--python", sys.executable,
        "--pid", str(os.getpid()),
    ]
    try:
        subprocess.Popen(args, cwd=root, **_detached_kwargs(log_path))
    except OSError as e:
        return {"status": "error", "auto_restart": False,
                "reason": f"Could not spawn relauncher: {e}"}

    def _die() -> None:
        time.sleep(max(0.1, delay))
        os._exit(0)

    threading.Thread(target=_die, daemon=True).start()
    return {"status": "restarting", "auto_restart": True,
            "message": "Server is restarting — it will be back in a few seconds."}


# ── the detached relauncher loop ─────────────────────────────────────────────
def _health_up(port: int) -> bool:
    url = f"http://127.0.0.1:{port}/health"
    try:
        with urllib.request.urlopen(url, timeout=_PROBE_TIMEOUT) as resp:
            return resp.status < 500
    except urllib.error.HTTPError as e:
        return e.code < 500
    except (urllib.error.URLError, OSError):
        return False


def _port_in_use(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1.0)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    except OSError:
        return False
    finally:
        s.close()


def _launch_server(python: str, root: str) -> bool:
    run_py = _run_py(root)
    if not os.path.isfile(run_py):
        return False
    log_path = os.path.join(root, "data", "relauncher.log")
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
    except OSError:
        log_path = None
    try:
        subprocess.Popen([python, "run.py"], cwd=root, **_detached_kwargs(log_path))
        return True
    except OSError:
        return False


def _relaunch(port: int, root: str, python: str) -> int:
    deadline = time.monotonic() + _DOWN_WAIT
    while time.monotonic() < deadline:               # 1. wait for the old server to go down
        if not _health_up(port):
            break
        time.sleep(0.7)

    deadline = time.monotonic() + _COOP_WAIT
    while time.monotonic() < deadline:               # 2. let a supervisor revive it first
        if _health_up(port):
            return 0                                  # someone else won → nothing to do
        time.sleep(0.7)

    if _port_in_use(port):                            # 3. port held but unhealthy → don't double-bind
        deadline = time.monotonic() + _PORT_WAIT
        while time.monotonic() < deadline:
            if _health_up(port):
                return 0                              # it finished booting → done
            if not _port_in_use(port):
                break                                 # freed up → safe to launch
            time.sleep(0.7)
        else:
            return 0                                  # still squatting → leave it; don't fight

    if not _launch_server(python, root):              # 4. nothing revived it → we do
        return 1

    deadline = time.monotonic() + _UP_WAIT
    while time.monotonic() < deadline:
        if _health_up(port):
            return 0
        time.sleep(1.0)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="WebAgent server relauncher")
    p.add_argument("--port", type=int, default=PORT)
    p.add_argument("--root", default=_project_root())
    p.add_argument("--python", default=sys.executable)
    p.add_argument("--pid", type=int, default=0)      # informational; we gate on /health
    # Manual multiprocess worker turnover: ``python app/relauncher.py
    # --mp-reload <supervisor-pid>`` forces a wedged Uvicorn supervisor's workers
    # over (SIGHUP, verify, then force — see _mp_reload). The in-process
    # ``trigger_restart`` path calls _mp_reload directly; this entrypoint is the
    # detached escape hatch for a supervisor whose every worker is wedged.
    p.add_argument("--mp-reload", dest="mp_reload", type=int, default=0)
    # Detached escape hatch for a supervisor stuck ABOVE its own --workers N: the
    # in-process restart path trims as part of every turnover (_trim_surplus_workers);
    # this entrypoint exists so a surplus can be retired without rolling the workers
    # at all — ``python app/relauncher.py --trim-workers <supervisor-pid>``.
    p.add_argument("--trim-workers", dest="trim_workers", type=int, default=0)
    p.add_argument("--delay", type=float, default=0.6)
    a = p.parse_args(argv)
    if a.trim_workers > 0:
        return _trim_surplus_workers(a.trim_workers)
    if a.mp_reload > 0:
        return _mp_reload(a.mp_reload, a.delay)
    return _relaunch(a.port, a.root, a.python or sys.executable)


if __name__ == "__main__":
    raise SystemExit(main())
