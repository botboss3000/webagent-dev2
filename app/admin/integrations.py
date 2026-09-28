"""Integrations admin endpoints — OAuth credential config (admin) + shared helpers.

Supported providers:
  Productivity
  - Google     (Gmail, Drive, Docs, Calendar)
  - Microsoft  (Outlook, OneDrive, SharePoint)
  - Yahoo      (Yahoo Mail)
  - Dropbox    (file storage)

  Social Media
  - Meta       (Facebook + Instagram — one app, two connections)
  - Twitter/X  (posts, DMs, profile)
  - LinkedIn   (posts, profile)
  - TikTok     (videos, profile)
  - Pinterest  (boards, pins)
  - Reddit     (posts, comments, profile)
  - Snapchat   (profile, Bitmoji)
  - Twitch     (channel, clips, subscriptions)

  Marketplaces
  - eBay       (Buy + Sell APIs)
  - Etsy       (Open API v3)
  - Shopify    (Admin API, per-shop)
  - Amazon     (Selling Partner API, LWA OAuth)
"""

import base64
import hashlib
import json
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlencode, urlparse, urlunparse

import httpx
import jwt
from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel

from app.auth.jwt import decode_token, get_secret
from app.integrations.oauth_helper import oauth_label

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/integrations", tags=["integrations"])

ANONYMOUS_KEY = "__anonymous__"
_ADMIN_USER = "admin"

# Sign OAuth state tokens with the app-wide JWT secret (no separate/hardcoded
# key) so they share the same hardened, generated secret. See app/auth/jwt.py.
_JWT_ALGORITHM = "HS256"

# ── OAuth scope definitions ───────────────────────────────────────────────

GOOGLE_SCOPES = [
    "openid",
    "email",
    "profile",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/docs",
    "https://www.googleapis.com/auth/docs.readonly",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.readonly",
]

MICROSOFT_SCOPES = [
    "openid",
    "email",
    "profile",
    "offline_access",
    "User.Read",
    "Mail.ReadWrite",
    "Mail.Send",
    "Calendars.ReadWrite",
    "Files.Read.All",
    "Sites.Read.All",
    "Files.ReadWrite.All",
    "Sites.ReadWrite.All",
]

YAHOO_SCOPES = [
    "openid",
    "email",
    "profile",
    "mail-r",
    "mail-w",
]

DROPBOX_SCOPES = [
    "account_info.read",
    "files.metadata.read",
    "files.metadata.write",
    "files.content.read",
    "files.content.write",
    "sharing.read",
    "sharing.write",
]

# ── Social media scopes ───────────────────────────────────────────────────

META_SCOPES = [
    "public_profile",
    "email",
    "pages_manage_posts",
    "pages_read_engagement",
    "instagram_basic",
    "instagram_content_publish",
    "instagram_manage_comments",
    "instagram_manage_insights",
]

TWITTER_SCOPES = [
    "tweet.read",
    "tweet.write",
    "tweet.moderate.write",
    "users.read",
    "follows.read",
    "follows.write",
    "offline.access",
    "like.read",
    "like.write",
    "dm.read",
    "dm.write",
]

LINKEDIN_SCOPES = [
    "openid",
    "profile",
    "email",
    "w_member_social",
    "r_liteprofile",
    "r_emailaddress",
]

TIKTOK_SCOPES = [
    "user.info.basic",
    "user.info.profile",
    "video.list",
    "video.upload",
    "video.publish",
]

PINTEREST_SCOPES = [
    "boards:read",
    "boards:write",
    "pins:read",
    "pins:write",
    "user_accounts:read",
]

REDDIT_SCOPES = [
    "identity",
    "read",
    "submit",
    "history",
    "mysubreddits",
    "privatemessages",
]

SNAPCHAT_SCOPES = [
    "https://auth.snapchat.com/oauth2/api/user.display_name",
    "https://auth.snapchat.com/oauth2/api/user.bitmoji.avatar",
    "https://auth.snapchat.com/oauth2/api/user.external_id",
]

TWITCH_SCOPES = [
    "user:read:email",
    "user:read:follows",
    "channel:read:subscriptions",
    "channel:manage:broadcast",
    "clips:edit",
    "chat:read",
    "chat:edit",
]

# ── Marketplace scopes ────────────────────────────────────────────────────

EBAY_SCOPES = [
    "https://api.ebay.com/oauth/api_scope",
    "https://api.ebay.com/oauth/api_scope/buy.order.readonly",
    "https://api.ebay.com/oauth/api_scope/sell.inventory",
    "https://api.ebay.com/oauth/api_scope/sell.inventory.readonly",
    "https://api.ebay.com/oauth/api_scope/sell.account",
    "https://api.ebay.com/oauth/api_scope/sell.account.readonly",
    "https://api.ebay.com/oauth/api_scope/sell.fulfillment",
    "https://api.ebay.com/oauth/api_scope/sell.fulfillment.readonly",
    "https://api.ebay.com/oauth/api_scope/sell.marketing",
    "https://api.ebay.com/oauth/api_scope/sell.marketing.readonly",
]

ETSY_SCOPES = [
    "email_r",
    "profile_r",
    "shops_r",
    "shops_w",
    "listings_r",
    "listings_w",
    "listings_d",
    "transactions_r",
]

# Shopify scopes are per-shop / per-install — these are the defaults requested
# at install time. Tweak in the admin UI to match the app's Partner config.
SHOPIFY_SCOPES = [
    "read_products",
    "write_products",
    "read_inventory",
    "write_inventory",
    "read_orders",
    "write_orders",
    "read_locations",
]

# Amazon SP-API uses a single LWA scope; the actual roles (Orders, Inventory,
# Pricing, etc.) are granted via the Seller Central app registration, not OAuth.
AMAZON_SCOPES = [
    "sellingpartnerapi::client_credential:refresh_token",
]

# ── Per-provider OAuth table — the single source of truth ──────────────────
#
# One row per provider drives all four generic helpers below (creds lookup,
# redirect URI, authorize-URL build, token revoke). Fields:
#   config_key       admin auth_elements service key holding the OAuth app creds
#   env              (client_id_env_var, client_secret_env_var) — env fallback
#   scopes           default scope list (admin can narrow it per provider)
#   auth_url         authorize endpoint (absent for shopify/amazon — bespoke build)
#   scope_sep        separator joining scopes in the URL ("," for a few; default " ")
#   client_id_param  query param carrying the client id (default "client_id")
#   extra_params     constant extra authorize-URL query params for this provider
#   pkce             True → generate a PKCE pair and stash the verifier in state
#   revoke           {"style", "url", ...} describing the revoke call, or absent
#   delete_aliases   extra auth_elements services to delete on disconnect (meta)
_PROVIDERS: dict[str, dict] = {
    "google": {
        "config_key": "google_oauth_config",
        "env": ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"),
        "scopes": GOOGLE_SCOPES,
        "auth_url": "https://accounts.google.com/o/oauth2/auth",
        # prompt=consent re-shows skipped sensitive-scope checkboxes;
        # include_granted_scopes keeps re-consent additive over a prior grant.
        "extra_params": {"access_type": "offline", "prompt": "consent", "include_granted_scopes": "true"},
        "revoke": {"style": "param_token", "url": "https://oauth2.googleapis.com/revoke"},
    },
    "microsoft": {
        "config_key": "microsoft_oauth_config",
        "env": ("MICROSOFT_CLIENT_ID", "MICROSOFT_CLIENT_SECRET"),
        "scopes": MICROSOFT_SCOPES,
        "auth_url": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "extra_params": {"response_mode": "query", "prompt": "consent"},
    },
    "yahoo": {
        "config_key": "yahoo_oauth_config",
        "env": ("YAHOO_CLIENT_ID", "YAHOO_CLIENT_SECRET"),
        "scopes": YAHOO_SCOPES,
        "auth_url": "https://api.login.yahoo.com/oauth2/request_auth",
    },
    "dropbox": {
        "config_key": "dropbox_oauth_config",
        "env": ("DROPBOX_APP_KEY", "DROPBOX_APP_SECRET"),
        "scopes": DROPBOX_SCOPES,
        "auth_url": "https://www.dropbox.com/oauth2/authorize",
        "extra_params": {"token_access_type": "offline"},
        "revoke": {"style": "bearer", "url": "https://api.dropboxapi.com/2/auth/token/revoke"},
    },
    "meta": {
        "config_key": "meta_oauth_config",
        "env": ("META_APP_ID", "META_APP_SECRET"),
        "scopes": META_SCOPES,
        "auth_url": "https://www.facebook.com/v19.0/dialog/oauth",
        "scope_sep": ",",
        "revoke": {"style": "meta_permissions", "url": "https://graph.facebook.com/v19.0/{uid}/permissions"},
        "delete_aliases": ["facebook", "instagram"],
    },
    "twitter": {
        "config_key": "twitter_oauth_config",
        "env": ("TWITTER_CLIENT_ID", "TWITTER_CLIENT_SECRET"),
        "scopes": TWITTER_SCOPES,
        "auth_url": "https://twitter.com/i/oauth2/authorize",
        "pkce": True,
        "revoke": {"style": "basic_form", "url": "https://api.twitter.com/2/oauth2/revoke"},
    },
    "linkedin": {
        "config_key": "linkedin_oauth_config",
        "env": ("LINKEDIN_CLIENT_ID", "LINKEDIN_CLIENT_SECRET"),
        "scopes": LINKEDIN_SCOPES,
        "auth_url": "https://www.linkedin.com/oauth/v2/authorization",
    },
    "tiktok": {
        "config_key": "tiktok_oauth_config",
        "env": ("TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET"),
        "scopes": TIKTOK_SCOPES,
        "auth_url": "https://www.tiktok.com/v2/auth/authorize/",
        "scope_sep": ",",
        "client_id_param": "client_key",
        "pkce": True,
        "revoke": {"style": "form_creds", "url": "https://open.tiktokapis.com/v2/oauth/revoke/"},
    },
    "pinterest": {
        "config_key": "pinterest_oauth_config",
        "env": ("PINTEREST_APP_ID", "PINTEREST_APP_SECRET"),
        "scopes": PINTEREST_SCOPES,
        "auth_url": "https://www.pinterest.com/oauth/",
        "scope_sep": ",",
    },
    "reddit": {
        "config_key": "reddit_oauth_config",
        "env": ("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET"),
        "scopes": REDDIT_SCOPES,
        "auth_url": "https://www.reddit.com/api/v1/authorize",
        "extra_params": {"duration": "permanent"},
        "revoke": {"style": "basic_form", "url": "https://www.reddit.com/api/v1/revoke_token",
                   "headers": {"User-Agent": "WebAgent/1.0"}},
    },
    "snapchat": {
        "config_key": "snapchat_oauth_config",
        "env": ("SNAPCHAT_CLIENT_ID", "SNAPCHAT_CLIENT_SECRET"),
        "scopes": SNAPCHAT_SCOPES,
        "auth_url": "https://accounts.snapchat.com/login/oauth2/authorize",
        "revoke": {"style": "form_token", "url": "https://accounts.snapchat.com/login/oauth2/revoke_token"},
    },
    "twitch": {
        "config_key": "twitch_oauth_config",
        "env": ("TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET"),
        "scopes": TWITCH_SCOPES,
        "auth_url": "https://id.twitch.tv/oauth2/authorize",
        "revoke": {"style": "param_client_token", "url": "https://id.twitch.tv/oauth2/revoke"},
    },
    "ebay": {
        "config_key": "ebay_oauth_config",
        "env": ("EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET"),
        "scopes": EBAY_SCOPES,
        "auth_url": "https://auth.ebay.com/oauth2/authorize",
    },
    "etsy": {
        "config_key": "etsy_oauth_config",
        "env": ("ETSY_CLIENT_ID", "ETSY_CLIENT_SECRET"),
        "scopes": ETSY_SCOPES,
        "auth_url": "https://www.etsy.com/oauth/connect",
        "pkce": True,
    },
    # Shopify (per-shop host) and Amazon (Seller-Central app id + region host)
    # keep bespoke `build_*` functions; they still resolve creds/scopes here.
    "shopify": {
        "config_key": "shopify_oauth_config",
        "env": ("SHOPIFY_API_KEY", "SHOPIFY_API_SECRET"),
        "scopes": SHOPIFY_SCOPES,
        "scope_sep": ",",
    },
    "amazon": {
        "config_key": "amazon_oauth_config",
        "env": ("AMAZON_LWA_CLIENT_ID", "AMAZON_LWA_CLIENT_SECRET"),
        "scopes": AMAZON_SCOPES,
    },
}


