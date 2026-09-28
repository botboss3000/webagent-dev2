"""OpenRouter free-model ranking control plane.

This module deliberately has no access to chat/session data.  Its probe payload is
fixed, candidate IDs are explicit, and credentials are accepted only transiently
from the existing roster-secret resolver.
"""
from __future__ import annotations

import asyncio
import json
import logging
import statistics
import time
from dataclasses import dataclass
from typing import Any, Iterable

import httpx

LOG = logging.getLogger(__name__)
OPENROUTER_MODELS = "https://openrouter.ai/api/v1/models"
OPENROUTER_CHAT = "https://openrouter.ai/api/v1/chat/completions"
# PROBE-TOKEN-FLOOR: a reasoning model spends output tokens on hidden reasoning BEFORE
# it emits the requested text or tool call, so a budget tuned for chat models starves
# it and the probe records "no tool call returned" / "empty completion" for a model
# that is actually healthy and compatible. Measured on nvidia/nemotron-3-nano-omni:
# 128 tokens returned an empty payload, 512 returned the answer plus 239 chars of
# reasoning. Every probe request is therefore floored at this budget; a larger
# configured max_tokens still wins.
PROBE_TOKEN_FLOOR = 512
DEFAULT_CONFIG = {"enabled": False, "cadence_hours": 1.0, "concurrency": 2,
                  "timeout_seconds": 20, "max_tokens": 512, "retention_days": 30,
                  "structured_probe": False}
PROBE_PROMPT = "Reply with exactly: WEBAGENT_FREE_PROBE_OK"
TOOL_PROBE_NAME = "webagent_compatibility_probe"
TOOL_PROBE_VALUE = "WEBAGENT_TOOL_OK"
TOOL_PROBE = {
    "type": "function",
    "function": {
        "name": TOOL_PROBE_NAME,
        "description": "Synthetic compatibility check. The tool is never executed.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["echo"]},
                "value": {"type": "string"},
            },
            "required": ["action", "value"],
            "additionalProperties": False,
        },
    },
}


def normalize_config(value: dict | None) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(value or {})
    cfg["enabled"] = bool(cfg["enabled"])
    cfg["cadence_hours"] = min(168.0, max(0.1, float(cfg["cadence_hours"])))
    cfg["concurrency"] = min(4, max(1, int(cfg["concurrency"])))
    cfg["timeout_seconds"] = min(60, max(3, int(cfg["timeout_seconds"])))
    cfg["max_tokens"] = min(1024, max(8, int(cfg["max_tokens"])))
    cfg["retention_days"] = min(90, max(1, int(cfg["retention_days"])))
    cfg["structured_probe"] = bool(cfg["structured_probe"])
    return cfg


def is_free_model(model: dict) -> bool:
    pricing = model.get("pricing") or {}
    try:
        return float(pricing.get("prompt", 1)) == 0 and float(pricing.get("completion", 1)) == 0
    except (TypeError, ValueError):
        return False


def free_candidates(models: Iterable[dict]) -> list[dict]:
    """Zero-price OpenRouter candidates, each annotated with detected capabilities.

    Capability detection is delegated to the app's single modality detector
    (`app.admin.settings._detect_model_modalities`) so a free candidate reports the
    same Text/Vision/Image/Voice set the primary model table shows, instead of a
    second, divergent guess. The import is lazy: this control plane must stay
    importable on its own, and settings pulls in the whole admin surface.
    """
    try:
        from app.admin.settings import _detect_model_modalities
    except Exception:  # pragma: no cover - detection is a display nicety, never a gate
        _detect_model_modalities = None
    rows = []
    for m in models:
        if not m.get("id") or str(m.get("id")) == "openrouter/free" or not is_free_model(m):
            continue
        row = {"id": str(m["id"]), "name": str(m.get("name") or m["id"]),
               "context_length": int(m.get("context_length") or 0),
               "capabilities": {}}
        if _detect_model_modalities is not None:
            text, image, image_out, voice, video, known = _detect_model_modalities(m)
            row["capabilities"] = {"text": bool(text), "vision": bool(image),
                                   "image_out": bool(image_out), "voice": bool(voice),
                                   "video": bool(video), "known": bool(known)}
        rows.append(row)
    return rows


def redact_error(value: object, limit: int = 240) -> str:
    text = str(value or "").replace("Bearer ", "Bearer [redacted] ")
    return text[:limit]


