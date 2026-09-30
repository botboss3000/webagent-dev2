"""Manual compaction behavior against disposable v2 user-plane stores."""
import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from cryptography.fernet import Fernet

from app.chat.contracts import Conflict, Fenced, Send
from app.chat.repository import ChatRepository, digest
from app.chat.runtime import Execution
from app.db.schema import render_plane, ensure_sqlite_plane_columns


class Cipher:
    level = "field"

    def __init__(self):
        self.key = Fernet(Fernet.generate_key())

    async def encrypt(self, owner, value):
        return self.key.encrypt((owner + "\\0" + value).encode()).decode()

    async def decrypt(self, owner, value):
        data = self.key.decrypt(value.encode()).decode()
        prefix = owner + "\\0"
        if not data.startswith(prefix):
            raise PermissionError("Wrong owner")
        return data[len(prefix):]


class ManualCompactionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "user.db"

        def connect():
            conn = sqlite3.connect(self.path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=5000")
            return conn

        self.connect = connect
        conn = connect()
        conn.executescript(render_plane("user", "sqlite"))
        conn.execute("INSERT INTO sessions(id,user_id,title,agent_id,status) VALUES ('s','alice','Test','agent','active')")
        conn.commit()
        conn.close()
        self.repo = ChatRepository("alice", connect=connect, cipher=Cipher())
        self.policy = {"require_flag": False, "require_notification": False}

    def tearDown(self):
        self.tmp.cleanup()

    async def accept(self, cmid, text, kind="chat"):
        return await self.repo.accept(
            "s", "alice", Send(client_message_id=cmid, text=text), self.policy, kind=kind,
        )

    async def finish(self, run_id, generation):
        await self.repo.consume(run_id, generation)
        self.assertTrue(self.repo.begin_finalization(run_id, generation))
        await self.repo.decide(
            run_id, generation, "notice", {"text": "Done"},
            accepted=True, reasons=[], policy_digest=digest(self.policy), terminal=True,
        )

    async def test_compaction_is_isolated_ordered_and_idempotent(self):
        first = await self.accept("a", "First")
        compact = await self.accept("b", "/compact", "compaction")
        later = await self.accept("c", "After")
        joined = await self.accept("d", "Also after")
        self.assertEqual(later["run_id"], joined["run_id"])
        self.assertEqual((await self.accept("b", "/compact", "compaction"))["run_id"], compact["run_id"])
        self.assertEqual(len({first["run_id"], compact["run_id"], later["run_id"]}), 3)
        with self.assertRaises(Conflict):
            self.repo.claim(compact["run_id"], "worker")
        with self.assertRaises(Conflict):
            self.repo.claim(later["run_id"], "worker")
        generation = self.repo.claim(first["run_id"], "worker")
        await self.finish(first["run_id"], generation)
        generation = self.repo.claim(compact["run_id"], "worker")
        self.assertEqual([r["text"] for r in await self.repo.consume(compact["run_id"], generation)], ["/compact"])
        with self.assertRaises(Conflict):
            self.repo.claim(later["run_id"], "other-worker")
        self.assertTrue(self.repo.begin_finalization(compact["run_id"], generation))
        await self.repo.decide(
            compact["run_id"], generation, "notice", {"text": "Nothing eligible", "compaction": {"status": "no_op"}},
            accepted=True, reasons=[], policy_digest=digest(self.policy), terminal=True,
        )
        self.assertEqual(self.repo.claim(later["run_id"], "worker"), 1)
        snapshot = await self.repo.snapshot("s")
        self.assertEqual([r["kind"] for r in snapshot["runs"]], ["chat", "compaction", "chat"])
        self.assertEqual(sum(i["terminal"] for i in snapshot["items"] if i["run_id"] == compact["run_id"]), 1)

    async def test_success_uses_v2_publication_without_legacy_db_writes(self):
        from app.chat.compaction import run_manual_compaction

        receipt = await self.accept("b", "/compact", "compaction")
        run_id = receipt["run_id"]
        generation = self.repo.claim(run_id, "worker")
        await self.repo.consume(run_id, generation)
        self.assertTrue(self.repo.begin_finalization(run_id, generation))
        with (
            patch("app.chat.compaction.get_context_settings", AsyncMock(return_value={"enabled": True})),
            patch("app.chat.compaction.maybe_compact", AsyncMock(return_value={"tokens_before": 100, "tokens_after": 40})) as fold,
            patch("app.agent.compaction._latest_provider_context_measurement", AsyncMock(return_value=(0, ""))),
            patch("app.chat.compaction.turn_prewarm.invalidate") as invalidate,
        ):
            await run_manual_compaction(Execution(self.repo, run_id, generation), object(),
                                        {"id": "s", "agent_id": "agent"})
        fold.assert_awaited_once()
        invalidate.assert_called_once_with("s")
        item = (await self.repo.snapshot("s"))["items"][-1]
        self.assertEqual((item["kind"], item["terminal"]), ("notice", True))
        self.assertEqual(item["payload"]["compaction"]["history_tokens_after"], 40)
        self.assertEqual((await self.repo.changes("s"))[-1]["item"], item)
        self.assertEqual([event["kind"] for event in await self.repo.activity("s")],
                         ["compaction_completed"])
        self.assertEqual((await self.repo.run(run_id))["state"], "completed")

    async def test_no_op_and_summarizer_failure_are_terminal(self):
        from app.chat.compaction import run_manual_compaction

        for cmid, info, expected_kind, expected_state in (
            ("no-op", None, "notice", "completed"),
            ("failure", {"status": "failed", "reason": "No model configured."}, "failure", "failed"),
        ):
            receipt = await self.accept(cmid, "/compact", "compaction")
            run_id = receipt["run_id"]
            generation = self.repo.claim(run_id, "worker")
            await self.repo.consume(run_id, generation)
            self.repo.begin_finalization(run_id, generation)
            with (
                patch("app.chat.compaction.get_context_settings", AsyncMock(return_value={"enabled": True})),
                patch("app.chat.compaction.maybe_compact", AsyncMock(return_value=info)),
            ):
                await run_manual_compaction(Execution(self.repo, run_id, generation), object(),
                                            {"id": "s", "agent_id": "agent"})
            item = (await self.repo.snapshot("s"))["items"][-1]
            self.assertEqual((item["kind"], item["payload"]["compaction"]["status"]),
                             (expected_kind, "no_op" if info is None else "failed"))
            self.assertEqual((await self.repo.run(run_id))["state"], expected_state)

    async def test_successor_worker_waits_for_auto_fold_before_claim(self):
        from app.chat import runtime

        entered, release = asyncio.Event(), asyncio.Event()
        claims = []

        class Repo:
            async def run(self, run_id):
                return {"session_id": "s"}

            def claim(self, run_id, worker_id):
                claims.append(run_id)
                raise Fenced("Run was stopped")

        async def wait_for_fold(owner, session_id):
            self.assertEqual((owner, session_id), ("alice", "s"))
            entered.set()
            await release.wait()

        with (
            patch("app.chat.runtime.ChatRepository", return_value=Repo()),
            patch("app.agent.compaction.wait_for_automatic_compaction", wait_for_fold),
        ):
            worker = asyncio.create_task(runtime._drive("alice", "next-run"))
            await asyncio.wait_for(entered.wait(), 1)
            self.assertEqual(claims, [])
            release.set()
            await asyncio.wait_for(worker, 1)

        self.assertEqual(claims, ["next-run"])


    async def test_manual_stop_cancels_its_fold(self):
        from app.chat.compaction import run_manual_compaction

        receipt = await self.accept("manual-stop", "/compact", "compaction")
        run_id = receipt["run_id"]
        generation = self.repo.claim(run_id, "worker")
        await self.repo.consume(run_id, generation)
        self.assertTrue(self.repo.begin_finalization(run_id, generation))
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def fold(*args, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        with (
            patch("app.chat.compaction.get_context_settings", AsyncMock(return_value={"enabled": True})),
            patch("app.chat.compaction.maybe_compact", fold),
        ):
            task = asyncio.create_task(run_manual_compaction(
                Execution(self.repo, run_id, generation), object(),
                {"id": "s", "agent_id": "agent"},
            ))
            await asyncio.wait_for(entered.wait(), 1)
            await self.repo.stop("s")
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertTrue(cancelled.is_set())
        item = (await self.repo.snapshot("s"))["items"][-1]
        self.assertEqual(item["payload"]["compaction"]["status"], "uncertain")


    async def test_stop_and_boot_recovery_report_uncertain_checkpoint(self):
        receipt = await self.accept("b", "/compact", "compaction")
        run_id = receipt["run_id"]
        generation = self.repo.claim(run_id, "worker")
        await self.repo.consume(run_id, generation)
        await self.repo.stop("s")
        item = (await self.repo.snapshot("s"))["items"][-1]
        self.assertEqual(item["payload"]["compaction"]["status"], "uncertain")
        with self.assertRaises(Fenced):
            self.repo.begin_finalization(run_id, generation)

        receipt = await self.accept("c", "/compact", "compaction")
        run_id = receipt["run_id"]
        self.repo.claim(run_id, "worker")
        self.assertEqual(await self.repo.recover_orphaned(), [run_id])
        self.assertEqual((await self.repo.run(run_id))["state"], "queued")
        generation = self.repo.claim(run_id, "worker-2")
        await self.repo.consume(run_id, generation)
        self.assertEqual(await self.repo.recover_orphaned(), [run_id])
        item = (await self.repo.snapshot("s"))["items"][-1]
        self.assertEqual((item["kind"], item["payload"]["compaction"]["status"]), ("failure", "uncertain"))
        self.assertEqual(self.repo.pending_runs(), [])

    async def test_expired_chat_pending_input_keeps_place_before_compaction(self):
        first = await self.accept("a", "First")
        generation = self.repo.claim(first["run_id"], "worker")
        await self.repo.consume(first["run_id"], generation)
        await self.accept("b", "Already accepted")
        compact = await self.accept("c", "/compact", "compaction")
        with self.repo.transaction() as conn:
            conn.execute("UPDATE chat_runs SET lease_until=0 WHERE id=?", (first["run_id"],))
        await self.repo.recover_expired()
        recovered = self.repo.pending_runs()
        self.assertEqual(len(recovered), 2)
        self.assertEqual(recovered[1], compact["run_id"])
        with self.assertRaises(Conflict):
            self.repo.claim(compact["run_id"], "worker")
        self.assertEqual([r["text"] for r in await self.repo.consume(
            recovered[0], self.repo.claim(recovered[0], "worker"))], ["Already accepted"])

    async def test_submit_accepts_compact_once_and_rejects_attached_command(self):
        from app.chat.runtime import submit
        from types import SimpleNamespace

        command = Send(client_message_id="compact", text="/compact")
        session = {"id": "s", "agent_id": "agent"}
        agent = {"id": "agent"}
        with (
            patch("app.db.get_db", return_value=object()),
            patch("app.chat.execution._require_chat_agent_access", AsyncMock(return_value=agent)),
            patch("app.chat.execution._enforce_billing_access", AsyncMock()) as billing,
            patch("app.api.rate_limit.enforce_tier_chat", AsyncMock()),
            patch("app.chat.runtime.resolve_policy", return_value=self.policy),
            patch("app.chat.runtime.schedule"),
        ):
            first = await submit(SimpleNamespace(), "alice", self.repo, session, command)
            second = await submit(SimpleNamespace(), "alice", self.repo, session, command)
            self.assertEqual(first["run_id"], second["run_id"])
            self.assertTrue(second["duplicate"])
            self.assertEqual(billing.await_count, 1)
            with self.assertRaises(Conflict):
                await submit(SimpleNamespace(), "alice", self.repo, session,
                             Send(client_message_id="bad", text="/compact", attachment_ids=["attachment"]))
        self.assertEqual((await self.repo.run(first["run_id"]))["kind"], "compaction")

    def test_existing_schema_adds_run_columns_with_safe_defaults(self):
        conn = self.connect()
        conn.execute("ALTER TABLE chat_runs DROP COLUMN kind")
        conn.execute("ALTER TABLE chat_runs DROP COLUMN queue_seq")
        ensure_sqlite_plane_columns(conn, "user")
        conn.execute("""INSERT INTO chat_runs(id,session_id,message_id,state,policy,created_at,updated_at)
                        VALUES ('old','s','m','queued','encrypted','now','now')""")
        row = conn.execute("SELECT kind,queue_seq FROM chat_runs WHERE id='old'").fetchone()
        self.assertEqual(tuple(row), ("chat", 0))
        conn.close()
