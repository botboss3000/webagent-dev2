
import logging
import json
import asyncio
from dataclasses import dataclass

logger = logging.getLogger(__name__)

@dataclass
class ToolError:
    error_type: str          # One of: "validation_error", "runtime_error", "timeout", "auth_error", "rate_limit", "not_found", "internal_error"
    tool_name: str
    message: str             # Human-readable summary
    recoverable: bool        # Can the LLM fix this and retry?
    retry_hint: str          # Guidance for the LLM on what to change

def classify_tool_error(e: Exception, tool_name: str, args: dict) -> ToolError:
    """
    Classifies a tool error into a ToolError object.
    """
    message = str(e)
    if isinstance(e, (KeyError, TypeError)):
        error_type = "validation_error"
        recoverable = True
        retry_hint = "Check required parameters and types"
    elif isinstance(e, (TimeoutError, asyncio.TimeoutError)):
        error_type = "timeout"
        recoverable = True
        retry_hint = "The tool timed out. Try with a smaller scope."
    elif "429" in message or "rate limit" in message.lower():
        error_type = "rate_limit"
        recoverable = True
        retry_hint = "Rate limited. Wait a moment, then retry."
    elif "401" in message or "403" in message or "unauthorized" in message.lower():
        error_type = "auth_error"
        recoverable = False
        retry_hint = "Authentication required. Ask the user to set up credentials."
    elif "404" in message:
        error_type = "not_found"
        recoverable = True
        retry_hint = "The resource was not found. Check the path/URL."
    elif isinstance(e, FileNotFoundError):
        error_type = "not_found"
        recoverable = True
        retry_hint = "File not found. Check the path."
    else:
        error_type = "runtime_error"
        recoverable = True  # default
        retry_hint = "An unexpected error occurred. Try a different approach."

    return ToolError(
        error_type=error_type,
        tool_name=tool_name,
        message=message,
        recoverable=recoverable,
        retry_hint=retry_hint,
    )
