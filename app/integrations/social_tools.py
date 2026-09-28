"""Social network integration tools — Twitter/X, LinkedIn, Meta (FB + IG),
Reddit, Pinterest, Snapchat.

All HTTP goes through `oauth_helper.oauth_api_call` so token refresh / 401
retry stays consistent.

Notes per provider:
  - Meta is one Meta app powering both Facebook and Instagram. The OAuth
    token stored under `service="meta"` works against the Graph API; we expose
    Facebook + Instagram tools separately for clarity.
  - Snapchat's Snap Kit is limited to identity / Bitmoji; no posting API is
    available to third-party apps. Only `snapchat_userinfo` is exposed.
  - LinkedIn's UGC posting endpoint was deprecated; we use the newer
    `/rest/posts` endpoint, which requires the `LinkedIn-Version` header.
  - Reddit requires a custom User-Agent on every request.
"""

import json

from app.integrations.oauth_helper import (
    oauth_api_call,
    get_oauth_token,
    not_connected_payload,
)


# ── Twitter / X ───────────────────────────────────────────────────────────

_TW = "https://api.twitter.com/2"


async def twitter_me(user_id: str, agent_id: str) -> str:
    result = await oauth_api_call(user_id, agent_id, "twitter", "GET", f"{_TW}/users/me")
    if result.get("status") == "not_connected":
        return not_connected_payload("twitter")
    return json.dumps(result)


async def twitter_post_tweet(user_id: str, agent_id: str, text: str, reply_to_tweet_id: str = "") -> str:
    if not text:
        return json.dumps({"status": "error", "message": "text required"})
    body: dict = {"text": text}
    if reply_to_tweet_id:
        body["reply"] = {"in_reply_to_tweet_id": reply_to_tweet_id}
    result = await oauth_api_call(user_id, agent_id, "twitter", "POST", f"{_TW}/tweets", json_body=body)
    if result.get("status") == "not_connected":
        return not_connected_payload("twitter")
    return json.dumps(result)


async def twitter_list_my_tweets(user_id: str, agent_id: str, max_results: int = 10) -> str:
    # Need our user id first (Twitter v2 requires :id path).
    me = await oauth_api_call(user_id, agent_id, "twitter", "GET", f"{_TW}/users/me")
    if me.get("status") == "not_connected":
        return not_connected_payload("twitter")
    if me.get("status") != "ok":
        return json.dumps(me)
    me_id = ((me.get("body") or {}).get("data") or {}).get("id", "")
    if not me_id:
        return json.dumps({"status": "error", "message": "could not resolve Twitter user id", "body": me.get("body")})
    result = await oauth_api_call(
        user_id, agent_id, "twitter", "GET",
        f"{_TW}/users/{me_id}/tweets",
        params={
            "max_results": max(5, min(int(max_results or 10), 100)),
            "tweet.fields": "created_at,public_metrics,text",
        },
    )
    return json.dumps(result)


# ── LinkedIn ──────────────────────────────────────────────────────────────

_LI = "https://api.linkedin.com"


async def linkedin_me(user_id: str, agent_id: str) -> str:
    result = await oauth_api_call(user_id, agent_id, "linkedin", "GET", f"{_LI}/v2/userinfo")
    if result.get("status") == "not_connected":
        return not_connected_payload("linkedin")
    return json.dumps(result)


