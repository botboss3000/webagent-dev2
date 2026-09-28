"""Instances — drop-in BACKEND for the admin Instances page: /admin/instances/*

This ONE router powers the whole Instances page (ui/main-panel/app/), which
replaced two older pages (Database & Devices + Server Manager). It has three
concerns, all admin-only:

  • Connection health / config for THIS device — the active database + secrets
    vault, each with a LIVE reachability probe, plus the shared/local
    classification and the silent-fallback flag. The connect / test / activate
    CONTROLS themselves reuse the existing /admin/storage/* endpoints (this router
    never re-implements them); we only add the probes + classification on top.
  • The device-presence registry — every computer running WebAgent against the
    shared database (app/devices/): auto-discovered, online/offline, each self-
    reporting its platform + code repo. These render as the fleet tiles.
  • Cloud VMs — live API control of servers in a connected cloud account (list /
    start / stop / delete), reusing the deploy subsystem (app/deploy/), plus the
    cloud-ACCOUNT sign-in (connect / disconnect) and a reachability ping. Each VM
    carries the repo it runs, overlaid from the ``instances`` DB table (shared).

Discovered + mounted by the page catalog (app/ui_pages/__init__.py
discover_routers, via this folder's page.json `router` field), so the API comes
and goes with the folder — NO edit to app/main.py. `.py` files under ui/ are never
served to the browser. Endpoints are admin-only via `resolve_admin_uid` (honours
open mode), mirroring app/api/deploy.py.

What lives where:
  • DB + vault config → the /admin/storage/* endpoints + app/db / app/secrets.
  • Cloud account keys + saved project/zone → the SAME encrypted vault + deploy.json
    the Deploy card uses (app/deploy/); never returned to the browser.
  • Per-VM metadata (repo, label, HTTPS domains, …) → the ``instances`` DB table
    (app/db/instance_meta.py), shared across all devices via the database.

NOTE: the old Server Manager's manually-tracked "machines" + "sites" (hand-typed
boxes / URLs) were intentionally dropped — this page is auto-discovered instances +
cloud infrastructure you control, not a manual asset inventory.
REMOVE-WHEN: the Instances view is dropped from the admin page catalog.
"""

from __future__ import annotations

import asyncio
import datetime
import hmac
import json
import logging
import re
import socket
import ssl
import os
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.deploy import credentials, manager
from app.deploy import store as deploy_store
from app.deploy.base import done, ev
from app.deploy.registry import get_provider

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/instances", tags=["admin-instances"])

# This-device Settings page catalog. Connected-instance cards never read it.
_APP_PAGES_PATH = Path(__file__).resolve().parents[3] / "data" / "config" / "app-pages.json"
_APP_PAGE_ROOT = Path(__file__).resolve().parent / "settings" / "pages"


