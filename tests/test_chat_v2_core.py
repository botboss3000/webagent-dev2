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

    async def test_agent_auth_tool_result_publishes_only_safe_public_fields(self):
        from app.chat.runtime import Execution
        run,generation=await self.start()
        execution=Execution(self.repo,run,generation)
        await execution.observe({"type":"tool_result","result":json.dumps({
            "ui":"agent_auth","local_signup_mode":"invite",
            "app_login_enabled":True,"private_token":"must-not-publish"})})
        snapshot=await self.repo.snapshot("s")
        component=[item for item in snapshot["items"] if item["kind"]=="component"]
        self.assertEqual(len(component),1)
        self.assertEqual(component[0]["payload"],{
            "ui":"agent_auth","local_signup_mode":"invite","app_login_enabled":True})
        self.assertNotIn("must-not-publish",json.dumps(snapshot))

    async def test_generated_file_is_a_durable_typed_public_item(self):
        from unittest.mock import patch
        from app.chat.runtime import Execution
        run,generation=await self.start()
        descriptor={"kind":"file_attachment","status":"ok","filename":"plot.png",
                    "mime_type":"image/png","url":"/api/v1/content/token","bytes":42,
                    "private_path":"must-not-publish"}
        with patch("app.chat.publication.decode_content_grant",return_value={
            "user_id":"alice","source_type":"artifact","artifact_id":"artifact-one"}):
            await Execution(self.repo,run,generation).observe({
                "type":"tool_result","tool":"save_file","result":json.dumps(descriptor)})
        snapshot=await self.repo.snapshot("s")
        item=next(item for item in snapshot["items"] if item["kind"]=="attachment")
        self.assertEqual(item["payload"],{
            "artifact_id":"artifact-one","source_type":"artifact",
            "original_name":"plot.png","mime_type":"image/png","size_bytes":42})
        self.assertNotIn("token",json.dumps(snapshot))
        self.assertNotIn("must-not-publish",json.dumps(snapshot))
        replay=await self.repo.snapshot("s")
        self.assertEqual(replay["items"],snapshot["items"])

    async def test_generated_file_cannot_publish_another_users_grant(self):
        from unittest.mock import patch
        from app.chat.runtime import Execution
        run,generation=await self.start()
        with patch("app.chat.publication.decode_content_grant",return_value={
            "user_id":"bob","source_type":"artifact","artifact_id":"artifact-one"}):
            await Execution(self.repo,run,generation).observe({
                "type":"tool_result","tool":"save_file","result":{
                    "kind":"file_attachment","status":"ok","filename":"secret.txt",
                    "mime_type":"text/plain","url":"/api/v1/content/token","bytes":1}})
        snapshot=await self.repo.snapshot("s")
        self.assertFalse(any(item["kind"]=="attachment" for item in snapshot["items"]))

    async def test_generated_images_and_screenshot_publish_owned_media(self):
        from unittest.mock import patch
        from app.chat.runtime import Execution
        run,generation=await self.start()
        def grant(token):
            return {"user_id":"alice","source_type":"path",
                    "storage_path":"alice/visuals/"+token+".png",
                    "original_name":token+".png","mime_type":"image/png"}
        with patch("app.chat.publication.decode_content_grant",side_effect=grant), \
             patch("app.chat.publication.register_path_artifact",
                   side_effect=["image-one","image-two","screenshot"]):
            execution=Execution(self.repo,run,generation)
            await execution.observe({"type":"tool_result","tool":"generate_image",
                "result":{"status":"ok","images":[
                    "/api/v1/content/one","/api/v1/content/two"]}})
            await execution.observe({"type":"tool_result","tool":"browser_action",
                "result":{"success":True,"mime_type":"image/png",
                          "result":"/api/v1/content/shot"}})
        snapshot=await self.repo.snapshot("s")
        media=[item["payload"] for item in snapshot["items"] if item["kind"]=="attachment"]
        self.assertEqual([item["artifact_id"] for item in media],
                         ["image-one","image-two","screenshot"])

    async def test_closer_summary_projects_once_after_final_answer(self):
        run,generation=await self.start()
        await self.pub.final(run,generation,"Done.",{"flag":"complete","cta":"Open"})
        first=await self.repo.publish_closer_summary("s","closer-row","Summary of work.")
        second=await self.repo.publish_closer_summary("s","closer-row","Summary of work.")
        self.assertEqual(first["id"],second["id"])
        snapshot=await self.repo.snapshot("s")
        self.assertEqual([item["kind"] for item in snapshot["items"]],
                         ["user","answer","notice"])
        self.assertEqual(snapshot["items"][-1]["payload"]["control"]["kind"],
                         "output_closer")
        self.assertEqual(snapshot["items"][-1]["payload"]["text"],"Summary of work.")

    async def test_unsupported_interactive_tool_result_is_explicit(self):
        from app.chat.runtime import Execution
        run,generation=await self.start()
        execution=Execution(self.repo,run,generation)
        await execution.observe({"type":"tool_result","result":{
            "ui":"agent_profile_admin","members":[{"private":"must-not-publish"}]}})
        snapshot=await self.repo.snapshot("s")
        notice=[item for item in snapshot["items"] if item["kind"]=="notice"]
        self.assertEqual(len(notice),1)
        self.assertIn("unavailable",notice[0]["payload"]["text"])
        self.assertNotIn("must-not-publish",json.dumps(snapshot))

    async def test_session_mode_notice_is_public_without_a_v1_run(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch
        from app.chat.api import SessionMode, session_mode
        db=SimpleNamespace(set_session_execution_mode=AsyncMock(return_value="plan"))
        with patch("app.chat.api.scope", AsyncMock(return_value=("alice",self.repo,{}))), \
             patch("app.db.get_db",return_value=db):
            result=await session_mode("s",SessionMode(mode="plan"),None)
        self.assertEqual(result,{"execution_mode":"plan"})
        snapshot=await self.repo.snapshot("s")
        self.assertEqual(len(snapshot["items"]),1)
        notice=snapshot["items"][0]
        self.assertEqual((notice["kind"],notice["run_id"]),("notice",None))
        self.assertEqual(notice["payload"]["control"]["mode"],"plan")
        self.assertEqual(snapshot["cursor"],1)

    async def test_voice_route_reads_v2_active_run(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch
        from app.chat.api import VoiceRoute, route_voice
        await self.start()
        request=SimpleNamespace(state=SimpleNamespace())
        with patch("app.chat.api.scope",AsyncMock(return_value=("alice",self.repo,{}))), \
             patch("app.chat.execution.voice_route",AsyncMock(return_value={"action":"noop"})) as route:
            result=await route_voice("s",VoiceRoute(utterance="stop"),request)
        self.assertEqual(result["action"],"noop")
        self.assertTrue(request.state.controlled_voice_active_run)
        self.assertEqual(route.await_args.args[0].session_id,"s")
        self.assertEqual(route.await_args.args[0].user_id,"alice")

    async def test_voice_stop_uses_v2_run_state_and_pause_is_unavailable(self):
        from unittest.mock import AsyncMock, patch
        from fastapi import HTTPException
        from app.chat.api import VoiceControl, control_voice
        with patch("app.chat.api.scope", AsyncMock(return_value=("alice",self.repo,{}))), \
             patch("app.chat.runtime.cancel") as cancel:
            pause=VoiceControl(action="pause",idempotency_key="12345678")
            with self.assertRaises(HTTPException) as caught:
                await control_voice("s",pause,None)
            self.assertEqual(caught.exception.status_code,409)
            stop=VoiceControl(action="stop",idempotency_key="12345678")
            idle=await control_voice("s",stop,None)
            await self.start(cmid="voice-active")
            active=await control_voice("s",stop,None)
        self.assertIs(idle["stopped"],False)
        self.assertIs(active["stopped"],True)
        self.assertEqual(cancel.call_count,2)

    async def test_row_details_returns_every_column_and_exact_plaintext(self):
        from unittest.mock import AsyncMock, patch
        from app.chat.api import row_details
        from fastapi import HTTPException
        receipt = await self.repo.accept("s", "alice", Send(client_message_id="row", text="Hello"), self.policy)
        scope = AsyncMock(return_value=("alice", self.repo, {}))
        with self.repo.transaction(write=False) as conn:
            stored = dict(conn.execute("SELECT * FROM chat_publications WHERE id=?",
                                       (receipt["message_id"],)).fetchone())
        raw = await self.repo.cipher.decrypt("alice", stored["payload"])
        with patch("app.chat.api.scope", scope):
            result = await row_details("s", receipt["message_id"], None)
            self.assertEqual(result["row"], {**stored, "payload": raw})
            self.assertEqual(result["parsed_payload"], json.loads(raw))
            self.assertEqual(result["render_type"], "user")
            self.assertEqual(result["table"], "chat_publications")
            self.assertEqual(scope.await_count, 2)
            with self.assertRaises(HTTPException) as caught:
                await row_details("different-session", receipt["message_id"], None)
            self.assertEqual(caught.exception.status_code, 404)

    async def test_row_details_checks_owner_and_rechecks_access(self):
        from unittest.mock import AsyncMock, patch
        from app.chat.api import row_details
        from fastapi import HTTPException
        receipt = await self.repo.accept("s", "alice", Send(client_message_id="row", text="Hello"), self.policy)
        denied = HTTPException(404, "Session not found")
        with patch("app.chat.api.scope", AsyncMock(side_effect=denied)):
            with self.assertRaises(HTTPException):
                await row_details("s", receipt["message_id"], None)
        with patch("app.chat.api.scope", AsyncMock(side_effect=[("alice", self.repo, {}), denied])):
            with self.assertRaises(HTTPException):
                await row_details("s", receipt["message_id"], None)

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

    async def test_new_message_starts_after_failed_recovery_without_replaying_work(self):
        run,g=await self.start()
        await self.repo.recover_orphaned()
        self.assertEqual((await self.repo.run(run))["state"],"failed")
        later=await self.repo.accept(
            "s","alice",Send(client_message_id="later",text="New direction"),self.policy)
        self.assertNotEqual(later["run_id"],run)
        generation=self.repo.claim(later["run_id"],"next-worker")
        rows=await self.repo.consume(later["run_id"],generation)
        self.assertEqual([row["text"] for row in rows],["New direction"])
        self.assertEqual((await self.repo.run(later["run_id"]))["parent_run_id"],None)

    async def test_v1_watchdog_does_not_claim_a_v2_run(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, Mock, patch
        from app.agent.runner import resume_one

        run,g=await self.start()
        legacy=SimpleNamespace(run_state_get=AsyncMock(return_value={
            "user_id":"alice","status":"interrupted","stop_cause":"crash"}))
        manager=Mock()
        with patch.dict("os.environ",{"WEBAGENT_V2_ISOLATED":"1"}), \
             patch("app.db.get_db",return_value=legacy), \
             patch("app.db.get_user_db",return_value=SimpleNamespace(_get_conn=self.connect)), \
             patch("app.agent.run_manager.get_run_manager",return_value=manager):
            self.assertFalse(await resume_one("s",reason="runtime:crash"))
        manager.is_running.assert_not_called()
        self.assertEqual((await self.repo.run(run))["state"],"running")


    async def test_unconfirmed_tool_result_blocks_automatic_replay(self):
        from app.chat.runtime import Execution, _settle_execution

        run,g=await self.start()
        await self.repo.journal(run,g,"tool_call",{
            "tool":"external_write","tool_call_id":"call-1"})
        self.assertTrue(await self.repo.has_uncertain_tool_result(run))
        await _settle_execution(Execution(self.repo,run,g))
        self.assertEqual((await self.repo.run(run))["state"],"failed")
        item=(await self.repo.snapshot("s"))["items"][-1]
        self.assertIn("unconfirmed tool result",item["payload"]["text"])

    async def test_auto_recovery_handles_expired_lease_and_unfinished_execution(self):
        from app.chat.runtime import Execution, _settle_execution
        run,g=await self.start()
        with self.repo.transaction() as conn:
            conn.execute("UPDATE chat_runs SET lease_until=0 WHERE id=?",(run,))
        await self.repo.recover_expired()
        resumed=await self.repo.resume_interrupted()
        self.assertEqual(len(resumed),1)
        g=self.repo.claim(resumed[0],"next")
        await self.repo.consume(resumed[0],g)
        await _settle_execution(Execution(self.repo,resumed[0],g))
        self.assertEqual(len(await self.repo.resume_interrupted()),1)

    async def test_auto_recovery_supervisor_schedules_real_continuation(self):
        from unittest.mock import AsyncMock, patch
        from types import SimpleNamespace
        from app.chat.recovery import recover_once
        run,g=await self.start()
        with patch("app.chat.recovery.get_app_db",return_value=SimpleNamespace(list_user_accounts=AsyncMock(return_value=[{"user_id":"alice"}]))), patch("app.chat.recovery.ChatRepository",return_value=self.repo), patch("app.chat.runtime.schedule") as schedule:
            await recover_once(startup=True)
        schedule.assert_called_once_with("alice",self.repo.pending_runs()[0])
    async def test_auto_recovery_resumes_restart_once_without_replaying_input(self):
        run,g=await self.start(text="Do the original work")
        await self.repo.recover_orphaned()
        resumed=await self.repo.resume_interrupted()
        self.assertEqual(len(resumed),1)
        self.assertEqual(await self.repo.resume_interrupted(),[])
        child=await self.repo.run(resumed[0])
        self.assertEqual(child["parent_run_id"],run)
        rows=await self.repo.consume(child["id"],self.repo.claim(child["id"],"next"))
        self.assertIn("recorded history",rows[0]["text"])
        self.assertNotIn("Do the original work",rows[0]["text"])

    async def test_auto_recovery_does_not_override_newer_user_work(self):
        await self.start()
        await self.repo.recover_orphaned()
        await self.repo.accept("s","alice",Send(client_message_id="new",text="New direction"),self.policy)
        self.assertEqual(await self.repo.resume_interrupted(),[])

    async def test_auto_recovery_blocks_unknown_tool_effects_and_user_stop(self):
        run,g=await self.start()
        await self.repo.journal(run,g,"tool_call",{"tool_call_id":"unknown","tool":"write"})
        await self.repo.recover_orphaned()
        self.assertEqual(await self.repo.resume_interrupted(),[])
        receipt=await self.repo.accept("s","alice",Send(client_message_id="stop",text="Continue"),self.policy)
        await self.repo.stop("s")
        self.assertEqual(await self.repo.resume_interrupted(),[])

    async def test_auto_recovery_is_bounded_across_repeated_restarts(self):
        await self.start()
        for attempt in range(3):
            await self.repo.recover_orphaned()
            resumed=await self.repo.resume_interrupted()
            self.assertEqual(len(resumed),1)
            g=self.repo.claim(resumed[0],"next")
            await self.repo.consume(resumed[0],g)
        await self.repo.recover_orphaned()
        self.assertEqual(await self.repo.resume_interrupted(),[])

    async def test_confirmed_tool_result_is_eligible_for_recovery(self):
        run,g=await self.start()
        await self.repo.journal(run,g,"tool_call",{
            "tool":"read_source","tool_call_id":"call-1"})
        await self.repo.journal(run,g,"tool_result",{
            "tool":"read_source","tool_call_id":"call-1"})
        self.assertFalse(await self.repo.has_uncertain_tool_result(run))


    async def test_consumed_execution_fails_without_v1_recovery_state(self):
        from unittest.mock import patch
        from app.chat.runtime import Execution, _settle_execution

        run,g=await self.start()
        with patch("app.db.get_db",side_effect=AssertionError("v1 run state consulted")):
            await _settle_execution(Execution(self.repo,run,g))
        self.assertEqual((await self.repo.run(run))["state"],"failed")
        self.assertEqual(self.repo.pending_runs(),[])
        self.assertEqual([item["kind"] for item in (await self.repo.snapshot("s"))["items"]],
                         ["user","failure"])

    async def test_shutdown_keeps_run_for_boot_recovery(self):
        from app.chat import runtime
        from app.chat.runtime import Execution, _settle_execution

        run,g=await self.start()
        execution=Execution(self.repo,run,g)
        prior=runtime._shutting_down
        runtime._shutting_down=True
        try:
            await _settle_execution(execution)
        finally:
            runtime._shutting_down=prior
        self.assertEqual((await self.repo.run(run))["state"],"running")
        self.assertEqual(await self.repo.recover_orphaned(),[run])
        self.assertEqual((await self.repo.run(run))["stop_reason"],"server_restart")
        self.assertEqual((await self.repo.snapshot("s"))["items"][-1]["kind"],"failure")

    async def test_nonrecoverable_execution_is_terminal(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch
        from app.chat.runtime import Execution, _settle_execution

        run,g=await self.start()
        execution=Execution(self.repo,run,g)
        legacy=SimpleNamespace(run_state_get=AsyncMock(return_value={
            "status":"error","stop_cause":"provider_unavailable"}))
        with patch("app.db.get_db",return_value=legacy):
            await _settle_execution(execution)
        self.assertEqual((await self.repo.run(run))["state"],"failed")
        self.assertEqual((await self.repo.snapshot("s"))["items"][-1]["kind"],
                         "failure")


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

    async def test_boot_orphan_fails_consumed_run_without_replaying_work(self):
        run,g=await self.start()
        await self.repo.journal(run,g,"model_stream",{"text":"partial"})
        self.assertEqual(await self.repo.recover_orphaned(),[run])
        recovered=await self.repo.run(run)
        self.assertEqual(recovered["state"],"failed")
        self.assertEqual(recovered["generation"],g+1)
        self.assertEqual(recovered["stop_reason"],"server_restart")
        self.assertEqual(self.repo.pending_runs(),[])
        self.assertEqual([item["kind"] for item in (await self.repo.snapshot("s"))["items"]],
                         ["user","failure"])
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

    async def test_boot_orphan_terminal_failure_is_idempotent(self):
        run,g=await self.start()
        await self.repo.recover_orphaned()
        self.assertEqual(await self.repo.recover_orphaned(),[])
        snapshot=await self.repo.snapshot("s")
        self.assertEqual([item["kind"] for item in snapshot["items"]],["user","failure"])
        self.assertEqual(sum(item["terminal"] for item in snapshot["items"]),1)

    async def test_startup_recovery_schedules_only_v2_pending_runs(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, Mock, patch
        from app.chat.recovery import recover_once

        order=[]
        app_db=SimpleNamespace(list_user_accounts=AsyncMock(return_value=[{"user_id":"alice"}]))
        repo=SimpleNamespace(
            recover_orphaned=AsyncMock(side_effect=lambda: order.append("recover") or ["run"]),
            resume_interrupted=AsyncMock(return_value=[]),
            pending_runs=lambda: ["run"],
        )
        schedule=Mock(side_effect=lambda owner,run: order.append("schedule"))
        with patch("app.chat.recovery.get_app_db",return_value=app_db), \
             patch("app.chat.recovery.ChatRepository",return_value=repo), \
             patch("app.chat.runtime.schedule",schedule):
            await recover_once(startup=True)
        self.assertEqual(order,["recover","schedule"])
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
