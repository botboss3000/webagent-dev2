"""Image Generation ability — SELF-CONTAINED drop-in.

A TIER-1 ability that lets an agent generate images from a text prompt using
whichever saved model is ticked for image output in App Config → Models
(Image-out), saving results under the user's ``visuals/`` data home.

This file is now fully self-contained: the handler, its per-provider
implementations, the config lookup, and the JSON schema all live HERE. There is
no ``app/tools/image_generation`` import anymore. ``build_tools`` closes the
handler over ``user_id`` and publishes the schema into module-level
TOOL_SCHEMAS, which the loader reads AFTER calling build_tools. All heavy
imports (httpx, app.db, app.admin.settings, app.user_workspace) are kept LAZY —
inside build_tools or inside the handler closures — to avoid loader import
cycles when this module is merely scanned for its FEATURE descriptor.

Which model generates images comes from the unified Models list: the saved
model ticked for image output (App Config → Models → Image-out), resolved by
``pick_image_generator`` in app/admin/settings.py. A standalone ``auth_elements``
row under service="image_gen" is still honored as a legacy fallback for setups
created before image generation was folded into Models.

Default dispatch hits an OpenAI-compatible ``/images/generations`` endpoint —
that covers OpenAI (DALL·E, gpt-image-1), Together AI, DeepInfra, Fireworks, and
other OpenAI-compatible image hosts. Three providers do NOT use that route and
are special-cased below: Stability AI, Google Gemini/Imagen, and OpenRouter.
OpenRouter has **no** ``/images/generations`` endpoint at all — image models
there are driven through ``/chat/completions`` with
``modalities: ["image","text"]``, and the generated image comes back as a data
URL in ``message.images[]``. Hitting the OpenAI route on OpenRouter returns its
website's 404 HTML page, so an OpenRouter image model MUST take the
``openrouter`` shape.

See plugins/abilities/_TEMPLATE.py for the discovery contract.
"""

from __future__ import annotations

import base64
import json
import logging
import time
import uuid
from typing import Optional

logger = logging.getLogger(__name__)

# Populated inside build_tools() so the schema can never drift from the handler.
# generate_image is non-destructive (the loader marks it destructive=False), so
# DESTRUCTIVE stays empty.
TOOL_SCHEMAS: dict = {}
DESTRUCTIVE: set = set()


# Provider presets known to support image generation. Mirrors the LLM
# PROVIDER_PRESETS shape so the UI can offer a familiar dropdown.
IMAGE_PROVIDER_PRESETS = {
    "openai": {
        "name": "OpenAI (DALL·E / gpt-image)",
        "base_url": "https://api.openai.com/v1",
        "api_shape": "openai",
        "suggested_models": ["dall-e-3", "dall-e-2", "gpt-image-1"],
    },
    "together": {
        "name": "Together AI",
        "base_url": "https://api.together.xyz/v1",
        "api_shape": "openai",
        "suggested_models": ["black-forest-labs/FLUX.1-schnell-Free", "black-forest-labs/FLUX.1-dev"],
    },
    "fireworks": {
        "name": "Fireworks AI",
        "base_url": "https://api.fireworks.ai/inference/v1",
        "api_shape": "openai",
        "suggested_models": ["accounts/fireworks/models/flux-1-schnell-fp8", "accounts/fireworks/models/playground-v2-5-1024px-aesthetic"],
    },
    "deepinfra": {
        "name": "DeepInfra",
        "base_url": "https://api.deepinfra.com/v1/openai",
        "api_shape": "openai",
        "suggested_models": ["black-forest-labs/FLUX-1-schnell", "stabilityai/sdxl-turbo"],
    },
    "stability": {
        "name": "Stability AI",
        "base_url": "https://api.stability.ai",
        "api_shape": "stability",
        "suggested_models": ["sd3.5-large", "sd3.5-medium", "core", "ultra"],
    },
    "gemini": {
        "name": "Google Gemini / Imagen",
        "base_url": "https://generativelanguage.googleapis.com",
        "api_shape": "gemini",
        "suggested_models": ["imagen-3.0-generate-002", "gemini-2.0-flash-exp-image-generation"],
    },
}


