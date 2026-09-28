"""Public Remote Access endpoints (the signpost).

These are intentionally NOT admin-gated — the phone and the remote PC are the
callers:

* ``POST /api/v1/remote-access/pointer`` — a PC reports its current address.
  Authorized by the secret push token bound to the rendezvous key (not a user
  login), because it's a server-to-server call.
* ``GET  /api/v1/remote-access/pointer/{key}`` — JSON lookup (for thin clients).
* ``GET  /go/{key}`` — the phone's permanent bookmark; forwards to the live
  address. This stays the same forever even as the PC's address changes.

The directory is multi-tenant: one entry per key, so this same install can host
the lookup for many users (see app/remote_access/store.py).
"""

from __future__ import annotations

import html
import logging

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from app.remote_access import store

logger = logging.getLogger(__name__)
router = APIRouter(tags=["remote-access"])


class PointerBody(BaseModel):
    key: str
    push_token: str = ""
    url: str
    label: str = ""


@router.post("/api/v1/remote-access/pointer")
async def push_pointer(body: PointerBody):
    if not body.key or not body.url:
        return JSONResponse({"ok": False, "error": "missing key or url"}, status_code=400)
    result = store.set_pointer(body.key, body.url, body.push_token, body.label)
    return JSONResponse(result, status_code=200)


@router.get("/api/v1/remote-access/pointer/{key}")
async def lookup_pointer(key: str):
    entry = store.get_pointer(key)
    if not entry or not entry.get("url"):
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    return {"ok": True, **entry}


_PAGE_CSS = """
:root{color-scheme:light dark}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
  font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
  background:#f4f5f7;color:#1b1b2a}
@media (prefers-color-scheme:dark){body{background:#0d0d1a;color:#e9e9f2}}
.card{max-width:420px;width:92%;padding:28px 26px;border-radius:14px;text-align:center;
  background:#ffffff;box-shadow:0 8px 40px rgba(0,0,0,.12)}
@media (prefers-color-scheme:dark){.card{background:#171728;box-shadow:0 8px 40px rgba(0,0,0,.5)}}
h1{font-size:18px;margin:0 0 6px}
p{font-size:14px;line-height:1.5;opacity:.8;margin:6px 0 18px}
a.btn{display:inline-block;padding:11px 20px;border-radius:9px;text-decoration:none;
  font-weight:600;font-size:14px;background:#5b8cff;color:#fff}
.muted{font-size:12px;opacity:.6;margin-top:16px;word-break:break-all}
"""


@router.get("/go/{key}", response_class=HTMLResponse)
async def go(key: str):
    """The phone's permanent bookmark — forward to the PC's current address."""
    entry = store.get_pointer(key)
    url = (entry or {}).get("url", "") if entry else ""
    label = (entry or {}).get("label", "") if entry else ""

    if url:
        safe_url = html.escape(url, quote=True)
        safe_label = html.escape(label) if label else "your PC"
        page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<meta http-equiv="refresh" content="1;url={safe_url}">
<title>Connecting…</title><style>{_PAGE_CSS}</style>
<script>setTimeout(function(){{location.href={safe_url!r};}},250);</script></head>
<body><div class="card"><h1>Connecting to {safe_label}…</h1>
<p>Taking you to the live address. If nothing happens, tap below.</p>
<a class="btn" href="{safe_url}">Open {safe_label}</a>
<div class="muted">{safe_url}</div></div></body></html>"""
        return HTMLResponse(page)

    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>Not connected</title><style>{_PAGE_CSS}</style></head>
<body><div class="card"><h1>PC not reachable yet</h1>
<p>This bookmark is valid, but the PC hasn't reported an address. Open WebAgent
on the PC, turn on a Remote Access method, and it will register here
automatically.</p>
<div class="muted">key: {html.escape(key)}</div></div></body></html>"""
    return HTMLResponse(page, status_code=404)
