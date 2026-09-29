"""The one route for bounded, tool-free completions.

Two kinds of model call exist here: the streaming tool loop in ``agent/loop.py``,
and everything else. "Everything else" — manager verdicts, closer audits,
compaction summaries, titling, suggestions, image description, one-off workers —
is the same call with a different prompt, and it used to be spread across two
modules with the provider quirks re-implemented at the call sites. This module is
now that single route.

What lives here, and why
------------------------
* **Transport dispatch** — OpenAI-compatible API key, or the OAuth-backed ChatGPT
  subscription Responses transport.
* **Parameter fallbacks** — a provider that rejects ``temperature``, wants
  ``max_completion_tokens``, or refuses the reasoning hint gets the parameter
  dropped and the call retried, rather than an exception.
* **The reasoning slot** — a reasoning model intermittently returns the whole
  answer in ``reasoning_content`` with ``content`` empty (finish_reason "stop",
  not an error). Reading both slots is one rule that every caller needs, so it is
  ``completion_answer`` here rather than three copies out there.
* **Outgoing hygiene** — whether ``reasoning_content`` may be sent back at all is
  a property of the endpoint, so it comes from the provider profile.
* **Error classification** — a failed call carries its ``provider_errors``
  classification, so a caller can tell "retry" from "do not retry unchanged"
  without matching on message text.

Callers keep what is theirs: the prompt, the size/timeout/temperature, the parse,
and their own success semantics (the closer, for instance, still treats
``finish_reason == length`` as a failed summary).
"""
from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any, Dict, List, Mapping, Optional, Sequence

from app.agent.provider_profile import SHAPE_CHAT, ProviderProfile, resolve_profile

logger = logging.getLogger(__name__)


class SubscriptionResponseClient:
    """Credential target for a ChatGPT-subscription Responses completion."""

    def __init__(self, *, scope: str, entry_id: str, user_id: str, agent_id: str) -> None:
        self.scope = scope
        self.entry_id = entry_id
        self.user_id = user_id
        self.agent_id = agent_id


