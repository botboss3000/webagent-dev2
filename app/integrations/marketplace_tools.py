"""Marketplace integration tools — eBay, Etsy, Shopify, Amazon SP-API.

All HTTP goes through `oauth_helper.oauth_api_call` so token refresh / 401
retry stays consistent.

Notes per provider:
  - eBay uses OAuth 2.0 with refresh tokens against api.ebay.com (production).
    Sell APIs (inventory, listing) require seller account scopes; Browse APIs
    (search, items) are read-only.
  - Etsy uses OAuth 2.0 + PKCE; Open API v3 lives at openapi.etsy.com.
    Requesting/refreshing tokens requires the `x-api-key` header set to the
    keystring (== client_id) on every call.
  - Shopify is per-shop: each install is tied to one shop. The shop domain
    (`{shop}.myshopify.com`) is stored in auth_elements.config and read at
    request time to build the per-tenant API base URL.
  - Amazon SP-API uses LWA (Login With Amazon) bearer tokens directly. AWS
    Sigv4 signing was deprecated in 2023, so OAuth bearer is sufficient. The
    region-specific endpoint (NA/EU/FE) is stored in auth_elements.config
    and selected at request time.
"""

import json
from typing import Optional

from app.integrations.oauth_helper import (
    oauth_api_call,
    oauth_label,
    not_connected_payload,
)


async def _get_auth_config(user_id: str, agent_id: str, provider: str) -> dict:
    """Read auth_elements.config for a (user, agent, provider). Returns {} if missing."""
    from app.db import get_db
    db = get_db()
    elem = await db.auth_element_get(user_id, provider, oauth_label(agent_id))
    if not elem:
        return {}
    cfg = elem.get("config") or {}
    if isinstance(cfg, str):
        try:
            cfg = json.loads(cfg)
        except Exception:
            cfg = {}
    return cfg or {}


# ── eBay ──────────────────────────────────────────────────────────────────

_EBAY_API = "https://api.ebay.com"


async def ebay_search_items(user_id: str, agent_id: str, query: str, limit: int = 10) -> str:
    """Search the eBay marketplace by keyword (Browse API)."""
    if not query:
        return json.dumps({"status": "error", "message": "query required"})
    result = await oauth_api_call(
        user_id, agent_id, "ebay", "GET",
        f"{_EBAY_API}/buy/browse/v1/item_summary/search",
        params={"q": query, "limit": max(1, min(int(limit or 10), 50))},
    )
    if result.get("status") == "not_connected":
        return not_connected_payload("ebay")
    return json.dumps(result)


async def ebay_get_item(user_id: str, agent_id: str, item_id: str) -> str:
    """Fetch details for a single eBay listing by item id (Browse API)."""
    if not item_id:
        return json.dumps({"status": "error", "message": "item_id required"})
    result = await oauth_api_call(
        user_id, agent_id, "ebay", "GET",
        f"{_EBAY_API}/buy/browse/v1/item/{item_id}",
    )
    if result.get("status") == "not_connected":
        return not_connected_payload("ebay")
    return json.dumps(result)


async def ebay_list_my_active_listings(user_id: str, agent_id: str, limit: int = 25, offset: int = 0) -> str:
    """List the connected seller's active inventory items (Sell Inventory API)."""
    result = await oauth_api_call(
        user_id, agent_id, "ebay", "GET",
        f"{_EBAY_API}/sell/inventory/v1/inventory_item",
        params={
            "limit": max(1, min(int(limit or 25), 200)),
            "offset": max(0, int(offset or 0)),
        },
    )
    if result.get("status") == "not_connected":
        return not_connected_payload("ebay")
    return json.dumps(result)


async def ebay_create_inventory_item(
    user_id: str,
    agent_id: str,
    sku: str,
    title: str,
    description: str = "",
    quantity: int = 1,
    image_url: str = "",
) -> str:
    """Create or replace an eBay inventory item (PUT /sell/inventory/v1/inventory_item/{sku}).

    Use ebay_create_offer next to actually list it for sale.
    """
    if not sku or not title:
        return json.dumps({"status": "error", "message": "sku and title required"})
    body: dict = {
        "product": {"title": title, "description": description or title},
        "availability": {"shipToLocationAvailability": {"quantity": max(1, int(quantity or 1))}},
        "condition": "NEW",
    }
    if image_url:
        body["product"]["imageUrls"] = [image_url]
    result = await oauth_api_call(
        user_id, agent_id, "ebay", "PUT",
        f"{_EBAY_API}/sell/inventory/v1/inventory_item/{sku}",
        json_body=body,
        headers={"Content-Language": "en-US"},
    )
    if result.get("status") == "not_connected":
        return not_connected_payload("ebay")
    return json.dumps(result)


