import pytest
from fastapi import HTTPException

from app.api.agents import _normalize_codex_code_update


def test_codex_config_absent_preserves_legacy_defaults():
    assert _normalize_codex_code_update(None) is None
    assert _normalize_codex_code_update({"model": "gpt-5"}) == {"model": "gpt-5"}


@pytest.mark.parametrize("mode", ["native_codex", "webagent_wrapper", "codex_portal"])
def test_codex_context_mode_accepts_closed_enum(mode):
    assert _normalize_codex_code_update({"context_mode": mode}) == {"context_mode": mode}


@pytest.mark.parametrize("enabled", [True, False])
def test_codex_closer_enabled_accepts_json_boolean(enabled):
    assert _normalize_codex_code_update({"closer_enabled": enabled}) == {"closer_enabled": enabled}


@pytest.mark.parametrize("mode", ["", "native", "wrapper", 1, None])
def test_codex_context_mode_rejects_invalid_values(mode):
    with pytest.raises(HTTPException) as exc:
        _normalize_codex_code_update({"context_mode": mode})
    assert exc.value.status_code == 400


@pytest.mark.parametrize("enabled", [0, 1, "true", "false", None])
def test_codex_closer_enabled_rejects_non_boolean_values(enabled):
    with pytest.raises(HTTPException) as exc:
        _normalize_codex_code_update({"closer_enabled": enabled})
    assert exc.value.status_code == 400
