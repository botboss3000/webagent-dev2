"""Closing-message gate tests — envelope parsing, taxonomy downgrade, and the
completion-path instruction that must never leak into the standing prompt.

Load-bearing properties:

* **Completion-only injection** — the instruction reaches the run exactly once,
  at the completion path, and never through ``active_rules()`` /
  ``contract_prompt_block()``.
* **The flag is advisory** — unknown or missing flags and ``complete`` with
  outstanding completion gaps all downgrade to a safe member of the taxonomy.
* **Empty configuration still works** — blank guidance layers are skipped, so the
  floor instruction alone produces a valid, parseable closing turn.
"""

import asyncio
import json
from pathlib import Path

import pytest

from app.agent import closing_message, gate_registry
from app.agent.contracts import active_rules, contract_prompt_block, parse_contract


FLOOR_MARKER = "Give your final response to the user."

#: The machine-readable envelope. It no longer lives on the bare floor — it
#: travels with the flag vocabulary, which is always present.
ENVELOPE_MARKER = "FLAG: <name>"

DEFAULT_TAXONOMY = ["needs_input", "complete", "error", "partial", "unverified"]


@pytest.fixture(autouse=True)
def _fresh_registry():
    """Re-scan plugins/gates/ for every test so a drop-in is picked up."""
    gate_registry.reload_gate_types()
    yield
    gate_registry.reload_gate_types()


def _agent(gate_rows=None, **extra):
    metadata = {}
    if gate_rows is not None:
        metadata["gates"] = {"schema_version": 1, "gates": gate_rows}
    rec = {"id": "agent-1", "name": "Test", "metadata": metadata}
    rec.update(extra)
    return rec


def _enabled(**instance):
    row = {"type": "closing_message", "enabled": True}
    row.update(instance)
    return _agent([row])


def _policy(rec, mode=None):
    policy = closing_message.resolve(rec, mode)
    assert policy is not None, "gate should be active for this agent/mode"
    return policy


# ── Descriptor ───────────────────────────────────────────────────────────────

def test_descriptor_is_a_completion_inject_bool_gate_with_typed_params():
    types = gate_registry.gate_types()
    assert "closing_message" in types
    descriptor = types["closing_message"]
    assert descriptor["phase"] == "completion"
    assert descriptor["inject"] == "completion"
    assert descriptor["view"] == {
        "key": "require_closing_message", "kind": "bool",
        "param": "value", "default": False,
    }
    assert set(descriptor["params"]) == {
        "flags", "cta_max_words", "require_flag", "require_notification",
        "output_closer_enabled", "notification", "format", "max_tokens",
        "format_enforcement_min_tokens", "rejection_message",
    }
    for key in ("require_flag", "require_notification", "output_closer_enabled"):
        spec = descriptor["params"][key]
        assert spec["type"] == "boolean"
        assert spec["default"] is True
    # The prose layers are STRING params. A string param must not carry
    # number-input bounds: the Modes UI routes on ``type`` and would otherwise
    # coerce free text to 0.
    for key in ("flags", "notification", "format", "rejection_message"):
        spec = descriptor["params"][key]
        assert spec["type"] == "string"
        assert spec["multiline"] is True
        assert "min" not in spec and "max" not in spec
    # The notification word limit is the one NUMERIC param, and it is app-level
    # like the taxonomy — the end-of-run validator reads exactly this config.
    limit = descriptor["params"]["cta_max_words"]
    assert limit["type"] == "number"
    assert limit["level"] == "app"
    assert limit["default"] == 5
    assert limit["min"] == 1
    output_limit = descriptor["params"]["max_tokens"]
    assert output_limit["type"] == "number"
    assert output_limit["default"] == 0
    assert output_limit["min"] == 0
    assert output_limit["max"] == 100000
    format_threshold = descriptor["params"]["format_enforcement_min_tokens"]
    assert format_threshold["type"] == "number"
    assert format_threshold["default"] == 1000
    assert format_threshold["min"] == 0
    assert format_threshold["max"] == 100000
    assert "0 disables" in format_threshold["hint"]
    # The floor is BARE by design: it asks for the final response and nothing
    # else. The envelope and the output format live on the layers above it.
    assert descriptor["prompt_instruction"] == FLOOR_MARKER
    assert ENVELOPE_MARKER not in descriptor["prompt_instruction"]


def test_menu_exposes_the_param_schema_to_the_modes_page():
    menu = {row["id"]: row for row in gate_registry.gate_type_menu()}
    json.dumps(menu)  # must survive an API response
    params = menu["closing_message"]["params"]
    assert params["flags"]["default"] == "needs_input, complete, error, partial, unverified"
    assert params["notification"]["label"] == "Notification / next-step guidance"
    assert params["format"]["label"] == "Output format guidance"
    # The word limit travels with the schema so the app-level Gates row renders a
    # number stepper rather than skipping the param.
    assert params["cta_max_words"]["type"] == "number"
    assert params["cta_max_words"]["default"] == 5
    assert params["format_enforcement_min_tokens"]["default"] == 1000
    # No shipped output format: the gate ships the envelope and nothing else, so
    # an agent that configures no format answers freeform. Format is taste — it
    # belongs to the app's global guidance or to the individual agent.
    assert not params["format"].get("default")


def test_private_status_tool_schema_and_validator_follow_policy():
    policy = _policy(_enabled())
    definition = closing_message.status_tool_definition(policy)
    function = definition["function"]
    assert function["name"] == "set_closer_status"
    assert function["parameters"]["additionalProperties"] is False
    assert function["parameters"]["properties"]["flag"]["enum"] == DEFAULT_TAXONOMY

    status, failures = closing_message.validate_status(
        {"flag": "complete", "notification": "Verified"}, policy)
    assert failures == []
    assert status == {"flag": "complete", "cta": "Verified"}

    status, failures = closing_message.validate_status(
        {"flag": "unknown", "notification": "word " * 100}, policy)
    assert status is None
    assert failures == ["flag", "notification"]

    # A current run's prose needs no visible envelope when the validated status
    # is supplied separately; the legacy parser remains the fallback when it is
    # not supplied, so historic rows keep their original meaning.
    assert closing_message.missing_requirements("Completed the change.", policy, {
        "flag": "complete", "cta": "Ready to use",
    }) == []
    record = closing_message.presentation("Completed the change.", policy, status={
        "flag": "complete", "cta": "Ready to use",
    })
    assert record["flag"] == "complete"
    assert record["cta"] == "Ready to use"
    assert record["envelope_valid"] is False


def test_flag_and_notification_requirements_are_independently_toggleable():
    flag_only = _policy(_enabled(params={
        "require_flag": True, "require_notification": False,
    }))
    assert closing_message.status_tool_definition(flag_only)["function"]["parameters"]["required"] == ["flag"]
    assert closing_message.missing_requirements("Done.", flag_only, {"flag": "complete"}) == []
    assert closing_message.missing_requirements("Done.", flag_only, {}) == ["flag"]

    notification_only = _policy(_enabled(params={
        "require_flag": False, "require_notification": True,
    }))
    assert closing_message.status_tool_definition(notification_only)["function"]["parameters"]["required"] == ["notification"]
    assert closing_message.missing_requirements("Done.", notification_only, {"cta": "Ready"}) == []
    assert closing_message.missing_requirements("Done.", notification_only, {}) == ["notification"]

    metadata_off = _policy(_enabled(params={
        "require_flag": False, "require_notification": False,
        "output_closer_enabled": False,
    }))
    assert closing_message.status_tool_definition(metadata_off) is None
    assert closing_message.missing_requirements("Done.", metadata_off, {}) == []
    assert metadata_off["output_closer_enabled"] is False


def test_output_token_limit_is_a_closing_message_requirement():
    policy = _policy(_enabled(params={
        "max_tokens": 20,
        "rejection_message": "Use concise bullet points only.",
    }))
    over_limit = "x" * 400 + "\nFLAG: complete\nReady"
    assert "output_token_limit" in closing_message.missing_requirements(over_limit, policy)
    instruction = closing_message.requirement(over_limit, policy)
    assert instruction is not None
    assert "maximum of 20 tokens" in instruction
    assert "Use concise bullet points only." in instruction
    assert closing_message.missing_requirements("Done.\nFLAG: complete\nReady", policy) == []