def score(observations: list[dict], rating: int | None = None, disabled: bool = False) -> dict:
    """Compute a transparent 0-100 score; failures never become eligible via rating."""
    if disabled:
        return {"score": 0.0, "eligible": False, "reason": "manually disabled"}
    recent = observations[-20:]
    if not recent:
        return {"score": 0.0, "eligible": False, "reason": "no observations"}
    success = [x for x in recent if x.get("ok")]
    reliability = len(success) / len(recent)
    if reliability < .70 or not success:
        return {"score": round(reliability * 30, 2), "eligible": False,
                "reason": "insufficient recent reliability", "reliability": reliability}
    total = [float(x.get("latency_ms") or 60000) for x in success]
    tps = [float(x.get("tokens_per_second") or 0) for x in success]
    agentic_rate = sum(bool(x.get("agentic_ok")) for x in recent) / len(recent)
    compatibility_rate = sum(bool(x.get("compatibility_ok")) for x in recent) / len(recent)
    median_latency = statistics.median(total)
    median_tps = statistics.median(tps)
    latency_points = max(0.0, 20.0 * (1.0 - min(median_latency, 20000.0) / 20000.0))
    throughput_points = min(10.0, median_tps * .5)
    manual_points = ((int(rating or 3) - 1) / 4) * 10
    total_score = (reliability * 30 + latency_points + throughput_points
                   + agentic_rate * 20 + compatibility_rate * 10 + manual_points)
    compatible = compatibility_rate >= .70
    components = {
        "reliability": round(reliability * 30, 2),
        "latency": round(latency_points, 2),
        "throughput": round(throughput_points, 2),
        "agentic": round(agentic_rate * 20, 2),
        "compatibility": round(compatibility_rate * 10, 2),
        "manual": round(manual_points, 2),
    }
    return {"score": round(min(100, total_score), 2), "score_components": components,
            "eligible": compatible, "reason": "eligible" if compatible else "tool API incompatible",
            "reliability": round(reliability, 3), "agentic_rate": round(agentic_rate, 3),
            "compatibility_rate": round(compatibility_rate, 3),
            "median_ttft_ms": None, "p95_latency_ms": round(max(total), 1),
            "tokens_per_second": round(median_tps, 2)}


def status_since(previous_status: str | None, status: str, prior_since: Any = None,
                 now: float | None = None) -> float:
    """Epoch seconds at which *status* most recently became the candidate's status.

    The ranking table reports how long a candidate has held its current health, so
    the counter base must survive a re-probe returning the same verdict and reset
    only on a real transition (pending -> ok, ok -> failed, ...). Callers pass the
    row as stored *before* the probe. A status whose base was never recorded
    (rows written before this stamp existed) is dated with *now* rather than
    claiming a false age.
    """
    if previous_status == status:
        try:
            base = float(prior_since)
        except (TypeError, ValueError):
            base = 0.0
        if base > 0:
            return base
    return float(time.time() if now is None else now)


async def discover() -> list[dict]:
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(OPENROUTER_MODELS)
        response.raise_for_status()
        return free_candidates((response.json() or {}).get("data") or [])


def validate_tool_probe(payload: dict) -> dict:
    """Validate a synthetic tool response without executing any returned call."""
    messages = [choice.get("message") or {} for choice in payload.get("choices") or []]
    calls = [call for message in messages for call in (message.get("tool_calls") or [])]
    if not calls:
        return {"agentic_ok": False, "compatibility_ok": False, "reason": "no tool call returned"}
    for call in calls:
        function = call.get("function") or {}
        if function.get("name") != TOOL_PROBE_NAME:
            continue
        arguments = function.get("arguments") or {}
        try:
            if isinstance(arguments, str):
                arguments = json.loads(arguments)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {"agentic_ok": True, "compatibility_ok": False, "reason": "tool arguments were not valid JSON"}
        if isinstance(arguments, dict) and arguments.get("action") == "echo" and arguments.get("value") == TOOL_PROBE_VALUE:
            return {"agentic_ok": True, "compatibility_ok": True, "reason": "valid WebAgent-style tool call"}
    return {"agentic_ok": True, "compatibility_ok": False, "reason": "tool call had incompatible name or arguments"}


