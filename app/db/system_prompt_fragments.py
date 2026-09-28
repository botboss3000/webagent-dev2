"""
Load system-prompt instructional fragments from app/defaults/app-prompts.json.

Each fragment lives under ``runtime_fragments.fragments`` as a
``{key: {purpose: ..., text: ...}}`` object. Callers get a flat
``{key: text}`` dict so existing code doesn't change.

If the JSON is missing or unparseable, returns ``{}`` — callers treat
all fragments as optional.
"""

from __future__ import annotations

import json
from typing import Dict, Optional

from app.util.paths import app_prompts_path

_cache: Optional[Dict[str, str]] = None


def _load_from_json() -> Dict[str, str]:
    _path = app_prompts_path()
    if not _path.is_file():
        return {}
    try:
        data = json.loads(_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    fragments = data.get("runtime_fragments", {}).get("fragments", {})
    out: Dict[str, str] = {}
    for key, entry in fragments.items():
        if isinstance(entry, dict):
            text = entry.get("text", "")
            if text and isinstance(text, str):
                out[key] = text.strip()
        elif isinstance(entry, str):
            out[key] = entry.strip()
    return out


def format_tool_subheadings_markdown(raw: str) -> str:
    """Fragment bodies use ### for tool lines so they are not parsed as top-level ## sections."""
    return raw.strip().replace("### ", "## ")


def get_prompt_fragments() -> Dict[str, str]:
    """Return fragments parsed from app-prompts.json (possibly empty)."""
    global _cache
    if _cache is None:
        _cache = _load_from_json()
    return _cache


def clear_prompt_fragments_cache() -> None:
    """Reload app-prompts.json on next get_prompt_fragments() call."""
    global _cache
    _cache = None
