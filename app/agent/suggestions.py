"""Suggested-Replies engine.

A SILENT, single-shot LLM helper that role-plays as the user and predicts the
next message(s) they are likely to want to send. Used to populate the tappable
suggestion chips above the chat pill.

Design constraints (the whole point of this module):
  - It NEVER runs the agent loop, never persists to the chat, never streams over
    the agent WebSocket, never calls tools. One prompt in, a small JSON array of
    candidate user messages out. That is how the "process work" stays hidden.
  - The persona/prompts come from the ``user-impersonator`` system agent template
    (``data/agents/user-impersonator.json``).
  - The runtime tunables (mode / count / idle seconds) live in a small per-machine
    runtime file (``data/config/suggestions.json``, gitignored) so the Agents-page
    config panel can edit them without touching the template-seeding machinery.

A SECOND, sibling helper lives here too: :func:`generate_page_suggestions`. It is
the same one-shot/no-loop idea but for the **page-assistant chat pills** (the
floating "advanced pill" on admin pages like App Settings). Instead of predicting
a reply from chat history, it reads the hovered page AREA's context out of
``app/defaults/app-prompts.json`` (``page_assistants.<page>.areas.<area>``) and
returns a few short, varied example REQUESTS an admin might type there (e.g. on
the Main Panel Pages area: "Build a sales dashboard page", "Add a blog"). The
frontend rotates these through the pill's placeholder in place of the static one.
"""

import json
import logging
import os
from typing import Any, Dict, List, Optional

from app.util.paths import app_prompts_path

logger = logging.getLogger(__name__)

# Repo root is two levels up from app/agent/suggestions.py (app/agent -> app -> root)
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_THIS_DIR))

_IMPERSONATOR_JSON = os.path.join(_REPO_ROOT, "data", "agents", "user-impersonator.json")
_RUNTIME_CONFIG = os.path.join(_REPO_ROOT, "data", "config", "suggestions.json")

# Mode values the config panel switches between.
VALID_MODES = ("off", "on", "scheduler")  # "scheduler" == on + idle refresh

_DEFAULTS = {
    "mode": "off",
    "count": 3,
    "idle_seconds": 25,
}


