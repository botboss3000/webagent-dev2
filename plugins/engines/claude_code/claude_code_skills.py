"""
Local Claude Code skills — read/write the SKILL.md files the local `claude` CLI
discovers, so the Claude agent's card can browse and edit them.

A "skill" is a folder holding a SKILL.md with simple frontmatter (name +
description) and a Markdown body. The CLI looks in two project-relevant places
this module manages (chosen to match the user's "personal + this agent's folder"
request — plugin skills are intentionally NOT touched here):
  • personal — ~/.claude/skills/<slug>/SKILL.md   (available in every project)
  • project  — <agent working folder>/.claude/skills/<slug>/SKILL.md

The working folder is resolved the SAME way the engine resolves it
(plugins/engines/claude_code/claude_code.py:_read_cfg → the configured folder,
else the app's project root), so "project" here points at the exact folder the
running `claude` would read from.

Used only by app/api/claude_skills.py (the HTTP routes the Skills tab calls);
admin-gated at the route layer, same as the rest of the engine.
Breadcrumbs: plugins/engines/claude_code/claude_code.py (runs `claude`),
plugins/engines/claude_code/claude_code_login.py (the sign-in sibling),
ui/main-panel/agents/js/claude-skills.js (the Skills tab UI).
REMOVE-WHEN: the Local Claude Code engine (claude_code) is dropped.
"""

import os
import re
import shutil
from typing import Any, Dict, List, Optional, Tuple

# Folder/slug for a skill: a leading alnum then alnum / dot / dash / underscore.
# Deliberately strict so a slug can never contain a path separator or "..".
_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# The two sources the Skills tab manages, in display order.
SOURCES = ("personal", "project")


# ── Location resolution ─────────────────────────────────────────────────────

def _read_folder(agent_rec: Optional[Dict[str, Any]]) -> str:
    """The agent's working folder — same resolution the engine uses, so the
    'project' skills dir is the one the running `claude` actually reads."""
    try:
        from .claude_code import _read_cfg
        folder = (_read_cfg(agent_rec).get("folder") or "").strip()
    except Exception:
        folder = ""
    if not folder:
        try:
            from app.util.paths import project_root
            folder = project_root()
        except Exception:
            folder = os.getcwd()
    return folder


