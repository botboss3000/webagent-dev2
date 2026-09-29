"""V2 element snapshots exercise real component persistence/projection with an in-memory API fixture."""
import json
import asyncio
from functools import wraps


def run_async(function):
    @wraps(function)
    def run():
        return asyncio.run(function())
    return run

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import pytest
from fastapi import HTTPException
from app.chat.api import components
from app.chat_components import save_component, apply_action, delete_component

class Store:
    def __init__(self):
        self.rows=[]
    async def insert_interaction(self, **row):
        self.rows.append(SimpleNamespace(**row,created_at=str(len(self.rows))))
    async def fetch_interactions(self, user_id, session_id):
        return [row for row in self.rows if row.user_id==user_id and row.session_id==session_id]
    async def get_session_component_ui(self, session_id):
        return {"active":"plan-overview","minimized":False}

@run_async
async def test_saved_plan_checkpoint_and_actions_reach_v2_snapshot():
    store=Store()
    scope=AsyncMock(return_value=("alice",None,{}))
    with patch("app.chat.api.scope",scope), patch("app.db.get_db",return_value=store):
        await save_component("alice","s",{"id":"plan-overview","type":"status","data":{"markdown":"Plan"}})
        await save_component("alice","s",{"id":"task-progress","type":"status","data":{"markdown":"Step pending"}})
        await save_component("alice","s",{"id":"todo","type":"todo_list","data":{"items":[{"id":"one","label":"Check"}]}})
        await store.insert_interaction(user_id="alice",session_id="s",role="assistant",
            content="PRIVATE EXECUTION",tool_name=None)
        before=await components("s",None)
        assert {row["id"] for row in before["components"]}=={"plan-overview","task-progress","todo"}
        assert "PRIVATE EXECUTION" not in json.dumps(before)
        assert before["ui"]["active"]=="plan-overview"
        await save_component("alice","s",{"id":"task-progress","type":"status","data":{"markdown":"Step done"}})
        await apply_action("alice","s","todo","toggle_item",{"item_id":"one","done":True})
        await delete_component("alice","s","plan-overview")
        after=await components("s",None)
        assert [row["id"] for row in after["components"]]==["task-progress"]
        assert after["components"][0]["data"]["markdown"]=="Step done"
        assert scope.await_count==4

@run_async
async def test_owner_scope_blocks_reads_before_accessing_resources():
    with patch("app.chat.api.scope",AsyncMock(side_effect=HTTPException(404,"Session not found"))), \
         patch("app.chat_components.list_components",AsyncMock()) as read:
        with pytest.raises(HTTPException) as caught:
            await components("someone-elses-session",None)
        assert caught.value.status_code==404
        read.assert_not_awaited()

@run_async
async def test_revocation_during_read_does_not_return_snapshot():
    scope=AsyncMock(side_effect=[("alice",None,{}),HTTPException(404,"Session not found")])
    with patch("app.chat.api.scope",scope), \
         patch("app.chat_components.list_components",AsyncMock(return_value=[])), \
         patch("app.chat_components.get_panel_ui",AsyncMock(return_value={})):
        with pytest.raises(HTTPException):
            await components("s",None)
