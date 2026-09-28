"""Stripe catalog and hosted Checkout tools."""

from __future__ import annotations

from typing import Any

from app.integrations.commerce_common import (
    clamp_limit, json_result, load_agent_credentials, not_configured, request_json,
)
from app.tools.provider_idempotency import stripe_headers

_BASE = "https://api.stripe.com/v1"


async def _config(user_id: str, agent_id: str) -> tuple[dict, str]:
    creds = await load_agent_credentials("stripe", user_id, agent_id)
    if not str(creds.get("secret_key") or "").strip():
        return {}, not_configured("stripe", ("secret_key",))
    return creds, ""


async def _call(user_id: str, agent_id: str, method: str, path: str, *, params=None, data=None, idempotent=False) -> str:
    cfg, error = await _config(user_id, agent_id)
    if error:
        return error
    headers = stripe_headers() if idempotent else {}
    result = await request_json(
        "stripe", method, f"{_BASE}/{path.lstrip('/')}", headers=headers,
        params=params, data=data, auth=(cfg["secret_key"], ""),
    )
    return json_result(result)


async def stripe_list_products(user_id: str, agent_id: str, active: bool = True, limit: int = 25) -> str:
    return await _call(
        user_id, agent_id, "GET", "products",
        params={"active": str(bool(active)).lower(), "limit": clamp_limit(limit, 100)},
    )


async def stripe_get_checkout_session(user_id: str, agent_id: str, session_id: str) -> str:
    return await _call(user_id, agent_id, "GET", f"checkout/sessions/{session_id}")


async def stripe_create_checkout(
    user_id: str, agent_id: str, line_items: list[dict],
    success_url: str, cancel_url: str = "", customer_email: str = "",
) -> str:
    if not line_items or not success_url:
        return json_result({"status": "error", "provider": "stripe", "message": "line_items and success_url are required"})

    data: dict[str, Any] = {"mode": "payment", "success_url": success_url}
    if cancel_url:
        data["cancel_url"] = cancel_url
    if customer_email:
        data["customer_email"] = customer_email

    for index, item in enumerate(line_items[:100]):
        quantity = max(1, int(item.get("quantity") or 1))
        if item.get("price_id"):
            data[f"line_items[{index}][price]"] = str(item["price_id"])
            data[f"line_items[{index}][quantity]"] = quantity
            continue
        name = str(item.get("name") or "").strip()
        amount = int(item.get("unit_amount") or 0)
        currency = str(item.get("currency") or "USD").lower()
        if not name or amount <= 0:
            return json_result({
                "status": "error", "provider": "stripe",
                "message": "Each line item needs price_id, or name and positive unit_amount.",
            })
        data[f"line_items[{index}][price_data][currency]"] = currency
        data[f"line_items[{index}][price_data][unit_amount]"] = amount
        data[f"line_items[{index}][price_data][product_data][name]"] = name
        data[f"line_items[{index}][quantity]"] = quantity
    return await _call(user_id, agent_id, "POST", "checkout/sessions", data=data, idempotent=True)


TOOLS = [
    {"name": "stripe_list_products", "provider": "stripe", "handler": stripe_list_products,
     "destructive": False, "parameters": {"type": "object", "properties": {
         "active": {"type": "boolean", "default": True}, "limit": {"type": "integer", "default": 25}}, "required": []}},
    {"name": "stripe_get_checkout_session", "provider": "stripe", "handler": stripe_get_checkout_session,
     "destructive": False, "parameters": {"type": "object", "properties": {
         "session_id": {"type": "string"}}, "required": ["session_id"]}},
    {"name": "stripe_create_checkout", "provider": "stripe", "handler": stripe_create_checkout,
     "destructive": True, "requires_confirmation": True, "parameters": {"type": "object", "properties": {
         "line_items": {"type": "array", "items": {"type": "object", "properties": {
             "price_id": {"type": "string"}, "name": {"type": "string"},
             "unit_amount": {"type": "integer"}, "currency": {"type": "string"},
             "quantity": {"type": "integer"}}}},
         "success_url": {"type": "string"}, "cancel_url": {"type": "string", "default": ""},
         "customer_email": {"type": "string", "default": ""}}, "required": ["line_items", "success_url"]}},
]
