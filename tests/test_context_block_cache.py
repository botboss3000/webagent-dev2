import tempfile
import unittest
from pathlib import Path

from app.agent.provider_cache import (
    merge_extra_body,
    prompt_cache_controls,
    stable_prompt_cache_key,
)
from app.agent.session_cache import CachedMessageList, SessionMessageCache


class ContextBlockCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_append_reuses_immutable_prefix_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = SessionMessageCache(cache_dir=Path(tmp), disk_enabled=True)
            original = [
                {"role": "system", "content": "stable"},
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "answer"},
            ]
            await cache.set("u", "s", original, "sys", "hist-1")
            first_ids = tuple(block.block_id for block in cache._data[("u", "s")].blocks)

            hit = await cache.get("u", "s", "sys", "hist-1")
            self.assertIsInstance(hit, CachedMessageList)
            hit.append({"role": "user", "content": "delta"})
            await cache.set("u", "s", hit, "sys", "hist-2")

            second = cache._data[("u", "s")]
            self.assertEqual(second.blocks[0].block_id, first_ids[0])
            self.assertEqual(second.message_count, 4)
            self.assertGreaterEqual(len(second.blocks), 2)
            replay = await cache.get("u", "s", "sys", "hist-2")
            self.assertEqual(replay, hit)

    async def test_prefix_mutation_prevents_stale_block_reuse(self):
        cache = SessionMessageCache(disk_enabled=False)
        await cache.set("u", "s", [{"role": "user", "content": "old"}], "sys", "h1")
        old_id = cache._data[("u", "s")].blocks[0].block_id
        hit = await cache.get("u", "s", "sys", "h1")
        hit[0] = {"role": "user", "content": "new"}
        await cache.set("u", "s", hit, "sys", "h2")
        self.assertNotEqual(cache._data[("u", "s")].blocks[0].block_id, old_id)

    async def test_cold_instance_rehydrates_blocks_from_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = SessionMessageCache(cache_dir=root, disk_enabled=True)
            messages = [{"role": "user", "content": "cold context"}]
            await first.set("tenant", "session", messages, "sys", "hist")

            cold = SessionMessageCache(cache_dir=root, disk_enabled=True)
            replay = await cold.get("tenant", "session", "sys", "hist")
            self.assertEqual(replay, messages)
            self.assertEqual((await cold.stats())["sessions"], 1)

    async def test_disk_manifest_advances_after_append(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            live = SessionMessageCache(cache_dir=root, disk_enabled=True)
            await live.set(
                "tenant", "session", [{"role": "user", "content": "first"}],
                "sys", "hist-1",
            )
            hit = await live.get("tenant", "session", "sys", "hist-1")
            hit.append({"role": "assistant", "content": "second"})
            await live.set("tenant", "session", hit, "sys", "hist-2")

            cold = SessionMessageCache(cache_dir=root, disk_enabled=True)
            replay = await cold.get("tenant", "session", "sys", "hist-2")
            self.assertEqual(replay, hit)

    async def test_disk_tier_works_when_block_exceeds_hot_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = SessionMessageCache(
                cache_dir=Path(tmp), hot_max_bytes=32, disk_enabled=True,
            )
            messages = [{"role": "user", "content": "x" * 5000}]
            await cache.set("u", "large", messages, "sys", "hist")
            self.assertLessEqual((await cache.stats())["hot_bytes"], 32)
            self.assertEqual(await cache.get("u", "large", "sys", "hist"), messages)

    async def test_cache_remains_tenant_and_history_scoped(self):
        cache = SessionMessageCache(disk_enabled=False)
        await cache.set("user-a", "same", [{"role": "user", "content": "secret"}], "sys", "h")
        self.assertIsNone(await cache.get("user-b", "same", "sys", "h"))
        self.assertIsNone(await cache.get("user-a", "same", "sys", "wrong"))


class ProviderPromptCacheTests(unittest.TestCase):
    def test_direct_openai_gets_hashed_routing_key_and_56_options(self):
        controls = prompt_cache_controls(
            provider="openai",
            base_url="https://api.openai.com/v1",
            model="gpt-5.6-terra",
            user_id="person@example.com",
            system_hash="abc",
        )
        self.assertEqual(controls["strategy"], "openai-routed-prefix")
        self.assertNotIn("person@example.com", controls["cache_key"])
        self.assertEqual(controls["extra_body"]["prompt_cache_options"]["ttl"], "30m")
        self.assertLessEqual(len(controls["cache_key"]), 64)

    def test_compatible_routes_use_safe_implicit_cache_without_unknown_fields(self):
        for provider, url in (
            ("openrouter", "https://openrouter.ai/api/v1"),
            ("gemini", "https://generativelanguage.googleapis.com/v1beta/openai"),
            ("deepseek", "https://api.deepseek.com/v1"),
        ):
            controls = prompt_cache_controls(
                provider=provider, base_url=url, model="model", user_id="u", system_hash="s",
            )
            self.assertEqual(controls["strategy"], "implicit-prefix")
            self.assertEqual(controls["extra_body"], {})

    def test_extra_body_merge_preserves_reasoning_and_cache_fields(self):
        merged = merge_extra_body(
            {"prompt_cache_key": "key"}, {"reasoning": {"effort": "high"}},
        )
        self.assertEqual(merged["prompt_cache_key"], "key")
        self.assertEqual(merged["reasoning"]["effort"], "high")

    def test_cache_key_is_stable(self):
        first = stable_prompt_cache_key(user_id="u", model="m", system_hash="s")
        second = stable_prompt_cache_key(user_id="u", model="m", system_hash="s")
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