def test_all_disabled_closing_controls_have_no_terminal_message_constraints():
    """The always-on coordinator is inert when every independent control is off."""
    policy = _policy(_enabled(params={
        "require_flag": False,
        "require_notification": False,
        "output_closer_enabled": False,
        "max_tokens": 1,
        "format_enforcement_min_tokens": 1,
    }))
    # This is intentionally over the configured limit and has no Markdown list.
    # With the closer disabled, it remains the working agent's terminal message.
    terminal = "The real terminal response remains intact and freeform."
    status = {"flag": "complete", "cta": "Ready"}

    assert policy["output_closer_enabled"] is False
    assert policy["max_tokens"] == 0
    assert policy["format_enforcement_min_tokens"] == 0
    assert policy["instruction"] == ""
    assert policy["advisory"] == ""
    assert FLOOR_MARKER not in policy["instruction"]
    assert closing_message.missing_requirements(terminal, policy, status) == []
    assert closing_message.requirement(terminal, policy, status=status) is None


def test_enabled_metadata_controls_share_one_completion_injection_without_output_enforcement():
    policy = _policy(_enabled(params={
        "require_flag": True,
        "require_notification": False,
        "output_closer_enabled": False,
        "max_tokens": 1,
    }))
    assert FLOOR_MARKER in policy["instruction"]
    assert ENVELOPE_MARKER in policy["instruction"]
    assert policy["max_tokens"] == 0
    assert closing_message.missing_requirements(
        "The working response.", policy, {"flag": "complete"},
    ) == []


# ── Prompt-leakage guard (the highest-risk item) ─────────────────────────────

def test_instruction_never_reaches_the_standing_contract_block():
    rec = _agent([
        {"type": "closing_message", "enabled": True},
        {"type": "verify_after_edit", "enabled": True, "message": "CONTROL-MARKER verify first"},
    ])
    contract = parse_contract(rec)
    # The view key exists (the registry seeds every known key) but is not an
    # active rule, which is what keeps the standing block clean.
    assert contract["require_closing_message"] is True
    assert "require_closing_message" not in active_rules(contract)

    block = contract_prompt_block(contract, rec)
    assert "CONTROL-MARKER" in block, "a prompt-inject gate must still render"
    assert FLOOR_MARKER not in block

    rendered = gate_registry.prompt_instructions_for_agent(rec)
    assert "require_closing_message" not in rendered
    assert "require_verify_after_edit" in rendered


def test_pre_tool_inject_types_never_reach_the_standing_contract_block():
    """The pre-tool progress-message gate's body asks the agent for a REPLY, so it is ``pre_tool``:
    delivered by its own rule's message at the moment it first fires, never by
    the standing block or the context estimate. Rendering it there would tell
    every turn to answer with a plan."""
    rec = _agent([{"type": "pre_tool_progress_message", "enabled": True,
                   "message": "CONTROL-MARKER plan first"}])
    contract = parse_contract(rec)
    assert contract["require_pre_tool_progress_message"] is True
    assert gate_registry.prompt_instructions_for_agent(rec).get(
        "require_pre_tool_progress_message") is None
    assert "CONTROL-MARKER" not in contract_prompt_block(contract, rec)


def test_prompt_instructions_skip_completion_inject_types_even_if_asked_for_them():
    """Defense in depth: the filter lives in the registry, not only in
    ``active_rules()``. Swapping the descriptor's inject to ``prompt`` is the
    only thing that may render it."""
    rec = _agent([{"type": "closing_message", "enabled": True}])
    rendered = gate_registry.prompt_instructions_for_agent(rec)
    assert "require_closing_message" not in rendered
    # Message Visibility is default-enabled independently of completion injection.
    assert "require_message_visibility" in rendered

    descriptor = gate_registry.gate_types()["closing_message"]
    descriptor["inject"] = "prompt"
    try:
        rendered = gate_registry.prompt_instructions_for_agent(rec)
        assert FLOOR_MARKER in rendered["require_closing_message"]
    finally:
        gate_registry.reload_gate_types()


# ── Envelope ─────────────────────────────────────────────────────────────────

def test_parse_envelope_splits_statement_flag_and_call_to_action():
    text = "Refactored the closer and landed the tests.\n\nFLAG: complete\nReady for review"
    parsed = closing_message.parse_envelope(text)
    assert parsed["valid"] is True
    assert parsed["statement"] == "Refactored the closer and landed the tests."
    assert parsed["flag"] == "complete"
    assert parsed["cta"] == "Ready for review"


def test_parse_envelope_requires_a_line_anchored_flag_and_uses_the_last_one():
    prose = "I set a flag: done in the log\nNothing else to report."
    assert closing_message.parse_envelope(prose)["valid"] is False
    assert closing_message.parse_envelope(prose)["statement"] == prose.strip()

    twice = "first\nFLAG: partial\nstale\nFLAG: needs_input\n4 questions"
    parsed = closing_message.parse_envelope(twice)
    assert parsed["flag"] == "needs_input"
    assert parsed["cta"] == "4 questions"


def test_parse_envelope_tolerates_indentation_and_a_bare_flag():
    parsed = closing_message.parse_envelope("Done.\n   FLAG:    error   \nRetry the fetch")
    assert parsed["valid"] is True
    assert parsed["flag"] == "error"
    assert parsed["cta"] == "Retry the fetch"
    # Nothing after the flag line — the badge still renders, the CTA is empty.
    assert closing_message.parse_envelope("Done.\nFLAG: partial")["cta"] == ""


def test_parse_envelope_accepts_the_inline_flag_and_status_form():
    """The status on the SAME line as the flag is understood, not dropped.

    A live run was rejected three times and promoted as a labelled PARTIAL purely
    because it wrote ``FLAG: complete — <status>``: the strict one-token form
    matched nothing, so every requirement came due at once. Every separator form
    now yields both the flag and the call-to-action.
    """
    parsed = closing_message.parse_envelope(
        "Model catalog now opens at the top.\n\n"
        "FLAG: complete — model catalog now opens at top"
    )
    assert parsed["valid"] is True
    assert parsed["flag"] == "complete"
    assert parsed["cta"] == "model catalog now opens at top"
    assert parsed["statement"] == "Model catalog now opens at the top."
    for text in (
        "Done.\nFLAG: partial - three checks left",
        "Done.\nFLAG: partial – three checks left",
        "Done.\nFLAG: partial: three checks left",
    ):
        parsed = closing_message.parse_envelope(text)
        assert parsed["flag"] == "partial", text
        assert parsed["cta"] == "three checks left", text


def test_parse_envelope_never_splits_a_hyphenated_flag_name():
    """A dash separates only when it is space-delimited, so a flag whose NAME
    carries a hyphen (a custom taxonomy) survives the tolerant parse."""
    parsed = closing_message.parse_envelope("Done.\nFLAG: needs-input\nSend it")
    assert parsed["flag"] == "needs-input"
    assert parsed["cta"] == "Send it"


def test_inline_envelope_satisfies_the_completion_requirement_check():
    """The incident's own message shape now passes every requirement — the whole
    point of the tolerant parse is that it must not cost a corrective turn."""
    policy = {"flags": DEFAULT_TAXONOMY, "taxonomy": True, "cta_max_words": 6}
    assert closing_message.missing_requirements(
        "Model catalog now opens at the top.\n\n"
        "FLAG: complete — model catalog now opens at top", policy) == []
    # The word limit still applies to an inline status.
    assert closing_message.missing_requirements(
        "Done.\nFLAG: complete — one two three four five six seven", policy,
    ) == ["notification"]


# ── Taxonomy ─────────────────────────────────────────────────────────────────

def test_configured_flags_are_app_level_only_and_never_read_the_instance(
    tmp_path, monkeypatch,
):
    """The taxonomy is ONE app-wide vocabulary.

    It resolves from the app level (App Settings → Gates) with the descriptor's
    shipped default as the fallback. The per-agent instance is deliberately NOT
    read: the flags are destined to line up with backend/session status values,
    so a per-agent list would let the badge drift from the durable status.
    """
    # The live app-settings.json is the DEVELOPER's own machine config: pin it to a
    # missing file, so "nothing is configured" really means nothing.
    monkeypatch.setattr(
        closing_message, "_SETTINGS_FILE", tmp_path / "app-settings.json",
    )
    descriptor = gate_registry.gate_types()["closing_message"]
    assert closing_message.configured_flags({}, descriptor) == DEFAULT_TAXONOMY
    # An instance-local list is ignored — the descriptor default stands.
    assert closing_message.configured_flags(
        {"params": {"flags": " shipped , needs_input "}}, descriptor
    ) == DEFAULT_TAXONOMY
    assert closing_message.configured_flags({}, None) == []


