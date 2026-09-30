"""Ability-bundled skills.

A feature/ability can ship a skill alongside its tools by declaring ``skill``
(and optional ``skill_mode`` / ``skill_handle``) in its module-level ``FEATURE``
header. When that ability is enabled for an agent, its skill is folded into the
agent's ``# [SKILLS]`` catalog automatically — the agent always sees the
"when to use it" line and pulls the full body on demand with ``load_skill``.

Identity: ability skills are keyed by a **minted-once-frozen handle** (e.g.
``email_a1b2c3d4``) so they can never collide with agent-authored skills (which
stay name-keyed) and so a loaded ability-skill still matches itself across
restarts / conversation replay. The author sets ``FEATURE['skill_handle']``;
if absent we derive a stable handle from the feature id (deterministic, never
regenerated).

Phase 2 wiring. Fully guarded — any failure yields no ability skills rather than
breaking the prompt build.
"""

from __future__ import annotations

import hashlib
import importlib
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def derive_handle(feature_id: str) -> str:
    """Stable, random-looking handle for a feature with no explicit one."""
    h = hashlib.md5(feature_id.encode("utf-8")).hexdigest()[:8]
    return f"{feature_id}_{h}"


def _skill_from_feature(feature: dict, module_name: str) -> Optional[dict]:
    if not isinstance(feature, dict):
        return None
    body = (feature.get("skill") or "").strip()
    if not body:
        return None
    fid = str(feature.get("id") or module_name)
    handle = str(feature.get("skill_handle") or derive_handle(fid))
    display = feature.get("display_name") or fid
    return {
        # `name` IS the load/active-tracking key — the handle for ability skills.
        "name": handle,
        "handle": handle,
        # Root compatibility identity. Added directory skills use their own
        # composite key below; legacy ability-id overrides still fall back here.
        "skill_id": "core",
        "skill_key": f"{fid}:core",
        "display_name": display,
        # The catalog "when to use it" line. Prefer a skill-specific summary so a
        # bundled skill can prompt loading in its own words; fall back to the
        # ability's tool summary when none is given.
        "description": (feature.get("skill_summary") or feature.get("summary") or "").strip(),
        "body": body,
        "mode": (feature.get("skill_mode") or "always_on"),
        "enabled": True,
        "source": display,
        # Per-tool mini-skills from ``tools/<tool_name>.skill.md`` files
        # (plugins/abilities/README.md). Each is delivered by the tool loader
        # when its exact tool activates — see app/tools/loader.py.
        "tool_sections": dict(feature.get("skill_tool_sections") or {}),
        "_ability": True,
    }


def _skill_from_ability_record(record: dict, ability_id: str, ability_display: str) -> Optional[dict]:
    """Turn one generated ability general-skill record into prompt-skill shape."""
    if not isinstance(record, dict):
        return None
    skill_id = str(record.get("skill_id") or "").strip()
    body = str(record.get("body") or "").strip()
    if not skill_id or not body:
        return None
    skill_key = str(record.get("skill_key") or f"{ability_id}:{skill_id}")
    # Preserve the root's existing explicit/deterministic handle. New records are
    # keyed by the composite identity so two abilities may both own e.g. "review".
    handle = derive_handle(ability_id) if skill_id == "core" else derive_handle(skill_key)
    title = str(record.get("title") or skill_id).strip()
    display = ability_display if skill_id == "core" else f"{ability_display} — {title}"
    return {
        "name": handle, "handle": handle, "skill_id": skill_id, "skill_key": skill_key,
        "display_name": display, "title": title,
        "description": str(record.get("when_to_use") or "").strip(),
        "body": body, "mode": record.get("default_mode") or "visible",
        "minimum_access": record.get("minimum_access") or "everyone",
        "enabled": True, "source": ability_display, "_ability": True,
    }


