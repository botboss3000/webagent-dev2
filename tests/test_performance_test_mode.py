import importlib

import pytest

from app import runtime_mode


def test_performance_mode_requires_explicit_isolated_data(monkeypatch):
    monkeypatch.setenv("WEBAGENT_PERF_TEST_MODE", "1")
    monkeypatch.delenv("WEBAGENT_PERF_TEST_DATA_DIR", raising=False)
    importlib.reload(runtime_mode)
    with pytest.raises(RuntimeError, match="requires WEBAGENT_PERF_TEST_DATA_DIR"):
        runtime_mode.data_root()


def test_performance_mode_rejects_live_data_tree(monkeypatch):
    monkeypatch.setenv("WEBAGENT_PERF_TEST_MODE", "1")
    monkeypatch.setenv(
        "WEBAGENT_PERF_TEST_DATA_DIR", str(runtime_mode.DEFAULT_DATA_ROOT / "perf")
    )
    importlib.reload(runtime_mode)
    with pytest.raises(RuntimeError, match="outside the live data"):
        runtime_mode.data_root()


def test_performance_mode_uses_isolated_root_and_disables_backgrounds(
    monkeypatch, tmp_path
):
    isolated = tmp_path / "isolated"
    monkeypatch.setenv("WEBAGENT_PERF_TEST_MODE", "true")
    monkeypatch.setenv("WEBAGENT_PERF_TEST_DATA_DIR", str(isolated))
    importlib.reload(runtime_mode)
    assert runtime_mode.data_root() == isolated.resolve()
    assert runtime_mode.background_services_enabled() is False
    assert isolated.is_dir()
