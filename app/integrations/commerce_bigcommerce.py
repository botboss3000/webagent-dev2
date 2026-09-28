"""BigCommerce REST Management API tools."""

from __future__ import annotations

from typing import Any

from app.integrations.commerce_common import clamp_limit, json_result, load_agent_credentials, not_configured, request_json


async def _config(user_id: str, agent_id: str) -> tuple[dict, str]:
    creds = await load_agent_credentials("bigcommerce", user_id, agent_id)
    required = ("store_hash", "access_token")
    if not all(str(creds.get(key) or "").strip() for key in required):
        return {}, not_configured("bigcommerce", required)
    store_hash = str(creds["store_hash"]).strip()
    if any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" for ch in store_hash):
        return {}, json_result({"status": "error", "provider": "bigcommerce", "message": "Invalid store hash."})
    return {
        **creds,
        "base": f"https://api.bigcommerce.com/stores/{store_hash}",
        "headers": {"X-Auth-Token": creds["access_token"], "Accept": "application/json"},
    }, ""


async def _call(user_id: str, agent_id: str, method: str, path: str, *, params=None, body=None) -> str:
    cfg, error = await _config(user_id, agent_id)
    if error:
        return error
    result = await request_json(
        "bigcommerce", method, f"{cfg['base']}/{path.lstrip('/')}",
        headers=cfg["headers"], params=params, json_body=body,
    )
    return json_result(result)


async def bigcommerce_list_products(user_id: str, agent_id: str, keyword: str = "", limit: int = 25) -> str:
    params: dict[str, Any] = {"limit": clamp_limit(limit, 250)}
    if keyword:
        params["keyword"] = keyword
    return await _call(user_id, agent_id, "GET", "v3/catalog/products", params=params)


async def bigcommerce_get_product(user_id: str, agent_id: str, product_id: int) -> str:
    return await _call(user_id, agent_id, "GET", f"v3/catalog/products/{int(product_id)}", params={"include": "variants"})


async def bigcommerce_list_orders(user_id: str, agent_id: str, email: str = "", limit: int = 25) -> str:
    params: dict[str, Any] = {"limit": clamp_limit(limit, 250)}
    if email:
        params["email"] = email
    return await _call(user_id, agent_id, "GET", "v2/orders", params=params)


async def bigcommerce_create_checkout(user_id: str, agent_id: str, line_items: list[dict], currency: str = "USD") -> str:
    if not line_items:
        return json_result({"status": "error", "provider": "bigcommerce", "message": "line_items is required"})
    body = {"line_items": line_items[:100], "currency": currency.upper()}
    return await _call(user_id, agent_id, "POST", "v3/carts", params={"include": "redirect_urls"}, body=body)


TOOLS = [
    {"name": "bigcommerce_list_products", "provider": "bigcommerce", "handler": bigcommerce_list_products,
     "destructive": False, "parameters": {"type": "object", "properties": {
         "keyword": {"type": "string", "default": ""}, "limit": {"type": "integer", "default": 25}}, "required": []}},
    {"name": "bigcommerce_get_product", "provider": "bigcommerce", "handler": bigcommerce_get_product,
     "destructive": False, "parameters": {"type": "object", "properties": {
         "product_id": {"type": "integer"}}, "required": ["product_id"]}},
    {"name": "bigcommerce_list_orders", "provider": "bigcommerce", "handler": bigcommerce_list_orders,
     "destructive": False, "parameters": {"type": "object", "properties": {
         "email": {"type": "string", "default": ""}, "limit": {"type": "integer", "default": 25}}, "required": []}},
    {"name": "bigcommerce_create_checkout", "provider": "bigcommerce", "handler": bigcommerce_create_checkout,
     "destructive": True, "requires_confirmation": True, "parameters": {"type": "object", "properties": {
         "line_items": {"type": "array", "items": {"type": "object"}},
         "currency": {"type": "string", "default": "USD"}}, "required": ["line_items"]}},
]
