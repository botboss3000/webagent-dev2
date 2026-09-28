"""Strict per-tool skill-file convention and delivery behavior."""

from pathlib import Path

from app import abilities
from app.tools.loader import ABILITY_TOOLS


def _root(ability_id: str) -> Path:
    path = abilities.skill_file_path(ability_id)
    assert path is not None, ability_id
    return path


def test_every_gated_ability_has_a_root_skill_file():
    missing = [aid for aid in ABILITY_TOOLS if not _root(aid).is_file()]
    assert not missing, f"missing root skill files: {missing}"


def test_every_gated_tool_has_a_nonempty_personal_skill_file():
    missing = []
    empty = []
    for aid, tools in ABILITY_TOOLS.items():
        for tool in tools:
            path = abilities.tool_skill_file_path(aid, tool)
            if path is None or not path.is_file():
                missing.append(f"{aid}/{tool}")
            elif not path.read_text(encoding="utf-8").strip():
                empty.append(str(path))
    assert not missing, "missing tool skill files: " + ", ".join(missing)
    assert not empty, "empty tool skill files: " + ", ".join(empty)


def test_zero_tool_abilities_need_no_tool_directory():
    for aid, tools in ABILITY_TOOLS.items():
        if tools:
            continue
        root = _root(aid)
        assert root.is_file(), aid


def test_root_skill_is_only_the_ability_lead():
    abilities.reload()
    feat = abilities.ability_feature_with_skill("user_files")
    assert feat
    root = _root("user_files").read_text(encoding="utf-8").strip()
    assert feat["skill"] == root
    assert feat["skill_full"] == root
    assert "Writes a file" not in root


def test_tool_file_resolves_by_exact_name_and_delivery_map():
    abilities.reload()
    path = abilities.tool_skill_file_path("user_files", "save_file")
    assert path and path.name == "save_file.skill.md"
    section = abilities.ability_tool_skill_section("user_files", "save_file")
    assert "Writes a file" in section
    assert abilities.ability_tool_skill_section("user_files", "not_a_tool") == ""


def test_payload_delivers_only_visible_tool_files():
    from app.agent.ability_skills import ability_skill_payload

    both = ability_skill_payload("user_files", ["save_file", "read_user_file"])
    assert both["body"]
    assert "Writes a file" in both["deferred"]
    assert "Reads back" in both["deferred"]
    only_save = ability_skill_payload("user_files", ["save_file"])["deferred"]
    assert "Writes a file" in only_save
    assert "Reads back" not in only_save


def test_unknown_tool_file_warning_is_safe_and_once(caplog):
    import logging

    entry = {"tools": ["save_file"]}
    sections = {"save_file": "ok", "typo_tool": "bad"}
    with caplog.at_level(logging.WARNING, logger="app.abilities"):
        abilities._warn_unknown_tool_sections("zz_file_fixture", entry, sections)
        abilities._warn_unknown_tool_sections("zz_file_fixture", entry, sections)
    messages = [record.getMessage() for record in caplog.records]
    assert len(messages) == 1
    assert "typo_tool" in messages[0]