# ── Config storage (parallel to settings.py LLM pattern) ────────────────────


def _user_visuals_dir(user_id: str):
    """The user's 'visuals' room in their data home (data/user_data/<uid>/visuals/)."""
    from app import user_workspace as ws
    return ws.user_dir(user_id, "visuals")


async def load_image_provider_for_user(user_id: str) -> Optional[dict]:
    """Return the image-generation config for this user.

    Source order:
      1. The unified Models list — the saved model the user ticked for image
         output (own config, then admin, resolved inside
         load_llm_capabilities_for_user), via ``pick_image_generator``.
      2. Legacy fallback — a standalone service="image_gen" auth element, if one
         still exists from before image generation was folded into Models.
    Returns {provider, base_url, api_key, model, api_shape} or None.
    """
    # 1) Unified Models list (preferred)
    try:
        from app.admin.settings import (
            load_llm_capabilities_for_user,
            pick_image_generator,
        )
        caps = await load_llm_capabilities_for_user(user_id)
        picked = pick_image_generator(caps)
        if picked:
            return picked
    except Exception as e:
        logger.debug("unified image-generator lookup failed: %s", e)

    # 2) Legacy standalone image_gen config (graceful migration)
    from app.db import get_db
    db = get_db()
    for uid in (user_id, "admin"):
        try:
            elem = await db.auth_element_get(uid, "image_gen", "default")
        except Exception as e:
            logger.debug("auth_element_get failed for image_gen %s: %s", uid, e)
            continue
        if not elem:
            continue
        cfg = elem.get("config") or {}
        if isinstance(cfg, str):
            try:
                cfg = json.loads(cfg)
            except Exception:
                cfg = {}
        cfg["api_key"] = elem.get("secret_ref", "")
        if cfg.get("api_key") and cfg.get("model"):
            return cfg
    return None


# ── Per-image cost estimation ───────────────────────────────────────────────
# Image endpoints that return real usage (OpenRouter chat-completions, Gemini
# generateContent) could report exact cost; OpenAI-shape /images/generations and
# Stability return NO usage at all. For those we estimate from published list
# prices (normalized model name → USD per image) and fall back to the admin's
# flat_image_cost_usd from the billing config. Credits then consume the same
# cost × multiplier as text.
_IMAGE_COST_USD = {
    "dall-e-3": 0.04, "dall-e-2": 0.02, "gpt-image-1": 0.04,
    "flux.1-schnell": 0.003, "flux.1-dev": 0.025,
    "flux-1-schnell": 0.003, "flux-1-dev": 0.025,
    "sd3.5-large": 0.065, "sd3.5-medium": 0.035,
    "core": 0.03, "ultra": 0.08,
    "imagen-3.0-generate-002": 0.04,
}


def _image_cost_usd(model: str, count: int, flat: Optional[float]) -> float:
    per = _IMAGE_COST_USD.get((model or "").lower().strip())
    if per is None:
        per = float(flat if flat and flat > 0 else 0.01)
    return round(per * max(int(count or 1), 1), 6)


# ── The tool ────────────────────────────────────────────────────────────────


