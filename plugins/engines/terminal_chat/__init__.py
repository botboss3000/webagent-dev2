"""
Terminal Chat engine — package entry point.

Re-exports the drop-in contract (``ENGINE_ID`` + ``stream``) from the adapter
module so the generic discovery in ``plugins/engines/__init__.py`` finds this
engine by importing the folder. The heavy runtime lives in ``terminal_chat.py``.
"""

from .terminal_chat import ENGINE_ID, stream

__all__ = ["ENGINE_ID", "stream"]