async def _subscription_completion(
    client: SubscriptionResponseClient,
    *,
    model: str,
    messages: Sequence[Mapping[str, Any]],
    max_tokens: int,
    timeout: float,
    extra_body: Mapping[str, Any] | None = None,
) -> Any:
    """Return a ChatCompletion-shaped response from ChatGPT's Responses API."""
    from app import openai_subscription as subscription
    from app.agent.response_chaining import build_responses_request, open_responses_stream

    auth = await subscription.transport_auth(
        scope=client.scope, entry_id=client.entry_id,
        user_id=client.user_id, agent_id=client.agent_id,
    )
    body = build_responses_request(
        model=model,
        messages=list(messages),
        tools=None,
        max_output_tokens=max_tokens,
        provider_family_name="openai",
        session_key=f"wa-background-{client.user_id}",
    )
    # The ChatGPT subscription endpoint rejects max_output_tokens. Keep the
    # requested bound in caller-side validation instead of sending an unsupported
    # transport parameter.
    body["store"] = False
    body["include"] = ["reasoning.encrypted_content"]
    if extra_body:
        body.update(dict(extra_body))
    body.pop("max_output_tokens", None)
    headers = {
        "originator": "webagent",
        "User-Agent": "webagent/1.0",
        "session-id": f"wa-background-{client.user_id}",
    }
    if auth.get("account_id"):
        headers["ChatGPT-Account-Id"] = auth["account_id"]
    if auth.get("residency"):
        headers["x-openai-internal-codex-residency"] = auth["residency"]

    stream = await open_responses_stream(
        base_url="codex://chatgpt", api_key=auth["access_token"], family="openai",
        body=body, timeout=timeout, request_url=subscription.RESPONSES_ENDPOINT,
        extra_headers=headers,
    )
    parts: list[str] = []
    try:
        async for chunk in stream:
            for choice in getattr(chunk, "choices", None) or []:
                delta = getattr(choice, "delta", None)
                text = getattr(delta, "content", None) if delta else None
                if text:
                    parts.append(str(text))
    finally:
        await stream.close()
    message = SimpleNamespace(content="".join(parts), reasoning_content=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")])


def _client_base_url(client: Any) -> str:
    """The endpoint a client is pointed at, when it knows (the OpenAI SDK does)."""
    return str(getattr(client, "base_url", "") or "")


def resolve_completion_profile(
    *,
    model: str = "",
    provider: str = "",
    base_url: str = "",
    client: Any = None,
    transport: str = "chat_completions",
) -> ProviderProfile:
    """The profile for one bounded completion.

    Derived from the endpoint rather than demanded of the caller: an SDK client
    carries its own ``base_url``, so existing call sites get the right answer
    without passing anything new. An explicit ``provider`` wins over the hostname
    when a caller knows more than the URL does (proxies, aliases).
    """
    from app.agent.provider_cache import provider_family

    url = str(base_url or _client_base_url(client) or "")
    family = provider_family(provider or "", url, model or "")
    return resolve_profile(
        family=family,
        transport=transport,
        provider=provider or "",
        base_url=url,
        model=model or "",
    )


def prepare_completion_messages(
    messages: Sequence[Mapping[str, Any]], profile: ProviderProfile,
) -> List[Dict[str, Any]]:
    """Outgoing messages, with reasoning the target will not accept removed.

    One endpoint requires its own thinking to travel back with the turn it belongs
    to; every other OpenAI-compatible endpoint rejects the field outright. Which
    is which is a property of the endpoint, so it comes from the profile instead
    of a substring test on the provider name.
    """
    keep = profile.reasoning_shape == SHAPE_CHAT
    out: List[Dict[str, Any]] = []
    for message in messages or []:
        item = dict(message)
        if not keep:
            item.pop("reasoning_content", None)
        out.append(item)
    return out


def completion_answer(resp: Any) -> str:
    """The assistant's text from a completion, from whichever slot holds it.

    A reasoning model can spend its budget thinking and return the whole answer in
    ``reasoning_content`` with ``content`` empty — a successful 200 carrying
    nothing in the obvious place. Preferring ``content`` and falling back is one
    rule that every caller of this module needs; three callers had written their
    own version of it, and one had written it twice.
    """
    choices = getattr(resp, "choices", None) or []
    message = getattr(choices[0], "message", None) if choices else None
    text = str(getattr(message, "content", None) or "").strip()
    if text:
        return text
    return str(getattr(message, "reasoning_content", None) or "").strip()


def _annotate_provider_error(exc: BaseException) -> BaseException:
    """Attach the provider-error classification to a raised exception.

    Callers then read ``exc.provider_error`` (kind, action, whether an unchanged
    retry is worth attempting) instead of matching on message text — the habit
    that let one wording of the same rule go unrecognised for two days.
    """
    try:
        if getattr(exc, "provider_error", None) is None:
            from app.agent.provider_errors import classify_provider_error
            exc.provider_error = classify_provider_error(exc)  # type: ignore[attr-defined]
    except Exception:
        logger.debug("provider error annotation failed", exc_info=True)
    return exc


def _completion_is_empty(resp: Any) -> tuple:
    """``(is_empty, finish_reason)`` for a chat completion.

    A reasoning model can return a 200 with NO visible text anywhere — it spent
    the whole budget on hidden reasoning — and that must be treated as a failure,
    not as a blank answer.
    """
    try:
        choices = getattr(resp, "choices", None) or []
        finish = getattr(choices[0], "finish_reason", None) if choices else None
        return (not completion_answer(resp)), finish
    except Exception:
        return False, None  # don't second-guess an unexpected shape


async def safe_chat_completion(
    client: Any, *, strict_max_tokens: bool = False, **kwargs: Any,
) -> Any:
    """``client.chat.completions.create`` with graceful fallback for models that
    reject common params OR return an empty reply.

    Robustness layers (a one-shot worker must not be silently defeated by a model
    quirk):
      • Some newer models (OpenAI GPT-5 family on OpenRouter) reject
        ``temperature``; others want ``max_completion_tokens`` instead of
        ``max_tokens`` — on such a 400 we drop/swap the param and retry.
      • Reasoning models spend completion tokens on hidden reasoning first; if
        that eats the whole budget the visible content comes back EMPTY. So we
        ask for LOW reasoning effort by default (budget goes to the answer), and
        if a reply still arrives empty because the budget was exhausted
        (finish_reason 'length'), we retry once with a much larger budget—unless
        ``strict_max_tokens`` is set for a caller that requires a hard ceiling.
      • Any param the provider doesn't understand (e.g. the reasoning hint on a
        non-reasoning model) is dropped and the call retried, never raised
        blindly.

    This is the lower layer: ``complete_text`` is the entry point, and it is what
    applies the provider profile to the outgoing messages.
    """
    # Keep "thinking" cheap so a describe/answer worker spends its tokens on the
    # visible reply. Caller can override by passing reasoning/extra_body itself.
    if "extra_body" not in kwargs and "reasoning_effort" not in kwargs:
        kwargs["extra_body"] = {"reasoning": {"effort": "low"}}
    bumped = False
    last = None
    for _ in range(6):
        try:
            resp = await client.chat.completions.create(**kwargs)
        except Exception as e:
            last = e
            msg = str(e).lower()
            if "temperature" in kwargs and "temperature" in msg \
                    and ("not supported" in msg or "unsupported" in msg):
                kwargs.pop("temperature", None)
                continue
            if "max_tokens" in kwargs and "max_completion_tokens" in msg:
                kwargs["max_completion_tokens"] = kwargs.pop("max_tokens")
                continue
            if "extra_body" in kwargs:  # provider may not accept the reasoning hint
                kwargs.pop("extra_body", None)
                continue
            raise _annotate_provider_error(e)
        # 200 but empty because reasoning exhausted the budget → retry bigger once.
        empty, finish = _completion_is_empty(resp)
        if empty and finish == "length" and not bumped and not strict_max_tokens:
            bumped = True
            for key in ("max_tokens", "max_completion_tokens"):
                if key in kwargs:
                    kwargs[key] = max(int(kwargs[key]) * 3, 3000)
            continue
        return resp
    raise _annotate_provider_error(last) if last else RuntimeError(
        "safe_chat_completion: retries exhausted")


async def complete_text(
    client: Any,
    *,
    model: str,
    messages: Sequence[Mapping[str, Any]],
    max_tokens: int,
    temperature: float,
    timeout: float,
    extra_body: Mapping[str, Any] | None = None,
    provider: str = "",
    base_url: str = "",
    profile: Optional[ProviderProfile] = None,
    strict_max_tokens: bool = False,
) -> Any:
    """Run one bounded background completion across supported transports.

    ``provider`` / ``base_url`` / ``profile`` are optional, and deliberately so:
    when they are omitted the endpoint is taken from the client itself, which
    means every existing caller inherits the profile rules without changing.
    """
    subscription = isinstance(client, SubscriptionResponseClient)
    active = profile or resolve_completion_profile(
        model=model,
        provider=provider or ("openai_subscription" if subscription else ""),
        base_url=base_url,
        client=client,
        transport="responses" if subscription else "chat_completions",
    )
    prepared = prepare_completion_messages(messages, active)
    if subscription:
        try:
            return await _subscription_completion(
                client, model=model, messages=prepared, max_tokens=max_tokens,
                timeout=timeout, extra_body=extra_body,
            )
        except Exception as exc:
            raise _annotate_provider_error(exc)
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": prepared,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if extra_body:
        kwargs["extra_body"] = dict(extra_body)
    return await safe_chat_completion(client, strict_max_tokens=strict_max_tokens, **kwargs)


async def ask_model(
    model_cfg: Dict[str, Any],
    system_prompt: str,
    user_text: str,
    attachments: Optional[List[Dict[str, Any]]] = None,
    *,
    max_tokens: int = 900,
    temperature: float = 0.2,
    timeout: float = 60.0,
    error_sink: Optional[List[str]] = None,
) -> Optional[str]:
    """Run one tools-free completion and return the model's text reply.

    Args:
      model_cfg:  {model, base_url, api_key} for the target model (provider key).
      system_prompt: the worker's instructions ("you are X, do Y, report Z").
      user_text:  the question / task for this round.
      attachments: optional attachment dicts; any inlinable image among them is
                   passed to the model as an image part (others are ignored — a
                   tools-free worker can't open files).
      error_sink: optional list; on failure the real error string is appended so
                  callers can surface WHY the call failed (e.g. "no remaining
                  credits") instead of only seeing None.
    Returns the reply text, or None when the config is incomplete or the call
    fails. Never raises — callers treat None as "delegation unavailable".
    """
    model = (model_cfg or {}).get("model", "")
    base_url = (model_cfg or {}).get("base_url", "")
    api_key = (model_cfg or {}).get("api_key", "")
    if not (model and base_url and api_key):
        if error_sink is not None:
            error_sink.append("ask_model: incomplete model config (missing model/base_url/api_key)")
        return None

    # Inline any image attachments (reusing the shared mime/size/storage guards).
    image_parts: List[Dict[str, Any]] = []
    if attachments:
        from app.agent.prompts import _attachment_to_image_data_url
        for att in attachments:
            try:
                url = await _attachment_to_image_data_url(att)
            except Exception:
                url = None
            if url:
                image_parts.append({"type": "image_url", "image_url": {"url": url}})

    if image_parts:
        user_content: Any = [{"type": "text", "text": user_text or ""}, *image_parts]
    else:
        user_content = user_text or ""

    try:
        from openai import AsyncOpenAI
    except ImportError:
        from app.openai_compat import AsyncOpenAI

    try:
        client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=timeout)
        resp = await complete_text(
            client,
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
            provider=str((model_cfg or {}).get("provider") or ""),
            base_url=base_url,
        )
        if resp and resp.choices:
            # Reads the reasoning slot too — a vision/worker model may put its
            # whole answer there, which used to read as an empty reply.
            return completion_answer(resp) or None
    except Exception as e:
        logger.warning("ask_model failed (model=%s): %s", model, e)
        if error_sink is not None:
            error_sink.append(str(e))
    return None
