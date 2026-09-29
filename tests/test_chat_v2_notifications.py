"""Account notification pagination uses stable session IDs and does not skip empty pages."""
import unittest
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

from app.chat.api import notifications


class Connection:
    def execute(self, _query, params):
        _actor, after, limit = params
        return [(sid,) for sid in ("a", "b", "c", "d", "e") if sid > after][:limit]


class Repository:
    @contextmanager
    def transaction(self, write=False):
        yield Connection()

    async def latest_notification(self, session_id):
        if session_id in {"a", "c", "e"}:
            return {"session_id": session_id, "cursor": ord(session_id)}
        return None

    def notification_ack(self, session_id):
        return {"cursor": 0, "manual_unread": False}


class NotificationPages(unittest.IsolatedAsyncioTestCase):
    async def test_scans_all_sessions_even_when_a_page_has_no_notification(self):
        repo = Repository()
        with patch("app.chat.api.scope", AsyncMock(return_value=("owner", repo, None))):
            first = await notifications(None, after="", limit=2)
            second = await notifications(None, after=first["next_after"], limit=2)
            third = await notifications(None, after=second["next_after"], limit=2)
        self.assertEqual([row["session_id"] for page in (first, second, third)
                          for row in page["notifications"]], ["a", "c", "e"])
        self.assertEqual([page["has_more"] for page in (first, second, third)],
                         [True, True, False])
        self.assertEqual([first["next_after"], second["next_after"], third["next_after"]],
                         ["b", "d", None])
