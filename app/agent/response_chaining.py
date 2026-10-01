"""Provider-neutral stateful response chaining for OpenResponses endpoints.

The local WebAgent transcript remains authoritative.  This module is only a
transport optimisation: supported providers may retain the previous response
and accept the next delta through ``previous_response_id``.  If a chain cannot
be resumed, callers can always rebuild a fresh request from the local messages.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, AsyncIterator, Dict, Iterable, List, Mapping, Optional
from urllib.parse import urlsplit

import httpx

from app.agent.provider_cache import provider_family
from app.agent.provider_profile import SHAPE_ENCRYPTED, reasoning_item_shape


_SUPPORTED_FAMILIES = frozenset({"openai", "openrouter", "xai", "azure_openai"})

#: Families that accept the Responses request SHAPE but cannot retain a response:
#: their ``previous_response_id`` schema admits null only, so no chain can ever be
#: resumed and every request has to carry the whole history. Treating one of these
#: as stateful sent a real id and drew HTTP 400 ``invalid_prompt`` ("expected null,
#: received string") on every model tried. The cost was not only the 400: a chained
#: turn that never lands leaves the model unable to see its own tool result, so it
#: re-issued one identical call until the loop guard broke the cycle.
_STATELESS_RESPONSES_FAMILIES = frozenset({"openrouter"})


def _enabled_by_default() -> bool:
    raw = os.environ.get("WEBAGENT_STATEFUL_RESPONSES", "1")
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def response_provider_family(provider: str, base_url: str, model: str) -> str:
    if provider.lower() == "openai_subscription":
        return "openai"
    identity = f"{provider} {base_url} {model}".lower()
    if "api.x.ai" in identity or provider.lower() in {"xai", "x.ai"}:
        return "xai"
    if "openai.azure.com" in identity or provider.lower() in {"azure", "azure_openai"}:
        return "azure_openai"
    return provider_family(provider, base_url, model)


@dataclass(frozen=True)
class ResponseChainCapability:
    enabled: bool
    family: str
    transport: str
    reason: str
    stateful: bool


def _is_official_deepseek(provider: str, base_url: str) -> bool:
    """True only for the first-party DeepSeek OpenAI-compatible endpoint."""
    if str(provider or "").strip().lower() != "deepseek":
        return False
    raw_url = str(base_url or "").strip()
    if not raw_url:
        return False
    parsed = urlsplit(raw_url if "://" in raw_url else f"https://{raw_url}")
    return (parsed.hostname or "").rstrip(".").lower() == "api.deepseek.com"


def response_chain_capability(
    *, provider: str, base_url: str, model: str, mode: Any = "auto",
) -> ResponseChainCapability:
    """Resolve whether this provider can use the OpenResponses stateful path."""
    family = response_provider_family(provider, base_url, model)
    requested = str(mode if mode is not None else "auto").strip().lower()
    if requested in {"0", "false", "off", "disabled", "chat_completions"}:
        return ResponseChainCapability(False, family, "chat_completions", "disabled", False)
    if not _enabled_by_default():
        return ResponseChainCapability(False, family, "chat_completions", "global_disabled", False)
    if _is_official_deepseek(provider, base_url):
        return ResponseChainCapability(
            True, "deepseek", "responses", "official_deepseek_stateless", False,
        )
    if family in _STATELESS_RESPONSES_FAMILIES:
        # Responses shape, no retention — the same stateless decision as the
        # first-party DeepSeek endpoint below, for the opposite reason: DeepSeek
        # resends by design, this endpoint can do nothing else.
        return ResponseChainCapability(
            True, family, "responses", "stateless_responses_only", False,
        )
    if family not in _SUPPORTED_FAMILIES:
        return ResponseChainCapability(False, family, "chat_completions", "unsupported_provider", False)
    return ResponseChainCapability(True, family, "responses", "supported", True)


def reasoning_replay_scope(
    *, provider: str, base_url: str, model: str, credential_scope: str = "",
    entry_id: str = "", credential_user_id: str = "", credential_agent_id: str = "",
) -> str:
    """Return a non-secret identity for private reasoning replay."""
    canonical = "\0".join((
        response_provider_family(provider, base_url, model),
        str(base_url).rstrip("/").lower(),
        str(model),
        str(credential_scope),
        str(entry_id),
        str(credential_user_id),
        str(credential_agent_id),
    ))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def response_chain_identity(*, provider: str, base_url: str, model: str) -> str:
    canonical = "\0".join((
        response_provider_family(provider, base_url, model),
        str(base_url).rstrip("/").lower(),
        str(model),
    ))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def response_chain_history_hash(messages: Iterable[Mapping[str, Any]]) -> str:
    """Fingerprint portable, non-system transcript state for safe continuation."""
    canonical = [dict(message) for message in messages if message.get("role") != "system"]
    raw = json.dumps(canonical, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _instructions(messages: Iterable[Mapping[str, Any]]) -> str:
    # Responses does not inherit instructions when previous_response_id is used,
    # so every current system layer is deliberately resent on every request.
    return "\n\n".join(
        str(message.get("content") or "").strip()
        for message in messages
        if message.get("role") == "system" and str(message.get("content") or "").strip()
    )


def _responses_content(content: Any) -> Any:
    if not isinstance(content, list):
        return content if content is not None else ""
    converted: List[dict] = []
    for part in content:
        if not isinstance(part, Mapping):
            converted.append({"type": "input_text", "text": str(part)})
            continue
        kind = str(part.get("type") or "")
        if kind in {"text", "input_text"}:
            converted.append({"type": "input_text", "text": str(part.get("text") or "")})
        elif kind in {"image_url", "input_image"}:
            image = part.get("image_url")
            url = image.get("url") if isinstance(image, Mapping) else image
            item = {"type": "input_image", "image_url": str(url or "")}
            detail = part.get("detail") or (image.get("detail") if isinstance(image, Mapping) else None)
            if detail:
                item["detail"] = detail
            converted.append(item)
        else:
            converted.append(dict(part))
    return converted


def responses_input(
    messages: Iterable[Mapping[str, Any]], *, delta_start: int = 0,
    plain_reasoning: bool = False,
) -> List[dict]:
    """Translate Chat Completions messages into portable Responses input items.

    ``plain_reasoning`` mirrors the capture side (``_EventState``): on a transport
    that carries thinking as plain ``reasoning_text``, an assistant turn whose
    reasoning survived only under the Chat Completions field name
    (``reasoning_content`` — rows written before item capture existed, or carried
    across a transport fallback) is replayed too, as a synthesized plain reasoning
    item. Without it the turn satisfies the message-level replay check —
    ``message_reasoning_count`` credits ``reasoning_content`` — while the built
    body emits no reasoning item at all, so the endpoint 400s ("reasoning_text in
    the thinking mode must be passed back").
    """
    source = list(messages)[max(0, int(delta_start or 0)):]
    result: List[dict] = []
    for message in source:
        role = str(message.get("role") or "")
        if role == "system":
            continue
        if role == "tool":
            call_id = str(message.get("tool_call_id") or "")
            if call_id:
                result.append({
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": str(message.get("content") or ""),
                })
            continue
        if role not in {"user", "assistant", "developer"}:
            continue
        if role == "assistant":
            _replayed = False
            for reasoning in message.get("_responses_reasoning") or []:
                if not isinstance(reasoning, Mapping):
                    continue
                if plain_reasoning and reasoning_item_shape(reasoning) == SHAPE_ENCRYPTED:
                    # An encrypted blob is not replayable to a plain-thinking
                    # endpoint: it never issued the blob and cannot verify it.
                    # Skipping it here lets the plain synthesis below supply the
                    # shape the endpoint actually requires — shipping the blob
                    # instead is exactly what produced the deterministic
                    # "reasoning_text ... must be passed back" 400.
                    continue
                result.append(dict(reasoning))
                _replayed = True
            if not _replayed and plain_reasoning:
                text = str(message.get("reasoning_content") or "").strip()
                if text:
                    result.append({
                        "type": "reasoning",
                        "content": [{"type": "reasoning_text", "text": text}],
                    })
        content = message.get("content")
        if content not in (None, "", []):
            result.append({"role": role, "content": _responses_content(content)})
        if role == "assistant":
            for call in message.get("tool_calls") or []:
                fn = call.get("function") if isinstance(call, Mapping) else None
                if not isinstance(fn, Mapping):
                    continue
                call_id = str(call.get("id") or "")
                if call_id:
                    result.append({
                        "type": "function_call",
                        "call_id": call_id,
                        "name": str(fn.get("name") or ""),
                        "arguments": str(fn.get("arguments") or "{}"),
                    })
    return result


def responses_tools(tools: Optional[Iterable[Mapping[str, Any]]]) -> Optional[List[dict]]:
    if not tools:
        return None
    result: List[dict] = []
    for tool in tools:
        fn = tool.get("function") if isinstance(tool, Mapping) else None
        if tool.get("type") == "function" and isinstance(fn, Mapping):
            result.append({
                "type": "function",
                "name": str(fn.get("name") or ""),
                "description": str(fn.get("description") or ""),
                "parameters": dict(fn.get("parameters") or {}),
            })
        elif isinstance(tool, Mapping):
            result.append(dict(tool))
    return result or None


def build_responses_request(
    *,
    model: str,
    messages: List[Mapping[str, Any]],
    tools: Optional[List[Mapping[str, Any]]],
    max_output_tokens: int,
    previous_response_id: Optional[str] = None,
    delta_start: int = 0,
    reasoning_effort: Optional[str] = None,
    prompt_cache_key: Optional[str] = None,
    provider_family_name: str = "",
    session_key: str = "",
    stateful: bool = True,
) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "model": model,
        "input": responses_input(
            messages,
            delta_start=delta_start if stateful else 0,
            plain_reasoning=(provider_family_name == "deepseek"),
        ),
        "instructions": _instructions(messages),
        "max_output_tokens": max_output_tokens,
        "stream": True,
    }
    if stateful or provider_family_name == "openrouter":
        # OpenRouter's OpenResponses schema exposes store=false only, so the value
        # is sent on its stateless path too — omitting the field is not the same as
        # sending false there.
        body["store"] = provider_family_name != "openrouter"
    converted_tools = responses_tools(tools)
    if converted_tools:
        body.update({"tools": converted_tools, "tool_choice": "auto"})
    if stateful and previous_response_id:
        body["previous_response_id"] = previous_response_id
    if reasoning_effort:
        body["reasoning"] = {"effort": reasoning_effort}
    if stateful and prompt_cache_key:
        body["prompt_cache_key"] = prompt_cache_key
    # No provider-side session for OpenRouter: it cannot resume a chain (see
    # _STATELESS_RESPONSES_FAMILIES), so a retained session could only drift from
    # the history we resend whole. ``session_key`` remains a parameter for the
    # callers that pass it; nothing on this transport consumes it.
    return body


def _responses_url(base_url: str) -> str:
    return str(base_url).rstrip("/") + "/responses"


def _headers(*, api_key: str, base_url: str, family: str) -> Dict[str, str]:
    headers = {"Accept": "text/event-stream", "Content-Type": "application/json"}
    key = str(api_key or "").strip()
    if family == "azure_openai" or "openai.azure.com" in urlsplit(base_url).netloc.lower():
        if key:
            headers["api-key"] = key
    elif key:
        # Never emit ``Authorization: Bearer `` with an empty value — httpx
        # rejects it at the HTTP layer ("Illegal header value b'Bearer '"),
        # which surfaces as a transport crash instead of an actionable error.
        headers["Authorization"] = f"Bearer {key}"
    return headers


@dataclass
class _EventState:
    tool_indices: Dict[str, int] = field(default_factory=dict)
    tool_names: Dict[str, str] = field(default_factory=dict)
    reasoning_items: List[dict] = field(default_factory=list)
    reasoning_text: Dict[str, List[str]] = field(default_factory=dict)
    preserve_plain_reasoning: bool = False


def _plain_reasoning_item(item: Mapping[str, Any], fallback: str = "") -> Optional[dict]:
    parts = []
    for part in item.get("content") or []:
        if isinstance(part, Mapping) and part.get("type") == "reasoning_text":
            text = str(part.get("text") or "")
            if text:
                parts.append({"type": "reasoning_text", "text": text})
    if not parts:
        # Some endpoints deliver the same text as a summary part instead.
        for part in item.get("summary") or []:
            if isinstance(part, Mapping):
                text = str(part.get("text") or "")
                if text:
                    parts.append({"type": "reasoning_text", "text": text})
    if not parts and fallback:
        parts.append({"type": "reasoning_text", "text": fallback})
    return {"type": "reasoning", "content": parts} if parts else None


def _usage_chunk(response: Mapping[str, Any]) -> Any:
    usage = response.get("usage") or {}
    input_details = usage.get("input_tokens_details") or {}
    output_details = usage.get("output_tokens_details") or {}
    return SimpleNamespace(
        usage=SimpleNamespace(
            prompt_tokens=usage.get("input_tokens", usage.get("prompt_tokens")),
            completion_tokens=usage.get("output_tokens", usage.get("completion_tokens")),
            cost=usage.get("cost"),
            prompt_tokens_details=input_details,
            completion_tokens_details=output_details,
            model_extra=dict(usage),
        ),
        choices=[],
    )


def _capture_reasoning_item(item: Mapping[str, Any], event: Mapping[str, Any],
                            state: _EventState) -> None:
    """Record one reasoning item in the shape the target endpoint replays.

    An endpoint can return the SAME reasoning item both ways at once: the plain
    text under ``content`` (``{"type": "reasoning_text", ...}``) and an opaque
    ``encrypted_content`` handle beside it. The handle is only meaningful to the
    endpoint that issued it, so when the target replays plain reasoning this
    transport keeps the text. Testing the handle first dropped the text sitting
    in the same item, which is what left the replay with nothing to pass back and
    produced "reasoning_text in the thinking mode must be passed back to the API".
    """
    item_id = str(item.get("id") or event.get("output_index") or "reasoning")
    if state.preserve_plain_reasoning:
        clean = _plain_reasoning_item(
            item, "".join(state.reasoning_text.get(item_id) or []),
        )
        if clean:
            if clean not in state.reasoning_items:
                state.reasoning_items.append(clean)
            return
    if item.get("encrypted_content"):
        clean = {
            "type": "reasoning",
            "encrypted_content": item["encrypted_content"],
            "summary": item.get("summary") or [],
        }
        if clean not in state.reasoning_items:
            state.reasoning_items.append(clean)


def response_event_chunks(event: Mapping[str, Any], state: _EventState) -> List[Any]:
    """Normalize one Responses SSE event into the loop's existing stream shape."""
    kind = str(event.get("type") or "")
    if kind == "response.output_text.delta":
        delta = SimpleNamespace(content=str(event.get("delta") or ""), tool_calls=None)
        return [SimpleNamespace(usage=None, choices=[SimpleNamespace(delta=delta)])]
    if kind in {"response.reasoning_text.delta", "response.reasoning_summary_text.delta"}:
        # Summary-style reasoning is the same information under a different event
        # name. Capturing only the plain-text delta silently dropped it, which left
        # a thinking-mode turn with nothing to replay — one half of the pairing
        # whose other half is enforced in provider_profile.
        if state.preserve_plain_reasoning:
            item_id = str(event.get("item_id") or event.get("output_index") or "reasoning")
            state.reasoning_text.setdefault(item_id, []).append(str(event.get("delta") or ""))
        return []
    if kind == "response.output_item.added":
        item = event.get("item") or {}
        if item.get("type") != "function_call":
            return []
        item_id = str(item.get("id") or item.get("call_id") or event.get("output_index") or "")
        index = int(event.get("output_index") or len(state.tool_indices))
        state.tool_indices[item_id] = index
        state.tool_names[item_id] = str(item.get("name") or "")
        fn = SimpleNamespace(name=state.tool_names[item_id], arguments=str(item.get("arguments") or ""))
        tc = SimpleNamespace(index=index, id=str(item.get("call_id") or item_id), function=fn)
        return [SimpleNamespace(usage=None, choices=[SimpleNamespace(
            delta=SimpleNamespace(content=None, tool_calls=[tc]),
        )])]
    if kind == "response.function_call_arguments.delta":
        item_id = str(event.get("item_id") or "")
        index = state.tool_indices.get(item_id, int(event.get("output_index") or 0))
        fn = SimpleNamespace(name=None, arguments=str(event.get("delta") or ""))
        tc = SimpleNamespace(index=index, id=None, function=fn)
        return [SimpleNamespace(usage=None, choices=[SimpleNamespace(
            delta=SimpleNamespace(content=None, tool_calls=[tc]),
        )])]
    if kind == "response.output_item.done":
        item = event.get("item") or {}
        if item.get("type") == "reasoning":
            _capture_reasoning_item(item, event, state)
        return []
    if kind in {"response.completed", "response.incomplete"}:
        response = event.get("response") or {}
        for item in response.get("output") or []:
            if isinstance(item, Mapping) and item.get("type") == "reasoning":
                _capture_reasoning_item(item, event, state)
        return [_usage_chunk(response)]
    if kind == "response.failed":
        response = event.get("response") or {}
        error = response.get("error") or event.get("error") or {}
        if isinstance(error, Mapping):
            message = error.get("message") or error.get("code")
        else:
            message = error
        raise RuntimeError(f"Responses API failed: {message or 'unknown provider error'}")
    if kind == "error":
        error = event.get("error") or event
        raise RuntimeError(str(error.get("message") or error))
    return []


class ResponsesStream:
    """Small HTTP/SSE stream adapter exposing ChatCompletion-like chunks."""

    def __init__(self, client: httpx.AsyncClient, context: Any, response: httpx.Response,
                 *, family: str = ""):
        self._client = client
        self._context = context
        self._response = response
        self._state = _EventState(preserve_plain_reasoning=(family == "deepseek"))
        self.response_id: Optional[str] = None
        self._chunks: List[Any] = []
        self._lines: Optional[AsyncIterator[str]] = None

    @property
    def reasoning_items(self) -> List[dict]:
        return list(self._state.reasoning_items)

    def __aiter__(self):
        self._lines = self._response.aiter_lines()
        return self

    async def __anext__(self):
        if self._chunks:
            return self._chunks.pop(0)
        if self._lines is None:
            self._lines = self._response.aiter_lines()
        async for line in self._lines:
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            event = json.loads(payload)
            response = event.get("response") or {}
            rid = response.get("id") or event.get("response_id")
            if rid:
                self.response_id = str(rid)
            self._chunks.extend(response_event_chunks(event, self._state))
            if self._chunks:
                return self._chunks.pop(0)
        raise StopAsyncIteration

    async def close(self) -> None:
        try:
            await self._context.__aexit__(None, None, None)
        finally:
            await self._client.aclose()


async def open_responses_stream(
    *, base_url: str, api_key: str, family: str, body: Mapping[str, Any], timeout: float = 60.0,
    transport: Optional[httpx.AsyncBaseTransport] = None,
    request_url: Optional[str] = None,
    extra_headers: Optional[Mapping[str, str]] = None,
) -> ResponsesStream:
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(timeout, read=None), transport=transport,
    )
    if not str(api_key or "").strip():
        # Fail with a configuration error before httpx ever sees an empty
        # ``Authorization`` header.  A missing key is a deterministic, user-
        # fixable problem — not a transport crash to retry.
        await client.aclose()
        raise ValueError(
            "No API key is configured for this provider. "
            "Open the model's settings and add its API key, then retry."
        )
    headers = _headers(api_key=api_key, base_url=base_url, family=family)
    if extra_headers:
        headers.update({
            str(key): str(value) for key, value in extra_headers.items() if value is not None
        })
    context = client.stream(
        "POST", request_url or _responses_url(base_url),
        headers=headers,
        json=dict(body),
    )
    try:
        response = await context.__aenter__()
        if response.status_code >= 400:
            detail = (await response.aread()).decode("utf-8", errors="replace")
            raise RuntimeError(f"Responses API HTTP {response.status_code}: {detail[:2000]}")
        return ResponsesStream(client, context, response, family=family)
    except BaseException:
        try:
            await context.__aexit__(None, None, None)
        finally:
            await client.aclose()
        raise


