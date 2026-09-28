from types import SimpleNamespace

from app.api.chat import _continue_authorizes_auto


def _assistant(content: str):
    return SimpleNamespace(role="assistant", content=content)


def test_continue_authorizes_pending_auto_handoff():
    assert _continue_authorizes_auto(" Continue! ", [
        _assistant("Please explicitly confirm: switch to Auto or go ahead autonomously."),
    ])


def test_continue_authorizes_write_capable_request():
    assert _continue_authorizes_auto("continue", [
        _assistant("Select and approve a write-capable mode before execution."),
    ])


def test_continue_does_not_authorize_without_pending_mode_request():
    assert not _continue_authorizes_auto("continue", [
        _assistant("I will continue researching the issue."),
    ])


def test_prose_containing_continue_does_not_authorize_auto():
    assert not _continue_authorizes_auto("Please continue with the research", [
        _assistant("Please switch to Auto to make the change."),
    ])