async def ebay_create_offer(
    user_id: str,
    agent_id: str,
    sku: str,
    price: float,
    currency: str = "USD",
    marketplace_id: str = "EBAY_US",
    category_id: str = "",
    merchant_location_key: str = "",
) -> str:
    """Create an eBay offer (price + marketplace) for an existing inventory SKU.

    Required: sku, price. Optional: category_id (eBay leaf category),
    merchant_location_key (seller fulfillment location).
    """
    if not sku or not price or float(price) <= 0:
        return json.dumps({"status": "error", "message": "sku and positive price required"})
    body: dict = {
        "sku": sku,
        "marketplaceId": marketplace_id or "EBAY_US",
        "format": "FIXED_PRICE",
        "availableQuantity": 1,
        "pricingSummary": {"price": {"value": f"{float(price):.2f}", "currency": currency or "USD"}},
    }
    if category_id:
        body["categoryId"] = category_id
    if merchant_location_key:
        body["merchantLocationKey"] = merchant_location_key
    result = await oauth_api_call(
        user_id, agent_id, "ebay", "POST",
        f"{_EBAY_API}/sell/inventory/v1/offer",
        json_body=body,
        headers={"Content-Language": "en-US"},
    )
    if result.get("status") == "not_connected":
        return not_connected_payload("ebay")
    return json.dumps(result)


async def ebay_end_offer(user_id: str, agent_id: str, offer_id: str, reason: str = "NOT_AVAILABLE") -> str:
    """Withdraw a published eBay offer (ends the listing)."""
    if not offer_id:
        return json.dumps({"status": "error", "message": "offer_id required"})
    result = await oauth_api_call(
        user_id, agent_id, "ebay", "POST",
        f"{_EBAY_API}/sell/inventory/v1/offer/{offer_id}/withdraw",
        json_body={"reasonForWithdrawal": reason or "NOT_AVAILABLE"},
    )
    if result.get("status") == "not_connected":
        return not_connected_payload("ebay")
    return json.dumps(result)


# ── Etsy ──────────────────────────────────────────────────────────────────

_ETSY_API = "https://openapi.etsy.com/v3"


async def _etsy_headers() -> dict:
    """Etsy requires the x-api-key (== client_id) header on every call."""
    try:
        from app.admin.integrations import get_etsy_creds
        client_id, _ = await get_etsy_creds()
        return {"x-api-key": client_id} if client_id else {}
    except Exception:
        return {}


async def etsy_me(user_id: str, agent_id: str) -> str:
    """Fetch the connected Etsy user (resolves the user_id internally)."""
    headers = await _etsy_headers()
    result = await oauth_api_call(
        user_id, agent_id, "etsy", "GET",
        f"{_ETSY_API}/application/users/me",
        headers=headers,
    )
    if result.get("status") == "not_connected":
        return not_connected_payload("etsy")
    return json.dumps(result)


async def etsy_search_listings(user_id: str, agent_id: str, keywords: str, limit: int = 25) -> str:
    """Search active Etsy listings by keyword."""
    if not keywords:
        return json.dumps({"status": "error", "message": "keywords required"})
    headers = await _etsy_headers()
    result = await oauth_api_call(
        user_id, agent_id, "etsy", "GET",
        f"{_ETSY_API}/application/listings/active",
        params={"keywords": keywords, "limit": max(1, min(int(limit or 25), 100))},
        headers=headers,
    )
    if result.get("status") == "not_connected":
        return not_connected_payload("etsy")
    return json.dumps(result)


