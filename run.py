"""WebAgent launcher — guarantees a SINGLE server instance on port 8080.

Why this matters: the live chat feed (WebSocket) and the running agent only
talk to each other through one process's in-memory listener list — there is no
cross-process event bus. If two servers stack on :8080, the OS splits incoming
connections between them: the browser's WebSocket lands on server A while the
"send message" request runs the agent on server B, so the live text is
broadcast into a process the browser isn't listening to. Nothing streams, yet
the answer still lands in the shared database — which is exactly the
"message partially appears / doesn't appear, but shows up after a session
switch" symptom.

Two distinct things can occupy :8080, and they need OPPOSITE responses:

  1. A LIVE server process is holding it. Response: kill it and wait until it
     is actually gone (take-over policy — a just-killed mid-turn run is
     recovered by the app's startup orphan-cleanup, so nothing is lost), then
     bind. A plain (non-SO_REUSEADDR) bind keeps a stray second launch from
     silently stacking: if our kill somehow fails, the bind fails fast instead.

  2. A DEAD predecessor's LEFTOVER SOCKETS are holding it. When a server is
     force-killed while browsers still have live WebSocket connections, Windows
     strands those connections (ESTABLISHED / FIN_WAIT_2 / TIME_WAIT) and even
     the LISTEN socket under the now-dead PID. There is NO process to kill, and
     a plain bind is refused (WinError 10048) until the kernel drains them —
     which can take minutes. Response: confirm no live process owns the port,
     then reclaim the address with SO_REUSEADDR. That is the only way to bind
     over the corpse's sockets, and it is safe HERE precisely because we only
     reach for it once we've proven no live server is there to stack on top of.

Telling the two apart is the whole game: we cross-check every PID that netstat
reports as LISTENING on :8080 against the live process table. A listener whose
PID is alive is case 1 (kill it); a listener whose PID is dead is case 2
(reclaim with reuse).
"""
import os
import socket
import subprocess
import sys
import time

import uvicorn  # noqa: F401  (kept so a missing dep fails here with a clear import)
from uvicorn.config import Config
from uvicorn.server import Server

HOST = "0.0.0.0"   # IPv4 "any"
HOST6 = "::"        # IPv6 "any" — so http://localhost:8080 works when the browser
                    # resolves "localhost" to ::1 (Windows browsers often try IPv6 first).

# The port is 8080 by default, but reads WEBAGENT_PORT when set so SEVERAL
# WebAgent checkouts can run side by side on one machine — each launched on its
# OWN port (8081, 8082, …) from its OWN repo folder. This is what the admin
# "Local Instances" page uses to run sibling repos as background servers. Every
# guard below (sole-instance kill, bind, take-over) references this one value, so
# an instance only ever guards/binds ITS port — a sibling on 8081 never touches
# the 8080 hub. A blank/garbage value falls back to 8080.
def _saved_hub_port() -> int:
    """The port the hub was told to bind next time, from the Deploy card's "change
    current deployment port" (data/config/local-instances.json ``hub_port``). Read
    with the stdlib only — run.py must not import the heavy app package. Returns 0
    when unset/invalid. A SIBLING is launched WITH WEBAGENT_PORT, so this is only
    consulted for the hub (no env), and never lets a sibling read it."""
    try:
        import json
        cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "data", "config", "local-instances.json")
        with open(cfg_path, "r", encoding="utf-8") as fh:
            v = int((json.load(fh) or {}).get("hub_port") or 0)
        return v if 1024 <= v <= 65535 else 0
    except Exception:  # noqa: BLE001  — missing/garbage file → fall back to 8080
        return 0


def _resolve_port() -> int:
    raw = (os.environ.get("WEBAGENT_PORT") or "").strip()
    if raw:
        try:
            p = int(raw)
            if 1 <= p <= 65535:
                return p
        except ValueError:
            pass
    saved = _saved_hub_port()
    if saved:
        return saved
    return 8080


PORT = _resolve_port()
_IS_WIN = sys.platform == "win32"
# Keep helper console tools (netstat/taskkill/tasklist) from flashing a window.
_NO_WINDOW = {"creationflags": 0x08000000} if _IS_WIN else {}  # CREATE_NO_WINDOW


