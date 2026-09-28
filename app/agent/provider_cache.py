"""Provider-aware controls for reusing stable prompt prefixes.

WebAgent deliberately keeps one OpenAI-compatible request path for broad
provider support. Most routed providers cache an identical prefix implicitly.
Direct OpenAI requests can additionally carry a stable ``prompt_cache_key``;
the wire fields are added through ``extra_body`` because the pinned SDK version
predates their typed parameters.

The complete logical context is still sent and still counts against the model
window. The optimisation lets the provider reuse already-computed KV state.
"""

from __future__ import annotations

import hashlib
import os
from typing import Any, Dict, Mapping


def _truthy_env(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def provider_family(provider: str, base_url: str, model: str) -> str:
    identity = f"{provider} {base_url} {model}".lower()
    if "api.openai.com" in identity or provider.lower() == "openai":
        return "openai"
    if "openrouter.ai" in identity or provider.lower() == "openrouter":
        return "openrouter"
    if "generativelanguage.googleapis.com" in identity or provider.lower() == "gemini":
        return "gemini"
    if "anthropic" in identity or "claude" in model.lower():
        return "anthropic"
    if "deepseek" in identity:
        return "deepseek"
    return "compatible"


def stable_prompt_cache_key(*, user_id: str, model: str, system_hash: str) -> str:
    """Return a non-identifying cache-routing key below the 64-char limit."""
    material = "\0".join((str(user_id), str(model), str(system_hash)))
    return "wa-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:40]


def prompt_cache_controls(
    *,
    provider: str,
    base_url: str,
    model: str,
    user_id: str,
    system_hash: str,
) -> Dict[str, Any]:
    """Return cache telemetry plus optional safe wire controls for a provider."""
    family = provider_family(provider, base_url, model)
    enabled = _truthy_env("WEBAGENT_PROVIDER_PROMPT_CACHE", True)
    result: Dict[str, Any] = {
        "enabled": enabled,
        "family": family,
        "strategy": "disabled" if not enabled else "implicit-prefix",
        "cache_key": None,
        "extra_body": {},
    }
    if not enabled:
        return result

    if family == "openai":
        cache_key = stable_prompt_cache_key(
            user_id=user_id, model=model, system_hash=system_hash,
        )
        body: Dict[str, Any] = {"prompt_cache_key": cache_key}
        if "gpt-5.6" in model.lower():
            # Let the provider choose the breakpoint. Do not force extended
            # retention; 30m is the currently supported minimum TTL.
            body["prompt_cache_options"] = {"mode": "implicit", "ttl": "30m"}
        result.update({
            "strategy": "openai-routed-prefix",
            "cache_key": cache_key,
            "extra_body": body,
        })
    return result


def merge_extra_body(
    existing: Mapping[str, Any] | None,
    additions: Mapping[str, Any] | None,
) -> Dict[str, Any]:
    """Merge independent optional controls without dropping reasoning config."""
    merged = dict(existing or {})
    merged.update(additions or {})
    return merged
