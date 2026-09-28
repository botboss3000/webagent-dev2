"""Shared runtime helpers for credential-backed commerce integrations."""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Optional

import httpx


def json_result(value: Any) -> str:
    return json.dumps(value, default=str)


def not_configured(provider: str, fields: Iterable[str]) -> str:
    return json_result({
        "status": "not_configured",
        "provider": provider,
        "message": f"{provider} credentials are incomplete.",
        "required": list(fields),
    })


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [str(item) for item in parsed if str(item).strip()]
        except Exception:
            return [part.strip() for part in value.split(",") if part.strip()]
    return []


async def load_agent_credentials(provider: str, user_id: str, agent_id: str) -> dict:
    """Read provider credentials without exposing them to the tool caller.

    Agent-scoped credentials are saved under the administrator who configured
    the agent. Public/anonymous chat turns use a different caller id, so after
    checking the caller we also check the agent's administrator identities.
    """
    from app.abilities import credentials as ability_credentials

    candidates = [user_id] if user_id else []
    if agent_id:
        try:
            from app.db import get_db
            agent = await get_db().get_agent_by_id(agent_id)
            for admin_id in _as_list((agent or {}).get("admin_users")):
                if admin_id not in candidates:
                    candidates.append(admin_id)
        except Exception:
            pass

    for candidate in candidates:
        values = await ability_credentials.read_credentials(
            provider, user_id=candidate, agent_id=agent_id,
        )
        if values:
            return values
    return {}


async def request_json(
    provider: str,
    method: str,
    url: str,
    *,
    headers: Optional[dict[str, str]] = None,
    params: Optional[dict[str, Any]] = None,
    json_body: Any = None,
    data: Any = None,
    auth: Any = None,
    timeout: float = 25.0,
) -> dict:
    """Make a bounded provider request and normalize success/error output."""
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            response = await client.request(
                method.upper(), url, headers=headers, params=params,
                json=json_body, data=data, auth=auth,
            )
    except Exception as exc:
        return {
            "status": "error", "provider": provider,
            "message": f"{type(exc).__name__}: {exc}",
        }

    try:
        payload: Any = response.json()
    except Exception:
        payload = response.text[:20000]

    if response.status_code >= 400:
        return {
            "status": "error", "provider": provider,
            "http_status": response.status_code, "error": payload,
        }
    return {
        "status": "ok", "provider": provider,
        "http_status": response.status_code, "data": payload,
    }


def clamp_limit(value: int, maximum: int = 100) -> int:
    return max(1, min(int(value or 25), maximum))


def is_positive_amount(value: Any) -> bool:
    try:
        amount = Decimal(str(value))
        return amount.is_finite() and amount > 0
    except (InvalidOperation, ValueError):
        return False


def require_https_base(value: str, provider: str) -> tuple[str, Optional[str]]:
    base = str(value or "").strip().rstrip("/")
    if not base.startswith(("https://", "http://localhost", "http://127.0.0.1")):
        return "", f"{provider} base URL must use HTTPS (localhost is allowed for development)."
    return base, None
