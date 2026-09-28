"""Remote Access admin API: /admin/remote-access/*

Drives the Remote Access card in App Config → App Settings. All endpoints are
admin-only — exposing this machine means exposing its (admin-gated) shell/file
tools, so configuration is locked to admins.
"""

from __future__ import annotations

import logging
import os
import subprocess
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.remote_access import netinfo, store, tunnels
from app.remote_access.manager import get_manager

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/remote-access", tags=["admin-remote-access"])


def _env_locked() -> bool:
    return os.environ.get("WEBAGENT_CONFIG_SOURCE", "").lower() == "env"


async def _require_admin(uid: str) -> None:
    # Honors open mode via the shared chokepoint: over a Cloudflare Tunnel the
    # page may carry no resolved user id, so a blank/non-admin caller in 'open'
    # mode is granted the bootstrap admin. Non-open modes still require a real
    # DB admin id. See app.auth.identity.resolve_admin_uid.
    from app.auth.identity import resolve_admin_uid
    if not await resolve_admin_uid(uid):
        raise HTTPException(status_code=403, detail="Admin required")


def _phone_url(cfg: Dict[str, Any]) -> str:
    sp = cfg.get("signpost", {})
    base = (sp.get("server_url") or "").rstrip("/")
    key = cfg.get("rendezvous_key", "")
    return f"{base}/go/{key}" if base and key else ""


# ── Models ──
class UserBody(BaseModel):
    requesting_user_id: str


class MethodBody(UserBody):
    method: str


class ConfigBody(UserBody):
    method: str
    config: Dict[str, Any] = {}


class SignpostBody(UserBody):
    config: Dict[str, Any] = {}


class AutoStartBody(UserBody):
    auto_start: bool


class AuthtokenBody(UserBody):
    authtoken: str


# ── Status ──
@router.get("/status")
async def status(requesting_user_id: str = ""):
    await _require_admin(requesting_user_id)
    cfg = store.load_config()
    mgr = get_manager()
    runtime = await mgr.runtime_status()

    primary = netinfo.primary_same_network_url()
    sn_urls = netinfo.same_network_urls()
    same_network = {
        "port": netinfo.get_port(),
        "primary_url": primary,
        "urls": sn_urls,
        "qr_svg": netinfo.qr_svg(primary),
        # Per-address entries so the card can stack every URL, each with its
        # own copy button + on-demand QR popup (read by remote-access.js).
        "entries": [{"url": u, "qr_svg": netinfo.qr_svg(u)} for u in sn_urls],
    }

    ts = tunnels.tailscale_status(cfg.get("tailscale", {}).get("bin_path", ""))

    methods = {
        "ngrok": {
            "available": tunnels.ngrok_available(cfg.get("ngrok", {}).get("bin_path", "")),
            "config": cfg.get("ngrok", {}),
        },
        "cloudflare": {
            "available": tunnels.cloudflared_available(cfg.get("cloudflare", {}).get("bin_path", "")),
            "config": cfg.get("cloudflare", {}),
        },
        "tailscale": {
            "available": ts.get("available", False),
            "ip": ts.get("ip", ""),
            "name": ts.get("name", ""),
            "running": ts.get("running", False),
            "url": f"http://{ts['ip']}:{netinfo.get_port()}" if ts.get("ip") else "",
        },
        "manual": {"config": cfg.get("manual", {})},
    }

    phone_url = _phone_url(cfg)
    return {
        "env_locked": _env_locked(),
        "active_method": cfg.get("active_method", "same_network"),
        "auto_start": cfg.get("auto_start", False),
        "same_network": same_network,
        "methods": methods,
        "runtime": runtime,
        "signpost": cfg.get("signpost", {}),
        "rendezvous_key": cfg.get("rendezvous_key", ""),
        "phone_url": phone_url,
        "phone_qr_svg": netinfo.qr_svg(phone_url) if phone_url else None,
    }


# ── Mutations ──
@router.post("/method")
async def set_method(body: MethodBody):
    await _require_admin(body.requesting_user_id)
    if _env_locked():
        raise HTTPException(status_code=403, detail="Config is env-locked")
    if body.method not in store.METHODS:
        raise HTTPException(status_code=400, detail="Unknown method")
    store.update_config({"active_method": body.method})
    return {"ok": True, "active_method": body.method}


