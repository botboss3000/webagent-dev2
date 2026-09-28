"""BTCPay Server Greenfield invoice tools."""

from __future__ import annotations

from typing import Any

from app.integrations.commerce_common import (
    clamp_limit, is_positive_amount, json_result, load_agent_credentials, not_configured,
    request_json, require_https_base,
)


async def _config(user_id: str, agent_id: str) -> tuple[dict, str]:
    creds = await load_agent_credentials("btcpay", user_id, agent_id)
    required = ("server_url", "store_id", "api_key")
    if not all(str(creds.get(key) or "").strip() for key in required):
        return {}, not_configured("btcpay", required)
    base, error = require_https_base(creds["server_url"], "BTCPay Server")
    if error:
        return {}, json_result({"status": "error", "provider": "btcpay", "message": error})
    return {
        **creds, "base": base,
        "headers": {"Authorization": f"token {creds['api_key']}", "Content-Type": "application/json"},
    }, ""


async def _call(user_id: str, agent_id: str, method: str, path: str, *, params=None, body=None) -> str:
    cfg, error = await _config(user_id, agent_id)
    if error:
        return error
    result = await request_json(
        "btcpay", method, f"{cfg['base']}/api/v1/stores/{cfg['store_id']}/{path.lstrip('/')}",
        headers=cfg["headers"], params=params, json_body=body,
    )
    return json_result(result)


async def btcpay_list_invoices(user_id: str, agent_id: str, skip: int = 0, take: int = 25, status: str = "") -> str:
    params: dict[str, Any] = {"skip": max(0, int(skip)), "take": clamp_limit(take, 100)}
    if status:
        params["status"] = status
    return await _call(user_id, agent_id, "GET", "invoices", params=params)


async def btcpay_get_invoice(user_id: str, agent_id: str, invoice_id: str) -> str:
    return await _call(user_id, agent_id, "GET", f"invoices/{invoice_id}")


async def btcpay_create_invoice(
    user_id: str, agent_id: str, amount: str, currency: str = "USD",
    order_id: str = "", item_description: str = "", checkout_redirect_url: str = "",
) -> str:
    value = str(amount or "").strip()
    if not is_positive_amount(value):
        return json_result({"status": "error", "provider": "btcpay", "message": "A positive amount is required."})
    body: dict[str, Any] = {"amount": value, "currency": currency.upper()}
    metadata = {key: val for key, val in {
        "orderId": order_id, "itemDesc": item_description,
    }.items() if val}
    if metadata:
        body["metadata"] = metadata
    if checkout_redirect_url:
        body["checkout"] = {"redirectURL": checkout_redirect_url}
    return await _call(user_id, agent_id, "POST", "invoices", body=body)


TOOLS = [
    {"name": "btcpay_list_invoices", "provider": "btcpay", "handler": btcpay_list_invoices,
     "destructive": False, "parameters": {"type": "object", "properties": {
         "skip": {"type": "integer", "default": 0}, "take": {"type": "integer", "default": 25},
         "status": {"type": "string", "default": ""}}, "required": []}},
    {"name": "btcpay_get_invoice", "provider": "btcpay", "handler": btcpay_get_invoice,
     "destructive": False, "parameters": {"type": "object", "properties": {
         "invoice_id": {"type": "string"}}, "required": ["invoice_id"]}},
    {"name": "btcpay_create_invoice", "provider": "btcpay", "handler": btcpay_create_invoice,
     "destructive": True, "requires_confirmation": True, "parameters": {"type": "object", "properties": {
         "amount": {"type": "string"}, "currency": {"type": "string", "default": "USD"},
         "order_id": {"type": "string", "default": ""}, "item_description": {"type": "string", "default": ""},
         "checkout_redirect_url": {"type": "string", "default": ""}}, "required": ["amount"]}},
]
