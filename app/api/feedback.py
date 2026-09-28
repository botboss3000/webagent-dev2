"""Feedback relay endpoint.

Forwards user feedback (bug reports, feature requests, messages to admin) to
the WebAgent feedback relay, which creates a GitHub issue in the private
feedback repo. Anonymous by default; the user may optionally include an email
for follow-up.

The relay URL and feedback toggle come from app-settings.json. The
installation_id (a per-deployment UUID) lives in app-settings.json and is generated
on first use, so the relay can rate-limit or block individual clones without
breaking everyone.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, urlunparse

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/feedback")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# Default relay URL — points at the upstream WebAgent project's relay (a
# Cloudflare Worker). Every clone inherits this and works out of the box;
# cloners can override via app-settings.json `feedback_relay_url`.
_DEFAULT_RELAY_URL = "https://webagent-feedback-relay.botboss.workers.dev/submit"

VALID_TYPES = {"bug", "feature", "message"}
MAX_BODY_LEN = 8000
MAX_EMAIL_LEN = 254


def _get_installation_id() -> str:
    """Return a stable per-deployment UUID. Generates one on first call.

    Stored in app-settings.json. (grep INSTALLATION-ID-MIGRATION)
    """
    from app.admin.settings import _load_app_settings, _save_app_settings

    s = _load_app_settings()
    inst = s.get("installation_id")
    if isinstance(inst, str) and len(inst) >= 16:
        return inst

    inst = str(uuid.uuid4())
    s["installation_id"] = inst
    _save_app_settings(s)
    return inst


def _load_feedback_settings() -> dict:
    """Read feedback-related fields from app-settings.json."""
    from app.admin.settings import _load_app_settings  # local import avoids cycle
    s = _load_app_settings()
    return {
        "feedback_enabled": s.get("feedback_enabled", True),
        "feedback_relay_url": s.get("feedback_relay_url") or _DEFAULT_RELAY_URL,
        "turnstile_site_key": s.get("turnstile_site_key") or os.environ.get("TURNSTILE_SITE_KEY", ""),
    }


def _relay_config_url(submit_url: str) -> str:
    """Derive the relay's /config URL from its /submit URL."""
    parsed = urlparse(submit_url)
    return urlunparse(parsed._replace(path="/config", query="", fragment=""))


# Cache the relay's /config response for 10 min so we don't fetch on every
# UI mount. The site key is public and almost never changes.
_RELAY_CONFIG_CACHE: dict = {"value": None, "expires": 0.0}
_RELAY_CONFIG_TTL = 600.0


async def _fetch_relay_site_key(relay_url: str) -> str:
    """Best-effort fetch of the upstream relay's public Turnstile site key.

    Returns "" on any failure. Cached in-process for 10 minutes.
    """
    now = time.time()
    cached = _RELAY_CONFIG_CACHE
    if cached["value"] is not None and now < cached["expires"]:
        return cached["value"] or ""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(_relay_config_url(relay_url))
            if r.status_code == 200:
                data = r.json()
                key = (data or {}).get("turnstile_site_key", "") or ""
                _RELAY_CONFIG_CACHE.update({"value": key, "expires": now + _RELAY_CONFIG_TTL})
                return key
    except Exception as e:
        logger.debug("Relay /config fetch failed: %s", e)
    _RELAY_CONFIG_CACHE.update({"value": "", "expires": now + 60.0})
    return ""


# ── Models ────────────────────────────────────────────────────────────────

class FeedbackSubmit(BaseModel):
    type: str = Field(..., description="bug | feature | message")
    body: str = Field(..., min_length=1, max_length=MAX_BODY_LEN)
    email: Optional[str] = Field(default=None, max_length=MAX_EMAIL_LEN)
    turnstile_token: Optional[str] = Field(default=None, max_length=4096)
    # Honeypot — must stay empty for real users. A non-empty value means a bot
    # filled the hidden decoy field, so we silently drop the submission.
    website: Optional[str] = Field(default=None, max_length=256)

    @field_validator("type")
    @classmethod
    def _valid_type(cls, v: str) -> str:
        if v not in VALID_TYPES:
            raise ValueError(f"type must be one of {sorted(VALID_TYPES)}")
        return v

    @field_validator("email")
    @classmethod
    def _valid_email(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v == "":
            return None
        # Light validation — we just store the string in the issue body;
        # the user typed it, and the relay does its own sanity-check.
        if "@" not in v or "." not in v.split("@")[-1]:
            raise ValueError("invalid email")
        return v.strip()


class FeedbackConfig(BaseModel):
    enabled: bool
    turnstile_site_key: str
    # We don't expose the relay URL — the browser doesn't need to know it,
    # since submissions go through this FastAPI endpoint.


# ── Endpoints ─────────────────────────────────────────────────────────────

@router.get("/config", response_model=FeedbackConfig)
async def get_feedback_config() -> FeedbackConfig:
    """Public config the UI needs to render the feedback form.

    If no local turnstile_site_key is configured, fall back to the upstream
    relay's /config so a fresh clone inherits the right site key
    automatically without any manual setup.
    """
    s = _load_feedback_settings()
    site_key = s["turnstile_site_key"]
    if not site_key:
        site_key = await _fetch_relay_site_key(s["feedback_relay_url"])
    return FeedbackConfig(
        enabled=bool(s["feedback_enabled"]),
        turnstile_site_key=site_key,
    )


@router.post("")
async def submit_feedback(payload: FeedbackSubmit, request: Request) -> dict:
    # Honeypot tripped: a bot filled the hidden decoy field. Pretend success so
    # the bot can't tell it was caught, but never forward to the relay.
    if payload.website:
        logger.info("Feedback honeypot tripped; dropping submission silently")
        return {"ok": True, "issue_url": None}

    s = _load_feedback_settings()
    if not s["feedback_enabled"]:
        raise HTTPException(status_code=403, detail="Feedback is disabled on this deployment")

    relay_url = s["feedback_relay_url"]
    installation_id = _get_installation_id()
    app_version = os.environ.get("WEBAGENT_VERSION", "")
    client_ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (
        request.client.host if request.client else ""
    )

    relay_payload = {
        "type": payload.type,
        "body": payload.body,
        "email": payload.email,
        "turnstile_token": payload.turnstile_token,
        "installation_id": installation_id,
        "app_version": app_version,
        "client_ip_hint": client_ip,
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(relay_url, json=relay_payload)
    except httpx.HTTPError as e:
        logger.error("Feedback relay call failed: %s", e)
        raise HTTPException(status_code=502, detail="Feedback service unreachable")

    if r.status_code == 429:
        raise HTTPException(status_code=429, detail="Too many submissions. Please try again later.")
    if r.status_code >= 400:
        logger.warning("Feedback relay returned %s: %s", r.status_code, r.text[:500])
        raise HTTPException(status_code=502, detail="Feedback service rejected the submission")

    try:
        body = r.json()
    except ValueError:
        body = {}
    return {
        "ok": True,
        "issue_url": body.get("issue_url"),
    }
