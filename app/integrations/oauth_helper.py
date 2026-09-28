"""Shared OAuth helpers for integration tools.

Centralises:
  - get_oauth_token(): reads access_token from auth_elements, auto-refreshes
    if expired (Google / Microsoft / Dropbox / Yahoo supported), persists the
    refreshed token back to auth_elements.
  - oauth_api_call(): generic HTTP call that injects the bearer token, parses
    JSON when possible, and surfaces structured errors the agent can act on.

Per-provider integration modules should always go through these helpers so
token refresh and error handling stay consistent.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)


# Mapping: provider -> (token_url, basic_auth_required, scope_in_refresh)
_REFRESH_ENDPOINTS = {
    "google":    ("https://oauth2.googleapis.com/token",                                False, False),
    "microsoft": ("https://login.microsoftonline.com/common/oauth2/v2.0/token",         False, True),
    "dropbox":   ("https://api.dropboxapi.com/oauth2/token",                            False, False),
    "yahoo":     ("https://api.login.yahoo.com/oauth2/get_token",                       True,  False),
    "reddit":    ("https://www.reddit.com/api/v1/access_token",                         True,  False),
    "twitter":   ("https://api.twitter.com/2/oauth2/token",                             True,  False),
    "linkedin":  ("https://www.linkedin.com/oauth/v2/accessToken",                      False, False),
    "tiktok":    ("https://open.tiktokapis.com/v2/oauth/token/",                        False, False),
    "twitch":    ("https://id.twitch.tv/oauth2/token",                                  False, False),
    "ebay":      ("https://api.ebay.com/identity/v1/oauth2/token",                      True,  False),
    "etsy":      ("https://api.etsy.com/v3/public/oauth/token",                         False, False),
    "amazon":    ("https://api.amazon.com/auth/o2/token",                               False, False),
    # Shopify access tokens don't expire — no refresh endpoint needed.
}


_PROVIDER_CRED_FNS = {
    "google":    "get_google_creds",
    "microsoft": "get_microsoft_creds",
    "yahoo":     "get_yahoo_creds",
    "dropbox":   "get_dropbox_creds",
    "meta":      "get_meta_creds",
    "twitter":   "get_twitter_creds",
    "linkedin":  "get_linkedin_creds",
    "tiktok":    "get_tiktok_creds",
    "pinterest": "get_pinterest_creds",
    "reddit":    "get_reddit_creds",
    "snapchat":  "get_snapchat_creds",
    "twitch":    "get_twitch_creds",
    "ebay":      "get_ebay_creds",
    "etsy":      "get_etsy_creds",
    "shopify":   "get_shopify_creds",
    "amazon":    "get_amazon_creds",
}


# User-facing aliases (mirrors check_oauth_connection aliases).
PROVIDER_ALIASES = {
    "gmail":     "google",
    "drive":     "google",
    "calendar":  "google",
    "gcal":      "google",
    "docs":      "google",
    "outlook":   "microsoft",
    "onedrive":  "microsoft",
    "facebook":  "meta",
    "instagram": "meta",
    "x":         "twitter",
}


def normalize_provider(provider: str) -> str:
    p = (provider or "").lower().strip()
    return PROVIDER_ALIASES.get(p, p)


def oauth_label(agent_id: str) -> str:
    """Build the auth_elements.label for OAuth tokens scoped to a single agent.

    Tokens are stored per (user_id, service, agent_id). Each agent must start
    fresh — a user signed in via Agent A is NOT signed in via Agent B until
    they re-authorize through Agent B's connect flow.
    """
    if not agent_id:
        raise ValueError("OAuth tokens are per-agent; agent_id is required")
    return f"oauth:{agent_id}"


async def _get_creds(
    provider: str,
    agent_id: str = "",
    source: str = "platform",
) -> Tuple[str, str]:
    """Look up the OAuth (client_id, client_secret) to use for this provider.

    - `source="platform"` (default): return the app-admin's platform creds.
    - `source="byo"`: return the agent admin's BYO creds for this agent, if
      configured. Falls back to ("","") so callers can show a setup error
      instead of silently using platform creds.
    """
    if source == "byo" and agent_id:
        try:
            from app.db import get_db
            db = get_db()
            if hasattr(db, "get_agent_byo_creds"):
                cid, csec = await db.get_agent_byo_creds(agent_id, provider)
                if cid and csec:
                    return (cid, csec)
        except Exception as e:
            logger.warning("BYO cred lookup failed for %s/%s: %s", provider, agent_id, e)
        return ("", "")

    fn_name = _PROVIDER_CRED_FNS.get(provider)
    if not fn_name:
        return ("", "")
    try:
        import importlib
        mod = importlib.import_module("app.admin.integrations")
        fn = getattr(mod, fn_name, None)
        if fn:
            return await fn()
    except Exception as e:
        logger.warning("could not load creds for %s: %s", provider, e)
    return ("", "")


def _is_expired(connected_at: str, expires_in: Any, skew_seconds: int = 60) -> bool:
    """Return True if the stored token is past (expiry - skew)."""
    try:
        if not connected_at or not expires_in:
            return False  # treat unknown as fresh; let API call fail-then-refresh
        ttl = int(expires_in)
        ts = datetime.fromisoformat(connected_at.replace("Z", "+00:00"))
        return datetime.now(timezone.utc) >= ts + timedelta(seconds=ttl - skew_seconds)
    except Exception:
        return False


async def _refresh_token(
    user_id: str,
    provider: str,
    refresh_token: str,
    *,
    agent_id: str = "",
    source: str = "platform",
) -> Optional[dict]:
    """Hit the provider's refresh endpoint. Returns a fresh secret dict or None."""
    if provider not in _REFRESH_ENDPOINTS:
        return None
    url, use_basic, send_scope = _REFRESH_ENDPOINTS[provider]
    client_id, client_secret = await _get_creds(provider, agent_id=agent_id, source=source)
    if not client_id or not client_secret:
        logger.warning("refresh skipped — admin creds missing for %s", provider)
        return None

    data = {"grant_type": "refresh_token", "refresh_token": refresh_token}
    headers = {}
    if use_basic:
        import base64 as _b64
        creds = _b64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        headers["Authorization"] = f"Basic {creds}"
        if provider == "reddit":
            headers["User-Agent"] = "WebAgent/1.0"
    else:
        data["client_id"] = client_id
        data["client_secret"] = client_secret
    if send_scope:
        # Microsoft requires the original scopes echoed back during refresh
        pass  # left blank; MS accepts refresh without scope when offline_access was granted

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(url, data=data, headers=headers)
        if resp.status_code != 200:
            logger.warning("refresh %s failed %s: %s", provider, resp.status_code, resp.text[:200])
            return None
        payload = resp.json()
        return {
            "access_token":  payload.get("access_token", ""),
            "refresh_token": payload.get("refresh_token", refresh_token),  # some providers omit on refresh
            "expires_in":    payload.get("expires_in", 3600),
            "token_type":    payload.get("token_type", "Bearer"),
        }
    except Exception as e:
        logger.warning("refresh %s exception: %s", provider, e)
        return None