def test_configured_flags_prefer_the_app_level_over_every_lower_layer(tmp_path, monkeypatch):
    settings = tmp_path / "app-settings.json"
    monkeypatch.setattr(closing_message, "_SETTINGS_FILE", settings)
    descriptor = gate_registry.gate_types()["closing_message"]
    settings.write_text(json.dumps({
        "gate_settings": {"closing_message": {"flags": "shipped, blocked"}},
    }), encoding="utf-8")
    assert closing_message.configured_flags({}, descriptor) == ["shipped", "blocked"]
    # The app-level list WINS even when an instance carries its own list.
    assert closing_message.configured_flags(
        {"params": {"flags": "needs_input"}}, descriptor
    ) == ["shipped", "blocked"]


def test_resolve_flag_downgrades_absent_unknown_and_dishonest_complete():
    taxonomy = list(DEFAULT_TAXONOMY)
    assert closing_message.resolve_flag("", taxonomy) == ("partial", "absent")
    assert closing_message.resolve_flag("<name>", taxonomy) == ("partial", "absent")
    assert closing_message.resolve_flag("shipped", taxonomy) == ("partial", "unknown")
    assert closing_message.resolve_flag("complete", taxonomy, ["ui_journey"]) == (
        "partial", "completion_gaps",
    )
    assert closing_message.resolve_flag("complete", taxonomy, []) == ("complete", "")
    assert closing_message.resolve_flag("needs_input", taxonomy, ["ui_journey"]) == (
        "needs_input", "",
    )


def test_resolve_flag_without_a_taxonomy_accepts_any_token_but_the_placeholder():
    assert closing_message.resolve_flag("shipped", None) == ("shipped", "")
    assert closing_message.resolve_flag("", None) == (closing_message.SAFE_FLAG, "absent")


def test_safe_flag_always_lands_inside_a_custom_taxonomy():
    assert closing_message.resolve_flag("bogus", ["blocked", "complete"]) == ("blocked", "unknown")
    assert closing_message.resolve_flag("bogus", ["complete"]) == ("complete", "unknown")


# ── Instruction assembly ─────────────────────────────────────────────────────

def test_instruction_layers_stack_on_the_floor_and_never_replace_it(tmp_path, monkeypatch):
    """Floor → app-level params → flag clause → agent-level params.

    There is NO generic app-level guidance blob: app-wide guidance IS the gate's
    own app-level params, so the layers above the floor are exactly the rows the
    gate declares (for ``closing_message``: notification and format).
    """
    settings = tmp_path / "app-settings.json"
    monkeypatch.setattr(closing_message, "_SETTINGS_FILE", settings)
    settings.write_text(json.dumps({
        "gate_settings": {"closing_message": {
            "notification": "APPNOTIFY: name the next step.",
            "format": "APPFORMAT: one short paragraph.",
        }},
    }), encoding="utf-8")
    instance = {
        "type": "closing_message",
        "enabled": True,
        "params": {
            "flags": "needs_input, complete",
            "notification": "AGENTNOTIFY: always name the next step.",
            "format": "AGENTFORMAT: one short paragraph.",
        },
        # There is deliberately NO per-instance instruction override: a free-text
        # agent-local edit could silently drop the envelope and the badge with it.
        # This key MUST be inert — a legacy stored `message` may not reach the
        # prompt, which is what keeps the envelope a machine-readable contract.
        "message": "OVERRIDE-MARKER end with the flag line.",
    }
    instruction = closing_message.assemble_instruction(
        instance, gate_registry.gate_types()["closing_message"],
    )
    for marker in ("APPNOTIFY", "APPFORMAT", "AGENTNOTIFY", "AGENTFORMAT"):
        assert marker in instruction
    assert "OVERRIDE-MARKER" not in instruction, "no per-instance override"
    assert FLOOR_MARKER in instruction, "the floor must survive every layer"
    # The taxonomy is app-wide, so the instance's own `flags` param is ignored and
    # the descriptor's shipped default is what resolves.
    assert "Valid flags: needs_input, complete, error, partial, unverified." in instruction
    # Order: floor → app params → flag clause → agent append.
    assert instruction.index(FLOOR_MARKER) < instruction.index("APPNOTIFY")
    assert instruction.index("APPNOTIFY") < instruction.index("APPFORMAT")
    assert instruction.index("APPFORMAT") < instruction.index(ENVELOPE_MARKER)
    assert instruction.index(ENVELOPE_MARKER) < instruction.index("AGENTNOTIFY")
    assert instruction.index("AGENTNOTIFY") < instruction.index("AGENTFORMAT")


def test_blank_configuration_still_produces_the_floor(tmp_path, monkeypatch):
    """An empty agent must not error — the shipped defaults alone are a valid
    instruction: the bare floor, the descriptor's flag taxonomy carrying the
    envelope, and the closing turn directive.

    Nothing needs typing in: the flag taxonomy AND the machine-readable envelope
    come from the descriptor's shipped flag default. No output format ships, so an
    agent that configures none answers freeform — exactly as it did before the
    gate existed. Only the absent-descriptor case omits the flag clause: that is
    the freeform path, and it must never invent a taxonomy.
    """
    monkeypatch.setattr(
        closing_message, "_SETTINGS_FILE", tmp_path / "app-settings.json",
    )
    instance = {"type": "closing_message", "enabled": True, "params": {}}
    instruction = closing_message.assemble_instruction(
        instance, gate_registry.gate_types()["closing_message"],
    )
    assert instruction.startswith(FLOOR_MARKER)
    assert "Valid flags: needs_input, complete, error, partial, unverified." in instruction
    # The envelope rides with the flag vocabulary, because the floor is bare.
    assert ENVELOPE_MARKER in instruction
    # Nothing else: floor + flag clause + the closing turn directive. No format
    # layer, so the STATEMENT stays freeform while the CONTRACT stays explicit.
    assert "Use bullets" not in instruction
    assert len(instruction.splitlines()) == 3
    assert instruction.splitlines()[-1] == closing_message.CLOSE_NOW_DIRECTIVE
    # No descriptor and no instance taxonomy ⇒ no flag clause, never invented
    # (the freeform path the user asked for).
    assert "Valid flags:" not in closing_message.assemble_instruction(instance, None)


def test_format_is_app_level_then_agent_level_and_blank_means_freeform(tmp_path, monkeypatch):
    """The gate ships no format, so the layers above it are the only source.

    The app level applies app-wide; a per-agent value APPENDS after it — never a
    replacement (the documented layering). With both blank the agent answers
    freeform, which is the state a freshly enabled gate starts in.
    """
    settings = tmp_path / "app-settings.json"
    monkeypatch.setattr(closing_message, "_SETTINGS_FILE", settings)
    descriptor = gate_registry.gate_types()["closing_message"]

    # Nothing configured anywhere: no format guidance at all.
    bare = {"type": "closing_message", "enabled": True, "params": {}}
    assert "Use bullets" not in closing_message.assemble_instruction(bare, descriptor)

    # App-level format applies app-wide.
    settings.write_text(json.dumps({
        "gate_settings": {"closing_message": {"format": "APP-FORMAT one line."}},
    }), encoding="utf-8")
    with_app = closing_message.assemble_instruction(bare, descriptor)
    assert "APP-FORMAT" in with_app
    assert "Use bullets" not in with_app

    # Per-agent format appends AFTER the app level — never a replacement.
    scoped = {"type": "closing_message", "enabled": True,
              "params": {"format": "AGENT-FORMAT three bullets."}}
    both = closing_message.assemble_instruction(scoped, descriptor)
    assert "APP-FORMAT" in both and "AGENT-FORMAT" in both
    assert both.index("APP-FORMAT") < both.index("AGENT-FORMAT")

    # The agent level alone is enough — that is where a house style lives, and it
    # lands after the flag clause so the envelope it must follow stays first.
    settings.write_text("{}", encoding="utf-8")
    agent_only = closing_message.assemble_instruction(scoped, descriptor)
    assert "AGENT-FORMAT" in agent_only
    assert "APP-FORMAT" not in agent_only
    assert agent_only.index(ENVELOPE_MARKER) < agent_only.index("AGENT-FORMAT")


