"""Admin API for the Tunnel Link panel (App Settings → Tunnel).

Talks to the hosted relay (the separate webagent-tunnel service) on the admin's
behalf: registers this instance, keeps its Cloudflare tunnel address current,
and mints short-lived pairing codes (returned with an inline QR SVG the panel
shows). The instance_secret stays here — it is never sent to the browser.

Mirrors app/admin/remote_access.py: an admin-gated router, requesting_user_id in
the body/query, JSON-file config via app/tunnel_link/store.py.
"""
from __future__ import annotations

from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.tunnel_link import store

router = APIRouter(prefix="/admin/tunnel-link", tags=["admin-tunnel-link"])

_RELAY_TIMEOUT = 12.0


async def _require_admin(uid: str) -> None:
    # Honors open mode via the shared chokepoint, so the Tunnel Link panel works
    # over a Cloudflare Tunnel where the page carries no resolved user id. See
    # app.auth.identity.resolve_admin_uid.
    from app.auth.identity import resolve_admin_uid
    if not await resolve_admin_uid(uid):
        raise HTTPException(status_code=403, detail="Admin required")


def _valid_https(value: str) -> bool:
    try:
        p = urlparse((value or "").strip())
    except (ValueError, AttributeError):
        return False
    return p.scheme == "https" and bool(p.netloc)


def _valid_base(value: str) -> bool:
    try:
        p = urlparse((value or "").strip())
    except (ValueError, AttributeError):
        return False
    return p.scheme in ("http", "https") and bool(p.netloc)


def _qr_svg(data: str):
    """Reuse the remote-access QR generator (optional `qrcode` dependency)."""
    try:
        from app.remote_access import netinfo
        return netinfo.qr_svg(data)
    except Exception:
        return None


def _public_status(cfg: dict, *, extra: dict | None = None) -> dict:
    """The shape the panel consumes — never includes the instance_secret."""
    base = (cfg.get("relay_base_url") or "").rstrip("/")
    registered = bool(cfg.get("instance_id")) and cfg.get("registered_relay") == base
    out = {
        "relay_base_url": base,
        "tunnel_url": cfg.get("tunnel_url") or "",
        "label": cfg.get("label") or "",
        "registered": registered,
        "viewer_url": base,  # where the phone goes to scan/link
    }
    if extra:
        out.update(extra)
    return out


async def _relay_register(base: str, label: str, tunnel_url: str) -> dict:
    async with httpx.AsyncClient(timeout=_RELAY_TIMEOUT) as client:
        r = await client.post(
            f"{base}/api/v1/instances/register",
            json={"label": label or None, "tunnel_url": tunnel_url or None},
        )
    if r.status_code >= 400:
        raise HTTPException(status_code=502, detail=_relay_error(r))
    return r.json()


async def _relay_update(base: str, iid: str, secret: str, label: str, tunnel_url: str) -> None:
    async with httpx.AsyncClient(timeout=_RELAY_TIMEOUT) as client:
        r = await client.put(
            f"{base}/api/v1/instances/{iid}",
            headers={"X-Instance-Secret": secret},
            json={"label": label or None, "tunnel_url": tunnel_url or None},
        )
    if r.status_code >= 400:
        raise HTTPException(status_code=502, detail=_relay_error(r))


async def _relay_pairing(base: str, iid: str, secret: str) -> dict:
    async with httpx.AsyncClient(timeout=_RELAY_TIMEOUT) as client:
        r = await client.post(
            f"{base}/api/v1/instances/{iid}/pairing-codes",
            headers={"X-Instance-Secret": secret},
        )
    if r.status_code >= 400:
        raise HTTPException(status_code=502, detail=_relay_error(r))
    return r.json()


def _relay_error(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        return "Relay: " + str(body.get("detail") or body.get("error") or resp.text)
    except ValueError:
        return f"Relay returned HTTP {resp.status_code}"


# --------------------------------------------------------------------------

class _AdminBody(BaseModel):
    requesting_user_id: str = ""


class SaveBody(_AdminBody):
    relay_base_url: str = ""
    tunnel_url: str = ""
    label: str = ""


@router.get("/status")
async def get_status(requesting_user_id: str = ""):
    await _require_admin(requesting_user_id)
    return _public_status(store.load_config())


@router.post("/save")
async def save(body: SaveBody):
    await _require_admin(body.requesting_user_id)

    base = (body.relay_base_url or "").strip().rstrip("/")
    tunnel_url = (body.tunnel_url or "").strip()
    label = (body.label or "").strip()

    if not _valid_base(base):
        raise HTTPException(status_code=400, detail="Relay address must be an http(s) URL")
    if tunnel_url and not _valid_https(tunnel_url):
        raise HTTPException(status_code=400, detail="Tunnel address must be an https:// URL")

    cfg = store.load_config()
    have_creds = bool(cfg.get("instance_id")) and bool(cfg.get("instance_secret"))
    same_relay = cfg.get("registered_relay") == base

    if have_creds and same_relay:
        # Already known to this relay — just refresh our address/label there.
        await _relay_update(base, cfg["instance_id"], cfg["instance_secret"], label, tunnel_url)
        cfg.update({"relay_base_url": base, "tunnel_url": tunnel_url, "label": label})
    else:
        # First time, or the relay changed → register afresh.
        created = await _relay_register(base, label, tunnel_url)
        cfg.update({
            "relay_base_url": base,
            "instance_id": created.get("instance_id", ""),
            "instance_secret": created.get("instance_secret", ""),
            "registered_relay": base,
            "tunnel_url": tunnel_url,
            "label": label,
        })

    store.save_config(cfg)
    return _public_status(cfg)


@router.post("/pairing-code")
async def pairing_code(body: _AdminBody):
    await _require_admin(body.requesting_user_id)
    cfg = store.load_config()
    base = (cfg.get("relay_base_url") or "").rstrip("/")
    if not (cfg.get("instance_id") and cfg.get("instance_secret") and cfg.get("registered_relay") == base):
        raise HTTPException(status_code=400, detail="Save your tunnel settings first")
    if not cfg.get("tunnel_url"):
        raise HTTPException(status_code=400, detail="Add your tunnel address first")

    result = await _relay_pairing(base, cfg["instance_id"], cfg["instance_secret"])
    link = result.get("link") or ""
    return {
        "link": link,
        "qr_svg": _qr_svg(link),
        "expires_at": result.get("expires_at"),
        "ttl_seconds": result.get("ttl_seconds"),
    }


@router.post("/reset")
async def reset(body: _AdminBody):
    """Forget the relay registration (keeps the entered relay/tunnel addresses)."""
    await _require_admin(body.requesting_user_id)
    cfg = store.load_config()
    cfg.update({"instance_id": "", "instance_secret": "", "registered_relay": ""})
    store.save_config(cfg)
    return _public_status(cfg)
