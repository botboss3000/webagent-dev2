"""Primary-agent work introductions emitted before run preparation.

This is deliberately not a Scout/Manager call.  It resolves the exact model
selected for the primary agent and asks that model for a tiny, tool-free public
statement before memory lookup, ability discovery, or tool preparation begins.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import re
from typing import Any, Dict, List, Mapping, Optional

logger = logging.getLogger(__name__)

_BANNED = (
    "getting oriented",
    "gathering the relevant context",
    "i'll keep this provisional",
    "prepared while the main response",
)

_SYSTEM_PROMPT = """You are the primary working agent about to handle the user's request.
Return exactly one JSON object with:
{"requires_work": boolean, "task_type": "answer|action|research|code|diagnose|mixed", "message": string}

Set requires_work=false and message="" only for a greeting, acknowledgement, or a question you can answer directly without inspecting anything or using tools.
When work is required, write one or two concise, user-facing sentences describing what YOU will do first and how you will verify or complete it.
- Name the concrete subject or outcome from the user's request.
- For action requests, say you will check the available tools/permissions and the safe action path before acting.
- For research or finding requests, name the sources or evidence you will check.
- For code tasks, mention the relevant code paths and verification/tests.
- For diagnosis, mention symptoms, likely failure points, and diagnostics.
- For a short follow-up, connect it to the recent conversation shown below.
- Do not claim a tool or permission has already been verified.
- Do not expose chain-of-thought, hidden reasoning, passwords, tokens, verification codes, or other secrets.
- Never say only that you are getting oriented, gathering context, or preparing a response.
- This is a work introduction, not the final answer.
Return JSON only."""


def _extract_json(value: Any) -> Optional[Dict[str, Any]]:
    text = str(value or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1] if "\n" in text else text
        text = text.rsplit("```", 1)[0].strip()
    try:
        parsed = json.loads(text)
    except Exception:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            parsed = json.loads(text[start:end + 1])
        except Exception:
            return None
    return parsed if isinstance(parsed, dict) else None


def normalize_intro_result(raw: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    requires_work = bool(raw.get("requires_work"))
    task_type = str(raw.get("task_type") or "mixed").strip().lower()
    if task_type not in {"answer", "action", "research", "code", "diagnose", "mixed"}:
        task_type = "mixed"
    message = " ".join(str(raw.get("message") or "").split()).strip()[:600]
    if not requires_work:
        return {"requires_work": False, "task_type": task_type, "message": ""}
    lowered = message.lower()
    if len(message) < 20 or any(phrase in lowered for phrase in _BANNED):
        return None
    return {"requires_work": True, "task_type": task_type, "message": message}


def _recent_context(rows: List[Any], current_interaction_id: str) -> str:
    compact: List[str] = []
    for row in rows:
        row_id = str(getattr(row, "id", "") or "")
        role = str(getattr(row, "role", "") or "")
        content = str(getattr(row, "content", "") or "").strip()
        if row_id == current_interaction_id or role not in {"user", "assistant"} or not content:
            continue
        compact.append(f"{role.upper()}: {content[:800]}")
    return "\n".join(compact[-6:])


def fallback_intro(user_message: str) -> Dict[str, Any]:
    """Return a request-specific fail-open intro when the primary call fails.

    The selected model remains the preferred author. This bounded fallback
    prevents provider latency from making memory/tools appear first while still
    naming the concrete task instead of emitting a generic orientation note.
    """
    raw = " ".join(str(user_message or "").split()).strip()
    safe = re.sub(r"(?i)(api[_ -]?key|token|password|secret)\s*[:=]\s*\S+", r"\1 [redacted]", raw)
    topic = safe[:180].rstrip(" .,:;-") or "this request"
    lowered = raw.lower()
    def _contains(words: tuple[str, ...]) -> bool:
        return any(
            (word in lowered if " " in word else bool(re.search(rf"\b{re.escape(word)}\b", lowered)))
            for word in words
        )

    if _contains((
        "repo", "code", "implement", "build", "fix", "bug", "test", "file", "database",
    )):
        task_type = "code"
        message = f'I’ll inspect the relevant code paths for “{topic}” and verify the behavior with focused tests.'
    elif _contains((
        "find", "research", "look up", "source", "compare", "investigate",
    )):
        task_type = "research"
        message = f'I’ll check the available sources for “{topic}” and compare the evidence before answering.'
    elif _contains((
        "send", "create", "update", "change", "delete", "run", "set up", "install",
    )):
        task_type = "action"
        message = f'I’ll check the available tools and permissions for “{topic}”, then take the safe action and verify the result.'
    else:
        task_type = "mixed"
        message = f'I’ll work through “{topic}” and verify the key details before responding.'
    return {
        "status": "complete", "requires_work": True, "task_type": task_type,
        "message": message[:600], "fallback": True,
    }


def _existing_intro(rows: List[Any], current_interaction_id: str) -> Optional[Dict[str, str]]:
    for row in reversed(rows):
        metadata = getattr(row, "metadata", None) or {}
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata or "{}")
            except Exception:
                metadata = {}
        if not isinstance(metadata, dict) or metadata.get("progress_kind") != "working_intro":
            continue
        owner = str(getattr(row, "turn_id", "") or getattr(row, "parent_id", "") or "")
        if owner != str(current_interaction_id or ""):
            continue
        content = str(getattr(row, "content", "") or "").strip()
        if content:
            return {"interaction_id": str(getattr(row, "id", "") or ""), "message": content}
    return None


async def generate_working_intro(
    *,
    db: Any,
    user_id: str,
    session_id: str,
    agent_rec: Mapping[str, Any],
    user_message: str,
    current_interaction_id: str,
    execution_mode: str,
    timeout_seconds: int = 12,
    style: str = "brief",
) -> Dict[str, Any]:
    """Generate a bounded introduction with the exact primary-agent model."""
    deadline = time.monotonic() + max(1, min(60, int(timeout_seconds or 12)))
    metadata = agent_rec.get("metadata") if isinstance(agent_rec, Mapping) else {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata or "{}")
        except Exception:
            metadata = {}
    engine_id = str((metadata or {}).get("engine") or "").strip()
    if engine_id and engine_id != "default":
        return {"status": "skipped", "reason": "alternate engine owns its introduction"}

    from app.admin.settings import apply_provider_for_run
    from app.agent.loop import _get_client, _record_billing_usage

    async def _history() -> List[Any]:
        try:
            return list(await db.fetch_interactions(user_id, session_id))
        except Exception:
            return []

    llm_config, rows = await asyncio.gather(
        apply_provider_for_run(user_id, dict(agent_rec), session_id, apply_env=False),
        _history(),
    )
    existing = _existing_intro(rows, current_interaction_id)
    if existing:
        return {
            "status": "existing", "requires_work": True, "task_type": "mixed",
            **existing,
        }
    model = str(llm_config.get("model") or "")
    base_url = str(llm_config.get("base_url") or "")
    if base_url and "openrouter.ai" not in base_url and "/" in model:
        model = model.split("/", 1)[-1]
    if not model or not base_url:
        return {"status": "skipped", "reason": "primary model is not configured"}

    context = _recent_context(rows, current_interaction_id)
    user_prompt = (
        f"INTRO STYLE: {style}\nEXECUTION MODE: {execution_mode}\n"
        f"CURRENT USER REQUEST:\n{str(user_message or '')[:6000]}\n\n"
        f"RECENT CONVERSATION (may be empty):\n{context or '(none)'}\n\n"
        "Return the JSON work-introduction decision now."
    )
    authored_prompt = str(agent_rec.get("system_prompt") or "").strip()[:6000]
    system_prompt = _SYSTEM_PROMPT
    if authored_prompt:
        system_prompt += (
            "\n\nPRIMARY AGENT'S AUTHORED INSTRUCTIONS (follow its voice and task posture, "
            "but do not use tools in this preflight):\n" + authored_prompt
        )
    client = _get_client(base_url, llm_config.get("api_key"))
    try:
        from app.agent.provider_completion import safe_chat_completion
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise asyncio.TimeoutError
        response = await asyncio.wait_for(
            safe_chat_completion(
                client,
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.0,
                max_tokens=96,
            ),
            timeout=remaining,
        )
    except asyncio.TimeoutError:
        return {
            **fallback_intro(user_message),
            "reason": "primary work introduction timed out",
        }
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - introductions fail open
        logger.debug("primary work introduction failed: %s", exc)
        return {**fallback_intro(user_message), "reason": str(exc)[:300]}

    content = ""
    try:
        content = response.choices[0].message.content or ""
    except Exception:
        pass
    parsed = _extract_json(content)
    normalized = normalize_intro_result(parsed or {})
    if normalized is None:
        return {
            **fallback_intro(user_message),
            "reason": "primary model returned an invalid work introduction",
        }

    usage = getattr(response, "usage", None)
    try:
        await _record_billing_usage(
            db, str(agent_rec.get("id") or ""), user_id,
            getattr(usage, "prompt_tokens", None),
            getattr(usage, "completion_tokens", None), None,
            model_name=model,
            provider_name=str(llm_config.get("provider") or ""),
            entry_id=str(llm_config.get("entry_id") or "") or None,
            interaction_id=current_interaction_id,
            session_id=session_id,
        )
    except Exception:
        logger.debug("working introduction usage recording failed", exc_info=True)
    return {
        "status": "complete",
        "model": model,
        "provider": str(llm_config.get("provider") or ""),
        **normalized,
    }
