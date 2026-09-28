"""The Output Closer bubble is labelled by the model's user-facing name.

The closer runs on the agent's Standard roster row. The chat must present the
caller's own display name for that model — falling back to the catalog model name
and only then the raw id — exactly as it labels the agent's own replies. Prior to
this, the closer persisted only the raw model id, so its bubble read as the true
model name.

Presentation only: the closer still requests the raw model id. This test drives
`_closer_display_name`, which supplies the label written into the persisted row's
metadata; it never changes which model runs.
"""

import asyncio

import app.admin.settings as settings
from app.agent import output_closer

RAW = "deepseek/deepseek-v4-flash-vision-exp"


def _with_resolver(fake):
    """Run `_closer_display_name` with `resolve_agent_role_config` patched."""
    original = settings.resolve_agent_role_config
    settings.resolve_agent_role_config = fake
    try:
        return asyncio.run(output_closer._closer_display_name("user-1", {"id": "agent-1"}, RAW))
    finally:
        settings.resolve_agent_role_config = original


def test_closer_reports_the_models_display_name_not_the_raw_id():
    calls = []

    async def fake(user_id, agent_rec, role):
        calls.append(role)
        return {"model": RAW, "provider": "deepseek", "display_name": "V4-Flash+Vis"}

    assert _with_resolver(fake) == "V4-Flash+Vis"
    # It resolves the same Standard role the closer actually runs on, so the label
    # and the model that ran can never disagree.
    assert calls == ["standard"]


def test_closer_name_falls_back_to_empty_when_no_display_name_exists():
    """A roster row with no alias stores the real model id in display_name — that
    is not a display name, so the caller records only the raw id as before."""

    async def no_alias(user_id, agent_rec, role):
        return {"model": RAW, "display_name": RAW}

    assert _with_resolver(no_alias) == ""


def test_closer_name_lookup_never_breaks_the_turn():
    async def boom(user_id, agent_rec, role):
        raise RuntimeError("no roster configured")

    assert _with_resolver(boom) == ""
    # No model resolved at all → nothing to look up.
    original = settings.resolve_agent_role_config
    settings.resolve_agent_role_config = boom
    try:
        assert asyncio.run(
            output_closer._closer_display_name("user-1", {"id": "agent-1"}, "")
        ) == ""
    finally:
        settings.resolve_agent_role_config = original


def test_closer_persists_the_display_name_alongside_the_raw_model():
    """The persisted closer row records both names: the raw id stays the internal
    key, the display name is what the bubble reads."""
    import inspect

    source = inspect.getsource(output_closer)
    assert '"display_name": closer_display_name,' in source
    assert 'closer_display_name = await _closer_display_name(user_id, agent_rec, model_str)' in source
