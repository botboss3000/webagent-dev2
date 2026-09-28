"""Attachment type-router — the policy layer that decides what happens to a file
a user attaches, based on its type and the agent's actual model capabilities.

WHY THIS EXISTS
    A user can attach an image (later: a document, a video, audio …) to an agent
    whose own model can't read that media. The agent must NEVER silently inline a
    file a blind model can't use (which made it hallucinate "I don't see an
    image"), and it must NEVER pretend it read a file it can't. This module routes
    each attachment to the ability that handles its type, checks that the ability
    is actually enabled for the agent, and returns a decision the chat pipeline
    executes synchronously (describe via a one-shot worker, leave inlined, or tell
    the agent it can't read the file).

DROP-IN CONTRACT
    An attachment-handling ability ships a companion ``<ability>.json`` next to its
    ``.py`` / ``.skill.md`` in plugins/abilities/, declaring the types it handles
    plus its prompt + guidance copy. New file-type abilities (document, video …)
    drop in with NO edit here. JSON shape (all keys optional except ``handles``)::

        {
          "handles": ["image/png", "image/jpeg", "image/gif", "image/webp"],
                       # explicit mimes, "image/*" wildcards, or a bare category
                       # ("image" | "document" | "audio" | "video")
          "worker_system": "system prompt for the one-shot describe worker; may
                            use {context} and {request} placeholders",
          "worker_instruction": "the user-turn line for the worker (optional)",
          "guidance": {
            "switch_available": "injected when a text+image brain exists to switch to",
            "describe_only":    "injected when only a worker is available",
            "not_enabled":      "injected when the ability is OFF / unconfigured —
                                 may use {names}"
          }
        }

This module is pure policy: it loads JSON, maps mime→ability, checks enablement,
and returns a plan. The actual model call + persistence + pipeline events stay in
app/api/chat.py (which owns the db + transport).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_ABILITIES_DIR = Path(__file__).resolve().parent.parent.parent / "plugins" / "abilities"

# Built-in defaults so routing is robust even if an ability ships no JSON (or a
# partial one). Kept generic — the JSON overrides these per ability.
_DEFAULT_GUIDANCE = {
    "switch_available": (
        "An attached file could not be read by your current model, so a "
        "description from a capable model has been added below. If it isn't enough "
        "for the user's request, you may switch to a capable model with "
        "set_model('{switch_target}') and inspect it directly, then revert with "
        "set_model('default')."
    ),
    "describe_only": (
        "An attached file could not be read by your current model, so a "
        "description from a capable model has been added below. If you need a "
        "sharper detail, call process_image with a precise question."
    ),
    "not_enabled": (
        "An image was attached ({names}) but the ability that reads it is not "
        "enabled for you. Tell the user plainly that the ability is turned off and "
        "that an admin can enable it for this agent. Do NOT say it is a limitation "
        "of your model, and do NOT guess or invent its contents."
    ),
    "no_vision_model": (
        "An image was attached ({names}) but no model able to read it is configured. "
        "Tell the user plainly that it isn't set up and that an admin can add a "
        "capable model. Do NOT guess or invent its contents."
    ),
}


def mime_category(mime: str) -> str:
    """Coarse category from a mime-type: image | audio | video | document | other."""
    m = (mime or "").lower()
    if m.startswith("image/"):
        return "image"
    if m.startswith("audio/"):
        return "audio"
    if m.startswith("video/"):
        return "video"
    if m in ("application/pdf", "application/json") or m.startswith("text/"):
        return "document"
    return "other"


_HANDLERS_CACHE: Optional[Dict[str, Dict[str, Any]]] = None


def _load_handlers() -> Dict[str, Dict[str, Any]]:
    """Scan plugins/abilities/*.json once and return {ability_id: config} for any
    that declare ``handles``. Cached; call reset_cache() in tests if needed."""
    global _HANDLERS_CACHE
    if _HANDLERS_CACHE is not None:
        return _HANDLERS_CACHE
    handlers: Dict[str, Dict[str, Any]] = {}
    try:
        for path in sorted(_ABILITIES_DIR.glob("*.json")):
            try:
                cfg = json.loads(path.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning("attachment_router: bad JSON %s: %s", path.name, e)
                continue
            if isinstance(cfg, dict) and cfg.get("handles"):
                handlers[path.stem] = cfg
    except Exception as e:
        logger.warning("attachment_router: could not scan ability JSONs: %s", e)
    _HANDLERS_CACHE = handlers
    return handlers


def reset_cache() -> None:
    global _HANDLERS_CACHE
    _HANDLERS_CACHE = None


def _handles_match(handles: Any, mime: str) -> bool:
    mime = (mime or "").lower()
    cat = mime_category(mime)
    for h in (handles or []):
        h = str(h).lower()
        if h == mime or h == cat:
            return True
        if h.endswith("/*") and mime.startswith(h[:-1]):  # e.g. "image/*"
            return True
    return False


def handler_for_mime(mime: str) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """Return (ability_id, config) for the ability that declares this mime, else
    (None, None)."""
    for ability_id, cfg in _load_handlers().items():
        if _handles_match(cfg.get("handles"), mime):
            return ability_id, cfg
    return None, None


async def _ability_enabled(agent_id: str, user_id: str, ability_id: str) -> bool:
    """True iff the ability is enabled for this agent (uses the same source the
    tool loader uses, so the router never disagrees with what tools were built)."""
    if not (agent_id and ability_id):
        return False
    try:
        from app.integrations import gather_enabled_providers
        return ability_id in await gather_enabled_providers(agent_id, user_id)
    except Exception as e:
        logger.debug("attachment_router: enablement check failed: %s", e)
        return False


async def plan_image_attachments(
    *,
    agent_id: str,
    user_id: str,
    image_atts: List[Dict[str, Any]],
    caps: dict,
    routing: dict,
    context: str = "",
    request: str = "",
) -> Dict[str, Any]:
    """Decide how to handle a group of image attachments for this turn.

    All images in one message face the same brain, so one decision covers them.
    Returns::

        {
          "mode": "inline" | "describe" | "unreadable",
          "describer": dict | None,      # worker model config (describe mode)
          "worker_system": str,          # context-filled system prompt for worker
          "worker_instruction": str|None,
          "guidance": str,               # foreground note to inject (may be "")
        }

    - inline:      the turn model can see images → leave them inlined, no worker.
    - describe:    blind brain + vision enabled + a worker is available → describe.
    - unreadable:  vision ability OFF or no vision model configured → fallback note,
                   caller must NOT inline the image to the blind model.
    """
    ability_id, cfg = handler_for_mime((image_atts[0].get("mime_type") if image_atts else "") or "image/png")
    ability_id = ability_id or "image_vision"
    cfg = cfg or {}
    guidance_cfg = {**_DEFAULT_GUIDANCE, **(cfg.get("guidance") or {})}

    # Brain can see images natively → nothing to do; keep them inlined.
    if routing.get("sees_natively"):
        return {"mode": "inline", "describer": None, "worker_system": "",
                "worker_instruction": None, "guidance": ""}

    enabled = await _ability_enabled(agent_id, user_id, ability_id)
    describer = routing.get("describer")

    if not enabled or not describer:
        names = ", ".join(a.get("original_name", "image") for a in image_atts) or "an image"
        # Distinguish the two unreadable causes so the agent tells the user the
        # accurate reason: the ability is OFF vs. no capable model is configured.
        if not enabled:
            note = guidance_cfg["not_enabled"].format(names=names)
            reason = "ability_disabled"
        else:
            note = guidance_cfg.get("no_vision_model", guidance_cfg["not_enabled"]).format(names=names)
            reason = "no_vision_model"
        return {"mode": "unreadable", "describer": None, "worker_system": "",
                "worker_instruction": None, "guidance": note,
                "ability_id": ability_id, "reason": reason}

    # Describe mode — build the context-tailored worker prompt + the foreground note.
    worker_system = (cfg.get("worker_system") or "").format(
        context=context or "(no additional context)",
        request=request or "(the user attached an image)",
    ) if cfg.get("worker_system") else ""
    worker_instruction = cfg.get("worker_instruction")

    # The "take over on a vision model" hint is only honest when the agent actually
    # has the Model Switcher ability (set_model). Without it, steer to process_image.
    switch_targets = routing.get("vision_switch_targets") or []
    can_switch = bool(switch_targets) and await _ability_enabled(agent_id, user_id, "model_switcher")
    if can_switch:
        guidance = guidance_cfg["switch_available"].format(switch_target=switch_targets[0])
    else:
        guidance = guidance_cfg["describe_only"]

    return {
        "mode": "describe",
        "describer": describer,
        "worker_system": worker_system,
        "worker_instruction": worker_instruction,
        "guidance": guidance,
    }