async def _get_enabled_scopes(config_service: str, default_scopes: list) -> list:
    """Return admin-configured scopes for a provider, or the full default list."""
    try:
        from app.db import get_db
        db = get_db()
        elem = await db.auth_element_get(_ADMIN_USER, config_service, "default")
        if elem and elem.get("config"):
            config = json.loads(elem["config"]) if isinstance(elem["config"], str) else elem["config"]
            scopes = config.get("enabled_scopes")
            if scopes and isinstance(scopes, list) and len(scopes) > 0:
                return scopes
    except Exception:
        pass
    return default_scopes


# ── Ability-aware scope resolution (3-tier OAuth) ─────────────────────────
#
# Key under auth_elements for per-ability admin policy: one row per ability.
#   user_id  = "__admin__"
#   service  = f"oauth_ability:{ability_id}"        # e.g. "oauth_ability:google.gmail_read"
#   label    = "default"
#   config   = {"mode": "platform_only"|"byo_only"|"both"|"disabled",
#               "platform_scopes": [scope, ...],    # platform-OAuth scope subset
#               "max_byo_scopes":  [scope, ...]}    # cap on what BYO can request
# This namespace is distinct from `_ABILITY_CONFIG_KEY` (the legacy
# host-side abilities — codebase_admin, create_tools, automation) so the two
# concepts never collide.
OAUTH_ABILITY_SERVICE_PREFIX = "oauth_ability:"

_OAUTH_ABILITY_MODES = ("disabled", "platform_only", "byo_only", "both")

# ── Global per-tool defaults (app-wide) ───────────────────────────────────
#
# The app-wide DEFAULT settings every agent inherits unless that agent has its
# own per-tool override. Two dimensions per tool:
#   permission  — "auto" | "ask" | "deny"
#   visibility  — "always" | "discoverable"
# Stored in the `tools` section of data/config/agent-abilities.json
# (app/admin/ability_config.py) as:
#   {"<tool>": {"permission": ..., "visibility": ...}, ...}
# Each tool key may carry either/both dimensions; an absent dimension = no global
# default for it; an absent key = no global override. Resolution order is
# agent-override ▸ this global default ▸ built-in default. (Legacy installs are
# migrated from the old `tool_defaults` vault row on first boot.)
TOOL_DEFAULTS_SERVICE = "tool_defaults"  # legacy vault service key (migration only)


def _oauth_ability_service(ability_id: str) -> str:
    return f"{OAUTH_ABILITY_SERVICE_PREFIX}{ability_id}"


async def get_oauth_ability_config(ability_id: str) -> dict:
    """Return the admin policy for an ability, with sensible defaults.

    Defaults preserve current behaviour: every registered ability is available
    under platform OAuth with its full registry scope set. Admin can tighten
    later without touching agent rows."""
    from app.integrations.ability_registry import get_ability
    ab = get_ability(ability_id)
    default_scopes = list(ab.scopes) if ab else []
    cfg = {
        "mode": "platform_only",
        "platform_scopes": default_scopes,
        "max_byo_scopes": default_scopes,
    }
    try:
        from app.db import get_db
        db = get_db()
        elem = await db.auth_element_get(_ADMIN_USER, _oauth_ability_service(ability_id), "default")
        if elem and elem.get("config"):
            stored = json.loads(elem["config"]) if isinstance(elem["config"], str) else elem["config"]
            if isinstance(stored, dict):
                cfg.update({k: v for k, v in stored.items() if k in cfg})
    except Exception:
        pass
    if cfg["mode"] not in _OAUTH_ABILITY_MODES:
        cfg["mode"] = "platform_only"
    return cfg


async def get_global_tool_defaults() -> dict:
    """Return the app-wide per-tool default map: ``{tool: {permission?, visibility?}}``.

    Reads the ``tools`` section of data/config/agent-abilities.json. Empty
    ``{}`` when unset or on any error, so callers can treat "no globals" as the
    safe no-op default. Mirrors ``get_oauth_ability_config``."""
    try:
        from app.admin import ability_config
        try:
            from app.db import get_db
            await ability_config.ensure_bootstrapped(get_db())
        except Exception:
            pass
        return ability_config.get_tools()
    except Exception:
        return {}


async def get_all_oauth_ability_configs() -> dict[str, dict]:
    """Fetch admin policies for every registered ability in a single DB query.

    Returns {ability_id: {mode, platform_scopes, max_byo_scopes}} — same shape
    as `get_oauth_ability_config` output, but batched so the agents UI doesn't
    run 58+ sequential queries when rendering the abilities tab.
    """
    from app.integrations.ability_registry import ABILITIES
    from app.db import get_db
    db = get_db()
    prefix = OAUTH_ABILITY_SERVICE_PREFIX

    # Pre-populate defaults for every ability in the registry
    configs: dict[str, dict] = {
        aid: {
            "mode": "platform_only",
            "platform_scopes": list(ab.scopes),
            "max_byo_scopes": list(ab.scopes),
        }
        for aid, ab in ABILITIES.items()
    }

    try:
        # One query — fetch all auth_elements for the admin user, then filter
        # by prefix so we only pay for the rows we care about.
        elements = await db.auth_element_list(_ADMIN_USER)
        for elem in elements:
            service = elem.get("service", "")
            if not service.startswith(prefix):
                continue
            ability_id = service[len(prefix):]
            cfg = configs.get(ability_id)
            if cfg is None:
                continue
            if elem.get("config"):
                stored = json.loads(elem["config"]) if isinstance(elem["config"], str) else elem["config"]
                if isinstance(stored, dict):
                    cfg.update({k: v for k, v in stored.items() if k in cfg})
            if cfg["mode"] not in _OAUTH_ABILITY_MODES:
                cfg["mode"] = "platform_only"
    except Exception:
        pass

    return configs


async def compute_required_scopes(
    agent_id: str,
    provider: str,
    *,
    source: str = "platform",
    legacy_fallback: Optional[list] = None,
) -> list[str]:
    """Resolve the OAuth scopes to request for an agent's authorize URL.

    Algorithm:
      1. Find the abilities this agent has enabled for `provider`.
      2. Union the scopes of each ability after intersecting with the
         app-admin policy for that ability:
           - `platform_only` mode: use `platform_scopes`
           - `byo_only` / `both`: use `max_byo_scopes` when source="byo",
             else `platform_scopes`
           - `disabled`: skipped entirely (treated as not enabled)
      3. Always include the provider's implicit abilities (profile / sign-in).

    When the agent has no enabled abilities (typical for legacy agents
    pre-migration), fall back to the legacy admin-configured scope list so
    every existing connection keeps working unchanged.
    """
    from app.integrations.ability_registry import (
        abilities_for_provider,
        implicit_abilities,
        get_ability,
    )
    p = (provider or "").lower()

    enabled_ability_ids: list[str] = []
    if agent_id:
        try:
            from app.db import get_db
            db = get_db()
            if hasattr(db, "get_agent_abilities"):
                rows = await db.get_agent_abilities(agent_id)
                enabled_ability_ids = [
                    r["ability_id"]
                    for r in rows
                    if r.get("enabled") and (r.get("source") or "platform") == source
                ]
        except Exception as e:
            logger.debug("get_agent_abilities(%s) failed: %s", agent_id, e)

    enabled_ability_ids = [
        aid for aid in enabled_ability_ids
        if (ab := get_ability(aid)) is not None and ab.provider == p
    ]

    if not enabled_ability_ids:
        # No per-agent ability rows yet — preserve the pre-existing behaviour
        # by handing back the admin's legacy scope list. Any new code path
        # populates ability rows so this branch only fires for unmigrated agents.
        if legacy_fallback is not None:
            return legacy_fallback
        return list({s for aid in abilities_for_provider(p) for s in (get_ability(aid).scopes if get_ability(aid) else ())})

    # Always include implicit abilities (sign-in) so we can identify the user.
    union_ids = list(dict.fromkeys(implicit_abilities(p) + enabled_ability_ids))
    scopes: list[str] = []
    for aid in union_ids:
        ab = get_ability(aid)
        if not ab:
            continue
        policy = await get_oauth_ability_config(aid)
        if policy["mode"] == "disabled" and not ab.implicit:
            continue
        cap = policy["max_byo_scopes"] if source == "byo" else policy["platform_scopes"]
        # Intersect ability's full scope set with the admin's cap.
        allowed = set(cap or [])
        for s in ab.scopes:
            if s in allowed and s not in scopes:
                scopes.append(s)
    return scopes


# ── Shared helpers (used by app/api/agents.py and app/api/oauth.py) ──────

def resolve_user_id(authorization: str = "", token_qs: str = "") -> str:
    raw = ""
    if authorization.startswith("Bearer "):
        raw = authorization[7:]
    if not raw and token_qs:
        raw = token_qs
    if raw:
        payload = decode_token(raw)
        if payload:
            return payload.get("user_id", ANONYMOUS_KEY)
    return ANONYMOUS_KEY


_LAST_SEEN_BASE_URL: str = ""


def _is_trusted_proxy(request: Request) -> bool:
    """Return True when we should trust X-Forwarded-Proto from this request's peer.

    The header is meaningful ONLY when the request arrived through a reverse proxy
    we control (Caddy/nginx on the same box, Cloud Run's GFE, etc.).  An arbitrary
    client can set X-Forwarded-Proto: https and we must not believe it.

    Trusted peers:
      - Loopback (127.0.0.0/8, ::1) — local Caddy / nginx / dev
      - RFC 1918 private networks (10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16)
      - Link-local (169.254.0.0/16) — cloud metadata / internal proxies
      - IPs / CIDRs from TRUSTED_PROXY_IPS env var (comma-separated), to cover
        Cloud Run GFE and other platforms where the proxy peer is a public IP.
    """
    import ipaddress
    client_host = (request.client.host if request.client else None)
    if not client_host:
        return False
    try:
        ip = ipaddress.ip_address(client_host)
    except ValueError:
        return False
    if ip.is_loopback or ip.is_private or ip.is_link_local:
        return True
    trusted_env = os.environ.get("TRUSTED_PROXY_IPS", "").strip()
    if trusted_env:
        try:
            nets = [ipaddress.ip_network(s.strip(), strict=False) for s in trusted_env.split(",") if s.strip()]
            if any(ip in net for net in nets):
                return True
        except ValueError:
            pass
    return False


def _get_configured_base_url() -> Optional[str]:
    """Always returns None — the app no longer supports a pre-configured URL.
    All URL detection is request-derived. Kept as a stub for any callers that
    checked this for "is the URL explicitly set by the operator" (now always
    false)."""
    return None


def _get_base_url(request: Optional[Request] = None) -> str:
    """Return the detected base URL.

    Priority order:
    1. Derived from the incoming HTTP request (scheme + host) — works correctly
       on Cloud Run and any other hosted environment without extra config
    2. Last-seen request-derived base URL (cached so background/agent code paths
       that have no Request object still produce the right URL instead of localhost)
    3. Fallback to http://localhost:8000

    Pre-configured URLs (env var, file) are no longer supported — the URL is
    always detected from traffic.
    """
    global _LAST_SEEN_BASE_URL
    base_url = ""
    if not base_url and request is not None:
        # Derive from the incoming request. Behind a TLS-terminating proxy (e.g.
        # Cloud Run, Caddy), the app sees http:// internally but the real scheme
        # is in the X-Forwarded-Proto header — use that only when the request
        # comes from a trusted reverse proxy so we don't blindly trust a header
        # any client can set.
        derived = str(request.base_url).rstrip("/")
        forwarded_proto = request.headers.get("x-forwarded-proto", "")
        if forwarded_proto and derived.startswith("http://") and _is_trusted_proxy(request):
            derived = "https://" + derived[len("http://"):]
        base_url = derived
    if base_url:
        # Remember the most recent base so request-less callers (agent tools,
        # scheduler) can reuse it. Never let a localhost/loopback hit overwrite
        # a real public URL already cached (production internal pings shouldn't
        # poison the cache), but localhost CAN overwrite an empty cache so
        # local dev still works.
        is_local = base_url.startswith("http://localhost") or base_url.startswith("http://127.")
        cache_is_public = bool(_LAST_SEEN_BASE_URL) and not (
            _LAST_SEEN_BASE_URL.startswith("http://localhost")
            or _LAST_SEEN_BASE_URL.startswith("http://127.")
        )
        if not (is_local and cache_is_public):
            _LAST_SEEN_BASE_URL = base_url
        return base_url
    if _LAST_SEEN_BASE_URL:
        return _LAST_SEEN_BASE_URL
    return "http://localhost:8000"


