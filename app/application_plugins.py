"""Standalone application plugins — independent of agent ability discovery.

RECORDER-HOOK: generic loader consumed by plugins/recorder/plugin.py. Remove its
main.py/frontend/GenUI hooks together if no application plugins remain. Plugin
absence is ordinary; imports are deferred, errors isolate one plugin. Manifests
live at plugins/<id>/plugin.json. Only declared frontend assets are served.
"""
from __future__ import annotations
import asyncio
import importlib
import hashlib
import json
import logging
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from app.api.db_viewer import require_admin

ROOT = Path(__file__).resolve().parents[1] / "plugins"
log = logging.getLogger(__name__)
router = APIRouter()
_asset_versions = {}

def asset_version(manifest):
    """RECORDER-HOOK: content-address the whole frontend in a URL directory.

    Relative JS/CSS imports inherit this directory. A query on only the entry
    module would not version its dependencies. Immutable builds preserve both
    CDN/browser cache reuse and coherent updates.
    """
    base = (ROOT / manifest["id"] / manifest.get("assets","frontend")).resolve()
    files = sorted(p for p in base.rglob("*") if p.is_file()
                   and p.suffix in {".js",".css",".svg",".woff2"}
                   and p.resolve().is_relative_to(base))
    signature = tuple((str(p),p.stat().st_mtime_ns,p.stat().st_size) for p in files)
    previous = _asset_versions.get(str(base))
    if previous and previous[0] == signature: return previous[1]
    digest = hashlib.sha256()
    for path in files:
        digest.update(str(path.relative_to(base)).encode())
        digest.update(path.read_bytes())
    version = "v-" + digest.hexdigest()[:16]
    _asset_versions[str(base)] = (signature, version)
    return version

def manifests():
    out = []
    # Direct-child manifests only: never walk engine adapters or runtime data.
    for path in sorted(ROOT.glob("*/plugin.json")):
        try:
            value = json.loads(path.read_text())
            if value.get("kind") != "application" or value.get("id") != path.parent.name:
                continue
            if not value["id"].replace("_", "").isalnum():
                continue
            out.append(value)
        except (OSError, ValueError, KeyError):
            continue
    return out

def modules():
    for manifest in manifests():
        try:
            yield manifest, importlib.import_module("plugins." + manifest["id"] + ".plugin")
        except Exception:
            log.warning("Application plugin %s unavailable", manifest["id"])

@router.get("/api/v1/application-plugins")
async def catalog(_auth: dict = Depends(require_admin)):
    return {"plugins": [{**m, "entry_url": "/plugin-assets/" + m["id"] + "/" + asset_version(m) + "/" + m["entry"]}
                        for m in manifests() if m.get("entry")]}

@router.get("/plugin-assets/{plugin_id}/{asset:path}")
async def asset(plugin_id: str, asset: str):
    manifest = next((m for m in manifests() if m["id"] == plugin_id), None)
    if not manifest:
        raise HTTPException(404, "Plugin not installed")
    version, separator, remainder = asset.partition("/")
    versioned = version.startswith("v-")
    if versioned:
        if not separator or version != asset_version(manifest):
            raise HTTPException(404, "Plugin build no longer available")
        asset = remainder
    base = (ROOT / plugin_id / manifest.get("assets", "frontend")).resolve()
    path = (base / asset).resolve()
    if not path.is_relative_to(base) or path.suffix not in {".js", ".css", ".svg", ".woff2"} or not path.is_file():
        raise HTTPException(404, "Asset not found")
    return FileResponse(path, headers={"Cache-Control": "public, max-age=31536000, immutable" if versioned else "no-cache"})

async def mount(app):
    if getattr(app.state, "application_plugins_mounted", False):
        return
    for manifest, module in modules():
        plugin_router = getattr(module, "router", None)
        if plugin_router is not None:
            app.include_router(plugin_router)
    app.state.application_plugins_mounted = True
    app.openapi_schema = None

async def start():
    for manifest, module in modules():
        callback = getattr(module, "start_background", None)
        if callback:
            try:
                await callback()
            except Exception:
                log.warning("Application plugin %s could not start", manifest["id"])

async def stop():
    for manifest, module in modules():
        callback = getattr(module, "stop_background", None)
        if callback:
            try:
                await callback()
            except Exception:
                pass

async def genui_pages(user_id):
    pages = []
    for manifest, module in modules():
        callback = getattr(module, "genui_pages", None)
        if callback:
            pages.extend(await callback(user_id))
    return pages

async def genui_html(user_id, slug):
    for manifest, module in modules():
        callback = getattr(module, "genui_html", None)
        if callback:
            html = await callback(user_id, slug)
            if html is not None:
                return html
    return None