def _port_listeners(port: int = PORT) -> list[int]:
    """PIDs with a LISTENING socket on `port`, excluding ourselves.

    Cross-platform, never raises. NOTE: on Windows a force-killed server can
    leave a LISTEN socket attributed to a now-DEAD pid, so a pid appearing here
    does NOT prove a live process — cross-check with `_live_pids()`.
    """
    me = os.getpid()
    pids: list[int] = []
    if _IS_WIN:
        try:
            out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True,
                                 timeout=10, **_NO_WINDOW).stdout
            for line in out.splitlines():
                if f":{port}" not in line or "LISTENING" not in line:
                    continue
                parts = line.split()
                if parts and parts[-1].isdigit():
                    pid = int(parts[-1])
                    if pid != me and pid not in pids:
                        pids.append(pid)
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        try:
            out = subprocess.run(["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
                                 capture_output=True, text=True, timeout=10).stdout
            for tok in out.split():
                if tok.isdigit() and int(tok) != me and int(tok) not in pids:
                    pids.append(int(tok))
        except (OSError, subprocess.SubprocessError):
            pass
    return pids


def _live_pids() -> set[int]:
    """Every currently-RUNNING process id. Used to tell a live port owner from a
    dead predecessor's leftover socket. Returns an empty set only if the probe
    itself failed (we always include at least ourselves otherwise)."""
    pids: set[int] = set()
    try:
        if _IS_WIN:
            out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True,
                                 text=True, timeout=10, **_NO_WINDOW).stdout
            for line in out.splitlines():
                cols = line.split('","')
                if len(cols) >= 2:
                    p = cols[1].strip().strip('"')
                    if p.isdigit():
                        pids.add(int(p))
        else:
            out = subprocess.run(["ps", "-e", "-o", "pid="], capture_output=True,
                                 text=True, timeout=10).stdout
            for tok in out.split():
                if tok.isdigit():
                    pids.add(int(tok))
    except (OSError, subprocess.SubprocessError):
        pass
    return pids


def _live_owners(port: int = PORT) -> list[int]:
    """PIDs that are LISTENING on `port` AND are actually alive — the only ones
    worth killing. Leftover sockets from a dead predecessor are excluded.

    If the liveness probe fails (empty set) we conservatively treat every
    listener as live, so we never reach for SO_REUSEADDR while a real server
    might still be up."""
    listeners = _port_listeners(port)
    if not listeners:
        return []
    live = _live_pids()
    if not live:
        return listeners  # probe failed — assume all live (don't risk stacking)
    return [pid for pid in listeners if pid in live]