# ── Per-provider stored redirect URI ──────────────────────────────────────

def per_agent_redirect_uri(provider: str, agent_id: str, request: Optional[Request] = None) -> str:
    """The stable per-agent OAuth landing URL.

    Agent admins running BYO OAuth register this URI in their own Google /
    Microsoft / etc. project so every per-agent flow lands here. The handler
    at this path does the full token exchange against the agent's BYO creds
    (Phase 3) — under platform OAuth (Phase 2) it 302s to the standard
    `/api/v1/oauth/callback/{provider}` endpoint.
    """
    base = _get_base_url(request).rstrip("/")
    return f"{base}/agents/{agent_id}/oauth/callback/{provider}"


async def _provider_redirect_uri(
    provider: str,
    service_key: str,
    request: Optional[Request],
    agent_id: str,
    source: str,
) -> str:
    """Shared resolver: per-agent URI for BYO, admin-stored or derived URI otherwise."""
    if source == "byo" and agent_id:
        return per_agent_redirect_uri(provider, agent_id, request)
    stored = await get_stored_oauth_redirect_uri(service_key)
    if stored:
        return stored
    return f"{_get_base_url(request).rstrip('/')}/api/v1/oauth/callback/{provider}"


# ── Cred resolver: platform vs BYO ─────────────────────────────────────────

async def resolve_oauth_creds(
    provider: str, agent_id: str = "", *, source: str = "platform",
) -> tuple[str, str]:
    """Return (client_id, client_secret) for an authorize / callback flow.

    For `source="platform"`, returns the app-admin's platform creds.
    For `source="byo"`, returns the agent admin's BYO creds (configured per
    agent in the abilities tab). Returns ("","") when BYO is selected but
    no creds are configured — callers should surface a setup error.
    """
    p = (provider or "").lower()
    if source == "byo" and agent_id:
        try:
            from app.db import get_db
            db = get_db()
            if hasattr(db, "get_agent_byo_creds"):
                cid, csec = await db.get_agent_byo_creds(agent_id, p)
                if cid and csec:
                    return (cid, csec)
        except Exception as e:
            logger.warning("BYO cred lookup failed for %s/%s: %s", p, agent_id, e)
        return ("", "")
    return await _provider_creds(p)


async def get_stored_oauth_redirect_uri(service_key: str) -> Optional[str]:
    """Return the admin-saved redirect URI for a provider, or None if unset.

    Each provider's stored OAuth config can carry an explicit `redirect_uri`
    chosen by the admin at setup time. That value is sent verbatim on the
    OAuth flow so the registered URI at the provider matches no matter which
    host the user originally arrived on. Returns None when the field isn't
    present so callers can fall back to a request-derived default."""
    try:
        from app.db import get_db
        db = get_db()
        elem = await db.auth_element_get(_ADMIN_USER, service_key, "default")
        if elem and elem.get("config"):
            config = json.loads(elem["config"]) if isinstance(elem["config"], str) else elem["config"]
            uri = (config or {}).get("redirect_uri") or ""
            return uri.strip() or None
    except Exception as e:
        logger.debug("get_stored_oauth_redirect_uri(%s) failed: %s", service_key, e)
    return None


def _validate_oauth_redirect_uri(uri: str, expected_path: str) -> str:
    """Validate an admin-entered redirect URI and return the normalized form.

    Path is fixed (the FastAPI route the OAuth provider must hit); scheme is
    locked to https (with an http exemption for localhost dev). Host is the
    legitimate variable — only a presence check here; the UI shows a soft
    warning when it differs from the admin's current host. Raises
    HTTPException(400) on any failure so the admin sees the rejection in the
    setup form."""
    raw = (uri or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="redirect_uri must not be empty")
    try:
        parsed = urlparse(raw)
    except Exception:
        raise HTTPException(status_code=400, detail="redirect_uri is not a valid URL")
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    if not host:
        raise HTTPException(status_code=400, detail="redirect_uri must include a host")
    is_loopback = host in ("localhost", "127.0.0.1", "::1")
    if scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="redirect_uri must use https")
    if scheme == "http" and not is_loopback:
        raise HTTPException(status_code=400, detail="redirect_uri must use https")
    path = parsed.path or ""
    if path.endswith("/") and path != "/":
        path = path.rstrip("/")
    if path != expected_path:
        raise HTTPException(
            status_code=400,
            detail=f"redirect_uri path must be {expected_path}",
        )
    if parsed.query:
        raise HTTPException(status_code=400, detail="redirect_uri must not include a query string")
    if parsed.fragment:
        raise HTTPException(status_code=400, detail="redirect_uri must not include a fragment")
    # Rebuild from validated components — lowercased scheme + host, original
    # port + userinfo if any, normalized path, no query/fragment.
    netloc = parsed.netloc
    # Lowercase only the host portion of netloc, preserving any port.
    if "@" in netloc or ":" in netloc.split("@")[-1]:
        # Has userinfo or explicit port — recompose carefully.
        userinfo = ""
        if "@" in netloc:
            userinfo, netloc = netloc.rsplit("@", 1)
            userinfo += "@"
        if ":" in netloc:
            h, port = netloc.rsplit(":", 1)
            netloc = f"{h.lower()}:{port}"
        else:
            netloc = netloc.lower()
        netloc = userinfo + netloc
    else:
        netloc = netloc.lower()
    return urlunparse((scheme, netloc, path, "", "", ""))


# ── Generic state-token helpers ───────────────────────────────────────────

def make_state_token(user_id: str, agent_id: str = "", provider: str = "google", **extra) -> str:
    """Create a signed JWT state token for OAuth CSRF protection.

    Extra keyword args are merged into the payload (e.g. pkce_verifier for Twitter).
    """
    payload = {
        "user_id": user_id,
        "agent_id": agent_id,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=10),
        "purpose": f"{provider}_oauth",
        **extra,
    }
    return jwt.encode(payload, get_secret(), algorithm=_JWT_ALGORITHM)


def decode_state_token(state: str, provider: str = "google") -> Optional[dict]:
    """Decode state token. Returns full payload dict (user_id, agent_id, ...) or None."""
    try:
        payload = jwt.decode(state, get_secret(), algorithms=[_JWT_ALGORITHM])
        if payload.get("purpose") != f"{provider}_oauth":
            return None
        return payload
    except Exception:
        return None


