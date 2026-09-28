"""Remote Access orchestrator.

Owns the active tunnel, computes the current public address for whichever method
is active, and runs a background watcher that re-pushes the signpost whenever a
managed tunnel's address changes (so the phone's fixed bookmark always resolves
to the live PC). Lifecycle hooks (``start_remote_access`` / ``stop_remote_access``)
are wired into app startup/shutdown.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
from typing import Optional

from . import netinfo, signpost, store, tunnels

logger = logging.getLogger(__name__)

_MANAGED = ("ngrok", "cloudflare")
_WATCH_INTERVAL = 10  # seconds between address-change checks
_RESOLVE_TRIES = 15   # ~15s to wait for a fresh tunnel to report its address


class RemoteAccessManager:
    def __init__(self) -> None:
        self._tunnel: Optional[tunnels.ManagedTunnel] = None
        self._method: str = "same_network"
        self._last_pushed_url: str = ""
        self._watch_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    # ── address resolution ──
    async def _remote_url_for(self, method: str, cfg: dict) -> str:
        if method in _MANAGED and self._tunnel:
            return await self._tunnel.resolve_public_url()
        if method == "tailscale":
            ts = tunnels.tailscale_status(cfg.get("tailscale", {}).get("bin_path", ""))
            if ts.get("ip"):
                return f"http://{ts['ip']}:{netinfo.get_port()}"
            return ""
        if method == "manual":
            return (cfg.get("manual", {}).get("public_url") or "").strip()
        if method == "same_network":
            return netinfo.primary_same_network_url()
        return ""

    async def _maybe_push(self, cfg: dict, url: str) -> None:
        sp = cfg.get("signpost", {})
        if not sp.get("enabled") or sp.get("role") not in ("client", "both"):
            return
        if not url or self._method == "same_network":
            return
        if url == self._last_pushed_url:
            return
        ok = await signpost.push_pointer(
            sp.get("server_url", ""), cfg.get("rendezvous_key", ""),
            cfg.get("push_token", ""), url, sp.get("label", ""),
        )
        if ok:
            self._last_pushed_url = url
            logger.info("Remote Access: pushed address to signpost (%s)", url)

    # ── lifecycle ──
    async def start_method(self, method: str, opts_override: Optional[dict] = None) -> dict:
        """Start a method. ``opts_override`` merges over the saved per-method
        options for THIS run only (nothing is persisted), so a caller can force
        e.g. a Cloudflare *quick* tunnel without clobbering the admin's saved
        named-tunnel settings."""
        async with self._lock:
            self._stop_tunnel()
            self._last_pushed_url = ""
            self._method = method
            cfg = store.update_config({"active_method": method})

            if method in _MANAGED:
                opts = dict(cfg.get(method, {}))
                if opts_override:
                    opts.update(opts_override)
                self._tunnel = tunnels.ManagedTunnel(method, netinfo.get_port(), opts)
                if not self._tunnel.start():
                    err = self._tunnel.error or "failed to start"
                    self._tunnel = None
                    return {"ok": False, "error": err}
                # Wait for the tunnel to report its address.
                url = ""
                for _ in range(_RESOLVE_TRIES):
                    if not self._tunnel.is_alive():
                        return {"ok": False, "error": self._tunnel.error or "tunnel exited",
                                "output": self._tunnel.recent_output()}
                    url = await self._tunnel.resolve_public_url()
                    if url:
                        break
                    await asyncio.sleep(1)

            url = await self._remote_url_for(method, cfg)
            await self._maybe_push(cfg, url)
            self._ensure_watcher()
            return {"ok": True, "public_url": url, "method": method}

    def _stop_tunnel(self) -> None:
        if self._tunnel:
            self._tunnel.stop()
            self._tunnel = None

    async def stop(self) -> dict:
        async with self._lock:
            self._stop_tunnel()
            self._last_pushed_url = ""
            return {"ok": True}

    # ── watcher ──
    def _ensure_watcher(self) -> None:
        if self._watch_task and not self._watch_task.done():
            return
        try:
            self._watch_task = asyncio.create_task(self._watch_loop())
        except RuntimeError:
            self._watch_task = None  # no running loop (shouldn't happen in-app)

    async def _watch_loop(self) -> None:
        while True:
            await asyncio.sleep(_WATCH_INTERVAL)
            try:
                if self._method not in _MANAGED or not self._tunnel:
                    continue
                if not self._tunnel.is_alive():
                    continue
                cfg = store.load_config()
                url = await self._tunnel.resolve_public_url()
                if url:
                    await self._maybe_push(cfg, url)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug("Remote Access watcher tick failed: %s", e)

    def stop_watcher(self) -> None:
        if self._watch_task and not self._watch_task.done():
            self._watch_task.cancel()
        self._watch_task = None

    # ── status for the card ──
    async def runtime_status(self) -> dict:
        running = bool(self._tunnel and self._tunnel.is_alive())
        url = ""
        if self._tunnel:
            url = await self._tunnel.resolve_public_url()
        return {
            "active_method": self._method,
            "running": running,
            "public_url": url,
            "error": self._tunnel.error if self._tunnel else "",
            "output": self._tunnel.recent_output() if self._tunnel else "",
        }


def tunnel_snapshot() -> dict:
    """A small, SYNCHRONOUS, event-loop-safe view of THIS device's Remote Access
    tunnel — stamped into the presence heartbeat (app/devices) so OTHER devices'
    Instances page can show whether a tunnel is set up / running here and its
    public address, and offer a remote Start.

    Never touches the network (no await, no subprocess): reads the live tunnel
    subprocess state + the saved config only. Best-effort — returns a
    'nothing configured' shape on any error. Fields:
      • provider     — 'cloudflare' | 'ngrok' | ''  (a *managed* method, else '')
      • method       — the raw active_method (may be same_network/manual/…)
      • configured   — a managed tunnel this device could actually start now
                       (helper binary present + runnable config)
      • running      — a managed tunnel is live right now
      • public_url   — the live address, or a named tunnel's fixed hostname even
                       when off, or '' (quick tunnels only have one while running)
    """
    try:
        cfg = store.load_config()
    except Exception:
        cfg = {}
    method = (cfg.get("active_method") or "same_network").strip()
    provider = method if method in _MANAGED else ""

    # A status file is only a hint: an abruptly closed terminal can leave a fresh
    # `starting` snapshot behind.  Prove active ownership through the controller
    # every time before presenting Stop Tunnel or a URL as live.
    slave_status = None
    slave_link = store.load_slave_link(netinfo.get_port())
    try:
        from .slave import probe_control, read_status
        file_status = read_status(netinfo.get_port())
        should_probe = bool(slave_link.get("running")) or bool(
            file_status and file_status.get("state") in ("starting", "running")
        )
        if should_probe:
            slave_status = probe_control(netinfo.get_port(), timeout=0.3)
    except Exception:
        slave_status = None

    mgr = get_manager()
    tun = mgr._tunnel
    running = bool(tun and tun.is_alive())
    public_url = (tun.public_url if tun else "") or ""

    configured = False
    try:
        if method == "cloudflare":
            opts = cfg.get("cloudflare", {}) or {}
            avail = tunnels.cloudflared_available(opts.get("bin_path", ""))
            runnable = bool(opts.get("quick") or opts.get("tunnel") or opts.get("hostname"))
            configured = bool(avail and runnable)
            # A NAMED tunnel has a fixed hostname we can show even while it's off.
            if not public_url:
                host = (opts.get("hostname") or "").strip()
                if host:
                    public_url = host if host.startswith("http") else f"https://{host}"
        elif method == "ngrok":
            opts = cfg.get("ngrok", {}) or {}
            configured = bool(tunnels.ngrok_available(opts.get("bin_path", "")))
    except Exception:
        configured = False

    # Keep the compatibility field consumed by the Instances UI, but source it
    # only from a fresh slave snapshot so an old ephemeral URL never looks live.
    headful_url = ""
    connected_at = ""
    remembered_url = str(slave_link.get("url") or "").strip()
    if remembered_url and not public_url:
        public_url = remembered_url
    if slave_status and slave_status.get("state") in ("starting", "running"):
        slave_provider = str(slave_status.get("provider") or "").strip()
        if slave_provider in _MANAGED:
            provider = slave_provider
            method = slave_provider
        running = True
        # A named Cloudflare tunnel may never print its fixed hostname. Keep the
        # configured URL calculated above until the slave reports a replacement.
        public_url = str(slave_status.get("url") or "").strip() or public_url
        headful_url = public_url
        try:
            started_at = float(slave_status.get("started_at") or 0)
            if started_at > 0:
                connected_at = datetime.datetime.fromtimestamp(
                    started_at, datetime.timezone.utc
                ).isoformat()
        except (TypeError, ValueError, OSError):
            connected_at = ""

    return {
        "provider": provider,
        "method": method,
        "configured": configured,
        "running": running,
        "public_url": public_url,
        "headful_url": headful_url,
        "connected_at": connected_at,
    }


_manager: Optional[RemoteAccessManager] = None


def get_manager() -> RemoteAccessManager:
    global _manager
    if _manager is None:
        _manager = RemoteAccessManager()
    return _manager


async def start_remote_access() -> None:
    """Adopt a live slave, else auto-start the configured managed tunnel."""
    cfg = store.load_config()
    mgr = get_manager()
    mgr._method = cfg.get("active_method", "same_network")
    try:
        from .slave import probe_control, read_status
        port = netinfo.get_port()
        file_status = read_status(port)
        slave_link = store.load_slave_link(port)
        should_probe = bool(slave_link.get("running")) or bool(
            file_status and file_status.get("state") in ("starting", "running")
        )
        slave_status = (
            await asyncio.to_thread(probe_control, port, timeout=1.0)
            if should_probe else None
        )
    except Exception:
        slave_status = None
    if slave_status and slave_status.get("state") in ("starting", "running"):
        url = str(slave_status.get("url") or "").strip()
        store.update_slave_state(
            netinfo.get_port(), running=True, url=url,
            provider=str(slave_status.get("provider") or ""),
            state=str(slave_status.get("state") or "running"),
            slave_pid=int(slave_status.get("pid") or 0),
            tunnel_pid=int(slave_status.get("tunnel_pid") or 0),
            started_at=float(slave_status.get("started_at") or 0),
        )
        logger.info("Remote Access adopted detached tunnel slave (pid=%s, url=%s)",
                    slave_status.get("pid"), url or "resolving")
        return
    if store.load_slave_link(netinfo.get_port()).get("running"):
        store.update_slave_state(
            netinfo.get_port(), running=False, state="stopped",
            clear_token=True, slave_pid=0, tunnel_pid=0,
        )
    if cfg.get("auto_start") and cfg.get("active_method") in _MANAGED:
        try:
            await mgr.start_method(cfg["active_method"])
            logger.info("Remote Access auto-started method '%s'", cfg["active_method"])
        except Exception as e:
            logger.warning("Remote Access auto-start failed: %s", e)


async def stop_remote_access() -> None:
    """Shutdown hook — stop the watcher and any managed tunnel."""
    mgr = get_manager()
    mgr.stop_watcher()
    try:
        await mgr.stop()
    except Exception:
        pass
