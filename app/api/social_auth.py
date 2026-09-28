"""Social sign-in HTTP endpoints (login/identity, not tool-connections).

Public:
  GET  /api/v1/auth/social/providers            → enabled+configured buttons
Flow:
  GET  /api/v1/auth/social/{id}/start           → 302 to the provider
  GET  /api/v1/auth/social/{id}/callback         → complete login (Apple: POST)
Admin:
  GET  /api/v1/auth/social/admin/providers       → all installed + config state
  PUT  /api/v1/auth/social/admin/providers/{id}   → save enable + credentials

The provider set is discovered from the drop-in tree `plugins/auth_providers/`
via `app/auth_providers`; this router owns only the web wiring + the
account-resolution rule (link by provider id, else by email, else create honoring
the app's access mode).
"""

import json
import logging
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi import Query as QueryParam
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

from app.auth_providers import registry, flow
from app.auth.jwt import create_access_token
from app.auth import users as user_store
from app.db import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/auth/social", tags=["social-auth"])


def _base_url(request: Request) -> str:
    from app.admin.integrations import _get_base_url
    return _get_base_url(request).rstrip("/")


def _redirect_uri(request: Request, provider_id: str) -> str:
    return f"{_base_url(request)}/api/v1/auth/social/{provider_id}/callback"


async def _require_admin(request: Request) -> str:
    """Return the caller's user_id iff they are a verified admin, else 403/401."""
    from app.auth.identity import request_user_id
    uid = request_user_id(request)
    if not uid:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        if not await get_db().is_user_admin(uid):
            raise HTTPException(status_code=403, detail="Admin only")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=403, detail="Admin only")
    return uid


# ── Public: which providers to show on the sign-in screen ───────────────────

@router.get("/providers")
async def public_providers():
    """Enabled + configured providers (display-only, no secrets)."""
    return {"providers": await registry.public_view()}


# ── Admin: configure providers ──────────────────────────────────────────────

@router.get("/admin/providers")
async def admin_providers(request: Request):
    await _require_admin(request)
    rows = await registry.admin_view()
    for r in rows:
        r["redirect_uri"] = _redirect_uri(request, r["id"])
    return {"providers": rows, "redirect_base": _base_url(request)}


class SaveProviderRequest(BaseModel):
    enabled: bool = False
    fields: dict = {}


@router.put("/admin/providers/{provider_id}")
async def save_provider(provider_id: str, body: SaveProviderRequest, request: Request):
    await _require_admin(request)
    if registry.get_descriptor(provider_id) is None:
        raise HTTPException(status_code=404, detail="Unknown provider")
    try:
        await registry.save_config(provider_id, enabled=body.enabled, field_values=body.fields or {})
    except Exception as e:  # noqa: BLE001
        logger.warning("save social provider %s failed: %s", provider_id, e)
        raise HTTPException(status_code=500, detail="Could not save provider settings")
    rows = await registry.admin_view()
    row = next((r for r in rows if r["id"] == provider_id), None)
    if row:
        row["redirect_uri"] = _redirect_uri(request, provider_id)
    return {"provider": row}


# ── Flow: start ─────────────────────────────────────────────────────────────

@router.get("/{provider_id}/start")
async def start_login(provider_id: str, request: Request):
    if registry.get_descriptor(provider_id) is None:
        raise HTTPException(status_code=404, detail="Unknown provider")
    if not await registry.is_enabled(provider_id):
        raise HTTPException(status_code=403, detail="This sign-in method is turned off")
    if not await registry.is_configured(provider_id):
        raise HTTPException(status_code=503, detail="This sign-in method is not configured")
    creds = await registry.get_credentials(provider_id)
    redirect_uri = _redirect_uri(request, provider_id)
    device_id = request.query_params.get("device_id", "").strip()
    if device_id and (
        len(device_id) > 128
        or not all(ch.isalnum() or ch in "-_" for ch in device_id)
    ):
        raise HTTPException(status_code=400, detail="Invalid device identifier")
    state = flow.make_state(provider_id, device_id=device_id)
    url = flow.build_authorize_url(provider_id, redirect_uri, creds, state)
    return RedirectResponse(url, status_code=302)


