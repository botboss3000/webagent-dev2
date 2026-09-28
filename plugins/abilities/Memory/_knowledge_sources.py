"""Shared profile storage helpers for connected Knowledge abilities.

Each ability owns its provider behavior and API surface. This module only
standardizes encrypted, agent-scoped connection records and authorization so
Document Libraries, SQL Databases, and Workspaces & Wikis present the same
security boundary without reviving the retired Config-tab connector system.
"""

from __future__ import annotations

import json
import re
import secrets
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import HTTPException, Request

from app.auth.identity import assert_caller_is


VAULT_USER = "admin"
_PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{5,63}$")


def as_dict(raw: Any) -> dict:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            value = json.loads(raw)
            return dict(value) if isinstance(value, dict) else {}
        except Exception:
            return {}
    return {}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def clean_list(values: Any, *, limit: int = 100, item_limit: int = 240) -> list[str]:
    if isinstance(values, str):
        values = values.split(",")
    out: list[str] = []
    for raw in values or []:
        value = str(raw or "").strip()
        if value and value not in out:
            out.append(value[:item_limit])
        if len(out) >= limit:
            break
    return out


def make_profile_id(prefix: str, requested: str = "") -> str:
    normalized = str(requested or "").strip().lower()
    if _PROFILE_RE.fullmatch(normalized):
        return normalized
    safe_prefix = re.sub(r"[^a-z0-9]", "", prefix.lower())[:8] or "src"
    return f"{safe_prefix}_{secrets.token_hex(8)}"


def label(agent_id: str, profile_id: str) -> str:
    return f"agent:{agent_id}:{profile_id}"


async def require_agent_admin(request: Request, agent_id: str, user_id: str) -> str:
    from app.api.agents import _is_agent_admin
    from app.db import get_db

    resolved = await assert_caller_is(request, user_id)
    if not await _is_agent_admin(get_db(), agent_id, resolved):
        raise HTTPException(status_code=403, detail="Only agent admins can manage knowledge connections.")
    return resolved


async def rows_for_agent(service: str, agent_id: str) -> list[dict]:
    from app.db import get_db

    rows = await get_db().auth_element_list(VAULT_USER, service)
    prefix = f"agent:{agent_id}:"
    return [row for row in (rows or []) if str(row.get("label") or "").startswith(prefix)]


def public_profile(agent_id: str, row: dict) -> dict:
    prefix = f"agent:{agent_id}:"
    raw_label = str(row.get("label") or "")
    config = as_dict(row.get("config"))
    return {
        **config,
        "id": raw_label[len(prefix):] if raw_label.startswith(prefix) else "",
        "secret_set": bool(str(row.get("secret_ref") or "").strip()),
    }


async def profiles(service: str, agent_id: str, *, include_secret: bool = False) -> list[dict]:
    found: list[dict] = []
    for row in await rows_for_agent(service, agent_id):
        item = public_profile(agent_id, row)
        if include_secret:
            item["secret"] = row.get("secret_ref") or ""
        found.append(item)
    found.sort(key=lambda item: (str(item.get("name") or "").casefold(), item.get("id") or ""))
    return found


async def find_profile(
    service: str, agent_id: str, profile_ref: str, *, include_secret: bool = True,
) -> Optional[dict]:
    ref = str(profile_ref or "").strip()
    for item in await profiles(service, agent_id, include_secret=include_secret):
        if item.get("id") == ref or str(item.get("name") or "").casefold() == ref.casefold():
            return item
    return None


async def save_profile(service: str, agent_id: str, profile_id: str, config: dict, secret: str = "") -> None:
    from app.db import get_db

    await get_db().auth_element_set(
        user_id=VAULT_USER,
        service=service,
        label=label(agent_id, profile_id),
        config=config,
        secret_ref=secret,
    )


async def delete_profile(service: str, agent_id: str, profile_id: str) -> bool:
    from app.db import get_db

    return bool(await get_db().auth_element_delete(VAULT_USER, service, label(agent_id, profile_id)))


async def runtime_settings(ability_id: str, agent_id: str) -> dict:
    from app.admin.ability_config import effective_ability_config
    from app.db import get_db

    per_agent: dict = {}
    try:
        rows = await get_db().get_agent_connections(agent_id)
        row = next((item for item in rows if item.get("section") == "ability" and item.get("connection_type") == ability_id), None)
        if row:
            per_agent = as_dict(row.get("config")).get("ability_settings", {}) or {}
    except Exception:
        pass
    return effective_ability_config(ability_id, per_agent)


def setting_on(settings: dict, key: str, default: bool = True) -> bool:
    value = settings.get(key, default)
    return str(value).strip().lower() not in {"0", "false", "off", "no", "disabled"}