def is_missing_response_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in (
        "previous_response_not_found", "previous response not found",
        "invalid previous_response_id", "unknown response",
    ))


def is_chain_resume_rejected(exc: BaseException) -> bool:
    """True when the endpoint refused the *chain parameter itself*.

    Distinct from :func:`is_missing_response_error`, which means the id was
    understood but the stored response is gone. Here nothing was ever retained:
    OpenRouter's Responses schema admits ``previous_response_id`` as null only and
    answers any string with HTTP 400 ``invalid_prompt`` ("expected null, received
    string"), so the request *shape* is accepted and only the chain is refused.

    The recovery is the same as a stale chain — rebuild from the local transcript
    and send it whole — but without recognising this signature the 400 fell through
    to "unrecognised client error" and ended the turn instead.
    """
    text = str(exc).lower()
    if "previous_response_id" not in text:
        return False
    return any(marker in text for marker in (
        "expected null", "received string", "must be null", "cannot be null",
        "does not support previous_response_id", "unsupported parameter",
    ))


def is_unsupported_responses_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in (
        "http 404", "http 405", "not found", "unknown endpoint",
        "unsupported endpoint", "responses api is not supported",
    ))


def is_unsupported_deepseek_responses_error(exc: BaseException) -> bool:
    """Recognize DeepSeek's model-scoped /responses rejection."""
    text = str(exc).lower()
    return "http 400" in text and any(marker in text for marker in (
        "model not exist",
        "unknown model",
        "unsupported model",
        "model does not support",
        "model is not supported",
    ))
