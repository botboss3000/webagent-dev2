"""Device job ACTIONS — non-agent commands one device runs on another.

A normal device job means "run this ``prompt`` through the agent loop on the
target device" (app/devices/worker.py ``_execute``). An ACTION job instead
carries ``payload = {"action": "<name>", ...}`` and runs a small local handler
with NO agent — fleet control the user triggers from the Instances page, e.g.
starting the Cloudflare tunnel on a remote instance that has it set up but off.

Why a registry (not an if/elif in the worker): actions are meant to grow
(start/stop tunnel today; other providers, restart, etc. later) WITHOUT touching
the core claim/execute loop. The worker calls ``run_device_action`` for any job
whose payload names an action; new capabilities ``register_action`` here.

Each handler is ``async def h(*, job, db, payload) -> str`` and returns a short
human-readable result (stored on the job's ``result_excerpt``). Raise to mark the
job errored — the worker records the message.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict

logger = logging.getLogger(__name__)

ActionHandler = Callable[..., Awaitable[str]]
_REGISTRY: Dict[str, ActionHandler] = {}


def register_action(name: str, handler: ActionHandler) -> None:
    """Register (or replace) the handler for an action name."""
    _REGISTRY[name] = handler


def has_action(name: str) -> bool:
    return name in _REGISTRY


async def run_device_action(name: str, *, job: dict, db: Any, payload: dict) -> str:
    """Dispatch a named action to its handler. Raises on an unknown action."""
    handler = _REGISTRY.get(name)
    if not handler:
        raise RuntimeError(f"unknown device action {name!r}")
    return await handler(job=job, db=db, payload=payload)


# ── Built-in actions ─────────────────────────────────────────────────────────
# Remote Access tunnel control. These reuse the SAME RemoteAccessManager
# singleton the local admin card / agent tool drive, so a remote Start can't
# spawn a second cloudflared fighting the local one (see app/remote_access).

async def _start_tunnel(*, job: dict, db: Any, payload: dict) -> str:
    from app.remote_access import store
    from app.remote_access.manager import get_manager

    cfg = store.load_config()
    # Prefer the sender's explicit provider hint; else start whatever managed
    # method this device has configured (mirrors the boot auto-start).
    method = (payload.get("provider") or cfg.get("active_method") or "").strip()
    if method not in ("cloudflare", "ngrok"):
        raise RuntimeError("no tunnel is set up on this device")
    res = await get_manager().start_method(method)
    if not res.get("ok"):
        raise RuntimeError(res.get("error") or "tunnel failed to start")
    url = res.get("public_url") or "(resolving address)"
    logger.info("Device action: started %s tunnel → %s", method, url)
    return f"{method} tunnel started: {url}"


async def _stop_tunnel(*, job: dict, db: Any, payload: dict) -> str:
    if await _stop_slave_if_present():
        logger.info("Device action: stopped detached tunnel slave")
        return "slave tunnel stopped"
    from app.remote_access.manager import get_manager
    await get_manager().stop()
    logger.info("Device action: stopped tunnel")
    return "tunnel stopped"


register_action("start_tunnel", _start_tunnel)


# ── Detached tunnel slave ────────────────────────────────────────────────────
# The Instances page launches an independent Python controller. On Windows it
# receives a visible console; on every platform it survives app restarts and is
# driven through a token-gated loopback endpoint. The old managed action remains
# available as the launch-failure fallback and for the Remote Access card.

async def _stop_slave_if_present() -> bool:
    import asyncio

    from app.remote_access import netinfo, store
    from app.remote_access.slave import control_request, probe_control

    port = netinfo.get_port()
    status = await asyncio.to_thread(probe_control, port)
    if not status:
        link = store.load_slave_link(port)
        if link.get("running"):
            store.update_slave_state(
                port, running=False, state="stopped", clear_token=True,
                slave_pid=0, tunnel_pid=0,
            )
        return False
    token = str(store.load_slave_link(port).get("token") or "")
    if not token:
        raise RuntimeError("a tunnel slave is running but its control token is unavailable")
    await asyncio.to_thread(control_request, port, "stop", token)
    store.update_slave_state(port, running=False, url="", clear_token=True)
    return True


async def _start_slave_tunnel(*, job: dict, db: Any, payload: dict) -> str:
    import asyncio
    from pathlib import Path
    import secrets
    import shutil
    import socket
    import subprocess
    import sys
    import time

    if not sys.platform.startswith("win"):
        raise RuntimeError(
            "tunnels from the Instances page are currently available only on "
            "Windows; Linux and macOS support is coming soon"
        )

    from app.remote_access import netinfo, store
    from app.remote_access.slave import control_request, probe_control, read_status

    port = netinfo.get_port()
    cfg = store.load_config()
    provider = (payload.get("provider") or cfg.get("active_method") or "").strip()
    if provider not in ("cloudflare", "ngrok"):
        raise RuntimeError("no Cloudflare or ngrok tunnel is set up on this device")

    existing_token = str(store.load_slave_link(port).get("token") or "")
    existing = await asyncio.to_thread(probe_control, port)
    if existing:
        if existing.get("state") in ("starting", "running"):
            url = str(existing.get("url") or "")
            return f"Slave tunnel already running: {url or '(resolving address)'}"
        if not existing_token:
            raise RuntimeError("a tunnel slave owns the control port but its token is unavailable")
        restarted = await asyncio.to_thread(control_request, port, "restart", existing_token)
        return f"Slave tunnel restarted: {restarted.get('url') or '(resolving address)'}"
    if store.load_slave_link(port).get("running"):
        # Durable state can outlive a terminal the user closed. A failed live
        # probe proves there is no controller to adopt, so clear only ownership;
        # the last URL remains in app-data connection history.
        store.update_slave_state(
            port, running=False, state="stopped", clear_token=True,
            slave_pid=0, tunnel_pid=0,
        )

    opts = cfg.get(provider, {}) or {}
    binary_name = "cloudflared" if provider == "cloudflare" else "ngrok"
    configured_bin = str(opts.get("bin_path") or "").strip()
    binary = (configured_bin if configured_bin and Path(configured_bin).is_file()
              else shutil.which(configured_bin or binary_name))

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", port + 1))
    except OSError as exc:
        # Do not start a managed tunnel when an unresponsive slave (or another
        # service) may already own the control port: that could double-launch.
        raise RuntimeError(f"tunnel slave control port {port + 1} is already in use") from exc

    proc = None
    try:
        if not binary:
            raise RuntimeError(f"{binary_name} not found on PATH — install it first")
        token = secrets.token_urlsafe(32)
        slave_path = Path(__file__).resolve().parents[1] / "remote_access" / "slave.py"
        argv = [sys.executable, str(slave_path), "--port", str(port), "--token", token,
                "--provider", provider, "--bin", str(binary)]
        if provider == "cloudflare":
            if opts.get("quick"):
                argv.append("--quick")
            elif opts.get("tunnel"):
                argv += ["--name", str(opts["tunnel"])]
                hostname = str(opts.get("hostname") or "").strip()
                if hostname:
                    public_url = hostname if hostname.startswith("http") else f"https://{hostname}"
                    argv += ["--public-url", public_url]
            else:
                raise RuntimeError("no Cloudflare quick or named tunnel is configured")
        elif opts.get("domain"):
            argv += ["--name", str(opts["domain"])]
            domain = str(opts["domain"]).strip()
            argv += ["--public-url", domain if domain.startswith("http") else f"https://{domain}"]
        else:
            argv.append("--quick")

        store.update_slave_state(
            port, token=token, running=True, url="", provider=provider,
            state="starting", started_at=time.time(),
        )
        popen_kwargs: Dict[str, Any] = {
            "cwd": str(Path(__file__).resolve().parents[2]), "close_fds": True,
        }
        if sys.platform.startswith("win"):
            popen_kwargs["creationflags"] = 0x00000010  # CREATE_NEW_CONSOLE
        else:
            popen_kwargs["start_new_session"] = True
        proc = subprocess.Popen(argv, **popen_kwargs)

        # The controller starts serving before it launches cloudflared, but give
        # slow Windows/antivirus process startup enough room to settle.  A false
        # timeout used to kill the visible console and silently fall back to an
        # uncontrollable background tunnel.
        deadline = time.monotonic() + 10.0
        status = None
        while time.monotonic() < deadline:
            status = await asyncio.to_thread(probe_control, port, timeout=0.3)
            if status or proc.poll() is not None:
                break
            await asyncio.sleep(0.1)
        if not status:
            detail = read_status(port) or {}
            raise RuntimeError(str(detail.get("error") or "tunnel slave did not open its control port"))
        store.update_slave_state(
            port, running=True, url=str(status.get("url") or ""),
            provider=str(status.get("provider") or provider),
            state=str(status.get("state") or "starting"),
            slave_pid=int(status.get("pid") or proc.pid),
            tunnel_pid=int(status.get("tunnel_pid") or 0),
            started_at=float(status.get("started_at") or time.time()),
        )
        logger.info("Device action: detached %s tunnel slave launched (pid=%s)", provider, proc.pid)
        return f"Slave tunnel launched: {status.get('url') or '(resolving address)'}"
    except Exception as slave_error:
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=2.0)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        store.update_slave_state(port, running=False, url="", clear_token=True)
        # Do not replace a failed visible/controller-owned launch with the old
        # in-process tunnel.  That process survives differently, has no durable
        # control token, and becomes an orphan as soon as WebAgent restarts.
        logger.error("Tunnel slave launch failed: %s", slave_error)
        raise RuntimeError(str(slave_error))


async def _stop_slave_tunnel(*, job: dict, db: Any, payload: dict) -> str:
    if await _stop_slave_if_present():
        logger.info("Device action: stopped tunnel slave")
        return "slave tunnel stopped"
    return await _stop_tunnel(job=job, db=db, payload=payload)


register_action("stop_tunnel", _stop_tunnel)
register_action("slave_tunnel", _start_slave_tunnel)
register_action("slave_stop", _stop_slave_tunnel)


# ── Server / repo fleet control ───────────────────────────────────────────────
# Restart this device's server, or bring its OWN app repo up to date / commit +
# push it — the "run it on that box, not here" versions of the local Server Reset
# and the Source-Control ⭐ button. Each runs on the TARGET device's machine when
# its worker claims the job, reusing the same helpers the local UI routes use so
# behaviour is identical. All git actions are PINNED to the running app's own repo
# (project root) — never whatever repo that device's admin last selected — so a
# fleet "pull + restart" is deterministic across every instance.

async def _restart_server(*, job: dict, db: Any, payload: dict) -> str:
    from app.relauncher import trigger_restart
    # Schedule the exit with a few seconds' grace so the worker can mark THIS job
    # 'done' before the process dies. Without the grace the job would stay
    # 'claimed', get reclaimed after its lease expires, and restart the box again
    # in a loop. Returns immediately; the daemon thread exits the process after.
    res = trigger_restart(delay=3.0)
    if not res.get("auto_restart"):
        raise RuntimeError(res.get("reason") or "this device can't restart itself")
    logger.info("Device action: restarting server on this device")
    return res.get("message") or "server is restarting"


async def _git_pull(*, job: dict, db: Any, payload: dict) -> str:
    from app.api.github import _pin_to_project_root, pull_repo
    _pin_to_project_root()
    res = await pull_repo()
    if res.get("status") == "error":
        raise RuntimeError(res.get("message") or "git pull failed")
    msg = res.get("message") or "pulled"
    if res.get("backend_changed"):
        msg += " (backend files changed — a restart is recommended)"
    logger.info("Device action: git pull → %s", msg)
    return msg


async def _git_commit_push(*, job: dict, db: Any, payload: dict) -> str:
    from app.api.github import _pin_to_project_root, commit_and_push_repo
    _pin_to_project_root()
    res = await commit_and_push_repo(payload.get("message") or "")
    status = res.get("status")
    message = res.get("message") or status or "done"
    if status in ("error", "blocked"):
        raise RuntimeError(message)
    logger.info("Device action: commit+push → %s: %s", status, message)
    # Prefix the status (committed / nothing_to_commit) so the Instances page can
    # show a clear one-line outcome.
    return f"{status}: {message}" if status else message


register_action("restart_server", _restart_server)
register_action("git_pull", _git_pull)
register_action("git_commit_push", _git_commit_push)


# ── Kill switch — fleet-wide silence ─────────────────────────────────────────
# Pressing the header kill switch on ONE device kills that device's processes
# (runs, revivals, watchdog, polling, workers). These actions let every OTHER
# device do the same: the kill-switch API broadcasts a targeted job to each
# known device, whose worker claims it and engages its OWN local kill switch.
# Each target persists the flag locally, so it stays killed across its own
# restarts. The matching resume action restarts background services. The
# broadcast ONLY originates from the HTTP endpoint — these handlers call
# engage/disengage directly with no re-broadcast, so the fleet can't loop.

async def _kill_switch(*, job: dict, db: Any, payload: dict) -> str:
    from app.kill_switch import engage
    res = await engage()
    # This device's browsers should clear their spinners right away, not wait
    # for the next poll — the admin who pressed the switch owns the job.
    await _notify_owner_browsers(job, True)
    return f"kill switch engaged ({res.get('cancelled_runs', 0)} run(s) cancelled)"


async def _kill_switch_resume(*, job: dict, db: Any, payload: dict) -> str:
    from app.kill_switch import disengage
    await disengage()
    await _notify_owner_browsers(job, False)
    return "kill switch disengaged — background services restarted"


async def _notify_owner_browsers(job: dict, engaged: bool) -> None:
    """Fire-and-forget WS broadcast to the job owner's live browsers so their
    session lists repaint immediately (agentWs re-dispatches kill_switch as
    kill-switch-changed). Never raises."""
    try:
        owner = (job or {}).get("owner_user_id")
        if not owner:
            return
        from app.api.chat import notify_user
        await notify_user(owner, {"type": "kill_switch", "engaged": engaged})
    except Exception:  # noqa: BLE001
        pass


register_action("kill_switch", _kill_switch)
register_action("kill_switch_resume", _kill_switch_resume)
