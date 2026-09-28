"""Tunnel supervisors for Remote Access.

Two of the methods the card offers are *managed* — the app starts, watches and
stops the tunnel program itself:

* **ngrok** — ``ngrok http <port>``; the public address is read from ngrok's
  own local API (127.0.0.1:4040). With a reserved ``--domain`` the address is
  stable; without one it changes on each restart.
* **cloudflared** — a *named* tunnel (``cloudflared tunnel run <name>``) gives a
  fixed hostname you mapped in Cloudflare; a *quick* tunnel prints an ephemeral
  ``trycloudflare.com`` address we parse from its output.

Two are *status only* — the app can't run them for you:

* **tailscale** — a system service; we just read the device's tailnet address.
* **manual** (port-forward) — you supply the address; we probe reachability.

Secrets never live here: ngrok and cloudflared read their own credentials from
their own config, set up once by the user.
"""

from __future__ import annotations

import collections
import logging
import re
import shutil
import subprocess
import sys
import threading
from typing import Deque, List, Optional

import httpx

logger = logging.getLogger(__name__)

_TRYCF_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")

# Hide the console window when spawning helpers on Windows.
if sys.platform == "win32":
    _SPAWN_FLAGS = 0x08000000  # CREATE_NO_WINDOW
else:
    _SPAWN_FLAGS = 0


def _resolve_bin(name: str, configured: str = "") -> Optional[str]:
    """Absolute path to a helper binary, honoring a configured override."""
    if configured:
        return configured if shutil.which(configured) or _exists(configured) else None
    found = shutil.which(name)
    return found


def _exists(path: str) -> bool:
    try:
        import os
        return bool(path) and os.path.isfile(path)
    except Exception:
        return False


def ngrok_available(bin_path: str = "") -> bool:
    return _resolve_bin("ngrok", bin_path) is not None


def cloudflared_available(bin_path: str = "") -> bool:
    return _resolve_bin("cloudflared", bin_path) is not None


def tailscale_available(bin_path: str = "") -> bool:
    return _resolve_bin("tailscale", bin_path) is not None


class ManagedTunnel:
    """A supervised tunnel subprocess (ngrok or cloudflared)."""

    def __init__(self, provider: str, port: int, opts: dict):
        self.provider = provider
        self.port = port
        self.opts = opts or {}
        self.proc: Optional[subprocess.Popen] = None
        self._out: Deque[str] = collections.deque(maxlen=200)
        self._public_url: str = ""
        self._drain_thread: Optional[threading.Thread] = None
        self._error: str = ""

    # ── argv builders ──
    def _argv(self) -> Optional[List[str]]:
        if self.provider == "ngrok":
            binp = _resolve_bin("ngrok", self.opts.get("bin_path", ""))
            if not binp:
                self._error = "ngrok not found on PATH"
                return None
            argv = [binp, "http", str(self.port), "--log", "stdout"]
            if self.opts.get("domain"):
                argv += ["--domain", self.opts["domain"]]
            if self.opts.get("region"):
                argv += ["--region", self.opts["region"]]
            return argv
        if self.provider == "cloudflare":
            binp = _resolve_bin("cloudflared", self.opts.get("bin_path", ""))
            if not binp:
                self._error = "cloudflared not found on PATH"
                return None
            if self.opts.get("quick"):
                return [binp, "tunnel", "--url", f"http://localhost:{self.port}"]
            tunnel = self.opts.get("tunnel", "")
            if not tunnel:
                self._error = "no cloudflare tunnel name configured"
                return None
            # Named tunnel → fixed hostname configured in Cloudflare.
            return [binp, "tunnel", "run", tunnel]
        self._error = f"unknown managed provider {self.provider}"
        return None

    def start(self) -> bool:
        argv = self._argv()
        if not argv:
            return False
        # Named cloudflared has a known hostname up front.
        if self.provider == "cloudflare" and not self.opts.get("quick"):
            host = (self.opts.get("hostname") or "").strip()
            if host:
                self._public_url = host if host.startswith("http") else f"https://{host}"
        try:
            self.proc = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                creationflags=_SPAWN_FLAGS,
            )
        except Exception as e:
            self._error = f"failed to launch: {e}"
            logger.warning("ManagedTunnel start failed: %s", e)
            return False
        self._drain_thread = threading.Thread(target=self._drain, daemon=True)
        self._drain_thread.start()
        return True

    def _drain(self) -> None:
        if not self.proc or not self.proc.stdout:
            return
        try:
            for line in self.proc.stdout:
                line = line.rstrip()
                if not line:
                    continue
                self._out.append(line)
                if self.provider == "cloudflare" and self.opts.get("quick") and not self._public_url:
                    m = _TRYCF_RE.search(line)
                    if m:
                        self._public_url = m.group(0)
        except Exception:
            pass

    async def resolve_public_url(self) -> str:
        """Best current public address; queries ngrok's local API when needed."""
        if self._public_url:
            return self._public_url
        if self.provider == "ngrok":
            url = await _ngrok_api_url()
            if url:
                self._public_url = url
        return self._public_url

    def is_alive(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)

    @property
    def public_url(self) -> str:
        """The already-resolved public address, read synchronously (no network).
        Unlike ``resolve_public_url`` this never queries ngrok's API — it returns
        whatever has been learned so far (parsed from stdout / known up front for a
        named tunnel), so it's safe to call from the event loop e.g. when stamping
        the presence heartbeat."""
        return self._public_url

    def recent_output(self, n: int = 12) -> str:
        return "\n".join(list(self._out)[-n:])

    @property
    def error(self) -> str:
        return self._error

    def stop(self) -> None:
        p = self.proc
        if not p:
            return
        try:
            p.terminate()
            try:
                p.wait(timeout=5)
            except Exception:
                p.kill()
        except Exception:
            pass
        finally:
            self.proc = None


