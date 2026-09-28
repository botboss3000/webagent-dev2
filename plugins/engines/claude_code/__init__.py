"""
Local Claude Code engine — package entry point.

Re-exports the drop-in contract (``ENGINE_ID`` + ``stream``) from the adapter
module so the generic discovery in ``plugins/engines/__init__.py`` finds this
engine by importing the folder. The heavy runtime lives in ``claude_code.py``;
its sign-in + skills siblings live alongside it (``claude_code_login.py`` /
``claude_code_skills.py``) and are imported directly by the thin core HTTP
wrappers ``app/api/claude_auth.py`` and ``app/api/claude_skills.py``.
"""

from .claude_code import ENGINE_ID, stream, compact_restart

__all__ = ["ENGINE_ID", "stream", "compact_restart"]
