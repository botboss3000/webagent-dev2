"""Public OAuth callback endpoints — handles redirects from OAuth providers.

Supported:
  Productivity:  google, microsoft, yahoo, dropbox
  Social:        meta (fb+ig), twitter, linkedin, tiktok, pinterest, reddit,
                 snapchat, twitch
  Marketplaces:  ebay, etsy, shopify, amazon

Every callback shares the same eight steps (error check → decode state →
confirm agent context → resolve creds → exchange code for a token → fetch
profile → store → success page). That scaffolding lives once in
`_run_callback`; each provider supplies only its `exchange` + `profile`
closures (and any spec overrides). Shopify keeps a bespoke handler because of
its per-shop host + non-expiring token.
"""

import base64
import json
import logging
import os
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, HTTPException, Query as QueryParam, Request
from fastapi.responses import HTMLResponse

from app.integrations.oauth_helper import oauth_label

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/oauth", tags=["oauth"])


def _success_html(provider_name: str, message_type: str) -> str:
    return f"""<!DOCTYPE html>
<html><head><title>Connected</title>
<style>
  body {{ background: #1a1b26; color: #c0caf5; font-family: system-ui; display: flex;
         align-items: center; justify-content: center; height: 100vh; margin: 0; }}
  .card {{ text-align: center; padding: 40px; }}
  .icon {{ font-size: 48px; margin-bottom: 16px; }}
  h2 {{ margin: 0 0 8px; }}
  p {{ color: #7aa2f7; margin: 0; }}
</style></head>
<body><div class="card">
  <div class="icon">&#10003;</div>
  <h2>{provider_name} Connected</h2>
  <p>You can close this window.</p>
</div>
<script>
  if (window.opener) {{
    window.opener.postMessage({{ type: '{message_type}' }}, '*');
  }}
  setTimeout(() => window.close(), 2000);
</script></body></html>"""


_ERROR_HTML = """<!DOCTYPE html>
<html><head><title>Error</title>
<style>
  body { background: #1a1b26; color: #c0caf5; font-family: system-ui; display: flex;
         align-items: center; justify-content: center; height: 100vh; margin: 0; }
  .card { text-align: center; padding: 40px; }
  .icon { font-size: 48px; margin-bottom: 16px; }
  h2 { margin: 0 0 8px; color: #f7768e; }
  p { color: #a9b1d6; margin: 0; }
</style></head>
<body><div class="card">
  <div class="icon">&#10007;</div>
  <h2>Connection Failed</h2>
  <p>%s</p>
</div>
<script>setTimeout(() => window.close(), 5000);</script></body></html>"""


# ── Shared store / state helpers ──────────────────────────────────────────

async def _store_oauth(db, user_id: str, service: str, config: dict, secret: dict,
                       agent_id: str, connection_type: str, section: str = "social",
                       *, source: str = "platform", token_data: dict | None = None):
    """Store tokens + upsert agent connection — both scoped to the calling agent.

    When `token_data` is supplied, the granted scopes (from `token_data["scope"]`)
    and the OAuth source ("platform" vs "byo") are persisted into config so
    refresh + ability gating can resolve the right creds and capabilities later.
    """
    from app.entitlements.resources import (
        ResourceEntitlementError, connection_resource_lock,
        enforce_connection_change,
    )
    try:
        await enforce_connection_change(
            db, user_id, agent_id, connection_type, enabling=True,
        )
    except ResourceEntitlementError as exc:
        raise HTTPException(status_code=403, detail=exc.detail()) from exc

    cfg = {**config}
    cfg["source"] = source
    if token_data and "scope" in token_data and "scopes" not in cfg:
        cfg["scopes"] = (token_data.get("scope", "") or "").split()
    await db.auth_element_set(
        user_id=user_id, service=service, config=cfg,
        secret_ref=json.dumps(secret), label=oauth_label(agent_id),
    )
    async with connection_resource_lock(user_id):
        try:
            await enforce_connection_change(
                db, user_id, agent_id, connection_type, enabling=True,
            )
        except ResourceEntitlementError as exc:
            raise HTTPException(status_code=403, detail=exc.detail()) from exc
        await db.upsert_agent_connection(
            agent_id=agent_id, connection_type=connection_type, section=section,
            enabled=True,
            config={"connected_user_id": user_id, "email": cfg.get("email", ""), "name": cfg.get("name", "")},
        )


