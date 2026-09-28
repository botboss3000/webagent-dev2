import asyncio
import unittest

from app.agent.voice_narration import (
    NarrationDetail,
    NarrationEvent,
    NarrationPriority,
    VoiceNarrationBroker,
    normalize_agent_event,
)


class VoiceNarrationTests(unittest.IsolatedAsyncioTestCase):
    def test_normalizes_events_without_exposing_tool_payloads(self):
        item = normalize_agent_event(
            "session-1",
            {"type": "tool_call", "tool": "web_search", "args": {"secret": "never speak this"}},
            now=10.0,
        )
        self.assertIsNotNone(item)
        self.assertEqual(item.event, NarrationEvent.TOOL_STARTED)
        self.assertEqual(item.priority, NarrationPriority.DETAIL)
        self.assertEqual(item.text, "I’m using web search.")
        self.assertNotIn("secret", item.text)

    def test_detail_level_filters_noisy_events(self):
        broker = VoiceNarrationBroker(throttle_seconds=0)
        self.assertIsNone(
            broker.publish_nowait(
                "session-1",
                {"type": "tool_call", "tool": "browser"},
                detail=NarrationDetail.NORMAL,
            )
        )
        self.assertIsNotNone(
            broker.publish_nowait(
                "session-1",
                {"type": "tool_call", "tool": "browser"},
                detail=NarrationDetail.DETAILED,
            )
        )

    def test_deduplicates_and_throttles_repeated_updates(self):
        now = [100.0]
        broker = VoiceNarrationBroker(clock=lambda: now[0], dedupe_seconds=5, throttle_seconds=2)
        event = {"type": "agent_status", "status": "running"}
        self.assertIsNotNone(broker.publish_nowait("session-1", event))
        now[0] += 1
        self.assertIsNone(broker.publish_nowait("session-1", event))
        now[0] += 5
        self.assertIsNotNone(broker.publish_nowait("session-1", event))

    def test_critical_event_displaces_detail_when_queue_is_full(self):
        broker = VoiceNarrationBroker(max_queue_size=1, throttle_seconds=0)
        broker.publish_nowait(
            "session-1",
            {"type": "tool_call", "tool": "browser"},
            detail=NarrationDetail.DETAILED,
        )
        broker.publish_nowait("session-1", {"type": "error", "message": "network unavailable"})
        queued = broker.get_nowait("session-1")
        self.assertEqual(queued.event, NarrationEvent.RUN_FAILED)

    def test_session_queues_are_bounded(self):
        now = [1.0]
        broker = VoiceNarrationBroker(max_sessions=2, clock=lambda: now[0], throttle_seconds=0)
        for session_id in ("session-1", "session-2"):
            broker.publish_nowait(session_id, {"type": "agent_status", "status": "running"})
            now[0] += 1
        broker.publish_nowait("session-3", {"type": "agent_status", "status": "running"})
        self.assertEqual(broker.queue_size("session-1"), 0)
        self.assertEqual(broker.queue_size("session-2"), 1)
        self.assertEqual(broker.queue_size("session-3"), 1)

    async def test_waiting_consumer_receives_next_item(self):
        broker = VoiceNarrationBroker(throttle_seconds=0)
        pending = asyncio.create_task(broker.get("session-1", timeout=0.5))
        await asyncio.sleep(0)
        broker.publish_nowait("session-1", {"type": "agent_status", "status": "queued"})
        item = await pending
        self.assertEqual(item.event, NarrationEvent.RUN_QUEUED)

    async def test_get_timeout_is_clean(self):
        broker = VoiceNarrationBroker()
        self.assertIsNone(await broker.get("missing", timeout=0.001))


if __name__ == "__main__":
    unittest.main()
