from pathlib import Path

import pytest

from app.agent.loop import _house_credit_charge
from app.entitlements.rosters import RosterError, validate_roster_entries
from plugins.billing.charge import locked_house_credits


def test_explicit_house_credit_schedule_is_token_based():
    credits, source = _house_credit_charge(
        {"credit_schedule": "pro", "credit_input_per_million": 100,
         "credit_output_per_million": 500},
        1_000_000, 2_000_000, 99.0,
    )
    assert credits == 1100
    assert source == "pro"


def test_unconfigured_schedule_uses_last_resort_only_without_deepseek():
    assert _house_credit_charge({}, 10, 20, 1.25) == (1250, "usd_last_resort")


def test_unconfigured_standard_model_uses_deepseek_flash_reference(monkeypatch):
    from app import model_catalog
    monkeypatch.setattr(model_catalog, "cost_for", lambda *args, **kwargs: (0.25, "catalog"))
    config = {"multi_providers": [{"model": "deepseek-v4-flash", "provider": "openrouter"}]}
    assert _house_credit_charge(config, 100, 50, 9.0) == (250, "deepseek_flash")


def test_active_roster_entry_supplies_explicit_rates_and_premium_fallback(monkeypatch):
    explicit = {"model": "replacement", "provider": "x", "multi_providers": [
        {"model": "replacement", "provider": "x", "credit_schedule": "pro",
         "credit_input_per_million": 100, "credit_output_per_million": 200},
    ]}
    assert _house_credit_charge(explicit, 1_000_000, 1_000_000, 99) == (300, "pro")
    from app import model_catalog
    monkeypatch.setattr(model_catalog, "cost_for", lambda model, *args, **kwargs: (0.5 if "pro" in model else 0.1, "catalog"))
    inferred = {"model": "replacement", "provider": "x", "multi_providers": [
        {"model": "replacement", "provider": "x", "high_effort_capable": True},
        {"model": "deepseek-v4-flash", "provider": "x"},
        {"model": "deepseek-v4-pro", "provider": "x"},
    ]}
    assert _house_credit_charge(inferred, 1, 1, 99) == (500, "deepseek_pro")


def test_byo_calls_never_consume_house_credits():
    assert locked_house_credits(False, 12.5) == 12.5
    assert locked_house_credits(True, 12.5) == 0
    assert locked_house_credits(False, -2) == 0


def test_roster_credit_schedule_validation():
    row = {"entry_id": "m1", "provider": "openai", "model": "x",
           "credit_schedule": "flash", "credit_input_per_million": 10.5,
           "credit_output_per_million": 20}
    assert validate_roster_entries([row])[0]["credit_input_per_million"] == 10.5
    with pytest.raises(RosterError, match="credit_schedule"):
        validate_roster_entries([{**row, "credit_schedule": "dollars"}])
    with pytest.raises(RosterError, match="non-negative"):
        validate_roster_entries([{**row, "credit_input_per_million": -1}])


def test_footer_and_hosted_migration_use_house_credits():
    footer = Path("ui/shared/js/chat-activity.js").read_text(encoding="utf-8")
    migration = Path("migrations/037_house_credits.sql").read_text(encoding="utf-8")
    assert "event.credit_units" in footer
    assert "total_credit_units" in footer
    assert "◇ " in footer
    assert "credit_units" in migration and "DEFAULT 0" in migration