def _load_impersonator_def() -> Dict[str, Any]:
    """Read the impersonator system-agent template from its JSON file.

    Falls back to a minimal built-in persona if the file is missing so the
    feature degrades gracefully rather than erroring.
    """
    try:
        with open(_IMPERSONATOR_JSON, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("suggestions: could not read impersonator template: %s", e)
        return {
            "system_prompt": (
                "You role-play as the user. Given the conversation, output ONLY a "
                "JSON array of 1-3 short messages the user is likely to send next, "
                "in the user's voice. No prose, no code fences."
            ),
            "model": None,
            "temperature": 0.7,
            "max_tokens": 300,
            "metadata": {},
        }


def load_runtime_config() -> Dict[str, Any]:
    """Return {mode, count, idle_seconds}, layering: file overrides → template
    metadata defaults → hard defaults."""
    cfg = dict(_DEFAULTS)
    # Template metadata defaults
    try:
        meta = (_load_impersonator_def().get("metadata") or {})
        if meta.get("suggestion_mode") in VALID_MODES:
            cfg["mode"] = meta["suggestion_mode"]
        if isinstance(meta.get("suggestion_count"), int):
            cfg["count"] = meta["suggestion_count"]
        if isinstance(meta.get("idle_seconds"), int):
            cfg["idle_seconds"] = meta["idle_seconds"]
    except Exception:
        pass
    # Per-machine file overrides
    try:
        if os.path.exists(_RUNTIME_CONFIG):
            with open(_RUNTIME_CONFIG, "r", encoding="utf-8") as fh:
                saved = json.load(fh) or {}
            if saved.get("mode") in VALID_MODES:
                cfg["mode"] = saved["mode"]
            if isinstance(saved.get("count"), int):
                cfg["count"] = max(1, min(5, saved["count"]))
            if isinstance(saved.get("idle_seconds"), int):
                cfg["idle_seconds"] = max(5, min(600, saved["idle_seconds"]))
    except Exception as e:
        logger.warning("suggestions: could not read runtime config: %s", e)
    return cfg


def save_runtime_config(updates: Dict[str, Any]) -> Dict[str, Any]:
    """Merge ``updates`` into the runtime config file and return the new config."""
    cfg = load_runtime_config()
    if updates.get("mode") in VALID_MODES:
        cfg["mode"] = updates["mode"]
    if isinstance(updates.get("count"), int):
        cfg["count"] = max(1, min(5, updates["count"]))
    if isinstance(updates.get("idle_seconds"), int):
        cfg["idle_seconds"] = max(5, min(600, updates["idle_seconds"]))
    try:
        os.makedirs(os.path.dirname(_RUNTIME_CONFIG), exist_ok=True)
        with open(_RUNTIME_CONFIG, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)
    except Exception as e:
        logger.warning("suggestions: could not write runtime config: %s", e)
    return cfg


def _parse_suggestions(raw: str, limit: int) -> List[str]:
    """Tolerantly pull a JSON array of short strings out of the model output."""
    if not raw:
        return []
    text = raw.strip()
    # Strip code fences if the model wrapped the array.
    if text.startswith("```"):
        text = text.strip("`")
        nl = text.find("\n")
        if nl != -1:
            text = text[nl + 1:]
        text = text.strip()
    # Find the first JSON array in the text.
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end != -1 and end > start:
        text = text[start:end + 1]
    out: List[str] = []
    try:
        arr = json.loads(text)
        if isinstance(arr, list):
            for item in arr:
                if isinstance(item, str):
                    s = item.strip()
                elif isinstance(item, dict):
                    s = str(item.get("text") or item.get("message") or "").strip()
                else:
                    s = ""
                if s:
                    out.append(s)
    except Exception:
        # Last resort: split lines that look like list items.
        for line in raw.splitlines():
            s = line.strip().lstrip("-*0123456789. ").strip().strip('"')
            if s and not s.startswith("[") and not s.startswith("]"):
                out.append(s)
    # De-dup (case-insensitive) preserving order, then clip to limit.
    seen = set()
    deduped = []
    for s in out:
        key = s.lower()
        if key not in seen:
            seen.add(key)
            deduped.append(s)
    return deduped[:limit]


async def generate_suggestions(
    db: Any,
    user_id: str,
    session_id: Optional[str],
    *,
    count: Optional[int] = None,
) -> List[str]:
    """Produce up to ``count`` suggested next user-messages for the given session.

    Returns an empty list when the engine is off, credentials are missing, or
    anything fails — callers must treat suggestions as best-effort.
    """
    cfg = load_runtime_config()
    if cfg["mode"] == "off":
        return []
    limit = count or cfg["count"]

    impersonator = _load_impersonator_def()
    system_prompt = impersonator.get("system_prompt") or ""
    temperature = impersonator.get("temperature", 0.7)
    max_tokens = impersonator.get("max_tokens", 300)
    model = impersonator.get("model") or os.environ.get("LLM_MODEL") or os.environ.get("OPENROUTER_MODEL") or ""

    if not model:
        logger.info("suggestions: no model configured; skipping")
        return []

    # Build the conversation history (best-effort; empty for a fresh chat).
    history: List[Dict[str, Any]] = []
    if session_id:
        try:
            from app.agent.session_history import build_openai_history_from_session
            history = await build_openai_history_from_session(db, user_id, session_id)
        except Exception as e:
            logger.warning("suggestions: could not load history: %s", e)
            history = []

    # Flatten the history into a readable transcript so the impersonator can read
    # it as context without us having to forward tool/system roles verbatim.
    transcript_lines: List[str] = []
    for m in history[-20:]:  # last ~20 turns is plenty of context
        role = m.get("role")
        content = m.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        if role == "user":
            transcript_lines.append(f"User: {content.strip()}")
        elif role == "assistant":
            transcript_lines.append(f"Assistant: {content.strip()}")
    transcript = "\n".join(transcript_lines).strip()

    if transcript:
        user_msg = (
            "Conversation so far:\n\n" + transcript +
            f"\n\nReturn a JSON array of up to {limit} short messages the user is "
            "likely to want to send next, in the user's voice. JSON array only."
        )
    else:
        user_msg = (
            "The conversation has not started yet. Return a JSON array of up to "
            f"{limit} short, natural opening messages this user might send to the "
            "assistant. JSON array only."
        )

    try:
        from app.agent.loop import _get_client
        client = _get_client()
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_msg},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
        )
        raw = resp.choices[0].message.content or ""
        try:
            _u = getattr(resp, "usage", None)
            if _u:
                from plugins.billing.usage import record_background_usage
                await record_background_usage(
                    model=model,
                    input_tokens=getattr(_u, "prompt_tokens", 0) or 0,
                    output_tokens=getattr(_u, "completion_tokens", 0) or 0,
                    label="suggest",
                )
        except Exception:
            pass
    except Exception as e:
        logger.warning("suggestions: LLM call failed: %s", e)
        return []

    return _parse_suggestions(raw, limit)