# ── Flow: callback (GET for most; POST for Apple form_post) ──────────────────

@router.get("/{provider_id}/callback")
async def callback_get(provider_id: str, request: Request,
                       code: str = QueryParam(None),
                       state: str = QueryParam(None),
                       error: str = QueryParam(None)):
    return await _complete(provider_id, request, code, state, error, form={})


@router.post("/{provider_id}/callback")
async def callback_post(provider_id: str, request: Request):
    form = {}
    try:
        form = dict(await request.form())
    except Exception:
        form = {}
    return await _complete(
        provider_id, request,
        form.get("code"), form.get("state"), form.get("error"), form=form,
    )


async def _complete(provider_id: str, request: Request, code, state, error, *, form: dict):
    d = registry.get_descriptor(provider_id)
    if d is None:
        return _error_page("Unknown sign-in provider.")
    name = d.get("display_name") or provider_id
    if error:
        return _error_page(f"{name} reported: {error}")
    state_payload = flow.read_state_payload(state, provider_id) if state else None
    if not code or not state_payload:
        return _error_page("Sign-in link expired or invalid. Please try again.")
    if not await registry.is_configured(provider_id):
        return _error_page(f"{name} sign-in is not configured on this server.")

    creds = await registry.get_credentials(provider_id)
    redirect_uri = _redirect_uri(request, provider_id)
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            token_data = await flow.exchange_code(provider_id, code, redirect_uri, creds, client)
            profile = await flow.fetch_profile(provider_id, token_data, creds, client, form=form)
    except Exception as e:  # noqa: BLE001
        logger.warning("%s sign-in exchange failed: %s", provider_id, e)
        return _error_page(f"Could not complete {name} sign-in. Please try again.")

    external_id = (profile.get("external_id") or "").strip()
    email = (profile.get("email") or "").strip().lower()
    display = (profile.get("name") or "").strip()
    if not external_id and not email:
        return _error_page(f"{name} did not return an identity we can sign in with.")

    user, created = await _resolve_account(provider_id, external_id, email, display)
    if user is None:
        return _error_page(
            f"{name} did not share an email address, so a new account could not "
            "be created. Enable email sharing and try again."
        )

    from app.admin.settings import get_access_mode as _gam_social
    if not user.is_approved and _gam_social() == "admin_approval":
        return _pending_page(name)

    device_id = str(state_payload.get("device_id") or "")
    token = create_access_token(
        user.username, user.user_id,
        expires_minutes=user.session_lifetime_minutes,
        device_id=device_id or None,
        reauthenticated=True,
    )
    try:
        from app.auth.device_location import location_for_ip, request_client_ip, request_origin
        from app.auth.jwt import decode_signed_token
        from app.auth.revocation import update_device_metadata

        token_payload = decode_signed_token(token) or {}
        resolved_device_id = str(token_payload.get("device_id") or "")
        ip_address = request_client_ip(request)
        update_device_metadata(
            user.user_id,
            resolved_device_id,
            ip_address=ip_address,
            location=await location_for_ip(ip_address),
            user_agent=request.headers.get("user-agent", ""),
            origin=request_origin(request),
        )
    except Exception:
        pass
    try:
        await get_db().upsert_user_profile(
            user.user_id, last_login_at=datetime.now(timezone.utc).isoformat()
        )
    except Exception:  # noqa: BLE001 — never fail the login on a profile write
        pass
    try:  # feed the Dashboard's Security & sign-ins card (best-effort)
        from app.auth import _record_auth_event
        _record_auth_event("info", f"Social sign-in: {user.username} via {provider_id}",
                           user_id=user.user_id)
    except Exception:  # noqa: BLE001
        pass

    logger.info("Social sign-in: %s via %s (%s account) user=%s",
                email or external_id, provider_id, "new" if created else "existing",
                user.user_id[:12])
    return _success_page(token, user)