async def probe(model_id: str, api_key: str, cfg: dict) -> dict:
    """Run fixed text and synthetic tool probes against an explicit candidate ID."""
    started = time.perf_counter()
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    body = {"model": model_id, "messages": [{"role": "user", "content": PROBE_PROMPT}],
            "max_tokens": max(PROBE_TOKEN_FLOOR, cfg["max_tokens"]), "temperature": 0, "stream": False}
    try:
        async with httpx.AsyncClient(timeout=cfg["timeout_seconds"]) as client:
            response = await client.post(OPENROUTER_CHAT, headers=headers, json=body)
        elapsed = round((time.perf_counter() - started) * 1000, 1)
        retry_after = response.headers.get("retry-after")
        if response.status_code != 200:
            return {"model_id": model_id, "ok": False, "latency_ms": elapsed,
                    "status_code": response.status_code, "retry_after": retry_after,
                    "error": redact_error(response.text)}
        payload = response.json()
        # PROVIDER-ERROR-AS-200: some providers report an upstream failure (rate limit,
        # resource exhaustion) as HTTP 200 with an empty choices list. Surface the real
        # reason instead of a blanket "empty completion", so the roster shows the cause
        # while the model still counts as unhealthy for the run.
        if not payload.get("choices") and payload.get("error"):
            detail = payload.get("error")
            detail = detail if isinstance(detail, str) else json.dumps(detail)
            return {"model_id": model_id, "ok": False, "latency_ms": elapsed,
                    "status_code": 200, "retry_after": retry_after,
                    "error": redact_error(detail, 200)}
        messages = [c.get("message") or {} for c in payload.get("choices") or []]
        text = " ".join(str(message.get("content") or "") for message in messages)
        # Some providers return a perfectly usable answer but do not follow the exact
        # marker wording. Treat a non-empty completion as health; retain marker state
        # as a diagnostic rather than making every otherwise successful model ineligible.
        marker_ok = PROBE_PROMPT.casefold() in text.casefold()
        ok = bool(text.strip())
        usage = payload.get("usage") or {}
        output = int(usage.get("completion_tokens") or 0)
        tool_body = {
            "model": model_id,
            "messages": [{"role": "user", "content":
                          "Call webagent_compatibility_probe with action echo and value WEBAGENT_TOOL_OK. Do not answer in text."}],
            "tools": [TOOL_PROBE],
            "tool_choice": {"type": "function", "function": {"name": TOOL_PROBE_NAME}},
            # Floor, not just the configured value: a starved forced tool call costs a
            # good reasoning model its eligibility, so never probe it with a budget the
            # model cannot reach the call within after spending its reasoning tokens.
            "max_tokens": max(PROBE_TOKEN_FLOOR, cfg["max_tokens"]),
            "temperature": 0,
            "stream": False,
        }
        agentic_ok = False
        compatibility_ok = False
        compatibility_reason = "no tool call returned"
        try:
            async with httpx.AsyncClient(timeout=cfg["timeout_seconds"]) as client:
                tool_response = await client.post(OPENROUTER_CHAT, headers=headers, json=tool_body)
            if tool_response.status_code == 200:
                validation = validate_tool_probe(tool_response.json())
                agentic_ok = validation["agentic_ok"]
                compatibility_ok = validation["compatibility_ok"]
                compatibility_reason = validation["reason"]
            else:
                compatibility_reason = f"tool probe HTTP {tool_response.status_code}"
        except Exception as tool_exc:
            compatibility_reason = redact_error(tool_exc, 120)
        return {"model_id": model_id, "ok": ok, "marker_ok": marker_ok,
                "agentic_ok": agentic_ok, "compatibility_ok": compatibility_ok,
                "compatibility_reason": compatibility_reason, "latency_ms": elapsed,
                "ttft_ms": None, "output_tokens": output,
                "tokens_per_second": round(output / max(.001, elapsed / 1000), 2),
                "status_code": 200, "error": "" if ok else "empty completion"}
    except Exception as exc:
        return {"model_id": model_id, "ok": False, "latency_ms": round((time.perf_counter()-started)*1000, 1),
                "status_code": 0, "error": redact_error(exc)}


@dataclass
class RankingResolver:
    """Small storage-agnostic resolver used by runtime configuration code."""
    rows: list[dict]
    enabled: bool

    def resolve(self) -> tuple[str, str]:
        if not self.enabled:
            return "openrouter/free", "ranking monitor disabled"
        eligible = [x for x in self.rows if x.get("eligible") and not x.get("disabled") and x.get("model_id")]
        if not eligible:
            return "openrouter/free", "no eligible ranked free candidate"
        chosen = max(eligible, key=lambda x: (float(x.get("score") or 0), str(x.get("model_id"))))
        return str(chosen["model_id"]), "ranked candidate selected"


class RankingStore:
    """Control-plane persistence. Secrets never enter this adapter."""
    def __init__(self, db: Any):
        self.db = db

    def config(self, user_id: str) -> dict:
        row = self.db.fetchone("SELECT enabled, cadence_hours FROM free_model_ranking_config WHERE user_id=?", (user_id,))
        return normalize_config(row)

    def rows(self, user_id: str) -> list[dict]:
        return self.db.fetchall("SELECT model_id, display_name, score, eligible, reliability, latency_ms, tokens_per_second, manual_rating, disabled, reason, updated_at FROM free_model_ranking_aggregates WHERE user_id=? ORDER BY score DESC, model_id", (user_id,))

    def resolve(self, user_id: str) -> tuple[str, str]:
        return RankingResolver(self.rows(user_id), self.config(user_id)["enabled"]).resolve()

    def set_config(self, user_id: str, value: dict) -> dict:
        cfg = normalize_config(value)
        self.db.execute("INSERT INTO free_model_ranking_config (user_id, enabled, cadence_hours, updated_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP) ON CONFLICT(user_id) DO UPDATE SET enabled=excluded.enabled, cadence_hours=excluded.cadence_hours, updated_at=CURRENT_TIMESTAMP", (user_id, int(cfg["enabled"]), cfg["cadence_hours"]))
        return cfg

    def set_override(self, user_id: str, model_id: str, rating: int | None, disabled: bool) -> None:
        if rating is not None and not 1 <= int(rating) <= 5:
            raise ValueError("manual rating must be 1 through 5")
        self.db.execute("UPDATE free_model_ranking_aggregates SET manual_rating=?, disabled=?, updated_at=CURRENT_TIMESTAMP WHERE user_id=? AND model_id=?", (rating, int(disabled), user_id, model_id))