def _discover_app_pages() -> Dict[str, Dict[str, Any]]:
    """Read first-class Settings page descriptors without trusting persisted metadata."""
    pages: Dict[str, Dict[str, Any]] = {
        "settings": {"label": "Settings", "icon": "settings-2", "description": "Configure this WebAgent installation.", "group": "settings", "runtime": "settings"}
    }
    for descriptor in sorted(_APP_PAGE_ROOT.glob("*/page.json")):
        try:
            item = json.loads(descriptor.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        page_id = item.get("id")
        if not isinstance(page_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", page_id) or page_id in pages:
            continue
        pages[page_id] = {
            "label": str(item.get("label") or page_id.replace("-", " ").title()),
            "icon": str(item.get("icon") or "circle"),
            "description": str(item.get("description") or ""),
            "group": str(item.get("group") or "other"),
            "runtime": str(item.get("runtime") or "settings"),
            "page": str(item.get("page") or page_id),
            "anchor": str(item.get("anchor") or ""),
            "paPage": str(item.get("paPage") or "app_settings"),
            "keywords": item.get("keywords") if isinstance(item.get("keywords"), list) else [],
        }
    return pages


def _app_pages_config():
    pages = _discover_app_pages()
    stored: Dict[str, Any] = {}
    try:
        raw = json.loads(_APP_PAGES_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, dict): stored = raw
    except (OSError, ValueError, TypeError):
        pass
    stored_groups = stored.get("groups") if isinstance(stored.get("groups"), list) else []
    groups, assigned = [], set()
    for raw_group in stored_groups:
        if not isinstance(raw_group, dict): continue
        ids = [page_id for page_id in raw_group.get("pageIds", []) if page_id in pages and page_id != "settings" and page_id not in assigned]
        if not ids: continue
        assigned.update(ids)
        groups.append({"id": str(raw_group.get("id") or "pages"), "label": str(raw_group.get("label") or "Pages"), "icon": str(raw_group.get("icon") or "panels-top-left"), "pageIds": ids})
    # A newly dropped page lands after configured siblings in its declared group.
    for page_id, page in pages.items():
        if page_id == "settings" or page_id in assigned: continue
        group_id = page.get("group") or "other"
        group = next((entry for entry in groups if entry["id"] == group_id), None)
        if group is None:
            group = {"id": group_id, "label": str(group_id).replace("-", " ").title(), "icon": "panels-top-left", "pageIds": []}
            groups.append(group)
        group["pageIds"].append(page_id)
    header = stored.get("headerPageIds") if isinstance(stored.get("headerPageIds"), list) else []
    return {"version": 2, "headerPageIds": ["settings"] + [page_id for page_id in header if page_id in pages and page_id != "settings" and header.count(page_id) == 1], "groups": groups, "pages": pages}

@router.get("/app-pages")
async def get_app_pages(requesting_user_id: str = ""):
    await _require_admin(requesting_user_id)
    return _app_pages_config()

@router.post("/app-pages")
async def save_app_pages(payload: Dict[str, Any]):
    await _require_admin(str(payload.get("requesting_user_id") or ""))
    if not isinstance(payload.get("headerPageIds"), list) or not isinstance(payload.get("groups"), list):
        raise HTTPException(status_code=400, detail="Invalid app pages configuration")
    config = _app_pages_config()
    config["headerPageIds"], config["groups"] = payload["headerPageIds"], payload["groups"]
    known, header = set(config["pages"]), config["headerPageIds"]
    config["headerPageIds"] = ["settings"] + [p for p in header if p in known and p != "settings"]
    _APP_PAGES_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _APP_PAGES_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, _APP_PAGES_PATH)
    return _app_pages_config()


# ── Embedded Dashboard tab backend ───────────────────────────────────────────
# The Dashboard is a TAB inside this page (ui/main-panel/app/dashboard/),
# not a top-level admin view — ui_pages discovery only scans top-level page
# folders (ui/admin-tools/<id>/page.json), so its server.py is registered here
# via this page.json's ``routers`` list instead. ui_pages.discover_routers()
# imports it and main.py mounts it DIRECTLY on the app, so the dashboard keeps
# its OWN /admin/dashboard/* prefix. (Do NOT include_router it into this page's
# router — FastAPI nests an included router under the parent's prefix, which
# would rewrite every endpoint to /admin/instances/admin/dashboard/* and break
# the frontend. See the discover_routers docstring in app/ui_pages/__init__.py.)


# ── DB-backed metadata helpers ─────────────────────────────────────────────────
async def _meta_get(ref: str) -> dict:
    """Read instance metadata. Returns {} when absent."""
    from app.db.instance_meta import get_instance
    inst = await get_instance(ref)
    if not inst:
        return {}
    return (inst.get("metadata") or {})


async def _meta_upsert(ref: str, **fields):
    """Persist one or more fields (kind, label, repo, domains, …)."""
    from app.db.instance_meta import upsert_instance
    await upsert_instance(ref, **fields)


async def _require_admin(uid: str) -> None:
    # Honours open mode via the shared chokepoint (mirrors app/api/deploy.py).
    from app.auth.identity import resolve_admin_uid
    if not await resolve_admin_uid(uid):
        raise HTTPException(status_code=403, detail="Admin required")


# ── P2P + SSH connection facts (for the Overview "Connection" section) ────────
# Remote instances reach the host two ways: the P2P sync channel (app/p2p peer
# store) and SSH (a saved login from deploy). The fleet /devices endpoint matches
# peers by remote instance id; cloud VMs match by IP (peer URL host) and by VM
# name/IP against the saved ssh_vm server profiles.

def _p2p_peer_info(p: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize one p2p peer store row into the shape the Overview renders."""
    sync = dict(p.get("sync") or {})
    return {
        "id": p.get("id", ""),
        "name": p.get("name", ""),
        "url": p.get("url", ""),
        "status": sync.get("status") or p.get("status") or "pending",
        "last_sync_at": sync.get("last_sync_at") or p.get("last_sync_at") or None,
        "last_sync_files": sync.get("last_sync_files") or p.get("last_sync_files") or 0,
    }


def _p2p_peers_by_remote_id() -> Dict[str, Dict[str, Any]]:
    """Index p2p peers by the remote instance id they link to."""
    out: Dict[str, Dict[str, Any]] = {}
    try:
        from app.p2p import store as p2p_store
        for _p in p2p_store.list_peers():
            _sync = _p.get("sync") or {}
            _rid = str(_sync.get("remote_instance_id") or _p.get("remote_instance_id") or "").strip()
            if _rid:
                out[_rid] = _p2p_peer_info(_p)
    except Exception:
        pass
    return out


def _p2p_peers_by_ip() -> Dict[str, Dict[str, Any]]:
    """Index p2p peers by the host part of their peer URL (cloud VMs match on IP)."""
    out: Dict[str, Dict[str, Any]] = {}
    try:
        from app.p2p import store as p2p_store
        for _p in p2p_store.list_peers():
            _url = str(_p.get("url") or "").strip().rstrip("/")
            if not _url:
                continue
            _host = _url.split("//")[-1].split("/")[0].split(":")[0]
            if _host:
                out[_host] = _p2p_peer_info(_p)
    except Exception:
        pass
    return out


def _p2p_peers_list() -> List[Dict[str, Any]]:
    """Every P2P peer, with its URL host extracted and its saved SSH login
    attached. The Instances page uses this to surface peers whose VM no longer
    exists in any connected cloud — they still show up as ghost tiles instead of
    silently vanishing, so a stale/offline peer link is never invisible."""
    out: List[Dict[str, Any]] = []
    try:
        from app.p2p import store as p2p_store
        for _p in p2p_store.list_peers():
            info = _p2p_peer_info(_p)
            _url = str(info.get("url") or "").strip().rstrip("/")
            _host = _url.split("//")[-1].split("/")[0].split(":")[0] if _url else ""
            info["host"] = _host
            _short = str(info.get("name") or "").split(".")[0]
            info["ssh"] = _ssh_profile_for_vm("", _short, _host) or None
            out.append(info)
    except Exception:
        pass
    return out


# ══════════════════════════════════════════════════════════════════════════════
# Connection health — the active database + secrets vault (was Database & Devices)
# ══════════════════════════════════════════════════════════════════════════════
# Application-Data backends that put the data on a networked database more than
# one machine can point at (everything except the single-file local SQLite).
_REMOTE_DB = {"postgres", "aws_rds", "gcp_cloud_sql", "azure_postgres", "neon"}
# How a vault choice travels across devices: cloud vaults are reachable from every
# machine (shared); the OS keyring is per-machine (local); the in-DB vault rides
# along inside the database; env is injected by the deployment.
_VAULT_REACH = {
    "hashicorp_vault": "shared", "azure_key_vault": "shared",
    "gcp_secret_manager": "shared", "aws_secrets_manager": "shared",
    "os_keyring": "local", "inline_db": "in_db", "env": "external",
}


def _db_target_summary(active: Dict[str, Any]) -> str:
    """A short human label for where the active database lives."""
    prov = (active.get("provider") or "sqlite").strip()
    if prov == "sqlite":
        return active.get("database") or "Local file (data/db/local.db)"
    host = active.get("host") or "?"
    port = active.get("port") or 5432
    dbname = active.get("database") or "?"
    user = active.get("username") or ""
    who = f"{user}@" if user else ""
    return f"{who}{host}:{port}/{dbname}"


def _vault_locator_summary(provider: str, configs: Dict[str, Any]) -> str:
    """A short human label for the active vault's address/project/region."""
    cfg = configs.get(provider) or {}
    if provider == "hashicorp_vault":
        return cfg.get("address") or "(address not set)"
    if provider == "azure_key_vault":
        return cfg.get("vault_url") or "(vault URL not set)"
    if provider == "gcp_secret_manager":
        return cfg.get("project") or "(project not set)"
    if provider == "aws_secrets_manager":
        return cfg.get("region") or "(region not set)"
    if provider == "os_keyring":
        return "This machine's credential store"
    if provider == "inline_db":
        return "Inside the app database"
    if provider == "env":
        return "Environment variables"
    return provider


async def _probe_db() -> Dict[str, Any]:
    """Cheap liveness probe of the ACTIVE database — one tiny row read. Read-only;
    never writes or switches anything.

    The raw client's ``.execute()`` is a BLOCKING network round-trip. Run it on a
    worker thread (``asyncio.to_thread``) so a slow/remote database can't freeze the
    whole event loop for the duration — otherwise this admin probe stalls every
    other request (tiles, static assets, the New Deployment tab) behind it."""
    def _sync_probe() -> None:
        from app.db import get_db
        raw = get_db().get_raw_client()
        raw.table("sessions").select("id").limit(1).execute()
    try:
        await asyncio.to_thread(_sync_probe)
        return {"reachable": True, "detail": "Connected"}
    except Exception as e:  # unreachable / not bootstrapped / wrong creds
        return {"reachable": False, "detail": (str(e) or type(e).__name__)[:200]}


async def _probe_vault() -> Dict[str, Any]:
    """Liveness probe of the ACTIVE secrets vault via its test_connection()."""
    try:
        from app.secrets import get_secrets
        res = await get_secrets().test_connection()
        if isinstance(res, dict):
            return {"reachable": bool(res.get("ok", True)),
                    "detail": res.get("detail") or res.get("error") or "Reachable"}
        return {"reachable": bool(res), "detail": "Reachable" if res else "Unreachable"}
    except Exception as e:
        return {"reachable": False, "detail": (str(e) or type(e).__name__)[:200]}


# This machine's public IPv4, cached so the metadata probe doesn't fire on every
# poll. Set WEBAGENT_PUBLIC_IP in .env to pin it (e.g. a static IP); otherwise it
# is discovered from the GCE metadata server, which answers on any Google VM with
# zero credentials. Anything else (bare metal, other clouds, a LAN box) yields ""
# and the instances page simply omits the fact.
_PUBLIC_IP_CACHE: Dict[str, Any] = {"ts": 0.0, "ip": ""}


async def _detect_public_ip() -> str:
    import time
    now = time.time()
    if now - float(_PUBLIC_IP_CACHE.get("ts", 0.0)) < 600:
        return str(_PUBLIC_IP_CACHE.get("ip", ""))
    ip = ""
    try:
        import os
        ip = os.environ.get("WEBAGENT_PUBLIC_IP", "").strip()
        if not ip:
            import httpx
            r = await httpx.get(
                "http://metadata.google.internal/computeMetadata/v1/instance/"
                "network-interfaces/0/accessconfigs/0/external-ip",
                headers={"Metadata-Flavor": "Google"}, timeout=2.0)
            if r.status_code == 200:
                ip = r.text.strip()
    except Exception:
        ip = ""
    _PUBLIC_IP_CACHE["ts"] = now
    _PUBLIC_IP_CACHE["ip"] = ip
    return ip


@router.get("/backend-status")
async def backend_status(requesting_user_id: str = ""):
    await _require_admin(requesting_user_id)
    # Reuse the storage page's own config builder so DB + vault facts come from a
    # single source of truth; we only add live probes + the shared/local split.
    from app.admin.storage import get_storage_config
    cfg = await get_storage_config(requesting_user_id=requesting_user_id)
    db_active = cfg.get("db", {}).get("active", {}) or {}
    secrets = cfg.get("secrets", {}) or {}

    db_provider = (db_active.get("provider") or "sqlite").strip()
    db_shared = db_provider in _REMOTE_DB
    db_reach = await _probe_db()

    # The local SQLite copy ALWAYS probes green, so "reachable" alone hides a
    # silent fallback. Pull the DB layer's own health record to surface a drop to
    # this device's local copy loudly.
    try:
        from app.db import get_connection_health
        health = get_connection_health()
    except Exception:
        health = {}
    degraded = bool(health.get("degraded")) and db_shared

    vault_provider = (secrets.get("provider") or "inline_db").strip()
    vault_reach = await _probe_vault()

    from app.devices import identity
    return {
        "device": {"id": identity.device_id(), "label": identity.device_label()},
        "env_locked": bool(cfg.get("env_locked")),
        "public_ip": await _detect_public_ip(),
        "db": {
            "provider": db_provider,
            "target": _db_target_summary(db_active),
            "shared": db_shared,
            "reachable": db_reach["reachable"],
            "detail": db_reach["detail"],
            "degraded": degraded,
            "active_backend": health.get("actual") or ("local" if not db_shared else None),
            "fallback_reason": health.get("reason") if degraded else None,
            "fallback_message": health.get("message") if degraded else None,
            "fallback_detail": health.get("detail") if degraded else None,
        },
        "vault": {
            "provider": vault_provider,
            "locator": _vault_locator_summary(vault_provider, secrets.get("configs", {}) or {}),
            "reach": _VAULT_REACH.get(vault_provider, "local"),
            "reachable": vault_reach["reachable"],
            "detail": vault_reach["detail"],
            "restart_recommended": bool(secrets.get("restart_recommended")),
            "boot_provider": secrets.get("boot_provider"),
        },
    }


# ══════════════════════════════════════════════════════════════════════════════
# Device presence registry — every WebAgent instance on the shared database
# ══════════════════════════════════════════════════════════════════════════════
@router.get("/devices")
async def devices(requesting_user_id: str = ""):
    await _require_admin(requesting_user_id)
    from app.devices import dispatch, identity

    me = identity.device_id()
    rows = await dispatch.list_devices(online_within_seconds=60)
    # Migration fallback for a Cloud Run service created before deployments began
    # publishing their provider metadata in heartbeats. Match its recorded public
    # URL once, then expose the saved repo/branch until the next image carries the
    # new WEBAGENT_* identity environment variables itself.
    cr_config = deploy_store.get_config("google_cloud_run") or {}
    cr_record = deploy_store.get_deployment("google_cloud_run") or {}
    cr_public_url = str(cr_record.get("public_url") or "").strip().rstrip("/")
    cr_fallback = {
        "project": str(cr_record.get("project") or cr_config.get("project_id") or "").strip(),
        "region": str(cr_record.get("region") or cr_config.get("region") or "").strip(),
        "service": str(cr_record.get("server") or cr_config.get("service_name") or "").strip(),
        "repo": str(cr_config.get("repo_url") or "").strip(),
        "branch": str(cr_config.get("branch") or "").strip(),
    }

    def _caps(raw):
        if isinstance(raw, str):
            try:
                return json.loads(raw or "{}")
            except Exception:
                return {}
        return raw or {}

    # Live facts for THIS machine, used to fill the self row even before its first
    # heartbeat lands (or if an older build wrote it without the repo).
    my_caps = identity.capabilities()
    # This device's tunnel is read LIVE (not from its possibly-stale heartbeat) so
    # the self tile reflects a start/stop instantly; other devices read the tunnel
    # state they published in their heartbeat's capabilities.
    try:
        from app.remote_access.manager import tunnel_snapshot
        my_tunnel = tunnel_snapshot()
    except Exception:
        my_tunnel = None

    # P2P peer links — map remote instance id → peer, so each device's Overview
    # can show whether it is connected to the host over the P2P sync channel
    # (app/p2p peer store) in addition to its HTTP(S) endpoint.
    _p2p_by_remote = await asyncio.to_thread(_p2p_peers_by_remote_id)

    # Load every instance's metadata in ONE parallel batch. The old per-row
    # sequential `await _meta_get(...)` inside the loop made this endpoint O(N)
    # DB round-trips. Each device also carries its persisted tile layout order.
    _metas = await asyncio.gather(*(_meta_get(d.get("instance_id") or "") for d in rows))
    out: List[Dict[str, Any]] = []
    seen_self = False
    for d, _dev_meta in zip(rows, _metas):
        iid = d.get("instance_id")
        is_self = iid == me
        if is_self:
            seen_self = True
        _custom_tunnel_url = (_dev_meta.get("tunnel_url") or "").strip()
        caps = dict(_caps(d.get("capabilities")))
        endpoint = str(caps.get("endpoint") or d.get("endpoint") or "").strip().rstrip("/")
        if (
            not caps.get("deployment_provider")
            and cr_public_url
            and endpoint == cr_public_url
            and all(cr_fallback.values())
        ):
            caps["deployment_provider"] = "google_cloud_run"
            caps["cloud_run"] = cr_fallback
            caps["repo"] = caps.get("repo") or cr_fallback["repo"]
            caps["branch"] = caps.get("branch") or cr_fallback["branch"]
        out.append({
            "instance_id": iid,
            "label": d.get("label") or iid,
            # Admin display overrides (shared DB) — the UI prefers these over the
            # self-reported hostname / platform icon. Empty string = no override.
            "custom_label": d.get("custom_label") or "",
            "custom_icon": d.get("custom_icon") or "",
            "online": bool(d.get("online")),
            "last_seen": d.get("last_seen"),
            "is_self": is_self,
            "platform": caps.get("platform") or (my_caps.get("platform", "") if is_self else ""),
            "endpoint": caps.get("endpoint") or d.get("endpoint") or "",
            "endpoint_https_auto": caps.get("endpoint_https_auto", False),
            "repo": caps.get("repo") or (my_caps.get("repo", "") if is_self else ""),
            "branch": caps.get("branch") or (my_caps.get("branch", "") if is_self else ""),
            "repo_stats": caps.get("repo_stats") or (my_caps.get("repo_stats") if is_self else None),
            "deployment_provider": (
                caps.get("deployment_provider")
                or (my_caps.get("deployment_provider", "") if is_self else "")
            ),
            "cloud_run": (
                caps.get("cloud_run")
                or (my_caps.get("cloud_run") if is_self else None)
            ),
            # Remote Access tunnel state this device published (self = live).
            "tunnel": (my_tunnel if is_self else caps.get("tunnel")) or None,
            # Custom tunnel URL set by the admin via annotations
            "custom_tunnel_url": _custom_tunnel_url or "",
            "annotation_key": iid or "",
            # All URLs this instance has ever reported (from instance_meta), each with
            # its own last_seen timestamp — the UI shows one row per URL.
            "urls": _dev_meta.get("urls") or None,
            # P2P peer link to this instance (matched by remote instance id) — lets
            # the Overview show whether it is connected to the host via P2P and/or
            # its HTTP(S) endpoint. None = no peer configured for this device.
            "p2p": _p2p_by_remote.get(iid or "") or None,
            "tile_order": int(_dev_meta.get("tile_order") or 0),
        })
    # Surface THIS device even before its first heartbeat (mirrors app/api/devices.py).
    if not seen_self:
        caps = identity.capabilities()
        _self_meta = await _meta_get(me or "")
        out.insert(0, {
            "instance_id": me,
            "label": identity.device_label(),
            "custom_label": "",
            "custom_icon": "",
            "online": True,
            "last_seen": None,
            "is_self": True,
            "platform": caps.get("platform", ""),
            "endpoint": caps.get("endpoint") or _self_base_url(),
            "endpoint_https_auto": caps.get("endpoint_https_auto", False),
            "repo": caps.get("repo", ""),
            "branch": caps.get("branch", ""),
            "repo_stats": caps.get("repo_stats") or None,
            "deployment_provider": caps.get("deployment_provider", ""),
            "cloud_run": caps.get("cloud_run") or None,
            "tunnel": my_tunnel or None,
            "custom_tunnel_url": "",
            "annotation_key": me or "",
            "urls": _self_meta.get("urls") or None,
            "p2p": None,  # this device IS the host — no remote P2P link to itself
            "tile_order": int(_self_meta.get("tile_order") or 0),
        })
    return {"devices": out, "self": me}


def _self_base_url() -> str:
    """This app's own reachable base URL (configured / last-seen request host),
    used to fill the self row's endpoint before the first heartbeat lands. '' when
    only the bare fallback is known, so the UI falls back to localhost:<port>."""
    try:
        from app.admin.integrations import _get_base_url
        url = (_get_base_url() or "").strip().rstrip("/")
        return "" if url == "http://localhost:8000" else url
    except Exception:
        return ""


@router.post("/device/unlink")
async def device_unlink(body: UnlinkBody):
    """Remove a stale device from the presence registry — the admin "Unlink"
    action. Two guard rails, mirrored in the UI so neither side can bypass them:
      • never THIS device (it would just re-add itself immediately), and
      • only a device that is currently OFFLINE — a live one would re-appear on
        its next heartbeat, so unlinking it is misleading.
    This clears the record only; it never touches the actual machine."""
    await _require_admin(body.requesting_user_id)
    from app.devices import dispatch, identity

    iid = (body.instance_id or "").strip()
    if not iid:
        raise HTTPException(status_code=400, detail="An instance id is required")
    if iid == identity.device_id():
        raise HTTPException(status_code=400, detail="This device can't unlink itself.")

    rows = await dispatch.list_devices(online_within_seconds=60)
    match = next((d for d in rows if d.get("instance_id") == iid), None)
    if match is None:
        return {"ok": True, "already_gone": True}   # someone cleared it first
    if match.get("online"):
        raise HTTPException(
            status_code=409,
            detail="That device is online — it will re-link on its next check-in. "
                   "Unlink is only for offline devices.",
        )
    await dispatch.remove_device(iid)
    return {"ok": True}


@router.post("/device/rename")
async def device_rename(body: RenameBody):
    """Set a device's custom display name and/or icon — the admin "rename an
    instance" action, mirroring the Agents page. Both are stored on the device's
    shared-registry row (custom_label / custom_icon), so they persist across the
    device's heartbeats and show identically on every device pointed at this
    database. Sending an empty string for a field clears that override (the tile
    falls back to the machine's hostname / platform icon)."""
    await _require_admin(body.requesting_user_id)
    from app.devices import dispatch

    iid = (body.instance_id or "").strip()
    if not iid:
        raise HTTPException(status_code=400, detail="An instance id is required")
    # Normalise: strip provided fields; None stays None (leave that field alone).
    label = body.label.strip() if isinstance(body.label, str) else None
    icon = body.icon.strip() if isinstance(body.icon, str) else None
    if label is None and icon is None:
        raise HTTPException(status_code=400, detail="Nothing to update")
    ok = await dispatch.set_device_override(iid, label=label, icon=icon)
    if not ok:
        raise HTTPException(
            status_code=404,
            detail="That device isn't in the shared registry yet — it needs to "
                   "check in once before it can be renamed.",
        )
    return {"ok": True}


@router.post("/tunnel/report")
async def tunnel_report(body: TunnelReportBody, request: Request):
    """Accept a state push from this device's token-authenticated tunnel slave."""
    from app.api.rate_limit import enforce
    from app.remote_access import netinfo, store

    peer = request.client.host if request.client else "unknown"
    enforce(f"instances-tunnel-report:{peer}", 60, 60,
            detail="Too many tunnel status reports.")
    port = netinfo.get_port()
    expected = str(store.load_slave_link(port).get("token") or "")
    if not expected or not hmac.compare_digest(expected, str(body.token or "")):
        raise HTTPException(status_code=403, detail="Invalid tunnel slave token")
    state = str(body.state or "").strip().lower()
    if state not in ("starting", "running", "stopped", "error"):
        raise HTTPException(status_code=400, detail="Invalid tunnel state")
    url = str(body.url or "").strip() if state in ("starting", "running") else ""
    if url and not url.startswith("https://"):
        raise HTTPException(status_code=400, detail="Invalid tunnel URL")
    store.update_slave_state(
        port,
        running=state in ("starting", "running"),
        url=url,
        provider=str(body.provider or ""),
        state=state,
        slave_pid=int(body.pid or 0),
        tunnel_pid=int(body.tunnel_pid or 0),
        started_at=float(body.started_at or 0),
    )

    # Put a newly resolved tunnel address into the same persistent URL map used
    # by real inbound requests. The later request heartbeat updates this exact
    # row's last_seen value instead of creating a second, legacy/new-format pair.
    if state in ("starting", "running") and url:
        from app.db.instance_meta import track_endpoint_url
        from app.db.offload import db_offload
        from app.devices import identity
        await db_offload(lambda: track_endpoint_url(identity.device_id(), url))

    # Wake an immediate heartbeat. This updates a remote hub's shared presence
    # view in seconds instead of waiting for the normal 15–45 second cadence.
    from app.devices.worker import refresh_presence
    refresh_presence()
    return {"ok": True}


@router.post("/tunnel/control")
async def tunnel_control(body: TunnelBody):
    """Start / stop the Remote Access tunnel on ANOTHER instance (or this one).

    There is no direct network push to a remote device — instead we drop a small
    ACTION job into the shared cross-device queue (app/devices), addressed to the
    target's instance_id. The target's own worker claims it and runs its LOCAL
    detached tunnel slave (app/remote_access/slave.py), so the provider starts on
    that machine, not here. The slave's report forces a fresh presence heartbeat.

    Guard-railed like the other device actions: the target must be a known,
    ONLINE device — a job for an offline one would sit queued and fire whenever it
    next checks in, which is misleading for a click-to-start."""
    await _require_admin(body.requesting_user_id)
    from app.devices import dispatch

    iid = (body.instance_id or "").strip()
    if not iid:
        raise HTTPException(status_code=400, detail="An instance id is required")
    action = (body.action or "start").strip().lower()
    if action not in ("start", "stop"):
        raise HTTPException(status_code=400, detail="Unknown action")

    dev = await dispatch.resolve_target(iid, online_within_seconds=60)
    if dev is None:
        raise HTTPException(
            status_code=404,
            detail="That device isn't in the shared registry yet — it needs to "
                   "check in once before it can be controlled.",
        )
    if not dev.get("online"):
        raise HTTPException(
            status_code=409,
            detail="That device is offline — it can't start its tunnel until its "
                   "app is running and checking in again.",
        )
    # The presence row carries the OS inside its `capabilities` JSON (there is no
    # top-level `platform` column), so parse it the same way /devices does —
    # otherwise this reads empty and rejects even genuine Windows devices.
    caps = dev.get("capabilities") or {}
    if isinstance(caps, str):
        try:
            caps = json.loads(caps or "{}")
        except Exception:
            caps = {}
    platform = str((caps or {}).get("platform") or "").strip().lower()
    if action == "start" and not platform.startswith("win"):
        raise HTTPException(
            status_code=400,
            detail="Tunnels are currently available only on Windows. "
                   "Linux and macOS support is coming soon.",
        )

    payload: Dict[str, Any] = {
        "action": "slave_tunnel" if action == "start" else "slave_stop",
        "source": "instances_page",
    }
    if (body.provider or "").strip():
        payload["provider"] = body.provider.strip()
    job_id = await dispatch.enqueue(
        owner_user_id=body.requesting_user_id or "",
        prompt="",
        target_instance=iid,
        target_label=dev.get("label") or iid,
        payload=payload,
    )

    # Wait for the quick action result so the UI can show preflight/control
    # failures, managed fallback, or an adopted slave's already-resolved URL.
    if action in ("start", "stop"):
        from app.db import get_db as _get_db
        _db = _get_db()
        deadline = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=20)
        last_status = ""
        while datetime.datetime.now(datetime.timezone.utc) < deadline:
            await asyncio.sleep(0.5)
            j = await _db.get_device_job(job_id)
            if not j:
                continue
            s = j.get("status", "")
            if s == "done":
                url = (j.get("result_excerpt") or "").strip()
                return {"ok": True, "job_id": job_id, "queued_for": iid,
                        "result": url, "pending": False}
            if s in ("error", "skipped") and s != last_status:
                last_status = s
                raise HTTPException(status_code=500, detail=j.get("error") or "Tunnel action failed")
        # Timed out — the remote worker may still claim the queued job shortly.
        return {"ok": True, "job_id": job_id, "queued_for": iid,
                "result": "", "pending": True}

    return {"ok": True, "job_id": job_id, "queued_for": iid}


# Fleet server/repo control — restart the server, or bring the target device's OWN
# app repo up to date / commit + push it, on ANY online instance. Same seam as the
# tunnel: drop a small ACTION job addressed to the target; its worker claims it and
# runs the action LOCALLY (app/devices/actions.py), reusing the very helpers the
# local Server Reset / Git Control ⭐ button use. No direct network push.
_DEVICE_ACTIONS = {
    "restart": "restart_server",
    "git_pull": "git_pull",
    "git_commit_push": "git_commit_push",
}


@router.post("/device/control")
async def device_control(body: DeviceControlBody):
    """Enqueue a restart / git-pull / commit-and-push on a target instance.

    Guard-railed like tunnel_control: the target must be a known, ONLINE device —
    a job for an offline one would sit queued and fire whenever it next checks in,
    which is misleading for a click-to-run. Returns the job id so the caller can
    poll the outcome via ``/device/action-status`` (git actions leave no visible
    heartbeat state, unlike a tunnel flip)."""
    await _require_admin(body.requesting_user_id)
    from app.devices import dispatch

    iid = (body.instance_id or "").strip()
    if not iid:
        raise HTTPException(status_code=400, detail="An instance id is required")
    action = (body.action or "").strip().lower()
    if action not in _DEVICE_ACTIONS:
        raise HTTPException(status_code=400, detail="Unknown action")

    dev = await dispatch.resolve_target(iid, online_within_seconds=60)
    if dev is None:
        raise HTTPException(
            status_code=404,
            detail="That device isn't in the shared registry yet — it needs to "
                   "check in once before it can be controlled.",
        )
    if not dev.get("online"):
        raise HTTPException(
            status_code=409,
            detail="That device is offline — it can't run this until its app is "
                   "running and checking in again.",
        )

    payload: Dict[str, Any] = {"action": _DEVICE_ACTIONS[action], "source": "instances_page"}
    job_id = await dispatch.enqueue(
        owner_user_id=body.requesting_user_id or "",
        prompt="",
        target_instance=iid,
        target_label=dev.get("label") or iid,
        payload=payload,
    )
    return {"ok": True, "job_id": job_id, "queued_for": iid}


@router.get("/device/action-status")
async def device_action_status(job_id: str = Query(...), requesting_user_id: str = Query("")):
    """Poll a fleet ACTION job's outcome so the Instances page can show a plain
    result line (git pull / commit+push report no heartbeat state). Returns the
    job's status (pending | claimed | done | error | skipped) plus its short
    result excerpt / error message once the target has run it."""
    await _require_admin(requesting_user_id)
    from app.db import get_db

    job = await get_db().get_device_job((job_id or "").strip())
    if not job:
        raise HTTPException(status_code=404, detail="No such action job.")
    return {
        "status": job.get("status"),
        "result": job.get("result_excerpt") or "",
        "error": job.get("error") or "",
    }


# ══════════════════════════════════════════════════════════════════════════════
# Cloud VMs + cloud-account sign-in (was Server Manager)
# ══════════════════════════════════════════════════════════════════════════════


class ActionBody(BaseModel):
    requesting_user_id: str = ""
    provider: str
    action: str            # "start" | "stop" | "delete"
    zone: str
    name: str


class NewInstanceDeployBody(BaseModel):
    requesting_user_id: str = ""
    provider: str
    config: dict = {}
    sync_options: dict = {}


class ConnectBody(BaseModel):
    # The sign-in panel's submission: the cloud 'id' fields (e.g. project) AND the
    # secret 'key', mixed in one map keyed by field key. The manager routes each
    # value to deploy.json or the vault by which field list it belongs to.
    requesting_user_id: str = ""
    provider: str
    values: dict = {}


class ProviderBody(BaseModel):
    requesting_user_id: str = ""
    provider: str
    # "Sign out" forgets only the key; "Remove account" also clears the saved id.
    forget_config: bool = False


class AnnotateBody(BaseModel):
    # Set persistent metadata for an auto-discovered instance (cloud VM, fleet
    # device, etc.). key = "cloud:<provider>|<zone>|<name>" or bare instance ref.
    requesting_user_id: str = ""
    key: str
    repo: str = ""
    label: str = ""
    kind: str = ""          # "cloud_vm" | "local_device" | "cloud_run" | …
    domains: Optional[List[str]] = None   # list of HTTPS domain(s) — None = don't touch; [] = clear
    tunnel_url: Optional[str] = None   # custom tunnel URL set by the admin; None = don't touch; "" = clear


class PingBody(BaseModel):
    requesting_user_id: str = ""
    address: str           # host[:port] or http(s)://url


class UnlinkBody(BaseModel):
    # Drop a stale device's presence row from the shared registry. Offline-only +
    # never this device — both enforced server-side below, not just in the UI.
    requesting_user_id: str = ""
    instance_id: str


class RenameBody(BaseModel):
    # Set a device's admin display overrides in the shared registry. Each field is
    # optional: omitted/None = leave unchanged; "" = clear the override (fall back
    # to the self-reported hostname / platform icon); any other string = set it.
    requesting_user_id: str = ""
    instance_id: str
    label: Optional[str] = None
    icon: Optional[str] = None


class TunnelBody(BaseModel):
    # Start/stop the detached tunnel slave on a target instance via the shared
    # device queue. provider is an optional hint; blank uses the target's config.
    requesting_user_id: str = ""
    instance_id: str
    action: str = "start"       # "start" | "stop"
    provider: str = ""


class TunnelReportBody(BaseModel):
    token: str
    state: str
    url: str = ""
    pid: int = 0
    tunnel_pid: int = 0
    provider: str = ""
    started_at: float = 0
    ts: float = 0
    exit_code: Optional[int] = None
    error: str = ""


class ClearUrlBody(BaseModel):
    requesting_user_id: str = ""
    ref: str       # instance ref (device id or "cloud:provider|zone|name")
    url: str       # exact URL to remove from the URL list


class SetUrlHiddenBody(BaseModel):
    requesting_user_id: str = ""
    ref: str       # instance ref (device id or "cloud:provider|zone|name")
    url: str       # exact URL in the URL list
    hidden: bool = True   # True = hide (declutter), False = show again


class DiscoverLocalUrlsBody(BaseModel):
    requesting_user_id: str = ""


class CanonicalUrlBody(BaseModel):
    """Set the deployment's canonical URL (writes webhook_base_url.txt so the
    redirect middleware picks it up) and toggle whether redirect is enabled."""
    requesting_user_id: str = ""
    url: Optional[str] = None          # the canonical URL to set; None = don't change
    redirect_enabled: Optional[bool] = None  # toggle redirect; None = don't change
    exclude_url: Optional[str] = None  # host[:port] to EXCLUDE from redirect
    include_url: Optional[str] = None  # host[:port] to RE-INCLUDE (undo exclude)


class DeviceControlBody(BaseModel):
    # Run a server/repo fleet ACTION on a target instance via the cross-device job
    # queue (sibling of TunnelBody). action: restart | git_pull | git_commit_push.
    requesting_user_id: str = ""
    instance_id: str
    action: str = "restart"


class CloudRunDeployBody(BaseModel):
    # Build the repo/branch published by a Cloud Run device and update that
    # service's image. This is infrastructure-side and does not queue work on the
    # ephemeral instance, so the target is allowed to be offline.
    requesting_user_id: str = ""
    instance_id: str


class HttpsEnableBody(BaseModel):
    """Enable HTTPS on a cloud VM by updating its Caddyfile with a domain.
    The VM must have a saved SSH server (from deploy or manual add)."""
    requesting_user_id: str = ""
    provider: str           # e.g. "google_vm"
    zone: str               # GCE zone (or "" for other providers)
    name: str               # VM name
    domain: str             # e.g. "app.yourcompany.com"
    email: str = ""         # Let's Encrypt notification email (Caddy uses for ACME)
    sibling_domain: str = "" # optional sibling domain (e.g. "www.app.yourcompany.com")


class HttpsStatusBody(BaseModel):
    """Check whether HTTPS is active on a cloud VM's domain / IP."""
    requesting_user_id: str = ""
    provider: str
    zone: str
    name: str


class HttpsTestDomainBody(BaseModel):
    """Test a single HTTPS domain on a cloud VM — streams progress over SSH."""
    requesting_user_id: str = ""
    provider: str
    zone: str
    name: str
    domain: str = ""  # if blank, tests every domain found in the Caddyfile


class HttpsDeleteDomainBody(BaseModel):
    """Delete one domain from a cloud VM's Caddyfile."""
    requesting_user_id: str = ""
    provider: str
    zone: str
    name: str
    domain: str


# This module is imported dynamically (page-catalog drop-in) and uses
# `from __future__ import annotations`, so the request models carry STRING
# annotations FastAPI can't resolve lazily later. Resolve them now, while the
# module namespace is live (same fix as ui/admin-tools/update/server.py).
for _m in (ActionBody, NewInstanceDeployBody, ConnectBody, ProviderBody, AnnotateBody, ClearUrlBody, SetUrlHiddenBody, CanonicalUrlBody, PingBody, UnlinkBody, RenameBody, TunnelBody, TunnelReportBody, DeviceControlBody, CloudRunDeployBody, HttpsEnableBody, HttpsStatusBody, HttpsTestDomainBody, HttpsDeleteDomainBody):
    _m.model_rebuild()


@router.get("/providers")
async def providers(requesting_user_id: str = ""):
    await _require_admin(requesting_user_id)
    return await manager.manageable_providers()


async def _new_instance_deploy_events(body: NewInstanceDeployBody, source_url: str = ""):
    """Deploy without mutating the legacy tab, then run scoped P2P bootstrap."""
    provider = get_provider(body.provider)
    if not provider:
        yield done({"ok": False, "message": "Unknown deployment target."})
        return
    if bool(getattr(provider, "manual", False)):
        yield done({
            "ok": False,
            "message": "Automatic P2P enrollment currently requires a reachable cloud target.",
        })
        return
    if not bool(getattr(provider, "supports_instances", False)):
        yield done({
            "ok": False,
            "message": (
                "This bootstrap requires a persistent server target; "
                "serverless filesystems cannot safely retain SQLite data."
            ),
        })
        return
    available, reason = provider.available()
    if not available:
        yield done({"ok": False, "message": reason or "This target is unavailable."})
        return

    # Start from the shared account's saved non-secret values, but keep this
    # wizard's per-deploy choices ephemeral so the legacy tab is not rewritten.
    base = await manager._load_config(provider)
    allowed = {str(field.get("key")) for field in provider.config_fields}
    allowed.add("instance_name")
    overrides = {
        str(key): value for key, value in (body.config or {}).items()
        if str(key) in allowed
    }
    config = {**base, **overrides}
    config["repo_url"] = "https://github.com/botboss3000/webagent"
    config["branch"] = "main"
    config["visibility"] = "public"
    config["domain"] = ""
    config["embed_config"] = False
    config["forget_keys"] = False
    # This flow cannot finish until the freshly-installed app exposes the native
    # scoped bootstrap endpoint. The e2-micro install can take well over ten
    # minutes while Playwright's OS packages are installed.
    config["_require_scoped_p2p"] = True
    config["_health_timeout_s"] = 1500
    if not str(config.get("instance_name") or "").strip():
        project_id = str(config.get("project_id") or "").strip()
        if project_id:
            config["instance_name"] = project_id

    creds = await credentials.read(body.provider, provider.credential_fields)
    creds.pop("github_token", None)
    required = provider.credential_required if provider.credential_required is not None else [
        field["key"] for field in provider.credential_fields if field.get("secret")
    ]
    if required and not all(str(creds.get(key) or "").strip() for key in required):
        yield done({
            "ok": False,
            "message": "Connect the cloud account before deploying this instance.",
        })
        return

    final: Dict[str, Any] = {"ok": False, "message": "Deployment produced no result."}
    try:
        async for event in provider.deploy(config, creds):
            if event.get("phase") == "done":
                final = dict(event.get("result") or final)
            else:
                yield event
    except Exception as exc:
        logger.exception("new-instance deploy crashed for %s", body.provider)
        yield done({"ok": False, "message": str(exc) or "Deployment crashed."})
        return

    if not final.get("ok"):
        yield done(final)
        return

    target_url = str(final.get("public_url") or "").strip().rstrip("/")
    if not target_url and final.get("ip"):
        target_url = "http://" + str(final["ip"]).strip()
    yield ev("Pairing with the new instance over P2P…", phase="p2p")
    try:
        from app.p2p.bootstrap import pair_and_push

        sync_result = await pair_and_push(
            target_url=target_url,
            source_url=source_url,
            options=body.sync_options or {},
        )
        final["p2p"] = sync_result
        final["message"] = final.get("message") or "Deployed and synchronized."
        yield ev(
            "P2P bootstrap complete: "
            f"{sync_result.get('app_rows', 0)} app rows, "
            f"{sync_result.get('secret_rows', 0)} secret rows and "
            f"{sync_result.get('config_files', 0)} config files.",
            phase="p2p",
            level="ok",
        )
    except Exception as exc:
        logger.exception("new-instance P2P bootstrap failed")
        final["p2p"] = {"ok": False, "message": str(exc)}
        final["message"] = (
            "The instance was deployed, but its P2P configuration sync failed: " + str(exc)
        )
        yield ev(final["message"], phase="p2p", level="warn")
    yield done(final)


@router.post("/new-instance/deploy")
async def new_instance_deploy(body: NewInstanceDeployBody, request: Request):
    await _require_admin(body.requesting_user_id)
    source_url = str(request.base_url).rstrip("/")
    try:
        source_url = str(_read_canonical().get("url") or source_url).rstrip("/")
    except Exception:
        pass
    return _stream(_new_instance_deploy_events(body, source_url))


@router.get("/cloud-instances")
async def cloud_instances(provider: str = Query(...), requesting_user_id: str = ""):
    await _require_admin(requesting_user_id)
    if not get_provider(provider):
        raise HTTPException(status_code=400, detail="Unknown target")
    res = await manager.list_instances(provider)
    deploy_cfg = deploy_store.get_config(provider)
    default_repo = (deploy_cfg.get("repo_url") or "").strip()
    deploy_domain = (deploy_cfg.get("domain") or "").strip()
    # P2P peers by URL host — lets each VM's Overview show its P2P link to this
    # host (matched on the VM's public IP). SSH login is matched per VM below.
    _p2p_by_ip = await asyncio.to_thread(_p2p_peers_by_ip)
    insts = res.get("instances", []) or []
    # Load every VM's metadata in ONE parallel batch (was sequential per VM).
    # Each VM also carries its persisted tile layout order.
    metas = await asyncio.gather(*(
        _meta_get("%s|%s|%s" % (provider, inst.get("zone", ""), inst.get("name", "")))
        for inst in insts))
    for inst, meta in zip(insts, metas):
        ref = "%s|%s|%s" % (provider, inst.get("zone", ""), inst.get("name", ""))
        repo = meta.get("repo") or (default_repo if inst.get("is_this_app") else "")
        inst["repo"] = repo
        inst["annotation_key"] = "cloud:" + ref
        inst["domain"] = (meta.get("domain") or deploy_domain or "")
        inst["custom_tunnel_url"] = (meta.get("tunnel_url") or "").strip()
        if meta.get("domains"):
            inst["domains"] = meta["domains"]
        inst["tile_order"] = int(meta.get("tile_order") or 0)
        # Connection facts for the Overview "Connection" section.
        inst["p2p"] = _p2p_by_ip.get(str(inst.get("ip") or "").strip()) or None
        inst["ssh"] = _ssh_profile_for_vm(
            provider, str(inst.get("name") or ""), str(inst.get("ip") or ""))
    return res


@router.get("/p2p-peers")
async def p2p_peers(requesting_user_id: str = ""):
    """Every P2P peer (with saved SSH login). The Instances page uses this to
    show peers whose VM no longer exists in a connected cloud as ghost tiles."""
    await _require_admin(requesting_user_id)
    return await asyncio.to_thread(_p2p_peers_list)


@router.delete("/p2p-peers/{peer_id}")
async def p2p_peer_remove(peer_id: str, requesting_user_id: str = ""):
    """Revoke a P2P peer — drops the stale link so its ghost tile disappears.
    Mirrors DELETE /admin/p2p/peers/{peer_id} but lives on the Instances API so
    the page doesn't need a second base path."""
    await _require_admin(requesting_user_id)
    try:
        from app.p2p.revocation import revoke_peer
        was_revoked = await asyncio.to_thread(revoke_peer, peer_id)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Revoke failed: {exc}")
    if not was_revoked:
        raise HTTPException(status_code=404, detail="Peer not found")
    return {"ok": True, "peer_id": peer_id}


@router.delete("/ssh/{server_id}")
async def ssh_remove(server_id: str, requesting_user_id: str = ""):
    """Remove a saved SSH login (its server profile + credentials) so the
    instance's SSH connection row drops to 'Not configured'."""
    await _require_admin(requesting_user_id)
    from app.deploy import store as deploy_store
    from app.deploy import credentials as deploy_credentials
    await asyncio.to_thread(deploy_store.delete_server, _SSH_PROVIDER_ID, server_id)
    try:
        await deploy_credentials.forget(_SSH_PROVIDER_ID, profile=server_id)
    except Exception:
        logger.exception("forgetting ssh_vm credentials for %s failed (non-fatal)", server_id)
    return {"ok": True, "server_id": server_id}


@router.post("/annotate")
async def annotate(body: AnnotateBody):
    await _require_admin(body.requesting_user_id)
    key = (body.key or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="A target key is required")
    ref = key
    if ref.startswith("cloud:"):
        ref = ref[6:]
    # Build metadata update
    meta: Dict[str, Any] = {}
    repo = (body.repo or "").strip()
    label = (body.label or "").strip()
    if repo:
        meta["repo"] = repo
    if label:
        meta["label"] = label
    if body.domains is not None:
        meta["domains"] = body.domains
    if body.tunnel_url is not None:
        meta["tunnel_url"] = (body.tunnel_url or "").strip()
    kind = (body.kind or "").strip()
    if not repo and not label and body.domains is None and not kind and body.tunnel_url is None:
        # Nothing to save — caller is clearing everything
        import json
        await _meta_upsert(ref, metadata={})
        return {"ok": True}
    upsert_fields: Dict[str, Any] = {}
    if kind:
        upsert_fields["kind"] = kind
    if meta:
        upsert_fields["metadata"] = meta
    await _meta_upsert(ref, **upsert_fields)
    return {"ok": True}


def _meta_key_for_instance_id(iid: str) -> str:
    """Map a frontend tile instance id to its shared-DB metadata key.

    The Instances page tiles use prefixed ids — cloud VMs are
    ``"cloud:<provider>|<zone>|<name>"`` (see _normalizeCloud in instances.js) —
    while the backend stores each cloud VM's metadata under the BARE
    ``"<provider>|<zone>|<name>"`` ref that /cloud-instances reads back. Registry
    device ids pass through unchanged; 'local:'/'peer:' ids are not metadata-
    backed (their tiles aren't arrangeable) and would simply be orphaned keys."""
    if iid.startswith("cloud:"):
        return iid[len("cloud:"):]
    return iid


class TileOrderBody(BaseModel):
    """The full top-left → bottom-right order after a tile rearrange."""
    requesting_user_id: str = ""
    ordered_ids: List[str] = []


@router.post("/tile-order")
async def tile_order(body: TileOrderBody):
    """Persist a drag-and-drop tile layout in one bulk request."""
    await _require_admin(body.requesting_user_id)
    ids: List[str] = []
    seen: set = set()
    for raw in body.ordered_ids or []:
        iid = _meta_key_for_instance_id((raw or "").strip())
        if iid and iid not in seen:
            seen.add(iid)
            ids.append(iid)
    if not ids:
        return {"ok": True, "count": 0}
    await asyncio.gather(*(
        _meta_upsert(iid, metadata={"tile_order": idx})
        for idx, iid in enumerate(ids, start=1)))
    return {"ok": True, "count": len(ids)}


@router.post("/clear-url")
async def clear_url(body: ClearUrlBody):
    await _require_admin(body.requesting_user_id)
    ref = (body.ref or "").strip()
    url = (body.url or "").strip()
    if not ref or not url:
        raise HTTPException(status_code=400, detail="ref and url are required")
    if ref.startswith("cloud:"):
        ref = ref[6:]
    from app.db.instance_meta import clear_endpoint_url
    ok = await clear_endpoint_url(ref, url)
    return {"ok": ok}


@router.post("/set-url-hidden")
async def set_url_hidden(body: SetUrlHiddenBody):
    """Hide (or re-show) one URL in an instance's overview URL list. Hidden URLs
    are kept in the database — just flagged so the UI collapses them away."""
    await _require_admin(body.requesting_user_id)
    ref = (body.ref or "").strip()
    url = (body.url or "").strip()
    if not ref or not url:
        raise HTTPException(status_code=400, detail="ref and url are required")
    if ref.startswith("cloud:"):
        ref = ref[6:]
    from app.db.instance_meta import set_endpoint_url_hidden
    ok = await set_endpoint_url_hidden(ref, url, body.hidden)
    return {"ok": ok}


# ── Local-network URL discovery (Overview URL list) ──────────────────────────
# When the Overview tab opens for "This device", the frontend asks us to figure
# out this machine's LAN addresses (http://<lan-ip>:<port>/) and add them to the
# device's URL list. They're listed by default — even with no signal from them
# and no users using them — because they work for anyone on the same network.
# Discovery is idempotent (app/db/instance_meta.py add_local_net_urls only adds
# genuinely new URLs; a real last_seen or an admin hidden flag always wins).
_PRIVATE_V4_PREFIXES = ("10.", "192.168.", "172.")  # RFC1918 — sort first


def _local_network_ips() -> List[str]:
    """This machine's own non-loopback IPv4 addresses (best-effort, never raises).

    The PRIMARY address comes from the UDP-connect trick — the interface with a
    route to the outside, i.e. the one that actually works from another machine on
    the same network. Hostname resolution then contributes extra addresses, but
    only those on the SAME /16 as the primary are kept: virtual adapters (WSL /
    Hyper-V / Docker) live on their own subnets and are not reachable from other
    machines, so they'd only clutter the URL list. Link-local 169.254.x addresses
    are dropped too. If no primary route exists (fully offline), all hostname
    addresses are kept as a best guess."""
    seen: Dict[str, None] = {}

    def _valid(ip: str) -> bool:
        try:
            parts = [int(x) for x in ip.split(".")]
        except ValueError:
            return False
        if len(parts) != 4:
            return False
        a, b = parts[0], parts[1]
        if a == 127:                 # loopback
            return False
        if a == 169 and b == 254:    # link-local
            return False
        if a == 0 or a >= 224:       # unspecified / multicast / reserved
            return False
        return True

    def _add(ip: str) -> None:
        if _valid(ip):
            seen.setdefault(ip, None)

    primary = ""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))   # nothing is sent — UDP connect never transmits
            primary = s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        pass
    if primary:
        _add(primary)

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if primary:
                # Only same-subnet (same /16) siblings of the primary interface —
                # virtual adapters are on other subnets and unreachable from LAN.
                if ip.split(".")[:2] != primary.split(".")[:2]:
                    continue
            _add(ip)
    except OSError:
        pass

    def _sort_key(ip: str) -> Tuple[int, str]:
        return (0 if ip == primary else 1, 0 if ip.startswith(_PRIVATE_V4_PREFIXES) else 1, ip)

    return sorted(seen.keys(), key=_sort_key)