def test_app_gate_values_reads_live_and_never_raises(tmp_path, monkeypatch):
    """The app-level layer is the gate's OWN params, read live and never raising.

    This is ``gate_settings.closing_message`` in app-settings.json — the tailored
    surface App Settings → Gates edits — NOT a generic guidance blob. A legacy
    top-level ``closing_message_guidance`` key is deliberately ignored.
    """
    settings = tmp_path / "app-settings.json"
    monkeypatch.setattr(closing_message, "_SETTINGS_FILE", settings)
    assert closing_message.app_gate_values() == {}  # missing file

    settings.write_text(json.dumps({
        "gate_settings": {"closing_message": {"flags": "  needs_input , complete  ",
                                              "format": "   "}},
    }), encoding="utf-8")
    # Values are stripped and blank keys are dropped, so clearing a field unsets it.
    assert closing_message.app_gate_values() == {"flags": "needs_input , complete"}

    settings.write_text(json.dumps({
        "gate_settings": {"closing_message": {"flags": {"nope": True}}},
    }), encoding="utf-8")
    assert closing_message.app_gate_values() == {}

    settings.write_text(json.dumps({"closing_message_guidance": "App-wide rule."}), encoding="utf-8")
    assert closing_message.app_gate_values() == {}  # legacy key is ignored

    settings.write_text("{not json", encoding="utf-8")
    assert closing_message.app_gate_values() == {}


# ── Policy resolution ────────────────────────────────────────────────────────

def test_policy_is_inert_for_legacy_master_off_unknown_or_out_of_scope():
    assert closing_message.resolve(_agent()) is None
    legacy_off = closing_message.resolve(_agent([{"type": "closing_message", "enabled": False}]))
    assert legacy_off is not None
    assert legacy_off["instruction"] == ""
    assert closing_message.missing_requirements("Working response.", legacy_off, {}) == []
    assert closing_message.resolve(_agent([{"type": "nope_gate", "enabled": True}])) is None
    scoped = _enabled(scope=["plan"])
    assert closing_message.resolve(scoped, "auto") is None
    assert closing_message.resolve(scoped, "plan") is not None
    assert closing_message.resolve(scoped) is not None  # no mode means every mode


def test_policy_carries_the_resolved_taxonomy_and_instruction():
    # A per-instance `flags` param is ignored: the taxonomy is app-wide, so the
    # descriptor's shipped default is what resolves on this policy.
    policy = _policy(_enabled(params={"flags": "shipped, blocked"}))
    assert policy["flags"] == DEFAULT_TAXONOMY
    assert policy["taxonomy"] is True
    assert policy["instance"]["type"] == "closing_message"
    assert FLOOR_MARKER in policy["instruction"]
    assert "Valid flags: needs_input, complete, error, partial, unverified." in policy["instruction"]


# ── Completion-path requirement ──────────────────────────────────────────────

def test_requirement_keeps_costing_turns_until_every_requirement_is_met():
    policy = _policy(_enabled())
    # No envelope: all three requirements are owed, so the instruction is injected.
    assert set(closing_message.missing_requirements("no envelope here", policy)) == {
        "flag", "notification", "message",
    }
    assert closing_message.requirement("no envelope here", policy, 0) == policy["instruction"]
    # A bare flag line still owes the output message and the notification.
    assert set(closing_message.missing_requirements("FLAG: complete", policy)) == {
        "notification", "message",
    }
    # Re-injection continues until the OVERALL budget is spent …
    for retries in range(policy["max_attempts"]):
        assert closing_message.requirement(
            "FLAG: complete", policy, retries,
        ) == policy["instruction"]
    # … and the cap is what ends it, so the loop cannot diverge.
    assert closing_message.requirement(
        "FLAG: complete", policy, policy["max_attempts"],
    ) is None
    # The per-requirement cap ALSO ends it — but only once EVERY still-missing
    # requirement has individually hit it.
    per_max = policy["max_attempts_per_requirement"]
    assert per_max > 0
    capped = {"flag": 0, "notification": per_max, "message": per_max}
    assert closing_message.requirement(
        "FLAG: complete", policy, 1, capped,
    ) is None
    # A requirement with attempts left keeps the run going (the others cannot
    # starve it of its own turns).
    assert closing_message.requirement(
        "FLAG: complete", policy, 1, {"notification": per_max},
    ) == policy["instruction"]
    # A fully compliant message is owed nothing.
    assert closing_message.requirement(
        "Done.\nFLAG: complete\nReady", policy, 0,
    ) is None
    # Gate off.
    assert closing_message.requirement("no envelope", None, 0) is None


def test_missing_requirements_validates_baseline_closing_contract(tmp_path, monkeypatch):
    """Flag in the taxonomy, notification above 0 and within the configured word
    limit, and output message above 0 are always enforced. Formatting remains
    freeform while the response is at or below its configured threshold."""
    monkeypatch.setattr(
        closing_message, "_SETTINGS_FILE", tmp_path / "app-settings.json",
    )
    policy = _policy(_enabled())
    assert policy["cta_max_words"] == 5

    assert closing_message.missing_requirements(
        "Landed the change.\nFLAG: complete\nReady to test", policy,
    ) == []

    # Empty output message.
    assert closing_message.missing_requirements(
        "FLAG: complete\nReady to test", policy,
    ) == ["message"]
    # Missing notification line entirely.
    assert closing_message.missing_requirements(
        "Landed it.\nFLAG: complete", policy,
    ) == ["notification"]
    # Notification ABOVE the word limit (six words against a limit of five).
    over = "Landed it.\nFLAG: complete\n" + " ".join(["word"] * 6)
    assert closing_message.missing_requirements(over, policy) == ["notification"]
    # Off-taxonomy flag costs a turn now, rather than only downgrading the badge.
    assert closing_message.missing_requirements(
        "Landed it.\nFLAG: done\nReady", policy,
    ) == ["flag"]
    # Nor is the literal placeholder a member of the taxonomy.
    assert closing_message.missing_requirements(
        "Landed it.\nFLAG: <name>\nReady", policy,
    ) == ["flag"]
    # A short response stays freeform under the shipped format threshold.
    at_limit = "A concise freeform answer.\nFLAG: complete\n" + " ".join(["word"] * 5)
    assert closing_message.missing_requirements(at_limit, policy) == []


def test_bullet_format_enforcement_is_threshold_gated_and_configurable():
    short = "A concise freeform answer.\nFLAG: complete\nReady"
    token_total = closing_message.token_count(short)
    assert token_total > 1

    # The shipped per-agent default is active unless the agent overrides it.
    assert _policy(_enabled())["format_enforcement_min_tokens"] == 1000
    malformed = _policy(_enabled(params={"format_enforcement_min_tokens": "nope"}))
    assert malformed["format_enforcement_min_tokens"] == 1000

    # Exact threshold is accepted unchanged, including freeform prose.
    exact = _policy(_enabled(params={"format_enforcement_min_tokens": token_total}))
    assert closing_message.missing_requirements(short, exact) == []

    # One token above the threshold requires a recognizable Markdown list item.
    over = _policy(_enabled(params={"format_enforcement_min_tokens": token_total - 1}))
    assert closing_message.missing_requirements(short, over) == ["output_format"]
    instruction = closing_message.requirement(short, over)
    assert instruction is not None
    assert "Markdown bullet points" in instruction
    assert "Reply with only the rewritten final response" in instruction

    bulleted = "- A concise result.\n- The next step.\nFLAG: complete\nReady"
    assert closing_message.missing_requirements(bulleted, over) == []

    # Zero intentionally disables only the format check.
    disabled = _policy(_enabled(params={"format_enforcement_min_tokens": 0}))
    assert closing_message.missing_requirements(short, disabled) == []


def test_cta_word_limit_is_app_level_configuration(tmp_path, monkeypatch):
    """The limit is read from the app-level Gate settings, like the taxonomy, and
    the stated instruction is generated from the SAME value the validator uses."""
    settings = tmp_path / "app-settings.json"
    monkeypatch.setattr(closing_message, "_SETTINGS_FILE", settings)
    descriptor = gate_registry.gate_types()["closing_message"]
    instance = {"type": "closing_message", "enabled": True, "params": {}}

    # Descriptor default when nothing is configured.
    assert closing_message.configured_cta_max_words(descriptor) == 5
    assert "a short 1-5 word status" in closing_message.assemble_instruction(
        instance, descriptor)

    # App-level override wins, and flows into BOTH the instruction and the check.
    settings.write_text(json.dumps({
        "gate_settings": {"closing_message": {"cta_max_words": 3}},
    }), encoding="utf-8")
    assert closing_message.configured_cta_max_words(descriptor) == 3
    instruction = closing_message.assemble_instruction(instance, descriptor)
    assert "a short 1-3 word status" in instruction
    policy = {"flags": DEFAULT_TAXONOMY, "taxonomy": True,
              "cta_max_words": closing_message.configured_cta_max_words(descriptor),
              "instruction": instruction}
    assert closing_message.missing_requirements(
        "Landed it.\nFLAG: complete\none two three", policy) == []
    assert closing_message.missing_requirements(
        "Landed it.\nFLAG: complete\none two three four", policy) == ["notification"]

    # A blank or unparseable value falls back to the shipped default rather than
    # to 0, so bad config can never reject every closing message.
    settings.write_text(json.dumps({
        "gate_settings": {"closing_message": {"cta_max_words": "not-a-number"}},
    }), encoding="utf-8")
    assert closing_message.configured_cta_max_words(descriptor) == 5
    monkeypatch.setattr(
        closing_message, "_SETTINGS_FILE", tmp_path / "absent.json",
    )
    assert closing_message.configured_cta_max_words(descriptor) == 5


