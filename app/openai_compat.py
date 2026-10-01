"""
OpenAI API compatibility shim for Android.
Provides AsyncOpenAI-like interface using httpx.
Avoids the jiter dependency issue in openai>=1.0 on Android.
On desktop, the real openai package is used instead.
"""

import json
import os
from typing import Optional


class _AsyncOpenAICompat:
    """
    Minimal AsyncOpenAI replacement using httpx.
    Supports streaming chat completions with tool calls via OpenRouter/OpenAI API.
    """

    def __init__(self, base_url: str = "", api_key: str = "", timeout: float = 60.0):
        import httpx

        self.base_url = base_url or os.environ.get("LLM_BASE_URL") or os.environ.get("OPENROUTER_BASE_URL") or ""
        self.api_key = api_key or os.environ.get("LLM_API_KEY") or os.environ.get("OPENROUTER_API_KEY", "")
        self.timeout = timeout
        self._client = httpx.AsyncClient(timeout=timeout)

    @property
    def chat(self):
        return self._ChatCompletions(self)


    class _ChatCompletions:
        def __init__(self, parent):
            self._parent = parent

        async def create(
            self,
            model: str,
            messages: list,
            tools: Optional[list] = None,
            tool_choice: Optional[str] = None,
            temperature: float = 0.0,
            max_tokens: int = 8096,
            stream: bool = False,
        ):
            body = {
                "model": model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "stream": stream,
            }
            if tools:
                body["tools"] = tools
            if tool_choice:
                body["tool_choice"] = tool_choice

            headers = {"Content-Type": "application/json"}
            api_key = str(self._parent.api_key or "").strip()
            if api_key:
                # Only attach Authorization when a key is present. An empty
                # ``Bearer `` value is rejected by httpx as an illegal header.
                headers["Authorization"] = f"Bearer {api_key}"

            response = await self._parent._client.post(
                f"{self._parent.base_url}/chat/completions",
                json=body,
                headers=headers,
            )
            # response.raise_for_status()

            if stream:
                return self._StreamWrapper(response)
            else:
                print("RESPONSE", response.text); data = response.json()
                return _Completion(data)


    class _StreamWrapper:
        def __init__(self, response):
            self._response = response
            self._lines = response.iter_lines()
            self._buffer = ""

        def __aiter__(self):
            return self._stream_chunks()

        async def _stream_chunks(self):
            async for line in self._response.aiter_lines():
                line = line.strip()
                if not line or line.startswith(":"):
                    continue
                if line.startswith("data: "):
                    data_str = line[6:]
                    if data_str == "[DONE]":
                        return
                    try:
                        data = json.loads(data_str)
                        choices = data.get("choices", [])
                        # The final SSE usage frame deliberately has no choices.
                        # Preserve it so callers can account for the completed call.
                        if not choices and data.get("usage") is not None:
                            yield _Chunk({}, data.get("usage"))
                        for choice in choices:
                            delta = choice.get("delta", {})
                            yield _Chunk(delta, data.get("usage"))
                    except json.JSONDecodeError:
                        continue


class _Usage:
    def __init__(self, data: dict):
        self.prompt_tokens = data.get("prompt_tokens", 0)
        self.completion_tokens = data.get("completion_tokens", 0)
        self.total_tokens = data.get("total_tokens", 0)
        self.cost = data.get("cost")
        self.prompt_tokens_details = data.get("prompt_tokens_details") or {}
        self.completion_tokens_details = data.get("completion_tokens_details") or {}
        self.model_extra = data


class _Completion:
    """Non-streaming completion response."""

    def __init__(self, data: dict):
        self._data = data
        self.choices = []
        for c in data.get("choices", []):
            self.choices.append(_Choice(c))
        usage_data = data.get("usage")
        self.usage = _Usage(usage_data) if usage_data else None


class _Choice:
    def __init__(self, data: dict):
        self._data = data
        self.delta = _Delta(data.get("delta", {}))
        self.message = _Message(data.get("message", {}))


class _Chunk:
    """Streaming chunk."""

    def __init__(self, delta: dict, usage: Optional[dict] = None):
        self.delta = _Delta(delta)
        self.choices = [_Choice({"delta": delta})]
        self.usage = _Usage(usage) if usage else None


class _Delta:
    def __init__(self, data: dict):
        self.content = data.get("content")
        self.tool_calls = None
        raw_tcs = data.get("tool_calls")
        if raw_tcs:
            self.tool_calls = [_ToolCall(tc) for tc in raw_tcs]


class _Message:
    def __init__(self, data: dict):
        self.content = data.get("content")
        self.tool_calls = None
        raw_tcs = data.get("tool_calls")
        if raw_tcs:
            self.tool_calls = [_ToolCall(tc) for tc in raw_tcs]


class _ToolCall:
    def __init__(self, data: dict):
        self.id = data.get("id", "")
        self.index = data.get("index", 0)
        self.type = data.get("type", "function")
        raw_fn = data.get("function", {})
        self.function = _Function(raw_fn)


class _Function:
    def __init__(self, data: dict):
        self.name = data.get("name", "")
        self.arguments = data.get("arguments", "{}")


def AsyncOpenAI(*args, **kwargs) -> _AsyncOpenAICompat:
    """Drop-in replacement for openai.AsyncOpenAI."""
    return _AsyncOpenAICompat(*args, **kwargs)