def _local_network_urls() -> List[str]:
    """``http://<lan-ip>:<hub port>/`` for every local IP — the addresses that
    work from another machine on the same network."""
    try:
        from app.local_instances import hub_port
        port = hub_port()
    except Exception:  # noqa: BLE001
        port = 8080
    return [f"http://{ip}:{port}/" for ip in _local_network_ips()]


@router.post("/discover-local-urls")
async def discover_local_urls(body: DiscoverLocalUrlsBody):
    """Detect THIS machine's local-network URLs and add them to the self device's
    Overview URL list (idempotent — only genuinely new addresses are added, and
    entries stay listed by default even with no signal / no users). Called when
    the Overview tab opens."""
    await _require_admin(body.requesting_user_id)
    from app.devices import identity
    from app.db.instance_meta import add_local_net_urls
    me = identity.device_id()
    urls = _local_network_urls()
    await add_local_net_urls(me, urls)
    return {"ok": True, "urls": urls}


# ── Canonical URL + redirect toggle ──────────────────────────────────────────
# The redirect middleware in app/main.py reads webhook_base_url.txt and 301-redirects
# non-matching hostnames to the canonical one. These endpoints let the admin pick a
# primary URL from the Instances Overview and turn the redirect on/off.

from pathlib import Path as _PathLibPath
_CANONICAL_FILE = _PathLibPath(__file__).resolve().parent.parent.parent.parent / "webhook_base_url.txt"
_CANONICAL_DISABLED = _PathLibPath(__file__).resolve().parent.parent.parent.parent / "webhook_base_url.txt.disabled"
_CANONICAL_EXCLUDES = _PathLibPath(__file__).resolve().parent.parent.parent.parent / "webhook_base_url_exclude.json"


