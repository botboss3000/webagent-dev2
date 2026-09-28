"""Web Access ability — SELF-CONTAINED drop-in.

TIER-1 ability: all three handlers now live IN THIS FILE (relocated out of core
so the ability is self-contained). ``web_search`` and ``get_weather`` were moved
from ``app/tools/core_tools.py``; ``maps_geocode`` (+ its schema) was moved from
the now-deleted ``app/tools/maps.py``. Handlers are defined at MODULE LEVEL so
other modules can import them directly — notably ``web_search`` is imported by
the Web Access ability through ``ability_module('web_access')``.

Discovered generically by core via three optional module hooks (see
app/abilities/__init__.py "Self-contained abilities"):
  • FEATURE       — catalog + UI + which tool names it gates
  • build_tools() — its tool handlers (app/tools/loader.py injects them)
  • TOOL_SCHEMAS / DESTRUCTIVE — read by the loader AFTER build_tools() runs

What it does: lets an agent search the web, look up weather, and geocode
addresses — lightweight web lookups that don't drive a real browser, using free
public APIs (no API keys needed).
"""

from __future__ import annotations

import json
import logging
import math
from typing import Optional

logger = logging.getLogger(__name__)

# Populated inside build_tools(): web_search/get_weather schemas are inlined
# below (copied VERBATIM from the loader blocks), maps_geocode's schema is the
# module-level _MAPS_PARAMS so it never drifts. The loader reads TOOL_SCHEMAS
# AFTER calling build_tools().
TOOL_SCHEMAS: dict = {}

# All three are read-only public-API lookups (loader marked them
# destructive=False), so DESTRUCTIVE stays empty.
DESTRUCTIVE: set = set()


# ── Web search ────────────────────────────────────────────────────────────────

# Returned when every free search backend refuses to serve this server (rate
# limit / bot challenge). It is deliberately explicit and anti-fabrication: a
# blocked backend is NOT the same as "no results", and the old "No results
# found" wording led models to invent an answer from training data.
_SEARCH_UNAVAILABLE = (
    "WEB SEARCH UNAVAILABLE: the free DuckDuckGo backend refused this request "
    "(rate-limited or bot-challenged for this server's IP). This is a backend "
    "outage, NOT an empty result set — do NOT guess or answer '{query}' from "
    "memory. Instead: use the browser (browser_action) to open a page, use "
    "http_request to fetch a specific URL or public API directly, or ask the "
    "admin to configure a search API key. If you cannot reach a source, say so."
)