async def get_oauth_token(user_id: str, agent_id: str, provider: str, allow_refresh: bool = True) -> Optional[dict]:
    """Return current token dict for (user_id, agent_id, provider). Refreshes if needed.

    Result shape: {"access_token": str, "provider": str, "account": str,
                   "scopes": list[str], "covered_abilities": list[str]}
                  or None if no connection exists / refresh impossible.

    `covered_abilities` is computed from `config.scopes`. Tokens predating the
    ability system (no `scopes` recorded) are treated as covering every
    ability for that provider — legacy behaviour, no forced re-auth.
    """
    provider = normalize_provider(provider)
    label = oauth_label(agent_id)
    from app.db import get_db
    db = get_db()
    elem = await db.auth_element_get(user_id, provider, label)
    if not elem or not elem.get("secret_ref"):
        return None

    try:
        secret = json.loads(elem["secret_ref"])
    except Exception:
        return None

    config = elem.get("config") or {}
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except Exception:
            config = {}

    access_token = secret.get("access_token", "")
    refresh_token = secret.get("refresh_token", "")
    expires_in = secret.get("expires_in")
    connected_at = config.get("connected_at", "")

    source = (config.get("source") or "platform")
    needs_refresh = bool(allow_refresh and refresh_token and _is_expired(connected_at, expires_in))
    if needs_refresh:
        fresh = await _refresh_token(
            user_id, provider, refresh_token,
            agent_id=agent_id, source=source,
        )
        if fresh and fresh.get("access_token"):
            access_token = fresh["access_token"]
            # Persist the refreshed token + new connected_at so next refresh check is correct.
            new_config = {**config, "connected_at": datetime.now(timezone.utc).isoformat()}
            await db.auth_element_set(
                user_id=user_id,
                service=provider,
                config=new_config,
                secret_ref=json.dumps(fresh),
                label=label,
            )
            config = new_config

    if not access_token:
        return None

    scopes = config.get("scopes") or []
    if isinstance(scopes, str):
        scopes = scopes.split()
    covered = _token_covered_abilities(provider, scopes)
    return {
        "access_token": access_token,
        "provider": provider,
        "account": config.get("email") or config.get("name") or "",
        "scopes": list(scopes),
        "covered_abilities": covered,
    }