def _pkce_pair() -> tuple[str, str]:
    """Generate a PKCE (code_verifier, code_challenge) pair."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


# ── Generic per-provider OAuth helpers ─────────────────────────────────────
#
# All four helpers read the `_PROVIDERS` table above. The public
# `get_<p>_creds` and `build_<p>_authorize_url` names are kept as one-line
# wrappers because they are imported by name across the app (app/tools/
# loader.py, app/integrations/oauth_helper.py, the per-agent authorize
# dispatcher in app/api/agents.py, and the per-provider tool modules).
# Token revocation collapses into the single generic `revoke_and_delete`.


async def _provider_creds(provider: str) -> tuple[str, str]:
    """(client_id, client_secret) for a provider — admin DB row first, env fallback."""
    spec = _PROVIDERS.get((provider or "").lower())
    if not spec:
        return ("", "")
    try:
        from app.db import get_db
        db = get_db()
        elem = await db.auth_element_get(_ADMIN_USER, spec["config_key"], "default")
        if elem and elem.get("config"):
            config = json.loads(elem["config"]) if isinstance(elem["config"], str) else elem["config"]
            cid = config.get("client_id", "")
            csec = elem.get("secret_ref", "") or config.get("client_secret", "")
            if cid and csec:
                return (cid, csec)
    except Exception as e:
        logger.debug("Failed to read %s creds from DB: %s", provider, e)
    env_id, env_secret = spec["env"]
    return (os.environ.get(env_id, ""), os.environ.get(env_secret, ""))


async def provider_redirect_uri(
    provider: str, request: Optional[Request] = None, *,
    agent_id: str = "", source: str = "platform",
) -> str:
    """The OAuth landing URI for a provider — admin-stored, per-agent (BYO), or derived.

    Replaces the old per-provider `get_<p>_redirect_uri` pass-throughs; both the
    admin config view and the OAuth callback handlers call this directly.
    """
    spec = _PROVIDERS.get((provider or "").lower())
    config_key = spec["config_key"] if spec else f"{(provider or '').lower()}_oauth_config"
    return await _provider_redirect_uri(provider, config_key, request, agent_id, source)


async def _build_authorize_url(
    provider: str, user_id: str, agent_id: str = "", request: Optional[Request] = None,
    *, source: str = "platform",
) -> tuple[str, str]:
    """Generic authorize-URL builder. Returns (authorize_url, state_token).

    Honors the table's scope separator, client-id param name, constant extra
    params and PKCE flag. Shopify / Amazon are built bespoke (see below).
    """
    spec = _PROVIDERS[(provider or "").lower()]
    client_id, _ = await _provider_creds(provider)
    redirect_uri = await provider_redirect_uri(provider, request, agent_id=agent_id, source=source)
    state_extra: dict = {}
    pkce_params: dict = {}
    if spec.get("pkce"):
        verifier, challenge = _pkce_pair()
        state_extra["pkce_verifier"] = verifier
        pkce_params = {"code_challenge": challenge, "code_challenge_method": "S256"}
    state = make_state_token(user_id, agent_id, provider=provider, source=source, **state_extra)
    legacy = await _get_enabled_scopes(spec["config_key"], spec["scopes"])
    scopes = await compute_required_scopes(agent_id, provider, source=source, legacy_fallback=legacy)
    params = {
        spec.get("client_id_param", "client_id"): client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": spec.get("scope_sep", " ").join(scopes),
        "state": state,
        **spec.get("extra_params", {}),
        **pkce_params,
    }
    return f"{spec['auth_url']}?{urlencode(params)}", state


async def _do_revoke(style: str, *, url: str, access_token: str, client_id: str = "",
                     client_secret: str = "", extra_headers: Optional[dict] = None,
                     provider_user_id: str = "") -> None:
    """Best-effort token revocation at the provider, dispatched by `style`."""
    headers = dict(extra_headers or {})
    async with httpx.AsyncClient() as c:
        if style == "param_token":
            await c.post(url, params={"token": access_token})
        elif style == "param_client_token":
            if not client_id:
                return
            await c.post(url, params={"client_id": client_id, "token": access_token})
        elif style == "bearer":
            await c.post(url, headers={**headers, "Authorization": f"Bearer {access_token}"})
        elif style == "form_token":
            await c.post(url, headers={**headers, "Content-Type": "application/x-www-form-urlencoded"},
                         data={"token": access_token})
        elif style == "form_creds":
            await c.post(url, headers={**headers, "Content-Type": "application/x-www-form-urlencoded"},
                         data={"client_key": client_id, "client_secret": client_secret, "token": access_token})
        elif style == "basic_form":
            creds = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
            await c.post(url, headers={**headers, "Authorization": f"Basic {creds}",
                                       "Content-Type": "application/x-www-form-urlencoded"},
                         data={"token": access_token, "token_type_hint": "access_token"})
        elif style == "meta_permissions":
            if provider_user_id:
                await c.delete(url.format(uid=provider_user_id), params={"access_token": access_token})


async def revoke_and_delete(provider: str, user_id: str, agent_id: str) -> bool:
    """Revoke this (user, agent)'s token at the provider (best-effort) and delete it.

    Replaces the 16 per-provider `revoke_and_delete_<p>` functions; the revoke
    style + endpoint come from `_PROVIDERS`. Providers with no revoke endpoint
    (Microsoft, Yahoo, LinkedIn, Pinterest, eBay, Etsy, Shopify, Amazon) are
    delete-only. Meta also clears its facebook / instagram aliases.
    """
    p = (provider or "").lower()
    spec = _PROVIDERS.get(p)
    if not spec:
        return False
    from app.db import get_db
    db = get_db()
    label = oauth_label(agent_id)
    rev = spec.get("revoke")
    if rev:
        elem = await db.auth_element_get(user_id, p, label)
        if elem and elem.get("secret_ref"):
            try:
                tokens = json.loads(elem["secret_ref"])
                access_token = tokens.get("access_token", "")
                client_id, client_secret = "", ""
                if rev["style"] in ("basic_form", "form_creds", "param_client_token"):
                    client_id, client_secret = await _provider_creds(p)
                provider_user_id = ""
                if rev["style"] == "meta_permissions":
                    cfg = elem.get("config")
                    cfg = json.loads(cfg) if isinstance(cfg, str) else (cfg or {})
                    provider_user_id = cfg.get("provider_user_id", "")
                if access_token:
                    await _do_revoke(
                        rev["style"], url=rev["url"], access_token=access_token,
                        client_id=client_id, client_secret=client_secret,
                        extra_headers=rev.get("headers"), provider_user_id=provider_user_id,
                    )
            except Exception as e:
                logger.warning("Failed to revoke %s token: %s", p, e)
    deleted = await db.auth_element_delete(user_id, p, label)
    aliases = spec.get("delete_aliases") or []
    for alias in aliases:
        await db.auth_element_delete(user_id, alias, label)
    return True if aliases else deleted


# ── Public per-provider wrappers (kept for import-by-name compatibility) ───

async def get_google_creds() -> tuple[str, str]:
    return await _provider_creds("google")


def get_google_creds_sync() -> tuple[str, str]:
    """Sync fallback — env vars only (for module-level checks)."""
    return (
        os.environ.get("GOOGLE_CLIENT_ID", ""),
        os.environ.get("GOOGLE_CLIENT_SECRET", ""),
    )


async def get_microsoft_creds() -> tuple[str, str]:
    return await _provider_creds("microsoft")


async def get_yahoo_creds() -> tuple[str, str]:
    return await _provider_creds("yahoo")


async def get_dropbox_creds() -> tuple[str, str]:
    return await _provider_creds("dropbox")


async def get_meta_creds() -> tuple[str, str]:
    return await _provider_creds("meta")


async def get_twitter_creds() -> tuple[str, str]:
    return await _provider_creds("twitter")


async def get_linkedin_creds() -> tuple[str, str]:
    return await _provider_creds("linkedin")


async def get_tiktok_creds() -> tuple[str, str]:
    return await _provider_creds("tiktok")


async def get_pinterest_creds() -> tuple[str, str]:
    return await _provider_creds("pinterest")


async def get_reddit_creds() -> tuple[str, str]:
    return await _provider_creds("reddit")


async def get_snapchat_creds() -> tuple[str, str]:
    return await _provider_creds("snapchat")


async def get_twitch_creds() -> tuple[str, str]:
    return await _provider_creds("twitch")


async def get_ebay_creds() -> tuple[str, str]:
    return await _provider_creds("ebay")


async def get_etsy_creds() -> tuple[str, str]:
    return await _provider_creds("etsy")


async def get_shopify_creds() -> tuple[str, str]:
    return await _provider_creds("shopify")


async def get_amazon_creds() -> tuple[str, str]:
    return await _provider_creds("amazon")


async def build_google_authorize_url(
    user_id: str, agent_id: str = "", request: Optional[Request] = None,
    *, source: str = "platform",
) -> str:
    url, _ = await _build_authorize_url("google", user_id, agent_id, request, source=source)
    return url


async def build_microsoft_authorize_url(
    user_id: str, agent_id: str = "", request: Optional[Request] = None,
    *, source: str = "platform",
) -> str:
    url, _ = await _build_authorize_url("microsoft", user_id, agent_id, request, source=source)
    return url


async def build_yahoo_authorize_url(
    user_id: str, agent_id: str = "", request: Optional[Request] = None,
    *, source: str = "platform",
) -> str:
    url, _ = await _build_authorize_url("yahoo", user_id, agent_id, request, source=source)
    return url


async def build_dropbox_authorize_url(
    user_id: str, agent_id: str = "", request: Optional[Request] = None,
    *, source: str = "platform",
) -> str:
    url, _ = await _build_authorize_url("dropbox", user_id, agent_id, request, source=source)
    return url


async def build_meta_authorize_url(
    user_id: str, agent_id: str = "", request: Optional[Request] = None,
    *, source: str = "platform",
) -> str:
    url, _ = await _build_authorize_url("meta", user_id, agent_id, request, source=source)
    return url


async def build_twitter_authorize_url(
    user_id: str, agent_id: str = "", request: Optional[Request] = None,
    *, source: str = "platform",
) -> tuple[str, str]:
    """Returns (authorize_url, state_token). Twitter requires PKCE."""
    return await _build_authorize_url("twitter", user_id, agent_id, request, source=source)


async def build_linkedin_authorize_url(
    user_id: str, agent_id: str = "", request: Optional[Request] = None,
    *, source: str = "platform",
) -> str:
    url, _ = await _build_authorize_url("linkedin", user_id, agent_id, request, source=source)
    return url


async def build_tiktok_authorize_url(
    user_id: str, agent_id: str = "", request: Optional[Request] = None,
    *, source: str = "platform",
) -> str:
    url, _ = await _build_authorize_url("tiktok", user_id, agent_id, request, source=source)
    return url


async def build_pinterest_authorize_url(
    user_id: str, agent_id: str = "", request: Optional[Request] = None,
    *, source: str = "platform",
) -> str:
    url, _ = await _build_authorize_url("pinterest", user_id, agent_id, request, source=source)
    return url


async def build_reddit_authorize_url(
    user_id: str, agent_id: str = "", request: Optional[Request] = None,
    *, source: str = "platform",
) -> str:
    url, _ = await _build_authorize_url("reddit", user_id, agent_id, request, source=source)
    return url


async def build_snapchat_authorize_url(
    user_id: str, agent_id: str = "", request: Optional[Request] = None,
    *, source: str = "platform",
) -> str:
    url, _ = await _build_authorize_url("snapchat", user_id, agent_id, request, source=source)
    return url


async def build_twitch_authorize_url(
    user_id: str, agent_id: str = "", request: Optional[Request] = None,
    *, source: str = "platform",
) -> str:
    url, _ = await _build_authorize_url("twitch", user_id, agent_id, request, source=source)
    return url


async def build_ebay_authorize_url(
    user_id: str, agent_id: str = "", request: Optional[Request] = None,
    *, source: str = "platform",
) -> str:
    url, _ = await _build_authorize_url("ebay", user_id, agent_id, request, source=source)
    return url


async def build_etsy_authorize_url(
    user_id: str, agent_id: str = "", request: Optional[Request] = None,
    *, source: str = "platform",
) -> str:
    """Etsy uses OAuth 2.0 + PKCE (verifier stashed in the state JWT)."""
    url, _ = await _build_authorize_url("etsy", user_id, agent_id, request, source=source)
    return url


# ── Shopify (per-shop) — bespoke build, generic creds/redirect/scopes ──────

def _shopify_normalize_shop(shop: str) -> str:
    """Normalize 'my-store' or 'my-store.myshopify.com' → 'my-store.myshopify.com'."""
    s = (shop or "").strip().lower().replace("https://", "").replace("http://", "").rstrip("/")
    if "." not in s:
        s = f"{s}.myshopify.com"
    return s


async def build_shopify_authorize_url(
    user_id: str,
    shop: str,
    agent_id: str = "",
    request: Optional[Request] = None,
    *, source: str = "platform",
) -> str:
    """Build the per-shop Shopify install URL. Caller must supply the shop domain."""
    if not shop:
        raise ValueError("shop domain required for Shopify authorize")
    shop_norm = _shopify_normalize_shop(shop)
    client_id, _ = await _provider_creds("shopify")
    redirect_uri = await provider_redirect_uri("shopify", request, agent_id=agent_id, source=source)
    state = make_state_token(user_id, agent_id, provider="shopify", shop=shop_norm, source=source)
    legacy = await _get_enabled_scopes("shopify_oauth_config", SHOPIFY_SCOPES)
    scopes = await compute_required_scopes(
        agent_id, "shopify", source=source, legacy_fallback=legacy,
    )
    params = {
        "client_id": client_id,
        "scope": ",".join(scopes),
        "redirect_uri": redirect_uri,
        "state": state,
    }
    return f"https://{shop_norm}/admin/oauth/authorize?{urlencode(params)}"


# ── Amazon SP-API (LWA) — bespoke build (app id + region host) ─────────────

async def _get_amazon_app_id() -> str:
    """The Seller Central application id (separate from the LWA client_id).

    Stored in admin config alongside the LWA creds.
    """
    try:
        from app.db import get_db
        db = get_db()
        elem = await db.auth_element_get(_ADMIN_USER, "amazon_oauth_config", "default")
        if elem and elem.get("config"):
            config = json.loads(elem["config"]) if isinstance(elem["config"], str) else elem["config"]
            return config.get("app_id", "") or os.environ.get("AMAZON_SP_APP_ID", "")
    except Exception:
        pass
    return os.environ.get("AMAZON_SP_APP_ID", "")


async def build_amazon_authorize_url(
    user_id: str,
    region: str = "NA",
    agent_id: str = "",
    request: Optional[Request] = None,
    *, source: str = "platform",
) -> str:
    """Build the Amazon Seller Central authorization URL.

    Sellers complete the OAuth handshake on Seller Central (region-specific),
    then Amazon redirects back to our LWA callback with a spapi_oauth_code.
    """
    app_id = await _get_amazon_app_id()
    redirect_uri = await provider_redirect_uri("amazon", request, agent_id=agent_id, source=source)
    state = make_state_token(user_id, agent_id, provider="amazon", region=region.upper(), source=source)
    # Seller Central hosts the consent screen, not LWA directly.
    sc_hosts = {
        "NA": "https://sellercentral.amazon.com",
        "EU": "https://sellercentral.amazon.co.uk",
        "FE": "https://sellercentral.amazon.co.jp",
    }
    host = sc_hosts.get(region.upper(), sc_hosts["NA"])
    params = {
        "application_id": app_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "version": "beta",
    }
    return f"{host}/apps/authorize/consent?{urlencode(params)}"


# Web Scraper (admin-global) and Browser Cookies (per-user) are now ordinary
# drop-in abilities under plugins/abilities/Web/. Their credentials live in the
# common-credential framework (app/abilities/credentials.py) — no bespoke reader,
# endpoint, or config map here anymore.


# ── Connection → admin-config mapping ─────────────────────────────────────
#
# Each agent-page connection_type maps to the auth_elements service key the
# admin uses to store OAuth credentials. `_ALWAYS_AVAILABLE` providers need
# no upfront setup — their configuration happens inside the agent's own
# Connections tab (e.g. telegram bot token) — so they are always surfaced.
_CONNECTION_ADMIN_CONFIG_KEY: dict[str, str] = {
    "google":    "google_oauth_config",
    "microsoft": "microsoft_oauth_config",
    "yahoo":     "yahoo_oauth_config",
    "dropbox":   "dropbox_oauth_config",
    "facebook":  "meta_oauth_config",
    "instagram": "meta_oauth_config",
    "twitter":   "twitter_oauth_config",
    "linkedin":  "linkedin_oauth_config",
    "tiktok":    "tiktok_oauth_config",
    "pinterest": "pinterest_oauth_config",
    "reddit":    "reddit_oauth_config",
    "snapchat":  "snapchat_oauth_config",
    "twitch":    "twitch_oauth_config",
    "ebay":      "ebay_oauth_config",
    "etsy":      "etsy_oauth_config",
    "shopify":   "shopify_oauth_config",
    "amazon":    "amazon_oauth_config",
}

# Communication channels — gated by an admin enable flag stored in
# auth_elements under `channel_{name}`. They have no upfront credentials at
# the platform level (the actual bot token / API key is supplied per-agent),
# so "configured" simply means the admin has enabled the channel.
_CHANNEL_CONFIG_KEY: dict[str, str] = {
    "telegram": "channel_telegram",
}

# Agent Tools — privileged host-side capabilities (codebase admin, tool
# creation, …). These are now fully catalog-driven: the set of toggleable
# abilities is every kind="ability" drop-in under plugins/abilities/, and each
# one's app-level on/off choice lives in data/config/agent-abilities.json (with
# the descriptor's `default_enabled` as the fallback when unset). There is
# deliberately NO hardcoded ability list here any more — adding an ability is a
# drop-in file, never a core edit (see CLAUDE.md "Core vs. plugins" and
# app/admin/ability_config.py).


async def _ability_app_enabled(db, ability: str) -> bool:
    """True iff `ability` is enabled at the app (platform) level.

    Catalog-driven (no hardcoded ability list): the on/off choice lives in
    data/config/agent-abilities.json. When an ability has no explicit stored
    choice it falls back to its descriptor `default_enabled` (on-by-default
    abilities like git_control/ui_admin set it true; credentialed/destructive
    ones default off). Any non-`kind:"ability"` id is not toggleable → False.
    """
    from app import abilities as abilities_catalog
    from app.admin import ability_config
    if not abilities_catalog.is_toggleable_ability(ability):
        return False
    await ability_config.ensure_bootstrapped(db)
    stored = ability_config.get_ability_enabled(ability)
    if stored is not None:
        return stored
    return abilities_catalog.ability_default_enabled(ability)


def _app_function_enabled(app_function_id: str) -> bool:
    """Sync app-level gate for an APP FUNCTION (Session Namer, …).

    Unlike a real agent ability, an app function has no per-agent connection row
    and is excluded from `get_ability_enabled_map()` (kind != "ability"), so its
    on/off resolves through the same app-level store via
    app.abilities.app_function_enabled. Fails ON on any read error (a
    default-on function is never silently disabled)."""
    try:
        from app import abilities as _abilities_gate
        return _abilities_gate.app_function_enabled(app_function_id)
    except Exception:
        return True


async def get_admin_configured_providers(user_id: Optional[str] = None) -> set[str]:
    """Return the set of connection_types that are set up and toggleable.

    A provider counts as configured when:
    - For channels (`telegram`, ...): the admin has enabled it in App Config
      → Agent Abilities (auth_elements row exists with secret_ref="enabled"), OR
    - Its admin `auth_elements` row exists with a non-empty `secret_ref`
      (the client secret / API key), OR
    - For a credential-bearing ability (one that declares a `credentials` block,
      e.g. web_scraper / browser_cookies): it is app-enabled AND its required
      secrets are present at its scope (per-user scopes need the `user_id`).
    """
    configured: set[str] = set()
    try:
        from app.db import get_db
        db = get_db()
    except Exception as e:
        logger.debug("Failed to import db while checking configured providers: %s", e)
        return configured

    # Pull ALL of the admin's auth elements in ONE query and index by service,
    # instead of a per-service `auth_element_get` round-trip in each loop below.
    # On a remote DB the old per-service form was ~13-20 serial round-trips and
    # made this function (called on every Agents-page connections/abilities load)
    # take multiple seconds. `secret_ref` semantics are unchanged.
    try:
        _admin_elems = await db.auth_element_list(_ADMIN_USER)
    except Exception as e:
        logger.debug("auth_element_list(admin) failed: %s", e)
        _admin_elems = []
    _admin_by_service: dict[str, dict] = {}
    for _el in _admin_elems:
        if (_el.get("label") or "default") == "default":
            _admin_by_service[_el.get("service")] = _el

    # Channels: admin-enabled if their channel auth_element exists.
    for ct, service_key in _CHANNEL_CONFIG_KEY.items():
        elem = _admin_by_service.get(service_key)
        if elem and elem.get("secret_ref"):
            configured.add(ct)

    # Agent Tools — every kind="ability" drop-in in the catalog. Whether each is
    # on is read from data/config/agent-abilities.json (with descriptor fallback)
    # by `_ability_app_enabled`, so new drop-ins are picked up automatically.
    # A credential-bearing ability (declares a `credentials` block) additionally
    # requires its secrets to be present — generic, no per-ability special case.
    from app import abilities as abilities_catalog
    from app.abilities import credentials as _ab_creds
    for ct in abilities_catalog.ability_ids("ability"):
        if not await _ability_app_enabled(db, ct):
            continue
        spec = _ab_creds.ability_credentials_spec(ct)
        if spec is not None and spec.get("scope") == "admin":
            # ADMIN-scoped credentials (one global secret, e.g. web_scraper's API
            # key): gate the ability — hide it from agents until the admin has
            # saved the secret, since nothing a per-agent user does could supply
            # it. USER/AGENT-scoped credentials (e.g. browser_cookies) are entered
            # by the agent's own user in the per-agent panel, so the ability must
            # stay VISIBLE even when empty — otherwise there is nowhere to enter
            # them (chicken-and-egg). Their tools already return "not configured"
            # at runtime until the user supplies the secret.
            try:
                if not await _ab_creds.is_configured(ct, user_id=user_id or ""):
                    continue  # enabled but admin secret missing → not yet usable
            except Exception as e:
                logger.debug("cred check failed for %s: %s", ct, e)
                continue
        configured.add(ct)

    # Cache per service_key — facebook & instagram share `meta_oauth_config`.
    seen: dict[str, bool] = {}
    for ct, service_key in _CONNECTION_ADMIN_CONFIG_KEY.items():
        if service_key in seen:
            if seen[service_key]:
                configured.add(ct)
            continue
        elem = _admin_by_service.get(service_key)
        ok = bool(elem and elem.get("secret_ref"))
        seen[service_key] = ok
        if ok:
            configured.add(ct)

    return configured


# ── Admin endpoints ──────────────────────────────────────────────────────

class OAuthConfigRequest(BaseModel):
    client_id: str
    client_secret: str
    scopes: Optional[list[str]] = None
    redirect_uri: Optional[str] = None


# Each provider's callback path under /api/v1/oauth/callback/... The save
# handlers validate that the admin-entered redirect_uri matches this path
# exactly (locking down everything but the host) before storing.
_PROVIDER_CALLBACK_PATH = {
    "google":    "/api/v1/oauth/callback/google",
    "microsoft": "/api/v1/oauth/callback/microsoft",
    "yahoo":     "/api/v1/oauth/callback/yahoo",
    "dropbox":   "/api/v1/oauth/callback/dropbox",
    "meta":      "/api/v1/oauth/callback/meta",
    "twitter":   "/api/v1/oauth/callback/twitter",
    "linkedin":  "/api/v1/oauth/callback/linkedin",
    "tiktok":    "/api/v1/oauth/callback/tiktok",
    "pinterest": "/api/v1/oauth/callback/pinterest",
    "reddit":    "/api/v1/oauth/callback/reddit",
    "snapchat":  "/api/v1/oauth/callback/snapchat",
    "twitch":    "/api/v1/oauth/callback/twitch",
    "ebay":      "/api/v1/oauth/callback/ebay",
    "etsy":      "/api/v1/oauth/callback/etsy",
    "shopify":   "/api/v1/oauth/callback/shopify",
    "amazon":    "/api/v1/oauth/callback/amazon",
}


def _apply_redirect_uri(cfg: dict, provider: str, req_redirect_uri: Optional[str]) -> None:
    """If the admin supplied a redirect_uri, validate & write it into cfg."""
    raw = (req_redirect_uri or "").strip()
    if not raw:
        return
    expected_path = _PROVIDER_CALLBACK_PATH.get(provider)
    if not expected_path:
        return
    cfg["redirect_uri"] = _validate_oauth_redirect_uri(raw, expected_path)


# Keep old name for backward compat
GoogleOAuthConfigRequest = OAuthConfigRequest


@router.get("")
async def get_integration_config(
    request: Request,
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Admin check: returns config status for all OAuth providers."""
    def _mask(s: str) -> str:
        return s[:20] + "..." if len(s) > 20 else s

    # Every read below is an independent DB round-trip. Run them concurrently
    # with asyncio.gather instead of awaiting ~48 in series — on a remote
    # Postgres backend the serial version paid one network RTT per read and was
    # the dominant cost of opening Agent Settings. Grouped into three fan-outs
    # (creds+maps, scopes, redirect URIs) so a single await drains each batch.
    import asyncio

    (
        (google_cid, google_csec),
        (ms_cid, ms_csec),
        (yahoo_cid, yahoo_csec),
        (dbx_cid, dbx_csec),
        (meta_cid, meta_csec),
        (tw_cid, tw_csec),
        (li_cid, li_csec),
        (tt_cid, tt_csec),
        (pin_cid, pin_csec),
        (red_cid, red_csec),
        (snap_cid, snap_csec),
        (twitch_cid, twitch_csec),
        (ebay_cid, ebay_csec),
        (etsy_cid, etsy_csec),
        (shop_cid, shop_csec),
        (amz_cid, amz_csec),
        channel_enabled,
        ability_enabled,
    ) = await asyncio.gather(
        get_google_creds(),
        get_microsoft_creds(),
        get_yahoo_creds(),
        get_dropbox_creds(),
        get_meta_creds(),
        get_twitter_creds(),
        get_linkedin_creds(),
        get_tiktok_creds(),
        get_pinterest_creds(),
        get_reddit_creds(),
        get_snapchat_creds(),
        get_twitch_creds(),
        get_ebay_creds(),
        get_etsy_creds(),
        get_shopify_creds(),
        get_amazon_creds(),
        get_channel_enabled_map(),
        get_ability_enabled_map(),
    )

    # Fetch enabled scopes for all providers (returns defaults when unconfigured)
    (
        g_scopes, ms_scopes, yh_scopes, dbx_scopes, meta_scopes, tw_scopes,
        li_scopes, tt_scopes, pin_scopes, red_scopes, snap_scopes, twit_scopes,
        ebay_scopes, etsy_scopes, shop_scopes, amz_scopes,
    ) = await asyncio.gather(
        _get_enabled_scopes("google_oauth_config",    GOOGLE_SCOPES),
        _get_enabled_scopes("microsoft_oauth_config", MICROSOFT_SCOPES),
        _get_enabled_scopes("yahoo_oauth_config",     YAHOO_SCOPES),
        _get_enabled_scopes("dropbox_oauth_config",   DROPBOX_SCOPES),
        _get_enabled_scopes("meta_oauth_config",      META_SCOPES),
        _get_enabled_scopes("twitter_oauth_config",   TWITTER_SCOPES),
        _get_enabled_scopes("linkedin_oauth_config",  LINKEDIN_SCOPES),
        _get_enabled_scopes("tiktok_oauth_config",    TIKTOK_SCOPES),
        _get_enabled_scopes("pinterest_oauth_config", PINTEREST_SCOPES),
        _get_enabled_scopes("reddit_oauth_config",    REDDIT_SCOPES),
        _get_enabled_scopes("snapchat_oauth_config",  SNAPCHAT_SCOPES),
        _get_enabled_scopes("twitch_oauth_config",    TWITCH_SCOPES),
        _get_enabled_scopes("ebay_oauth_config",      EBAY_SCOPES),
        _get_enabled_scopes("etsy_oauth_config",      ETSY_SCOPES),
        _get_enabled_scopes("shopify_oauth_config",   SHOPIFY_SCOPES),
        _get_enabled_scopes("amazon_oauth_config",    AMAZON_SCOPES),
    )

    # Configured-view redirect URI per provider (saved value, or a request-
    # derived fallback). Fan these out too — each is another DB read.
    _ruri_providers = [
        "google", "microsoft", "yahoo", "dropbox", "meta", "twitter",
        "linkedin", "tiktok", "pinterest", "reddit", "snapchat", "twitch",
        "ebay", "etsy", "shopify", "amazon",
    ]
    _ruri_values = await asyncio.gather(
        *(provider_redirect_uri(p, request) for p in _ruri_providers)
    )
    ruri = dict(zip(_ruri_providers, _ruri_values))

    # The configured-view URI (per provider) is whatever the admin saved at
    # setup time; if nothing is saved yet, the helper falls back to a
    # request-derived URI so the setup form has a sensible placeholder.
    suggested_base = _get_base_url(request).rstrip("/")
    def _suggest(provider: str) -> str:
        return f"{suggested_base}/api/v1/oauth/callback/{provider}"

    return {
        # Productivity
        "google_configured":    bool(google_cid and google_csec),
        "google_client_id":     _mask(google_cid),
        "google_scopes":        g_scopes,
        "redirect_uri":         ruri["google"],
        "redirect_uri_suggested": _suggest("google"),
        "microsoft_configured": bool(ms_cid and ms_csec),
        "microsoft_client_id":  _mask(ms_cid),
        "microsoft_scopes":     ms_scopes,
        "microsoft_redirect_uri": ruri["microsoft"],
        "microsoft_redirect_uri_suggested": _suggest("microsoft"),
        "yahoo_configured":     bool(yahoo_cid and yahoo_csec),
        "yahoo_client_id":      _mask(yahoo_cid),
        "yahoo_scopes":         yh_scopes,
        "yahoo_redirect_uri":   ruri["yahoo"],
        "yahoo_redirect_uri_suggested": _suggest("yahoo"),
        "dropbox_configured":   bool(dbx_cid and dbx_csec),
        "dropbox_client_id":    _mask(dbx_cid),
        "dropbox_scopes":       dbx_scopes,
        "dropbox_redirect_uri": ruri["dropbox"],
        "dropbox_redirect_uri_suggested": _suggest("dropbox"),
        # Social media
        "meta_configured":      bool(meta_cid and meta_csec),
        "meta_client_id":       _mask(meta_cid),
        "meta_scopes":          meta_scopes,
        "meta_redirect_uri":    ruri["meta"],
        "meta_redirect_uri_suggested": _suggest("meta"),
        "twitter_configured":   bool(tw_cid and tw_csec),
        "twitter_client_id":    _mask(tw_cid),
        "twitter_scopes":       tw_scopes,
        "twitter_redirect_uri": ruri["twitter"],
        "twitter_redirect_uri_suggested": _suggest("twitter"),
        "linkedin_configured":  bool(li_cid and li_csec),
        "linkedin_client_id":   _mask(li_cid),
        "linkedin_scopes":      li_scopes,
        "linkedin_redirect_uri": ruri["linkedin"],
        "linkedin_redirect_uri_suggested": _suggest("linkedin"),
        "tiktok_configured":    bool(tt_cid and tt_csec),
        "tiktok_client_id":     _mask(tt_cid),
        "tiktok_scopes":        tt_scopes,
        "tiktok_redirect_uri":  ruri["tiktok"],
        "tiktok_redirect_uri_suggested": _suggest("tiktok"),
        "pinterest_configured": bool(pin_cid and pin_csec),
        "pinterest_client_id":  _mask(pin_cid),
        "pinterest_scopes":     pin_scopes,
        "pinterest_redirect_uri": ruri["pinterest"],
        "pinterest_redirect_uri_suggested": _suggest("pinterest"),
        "reddit_configured":    bool(red_cid and red_csec),
        "reddit_client_id":     _mask(red_cid),
        "reddit_scopes":        red_scopes,
        "reddit_redirect_uri":  ruri["reddit"],
        "reddit_redirect_uri_suggested": _suggest("reddit"),
        "snapchat_configured":  bool(snap_cid and snap_csec),
        "snapchat_client_id":   _mask(snap_cid),
        "snapchat_scopes":      snap_scopes,
        "snapchat_redirect_uri": ruri["snapchat"],
        "snapchat_redirect_uri_suggested": _suggest("snapchat"),
        "twitch_configured":    bool(twitch_cid and twitch_csec),
        "twitch_client_id":     _mask(twitch_cid),
        "twitch_scopes":        twit_scopes,
        "twitch_redirect_uri":  ruri["twitch"],
        "twitch_redirect_uri_suggested": _suggest("twitch"),
        # Marketplaces
        "ebay_configured":      bool(ebay_cid and ebay_csec),
        "ebay_client_id":       _mask(ebay_cid),
        "ebay_scopes":          ebay_scopes,
        "ebay_redirect_uri":    ruri["ebay"],
        "ebay_redirect_uri_suggested": _suggest("ebay"),
        "etsy_configured":      bool(etsy_cid and etsy_csec),
        "etsy_client_id":       _mask(etsy_cid),
        "etsy_scopes":          etsy_scopes,
        "etsy_redirect_uri":    ruri["etsy"],
        "etsy_redirect_uri_suggested": _suggest("etsy"),
        "shopify_configured":   bool(shop_cid and shop_csec),
        "shopify_client_id":    _mask(shop_cid),
        "shopify_scopes":       shop_scopes,
        "shopify_redirect_uri": ruri["shopify"],
        "shopify_redirect_uri_suggested": _suggest("shopify"),
        "amazon_configured":    bool(amz_cid and amz_csec),
        "amazon_client_id":     _mask(amz_cid),
        "amazon_scopes":        amz_scopes,
        "amazon_redirect_uri":  ruri["amazon"],
        "amazon_redirect_uri_suggested": _suggest("amazon"),
        # Channels (admin enable/disable; per-agent creds live in Connections)
        "telegram_configured":       channel_enabled.get("telegram", False),
        # Agent Tools — generic catalog-driven map {ability_id: enabled}. The
        # admin ability table loads its toggles from this, so newly dropped-in
        # abilities reflect their saved state with no per-ability wiring. The
        # individual `<id>_configured` keys below are kept for older readers.
        "abilities":                 ability_enabled,
        # Agent Tools (host-side privileged capabilities)
        "codebase_admin_configured":   ability_enabled.get("codebase_admin", False),
        "git_control_configured":      ability_enabled.get("git_control", False),
        "ui_admin_configured":         ability_enabled.get("ui_admin", False),
        "terminal_control_configured": ability_enabled.get("terminal_control", False),
        "create_tools_configured":     ability_enabled.get("create_tools", False),
        "automation_configured":       ability_enabled.get("automation", False),
        "web_access_configured":       ability_enabled.get("web_access", False),
        "browser_control_configured":  ability_enabled.get("browser_control", False),
        "image_generation_configured": ability_enabled.get("image_generation", False),
        "visualizer_configured":       ability_enabled.get("visualizer", False),
        "agent_orchestration_configured": ability_enabled.get("agent_orchestration", False),
        "diagnostics_configured":         ability_enabled.get("diagnostics", False),
        "agent_management_configured":    ability_enabled.get("agent_management", False),
        "app_control_configured":         ability_enabled.get("app_control", False),
        "wiki_control_configured":         ability_enabled.get("wiki_control", False),
        "image_vision_configured":         ability_enabled.get("image_vision", False),
        # Session Namer is an APP FUNCTION, not an agent ability — it's excluded
        # from the `abilities` map above, so resolve its legacy reader key from
        # the app-level gate instead (never a stale False).
        "session_titler_configured":       _app_function_enabled("session_titler"),
    }