def _dirs(agent_rec: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """{source: absolute skills directory} for the two managed sources."""
    home = os.path.join(os.path.expanduser("~"), ".claude", "skills")
    project = os.path.join(_read_folder(agent_rec), ".claude", "skills")
    return {"personal": home, "project": project}


def dir_labels(agent_rec: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """The resolved directories, for the UI to show where each source lives."""
    return _dirs(agent_rec)


def _safe_slug(slug: Optional[str]) -> Optional[str]:
    s = (slug or "").strip()
    if not s or s in (".", "..") or not _SLUG_RE.match(s):
        return None
    return s


def _slugify(name: str) -> Optional[str]:
    """Derive a folder slug from a display name (kebab-case, sanitised)."""
    s = (name or "").strip().lower()
    s = re.sub(r"[^a-z0-9._-]+", "-", s).strip("-._")
    return _safe_slug(s[:64])


def _within(base: str, target: str) -> bool:
    """True when `target` resolves inside `base` (defence-in-depth over the slug
    regex, which already forbids separators/'..')."""
    try:
        base_r = os.path.realpath(base)
        tgt_r = os.path.realpath(target)
        return os.path.commonpath([base_r, tgt_r]) == base_r and tgt_r != base_r
    except ValueError:
        # commonpath raises across drives on Windows → not contained.
        return False


# ── SKILL.md (de)serialisation ──────────────────────────────────────────────

def _yaml_dq(v: str) -> str:
    """A double-quoted YAML scalar, single-line, with quotes/backslashes escaped
    — robust for names/descriptions that contain ':' or '#'."""
    one = " ".join((v or "").splitlines()).strip()
    return '"' + one.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        inner = v[1:-1]
        if v[0] == '"':
            inner = inner.replace('\\"', '"').replace("\\\\", "\\")
        return inner
    return v


def _parse(text: str) -> Tuple[Dict[str, str], str]:
    """Split a leading '---' frontmatter block (simple `key: value` lines) from
    the Markdown body. Tolerant: no frontmatter → ({}, whole text)."""
    if text.startswith("﻿"):
        text = text[1:]
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        end = None
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                end = i
                break
        if end is not None:
            fm: Dict[str, str] = {}
            for ln in lines[1:end]:
                if ":" in ln:
                    k, val = ln.split(":", 1)
                    fm[k.strip().lower()] = _unquote(val)
            body = "\n".join(lines[end + 1:])
            return fm, body.lstrip("\n")
    return {}, text


def _compose(name: str, description: str, body: str) -> str:
    fm = "---\nname: {}\ndescription: {}\n---\n\n".format(
        _yaml_dq(name), _yaml_dq(description))
    return fm + (body or "").lstrip("\n")


# ── Read ────────────────────────────────────────────────────────────────────

def _read_one(source: str, base: str, slug: str) -> Optional[Dict[str, Any]]:
    safe = _safe_slug(slug)
    if not safe:
        return None
    md = os.path.join(base, safe, "SKILL.md")
    if not os.path.isfile(md):
        return None
    try:
        with open(md, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return None
    fm, body = _parse(text)
    return {
        "source": source,
        "slug": safe,
        "name": fm.get("name") or safe,
        "description": fm.get("description") or "",
        "body": body,
    }


def list_skills(agent_rec: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Every readable skill across the two sources, personal first."""
    out: List[Dict[str, Any]] = []
    dirs = _dirs(agent_rec)
    for source in SOURCES:
        base = dirs[source]
        if not os.path.isdir(base):
            continue
        try:
            slugs = sorted(os.listdir(base), key=str.lower)
        except OSError:
            continue
        for slug in slugs:
            sk = _read_one(source, base, slug)
            if sk:
                out.append(sk)
    return out


# ── Write / delete ──────────────────────────────────────────────────────────

def save_skill(agent_rec: Optional[Dict[str, Any]], source: str,
               slug: Optional[str], name: str, description: str,
               body: str) -> Dict[str, Any]:
    """Create or update a skill's SKILL.md and return the saved record.

    `slug` set → update that skill (the folder is never renamed, even if the
    display name changes). `slug` empty → derive a slug from the name and create
    a new skill folder. Raises ValueError on bad input (surfaced as HTTP 400)."""
    dirs = _dirs(agent_rec)
    if source not in dirs:
        raise ValueError("Unknown skill source.")
    base = dirs[source]

    final_slug = _safe_slug(slug) if slug else _slugify(name)
    if not final_slug:
        raise ValueError("Give the skill a name using letters, numbers or dashes.")

    skill_dir = os.path.join(base, final_slug)
    if not _within(base, skill_dir):
        raise ValueError("Invalid skill location.")

    os.makedirs(skill_dir, exist_ok=True)
    with open(os.path.join(skill_dir, "SKILL.md"), "w", encoding="utf-8") as f:
        f.write(_compose(name or final_slug, description, body))

    saved = _read_one(source, base, final_slug)
    if not saved:
        raise ValueError("Saved the skill but couldn't read it back.")
    return saved


def delete_skill(agent_rec: Optional[Dict[str, Any]], source: str,
                 slug: str) -> bool:
    """Remove a skill's whole folder. Returns False if it wasn't there
    (idempotent). Raises ValueError on bad input."""
    dirs = _dirs(agent_rec)
    if source not in dirs:
        raise ValueError("Unknown skill source.")
    base = dirs[source]
    safe = _safe_slug(slug)
    if not safe:
        raise ValueError("Invalid skill name.")
    skill_dir = os.path.join(base, safe)
    if not _within(base, skill_dir):
        raise ValueError("Invalid skill location.")
    if not os.path.isfile(os.path.join(skill_dir, "SKILL.md")):
        return False
    shutil.rmtree(skill_dir, ignore_errors=True)
    return True