async def _ngrok_api_url() -> str:
    """Read the current https public_url from ngrok's local API (127.0.0.1:4040)."""
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            resp = await client.get("http://127.0.0.1:4040/api/tunnels")
        if resp.status_code != 200:
            return ""
        tunnels = resp.json().get("tunnels", [])
        https = [t for t in tunnels if t.get("proto") == "https"]
        chosen = https[0] if https else (tunnels[0] if tunnels else None)
        return chosen.get("public_url", "") if chosen else ""
    except Exception:
        return ""


def tailscale_status(bin_path: str = "") -> dict:
    """Device's tailnet IPv4 + DNS name, or availability=False if not installed."""
    binp = _resolve_bin("tailscale", bin_path)
    if not binp:
        return {"available": False, "ip": "", "name": "", "running": False}
    out = {"available": True, "ip": "", "name": "", "running": False}
    try:
        r = subprocess.run([binp, "ip", "-4"], capture_output=True, text=True, timeout=6)
        ip = (r.stdout or "").strip().splitlines()
        if ip:
            out["ip"] = ip[0].strip()
            out["running"] = True
    except Exception:
        pass
    try:
        import json as _json
        r = subprocess.run([binp, "status", "--json"], capture_output=True, text=True, timeout=6)
        data = _json.loads(r.stdout or "{}")
        myself = data.get("Self", {}) or {}
        out["name"] = (myself.get("DNSName", "") or "").rstrip(".")
        if myself.get("Online"):
            out["running"] = True
    except Exception:
        pass
    return out


async def probe_reachable(url: str) -> bool:
    """True if ``url`` answers an HTTP request (used for the manual/port-forward check)."""
    if not url:
        return False
    try:
        async with httpx.AsyncClient(timeout=6.0, follow_redirects=True, verify=False) as client:
            resp = await client.get(url.rstrip("/") + "/health")
        return resp.status_code < 500
    except Exception:
        try:
            async with httpx.AsyncClient(timeout=6.0, follow_redirects=True, verify=False) as client:
                resp = await client.get(url)
            return resp.status_code < 500
        except Exception:
            return False