async def linkedin_post(user_id: str, agent_id: str, text: str, visibility: str = "PUBLIC") -> str:
    """Share a text post to the connected user's LinkedIn feed.

    visibility: PUBLIC | CONNECTIONS.
    """
    if not text:
        return json.dumps({"status": "error", "message": "text required"})

    me = await oauth_api_call(user_id, agent_id, "linkedin", "GET", f"{_LI}/v2/userinfo")
    if me.get("status") == "not_connected":
        return not_connected_payload("linkedin")
    if me.get("status") != "ok":
        return json.dumps(me)
    sub = ((me.get("body") or {}).get("sub", ""))
    if not sub:
        return json.dumps({"status": "error", "message": "could not resolve LinkedIn member id", "body": me.get("body")})
    author = f"urn:li:person:{sub}"

    body = {
        "author": author,
        "commentary": text,
        "visibility": visibility,
        "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [], "thirdPartyDistributionChannels": []},
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": False,
    }
    headers = {
        "LinkedIn-Version": "202404",
        "X-Restli-Protocol-Version": "2.0.0",
    }
    result = await oauth_api_call(
        user_id, agent_id, "linkedin", "POST",
        f"{_LI}/rest/posts",
        json_body=body,
        headers=headers,
    )
    return json.dumps(result)


# ── Meta — Facebook + Instagram (one OAuth app, one token) ────────────────

_META = "https://graph.facebook.com/v19.0"


async def facebook_me(user_id: str, agent_id: str) -> str:
    result = await oauth_api_call(
        user_id, agent_id, "meta", "GET",
        f"{_META}/me",
        params={"fields": "id,name,email"},
    )
    if result.get("status") == "not_connected":
        return not_connected_payload("meta")
    return json.dumps(result)


async def facebook_list_pages(user_id: str, agent_id: str) -> str:
    """List Facebook pages the connected user manages. Each page comes with
    its own page-level access token (use `access_token` field for posting)."""
    result = await oauth_api_call(
        user_id, agent_id, "meta", "GET",
        f"{_META}/me/accounts",
        params={"fields": "id,name,access_token,category,tasks"},
    )
    if result.get("status") == "not_connected":
        return not_connected_payload("meta")
    return json.dumps(result)


async def facebook_post_to_page(user_id: str, agent_id: str, page_id: str, message: str, page_access_token: str) -> str:
    """Post text to a Facebook page. page_access_token comes from facebook_list_pages."""
    if not page_id or not message or not page_access_token:
        return json.dumps({"status": "error", "message": "page_id, message, page_access_token required"})
    # Page-token posts don't go through our bearer — pass the page token as a form param.
    import httpx
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{_META}/{page_id}/feed",
                data={"message": message, "access_token": page_access_token},
            )
    except Exception as e:
        return json.dumps({"status": "error", "http_status": 0, "body": str(e)})
    try:
        body = resp.json()
    except Exception:
        body = resp.text
    return json.dumps({
        "status": "ok" if resp.is_success else "error",
        "http_status": resp.status_code,
        "body": body,
    })


async def instagram_list_accounts(user_id: str, agent_id: str) -> str:
    """List Instagram Business / Creator accounts linked to the user's FB pages."""
    result = await oauth_api_call(
        user_id, agent_id, "meta", "GET",
        f"{_META}/me/accounts",
        params={"fields": "id,name,instagram_business_account{id,username}"},
    )
    if result.get("status") == "not_connected":
        return not_connected_payload("meta")
    return json.dumps(result)


async def instagram_recent_media(user_id: str, agent_id: str, ig_user_id: str, max_results: int = 12) -> str:
    if not ig_user_id:
        return json.dumps({"status": "error", "message": "ig_user_id required (from instagram_list_accounts)"})
    result = await oauth_api_call(
        user_id, agent_id, "meta", "GET",
        f"{_META}/{ig_user_id}/media",
        params={
            "fields": "id,caption,media_type,media_url,permalink,timestamp",
            "limit": max(1, min(int(max_results or 12), 100)),
        },
    )
    return json.dumps(result)


# ── Reddit ────────────────────────────────────────────────────────────────

_REDDIT = "https://oauth.reddit.com"
_REDDIT_UA = {"User-Agent": "WebAgent/1.0"}


async def reddit_me(user_id: str, agent_id: str) -> str:
    result = await oauth_api_call(
        user_id, agent_id, "reddit", "GET",
        f"{_REDDIT}/api/v1/me",
        headers=_REDDIT_UA,
    )
    if result.get("status") == "not_connected":
        return not_connected_payload("reddit")
    return json.dumps(result)