async def generate_image(
    user_id: str,
    prompt: str,
    size: str = "1024x1024",
    n: int = 1,
    quality: Optional[str] = None,
    style: Optional[str] = None,
    *,
    agent_id: str = "",
    session_id: str = "",
) -> str:
    """Generate one or more images from a text prompt using the configured provider.

    On success the estimated provider cost flows through the same billing path
    as text runs (plugins.billing.charge.record_and_charge): inherited-model
    runs burn credits (trial grant first), own-key runs are free. Best-effort —
    billing never blocks or fails the image."""
    if not prompt or not prompt.strip():
        return json.dumps({"status": "error", "message": "prompt is required"})

    # Agent's Image-out directive (grep ROLE-DIRECTIVE-INJECT): prepend it
    # to every generated prompt when set, so the agent admin can steer style /
    # safety for THIS agent without touching the ability or the caller's wording.
    try:
        from app.admin.settings import get_agent_model_directives
        _directive = (await get_agent_model_directives(agent_id)).get("image_out") or ""
    except Exception:  # noqa: BLE001
        _directive = ""
    if _directive:
        prompt = f"{_directive}\n\n{prompt}"

    cfg = await load_image_provider_for_user(user_id)
    if not cfg:
        return json.dumps({
            "status": "error",
            "code": "image_gen_not_configured",
            "message": (
                "No image generation model is configured. Ask the admin to open "
                "App Config → Models, save a model that can create images, and tick "
                "its Image-out box."
            ),
        })

    provider = (cfg.get("provider") or "").lower().strip()
    base_url = (cfg.get("base_url") or "").rstrip("/")
    api_key = cfg.get("api_key") or ""
    model = cfg.get("model") or ""
    api_shape = (cfg.get("api_shape")
                 or IMAGE_PROVIDER_PRESETS.get(provider, {}).get("api_shape")
                 or "openai")

    if not api_key or not model:
        return json.dumps({
            "status": "error",
            "message": "Image generation config missing api_key or model.",
        })

    try:
        import httpx
    except Exception as e:
        return json.dumps({"status": "error", "message": f"httpx unavailable: {e}"})

    n = max(1, min(int(n or 1), 4))

    try:
        if api_shape == "openai":
            out = await _openai_compatible(
                user_id=user_id, base_url=base_url, api_key=api_key, model=model,
                prompt=prompt, size=size, n=n, quality=quality, style=style,
                client_cls=httpx.AsyncClient,
            )
        elif api_shape == "stability":
            out = await _stability(
                user_id=user_id, base_url=base_url, api_key=api_key, model=model,
                prompt=prompt, client_cls=httpx.AsyncClient,
            )
        elif api_shape == "gemini":
            out = await _gemini(
                user_id=user_id, base_url=base_url, api_key=api_key, model=model,
                prompt=prompt, n=n, client_cls=httpx.AsyncClient,
            )
        elif api_shape == "openrouter":
            out = await _openrouter(
                user_id=user_id, base_url=base_url, api_key=api_key, model=model,
                prompt=prompt, client_cls=httpx.AsyncClient,
            )
        else:
            return json.dumps({"status": "error", "message": f"Unknown api_shape '{api_shape}'"})
        await _bill_image(user_id=user_id, agent_id=agent_id, session_id=session_id,
                          model=model, provider=provider, result_str=out,
                          entry_id=str(cfg.get("entry_id") or ""))
        return out
    except Exception as e:
        logger.exception("generate_image failed")
        return json.dumps({"status": "error", "message": str(e)})


async def _bill_image(user_id: str, agent_id: str, session_id: str, model: str,
                      provider: str, result_str: str, entry_id: str = "") -> None:
    """Best-effort billing for a successful image render: estimate the provider
    cost, then run the shared charge path (trial grant first, then credits).

    The own-key test for images is the user's IMAGE provider, not their text
    LLM — a user who brought their own image key isn't costing the platform
    anything, so the render is free even if their text model is inherited."""
    if not user_id or not agent_id:
        return
    try:
        payload = json.loads(result_str)
    except Exception:
        return
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        return
    count = int(payload.get("count") or 0)
    if count <= 0:
        return
    try:
        from app.db import get_app_db
        from plugins.billing import pricing as _pricing
        from plugins.billing.charge import record_and_charge
        db = get_app_db()
        cfg = await _pricing.load_effective_config(db, agent_id)
        est_usd = _image_cost_usd(model, count, cfg.get("flat_image_cost_usd"))
        own_key = await user_has_own_image_config(user_id)
        await record_and_charge(
            db, agent_id, user_id,
            provider_cost_cents=int(round(est_usd * 100)),
            model=model or "",
            provider=provider or "",
            entry_id=entry_id or None,
            session_id=session_id or None,
            cost_usd=est_usd,
            cost_source="image_estimate",
            source="image",
            own_key=own_key,
        )
    except Exception as e:
        logger.debug("image billing skipped: %s", e)