async def etsy_list_my_shop_listings(user_id: str, agent_id: str, state: str = "active", limit: int = 25) -> str:
    """List listings for the connected Etsy seller's shop.

    state: active | inactive | draft | expired | sold_out
    """
    cfg = await _get_auth_config(user_id, agent_id, "etsy")
    shop_id = cfg.get("shop_id")
    if not shop_id:
        return json.dumps({"status": "error", "message": "no Etsy shop on this account"})
    headers = await _etsy_headers()
    valid_states = {"active", "inactive", "draft", "expired", "sold_out"}
    state = state if state in valid_states else "active"
    result = await oauth_api_call(
        user_id, agent_id, "etsy", "GET",
        f"{_ETSY_API}/application/shops/{shop_id}/listings",
        params={"state": state, "limit": max(1, min(int(limit or 25), 100))},
        headers=headers,
    )
    if result.get("status") == "not_connected":
        return not_connected_payload("etsy")
    return json.dumps(result)


async def etsy_create_draft_listing(
    user_id: str,
    agent_id: str,
    title: str,
    description: str,
    price: float,
    quantity: int = 1,
    who_made: str = "i_did",
    when_made: str = "made_to_order",
    taxonomy_id: int = 0,
) -> str:
    """Create a draft listing in the connected seller's Etsy shop.

    Draft listings are not published until activated in Etsy's UI.
    """
    if not title or not description or not price or float(price) <= 0:
        return json.dumps({"status": "error", "message": "title, description, positive price required"})
    cfg = await _get_auth_config(user_id, agent_id, "etsy")
    shop_id = cfg.get("shop_id")
    if not shop_id:
        return json.dumps({"status": "error", "message": "no Etsy shop on this account"})
    headers = {**(await _etsy_headers()), "Content-Type": "application/x-www-form-urlencoded"}
    data = {
        "quantity": str(max(1, int(quantity or 1))),
        "title": title,
        "description": description,
        "price": f"{float(price):.2f}",
        "who_made": who_made or "i_did",
        "when_made": when_made or "made_to_order",
        "taxonomy_id": str(int(taxonomy_id or 0)),
        "state": "draft",
    }
    result = await oauth_api_call(
        user_id, agent_id, "etsy", "POST",
        f"{_ETSY_API}/application/shops/{shop_id}/listings",
        data=data,
        headers=headers,
    )
    if result.get("status") == "not_connected":
        return not_connected_payload("etsy")
    return json.dumps(result)


# ── Shopify (per-shop) ────────────────────────────────────────────────────

_SHOPIFY_API_VERSION = "2024-10"


async def _shopify_base(user_id: str, agent_id: str) -> Optional[str]:
    """Return the per-shop admin API base URL, or None if no shop on file."""
    cfg = await _get_auth_config(user_id, agent_id, "shopify")
    shop = (cfg.get("shop") or "").strip().lower()
    if not shop:
        return None
    if not shop.endswith(".myshopify.com"):
        shop = f"{shop}.myshopify.com"
    return f"https://{shop}/admin/api/{_SHOPIFY_API_VERSION}"


async def shopify_list_products(user_id: str, agent_id: str, limit: int = 50) -> str:
    """List products in the connected Shopify store."""
    base = await _shopify_base(user_id, agent_id)
    if not base:
        return json.dumps({"status": "error", "message": "no Shopify shop on this account"})
    result = await oauth_api_call(
        user_id, agent_id, "shopify", "GET",
        f"{base}/products.json",
        params={"limit": max(1, min(int(limit or 50), 250))},
    )
    if result.get("status") == "not_connected":
        return not_connected_payload("shopify")
    return json.dumps(result)


async def shopify_get_product(user_id: str, agent_id: str, product_id: str) -> str:
    """Fetch a single Shopify product by id."""
    if not product_id:
        return json.dumps({"status": "error", "message": "product_id required"})
    base = await _shopify_base(user_id, agent_id)
    if not base:
        return json.dumps({"status": "error", "message": "no Shopify shop on this account"})
    result = await oauth_api_call(
        user_id, agent_id, "shopify", "GET",
        f"{base}/products/{product_id}.json",
    )
    if result.get("status") == "not_connected":
        return not_connected_payload("shopify")
    return json.dumps(result)