async def reddit_listing(user_id: str, agent_id: str, subreddit: str = "", listing: str = "hot", limit: int = 10) -> str:
    """Fetch a Reddit listing. subreddit empty = front page; listing = hot|new|top|rising."""
    listing = (listing or "hot").lower()
    if listing not in {"hot", "new", "top", "rising", "controversial", "best"}:
        listing = "hot"
    url = f"{_REDDIT}/r/{subreddit}/{listing}" if subreddit else f"{_REDDIT}/{listing}"
    result = await oauth_api_call(
        user_id, agent_id, "reddit", "GET", url,
        params={"limit": max(1, min(int(limit or 10), 100))},
        headers=_REDDIT_UA,
    )
    if result.get("status") == "not_connected":
        return not_connected_payload("reddit")
    return json.dumps(result)


async def reddit_submit(user_id: str, agent_id: str, subreddit: str, title: str, text: str = "", url: str = "") -> str:
    """Submit a post to a subreddit. Provide text (self post) OR url (link post)."""
    if not subreddit or not title:
        return json.dumps({"status": "error", "message": "subreddit and title required"})
    if not text and not url:
        return json.dumps({"status": "error", "message": "either text or url required"})

    data = {
        "sr": subreddit,
        "title": title,
        "api_type": "json",
        "kind": "self" if text else "link",
    }
    if text:
        data["text"] = text
    if url:
        data["url"] = url

    # Reddit submit endpoint expects form-encoded body.
    result = await oauth_api_call(
        user_id, agent_id, "reddit", "POST",
        f"{_REDDIT}/api/submit",
        data=data,
        headers={**_REDDIT_UA, "Content-Type": "application/x-www-form-urlencoded"},
    )
    return json.dumps(result)


async def reddit_comment(user_id: str, agent_id: str, parent_fullname: str, text: str) -> str:
    """Comment on a post or reply to a comment. parent_fullname = 't3_<id>' for posts, 't1_<id>' for comments."""
    if not parent_fullname or not text:
        return json.dumps({"status": "error", "message": "parent_fullname and text required"})
    result = await oauth_api_call(
        user_id, agent_id, "reddit", "POST",
        f"{_REDDIT}/api/comment",
        data={"thing_id": parent_fullname, "text": text, "api_type": "json"},
        headers={**_REDDIT_UA, "Content-Type": "application/x-www-form-urlencoded"},
    )
    return json.dumps(result)


# ── Pinterest ─────────────────────────────────────────────────────────────

_PIN = "https://api.pinterest.com/v5"


async def pinterest_list_boards(user_id: str, agent_id: str, page_size: int = 25) -> str:
    result = await oauth_api_call(
        user_id, agent_id, "pinterest", "GET",
        f"{_PIN}/boards",
        params={"page_size": max(1, min(int(page_size or 25), 250))},
    )
    if result.get("status") == "not_connected":
        return not_connected_payload("pinterest")
    return json.dumps(result)


async def pinterest_list_pins(user_id: str, agent_id: str, board_id: str = "", page_size: int = 25) -> str:
    if board_id:
        url = f"{_PIN}/boards/{board_id}/pins"
    else:
        url = f"{_PIN}/pins"
    result = await oauth_api_call(
        user_id, agent_id, "pinterest", "GET", url,
        params={"page_size": max(1, min(int(page_size or 25), 250))},
    )
    if result.get("status") == "not_connected":
        return not_connected_payload("pinterest")
    return json.dumps(result)


async def pinterest_create_pin(
    user_id: str,
    agent_id: str,
    board_id: str,
    image_url: str,
    title: str = "",
    description: str = "",
    link: str = "",
) -> str:
    if not board_id or not image_url:
        return json.dumps({"status": "error", "message": "board_id and image_url required"})
    body = {
        "board_id": board_id,
        "media_source": {"source_type": "image_url", "url": image_url},
    }
    if title:
        body["title"] = title
    if description:
        body["description"] = description
    if link:
        body["link"] = link
    result = await oauth_api_call(
        user_id, agent_id, "pinterest", "POST",
        f"{_PIN}/pins",
        json_body=body,
    )
    return json.dumps(result)