@router.post("/google")
async def save_google_config(
    req: OAuthConfigRequest,
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Save Google OAuth credentials (admin). Stored in auth_elements table."""
    from app.db import get_db
    db = get_db()
    cfg: dict = {"client_id": req.client_id.strip()}
    if req.scopes is not None:
        cfg["enabled_scopes"] = req.scopes
    _apply_redirect_uri(cfg, "google", req.redirect_uri)
    await db.auth_element_set(
        user_id=_ADMIN_USER,
        service="google_oauth_config",
        config=cfg,
        secret_ref=req.client_secret.strip(),
        label="default",
    )
    logger.info("Google OAuth config saved by admin")
    return {"status": "ok", "message": "Google OAuth configured."}


@router.delete("/google")
async def delete_google_config(
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Remove Google OAuth credentials (admin). Does not revoke user tokens."""
    from app.db import get_db
    db = get_db()
    deleted = await db.auth_element_delete(_ADMIN_USER, "google_oauth_config", "default")
    logger.info("Google OAuth config removed by admin (deleted=%s)", deleted)
    return {"status": "ok", "deleted": deleted}


@router.post("/microsoft")
async def save_microsoft_config(
    req: OAuthConfigRequest,
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Save Microsoft OAuth credentials (admin)."""
    from app.db import get_db
    db = get_db()
    cfg: dict = {"client_id": req.client_id.strip()}
    if req.scopes is not None:
        cfg["enabled_scopes"] = req.scopes
    _apply_redirect_uri(cfg, "microsoft", req.redirect_uri)
    await db.auth_element_set(
        user_id=_ADMIN_USER,
        service="microsoft_oauth_config",
        config=cfg,
        secret_ref=req.client_secret.strip(),
        label="default",
    )
    logger.info("Microsoft OAuth config saved by admin")
    return {"status": "ok", "message": "Microsoft OAuth configured."}


@router.delete("/microsoft")
async def delete_microsoft_config(
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Remove Microsoft OAuth credentials (admin)."""
    from app.db import get_db
    db = get_db()
    deleted = await db.auth_element_delete(_ADMIN_USER, "microsoft_oauth_config", "default")
    logger.info("Microsoft OAuth config removed by admin (deleted=%s)", deleted)
    return {"status": "ok", "deleted": deleted}


@router.post("/yahoo")
async def save_yahoo_config(
    req: OAuthConfigRequest,
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Save Yahoo OAuth credentials (admin)."""
    from app.db import get_db
    db = get_db()
    cfg: dict = {"client_id": req.client_id.strip()}
    if req.scopes is not None:
        cfg["enabled_scopes"] = req.scopes
    _apply_redirect_uri(cfg, "yahoo", req.redirect_uri)
    await db.auth_element_set(
        user_id=_ADMIN_USER,
        service="yahoo_oauth_config",
        config=cfg,
        secret_ref=req.client_secret.strip(),
        label="default",
    )
    logger.info("Yahoo OAuth config saved by admin")
    return {"status": "ok", "message": "Yahoo OAuth configured."}


@router.delete("/yahoo")
async def delete_yahoo_config(
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Remove Yahoo OAuth credentials (admin)."""
    from app.db import get_db
    db = get_db()
    deleted = await db.auth_element_delete(_ADMIN_USER, "yahoo_oauth_config", "default")
    logger.info("Yahoo OAuth config removed by admin (deleted=%s)", deleted)
    return {"status": "ok", "deleted": deleted}


@router.post("/dropbox")
async def save_dropbox_config(
    req: OAuthConfigRequest,
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Save Dropbox OAuth credentials (admin)."""
    from app.db import get_db
    db = get_db()
    cfg: dict = {"client_id": req.client_id.strip()}
    if req.scopes is not None:
        cfg["enabled_scopes"] = req.scopes
    _apply_redirect_uri(cfg, "dropbox", req.redirect_uri)
    await db.auth_element_set(
        user_id=_ADMIN_USER,
        service="dropbox_oauth_config",
        config=cfg,
        secret_ref=req.client_secret.strip(),
        label="default",
    )
    logger.info("Dropbox OAuth config saved by admin")
    return {"status": "ok", "message": "Dropbox OAuth configured."}


@router.delete("/dropbox")
async def delete_dropbox_config(
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Remove Dropbox OAuth credentials (admin)."""
    from app.db import get_db
    db = get_db()
    deleted = await db.auth_element_delete(_ADMIN_USER, "dropbox_oauth_config", "default")
    logger.info("Dropbox OAuth config removed by admin (deleted=%s)", deleted)
    return {"status": "ok", "deleted": deleted}


# ── Social media admin CRUD endpoints ────────────────────────────────────

def _make_social_endpoints(provider: str, config_key: str, label: str):
    """Factory — generates save + delete admin endpoints for a social provider."""

    async def _save(
        req: OAuthConfigRequest,
        authorization: Optional[str] = Header(None),
        token: Optional[str] = Query(None),
    ):
        from app.db import get_db
        db = get_db()
        cfg: dict = {"client_id": req.client_id.strip()}
        if req.scopes is not None:
            cfg["enabled_scopes"] = req.scopes
        _apply_redirect_uri(cfg, provider, req.redirect_uri)
        await db.auth_element_set(
            user_id=_ADMIN_USER,
            service=config_key,
            config=cfg,
            secret_ref=req.client_secret.strip(),
            label="default",
        )
        logger.info("%s OAuth config saved by admin", label)
        return {"status": "ok", "message": f"{label} OAuth configured."}

    async def _delete(
        authorization: Optional[str] = Header(None),
        token: Optional[str] = Query(None),
    ):
        from app.db import get_db
        db = get_db()
        deleted = await db.auth_element_delete(_ADMIN_USER, config_key, "default")
        logger.info("%s OAuth config removed by admin (deleted=%s)", label, deleted)
        return {"status": "ok", "deleted": deleted}

    return _save, _delete


_meta_save, _meta_delete         = _make_social_endpoints("meta",       "meta_oauth_config",      "Meta")
_tw_save,   _tw_delete           = _make_social_endpoints("twitter",    "twitter_oauth_config",   "Twitter/X")
_li_save,   _li_delete           = _make_social_endpoints("linkedin",   "linkedin_oauth_config",  "LinkedIn")
_tt_save,   _tt_delete           = _make_social_endpoints("tiktok",     "tiktok_oauth_config",    "TikTok")
_pin_save,  _pin_delete          = _make_social_endpoints("pinterest",  "pinterest_oauth_config", "Pinterest")
_red_save,  _red_delete          = _make_social_endpoints("reddit",     "reddit_oauth_config",    "Reddit")
_snap_save, _snap_delete         = _make_social_endpoints("snapchat",   "snapchat_oauth_config",  "Snapchat")
_twitch_save, _twitch_delete     = _make_social_endpoints("twitch",     "twitch_oauth_config",    "Twitch")
_ebay_save,   _ebay_delete       = _make_social_endpoints("ebay",       "ebay_oauth_config",      "eBay")
_etsy_save,   _etsy_delete       = _make_social_endpoints("etsy",       "etsy_oauth_config",      "Etsy")
_shop_save,   _shop_delete       = _make_social_endpoints("shopify",    "shopify_oauth_config",   "Shopify")
_amz_save,    _amz_delete        = _make_social_endpoints("amazon",     "amazon_oauth_config",    "Amazon SP-API")

router.post("/meta",      response_model=None)(_meta_save)
router.delete("/meta",    response_model=None)(_meta_delete)
router.post("/twitter",   response_model=None)(_tw_save)
router.delete("/twitter", response_model=None)(_tw_delete)
router.post("/linkedin",  response_model=None)(_li_save)
router.delete("/linkedin",response_model=None)(_li_delete)
router.post("/tiktok",    response_model=None)(_tt_save)
router.delete("/tiktok",  response_model=None)(_tt_delete)
router.post("/pinterest", response_model=None)(_pin_save)
router.delete("/pinterest",response_model=None)(_pin_delete)
router.post("/reddit",    response_model=None)(_red_save)
router.delete("/reddit",  response_model=None)(_red_delete)
router.post("/snapchat",  response_model=None)(_snap_save)
router.delete("/snapchat",response_model=None)(_snap_delete)
router.post("/twitch",    response_model=None)(_twitch_save)
router.delete("/twitch",  response_model=None)(_twitch_delete)
router.post("/ebay",      response_model=None)(_ebay_save)
router.delete("/ebay",    response_model=None)(_ebay_delete)
router.post("/etsy",      response_model=None)(_etsy_save)
router.delete("/etsy",    response_model=None)(_etsy_delete)
router.post("/shopify",   response_model=None)(_shop_save)
router.delete("/shopify", response_model=None)(_shop_delete)
router.post("/amazon",    response_model=None)(_amz_save)
router.delete("/amazon",  response_model=None)(_amz_delete)


# Web Scraper (its own ability) and the cookie-replay tools (folded into Browser
# Control) declare their credentials in their descriptors; those creds are
# saved/read/deleted through the generic endpoints in app/api/agents.py
# (GET/POST/DELETE /api/v1/abilities/{id}/credentials) backed by
# app/abilities/credentials.py. The old bespoke /scraper and /browser-session
# endpoints were removed.


# ── Channels (admin enable/disable) ───────────────────────────────────────

async def get_channel_enabled_map() -> dict[str, bool]:
    """Return {channel_name: enabled} for every channel in `_CHANNEL_CONFIG_KEY`."""
    try:
        from app.db import get_db
        db = get_db()
    except Exception as e:
        logger.debug("Failed to import db while reading channel states: %s", e)
        return {ct: False for ct in _CHANNEL_CONFIG_KEY}
    out: dict[str, bool] = {}
    for ct, service_key in _CHANNEL_CONFIG_KEY.items():
        try:
            elem = await db.auth_element_get(_ADMIN_USER, service_key, "default")
            out[ct] = bool(elem and elem.get("secret_ref"))
        except Exception as e:
            logger.debug("auth_element_get failed for channel %s: %s", service_key, e)
            out[ct] = False
    return out


async def seed_default_abilities() -> dict:
    """First-boot bootstrap of the admin ability config file (idempotent).

    The on/off state now lives in data/config/agent-abilities.json. On first
    boot (file absent) `ensure_bootstrapped` seeds it from any existing vault
    rows — preserving an upgrading admin's configuration — or from each
    ability's descriptor `default_enabled` on a brand-new install. Once the file
    exists this is a no-op, so admin disables persist across restarts.
    """
    try:
        from app.db import get_db
        db = get_db()
    except Exception as e:
        logger.debug("seed_default_abilities: db unavailable: %s", e)
        return {"seeded": False, "reason": "no-db"}

    from app.admin import ability_config
    already = ability_config.file_exists()
    await ability_config.ensure_bootstrapped(db)
    if already:
        return {"seeded": False, "reason": "already-seeded"}
    return {"seeded": True, "enabled": sorted(ability_config.all_ability_states().keys())}


async def get_ability_enabled_map() -> dict[str, bool]:
    """Return {ability_id: enabled} for every kind="ability" drop-in in the catalog."""
    from app import abilities as abilities_catalog
    try:
        from app.db import get_db
        db = get_db()
    except Exception as e:
        logger.debug("Failed to import db while reading ability states: %s", e)
        return {ab: abilities_catalog.ability_default_enabled(ab)
                for ab in abilities_catalog.ability_ids("ability")}
    out: dict[str, bool] = {}
    for ab in abilities_catalog.ability_ids("ability"):
        out[ab] = await _ability_app_enabled(db, ab)
    return out


async def is_ability_enabled_for_agent(agent_id: str, ability: str) -> bool:
    """True iff the ability is enabled both at app level AND for this specific agent."""
    from app import abilities as abilities_catalog
    if not abilities_catalog.is_toggleable_ability(ability):
        return False
    try:
        from app.db import get_db
        db = get_db()
        if not await _ability_app_enabled(db, ability):
            return False
        rows = await db.get_agent_connections(agent_id)
        match = next(
            (r for r in rows
             if r.get("connection_type") == ability and r.get("section") == "ability"),
            None,
        )
        return bool(match and match.get("enabled"))
    except Exception as e:
        logger.debug("is_ability_enabled_for_agent(%s, %s) failed: %s", agent_id, ability, e)
        return False


# ── OAuth Ability admin endpoints ─────────────────────────────────────────

class OAuthAbilityPolicyRequest(BaseModel):
    mode: str                             # disabled | platform_only | byo_only | both
    platform_scopes: Optional[list[str]] = None
    max_byo_scopes:  Optional[list[str]] = None


@router.get("/oauth-abilities")
async def list_oauth_abilities(
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Return every registered OAuth ability with its admin policy.

    Response shape mirrors the registry: one entry per ability with the
    current `mode`, configured `platform_scopes`, and `max_byo_scopes`.
    Agent admin UI uses this to know which abilities are toggleable per agent.
    """
    from app.integrations.ability_registry import ABILITIES
    out = []
    for ab in ABILITIES.values():
        policy = await get_oauth_ability_config(ab.id)
        out.append({
            "id": ab.id,
            "provider": ab.provider,
            "display_name": ab.display_name,
            "description": ab.description,
            "scopes": list(ab.scopes),
            "implicit": ab.implicit,
            "mode": policy["mode"],
            "platform_scopes": policy["platform_scopes"],
            "max_byo_scopes": policy["max_byo_scopes"],
        })
    return {"abilities": out}


@router.post("/oauth-abilities/{ability_id}")
async def set_oauth_ability_policy(
    ability_id: str,
    req: OAuthAbilityPolicyRequest,
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Set the admin policy for a single OAuth ability."""
    from app.integrations.ability_registry import get_ability
    ab = get_ability(ability_id)
    if not ab:
        raise HTTPException(status_code=404, detail=f"Unknown OAuth ability: {ability_id}")
    if req.mode not in _OAUTH_ABILITY_MODES:
        raise HTTPException(status_code=400, detail=f"mode must be one of {_OAUTH_ABILITY_MODES}")
    # Caps must be a subset of the ability's registered scopes — admin can't
    # invent new scopes by editing this endpoint.
    allowed = set(ab.scopes)
    platform = [s for s in (req.platform_scopes or list(ab.scopes)) if s in allowed]
    max_byo = [s for s in (req.max_byo_scopes or list(ab.scopes)) if s in allowed]
    from app.db import get_db
    db = get_db()
    await db.auth_element_set(
        user_id=_ADMIN_USER,
        service=_oauth_ability_service(ability_id),
        config={"mode": req.mode, "platform_scopes": platform, "max_byo_scopes": max_byo},
        secret_ref="set",
        label="default",
    )
    logger.info("OAuth ability %s set to mode=%s", ability_id, req.mode)
    return {"status": "ok", "ability_id": ability_id, "mode": req.mode,
            "platform_scopes": platform, "max_byo_scopes": max_byo}


@router.delete("/oauth-abilities/{ability_id}")
async def reset_oauth_ability_policy(
    ability_id: str,
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Delete the admin policy — reverts the ability to its registry defaults."""
    from app.db import get_db
    db = get_db()
    deleted = await db.auth_element_delete(_ADMIN_USER, _oauth_ability_service(ability_id), "default")
    return {"status": "ok", "ability_id": ability_id, "deleted": deleted}


# ── Global per-tool defaults admin endpoints ──────────────────────────────

class GlobalToolDefaultRequest(BaseModel):
    permission: Optional[str] = None   # auto | ask | deny | null (clear)
    visibility: Optional[str] = None   # always | discoverable | null (clear)


async def _read_tool_defaults_blob() -> dict:
    """Read the full ``{"tools": {...}}`` blob (defaulting to an empty tools map).

    Backed by data/config/agent-abilities.json (`tools` section)."""
    from app.admin import ability_config
    try:
        from app.db import get_db
        await ability_config.ensure_bootstrapped(get_db())
    except Exception:
        pass
    return {"tools": ability_config.get_tools()}


async def _write_tool_defaults_blob(blob: dict) -> None:
    from app.admin import ability_config
    tools = blob.get("tools") if isinstance(blob, dict) else None
    ability_config.set_tools(tools if isinstance(tools, dict) else {})


@router.get("/tool-defaults")
async def get_tool_defaults_endpoint(
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Return the app-wide per-tool defaults blob.

    Shape: ``{"tools": {"<tool>": {"permission": ..., "visibility": ...}}}`` —
    an empty ``tools`` map when nothing has been set. These are the DEFAULTS
    every agent inherits unless it has its own per-tool override.

    ``confirm_tools`` lists every tool that inherently pauses for confirmation
    (its built-in destructive/requires_confirmation nature) so the admin panel can
    show those at their true "Ask" floor and lock the looser side, rather than
    misreporting them as "Auto"."""
    tools = await get_global_tool_defaults()
    try:
        from app.abilities import confirm_gated_tools
        confirm = sorted(confirm_gated_tools())
    except Exception:
        confirm = []
    return {"tools": tools, "confirm_tools": confirm}


@router.put("/tool-defaults/{tool_name}")
async def set_tool_default_endpoint(
    tool_name: str,
    req: GlobalToolDefaultRequest,
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Set/clear a tool's app-wide default for either or both dimensions.

    A provided value sets that dimension; a null value clears it. If both
    dimensions end up empty the whole tool key is dropped. Read-modify-write the
    single blob.

    Validation: permission ∈ {auto,ask,deny}; visibility ∈ TOGGLEABLE_MODES;
    visibility is rejected for locked (core) tools. Explicit deny is accepted for
    every tool and is enforced before any model-visible catalog is constructed."""
    from app.tools.tool_modes import TOGGLEABLE_MODES, is_locked
    from app.tools.tool_defaults import PERMISSION_VALUES

    if req.permission is not None and req.permission not in PERMISSION_VALUES:
        raise HTTPException(
            status_code=400,
            detail=f"permission must be one of {PERMISSION_VALUES} or null.",
        )
    if req.visibility is not None:
        if is_locked(tool_name):
            raise HTTPException(
                status_code=400,
                detail=f"'{tool_name}' is a core tool — its visibility cannot be changed.",
            )
        if req.visibility not in TOGGLEABLE_MODES:
            raise HTTPException(
                status_code=400,
                detail=f"visibility must be one of {TOGGLEABLE_MODES} or null.",
            )
    blob = await _read_tool_defaults_blob()
    entry = dict(blob["tools"].get(tool_name) or {})

    # A provided value sets that dimension; null clears it.
    if req.permission is not None:
        entry["permission"] = req.permission
    else:
        entry.pop("permission", None)
    if req.visibility is not None:
        entry["visibility"] = req.visibility
    else:
        entry.pop("visibility", None)

    if entry:
        blob["tools"][tool_name] = entry
    else:
        blob["tools"].pop(tool_name, None)

    await _write_tool_defaults_blob(blob)
    logger.info(
        "Global tool default set: %s permission=%s visibility=%s",
        tool_name, req.permission, req.visibility,
    )
    return {"status": "ok", "tool": tool_name,
            "permission": entry.get("permission"),
            "visibility": entry.get("visibility")}


@router.delete("/tool-defaults")
async def clear_all_tool_defaults_endpoint(
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Clear ALL app-wide per-tool defaults (empty the `tools` section)."""
    from app.admin import ability_config
    had = bool(ability_config.get_tools())
    ability_config.set_tools({})
    return {"status": "ok", "deleted": had}


@router.delete("/tool-defaults/{tool_name}")
async def clear_tool_default_endpoint(
    tool_name: str,
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Clear the app-wide default for a single tool (pop its key, RMW the blob)."""
    blob = await _read_tool_defaults_blob()
    existed = tool_name in blob["tools"]
    blob["tools"].pop(tool_name, None)
    await _write_tool_defaults_blob(blob)
    return {"status": "ok", "tool": tool_name, "deleted": existed}


@router.post("/abilities/{ability}")
async def enable_ability(
    ability: str,
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Enable an agent ability (admin). Once enabled here, agent admins can
    turn it on per-agent in the Abilities tab. Catalog-driven — any kind="ability"
    drop-in is accepted; the choice persists to data/config/agent-abilities.json."""
    from app import abilities as abilities_catalog
    from app.admin import ability_config
    if not abilities_catalog.is_toggleable_ability(ability):
        raise HTTPException(status_code=404, detail=f"Unknown ability: {ability}")
    from app.db import get_db
    db = get_db()
    await ability_config.ensure_bootstrapped(db)
    ability_config.set_ability_enabled(ability, True)
    logger.info("Ability %s enabled by admin", ability)
    return {"status": "ok", "ability": ability, "enabled": True}


@router.delete("/abilities/{ability}")
async def disable_ability(
    ability: str,
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Disable an agent ability (admin). Hides it from the agent Abilities
    tab. Existing per-agent `agent_connections` rows stay so they reactivate
    if the ability is re-enabled — except for `automation`, which purges all
    per-agent automation tasks, event subscriptions, and ability toggles.
    Catalog-driven; the off choice persists to data/config/agent-abilities.json."""
    from app import abilities as abilities_catalog
    from app.admin import ability_config
    if not abilities_catalog.is_toggleable_ability(ability):
        raise HTTPException(status_code=404, detail=f"Unknown ability: {ability}")
    # A locked-on safety ability (e.g. Context Control) cannot be turned off.
    if abilities_catalog.ability_is_locked_on(ability):
        raise HTTPException(
            status_code=400,
            detail="This is a safety device and cannot be deactivated.",
        )
    from app.db import get_db
    db = get_db()
    await ability_config.ensure_bootstrapped(db)
    ability_config.set_ability_enabled(ability, False)
    if ability == "automation":
        # Wipe every agent's automation data so the feature truly resets.
        tasks_removed = await db.delete_all_automations()
        subs_removed = await db.delete_all_event_subscriptions()
        toggles_removed = await db.delete_all_ability_connections("automation")
        logger.info(
            "Automation ability disabled: purged %s tasks, %s subscriptions, %s per-agent toggles",
            tasks_removed, subs_removed, toggles_removed,
        )
    logger.info("Ability %s disabled by admin", ability)
    return {"status": "ok", "ability": ability, "enabled": False}


class AbilityOrderBody(BaseModel):
    groups: Optional[dict] = None      # {group_id: order_int}
    abilities: Optional[dict] = None   # {ability_id: order_int}


@router.get("/abilities/order")
async def get_ability_order(
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """The admin-editable display order of the ability table — group order and
    per-ability order — read from data/config/agent-abilities.json. Seeded from
    the drop-in descriptors on first boot; this file is then the live source."""
    from app.admin import ability_config
    from app.db import get_db
    await ability_config.ensure_bootstrapped(get_db())
    return {"status": "ok", **ability_config.get_order()}


@router.put("/abilities/order")
async def set_ability_order(
    body: AbilityOrderBody,
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Persist a new display order for the ability table (groups and/or
    abilities) to data/config/agent-abilities.json. Either map may be omitted to
    leave that side unchanged. Takes effect on the next catalog fetch — both
    ability panels render from the same /abilities/catalog payload, which now
    honours these overrides."""
    from app.admin import ability_config
    from app.db import get_db
    await ability_config.ensure_bootstrapped(get_db())
    ability_config.set_order(groups=body.groups, abilities=body.abilities)
    logger.info(
        "Ability table order updated by admin (%s groups, %s abilities)",
        len(body.groups) if body.groups else 0,
        len(body.abilities) if body.abilities else 0,
    )
    return {"status": "ok", **ability_config.get_order()}


# ── UI page configuration (main header tabs + Admin Tools sidebar views) ──────
# Catalog-driven, mirroring the ability order/toggle routes above. Overrides
# persist to data/config/main-panel-pages.json (scope=main) and
# admin-panel-pages.json (scope=admin). Page DEFINITIONS are drop-in page.json
# descriptors under ui/ — never edited here.

def _page_scope_or_404(scope: str) -> str:
    if scope not in ("main", "admin"):
        raise HTTPException(status_code=404, detail=f"Unknown page scope: {scope}")
    return scope


class PageOrderBody(BaseModel):
    pages: dict  # {page_id: order_int}


class PageOverrideBody(BaseModel):
    label: Optional[str] = None    # rename; "" / null clears the override
    icon: Optional[str] = None     # custom Lucide icon; "" / null clears
    hidden: Optional[bool] = None  # legacy: hide from the strip (== visibility "off")
    visibility: Optional[str] = None  # "all" | "auth" | "off"; "all" clears the override


@router.put("/pages/{scope}/order")
async def set_pages_order(
    scope: str,
    body: PageOrderBody,
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Persist a new display order for one page strip ('main' header tabs or
    'admin' sidebar views) to data/config/<scope>-panel-pages.json. Takes effect
    on the next /api/v1/pages/catalog fetch."""
    _page_scope_or_404(scope)
    from app.admin import page_config
    page_config.set_order(scope, body.pages or {})
    logger.info("Page order updated by admin (scope=%s, %d pages)", scope, len(body.pages or {}))
    return {"status": "ok", "scope": scope, "order": page_config.get_order(scope)}


@router.post("/pages/{scope}/{page_id}")
async def set_page_override(
    scope: str,
    page_id: str,
    body: PageOverrideBody,
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Set per-page overrides (rename, custom icon, visibility) for one page. Only
    the fields present in the body are applied; passing an empty string / null
    for label or icon clears that override (reverting to the descriptor default).
    A locked page (Admin Tools / Admin Configuration) can never be turned "off" —
    that would lock the user out of the app's configuration — so an attempt to set
    it off (via `visibility:"off"` or the legacy `hidden:true`) is rejected; it may
    still be set to "auth" (signed-in only) or "all" (always)."""
    scope = _page_scope_or_404(scope)
    from app.ui_pages import is_known_page, page_entry
    if not is_known_page(scope, page_id):
        raise HTTPException(status_code=404, detail=f"Unknown page: {page_id}")
    is_locked = bool((page_entry(scope, page_id) or {}).get("locked"))
    from app.admin import page_config
    kwargs = {}
    fields = body.model_dump(exclude_unset=True)
    if "label" in fields:
        kwargs["label"] = fields["label"]
    if "icon" in fields:
        kwargs["icon"] = fields["icon"]
    if "hidden" in fields:
        if is_locked and bool(fields["hidden"]):
            raise HTTPException(status_code=400, detail="This page cannot be hidden.")
        kwargs["hidden"] = fields["hidden"]
    if "visibility" in fields:
        vis = fields["visibility"]
        if vis not in ("all", "auth", "off"):
            raise HTTPException(status_code=400,
                                detail="visibility must be one of: all, auth, off")
        if is_locked and vis == "off":
            raise HTTPException(status_code=400, detail="This page cannot be hidden.")
        kwargs["visibility"] = vis
    page_config.set_page(scope, page_id, **kwargs)
    logger.info("Page override set by admin (scope=%s, page=%s, fields=%s)",
                scope, page_id, list(kwargs.keys()))
    return {"status": "ok", "scope": scope, "page": page_id,
            "override": page_config.get_overrides(scope).get(page_id, {})}


@router.post("/channels/{channel}")
async def enable_channel(
    channel: str,
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Enable a communication channel (admin). Per-agent credentials still
    live in the agent's Connections tab — this just makes the channel
    available to agent admins and starts pollers for existing connections."""
    if channel not in _CHANNEL_CONFIG_KEY:
        return {"status": "error", "message": f"Unknown channel: {channel}"}
    from app.db import get_db
    db = get_db()
    await db.auth_element_set(
        user_id=_ADMIN_USER,
        service=_CHANNEL_CONFIG_KEY[channel],
        config={"enabled": True},
        secret_ref="enabled",
        label="default",
    )
    logger.info("Channel %s enabled by admin", channel)
    # Spin up pollers for any existing per-agent connections so the channel
    # works immediately without waiting for a restart.
    try:
        from app.communications.manager import get_plugin_manager
        pm = get_plugin_manager()
        if hasattr(pm, "reload_agent_connections"):
            import asyncio
            asyncio.create_task(pm.reload_agent_connections())
    except Exception as e:
        logger.debug("reload_agent_connections after enable failed: %s", e)
    return {"status": "ok", "channel": channel, "enabled": True}


@router.delete("/channels/{channel}")
async def disable_channel(
    channel: str,
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Disable a communication channel (admin). Stops all active pollers for
    the channel and hides it from the agent page. Existing per-agent rows
    stay in the DB so they reactivate if the channel is re-enabled."""
    if channel not in _CHANNEL_CONFIG_KEY:
        return {"status": "error", "message": f"Unknown channel: {channel}"}
    from app.db import get_db
    db = get_db()
    deleted = await db.auth_element_delete(_ADMIN_USER, _CHANNEL_CONFIG_KEY[channel], "default")
    logger.info("Channel %s disabled by admin (deleted=%s)", channel, deleted)
    # Tear down any running pollers for this channel so messages stop arriving.
    stopped = 0
    try:
        from app.communications.manager import get_plugin_manager
        pm = get_plugin_manager()
        if hasattr(pm, "stop_channel_pollers"):
            stopped = await pm.stop_channel_pollers(channel)
    except Exception as e:
        logger.warning("stop_channel_pollers(%s) failed: %s", channel, e)
    return {"status": "ok", "channel": channel, "enabled": False, "pollers_stopped": stopped}