def _read_canonical() -> dict:
    """Return {url, redirect_enabled, excluded} from the canonical URL files."""
    # Check active file first, then the disabled backup.
    for path, enabled in [(_CANONICAL_FILE, True), (_CANONICAL_DISABLED, False)]:
        try:
            if path.exists():
                url = path.read_text().strip() or None
                break
        except Exception:
            pass
    else:
        return {"url": None, "redirect_enabled": False, "excluded": []}

    excluded: list = []
    try:
        if _CANONICAL_EXCLUDES.exists():
            excluded = json.loads(_CANONICAL_EXCLUDES.read_text() or "[]")
            if not isinstance(excluded, list):
                excluded = []
    except Exception:
        excluded = []

    return {"url": url, "redirect_enabled": enabled, "excluded": excluded}


def _write_canonical(url: Optional[str], redirect_enabled: Optional[bool],
                     exclude_url: Optional[str] = None,
                     include_url: Optional[str] = None) -> dict:
    """Write the canonical URL to disk, toggling the redirect and managing per-host exclusions."""
    state = _read_canonical()
    if url is not None:
        state["url"] = (url.strip() or None)
    if redirect_enabled is not None:
        state["redirect_enabled"] = bool(redirect_enabled)

    canonical_url = state.get("url") or ""
    enabled = state.get("redirect_enabled", False)
    excluded = list(state.get("excluded") or [])

    # Manage per-host excludes
    if exclude_url:
        h = _host_only(exclude_url)
        if h and h not in excluded:
            excluded.append(h)
    if include_url:
        h = _host_only(include_url)
        if h and h in excluded:
            excluded.remove(h)
    state["excluded"] = excluded

    # Remove active+disabled files first so we never have two.
    for p in (_CANONICAL_FILE, _CANONICAL_DISABLED):
        try:
            if p.exists():
                p.unlink()
        except Exception:
            pass

    if canonical_url and enabled:
        try:
            _CANONICAL_FILE.write_text(canonical_url)
        except Exception:
            pass
    elif canonical_url and not enabled:
        try:
            _CANONICAL_DISABLED.write_text(canonical_url)
        except Exception:
            pass

    # Write exclude list (always, even if empty — keeps it clean)
    try:
        _CANONICAL_EXCLUDES.write_text(json.dumps(excluded, ensure_ascii=False))
    except Exception:
        pass

    return _read_canonical()


