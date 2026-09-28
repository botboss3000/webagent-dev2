"""PayPal Orders v2 tools."""

from __future__ import annotations

import uuid
from typing import Any

from app.integrations.commerce_common import (
    is_positive_amount, json_result, load_agent_credentials, not_configured, request_json,
)


async def _config(user_id: str, agent_id: str) -> tuple[dict, str]:
    creds = await load_agent_credentials("paypal", user_id, agent_id)
    required = ("client_id", "client_secret")
    if not all(str(creds.get(key) or "").strip() for key in required):
        return {}, not_configured("paypal", required)
    live = str(creds.get("environment") or "sandbox").lower() == "live"
    return {**creds, "base": "https://api-m.paypal.com" if live else "https://api-m.sandbox.paypal.com"}, ""


async def _access_token(cfg: dict) -> tuple[str, dict | None]:
    result = await request_json(
        "paypal", "POST", f"{cfg['base']}/v1/oauth2/token",
        headers={"Accept": "application/json", "Accept-Language": "en_US"},
        data={"grant_type": "client_credentials"},
        auth=(cfg["client_id"], cfg["client_secret"]),
    )
    token = str((result.get("data") or {}).get("access_token") or "")
    return token, None if token else result


async def _call(user_id: str, agent_id: str, method: str, path: str, *, body=None, request_id="") -> str:
    cfg, error = await _config(user_id, agent_id)
    if error:
        return error
    token, token_error = await _access_token(cfg)
    if token_error:
        return json_result(token_error)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    if request_id:
        headers["PayPal-Request-Id"] = request_id
    result = await request_json("paypal", method, f"{cfg['base']}/{path.lstrip('/')}", headers=headers, json_body=body)
    return json_result(result)


async def paypal_get_order(user_id: str, agent_id: str, order_id: str) -> str:
    return await _call(user_id, agent_id, "GET", f"v2/checkout/orders/{order_id}")


async def paypal_create_order(
    user_id: str, agent_id: str, amount: str, currency: str = "USD",
    description: str = "", return_url: str = "", cancel_url: str = "",
) -> str:
    value = str(amount or "").strip()
    if not is_positive_amount(value):
        return json_result({"status": "error", "provider": "paypal", "message": "A positive amount is required."})
    unit: dict[str, Any] = {"amount": {"currency_code": currency.upper(), "value": value}}
    if description:
        unit["description"] = description
    body: dict[str, Any] = {"intent": "CAPTURE", "purchase_units": [unit]}
    if return_url or cancel_url:
        body["payment_source"] = {"paypal": {"experience_context": {
            "user_action": "PAY_NOW", **({"return_url": return_url} if return_url else {}),
            **({"cancel_url": cancel_url} if cancel_url else {}),
        }}}
    return await _call(user_id, agent_id, "POST", "v2/checkout/orders", body=body, request_id=str(uuid.uuid4()))


async def paypal_capture_order(user_id: str, agent_id: str, order_id: str) -> str:
    return await _call(
        user_id, agent_id, "POST", f"v2/checkout/orders/{order_id}/capture",
        body={}, request_id=str(uuid.uuid4()),
    )


TOOLS = [
    {"name": "paypal_get_order", "provider": "paypal", "handler": paypal_get_order,
     "destructive": False, "parameters": {"type": "object", "properties": {
         "order_id": {"type": "string"}}, "required": ["order_id"]}},
    {"name": "paypal_create_order", "provider": "paypal", "handler": paypal_create_order,
     "destructive": True, "requires_confirmation": True, "parameters": {"type": "object", "properties": {
         "amount": {"type": "string"}, "currency": {"type": "string", "default": "USD"},
         "description": {"type": "string", "default": ""}, "return_url": {"type": "string", "default": ""},
         "cancel_url": {"type": "string", "default": ""}}, "required": ["amount"]}},
    {"name": "paypal_capture_order", "provider": "paypal", "handler": paypal_capture_order,
     "destructive": True, "requires_confirmation": True, "parameters": {"type": "object", "properties": {
         "order_id": {"type": "string"}}, "required": ["order_id"]}},
]
