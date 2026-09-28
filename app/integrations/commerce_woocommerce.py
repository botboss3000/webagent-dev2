"""WooCommerce REST API tools."""

from __future__ import annotations

from typing import Any, Optional

from app.integrations.commerce_common import (
    clamp_limit, json_result, load_agent_credentials, not_configured,
    request_json, require_https_base,
)


async def _config(user_id: str, agent_id: str) -> tuple[dict, str]:
    creds = await load_agent_credentials("woocommerce", user_id, agent_id)
    required = ("base_url", "consumer_key", "consumer_secret")
    if not all(str(creds.get(key) or "").strip() for key in required):
        return {}, not_configured("woocommerce", required)
    base, error = require_https_base(creds["base_url"], "WooCommerce")
    if error:
        return {}, json_result({"status": "error", "provider": "woocommerce", "message": error})
    return {**creds, "api": f"{base}/wp-json/wc/v3"}, ""


async def _call(user_id: str, agent_id: str, method: str, path: str, *, params=None, body=None) -> str:
    cfg, error = await _config(user_id, agent_id)
    if error:
        return error
    result = await request_json(
        "woocommerce", method, f"{cfg['api']}/{path.lstrip('/')}",
        params=params, json_body=body,
        auth=(cfg["consumer_key"], cfg["consumer_secret"]),
    )
    return json_result(result)


async def woocommerce_list_products(user_id: str, agent_id: str, search: str = "", sku: str = "", limit: int = 25) -> str:
    params: dict[str, Any] = {"per_page": clamp_limit(limit)}
    if search:
        params["search"] = search
    if sku:
        params["sku"] = sku
    return await _call(user_id, agent_id, "GET", "products", params=params)


async def woocommerce_get_product(user_id: str, agent_id: str, product_id: int) -> str:
    return await _call(user_id, agent_id, "GET", f"products/{int(product_id)}")


async def woocommerce_list_orders(user_id: str, agent_id: str, status: str = "any", search: str = "", limit: int = 25) -> str:
    params: dict[str, Any] = {"per_page": clamp_limit(limit), "status": status or "any"}
    if search:
        params["search"] = search
    return await _call(user_id, agent_id, "GET", "orders", params=params)


async def woocommerce_create_checkout(
    user_id: str, agent_id: str, line_items: list[dict],
    customer_note: str = "", billing: Optional[dict] = None,
) -> str:
    if not line_items:
        return json_result({"status": "error", "provider": "woocommerce", "message": "line_items is required"})
    body: dict[str, Any] = {
        "status": "pending", "set_paid": False,
        "line_items": line_items[:100],
    }
    if customer_note:
        body["customer_note"] = customer_note
    if billing:
        body["billing"] = billing
    return await _call(user_id, agent_id, "POST", "orders", body=body)


TOOLS = [
    {"name": "woocommerce_list_products", "provider": "woocommerce", "handler": woocommerce_list_products,
     "destructive": False, "parameters": {"type": "object", "properties": {
         "search": {"type": "string", "default": ""}, "sku": {"type": "string", "default": ""},
         "limit": {"type": "integer", "default": 25}}, "required": []}},
    {"name": "woocommerce_get_product", "provider": "woocommerce", "handler": woocommerce_get_product,
     "destructive": False, "parameters": {"type": "object", "properties": {
         "product_id": {"type": "integer"}}, "required": ["product_id"]}},
    {"name": "woocommerce_list_orders", "provider": "woocommerce", "handler": woocommerce_list_orders,
     "destructive": False, "parameters": {"type": "object", "properties": {
         "status": {"type": "string", "default": "any"}, "search": {"type": "string", "default": ""},
         "limit": {"type": "integer", "default": 25}}, "required": []}},
    {"name": "woocommerce_create_checkout", "provider": "woocommerce", "handler": woocommerce_create_checkout,
     "destructive": True, "requires_confirmation": True, "parameters": {"type": "object", "properties": {
         "line_items": {"type": "array", "items": {"type": "object"}},
         "customer_note": {"type": "string", "default": ""},
         "billing": {"type": "object"}}, "required": ["line_items"]}},
]