def _load_app_prompts() -> Dict[str, Any]:
    """Read app/defaults/app-prompts.json (the page-assistant text catalog)."""
    try:
        with open(app_prompts_path(), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("suggestions: could not read app-prompts.json: %s", e)
        return {}


async def _resolve_default_llm(user_id: Optional[str]) -> Dict[str, Any]:
    """Resolve the app's DEFAULT text model + provider key/base, mirroring how a
    run with no agent/session override would. Env vars (and a literal default) are
    the last-resort fallback. Same approach compaction uses, so page suggestions
    work without the env-only model that the chat-suggestions engine relies on."""
    model = base_url = api_key = ""
    try:
        from app.admin.settings import resolve_active_model, _resolve_user_config
        ucfg = await _resolve_user_config(user_id) if user_id else {}
        api_key = (ucfg or {}).get("api_key") or ""
        base_url = (ucfg or {}).get("base_url") or ""
        if user_id:
            active = await resolve_active_model(user_id)
            model = (active or {}).get("model") or ""
            base_url = (active or {}).get("base_url") or base_url
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("suggestions: default-model resolve failed: %s", e)
    model = (model or os.environ.get("LLM_MODEL")
             or os.environ.get("OPENROUTER_MODEL") or "")
    base_url = (base_url or os.environ.get("LLM_BASE_URL")
                or os.environ.get("OPENROUTER_BASE_URL") or "")
    api_key = (api_key or os.environ.get("LLM_API_KEY")
               or os.environ.get("OPENROUTER_API_KEY") or "")
    return {"model": model, "base_url": base_url, "api_key": api_key}


def _existing_page_labels(area: str) -> List[str]:
    """The display names of pages the app ALREADY has for a page area, so the
    suggester can propose NEW ones instead of duplicating what's there. Only the
    page areas (main_panel / admin_panel) map to a catalog; others return []."""
    kind = {"main_panel": "main", "admin_panel": "admin"}.get(area)
    if not kind:
        return []
    try:
        from app.ui_pages import ui_catalog
        rows = ui_catalog().get(kind) or []
        return [r.get("label") for r in rows if r.get("label")]
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("suggestions: page catalog read failed: %s", e)
        return []


async def _recall_user_context(user_id: Optional[str], query: str, limit: int = 4) -> List[str]:
    """Best-effort recall of what we know about THIS user from the default
    WebAgent's memory (cross-session) so suggestions can be personalised — e.g.
    a user who runs a bakery gets "Add a bakery menu page". Uses the full hybrid
    (keyword + semantic) recall path: embeddings are now in-process/local by
    default (no network round-trip, no API cost), so the semantic leg is fast
    enough to run even on hover and catches related-but-differently-worded
    memories keyword search would miss. Degrades to keyword-only automatically if
    the local engine isn't installed. Returns short snippets; empty on any failure
    (personalisation is optional)."""
    if not user_id or not (query or "").strip():
        return []
    try:
        from app.db import get_db
        db = get_db()
        rows = await db.memory_search(user_id, query, limit=limit, vector=True)
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("suggestions: memory recall failed: %s", e)
        return []
    out: List[str] = []
    for r in rows or []:
        title = (r.get("title") or "").strip()
        truth = " ".join((r.get("compiled_truth") or "").split()).strip()
        if truth:
            out.append(((title + ": ") if title else "") + truth[:160])
        elif title:
            out.append(title)
    return out[:limit]


async def generate_page_suggestions(
    page: str,
    area: str,
    *,
    count: int = 4,
    user_id: Optional[str] = None,
) -> List[str]:
    """Produce up to ``count`` short example REQUESTS for a page-assistant area.

    ``page`` is a key under ``page_assistants`` in app-prompts.json (e.g.
    ``app_settings``) and ``area`` is one of that page's ``areas`` keys (e.g.
    ``main_panel``). The returned strings are short, varied, first-person things
    an admin might type into that page's chat pill — the frontend shows them, one
    at a time on rotation, in place of the pill's static placeholder.

    Returns an empty list on any failure so the caller can fall back to the
    static placeholder.
    """
    limit = max(1, min(8, int(count or 4)))

    prompts = _load_app_prompts()
    page_cfg = ((prompts.get("page_assistants") or {}).get(page)) or {}
    areas = page_cfg.get("areas") or {}
    area_cfg = areas.get(area) or {}
    if not area_cfg:
        return []

    label = area_cfg.get("label") or area
    # Prefer the human-facing `suggest` hint (short, idea-rich); fall back to the
    # implementation-heavy agent `prompt` only if no hint is provided.
    context = (area_cfg.get("suggest") or area_cfg.get("prompt") or "").strip()

    # Resolve the app's default model + provider key (robust, not env-only).
    llm = await _resolve_default_llm(user_id)
    model = llm["model"]
    if not model or not llm["api_key"]:
        logger.info("suggestions: no model/key configured; skipping page suggestions")
        return []

    # Extra context so suggestions feel fresh + personal, not canned:
    #   • existing pages → propose NEW ones, never duplicate what's already there.
    #   • the default WebAgent's memory of this user → personalise some ideas.
    existing_pages = _existing_page_labels(area)
    memory_query = " ".join(filter(None, [label, context]))[:300]
    user_memory = await _recall_user_context(user_id, memory_query)

    system_prompt = (
        "You are WebAgent, the manager of this app, suggesting things its ADMIN "
        "might ask you to build or change on one section of a settings page. "
        f"Output ONLY a JSON array of {limit} short, varied, first-person requests "
        "phrased as imperatives (e.g. \"Build a sales dashboard page\", "
        "\"Add a blog\"). Keep each under ~60 characters, make them concrete and "
        "distinct, and stay strictly on the topic of the given section. Always "
        "keep the list FRESH: blend an evergreen idea or two (like a blog or a "
        "store) with novel and personalised ones. No prose, no numbering, no code "
        "fences — just the JSON array."
    )
    user_msg_parts = [f"Page section: {label}."]
    if context:
        user_msg_parts.append(f"What this section is for / good evergreen ideas: {context}")
    if existing_pages:
        user_msg_parts.append(
            "The app ALREADY has these pages — do NOT suggest duplicates; suggest "
            "NEW, complementary ones the admin doesn't have yet: "
            + ", ".join(existing_pages)
        )
    if user_memory:
        user_msg_parts.append(
            "What we know about this admin/user (from memory) — personalise SOME "
            "suggestions to them where it fits, but keep the rest broadly useful:\n- "
            + "\n- ".join(user_memory)
        )
    user_msg_parts.append(
        f"Return a JSON array of {limit} short example requests an admin might "
        "type here."
    )
    user_msg = "\n\n".join(user_msg_parts)

    try:
        try:
            from openai import AsyncOpenAI
        except ImportError:  # pragma: no cover
            from app.openai_compat import AsyncOpenAI
        client = AsyncOpenAI(base_url=llm["base_url"], api_key=llm["api_key"], timeout=60.0)
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.85,  # a touch hotter than chat suggestions for variety
            max_tokens=400,     # headroom: the richer (pages + memory) prompt can
                                # make the model pad before the JSON array
        )
        raw = resp.choices[0].message.content or ""
        try:
            _u = getattr(resp, "usage", None)
            if _u:
                from plugins.billing.usage import record_background_usage
                await record_background_usage(
                    model=model,
                    input_tokens=getattr(_u, "prompt_tokens", 0) or 0,
                    output_tokens=getattr(_u, "completion_tokens", 0) or 0,
                    label="page-suggest",
                )
        except Exception:
            pass
    except Exception as e:
        logger.warning("suggestions: page-suggestion LLM call failed: %s", e)
        return []

    parsed = _parse_suggestions(raw, limit)
    if not parsed:
        logger.warning("suggestions: page-suggestion empty parse for area=%s; raw=%r",
                       area, raw[:300])
    return parsed