def _kill(pid: int) -> None:
    try:
        if _IS_WIN:
            subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"],
                           capture_output=True, **_NO_WINDOW)
        else:
            import signal
            os.kill(pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass


def _ensure_sole_instance() -> None:
    """Take over :8080 from any LIVE server holding it, and wait until it's gone.

    Dead-predecessor leftover sockets are intentionally ignored here — there's
    nothing to kill; the bind step reclaims those with SO_REUSEADDR."""
    owners = _live_owners(PORT)
    if not owners:
        return
    print(f"[WebAgent] Port {PORT} held by a live server (pid(s) {owners}) — taking "
          f"over (killing it so there is only one server).", flush=True)
    for pid in owners:
        _kill(pid)
    # Wait (up to ~5s) for the OS to tear the killed process down.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if not _live_owners(PORT):
            break
        time.sleep(0.25)


def _make_listener(family: int, host: str, *, reuse: bool) -> socket.socket:
    """Create a bound, listening socket. Raises OSError if the bind fails.

    reuse=False → a plain bind (no SO_REUSEADDR): refuses a second live server on
    the same port, so duplicates can't silently stack. This is the default.

    reuse=True → SO_REUSEADDR: lets the bind step OVER a dead predecessor's
    lingering sockets (TIME_WAIT / orphaned ESTABLISHED / leftover LISTEN). Only
    used once we've confirmed no LIVE process owns the port, so it can never
    stack on top of a running server.
    """
    s = socket.socket(family, socket.SOCK_STREAM)
    try:
        # Never let a child process (terminal/tmux/git subprocess, etc.) inherit
        # the listen handle — an inherited handle would keep :8080 occupied after
        # this server exits, recreating the very "stuck port" we guard against.
        s.set_inheritable(False)
    except (OSError, AttributeError):
        pass
    if family == socket.AF_INET6:
        # Keep the IPv6 socket v6-only so it coexists with the separate IPv4
        # socket on the SAME port (Windows defaults to v6only=1; we set it
        # explicitly so the two-socket setup behaves the same everywhere). If it
        # weren't v6-only it would try to claim IPv4 too and collide with sock4.
        try:
            s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        except (OSError, AttributeError):
            pass
    if reuse:
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        except (OSError, AttributeError):
            pass
    try:
        s.bind((host, PORT))
        s.listen(256)
    except OSError:
        s.close()
        raise
    return s


def _bind_with_takeover(family: int, host: str):
    """Bind robustly. Returns (socket, reuse_used) on success, or (None, reuse).

    Normal path: a plain bind (blocks a second LIVE server from stacking). On
    failure, decide WHY the port is busy and react:
      • a live process owns it  → kill it and retry (take-over)
      • only dead leftovers      → reclaim the address with SO_REUSEADDR
    Gives up after ~15s so a genuinely stuck live process surfaces as an error
    rather than hanging the launcher forever.
    """
    deadline = time.monotonic() + 15.0
    reuse = False
    announced = False
    last: OSError | None = None
    while time.monotonic() < deadline:
        try:
            return _make_listener(family, host, reuse=reuse), reuse
        except OSError as e:
            last = e
            owners = _live_owners(PORT)
            if owners:
                # A real server (re)appeared on the port — take it over.
                print(f"[WebAgent] Port {PORT} still held by live pid(s) {owners} — "
                      f"killing to take over…", flush=True)
                for pid in owners:
                    _kill(pid)
                reuse = False  # prefer a clean plain bind once it's actually dead
            else:
                # No live owner: the only thing in the way is a dead
                # predecessor's lingering sockets. Reclaim the address.
                if not announced:
                    print(f"[WebAgent] Port {PORT} occupied by a previous server's "
                          f"leftover sockets (no live process owns it) — reclaiming "
                          f"the address…", flush=True)
                    announced = True
                reuse = True
            time.sleep(0.4)
    if last is not None:
        print(f"[WebAgent] (last bind error: {last})", flush=True)
    return None, reuse


def main():
    _ensure_sole_instance()

    # IPv4 is REQUIRED.
    sock4, reuse = _bind_with_takeover(socket.AF_INET, HOST)
    if sock4 is None:
        owners = _live_owners(PORT)
        if owners:
            print(f"[WebAgent] ERROR: port {PORT} is held by live process(es) "
                  f"{owners} that won't terminate. Stop them (TUI Stop button / "
                  f"run kill_webagent.bat as Administrator) and retry.", flush=True)
        else:
            print(f"[WebAgent] ERROR: port {PORT} is still tied up by a previous "
                  f"server's leftover sockets and wouldn't release in time. Wait "
                  f"~30s and retry, or run kill_webagent.bat.", flush=True)
        sys.exit(1)

    # IPv6 is BEST-EFFORT. Many browsers resolve "localhost" to ::1 first, so
    # binding it too makes http://localhost:8080 load reliably regardless of
    # which family the browser picks. Reuse the SAME reuse decision IPv4 made (if
    # IPv4 had to reclaim leftovers, IPv6 almost certainly does too). If IPv6
    # can't bind, keep serving IPv4 alone rather than failing the whole launch.
    sock6 = None
    try:
        sock6 = _make_listener(socket.AF_INET6, HOST6, reuse=reuse)
    except OSError as e:
        print(f"[WebAgent] IPv6 listener unavailable ({e}) — serving IPv4 only.", flush=True)

    sockets = [sock4] + ([sock6] if sock6 is not None else [])
    families = "IPv4+IPv6" if sock6 is not None else "IPv4 only"
    how = "reclaimed leftovers" if reuse else "exclusive"
    print(f"[WebAgent] Bound port {PORT} ({families}, {how} — single instance)", flush=True)

    # The lightweight wrapper serves /app and static cache assets while the
    # heavyweight FastAPI application completes its own core startup.
    # Uvicorn's access logger includes the raw query string (and therefore
    # legacy ``?token=...`` credentials) and writes synchronously to stdout.
    # Under a retrying background tab that can both leak credentials and stall
    # the serving loop on console backpressure. The app's structured access
    # recorder already captures method/path/status/duration without queries.
    # Bound worker shutdown. Uvicorn's default (unset) waits forever, so a worker
    # holding a long-lived SSE/WebSocket connection never drains; Uvicorn's
    # restart_all() then parks the supervisor in Process.join() (waitpid), its only
    # signal-draining thread stops running, and EVERY later restart silently
    # no-ops. See app/relauncher.py::_mp_reload for the force-turnover recovery.
    config = Config(
        app="app.bootstrap_asgi:bootstrap_app",
        ws="wsproto",
        access_log=False,
        timeout_graceful_shutdown=10,
    )
    server = Server(config=config)

    # Print our own banner — uvicorn skips its "Uvicorn running on" line when a
    # pre-bound socket is passed in.
    print(f"[WebAgent] Server running on http://localhost:{PORT}", flush=True)
    print(f"[WebAgent] UI: http://localhost:{PORT}/index.html", flush=True)
    print(f"[WebAgent] Swagger: http://localhost:{PORT}/docs", flush=True)
    print(f"[WebAgent] Press Ctrl+C in the WebAgent.bat window to stop.", flush=True)
    print(flush=True)

    server.run(sockets=sockets)


if __name__ == "__main__":
    main()
