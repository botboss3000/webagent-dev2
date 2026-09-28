"""
Shared alert utilities — persist user-facing system messages for critical events
that would otherwise be invisible (buried in server logs).

Keep this module free of heavy imports so any module can use it without cycles.
"""

import logging

logger = logging.getLogger(__name__)

# Rate-limiter: one 402 alert per session (in-memory; cleared on server restart).
_402_ALERTED_SESSIONS: set = set()

# Provider billing/credit failures arrive in two shapes: OpenAI-style HTTP 402
# "Insufficient Balance", and gateway/proxy 400s such as Abacus RouteLLM's
# "You have no remaining credits to use the LLM apis." Match both families so
# the user-facing alert fires on every "the provider account is out of money"
# variant, not just the 402 spelling.
_CREDIT_MARKERS = (
    "402",
    "insufficient balance",
    "remaining credits",
    "no credits",
    "out of credits",
    "insufficient credits",
    "not enough credits",
)
# Markers that indicate the account is out of CREDITS specifically (vs. a plain
# 402 balance error) — these get the friendlier "add credits" message.
_CREDIT_OUT_MARKERS = (
    "remaining credits",
    "no credits",
    "out of credits",
    "insufficient credits",
    "not enough credits",
)


def is_provider_credit_error(error_text: str) -> bool:
    """True when ``error_text`` is a provider billing failure — the account the
    configured API key belongs to has no credits/balance left. Shared by the
    alert utility, the agent loop and the task classifier so every detection
    site agrees on what counts."""
    if not error_text:
        return False
    low = error_text.lower()
    return any(m in low for m in _CREDIT_MARKERS)


def _identity_part(provider_model: str, provider_name: str) -> str:
    """Human-readable '(provider, model)' suffix for alert messages. Both fields
    are optional: the provider name (e.g. 'abacus') is the account that is out of
    credits, the model (e.g. 'gemini-3.1-flash-lite') is what was being called."""
    model = (provider_model or "").strip()
    provider = (provider_name or "").strip()
    if provider and model:
        return f" (provider: {provider}, model: {model})"
    if model:
        return f" (provider model: {model})"
    if provider:
        return f" (provider: {provider})"
    return ""


async def persist_402_alert(error_text: str, user_id: str, session_id: str,
                            provider_model: str = "", provider_name: str = "") -> bool:
    """If ``error_text`` is a provider credit/billing error, persist a visible
    system:error message in the given session. Rate-limited to one alert per
    session.

    Args:
      provider_model: the model name that failed (e.g. 'gemini-3.1-flash-lite').
      provider_name:  the provider/account that owns it (e.g. 'abacus') — the
                      thing the user actually needs to top up.

    Returns True when an alert was persisted, False otherwise.
    """
    # Only fire on actual billing errors (402 balance OR gateway "no credits").
    if not is_provider_credit_error(error_text):
        return False

    # Rate-limit.
    if session_id in _402_ALERTED_SESSIONS:
        return False
    _402_ALERTED_SESSIONS.add(session_id)

    id_part = _identity_part(provider_model, provider_name)
    low = error_text.lower()
    if any(m in low for m in _CREDIT_OUT_MARKERS):
        msg = (
            f"⚠️ The AI provider is out of credits — model calls are failing{id_part}. "
            f"Add credits to the provider account (billing) to restore service."
        )
    else:
        msg = f"Error 402: Insufficient Balance{id_part}."

    try:
        from app.db import get_db
        db = get_db()
        # Allocate a session_seq: transcript fetches order by session_seq and the
        # live reconcile poll filters "session_seq IS NOT NULL", so a seq-less row
        # would exist in the DB but never reach the chat UI (invisible alert).
        seq = None
        try:
            seq = await db.next_session_seq(session_id)
        except Exception:
            seq = None
        await db.insert_interaction(
            user_id=user_id,
            session_id=session_id,
            role="system",
            content=msg,
            source="system:error",
            session_seq=seq,
        )
        logger.info("402 alert persisted for session %s (provider: %s)", session_id, provider_model)
        return True
    except Exception:
        return False
