"""
Guardrails for filesystem access.

Blocks dangerous paths so the agent can't read/write/delete sensitive files.
Update DENIED_PATTERNS to add or remove restrictions.

To disable guardrails entirely: delete this file. source.py falls through
to unrestricted access when guardrails.py is missing.
"""

import fnmatch
import logging

logger = logging.getLogger(__name__)

# ── Denied path patterns (fnmatch syntax) ─────────────────────────────────────
# These apply to read, write, edit, and delete operations.
DENIED_PATTERNS = [
    # Environment files — contain API keys, secrets, DB passwords
    ".env",
    ".env.*",
    "*.env",
    # Shell configs — contain tokens, history
    ".bash_history",
    ".zsh_history",
    ".gitconfig",
    # SSH keys
    ".ssh/*",
    ".ssh/**/*",
]

# ── Denied command patterns (substring match on shell commands) ───────────────
DENIED_COMMANDS = [
    "rm -rf /",
    "rm -rf ~",
    "rm -rf --no-preserve-root",
    ":(){ :|:& };:",  # fork bomb
]


def _deny(action: str, path: str, pattern: str) -> None:
    """Log and raise for a denied path. Centralised so every match branch in
    check_path reports identically."""
    logger.warning("Blocked %s on denied path: %s (matched: %s)", action, path, pattern)
    raise PermissionError(
        f"Access denied: '{path}' matches restricted pattern '{pattern}'. "
        f"This path contains sensitive information."
    )


async def check_path(path: str, action: str = "read") -> None:
    """
    Check if a path is allowed. Raises PermissionError if denied.
    Called by source.py endpoints before every read/write/delete operation.

    Args:
        path: Absolute filesystem path
        action: 'read', 'write', or 'delete'

    Raises:
        PermissionError if the path matches a denied pattern
    """
    import os
    normalized = path.replace("\\", "/")
    basename = os.path.basename(normalized)
    # Path segments (drop the drive letter / empty leading segment) so a pattern
    # like ".ssh/*" can match an ABSOLUTE path. fnmatch on the whole absolute path
    # only ever matched relative inputs, so directory patterns such as ".ssh/*"
    # and ".ssh/**/*" were dead against the absolute paths the source tools pass
    # in — SSH keys slipped through. We now also test the trailing path against
    # every "<dir>/" pattern and check each segment by name.
    segments = [s for s in normalized.split("/") if s and ":" not in s]

    for pattern in DENIED_PATTERNS:
        # 1) Whole-path and basename match (catches ".env", "*.env", ".gitconfig").
        if fnmatch.fnmatch(normalized, pattern) or fnmatch.fnmatch(basename, pattern):
            _deny(action, path, pattern)
        # 2) Any single path segment matches the pattern's leading component —
        #    catches a denied DIRECTORY anywhere in an absolute path, e.g.
        #    ".ssh/*" / ".ssh/**/*" → block anything under a ".ssh" segment.
        lead = pattern.split("/", 1)[0]
        if lead and lead not in ("", "*", "**") and any(
            fnmatch.fnmatch(seg, lead) for seg in segments
        ):
            _deny(action, path, pattern)
        # 3) Suffix match on "dir/<glob>" patterns against the tail of the path,
        #    so ".ssh/config" style explicit patterns still match absolute paths.
        if "/" in pattern and fnmatch.fnmatch(normalized, "*/" + pattern):
            _deny(action, path, pattern)


async def check_command(command: str) -> None:
    """
    Check if a shell command is allowed. Raises PermissionError if denied.

    Args:
        command: Shell command string

    Raises:
        PermissionError if the command matches a denied pattern
    """
    for pattern in DENIED_COMMANDS:
        if pattern in command:
            logger.warning("Blocked denied command: %s", command[:100])
            raise PermissionError(
                f"Command blocked: matches restricted pattern '{pattern}'"
            )
