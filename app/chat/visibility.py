"""Local main-message publishing protocol for working-agent visibility.

Ordinary non-final assistant text is activity content.  An agent asks for a
standalone working notice only through the reserved local function defined
here; the loop consumes that call without exposing it as a user tool.

The legacy leading-marker helpers remain for recovery of already in-flight
responses from earlier versions.  New prompt contracts never instruct models
to emit a marker.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Optional


MAIN_MESSAGE_TOOL_NAME = "publish_main_message"

_MARKER = re.compile(
    r"\A[ \t\r\n]*<webagent:visibility[ \t]+(internal|main)[ \t]*>[ \t\r\n]*",
    re.IGNORECASE,
)
_CONTROL_PREFIX = re.compile(
    r"\A[ \t\r\n]*<webagent:visibility\b[^>]{0,160}>[ \t\r\n]*",
    re.IGNORECASE,
)


def main_message_tool_definition() -> dict[str, Any]:
    """Return the provider-only schema for one clean working notice."""
    return {
        "type": "function",
        "function": {
            "name": MAIN_MESSAGE_TOOL_NAME,
            "description": (
                "Publish one concise user-visible working update as a normal assistant "
                "bubble, then continue the task. Use only when the user should see a "
                "mid-run update; ordinary working text stays internal automatically."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "The clean, user-visible working update.",
                    },
                },
                "required": ["text"],
                "additionalProperties": False,
            },
        },
    }


def main_message_text(arguments: Any) -> str:
    """Return validated visible text or an empty string for a malformed control call."""
    if not isinstance(arguments, dict) or set(arguments) != {"text"}:
        return ""
    text = arguments["text"]
    if not isinstance(text, str) or len(text) > 100000:
        return ""
    return text.strip()


@dataclass(frozen=True)
class VisibilityDecision:
    """Legacy leading-marker classification for already in-flight responses."""

    content: str
    main: bool = False
    marker_seen: bool = False


def enabled(agent_rec: Optional[dict]) -> bool:
    """Return the effective all-mode setting, failing closed only on registry failure."""
    try:
        from app.agent.gate_registry import enabled_instance
        return enabled_instance(agent_rec, "message_visibility", mode=None) is not None
    except Exception:
        return False


def classify(content: str, *, active: bool) -> VisibilityDecision:
    """Legacy compatibility parser; new runs use ``publish_main_message`` instead."""
    text = str(content or "")
    if not active:
        return VisibilityDecision(content=text)
    match = _MARKER.match(text)
    if match:
        return VisibilityDecision(
            content=text[match.end():],
            main=match.group(1).lower() == "main",
            marker_seen=True,
        )
    malformed = _CONTROL_PREFIX.match(text)
    if malformed:
        return VisibilityDecision(content=text[malformed.end():], marker_seen=True)
    return VisibilityDecision(content=text)


def leading_marker_pending(content: str, *, active: bool) -> bool:
    """Legacy compatibility helper for streams started under the old prompt."""
    if not active:
        return False
    candidate = str(content or "").lstrip()
    if not candidate:
        return True
    protocols = (
        "<webagent:visibility internal>",
        "<webagent:visibility main>",
    )
    return any(protocol.startswith(candidate.lower()) for protocol in protocols)
