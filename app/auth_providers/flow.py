"""OAuth mechanics for social sign-in: authorize URL, code exchange, profile.

Generic for any standard OAuth2 / OpenID Connect provider (Google, Microsoft,
GitHub). A provider whose behaviour differs ships hooks in its `<id>.py`:

  • `build_client_secret(*, descriptor, creds) -> str`  (Apple signs a JWT)
  • `extract_profile(*, descriptor, token_data, access_token, http, creds, **ctx)`
    (GitHub fetches the primary email; Apple reads the id_token)

State is a short-lived JWT signed with the app's own secret (`app.auth.jwt`), so
every token in the app shares one key. It carries no user id — this is a login,
there is no user yet.
"""

from __future__ import annotations

import inspect
import logging
import secrets as _secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
import jwt

from app.auth.jwt import get_secret
from app.auth_providers import registry

logger = logging.getLogger(__name__)

_ALG = "HS256"
_STATE_PURPOSE = "social_login"


# ── State token ──────────────────────────────────────────────────────────────

def make_state(provider_id: str, *, device_id: str = "") -> str:
    payload = {
        "purpose": _STATE_PURPOSE,
        "provider": provider_id,
        "nonce": _secrets.token_hex(8),
        "device_id": device_id,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=10),
    }
    return jwt.encode(payload, get_secret(), algorithm=_ALG)


def read_state(state: str, provider_id: str) -> bool:
    return read_state_payload(state, provider_id) is not None


def read_state_payload(state: str, provider_id: str) -> dict | None:
    try:
        p = jwt.decode(state, get_secret(), algorithms=[_ALG])
    except Exception:
        return None
    if p.get("purpose") != _STATE_PURPOSE or p.get("provider") != provider_id:
        return None
    return p


# ── Authorize URL ────────────────────────────────────────────────────────────

def build_authorize_url(provider_id: str, redirect_uri: str, creds: dict, state: str) -> str:
    d = registry.get_descriptor(provider_id) or {}
    client_id = creds.get(registry.client_id_field(provider_id), "")
    scopes = d.get("scopes") or []
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": (d.get("scope_sep") or " ").join(scopes),
        "state": state,
        **(d.get("authorize_extra") or {}),
    }
    return f"{d.get('authorize_url', '')}?{urlencode(params)}"


# ── Token exchange ───────────────────────────────────────────────────────────

def _client_secret(provider_id: str, creds: dict) -> str:
    """The client_secret to send — a stored string, or one computed by the hook."""
    mod = registry.get_module(provider_id)
    if mod is not None and hasattr(mod, "build_client_secret"):
        try:
            d = registry.get_descriptor(provider_id) or {}
            return mod.build_client_secret(descriptor=d, creds=creds) or ""
        except Exception as e:  # noqa: BLE001
            logger.warning("%s build_client_secret failed: %s", provider_id, e)
            return ""
    return creds.get("client_secret", "")


async def exchange_code(provider_id: str, code: str, redirect_uri: str,
                        creds: dict, http: httpx.AsyncClient) -> dict:
    d = registry.get_descriptor(provider_id) or {}
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": creds.get(registry.client_id_field(provider_id), ""),
        "client_secret": _client_secret(provider_id, creds),
    }
    headers = {"Accept": "application/json"}
    resp = await http.post(d.get("token_url", ""), data=data, headers=headers)
    if resp.status_code != 200:
        logger.error("%s token exchange failed %s: %s",
                     provider_id, resp.status_code, resp.text[:300])
        raise RuntimeError(f"token exchange failed ({resp.status_code})")
    try:
        return resp.json()
    except Exception:
        # A few providers return form-encoded when Accept is ignored.
        from urllib.parse import parse_qs
        return {k: v[0] for k, v in parse_qs(resp.text).items()}


# ── Profile ──────────────────────────────────────────────────────────────────

async def fetch_profile(provider_id: str, token_data: dict, creds: dict,
                        http: httpx.AsyncClient, *, form: dict | None = None) -> dict:
    """Return {external_id, email, name, avatar}."""
    d = registry.get_descriptor(provider_id) or {}
    access_token = token_data.get("access_token", "")
    mod = registry.get_module(provider_id)
    if mod is not None and hasattr(mod, "extract_profile"):
        fn = mod.extract_profile
        kwargs = dict(descriptor=d, token_data=token_data,
                      access_token=access_token, http=http, creds=creds)
        # Pass `form` only to hooks that accept it (Apple), keep others clean.
        try:
            sig = inspect.signature(fn)
            if "form" in sig.parameters or any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
            ):
                kwargs["form"] = form or {}
        except (TypeError, ValueError):
            pass
        return await fn(**kwargs)

    # Generic: hit the userinfo endpoint, map fields via profile_map.
    pm = d.get("profile_map") or {}
    info = {}
    url = d.get("userinfo_url") or ""
    if url and access_token:
        try:
            r = await http.get(url, headers={"Authorization": f"Bearer {access_token}",
                                             "Accept": "application/json"})
            if r.status_code == 200:
                info = r.json()
        except Exception as e:  # noqa: BLE001
            logger.warning("%s userinfo fetch failed: %s", provider_id, e)

    def pick(field: str) -> str:
        key = pm.get(field)
        return str(info.get(key, "")) if key else ""

    email = pick("email")
    return {
        "external_id": pick("external_id"),
        "email": email,
        "name": pick("name") or (email.split("@")[0] if email else ""),
        "avatar": pick("avatar"),
    }