def _parse_ddg_html(html: str, max_results: int) -> list:
    """Parse result rows from either DDG markup (html/ uses result__a/__snippet;
    lite/ uses a results table of result-link <a>s). Returns formatted strings."""
    import re
    results: list = []
    # Rich html.duckduckgo.com markup
    link_pattern = re.compile(r'<a[^>]+class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.DOTALL)
    snippet_pattern = re.compile(r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>', re.DOTALL)
    links = link_pattern.findall(html)
    snippets = snippet_pattern.findall(html)
    if links:
        for i in range(min(len(links), max_results)):
            href, title_html = links[i]
            title = re.sub(r'<[^>]+>', '', title_html).strip()
            snippet = re.sub(r'<[^>]+>', '', snippets[i]).strip() if i < len(snippets) else ""
            if href.startswith("//"):
                href = "https:" + href
            results.append(f"{i+1}. {title}\n   {snippet}\n   {href}")
        return results
    # lite.duckduckgo.com markup: <a class="result-link" href="...">title</a>
    lite = re.findall(r'<a[^>]+class="result-link"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html, re.DOTALL)
    for i, (href, title_html) in enumerate(lite[:max_results]):
        title = re.sub(r'<[^>]+>', '', title_html).strip()
        if href.startswith("//"):
            href = "https:" + href
        results.append(f"{i+1}. {title}\n   {href}")
    return results


async def web_search(query: str, max_results: int = 5) -> str:
    """
    Search the web using DuckDuckGo (no API key required).

    Returns titled search results with snippets and URLs. Use for finding
    current information, documentation, news, or any web content.

    If the free backend is rate-limited/blocked for this server, returns an
    explicit "WEB SEARCH UNAVAILABLE" message — that is a backend outage, not an
    empty result set. Never invent results from memory in that case; fall back to
    browser_action / http_request, or ask the admin to configure a search key.
    """
    import httpx

    max_results = min(max_results, 10)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    # DuckDuckGo returns HTTP 202 with a bot-challenge page when it blocks a
    # request; that is a 2xx, so raise_for_status() would NOT catch it. Treat any
    # non-200 — or a 200 whose markup yields zero parseable rows — as a backend
    # failure and try the next endpoint before giving up.
    endpoints = [
        ("post", "https://html.duckduckgo.com/html/"),
        ("get", "https://lite.duckduckgo.com/lite/"),
    ]
    blocked = False
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True, headers=headers) as client:
            for method, url in endpoints:
                try:
                    if method == "post":
                        resp = await client.post(url, data={"q": query})
                    else:
                        resp = await client.get(url, params={"q": query})
                except Exception:
                    blocked = True
                    continue
                if resp.status_code != 200:
                    blocked = True
                    continue
                results = _parse_ddg_html(resp.text, max_results)
                if results:
                    return f"Search results for '{query}':\n\n" + "\n\n".join(results)
                # 200 but nothing parseable: could be a genuine no-hit OR a
                # challenge page served as 200. The challenge page never contains
                # result markup AND is short on the query echo, so fall through to
                # the next endpoint; if all endpoints come back empty, report it
                # as unavailable rather than a confident "no results".
                blocked = True
    except Exception:
        blocked = True

    if blocked:
        return _SEARCH_UNAVAILABLE.format(query=query)
    return f"No results found for '{query}'."


# ── Weather ───────────────────────────────────────────────────────────────────

async def get_weather(location: str, units: str = "metric") -> str:
    """
    Get current weather conditions for a location.

    Uses Open-Meteo (free, no API key required). Fetches temperature,
    conditions, wind speed, and humidity.

    Args:
        location: City name or "lat,lon" coordinates (e.g. "London", "New York", "40.71,-74.01")
        units: "metric" (Celsius, km/h) or "imperial" (Fahrenheit, mph)

    Returns:
        Formatted weather report for the location.
    """
    import httpx

    try:
        # Resolve location to coordinates
        lat, lon = await _resolve_location(location)

        # Fetch weather from Open-Meteo (free, no key)
        temp_unit = "celsius" if units == "metric" else "fahrenheit"
        wind_unit = "kmh" if units == "metric" else "mph"

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "current": ["temperature_2m", "relative_humidity_2m",
                                "apparent_temperature", "weather_code",
                                "wind_speed_10m", "wind_direction_10m"],
                    "temperature_unit": temp_unit,
                    "wind_speed_unit": wind_unit,
                    "timezone": "auto",
                },
            )
            resp.raise_for_status()
            data = resp.json()

        current = data.get("current", {})
        temp = current.get("temperature_2m", "?")
        feels_like = current.get("apparent_temperature", "?")
        humidity = current.get("relative_humidity_2m", "?")
        wind_speed = current.get("wind_speed_10m", "?")
        weather_code = current.get("weather_code", 0)

        conditions = _weather_code_to_text(weather_code)
        temp_label = "C" if units == "metric" else "F"
        wind_label = "km/h" if units == "metric" else "mph"

        return (
            f"Weather in {location}: {conditions}\n"
            f"Temperature: {temp}°{temp_label} (feels like {feels_like}°{temp_label})\n"
            f"Humidity: {humidity}%\n"
            f"Wind: {wind_speed} {wind_label}"
        )

    except Exception as e:
        logger.warning("Open-Meteo failed for %s: %s", location, e)
        # Fallback: try wttr.in
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"https://wttr.in/{_encode_location(location)}?format=%C+%t+%w+%h"
                )
                resp.raise_for_status()
                text = resp.text.strip()
                if text:
                    return f"Weather in {location}: {text}"
        except Exception:
            pass
        return f"Could not fetch weather for '{location}'. Please try a different location name or use 'lat,lon' coordinates."