async def shopify_create_product(
    user_id: str,
    agent_id: str,
    title: str,
    body_html: str = "",
    vendor: str = "",
    product_type: str = "",
    price: float = 0.0,
    sku: str = "",
    inventory_quantity: int = 0,
) -> str:
    """Create a new product in the connected Shopify store.

    Creates a single default variant with the given price / SKU / inventory.
    """
    if not title:
        return json.dumps({"status": "error", "message": "title required"})
    base = await _shopify_base(user_id, agent_id)
    if not base:
        return json.dumps({"status": "error", "message": "no Shopify shop on this account"})
    variant: dict = {}
    if price and float(price) > 0:
        variant["price"] = f"{float(price):.2f}"
    if sku:
        variant["sku"] = sku
    if inventory_quantity and int(inventory_quantity) > 0:
        variant["inventory_quantity"] = int(inventory_quantity)
        variant["inventory_management"] = "shopify"
    product: dict = {"title": title}
    if body_html:
        product["body_html"] = body_html
    if vendor:
        product["vendor"] = vendor
    if product_type:
        product["product_type"] = product_type
    if variant:
        product["variants"] = [variant]
    result = await oauth_api_call(
        user_id, agent_id, "shopify", "POST",
        f"{base}/products.json",
        json_body={"product": product},
    )
    if result.get("status") == "not_connected":
        return not_connected_payload("shopify")
    return json.dumps(result)


async def shopify_update_variant_inventory(
    user_id: str,
    agent_id: str,
    inventory_item_id: str,
    location_id: str,
    available: int,
) -> str:
    """Set the available inventory for a variant at a specific location."""
    if not inventory_item_id or not location_id:
        return json.dumps({"status": "error", "message": "inventory_item_id and location_id required"})
    base = await _shopify_base(user_id, agent_id)
    if not base:
        return json.dumps({"status": "error", "message": "no Shopify shop on this account"})
    result = await oauth_api_call(
        user_id, agent_id, "shopify", "POST",
        f"{base}/inventory_levels/set.json",
        json_body={
            "location_id": int(location_id),
            "inventory_item_id": int(inventory_item_id),
            "available": int(available),
        },
    )
    if result.get("status") == "not_connected":
        return not_connected_payload("shopify")
    return json.dumps(result)


# ── Amazon SP-API ─────────────────────────────────────────────────────────

# Region-scoped endpoints (LWA OAuth bearer is sufficient since 2023)
_AMAZON_REGION_HOSTS = {
    "NA": "https://sellingpartnerapi-na.amazon.com",
    "EU": "https://sellingpartnerapi-eu.amazon.com",
    "FE": "https://sellingpartnerapi-fe.amazon.com",
}


async def _amazon_base(user_id: str, agent_id: str) -> Optional[str]:
    """Return the region-specific SP-API base URL, or None if not configured."""
    cfg = await _get_auth_config(user_id, agent_id, "amazon")
    region = (cfg.get("region") or "NA").upper()
    return _AMAZON_REGION_HOSTS.get(region)


async def amazon_list_orders(
    user_id: str,
    agent_id: str,
    created_after: str,
    marketplace_ids: str = "",
    max_results: int = 50,
) -> str:
    """List Amazon orders created after the given ISO-8601 timestamp.

    marketplace_ids: comma-separated Amazon marketplace IDs (e.g.
    ATVPDKIKX0DER for US). If omitted, uses the marketplace stored on the
    connected account.
    """
    if not created_after:
        return json.dumps({"status": "error", "message": "created_after (ISO-8601) required"})
    base = await _amazon_base(user_id, agent_id)
    if not base:
        return json.dumps({"status": "error", "message": "Amazon region not configured on this account"})
    cfg = await _get_auth_config(user_id, agent_id, "amazon")
    mids = marketplace_ids or cfg.get("marketplace_id", "")
    if not mids:
        return json.dumps({"status": "error", "message": "marketplace_ids required (no default on file)"})
    result = await oauth_api_call(
        user_id, agent_id, "amazon", "GET",
        f"{base}/orders/v0/orders",
        params={
            "CreatedAfter": created_after,
            "MarketplaceIds": mids,
            "MaxResultsPerPage": max(1, min(int(max_results or 50), 100)),
        },
    )
    if result.get("status") == "not_connected":
        return not_connected_payload("amazon")
    return json.dumps(result)


