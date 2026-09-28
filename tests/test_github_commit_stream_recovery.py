import asyncio
import unittest
from unittest.mock import patch

from app.api import github


class GithubCommitStreamRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        github._COMMIT_OPERATIONS.clear()

    async def asyncTearDown(self):
        for record in github._COMMIT_OPERATIONS.values():
            task = record.get("task")
            if task and not task.done():
                task.cancel()
        await asyncio.gather(
            *(record["task"] for record in github._COMMIT_OPERATIONS.values()),
            return_exceptions=True,
        )
        github._COMMIT_OPERATIONS.clear()

    async def test_operation_finishes_without_a_stream_consumer(self):
        release = asyncio.Event()

        async def events(*_args, **_kwargs):
            yield {"phase": "analyzing"}
            await release.wait()
            yield {"phase": "done", "result": {
                "status": "committed", "push": {"ok": True}}}

        with patch.object(github, "_commit_and_push_events", events):
            operation_id, record = github._start_commit_operation(
                "message", skip_push=False, include_untracked=True)
            first = await record["queue"].get()
            self.assertEqual(first, {"phase": "analyzing"})

            # Simulate the response observer disappearing: nobody reads the queue
            # again, but the independent Git task must still reach its result.
            release.set()
            await asyncio.wait_for(record["task"], timeout=1)

        self.assertIn(operation_id, github._COMMIT_OPERATIONS)
        self.assertEqual(record["result"]["status"], "committed")
        self.assertTrue(record["result"]["push"]["ok"])

    async def test_cancelling_operation_records_an_actionable_result(self):
        started = asyncio.Event()

        async def events(*_args, **_kwargs):
            started.set()
            await asyncio.Event().wait()
            yield {"phase": "unreachable"}

        with patch.object(github, "_commit_and_push_events", events):
            _, record = github._start_commit_operation(
                "message", skip_push=False, include_untracked=True)
            await asyncio.wait_for(started.wait(), timeout=1)
            record["task"].cancel()
            await asyncio.wait_for(record["task"], timeout=1)

        self.assertEqual(record["result"]["status"], "error")
        self.assertIn("cancelled", record["result"]["message"].lower())


if __name__ == "__main__":
    unittest.main()
