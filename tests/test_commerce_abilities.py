import unittest

from app.abilities import ability_credentials_spec, ui_catalog
from app.api.agents import _CONNECTION_CATALOG


class CommerceAbilitiesTest(unittest.TestCase):
    def test_commerce_group_discovers_store_and_marketplace_providers(self):
        catalog = ui_catalog()
        groups = {group["id"]: group for group in catalog["groups"]}

        self.assertNotIn("marketplaces", groups)
        self.assertEqual(
            set(groups["commerce"]["members"]),
            {
                "shopify", "woocommerce", "bigcommerce", "square", "ebay", "etsy", "amazon",
                "stripe", "paypal", "btcpay",
            },
        )

    def test_commerce_provider_status_preserves_working_integrations(self):
        abilities = ui_catalog()["abilities"]

        for ability_id in ("shopify", "ebay", "etsy", "amazon"):
            self.assertEqual(abilities[ability_id]["group"], "commerce")
            self.assertEqual(abilities[ability_id]["kind"], "oauth")
            self.assertFalse(abilities[ability_id]["placeholder"])
            self.assertTrue(abilities[ability_id]["tools"])

        for ability_id in ("woocommerce", "bigcommerce", "square", "stripe", "paypal", "btcpay"):
            self.assertEqual(abilities[ability_id]["group"], "commerce")
            self.assertEqual(abilities[ability_id]["kind"], "ability")
            self.assertFalse(abilities[ability_id]["placeholder"])
            self.assertTrue(abilities[ability_id]["tools"])
            self.assertEqual(ability_credentials_spec(ability_id)["scope"], "agent")

        rows = {row["connection_type"]: row for row in _CONNECTION_CATALOG}
        for ability_id in ("woocommerce", "bigcommerce", "square", "stripe", "paypal", "btcpay"):
            self.assertEqual(rows[ability_id]["section"], "ability")
            self.assertEqual(rows[ability_id]["status"], "available")


if __name__ == "__main__":
    unittest.main()
