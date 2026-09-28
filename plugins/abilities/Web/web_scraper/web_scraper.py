"""Web Scraper ability — SELF-CONTAINED drop-in.

Bring-your-own scraping provider (Apify / RapidAPI / custom HTTP) for sites with
no public API. The handlers used to live in ``app/integrations/web_tools.py``
gated by the legacy ``scraper`` provider; they now live here and read their
credentials through the common-credential framework
(``app/abilities/credentials.py``) — declared in ``web_scraper.json``'s
``credentials`` block (admin scope, key ``api_key``).

Discovered generically by core via three module hooks (app/tools/loader.py
"Self-contained ability tools"): FEATURE-less — the JSON descriptor carries the
metadata; ``build_tools()`` returns the handlers; ``TOOL_SCHEMAS`` / ``DESTRUCTIVE``
are read after build_tools().
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 30.0

# Both scrape tools are read-only fetches.
DESTRUCTIVE: set = set()
TOOL_SCHEMAS: dict = {}


def _err(msg: str, **extra) -> str:
    return json.dumps({"status": "error", "message": msg, **extra})


def _not_configured() -> str:
    return json.dumps({
        "status": "not_configured",
        "provider": "scraper",
        "message": "Web Scraper is not configured — set its provider + API key in "
                   "App Config → Agent Settings → Web → Web Scraper.",
    })


async def _load_scraper_creds() -> Optional[dict]:
    """Flat creds dict {provider, api_key, endpoint, actor_id, host} or None."""
    try:
        from app.abilities import credentials as _creds
        creds = await _creds.read_credentials("web_scraper")
    except Exception as e:
        logger.warning("read web_scraper creds failed: %s", e)
        return None
    if not creds or not (creds.get("api_key") or "").strip():
        return None
    return creds


async def web_scrape_search(
    query: str,
    location: str = "",
    filters: Optional[dict] = None,
    max_results: int = 20,
) -> str:
    """Search via the configured scraper. Payload is intentionally generic;
    actor-specific keys can be passed via `filters`."""
    if not query:
        return _err("query is required")
    creds = await _load_scraper_creds()
    if not creds:
        return _not_configured()

    provider = (creds.get("provider") or "").lower()
    api_key = creds["api_key"]
    endpoint = (creds.get("endpoint") or "").strip()
    payload = {
        "query": query,
        "location": location,
        "filters": filters or {},
        "maxItems": max(1, min(int(max_results or 20), 100)),
    }

    try:
        import httpx
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            if provider == "apify":
                actor = (creds.get("actor_id") or "").strip() or endpoint
                if not actor:
                    return _err("apify provider requires an Apify Actor ID (set it in the Web Scraper credentials)")
                url = f"https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items?token={api_key}"
                resp = await client.post(url, json=payload)
            elif provider == "rapidapi":
                host = (creds.get("host") or "").strip()
                base = endpoint or (f"https://{host}" if host else "")
                if not base:
                    return _err("rapidapi provider requires an endpoint or host (set it in the Web Scraper credentials)")
                headers = {"X-RapidAPI-Key": api_key}
                if host:
                    headers["X-RapidAPI-Host"] = host
                resp = await client.get(base, params=payload, headers=headers)
            else:
                if not endpoint:
                    return _err("custom_http provider requires an endpoint (set it in the Web Scraper credentials)")
                headers = {"Authorization": f"Bearer {api_key}"}
                resp = await client.get(endpoint, params=payload, headers=headers)
    except Exception as e:
        return _err(f"upstream error: {e}")

    try:
        body: Any = resp.json()
    except Exception:
        body = resp.text
    return json.dumps({
        "status": "ok" if resp.is_success else "error",
        "http_status": resp.status_code,
        "provider": provider,
        "results": body if isinstance(body, list) else (body if isinstance(body, dict) else {"raw": body}),
    })


async def web_scrape_url(url: str, render_js: bool = False) -> str:
    """Fetch a single URL through the configured scraper (handles JS / blocks)."""
    if not url:
        return _err("url is required")
    creds = await _load_scraper_creds()
    if not creds:
        return _not_configured()

    provider = (creds.get("provider") or "").lower()
    api_key = creds["api_key"]
    endpoint = (creds.get("endpoint") or "").strip()

    try:
        import httpx
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            if provider == "apify":
                actor = (creds.get("actor_id") or "").strip() or endpoint
                if not actor:
                    return _err("apify provider requires an Apify Actor ID")
                api_url = f"https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items?token={api_key}"
                resp = await client.post(api_url, json={"startUrls": [{"url": url}], "renderJs": bool(render_js)})
            elif provider == "rapidapi":
                host = (creds.get("host") or "").strip()
                base = endpoint or (f"https://{host}" if host else "")
                if not base:
                    return _err("rapidapi provider requires an endpoint or host")
                headers = {"X-RapidAPI-Key": api_key}
                if host:
                    headers["X-RapidAPI-Host"] = host
                resp = await client.get(base, params={"url": url, "render_js": int(bool(render_js))}, headers=headers)
            else:
                if not endpoint:
                    return _err("custom_http provider requires an endpoint")
                headers = {"Authorization": f"Bearer {api_key}"}
                resp = await client.get(endpoint, params={"url": url, "render_js": int(bool(render_js))}, headers=headers)
    except Exception as e:
        return _err(f"upstream error: {e}")

    try:
        body = resp.json()
    except Exception:
        body = resp.text
    return json.dumps({
        "status": "ok" if resp.is_success else "error",
        "http_status": resp.status_code,
        "url": url,
        "body": body,
    })


def build_tools(*, user_id: str = "", session_id: str = "", agent_id: str = "",
                agent_template_id: Optional[str] = None, enabled_providers=None,
                **_ctx):
    """Return {tool_name: handler}. Creds are admin-global, so the handlers need
    no per-user closure — they read them through the credential framework."""
    TOOL_SCHEMAS.clear()
    TOOL_SCHEMAS.update({
        "web_scrape_search": {"type": "object", "properties": {
            "query":       {"type": "string",  "description": "Search query — passed through to the configured scraper."},
            "location":    {"type": "string",  "description": "Optional location filter.", "default": ""},
            "filters":     {"type": "object",  "description": "Optional scraper-specific filters (passed through verbatim)."},
            "max_results": {"type": "integer", "description": "Max items to return (1-100).", "default": 20},
        }, "required": ["query"]},
        "web_scrape_url": {"type": "object", "properties": {
            "url":       {"type": "string",  "description": "Full URL to scrape."},
            "render_js": {"type": "boolean", "description": "Ask the scraper to render JS (slower; needed for SPAs).", "default": False},
        }, "required": ["url"]},
    })
    return {
        "web_scrape_search": web_scrape_search,
        "web_scrape_url": web_scrape_url,
    }