def _host_only(raw: str) -> str:
    """Strip scheme + path from a URL, returning just the host[:port]."""
    s = (raw or "").strip()
    if "://" in s:
        s = s.split("://", 1)[1]
    return s.split("/", 1)[0].lower()


@router.post("/canonical-url")
async def canonical_url(body: CanonicalUrlBody):
    """Get or set the deployment's canonical URL and redirect toggle.

    Send without url/redirect_enabled to read current state.
    Send with url to set the canonical URL (implicitly enables redirect when a
    URL is first set).  Send redirect_enabled to toggle the redirect on/off.
    Send exclude_url to add a host to the redirect-exclusion list so it is NOT
    redirected even when the redirect is on.  Send include_url to undo that."""
    await _require_admin(body.requesting_user_id)

    if body.url is None and body.redirect_enabled is None and \
       body.exclude_url is None and body.include_url is None:
        # Read-only
        return {"ok": True, **_read_canonical()}

    state = _write_canonical(body.url, body.redirect_enabled,
                             body.exclude_url, body.include_url)
    return {"ok": True, **state}


# ── Reachability ping (devices + cloud VMs) ──
# Server-side so it isn't blocked by the browser's cross-origin / mixed-content
# rules. http(s):// → an HTTP request (any response = reachable). Otherwise a
# plain TCP connect to host[:port] (default port 80).
@router.post("/ping")
async def ping(body: PingBody):
    await _require_admin(body.requesting_user_id)
    addr = (body.address or "").strip()
    if not addr:
        return {"reachable": False, "detail": "No address to check."}

    if addr.startswith("http://") or addr.startswith("https://"):
        try:
            import httpx
            async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
                r = await client.get(addr)
            return {"reachable": r.status_code < 500,
                    "detail": "HTTP %s" % r.status_code, "status": r.status_code}
        except Exception as e:
            return {"reachable": False, "detail": str(e) or "Could not reach the site."}

    host = addr
    port = 80
    if ":" in addr and not addr.count(":") > 1:   # host:port (skip IPv6 literals)
        host, _, p = addr.partition(":")
        try:
            port = int(p)
        except ValueError:
            port = 80
    try:
        fut = asyncio.open_connection(host, port)
        reader, writer = await asyncio.wait_for(fut, timeout=6.0)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return {"reachable": True, "detail": "Reachable on port %s" % port}
    except Exception as e:
        return {"reachable": False, "detail": "No response on port %s (%s)" % (port, type(e).__name__)}


@router.post("/connect")
async def connect(body: ConnectBody):
    await _require_admin(body.requesting_user_id)
    if not get_provider(body.provider):
        raise HTTPException(status_code=400, detail="Unknown target")
    res = await manager.save_connection(body.provider, body.values or {})
    if not res.get("ok"):
        raise HTTPException(status_code=500, detail=res.get("detail") or "Could not connect")
    return res


@router.post("/disconnect")
async def disconnect(body: ProviderBody):
    await _require_admin(body.requesting_user_id)
    if not get_provider(body.provider):
        raise HTTPException(status_code=400, detail="Unknown target")
    return await manager.disconnect(body.provider, body.forget_config)


