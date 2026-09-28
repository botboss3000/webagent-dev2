"""Per-agent anonymous publication, funding, usage, and data policy."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException


PLATFORM_SHOWCASE_AGENT_ID = "shared_default"
FUNDING_MODES = {"platform_showcase", "owner_wallet", "dedicated_key", "inherited_global"}
NON_DELEGABLE_ABILITY_GROUPS = {"platform_admin", "platform_infra", "tool_creation"}
NON_DELEGABLE_TOOLS = {
    "create_tool", "db_query", "read_source", "write_source", "edit_source",
    "delete_source", "resolve_conflict", "commit_and_push", "run_command",
    "restart_server", "terminal_open", "terminal_send", "terminal_close",
}

DEFAULT_ANONYMOUS_DATA = {
    "session_retention_days": 14,
    "max_sessions_per_guest": 5,
    "max_transcript_bytes_per_guest": 1024 * 1024,
    "max_total_storage_bytes": 1024 * 1024 * 1024,
}
DEFAULT_ANONYMOUS_USAGE = {
    "turns_per_agent_per_day": 5000,
    "concurrent_runs": 10,
    "tokens_per_guest_per_day": 100000,
    "tokens_per_agent_per_month": 5000000,
    "cost_cents_per_agent_per_month": 5000,
}


def _metadata(agent: dict) -> dict:
    value = agent.get("metadata") or {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            value = {}
    return value if isinstance(value, dict) else {}


def normalize_public_access(agent: dict) -> dict:
    """Return the safe effective public policy for an agent."""
    meta = _metadata(agent)
    raw = meta.get("public_access") if isinstance(meta.get("public_access"), dict) else {}
    funding = raw.get("funding") if isinstance(raw.get("funding"), dict) else {}
    data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
    usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
    capabilities = raw.get("capabilities") if isinstance(raw.get("capabilities"), dict) else {}
    mode = str(funding.get("mode") or "").strip().lower()
    if str(agent.get("id") or "") == PLATFORM_SHOWCASE_AGENT_ID:
        mode = "platform_showcase"
    return {
        "enabled": bool(raw.get("enabled", (agent.get("user_mode") or "anonymous") == "anonymous")),
        "funding": {
            "mode": mode,
            "owner_user_id": str(funding.get("owner_user_id") or meta.get("owner_user_id") or ""),
            "credential_binding_id": str(funding.get("credential_binding_id") or ""),
            "fallback": "deny",
        },
        "data": {**DEFAULT_ANONYMOUS_DATA, **{k: data[k] for k in DEFAULT_ANONYMOUS_DATA if k in data}},
        "usage": {**DEFAULT_ANONYMOUS_USAGE, **{k: usage[k] for k in DEFAULT_ANONYMOUS_USAGE if k in usage}},
        "capabilities": {
            "mode": "showcase" if str(agent.get("id") or "") == PLATFORM_SHOWCASE_AGENT_ID
                    else str(capabilities.get("mode") or "explicit"),
            "abilities": [str(v) for v in (capabilities.get("abilities") or []) if isinstance(v, str)],
            "tools": [str(v) for v in (capabilities.get("tools") or []) if isinstance(v, str)],
            "features": [str(v) for v in (capabilities.get("features") or []) if isinstance(v, str)],
        },
        "chat_ui": raw.get("chat_ui") if isinstance(raw.get("chat_ui"), dict) else {},
    }


def is_showcase_policy(agent: dict) -> bool:
    return normalize_public_access(agent)["capabilities"]["mode"] == "showcase"


async def _owner_id(db: Any, agent: dict, policy: dict) -> str:
    configured = str(policy["funding"].get("owner_user_id") or "")
    if configured:
        return configured
    try:
        roles = await db.get_agent_roles(str(agent.get("id") or ""))
        admins = roles.get("admin_users") or []
        return str(admins[0]) if admins else ""
    except Exception:
        return ""


async def _has_inherited_global_model(owner_id: str) -> bool:
    """Whether an owner has a credential-backed global model that agents inherit.

    Public anonymous runs already resolve their provider configuration as the agent
    owner (see ``apply_provider_for_run``).  Treat that inherited configuration as
    a valid sponsor only when the app-level inheritance switch is on and there is
    an actual resolved provider/model pair. Platform-managed credentials are not
    necessarily represented as a raw ``api_key`` in the resolved configuration.
    """
    if not owner_id:
        return False
    try:
        from app.admin.settings import _load_app_settings, _resolve_user_config
        if _load_app_settings().get("extend_llm_to_agents", True) is False:
            return False
        config = await _resolve_user_config(owner_id)
        return bool(config.get("model") and config.get("provider"))
    except Exception:
        return False


async def public_funding_status(db: Any, agent: dict) -> dict:
    policy = normalize_public_access(agent)
    mode = policy["funding"]["mode"]
    owner_id = await _owner_id(db, agent, policy)
    if mode == "platform_showcase":
        valid = str(agent.get("id") or "") == PLATFORM_SHOWCASE_AGENT_ID
        reason = "platform_showcase" if valid else "platform_sponsorship_reserved"
    elif mode == "dedicated_key":
        from plugins.billing.pricing import _is_byo_llm
        valid = bool(_is_byo_llm(agent))
        reason = "dedicated_key" if valid else "dedicated_key_missing"
    elif mode == "owner_wallet":
        valid = False
        if owner_id:
            try:
                from plugins.billing.wallet import get_balance
                from app.db import get_app_db
                wallet = await get_balance(get_app_db(), owner_id)
                valid = bool(wallet and wallet.available_cents > 0)
            except Exception:
                valid = False
        reason = "owner_wallet" if valid else "owner_wallet_empty"
    elif not mode:
        valid = await _has_inherited_global_model(owner_id)
        mode = "inherited_global" if valid else ""
        reason = "inherited_global" if valid else "inherited_global_model_unavailable"
    else:
        valid, reason = False, "funding_not_configured"
    return {
        "valid": valid, "mode": mode, "reason": reason,
        "owner_user_id": owner_id, "fallback": "deny", "policy": policy,
    }


async def require_public_funding(db: Any, agent: dict) -> dict:
    status = await public_funding_status(db, agent)
    if not status["valid"]:
        raise HTTPException(
            status_code=402,
            detail={
                "code": "public_agent_funding_unavailable",
                "message": "This public agent is temporarily unavailable.",
            },
        )
    return status


async def consume_public_turn_budget(db: Any, agent: dict, user_id: str, message: str = "") -> dict:
    """Consume the agent's turn, token, and cost sponsorship allowances."""
    status = await require_public_funding(db, agent)
    usage = status["policy"]["usage"]
    maximum = max(1, int(usage["turns_per_agent_per_day"]))
    from app.api.rate_limit import (
        _estimate_admission, _persistent_consume_many, _persistent_hit,
        anon_native_controls,
    )
    import asyncio
    allowed = await asyncio.to_thread(
        _persistent_hit,
        f"public-agent-daily:{agent.get('id')}", maximum, 86400,
    )
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail={
                "code": "public_agent_budget_exhausted",
                "message": "This public agent has reached its usage allowance.",
                "limit": maximum,
                "window_seconds": 86400,
            },
            headers={"Retry-After": "86400"},
    )
    tokens, cost_microusd = _estimate_admission(message, anon_native_controls())
    if status["mode"] == "owner_wallet":
        # Fail before model work when the sponsor cannot cover even the
        # conservative admission estimate. The post-run charge still settles
        # against actual provider cost.
        import math
        from app.db import get_app_db
        from plugins.billing.wallet import get_balance
        wallet = await get_balance(get_app_db(), status["owner_user_id"])
        admission_cents = max(1, int(math.ceil(cost_microusd / 10000)))
        if not wallet or wallet.available_cents < admission_cents:
            raise HTTPException(
                status_code=402,
                detail={
                    "code": "public_agent_funding_unavailable",
                    "message": "This public agent is temporarily unavailable.",
                },
            )
    guest_tokens = max(1, int(usage["tokens_per_guest_per_day"]))
    guest = await asyncio.to_thread(
        _persistent_consume_many,
        [(f"public-agent-guest-tokens:{agent.get('id')}:{user_id}", tokens, guest_tokens)],
        86400,
    )
    if not guest["allowed"]:
        raise HTTPException(
            status_code=429,
            detail={"code": "public_agent_guest_budget_exhausted",
                    "message": "This guest has reached the agent's usage allowance."},
            headers={"Retry-After": "86400"},
        )
    month_tokens = max(1, int(usage["tokens_per_agent_per_month"]))
    month_cost = max(1, int(usage["cost_cents_per_agent_per_month"])) * 10000
    monthly = await asyncio.to_thread(
        _persistent_consume_many,
        [
            (f"public-agent-month-tokens:{agent.get('id')}", tokens, month_tokens),
            (f"public-agent-month-cost:{agent.get('id')}", cost_microusd, month_cost),
        ],
        30 * 86400,
    )
    if not monthly["allowed"]:
        raise HTTPException(
            status_code=429,
            detail={"code": "public_agent_sponsor_budget_exhausted",
                    "message": "This public agent has reached its sponsored usage budget."},
            headers={"Retry-After": str(30 * 86400)},
        )
    return status