def _token_secret(token_data: dict) -> dict:
    return {
        "access_token":  token_data.get("access_token", ""),
        "refresh_token": token_data.get("refresh_token", ""),
        "expires_in":    token_data.get("expires_in", 3600),
        "token_type":    token_data.get("token_type", "Bearer"),
    }


def _extract_state(state_data: dict) -> tuple[str, str, str]:
    """Pull (user_id, agent_id, source) out of a decoded state token."""
    return (
        state_data.get("user_id", ""),
        state_data.get("agent_id", "") or "",
        state_data.get("source") or "platform",
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── The shared callback driver ────────────────────────────────────────────

async def _run_callback(
    request: Request,
    *,
    provider: str,
    name: str,
    message_type: str,
    code: str,
    state: str,
    error: str,
    error_description: str | None = None,
    exchange,
    profile,
    section: str = "social",
    success_name: str | None = None,
    missing_msg: str = "Missing code or state parameter.",
    not_configured_msg: str | None = None,
    token_parse=None,
    secret_builder=None,
    aliases: list[str] | None = None,
    extra_connections: list[str] | None = None,
) -> HTMLResponse:
    """Run the common OAuth-callback flow, parameterized per provider.

    `exchange(client, *, code, redirect_uri, client_id, client_secret, state_data)`
    must return the token-endpoint httpx.Response. `profile(client, *,
    access_token, client_id, token_data, state_data)` returns the config dict to
    store (it may set the control key `_connection_type` to override the agent
    connection's type, as Meta does → "facebook").
    """
    if error:
        return HTMLResponse(_ERROR_HTML % f"{name} returned: {error_description or error}", status_code=400)
    if not code or not state:
        return HTMLResponse(_ERROR_HTML % missing_msg, status_code=400)

    from app.admin.integrations import decode_state_token, resolve_oauth_creds, provider_redirect_uri
    state_data = decode_state_token(state, provider=provider)
    if not state_data or not state_data.get("user_id"):
        return HTMLResponse(_ERROR_HTML % "Invalid or expired state token.", status_code=400)

    user_id, agent_id, source = _extract_state(state_data)
    if not agent_id:
        return HTMLResponse(_ERROR_HTML % "Missing agent context — re-launch sign-in from an agent's Connections tab.", status_code=400)

    client_id, client_secret = await resolve_oauth_creds(provider, agent_id, source=source)
    if not client_id or not client_secret:
        return HTMLResponse(_ERROR_HTML % (not_configured_msg or f"{name} OAuth not configured on server."), status_code=500)

    redirect_uri = await provider_redirect_uri(provider, request, agent_id=agent_id, source=source)

    async with httpx.AsyncClient() as client:
        token_resp = await exchange(
            client, code=code, redirect_uri=redirect_uri,
            client_id=client_id, client_secret=client_secret, state_data=state_data,
        )
    if token_resp.status_code != 200:
        logger.error("%s token exchange failed: %s", name, token_resp.text)
        return HTMLResponse(_ERROR_HTML % "Token exchange failed.", status_code=400)

    token_data = (token_parse or (lambda r: r.json()))(token_resp)
    access_token = token_data.get("access_token", "")

    async with httpx.AsyncClient() as client:
        config = await profile(
            client, access_token=access_token, client_id=client_id,
            token_data=token_data, state_data=state_data,
        )

    secret = (secret_builder or _token_secret)(token_data)
    connection_type = config.pop("_connection_type", provider)

    from app.db import get_db
    db = get_db()
    await _store_oauth(db, user_id, provider, config, secret, agent_id, connection_type,
                       section=section, source=source, token_data=token_data)

    # Per-provider aliases (Meta stores the same token under facebook + instagram).
    for alias in (aliases or []):
        cfg_aliased = {**config, "scopes": (token_data.get("scope", "") or "").split(), "source": source}
        await db.auth_element_set(user_id=user_id, service=alias, config=cfg_aliased,
                                  secret_ref=json.dumps(secret), label=oauth_label(agent_id))
    for ct in (extra_connections or []):
        try:
            await _store_oauth(
                db, user_id, provider, config, secret, agent_id, ct,
                section=section, source=source, token_data=token_data,
            )
        except HTTPException:
            raise
        except Exception:
            pass

    logger.info("%s OAuth connected for user %s (%s), agent=%s", name, user_id[:12],
                config.get("email") or config.get("name") or config.get("username") or "?", agent_id or "none")
    return HTMLResponse(_success_html(success_name or name, message_type))


# ── Google ────────────────────────────────────────────────────────────────

@router.get("/callback/google")
async def google_callback(
    request: Request,
    code: str = QueryParam(None),
    state: str = QueryParam(None),
    error: str = QueryParam(None),
):
    async def exchange(client, *, code, redirect_uri, client_id, client_secret, state_data):
        return await client.post(
            "https://oauth2.googleapis.com/token",
            data={"code": code, "client_id": client_id, "client_secret": client_secret,
                  "redirect_uri": redirect_uri, "grant_type": "authorization_code"},
        )

    async def profile(client, *, access_token, client_id, token_data, state_data):
        r = await client.get("https://www.googleapis.com/oauth2/v2/userinfo",
                             headers={"Authorization": f"Bearer {access_token}"})
        u = r.json() if r.status_code == 200 else {}
        return {"email": u.get("email", ""), "name": u.get("name", ""), "picture": u.get("picture", ""),
                "scopes": token_data.get("scope", "").split(), "connected_at": _now()}

    return await _run_callback(
        request, provider="google", name="Google", message_type="google-oauth-success",
        section="integration", code=code, state=state, error=error,
        exchange=exchange, profile=profile,
    )


# ── Microsoft ─────────────────────────────────────────────────────────────

@router.get("/callback/microsoft")
async def microsoft_callback(
    request: Request,
    code: str = QueryParam(None),
    state: str = QueryParam(None),
    error: str = QueryParam(None),
    error_description: str = QueryParam(None),
):
    async def exchange(client, *, code, redirect_uri, client_id, client_secret, state_data):
        return await client.post(
            "https://login.microsoftonline.com/common/oauth2/v2.0/token",
            data={"code": code, "client_id": client_id, "client_secret": client_secret,
                  "redirect_uri": redirect_uri, "grant_type": "authorization_code"},
        )

    async def profile(client, *, access_token, client_id, token_data, state_data):
        r = await client.get("https://graph.microsoft.com/v1.0/me",
                             headers={"Authorization": f"Bearer {access_token}"})
        u = r.json() if r.status_code == 200 else {}
        return {"email": u.get("mail") or u.get("userPrincipalName", ""),
                "name": u.get("displayName", ""), "picture": "",
                "scopes": token_data.get("scope", "").split(), "connected_at": _now()}

    return await _run_callback(
        request, provider="microsoft", name="Microsoft", message_type="microsoft-oauth-success",
        section="integration", code=code, state=state, error=error, error_description=error_description,
        exchange=exchange, profile=profile,
    )


# ── Yahoo ─────────────────────────────────────────────────────────────────

@router.get("/callback/yahoo")
async def yahoo_callback(
    request: Request,
    code: str = QueryParam(None),
    state: str = QueryParam(None),
    error: str = QueryParam(None),
):
    async def exchange(client, *, code, redirect_uri, client_id, client_secret, state_data):
        credentials = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        return await client.post(
            "https://api.login.yahoo.com/oauth2/get_token",
            headers={"Authorization": f"Basic {credentials}",
                     "Content-Type": "application/x-www-form-urlencoded"},
            data={"code": code, "redirect_uri": redirect_uri, "grant_type": "authorization_code"},
        )

    async def profile(client, *, access_token, client_id, token_data, state_data):
        r = await client.get("https://api.login.yahoo.com/openid/v1/userinfo",
                             headers={"Authorization": f"Bearer {access_token}"})
        u = r.json() if r.status_code == 200 else {}
        return {"email": u.get("email", ""), "name": u.get("name", u.get("nickname", "")),
                "picture": u.get("picture", ""), "yahoo_guid": token_data.get("xoauth_yahoo_guid", ""),
                "scopes": (token_data.get("scope", "") or "").split(), "connected_at": _now()}

    return await _run_callback(
        request, provider="yahoo", name="Yahoo", message_type="yahoo-oauth-success",
        section="integration", code=code, state=state, error=error,
        exchange=exchange, profile=profile,
    )


# ── Dropbox ───────────────────────────────────────────────────────────────

@router.get("/callback/dropbox")
async def dropbox_callback(
    request: Request,
    code: str = QueryParam(None),
    state: str = QueryParam(None),
    error: str = QueryParam(None),
    error_description: str = QueryParam(None),
):
    async def exchange(client, *, code, redirect_uri, client_id, client_secret, state_data):
        return await client.post(
            "https://api.dropboxapi.com/oauth2/token",
            data={"code": code, "client_id": client_id, "client_secret": client_secret,
                  "redirect_uri": redirect_uri, "grant_type": "authorization_code"},
        )

    async def profile(client, *, access_token, client_id, token_data, state_data):
        r = await client.post("https://api.dropboxapi.com/2/users/get_current_account",
                             headers={"Authorization": f"Bearer {access_token}",
                                      "Content-Type": "application/json"}, content=b"null")
        acct = r.json() if r.status_code == 200 else {}
        return {"email": acct.get("email", ""),
                "name": acct.get("name", {}).get("display_name", ""),
                "picture": acct.get("profile_photo_url", ""),
                "account_id": acct.get("account_id", ""),
                "scopes": (token_data.get("scope", "") or "").split(), "connected_at": _now()}

    # Dropbox short-lived tokens default to a 4-hour expiry.
    def _dbx_secret(token_data):
        return {"access_token": token_data.get("access_token", ""),
                "refresh_token": token_data.get("refresh_token", ""),
                "expires_in": token_data.get("expires_in", 14400),
                "token_type": token_data.get("token_type", "Bearer")}

    return await _run_callback(
        request, provider="dropbox", name="Dropbox", message_type="dropbox-oauth-success",
        section="integration", code=code, state=state, error=error, error_description=error_description,
        exchange=exchange, profile=profile, secret_builder=_dbx_secret,
    )


# ── Meta (Facebook + Instagram) ───────────────────────────────────────────

@router.get("/callback/meta")
async def meta_callback(
    request: Request,
    code: str = QueryParam(None),
    state: str = QueryParam(None),
    error: str = QueryParam(None),
    error_description: str = QueryParam(None),
):
    async def exchange(client, *, code, redirect_uri, client_id, client_secret, state_data):
        return await client.get(
            "https://graph.facebook.com/v19.0/oauth/access_token",
            params={"client_id": client_id, "client_secret": client_secret,
                    "redirect_uri": redirect_uri, "code": code},
        )

    async def profile(client, *, access_token, client_id, token_data, state_data):
        r = await client.get("https://graph.facebook.com/v19.0/me",
                             params={"fields": "id,name,email,picture.type(large)", "access_token": access_token})
        me = r.json() if r.status_code == 200 else {}
        return {
            "email": me.get("email", ""), "name": me.get("name", ""),
            "picture": me.get("picture", {}).get("data", {}).get("url", "") if isinstance(me.get("picture"), dict) else "",
            "provider_user_id": me.get("id", ""), "connected_at": _now(),
            "_connection_type": "facebook",  # primary agent connection stored as facebook
        }

    return await _run_callback(
        request, provider="meta", name="Meta", success_name="Meta (Facebook & Instagram)",
        message_type="meta-oauth-success", section="social",
        code=code, state=state, error=error, error_description=error_description,
        exchange=exchange, profile=profile,
        aliases=["facebook", "instagram"], extra_connections=["instagram"],
    )


# ── Twitter / X ───────────────────────────────────────────────────────────

@router.get("/callback/twitter")
async def twitter_callback(
    request: Request,
    code: str = QueryParam(None),
    state: str = QueryParam(None),
    error: str = QueryParam(None),
    error_description: str = QueryParam(None),
):
    async def exchange(client, *, code, redirect_uri, client_id, client_secret, state_data):
        creds_b64 = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        return await client.post(
            "https://api.twitter.com/2/oauth2/token",
            headers={"Authorization": f"Basic {creds_b64}", "Content-Type": "application/x-www-form-urlencoded"},
            data={"code": code, "grant_type": "authorization_code", "redirect_uri": redirect_uri,
                  "code_verifier": state_data.get("pkce_verifier", "")},
        )

    async def profile(client, *, access_token, client_id, token_data, state_data):
        r = await client.get("https://api.twitter.com/2/users/me",
                             headers={"Authorization": f"Bearer {access_token}"},
                             params={"user.fields": "name,username,profile_image_url,description"})
        me = r.json().get("data", {}) if r.status_code == 200 else {}
        return {"name": me.get("name", ""), "username": me.get("username", ""),
                "picture": me.get("profile_image_url", ""), "email": "", "connected_at": _now()}

    return await _run_callback(
        request, provider="twitter", name="Twitter", success_name="Twitter / X",
        message_type="twitter-oauth-success", section="social",
        code=code, state=state, error=error, error_description=error_description,
        exchange=exchange, profile=profile,
    )


# ── LinkedIn ──────────────────────────────────────────────────────────────

@router.get("/callback/linkedin")
async def linkedin_callback(
    request: Request,
    code: str = QueryParam(None),
    state: str = QueryParam(None),
    error: str = QueryParam(None),
    error_description: str = QueryParam(None),
):
    async def exchange(client, *, code, redirect_uri, client_id, client_secret, state_data):
        return await client.post(
            "https://www.linkedin.com/oauth/v2/accessToken",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={"code": code, "client_id": client_id, "client_secret": client_secret,
                  "redirect_uri": redirect_uri, "grant_type": "authorization_code"},
        )

    async def profile(client, *, access_token, client_id, token_data, state_data):
        r = await client.get("https://api.linkedin.com/v2/userinfo",
                             headers={"Authorization": f"Bearer {access_token}"})
        me = r.json() if r.status_code == 200 else {}
        return {"email": me.get("email", ""), "name": me.get("name", ""),
                "picture": me.get("picture", ""), "connected_at": _now()}

    return await _run_callback(
        request, provider="linkedin", name="LinkedIn", message_type="linkedin-oauth-success",
        section="social", code=code, state=state, error=error, error_description=error_description,
        exchange=exchange, profile=profile,
    )


# ── TikTok ────────────────────────────────────────────────────────────────

@router.get("/callback/tiktok")
async def tiktok_callback(
    request: Request,
    code: str = QueryParam(None),
    state: str = QueryParam(None),
    error: str = QueryParam(None),
    error_description: str = QueryParam(None),
):
    async def exchange(client, *, code, redirect_uri, client_id, client_secret, state_data):
        return await client.post(
            "https://open.tiktokapis.com/v2/oauth/token/",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={"client_key": client_id, "client_secret": client_secret, "code": code,
                  "grant_type": "authorization_code", "redirect_uri": redirect_uri,
                  "code_verifier": state_data.get("pkce_verifier", "")},
        )

    async def profile(client, *, access_token, client_id, token_data, state_data):
        r = await client.get("https://open.tiktokapis.com/v2/user/info/",
                             headers={"Authorization": f"Bearer {access_token}"},
                             params={"fields": "open_id,display_name,avatar_url"})
        me = r.json().get("data", {}).get("user", {}) if r.status_code == 200 else {}
        return {"name": me.get("display_name", ""), "picture": me.get("avatar_url", ""),
                "email": "", "connected_at": _now()}

    return await _run_callback(
        request, provider="tiktok", name="TikTok", message_type="tiktok-oauth-success",
        section="social", code=code, state=state, error=error, error_description=error_description,
        exchange=exchange, profile=profile,
        token_parse=lambda r: r.json().get("data", r.json()),
    )


# ── Pinterest ─────────────────────────────────────────────────────────────

@router.get("/callback/pinterest")
async def pinterest_callback(
    request: Request,
    code: str = QueryParam(None),
    state: str = QueryParam(None),
    error: str = QueryParam(None),
):
    async def exchange(client, *, code, redirect_uri, client_id, client_secret, state_data):
        creds_b64 = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        return await client.post(
            "https://api.pinterest.com/v5/oauth/token",
            headers={"Authorization": f"Basic {creds_b64}", "Content-Type": "application/x-www-form-urlencoded"},
            data={"code": code, "redirect_uri": redirect_uri, "grant_type": "authorization_code"},
        )

    async def profile(client, *, access_token, client_id, token_data, state_data):
        r = await client.get("https://api.pinterest.com/v5/user_account",
                             headers={"Authorization": f"Bearer {access_token}"})
        me = r.json() if r.status_code == 200 else {}
        return {"name": me.get("username", ""), "picture": me.get("profile_image", ""),
                "email": "", "connected_at": _now()}

    return await _run_callback(
        request, provider="pinterest", name="Pinterest", message_type="pinterest-oauth-success",
        section="social", code=code, state=state, error=error,
        exchange=exchange, profile=profile,
    )


# ── Reddit ────────────────────────────────────────────────────────────────

@router.get("/callback/reddit")
async def reddit_callback(
    request: Request,
    code: str = QueryParam(None),
    state: str = QueryParam(None),
    error: str = QueryParam(None),
):
    async def exchange(client, *, code, redirect_uri, client_id, client_secret, state_data):
        creds_b64 = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        return await client.post(
            "https://www.reddit.com/api/v1/access_token",
            headers={"Authorization": f"Basic {creds_b64}", "Content-Type": "application/x-www-form-urlencoded",
                     "User-Agent": "WebAgent/1.0"},
            data={"code": code, "redirect_uri": redirect_uri, "grant_type": "authorization_code"},
        )

    async def profile(client, *, access_token, client_id, token_data, state_data):
        r = await client.get("https://oauth.reddit.com/api/v1/me",
                             headers={"Authorization": f"Bearer {access_token}", "User-Agent": "WebAgent/1.0"})
        me = r.json() if r.status_code == 200 else {}
        return {"name": me.get("name", ""), "picture": me.get("icon_img", "").split("?")[0],
                "email": "", "connected_at": _now()}

    return await _run_callback(
        request, provider="reddit", name="Reddit", message_type="reddit-oauth-success",
        section="social", code=code, state=state, error=error,
        exchange=exchange, profile=profile,
    )


# ── Snapchat ──────────────────────────────────────────────────────────────

@router.get("/callback/snapchat")
async def snapchat_callback(
    request: Request,
    code: str = QueryParam(None),
    state: str = QueryParam(None),
    error: str = QueryParam(None),
):
    async def exchange(client, *, code, redirect_uri, client_id, client_secret, state_data):
        creds_b64 = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        return await client.post(
            "https://accounts.snapchat.com/login/oauth2/access_token",
            headers={"Authorization": f"Basic {creds_b64}", "Content-Type": "application/x-www-form-urlencoded"},
            data={"code": code, "redirect_uri": redirect_uri, "grant_type": "authorization_code"},
        )

    async def profile(client, *, access_token, client_id, token_data, state_data):
        r = await client.get("https://kit.snapchat.com/v1/me",
                             headers={"Authorization": f"Bearer {access_token}"},
                             params={"query": "{me{displayName bitmoji{avatar}}}"})
        me = r.json().get("data", {}).get("me", {}) if r.status_code == 200 else {}
        return {"name": me.get("displayName", ""), "picture": me.get("bitmoji", {}).get("avatar", ""),
                "email": "", "connected_at": _now()}

    return await _run_callback(
        request, provider="snapchat", name="Snapchat", message_type="snapchat-oauth-success",
        section="social", code=code, state=state, error=error,
        exchange=exchange, profile=profile,
    )


# ── Twitch ────────────────────────────────────────────────────────────────

@router.get("/callback/twitch")
async def twitch_callback(
    request: Request,
    code: str = QueryParam(None),
    state: str = QueryParam(None),
    error: str = QueryParam(None),
    error_description: str = QueryParam(None),
):
    async def exchange(client, *, code, redirect_uri, client_id, client_secret, state_data):
        return await client.post(
            "https://id.twitch.tv/oauth2/token",
            params={"client_id": client_id, "client_secret": client_secret, "code": code,
                    "grant_type": "authorization_code", "redirect_uri": redirect_uri},
        )

    async def profile(client, *, access_token, client_id, token_data, state_data):
        r = await client.get("https://api.twitch.tv/helix/users",
                             headers={"Authorization": f"Bearer {access_token}", "Client-Id": client_id})
        me = (r.json().get("data") or [{}])[0] if r.status_code == 200 else {}
        return {"name": me.get("display_name", ""), "picture": me.get("profile_image_url", ""),
                "email": me.get("email", ""), "connected_at": _now()}

    return await _run_callback(
        request, provider="twitch", name="Twitch", message_type="twitch-oauth-success",
        section="social", code=code, state=state, error=error, error_description=error_description,
        exchange=exchange, profile=profile,
    )


# ── eBay ──────────────────────────────────────────────────────────────────

@router.get("/callback/ebay")
async def ebay_callback(
    request: Request,
    code: str = QueryParam(None),
    state: str = QueryParam(None),
    error: str = QueryParam(None),
    error_description: str = QueryParam(None),
):
    async def exchange(client, *, code, redirect_uri, client_id, client_secret, state_data):
        creds_b64 = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        return await client.post(
            "https://api.ebay.com/identity/v1/oauth2/token",
            headers={"Authorization": f"Basic {creds_b64}", "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri},
        )

    async def profile(client, *, access_token, client_id, token_data, state_data):
        # Fetch the seller's username so the UI can show who's connected.
        name = ""
        try:
            r = await client.get("https://apiz.ebay.com/commerce/identity/v1/user/",
                                 headers={"Authorization": f"Bearer {access_token}"})
            if r.status_code == 200:
                me = r.json()
                name = me.get("username", "") or me.get("userId", "")
        except Exception:
            pass
        return {"name": name, "email": "", "picture": "", "connected_at": _now()}

    return await _run_callback(
        request, provider="ebay", name="eBay", message_type="ebay-oauth-success",
        section="marketplace", code=code, state=state, error=error, error_description=error_description,
        exchange=exchange, profile=profile,
    )


# ── Etsy ──────────────────────────────────────────────────────────────────

@router.get("/callback/etsy")
async def etsy_callback(
    request: Request,
    code: str = QueryParam(None),
    state: str = QueryParam(None),
    error: str = QueryParam(None),
):
    async def exchange(client, *, code, redirect_uri, client_id, client_secret, state_data):
        # Etsy is PKCE — the client_id goes in the body and there is no secret.
        return await client.post(
            "https://api.etsy.com/v3/public/oauth/token",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "authorization_code", "client_id": client_id,
                  "redirect_uri": redirect_uri, "code": code,
                  "code_verifier": state_data.get("pkce_verifier", "")},
        )

    async def profile(client, *, access_token, client_id, token_data, state_data):
        # Etsy access_tokens are formatted "<user_id>.<token>".
        etsy_user_id = access_token.split(".", 1)[0] if "." in access_token else ""
        name = email = picture = shop_id = ""
        try:
            r = await client.get(f"https://openapi.etsy.com/v3/application/users/{etsy_user_id}",
                                 headers={"Authorization": f"Bearer {access_token}", "x-api-key": client_id})
            if r.status_code == 200:
                me = r.json()
                name = (me.get("first_name", "") + " " + me.get("last_name", "")).strip() or me.get("login_name", "")
                email = me.get("primary_email", "")
                picture = me.get("image_url_75x75", "")
            shop_resp = await client.get(f"https://openapi.etsy.com/v3/application/users/{etsy_user_id}/shops",
                                         headers={"Authorization": f"Bearer {access_token}", "x-api-key": client_id})
            if shop_resp.status_code == 200:
                sd = shop_resp.json() or {}
                shop_id = str(sd.get("shop_id", "")) if "shop_id" in sd else ""
        except Exception as e:
            logger.warning("Etsy profile/shop lookup failed: %s", e)
        return {"name": name, "email": email, "picture": picture,
                "etsy_user_id": etsy_user_id, "shop_id": shop_id, "connected_at": _now()}

    return await _run_callback(
        request, provider="etsy", name="Etsy", message_type="etsy-oauth-success",
        section="marketplace", code=code, state=state, error=error,
        exchange=exchange, profile=profile,
    )


# ── Shopify (per-shop) ────────────────────────────────────────────────────
#
# Bespoke handler (the task's documented exception): the token endpoint lives
# on the per-shop host, the access token never expires, and the shop domain
# from the redirect must match the one captured in state.

@router.get("/callback/shopify")
async def shopify_callback(
    request: Request,
    code: str = QueryParam(None),
    state: str = QueryParam(None),
    shop: str = QueryParam(None),
    hmac: str = QueryParam(None),
    error: str = QueryParam(None),
):
    if error:
        return HTMLResponse(_ERROR_HTML % f"Shopify returned: {error}", status_code=400)
    if not code or not state or not shop:
        return HTMLResponse(_ERROR_HTML % "Missing code, state, or shop parameter.", status_code=400)

    from app.admin.integrations import decode_state_token, resolve_oauth_creds
    state_data = decode_state_token(state, provider="shopify")
    if not state_data or not state_data.get("user_id"):
        return HTMLResponse(_ERROR_HTML % "Invalid or expired state token.", status_code=400)

    user_id, agent_id, source = _extract_state(state_data)
    if not agent_id:
        return HTMLResponse(_ERROR_HTML % "Missing agent context — re-launch sign-in from an agent's Connections tab.", status_code=400)
    expected_shop = (state_data.get("shop") or "").lower()
    if expected_shop and expected_shop != shop.lower():
        return HTMLResponse(_ERROR_HTML % "Shop domain mismatch.", status_code=400)

    client_id, client_secret = await resolve_oauth_creds("shopify", agent_id, source=source)
    if not client_id or not client_secret:
        return HTMLResponse(_ERROR_HTML % "Shopify OAuth not configured on server.", status_code=500)

    async with httpx.AsyncClient() as c:
        token_resp = await c.post(
            f"https://{shop}/admin/oauth/access_token",
            json={"client_id": client_id, "client_secret": client_secret, "code": code},
            headers={"Content-Type": "application/json"},
        )
    if token_resp.status_code != 200:
        logger.error("Shopify token exchange failed: %s", token_resp.text)
        return HTMLResponse(_ERROR_HTML % "Token exchange failed.", status_code=400)

    token_data = token_resp.json()
    access_token = token_data.get("access_token", "")

    # Fetch shop info for display.
    shop_name = shop
    email = ""
    try:
        async with httpx.AsyncClient() as c:
            shop_resp = await c.get(
                f"https://{shop}/admin/api/2024-10/shop.json",
                headers={"X-Shopify-Access-Token": access_token},
            )
        if shop_resp.status_code == 200:
            s = shop_resp.json().get("shop", {})
            shop_name = s.get("name", shop)
            email = s.get("email", "")
    except Exception:
        pass

    # Shopify tokens don't expire; store as-is without expires_in.
    secret = {
        "access_token": access_token,
        "refresh_token": "",
        "expires_in": 0,
        "token_type": "Bearer",
        "scope": token_data.get("scope", ""),
    }

    from app.db import get_db
    db = get_db()
    config = {
        "name": shop_name,
        "email": email,
        "picture": "",
        "shop": shop,
        "connected_at": _now(),
    }
    await _store_oauth(db, user_id, "shopify", config, secret, agent_id, "shopify", section="marketplace", source=source, token_data=token_data)
    logger.info("Shopify OAuth connected for user %s (%s), agent=%s", user_id[:12], shop, agent_id or "none")
    return HTMLResponse(_success_html("Shopify", "shopify-oauth-success"))


# ── Amazon SP-API (LWA) ───────────────────────────────────────────────────

@router.get("/callback/amazon")
async def amazon_callback(
    request: Request,
    spapi_oauth_code: str = QueryParam(None),
    state: str = QueryParam(None),
    selling_partner_id: str = QueryParam(None),
    mws_auth_token: str = QueryParam(None),
    error: str = QueryParam(None),
):
    async def exchange(client, *, code, redirect_uri, client_id, client_secret, state_data):
        return await client.post(
            "https://api.amazon.com/auth/o2/token",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "authorization_code", "code": code, "client_id": client_id,
                  "client_secret": client_secret, "redirect_uri": redirect_uri},
        )

    async def profile(client, *, access_token, client_id, token_data, state_data):
        # Amazon supplies the seller id on the redirect; there is no profile call.
        region = (state_data.get("region") or "NA").upper()
        return {"name": selling_partner_id or "", "email": "", "picture": "",
                "selling_partner_id": selling_partner_id or "", "region": region, "connected_at": _now()}

    return await _run_callback(
        request, provider="amazon", name="Amazon", message_type="amazon-oauth-success",
        section="marketplace", code=spapi_oauth_code, state=state, error=error,
        missing_msg="Missing spapi_oauth_code or state parameter.",
        not_configured_msg="Amazon LWA not configured on server.",
        exchange=exchange, profile=profile,
    )
