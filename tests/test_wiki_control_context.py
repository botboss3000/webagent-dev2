import asyncio

from app import abilities
from plugins.abilities.Memory.wiki_context import wiki_context


def test_wiki_control_descriptor_advertises_automatic_recall():
    abilities.reload()
    item = abilities.ui_catalog()["abilities"]["wiki_context"]

    assert item["display_name"] == "Wiki Control"
    assert "Automatically recalls" in item["description"]


def test_wiki_prompt_context_is_bounded_and_points_to_full_article(monkeypatch):
    calls = []

    async def search(query, limit, include_drafts):
        calls.append((query, limit, include_drafts))
        return [{"slug": "deploy", "title": "Deploy Guide", "snippet": "Use the release checklist."}]

    monkeypatch.setattr("app.wiki.store.search_articles", search)
    monkeypatch.setattr("app.wiki.store.is_public_actor", lambda _user_id: False)

    context = asyncio.run(wiki_context.build_prompt_context(
        user_id="member-1", query="how do I deploy?", limit=99,
    ))

    assert calls == [("how do I deploy?", 5, True)]
    assert "Relevant company Wiki excerpts" in context
    assert "Deploy Guide (`deploy`)" in context
    assert "wiki_get" in context


def test_prompt_context_runs_only_enabled_ability_hooks(monkeypatch):
    class EnabledModule:
        @staticmethod
        async def build_prompt_context(**_kwargs):
            return "enabled context"

    async def enabled(_agent_id, _user_id):
        return {"wiki_context", "no_hook"}

    monkeypatch.setattr("app.integrations.gather_enabled_providers", enabled)
    monkeypatch.setattr(
        abilities,
        "ability_module",
        lambda ability_id: EnabledModule if ability_id == "wiki_context" else object(),
    )

    result = asyncio.run(abilities.prompt_context_for_agent(
        "agent-1", "user-1", "question",
    ))
    assert result == "enabled context"
