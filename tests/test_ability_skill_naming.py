"""Filesystem guards for strict per-tool ability skills."""

from pathlib import Path
import re

from app import abilities
from app.tools.loader import ABILITY_TOOLS

_LEGACY = re.compile(r"^[ \t]*##[ \t]+tool[ \t]*:", re.IGNORECASE | re.MULTILINE)
_SUFFIX = ".skill.md"


def test_root_skills_contain_no_legacy_tool_headings():
    offenders = []
    for aid in ABILITY_TOOLS:
        root = abilities.skill_file_path(aid)
        if root and root.is_file() and _LEGACY.search(root.read_text(encoding="utf-8")):
            offenders.append(str(root))
    assert not offenders, "legacy ## tool: headings are forbidden: " + ", ".join(offenders)


def test_tool_file_names_match_exact_runtime_gated_tools():
    offenders = []
    for aid, tools in ABILITY_TOOLS.items():
        root = abilities.skill_file_path(aid)
        if root is None:
            continue
        directory = root.parent / "tools"
        if not directory.is_dir():
            continue
        allowed = set(tools)
        for path in directory.glob(f"*{_SUFFIX}"):
            name = path.name[:-len(_SUFFIX)]
            if name not in allowed:
                offenders.append(f"{path}: {name!r} is not gated by {aid}")
    assert not offenders, "tool files that can never be delivered:\n  " + "\n  ".join(offenders)


def test_each_tool_file_is_canonical_and_unique():
    problems = []
    for aid, tools in ABILITY_TOOLS.items():
        for tool in tools:
            path = abilities.tool_skill_file_path(aid, tool)
            if path is None or path.name != f"{tool}{_SUFFIX}":
                problems.append(f"{aid}/{tool}")
    assert not problems, "non-canonical tool skill paths: " + ", ".join(problems)