# ── Snapchat (identity only) ──────────────────────────────────────────────

async def snapchat_userinfo(user_id: str, agent_id: str) -> str:
    """Snap Kit only exposes display name / Bitmoji / external id — no posting API exists."""
    result = await oauth_api_call(
        user_id, agent_id, "snapchat", "GET",
        "https://kit.snapchat.com/v1/me",
    )
    if result.get("status") == "not_connected":
        return not_connected_payload("snapchat")
    return json.dumps(result)


# ── Tool registry ─────────────────────────────────────────────────────────

FEATURE = {
    "id": "social",
    "display_name": "Social (X / LinkedIn / Meta / Reddit / Pinterest)",
    "category": "integration",
    "status": "beta",
    "summary": "Post and read across social platforms; coverage varies by provider.",
    "requires": ["Per-platform OAuth credentials"],
}

TOOLS = [
    # ── Twitter / X ──
    {"name": "twitter_me", "provider": "twitter", "handler": twitter_me, "stages": ["execute_tools"], "destructive": False,
     "parameters": {"type": "object", "properties": {}, "required": []}},
    {"name": "twitter_post_tweet", "provider": "twitter", "handler": twitter_post_tweet, "stages": ["execute_tools"], "destructive": True, "requires_confirmation": True,
     "parameters": {"type": "object", "properties": {
         "text": {"type": "string", "description": "Tweet text (≤280 chars unless Twitter Blue)."},
         "reply_to_tweet_id": {"type": "string", "description": "Optional tweet ID to reply to.", "default": ""},
     }, "required": ["text"]}},
    {"name": "twitter_list_my_tweets", "provider": "twitter", "handler": twitter_list_my_tweets, "stages": ["execute_tools"], "destructive": False,
     "parameters": {"type": "object", "properties": {
         "max_results": {"type": "integer", "description": "Max tweets (5-100).", "default": 10},
     }, "required": []}},

    # ── LinkedIn ──
    {"name": "linkedin_me", "provider": "linkedin", "handler": linkedin_me, "stages": ["execute_tools"], "destructive": False,
     "parameters": {"type": "object", "properties": {}, "required": []}},
    {"name": "linkedin_post", "provider": "linkedin", "handler": linkedin_post, "stages": ["execute_tools"], "destructive": True, "requires_confirmation": True,
     "parameters": {"type": "object", "properties": {
         "text":       {"type": "string", "description": "Post body."},
         "visibility": {"type": "string", "enum": ["PUBLIC", "CONNECTIONS"], "default": "PUBLIC"},
     }, "required": ["text"]}},

    # ── Meta (Facebook + Instagram) ──
    {"name": "facebook_me", "provider": "meta", "handler": facebook_me, "stages": ["execute_tools"], "destructive": False,
     "parameters": {"type": "object", "properties": {}, "required": []}},
    {"name": "facebook_list_pages", "provider": "meta", "handler": facebook_list_pages, "stages": ["execute_tools"], "destructive": False,
     "parameters": {"type": "object", "properties": {}, "required": []}},
    {"name": "facebook_post_to_page", "provider": "meta", "handler": facebook_post_to_page, "stages": ["execute_tools"], "destructive": True, "requires_confirmation": True,
     "parameters": {"type": "object", "properties": {
         "page_id":            {"type": "string", "description": "Facebook page ID (from facebook_list_pages)."},
         "message":            {"type": "string", "description": "Post body."},
         "page_access_token":  {"type": "string", "description": "Page-level access token (from facebook_list_pages → access_token)."},
     }, "required": ["page_id", "message", "page_access_token"]}},
    {"name": "instagram_list_accounts", "provider": "meta", "handler": instagram_list_accounts, "stages": ["execute_tools"], "destructive": False,
     "parameters": {"type": "object", "properties": {}, "required": []}},
    {"name": "instagram_recent_media", "provider": "meta", "handler": instagram_recent_media, "stages": ["execute_tools"], "destructive": False,
     "parameters": {"type": "object", "properties": {
         "ig_user_id":  {"type": "string",  "description": "Instagram Business/Creator user id."},
         "max_results": {"type": "integer", "description": "Max media items (1-100).", "default": 12},
     }, "required": ["ig_user_id"]}},

    # ── Reddit ──
    {"name": "reddit_me", "provider": "reddit", "handler": reddit_me, "stages": ["execute_tools"], "destructive": False,
     "parameters": {"type": "object", "properties": {}, "required": []}},
    {"name": "reddit_listing", "provider": "reddit", "handler": reddit_listing, "stages": ["execute_tools"], "destructive": False,
     "parameters": {"type": "object", "properties": {
         "subreddit": {"type": "string",  "description": "Subreddit name without '/r/'. Empty = front page.", "default": ""},
         "listing":   {"type": "string",  "enum": ["hot", "new", "top", "rising", "controversial", "best"], "default": "hot"},
         "limit":     {"type": "integer", "description": "Max items (1-100).", "default": 10},
     }, "required": []}},
    {"name": "reddit_submit", "provider": "reddit", "handler": reddit_submit, "stages": ["execute_tools"], "destructive": True, "requires_confirmation": True,
     "parameters": {"type": "object", "properties": {
         "subreddit": {"type": "string", "description": "Subreddit to post in (no '/r/')."},
         "title":     {"type": "string", "description": "Post title."},
         "text":      {"type": "string", "description": "Self-post body (omit for link post).", "default": ""},
         "url":       {"type": "string", "description": "Link URL (omit for text post).", "default": ""},
     }, "required": ["subreddit", "title"]}},
    {"name": "reddit_comment", "provider": "reddit", "handler": reddit_comment, "stages": ["execute_tools"], "destructive": True, "requires_confirmation": True,
     "parameters": {"type": "object", "properties": {
         "parent_fullname": {"type": "string", "description": "Reddit thing fullname — 't3_<id>' for a post, 't1_<id>' for a comment."},
         "text":            {"type": "string", "description": "Comment body."},
     }, "required": ["parent_fullname", "text"]}},

    # ── Pinterest ──
    {"name": "pinterest_list_boards", "provider": "pinterest", "handler": pinterest_list_boards, "stages": ["execute_tools"], "destructive": False,
     "parameters": {"type": "object", "properties": {
         "page_size": {"type": "integer", "description": "Max boards (1-250).", "default": 25},
     }, "required": []}},
    {"name": "pinterest_list_pins", "provider": "pinterest", "handler": pinterest_list_pins, "stages": ["execute_tools"], "destructive": False,
     "parameters": {"type": "object", "properties": {
         "board_id":  {"type": "string",  "description": "Board ID. Empty = all pins.", "default": ""},
         "page_size": {"type": "integer", "description": "Max pins (1-250).", "default": 25},
     }, "required": []}},
    {"name": "pinterest_create_pin", "provider": "pinterest", "handler": pinterest_create_pin, "stages": ["execute_tools"], "destructive": True, "requires_confirmation": True,
     "parameters": {"type": "object", "properties": {
         "board_id":    {"type": "string", "description": "Target board ID."},
         "image_url":   {"type": "string", "description": "Publicly-reachable image URL."},
         "title":       {"type": "string", "description": "Optional title.", "default": ""},
         "description": {"type": "string", "description": "Optional pin description.", "default": ""},
         "link":        {"type": "string", "description": "Optional destination URL.", "default": ""},
     }, "required": ["board_id", "image_url"]}},

    # ── Snapchat ──
    {"name": "snapchat_userinfo", "provider": "snapchat", "handler": snapchat_userinfo, "stages": ["execute_tools"], "destructive": False,
     "parameters": {"type": "object", "properties": {}, "required": []}},
]