async def _resolve_account(provider_id: str, external_id: str, email: str, display: str):
    """(user, created). Link by provider id, else by email, else create.

    New accounts follow the app's access mode: instant in Open Registration,
    pending admin approval in Private.
    """
    # 1. Already linked to this exact provider identity.
    user = await user_store.get_user_by_social(provider_id, external_id) if external_id else None
    if user is not None:
        return user, False

    # 2. An existing account with the same email — link the provider to it.
    if email:
        existing = await user_store.get_user(email)
        if existing is not None:
            if external_id:
                await user_store.link_social(existing.username, provider_id, external_id)
            return existing, False

    # 3. Brand-new account. Needs an email to key the username on.
    if not email:
        return None, False
    from app.admin.settings import get_access_mode
    auto_approve = get_access_mode() != "admin_approval"
    created = await user_store.register_social_user(
        email, display, provider_id, external_id, is_approved=auto_approve
    )
    if created is None:
        # Lost a create race — adopt the now-existing account.
        existing = await user_store.get_user(email)
        if existing is not None and external_id:
            await user_store.link_social(existing.username, provider_id, external_id)
        return existing, False
    return created, True


# ── Result pages ────────────────────────────────────────────────────────────

_PAGE_HEAD = """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>%(title)s</title><meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<style>
 body{background:#0f1117;color:#e6e9f0;font-family:system-ui,-apple-system,sans-serif;
      display:flex;align-items:center;justify-content:center;height:100vh;margin:0;}
 .card{text-align:center;padding:40px;max-width:380px;}
 .icon{font-size:44px;margin-bottom:14px;}
 h2{margin:0 0 8px;font-size:19px;} p{color:#9aa4bf;margin:0;line-height:1.5;font-size:14px;}
</style></head><body><div class="card">"""
_PAGE_FOOT = "</div></body></html>"


def _success_page(token: str, user) -> HTMLResponse:
    payload = json.dumps({
        "type": "social-login-success",
        "access_token": token,
        "user_id": user.user_id,
        "username": user.username,
        "display_name": user.display_name,
    })
    body = (_PAGE_HEAD % {"title": "Signed in"}) + f"""
      <div class="icon">&#10003;</div>
      <h2>Signed in</h2><p>You can close this window.</p>
      <script>
        var data = {payload};
        try {{
          if (window.opener) {{ window.opener.postMessage(data, window.location.origin); }}
        }} catch (e) {{}}
        // Fallback for a full-page (non-popup) flow: store + go to the app.
        if (!window.opener) {{
          try {{
            localStorage.setItem('auth_token', data.access_token);
            localStorage.setItem('auth_user_id', data.user_id);
            localStorage.setItem('auth_username', data.username);
            localStorage.setItem('auth_display_name', data.display_name);
          }} catch (e) {{}}
          window.location.replace('/');
        }} else {{
          setTimeout(function() {{ window.close(); }}, 800);
        }}
      </script>""" + _PAGE_FOOT
    return HTMLResponse(body)


def _pending_page(provider_name: str) -> HTMLResponse:
    body = (_PAGE_HEAD % {"title": "Awaiting approval"}) + f"""
      <div class="icon">&#9203;</div>
      <h2>Account created</h2>
      <p>Your {provider_name} account was created and is waiting for an admin to
      approve it. You'll be able to sign in once approved. You can close this window.</p>
      <script>setTimeout(function(){{ if(window.opener) window.close(); }}, 3000);</script>
      """ + _PAGE_FOOT
    return HTMLResponse(body)


def _error_page(message: str) -> HTMLResponse:
    body = (_PAGE_HEAD % {"title": "Sign-in error"}) + f"""
      <div class="icon">&#9888;</div>
      <h2>Sign-in didn't work</h2><p>{message}</p>
      <script>setTimeout(function(){{ if(window.opener) window.close(); }}, 4000);</script>
      """ + _PAGE_FOOT
    return HTMLResponse(body, status_code=400)