_WEATHER_CODES = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Foggy",
    48: "Depositing rime fog",
    51: "Light drizzle",
    53: "Moderate drizzle",
    55: "Dense drizzle",
    56: "Light freezing drizzle",
    57: "Dense freezing drizzle",
    61: "Slight rain",
    63: "Moderate rain",
    65: "Heavy rain",
    66: "Light freezing rain",
    67: "Heavy freezing rain",
    71: "Slight snowfall",
    73: "Moderate snowfall",
    75: "Heavy snowfall",
    77: "Snow grains",
    80: "Slight rain showers",
    81: "Moderate rain showers",
    82: "Violent rain showers",
    85: "Slight snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with slight hail",
    99: "Thunderstorm with heavy hail",
}


def _weather_code_to_text(code: int) -> str:
    return _WEATHER_CODES.get(code, f"Unknown ({code})")


def _encode_location(location: str) -> str:
    """Encode location for URL use."""
    import urllib.parse
    return urllib.parse.quote(location.replace(",", " "))


async def _resolve_location(location: str) -> tuple:
    """
    Resolve a location name to (lat, lon) coordinates.
    If already "lat,lon" format, parse directly.
    """
    # Check if already coordinates
    parts = location.split(",")
    if len(parts) == 2:
        try:
            lat = float(parts[0].strip())
            lon = float(parts[1].strip())
            if -90 <= lat <= 90 and -180 <= lon <= 180:
                return (lat, lon)
        except ValueError:
            pass

    # Geocode via Open-Meteo (free, no key)
    import httpx
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": location, "count": 1, "language": "en", "format": "json"},
        )
        resp.raise_for_status()
        data = resp.json()

    results = data.get("results")
    if not results:
        raise ValueError(f"Location '{location}' not found")

    return (results[0]["latitude"], results[0]["longitude"])


# ── Maps & geocoding (Nominatim — free, no API key) ───────────────────────────
# Relocated from the deleted app/tools/maps.py.
#
# Nominatim usage policy requires an identifying User-Agent and a rate cap of
# ~1 req/sec. The webagent backend is a single-tenant host so this is fine in
# practice; if usage grows the operator can swap in a paid provider.
_NOMINATIM_BASE = "https://nominatim.openstreetmap.org"
_USER_AGENT = "webagent/1.0 (+https://webagent.live)"
_EARTH_KM = 6371.0088


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * _EARTH_KM * math.asin(math.sqrt(a))


