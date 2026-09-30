"""Provider (LLM) error classification and the recovery action it implies.

Why this module exists
----------------------
DeepSeek requires that an assistant turn produced in thinking mode carries its
reasoning back on the next request.  When we violated that, the provider answered
HTTP 400 and the loop decided what to do by matching the *wording* of the message:

    "reasoning_content" in err and "passed back" in err

That predicate was written for the Chat Completions transport, where the field is
called ``reasoning_content``.  The Responses transport names the same field
``reasoning_text``, so the predicate never fired: a deterministic client error
became a crash, and the crash-recovery loop replayed three byte-identical
requests before the session was marked failed.

Two rules follow, and both are encoded here:

1. Classify on structural evidence — HTTP status, the provider's own error
   ``type``/``code``, and what the endpoint declared about itself — never on one
   provider's prose.
2. A deterministic 4xx must not be retried with an unchanged request.  Retrying
   is only meaningful when the retry differs from the request that failed, which
   ``identical_retry_ok`` records.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

from app.agent.response_chaining import (
    is_chain_resume_rejected,
    is_missing_response_error,
    is_unsupported_deepseek_responses_error,
    is_unsupported_responses_error,
)


class Kind(str, Enum):
    THINKING_REPLAY_REJECTED = "thinking_replay_rejected"
    ENCRYPTED_HISTORY_REJECTED = "encrypted_history_rejected"
    TOOL_CHOICE_REJECTED = "tool_choice_rejected"
    STALE_CHAIN = "stale_chain"
    MODEL_SCOPED_REJECTION = "model_scoped_rejection"
    UNSUPPORTED_TRANSPORT = "unsupported_transport"
    QUOTA = "quota"
    AUTH = "auth"
    TRANSIENT = "transient"
    UNKNOWN_CLIENT = "unknown_client"
    UNKNOWN = "unknown"


class Action(str, Enum):
    #: Rebuild the outbound history so every replayed turn carries what the
    #: endpoint requires, then resend on the same transport.
    REBUILD_REPLAYABLE_HISTORY = "rebuild_replayable_history"
    REBASE_HISTORY = "rebase_history"
    DROP_FORCED_TOOL_CHOICE = "drop_forced_tool_choice"
    FALLBACK_TRANSPORT = "fallback_transport"
    REBASE_CHAIN = "rebase_chain"
    TERMINAL = "terminal"
    RETRY_BACKOFF = "retry_backoff"
    PROPAGATE = "propagate"


@dataclass(frozen=True)
class ProviderErrorInfo:
    kind: Kind
    action: Action
    status: Optional[int]
    codes: str
    #: False for deterministic client errors: replaying an unchanged request is
    #: guaranteed to fail again, so recovery must change the request or stop.
    identical_retry_ok: bool
    detail: str = ""

    @property
    def is_terminal(self) -> bool:
        return self.action is Action.TERMINAL

    def describe(self) -> str:
        return "kind=%s action=%s status=%s codes=%s identical_retry_ok=%s" % (
            self.kind.value, self.action.value, self.status, self.codes or "-",
            self.identical_retry_ok,
        )


_STATUS_RE = re.compile(r"\bhttp[/ ]?(\d{3})\b", re.IGNORECASE)
_CODE_RE = re.compile(r'"(?:code|type)"\s*:\s*"([^"]{1,80})"')


def _status_of(lower: str, exc: BaseException) -> Optional[int]:
    for attr in ("status_code", "status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int) and 100 <= value < 600:
            return value
    match = _STATUS_RE.search(lower)
    return int(match.group(1)) if match else None


def _codes_of(text: str) -> str:
    """The provider's own ``code``/``type`` values, lowercased.

    Collected from the embedded JSON error body rather than parsed as a whole:
    the body is prefixed by our own ``Responses API HTTP 400:`` text and may be
    truncated, so a tolerant scan is more robust than ``json.loads``.
    """
    return " ".join(match.group(1).lower() for match in _CODE_RE.finditer(text or ""))


def _info(kind, action, status, codes, identical_retry_ok, detail) -> ProviderErrorInfo:
    return ProviderErrorInfo(
        kind=kind, action=action, status=status, codes=codes,
        identical_retry_ok=identical_retry_ok, detail=str(detail or "")[:600],
    )


def _is_thinking_replay_rejection(lower: str) -> bool:
    """A thinking-mode assistant turn was replayed without its reasoning.

    Matched on the *field named* plus the *rule stated*, so a provider renaming
    the field (as DeepSeek did between transports) keeps being recognised.
    """
    mentions = "reasoning" in lower or "thinking" in lower
    if not mentions:
        return False
    states_rule = any(phrase in lower for phrase in (
        "passed back", "must be passed", "must pass back", "not passed back",
        "has to be passed", "should be passed", "needs to be passed",
    ))
    if not states_rule:
        return False
    return ("reasoning_text" in lower or "reasoning_content" in lower
            or "thinking mode" in lower or "reasoning" in lower)


def _is_encrypted_history_rejection(lower: str, codes: str) -> bool:
    if "invalid_encrypted_content" in codes or "invalid_encrypted_content" in lower:
        return True
    return "encrypted content" in lower and any(
        word in lower for word in ("verified", "decrypted", "parsed")
    )


def _is_tool_choice_rejection(lower: str) -> bool:
    if "tool_choice" not in lower and "tool choice" not in lower:
        return False
    return any(marker in lower for marker in (
        "does not support", "not supported", "unsupported", "invalid", "not allowed",
    ))


def classify_provider_error(exc: BaseException) -> ProviderErrorInfo:
    """Map a provider/LLM exception onto one recovery action.

    Order matters: the specific 400 signatures are tested before the generic
    "unrecognised client error" bucket, and auth/quota are terminal regardless of
    what else the message says.
    """
    text = str(exc or "")
    lower = text.lower()
    status = _status_of(lower, exc)
    codes = _codes_of(text)

    if status in (401, 403) or "unauthorized" in lower or "invalid api key" in lower:
        return _info(Kind.AUTH, Action.TERMINAL, status, codes, False, text)

    if status == 429 or "rate limit" in lower:
        exhausted = ("usage_limit_reached" in codes or "quota" in lower
                     or "insufficient" in lower)
        if exhausted:
            return _info(Kind.QUOTA, Action.TERMINAL, status, codes, False, text)
        return _info(Kind.QUOTA, Action.RETRY_BACKOFF, status, codes, True, text)

    # Signature-based kinds are tested before the status buckets: a stale chain or
    # a renamed reasoning field is recognised from the message itself, and the
    # adapter does not always attach a status code to one.
    if _is_thinking_replay_rejection(lower):
        return _info(Kind.THINKING_REPLAY_REJECTED,
                     Action.REBUILD_REPLAYABLE_HISTORY, status, codes, False, text)
    if _is_encrypted_history_rejection(lower, codes):
        return _info(Kind.ENCRYPTED_HISTORY_REJECTED,
                     Action.REBASE_HISTORY, status, codes, False, text)
    if is_missing_response_error(exc) or is_chain_resume_rejected(exc):
        # A chain that cannot be resumed and a chain parameter the endpoint refuses
        # outright share one recovery: drop the chain, resend the local transcript
        # whole.
        return _info(Kind.STALE_CHAIN, Action.REBASE_CHAIN, status, codes, False, text)
    if _is_tool_choice_rejection(lower):
        return _info(Kind.TOOL_CHOICE_REJECTED,
                     Action.DROP_FORCED_TOOL_CHOICE, status, codes, False, text)

    if status is None or status >= 500:
        return _info(Kind.TRANSIENT, Action.RETRY_BACKOFF, status, codes, True, text)

    # Transport fallbacks only make sense for a client-side rejection; a 5xx that
    # happens to mention a missing endpoint is an outage, not a wrong transport.
    if is_unsupported_deepseek_responses_error(exc):
        return _info(Kind.MODEL_SCOPED_REJECTION,
                     Action.FALLBACK_TRANSPORT, status, codes, False, text)
    if is_unsupported_responses_error(exc):
        return _info(Kind.UNSUPPORTED_TRANSPORT,
                     Action.FALLBACK_TRANSPORT, status, codes, False, text)
    if status >= 400:
        return _info(Kind.UNKNOWN_CLIENT, Action.TERMINAL, status, codes, False, text)

    return _info(Kind.UNKNOWN, Action.PROPAGATE, status, codes, False, text)


def alias_failover_eligible(exc: BaseException) -> bool:
    """Whether a pre-output provider failure may try an explicit alias.

    Keep this allow-list narrow. Alias failover is for provider availability or
    credential/quota failures, not for malformed prompts, policy rejections, or
    deterministic request bugs that another model must not silently reinterpret.
    """
    return classify_provider_error(exc).kind in {
        Kind.AUTH,
        Kind.QUOTA,
        Kind.TRANSIENT,
        Kind.MODEL_SCOPED_REJECTION,
    }


def provider_stop_cause(exc: BaseException) -> str:
    """Stop cause for a supervised turn that died on a provider error.

    ``runner._RESUMABLE_CAUSES`` contains ``crash``, so an unclassified failure
    is retried by the self-healing layer.  That is right for a network blip and
    wrong for a deterministic client error: the resume replays the same request,
    which is how three attempts got spent proving an HTTP 400 would keep being an
    HTTP 400.  Returning a cause outside that set stops the loop while the loop
    is still cheap, and the user sees the real error.
    """
    info = classify_provider_error(exc)
    if info.status is None:
        return "crash"
    if info.is_terminal and not info.identical_retry_ok:
        return "provider_error"
    return "crash"