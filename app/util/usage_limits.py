"""Safe recognition and presentation data for OpenAI subscription usage limits."""
from __future__ import annotations
import re
from typing import Any, Dict, Optional
_USAGE_LIMIT_RE = re.compile(r'"type"\s*:\s*"usage_limit_reached"')
_RESET_RE = re.compile(r'"resets_at"\s*:\s*(\d+)')
def openai_usage_limit_metadata(error_text: str, provider: str = "") -> Optional[Dict[str, Any]]:
    """Return safe display metadata for an explicit OpenAI usage-limit error."""
    if provider != "openai_subscription" or not error_text or not _USAGE_LIMIT_RE.search(error_text): return None
    result: Dict[str, Any] = {"kind": "openai_usage_limit"}
    match = _RESET_RE.search(error_text)
    if match: result["resets_at"] = int(match.group(1))
    return result