@router.post("/config")
async def save_config(body: ConfigBody):
    await _require_admin(body.requesting_user_id)
    if _env_locked():
        raise HTTPException(status_code=403, detail="Config is env-locked")
    if body.method == "signpost":
        store.update_config({"signpost": body.config})
    elif body.method in store.METHODS:
        store.update_config({body.method: body.config})
    else:
        raise HTTPException(status_code=400, detail="Unknown method")
    return {"ok": True}


@router.post("/auto-start")
async def set_auto_start(body: AutoStartBody):
    await _require_admin(body.requesting_user_id)
    if _env_locked():
        raise HTTPException(status_code=403, detail="Config is env-locked")
    store.update_config({"auto_start": bool(body.auto_start)})
    return {"ok": True, "auto_start": bool(body.auto_start)}


@router.post("/start")
async def start(body: MethodBody):
    await _require_admin(body.requesting_user_id)
    if _env_locked():
        raise HTTPException(status_code=403, detail="Config is env-locked")
    method = body.method or store.load_config().get("active_method", "same_network")
    result = await get_manager().start_method(method)
    return result


@router.post("/stop")
async def stop(body: UserBody):
    await _require_admin(body.requesting_user_id)
    return await get_manager().stop()


@router.post("/test")
async def test(body: MethodBody):
    await _require_admin(body.requesting_user_id)
    cfg = store.load_config()
    m = body.method
    if m == "ngrok":
        ok = tunnels.ngrok_available(cfg.get("ngrok", {}).get("bin_path", ""))
        return {"ok": ok, "detail": "ngrok found on PATH" if ok else "ngrok not installed / not on PATH"}
    if m == "cloudflare":
        ok = tunnels.cloudflared_available(cfg.get("cloudflare", {}).get("bin_path", ""))
        return {"ok": ok, "detail": "cloudflared found" if ok else "cloudflared not installed / not on PATH"}
    if m == "tailscale":
        ts = tunnels.tailscale_status(cfg.get("tailscale", {}).get("bin_path", ""))
        if not ts.get("available"):
            return {"ok": False, "detail": "tailscale not installed"}
        if not ts.get("ip"):
            return {"ok": False, "detail": "tailscale installed but not connected (run: tailscale up)"}
        return {"ok": True, "detail": f"reachable at http://{ts['ip']}:{netinfo.get_port()}"}
    if m == "manual":
        url = (cfg.get("manual", {}).get("public_url") or "").strip()
        if not url:
            return {"ok": False, "detail": "no address entered"}
        reachable = await tunnels.probe_reachable(url)
        return {"ok": reachable, "detail": "reachable" if reachable else "no response from that address"}
    if m == "same_network":
        return {"ok": True, "detail": netinfo.primary_same_network_url()}
    raise HTTPException(status_code=400, detail="Unknown method")


@router.post("/regenerate-key")
async def regenerate_key(body: UserBody):
    await _require_admin(body.requesting_user_id)
    if _env_locked():
        raise HTTPException(status_code=403, detail="Config is env-locked")
    keys = store.regenerate_keys()
    cfg = store.load_config()
    return {"ok": True, "rendezvous_key": keys["rendezvous_key"], "phone_url": _phone_url(cfg)}


@router.post("/ngrok-authtoken")
async def ngrok_authtoken(body: AuthtokenBody):
    """Convenience: stash an ngrok authtoken into ngrok's own config.

    WebAgent never stores the token — it just runs ``ngrok config add-authtoken``
    so the user doesn't have to open a terminal.
    """
    await _require_admin(body.requesting_user_id)
    token = (body.authtoken or "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="Missing authtoken")
    binp = tunnels._resolve_bin("ngrok", store.load_config().get("ngrok", {}).get("bin_path", ""))
    if not binp:
        return {"ok": False, "detail": "ngrok not installed / not on PATH"}
    try:
        r = subprocess.run([binp, "config", "add-authtoken", token],
                           capture_output=True, text=True, timeout=20)
        if r.returncode == 0:
            return {"ok": True, "detail": "authtoken saved to ngrok config"}
        return {"ok": False, "detail": (r.stderr or r.stdout or "ngrok error").strip()[:300]}
    except Exception as e:
        return {"ok": False, "detail": str(e)}