def _token_covered_abilities(provider: str, scopes: list[str]) -> list[str]:
    """Compute the abilities a token's scope set covers.

    Legacy tokens (no scopes recorded) cover every ability for that provider
    so existing connections keep working without forced re-consent.
    """
    from app.integrations.ability_registry import abilities_for_provider, cover_abilities
    if not scopes:
        return abilities_for_provider(provider)
    return cover_abilities(provider, scopes)


async def check_ability_authorized(
    user_id: str, agent_id: str, provider: str, ability_id: str,
) -> bool:
    """True iff the user's token for this (user, agent, provider) covers `ability_id`."""
    tok = await get_oauth_token(user_id, agent_id, provider, allow_refresh=False)
    if not tok:
        return False
    return ability_id in (tok.get("covered_abilities") or [])


def not_connected_payload(provider: str, ability: Optional[str] = None) -> str:
    """Standard JSON the agent sees when no token exists / required ability missing.

    When `ability` is provided, the message tells the agent to prompt the user
    to enable that specific capability — the connect link in the agent's
    Connections tab will surface the per-ability toggles.
    """
    provider = normalize_provider(provider)
    if ability:
        from app.integrations.ability_registry import get_ability
        ab = get_ability(ability)
        label = ab.display_name if ab else ability
        return json.dumps({
            "status": "not_connected",
            "provider": provider,
            "ability": ability,
            "reauth_required": True,
            "message": (
                f"This action requires the '{label}' ability on {provider}. "
                f"Call check_oauth_connection('{provider}') to get a connect link, "
                f"then ask the user to enable '{ability}' and re-authorize."
            ),
        })
    return json.dumps({
        "status": "not_connected",
        "provider": provider,
        "message": (
            f"No connected {provider} account for this user. "
            f"Call check_oauth_connection('{provider}') to get a connect link, "
            f"then ask the user to click it."
        ),
    })


async def oauth_api_call(
    user_id: str,
    agent_id: str,
    provider: str,
    method: str,
    url: str,
    *,
    params: Optional[dict] = None,
    json_body: Optional[Any] = None,
    data: Optional[Any] = None,
    headers: Optional[dict] = None,
    timeout: float = 30.0,
    retry_on_401: bool = True,
    ability: Optional[str] = None,
) -> dict:
    """Generic OAuth-authenticated HTTP call scoped to (user, agent).

    Returns a dict: {"status": "ok"|"error"|"not_connected", "http_status": int,
                     "body": parsed_json_or_text, "url": url}
    Bearer token is injected automatically. On 401 a single token refresh +
    retry is attempted.

    When `ability` is supplied, the token must cover that ability — otherwise
    the call short-circuits with a "not_connected" payload so the agent can
    surface a per-ability re-authorize prompt without burning a network call.
    """
    tok = await get_oauth_token(user_id, agent_id, provider)
    if not tok:
        return {"status": "not_connected", "provider": normalize_provider(provider), "ability": ability}
    if ability and ability not in (tok.get("covered_abilities") or []):
        return {
            "status": "not_connected",
            "provider": normalize_provider(provider),
            "ability": ability,
            "reauth_required": True,
        }

    hdrs = dict(headers or {})
    hdrs.setdefault("Authorization", f"Bearer {tok['access_token']}")
    hdrs.setdefault("Accept", "application/json")
    if json_body is not None and "Content-Type" not in hdrs:
        hdrs["Content-Type"] = "application/json"

    method = method.upper()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.request(
                method, url,
                params=params,
                json=json_body if json_body is not None else None,
                data=data if data is not None else None,
                headers=hdrs,
            )
    except Exception as e:
        return {"status": "error", "http_status": 0, "body": str(e), "url": url}

    if resp.status_code == 401 and retry_on_401:
        # Force a refresh + one retry.
        tok2 = await get_oauth_token(user_id, agent_id, provider, allow_refresh=True)
        if tok2 and tok2["access_token"] != tok["access_token"]:
            hdrs["Authorization"] = f"Bearer {tok2['access_token']}"
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    resp = await client.request(
                        method, url,
                        params=params,
                        json=json_body if json_body is not None else None,
                        data=data if data is not None else None,
                        headers=hdrs,
                    )
            except Exception as e:
                return {"status": "error", "http_status": 0, "body": str(e), "url": url}

    # Parse body — JSON when possible, raw text otherwise.
    body: Any
    ctype = resp.headers.get("content-type", "")
    if "json" in ctype.lower():
        try:
            body = resp.json()
        except Exception:
            body = resp.text
    else:
        body = resp.text

    status = "ok" if 200 <= resp.status_code < 300 else "error"
    return {"status": status, "http_status": resp.status_code, "body": body, "url": url}