async def user_has_own_image_config(user_id: str) -> bool:
    """True when the user's OWN config provides the image generator (their key
    pays for generation). Mirrors load_image_provider_for_user's source order
    but WITHOUT the admin fallback, so "own" means exactly the user's key."""
    try:
        from app.db import get_db
        from app.admin.settings import _rehydrate_llm_keys
        db = get_db()

        def _img_out(e: dict) -> bool:
            return bool(e.get("use_for_image_out") and e.get("image_out_capable")
                        and e.get("model") and (e.get("api_key") or e.get("secret_ref")))

        # 1) Unified Models list — the user's OWN llm config only.
        elem = await db.auth_element_get(user_id, "llm", "default")
        if elem:
            c = elem.get("config") or {}
            if isinstance(c, str):
                try:
                    c = json.loads(c)
                except Exception:
                    c = {}
            _rehydrate_llm_keys(c, elem.get("secret_ref", ""))
            if _img_out(c):
                return True
            for p in (c.get("multi_providers") or []):
                if isinstance(p, dict) and _img_out(p):
                    return True
        # 2) Legacy standalone image_gen config (user's own only).
        legacy = await db.auth_element_get(user_id, "image_gen", "default")
        if legacy:
            lc = legacy.get("config") or {}
            if isinstance(lc, str):
                try:
                    lc = json.loads(lc)
                except Exception:
                    lc = {}
            if lc.get("model") and (lc.get("api_key") or legacy.get("secret_ref")):
                return True
    except Exception:
        pass
    return False


# ── Per-provider implementations ────────────────────────────────────────────


async def _save_png_bytes(user_id: str, raw: bytes, ext: str = "png") -> str:
    """Persist raw image bytes under visuals/users/{user_id}/ and return the
    relative URL the chat UI can use to render it."""
    from app import user_workspace as ws
    from app.encryption.workspace_files import write_encrypted_file
    out_dir = _user_visuals_dir(user_id)
    fname = f"imggen-{int(time.time())}-{uuid.uuid4().hex[:8]}.{ext}"
    path = out_dir / fname
    await write_encrypted_file(user_id, path, raw)
    return ws.public_url(user_id, "visuals", fname)


async def _save_from_url(user_id: str, url: str, http_client) -> Optional[str]:
    """Download a provider-hosted URL and persist a local copy. Used so images
    survive after provider URLs expire."""
    try:
        resp = http_client.get(url)
        if resp.status_code != 200:
            return None
        ct = resp.headers.get("content-type", "image/png")
        ext = "png" if "png" in ct else ("jpg" if "jpeg" in ct or "jpg" in ct else "png")
        return await _save_png_bytes(user_id, resp.content, ext=ext)
    except Exception:
        return None


async def _openai_compatible(*, user_id, base_url, api_key, model, prompt, size, n, quality, style, client_cls) -> str:
    """POST {base_url}/images/generations with OpenAI's payload shape.

    Most providers return either a `data[].url` (transient) or `data[].b64_json`.
    We persist either form to disk and return the local URLs.
    """
    url = f"{base_url}/images/generations"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload: dict = {"model": model, "prompt": prompt, "n": n, "size": size}
    if quality:
        payload["quality"] = quality
    if style:
        payload["style"] = style
    async with client_cls(timeout=120.0) as client:
        resp = await client.post(url, headers=headers, json=payload)
        if resp.status_code != 200:
            return json.dumps({
                "status": "error",
                "message": f"Provider HTTP {resp.status_code}: {resp.text[:500]}",
            })
        data = resp.json() or {}
        items = data.get("data") or []
        saved = []
        for item in items:
            if item.get("b64_json"):
                raw = base64.b64decode(item["b64_json"])
                saved.append(await _save_png_bytes(user_id, raw, ext="png"))
            elif item.get("url"):
                rel = await _save_from_url(user_id, item["url"], client)
                saved.append(rel or item["url"])
        return json.dumps({
            "status": "ok",
            "provider": "openai_compatible",
            "model": model,
            "images": saved,
            "count": len(saved),
        })


