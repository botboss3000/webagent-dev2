"""Model-supported reasoning levels shared by the picker and runtime."""

EFFORT_LEVELS = ("default", "none", "minimal", "low", "medium", "high", "xhigh", "max")
EFFORT_ORDER = {level: index for index, level in enumerate(EFFORT_LEVELS)}


def supported_efforts(model: str, provider: str = "") -> list[str]:
    model = str(model or "").rsplit("/", 1)[-1].lower()
    if model in {"gpt-6.1-sol", "gpt-6-astra"}:
        return ["low", "medium", "high", "xhigh", "max"]
    if model in {"gpt-6-sol", "gpt-6-luna"}:
        return ["none", "low", "medium", "high", "xhigh", "max"]
    if provider == "openai_subscription":
        from app.openai_subscription import models
        known = next((row for row in models() if row["id"] == model), None)
        if known:
            return list(known["reasoning_efforts"])
    from app import model_catalog
    meta = model_catalog.lookup(model, provider_hint=provider) or {}
    if meta.get("reasoning") is False:
        return []
    levels = meta.get("reasoning_efforts")
    if isinstance(levels, list):
        return [level for level in levels if level in EFFORT_ORDER and level != "default"]
    # Preserve the existing generic scale when the provider supplies no metadata.
    return ["minimal", "low", "medium", "high"]


def normalize_effort(
    level: str | None, model: str, provider: str = "", maximum: str = "max",
) -> str | None:
    level = str(level or "").strip().lower()
    if level in {"", "default"}:
        return None
    levels = [level for level in supported_efforts(model, provider)
              if EFFORT_ORDER[level] <= EFFORT_ORDER.get(maximum, 0)]
    # Older footer saves used Minimal for the lowest tick. These two models
    # reject it; migrate that hint to their lowest supported reasoning level.
    if level == "minimal" and str(model).rsplit("/", 1)[-1].lower() in {"gpt-6.1-sol", "gpt-6-astra"}:
        level = "low"
    return level if level in levels else None