def test_requirement_budget_is_agent_limit_clamped_to_the_app_ceiling(tmp_path, monkeypatch):
    """The effective budget is min(agent limit, app ceiling), and the loop carries
    BOTH counters in contract state (overall + per requirement). Re-injection
    continues until a requirement is met, so this budget is the ONLY thing bounding
    the completion path.

    These are requirement-compliance budgets, NOT the runtime page's resume/error
    counters — a different trigger, counter and lifecycle."""
    settings = tmp_path / "app-settings.json"
    monkeypatch.setattr(closing_message, "_SETTINGS_FILE", settings)
    # Shipped defaults when neither layer is configured.
    assert closing_message.effective_max_attempts(None) == closing_message.DEFAULT_MAX_ATTEMPTS
    assert (closing_message.effective_max_attempts_per_requirement(None)
            == closing_message.DEFAULT_MAX_ATTEMPTS_PER_REQUIREMENT)
    # The app ceiling clamps a larger agent limit; the effective overall is the min.
    settings.write_text(json.dumps({
        "run_max_closing_attempts": 4,
        "run_max_closing_attempts_per_requirement": 2,
    }), encoding="utf-8")
    agent = {"metadata": {"closing_max_attempts": 9,
                          "closing_max_attempts_per_requirement": 9}}
    assert closing_message.effective_max_attempts(agent) == 4
    assert closing_message.effective_max_attempts_per_requirement(agent) == 2
    # An agent value BELOW the ceiling wins (the agent may be stricter).
    assert closing_message.effective_max_attempts({"metadata": {"closing_max_attempts": 2}}) == 2
    # A stale zero/blank means inherit, so it can never silently disable the gate.
    assert closing_message.effective_max_attempts({"metadata": {"closing_max_attempts": 0}}) == 4

    # The loop carries both state keys and re-injects through requirement().
    loop_source = (
        Path(__file__).resolve().parents[1] / "app" / "agent" / "loop.py"
    ).read_text(encoding="utf-8")
    assert '"closing_message_completion_retries"' in loop_source
    assert '"closing_message_requirement_retries"' in loop_source
    assert "closing_message_required" in loop_source


# ── Durable presentation ─────────────────────────────────────────────────────

def test_missing_requirement_classification_keeps_metadata_out_of_the_prose_lane():
    assert closing_message.classify_missing_requirements([
        "flag", "notification", "output_token_limit", "output_format", "message",
    ]) == {
        "metadata": ["flag", "notification"],
        "output": ["output_token_limit", "output_format", "message"],
    }
    # Unknown requirements must never silently acquire permission to trigger a
    # private-only repair. They retain the conservative output/prose lane.
    assert closing_message.classify_missing_requirements(["future_requirement"]) == {
        "metadata": [], "output": ["future_requirement"],
    }


def test_metadata_repair_instruction_never_requests_visible_prose():
    instruction = closing_message.metadata_repair_instruction(["flag", "notification"])
    assert "set_closer_status" in instruction
    assert "Do not write any user-facing response" in instruction
    assert "must remain unchanged" in instruction
    assert "rewritten final response" not in instruction


def test_presentation_records_flag_call_to_action_and_downgrade():
    policy = _policy(_enabled())
    record = closing_message.presentation(
        "Landed the change.\nFLAG: complete\nReady to test", policy, [],
    )
    assert record == {
        "flag": "complete", "cta": "Ready to test",
        "envelope_valid": True, "downgraded": "", "failed": [], "partial": False,
    }

    downgraded = closing_message.presentation(
        "Landed the change.\nFLAG: complete\nReady to test", policy, ["ui_journey"],
    )
    assert downgraded["flag"] == "partial"
    assert downgraded["downgraded"] == "completion_gaps"
    assert downgraded["envelope_valid"] is True

    # Exhausted gate: the message is promoted anyway but the badge is a labelled
    # PARTIAL — the safe flag is forced and the reason explains itself.
    gave_up = closing_message.presentation(
        "Still working.\nFLAG: complete\nKeep going", policy, [], partial=True,
    )
    assert gave_up["partial"] is True
    assert gave_up["flag"] == "partial"
    assert gave_up["downgraded"] == "partial"


def test_presentation_is_safe_without_an_envelope_and_bounds_the_call_to_action():
    policy = _policy(_enabled())
    bare = closing_message.presentation("No envelope at all.", policy, [])
    assert bare["flag"] == "partial"
    assert bare["cta"] == ""
    assert bare["envelope_valid"] is False
    assert bare["downgraded"] == "absent"

    long_cta = closing_message.presentation(
        "Done.\nFLAG: partial\n" + "x" * 200, policy, [],
    )
    assert len(long_cta["cta"]) == 80


def test_completion_gaps_reports_outstanding_requirements():
    """Gaps are read from the real contract-state keys the loop maintains —
    ``repository_change_revision`` / ``service_worker_cache_bump_pending``
    (see contracts.note_repository_change) and the UI-journey state."""
    contract = {"require_final_service_worker_cache_bump": True}
    pending = {
        "repository_change_revision": 1,
        "service_worker_cache_bump_pending": True,
        "ui_changed_paths": [], "ui_journey": {},
    }
    assert "service_worker_cache_bump" in closing_message.completion_gaps(contract, pending)
    # A satisfied cache bump clears the gap.
    assert closing_message.completion_gaps(contract, {
        "repository_change_revision": 1,
        "service_worker_cache_bump_pending": False,
    }) == []
    # No contract ⇒ no requirement is outstanding.
    assert closing_message.completion_gaps({}, {"service_worker_cache_bump_pending": True}) == []


# ── Aborted-attempt folding (promoted-message duplicate guard) ───────────────

def _loop_source() -> str:
    return (
        Path(__file__).resolve().parents[1] / "app" / "agent" / "loop.py"
    ).read_text(encoding="utf-8")


def _completion_region(source: str) -> str:
    start = source.index("# ── Empty-response safety net ──")
    end = source.index("# ── No tool calls → final response ──")
    assert start < end
    return source[start:end]


def test_metadata_retry_preserves_and_folds_the_original_candidate():
    """Metadata repair uses a private status turn, never a replacement draft.

    The original terminal row is folded before repair, its durable identity is
    retained as ``_closing_metadata_candidate``, and the common promotion block
    later adopts that candidate rather than the repair turn's empty row.
    """
    region = _completion_region(_loop_source())
    marker = "if _closing_metadata_only:"
    assert marker in region
    metadata = region[region.index(marker):region.index("else:", region.index(marker))]
    assert "_closing_metadata_candidate" in metadata
    assert "_finalize_progress_attempt(_final_content)" in metadata
    assert "_rejected_draft_turn" not in metadata
    assert "metadata_repair_instruction" in region
    assert "if _closing_metadata_candidate is not None:" in region
    assert "streaming_asst_id = _candidate_id" in region


def test_fold_marks_the_aborted_row_progress_and_emits_a_progress_step():
    source = _loop_source()
    assert "async def _finalize_progress_attempt" in source
    helper = source[source.index("async def _finalize_progress_attempt"):
                    source.index("async def _persist_stream_progress")]
    assert 'message_phase="progress"' in helper
    assert '"type": "agent_step_end"' in helper
    assert 'status="complete"' in helper
    # The handle is cleared before the write so an interrupt during the retry
    # cannot re-finalize the same row.
    assert helper.index("streaming_asst_id = None") < helper.index("db.update_interaction")


# ── Rejected-draft replay (the retry revises its own text) ───────────────────

