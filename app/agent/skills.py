"""
On-demand agent skills — shared helpers.

A **skill** is a named knowledge pack with three authored parts: a `name`, a
`description` of when to use it (always shown to the agent), and a `body` (the
full how-to). Each skill is either:

  - **always_on**  — its body is injected into the system prompt every turn, or
  - **selectable** — only its name + description are shown; the body is replaced
    by a placeholder until the agent pulls it in with the `load_skill` tool.

**Storage:** skills live in the prompt-slot tables, not in agent metadata. Each
agent has a single `__skills__` slot (in `agent_prompt_templates` for the
seed/template, cloned into `agent_prompts` per agent) whose `content` is a JSON
array of skill objects. This rides the existing slot seeding / cloning / admin
machinery. The DB layer (`get_agent_skills` / `set_agent_skills`) reads/writes
that slot; this module only deals with the parsed skill objects + the strings
that the prompt-builder, tools, and endpoints share.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from app.context.md_seeder import normalize_skills

# Slot name that holds the agent's skills (JSON array) in the prompt-slot tables.
SKILLS_SLOT_NAME = "__skills__"

# Key under which the active-skill name list is stored in a session's metadata.
SESSION_ACTIVE_SKILLS_KEY = "active_skills"


def parse_skills_content(content: Optional[str]) -> List[Dict[str, Any]]:
    """Parse the JSON `content` of a `__skills__` slot into normalized skills."""
    if not content or not content.strip():
        return []
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return []
    return normalize_skills(data)


def skills_to_content(skills: Any) -> str:
    """Serialize a skills list to the `__skills__` slot content (normalized)."""
    return json.dumps(normalize_skills(skills))


def loaded_result_text(skill: Dict[str, Any]) -> str:
    """The text `load_skill` returns (and persists) when a skill is loaded.

    Begins with a stable header line keyed by skill name so the deactivate
    path can find and neutralize this row later via a prefix match.
    """
    body = (skill.get("body") or "").strip()
    return (
        f'SKILL "{skill["name"]}" LOADED — its full instructions follow and stay '
        f"active for the rest of this conversation:\n\n{body}"
    )


def loaded_result_prefix(name: str) -> str:
    """Stable prefix used to match a skill's load row for neutralization."""
    return f'SKILL "{name}" LOADED'


def deactivated_text(name: str) -> str:
    """Replacement content written over a load row when the user deactivates it."""
    return (
        f'[Skill "{name}" was deactivated by the user and is no longer in '
        f"context. Call load_skill again if it becomes relevant.]"
    )


def format_skills_section(
    skills: List[Dict[str, Any]],
    active_names: Optional[List[str]] = None,
) -> str:
    """Build the `# [SKILLS]` system-prompt block.

    For EVERY enabled skill the name + description are always shown. The body is
    shown only when the skill is always_on or currently loaded; for a selectable
    skill that isn't loaded yet, a placeholder tells the agent to call
    load_skill. This way authors never hand-write the catalog — it's generated
    from the skill definitions, and the heavy body text stays out of the prompt
    until it's actually needed.

    Returns "" when there are no enabled skills.
    """
    active = {n for n in (active_names or [])}
    enabled = [s for s in skills if s.get("enabled", True)]
    if not enabled:
        return ""

    lines: List[str] = [
        "# [SKILLS]",
        "You have the skills below. Each lists when to use it. For a skill marked "
        "“load on demand”, call `load_skill` with its name to pull its full "
        "instructions into the conversation the moment a task needs it — don't "
        "guess at the steps before loading it.",
    ]

    for s in enabled:
        # `token` is what load_skill / active-tracking key on: the handle for an
        # ability skill, otherwise the name. `display` is the human heading.
        token = s.get("handle") or s["name"]
        display = s.get("display_name") or s["name"]
        desc = (s.get("description") or "").strip()
        suffix = "  _(ability skill)_" if s.get("_ability") else ""
        lines.append(f"\n## {display}{suffix}")
        if desc:
            lines.append(desc)

        # "visible" (legacy always_on) → body shown every turn; "discoverable"
        # (legacy selectable) → placeholder until loaded. Normalized so the unified
        # vocabulary and the old skill modes both work.
        from app.tools.tool_modes import normalize_visibility as _nv, VISIBLE as _VIS
        if _nv(s.get("mode")) == _VIS:
            body = (s.get("body") or "").strip()
            if body:
                lines.append(body)
        elif token in active:
            lines.append("_(Loaded — the full instructions are in the conversation above.)_")
        else:
            lines.append(
                f'_(Load on demand — this skill’s full instructions are only '
                f'visible after you call load_skill("{token}").)_'
            )

    return "\n".join(lines).strip()