async def amazon_get_order(user_id: str, agent_id: str, order_id: str) -> str:
    """Fetch an Amazon order by AmazonOrderId."""
    if not order_id:
        return json.dumps({"status": "error", "message": "order_id required"})
    base = await _amazon_base(user_id, agent_id)
    if not base:
        return json.dumps({"status": "error", "message": "Amazon region not configured on this account"})
    result = await oauth_api_call(
        user_id, agent_id, "amazon", "GET",
        f"{base}/orders/v0/orders/{order_id}",
    )
    if result.get("status") == "not_connected":
        return not_connected_payload("amazon")
    return json.dumps(result)


async def amazon_list_inventory(user_id: str, agent_id: str, marketplace_ids: str = "", max_results: int = 50) -> str:
    """List FBA inventory summaries for the connected seller."""
    base = await _amazon_base(user_id, agent_id)
    if not base:
        return json.dumps({"status": "error", "message": "Amazon region not configured on this account"})
    cfg = await _get_auth_config(user_id, agent_id, "amazon")
    mids = marketplace_ids or cfg.get("marketplace_id", "")
    if not mids:
        return json.dumps({"status": "error", "message": "marketplace_ids required (no default on file)"})
    result = await oauth_api_call(
        user_id, agent_id, "amazon", "GET",
        f"{base}/fba/inventory/v1/summaries",
        params={
            "granularityType": "Marketplace",
            "granularityId": mids.split(",")[0].strip(),
            "marketplaceIds": mids,
            "details": "true",
            "maxResultsPerPage": max(1, min(int(max_results or 50), 50)),
        },
    )
    if result.get("status") == "not_connected":
        return not_connected_payload("amazon")
    return json.dumps(result)


async def amazon_update_listing_price(
    user_id: str,
    agent_id: str,
    seller_id: str,
    sku: str,
    price: float,
    marketplace_id: str = "",
) -> str:
    """Patch the price on an existing Amazon listing (Listings Items API)."""
    if not seller_id or not sku or not price or float(price) <= 0:
        return json.dumps({"status": "error", "message": "seller_id, sku, positive price required"})
    base = await _amazon_base(user_id, agent_id)
    if not base:
        return json.dumps({"status": "error", "message": "Amazon region not configured on this account"})
    cfg = await _get_auth_config(user_id, agent_id, "amazon")
    mid = marketplace_id or cfg.get("marketplace_id", "")
    if not mid:
        return json.dumps({"status": "error", "message": "marketplace_id required (no default on file)"})
    body = {
        "productType": "PRODUCT",
        "patches": [{
            "op": "replace",
            "path": "/attributes/purchasable_offer",
            "value": [{
                "marketplace_id": mid,
                "currency": "USD",
                "our_price": [{"schedule": [{"value_with_tax": f"{float(price):.2f}"}]}],
            }],
        }],
    }
    result = await oauth_api_call(
        user_id, agent_id, "amazon", "PATCH",
        f"{base}/listings/2021-08-01/items/{seller_id}/{sku}",
        params={"marketplaceIds": mid},
        json_body=body,
    )
    if result.get("status") == "not_connected":
        return not_connected_payload("amazon")
    return json.dumps(result)


# ── Tool registry ─────────────────────────────────────────────────────────

FEATURE = {
    "id": "marketplace",
    "display_name": "Marketplace (eBay, etc.)",
    "category": "integration",
    "status": "experimental",
    "summary": "Marketplace search/listing endpoints; early coverage.",
    "requires": ["Per-marketplace OAuth credentials"],
}