async def maps_geocode(
    action: str,
    location: Optional[str] = None,
    lat: Optional[float] = None,
    lon: Optional[float] = None,
    lat2: Optional[float] = None,
    lon2: Optional[float] = None,
    limit: int = 5,
) -> str:
    """Forward geocode, reverse geocode, or compute distance between two points.

    action="geocode":  address/place name → list of {display_name, lat, lon, type}
    action="reverse":  (lat, lon) → {display_name, address parts}
    action="distance": (lat, lon) and (lat2, lon2) → {km, miles}
    """
    try:
        import httpx
    except Exception as e:
        return json.dumps({"status": "error", "message": f"httpx unavailable: {e}"})

    action = (action or "").lower().strip()

    if action == "distance":
        if lat is None or lon is None or lat2 is None or lon2 is None:
            return json.dumps({"status": "error", "message": "distance requires lat, lon, lat2, lon2"})
        km = _haversine_km(float(lat), float(lon), float(lat2), float(lon2))
        return json.dumps({"status": "ok", "km": round(km, 3), "miles": round(km * 0.621371, 3)})

    headers = {"User-Agent": _USER_AGENT, "Accept": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            if action == "geocode":
                if not location:
                    return json.dumps({"status": "error", "message": "geocode requires 'location'"})
                resp = await client.get(
                    f"{_NOMINATIM_BASE}/search",
                    params={"q": location, "format": "jsonv2", "limit": max(1, min(limit, 10))},
                    headers=headers,
                )
                if resp.status_code != 200:
                    return json.dumps({"status": "error", "message": f"Nominatim HTTP {resp.status_code}"})
                rows = resp.json() or []
                results = [
                    {
                        "display_name": r.get("display_name"),
                        "lat": float(r.get("lat")) if r.get("lat") else None,
                        "lon": float(r.get("lon")) if r.get("lon") else None,
                        "type": r.get("type"),
                        "category": r.get("category"),
                    }
                    for r in rows
                ]
                return json.dumps({"status": "ok", "query": location, "results": results, "count": len(results)})

            if action == "reverse":
                if lat is None or lon is None:
                    return json.dumps({"status": "error", "message": "reverse requires 'lat' and 'lon'"})
                resp = await client.get(
                    f"{_NOMINATIM_BASE}/reverse",
                    params={"lat": lat, "lon": lon, "format": "jsonv2"},
                    headers=headers,
                )
                if resp.status_code != 200:
                    return json.dumps({"status": "error", "message": f"Nominatim HTTP {resp.status_code}"})
                row = resp.json() or {}
                return json.dumps({
                    "status": "ok",
                    "display_name": row.get("display_name"),
                    "address": row.get("address") or {},
                    "lat": float(row.get("lat")) if row.get("lat") else None,
                    "lon": float(row.get("lon")) if row.get("lon") else None,
                })

            return json.dumps({"status": "error", "message": f"unknown action '{action}'. Use geocode, reverse, or distance."})
    except Exception as e:
        logger.exception("maps_geocode failed")
        return json.dumps({"status": "error", "message": str(e)})


# maps_geocode's JSON schema — relocated VERBATIM from app/tools/maps.TOOL_PARAMETERS.
_MAPS_PARAMS = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["geocode", "reverse", "distance"],
            "description": "geocode = address→coords; reverse = coords→address; distance = great-circle km/miles between two points",
        },
        "location": {"type": "string", "description": "Address or place name (for geocode action)"},
        "lat": {"type": "number", "description": "Latitude (reverse and distance actions)"},
        "lon": {"type": "number", "description": "Longitude (reverse and distance actions)"},
        "lat2": {"type": "number", "description": "Second-point latitude (distance action)"},
        "lon2": {"type": "number", "description": "Second-point longitude (distance action)"},
        "limit": {"type": "integer", "description": "Max geocode matches (default 5, max 10)", "default": 5},
    },
    "required": ["action"],
}


def build_tools(*, user_id: str = "", session_id: str = "", agent_id: str = "",
                agent_template_id: Optional[str] = None, enabled_providers=None,
                **_ctx):
    """Return {tool_name: handler} for the web-access tools.

    Returns the three module-level handlers directly (they take their args
    directly — no user_id closure needed). Inlines the web_search / get_weather
    schemas and pulls maps_geocode's schema from the module-level _MAPS_PARAMS,
    refreshing module-level TOOL_SCHEMAS so it can't drift.
    """
    TOOL_SCHEMAS.clear()
    TOOL_SCHEMAS.update({
        "web_search": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query"},
                "max_results": {
                    "type": "integer",
                    "description": "Maximum results to return (default 5, max 10)",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
        "maps_geocode": _MAPS_PARAMS,
        "get_weather": {
            "type": "object",
            "properties": {
                "location": {"type": "string", "description": "City name or 'lat,lon' coordinates (e.g. 'London', '40.71,-74.01')"},
                "units": {"type": "string", "enum": ["metric", "imperial"], "description": "metric = Celsius/km/h, imperial = Fahrenheit/mph", "default": "metric"},
            },
            "required": ["location"],
        },
    })

    return {
        "web_search": web_search,
        "maps_geocode": maps_geocode,
        "get_weather": get_weather,
    }
