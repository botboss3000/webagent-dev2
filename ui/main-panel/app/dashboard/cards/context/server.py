"""context — Context Window card backend.

Contributes the ``context`` snapshot section: the app's default model + the
default agent's output cap (model_catalog). Best-effort; degrades to None.

REMOVE-WHEN: the Dashboard tab is dropped from the Instances page.
"""

from __future__ import annotations

from typing import Any, Dict

from dashboard_server_lib import logger


async def build_section(ctx: Dict[str, Any]) -> Dict[str, Any]:
    out = {"model": None, "max_input": None, "max_output": None}
    try:
        from app.agent.suggestions import _resolve_default_llm
        llm = await _resolve_default_llm(ctx.get("uid") or "")
        model = llm.get("model")
        out["model"] = model
        try:
            from app import model_catalog
            info = model_catalog.enrich(model) if model else None
            if info:
                out["max_input"] = info.get("context")
                out["max_output"] = info.get("max_output")
        except Exception:
            pass
    except Exception as e:
        logger.debug("dashboard context failed: %s", e)
    return out