def test_rejected_draft_turn_replays_the_draft_as_a_revisable_assistant_turn():
    """The retry must see the draft it is being asked to fix. A tool-free turn is
    never appended to ``messages`` — only tool-call turns are — so a corrective
    turn that re-states the contract but omits the draft leaves the model with
    nothing to revise. In session d4f7de36-ed3e-483a-aac5-fb7849adacb4 the closing
    gate folded FOUR consecutive "here's the plan" drafts that never carried the
    ``FLAG:`` envelope, each re-derived from scratch until the budget was spent."""
    from app.agent import loop as agent_loop

    turn = agent_loop._rejected_draft_turn("  Here's the plan.\n\n1. Do the thing.  ")
    assert turn is not None
    assert turn["role"] == "assistant"
    # Stripped, so the replayed turn matches the text the transcript folded.
    assert turn["content"] == "Here's the plan.\n\n1. Do the thing."

    # An empty draft never becomes a blank assistant turn.
    assert agent_loop._rejected_draft_turn("") is None
    assert agent_loop._rejected_draft_turn("   \n  ") is None
    assert agent_loop._rejected_draft_turn(None) is None

    # Reasoning travels WITH the replayed turn: DeepSeek thinking mode 400s a
    # request whose assistant turn drops the reasoning it returned.
    thinking = agent_loop._rejected_draft_turn(
        "Draft.", reasoning="because", responses_reasoning=[{"type": "reasoning"}],
        reasoning_scope="deepseek",
    )
    assert thinking["reasoning_content"] == "because"
    assert thinking["_responses_reasoning"] == [{"type": "reasoning"}]
    assert thinking["_reasoning_replay_scope"] == "deepseek"


def test_output_retry_replays_the_draft_but_metadata_retry_does_not():
    """Only explicitly enabled output constraints may request replacement prose."""
    region = _completion_region(_loop_source())
    marker = "else:\n                        # Output constraints are the only failures allowed"
    assert marker in region
    output = region[region.index(marker):region.index("messages.append({\"role\": \"system\", \"content\": _closing_msg})", region.index(marker))]
    assert "_draft_replay = _rejected_draft_turn(" in output
    assert "messages.append(_draft_replay)" in output
    assert "if _draft_replay is not None and not _response_chain_id:" in output
    metadata = region[region.index("if _closing_metadata_only:"):region.index(marker)]
    assert "_rejected_draft_turn" not in metadata


def test_closing_retry_is_a_terminal_revision_turn_that_carries_no_tools():
    """Once the gate rejects a message, the run may no longer go back to work.

    The corrective instruction tells the model to start no new work and send no
    tool calls, but that was only ever REQUESTED. In session
    f1947641-da50-40d8-9b57-4a915a2e0740 the model answered a rejection by
    returning to work and spent the run's compliance budget re-deriving ~70 rows
    of an already-verified inventory before its next closing attempt — on a turn
    whose three requirements (flag, notification, message) are all properties of
    the closing TEXT, so no tool result could have satisfied any of them. The
    retry turn still re-enters the loop (that is how the model delivers its
    revised message), but it now carries no tool schemas and the work-policy
    nudges decline to send it back to work."""
    source = _loop_source()
    region = _completion_region(source)

    # Declared before the loop body, so every guarded site below reads it.
    declaration = source.index("_closing_retry_terminal = False")
    assert declaration < source.index("if _closing_retry_terminal:")

    # The retry masks every normal schema after the shared-cache write, then
    # injects ONLY the gate-owned private status tool for this final revision.
    cached = source.index("await _td_cache.set(_td_key, tool_definitions)")
    mask = source.index("if _closing_retry_terminal:", cached)
    assert mask < source.index("tool_definitions = []", mask)
    assert "status_tool_definition(_closing_status_policy)" in source
    assert '"name": "set_closer_status"' in source
    # The status call is intercepted and removed before the ordinary handler;
    # it must never yield a visible tool event, database tool row, or replay item.
    private_start = source.index("# ── Gate-owned closer status")
    ordinary_start = source.index("# ── Handle tool calls ──", private_start)
    private_region = source[private_start:ordinary_start]
    assert "validate_status" in private_region
    assert "collected_tool_calls = _remaining_calls" in private_region
    assert "messages.append" not in private_region

    # Armed inside the closing gate itself, after the aborted attempt is folded
    # and immediately before the loop re-enters: nothing between here and the
    # next verdict can hand the run back to work.
    block = region[region.index("_draft_replay = _rejected_draft_turn("):]
    block = block[:block.index("turn_count -= 1")]
    assert block.index("_closing_msg") < block.index("_closing_retry_terminal = True")
    assert block.index(
        "yield await _finalize_progress_attempt(_final_content)"
    ) < block.index("_closing_retry_terminal = True")
    assert "_closing_retry_terminal = True" in block
    assert "_closing_status_pending = True" in block
    assert "_closer_status = None" in block

    # Every rung that can force work declines a revision turn: with no schemas
    # sent, "call the next necessary tool now" could only be a contradiction.
    # Whitespace is collapsed so the three rungs are matched by shape, not by
    # the indentation of the call site they sit in.
    normalized = " ".join(source.split())
    assert normalized.count(
        "not _closing_retry_terminal and _work_policy.requires_tool"
    ) == 2, "both pre-call nudges must decline a revision turn"
    assert (
        "_work_policy.requires_tool and not _closing_retry_terminal"
        in normalized
    ), "the post-call rung must decline a revision turn"


def test_terminal_promotion_is_the_closing_gates_verdict():
    """Only a message that satisfies all three requirements is a clean ``main``.
    A message whose budget is spent is STILL promoted — but stamped as a labelled
    PARTIAL — and exactly ONE user-visible (non-debug) system row is written
    beside it, so the run never ends with nothing to show. Exactly ONE terminal is
    promoted per run: a run re-entered by a crash-resume or a browser retry folds
    its later terminal instead of surfacing a second bubble. With no closing policy
    there is nothing to pass, so the ordinary terminal message stays the main reply
    and gate-off agents are unchanged."""
    source = _loop_source()
    region = source[source.index("# ── Promotion is the gate's verdict ──"):
                    source.index("# ── Stall guard stop")]
    # The verdict is computed from the same predicate the retry loop enforces,
    # AND the one-promoted-terminal-per-run latch.
    assert "_closing_failed = _closing_missing(" in region
    assert "_final_content, _closing_policy, _closer_status or {}" in region
    assert "(not _closing_failed or _closing_partial)" in region
    assert "and not await _run_promotion_taken()" in region
    # Exhaustion is the per-requirement/overall budget verdict, not a fold: the
    # message is promoted as a labelled PARTIAL…
    assert "_closing_partial = _closing_exhausted(" in region
    assert "partial=_closing_partial" in region
    # …and exactly ONE non-debug system row says so, guarded by a contract-state
    # latch (and only alongside this run's single promotion).
    assert 'source="system:closing_partial"' in region
    assert "_persist_user_visible_system_error(" in region
    assert "closing_partial_notice_emitted" in region
    assert region.count("_persist_user_visible_system_error(") == 1
    # The latch is defined once and reads the durable render kind, so a
    # re-entered run cannot promote a second terminal.
    assert "async def _run_promotion_taken() -> bool:" in source
    assert '"has_promoted_closing_terminal"' in source
    # Both the durable row and the live event carry the verdict-derived kind.
    assert region.count('chat_render_kind=("promoted_answer" if _final_promoted else "activity")') == 2
    assert region.count('"chat_render_kind": ("promoted_answer" if _final_promoted else "activity")') == 1
    # An unpromoted message reports why, and never reaches the Output Closer —
    # the closer's recap would otherwise become the visible bubble instead.
    assert 'meta_final_data["closing_promoted"] = bool(_final_promoted)' in region
    assert 'meta_final_data["closing_failed"] = list(_closing_failed)' in region
    assert "if _final_promoted:" in region
    assert region.index("if _final_promoted:") < region.index("_schedule_output_closer(")


def test_promoted_answers_retain_the_originating_run_turn_id():
    """Every durable assistant row that can become the terminal answer must
    retain the run-turn identity for live reconciliation and replay."""
    source = _loop_source()
    # One DB-first streaming destination is later promoted in place. The four
    # direct fallbacks are cleanup-final, ordinary final, stall-stop, and
    # max-turns. Losing any one reintroduces an unkeyed promoted answer.
    assert source.count("turn_id=_run_turn_id,") >= 5


def test_cleanup_final_cannot_bypass_the_gate():
    """The cleanup-final path promotes a step to ``main`` and returns early,
    which would skip the terminal block — and therefore the gate — entirely.
    With an active closing policy that the step's content does not satisfy, the
    early exit must stand down so the run reaches the terminal block."""
    source = _loop_source()
    assert "from app.agent.closing_message import (" in source
    assert "if _cu_policy and _cu_missing(assistant_content, _cu_policy):" in source
    assert "cleanup_final = False" in source
    # The demotion must be guarded so a gate failure can never fail a run.
    assert "except Exception:  # noqa: BLE001 — the gate must never fail a run" in source


