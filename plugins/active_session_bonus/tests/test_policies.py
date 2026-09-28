from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from plugins.active_session_bonus import policies
from plugins.active_session_bonus.policies import PolicyStore, PolicyValidationError, validate_policies
from plugins.active_session_bonus.qualification import parse_time, utcnow
from plugins.active_session_bonus.store import BonusStore


FLAGS = ["needs_input", "error"]


def policy(flag, **overrides):
    value = {
        "flag": flag,
        "max_bonus": 75,
        "resolution_seconds": 3600,
        "resolution_action": "continue",
    }
    value.update(overrides)
    return value


def test_policy_list_must_exactly_match_authoritative_flags():
    assert validate_policies([policy("error"), policy("needs_input")], FLAGS) == [
        policy("needs_input"), policy("error")
    ]
    with pytest.raises(PolicyValidationError, match="exactly match"):
        validate_policies([policy("needs_input")], FLAGS)
    with pytest.raises(PolicyValidationError, match="missing or duplicate"):
        validate_policies([policy("needs_input"), policy("needs_input")], FLAGS)
    with pytest.raises(PolicyValidationError, match="exactly match"):
        validate_policies([policy("needs_input"), policy("unknown")], FLAGS)


def test_store_reports_mismatch_when_gate_taxonomy_changes(monkeypatch):
    with TemporaryDirectory() as tmp:
        monkeypatch.setattr(policies, "authoritative_flags", lambda: FLAGS)
        store = PolicyStore(Path(tmp) / "policies.json")
        saved = store.save([policy("needs_input"), policy("error", resolution_action="complete")])
        assert saved["matches"] is True
        monkeypatch.setattr(policies, "authoritative_flags", lambda: ["needs_input", "complete"])
        state = store.current()
        assert state["matches"] is False
        assert [item["flag"] for item in state["policies"]] == ["needs_input", "complete"]


def test_new_offer_freezes_policy_amount_and_resolution_deadline():
    with TemporaryDirectory() as tmp:
        store = BonusStore(Path(tmp) / "bonus.db")
        now = utcnow()
        selected = policy("error", max_bonus=75, resolution_seconds=3600, resolution_action="complete")
        offer = store.create_offer(
            user_id="u", session_id="s", closer_id="closer", now=now,
            snapshot={"task_id": "t", "task_revision": 1, "work_revision": 1},
            policy=selected,
        )
        # Policy retains the configured cap; issuance rounds to an amount the
        # cents-based wallet can credit exactly and the footer can display exactly.
        assert offer["initial_amount"] == 70
        assert offer["policy"] == selected
        assert parse_time(offer["expires_at"]) == now + timedelta(seconds=3600)
