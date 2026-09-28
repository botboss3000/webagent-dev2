import json
import unittest
from unittest.mock import AsyncMock, patch

from app.integrations import (
    commerce_bigcommerce as bigcommerce,
    commerce_btcpay as btcpay,
    commerce_paypal as paypal,
    commerce_square as square,
    commerce_stripe as stripe,
    commerce_woocommerce as woocommerce,
)
from app.integrations import commerce_common
from app.integrations import inject_integration_tools


class CommerceProviderToolsTest(unittest.IsolatedAsyncioTestCase):
    async def test_public_widget_can_use_agent_admin_credentials(self):
        db = AsyncMock()
        db.get_agent_by_id.return_value = {"admin_users": '["store-admin"]'}

        async def read_credentials(provider, *, user_id="", agent_id=""):
            return {"secret_key": "stored"} if user_id == "store-admin" else {}

        with patch("app.db.get_db", return_value=db), \
             patch("app.abilities.credentials.read_credentials", side_effect=read_credentials):
            credentials = await commerce_common.load_agent_credentials("stripe", "anonymous", "agent-1")
        self.assertEqual(credentials["secret_key"], "stored")

    async def test_woocommerce_uses_store_api_and_basic_auth(self):
        with patch.object(woocommerce, "load_agent_credentials", AsyncMock(return_value={
            "base_url": "https://shop.example", "consumer_key": "ck", "consumer_secret": "cs",
        })), patch.object(woocommerce, "request_json", AsyncMock(return_value={"status": "ok"})) as request:
            result = json.loads(await woocommerce.woocommerce_list_products("u", "a", sku="ABC"))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(request.await_args.args[2], "https://shop.example/wp-json/wc/v3/products")
        self.assertEqual(request.await_args.kwargs["auth"], ("ck", "cs"))
        self.assertEqual(request.await_args.kwargs["params"]["sku"], "ABC")

    async def test_bigcommerce_creates_cart_with_redirect_urls(self):
        with patch.object(bigcommerce, "load_agent_credentials", AsyncMock(return_value={
            "store_hash": "abc123", "access_token": "token",
        })), patch.object(bigcommerce, "request_json", AsyncMock(return_value={"status": "ok"})) as request:
            await bigcommerce.bigcommerce_create_checkout("u", "a", [{"quantity": 1, "product_id": 7}])
        self.assertEqual(request.await_args.args[2], "https://api.bigcommerce.com/stores/abc123/v3/carts")
        self.assertEqual(request.await_args.kwargs["headers"]["X-Auth-Token"], "token")
        self.assertEqual(request.await_args.kwargs["params"], {"include": "redirect_urls"})

    async def test_square_creates_hosted_payment_link(self):
        with patch.object(square, "load_agent_credentials", AsyncMock(return_value={
            "environment": "sandbox", "access_token": "token", "location_id": "LOC",
        })), patch.object(square, "request_json", AsyncMock(return_value={"status": "ok"})) as request:
            await square.square_create_payment_link("u", "a", "Widget", 1200)
        self.assertEqual(request.await_args.args[2], "https://connect.squareupsandbox.com/v2/online-checkout/payment-links")
        self.assertEqual(request.await_args.kwargs["json_body"]["quick_pay"]["price_money"]["amount"], 1200)

    async def test_square_queries_inventory_counts(self):
        with patch.object(square, "load_agent_credentials", AsyncMock(return_value={
            "environment": "live", "access_token": "token", "location_id": "LOC",
        })), patch.object(square, "request_json", AsyncMock(return_value={"status": "ok"})) as request:
            await square.square_get_inventory("u", "a", ["variation-1", "variation-2"])
        self.assertEqual(request.await_args.args[2], "https://connect.squareup.com/v2/inventory/counts")
        self.assertEqual(request.await_args.kwargs["params"]["catalog_object_ids"], "variation-1,variation-2")

    async def test_stripe_checkout_uses_form_encoding(self):
        with patch.object(stripe, "load_agent_credentials", AsyncMock(return_value={"secret_key": "sk_test"})), \
             patch.object(stripe, "stripe_headers", return_value={"Idempotency-Key": "idem"}), \
             patch.object(stripe, "request_json", AsyncMock(return_value={"status": "ok"})) as request:
            await stripe.stripe_create_checkout(
                "u", "a", [{"price_id": "price_1", "quantity": 2}], "https://example/success",
            )
        self.assertEqual(request.await_args.args[2], "https://api.stripe.com/v1/checkout/sessions")
        self.assertEqual(request.await_args.kwargs["headers"]["Idempotency-Key"], "idem")
        self.assertEqual(request.await_args.kwargs["data"]["line_items[0][price]"], "price_1")

    async def test_paypal_authenticates_then_creates_order(self):
        responses = [
            {"status": "ok", "data": {"access_token": "bearer"}},
            {"status": "ok", "data": {"id": "ORDER"}},
        ]
        with patch.object(paypal, "load_agent_credentials", AsyncMock(return_value={
            "environment": "sandbox", "client_id": "client", "client_secret": "secret",
        })), patch.object(paypal, "request_json", AsyncMock(side_effect=responses)) as request:
            result = json.loads(await paypal.paypal_create_order("u", "a", "12.50"))
        self.assertEqual(result["data"]["id"], "ORDER")
        self.assertEqual(request.await_args_list[0].args[2], "https://api-m.sandbox.paypal.com/v1/oauth2/token")
        self.assertEqual(request.await_args_list[1].args[2], "https://api-m.sandbox.paypal.com/v2/checkout/orders")
        self.assertEqual(request.await_args_list[1].kwargs["headers"]["Authorization"], "Bearer bearer")

    async def test_btcpay_creates_greenfield_invoice(self):
        with patch.object(btcpay, "load_agent_credentials", AsyncMock(return_value={
            "server_url": "https://btcpay.example", "store_id": "STORE", "api_key": "key",
        })), patch.object(btcpay, "request_json", AsyncMock(return_value={"status": "ok"})) as request:
            await btcpay.btcpay_create_invoice("u", "a", "0.01", "BTC", order_id="42")
        self.assertEqual(request.await_args.args[2], "https://btcpay.example/api/v1/stores/STORE/invoices")
        self.assertEqual(request.await_args.kwargs["headers"]["Authorization"], "token key")
        self.assertEqual(request.await_args.kwargs["json_body"]["metadata"]["orderId"], "42")

    def test_all_money_moving_tools_require_confirmation(self):
        modules = (woocommerce, bigcommerce, square, stripe, paypal, btcpay)
        destructive = [tool for module in modules for tool in module.TOOLS if tool.get("destructive")]
        self.assertTrue(destructive)
        self.assertTrue(all(tool.get("requires_confirmation") for tool in destructive))

    def test_direct_commerce_provider_does_not_enable_generic_oauth_tool(self):
        class ToolInfo:
            def __init__(self, **values):
                self.values = values

        tools = {}
        inject_integration_tools(
            tools, "u", "a", enabled_providers={"stripe"}, tool_info_cls=ToolInfo,
        )
        self.assertNotIn("oauth_api_call", tools)
        self.assertIn("stripe_create_checkout", tools)


if __name__ == "__main__":
    unittest.main()