def test_client_promotion_is_the_gate_verdict_only():
    """The browser consumes the explicit render contract without a phase reader."""
    repo = Path(__file__).resolve().parents[1]
    ui = repo / "ui" / "chat" / "js"
    source = (ui / "session-load.js").read_text(encoding="utf-8")
    assert "function _buildPhasedRenderables" not in source
    assert "function _messagePhase" not in source
    assert "function _explicitPhase" not in source
    assert "['user', 'promoted_answer', 'notice', 'error']" in source

    stream = (ui / "chat-stream.js").read_text(encoding="utf-8")
    assert "ownerTurnId, interactionSeq, chatRenderKind, midRun) {" in stream
    assert "if (renderKind !== 'promoted_answer') {" in stream
    assert "const verdict = String(phase || '')" not in stream

    reconcile = (ui / "chat-reconcile.js").read_text(encoding="utf-8")
    assert "const renderKind = String(msg.chat_render_kind || 'activity').toLowerCase();" in reconcile
    assert "if (renderKind === 'promoted_answer' || renderKind === 'notice') {" in reconcile
    assert "renderKind === 'notice'," in reconcile
    assert "msg.interaction_seq ?? msg.session_seq," + "\n" + "            renderKind," in reconcile

    # Live WebSocket delivery must consume the same closer-gate verdict as durable
    # replay. Falling back to message_phase alone turns a promoted terminal into an
    # activity response until refresh reads the persisted row.
    ws = (repo / "ui" / "shared" / "js" / "agentWs.js").read_text(encoding="utf-8")
    assert "event.chat_render_kind || event.message_phase || ''" in ws


# ── Work-contract closure (exhausted-work handoff) ──────────────────────────


def test_work_contract_gap_forbids_a_complete_flag():
    """A run whose work policy is still pending has NOT done what the user asked,
    so the agent's closing message may not claim ``complete``. The loop stamps the
    LIVE work state (``work_policy_pending``) on the contract state at the
    completion path, and the gap probe reads it."""
    policy = _policy(_enabled())
    gaps = closing_message.completion_gaps({}, {"work_policy_pending": True})
    assert "work_pending" in gaps

    record = closing_message.presentation(
        "Could not start the work.\nFLAG: complete\nSend continue", policy, gaps,
    )
    assert record["flag"] == "partial"
    assert record["downgraded"] == "completion_gaps"

    # A satisfied work contract leaves no gap, so real completion still closes.
    assert closing_message.completion_gaps({}, {"work_policy_pending": False}) == []


def test_exhausted_work_contract_hands_off_to_the_gate_only_when_active():
    """With a closing gate active, an exhausted work contract is handed to the
    gate — the agent authors its OWN closing message — instead of hard-stopping
    with a red ``work_not_started`` error. With no gate there is nothing to author
    that message, so the hard stop is preserved EXACTLY as before."""
    source = _loop_source()
    region = _completion_region(source)

    # The handoff step and the gate probe live inside the completion path.
    assert '"step": "work_policy_closer_handoff"' in region
    assert (
        "from app.agent.closing_message import resolve as _resolve_work_closer"
        in region
    )
    # The probe is fail-open, so a gate lookup can never fail a run.
    assert "_work_closer_active = False" in region
    # ...and the gate-off hard stop is UNCHANGED.
    assert '"stop_cause": "work_not_started"' in region

    # The live work state is stamped for the gate's gap probe, recomputed on the
    # completion path so work that DID happen clears the gap.
    assert (
        '_contract_state["work_policy_pending"] = bool(_work_policy.requires_tool)'
        in source
    )


# ── Rejection logging (status + reason) ──────────────────────────────────────


def test_closing_rejection_logs_status_and_failing_requirements(monkeypatch):
    """Each rejection records WHY it was rejected — the status plus the
    still-missing requirement names — not merely that it happened.

    Load-bearing because a run rejected once or twice and then promoted writes
    NO ``closing_partial`` row, so without this payload those rejections leave
    no trace at all. The record must stay INFO (never warning): a retry the
    agent then fixes is routine, and durability comes from the INFO persist
    level, so the step must NOT be pushed into ``_PROBLEM_STEPS`` to become
    queryable.
    """
    from app.agent import diagnostics as diag_mod

    # The loop emits the reason keys on the rejection event.
    source = _loop_source()
    assert '"status": "rejected"' in source
    assert '"failed": list(_closing_missing_now)' in source
    assert "closing_message_required" not in diag_mod._PROBLEM_STEPS

    # A minimal recorder: proves level + durability without touching disk.
    recorder = object.__new__(diag_mod.DiagnosticRecorder)
    recorder.enabled = True
    recorder.file_logging = False
    recorder.persist_level = 20  # INFO — the shipped default
    recorder._ring = __import__("collections").deque(maxlen=8)
    recorder._pending = __import__("collections").deque(maxlen=32)
    recorder._pending_files = __import__("collections").deque(maxlen=32)
    recorder._counts = {k: 0 for k in diag_mod.LEVELS}
    recorder._ensure_task = lambda: None
    monkeypatch.setattr(diag_mod, "get_recorder", lambda: recorder)

    diag_mod.tap_loop_event(
        "session-1",
        {"type": "pipeline", "level": "pipeline",
         "step": "closing_message_required", "turn": 7, "attempt": 2,
         "status": "rejected", "reason": "flag, notification",
         "failed": ["flag", "notification"]},
    )

    record = recorder._ring[-1]
    assert record["level"] == "info"          # routine, not a warning
    assert record["category"] == "loop"
    assert record["source"] == "closing_message_required"
    detail = json.loads(record["detail"])
    assert detail["failed"] == ["flag", "notification"]
    assert detail["reason"] == "flag, notification"
    assert detail["status"] == "rejected"
    assert detail["attempt"] == 2
    # Durable: persist=None auto-decides as INFO >= persist_level → queued.
    assert list(recorder._pending) == [record]


# ── The envelope, delivered BEFORE the closing turn (root cause C) ───────────

ADVISORY_MARKER = "When you write your FINAL message to the user"


def test_advisory_carries_the_envelope_and_never_the_floor(tmp_path, monkeypatch):
    """The completion-path instruction is completion-INJECT, so the model meets the
    envelope only AFTER its draft has been judged and rejected — a weak model then
    re-derives the same prose and the budget is spent on one class of miss. The
    advisory is that same envelope delivered before the closing turn.

    It is the ENVELOPE ONLY, never the floor: the floor asks for the final
    response, and saying that before the run has finished its work reads as "stop
    now" — the exact failure the completion-only injection exists to avoid."""
    # This test is about the SHIPPED defaults, so it must not read whatever the
    # developer's own app-settings.json happens to say.
    monkeypatch.setattr(
        closing_message, "_SETTINGS_FILE", tmp_path / "app-settings.json",
    )
    policy = _policy(_enabled())
    advisory = policy["advisory"]
    assert advisory.startswith(ADVISORY_MARKER)
    assert ENVELOPE_MARKER in advisory
    assert "Valid flags: needs_input, complete, error, partial, unverified." in advisory
    assert FLOOR_MARKER not in advisory
    # The word limit is read from the SAME config the validator enforces.
    assert f"1-{policy['cta_max_words']} word status." in advisory
    # The completion instruction is still floor + envelope, and now carries the turn
    # directive too — which the ADVISORY must never carry: the advisory is delivered
    # before the work is finished, so "send no tool calls" there would read as
    # permission to stop early.
    assert FLOOR_MARKER in policy["instruction"]
    assert ENVELOPE_MARKER in policy["instruction"]
    assert closing_message.CLOSE_NOW_DIRECTIVE in policy["instruction"]
    assert FLOOR_MARKER not in advisory
    assert closing_message.CLOSE_NOW_DIRECTIVE not in advisory

    # No descriptor ⇒ no taxonomy ⇒ nothing to advise (the freeform path).
    assert closing_message.envelope_advisory({"type": "closing_message"}) == ""
    # Both helpers compose from one clause, so they can never drift apart.
    instance = closing_message.resolve(_enabled())["instance"]
    assert closing_message.envelope_advisory(
        instance, gate_registry.gate_types()["closing_message"],
    ).endswith(closing_message._flag_clause(
        instance, gate_registry.gate_types()["closing_message"],
    ))


