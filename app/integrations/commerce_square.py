"""Square catalog, orders, and hosted checkout tools."""

from __future__ import annotations

import uuid
from typing import Any, Optional

from app.integrations.commerce_common import clamp_limit, json_result, load_agent_credentials, not_configured, request_json

_SQUARE_VERSION = "2026-07-15"


async def _config(user_id: str, agent_id: str) -> tuple[dict, str]:
    creds = await load_agent_credentials("square", user_id, agent_id)
    required = ("access_token", "location_id")
    if not all(str(creds.get(key) or "").strip() for key in required):
        return {}, not_configured("square", required)
    sandbox = str(creds.get("environment") or "sandbox").lower() != "live"
    return {
        **creds,
        "base": "https://connect.squareupsandbox.com" if sandbox else "https://connect.squareup.com",
        "headers": {
            "Authorization": f"Bearer {creds['access_token']}",
            "Square-Version": _SQUARE_VERSION,
            "Content-Type": "application/json",
        },
    }, ""


async def _call(user_id: str, agent_id: str, method: str, path: str, *, params=None, body=None) -> str:
    cfg, error = await _config(user_id, agent_id)
    if error:
        return error
    result = await request_json("square", method, f"{cfg['base']}/{path.lstrip('/')}", headers=cfg["headers"], params=params, json_body=body)
    return json_result(result)


async def square_list_catalog(user_id: str, agent_id: str, cursor: str = "", object_types: str = "ITEM") -> str:
    params: dict[str, Any] = {"types": object_types or "ITEM"}
    if cursor:
        params["cursor"] = cursor
    return await _call(user_id, agent_id, "GET", "v2/catalog/list", params=params)


async def square_search_orders(user_id: str, agent_id: str, limit: int = 25, cursor: str = "") -> str:
    cfg, error = await _config(user_id, agent_id)
    if error:
        return error
    body: dict[str, Any] = {"location_ids": [cfg["location_id"]], "limit": clamp_limit(limit, 100)}
    if cursor:
        body["cursor"] = cursor
    result = await request_json("square", "POST", f"{cfg['base']}/v2/orders/search", headers=cfg["headers"], json_body=body)
    return json_result(result)


async def square_get_inventory(
    user_id: str, agent_id: str, catalog_object_ids: list[str], cursor: str = "",
) -> str:
    if not catalog_object_ids:
        return json_result({"status": "error", "provider": "square", "message": "catalog_object_ids is required"})
    params: dict[str, Any] = {
        "catalog_object_ids": ",".join(str(value) for value in catalog_object_ids[:100]),
    }
    if cursor:
        params["cursor"] = cursor
    return await _call(user_id, agent_id, "GET", "v2/inventory/counts", params=params)


async def square_create_payment_link(
    user_id: str, agent_id: str, name: str, amount_cents: int,
    currency: str = "USD", redirect_url: str = "", idempotency_key: str = "",
) -> str:
    cfg, error = await _config(user_id, agent_id)
    if error:
        return error
    if not name or int(amount_cents) <= 0:
        return json_result({"status": "error", "provider": "square", "message": "name and positive amount_cents are required"})
    body: dict[str, Any] = {
        "idempotency_key": idempotency_key or str(uuid.uuid4()),
        "quick_pay": {
            "name": name,
            "price_money": {"amount": int(amount_cents), "currency": currency.upper()},
            "location_id": cfg["location_id"],
        },
    }
    if redirect_url:
        body["checkout_options"] = {"redirect_url": redirect_url}
    result = await request_json("square", "POST", f"{cfg['base']}/v2/online-checkout/payment-links", headers=cfg["headers"], json_body=body)
    return json_result(result)


TOOLS = [
    {"name": "square_list_catalog", "provider": "square", "handler": square_list_catalog,
     "destructive": False, "parameters": {"type": "object", "properties": {
         "cursor": {"type": "string", "default": ""}, "object_types": {"type": "string", "default": "ITEM"}}, "required": []}},
    {"name": "square_search_orders", "provider": "square", "handler": square_search_orders,
     "destructive": False, "parameters": {"type": "object", "properties": {
         "limit": {"type": "integer", "default": 25}, "cursor": {"type": "string", "default": ""}}, "required": []}},
    {"name": "square_get_inventory", "provider": "square", "handler": square_get_inventory,
     "destructive": False, "parameters": {"type": "object", "properties": {
         "catalog_object_ids": {"type": "array", "items": {"type": "string"}},
         "cursor": {"type": "string", "default": ""}}, "required": ["catalog_object_ids"]}},
    {"name": "square_create_payment_link", "provider": "square", "handler": square_create_payment_link,
     "destructive": True, "requires_confirmation": True, "parameters": {"type": "object", "properties": {
         "name": {"type": "string"}, "amount_cents": {"type": "integer"},
         "currency": {"type": "string", "default": "USD"}, "redirect_url": {"type": "string", "default": ""},
         "idempotency_key": {"type": "string", "default": ""}}, "required": ["name", "amount_cents"]}},
]
