"""Focused coverage for server-authoritative mobile Agents sidebar favorites."""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from app.api import agents as agents_api
from app.db.local import LocalBackend


def run(coro):
    return asyncio.run(coro)


def test_mobile_agent_sidebar_preferences_are_ordered_and_revisioned():
    with tempfile.TemporaryDirectory() as directory:
        db = LocalBackend(str(Path(directory) / "preferences.db"), seed=False)

        assert run(db.get_user_agent_sidebar_preferences("alice")) == {
            "agent_ids": [], "revision": 0, "configured": False,
        }

        first = run(db.replace_user_agent_sidebar_preferences(
            "alice", ["agent-b", "agent-a", "agent-b"], 0,
        ))
        assert first == {
            "conflict": False,
            "snapshot": {"agent_ids": ["agent-b", "agent-a"], "revision": 1, "configured": True},
        }

        stale = run(db.replace_user_agent_sidebar_preferences("alice", ["agent-c"], 0))
        assert stale == {
            "conflict": True,
            "snapshot": {"agent_ids": ["agent-b", "agent-a"], "revision": 1, "configured": True},
        }

        cleared = run(db.replace_user_agent_sidebar_preferences("alice", [], 1))
        assert cleared == {
            "conflict": False,
            "snapshot": {"agent_ids": [], "revision": 2, "configured": True},
        }
        assert run(db.get_user_agent_sidebar_preferences("alice")) == cleared["snapshot"]


def test_profile_piggybacks_mobile_agent_favorites():
    db = Mock()
    db.get_user_profile = AsyncMock(return_value={
        "user_id": "alice", "is_admin": 0, "default_agent_id": "agent-a",
    })
    db.get_user_agent_sidebar_preferences = AsyncMock(return_value={
        "agent_ids": ["agent-a"], "revision": 4, "configured": True,
    })

    with patch.object(agents_api, "get_db", return_value=db), patch.object(
        agents_api, "assert_caller_is", new=AsyncMock(return_value="alice"),
    ):
        result = run(agents_api.get_user_profile(Mock(), "alice"))

    assert result["mobile_agent_favorites"] == {
        "agent_ids": ["agent-a"], "revision": 4, "configured": True,
    }
    db.get_user_agent_sidebar_preferences.assert_awaited_once_with("alice")


def test_mobile_agent_favorites_route_checks_identity_and_returns_conflict_snapshot():
    db = Mock()
    db.replace_user_agent_sidebar_preferences = AsyncMock(return_value={
        "conflict": True,
        "snapshot": {"agent_ids": ["agent-a"], "revision": 3, "configured": True},
    })
    request = Mock()
    payload = agents_api.MobileAgentFavoritesRequest(
        agent_ids=["agent-a", "agent-a"], expected_revision=2,
    )

    with patch.object(agents_api, "get_db", return_value=db), patch.object(
        agents_api, "assert_caller_is", new=AsyncMock(return_value="alice"),
    ):
        response = run(agents_api.replace_mobile_agent_favorites(payload, request, "alice"))

    assert response.status_code == 409
    assert json.loads(response.body) == {
        "code": "mobile_agent_favorites_revision_conflict",
        "current": {"agent_ids": ["agent-a"], "revision": 3, "configured": True},
    }
    db.replace_user_agent_sidebar_preferences.assert_awaited_once_with(
        "alice", ["agent-a"], 2,
    )