async def validate_publication(db: Any, agent: dict, public_access: dict) -> dict:
    """Validate an owner-authored policy before making an agent public."""
    candidate = dict(agent)
    meta = _metadata(agent)
    meta["public_access"] = public_access
    candidate["metadata"] = meta
    capabilities = public_access.get("capabilities") if isinstance(public_access, dict) else {}
    capabilities = capabilities if isinstance(capabilities, dict) else {}
    abilities = [str(value) for value in capabilities.get("abilities") or []]
    tools = [str(value) for value in capabilities.get("tools") or []]
    if "*" in abilities or "*" in tools:
        raise HTTPException(
            status_code=400,
            detail={"code": "public_capability_wildcard_forbidden",
                    "message": "Public abilities and tools must be explicitly selected."},
        )
    from app.entitlements.abilities import ability_group
    blocked_abilities = [
        ability for ability in abilities
        if ability_group(ability) in NON_DELEGABLE_ABILITY_GROUPS
    ]
    blocked_tools = sorted(set(tools) & NON_DELEGABLE_TOOLS)
    if blocked_abilities or blocked_tools:
        raise HTTPException(
            status_code=400,
            detail={"code": "public_capability_forbidden",
                    "abilities": blocked_abilities, "tools": blocked_tools},
        )
    status = await public_funding_status(db, candidate)
    if not status["valid"]:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "public_agent_funding_required",
                "reason": status["reason"],
                "message": "Configure an inherited global model, a funded owner wallet, or a dedicated agent model key before publishing.",
            },
        )
    return status["policy"]