async def _stability(*, user_id, base_url, api_key, model, prompt, client_cls) -> str:
    """Stability AI v2beta REST endpoint. Model names: 'core', 'ultra', 'sd3.5-large', etc.
    Endpoint path varies by model family."""
    model_lc = (model or "core").lower()
    if model_lc.startswith("sd3"):
        path = "/v2beta/stable-image/generate/sd3"
        form = {"prompt": (None, prompt), "model": (None, model_lc), "output_format": (None, "png")}
    elif model_lc == "ultra":
        path = "/v2beta/stable-image/generate/ultra"
        form = {"prompt": (None, prompt), "output_format": (None, "png")}
    else:
        path = "/v2beta/stable-image/generate/core"
        form = {"prompt": (None, prompt), "output_format": (None, "png")}
    url = f"{base_url}{path}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "image/*",
    }
    async with client_cls(timeout=180.0) as client:
        resp = await client.post(url, headers=headers, files=form)
        if resp.status_code != 200:
            return json.dumps({
                "status": "error",
                "message": f"Stability HTTP {resp.status_code}: {resp.text[:500]}",
            })
        rel = await _save_png_bytes(user_id, resp.content, ext="png")
        return json.dumps({
            "status": "ok",
            "provider": "stability",
            "model": model,
            "images": [rel],
            "count": 1,
        })


async def _gemini(*, user_id, base_url, api_key, model, prompt, n, client_cls) -> str:
    """Google Generative Language API — supports both Imagen (`predict`) and
    Gemini 2.0 Flash image (`generateContent`).

    The two endpoint paths return different payloads:
      - imagen-* → `predictions[].bytesBase64Encoded`
      - gemini-*-image* → `candidates[].content.parts[].inlineData.data` (b64)
    """
    model_lc = (model or "").lower()
    headers = {"Content-Type": "application/json"}
    if model_lc.startswith("imagen"):
        url = f"{base_url}/v1beta/models/{model}:predict?key={api_key}"
        payload = {
            "instances": [{"prompt": prompt}],
            "parameters": {"sampleCount": n},
        }
    else:
        url = f"{base_url}/v1beta/models/{model}:generateContent?key={api_key}"
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseModalities": ["IMAGE"]},
        }
    async with client_cls(timeout=180.0) as client:
        resp = await client.post(url, headers=headers, json=payload)
        if resp.status_code != 200:
            return json.dumps({
                "status": "error",
                "message": f"Gemini HTTP {resp.status_code}: {resp.text[:500]}",
            })
        data = resp.json() or {}
        saved: list[str] = []
        for pred in (data.get("predictions") or []):
            b64 = pred.get("bytesBase64Encoded")
            if b64:
                saved.append(await _save_png_bytes(user_id, base64.b64decode(b64), ext="png"))
        for cand in (data.get("candidates") or []):
            for part in (cand.get("content", {}).get("parts") or []):
                inline = part.get("inlineData") or part.get("inline_data") or {}
                b64 = inline.get("data")
                if b64:
                    saved.append(await _save_png_bytes(user_id, base64.b64decode(b64), ext="png"))
        return json.dumps({
            "status": "ok",
            "provider": "gemini",
            "model": model,
            "images": saved,
            "count": len(saved),
        })


async def _openrouter(*, user_id, base_url, api_key, model, prompt, client_cls) -> str:
    """OpenRouter image generation — via /chat/completions, NOT /images/generations.

    OpenRouter routes image models through the chat API: send the prompt as a user
    message with ``modalities: ["image", "text"]``; the result image(s) come back as
    data URLs under ``choices[].message.images[].image_url.url``. We decode and
    persist them like every other provider.
    """
    url = f"{base_url}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "modalities": ["image", "text"],
    }
    async with client_cls(timeout=180.0) as client:
        resp = await client.post(url, headers=headers, json=payload)
        if resp.status_code != 200:
            return json.dumps({
                "status": "error",
                "message": f"OpenRouter HTTP {resp.status_code}: {resp.text[:500]}",
            })
        data = resp.json() or {}
        saved: list[str] = []
        for choice in (data.get("choices") or []):
            msg = (choice or {}).get("message") or {}
            for img in (msg.get("images") or []):
                # OpenRouter shape: {"type":"image_url","image_url":{"url":"data:image/png;base64,..."}}
                src = ((img or {}).get("image_url") or {}).get("url") or img.get("url") or ""
                if src.startswith("data:"):
                    try:
                        b64 = src.split(",", 1)[1]
                        saved.append(await _save_png_bytes(user_id, base64.b64decode(b64), ext="png"))
                    except Exception:
                        continue
                elif src.startswith("http"):
                    rel = await _save_from_url(user_id, src, client)
                    if rel:
                        saved.append(rel)
        if not saved:
            # No image came back — surface any text the model returned to explain why.
            txt = ""
            for choice in (data.get("choices") or []):
                txt = ((choice or {}).get("message") or {}).get("content") or ""
                if txt:
                    break
            return json.dumps({
                "status": "error",
                "message": f"OpenRouter returned no image. Model said: {txt[:300]}" if txt
                else "OpenRouter returned no image (the model may not support image output).",
            })
        return json.dumps({
            "status": "ok",
            "provider": "openrouter",
            "model": model,
            "images": saved,
            "count": len(saved),
        })


# ── Tool schema ─────────────────────────────────────────────────────────────


TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "prompt": {"type": "string", "description": "What to draw — be specific and visual."},
        "size": {
            "type": "string",
            "description": "WxH for OpenAI-shape providers (e.g. '1024x1024', '1024x1792'). Ignored by Stability/Gemini.",
            "default": "1024x1024",
        },
        "n": {
            "type": "integer",
            "description": "How many images to generate (1–4). Some providers only honor n=1.",
            "default": 1,
        },
        "quality": {"type": "string", "description": "Optional OpenAI quality flag ('standard' | 'hd')."},
        "style": {"type": "string", "description": "Optional OpenAI style flag ('vivid' | 'natural')."},
    },
    "required": ["prompt"],
}


# ── Drop-in contract ─────────────────────────────────────────────────────────


def build_tools(*, user_id: str = "", session_id: str = "", agent_id: str = "",
                agent_template_id: Optional[str] = None, enabled_providers=None, **_ctx):
    """Return {tool_name: handler} for the image_generation ability.

    Closes the self-contained ``generate_image`` handler over ``user_id`` exactly
    as the old loader block did, and publishes the schema into module-level
    TOOL_SCHEMAS so the loader (which reads it AFTER this call) sees the live
    schema.
    """
    async def _generate_image_wrapper(prompt: str, size: str = "1024x1024",
                                      n: int = 1, quality: Optional[str] = None,
                                      style: Optional[str] = None):
        return await generate_image(
            user_id=user_id, prompt=prompt, size=size, n=n,
            quality=quality, style=style,
            agent_id=agent_id or "", session_id=session_id or "",
        )

    TOOL_SCHEMAS.clear()
    TOOL_SCHEMAS.update({"generate_image": TOOL_PARAMETERS})

    return {"generate_image": _generate_image_wrapper}
