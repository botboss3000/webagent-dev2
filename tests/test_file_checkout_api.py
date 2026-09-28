import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.agent import file_checkout
from app.api import github


class _Db:
    async def assert_session_owned(self, user_id, session_id):
        return None

    async def get_session_change_claims(self, session_id):
        return ["dirty.py"]

    async def clear_session_change_claims(self, session_id, paths):
        return None

    async def get_session_change_owners(self, paths):
        return {"dirty.py": ["session-1"]}


def test_chat_changes_unions_clean_checkout_and_dirty_checked_in_file():
    request = SimpleNamespace()
    payload = {
        "files": [{"path": "dirty.py", "status": " M", "added": 2, "removed": 1}]
    }
    checkouts = [
        {
            "path": "README.md", "status": "checked_out", "active": True,
            "owner": "session-1", "expires_at": "later", "modified": False,
            "validated": False, "violation": None,
        },
        {
            "path": "dirty.py", "status": "checked_in", "active": False,
            "owner": None, "expires_at": None, "modified": True,
            "validated": True, "validation_note": "tests pass", "violation": None,
        },
        {
            "path": "research.py", "status": "observed", "active": False,
            "owner": None, "expires_at": None, "modified": False,
            "validated": False, "violation": None,
        },
        {
            "path": "stalled.py", "status": "checked_out_elsewhere", "active": False,
            "owner": "dead-session", "expires_at": "later", "modified": False,
            "validated": False, "violation": None,
            "manual_checkin_allowed": True,
        },
    ]

    async def scenario():
        with (
            patch.object(github, "_require_admin"),
            patch.object(github, "_get_user_id_from_request", return_value="admin"),
            patch.object(github, "get_db", return_value=_Db()),
            patch.object(github, "_run_git_job", new=AsyncMock(return_value=payload)),
            patch.object(file_checkout, "file_checkout_status", new=AsyncMock(return_value=checkouts)),
        ):
            return await github.get_chat_changes(request, session_id="session-1")

    result = asyncio.run(scenario())
    by_path = {row["path"]: row for row in result["files"]}
    assert by_path["README.md"]["checkout_status"] == "Checked out"
    assert by_path["README.md"]["status"] == ""
    assert by_path["dirty.py"]["checkout_status"] == "Checked in"
    assert by_path["dirty.py"]["added"] == 2
    assert by_path["dirty.py"]["removed"] == 1
    assert by_path["research.py"]["checkout_status"] == "Read"
    assert by_path["research.py"]["status"] == ""
    assert by_path["stalled.py"]["checkout_status"] == "Checked out elsewhere"
    assert by_path["stalled.py"]["checkout_owner"] == "dead-session"
    assert by_path["stalled.py"]["manual_checkin_allowed"] is True
