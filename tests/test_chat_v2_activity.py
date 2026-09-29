"""Activity projection tests use disposable storage, never runtime session files."""
import unittest
from unittest.mock import AsyncMock, patch
from fastapi import HTTPException
from tests import test_chat_v2_core as fixtures
from app.chat.activity import activity_page
from app.chat.contracts import Send, NotFound

class ActivityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture=fixtures.ChatCoreTests()
        self.fixture.setUp()
        self.repo=self.fixture.repo

    def tearDown(self):
        self.fixture.tearDown()

    async def test_pages_and_segments_survive_interrupt_and_terminal(self):
        run,generation=await self.fixture.start()
        first=await self.repo.journal(run,generation,"tool_call",{"tool":"read","tool_call_id":"a"})
        await self.repo.accept("s","alice",Send(client_message_id="two",text="Also check"),self.fixture.policy)
        second=await self.repo.journal(run,generation,"tool_result",{"tool":"read","tool_call_id":"a"})
        await self.repo.consume(run,generation)
        await self.fixture.pub.final(run,generation,"Done",{"flag":"complete","cta":"Done"})
        page=await activity_page(self.repo,"s",limit=1)
        self.assertTrue(page["has_more"])
        self.assertEqual(page["cursor"],first)
        self.assertEqual([s["closed"] for s in page["segments"]],[1,1])
        self.assertEqual([s["first_seq"] for s in page["segments"]],[first,second])
        tail=await activity_page(self.repo,"s",after=page["cursor"])
        self.assertFalse(tail["has_more"])
        self.assertEqual(tail["events"][0]["seq"],second)
        snapshot=await self.repo.snapshot("s")
        self.assertNotIn("tool_call",[i["kind"] for i in snapshot["items"]])
        with self.assertRaises(NotFound):
            await activity_page(self.repo,"missing")

    async def test_late_finalization_work_follows_intervening_user_message(self):
        run,generation=await self.fixture.start()
        before=await self.repo.journal(run,generation,"pipeline",{"step":"closer_start"})
        self.assertTrue(await self.repo.freeze_finalization(run,generation,"Draft",{}))
        queued=await self.repo.accept("s","alice",Send(client_message_id="two",text="Next"),self.fixture.policy)
        after=await self.repo.journal(run,generation,"pipeline",{"step":"closer_end"})
        page=await activity_page(self.repo,"s")
        original=(await self.repo.snapshot("s"))["items"][0]["id"]
        groups=[s for s in page["segments"] if s["run_id"]==run]
        self.assertEqual([s["boundary_item_id"] for s in groups],[original,queued["message_id"]])
        self.assertEqual([(s["first_seq"],s["last_seq"]) for s in groups],[(before,before),(after,after)])
        self.assertLess(groups[0]["order_id"],groups[1]["order_id"])
        self.assertEqual([e["seq"] for e in page["events"]],[before,after])
        await self.fixture.pub.final(run,generation,"Done",{"flag":"complete","cta":"Done"})
        stale=await self.repo.journal(run,generation,"tool_call",{"tool":"late"})
        finished=await activity_page(self.repo,"s")
        self.assertEqual([s["id"] for s in finished["segments"] if s["run_id"]==run],
                         [s["id"] for s in groups])
        self.assertEqual(finished["events"][-1]["seq"],stale)
        self.assertEqual(finished["events"][-1]["kind"],"stale:tool_call")

    async def test_queued_run_reanchors_when_prior_run_finishes(self):
        run,generation=await self.fixture.start()
        self.assertTrue(await self.repo.freeze_finalization(run,generation,"Draft",{}))
        queued=await self.repo.accept("s","alice",Send(client_message_id="two",text="Next"),self.fixture.policy)
        await self.fixture.pub.final(run,generation,"Done",{"flag":"complete","cta":"Done"})
        answer=(await self.repo.snapshot("s"))["items"][-1]
        generation2=self.repo.claim(queued["run_id"],"queued-worker")
        seq=await self.repo.journal(queued["run_id"],generation2,"pipeline",{"step":"turn_start"})
        page=await activity_page(self.repo,"s")
        groups=[s for s in page["segments"] if s["run_id"]==queued["run_id"]]
        self.assertEqual([s["boundary_item_id"] for s in groups],
                         [queued["message_id"],answer["id"]])
        self.assertEqual(groups[-1]["first_seq"],seq)

    async def test_endpoint_retains_admin_gate(self):
        from app.chat.api import activity
        db=AsyncMock()
        db.is_user_admin.return_value=False
        with patch("app.chat.api.scope",AsyncMock(return_value=("alice",self.repo,{}))), \
             patch("app.db.get_app_db",return_value=db):
            with self.assertRaises(HTTPException) as caught:
                await activity("s",None,after=0)
        self.assertEqual(caught.exception.status_code,403)

    async def test_retry_without_user_segment_has_stable_failure_boundary(self):
        run,generation=await self.fixture.start()
        await self.fixture.pub.final(run,generation,"Unvalidated draft",{})
        failed=(await self.repo.snapshot("s"))["items"][-1]
        candidate=failed["payload"]["retry"]["candidate_id"]
        retry=await self.repo.start_finalization_retry("s",candidate)
        retry_generation=self.repo.claim(retry["run_id"],"retry-worker")
        seq=await self.repo.journal(retry["run_id"],retry_generation,"repair",{"status":"checking"})
        page=await activity_page(self.repo,"s")
        segment=next(s for s in page["segments"] if s["run_id"]==retry["run_id"])
        self.assertEqual(segment["boundary_item_id"],failed["id"])
        self.assertEqual(segment["first_seq"],seq)
        self.assertEqual(segment["id"],"diagnostic:"+retry["run_id"])
        self.assertEqual(segment,(await activity_page(self.repo,"s"))["segments"][-1])
