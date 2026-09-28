"""Video / streaming integration tools — TikTok + Twitch.

TikTok upload is a multi-step flow (init → upload chunks → publish) requiring
direct chunked uploads to a TikTok-provided URL; only `tiktok_userinfo` and
`tiktok_list_videos` are exposed here. Use the generic `oauth_api_call` for
the upload init endpoint if needed.

Twitch API endpoints require BOTH a bearer token AND a client-id header.
We inject the client-id ourselves so the agent doesn't need to know it.
"""

import json

from app.integrations.oauth_helper import oauth_api_call, not_connected_payload


# ── TikTok ────────────────────────────────────────────────────────────────

_TT = "https://open.tiktokapis.com/v2"


async def tiktok_userinfo(user_id: str, agent_id: str) -> str:
    result = await oauth_api_call(
        user_id, agent_id, "tiktok", "GET",
        f"{_TT}/user/info/",
        params={"fields": "open_id,union_id,avatar_url,display_name,bio_description,profile_deep_link,is_verified,follower_count,following_count,likes_count,video_count"},
    )
    if result.get("status") == "not_connected":
        return not_connected_payload("tiktok")
    return json.dumps(result)


async def tiktok_list_videos(user_id: str, agent_id: str, max_results: int = 10) -> str:
    body = {
        "max_count": max(1, min(int(max_results or 10), 20)),
    }
    result = await oauth_api_call(
        user_id, agent_id, "tiktok", "POST",
        f"{_TT}/video/list/",
        params={"fields": "id,title,cover_image_url,share_url,video_description,duration,create_time,like_count,comment_count,share_count,view_count"},
        json_body=body,
    )
    if result.get("status") == "not_connected":
        return not_connected_payload("tiktok")
    return json.dumps(result)


# ── Twitch ────────────────────────────────────────────────────────────────

_TWITCH = "https://api.twitch.tv/helix"


async def _twitch_client_id() -> str:
    """Fetch Twitch Client-Id (required on every Helix call)."""
    try:
        from app.admin.integrations import get_twitch_creds
        cid, _ = await get_twitch_creds()
        return cid or ""
    except Exception:
        return ""


async def _twitch_call(user_id: str, agent_id: str, method: str, path: str, params: dict = None, json_body: dict = None) -> dict:
    cid = await _twitch_client_id()
    if not cid:
        return {"status": "error", "message": "Twitch Client-Id missing — admin must configure Twitch OAuth in App Config → Integrations."}
    headers = {"Client-Id": cid}
    return await oauth_api_call(
        user_id, agent_id, "twitch", method,
        f"{_TWITCH}/{path.lstrip('/')}",
        params=params, json_body=json_body, headers=headers,
    )


async def twitch_me(user_id: str, agent_id: str) -> str:
    result = await _twitch_call(user_id, agent_id, "GET", "users")
    if result.get("status") == "not_connected":
        return not_connected_payload("twitch")
    return json.dumps(result)


async def twitch_get_streams(user_id: str, agent_id: str, user_login: str = "", game_id: str = "", limit: int = 20) -> str:
    """Live streams. Without filters, returns the most active live streams."""
    params: dict = {"first": max(1, min(int(limit or 20), 100))}
    if user_login:
        params["user_login"] = [s.strip() for s in user_login.split(",") if s.strip()]
    if game_id:
        params["game_id"] = game_id
    result = await _twitch_call(user_id, agent_id, "GET", "streams", params=params)
    if result.get("status") == "not_connected":
        return not_connected_payload("twitch")
    return json.dumps(result)


async def twitch_followed_channels(user_id: str, agent_id: str, limit: int = 20) -> str:
    """List channels the connected user follows."""
    me = await _twitch_call(user_id, agent_id, "GET", "users")
    if me.get("status") == "not_connected":
        return not_connected_payload("twitch")
    if me.get("status") != "ok":
        return json.dumps(me)
    data = ((me.get("body") or {}).get("data") or [])
    if not data:
        return json.dumps({"status": "error", "message": "could not resolve Twitch user id", "body": me.get("body")})
    me_id = data[0].get("id", "")
    result = await _twitch_call(
        user_id, agent_id, "GET", "channels/followed",
        params={"user_id": me_id, "first": max(1, min(int(limit or 20), 100))},
    )
    return json.dumps(result)


# ── Tool registry ─────────────────────────────────────────────────────────

FEATURE = {
    "id": "media",
    "display_name": "Media (TikTok / Twitch)",
    "category": "integration",
    "status": "beta",
    "summary": "TikTok and Twitch identity and media endpoints.",
    "requires": ["TikTok and/or Twitch OAuth credentials"],
}

TOOLS = [
    # ── TikTok ──
    {"name": "tiktok_userinfo", "provider": "tiktok", "handler": tiktok_userinfo, "stages": ["execute_tools"], "destructive": False,
     "parameters": {"type": "object", "properties": {}, "required": []}},
    {"name": "tiktok_list_videos", "provider": "tiktok", "handler": tiktok_list_videos, "stages": ["execute_tools"], "destructive": False,
     "parameters": {"type": "object", "properties": {
         "max_results": {"type": "integer", "description": "Max videos (1-20).", "default": 10},
     }, "required": []}},

    # ── Twitch ──
    {"name": "twitch_me", "provider": "twitch", "handler": twitch_me, "stages": ["execute_tools"], "destructive": False,
     "parameters": {"type": "object", "properties": {}, "required": []}},
    {"name": "twitch_get_streams", "provider": "twitch", "handler": twitch_get_streams, "stages": ["execute_tools"], "destructive": False,
     "parameters": {"type": "object", "properties": {
         "user_login": {"type": "string",  "description": "Comma-separated Twitch user logins. Blank = top live streams.", "default": ""},
         "game_id":    {"type": "string",  "description": "Optional game ID to filter.", "default": ""},
         "limit":      {"type": "integer", "description": "Max streams (1-100).", "default": 20},
     }, "required": []}},
    {"name": "twitch_followed_channels", "provider": "twitch", "handler": twitch_followed_channels, "stages": ["execute_tools"], "destructive": False,
     "parameters": {"type": "object", "properties": {
         "limit": {"type": "integer", "description": "Max channels (1-100).", "default": 20},
     }, "required": []}},
]
