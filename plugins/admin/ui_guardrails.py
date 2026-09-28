"""
Guardrails for UI-only filesystem access.

Blocks any path outside ui/ and any file extension that isn't .css or .html.
Sensitive UI pages (privacy, tos, admin-tools) are read-only.

To disable guardrails entirely: delete this file.
"""

import fnmatch
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Project root — resolved relative to this file (plugins/admin/../../)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# ── Allowed extensions for write/edit/delete ──
ALLOWED_WRITE_EXTENSIONS = {".css", ".html"}

# ── Allowed extensions for read/search ──
ALLOWED_READ_EXTENSIONS = {".css", ".html", ".js", ".json", ".svg", ".png", ".ico", ".webmanifest"}

# ── Read-only paths (can read, cannot write/edit/delete) ──
# fnmatch patterns relative to ui/
READ_ONLY_PATTERNS = [
    "privacy.html",
    "tos.html",
    "admin-tools/*",
    "admin-tools/**/*",
]


async def check_path(path: str, action: str = "read") -> None:
    """
    Check if a path is allowed for UI admin operations.

    Args:
        path: Absolute filesystem path
        action: 'read', 'write', or 'delete'

    Raises:
        PermissionError if the path is denied
    """
    p = Path(path).resolve()
    project_root = _PROJECT_ROOT.resolve()
    ui_dir = project_root / "ui"

    # Must be inside the project
    if not str(p).startswith(str(project_root)):
        raise PermissionError(
            f"Access denied: '{path}' is outside the project root. "
            f"UI admin can only modify files under ui/."
        )

    # Must be inside ui/ directory
    if not str(p).startswith(str(ui_dir)):
        raise PermissionError(
            f"Access denied: '{path}' is outside the ui/ directory. "
            f"UI admin can only modify files under ui/."
        )

    ext = p.suffix.lower()

    if action in ("write", "delete"):
        # Check extension
        if ext not in ALLOWED_WRITE_EXTENSIONS:
            raise PermissionError(
                f"Access denied: '{path}' has extension '{ext}'. "
                f"UI admin can only write .css and .html files."
            )

        # Check read-only patterns
        rel_path = p.relative_to(ui_dir)
        rel_str = str(rel_path.as_posix())
        for pattern in READ_ONLY_PATTERNS:
            if fnmatch.fnmatch(rel_str, pattern):
                raise PermissionError(
                    f"Access denied: '{rel_str}' is a read-only page. "
                    f"UI admin cannot modify this file."
                )

    elif action == "read":
        # For reads, allow a broader set of extensions
        if ext not in ALLOWED_READ_EXTENSIONS:
            raise PermissionError(
                f"Access denied: '{path}' has extension '{ext}'. "
                f"UI admin can only read .css, .html, .js, .json, .svg, .png files."
            )
