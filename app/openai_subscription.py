"""Direct ChatGPT subscription transport, modelled after OpenCode's OAuth plugin.

This module owns OAuth only. WebAgent remains the agent harness and sends its
normal Responses requests directly to ChatGPT's subscription endpoint.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from typing import Any, Dict, Iterable, Mapping, Optional

import httpx

logger = logging.getLogger(__name__)

CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
ISSUER = "https://auth.openai.com"
RESPONSES_ENDPOINT = "https://chatgpt.com/backend-api/codex/responses"
DEVICE_VERIFICATION_URL = f"{ISSUER}/codex/device"
_DEVICE_START_URL = f"{ISSUER}/api/accounts/deviceauth/usercode"
_DEVICE_TOKEN_URL = f"{ISSUER}/api/accounts/deviceauth/token"
_TOKEN_URL = f"{ISSUER}/oauth/token"
_REDIRECT_URI = f"{ISSUER}/deviceauth/callback"
_PLATFORM_VAULT_USER = "_platform"
_VAULT_SERVICE = "openai_subscription_oauth"
_USER_AGENT = "webagent/1.0"
_REFRESH_SKEW_SECONDS = 60
DEVICE_LOGIN_TTL_SECONDS = 15 * 60

# OpenCode explicitly permits 5.3 Spark, 5.4/mini, and 5.5, then permits
# suffixed GPT releases newer than 5.4. Keep this subscription catalog distinct
# from the API-key model catalog.
_MODELS = (
    {"id": "gpt-5.3-codex-spark", "name": "GPT-5.3 Codex Spark"},
    {"id": "gpt-5.4", "name": "GPT-5.4"},
    {"id": "gpt-5.4-mini", "name": "GPT-5.4 mini"},
    {"id": "gpt-5.5", "name": "GPT-5.5"},
    {"id": "gpt-5.6-sol", "name": "GPT-5.6 Sol"},
    {"id": "gpt-5.6-terra", "name": "GPT-5.6 Terra"},
    {"id": "gpt-5.6-luna", "name": "GPT-5.6 Luna"},
)

_refresh_locks: dict[tuple[str, str], asyncio.Lock] = {}
_login_tasks: dict[tuple[str, str], asyncio.Task] = {}
_login_errors: dict[tuple[str, str], str] = {}
_ENTRY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def credential_owner(scope: str = "platform", *, user_id: str = "", agent_id: str = "") -> str:
    scope = str(scope or "platform").strip().lower()
    if scope == "platform":
        return _PLATFORM_VAULT_USER
    if scope == "agent" and str(agent_id or "").strip():
        return f"_agent:{str(agent_id).strip()}"
    if scope == "user" and str(user_id or "").strip() and not str(user_id).startswith("anon_"):
        return str(user_id).strip()
    raise ValueError("A valid credential tenant is required.")


def _entry_id(value: str) -> str:
    entry_id = str(value or "").strip()
    if not _ENTRY_ID_RE.fullmatch(entry_id):
        raise ValueError("Model entry ID is invalid.")
    return entry_id


def _tenant(scope: str = "platform", *, user_id: str = "", agent_id: str = "",
            entry_id: str = "") -> tuple[str, str]:
    return credential_owner(scope, user_id=user_id, agent_id=agent_id), _entry_id(entry_id)


async def reserve_entry(*, scope: str = "platform", user_id: str = "",
                        agent_id: str = "", entry_id: str = "") -> None:
    """Reserve a server-issued credential identity before a model is selected."""
    owner, entry_id = _tenant(
        scope, user_id=user_id, agent_id=agent_id, entry_id=entry_id,
    )
    from app import model_credentials
    current = await model_credentials.get_owned(
        scope, owner=owner, service=_VAULT_SERVICE, label=entry_id,
        user_id=user_id, agent_id=agent_id,
    )
    if current:
        return
    await model_credentials.set_owned(
        scope, owner=owner, service=_VAULT_SERVICE, label=entry_id,
        user_id=user_id, agent_id=agent_id,
        config={"pending": True, "reserved_at": int(time.time())},
        secret_ref="",
    )


async def entry_is_reserved(scope: str = "platform", *, user_id: str = "",
                            agent_id: str = "", entry_id: str = "") -> bool:
    """Return whether this credential ID belongs to the requested tenant."""
    owner, entry_id = _tenant(
        scope, user_id=user_id, agent_id=agent_id, entry_id=entry_id,
    )
    from app import model_credentials
    return bool(await model_credentials.get_owned(
        scope, owner=owner, service=_VAULT_SERVICE, label=entry_id,
        user_id=user_id, agent_id=agent_id,
    ))


async def existing_entries_with_reservations(
    entries: Iterable[Mapping[str, Any]],
    existing_entries: Iterable[Mapping[str, Any]],
    *,
    scope: str = "platform",
    user_id: str = "",
    agent_id: str = "",
) -> list[dict[str, Any]]:
    """Extend the ID allowlist with verified pending subscription identities."""
    accepted = [dict(item) for item in existing_entries if isinstance(item, Mapping)]
    accepted_ids = {str(item.get("entry_id") or "") for item in accepted}
    for raw in entries:
        if not isinstance(raw, Mapping):
            continue
        entry_id = str(raw.get("entry_id") or "").strip()
        subscription = (
            raw.get("provider") == "openai_subscription"
            or raw.get("auth_mode") == "chatgpt"
        )
        if not entry_id or entry_id in accepted_ids or not subscription:
            continue
        if await entry_is_reserved(
            scope, user_id=user_id, agent_id=agent_id, entry_id=entry_id,
        ):
            accepted.append({"entry_id": entry_id})
            accepted_ids.add(entry_id)
    return accepted


def _jwt_claims(token: str) -> Dict[str, Any]:
    parts = str(token or "").split(".")
    if len(parts) != 3:
        return {}
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        value = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def token_metadata(tokens: Dict[str, Any]) -> Dict[str, str]:
    claims = _jwt_claims(str(tokens.get("id_token") or ""))
    if not claims:
        claims = _jwt_claims(str(tokens.get("access_token") or ""))
    auth = claims.get("https://api.openai.com/auth") or {}
    if not isinstance(auth, dict):
        auth = {}
    organizations = claims.get("organizations") or []
    organization_id = ""
    if organizations and isinstance(organizations[0], dict):
        organization_id = str(organizations[0].get("id") or "")
    residency = str(
        auth.get("chatgpt_compute_residency")
        or claims.get("chatgpt_compute_residency")
        or ""
    )
    if residency == "no_constraint":
        residency = ""
    return {
        "account_id": str(
            claims.get("chatgpt_account_id")
            or auth.get("chatgpt_account_id")
            or organization_id
            or ""
        ),
        "residency": residency,
        "plan_type": str(
            auth.get("chatgpt_plan_type")
            or claims.get("chatgpt_plan_type")
            or ""
        ),
        "email": str(claims.get("email") or ""),
    }


async def _load_tokens(owner: str, entry_id: str, *, scope: str = "platform",
                       user_id: str = "", agent_id: str = "") -> Dict[str, Any]:
    from app import model_credentials
    row = await model_credentials.get_owned(
        scope, owner=owner, service=_VAULT_SERVICE, label=entry_id,
        user_id=user_id, agent_id=agent_id,
    )
    if not row or not row.get("secret_ref"):
        return {}
    try:
        value = json.loads(row["secret_ref"])
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


async def _save_tokens(tokens: Dict[str, Any], owner: str, entry_id: str, *,
                       scope: str = "platform", user_id: str = "",
                       agent_id: str = "") -> None:
    from app import model_credentials
    metadata = token_metadata(tokens)
    await model_credentials.set_owned(
        scope, owner=owner, service=_VAULT_SERVICE, label=entry_id,
        user_id=user_id, agent_id=agent_id,
        config={key: value for key, value in metadata.items() if value},
        secret_ref=json.dumps(tokens, separators=(",", ":")),
    )


async def _post_form(url: str, data: Dict[str, str]) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            url,
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "User-Agent": _USER_AGENT},
            data=data,
        )
    if response.status_code >= 400:
        raise RuntimeError(f"ChatGPT authorization HTTP {response.status_code}")
    value = response.json()
    if not isinstance(value, dict):
        raise RuntimeError("ChatGPT authorization returned an invalid response")
    return value


async def _post_json(url: str, data: Dict[str, str]) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            url,
            headers={"Content-Type": "application/json", "User-Agent": _USER_AGENT},
            json=data,
        )
    if response.status_code >= 400:
        raise RuntimeError(f"ChatGPT authorization HTTP {response.status_code}")
    value = response.json()
    if not isinstance(value, dict):
        raise RuntimeError("ChatGPT authorization returned an invalid response")
    return value


async def _refresh(tokens: Dict[str, Any], owner: str, entry_id: str, *,
                   scope: str = "platform", user_id: str = "",
                   agent_id: str = "") -> Dict[str, Any]:
    refresh_token = str(tokens.get("refresh_token") or "")
    if not refresh_token:
        raise RuntimeError("ChatGPT authorization expired; sign in again.")
    fresh = await _post_form(_TOKEN_URL, {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": CLIENT_ID,
    })
    merged = {
        **tokens,
        **fresh,
        "refresh_token": str(fresh.get("refresh_token") or refresh_token),
        "expires_at": time.time() + int(fresh.get("expires_in") or 3600),
    }
    metadata = token_metadata(merged)
    for key, value in metadata.items():
        if value:
            merged[key] = value
    await _save_tokens(
        merged, owner, entry_id, scope=scope, user_id=user_id, agent_id=agent_id,
    )
    return merged


async def transport_auth(*, scope: str = "platform", user_id: str = "", agent_id: str = "",
                         entry_id: str = "") -> Dict[str, str]:
    """Return current auth for the explicitly selected credential tenant."""
    owner, entry_id = _tenant(
        scope, user_id=user_id, agent_id=agent_id, entry_id=entry_id,
    )
    lock = _refresh_locks.setdefault((owner, entry_id), asyncio.Lock())
    async with lock:
        tokens = await _load_tokens(
            owner, entry_id, scope=scope, user_id=user_id, agent_id=agent_id,
        )
        if not tokens.get("access_token"):
            raise RuntimeError("Authorize a ChatGPT subscription for this model account first.")
        expires_at = float(tokens.get("expires_at") or 0)
        if not expires_at or expires_at <= time.time() + _REFRESH_SKEW_SECONDS:
            tokens = await _refresh(
                tokens, owner, entry_id,
                scope=scope, user_id=user_id, agent_id=agent_id,
            )
        metadata = token_metadata(tokens)
        return {
            "access_token": str(tokens.get("access_token") or ""),
            "account_id": str(tokens.get("account_id") or metadata["account_id"]),
            "residency": str(tokens.get("residency") or metadata["residency"]),
        }


async def _poll_device_login(device: Dict[str, Any], owner: str, entry_id: str, *,
                             scope: str = "platform", user_id: str = "",
                             agent_id: str = "") -> None:
    key = (owner, entry_id)
    interval = max(int(device.get("interval") or 5), 1) + 3
    deadline = time.monotonic() + DEVICE_LOGIN_TTL_SECONDS
    while time.monotonic() < deadline:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                _DEVICE_TOKEN_URL,
                headers={"Content-Type": "application/json", "User-Agent": _USER_AGENT},
                json={
                    "device_auth_id": str(device.get("device_auth_id") or ""),
                    "user_code": str(device.get("user_code") or ""),
                },
            )
        if response.status_code == 200:
            authorization = response.json()
            tokens = await _post_form(_TOKEN_URL, {
                "grant_type": "authorization_code",
                "code": str(authorization.get("authorization_code") or ""),
                "redirect_uri": _REDIRECT_URI,
                "client_id": CLIENT_ID,
                "code_verifier": str(authorization.get("code_verifier") or ""),
            })
            tokens["expires_at"] = time.time() + int(tokens.get("expires_in") or 3600)
            tokens.update({k: v for k, v in token_metadata(tokens).items() if v})
            await _save_tokens(
                tokens, owner, entry_id,
                scope=scope, user_id=user_id, agent_id=agent_id,
            )
            _login_errors.pop(key, None)
            return
        if response.status_code not in {403, 404}:
            _login_errors[key] = f"ChatGPT device authorization HTTP {response.status_code}"
            return
        await asyncio.sleep(interval)
    _login_errors[key] = "ChatGPT device authorization timed out."
    await _delete_pending_credential(
        owner, entry_id, scope=scope, user_id=user_id, agent_id=agent_id,
    )


def _consume_login_result(key: tuple[str, str], task: asyncio.Task) -> None:
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        logger.warning("ChatGPT device authorization failed: %s", exc)
        _login_errors[key] = str(exc)


async def start_device_login(*, scope: str = "platform", user_id: str = "",
                             agent_id: str = "", entry_id: str = "") -> Dict[str, Any]:
    owner, entry_id = _tenant(
        scope, user_id=user_id, agent_id=agent_id, entry_id=entry_id,
    )
    key = (owner, entry_id)
    device = await _post_json(_DEVICE_START_URL, {"client_id": CLIENT_ID})
    user_code = str(device.get("user_code") or "")
    device_id = str(device.get("device_auth_id") or "")
    if not user_code or not device_id:
        raise RuntimeError("ChatGPT did not return device authorization details.")
    current = _login_tasks.get(key)
    if current and not current.done():
        current.cancel()
    _login_errors.pop(key, None)
    task = asyncio.create_task(_poll_device_login(
        device, owner, entry_id, scope=scope, user_id=user_id, agent_id=agent_id,
    ))
    _login_tasks[key] = task
    task.add_done_callback(lambda done: _consume_login_result(key, done))
    return {
        "verification_url": DEVICE_VERIFICATION_URL,
        "user_code": user_code,
        "expires_in_seconds": DEVICE_LOGIN_TTL_SECONDS,
        "expires_at": int(time.time()) + DEVICE_LOGIN_TTL_SECONDS,
    }


async def _delete_pending_credential(owner: str, entry_id: str, *,
                                     scope: str = "platform", user_id: str = "",
                                     agent_id: str = "") -> bool:
    from app import model_credentials
    row = await model_credentials.get_owned(
        scope, owner=owner, service=_VAULT_SERVICE, label=entry_id,
        user_id=user_id, agent_id=agent_id,
    )
    config = row.get("config") if row else {}
    if isinstance(config, str):
        try:
            config = json.loads(config or "{}")
        except (TypeError, ValueError):
            config = {}
    pending_only = bool(
        row and not row.get("secret_ref")
        and isinstance(config, dict) and config.get("pending")
    )
    if pending_only:
        await model_credentials.delete_owned(
            scope, owner=owner, service=_VAULT_SERVICE, label=entry_id,
            user_id=user_id, agent_id=agent_id,
        )
    return pending_only


async def reset_device_login(*, scope: str = "platform", user_id: str = "",
                             agent_id: str = "", entry_id: str = "") -> bool:
    """Cancel this local device attempt without disconnecting a saved account."""
    owner, entry_id = _tenant(
        scope, user_id=user_id, agent_id=agent_id, entry_id=entry_id,
    )
    key = (owner, entry_id)
    task = _login_tasks.pop(key, None)
    if task and not task.done():
        task.cancel()
    _login_errors.pop(key, None)

    # A code issued before model selection owns only a placeholder credential.
    # Remove that placeholder on reset, while preserving credentials belonging
    # to an already-connected model row.
    return await _delete_pending_credential(
        owner, entry_id, scope=scope, user_id=user_id, agent_id=agent_id,
    )


async def sign_out(*, scope: str = "platform", user_id: str = "", agent_id: str = "",
                   entry_id: str = "") -> None:
    owner, entry_id = _tenant(
        scope, user_id=user_id, agent_id=agent_id, entry_id=entry_id,
    )
    key = (owner, entry_id)
    task = _login_tasks.pop(key, None)
    if task and not task.done():
        task.cancel()
    _login_errors.pop(key, None)
    from app import model_credentials
    await model_credentials.delete_owned(
        scope, owner=owner, service=_VAULT_SERVICE, label=entry_id,
        user_id=user_id, agent_id=agent_id,
    )


async def status(*, scope: str = "platform", user_id: str = "", agent_id: str = "",
                 entry_id: str = "") -> Dict[str, Any]:
    owner, entry_id = _tenant(
        scope, user_id=user_id, agent_id=agent_id, entry_id=entry_id,
    )
    key = (owner, entry_id)
    tokens = await _load_tokens(
        owner, entry_id, scope=scope, user_id=user_id, agent_id=agent_id,
    )
    metadata = token_metadata(tokens)
    signed_in = bool(tokens.get("access_token") and tokens.get("refresh_token"))
    task = _login_tasks.get(key)
    return {
        "installed": True,
        "signed_in": signed_in,
        "chatgpt_authorized": signed_in,
        "detail": (
            f"ChatGPT {tokens.get('plan_type') or metadata['plan_type'] or 'subscription'}"
            if signed_in else (_login_errors.get(key) or "Not authorized with a ChatGPT subscription.")
        ),
        "account": {
            "type": "chatgpt",
            "email": tokens.get("email") or metadata["email"],
        } if signed_in else None,
        "plan_type": tokens.get("plan_type") or metadata["plan_type"] or None,
        "rate_limits": {},
        "login_pending": bool(task and not task.done()),
        "credential_scope": scope,
        "entry_id": entry_id,
    }


def models() -> list[dict]:
    return [{
        **item,
        "description": "Available through the connected ChatGPT subscription.",
        "text_capable": True,
        "image_capable": True,
        "image_out_capable": False,
        "voice_capable": False,
        "input_modalities": ["text", "image"],
        "output_modalities": ["text"],
        "tool_capable": True,
        "modality_known": True,
        "reasoning_efforts": ["low", "medium", "high", "xhigh"],
        "default_reasoning_effort": "medium",
        "is_default": item["id"] == "gpt-5.5",
    } for item in _MODELS]