# ── Start / Stop / Delete one cloud server (streaming NDJSON) ──
def _stream(agen):
    async def _gen():
        try:
            async for evt in agen:
                yield json.dumps(evt) + "\n"
        except Exception as e:  # never leave the client's reader hanging
            logger.exception("instances cloud-action stream crashed")
            yield json.dumps({"phase": "done", "result": {"ok": False, "message": str(e) or "failed"}}) + "\n"
    return StreamingResponse(
        _gen(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


@router.post("/instance/action")
async def instance_action(body: ActionBody):
    await _require_admin(body.requesting_user_id)
    if not get_provider(body.provider):
        raise HTTPException(status_code=400, detail="Unknown target")
    if body.action not in ("start", "stop", "delete"):
        raise HTTPException(status_code=400, detail="Unknown action")
    return _stream(manager.run_instance_action(body.provider, body.action, body.zone, body.name))


@router.post("/cloud-run/build-deploy")
async def cloud_run_build_deploy(body: CloudRunDeployBody):
    """Build a Cloud Run device's recorded repo and roll its service image."""
    await _require_admin(body.requesting_user_id)
    from app.devices import dispatch

    iid = (body.instance_id or "").strip()
    if not iid:
        raise HTTPException(status_code=400, detail="An instance id is required")
    dev = await dispatch.resolve_target(iid, online_within_seconds=60)
    if dev is None:
        raise HTTPException(
            status_code=404,
            detail="That Cloud Run device is no longer in the shared registry.",
        )
    raw = dev.get("capabilities")
    if isinstance(raw, str):
        try:
            caps = json.loads(raw or "{}")
        except Exception:
            caps = {}
    else:
        caps = raw if isinstance(raw, dict) else {}
    cloud_run = caps.get("cloud_run") if isinstance(caps.get("cloud_run"), dict) else {}
    if caps.get("deployment_provider") != "google_cloud_run":
        config = deploy_store.get_config("google_cloud_run") or {}
        record = deploy_store.get_deployment("google_cloud_run") or {}
        endpoint = str(caps.get("endpoint") or dev.get("endpoint") or "").strip().rstrip("/")
        public_url = str(record.get("public_url") or "").strip().rstrip("/")
        fallback = {
            "project": str(record.get("project") or config.get("project_id") or "").strip(),
            "region": str(record.get("region") or config.get("region") or "").strip(),
            "service": str(record.get("server") or config.get("service_name") or "").strip(),
            "repo": str(config.get("repo_url") or "").strip(),
            "branch": str(config.get("branch") or "").strip(),
        }
        if endpoint and endpoint == public_url and all(fallback.values()):
            caps = {**caps, "deployment_provider": "google_cloud_run"}
            cloud_run = fallback
    if caps.get("deployment_provider") != "google_cloud_run":
        raise HTTPException(status_code=400, detail="That device is not a Google Cloud Run deployment.")
    target = {
        "project": str(cloud_run.get("project") or "").strip(),
        "region": str(cloud_run.get("region") or "").strip(),
        "service": str(cloud_run.get("service") or "").strip(),
        "repo": str(cloud_run.get("repo") or caps.get("repo") or "").strip(),
        "branch": str(cloud_run.get("branch") or caps.get("branch") or "").strip(),
    }
    if not all(target.values()):
        raise HTTPException(
            status_code=409,
            detail=(
                "This device predates Cloud Run deployment metadata. Deploy it once "
                "from New Deployment before using Build & Deploy."
            ),
        )
    return _stream(manager.run_cloud_run_rebuild(target))


# ══════════════════════════════════════════════════════════════════════════════
# HTTPS / SSL — enable Caddy's automatic HTTPS on a cloud VM (via SSH)
# ══════════════════════════════════════════════════════════════════════════════
# Caddy is the web server the bootstrap installs on every fresh VM. It has
# automatic Let's Encrypt built in — just put a domain in its Caddyfile and
# reload, and Caddy provisions + renews the cert. No certbot needed.
#
# We reach the VM over SSH using the saved SSH login that was stored when the
# VM was created (or added manually). The SSH target's paramiko plumbing is
# reused from app/deploy/providers/ssh_vm.py. Each action streams live NDJSON
# so the Instances page can show a live log, same as Start/Stop/Delete.

_SSH_PROVIDER_ID = "ssh_vm"


def _ssh_profile_for_vm(provider: str, name: str, ip: str) -> Optional[Dict[str, Any]]:
    """Saved SSH login for a cloud VM / peer. Prefers an EXACT host-IP match (the
    VM's current address), falling back to the label (VM name) — names get reused
    across deployments, so a label-only match can point at a stale profile.
    Pass provider="" to match any deploy source (used for P2P peers, whose
    provider isn't known). Returns {host, ssh_user, ssh_port, server_id} or None —
    enough for the Overview to show whether SSH access is configured, plus the
    server_id so the admin can Disconnect (remove) the saved login."""
    from app.deploy import store as deploy_store
    servers = deploy_store.list_servers(_SSH_PROVIDER_ID)
    label_hit = None   # (server_id, record)
    for _sid, _rec in servers.items():
        if not isinstance(_rec, dict):
            continue
        if provider and (str(_rec.get("source") or "").strip() or "") != provider:
            continue
        _lbl = (_rec.get("label") or "").strip()
        _hst = (_rec.get("host") or "").strip()
        if ip and _hst == ip:
            return {
                "host": _hst,
                "ssh_user": (str(_rec.get("ssh_user") or "webagent").strip() or "webagent"),
                "ssh_port": int(str(_rec.get("ssh_port") or 22).strip() or 22),
                "server_id": _sid,
            }
        if not label_hit and _lbl == name:
            label_hit = (_sid, _rec)
    if label_hit:
        _sid, _rec = label_hit
        return {
            "host": (_rec.get("host") or "").strip(),
            "ssh_user": (str(_rec.get("ssh_user") or "webagent").strip() or "webagent"),
            "ssh_port": int(str(_rec.get("ssh_port") or 22).strip() or 22),
            "server_id": _sid,
        }
    return None

# Caddy's default ACME email — if the admin doesn't supply one, Caddy still works;
# Let's Encrypt just won't send expiry notices. '' means Caddy's own default.
# We only set it explicitly if the admin provides one.
_CADDY_GLOBAL_OPTS = "/etc/caddy/Caddyfile.global"
_CADDYFILE = "/etc/caddy/Caddyfile"
_CADDY_PORT = "8080"   # uvicorn port the bootstrap uses (from app/deploy/bootstrap.py)


def _find_ssh_profile(provider: str, zone: str, name: str) -> Optional[Dict[str, Any]]:
    """Find the saved SSH server profile that matches a cloud VM.
    Returns dict with keys: host, ssh_user, ssh_port, server_id, or None."""
    from app.deploy import store as deploy_store
    servers = deploy_store.list_servers(_SSH_PROVIDER_ID)
    candidates = []
    for sid, rec in servers.items():
        rec = rec or {}
        src = (rec.get("source") or "").strip()
        hst = (rec.get("host") or "").strip()
        lbl = (rec.get("label") or "").strip()
        # A Google VM's saved server uses the VM name as its label (see
        # google_vm.py link_ssh_server call). Match on that OR the host IP.
        if src == provider and (lbl == name or hst):
            candidates.append((sid, rec))
    if not candidates:
        return None
    # Prefer an exact name match; fall back to the first candidate.
    for sid, rec in candidates:
        if (rec.get("label") or "").strip() == name:
            return {
                "host": (rec.get("host") or "").strip(),
                "ssh_user": (rec.get("ssh_user") or "webagent").strip() or "webagent",
                "ssh_port": int(str(rec.get("ssh_port") or 22).strip() or 22),
                "server_id": sid,
            }
    rec = candidates[0][1]
    return {
        "host": (rec.get("host") or "").strip(),
        "ssh_user": (rec.get("ssh_user") or "webagent").strip() or "webagent",
        "ssh_port": int(str(rec.get("ssh_port") or 22).strip() or 22),
        "server_id": candidates[0][0],
    }


async def _ssh_connect(profile: Dict[str, Any]) -> Tuple[Any, bool, str]:
    """Open an SSH connection to the saved server. Returns (client, is_root, password)."""
    from app.deploy import credentials as deploy_credentials
    from app.deploy.providers.ssh_vm import SSHVMProvider
    sid = profile.get("server_id", "")
    creds = await deploy_credentials.read(_SSH_PROVIDER_ID,
        SSHVMProvider.credential_fields, profile=sid)
    cfg = {
        "host": profile["host"],
        "ssh_user": profile["ssh_user"],
        "ssh_port": profile["ssh_port"],
    }
    provider = SSHVMProvider()
    loop = asyncio.get_running_loop()
    client = await loop.run_in_executor(None, provider._connect, cfg, creds)
    password = (creds.get("password") or "").strip()
    return client, profile["ssh_user"] == "root", password


def _ssh_run(client, cmd: str, timeout: int = 60, stdin_data: str = None) -> Tuple[int, str, str]:
    """Run one command over SSH (sync, called via run_in_executor). Returns
    (exit_code, stdout, stderr). Optionally sends stdin_data (e.g. a sudo password)."""
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    if stdin_data is not None:
        try:
            stdin.write(stdin_data)
            stdin.flush()
            stdin.channel.shutdown_write()
        except Exception:
            pass
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    rc = stdout.channel.recv_exit_status()
    return rc, out, err


def _ssh_run_sudo(client, cmd: str, is_root: bool, password: str = "",
                   timeout: int = 60) -> Tuple[int, str, str]:
    """Run a command, prefixing sudo if not root. Feeds the password to sudo -S
    if one is provided, so non-root users without passwordless sudo still work."""
    if is_root:
        return _ssh_run(client, cmd, timeout)
    if password:
        return _ssh_run(client, f"sudo -S -p '' {cmd}", timeout, stdin_data=password + "\n")
    return _ssh_run(client, f"sudo {cmd}", timeout)


async def _caddy_https_setup(client, is_root: bool, password: str,
                              domain: str, email: str,
                              sibling_domain: str = "") -> AsyncIterator[Dict[str, Any]]:
    """Write a domain block into Caddy's config over an active SSH connection,
    reload Caddy, and verify HTTPS for each domain individually. Caller owns the
    SSH client lifecycle."""
    import os

    # Step 1: Read the current Caddyfile for context
    yield ev("Reading current Caddy config…", phase="read")
    rc, caddyfile, err = await asyncio.get_running_loop().run_in_executor(
        None, _ssh_run_sudo, client, f"cat {_CADDYFILE}", is_root, password)
    if rc != 0:
        yield done({"ok": False, "message": f"Could not read Caddyfile: {err}"})
        return

    # Summarise current config so the admin can see what's there before we change it
    current_domains: List[str] = []
    has_plain_http = False
    for line in caddyfile.splitlines():
        line = line.strip()
        if line.startswith("#") or not line:
            continue
        if ":80 {" in line:
            has_plain_http = True
        if "{" in line and not line.startswith(":") and not line.startswith("#"):
            addr = line[:line.index("{")].strip()
            for part in addr.split(","):
                part = part.strip()
                if part and not part.startswith(":") and "." in part:
                    current_domains.append(part)

    if current_domains:
        yield ev("Current Caddy domains: " + ", ".join(current_domains), phase="read", level="ok")
    elif has_plain_http:
        yield ev("Current config: serving on :80 (plain HTTP, no domains)", phase="read")
    else:
        yield ev("Current config: no domain blocks found", phase="read")

    # Step 2: Write the new Caddyfile with the domain
    # Caddy auto-provisions HTTPS when the site block has a domain (not :80).
    # If there is already a domain block (any domain), REPLACE it entirely with
    # the new one — never append. This avoids accumulating duplicate blocks from
    # repeated enables. The :80 block is replaced on first enable and never returns.
    domains_to_check = [domain]
    if sibling_domain:
        addr = f"{domain}, {sibling_domain}"
        new_block = f"{addr} {{\n    reverse_proxy localhost:{_CADDY_PORT}\n}}\n"
        domains_to_check.append(sibling_domain)
        yield ev(f"Writing Caddyfile for {domain} + {sibling_domain}…", phase="write")
    else:
        new_block = f"{domain} {{\n    reverse_proxy localhost:{_CADDY_PORT}\n}}\n"
        yield ev(f"Writing Caddyfile for {domain}…", phase="write")

    # Build the replacement: remove EVERY existing site block (plain-http :80 and
    # any domain block), then insert the new one at the top.
    lines = caddyfile.splitlines(keepends=True)
    out_lines: List[str] = []
    brace_depth = 0
    inside_site_block = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        # Detect start of a site block — any line ending with "{" that is not
        # a comment and not inside another block.
        if not inside_site_block and "{" in stripped and not stripped.startswith("#"):
            inside_site_block = True
            brace_depth = 0
            # Skip this line (don't emit it) — we're dropping the block
        if inside_site_block:
            brace_depth += stripped.count("{") - stripped.count("}")
            if brace_depth <= 0:
                inside_site_block = False
            continue   # skip every line inside the block (including closing brace)
        out_lines.append(line)

    # Insert the new block at the top of the remaining config (after any leading
    # comments / whitespace, before the first non-comment config line).
    insert_at = 0
    for i, line in enumerate(out_lines):
        s = line.strip()
        if s and not s.startswith("#"):
            insert_at = i
            break
    else:
        insert_at = len(out_lines)
    out_lines.insert(insert_at, new_block)

    new_caddyfile = "".join(out_lines)

    # Deduplicate by collapsing consecutive identical domain blocks (safety net).
    new_caddyfile = _dedupe_caddyfile(new_caddyfile)

    _temp = f"/tmp/_caddy_{os.urandom(4).hex()}"
    try:
        sftp = client.open_sftp()
        try:
            with sftp.open(_temp, "w") as f:
                f.write(new_caddyfile)
        finally:
            sftp.close()
    except Exception as e:
        yield done({"ok": False, "message": f"Could not upload Caddyfile content: {e}"})
        return
    copy_cmd = f"cp {_temp} {_CADDYFILE} && rm -f {_temp}"
    rc, out, err = await asyncio.get_running_loop().run_in_executor(
        None, _ssh_run_sudo, client, copy_cmd, is_root, password)
    if rc != 0:
        yield done({"ok": False, "message": f"Could not write Caddyfile: {err}"})
        return

    # Step 3: If email provided, set it in Caddy's global options
    if email:
        yield ev("Setting Let's Encrypt notification email…", phase="email")
        email_cmd = (
            f"mkdir -p /etc/caddy && "
            f"echo '{{' > {_CADDY_GLOBAL_OPTS} && "
            f"echo '    email {email}' >> {_CADDY_GLOBAL_OPTS} && "
            f"echo '}}' >> {_CADDY_GLOBAL_OPTS}"
        )
        await asyncio.get_running_loop().run_in_executor(
            None, _ssh_run_sudo, client, email_cmd, is_root, password)

    # Step 4: Reload Caddy — it will auto-provision the cert
    yield ev("Reloading Caddy to provision SSL certificate…", phase="reload")
    rc, out, err = await asyncio.get_running_loop().run_in_executor(
        None, _ssh_run_sudo, client, "systemctl reload caddy", is_root, password)
    if rc != 0:
        rc2, out2, err2 = await asyncio.get_running_loop().run_in_executor(
            None, _ssh_run_sudo, client, "systemctl restart caddy", is_root, password)
        if rc2 != 0:
            yield done({"ok": False, "message": (
                f"Caddy reload failed. The Caddyfile was written but the server "
                f"couldn't restart: {err or err2}")})
            return

    # Step 5: Probe each domain individually so the admin sees per-domain results.
    # Caddy can take up to ~30s for Let's Encrypt DNS-01 + issuance; we wait a few
    # seconds then probe each with a short retry.
    await asyncio.sleep(4)
    import httpx

    results: List[Dict[str, Any]] = []
    for d in domains_to_check:
        yield ev(f"Probing https://{d}/…", phase="verify")
        ok, detail, status = await _probe_https(d)
        if ok:
            yield ev(f"  https://{d}/ ✓  (HTTP {status})", phase="verify", level="ok")
        else:
            yield ev(f"  https://{d}/ ✗  ({detail})", phase="verify", level="err")
            # Give a helpful hint
            hint = _https_failure_hint(detail)
            if hint:
                yield ev(f"  → {hint}", phase="verify", level="warn")
        results.append({"domain": d, "ok": ok, "detail": detail, "status": status})

    all_ok = all(r["ok"] for r in results)
    if all_ok:
        dom_list = ", ".join(f"https://{r['domain']}/" for r in results)
        yield done({"ok": True, "message": (
            f"HTTPS is now active: {dom_list} — "
            f"Caddy automatically provisions and renews the certificate.")})
    else:
        ok_doms = [r for r in results if r["ok"]]
        bad_doms = [r for r in results if not r["ok"]]
        msgs = []
        if ok_doms:
            msgs.append("Active: " + ", ".join(f"https://{r['domain']}/" for r in ok_doms))
        if bad_doms:
            msgs.append("Unreachable: " + ", ".join(r["domain"] for r in bad_doms))
        yield done({"ok": bool(ok_doms), "message": (
            "Caddyfile written. " + ". ".join(msgs) +
            ". Caddy retries automatically — DNS changes or certificate issuance "
            "may take a few minutes.")})


async def _probe_https(domain: str) -> Tuple[bool, str, int]:
    """Quick HTTPS probe. Returns (reachable, detail, http_status)."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as hc:
            r = await hc.get(f"https://{domain}/")
        if r.status_code < 500:
            return True, f"HTTP {r.status_code}", r.status_code
        return False, f"HTTP {r.status_code}", r.status_code
    except Exception as e:
        msg = str(e) or type(e).__name__
        return False, msg[:120], 0


def _https_failure_hint(error_text: str) -> str:
    """Return a plain-English hint for common HTTPS probe failures."""
    et = (error_text or "").lower()
    if "tlsv1_alert" in et or "ssl" in et or "tls" in et:
        return ("The TLS handshake failed — this usually means the domain's DNS "
                "A record doesn't point to this server's IP, or a firewall/CDN "
                "is intercepting port 443. Check your DNS settings.")
    if "connection refused" in et or "connect" in et:
        return ("Connection refused on port 443. Caddy may not have finished "
                "starting. Wait 30 seconds and test again, or SSH in and run "
                "'systemctl status caddy'.")
    if "nodename nor servname" in et or "name resolution" in et or "getaddrinfo" in et:
        return ("DNS can't resolve this domain yet. Point an A record at this "
                "server's IP, then wait for propagation (usually 1–5 minutes).")
    if "timeout" in et:
        return ("HTTPS timed out. The server may not be reachable on port 443. "
                "Check that the firewall allows inbound TCP/443 and that Caddy "
                "is running ('systemctl status caddy').")
    return ""


def _parse_caddy_domains(caddyfile: str) -> List[str]:
    """Extract unique domain names from a Caddyfile's site address lines."""
    domains: List[str] = []
    for line in caddyfile.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "{" in line and not line.startswith(":"):
            addr = line[:line.index("{")].strip()
            for part in addr.split(","):
                part = part.strip()
                if part and not part.startswith(":") and "." in part:
                    if part not in domains:
                        domains.append(part)
    return domains


def _dedupe_caddyfile(caddyfile: str) -> str:
    """Remove duplicate consecutive site blocks from a Caddyfile."""
    lines = caddyfile.splitlines(keepends=True)
    seen_blocks: set = set()
    out: List[str] = []
    brace_depth = 0
    block_start = -1
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            out.append(line)
            continue
        if block_start < 0 and "{" in stripped and not stripped.startswith(":"):
            block_start = i
        if block_start >= 0:
            brace_depth += stripped.count("{") - stripped.count("}")
            if brace_depth <= 0:
                # Block just closed — check if we've seen it
                addr = lines[block_start].strip()
                addr = addr[:addr.index("{")].strip()
                if addr not in seen_blocks:
                    seen_blocks.add(addr)
                    for j in range(block_start, i + 1):
                        out.append(lines[j])
                block_start = -1
                brace_depth = 0
        elif block_start < 0:
            out.append(line)
    return "".join(out)
async def _gcp_ssh_inject_and_setup(provider: str, zone: str, name: str,
                                     domain: str, email: str,
                                     sibling_domain: str = "") -> AsyncIterator[Dict[str, Any]]:
    """Fallback: use the cloud account's service-account JSON to inject a fresh SSH
    key into the Google VM, connect, set up HTTPS, and persist the key to the vault
    so the next action finds it without re-injecting."""
    from app.deploy import credentials as deploy_credentials
    from app.deploy.providers.ssh_vm import SSHVMProvider
    from app.deploy.providers.google_vm import (
        _gen_ssh_keypair, _access_token, _COMPUTE, _google_error,
    )

    SSHVMProvider()  # triggers paramiko import check early

    # 1. Load the cloud account credentials
    yield ev("Loading cloud account credentials…", phase="locate")
    p = get_provider(provider)
    if not p:
        yield done({"ok": False, "message": f"Unknown provider: {provider}"})
        return
    creds = await deploy_credentials.read(provider, p.credential_fields)
    if not str(creds.get("service_account_json") or "").strip():
        yield done({"ok": False, "message": (
            "No cloud account key is saved for Google Cloud. Connect your account "
            "in the Cloud Accounts section first.")})
        return

    # 2. Authenticate with Google Cloud
    yield ev("Authenticating with Google Cloud…", phase="auth")
    try:
        token = await _access_token(creds)
    except Exception as e:
        yield done({"ok": False, "message": f"Could not authenticate: {e}"})
        return

    # 3. Read the connect config for the project id
    connect_cfg = await deploy_credentials.read_connect(provider, p.connect_config_keys)
    project = (connect_cfg.get("project_id") or "").strip()
    if not project:
        yield done({"ok": False, "message": "No Google Cloud project ID set."})
        return

    # 4. GET the instance to read its IP and metadata fingerprint
    yield ev(f"Looking up VM '{name}'…", phase="locate")
    import httpx
    inst_url = f"{_COMPUTE}/projects/{project}/zones/{zone}/instances/{name}"
    try:
        async with httpx.AsyncClient(timeout=20.0) as hc:
            r = await hc.get(inst_url, headers={"Authorization": f"Bearer {token}"})
            if r.status_code >= 300:
                yield done({"ok": False, "message": (
                    f"Could not find VM '{name}' in {project}/{zone}: "
                    f"HTTP {r.status_code}")})
                return
            inst = r.json() if r.content else {}
    except Exception as e:
        yield done({"ok": False, "message": f"Could not reach Google Cloud: {e}"})
        return

    ip = ""
    try:
        ip = inst.get("networkInterfaces", [])[0].get("accessConfigs", [])[0].get("natIP", "") or ""
    except Exception:
        pass
    if not ip:
        yield done({"ok": False, "message": "This VM has no public IP address — it must have one for SSH to work."})
        return

    fingerprint = inst.get("metadata", {}).get("fingerprint", "")

    # 5. Generate a fresh SSH keypair & inject the public key
    yield ev("Generating a new SSH key for this server…", phase="locate")
    ssh_priv, ssh_pub = _gen_ssh_keypair()
    ssh_user = "webagent"

    # Merge into the existing ssh-keys metadata line
    existing_items = inst.get("metadata", {}).get("items") or []
    new_ssh_keys_line = f"{ssh_user}:{ssh_pub} {ssh_user}"
    updated = False
    for item in existing_items:
        if item.get("key") == "ssh-keys":
            item["value"] = (item.get("value") or "") + "\n" + new_ssh_keys_line
            updated = True
            break
    if not updated:
        existing_items.append({"key": "ssh-keys", "value": new_ssh_keys_line})

    yield ev("Injecting SSH key into the VM…", phase="provision")
    metadata_body = {"fingerprint": fingerprint, "items": existing_items}
    setmd_url = f"{inst_url}/setMetadata"
    try:
        async with httpx.AsyncClient(timeout=40.0) as hc:
            r = await hc.post(setmd_url, headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }, json=metadata_body)
            if r.status_code >= 300:
                yield done({"ok": False, "message": (
                    f"Could not add SSH key to the VM: {_google_error(r)}")})
                return
            op = r.json() if r.content else {}
    except Exception as e:
        yield done({"ok": False, "message": f"Could not update VM metadata: {e}"})
        return

    # 6. Poll the operation (metadata changes are usually fast)
    try:
        async with httpx.AsyncClient(timeout=30.0) as hc:
            op_url = f"{_COMPUTE}/projects/{project}/zones/{zone}/operations/{op.get('name', '')}"
            for _ in range(30):
                await asyncio.sleep(1)
                r2 = await hc.get(op_url, headers={"Authorization": f"Bearer {token}"})
                op_body = r2.json() if r2.content else {}
                if op_body.get("status") == "DONE":
                    if op_body.get("error"):
                        yield done({"ok": False, "message": (
                            "Metadata update failed: " +
                            str(op_body["error"].get("errors", [{}])[0].get("message", "unknown")))})
                        return
                    break
    except Exception:
        pass  # best-effort; if it timed out the key may still be propagating

    # 7. Connect over SSH with the fresh key
    yield ev(f"Connecting to {ssh_user}@{ip}…", phase="connect")
    cfg = {"host": ip, "ssh_user": ssh_user, "ssh_port": 22}
    ssh_creds = {"private_key": ssh_priv, "password": "", "key_passphrase": ""}
    svc = SSHVMProvider()
    loop = asyncio.get_running_loop()
    try:
        client = await loop.run_in_executor(None, svc._connect, cfg, ssh_creds)
    except Exception as e:
        yield done({"ok": False, "message": (
            f"SSH key was injected but connection failed: {e}. "
            f"The VM's guest agent may still be applying the key — "
            f"wait 30 seconds and try again.")})
        return
    is_root = (ssh_user == "root")
    password = ""

    try:
        # 8. Run the Caddy HTTPS setup (same as the normal path)
        async for evt in _caddy_https_setup(client, is_root, password, domain, email, sibling_domain):
            yield evt
    finally:
        try:
            client.close()
        except Exception:
            pass

    # 9. Persist the SSH key so subsequent actions find it
    yield ev("Saving SSH key for future use…", phase="save")
    try:
        await manager.link_ssh_server(
            host=ip, ssh_user=ssh_user, ssh_port=22, private_key=ssh_priv,
            label=name, source=provider)
    except Exception:
        logger.exception("saving injected ssh key failed (non-fatal)")


async def _https_enable_stream(provider: str, zone: str, name: str,
                               domain: str, email: str,
                               sibling_domain: str = "") -> AsyncIterator[Dict[str, Any]]:
    """Streaming NDJSON of the HTTPS-enable operation over SSH."""
    yield ev("Finding this server's SSH login…", phase="locate")

    profile = _find_ssh_profile(provider, zone, name)

    # ── Fallback for Google VMs: use the cloud account to inject an SSH key ──
    if (not profile or not profile.get("host")) and provider == "google_vm":
        try:
            async for evt in _gcp_ssh_inject_and_setup(provider, zone, name, domain, email, sibling_domain):
                yield evt
        except Exception as e:
            logger.exception("gcp ssh inject fallback failed")
            yield done({"ok": False, "message": f"Could not set up SSH via cloud account: {e}"})
        return

    if not profile or not profile.get("host"):
        yield done({"ok": False, "message": (
            "No saved SSH login for this server. It needs an SSH key stored first — "
            "this happens automatically when a Google VM is created with 'Keep SSH access' "
            "on, or you can add it manually in New Deployment → Existing server (SSH).")})
        return

    try:
        from app.deploy.providers.ssh_vm import SSHVMProvider
        SSHVMProvider()  # triggers paramiko import check
    except Exception:
        yield done({"ok": False, "message": (
            "The 'paramiko' SSH package isn't installed on this WebAgent server. "
            "Install it (pip install paramiko) and restart.")})
        return

    yield ev(f"Connecting to {profile['ssh_user']}@{profile['host']}…", phase="connect")
    client = None
    password = ""
    try:
        client, is_root, password = await _ssh_connect(profile)
    except Exception as e:
        yield done({"ok": False, "message": f"Could not connect over SSH: {e}"})
        return

    try:
        async for evt in _caddy_https_setup(client, is_root, password, domain, email, sibling_domain):
            yield evt
    finally:
        try:
            client.close()
        except Exception:
            pass


@router.post("/https/enable")
async def https_enable(body: HttpsEnableBody):
    """Enable HTTPS on a cloud VM by updating Caddy's config with a domain.
    Streams NDJSON progress events."""
    await _require_admin(body.requesting_user_id)
    domain = (body.domain or "").strip()
    if not domain:
        raise HTTPException(status_code=400, detail="A domain name is required.")
    # Basic validation: at least "x.y" format
    if "." not in domain or len(domain) < 3:
        raise HTTPException(status_code=400, detail="Enter a valid domain name (e.g. app.yourcompany.com).")
    email = (body.email or "").strip()
    sibling = (body.sibling_domain or "").strip()
    return _stream(_https_enable_stream(
        body.provider, body.zone, body.name, domain, email, sibling))


@router.post("/https/status")
async def https_status(body: HttpsStatusBody):
    """Check HTTPS reachability + cert details for a cloud VM."""
    await _require_admin(body.requesting_user_id)
    profile = _find_ssh_profile(body.provider, body.zone, body.name)
    if not profile or not profile.get("host"):
        return {"https_active": False, "detail": "No saved SSH login for this server."}

    # 1. Read the Caddyfile to find domains
    client = None
    domains = []
    caddy_ok = False
    password = ""
    try:
        client, is_root, password = await _ssh_connect(profile)
        rc, caddyfile, _ = await asyncio.get_running_loop().run_in_executor(
            None, _ssh_run_sudo, client, f"cat {_CADDYFILE}", is_root, password)
        if rc == 0:
            # Extract domain names from lines like "example.com {" or "example.com, www.example.com {"
            for line in caddyfile.splitlines():
                line = line.strip()
                if line and not line.startswith("#") and not line.startswith(":") and "{" in line:
                    # The site address portion before "{"
                    addr = line[:line.index("{")].strip()
                    for part in addr.split(","):
                        part = part.strip()
                        if part and not part.startswith(":") and "." in part:
                            domains.append(part)
            caddy_ok = True
    except Exception as e:
        return {"https_active": False, "detail": f"Could not connect over SSH: {e}"}
    finally:
        if client:
            try:
                client.close()
            except Exception:
                pass

    if not caddy_ok:
        return {"https_active": False, "detail": "Could not read the server's Caddyfile."}

    if not domains:
        return {"https_active": False, "detail": "No domain configured in Caddyfile — HTTPS needs a domain.",
                "caddy_configured": True}

    # 2. Probe HTTPS + grab cert details from the server side
    import httpx
    import ssl
    import socket
    import datetime

    results = []
    for domain in domains[:2]:  # check first two domains
        entry = {"domain": domain, "reachable": False, "cert": None}
        try:
            async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as hc:
                r = await hc.get(f"https://{domain}/")
            entry["reachable"] = r.status_code < 500
            entry["http_status"] = r.status_code

            # Try to get cert details via raw SSL socket
            try:
                ctx = ssl.create_default_context()
                with socket.create_connection((domain, 443), timeout=5) as sock:
                    with ctx.wrap_socket(sock, server_hostname=domain) as ssock:
                        cert = ssock.getpeercert()
                        if cert:
                            not_after = cert.get("notAfter", "")
                            issuer = dict(x[0] for x in cert.get("issuer", []))
                            subject = dict(x[0] for x in cert.get("subject", []))
                            entry["cert"] = {
                                "issuer": issuer.get("organizationName", issuer.get("commonName", "")),
                                "subject_cn": subject.get("commonName", ""),
                                "not_after": not_after,
                            }
                            if not_after:
                                try:
                                    expiry = datetime.datetime.strptime(
                                        not_after, "%b %d %H:%M:%S %Y %Z")
                                    days_left = (expiry - datetime.datetime.utcnow()).days
                                    entry["cert"]["days_left"] = days_left
                                except Exception:
                                    pass
            except Exception:
                pass
        except Exception:
            pass
        results.append(entry)

    any_reachable = any(r["reachable"] for r in results)
    return {
        "https_active": any_reachable,
        "domains": domains,
        "results": results,
        "caddy_configured": True,
    }


@router.post("/https/test-domain")
async def https_test_domain(body: HttpsTestDomainBody):
    """Test HTTPS reachability + cert details for a specific domain on a cloud VM.
    Streams NDJSON progress events (SSH connect → read Caddyfile → probe → done)."""
    await _require_admin(body.requesting_user_id)
    domain = (body.domain or "").strip()
    return _stream(_https_test_domain_stream(
        body.provider, body.zone, body.name, domain))


@router.post("/https/domains-read")
async def https_domains_read(body: HttpsStatusBody):
    """Read the current Caddyfile and return the list of configured domains."""
    await _require_admin(body.requesting_user_id)
    profile = _find_ssh_profile(body.provider, body.zone, body.name)
    if not profile or not profile.get("host"):
        return {"ok": False, "domains": [], "detail": "No saved SSH login for this server."}

    client = None
    password = ""
    try:
        client, is_root, password = await _ssh_connect(profile)
        rc, caddyfile, err = await asyncio.get_running_loop().run_in_executor(
            None, _ssh_run_sudo, client, f"cat {_CADDYFILE}", is_root, password)
        if rc != 0:
            return {"ok": False, "domains": [], "detail": f"Could not read Caddyfile: {err}"}
        domains = _parse_caddy_domains(caddyfile)
        return {"ok": True, "domains": domains}
    except Exception as e:
        return {"ok": False, "domains": [], "detail": str(e)}
    finally:
        if client:
            try:
                client.close()
            except Exception:
                pass


@router.post("/https/domain-delete")
async def https_domain_delete(body: HttpsDeleteDomainBody):
    """Remove one domain from the Caddyfile and reload Caddy."""
    await _require_admin(body.requesting_user_id)
    domain = (body.domain or "").strip()
    if not domain:
        raise HTTPException(status_code=400, detail="A domain name is required.")
    return _stream(_https_domain_delete_stream(
        body.provider, body.zone, body.name, domain))


async def _https_domain_delete_stream(provider: str, zone: str, name: str,
                                       domain: str) -> AsyncIterator[Dict[str, Any]]:
    """Stream NDJSON: SSH in, remove the domain, reload Caddy."""
    yield ev("Finding this server's SSH login…", phase="locate")

    profile = _find_ssh_profile(provider, zone, name)
    if not profile or not profile.get("host"):
        yield done({"ok": False, "message": "No saved SSH login for this server."})
        return

    yield ev(f"Connecting to {profile['ssh_user']}@{profile['host']}…", phase="connect")
    client = None
    password = ""
    try:
        client, is_root, password = await _ssh_connect(profile)
    except Exception as e:
        yield done({"ok": False, "message": f"Could not connect over SSH: {e}"})
        return

    try:
        yield ev("Reading current Caddy config…", phase="read")
        rc, caddyfile, err = await asyncio.get_running_loop().run_in_executor(
            None, _ssh_run_sudo, client, f"cat {_CADDYFILE}", is_root, password)
        if rc != 0:
            yield done({"ok": False, "message": f"Could not read Caddyfile: {err}"})
            return

        current_domains = _parse_caddy_domains(caddyfile)
        if domain not in current_domains:
            yield done({"ok": True, "message": f"Domain {domain} is not in the Caddyfile."})
            return

        # Build a new Caddyfile with that domain removed from its site block.
        # If the site block ends up with no domains, remove the whole block.
        lines = caddyfile.splitlines(keepends=True)
        out_lines: List[str] = []
        brace_depth = 0
        block_start = -1
        drop_this_block = False
        for i, line in enumerate(lines):
            stripped = line.strip()
            if block_start < 0 and "{" in stripped and not stripped.startswith("#") and not stripped.startswith(":"):
                block_start = i
                brace_depth = 1
                # Parse the address line
                addr = stripped[:stripped.index("{")].strip()
                parts = [p.strip() for p in addr.split(",")]
                remaining = [p for p in parts if p != domain]
                if remaining:
                    out_lines.append(", ".join(remaining) + " {\n")
                else:
                    drop_this_block = True
                continue
            if block_start >= 0:
                brace_depth += stripped.count("{") - stripped.count("}")
                if brace_depth <= 0:
                    if not drop_this_block:
                        out_lines.append(line)
                    block_start = -1
                    brace_depth = 0
                    drop_this_block = False
                elif not drop_this_block:
                    out_lines.append(line)
                continue
            out_lines.append(line)

        new_caddyfile = "".join(out_lines)
        # If no site block left, add back a fallback :80 block
        remaining_after = _parse_caddy_domains(new_caddyfile)
        if not remaining_after:
            fallback = f":80 {{\n    reverse_proxy localhost:{_CADDY_PORT}\n}}\n"
            new_caddyfile = fallback + new_caddyfile
            yield ev("No domains remain — restoring plain HTTP (:80) fallback.", phase="write")

        import os
        _temp = f"/tmp/_caddy_{os.urandom(4).hex()}"
        try:
            sftp = client.open_sftp()
            try:
                with sftp.open(_temp, "w") as f:
                    f.write(new_caddyfile)
            finally:
                sftp.close()
        except Exception as e:
            yield done({"ok": False, "message": f"Could not upload Caddyfile: {e}"})
            return
        copy_cmd = f"cp {_temp} {_CADDYFILE} && rm -f {_temp}"
        rc, out, err = await asyncio.get_running_loop().run_in_executor(
            None, _ssh_run_sudo, client, copy_cmd, is_root, password)
        if rc != 0:
            yield done({"ok": False, "message": f"Could not write Caddyfile: {err}"})
            return

        yield ev("Reloading Caddy…", phase="reload")
        rc, out, err = await asyncio.get_running_loop().run_in_executor(
            None, _ssh_run_sudo, client, "systemctl reload caddy", is_root, password)
        if rc != 0:
            await asyncio.get_running_loop().run_in_executor(
                None, _ssh_run_sudo, client, "systemctl restart caddy", is_root, password)

        yield done({"ok": True, "message": f"Domain {domain} removed."})
    finally:
        try:
            client.close()
        except Exception:
            pass


async def _https_test_domain_stream(provider: str, zone: str, name: str,
                                     domain: str) -> AsyncIterator[Dict[str, Any]]:
    """Stream NDJSON: progress events → domain test result."""
    yield ev("Finding this server's SSH login…", phase="locate")

    profile = _find_ssh_profile(provider, zone, name)
    if not profile or not profile.get("host"):
        yield done({"ok": False, "message": "No saved SSH login for this server."})
        return

    yield ev(f"Connecting to {profile['ssh_user']}@{profile['host']}…", phase="connect")
    client = None
    password = ""
    try:
        client, is_root, password = await _ssh_connect(profile)
    except Exception as e:
        yield done({"ok": False, "message": f"Could not connect over SSH: {e}"})
        return

    domains_to_test: List[str] = []
    if domain and "." in domain:
        domains_to_test = [domain]
    else:
        # Read Caddyfile to discover configured domains
        yield ev("Reading Caddy config…", phase="read")
        try:
            rc, caddyfile, _ = await asyncio.get_running_loop().run_in_executor(
                None, _ssh_run_sudo, client, f"cat {_CADDYFILE}", is_root, password)
            if rc == 0:
                for line in caddyfile.splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and not line.startswith(":") and "{" in line:
                        addr = line[:line.index("{")].strip()
                        for part in addr.split(","):
                            part = part.strip()
                            if part and not part.startswith(":") and "." in part:
                                domains_to_test.append(part)
        except Exception as e:
            try:
                client.close()
            except Exception:
                pass
            yield done({"ok": False, "message": f"Could not read Caddyfile: {e}"})
            return

    if not domains_to_test:
        try:
            client.close()
        except Exception:
            pass
        yield done({"ok": False, "message": "No domains configured in the Caddyfile."})
        return

    try:
        import httpx
        import datetime
        import socket
        import ssl

        results: List[Dict[str, Any]] = []
        for d in domains_to_test:
            yield ev(f"Probing https://{d}/…", phase="probe")
            entry: Dict[str, Any] = {"domain": d, "reachable": False, "cert": None}
            try:
                async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as hc:
                    r = await hc.get(f"https://{d}/")
                entry["reachable"] = r.status_code < 500
                entry["http_status"] = r.status_code

                try:
                    ctx = ssl.create_default_context()
                    with socket.create_connection((d, 443), timeout=5) as sock:
                        with ctx.wrap_socket(sock, server_hostname=d) as ssock:
                            cert = ssock.getpeercert()
                            if cert:
                                not_after = cert.get("notAfter", "")
                                issuer = dict(x[0] for x in cert.get("issuer", []))
                                entry["cert"] = {
                                    "issuer": issuer.get("organizationName", issuer.get("commonName", "")),
                                    "subject_cn": dict(x[0] for x in cert.get("subject", [])).get("commonName", ""),
                                    "not_after": not_after,
                                }
                                if not_after:
                                    try:
                                        expiry = datetime.datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z")
                                        entry["cert"]["days_left"] = (expiry - datetime.datetime.utcnow()).days
                                    except Exception:
                                        pass
                except Exception:
                    pass
            except Exception as e:
                entry["error"] = str(e)
            results.append(entry)

        any_reachable = any(r["reachable"] for r in results)
        yield done({
            "ok": True,
            "https_active": any_reachable,
            "domains": [r["domain"] for r in results],
            "results": results,
        })
    finally:
        try:
            client.close()
        except Exception:
            pass
