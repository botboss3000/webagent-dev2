import asyncio
import json

import pytest

from app.automation import entitlements as automation_entitlements
from app.automation import sync as automation_sync
from app.automation.parser import ParseResult, ParsedTask
from plugins.abilities.Core.automation import automation as automation_ability


def run(coro):
    return asyncio.run(coro)


class FakeDB:
    def __init__(self, *, tasks=None, subscriptions=None):
        self.tasks = list(tasks or [])
        self.subscriptions = list(subscriptions or [])
        self.created = []
        self.upserted = []

    async def list_automations(self, **_kwargs):
        return list(self.tasks)

    async def list_event_subscriptions(self, **_kwargs):
        return list(self.subscriptions)

    async def create_automation(self, **kwargs):
        self.created.append(kwargs)
        return {"id": "new-automation", **kwargs}

    async def upsert_automation(self, **kwargs):
        self.upserted.append(("task", kwargs))
        return {"id": "slot-task", "origin": "slot", **kwargs}

    async def upsert_event_subscription(self, **kwargs):
        self.upserted.append(("event", kwargs))
        return {"id": "slot-event", "origin": "slot", **kwargs}

    async def delete_automations_not_in(self, *_args):
        return 0

    async def delete_event_subscriptions_not_in(self, *_args):
        return []


def _capabilities(*, enabled=True, limit=10):
    return {
        "features": {"automations": enabled},
        "limits": {"max_automations": limit},
    }


def test_combined_task_and_event_count_enforces_tier_limit(monkeypatch):
    db = FakeDB(
        tasks=[{"id": "task-1"}],
        subscriptions=[{"id": "event-1"}],
    )

    async def resolve(*_args, **_kwargs):
        return _capabilities(limit=2)

    monkeypatch.setattr("app.entitlements.service.resolve_capabilities", resolve)

    with pytest.raises(automation_entitlements.AutomationEntitlementError) as exc:
        run(automation_entitlements.enforce_automation_entitlement(
            db, "user-1", additional=1,
        ))

    assert exc.value.code == "max_automations_reached"


def test_slot_sync_denial_happens_before_any_database_mutation(monkeypatch):
    db = FakeDB()
    parsed = ParseResult(tasks=[ParsedTask(
        task_label="Daily",
        prompt="summarize",
        schedule_cron="0 9 * * *",
        schedule_natural="daily",
        timezone="UTC",
        channel=None,
        channel_recipient=None,
        silent=False,
    )])

    async def parse(*_args, **_kwargs):
        return parsed

    async def resolve(*_args, **_kwargs):
        return _capabilities(enabled=False, limit=0)

    monkeypatch.setattr(automation_sync, "parse_automation_file", parse)
    monkeypatch.setattr("app.entitlements.service.resolve_capabilities", resolve)

    with pytest.raises(automation_entitlements.AutomationEntitlementError) as exc:
        run(automation_sync.sync_automations(
            db, "agent-1", "free-user", "every day",
        ))

    assert exc.value.code == "automations_not_allowed"
    assert db.upserted == []


def test_slot_replacement_is_allowed_when_user_is_at_cap(monkeypatch):
    db = FakeDB(tasks=[{
        "id": "old-slot-task",
        "agent_id": "agent-1",
        "owner_user_id": "pro-user",
        "origin": "slot",
    }])

    async def resolve(*_args, **_kwargs):
        return _capabilities(limit=1)

    monkeypatch.setattr("app.entitlements.service.resolve_capabilities", resolve)

    run(automation_entitlements.enforce_slot_automation_projection(
        db,
        "pro-user",
        "agent-1",
        task_hashes={"replacement"},
        event_hashes=set(),
    ))


def test_direct_core_reminder_path_cannot_bypass_entitlements(monkeypatch):
    """The factory's reminder closure bypasses build_tools wrappers by design."""
    db = FakeDB()

    async def resolve(*_args, **_kwargs):
        return _capabilities(enabled=False, limit=0)

    monkeypatch.setattr("app.entitlements.service.resolve_capabilities", resolve)
    monkeypatch.setattr("app.db.get_db", lambda: db)
    handlers = automation_ability.build_automation_tools(
        user_id="free-user",
        agent_id="agent-1",
        session_id="session-1",
    )

    result = json.loads(run(handlers["remind_me"]("stretch", in_minutes=5)))

    assert result["status"] == "error"
    assert "not available" in result["message"]
    assert db.created == []
