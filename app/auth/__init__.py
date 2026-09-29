"""Auth router — login with remember-me + recall endpoint."""

import asyncio
import logging
import json
import secrets
from copy import deepcopy
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator
from pathlib import Path

from app.auth.jwt import create_access_token, decode_signed_token, decode_token
from app.auth.users import (
    admin_exists,
    authenticate,
    set_remember_token,
    clear_remember_token,
    resolve_remember_token,
    get_user,
    get_user_by_id,
    register_user,
    update_user,
    change_password,
    delete_user_self,
    set_session_policy,
    clamp_session_lifetime,
    DEFAULT_SESSION_LIFETIME_MINUTES,
    UpdateConflict,
)
from app.db import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

_BOOTSTRAP_ADMIN_ID = "admin"


class LoginRequest(BaseModel):
    email: str = Field(min_length=1, max_length=320)
    password: str = Field(min_length=1, max_length=128)
    remember_me: bool = False
    device_id: str = Field(default="", max_length=128)

    @field_validator("password")
    @classmethod
    def password_fits_bcrypt(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 72:
            raise ValueError("password must be at most 72 UTF-8 bytes")
        return value


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    username: str
    user_id: str
    display_name: str
    remember_token: str = ""


class RegisterRequest(BaseModel):
    email: str = Field(min_length=1, max_length=320)
    password: str = Field(min_length=1, max_length=128)
    display_name: str = Field(default="", max_length=128)

    @field_validator("password")
    @classmethod
    def password_fits_bcrypt(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 72:
            raise ValueError("password must be at most 72 UTF-8 bytes")
        return value


class RegisterResponse(BaseModel):
    access_token: str = ""
    token_type: str = "bearer"
    username: str
    user_id: str
    display_name: str
    pending_approval: bool = False


class RecallRequest(BaseModel):
    remember_token: str


class RecallResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    username: str
    user_id: str
    display_name: str


class AnonymousRequest(BaseModel):
    browser_id: str | None = None
    guest_credential: str | None = None


class AnonymousResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: str
    guest_credential: str
    access_token_expires_in: int = 3600


class RevokeDevicesRequest(BaseModel):
    device_ids: list[str]


class ConnectionTicketRequest(BaseModel):
    path: str = Field(min_length=1, max_length=128)


@router.post("/connection-ticket")
async def create_connection_ticket(req: ConnectionTicketRequest, request: Request):
    from app.auth.connection_tickets import issue
    from app.auth.identity import request_user_id

    user_id = request_user_id(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        ticket = await asyncio.to_thread(issue, user_id, req.path)
    except ValueError:
        raise HTTPException(status_code=400, detail="Unsupported connection path")
    return {"ticket": ticket, "expires_in": 60, "path": req.path}


@router.post("/anonymous", response_model=AnonymousResponse)
async def create_anonymous_session(req: AnonymousRequest, request: Request):
    """Issue a JWT for an anonymous visitor on the main page.

    Does not require an agent_id (unlike the per-agent anon-session
    endpoint), so any page visitor can get a token and start chatting.
    The identity is persisted as a channel_identity (web_anonymous) so
    sessions and interactions are properly associated.
    """
    import uuid as _uuid_mod
    from app.communications.auth import get_or_create_identity

    browser_id = req.browser_id or _uuid_mod.uuid4().hex
    from app.db import get_app_db
    from app.auth.guest_credentials import issue as issue_guest_credential, recover_and_rotate
    auth_db = get_app_db()
    recovered = recover_and_rotate(
        auth_db, req.guest_credential, channel="web_anonymous",
    )
    if recovered:
        browser_id = recovered["external_id"]
    from app.api.rate_limit import enforce_anon_session_creation, bind_anon_session_identity
    reservation = await enforce_anon_session_creation(request, browser_id)
    identity = await get_or_create_identity(channel="web_anonymous", external_id=browser_id)
    await bind_anon_session_identity(reservation, identity.user_id)

    durable_credential = (
        recovered["guest_credential"] if recovered else issue_guest_credential(
            auth_db, user_id=identity.user_id, channel="web_anonymous",
            external_id=browser_id,
        )
    )

    token = create_access_token(
        username=identity.user_id,
        user_id=identity.user_id,
        expires_minutes=60,
        extra_claims={
            "anon_admission": True,
            "anon_admission_id": secrets.token_urlsafe(18),
            "anon_source": reservation["source_hash"],
        },
    )
    return AnonymousResponse(
        access_token=token, user_id=identity.user_id,
        guest_credential=durable_credential,
    )


def _record_auth_event(level: str, message: str, user_id: str | None = None) -> None:
    """Write a sign-in/security event into the durable diagnostics store
    (category ``auth``). Feeds the admin Dashboard's "Security & sign-ins"
    card — sign-ins, failed attempts and registrations become queryable via
    ``query_diagnostics(categories=["auth"])``. Best-effort, never raises."""
    try:
        from app.agent.diagnostics import record
        record(level, "auth", message, user_id=user_id, persist=True)
    except Exception:
        pass


@router.post("/login", response_model=LoginResponse)
async def login(request: Request, req: LoginRequest):
    """Authenticate with username and password.

    If remember_me is True, a persistent remember token is generated
    (invalidates any previous one). Returns it alongside the JWT.
    """
    from app.api.rate_limit import (
        begin_login_attempt,
        clear_login_failures,
        record_login_failure,
    )
    failure_key = await begin_login_attempt(request, req.email)
    user = await authenticate(req.email, req.password)
    if user is None:
        await record_login_failure(failure_key)
        _record_auth_event("warning", "Failed sign-in")
        raise HTTPException(status_code=401, detail="Invalid email or password")
    await clear_login_failures(failure_key)
    from app.admin.settings import get_access_mode as _gam
    if not user.is_approved and _gam() == "admin_approval":
        _record_auth_event("warning", "Sign-in blocked (pending approval)", user_id=user.user_id)
        raise HTTPException(status_code=403, detail="Account pending admin approval")
    _record_auth_event("info", f"Sign-in: {user.username}", user_id=user.user_id)

    # Mint the pass for the user's own chosen lifetime (Manage Account →
    # Sign-in & sessions; defaults to 30 days for accounts that haven't set one).
    requested_device_id = req.device_id.strip()
    if requested_device_id and (
        len(requested_device_id) > 128
        or not all(ch.isalnum() or ch in "-_" for ch in requested_device_id)
    ):
        raise HTTPException(status_code=400, detail="Invalid device identifier")
    token = create_access_token(
        user.username, user.user_id,
        expires_minutes=user.session_lifetime_minutes,
        device_id=requested_device_id or None,
        reauthenticated=True,
    )
    resp = LoginResponse(
        access_token=token,
        username=user.username,
        user_id=user.user_id,
        display_name=user.display_name,
    )

    # Issue a renewal ticket if the user ticked "Remember me" OR has auto-renew
    # on as a standing account preference — either way the silent refresh then
    # keeps the session alive so it never dies in place at the lifetime mark.
    if req.remember_me or user.auto_renew:
        token_payload = decode_signed_token(token) or {}
        remember = await set_remember_token(
            req.email,
            device_id=str(token_payload.get("device_id") or ""),
        )
        if remember:
            resp.remember_token = remember

    try:
        from app.auth.device_location import location_for_ip, request_client_ip, request_origin
        from app.auth.revocation import update_device_metadata

        token_payload = decode_signed_token(token) or {}
        device_id = str(token_payload.get("device_id") or "")
        ip_address = request_client_ip(request)
        location = await location_for_ip(ip_address)
        update_device_metadata(
            user.user_id,
            device_id,
            ip_address=ip_address,
            location=location,
            user_agent=request.headers.get("user-agent", ""),
            origin=request_origin(request),
        )
    except Exception:
        pass

    # Track user login: create/update user_profiles row
    now_iso = datetime.now(timezone.utc).isoformat()
    db = get_db()
    await db.upsert_user_profile(user.user_id, last_login_at=now_iso)

    return resp


@router.post("/recall", response_model=RecallResponse)
async def recall(req: RecallRequest):
    """Exchange a valid remember token for a fresh JWT (auto-login).

    Called when user returns and the JWT has expired / doesn't exist yet.
    Returns a fresh access_token.
    """
    user = await resolve_remember_token(req.remember_token)
    if user is None:
        _record_auth_event("warning", "Auto-login failed (invalid or expired remember token)")
        raise HTTPException(status_code=401, detail="Invalid or expired remember token")
    from app.admin.settings import get_access_mode as _gam2
    if not user.is_approved and _gam2() == "admin_approval":
        raise HTTPException(status_code=403, detail="Account pending admin approval")

    from app.auth.revocation import (
        claim_legacy_remember_token,
        remember_token_device,
    )

    device_id = remember_token_device(user.user_id, req.remember_token)
    if not device_id:
        device_id = claim_legacy_remember_token(user.user_id, req.remember_token)
    if not device_id:
        _record_auth_event(
            "warning",
            "Auto-login failed (remember token device revoked or ambiguous)",
            user_id=user.user_id,
        )
        raise HTTPException(status_code=401, detail="Invalid or expired remember token")

    token = create_access_token(
        user.username, user.user_id,
        expires_minutes=user.session_lifetime_minutes,
        device_id=device_id,
    )
    resp = RecallResponse(
        access_token=token,
        username=user.username,
        user_id=user.user_id,
        display_name=user.display_name,
    )

    # Auto-login is a login too — refresh the device's sign-in context (IP,
    # geo, user agent and the origin URL the session came through) so the
    # Signed-in devices table stays accurate for silently-renewed sessions.
    try:
        from app.auth.device_location import location_for_ip, request_client_ip, request_origin
        from app.auth.revocation import update_device_metadata

        ip_address = request_client_ip(request)
        update_device_metadata(
            user.user_id,
            device_id,
            ip_address=ip_address,
            location=await location_for_ip(ip_address),
            user_agent=request.headers.get("user-agent", ""),
            origin=request_origin(request),
        )
    except Exception:
        pass

    # Track auto-login from remember token
    now_iso = datetime.now(timezone.utc).isoformat()
    db = get_db()
    await db.upsert_user_profile(user.user_id, last_login_at=now_iso)

    return resp


@router.post("/register", response_model=RegisterResponse)
async def register(req: RegisterRequest, request: Request):
    """Register a new user account.

    Username must be unique. On success, returns a JWT (auto-login).
    Honors the app's access_mode:
      - admin_approval:   account created but unapproved; login blocked until admin approves
      - public_registered: account created and auto-approved (must still sign in)
      - open:             auto-admin localhost mode; normal registration still auto-approves
    """
    from app.admin.settings import get_access_mode as _gam
    from app.api.rate_limit import enforce_public_registration
    await enforce_public_registration(request)
    mode = _gam()
    auto_approve = (mode != "admin_approval")
    user = await register_user(req.email, req.password, req.display_name, is_approved=auto_approve)
    if user is None:
        raise HTTPException(status_code=409, detail="Email already registered")
    _record_auth_event("info", f"New account registered: {user.username}"
                       + ("" if auto_approve else " (pending approval)"), user_id=user.user_id)

    # Registration is the supported escape hatch from the strict one-browser-
    # per-network guest policy. If the request carries a valid anonymous JWT,
    # move its channel mapping and owned chat rows to the new account so the
    # user does not lose the conversation that persuaded them to register.
    from app.auth.identity import request_user_id
    prior_user_id = request_user_id(request)
    if prior_user_id.startswith("anon_"):
        try:
            from app.communications.auth import migrate_anonymous_to_user
            await migrate_anonymous_to_user(prior_user_id, user.user_id)
        except Exception as migration_error:
            logger.warning(
                "Could not migrate anonymous account %s after registration: %s",
                prior_user_id,
                migration_error,
            )

    if not auto_approve:
        # Created but unapproved — no token issued, client must wait for admin
        return RegisterResponse(
            access_token="",
            username=user.username,
            user_id=user.user_id,
            display_name=user.display_name,
            pending_approval=True,
        )

    token = create_access_token(user.username, user.user_id)
    resp = RegisterResponse(
        access_token=token,
        username=user.username,
        user_id=user.user_id,
        display_name=user.display_name,
        pending_approval=False,
    )

    # Track new user registration (first login)
    now_iso = datetime.now(timezone.utc).isoformat()
    db = get_db()
    await db.upsert_user_profile(user.user_id, last_login_at=now_iso)

    return resp


class AccessModeResponse(BaseModel):
    access_mode: str  # admin_approval | public_registered | open


@router.get("/access-mode", response_model=AccessModeResponse)
async def access_mode():
    """Public endpoint: returns the current registration/access policy.
    Used by the sign-in modal and chat UI to gate registration and anonymous use.
    """
    from app.admin.settings import get_access_mode as _gam
    return AccessModeResponse(access_mode=_gam())


CHAT_UI_CONFIG_PATH = Path("data/config/chat_ui.json")

def _load_chat_ui_config() -> dict:
    """Load chat_ui.json — the source of truth for chat presentation and copy."""
    try:
        if CHAT_UI_CONFIG_PATH.exists():
            return json.loads(CHAT_UI_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("Failed to load chat_ui.json, using empty default")
    return {}


def _chat_ui_deep_merge(base: dict, override: dict) -> dict:
    """Merge a chat UI overlay without mutating the authored configuration."""
    out = deepcopy(base) if isinstance(base, dict) else {}
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _chat_ui_deep_merge(out[key], value)
        else:
            out[key] = deepcopy(value)
    return out


def _chat_ui_requirement_allowed(requirement: dict, capabilities: dict) -> bool:
    """Evaluate a secret-free chat-ui requirement against effective capabilities."""
    if not isinstance(requirement, dict):
        return True
    tier = capabilities.get("tier") or {}
    subject = capabilities.get("subject") or {}
    features = capabilities.get("features") or {}
    pages = capabilities.get("pages") or {}
    ability_groups = set(capabilities.get("ability_groups") or [])
    checks = (
        all(features.get(name) is True for name in requirement.get("features", [])),
        all(pages.get(name) is True for name in requirement.get("pages", [])),
        all(name in ability_groups for name in requirement.get("ability_groups", [])),
        not requirement.get("tiers") or tier.get("slug") in requirement["tiers"],
        not requirement.get("subject_classes")
        or subject.get("class") in requirement["subject_classes"],
        not requirement.get("admin_only") or subject.get("is_admin") is True,
    )
    return all(checks)


def _apply_chat_ui_control_gates(value, denied_controls: set[str]):
    """Force denied controls off in every header/footer/control-library map."""
    if isinstance(value, list):
        return [
            _apply_chat_ui_control_gates(item, denied_controls)
            for item in value
            if not (isinstance(item, str) and item in denied_controls)
        ]
    if not isinstance(value, dict):
        return deepcopy(value)
    out = {}
    for key, child in value.items():
        if key == "controls" and isinstance(child, dict):
            gated = {}
            for control_name, control_cfg in child.items():
                cfg = _apply_chat_ui_control_gates(control_cfg, denied_controls)
                if control_name in denied_controls:
                    cfg = _chat_ui_deep_merge(cfg if isinstance(cfg, dict) else {}, {"enabled": False})
                gated[control_name] = cfg
            out[key] = gated
        elif key in denied_controls:
            # Some configuration surfaces use a named boolean (for example,
            # session_dropdown.menu.session_id) instead of a controls map.
            # Clamp those values too so an agent override cannot expose them.
            out[key] = False
        else:
            out[key] = _apply_chat_ui_control_gates(child, denied_controls)
    return out


def _resolve_chat_ui_config(raw: dict, capabilities: dict) -> dict:
    """Resolve all chat_ui tier/capability gates before config reaches a browser.

    ``tier_gates`` is configuration metadata, not UI data.  The active subject
    and tier overlays are merged last, and controls whose requirements are not
    satisfied are forced disabled throughout every surface.  ``_tier_enforced``
    is intentionally retained so the browser can re-apply the same final clamp
    after merging a cached per-agent override.
    """
    source = deepcopy(raw) if isinstance(raw, dict) else {}
    gates = source.pop("tier_gates", {})
    if not isinstance(gates, dict):
        gates = {}

    root_requirement = gates.get("requires") or {}
    if not _chat_ui_requirement_allowed(root_requirement, capabilities):
        return {
            "version": source.get("version", 3),
            "chat_common": {
                "chat_header": {"enabled": False},
                "active_footer": {"enabled": False},
                "idle_footer": {"enabled": False},
            },
            "_tier_enforced": {"denied": True, "controls": [], "override": {}},
        }

    subject = capabilities.get("subject") or {}
    tier = capabilities.get("tier") or {}
    overlay = {}
    default_overlay = gates.get("default")
    if isinstance(default_overlay, dict):
        overlay = _chat_ui_deep_merge(overlay, default_overlay)
    subject_overlay = (gates.get("subjects") or {}).get(subject.get("class"))
    if isinstance(subject_overlay, dict):
        overlay = _chat_ui_deep_merge(overlay, subject_overlay)
    tier_overlay = (gates.get("tiers") or {}).get(tier.get("slug"))
    if isinstance(tier_overlay, dict):
        overlay = _chat_ui_deep_merge(overlay, tier_overlay)
    if subject.get("is_admin") and isinstance(gates.get("admin"), dict):
        overlay = _chat_ui_deep_merge(overlay, gates["admin"])

    requirements = gates.get("controls") or {}
    denied_controls = {
        name for name, requirement in requirements.items()
        if not _chat_ui_requirement_allowed(requirement, capabilities)
    } if isinstance(requirements, dict) else set()
    resolved = _chat_ui_deep_merge(source, overlay)
    resolved = _apply_chat_ui_control_gates(resolved, denied_controls)
    resolved["_tier_enforced"] = {
        "denied": False,
        "tier": tier.get("slug"),
        "controls": sorted(denied_controls),
        "override": overlay,
    }
    return resolved


class UiConfigResponse(BaseModel):
    # Per-theme animated-background choice (background ids or "none").
    background_dark: str
    background_light: str
    # Global border + typography + full theme palette (see
    # app.admin.settings.get_appearance_config). Injected at boot as CSS-variable
    # overrides by ui/shared/js/appearance.js. The palette keys come in
    # _dark / _light pairs and mirror the design-system.css palette blocks.
    border_color_dark: str
    border_color_light: str
    border_width: str
    font_sans: str
    font_mono: str
    accent_dark: str
    accent_light: str
    secondary_dark: str
    secondary_light: str
    success_dark: str
    success_light: str
    warning_dark: str
    warning_light: str
    danger_dark: str
    danger_light: str
    surface_bg_dark: str
    surface_bg_light: str
    surface_panel_dark: str
    surface_panel_light: str
    surface_tint_dark: str
    surface_tint_light: str
    text_dark: str
    text_light: str
    text_muted_dark: str
    text_muted_light: str
    ambient_dark: str
    ambient_light: str
    # Chat bubble overrides (blank = follow theme default; #rrggbb[aa]).
    user_bubble_dark: str
    user_bubble_light: str
    user_bubble_border_dark: str
    user_bubble_border_light: str
    agent_bubble_dark: str
    agent_bubble_light: str
    agent_bubble_border_dark: str
    agent_bubble_border_light: str
    # Chat composer pill fill and border (blank = follow theme default; #rrggbb[aa]).
    chat_pill_bg_dark: str
    chat_pill_bg_light: str
    chat_pill_border_dark: str
    chat_pill_border_light: str
    # Theme-independent style knobs (JSON-only; injected by appearance.js).
    radius_scale: str
    shadow_strength: str
    ui_scale: str
    reduce_motion: str
    cursor_glow: str
    # RAM speed layer (ui/shared/js/media-cache.js): app-wide On/Off + memory
    # budget (MB) for the in-browser attachment/thumbnail cache.
    media_cache_enabled: str
    media_cache_budget_mb: str
    # Chat-composer geometry + button knobs (CSS lengths; see app1.css
    # CHAT-PILL-VARS). Injected app-wide by ui/shared/js/appearance.js.
    chat_pill_max_width: str
    chat_pill_radius: str
    chat_pill_padding: str
    chat_pill_min_height: str
    chat_pill_max_height: str
    chat_pill_font_size: str
    chat_pill_attach_size: str
    chat_pill_attach_icon: str
    chat_pill_button_size: str
    chat_pill_button_icon: str
    # Chat panel layout (app-wide defaults; injected by appearance.js).
    #   chat_position        — "right" (default) | "left" (desktop split side)
    #   chat_default_visible — "visible" (default) | "hidden" (first-visit default)
    #   chat_panel_width     — positive desktop width in px or %, with a
    #                          browser-local manually dragged width taking precedence
    chat_position: str
    chat_default_visible: str
    chat_panel_width: str
    # Master on/off for the welcome landing page (ui/splash/splash-page), app-wide.
    # app/main.py serves the landing at / when this is on; the per-device
    # "don't show again" / account preference (wa_seen_splash cookie) layers on top.
    splash_enabled: bool
    # First-class genui (ui/main-panel/genui): render agent-authored genui
    # directly in the app (no sandboxed iframe) with real-page powers — live
    # webcam/mic, direct agent calls. A genui runs with the VIEWER's OWN app
    # trust (it is per-user — a person only ever sees their own genui, in their
    # own browser/session), so this is gated by the Gen UI page's VISIBILITY
    # setting, enforced server-side (app.auth.identity.caller_may_access_page):
    #   • "all"  → everyone, including anonymous guests
    #   • "auth" → any signed-in REGISTERED user (the default — registration
    #              required out of the box, NOT admin-only)
    #   • "off"  → admins only
    # Admins always pass; 'open' single-user/local mode passes everything (local
    # convenience, incl. over a Cloudflare tunnel where the browser can't mint a
    # JWT). The frontend MUST send the caller's auth token on the /ui-config fetch
    # so the gate can see who they are (see _fetchGenuiGate in autoagent.js). The
    # genui HTTP endpoints (app/api/genui.py) re-check this SAME gate + per-user
    # ownership, so a hidden tab can't be bypassed by calling the API directly.
    genui_first_class: bool
    # Whether signed-in users may set their own theme (App Settings → Appearance →
    # "Allow per-user themes"). The account page (ui/shared/js/account.js) shows
    # its "My appearance" editor only when this is True; this same endpoint also
    # layers the caller's saved per-user overrides on top of the palette/background
    # fields below when it is True. Default False (the app behaves exactly as
    # before — one global theme).
    allow_user_appearance: bool
    # Master on/off for the sliding session-completion notification panel
    # (running → done/interrupted/error), app-wide.
    # ui/chat/js/session-notification.js reads this at boot and skips the panel
    # entirely when False. Defaults True (the panel has always been available).
    session_completion_notifications: bool
    # App-wide policy for browser-native vs recorded/LLM voice dictation.
    voice_dictation_llm_enabled: bool
    voice_dictation_mode: str
    # Safety lock — when True, the app is starting up from a shut-down state and
    # MUST show a confirmation modal before running any recovery/automation.
    # Set when the server shuts down, cleared when the admin confirms.
    safety_lock_active: bool = False
    # Master switch for the safety lock feature. When False, the feature is
    # completely disabled — no splash, no gate, no recovery suppression.
    safety_lock_enabled: bool = True
    # Hide main header when mobile on-screen keyboard is open (≤800px viewport).
    # Default OFF; gated by the App Settings toggle (hide_header_on_keyboard).
    hide_header_on_keyboard: bool = False
    # Viewport height threshold for the above: 0 = always hide; only hide when
    # remaining viewport height is below this many px.
    hide_header_kb_threshold: int = 200
    # Compact hamburger/sidebar navigation is the default on mobile viewports.
    mobile_mode: bool = True
    # Master on/off for the always-on display (Screen Wake Lock), app-wide.
    # When True, every visitor's browser keeps the screen on while the app tab
    # is visible — the same operation as the chat wake_lock control
    # (ui/shared/js/main.js applies it at boot; the App Settings toggle applies
    # it live). Default OFF (screen sleeps normally).
    always_on_display: bool = False
    site_name: str = "WebAgent"
    # Chat presentation + copy — served as a dict so every chat surface can be
    # configured at boot without a server restart.
    chat_ui: dict
    # Chat widget config — corners, agent, prompt, detection modes.
    # Sourced from app-settings.json; {} means use chat_ui.json defaults.
    chat_widget: dict


@router.get("/ui-config", response_model=UiConfigResponse)
async def ui_config(request: Request, agent_id: str = Query(None)):
    """Public endpoint: app-wide UI config every visitor needs before render.

    Carries the admin's per-theme animated-background choice (so the background
    engine ui/background/_engine/manager.js renders the right one) plus the
    global border + font appearance knobs (so ui/shared/js/appearance.js can
    recolour/resize borders and swap the UI font app-wide) — for anonymous and
    signed-in visitors alike. Also reports whether first-class genui are
    permitted (local-only safety gate; see app/visualizer + the Gen UI tab).

    When ``agent_id`` is given, the returned ``chat_ui`` is the app-wide
    chat_ui.json deep-merged with that agent's per-agent ``metadata.chat_ui``
    override, so the chat panel renders with the agent's custom chrome.
    """
    from app.admin.settings import (
        get_background_config,
        get_appearance_config,
        get_access_mode,
        get_splash_enabled,
        get_allow_user_appearance,
        get_session_completion_notifications,
        get_always_on_display,
        get_site_name,
        get_voice_dictation_config,
        get_hide_header_on_keyboard,
        get_hide_header_kb_threshold,
        get_mobile_mode,
        get_safety_lock_active,
        get_safety_lock_enabled,
        valid_background,
        _load_app_settings,
    )
    cfg = get_background_config()
    app = get_appearance_config()
    bg_dark, bg_light = cfg["dark"], cfg["light"]
    # Whether THIS caller may run first-class (full-trust) genui is now driven
    # by the Gen UI page's VISIBILITY setting, enforced server-side — not a
    # separate hardcoded admin-only gate. caller_may_access_page resolves the
    # caller from the token the frontend sends and applies the 3-state rule:
    #   • Gen UI "all"  → everyone, incl. anonymous guests
    #   • Gen UI "auth" → any signed-in registered user (the default — so genui
    #                     is registration-required out of the box, not admin-only)
    #   • Gen UI "off"  → admins only
    # Admins always pass, and 'open' single-user/local mode passes everything
    # (local convenience, incl. over a tunnel where the browser can't mint a JWT).
    from app.auth.identity import request_user_id, caller_may_access_page
    caller_uid = request_user_id(request)
    genui_first_class = await caller_may_access_page(request, "main", "genui")
    # Per-user theme: when the admin has enabled it, layer the signed-in caller's
    # saved overrides on top of the global appearance, so the boot-time applier
    # (ui/shared/js/appearance.js) needs NO change — it just receives an already
    # personalised theme. A blank/absent token keeps the global value (fall back
    # to global); the background is re-validated against installed plugins.
    allow_user_appearance = get_allow_user_appearance()
    voice_dictation = get_voice_dictation_config()
    if allow_user_appearance and caller_uid:
        try:
            from app.db import get_db
            db = get_db()
            overrides = {}
            if hasattr(db, "get_user_appearance"):
                overrides = await db.get_user_appearance(caller_uid) or {}
            for key, val in overrides.items():
                if key in app and isinstance(val, str) and val.strip():
                    app[key] = val.strip()
            bg_dark = valid_background(overrides.get("background_dark"), bg_dark)
            bg_light = valid_background(overrides.get("background_light"), bg_light)
        except Exception:  # noqa: BLE001 — per-user theme must never break boot config
            pass

    # Build the chat_ui response — start with the global chat_ui.json, then
    # deep-merge the agent's per-agent override when agent_id is given.
    chat_ui = _load_chat_ui_config()
    agent_public_policy = None
    if agent_id:
        try:
            from app.api.agents import _deep_merge
            from app.db import get_db
            db = get_db()
            agent = await db.get_agent_by_id(agent_id)
            if agent:
                meta_raw = agent.get("metadata")
                meta = {}
                if isinstance(meta_raw, str):
                    try:
                        meta = json.loads(meta_raw)
                    except Exception:
                        pass
                elif isinstance(meta_raw, dict):
                    meta = dict(meta_raw)
                agent_cu = meta.get("chat_ui")
                if isinstance(agent_cu, dict) and agent_cu:
                    chat_ui = _deep_merge(chat_ui, agent_cu)
                if str(caller_uid or "").startswith("anon_"):
                    from app.agent.public_policy import normalize_public_access
                    agent_public_policy = normalize_public_access(agent)
                    public_ui = agent_public_policy.get("chat_ui")
                    if isinstance(public_ui, dict) and public_ui:
                        chat_ui = _deep_merge(chat_ui, public_ui)
        except Exception:  # noqa: BLE001 — per-agent override must never break boot config
            pass

    # Resolve the complete UI document for this caller.  This happens after
    # the per-agent merge so an agent override cannot restore a control denied
    # by the effective experience-tier policy.
    try:
        from app.entitlements.service import resolve_capabilities
        capabilities = await resolve_capabilities(caller_uid or "__anonymous__")
        if agent_public_policy and agent_public_policy.get("capabilities", {}).get("mode") != "showcase":
            sponsored = agent_public_policy.get("capabilities") or {}
            capabilities = deepcopy(capabilities)
            capabilities["tier"] = {**(capabilities.get("tier") or {}), "slug": "public_agent"}
            capabilities["features"] = {
                **(capabilities.get("features") or {}),
                **{name: True for name in sponsored.get("features") or []},
            }
        chat_ui = _resolve_chat_ui_config(chat_ui, capabilities)
    except Exception:  # fail closed if the entitlement authority is unavailable
        chat_ui = _resolve_chat_ui_config(chat_ui, {
            "subject": {"class": "anonymous", "is_admin": False},
            "tier": {"slug": "anonymous"},
            "features": {}, "pages": {}, "ability_groups": [],
        })

    # `app` (get_appearance_config) carries every appearance key — the border /
    # font knobs and the full per-theme palette — each concrete (never blank).
    # Spread it so newly-added palette tokens flow through with no per-key wiring.
    return UiConfigResponse(
        background_dark=bg_dark,
        background_light=bg_light,
        genui_first_class=genui_first_class,
        safety_lock_active=get_safety_lock_active(),
        safety_lock_enabled=get_safety_lock_enabled(),
        splash_enabled=get_splash_enabled(),
        allow_user_appearance=allow_user_appearance,
        session_completion_notifications=get_session_completion_notifications(),
        always_on_display=get_always_on_display(),
        site_name=get_site_name(),
        voice_dictation_llm_enabled=voice_dictation["llm_enabled"],
        voice_dictation_mode=voice_dictation["mode"],
        hide_header_on_keyboard=get_hide_header_on_keyboard(),
        hide_header_kb_threshold=get_hide_header_kb_threshold(),
        mobile_mode=get_mobile_mode(),
        chat_ui=chat_ui,
        chat_widget=_load_app_settings().get("chat_widget", {}),
        **app,
    )


@router.get("/backgrounds")
async def list_backgrounds_public():
    """Public: the installed animated-background catalog (id, name, status).

    The per-user theme editor (account page → ui/shared/js/appearance-me.js) needs
    this to populate its background selects; the admin equivalent lives under
    /admin/settings/backgrounds. The catalog is just plugin display names, so it
    is not gated."""
    try:
        from app.ui_backgrounds import catalog as _bg_catalog
        return {"backgrounds": _bg_catalog()}
    except Exception:  # noqa: BLE001
        return {"backgrounds": []}


class SetupStatusResponse(BaseModel):
    initialized: bool


class SetupAdminRequest(BaseModel):
    password: str
    display_name: str = ""
    username: str = "admin"


class SetupAdminResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    username: str
    user_id: str
    display_name: str


@router.get("/setup-status", response_model=SetupStatusResponse)
async def setup_status():
    """Public endpoint: returns whether an admin account exists."""
    return SetupStatusResponse(initialized=await admin_exists())


@router.post("/setup-admin", response_model=SetupAdminResponse)
async def setup_admin(req: SetupAdminRequest, request: Request):
    """Create the first admin account.

    Open to ANY first visitor (local or remote) — the only guard is that no admin
    exists yet. The ``admin_exists()`` check below closes the door for good the
    instant the first admin is created, so this is strictly a one-time,
    first-visitor action. There is deliberately NO localhost restriction: a fresh
    install (including a freshly-deployed cloud server, whose own browser is
    remote) must always be able to reach this page to set the very first password."""
    if await admin_exists():
        raise HTTPException(status_code=400, detail="Admin already exists")

    if len(req.password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")

    # The operator may choose any login name (or leave the default "admin").
    # The canonical admin user_id stays "admin" so every downstream admin gate
    # (DB is_admin seed, open-mode fallback, delete protection) keeps working.
    login_name = (req.username or "").strip() or "admin"
    user = await register_user(
        login_name, req.password, req.display_name or "Admin",
        user_id=_BOOTSTRAP_ADMIN_ID,
    )
    if user is None:
        raise HTTPException(status_code=409, detail="That username is already taken")

    from app.auth.jwt import create_access_token

    token = create_access_token(user.username, user.user_id)

    # Track setup as first login
    now_iso = datetime.now(timezone.utc).isoformat()
    db = get_db()
    await db.upsert_user_profile(user.user_id, last_login_at=now_iso)

    return SetupAdminResponse(
        access_token=token,
        username=user.username,
        user_id=user.user_id,
        display_name=user.display_name,
    )


class SetupBundleRequest(BaseModel):
    code: str
    # The source install's admin password — decrypts the code AND becomes this
    # install's admin password (it also rides inside the bundle's admin section).
    password: str


@router.post("/setup-bundle", response_model=SetupAdminResponse)
async def setup_bundle(req: SetupBundleRequest):
    """Provision a FRESH install from a pasted setup code, on the setup page.

    Same one-time gate as ``setup_admin`` (refused once an admin exists). The
    password both decrypts the bundle and becomes the admin login — so one typed
    value unlocks the keys AND claims the box. Applies DB/vault/LLM/admin and
    returns a token so the setup page logs straight in."""
    if await admin_exists():
        raise HTTPException(status_code=400, detail="Admin already exists")

    from app.admin import bootstrap_bundle as bb
    try:
        bundle = bb.decode_bundle(req.code, req.password)
    except bb.BundleError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Fresh box → apply everything and switch the DB on. Sections use overwrite so
    # the bundle wins outright on a blank install.
    sections = (bundle.get("sections") or {})
    choices = {k: bb.MERGE_OVERWRITE for k in sections}
    await bb.apply_bundle(bundle, choices, activate_db=True)

    # Guarantee an admin exists even if the bundle didn't carry one: the password
    # they typed becomes the admin password.
    if not await admin_exists():
        await register_user("admin", req.password, "Admin", user_id=_BOOTSTRAP_ADMIN_ID)

    # Ensure the admin PRIVILEGE too: if the bundle's shared DB already carried the
    # admin login, register_user was skipped above, so its is_admin flag may be
    # absent on this (possibly remote) control DB. Self-heal so Admin Tools unlock.
    from app.auth.users import ensure_admin_privilege
    await ensure_admin_privilege()

    user = await get_user_by_id(_BOOTSTRAP_ADMIN_ID)
    if user is None:
        raise HTTPException(status_code=500, detail="Setup failed to create an admin account")

    from app.auth.jwt import create_access_token
    token = create_access_token(user.username, user.user_id)
    now_iso = datetime.now(timezone.utc).isoformat()
    db = get_db()
    await db.upsert_user_profile(user.user_id, last_login_at=now_iso)

    return SetupAdminResponse(
        access_token=token,
        username=user.username,
        user_id=user.user_id,
        display_name=user.display_name,
    )


class GuestLoginRequest(BaseModel):
    browser_id: str | None = None
    guest_credential: str | None = None


class GuestLoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    username: str
    user_id: str
    display_name: str
    guest_credential: str
    access_token_expires_in: int = 3600


@router.post("/guest-login", response_model=GuestLoginResponse)
async def guest_login(request: Request, req: GuestLoginRequest | None = None):
    """Mint an anonymous guest session — only in 'public_registered' (Open
    Registration) access mode.

    Open Registration means the app is open to anonymous visitors: anyone may
    browse and chat without first creating an account, and whether a *given*
    agent will actually talk to a guest is decided per-agent (its `user_mode`),
    NOT at the app level. This endpoint is what lets that work: it hands a
    tokenless visitor a persistent anonymous identity — a `web_public` guest
    keyed to their browser id, so the same browser keeps the same guest (and its
    sessions) across reloads — plus a JWT so the WebSocket handshake and the chat
    gate accept them. It is the app-level sibling of the per-agent public-URL
    flow (`POST /api/v1/agents/{id}/anon-session`), which mints the same kind of
    `web_public` identity for a single public agent page.

    Gated to 'public_registered' only. The stricter 'admin_approval' mode
    requires a real approved account, so a guest token is refused (403) there.
    """
    import uuid as _uuid_mod
    from app.admin.settings import get_access_mode as _gam
    if _gam() != "public_registered":
        raise HTTPException(status_code=403, detail="Guest access is not enabled")

    browser_id = (req.browser_id if req else None) or _uuid_mod.uuid4().hex
    from app.db import get_app_db
    from app.auth.guest_credentials import issue as issue_guest_credential, recover_and_rotate
    auth_db = get_app_db()
    recovered = recover_and_rotate(
        auth_db, req.guest_credential if req else None, channel="web_public",
    )
    if recovered:
        browser_id = recovered["external_id"]
    from app.api.rate_limit import enforce_anon_session_creation, bind_anon_session_identity
    reservation = await enforce_anon_session_creation(request, browser_id)
    from app.communications.auth import get_or_create_identity
    identity = await get_or_create_identity(channel="web_public", external_id=browser_id)
    await bind_anon_session_identity(reservation, identity.user_id)

    durable_credential = (
        recovered["guest_credential"] if recovered else issue_guest_credential(
            auth_db, user_id=identity.user_id, channel="web_public",
            external_id=browser_id,
        )
    )

    token = create_access_token(
        username=identity.user_id,
        user_id=identity.user_id,
        expires_minutes=60,
        extra_claims={
            "anon_admission": True,
            "anon_admission_id": secrets.token_urlsafe(18),
            "anon_source": reservation["source_hash"],
        },
    )

    return GuestLoginResponse(
        access_token=token,
        username=identity.user_id,
        user_id=identity.user_id,
        display_name=identity.display_name or "Guest",
        guest_credential=durable_credential,
    )


@router.post("/logout")
async def logout(request: Request):
    """Sign out only the device represented by the caller's access token."""
    _username, user_id = _require_auth(request)
    device_id = _request_device_id(request)
    from app.auth.revocation import revoke_device

    revoked = revoke_device(user_id, device_id) if device_id else False
    return {
        "status": "ok",
        "message": "Logged out on this device.",
        "device_id": device_id,
        "revoked": revoked,
        "purge_browser_storage": True,
    }


# ── Self-service profile management ─────────────────────────────────────────

class MeResponse(BaseModel):
    username: str
    user_id: str
    display_name: str
    is_approved: bool


class UpdateMeRequest(BaseModel):
    email: str | None = None
    display_name: str | None = None


class UpdateMeResponse(BaseModel):
    username: str
    user_id: str
    display_name: str
    is_approved: bool
    access_token: str  # fresh JWT (sub claim may have changed)
    token_type: str = "bearer"


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class ChangePasswordResponse(BaseModel):
    status: str = "ok"
    remember_token: str = ""  # rotated; client should replace any cached copy


class DeleteMeRequest(BaseModel):
    password: str


def _require_auth(request: Request) -> tuple[str, str]:
    """Decode the Bearer JWT from the Authorization header.

    AuthMiddleware is not registered in main.py, so each route resolves the
    caller from the header directly. Returns (username, user_id).
    """
    auth_header = request.headers.get("Authorization", "")
    token = auth_header[7:] if auth_header.startswith("Bearer ") else ""
    if not token:
        token = request.query_params.get("token", "")
    payload = decode_token(token) if token else None
    if not payload:
        raise HTTPException(status_code=401, detail="Not authenticated")
    username = payload.get("sub") or ""
    user_id = payload.get("user_id") or ""
    if not username or not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return username, user_id


def _signed_device_identity(request: Request) -> tuple[str, str]:
    """Resolve only (user_id, device_id) from a signed token for purge protocol."""
    auth_header = request.headers.get("Authorization", "")
    token = auth_header[7:] if auth_header.startswith("Bearer ") else ""
    payload = decode_signed_token(token, verify_expiry=False) if token else None
    user_id = str((payload or {}).get("user_id") or (payload or {}).get("sub") or "")
    device_id = str((payload or {}).get("device_id") or "")
    if not user_id or not device_id:
        raise HTTPException(status_code=401, detail="Invalid device credential")
    return user_id, device_id


def _request_device_id(request: Request) -> str:
    auth_header = request.headers.get("Authorization", "")
    token = auth_header[7:] if auth_header.startswith("Bearer ") else ""
    payload = decode_signed_token(token) if token else None
    return str((payload or {}).get("device_id") or "")


@router.get("/me", response_model=MeResponse)
async def get_me(request: Request):
    """Return the currently authenticated user's profile."""
    username, _user_id = _require_auth(request)
    user = await get_user(username)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return MeResponse(
        username=user.username,
        user_id=user.user_id,
        display_name=user.display_name,
        is_approved=user.is_approved,
    )


@router.get("/me/data-export")
async def export_me(request: Request):
    """Export the caller's server-authority data under administrator policy."""
    _username, user_id = _require_auth(request)
    from app.db.browser_policy import require_export_enabled
    try:
        require_export_enabled()
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    db = get_db()
    exporter = getattr(db, "export_user_data", None)
    if exporter is None:
        raise HTTPException(
            status_code=501,
            detail="The active storage provider does not support scoped export",
        )
    payload = await exporter(user_id)
    from app.db.user_store import export_user_store_data
    sidecar = await export_user_store_data(user_id)
    if sidecar is not None:
        payload["browser_sync_store"] = sidecar
    return payload


@router.get("/me/devices")
async def get_my_devices(request: Request):
    """List opaque login-device records for the authenticated account."""
    _username, user_id = _require_auth(request)
    from app.auth.revocation import current_epoch, list_devices
    auth_header = request.headers.get("Authorization", "")
    payload = decode_token(auth_header[7:]) if auth_header.startswith("Bearer ") else None
    current_device_id = str((payload or {}).get("device_id") or "")

    return {
        "revocation_epoch": current_epoch(user_id),
        "current_device_id": current_device_id,
        "devices": list_devices(user_id),
    }


@router.delete("/me/devices/{device_id}")
async def delete_my_device(request: Request, device_id: str):
    """Revoke one login pass without trusting a client-claimed user id."""
    _username, user_id = _require_auth(request)
    from app.auth.revocation import revoke_device

    revoked = revoke_device(user_id, device_id)
    if not revoked:
        raise HTTPException(status_code=404, detail="Device session not found")
    return {
        "status": "ok",
        "device_id": device_id,
        "revoked": True,
        "purge_requested": True,
    }


@router.post("/me/devices/revoke")
async def revoke_my_devices(request: Request, body: RevokeDevicesRequest):
    """Immediately invalidate several device sessions in one transaction."""
    _username, user_id = _require_auth(request)
    device_ids = [str(value).strip() for value in body.device_ids]
    if not device_ids or any(
        not value
        or len(value) > 128
        or not all(ch.isalnum() or ch in "-_" for ch in value)
        for value in device_ids
    ):
        raise HTTPException(status_code=400, detail="Invalid device selection")

    from app.auth.revocation import revoke_devices

    try:
        revoked_ids = revoke_devices(user_id, device_ids)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not revoked_ids:
        raise HTTPException(status_code=404, detail="Device sessions not found")

    # Connected targets purge immediately. The durable purge directive remains
    # authoritative for offline/cross-worker devices and runs on their next contact.
    try:
        from app.events.delivery import revoke_user_device_connections

        await revoke_user_device_connections(user_id, revoked_ids, {
            "type": "device_revoked",
            "device_ids": revoked_ids,
            "purge_required": True,
        })
    except Exception:
        pass

    return {
        "status": "ok",
        "revoked_device_ids": revoked_ids,
        "revoked_count": len(revoked_ids),
        "purge_required": True,
    }


@router.post("/device/purge-status")
async def post_device_purge_status(request: Request):
    """Check a device's purge directive even after its normal JWT was revoked."""
    user_id, device_id = _signed_device_identity(request)
    from app.auth.revocation import device_purge_status

    return {"device_id": device_id, **device_purge_status(user_id, device_id)}


@router.post("/device/purge-ack")
async def post_device_purge_ack(request: Request):
    """Acknowledge local purge using only the signed device identity."""
    user_id, device_id = _signed_device_identity(request)
    from app.auth.revocation import acknowledge_device_purge, device_purge_status

    acknowledged = acknowledge_device_purge(user_id, device_id)
    return {
        "device_id": device_id,
        "acknowledged": acknowledged,
        **device_purge_status(user_id, device_id),
    }


@router.patch("/me", response_model=UpdateMeResponse)
async def patch_me(request: Request, body: UpdateMeRequest):
    """Update the current user's email (username) and/or display name.

    Returns a fresh JWT because the `sub` claim may have changed.
    """
    current_username, _user_id = _require_auth(request)
    user = await get_user(current_username)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    new_email = (body.email or "").strip() or None
    new_display = body.display_name if body.display_name is not None else None

    if new_email is not None and "@" not in new_email:
        raise HTTPException(status_code=400, detail="Invalid email address")

    try:
        updated = await update_user(
            current_username,
            new_username=new_email,
            new_display_name=new_display,
        )
    except UpdateConflict:
        raise HTTPException(status_code=409, detail="Email already in use")

    if updated is None:
        raise HTTPException(status_code=404, detail="User not found")

    token = create_access_token(
        updated.username, updated.user_id,
        expires_minutes=updated.session_lifetime_minutes,
    )
    return UpdateMeResponse(
        username=updated.username,
        user_id=updated.user_id,
        display_name=updated.display_name,
        is_approved=updated.is_approved,
        access_token=token,
    )


@router.post("/change-password", response_model=ChangePasswordResponse)
async def post_change_password(request: Request, body: ChangePasswordRequest):
    """Change the authenticated user's password.

    Verifies the current password, writes a new bcrypt hash, and rotates the
    remember token so any previously-issued remember credential is invalidated.
    """
    username, _user_id = _require_auth(request)

    if not body.new_password or len(body.new_password) < 4:
        raise HTTPException(status_code=400, detail="New password must be at least 4 characters")

    verified = await authenticate(username, body.current_password)
    if verified is None:
        raise HTTPException(status_code=401, detail="Current password is incorrect")

    ok = await change_password(username, body.new_password)
    if not ok:
        raise HTTPException(status_code=404, detail="User not found")

    # Rotate remember token so old persistent credentials can't auto-login.
    new_remember = await set_remember_token(
        username,
        device_id=_request_device_id(request),
    ) or ""
    return ChangePasswordResponse(status="ok", remember_token=new_remember)


@router.delete("/me")
async def delete_me(request: Request, body: DeleteMeRequest):
    """Delete the authenticated user's account.

    Requires password confirmation. The bootstrap `admin` user
    cannot be deleted (returns 403).
    """
    username, _user_id = _require_auth(request)
    from app.db.browser_policy import require_delete_enabled
    try:
        require_delete_enabled()
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))

    verified = await authenticate(username, body.password)
    if verified is None:
        raise HTTPException(status_code=401, detail="Password is incorrect")
    if _user_id == _BOOTSTRAP_ADMIN_ID:
        raise HTTPException(
            status_code=403,
            detail="The bootstrap admin account cannot be deleted",
        )

    from app.auth.revocation import revoke_user
    revoke_user(_user_id)
    ok, reason = await delete_user_self(username)
    if not ok:
        if reason == "protected":
            raise HTTPException(status_code=403, detail="The bootstrap admin account cannot be deleted")
        raise HTTPException(status_code=404, detail="User not found")
    try:
        from app.agent.turn_reservations import delete_user_reservations
        delete_user_reservations(_user_id)
    except Exception:
        logger.warning("Could not remove durable turn receipts for deleted user %s", _user_id)

    return {"status": "ok", "purge_browser_storage": True}


# ── Per-user session policy (Manage Account → Sign-in & sessions) ────────────
# Each user controls how long their own login pass lasts and whether it silently
# auto-renews. Stored on the user record (app.auth.users). The lifetime is read
# at every token mint (login / recall / profile-edit) so a change takes effect on
# the next pass; the PUT below also re-mints the caller's current pass + issues or
# clears the renewal ticket so the change applies immediately, no re-login needed.

class SessionPolicyResponse(BaseModel):
    lifetime_minutes: int
    auto_renew: bool


class UpdateSessionPolicyRequest(BaseModel):
    lifetime_minutes: int
    auto_renew: bool


class UpdateSessionPolicyResponse(BaseModel):
    lifetime_minutes: int
    auto_renew: bool
    access_token: str           # fresh pass minted for the new lifetime
    token_type: str = "bearer"
    remember_token: str = ""     # new ticket when auto-renew on; "" when off


@router.get("/me/session-policy", response_model=SessionPolicyResponse)
async def get_my_session_policy(request: Request):
    """Return the caller's session policy (login-pass lifetime + auto-renew)."""
    username, _user_id = _require_auth(request)
    user = await get_user(username)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return SessionPolicyResponse(
        lifetime_minutes=user.session_lifetime_minutes,
        auto_renew=user.auto_renew,
    )


@router.put("/me/session-policy", response_model=UpdateSessionPolicyResponse)
async def put_my_session_policy(request: Request, body: UpdateSessionPolicyRequest):
    """Update the caller's session policy and apply it immediately.

    Clamps the lifetime to the allowed range, saves it, then re-mints the
    caller's current pass for the new lifetime. When auto-renew is on a fresh
    renewal ticket is issued (so the silent refresh can keep the session alive);
    when off the ticket is cleared (the session simply ends at the lifetime).
    """
    username, _user_id = _require_auth(request)
    user = await get_user(username)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    updated = await set_session_policy(
        username,
        clamp_session_lifetime(body.lifetime_minutes),
        bool(body.auto_renew),
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="User not found")

    token = create_access_token(
        updated.username, updated.user_id,
        expires_minutes=updated.session_lifetime_minutes,
    )
    remember = ""
    if updated.auto_renew:
        token_payload = decode_signed_token(token) or {}
        remember = await set_remember_token(
            username,
            device_id=str(token_payload.get("device_id") or ""),
        ) or ""
    else:
        await clear_remember_token(username)
    return UpdateSessionPolicyResponse(
        lifetime_minutes=updated.session_lifetime_minutes,
        auto_renew=updated.auto_renew,
        access_token=token,
        remember_token=remember,
    )


# ── Per-user appearance (self-service "My appearance" theme editor) ──────────
# The account page (ui/shared/js/account.js) reads/writes the signed-in user's
# own theme override here. Storage is the user's profile (user_profiles.appearance,
# a sparse JSON patch of the same keys as the global appearance). The override is
# applied at boot by the per-user merge in GET /api/v1/auth/ui-config above, so a
# user's theme follows them across devices. Gated by the admin's
# `allow_user_appearance` flag — when off, saving is refused (403) and the editor
# is hidden.

def _allowed_appearance_keys() -> set:
    """The set of appearance tokens a user may override: every key the global
    appearance carries, plus the two per-theme background ids."""
    from app.admin.settings import _DEFAULT_APPEARANCE
    return set(_DEFAULT_APPEARANCE.keys()) | {"background_dark", "background_light"}


@router.get("/me/appearance")
async def get_my_appearance(request: Request):
    """Return the caller's own theme overrides plus whether the feature is on.

    ``allow`` mirrors the admin's allow_user_appearance flag so the account page
    can decide whether to render its editor; ``overrides`` is the user's sparse
    patch (empty when they follow the global theme)."""
    from app.admin.settings import get_allow_user_appearance
    _username, user_id = _require_auth(request)
    from app.db import get_db
    db = get_db()
    overrides = {}
    if hasattr(db, "get_user_appearance"):
        try:
            overrides = await db.get_user_appearance(user_id) or {}
        except Exception:  # noqa: BLE001
            overrides = {}
    return {"allow": get_allow_user_appearance(), "overrides": overrides}


@router.put("/me/appearance")
async def put_my_appearance(request: Request):
    """Merge a partial theme patch into the caller's own overrides.

    Only known appearance keys are accepted (others are dropped). A key sent with
    a blank value clears that token (the user falls back to the global theme for
    it). Refused with 403 when the admin has not enabled per-user themes."""
    from app.admin.settings import get_allow_user_appearance
    _username, user_id = _require_auth(request)
    if not get_allow_user_appearance():
        raise HTTPException(status_code=403, detail="Per-user themes are disabled")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Body must be an object of appearance keys")
    allowed = _allowed_appearance_keys()
    patch = {k: v for k, v in body.items() if k in allowed and isinstance(v, str)}
    from app.db import get_db
    db = get_db()
    if not hasattr(db, "merge_user_appearance"):
        raise HTTPException(status_code=501, detail="Per-user themes not supported by this backend")
    merged = await db.merge_user_appearance(user_id, patch)
    return {"overrides": merged}


@router.delete("/me/appearance")
async def delete_my_appearance(request: Request):
    """Clear ALL of the caller's theme overrides — they fall back to the global
    theme. Allowed regardless of the admin flag (a reset is always safe)."""
    _username, user_id = _require_auth(request)
    from app.db import get_db
    db = get_db()
    if hasattr(db, "set_user_appearance"):
        try:
            await db.set_user_appearance(user_id, {})
        except Exception:  # noqa: BLE001
            pass
    return {"overrides": {}}