def ability_skill_payload(ability_id: str,
                          visible_tools: Optional[List[str]] = None,
                          skill_modes: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """An ability's skill split into its two layers, ready to hand to the model.

    ``body`` is the root ability-level lead. ``deferred`` is the concatenated
    exact-name tool-file skills for ``visible_tools``: tools the model is ALREADY being sent and
    will therefore never ``load_tool``, so this is their only delivery point.
    Tools the agent turned off simply aren't in ``visible_tools`` and contribute
    nothing; discoverable tools deliver their mini-skill later, from the tool
    loader itself. Returns empty fields when the ability has no skill at all.

    Shared by ``load_ability`` and the loop's ability auto-reveal so the two
    delivery paths can never drift apart.
    """
    out: Dict[str, Any] = {"body": "", "deferred": "", "handle": None, "summary": ""}
    try:
        from app.abilities import ability_feature_with_skill
        feat = ability_feature_with_skill(ability_id)
        if not feat:
            return out
        sk = _skill_from_feature(feat, ability_id)
        if not sk:
            return out
        out["body"] = (sk.get("body") or "").strip()
        out["handle"] = sk.get("handle")
        out["summary"] = sk.get("description") or ""
        from app.tools.tool_modes import resolve_skill_mode
        if resolve_skill_mode(f"{ability_id}:core", skill_modes, sk.get("mode")) == "off":
            out.update(body="", handle=None, summary="")
        sections = sk.get("tool_sections") or {}
        if sections and visible_tools:
            out["deferred"] = "\n\n".join(
                sections[t].strip() for t in visible_tools if sections.get(t)
            )
    except Exception as e:
        logger.debug("ability_skill_payload failed for %s: %s", ability_id, e)
    return out


async def _agent_ability_settings(agent_id: str) -> Dict[str, dict]:
    """Map of ``ability_id -> ability_settings dict`` for this agent's enabled
    ability rows. Used to resolve declarative per-ability ``bias`` postures.

    Fully guarded — any failure yields an empty map, so a read error never
    breaks the prompt build."""
    out: Dict[str, dict] = {}
    if not agent_id:
        return out
    try:
        import json as _json
        from app.db import get_db
        db = get_db()
        rows = await db.get_agent_connections(agent_id)
        for r in rows or []:
            if r.get("section") != "ability":
                continue
            aid = r.get("connection_type")
            if not aid:
                continue
            cfg = r.get("config") or {}
            if isinstance(cfg, str):
                try:
                    cfg = _json.loads(cfg or "{}")
                except Exception:
                    cfg = {}
            if not isinstance(cfg, dict):
                cfg = {}
            s = cfg.get("ability_settings")
            out[aid] = s if isinstance(s, dict) else cfg
    except Exception as e:
        logger.debug("_agent_ability_settings failed: %s", e)
    return out


def _bias_posture_line(feature: dict, settings: dict) -> str:
    """Resolve a declarative ``config.bias`` posture for one ability.

    An ability opts in by declaring in its descriptor::

        "config": {"bias": {"key": "<setting_key>", "default": "...",
                            "levels": {"<value>": "<posture sentence>", ...}}}

    The selected level comes from the agent's saved ability setting (the same
    value the config panel writes), falling back to ``bias.default``. Returns the
    matching posture sentence (may be ""), or "" when the ability declares no
    bias. Generic — no ability is named here; the text lives in each ability's
    own drop-in descriptor."""
    try:
        cfg = feature.get("config")
        if not isinstance(cfg, dict):
            return ""
        bias = cfg.get("bias")
        if not isinstance(bias, dict):
            return ""
        key = bias.get("key")
        levels = bias.get("levels")
        if not key or not isinstance(levels, dict):
            return ""
        val = settings.get(key)
        if val is None or val == "":
            val = bias.get("default")
        return str(levels.get(str(val), "")).strip()
    except Exception:
        return ""


async def collect_ability_skills(agent_id: str, user_id: Optional[str] = None) -> List[dict]:
    """Skill dicts contributed by the abilities enabled on this agent.

    Two sources, both keyed off the agent's *enabled* set (``gather_enabled_providers``
    returns enabled OAuth providers AND enabled host abilities, already
    admin-gated):

      1. **Integration abilities** — each enabled OAuth provider maps to the
         integration module(s) that serve it; pull each module's ``FEATURE.skill``.
      2. **Host abilities** — each enabled ability id that matches a drop-in file
         in ``plugins/abilities/`` declaring a skill (inline or a sibling file).

    Deduped by handle. Fully guarded — any failure yields fewer skills, never an
    error in the prompt build.
    """
    if not agent_id:
        return []
    out: List[dict] = []
    seen: set = set()
    try:
        from app.integrations import gather_enabled_providers, _discover_tool_specs
        # `providers` = the agent's enabled OAuth/host abilities. May be empty —
        # sections 1 & 2 below then no-op, but section 3 (always-on virtual
        # abilities) must still run, so we do NOT early-return on an empty set.
        providers = await gather_enabled_providers(agent_id, user_id) or set()

        # ── 1. Integration-module skills (keyed by served provider) ──
        mod_providers: Dict[str, set] = {}
        for spec in _discover_tool_specs():
            p = spec.get("provider")
            m = spec.get("_module")
            if p and m:
                mod_providers.setdefault(m, set()).add(p)
        for mod_name, provs in mod_providers.items():
            if not (provs & providers):
                continue
            try:
                mod = importlib.import_module(f"app.integrations.{mod_name}")
            except Exception:
                continue
            skill = _skill_from_feature(getattr(mod, "FEATURE", None), mod_name)
            if skill and skill["handle"] not in seen:
                seen.add(skill["handle"])
                out.append(skill)

        # ── 2. Host-ability skills (plugins/abilities/*) ──
        try:
            from app.abilities import ability_feature_with_skill, ability_general_skills
            ability_settings = await _agent_ability_settings(agent_id)
            for ability_id in providers:
                feat = ability_feature_with_skill(ability_id)
                if not feat:
                    continue
                ability_display = str(feat.get("display_name") or ability_id)
                # Root and directory-discovered general skills are emitted as
                # independent records. The root retains its legacy handle; each
                # added file has a stable ability_id:skill_id identity.
                for record in ability_general_skills(ability_id):
                    skill = _skill_from_ability_record(record, ability_id, ability_display)
                    if not skill or skill["handle"] in seen:
                        continue
                    seen.add(skill["handle"])
                    skill["ability_id"] = ability_id
                    # An ability posture is lead-level guidance, not duplicate
                    # specialist-skill metadata, so retain it on core only.
                    if skill.get("skill_id") == "core":
                        eff_settings = ability_settings.get(ability_id) or {}
                        try:
                            from app.admin import ability_config as _abcfg
                            eff_settings = _abcfg.effective_ability_config(ability_id, eff_settings)
                        except Exception:
                            pass
                        posture = _bias_posture_line(feat, eff_settings)
                        if posture:
                            desc = (skill.get("description") or "").strip()
                            skill["description"] = (desc + " " + posture).strip() if desc else posture
                    out.append(skill)
        except Exception as e:
            logger.debug("host-ability skill collection failed: %s", e)

        # ── 3. Per-agent database-backed soft abilities ──
        try:
            from app.db import get_db
            rows = await get_db().get_agent_soft_abilities(agent_id, enabled_only=True)
            for row in rows:
                handle = f"soft_{row['id']}"
                if handle in seen or not (row.get("skill_body") or "").strip():
                    continue
                seen.add(handle)
                allowed = row.get("allowed_tools") or []
                body = (row.get("skill_body") or "").strip()
                body += ("\n\nExecute the saved workflow with `run_soft_ability` using ability id "
                         f"`{row['id']}` (or slug `{row.get('slug', '')}`). Pass user-supplied "
                         "workflow values in its `inputs` object. Do not imitate the workflow "
                         "manually when it is available.")
                if allowed:
                    body += "\n\nAllowed existing tools for this ability: " + ", ".join(allowed)
                out.append({
                    "name": handle,
                    "handle": handle,
                    "display_name": row.get("display_name") or row.get("slug"),
                    "description": (row.get("skill_summary") or row.get("description") or "").strip(),
                    "body": body,
                    "mode": "always_on",
                    "enabled": True,
                    "source": "Custom ability",
                    "_ability": True,
                    "soft_ability_id": row["id"],
                })
        except Exception as e:
            logger.debug("soft-ability skill collection failed: %s", e)

        # ── 4. Always-on VIRTUAL abilities (e.g. Core ▸ Base) ──
        # A virtual ability is wired into every agent and owns no agent_connections
        # row, so it never appears in `providers` — yet its bundled skill (the
        # self-improvement guide on Base) should reach every agent. Pull each
        # virtual ability's skill here, deduped by handle with the sections above.
        try:
            from app.abilities import all_raw, ability_feature_with_skill
            for ability_id, entry in all_raw().items():
                if ability_id in providers or not entry.get("virtual"):
                    continue
                feat = ability_feature_with_skill(ability_id)
                if not feat:
                    continue
                skill = _skill_from_feature(feat, ability_id)
                if skill and skill["handle"] not in seen:
                    seen.add(skill["handle"])
                    skill["ability_id"] = ability_id
                    out.append(skill)
        except Exception as e:
            logger.debug("virtual-ability skill collection failed: %s", e)

        from app.db import get_db
        from app.tools.tool_modes import resolve_skill_mode
        modes = await get_db().get_agent_skill_modes(agent_id)
        for skill in out:
            if skill.get("ability_id"):
                skill["mode"] = resolve_skill_mode(
                    skill.get("skill_key") or skill["ability_id"], modes, skill.get("mode"))
        return [skill for skill in out if skill.get("mode") != "off"]
    except Exception as e:
        logger.debug("collect_ability_skills failed: %s", e)
        return out
