"""
UI Admin tools — file editing confined to the front-end (ui/, .css/.html).

Wires the existing rich source tools (read / write / edit / patch / delete /
search / list directory) behind `plugins/admin/ui_guardrails.py` so a "UI Admin"
agent can alter visual details only — never backend code, and never the shell,
Python, git, or server restart.

How it works: `inject_source_tools` builds the full filesystem toolset into a
throwaway dict; we re-expose only the file tools, each wrapped with a
`ui_guardrails.check_path` pre-check on its `path` argument. The guardrail is the
security boundary (project-root → ui/ → extension allow-list); the wrapper just
adds a sensible default of `ui/` for the search/list tools.

Gated by the `ui_admin` ability (see app/tools/loader.py). Delete this file (or
plugins/admin/ui_guardrails.py) to remove the capability.
"""

import logging
from pathlib import Path

from app.tools.loader import ToolInfo

logger = logging.getLogger(__name__)

# Project root — resolved relative to this file (plugins/admin/../../)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# File tools re-exposed for UI admin (everything except run_command, run_python,
# restart_server, git_tool, resolve_conflict — those are intentionally dropped).
_UI_FILE_TOOLS = (
    "read_source", "write_source", "edit_source", "patch_source",
    "delete_source", "search_source", "read_directory",
)

# Tools whose `path` should default to the ui/ root when omitted or given as the
# project root ("."), so a UI-admin agent doesn't get denied for a broad default.
_DEFAULT_TO_UI = ("search_source", "read_directory")

# Which guardrail action each tool maps to.
_WRITE_TOOLS = {"write_source", "edit_source", "patch_source"}


def _resolve(path: str) -> str:
    """Resolve a (possibly relative) path to an absolute string under the project."""
    p = Path(path or "ui")
    if not p.is_absolute():
        p = _PROJECT_ROOT / p
    return str(p.resolve())


def inject_ui_tools(tools: dict, user_id: str) -> None:
    """Inject UI-confined filesystem tools into the tools dict."""
    try:
        from plugins.admin.source_tools import inject_source_tools
        from plugins.admin import ui_guardrails
    except Exception as e:  # pragma: no cover — source tools / guardrails absent
        logger.warning("UI admin tools unavailable: %s", e)
        return

    tmp: dict = {}
    try:
        inject_source_tools(tmp, user_id)
    except Exception as e:
        logger.warning("UI admin: could not build base source tools: %s", e)
        return

    def _wrap(name: str, info: ToolInfo) -> ToolInfo:
        base_handler = info.handler
        action = "delete" if name == "delete_source" else ("write" if name in _WRITE_TOOLS else "read")

        async def _guarded(**kwargs):
            raw = kwargs.get("path", "")
            if (not raw or raw in (".", "./")) and name in _DEFAULT_TO_UI:
                raw = "ui"
                kwargs["path"] = raw
            try:
                await ui_guardrails.check_path(_resolve(raw), action)
            except PermissionError as e:
                return f"Error: {e}"
            return await base_handler(**kwargs)

        return ToolInfo(
            name=name,
            handler=_guarded,
            parameters=info.parameters,
            requires_confirmation=info.requires_confirmation,
            destructive=info.destructive,
        )

    for name in _UI_FILE_TOOLS:
        info = tmp.get(name)
        if info is not None:
            tools[name] = _wrap(name, info)
