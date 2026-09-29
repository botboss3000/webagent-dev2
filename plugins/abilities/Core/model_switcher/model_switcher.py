"""Model Switcher ability — SELF-CONTAINED drop-in.

The single home for an agent changing THIS conversation's model at runtime. (The
image_vision ability deliberately does NOT switch models — it only reads images;
taking over on a vision/image-output model is done here via ``set_model``.)

Five tools — a read-only menu plus the switches:

  • ``list_models`` — the MENU the agent reads on demand: every enabled, runnable
    model with capability badges (sees images / makes images / premium tier), which
    one is running now, which is the default, and the reasoning-effort levels. This
    is what lets the agent switch by *capability* instead of memorising ids — it
    only needs a real model name to honour a user's explicit request. Read-only.

  • ``set_model`` — switch onto any *enabled, tool-capable* model: the user asked
    for a specific model, the agent wants a vision/image-output model for sustained
    image work, or it wants to drop back to the cheap default. Pass a model id to
    take over on it, or "default"/empty to revert.

  • ``use_premium_model`` — for a genuinely hard task the agent can UPGRADE
    itself onto the configured premium model (the row an admin assigned the
    *Premium* role to in App Config → Models; the role is stored per-model in
    the user's DB llm config as ``high_effort_capable`` and resolved through
    the same role-slot mechanism the chat footer picker uses —
    ``app.admin.settings._assign_slots`` with the legacy
    ``high_effort_targets`` resolver as a fallback). The premium model counts
    even when it is NOT enabled as an everyday brain, i.e. the "premium-only"
    pattern. If it's already on a premium model, this is a no-op. Pair it with
    ``set_model('default')`` to fall back to the cheap model when the hard task
    is done.

  • ``set_effort`` / ``reset_to_default`` — tune the reasoning depth for this chat,
    and drop both model + effort back to the agent's defaults in one call.

The switches write a per-session model override (sessions.metadata.llm_config) that
the loop resolves on the NEXT turn (app-default → agent → session). The spending
switches are DESTRUCTIVE (they can raise spend), but the loop exempts the
model-switch tools from the confirmation gate in BOTH plan and ask modes
(grep MODEL-SWITCH-EXEMPT in app/agent/loop.py) — right-sizing the model is core
agent behaviour, so the agent may switch freely; auto mode was never gated.
``list_models`` and ``reset_to_default`` are not gated either.

Drop-in contract: descriptor .json + build_tools()/TOOL_SCHEMAS/DESTRUCTIVE,
discovered generically by app/tools/loader.py. See plugins/abilities/_TEMPLATE.py.

╔══════════════════════════════════════════════════════════════════════════════╗
║  SISTER-SYNC: SESSION-MODEL-OVERRIDE                                          ║
║  The session override + capability ladder (text+tools = a valid brain) is the ║
║  SAME mechanism the chat footer model picker uses (and that image_vision's     ║
║  describe/route guidance now points back to here). The premium tier is the    ║
║  *Premium* role slot resolved by app.admin.settings._assign_slots (the legacy ║
║  high_effort_targets / "Eff" resolver remains as a fallback for old configs). ║
║  If that resolver or set_session_llm_override changes, update here. (grep SISTER-SYNC: SESSION-MODEL-OVERRIDE)║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

from typing import Optional

TOOL_SCHEMAS: dict = {}
# Anything that can INCREASE spend (a model swap, an upgrade, or raising reasoning
# effort) is flagged DESTRUCTIVE so it shows "Ask" in the Tools panel and lands in
# the confirmation set — but the loop exempts the model-switch tools in plan AND
# ask modes (grep MODEL-SWITCH-EXEMPT), so in practice they run freely everywhere.
#   set_effort is listed here too; the guardrail's per-arg exemption
#   (loop._effort_raises_spend) also lets lowering/clearing run without confirmation.
DESTRUCTIVE: set = {"set_model", "use_premium_model", "set_effort"}

# The reasoning-effort scale (mirrors REASONING_EFFORT_LEVELS in
# app/admin/settings.py and the footer picker). "default" clears the hint.
_EFFORT_LEVELS = ("default", "minimal", "low", "medium", "high")


def build_tools(*, user_id: str = "", session_id: str = "", agent_id: str = "",
                agent_template_id: Optional[str] = None, enabled_providers=None, **_ctx):
    """Return {tool_name: handler} for the model_control ability. Heavy imports stay
    lazy (inside the handlers) so scanning the descriptor stays cheap."""

    async def _load_caps():
        """Warm the catalog (so tool-capability guards have data) and read the
        user's model config. Returns (caps, error_str_or_None).

        When an agent_id is available, fetches the agent record so the caps
        default reflects the agent's effective model — not just the global
        app default. (grep AGENT-AWARE-CAPS)"""
        try:
            from app import model_catalog
            await model_catalog.ensure_fresh()
        except Exception:
            pass
        try:
            from app.admin.settings import load_llm_capabilities_for_user
            agent_rec = None
            if agent_id:
                try:
                    from app.db import get_db
                    agent_rec = await get_db().get_agent_by_id(agent_id)
                except Exception:
                    pass
            return (await load_llm_capabilities_for_user(user_id, agent_rec=agent_rec)), None
        except Exception as e:  # noqa: BLE001
            return None, f"Could not read model config: {e}"

    def _display_model_name(caps: dict, model: str) -> str:
        """Return the configured public alias for a model, or its ID as fallback.

        Model IDs remain the switcher's routing keys. This helper is deliberately
        presentation-only so chat notices and tool copy never leak an alias's
        backing provider model while calls continue to use the real ID.
        """
        if not model:
            return ""
        default = caps.get("default") or {}
        if default.get("model") == model:
            return str(default.get("display_name") or "").strip() or model
        for entry in caps.get("racers") or []:
            if entry.get("model") == model:
                return str(entry.get("display_name") or "").strip() or model
        return model

    def _enabled_brains(caps: dict, current_model: str = "") -> list:
        """Model ids that can be the brain. Only includes models where at least ONE
        of the capability checkboxes is checked in the model table:
        - text_capable (Text)
        - image_capable (Image-in / Text+)
        - image_out_capable (Image-out)

        Models with none of these checked are not switchable by the ability —
        the user can still switch to them manually via the footer picker, but
        the ability should respect the user's configured model roster."""
        from app.admin.settings import _is_tool_capable
        d = caps.get("default") or {}
        # Collect ALL racers regardless of the legacy "enabled" flag.
        all_entries: dict = {}
        for r in (caps.get("racers") or []):
            if r.get("model"):
                all_entries.setdefault(r["model"], r)
        if d.get("model"):
            all_entries[d["model"]] = d

        def _has_any_capability(e: dict) -> bool:
            """True if the model has at least one capability checkbox checked."""
            return (e.get("text_capable") is not False  # default True
                    or e.get("image_capable")
                    or e.get("image_out_capable"))

        brains = {
            e["model"] for e in all_entries.values()
            if _has_any_capability(e) and _is_tool_capable(e)
        }
        # Also include the session-override model if it's not already accounted
        # for (e.g. when it was set via the footer picker before being saved).
        # Skip the catalog tool-call guard for it — it's already running as the
        # brain, so it demonstrably supports tools even if the catalog is stale.
        if current_model and current_model not in brains:
            cur = all_entries.get(current_model)
            if cur and _has_any_capability(cur):
                brains.add(current_model)
        return sorted(brains)

    def _premium_targets(caps: dict) -> list:
        """Model ids in the configured *Premium* role that can be the agent's brain.

        The premium tier is the model assigned the Premium role in App Config →
        Models — resolved here through the SAME role-slot mechanism the chat
        footer picker uses (``_assign_slots`` → ``roles['premium']``), so the
        ability agrees with what the user sees in the footer. Falls back to the
        legacy ``high_effort_targets`` (the old per-model "Eff" flag) for
        configurations saved before the role rename. ``enabled`` is deliberately
        NOT required — the common "premium-only" pattern stores the premium
        model with ``enabled: false`` so it's an upgrade target, not an everyday
        brain. A premium model must still be text + tool-capable (the same gate
        the legacy resolver applies) so the ability never upgrades onto a model
        that can't run the agent loop. Returns a sorted list of model ids
        (empty when none configured)."""
        from app.admin.settings import _assign_slots, high_effort_targets, _is_tool_capable
        d = caps.get("default") or {}
        union = ([d] if d.get("model") else []) + list(caps.get("racers") or [])
        try:
            slots = _assign_slots(union)
            prem = (slots.get("roles") or {}).get("premium") or {}
            if (prem.get("model") and prem.get("text_capable") is not False
                    and _is_tool_capable(prem)):
                return sorted({prem["model"]})
        except Exception:  # noqa: BLE001
            pass
        return sorted(high_effort_targets(caps))

    async def _current_model(caps: dict) -> str:
        """The model this conversation is running on right now: the session
        override if set, else the agent/app default.

        Handles BOTH concrete-model overrides (``{model: <id>}``, the Model
        Switcher ability's own stored form) AND slot-based overrides
        (``{selection_type: "role", role: "premium"}``, the chat footer picker's
        stored form — resolved LIVE through _assign_slots + _resolve_slot)."""
        from app.db import get_db
        db = get_db()
        try:
            override = await db.get_session_llm_override(session_id)
        except Exception:
            override = None

        if isinstance(override, dict) and override.get("use_default") is False:
            # Concrete-model override (the Model Switcher ability's own form).
            if override.get("model"):
                return override["model"]

            # Slot-based override (the chat footer model picker) — resolve LIVE.
            sel_type = override.get("selection_type")
            if sel_type:
                from app.admin.settings import _assign_slots, _resolve_slot, _normalize_role
                default_id = (caps.get("default") or {}).get("model") or ""
                racers = caps.get("racers") or []
                # Build the provider roster: default entry + every racer that has
                # a model id (the same union _merge_agent_override feeds into the
                # slot resolver at runtime).
                default_entry = caps.get("default") or {}
                providers = [default_entry] if default_entry.get("model") else []
                seen = {default_entry.get("model")} if default_entry.get("model") else set()
                for r in racers:
                    m = r.get("model")
                    if m and m not in seen:
                        providers.append(r)
                        seen.add(m)
                slots = _assign_slots(providers)
                role = _normalize_role(override.get("role", ""))
                pos = override.get("custom_position", 0)
                entry_id = str(override.get("entry_id") or "")
                resolved = _resolve_slot(slots, sel_type, role, pos, entry_id)
                if resolved and resolved.get("model"):
                    return resolved["model"]

        # No session override, unresolvable slot, or override cleared → fall back
        # to the effective default model from caps.
        return (caps.get("default") or {}).get("model") or ""

    async def _stash_prior_slot(db, session_id) -> Optional[dict]:
        """Return the session's current USER footer-picker slot selection as a
        stash dict, so set_model / use_premium_model can restore it after the
        agent's temporary upgrade is reset on the next user message. Reuses an
        existing stash when the agent upgrades again on top of its own prior
        upgrade; returns None when there is no user-picked slot to preserve."""
        try:
            prior = await db.get_session_llm_override(session_id)
        except Exception:
            return None
        if not isinstance(prior, dict):
            return None
        existing = prior.get("_prior_slot")
        if isinstance(existing, dict) and existing.get("selection_type"):
            return existing
        if prior.get("selection_type"):
            stash = {"selection_type": prior["selection_type"]}
            if prior.get("role"):
                stash["role"] = prior["role"]
            if prior.get("custom_position") is not None:
                stash["custom_position"] = prior["custom_position"]
            if prior.get("entry_id"):
                stash["entry_id"] = prior["entry_id"]
            return stash
        return None

    async def _emit_override_state(session_id, active: bool, model: str = "", label: str = "") -> None:
        """Best-effort live push to the chat UI about the session's AGENT-driven
        model override. The footer model changer turns warning-colored while an
        override is active (active=True, model=<id>) and reverts to normal when
        it clears (active=False) — including the backend's run-end cleanup, so
        the pill always reflects the model the next turn will actually use.
        Never raises — this is a cosmetic side channel."""
        if not session_id:
            return
        try:
            from app.chat.execution import _emit_to_visualizers
            await _emit_to_visualizers(session_id, {
                "type": "model_override",
                "active": bool(active),
                "model": model or "",
                "label": label or "",
            }, user_id=user_id)
        except Exception:  # noqa: BLE001 — cosmetic; never break the tool call
            pass

    async def set_model(model: str = "") -> str:
        """Switch THIS conversation's model. Pass a model id to run on it (must be
        an enabled, tool-capable model), or "default"/empty to revert to the
        agent's default. Persists for the session; the next LLM call (even
        mid-message, on the next tool-call iteration) uses the new model."""
        import json
        from app.db import get_db

        db = get_db()
        target = (model or "").strip()

        # Revert path.
        if target in ("", "default", "reset"):
            await db.set_session_llm_override(session_id, None)
            await _emit_override_state(session_id, False)
            # Durable twin: the agent reverted to the standard model — the row
            # records the initiator even though the set_model tool call is
            # already visible in the transcript.
            try:
                await db.add_model_switch_notice(
                    user_id, session_id, "Standard model enabled",
                    initiator="agent", tool="set_model",
                    from_state="override", to_state="default")
            except Exception:  # noqa: BLE001 — best-effort
                pass
            return json.dumps({"status": "ok", "model": "",
                               "message": "Reverted to the standard model (on the next LLM call)."})

        caps, err = await _load_caps()
        if err:
            return json.dumps({"status": "error", "message": err})
        current = await _current_model(caps)
        brains = _enabled_brains(caps, current)
        if target not in brains:
            return json.dumps({
                "status": "error",
                "code": "not_a_brain_model",
                "message": (f"'{target}' isn't a model that can run the agent "
                            f"(it must support tools and be configured). You can switch to: "
                            f"{', '.join(brains) or '(none configured)'}."),
            })

        # Stash any user footer-picker slot selection before overwriting it, so
        # the new-user-message reset can restore it (SISTER-SYNC stash-restore).
        target_display_name = _display_model_name(caps, target)
        prior_slot = await _stash_prior_slot(db, session_id)
        selection = {"type": "model", "model": target}
        if prior_slot:
            selection["_prior_slot"] = prior_slot
        await db.set_session_llm_override(session_id, selection)
        await _emit_override_state(session_id, True, target, f"Switched to {target_display_name}")
        # Durable twin: agent switched the conversation's model — the row
        # records the initiator + tool even though the set_model tool call is
        # already visible in the transcript.
        try:
            await db.add_model_switch_notice(
                user_id, session_id, "Custom model enabled",
                initiator="agent", tool="set_model", model=target,
                # Identity keys (not default/override) — two different custom
                # models must net as a change, and only a return to the run's
                # starting selection cancels.
                from_state="default", to_state=f"override:{target}")
        except Exception:  # noqa: BLE001 — best-effort
            pass
        return json.dumps({
            "status": "ok",
            "model": target,
            "message": (f"This conversation will now run on {target_display_name} (on the next LLM call). "
                        "Call set_model('default') to revert when you're done."),
        })

    async def use_premium_model() -> str:
        """Upgrade THIS conversation onto the configured premium model for a hard
        task. No-op if already on one. Revert later with set_model('default').
        Runs without confirmation in every mode — the loop exempts model-switch
        tools in plan and ask (auto was never gated)."""
        import json
        from app.db import get_db

        caps, err = await _load_caps()
        if err:
            return json.dumps({"status": "error", "message": err})

        targets = _premium_targets(caps)
        if not targets:
            return json.dumps({
                "status": "error",
                "code": "no_premium_model",
                "message": ("No premium model is configured, so there's nothing stronger "
                            "to upgrade onto. Tell the user an admin can assign one the "
                            "Premium role in App Config → Models."),
            })

        current = await _current_model(caps)
        if current in targets:
            return json.dumps({
                "status": "ok",
                "model": current,
                "changed": False,
                "message": "Already running on the premium model; no change needed.",
            })

        # Prefer a target that isn't the current model (it won't be, given the
        # guard above, but stay defensive), else the first configured one.
        target = next((t for t in targets if t != current), targets[0])
        db = get_db()
        # Stash any user footer-picker slot selection before overwriting it.
        prior_slot = await _stash_prior_slot(db, session_id)
        selection = {"type": "model", "model": target}
        if prior_slot:
            selection["_prior_slot"] = prior_slot
        await db.set_session_llm_override(session_id, selection)
        await _emit_override_state(session_id, True, target, "Upgraded to the premium model")
        # Durable twin: agent upgraded the conversation to the premium model —
        # the row records the initiator + tool even though the
        # use_premium_model tool call is already visible in the transcript.
        try:
            await db.add_model_switch_notice(
                user_id, session_id, "Premium model enabled",
                initiator="agent", tool="use_premium_model", model=target,
                reason="premium tier requested by the agent",
                from_state="default", to_state="override:premium")
        except Exception:  # noqa: BLE001 — best-effort
            pass
        return json.dumps({
            "status": "ok",
            "model": target,
            "changed": True,
            "message": ("Upgraded this conversation to the premium model (on the next LLM call). "
                        "The chat reverts to the standard model automatically when the run ends; "
                        "set_model('default') mid-run is optional if you want to drop back sooner."),
        })

    async def set_effort(level: str = "default", model: str = "") -> str:
        """Set how hard the model THINKS on this conversation — the reasoning
        effort. ``level`` is one of default / minimal / low / medium / high
        ('default' clears the hint = the model's own default). ``model`` is the
        model id to set it for; leave empty to set it for the model the chat is
        running on right now. Each model remembers its own level. The next LLM call
        (even mid-message) uses the new level. Runs without confirmation in every
        mode — the loop exempts model-switch tools in plan and ask."""
        import json
        from app.db import get_db

        lvl = (level or "default").strip().lower()
        if lvl not in _EFFORT_LEVELS:
            return json.dumps({
                "status": "error", "code": "bad_level",
                "message": (f"'{level}' isn't a valid effort level. Choose one of: "
                            f"{', '.join(_EFFORT_LEVELS)} (use 'default' to clear it)."),
            })

        caps, err = await _load_caps()
        if err:
            return json.dumps({"status": "error", "message": err})
        target_model = (model or "").strip() or await _current_model(caps)
        if not target_model:
            return json.dumps({
                "status": "error", "code": "no_model",
                "message": "Couldn't resolve which model to set the effort for. Name a model id.",
            })

        target_display_name = _display_model_name(caps, target_model)
        db = get_db()
        await db.set_session_model_effort(session_id, target_model, lvl)
        if lvl == "default":
            msg = (f"Cleared the reasoning-effort hint for {target_display_name} (next turn) — "
                   "it'll use its own default thinking depth.")
        else:
            msg = (f"{target_display_name} will now think at '{lvl}' reasoning effort (next turn). "
                   "The effort hint clears automatically when the run ends; "
                   "set_effort('default') mid-run is optional.")
        return json.dumps({"status": "ok", "model": target_model, "reasoning_effort": lvl,
                           "message": msg})

    async def reset_to_default() -> str:
        """Return this conversation to the agent's DEFAULT model at DEFAULT effort —
        clears both the picked-model override and every per-model reasoning-effort
        level in one call. OPTIONAL mid-run revert only: the backend also clears
        agent-driven overrides automatically when the run ends, so you don't need to
        call this as a closing step. Takes effect next turn."""
        import json
        from app.db import get_db

        db = get_db()
        await db.clear_session_llm_override(session_id)
        await _emit_override_state(session_id, False)
        # Durable twin: agent reverted the conversation to its default model +
        # effort — the row records the initiator even though the
        # reset_to_default tool call is already visible in the transcript.
        try:
            await db.add_model_switch_notice(
                user_id, session_id, "Standard model enabled",
                initiator="agent", tool="reset_to_default",
                reason="agent reverted to the default model and effort",
                from_state="override", to_state="default")
        except Exception:  # noqa: BLE001 — best-effort
            pass
        return json.dumps({
            "status": "ok", "model": "", "reasoning_effort": "default",
            "message": "Reverted this conversation to the standard model and default "
                       "reasoning effort (next turn)."
        })

    async def list_models() -> str:
        """List the models this conversation can switch onto and what each can do —
        call this BEFORE switching so you pick by capability, not by guessing an id.
        Returns models where at least one capability checkbox (Text, Image-in, or
        Image-out) is checked in the model table. Each model shows badges for whether
        it can see images, generate images, and whether it's the premium tier — plus
        which model is running now, which is the default, and the reasoning-effort
        levels you can set. Read-only; changes nothing.

        Use it like a menu: to honour a user's explicit model request pass that id to
        set_model; to right-size on your own, switch by *capability* — use_premium_model()
        for the premium tier, or set_model onto a model whose makes_images/sees_images
        is true for image work — without memorising any id."""
        import json
        from app.admin.settings import (
            model_sees_images, model_makes_images,
        )

        caps, err = await _load_caps()
        if err:
            return json.dumps({"status": "error", "message": err})

        # Index every configured entry by model id so we can read its capability
        # flags. A same-id racer is filled first; the default row then wins.
        entries: dict = {}
        for r in (caps.get("racers") or []):
            if r.get("model"):
                entries.setdefault(r["model"], r)
        d = caps.get("default") or {}
        if d.get("model"):
            entries[d["model"]] = d

        current = await _current_model(caps)
        brains = _enabled_brains(caps, current)   # the valid set_model targets
        premium = set(_premium_targets(caps))     # the *Premium* role tier
        default_id = (caps.get("default") or {}).get("model") or ""

        models = []
        for mid in brains:
            e = entries.get(mid, {"model": mid})
            models.append({
                "id": mid,
                "name": _display_model_name(caps, mid),
                "current": mid == current,
                "default": mid == default_id,
                "sees_images": model_sees_images(e),
                "makes_images": model_makes_images(e),
                "premium": mid in premium,
            })

        actionable_effort = [lvl for lvl in _EFFORT_LEVELS if lvl != "default"]
        return json.dumps({
            "status": "ok",
            "current": current,
            "default": default_id,
            "models": models,
            "premium_available": bool(premium),
            "image_out_available": any(m["makes_images"] for m in models),
            "vision_available": any(m["sees_images"] for m in models),
            "effort_levels": actionable_effort,
            "message": (
                "These are the models you can switch onto with set_model('<id>'). For a "
                "user's explicit model request, pass that id. To right-size on your own, "
                "switch by capability: use_premium_model() for the premium tier, or set_model "
                "onto a model whose makes_images/sees_images is true for image work. Tune "
                "thinking depth with set_effort (levels above; 'default' clears it), and revert "
                "with reset_to_default() when the task is done. Some models ignore reasoning "
                "effort — it's applied if the model supports it, harmlessly dropped if not."
            ),
        })

    TOOL_SCHEMAS.clear()
    TOOL_SCHEMAS.update({
        "set_model": {
            "type": "object",
            "properties": {
                "model": {"type": "string",
                          "description": "Model id to run this conversation on (must be enabled and support tools), or 'default' to revert."},
            },
            "required": ["model"],
        },
        "use_premium_model": {
            "type": "object",
            "properties": {},
            "required": [],
        },
        "set_effort": {
            "type": "object",
            "properties": {
                "level": {"type": "string", "enum": list(_EFFORT_LEVELS),
                          "description": "Reasoning effort: default (clear), minimal, low, medium, or high."},
                "model": {"type": "string",
                          "description": "Model id to set the effort for; empty = the model this chat is running on now."},
            },
            "required": ["level"],
        },
        "reset_to_default": {
            "type": "object",
            "properties": {},
            "required": [],
        },
        "list_models": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    })
    return {"set_model": set_model, "use_premium_model": use_premium_model,
            "set_effort": set_effort, "reset_to_default": reset_to_default,
            "list_models": list_models}
