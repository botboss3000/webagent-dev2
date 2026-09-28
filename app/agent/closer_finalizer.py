"""Provider-capped finalization for the Closing Message token requirement.

The working agent still does the work and authors the first draft.  When that
draft exceeds a configured Closing Message ``max_tokens`` value, this module
runs one separate, tool-free rewrite call.  Its provider completion cap is the
configured value, unlike the ordinary working loop's per-turn Runtime limit.
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional

from app.agent.provider_completion import complete_text, completion_answer

_FINALIZER_TIMEOUT_SECONDS = 60.0


def messages_for_finalizer(draft: str, policy: Mapping[str, Any]) -> List[Dict[str, str]]:
    """Return the isolated finalizer prompt for one rejected closing draft."""
    limit = int(policy.get("max_tokens") or 0)
    instruction = str(policy.get("instruction") or "").strip()
    rejection = str(policy.get("rejection_message") or "").strip()
    system = "\n\n".join(part for part in (
        "You are the final-message formatter. Rewrite the supplied draft into "
        "the complete response the user should receive. Return only that final "
        "response: do not discuss this rewrite, call tools, or add reasoning.",
        f"The provider enforces a hard maximum of {limit} output tokens. "
        "Fit all essential facts, the required FLAG line, and the short status "
        "line within that limit. Do not intentionally leave the response partial.",
        instruction,
        rejection,
    ) if part)
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "Rejected working draft:\n\n" + str(draft or "")},
    ]


async def finalize(
    client: Any,
    *,
    model: str,
    provider: str,
    base_url: str,
    draft: str,
    policy: Mapping[str, Any],
) -> Optional[str]:
    """Return a provider-capped final rewrite, or ``None`` on an unusable call.

    ``strict_max_tokens`` is essential: normal background completions may expand
    an empty length-stopped request for resilience, but that would violate this
    finalizer's hard-cap contract.
    """
    limit = int(policy.get("max_tokens") or 0)
    if limit <= 0:
        return None
    try:
        response = await complete_text(
            client,
            model=model,
            messages=messages_for_finalizer(draft, policy),
            max_tokens=limit,
            temperature=0.0,
            timeout=_FINALIZER_TIMEOUT_SECONDS,
            provider=provider,
            base_url=base_url,
            strict_max_tokens=True,
        )
    except Exception:
        return None
    content = completion_answer(response)
    return content or None
