"""
Shared prompt loader for optimizer agents.

Reads markdown files from app/optimizer/prompts/<title>.md.
Returns "" if no file present — no hardcoded fallbacks.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_PROMPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts")


async def load_prompt(title: str) -> str:
    """Load an optimizer prompt by `title`. Returns "" if file not found."""
    safe = title.replace("/", "_").replace("\\", "_")
    path = os.path.join(_PROMPTS_DIR, f"{safe}.md")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        logger.debug("Optimizer prompt %s not found at %s", title, path)
        return ""
    except Exception as e:
        logger.warning("Failed to load optimizer prompt %s: %s", title, e)
        return ""
