"""Behavioral tests against disposable user-plane stores, never session data."""
import asyncio
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from cryptography.fernet import Fernet
from app.chat.contracts import Conflict, Fenced, Send
from app.chat.repository import ChatRepository, digest
from app.chat.publication import PublicationService
from app.db.schema import render_plane

class Cipher:
    level = "field"
    def __init__(self):
        self.key = Fernet(Fernet.generate_key())
    async def encrypt(self, owner, value):
        return self.key.encrypt((owner+"\0"+value).encode()).decode()
    async def decrypt(self, owner, value):
        decoded=self.key.decrypt(value.encode()).decode()
        prefix=owner+"\0"
        if not decoded.startswith(prefix):
            raise PermissionError("Wrong owner")
        return decoded[len(prefix):]

class ChatCoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)/"test.db"
        def connect():
            conn=sqlite3.connect(self.path)
            conn.row_factory=sqlite3.Row
            conn.execute("PRAGMA busy_timeout=5000")
            return conn
        self.connect=connect
        conn=connect()
        conn.executescript(render_plane("user","sqlite"))
        conn.execute("INSERT INTO sessions(id,user_id,title,status) VALUES ('s','alice','Test','active')")
        conn.commit();conn.close()
        self.repo=ChatRepository("alice",connect=connect,cipher=Cipher())
        self.policy={"require_flag":True,"require_notification":True,"flags":["complete","partial","error"],
                     "cta_max_words":5,"max_tokens":0,"format_enforcement_min_tokens":0,
                     "max_attempts":2,"max_attempts_per_requirement":1}
        self.pub=PublicationService(self.repo)

    def tearDown(self):
        self.tmp.cleanup()

    async def start(self,text="Hello",cmid="one"):
        receipt=await self.repo.accept("s","alice",Send(client_message_id=cmid,text=text),self.policy)
        generation=self.repo.claim(receipt["run_id"],"worker")
        await self.repo.consume(receipt["run_id"],generation)
        return receipt["run_id"],generation

    async def test_accept_is_idempotent_and_conflicts_on_changed_content(self):
        command=Send(client_message_id="one",text="Hello")
        first=await self.repo.accept("s","alice",command,self.policy)
        second=await self.repo.accept("s","alice",command,self.policy)
        self.assertEqual(first["message_id"],second["message_id"])
        with self.assertRaises(Conflict):
            await self.repo.accept("s","alice",Send(client_message_id="one",text="Other"),self.policy)
        self.assertEqual(len((await self.repo.snapshot("s"))["items"]),1)

    async def test_committed_receipt_is_available_before_retry_metering(self):
        command=Send(client_message_id="one",text="Hello")
        self.assertIsNone(self.repo.receipt_for("s","alice",command))
        first=await self.repo.accept("s","alice",command,self.policy)
        self.assertEqual(self.repo.receipt_for("s","alice",command),
                         {**first,"duplicate":True})
        with self.assertRaises(Conflict):
            self.repo.receipt_for("s","alice",Send(client_message_id="one",text="Changed"))
        self.assertEqual(len((await self.repo.snapshot("s"))["items"]),1)

    async def test_receipt_lookup_cannot_cross_owner(self):
        from app.chat.contracts import NotFound
        await self.repo.accept("s","alice",Send(client_message_id="one",text="Hello"),self.policy)
        other=ChatRepository("bob",connect=self.connect,cipher=self.repo.cipher)
        with self.assertRaises(NotFound):
            other.receipt_for("s","alice",Send(client_message_id="one",text="Hello"))

    async def test_widget_display_text_and_element_context_are_separate(self):
        from pydantic import ValidationError
        command=Send(client_message_id="widget",text="Hidden page context\nUser question",
                     display_text="User question",app_control={"label":"Save button","x":12})
        receipt=await self.repo.accept("s","alice",command,self.policy)
        item=(await self.repo.snapshot("s"))["items"][0]
        self.assertEqual(item["payload"]["text"],"User question")
        self.assertNotIn("Hidden page context",json.dumps(item))
        self.assertNotIn("Save button",json.dumps(item))
        generation=self.repo.claim(receipt["run_id"],"worker")
        accepted=(await self.repo.consume(receipt["run_id"],generation))[0]
        self.assertEqual(accepted["text"],"Hidden page context\nUser question")
        self.assertEqual(accepted["app_control"]["label"],"Save button")
        with self.assertRaises(ValidationError):
            Send(client_message_id="bad",text="Hi",app_control={"unknown":"raw"})

    async def test_raw_output_never_enters_public_snapshot_or_replay(self):
        run,g=await self.start()
        await self.repo.journal(run,g,"model_delta",{"text":"PRIVATE WORKING TEXT"})
        snapshot=await self.repo.snapshot("s")
        changes=await self.repo.changes("s")
        self.assertNotIn("PRIVATE",json.dumps(snapshot)+json.dumps(changes))
        self.assertEqual((await self.repo.activity("s"))[0]["payload"]["text"],"PRIVATE WORKING TEXT")
        self.assertNotIn(b"PRIVATE WORKING TEXT",self.path.read_bytes())

    async def test_runtime_adapter_keeps_work_private_and_publishes_validated_items(self):
        from app.chat.runtime import Execution
        run,g=await self.start()
        execution=Execution(self.repo,run,g)
        await execution.observe({"type":"stream","content":"PRIVATE STREAM"})
        await execution.observe({"type":"response","content":"PRIVATE WORK", "chat_render_kind":"activity"})
        self.assertEqual([item["kind"] for item in (await self.repo.snapshot("s"))["items"]],["user"])
        await execution.observe({"type":"response","content":"Approved update",
                                 "chat_render_kind":"notice","mid_run":True})
        await execution.observe({"type":"response","content":"Finished.",
                                 "terminal_candidate":True,
                                 "private_closer_status":{"flag":"complete","cta":"Done"}})
        snapshot=await self.repo.snapshot("s")
        self.assertEqual([item["kind"] for item in snapshot["items"]],["user","notice","answer"])
        self.assertEqual(snapshot["items"],[event["item"] for event in await self.repo.changes("s")])
        self.assertNotIn("PRIVATE",json.dumps(snapshot))
        self.assertIn("PRIVATE STREAM",json.dumps(await self.repo.activity("s")))
        self.assertEqual((await self.repo.run(run))["state"],"completed")

    async def test_notice_and_final_have_same_snapshot_and_replay(self):
        run,g=await self.start()
        await self.pub.notice(run,g,"An approved update")
        await self.pub.final(run,g,"Finished.",{"flag":"complete","cta":"Done"})
        snapshot=await self.repo.snapshot("s")
        changes=await self.repo.changes("s")
        self.assertEqual(snapshot["items"],[x["item"] for x in changes])
        self.assertEqual(snapshot["cursor"],changes[-1]["cursor"])
        self.assertEqual((await self.repo.run(run))["state"],"completed")

    async def test_final_cannot_publish_twice(self):
        run,g=await self.start()
        await self.pub.final(run,g,"Finished.",{"flag":"complete","cta":"Done"})
        with self.assertRaises(Fenced):
            await self.pub.final(run,g,"Duplicate.",{"flag":"complete","cta":"Done"})
        self.assertEqual(sum(i["kind"]=="answer" for i in (await self.repo.snapshot("s"))["items"]),1)

    async def test_failed_validation_publishes_only_safe_notice(self):
        run,g=await self.start()
        await self.pub.final(run,g,"REJECTED DRAFT",{})
        snapshot=await self.repo.snapshot("s")
        self.assertEqual(snapshot["items"][-1]["kind"],"failure")
        self.assertNotIn("REJECTED DRAFT",json.dumps(snapshot))
        self.assertEqual((await self.repo.run(run))["state"],"failed")
        self.assertIn("candidate_id",snapshot["items"][-1]["payload"]["retry"])

    async def test_notification_ack_is_bound_to_observed_terminal_revision(self):
        first,g=await self.start()
        await self.pub.final(first,g,"First answer",{"flag":"complete","cta":"First ready"})
        observed=await self.repo.latest_notification("s")
        self.assertEqual(observed["message"],"First ready")
        self.assertTrue(self.repo.is_notification_cursor("s",observed["cursor"]))
        self.assertEqual(self.repo.notification_ack("s")["cursor"],0)
        self.repo.acknowledge("s",observed["cursor"])
        self.assertEqual(self.repo.notification_ack("s")["cursor"],observed["cursor"])
        second,g=await self.start("Again","two")
        await self.pub.final(second,g,"Second answer",{"flag":"complete","cta":"Second ready"})
        latest=await self.repo.latest_notification("s")
        self.assertGreater(latest["cursor"],self.repo.notification_ack("s")["cursor"])
        self.assertFalse(self.repo.is_notification_cursor("s",latest["cursor"]+1))
        self.repo.acknowledge("s",observed["cursor"],unread=True)
        self.assertTrue(self.repo.notification_ack("s")["manual_unread"])
        self.repo.acknowledge("s",latest["cursor"])
        self.assertFalse(self.repo.notification_ack("s")["manual_unread"])

    async def test_disabled_closer_does_not_fabricate_partial_notification_flag(self):
        relaxed={**self.policy,"require_flag":False,"require_notification":False}
        receipt=await self.repo.accept("s","alice",Send(client_message_id="one",text="Hi"),relaxed)
        run=receipt["run_id"]
        generation=self.repo.claim(run,"worker")
        await self.repo.consume(run,generation)
        await self.pub.final(run,generation,"Hello",{})
        notification=await self.repo.latest_notification("s")
        self.assertIsNone(notification["flag"])
        self.assertEqual(notification["message"],"Response ready")

    async def test_finalization_retry_uses_stored_draft_without_repeating_work(self):
        run,g=await self.start()
        await self.repo.journal(run,g,"tool_result",{"result":"work already done"})
        await self.pub.final(run,g,"Finished work",{})
        failure=(await self.repo.snapshot("s"))["items"][-1]
        candidate_id=failure["payload"]["retry"]["candidate_id"]
        source=await self.repo.retry_source("s",candidate_id)
        self.assertEqual(source["candidate"]["text"],"Finished work")
        self.assertEqual(source["user_request"],"Hello")
        self.assertIsNone(self.repo.finalization_retry_receipt(run,candidate_id))
        first=await self.repo.start_finalization_retry("s",candidate_id)
        again=await self.repo.start_finalization_retry("s",candidate_id)
        self.assertEqual(first["run_id"],again["run_id"])
        self.assertTrue(again["duplicate"])
        retry=first["run_id"]
        generation=self.repo.claim(retry,"retry-worker")
        calls=[]
        async def repair(text,status,missing,policy):
            calls.append((text,missing))
            return {"status":{"flag":"complete","cta":"Done"}}
        await self.pub.final(retry,generation,source["candidate"]["text"],
                             source["candidate"]["status"],repair=repair)
        items=(await self.repo.snapshot("s"))["items"]
        self.assertEqual([item["kind"] for item in items],["user","failure","answer"])
        self.assertEqual(items[-1]["payload"]["text"],"Finished work")
        self.assertEqual(items[-1]["run_id"],retry)
        self.assertEqual((await self.repo.run(run))["state"],"failed")
        self.assertEqual((await self.repo.run(retry))["state"],"completed")
        self.assertEqual(len(calls),1)
        self.assertEqual(sum(event["kind"]=="tool_result"
                             for event in await self.repo.activity("s")),1)
        with self.assertRaises(Conflict):
            await self.repo.retry_source("s","not-a-candidate")

    async def test_manual_retry_gets_one_repair_when_automatic_budget_is_zero(self):
        zero={**self.policy,"max_attempts":0,"max_attempts_per_requirement":0}
        receipt=await self.repo.accept("s","alice",Send(client_message_id="one",text="Hello"),zero)
        original=receipt["run_id"]
        generation=self.repo.claim(original,"worker")
        await self.repo.consume(original,generation)
        await self.pub.final(original,generation,"Draft answer",{})
        first_failure=(await self.repo.snapshot("s"))["items"][-1]
        first_candidate=first_failure["payload"]["retry"]["candidate_id"]
        retry=await self.repo.start_finalization_retry("s",first_candidate)
        generation=self.repo.claim(retry["run_id"],"retry-worker")
        source=await self.repo.retry_source("s",first_candidate)
        await self.pub.final(retry["run_id"],generation,source["candidate"]["text"],
                             source["candidate"]["status"],manual_retry=True)
        second_failure=(await self.repo.snapshot("s"))["items"][-1]
        second_candidate=second_failure["payload"]["retry"]["candidate_id"]
        second_source=await self.repo.retry_source("s",second_candidate)
        self.assertEqual(second_source["user_request"],"Hello")
        second=await self.repo.start_finalization_retry("s",second_candidate)
        generation=self.repo.claim(second["run_id"],"retry-worker-2")
        calls=[]
        async def repair(text,status,missing,policy):
            calls.append(missing)
            return {"status":{"flag":"complete","cta":"Done"}}
        await self.pub.final(second["run_id"],generation,second_source["candidate"]["text"],
                             second_source["candidate"]["status"],repair=repair,manual_retry=True)
        self.assertEqual(len(calls),1)
        self.assertEqual((await self.repo.snapshot("s"))["items"][-1]["kind"],"answer")

    async def test_finalization_model_adapter_is_tool_free_and_private(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch
        from app.chat.finalization import repair_callback
        run,g=await self.start()
        execution=SimpleNamespace(repo=self.repo,run_id=run,generation=g)
        agent_db=SimpleNamespace(get_agent_by_id=AsyncMock(return_value={"id":"agent"}))
        config={"model":"fixture","provider":"openai_subscription",
                "credential_scope":"agent","entry_id":"fixture-entry"}
        response=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content='{"status":{"flag":"complete","cta":"Done"}}'))])
        completion=AsyncMock(return_value=response)
        with patch("app.db.get_db",return_value=agent_db), \
             patch("app.admin.settings.resolve_agent_role_config",
                   new=AsyncMock(return_value=config)), \
             patch("app.chat.finalization.complete_text",new=completion):
            repair=await repair_callback(execution,"agent","Original user request")
            result=await repair("Original answer",{},["flag","notification"],self.policy)
        self.assertEqual(result,{"status":{"flag":"complete","cta":"Done"}})
        options=completion.await_args.kwargs
        self.assertNotIn("tools",options)
        self.assertIn("Original user request",options["messages"][1]["content"])
        self.assertIn("finalization_repair_output",
                      [event["kind"] for event in await self.repo.activity("s")])
        self.assertNotIn("Original user request",json.dumps(await self.repo.snapshot("s")))

    async def test_execution_error_has_no_finalization_retry(self):
        run,g=await self.start()
        await self.repo.journal(run,g,"execution_error",{"error_type":"ProviderFailure"})
        await self.repo.fail_execution(run,g)
        item=(await self.repo.snapshot("s"))["items"][-1]
        self.assertEqual(item["kind"],"failure")
        self.assertNotIn("retry",item["payload"])
        self.assertEqual((await self.repo.run(run))["state"],"failed")
        with self.assertRaises(Fenced):
            await self.pub.notice(run,g,"Late answer")

    async def test_metadata_repair_cannot_replace_answer(self):
        run,g=await self.start()
        async def repair(text,status,missing,policy):
            return {"text":"UNREQUESTED REPLACEMENT","status":{"flag":"complete","cta":"Done"}}
        await self.pub.final(run,g,"Original answer",{},repair=repair)
        self.assertEqual((await self.repo.snapshot("s"))["items"][-1]["payload"]["text"],"Original answer")

    async def test_status_schema_repair_preserves_valid_answer_text(self):
        run,g=await self.start()
        async def repair(text,status,missing,policy):
            self.assertEqual(missing,["status_schema"])
            return {"text":"Unapproved rewrite",
                    "status":{"flag":"complete","cta":"Done"}}
        await self.pub.final(run,g,"Keep this answer",
                             {"flag":"complete","cta":"Done","raw":"private"},repair=repair)
        item=(await self.repo.snapshot("s"))["items"][-1]
        self.assertEqual(item["kind"],"answer")
        self.assertEqual(item["payload"]["text"],"Keep this answer")
        self.assertNotIn("private",json.dumps(item))

    async def test_repair_budget_is_bounded(self):
        run,g=await self.start()
        calls=[]
        async def repair(*args):
            calls.append(1)
            return {"text":"Still invalid","status":{}}
        await self.pub.final(run,g,"Draft",{},repair=repair)
        self.assertEqual(len(calls),1)

    async def test_queued_input_prevents_finalization(self):
        run,g=await self.start()
        receipt=await self.repo.accept("s","alice",Send(client_message_id="two",text="More"),self.policy)
        self.assertEqual(receipt["run_id"],run)
        self.assertFalse(self.repo.begin_finalization(run,g))
        self.assertEqual((await self.repo.consume(run,g))[0]["text"],"More")
        self.assertTrue(self.repo.begin_finalization(run,g))

    async def test_input_during_finalization_creates_next_run(self):
        run,g=await self.start()
        self.repo.begin_finalization(run,g)
        receipt=await self.repo.accept("s","alice",Send(client_message_id="two",text="More"),self.policy)
        self.assertNotEqual(receipt["run_id"],run)
        with self.assertRaises(Conflict):
            self.repo.claim(receipt["run_id"],"second-worker")

    async def test_stop_fences_output_and_is_idempotent(self):
        run,g=await self.start()
        await self.repo.stop("s")
        self.assertEqual(await self.repo.stop("s"),[])
        with self.assertRaises(Fenced):
            await self.pub.notice(run,g,"Late public output")
        await self.repo.journal(run,g,"model_delta",{"text":"Late private output"})
        self.assertTrue((await self.repo.activity("s"))[-1]["kind"].startswith("stale:"))
        self.assertEqual(len((await self.repo.snapshot("s"))["items"]),2)

    async def test_other_owner_cannot_read_session(self):
        from app.chat.contracts import NotFound
        repo=ChatRepository("bob",connect=self.connect,cipher=self.repo.cipher)
        with self.assertRaises(NotFound):
            await repo.snapshot("s")

    async def test_publication_and_terminal_state_rollback_together(self):
        run,g=await self.start()
        self.repo.begin_finalization(run,g)
        with self.assertRaises(ValueError):
            await self.repo.decide(run,g,"answer",{"text":"Do not commit"},accepted=True,reasons=[],
                                   policy_digest=digest(self.policy),terminal=True,outcome="running")
        self.assertEqual(len((await self.repo.snapshot("s"))["items"]),1)
        self.assertEqual((await self.repo.run(run))["state"],"finalizing")

    async def test_worker_lease_exclusivity(self):
        run,g=await self.start()
        with self.assertRaises(Fenced):
            self.repo.claim(run,"other-worker")
        with self.assertRaises(Fenced):
            self.repo.renew(run,g,"other-worker")

    async def test_durable_replay_survives_repository_recreation(self):
        run,g=await self.start()
        await self.pub.notice(run,g,"Persisted")
        second=ChatRepository("alice",connect=self.connect,cipher=self.repo.cipher)
        self.assertEqual(await self.repo.changes("s"),await second.changes("s"))

    async def test_boot_orphan_requeues_consumed_run_for_v1_resume(self):
        run,g=await self.start()
        await self.repo.journal(run,g,"model_stream",{"text":"partial"})
        self.assertEqual(await self.repo.recover_orphaned(),[run])
        recovered=await self.repo.run(run)
        self.assertEqual(recovered["state"],"queued")
        self.assertEqual(recovered["generation"],g+1)
        self.assertEqual(recovered["stop_reason"],"server_restart_resume")
        self.assertEqual(self.repo.pending_runs(),[run])
        self.assertEqual([item["kind"] for item in (await self.repo.snapshot("s"))["items"]],["user"])
        with self.assertRaises(Fenced):
            await self.pub.notice(run,g,"Stale worker")

    async def test_boot_orphan_keeps_unconsumed_input_on_normal_queue(self):
        receipt=await self.repo.accept("s","alice",Send(client_message_id="one",text="Hello"),self.policy)
        run=receipt["run_id"]
        generation=self.repo.claim(run,"old-worker")
        self.assertEqual(await self.repo.recover_orphaned(),[run])
        recovered=await self.repo.run(run)
        self.assertEqual(recovered["state"],"queued")
        self.assertIsNone(recovered["stop_reason"])
        next_generation=self.repo.claim(run,"new-worker")
        self.assertGreater(next_generation,generation)
        self.assertEqual([row["text"] for row in await self.repo.consume(run,next_generation)],["Hello"])

    async def test_boot_orphan_resume_publishes_through_v2_authority(self):
        from unittest.mock import AsyncMock, patch
        from app.chat.context import current
        from app.chat.execution import _emit_to_visualizers
        from app.chat.runtime import Execution, _resume_orphan

        run,g=await self.start()
        await self.repo.recover_orphaned()
        generation=self.repo.claim(run,"recovery-worker")
        execution=Execution(self.repo,run,generation)

        async def resumed(session_id,reason=None):
            self.assertEqual((session_id,reason),("s","v2_boot"))
            await _emit_to_visualizers("s",{
                "type":"response","content":"Recovered answer",
                "terminal_candidate":True,
                "private_closer_status":{"flag":"complete","cta":"Recovered"},
            })
            return True

        token=current.set(execution)
        try:
            with patch("app.agent.runner.resume_one",new=AsyncMock(side_effect=resumed)):
                self.assertTrue(await _resume_orphan(execution,await self.repo.run(run)))
        finally:
            current.reset(token)
        self.assertEqual((await self.repo.run(run))["state"],"completed")
        item=(await self.repo.snapshot("s"))["items"][-1]
        self.assertEqual((item["kind"],item["payload"]["text"]),("answer","Recovered answer"))

    async def test_startup_recovery_marks_legacy_before_scheduling_v2_run(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, Mock, patch
        from app.chat.recovery import recover_once

        order=[]
        app_db=SimpleNamespace(list_user_accounts=AsyncMock(return_value=[{"user_id":"alice"}]))
        user_db=SimpleNamespace(mark_orphans_for_resume=AsyncMock(
            side_effect=lambda: order.append("mark") or 1))
        repo=SimpleNamespace(
            recover_orphaned=AsyncMock(side_effect=lambda: order.append("recover") or ["run"]),
            pending_runs=lambda: ["run"],
        )
        schedule=Mock(side_effect=lambda owner,run: order.append("schedule"))
        with patch("app.chat.recovery.get_app_db",return_value=app_db), \
             patch("app.db.get_user_db",return_value=user_db), \
             patch("app.chat.recovery.ChatRepository",return_value=repo), \
             patch("app.chat.runtime.schedule",schedule):
            await recover_once(startup=True)
        self.assertEqual(order,["mark","recover","schedule"])
        schedule.assert_called_once_with("alice","run")

    async def test_expired_attempt_is_fenced_without_repeating_consumed_input(self):
        run,g=await self.start()
        await self.repo.journal(run,g,"tool_started",{"tool":"external_write"})
        with self.repo.transaction() as conn:
            conn.execute("UPDATE chat_runs SET lease_until=0 WHERE id=?",(run,))
        self.assertEqual(await self.repo.recover_expired(),[run])
        self.assertEqual(await self.repo.recover_expired(),[])
        self.assertEqual(self.repo.pending_runs(),[])
        self.assertEqual((await self.repo.run(run))["state"],"failed")
        with self.assertRaises(Fenced):
            await self.pub.notice(run,g,"Late result")
        self.assertEqual((await self.repo.snapshot("s"))["items"][-1]["kind"],"failure")

    async def test_recovery_preserves_only_unconsumed_accepted_input(self):
        run,g=await self.start()
        await self.repo.accept("s","alice",Send(client_message_id="two",text="Pending input"),self.policy)
        with self.repo.transaction() as conn:
            conn.execute("UPDATE chat_runs SET lease_until=0 WHERE id=?",(run,))
        await self.repo.recover_expired()
        queued=self.repo.pending_runs()
        self.assertEqual(len(queued),1)
        self.assertEqual((await self.repo.run(queued[0]))["parent_run_id"],run)
        generation=self.repo.claim(queued[0],"replacement")
        messages=await self.repo.consume(queued[0],generation)
        self.assertEqual([m["text"] for m in messages],["Pending input"])

    async def test_live_lease_is_not_recovered(self):
        run,g=await self.start()
        self.assertEqual(await self.repo.recover_expired(),[])
        await self.pub.notice(run,g,"Still active")

    async def test_two_competing_claimants_have_exactly_one_winner(self):
        from concurrent.futures import ThreadPoolExecutor
        row=await self.repo.accept("s","alice",Send(client_message_id="one",text="Hello"),self.policy)
        def claim(worker):
            try:
                return self.repo.claim(row["run_id"],worker)
            except Fenced:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(claim,["worker-a","worker-b"]))
        self.assertEqual(sum(value is not None for value in results),1)

    async def test_validator_exception_never_admits_answer(self):
        from unittest.mock import patch
        run,g=await self.start()
        with patch("app.chat.closing.missing_requirements",side_effect=RuntimeError("failure")):
            await self.pub.final(run,g,"REJECT ME",{"flag":"complete","cta":"Done"})
        items=(await self.repo.snapshot("s"))["items"]
        self.assertEqual(items[-1]["kind"],"failure")
        self.assertNotIn("REJECT ME",json.dumps(items))

    async def test_malformed_main_message_control_is_private(self):
        from app.chat.visibility import main_message_text
        for value in (None,[],{"text":42},{"text":{"secret":"x"}},{"text":"valid","extra":True}):
            self.assertEqual(main_message_text(value),"")
        self.assertEqual(main_message_text({"text":" Valid "}),"Valid")

    async def test_finalization_freezes_private_candidate_and_policy(self):
        run,g=await self.start()
        self.assertTrue(await self.repo.freeze_finalization(run,g,"FIRST DRAFT",{}))
        self.assertTrue(await self.repo.freeze_finalization(run,g,"REPLACEMENT",{}))
        with self.repo.transaction(write=False) as conn:
            rows=list(conn.execute("SELECT payload,policy_digest FROM chat_candidates WHERE run_id=?",(run,)))
        self.assertEqual(len(rows),1)
        self.assertEqual((await self.repo.decode(rows[0]["payload"]))["text"],"FIRST DRAFT")
        self.assertEqual(rows[0]["policy_digest"],digest(self.policy))
        self.assertNotIn("DRAFT",json.dumps(await self.repo.snapshot("s")))

    async def test_frozen_candidate_assigns_later_input_to_new_run(self):
        run,g=await self.start()
        await self.repo.freeze_finalization(run,g,"Draft",{})
        receipt=await self.repo.accept("s","alice",Send(client_message_id="later",text="Next"),self.policy)
        self.assertNotEqual(receipt["run_id"],run)
        await self.pub.final(run,g,"Ready",{"flag":"complete","cta":"Done"})
        self.assertEqual((await self.repo.run(receipt["run_id"]))["state"],"queued")

    async def test_continue_links_new_run_without_replacing_terminal(self):
        run,g=await self.start()
        await self.repo.stop("s")
        previous=(await self.repo.snapshot("s"))["items"]
        receipt=await self.repo.accept("s","alice",Send(client_message_id="continue",text="Continue"),self.policy)
        self.assertNotEqual(run,receipt["run_id"])
        self.assertEqual((await self.repo.run(receipt["run_id"]))["parent_run_id"],run)
        self.assertEqual((await self.repo.snapshot("s"))["items"][:-1],previous)

    async def test_unknown_status_fields_cannot_enter_public_payload(self):
        run,g=await self.start()
        await self.pub.final(run,g,"Valid looking",{"flag":"complete","cta":"Done","raw":"PRIVATE"})
        snapshot=await self.repo.snapshot("s")
        self.assertEqual(snapshot["items"][-1]["kind"],"failure")
        self.assertNotIn("PRIVATE",json.dumps(snapshot))

    async def test_fresh_user_plane_supports_retained_execution_writer(self):
        from unittest.mock import AsyncMock
        from app.db.local import LocalBackend
        backend=object.__new__(LocalBackend)
        backend._get_conn=self.connect
        backend._write_lock=asyncio.Lock()
        backend.assert_session_owned=AsyncMock()
        mid=await backend.insert_interaction("alice","s",role="user",content="hi")
        conn=self.connect()
        try:
            row=conn.execute("SELECT content,session_seq,chat_render_kind FROM interactions WHERE id=?",(mid,)).fetchone()
            self.assertEqual(tuple(row),("hi",1,"user"))
        finally:
            conn.close()
        self.assertEqual((await self.repo.snapshot("s"))["items"],[])