TOOLS = [
    # ── eBay ──
    {"name": "ebay_search_items", "provider": "ebay", "handler": ebay_search_items,
     "stages": ["execute_tools"], "destructive": False,
     "parameters": {"type": "object", "properties": {
         "query": {"type": "string", "description": "Keyword search."},
         "limit": {"type": "integer", "description": "Max results (1-50).", "default": 10},
     }, "required": ["query"]}},
    {"name": "ebay_get_item", "provider": "ebay", "handler": ebay_get_item,
     "stages": ["execute_tools"], "destructive": False,
     "parameters": {"type": "object", "properties": {
         "item_id": {"type": "string", "description": "eBay item id (e.g., v1|1234|0)."},
     }, "required": ["item_id"]}},
    {"name": "ebay_list_my_active_listings", "provider": "ebay", "handler": ebay_list_my_active_listings,
     "stages": ["execute_tools"], "destructive": False,
     "parameters": {"type": "object", "properties": {
         "limit":  {"type": "integer", "description": "Max items (1-200).", "default": 25},
         "offset": {"type": "integer", "description": "Pagination offset.",  "default": 0},
     }, "required": []}},
    {"name": "ebay_create_inventory_item", "provider": "ebay", "handler": ebay_create_inventory_item,
     "stages": ["execute_tools"], "destructive": True, "requires_confirmation": True,
     "parameters": {"type": "object", "properties": {
         "sku":         {"type": "string",  "description": "Seller-defined SKU (unique per seller)."},
         "title":       {"type": "string",  "description": "Product title (≤80 chars)."},
         "description": {"type": "string",  "description": "Product description.", "default": ""},
         "quantity":    {"type": "integer", "description": "Available quantity.", "default": 1},
         "image_url":   {"type": "string",  "description": "Optional public image URL.", "default": ""},
     }, "required": ["sku", "title"]}},
    {"name": "ebay_create_offer", "provider": "ebay", "handler": ebay_create_offer,
     "stages": ["execute_tools"], "destructive": True, "requires_confirmation": True,
     "parameters": {"type": "object", "properties": {
         "sku":                    {"type": "string", "description": "Inventory SKU to list."},
         "price":                  {"type": "number", "description": "Sale price."},
         "currency":               {"type": "string", "description": "ISO currency code.", "default": "USD"},
         "marketplace_id":         {"type": "string", "description": "eBay marketplace (e.g. EBAY_US, EBAY_GB).", "default": "EBAY_US"},
         "category_id":            {"type": "string", "description": "Optional leaf category id.", "default": ""},
         "merchant_location_key":  {"type": "string", "description": "Seller fulfillment location key.", "default": ""},
     }, "required": ["sku", "price"]}},
    {"name": "ebay_end_offer", "provider": "ebay", "handler": ebay_end_offer,
     "stages": ["execute_tools"], "destructive": True, "requires_confirmation": True,
     "parameters": {"type": "object", "properties": {
         "offer_id": {"type": "string", "description": "eBay offer id to withdraw."},
         "reason":   {"type": "string", "description": "Withdrawal reason.", "default": "NOT_AVAILABLE"},
     }, "required": ["offer_id"]}},

    # ── Etsy ──
    {"name": "etsy_me", "provider": "etsy", "handler": etsy_me,
     "stages": ["execute_tools"], "destructive": False,
     "parameters": {"type": "object", "properties": {}, "required": []}},
    {"name": "etsy_search_listings", "provider": "etsy", "handler": etsy_search_listings,
     "stages": ["execute_tools"], "destructive": False,
     "parameters": {"type": "object", "properties": {
         "keywords": {"type": "string",  "description": "Search keywords."},
         "limit":    {"type": "integer", "description": "Max results (1-100).", "default": 25},
     }, "required": ["keywords"]}},
    {"name": "etsy_list_my_shop_listings", "provider": "etsy", "handler": etsy_list_my_shop_listings,
     "stages": ["execute_tools"], "destructive": False,
     "parameters": {"type": "object", "properties": {
         "state": {"type": "string", "enum": ["active", "inactive", "draft", "expired", "sold_out"], "default": "active"},
         "limit": {"type": "integer", "description": "Max listings (1-100).", "default": 25},
     }, "required": []}},
    {"name": "etsy_create_draft_listing", "provider": "etsy", "handler": etsy_create_draft_listing,
     "stages": ["execute_tools"], "destructive": True, "requires_confirmation": True,
     "parameters": {"type": "object", "properties": {
         "title":        {"type": "string",  "description": "Listing title (≤140 chars)."},
         "description":  {"type": "string",  "description": "Listing description."},
         "price":        {"type": "number",  "description": "Listing price."},
         "quantity":     {"type": "integer", "description": "Available quantity.", "default": 1},
         "who_made":     {"type": "string",  "enum": ["i_did", "someone_else", "collective"], "default": "i_did"},
         "when_made":    {"type": "string",  "description": "Etsy when_made enum (e.g. made_to_order, 2020_2024).", "default": "made_to_order"},
         "taxonomy_id":  {"type": "integer", "description": "Etsy taxonomy (category) id.", "default": 0},
     }, "required": ["title", "description", "price"]}},

    # ── Shopify ──
    {"name": "shopify_list_products", "provider": "shopify", "handler": shopify_list_products,
     "stages": ["execute_tools"], "destructive": False,
     "parameters": {"type": "object", "properties": {
         "limit": {"type": "integer", "description": "Max products (1-250).", "default": 50},
     }, "required": []}},
    {"name": "shopify_get_product", "provider": "shopify", "handler": shopify_get_product,
     "stages": ["execute_tools"], "destructive": False,
     "parameters": {"type": "object", "properties": {
         "product_id": {"type": "string", "description": "Shopify product id."},
     }, "required": ["product_id"]}},
    {"name": "shopify_create_product", "provider": "shopify", "handler": shopify_create_product,
     "stages": ["execute_tools"], "destructive": True, "requires_confirmation": True,
     "parameters": {"type": "object", "properties": {
         "title":              {"type": "string",  "description": "Product title."},
         "body_html":          {"type": "string",  "description": "HTML description.", "default": ""},
         "vendor":             {"type": "string",  "description": "Vendor / brand name.", "default": ""},
         "product_type":       {"type": "string",  "description": "Product category.", "default": ""},
         "price":              {"type": "number",  "description": "Default variant price.", "default": 0},
         "sku":                {"type": "string",  "description": "Default variant SKU.", "default": ""},
         "inventory_quantity": {"type": "integer", "description": "Initial inventory.", "default": 0},
     }, "required": ["title"]}},
    {"name": "shopify_update_variant_inventory", "provider": "shopify", "handler": shopify_update_variant_inventory,
     "stages": ["execute_tools"], "destructive": True, "requires_confirmation": True,
     "parameters": {"type": "object", "properties": {
         "inventory_item_id": {"type": "string",  "description": "Variant's inventory_item_id."},
         "location_id":       {"type": "string",  "description": "Shopify location id."},
         "available":         {"type": "integer", "description": "New available quantity."},
     }, "required": ["inventory_item_id", "location_id", "available"]}},

    # ── Amazon SP-API ──
    {"name": "amazon_list_orders", "provider": "amazon", "handler": amazon_list_orders,
     "stages": ["execute_tools"], "destructive": False,
     "parameters": {"type": "object", "properties": {
         "created_after":    {"type": "string",  "description": "ISO-8601 timestamp; only orders after this time."},
         "marketplace_ids":  {"type": "string",  "description": "Comma-separated Amazon marketplace IDs. Defaults to the marketplace on file.", "default": ""},
         "max_results":      {"type": "integer", "description": "Max orders (1-100).", "default": 50},
     }, "required": ["created_after"]}},
    {"name": "amazon_get_order", "provider": "amazon", "handler": amazon_get_order,
     "stages": ["execute_tools"], "destructive": False,
     "parameters": {"type": "object", "properties": {
         "order_id": {"type": "string", "description": "AmazonOrderId."},
     }, "required": ["order_id"]}},
    {"name": "amazon_list_inventory", "provider": "amazon", "handler": amazon_list_inventory,
     "stages": ["execute_tools"], "destructive": False,
     "parameters": {"type": "object", "properties": {
         "marketplace_ids": {"type": "string",  "description": "Comma-separated marketplace IDs. Defaults to the marketplace on file.", "default": ""},
         "max_results":     {"type": "integer", "description": "Max items (1-50).", "default": 50},
     }, "required": []}},
    {"name": "amazon_update_listing_price", "provider": "amazon", "handler": amazon_update_listing_price,
     "stages": ["execute_tools"], "destructive": True, "requires_confirmation": True,
     "parameters": {"type": "object", "properties": {
         "seller_id":      {"type": "string", "description": "Seller's Amazon merchant id."},
         "sku":            {"type": "string", "description": "Listing SKU to update."},
         "price":          {"type": "number", "description": "New price."},
         "marketplace_id": {"type": "string", "description": "Amazon marketplace id. Defaults to the one on file.", "default": ""},
     }, "required": ["seller_id", "sku", "price"]}},
]