def test_envelope_advisory_is_delivered_before_the_first_closing_attempt():
    """The advisory must land EARLIER in the loop than the completion path (where
    the gate judges the draft), or it is not in context when the model writes its
    first closing attempt and the fix does nothing."""
    source = _loop_source()
    advisory = source.index('"step": "closing_advisory"')
    completion = source.index("# ── Empty-response safety net ──")
    assert advisory < completion, "the advisory must precede the completion path"

    # Delivered on a turn whose next prose reply COULD be the closer: the run asked
    # for no work, or the work it asked for is finished.
    block = source[source.index("# ── Closing-envelope advisory"):advisory]
    assert "_work_policy.state in (WorkState.CONVERSATION," in block
    assert "WorkState.WORK_SATISFIED" in block
    assert 'messages.append({"role": "system", "content": _closing_advisory})' in block
    # …and it is the policy's advisory, never the assembled completion instruction.
    assert '_resolve_advisory(_agent_rec, execution_mode)' in block
    assert '"advisory") or "").strip()' in block
    assert "_closing_msg" not in block

    # RE-ARMED, not latched once per run. The work state oscillates mid-task — a
    # meaningful tool call satisfies the work, the next pending-checkpoint read
    # re-opens it — so a one-shot latch is spent on the FIRST satisfied turn: a live
    # run spent it on turn 1, eleven turns before the model actually closed, and the
    # model then wrote a flagless final message. Returning to outstanding work
    # clears the latch, so the envelope is stated again near the real close.
    assert '_contract_state["closing_advisory_delivered"] = False' in block
    # Bounded: never two statements on consecutive turns, because every statement
    # re-enters the transport context (a chained run would accumulate copies).
    assert '_contract_state.get("closing_advisory_turn")' in block
    assert "turn_count - int(_last_advisory_turn) > 1" in block
    # Exactly ONE copy, kept LAST in the context.
    assert "messages[:] = [" in block


def test_flag_clause_states_what_each_flag_MEANS(tmp_path, monkeypatch):
    """The clause listed the vocabulary but never its meanings, so the model had to
    tell ``partial`` from ``unverified`` out of the bare names — and the observed
    miss was a closing message carrying no flag line at all. Both surfaces that
    state the contract carry the definitions, because both compose from one clause.
    """
    monkeypatch.setattr(
        closing_message, "_SETTINGS_FILE", tmp_path / "app-settings.json",
    )
    policy = _policy(_enabled())
    assert policy["flags"] == DEFAULT_TAXONOMY
    for name, meaning in closing_message.FLAG_MEANINGS.items():
        for surface in (policy["advisory"], policy["instruction"]):
            assert f"{name} = {meaning}" in surface, (name, surface[:200])

    # A custom vocabulary glosses exactly what it names: a flag we have no meaning
    # for degrades to the bare list rather than to a definition that is not ours.
    (tmp_path / "app-settings.json").write_text(json.dumps({
        "gate_settings": {"closing_message": {"flags": "partial, somersault"}},
    }), encoding="utf-8")
    instance = closing_message.resolve(_enabled())["instance"]
    clause = closing_message._flag_clause(
        instance, gate_registry.gate_types()["closing_message"])
    assert "Valid flags: partial, somersault." in clause
    assert "partial = " in clause
    assert "somersault = " not in clause
    assert "needs_input = " not in clause


# ── The budget that survives a crash-resume (root cause B) ───────────────────

def test_closing_tally_is_stamped_durably_and_continues_after_a_resume():
    """The compliance tally lived only in ``_contract_state``, which dies with the
    run, so every crash-resume restarted the budget at attempt 1 and the gate never
    reached ``attempts_exhausted`` — it looped until the RESUME budget gave out
    instead of promoting its labelled PARTIAL. The folded draft row now carries the
    tally, and the gate seeds itself from the run's high-water mark."""
    marker = closing_message.attempt_marker(3, {"flag": 3, "notification": 1})
    assert marker == {closing_message.ATTEMPTS_KEY: {
        "total": 3, "per_requirement": {"flag": 3, "notification": 1},
    }}
    # The attempts marker must NOT collide with the promoted row's presentation
    # record — that key is what the chat UI and the DB viewer read.
    assert closing_message.ATTEMPTS_KEY != closing_message.META_KEY
    # A tally of zero is not worth a row mark; the loop only stamps a spent budget.
    assert closing_message.attempt_marker(0) == {
        closing_message.ATTEMPTS_KEY: {"total": 0, "per_requirement": {}}}

    # The durable read the loop seeds from.
    class _Db:
        async def closing_attempts_for_run(self, user_id, session_id):
            return {"total": 4, "per_requirement": {"flag": 4}}

    assert asyncio.run(closing_message.durable_attempts(_Db(), "u", "s")) == {
        "total": 4, "per_requirement": {"flag": 4}}

    # Best-effort by design: no probe, or a raising probe, reports None and the
    # run behaves exactly as it would have — the latch can never fail a run.
    assert asyncio.run(closing_message.durable_attempts(object(), "u", "s")) is None

    class _Boom:
        async def closing_attempts_for_run(self, user_id, session_id):
            raise RuntimeError("transcript unreadable")

    assert asyncio.run(closing_message.durable_attempts(_Boom(), "u", "s")) is None

    # The loop reads the durable tally at the gate, raises (never lowers) its
    # in-memory counters, and stamps the marker from the fold helper.
    source = _loop_source()
    assert "durable_attempts as _durable" in source
    assert "await _closing_prior_attempts()" in source
    assert '_closing_retries = max(' in source
    assert 'int(_prior_closing.get("total") or 0)' in source
    assert "closing_message import attempt_marker as _marker" in source
    # Both backends expose the probe: the base class's Python fallback keeps
    # Postgres in step with the SQLite override.
    from app.db.interface import StorageBackend
    from app.db.local import LocalBackend
    assert hasattr(StorageBackend, "closing_attempts_for_run")
    assert hasattr(LocalBackend, "closing_attempts_for_run")


async def _durable_tally_probe_reads_only_this_run(tmp_path, monkeypatch):
    """The probe must report THIS run's tally and nothing else: no tally before the
    run's user turn, the NEWEST fold when several were written, and nothing from an
    earlier run once a new user turn opens."""
    async def no_status(*args, **kwargs):
        return None

    from app.db.local import LocalBackend

    monkeypatch.setattr("app.db.local._emit_agent_run_status", no_status)
    db = LocalBackend(str(tmp_path / "closing.db"), seed=False)
    session_id = "closing-session"
    conn = db._get_conn()
    try:
        conn.execute(
            "INSERT INTO sessions (id, user_id, title, metadata) VALUES (?, ?, ?, ?)",
            (session_id, "user", "closing", "{}"),
        )
        conn.commit()
    finally:
        conn.close()

    # No user turn → no run boundary → nothing spent.
    assert await db.closing_attempts_for_run("user", session_id) is None

    await db.insert_interaction("user", session_id, role="user", content="go")
    # A fold that spent no closing budget carries no tally.
    await db.insert_interaction(
        "user", session_id, role="assistant", content="working…",
        metadata=json.dumps({"message_phase": "progress"}),
    )
    assert await db.closing_attempts_for_run("user", session_id) is None

    # Two rejected drafts: the NEWEST row is the high-water mark the resume reads.
    await db.insert_interaction(
        "user", session_id, role="assistant", content="draft 1",
        metadata=json.dumps({
            "message_phase": "progress",
            **closing_message.attempt_marker(1, {"flag": 1}),
        }),
    )
    draft_2_id = await db.insert_interaction(
        "user", session_id, role="assistant", content="draft 2",
        metadata=json.dumps({
            "message_phase": "progress",
            "closing_metadata_candidate": True,
            **closing_message.attempt_marker(2, {"flag": 2, "notification": 1}),
        }),
    )
    assert await db.closing_attempts_for_run("user", session_id) == {
        "total": 2,
        "per_requirement": {"flag": 2, "notification": 1},
        "metadata_candidate": {
            "interaction_id": str(draft_2_id),
            "content": "draft 2",
        },
    }

    # A NEW user turn opens a FRESH run: the old run's tally must not shrink the
    # new run's budget.
    await db.insert_interaction("user", session_id, role="user", content="again")
    assert await db.closing_attempts_for_run("user", session_id) is None


def test_durable_tally_probe_reads_only_this_run(tmp_path, monkeypatch):
    asyncio.run(_durable_tally_probe_reads_only_this_run(tmp_path, monkeypatch))

