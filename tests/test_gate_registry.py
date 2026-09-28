"""Gate registry tests — Markdown discovery, catalog-only activation, and coercion.

Load-bearing properties:

* **Fail closed** — a missing or malformed ``metadata['gates']`` block leaves
  every gate OFF, even when legacy contract fields are populated.
* **Validation on write** — an unknown gate type is rejected rather than stored
  as a gate that silently never fires.
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.agent import gate_registry
from app.agent.contracts import parse_contract
from app.api.agents import UpdateAgentRequest, update_agent


@pytest.fixture(autouse=True)
def _fresh_registry():
    """Re-scan plugins/gates/ for every test so a drop-in is picked up."""
    gate_registry.reload_gate_types()
    yield
    gate_registry.reload_gate_types()


def _agent(metadata=None, **extra):
    rec = {"id": "agent-1", "name": "Test", "metadata": metadata or {}}
    rec.update(extra)
    return rec


def _update_contract(contract_in, existing_contract):
    """Drive a normalized contract write against historical metadata."""
    db = SimpleNamespace(
        is_user_admin=AsyncMock(return_value=True),
        get_agent_by_id=AsyncMock(return_value={
            "id": "agent-1",
            "source": "custom",
            "metadata": {"contract": existing_contract},
        }),
        update_agent_fields=AsyncMock(return_value={
            "id": "agent-1", "source": "custom", "metadata": "{}",
        }),
    )
    req = UpdateAgentRequest(user_id="admin", contract=contract_in)
    request = Request({"type": "http", "headers": []})
    with (
        patch("app.api.agents.get_db", return_value=db),
        patch("app.api.agents.assert_caller_is", new=AsyncMock(return_value="admin")),
        patch("app.api.agents._is_agent_admin", new=AsyncMock(return_value=True)),
    ):
        asyncio.run(update_agent("agent-1", req, request))
    return db.update_agent_fields.await_args.kwargs["updates"]["metadata"]["contract"]


def _update(gates_in):
    """Drive a real update_agent call carrying only a gates payload."""
    db = SimpleNamespace(
        is_user_admin=AsyncMock(return_value=True),
        get_agent_by_id=AsyncMock(return_value={
            "id": "agent-1", "source": "custom", "metadata": {},
        }),
        update_agent_fields=AsyncMock(return_value={
            "id": "agent-1", "source": "custom", "metadata": "{}",
        }),
    )
    req = UpdateAgentRequest(user_id="admin", gates=gates_in)
    request = Request({"type": "http", "headers": []})
    with (
        patch("app.api.agents.get_db", return_value=db),
        patch("app.api.agents.assert_caller_is", new=AsyncMock(return_value="admin")),
        patch("app.api.agents._is_agent_admin", new=AsyncMock(return_value=True)),
    ):
        asyncio.run(update_agent("agent-1", req, request))
    return db.update_agent_fields.await_args.kwargs["updates"]["metadata"]["gates"]


def _update_with_prior(gates_in, prior_gates):
    """Drive update_agent against an agent that ALREADY stores gate rows.

    The GATES-MERGE-ON-WRITE contract is only observable against a stored block —
    that is where the lossy partial saves used to bite (a param-only save
    disarming the gate, a one-gate payload dropping every other row).
    """
    db = SimpleNamespace(
        is_user_admin=AsyncMock(return_value=True),
        get_agent_by_id=AsyncMock(return_value={
            "id": "agent-1", "source": "custom",
            "metadata": {"gates": {"schema_version": 1, "gates": prior_gates}},
        }),
        update_agent_fields=AsyncMock(return_value={
            "id": "agent-1", "source": "custom", "metadata": "{}",
        }),
    )
    req = UpdateAgentRequest(user_id="admin", gates=gates_in)
    request = Request({"type": "http", "headers": []})
    with (
        patch("app.api.agents.get_db", return_value=db),
        patch("app.api.agents.assert_caller_is", new=AsyncMock(return_value="admin")),
        patch("app.api.agents._is_agent_admin", new=AsyncMock(return_value=True)),
    ):
        asyncio.run(update_agent("agent-1", req, request))
    return db.update_agent_fields.await_args.kwargs["updates"]["metadata"]["gates"]


# ── Discovery ────────────────────────────────────────────────────────────────

def test_discovers_the_shipped_gate_types():
    types = gate_registry.gate_types()
    for expected in (
        "pre_tool_progress_message", "verify_after_edit", "error_strike", "tool_call_cap",
        "browser_journey", "cache_bump", "named_reference", "file_checkout",
    ):
        assert expected in types, f"{expected} was not discovered"


def test_every_descriptor_is_valid_and_ids_match_filenames():
    for gate_id, descriptor in gate_registry.gate_types().items():
        assert descriptor["id"] == gate_id
        assert descriptor["phase"] in ("pre_tool", "completion", "prompt")
        assert descriptor["view"]["key"].startswith(("require_", "max_"))
        assert isinstance(descriptor["order"], int)
        assert descriptor["source"] == "markdown"


def test_menu_is_json_safe_and_ordered():
    menu = gate_registry.gate_type_menu()
    assert menu
    json.dumps(menu)  # must survive an API response
    orders = [row["order"] for row in menu]
    assert orders == sorted(orders)


def test_every_gate_type_drives_a_key_the_contract_parser_knows():
    """A type projecting onto an unknown key would be a gate that silently does
    nothing — catch that at test time, not in a run."""
    legacy = parse_contract(_agent())
    for descriptor in gate_registry.gate_types().values():
        assert descriptor["view"]["key"] in legacy, (
            f"{descriptor['id']} projects onto {descriptor['view']['key']!r}, "
            "which parse_contract does not emit"
        )


# ── Back-compat (the load-bearing path) ──────────────────────────────────────

def test_agent_without_a_gates_block_is_all_off_despite_legacy_keys():
    rec = _agent({"contract": {"require_browser_journey_for_ui_change": True,
                               "max_tool_calls_per_turn": 25}})
    view = gate_registry.gate_view_for_agent(rec)
    assert all(value is False or value == 0 for value in view.values())
    contract = parse_contract(rec)
    assert contract["require_browser_journey_for_ui_change"] is False
    assert contract["max_tool_calls_per_turn"] == 0


def test_retired_contract_metadata_is_ignored_and_contract_updates_are_not_persisted():
    historical = {"require_tool_use": True, "future_rule": "preserve-me"}
    parsed = parse_contract(_agent({"contract": historical}))
    assert "require_tool_use" not in parsed

    # The API no longer owns a baseline contract. Legacy input is ignored rather
    # than recreating a parallel behavior source.
    db = SimpleNamespace(
        is_user_admin=AsyncMock(return_value=True),
        get_agent_by_id=AsyncMock(return_value={
            "id": "agent-1", "source": "custom", "metadata": {"contract": historical},
        }),
        update_agent_fields=AsyncMock(return_value={"id": "agent-1", "source": "custom", "metadata": "{}"}),
    )
    req = UpdateAgentRequest(user_id="admin", contract={"require_verify_after_edit": True})
    request = Request({"type": "http", "headers": []})
    with (
        patch("app.api.agents.get_db", return_value=db),
        patch("app.api.agents.assert_caller_is", new=AsyncMock(return_value="admin")),
        patch("app.api.agents._is_agent_admin", new=AsyncMock(return_value=True)),
    ):
        asyncio.run(update_agent("agent-1", req, request))
    assert "metadata" not in db.update_agent_fields.await_args.kwargs["updates"]


def test_malformed_gates_block_is_all_off():
    rec = _agent({"contract": {"require_verify_after_edit": True},
                  "gates": {"gates": "not-a-list"}})
    assert gate_registry.gate_view_for_agent(rec)["require_verify_after_edit"] is False
    assert parse_contract(rec)["require_verify_after_edit"] is False


# ── Explicit gates are authoritative ─────────────────────────────────────────

def test_explicit_enabled_catalog_row_overrides_legacy_keys():
    rec = _agent({
        "contract": {"require_browser_journey_for_ui_change": True},
        "gates": {"schema_version": 1, "gates": [{"type": "verify_after_edit", "enabled": True}]},
    })
    contract = parse_contract(rec)
    assert contract["require_verify_after_edit"] is True
    assert contract["require_browser_journey_for_ui_change"] is False


def test_omitted_gate_is_off():
    rec = _agent({"contract": {}, "gates": {"gates": [{"type": "pre_tool_progress_message", "enabled": True}]}})
    contract = parse_contract(rec)
    assert contract["require_pre_tool_progress_message"] is True


def test_disabled_instance_is_inert():
    rec = _agent({"gates": {"gates": [{"type": "pre_tool_progress_message", "enabled": False}]}})
    assert parse_contract(rec)["require_pre_tool_progress_message"] is False


def test_unknown_gate_type_is_ignored_not_fatal():
    rec = _agent({"gates": {"gates": [
        {"type": "no_such_gate"}, {"type": "pre_tool_progress_message", "enabled": True},
    ]}})
    assert parse_contract(rec)["require_pre_tool_progress_message"] is True


# ── Integer params ───────────────────────────────────────────────────────────

def test_int_param_drives_its_contract_key():
    rec = _agent({"gates": {"gates": [
        {"type": "tool_call_cap", "enabled": True, "params": {"limit": 12}},
        {"type": "error_strike", "enabled": True, "params": {"threshold": 3}},
    ]}})
    contract = parse_contract(rec)
    assert contract["max_tool_calls_per_turn"] == 12
    assert contract["max_tool_errors"] == 3


@pytest.mark.parametrize("bad", ["abc", None, -5, 2.7])
def test_int_param_is_coerced_safely(bad):
    rec = _agent({"gates": {"gates": [
        {"type": "tool_call_cap", "enabled": True, "params": {"limit": bad}},
    ]}})
    value = parse_contract(rec)["max_tool_calls_per_turn"]
    assert value >= 0 and isinstance(value, int)


# ── Scope ────────────────────────────────────────────────────────────────────

def test_scope_filters_by_mode():
    rec = _agent({"gates": {"gates": [{"type": "pre_tool_progress_message", "enabled": True, "scope": ["auto"]}]}})
    assert gate_registry.gate_view_for_agent(rec, "auto")["require_pre_tool_progress_message"] is True
    assert gate_registry.gate_view_for_agent(rec, "plan")["require_pre_tool_progress_message"] is False


def test_file_checkout_gate_is_mode_scoped_by_contract_parser():
    rec = _agent({"gates": {"gates": [
        {"type": "file_checkout", "enabled": True, "scope": ["auto"]},
    ]}})
    assert parse_contract(rec, mode="auto")["require_file_checkout"] is True
    assert parse_contract(rec, mode="plan")["require_file_checkout"] is False


def test_scope_wildcard_and_empty_mean_every_mode():
    for scope in (["*"], [], None):
        rec = _agent({"gates": {"gates": [
            {"type": "verify_after_edit", "enabled": True, "scope": scope},
        ]}})
        for mode in ("ask", "plan", "auto"):
            assert gate_registry.gate_view_for_agent(rec, mode)["require_verify_after_edit"] is True


def test_unknown_mode_does_not_apply_a_scoped_gate():
    rec = _agent({"gates": {"gates": [{"type": "verify_after_edit", "enabled": True, "scope": ["auto"]}]}})
    assert gate_registry.gate_view_for_agent(rec, "research")["require_verify_after_edit"] is False


# ── Migration view ───────────────────────────────────────────────────────────

def test_instances_include_every_catalog_type_and_default_to_off():
    rec = _agent({"contract": {"require_browser_journey_for_ui_change": True, "max_tool_errors": 4}})
    rows = {inst["type"]: inst for inst in gate_registry.instances_for_agent(rec)}
    assert set(rows) == set(gate_registry.gate_types())
    assert all(row["enabled"] is False for row in rows.values())


def test_explicit_catalog_rows_are_returned_alongside_disabled_types():
    rec = _agent({"gates": {"gates": [{"type": "verify_after_edit", "enabled": True}]}})
    rows = {row["type"]: row for row in gate_registry.instances_for_agent(rec)}
    assert rows["verify_after_edit"]["enabled"] is True
    assert rows["pre_tool_progress_message"]["enabled"] is False


def test_metadata_as_json_string_is_parsed():
    rec = {"id": "agent-1", "metadata": json.dumps({
        "gates": {"gates": [{"type": "pre_tool_progress_message", "enabled": True}]},
    })}
    assert parse_contract(rec)["require_pre_tool_progress_message"] is True


# ── API write path ───────────────────────────────────────────────────────────

def test_gates_update_round_trips_and_is_normalised():
    saved = _update({"schema_version": 1, "gates": [
        {"type": "tool_call_cap", "enabled": True, "params": {"limit": 7}, "scope": ["auto"],
         "message": "  Wrap it up.  "},
        {"type": "pre_tool_progress_message", "enabled": False},
        {"type": "browser_journey", "enabled": True},
    ]})
    assert saved["schema_version"] == 1
    rows = {row["type"]: row for row in saved["gates"]}
    assert rows["tool_call_cap"]["params"] == {"limit": 7}
    assert rows["tool_call_cap"]["scope"] == ["auto"]
    assert rows["tool_call_cap"]["message"] == "Wrap it up."  # stripped
    assert rows["pre_tool_progress_message"]["enabled"] is False
    # An unscoped, defaulted row keeps only its type.
    assert rows["browser_journey"] == {"type": "browser_journey", "enabled": True}


def test_gates_accepted_as_a_bare_list():
    saved = _update([{"type": "verify_after_edit"}])
    assert saved["gates"] == [{"type": "verify_after_edit", "enabled": False}]


# ── Partial saves (GATES-MERGE-ON-WRITE) ─────────────────────────────────────

def test_param_only_save_keeps_the_stored_activation():
    """Regression: editing a gate's params used to silently disarm it.

    A client that sends only the row it is changing leaves `enabled` out; that
    must mean "leave the stored activation alone", never "default to OFF".
    """
    saved = _update_with_prior(
        [{"type": "assertions", "params": {"format": "One line, main bubble."}}],
        [{"type": "assertions", "enabled": True, "params": {"format": "old"}}],
    )
    row = saved["gates"][0]
    assert row["enabled"] is True
    assert row["params"] == {"format": "One line, main bubble."}


def test_an_unmentioned_gate_type_keeps_its_row_and_activation():
    """A one-gate payload must not drop every other stored gate row."""
    saved = _update_with_prior(
        [{"type": "assertions", "enabled": True}],
        [{"type": "pre_tool_progress_message", "enabled": True, "scope": ["auto"]},
         {"type": "assertions", "enabled": False}],
    )
    rows = {row["type"]: row for row in saved["gates"]}
    assert rows["pre_tool_progress_message"] == {
        "type": "pre_tool_progress_message", "enabled": True, "scope": ["auto"],
    }
    assert rows["assertions"]["enabled"] is True


def test_explicit_false_still_disarms_and_omission_never_invents_activation():
    saved = _update_with_prior(
        [{"type": "assertions", "enabled": False},
         {"type": "tool_call_cap", "params": {"limit": 5}}],
        [{"type": "assertions", "enabled": True}],
    )
    rows = {row["type"]: row for row in saved["gates"]}
    # An explicit `enabled: false` is still how a client disarms a gate.
    assert rows["assertions"]["enabled"] is False
    # A type with no stored row and no explicit flag stays off — omission
    # preserves activation, it never invents it.
    assert rows["tool_call_cap"]["enabled"] is False


def test_legacy_closing_master_off_projects_to_an_inert_always_on_coordinator():
    rec = _agent({"gates": {"schema_version": 1, "gates": [
        {"type": "closing_message", "enabled": False, "params": {"max_tokens": 10}},
    ]}})
    row = next(item for item in gate_registry.instances_for_agent(rec) if item["type"] == "closing_message")
    assert row["enabled"] is True
    assert row["params"]["require_flag"] is False
    assert row["params"]["require_notification"] is False
    assert row["params"]["output_closer_enabled"] is False
    # A subsequent save persists the same canonical shape instead of restoring a
    # user-facing master switch.
    saved = _update({"gates": [{"type": "closing_message", "enabled": False}]})
    persisted = next(item for item in saved["gates"] if item["type"] == "closing_message")
    assert persisted == {"type": "closing_message", "enabled": True, "params": {
        "require_flag": False, "require_notification": False, "output_closer_enabled": False,
    }}


def test_app_level_params_are_stripped_from_a_written_agent_row():
    """A ``level: "app"`` param can never be persisted on an agent row.

    The closing_message flag taxonomy is configured once for the whole app (App
    Settings → Gates), so this write-path backstop drops it even when a legacy
    row or an older client still sends it. The Modes UI strips the same key
    client-side (ui/main-panel/agents/configs/content.js), and both copies exist
    so one app-wide vocabulary stays authoritative.
    """
    saved = _update({"gates": [{
        "type": "closing_message", "enabled": True,
        "params": {"flags": "needs_input, complete", "notification": "Name the next step."},
    }]})
    assert saved["gates"][0]["params"] == {"notification": "Name the next step."}

    # A row carrying ONLY app-level params stores no `params` key at all, rather
    # than an empty dict the Modes page would have to special-case.
    only_app = _update({"gates": [{
        "type": "closing_message", "enabled": True, "params": {"flags": "complete"},
    }]})["gates"][0]
    assert "params" not in only_app


def test_unknown_gate_type_is_rejected_on_write():
    with pytest.raises(HTTPException) as exc:
        _update({"gates": [{"type": "not_a_real_gate"}]})
    assert exc.value.status_code == 400
    assert "unknown gate type" in str(exc.value.detail)


def test_non_object_gate_row_is_rejected():
    with pytest.raises(HTTPException) as exc:
        _update({"gates": ["just-a-string"]})
    assert exc.value.status_code == 400


def test_gates_payload_must_be_a_list():
    with pytest.raises(HTTPException) as exc:
        _update({"gates": "nope"})
    assert exc.value.status_code == 400


def test_a_saved_gates_block_then_resolves():
    """End to end: what the API stores is what parse_contract then resolves."""
    saved = _update({"gates": [
        {"type": "tool_call_cap", "enabled": True, "params": {"limit": 9}},
        {"type": "verify_after_edit", "enabled": True},
    ]})
    contract = parse_contract({"metadata": {"gates": saved}})
    assert contract["max_tool_calls_per_turn"] == 9
    assert contract["require_verify_after_edit"] is True
    # Gates are authoritative: nothing the payload left out is inherited.
    assert contract["require_pre_tool_progress_message"] is False


def test_gate_write_rejects_scope_for_a_missing_mode():
    from fastapi import HTTPException

    db = SimpleNamespace(
        is_user_admin=AsyncMock(return_value=True),
        get_agent_by_id=AsyncMock(return_value={
            "id": "agent-1", "source": "custom",
            "metadata": {"execution_modes": [{"id": "research", "permission_policy": "read_only"}]},
        }),
        update_agent_fields=AsyncMock(),
    )
    req = UpdateAgentRequest(user_id="admin", gates={"gates": [{
        "type": "file_checkout", "enabled": True, "scope": ["missing-mode"],
    }]})
    request = Request({"type": "http", "headers": []})
    with (
        patch("app.api.agents.get_db", return_value=db),
        patch("app.api.agents.assert_caller_is", new=AsyncMock(return_value="admin")),
        patch("app.api.agents._is_agent_admin", new=AsyncMock(return_value=True)),
    ):
        try:
            asyncio.run(update_agent("agent-1", req, request))
        except HTTPException as exc:
            assert exc.status_code == 400
            assert exc.detail == "gate scope must reference configured modes only."
        else:
            raise AssertionError("invalid gate scope was accepted")
