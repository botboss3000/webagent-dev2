"""Ownership-local storage for model credentials.

Model metadata stays in its owning app/user/agent authority. Secret-bearing
auth_elements rows are routed explicitly to the same ownership plane. Existing
rows in the installation vault are copied, verified, and removed on first access
so upgrades do not lose credentials.
"""
from __future__ import annotations

import json
from typing import Any


def backend_for(scope: str, *, user_id: str = "", agent_id: str = ""):
    from app.db import get_agent_db, get_app_db, get_user_db, is_remote_db

    scope = str(scope or "platform").strip().lower()
    if scope == "platform":
        return get_app_db()
    if is_remote_db():
        return get_app_db()
    if scope == "agent" and str(agent_id or "").strip():
        return get_agent_db(str(agent_id).strip())
    if scope == "user" and str(user_id or "").strip():
        return get_user_db(str(user_id).strip())
    raise ValueError("A valid model credential owner is required.")


def legacy_backend():
    from app.db import get_app_db
    return get_app_db()


def _config(value: Any) -> dict:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value or "{}")
            return dict(parsed) if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            return {}
    return {}


async def _verified_copy(destination, row: dict, *, owner: str, service: str, label: str):
    await destination.auth_element_set(
        user_id=owner, service=service, label=label,
        config=_config(row.get("config")),
        secret_ref=str(row.get("secret_ref") or ""),
    )
    copied = await destination.auth_element_get(owner, service, label)
    if not copied:
        raise RuntimeError("Owned model credential copy could not be verified.")
    if row.get("secret_ref") and not copied.get("secret_ref"):
        raise RuntimeError("Owned model credential secret copy could not be verified.")
    return copied


async def get_owned(scope: str, *, owner: str, service: str, label: str = "default",
                    user_id: str = "", agent_id: str = ""):
    destination = backend_for(scope, user_id=user_id, agent_id=agent_id)
    legacy = legacy_backend()
    row = await destination.auth_element_get(owner, service, label)
    if row:
        if destination is not legacy:
            old = await legacy.auth_element_get(owner, service, label)
            if old:
                await legacy.auth_element_delete(owner, service, label)
        return row
    if destination is legacy:
        return None
    old = await legacy.auth_element_get(owner, service, label)
    if not old:
        return None
    copied = await _verified_copy(destination, old, owner=owner, service=service, label=label)
    await legacy.auth_element_delete(owner, service, label)
    return copied


async def list_owned(scope: str, *, owner: str, service: str,
                     user_id: str = "", agent_id: str = "") -> list[dict]:
    """List one owner's service rows, migrating legacy rows before returning.

    Callers must project these rows before sending them to a browser: ``secret_ref``
    is intentionally present here so server-side owner-only reads remain possible.
    """
    destination = backend_for(scope, user_id=user_id, agent_id=agent_id)
    legacy = legacy_backend()
    rows = list(await destination.auth_element_list(owner, service) or [])
    labels = {str(row.get("label") or "") for row in rows if isinstance(row, dict)}
    if destination is legacy:
        return rows
    for old in list(await legacy.auth_element_list(owner, service) or []):
        if not isinstance(old, dict):
            continue
        label = str(old.get("label") or "")
        if not label:
            continue
        if label not in labels:
            copied = await _verified_copy(
                destination, old, owner=owner, service=service, label=label,
            )
            rows.append(copied)
            labels.add(label)
        await legacy.auth_element_delete(owner, service, label)
    return rows


async def set_owned(scope: str, *, owner: str, service: str, config: dict,
                    secret_ref: str = "", label: str = "default",
                    user_id: str = "", agent_id: str = ""):
    destination = backend_for(scope, user_id=user_id, agent_id=agent_id)
    saved = await destination.auth_element_set(
        user_id=owner, service=service, label=label,
        config=config, secret_ref=secret_ref,
    )
    verified = await destination.auth_element_get(owner, service, label)
    if not verified:
        raise RuntimeError("Owned model credential write could not be verified.")
    if secret_ref and not verified.get("secret_ref"):
        raise RuntimeError("Owned model credential secret write could not be verified.")
    legacy = legacy_backend()
    if destination is not legacy:
        await legacy.auth_element_delete(owner, service, label)
    return saved


async def delete_owned(scope: str, *, owner: str, service: str, label: str = "default",
                       user_id: str = "", agent_id: str = "") -> bool:
    destination = backend_for(scope, user_id=user_id, agent_id=agent_id)
    removed = await destination.auth_element_delete(owner, service, label)
    legacy = legacy_backend()
    if destination is not legacy:
        removed = await legacy.auth_element_delete(owner, service, label) or removed
    return bool(removed)
