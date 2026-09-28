from datetime import datetime, timezone

import pytest

from app import model_catalog


def _at_utc(monkeypatch, year, month, day, hour):
    instant = datetime(year, month, day, hour, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(model_catalog.time, "time", lambda: instant)


@pytest.fixture(autouse=True)
def _catalog(monkeypatch):
    models = {
        "deepseek/deepseek-flash": {
            "id": "deepseek/deepseek-flash",
            "cost_input": 99.0,
            "cost_output": 99.0,
            "cost_cache_read": 99.0,
        },
        "deepseek/deepseek-v4-flash-vision-exp": {
            "id": "deepseek/deepseek-v4-flash-vision-exp",
            "cost_input": 88.0,
            "cost_output": 88.0,
            "cost_cache_read": 88.0,
        },
        "deepseek/deepseek-v4-pro": {
            "id": "deepseek/deepseek-v4-pro",
            "cost_input": 77.0,
            "cost_output": 77.0,
            "cost_cache_read": 77.0,
        },
    }
    monkeypatch.setattr(model_catalog, "_catalog", {
        "models": models,
        "index": {key: key for key in models},
    })


@pytest.mark.parametrize("model_id", [
    "deepseek-flash",
    "deepseek-v4-flash",
    "deepseek-v4-flash-vision-exp",
])
def test_native_deepseek_flash_ids_use_current_off_peak_prices(monkeypatch, model_id):
    _at_utc(monkeypatch, 2026, 9, 14, 0)  # Monday, outside peak hours.
    meta = model_catalog.lookup(model_id, "deepseek")
    assert (meta["cost_input"], meta["cost_output"], meta["cost_cache_read"]) == (
        0.15, 0.6, 0.003,
    )


@pytest.mark.parametrize("hour", [1, 3, 6, 9])
def test_native_deepseek_flash_uses_peak_prices_during_published_windows(monkeypatch, hour):
    _at_utc(monkeypatch, 2026, 9, 14, hour)  # Monday.
    meta = model_catalog.lookup("deepseek-flash", "deepseek")
    assert (meta["cost_input"], meta["cost_output"], meta["cost_cache_read"]) == (
        0.3, 1.2, 0.006,
    )


def test_native_deepseek_pro_cost_is_call_time_and_cache_aware(monkeypatch):
    _at_utc(monkeypatch, 2026, 9, 14, 4)  # Monday, gap between peak windows.
    cost, source = model_catalog.cost_for(
        "deepseek-v4-pro", input_tokens=1_000_000, output_tokens=1_000_000,
        provider_hint="deepseek", cached_input_tokens=250_000,
    )
    assert cost == pytest.approx(0.75 * 0.66 + 0.25 * 0.022 + 1.98)
    assert source == "cache_aware_catalog"


def test_deepseek_override_does_not_change_reseller_pricing(monkeypatch):
    _at_utc(monkeypatch, 2026, 9, 14, 1)
    meta = model_catalog.lookup("deepseek/deepseek-v4-pro", "openrouter")
    assert (meta["cost_input"], meta["cost_output"]) == (77.0, 77.0)
