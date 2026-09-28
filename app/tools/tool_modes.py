"""
Per-agent tool exposure modes — the single source of truth for *how* a tool is
presented to the model each turn.

Background
----------
Every tool the agent is allowed to use (after ability gating) is loaded into the
tool set. But not every tool's full JSON schema needs to be sent to the model on
every turn — that costs context tokens and, historically, encouraged the model
to "discover" tools it didn't have. Instead each tool has a **mode**:

  - ``core``         — fundamental, always sent, NOT toggleable. The discovery /
                       activation tools live here (without ``load_tool`` the
                       model could never reach anything else).
  - ``always``       — full schema sent every turn. This is the default for any
                       tool with no explicit per-agent setting, so existing
                       agents behave exactly as before.
  - ``discoverable`` — only the name + one-line description appear in the
                       generated ``# [TOOLS]`` index. The full schema is withheld
                       until the model calls ``load_tool("<name>")``, which marks
                       it active for the rest of the session.

The per-agent overrides live in ``agents.metadata`` under ``tool_modes`` —
a flat ``{tool_name: "always" | "discoverable"}`` map. The session's
``active_tools`` list (in ``sessions.metadata``) records which discoverable
tools the model has loaded so far. Both are written by dedicated DB methods.

This module is intentionally dependency-light: it imports the built-in tool
metadata lazily so it can be used from the loop, the loader, and the API layer
without creating import cycles.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional

# ── Core tools: always sent, never toggleable ──────────────────────────────────
# Keep this set tiny — only the meta tools the agent cannot function without.
# load_tool / load_ability are what make discoverable tools/abilities reachable;
# list_skills / load_skill are the equivalent discovery/activation tools for skills.
CORE_TOOLS = {"load_tool", "load_ability", "list_skills", "load_skill"}

# ── Canonical visibility vocabulary ────────────────────────────────────────────
# The SAME two values describe visibility for tools, skills, AND abilities:
#   "visible"      — shown / full schema sent now.
#   "discoverable" — withheld (name + one-liner only) until loaded on demand.
# The locked "core" category above is separate (always sent, not toggleable).
VISIBLE = "visible"
DISCOVERABLE = "discoverable"

# Valid explicit modes a user can pick for a non-core tool / skill / ability.
TOGGLEABLE_MODES = (VISIBLE, DISCOVERABLE)

# Legacy synonyms normalized to the canonical pair on read (older tool modes used
# "always"; older skill modes used "always_on"/"selectable"). Write paths only
# ever emit the canonical pair.
_VISIBILITY_ALIASES = {
    "always": VISIBLE, "always_on": VISIBLE, "visible": VISIBLE,
    "selectable": DISCOVERABLE, "discoverable": DISCOVERABLE,
}


def normalize_visibility(value, default: Optional[str] = None) -> Optional[str]:
    """Map any legacy or canonical visibility string to VISIBLE / DISCOVERABLE.
    Returns ``default`` for empty/unknown input."""
    if isinstance(value, str):
        return _VISIBILITY_ALIASES.get(value.strip().lower(), default)
    return default


# Key under which the per-agent tool-mode map lives in agents.metadata.
AGENT_TOOL_MODES_KEY = "tool_modes"

# Key under which the per-agent ability-visibility map lives in agents.metadata
# ({ability_id: "visible"|"discoverable"}). Discovery is tuned PER AGENT — there
# is deliberately no app-wide ability-visibility default (the admin sets only the
# ceiling: which abilities exist + permission limits).
AGENT_ABILITY_MODES_KEY = "ability_modes"

# Key under which a per-agent DEFAULT visibility lives in agents.metadata
# ({"discovery_default": "visible"|"discoverable"}). It is the fallback applied to
# any ability (and, through the ability gate, its tools) that has NO explicit
# per-item choice. Absent ⇒ "visible" (every existing agent behaves exactly as
# before). Set to "discoverable" so an agent's whole tool surface collapses to a
# compact `# [ABILITIES]` menu that the model pulls from on demand via
# load_ability — instead of shipping every ability's full tool schema each turn.
AGENT_DISCOVERY_DEFAULT_KEY = "discovery_default"

# Key under which the per-agent ability-SKILL visibility map lives in
# agents.metadata ({ability_id: "visible"|"discoverable"}). Lets an agent make a
# bundled skill's body always-shown (visible) or load-on-demand (discoverable),
# independent of the ability's own visibility.
AGENT_SKILL_MODES_KEY = "skill_modes"

# ── Per-ability caller-ACCESS vocabulary ───────────────────────────────────────
# A SEPARATE axis from visibility. Visibility tunes how the AGENT sees an ability
# (visible vs discoverable); access decides WHICH CALLER may trigger it at all.
# A ladder of three rungs, strictest last:
#   "everyone"   — anyone, including not-signed-in anonymous guests (the default).
#   "registered" — only signed-in (non-anonymous) accounts.
#   "admin"      — only admin users.
# Enforced server-side at tool-assembly time (the ability's tools are never
# materialized for a caller below the rung) — a real boundary, not a prompt hint.
ACCESS_EVERYONE = "everyone"
ACCESS_REGISTERED = "registered"
ACCESS_ADMIN = "admin"

# Caller capability is also ranked on this ladder; an ability is kept only when
# the caller's rank ≥ the ability's required rank.
ACCESS_RANK = {ACCESS_EVERYONE: 1, ACCESS_REGISTERED: 2, ACCESS_ADMIN: 3}

# Legacy / friendly synonyms normalized to the canonical trio on read. Write
# paths only ever emit the canonical values.
_ACCESS_ALIASES = {
    "everyone": ACCESS_EVERYONE, "all": ACCESS_EVERYONE, "public": ACCESS_EVERYONE,
    "anonymous": ACCESS_EVERYONE, "anon": ACCESS_EVERYONE, "": ACCESS_EVERYONE,
    "registered": ACCESS_REGISTERED, "auth": ACCESS_REGISTERED,
    "user": ACCESS_REGISTERED, "users": ACCESS_REGISTERED, "signed_in": ACCESS_REGISTERED,
    "admin": ACCESS_ADMIN, "admins": ACCESS_ADMIN, "admin_only": ACCESS_ADMIN,
}


def normalize_access(value, default: Optional[str] = None) -> Optional[str]:
    """Map any legacy/friendly access string to the canonical
    everyone/registered/admin trio. Returns ``default`` for unknown input."""
    if isinstance(value, str):
        return _ACCESS_ALIASES.get(value.strip().lower(), default)
    return default


def resolve_ability_access(ability_id: str, access_map: Optional[Dict[str, str]]) -> str:
    """The ability's effective required-access rung for an agent: the per-agent
    choice (normalized) else the default ``everyone`` (no restriction)."""
    explicit = normalize_access((access_map or {}).get(ability_id))
    return explicit or ACCESS_EVERYONE


# Key under which the per-agent ability-ACCESS map lives in agents.metadata
# ({ability_id: "everyone"|"registered"|"admin"}). Per-agent only — absent means
# everyone (no restriction), so every existing agent is unaffected until set.
AGENT_ABILITY_ACCESS_KEY = "ability_access"

# Key under which the active (loaded) tool-name list lives in sessions.metadata.
SESSION_ACTIVE_TOOLS_KEY = "active_tools"

# Key under which the active (load_ability'd) ability-id list lives in
# sessions.metadata. Mirrors active_tools / active_skills.
SESSION_ACTIVE_ABILITIES_KEY = "active_abilities"

# Key under which the per-session SUPPRESSED ability-id list lives in
# sessions.metadata. An ability here is turned OFF for the conversation from the
# chat Abilities panel even when the agent's config makes it ``visible`` — its
# tools + skill are withheld until the user re-arms it. Mirrors active_abilities.
SESSION_SUPPRESSED_ABILITIES_KEY = "suppressed_abilities"


def resolve_mode(name: str, modes_map: Optional[Dict[str, str]],
                 global_defaults: Optional[Dict[str, Dict[str, str]]] = None) -> str:
    """Resolve a tool's effective exposure mode for an agent.

    Resolution order (effective = agent-override ▸ global-default ▸ built-in):

    core                → if the tool is in CORE_TOOLS (locked).
    always/discoverable → the agent's explicit setting, when present and valid.
    always/discoverable → the global per-tool default's ``visibility``, when the
                          agent has no explicit setting (``global_defaults`` is the
                          ``{tool: {permission?, visibility?}}`` admin blob).
    always              → the hybrid default for any tool with neither setting,
                          so nothing an agent already had silently stops being sent.
    """
    if name in CORE_TOOLS:
        return "core"
    explicit = normalize_visibility((modes_map or {}).get(name))
    if explicit:
        return explicit
    if global_defaults:
        g_vis = normalize_visibility((global_defaults.get(name) or {}).get("visibility"))
        if g_vis:
            return g_vis
    return VISIBLE


def is_sent(name: str, modes_map: Optional[Dict[str, str]],
            active_names: Optional[Iterable[str]],
            global_defaults: Optional[Dict[str, Dict[str, str]]] = None) -> bool:
    """True when this tool's full schema should be sent to the model this turn.

    Sent when it is core/always, OR when it is discoverable and the model has
    already loaded it this session (so a freshly loaded tool becomes callable on
    the next turn). ``global_defaults`` is threaded through so the global
    visibility fallback is honoured identically to ``resolve_mode``.
    """
    mode = resolve_mode(name, modes_map, global_defaults)
    if mode in ("core", VISIBLE):
        return True
    return name in set(active_names or ())


def is_locked(name: str) -> bool:
    """True when the tool's mode cannot be changed (core tools)."""
    return name in CORE_TOOLS


# ── Ability ↔ tool mapping (derived from BUILTIN_TOOL_METADATA) ────────────────

def ability_for_tool(name: str) -> Optional[str]:
    """The ability that gates a built-in tool, or None for always-on utilities /
    DB tools / unknown names. Reverse lookup over loader.ABILITY_TOOLS."""
    try:
        from app.tools.loader import ABILITY_TOOLS
    except Exception:
        return None
    for ability, names in ABILITY_TOOLS.items():
        if name in names:
            return ability
    return None


def tools_for_ability(ability: str) -> List[str]:
    """All built-in tool names an ability provides — used to seed 'discoverable'
    defaults the moment an ability is toggled on for an agent."""
    if not ability:
        return []
    try:
        from app.tools.loader import ABILITY_TOOLS
    except Exception:
        return []
    return list(ABILITY_TOOLS.get(ability, []))


# ── Ability visibility ─────────────────────────────────────────────────────────

def resolve_ability_mode(ability_id: str, modes_map: Optional[Dict[str, str]],
                         default: Optional[str] = None) -> str:
    """Effective visibility of an ability for an agent: the agent's explicit
    per-ability choice (normalized) ▸ the agent's ``discovery_default`` (passed in
    as ``default``) ▸ ``VISIBLE``. Abilities have no app-wide default — discovery
    is tuned per agent, so ``default`` carries the agent-level preference."""
    explicit = normalize_visibility((modes_map or {}).get(ability_id))
    if explicit:
        return explicit
    return normalize_visibility(default) or VISIBLE


def resolve_skill_mode(skill_key: str, modes_map: Optional[Dict[str, str]],
                       default: Optional[str] = None) -> str:
    """Resolve one bundled general skill's visibility.

    New records use ``ability_id:skill_id``. A legacy ``ability_id`` override
    remains the fallback for the implicit ``core`` record, so existing agents
    retain their saved choice without migration.
    """
    modes = modes_map or {}
    explicit = normalize_visibility(modes.get(skill_key))
    if not explicit and ":" in skill_key:
        ability_id, skill_id = skill_key.split(":", 1)
        if skill_id == "core":
            explicit = normalize_visibility(modes.get(ability_id))
    if explicit:
        return explicit
    return normalize_visibility(default) or DISCOVERABLE


def ability_is_revealed(ability_id: str, modes_map: Optional[Dict[str, str]],
                        active_ability_names: Optional[Iterable[str]],
                        suppressed_ability_names: Optional[Iterable[str]] = None,
                        ability_default: Optional[str] = None) -> bool:
    """True when an ability's tools + bundled skill should flow into the prompt
    this turn: it is ``visible``, OR it is discoverable but already pulled in via
    ``load_ability`` this session.

    ``suppressed_ability_names`` lets a session turn an ability OFF from the chat
    Abilities panel even if it's ``visible`` by the agent's config — a suppressed
    ability is never revealed (its tools + skill are withheld until re-armed).
    ``ability_default`` is the agent-level discovery default applied when the
    ability has no explicit per-ability visibility choice."""
    if ability_id in set(suppressed_ability_names or ()):
        return False
    if resolve_ability_mode(ability_id, modes_map, ability_default) == VISIBLE:
        return True
    return ability_id in set(active_ability_names or ())


def tool_hidden_by_ability(
    name: str,
    ability_modes: Optional[Dict[str, str]],
    active_ability_names: Optional[Iterable[str]],
    active_tool_names: Optional[Iterable[str]] = None,
    suppressed_ability_names: Optional[Iterable[str]] = None,
    ability_default: Optional[str] = None,
) -> bool:
    """True when a tool must be withheld entirely (not indexed, not sent) because
    the ability that gates it is ``discoverable`` and has NOT been loaded yet — or
    has been suppressed for this session from the chat Abilities panel.

    Tools with no gating ability (always-on utilities, DB tools), tools of a
    revealed ability, and a tool the agent already loaded individually
    (``load_tool``) are never hidden by this rule. ``ability_default`` is the
    agent-level discovery default applied to an ability with no explicit choice."""
    ability = ability_for_tool(name)
    if not ability:
        return False
    if ability_is_revealed(ability, ability_modes, active_ability_names,
                           suppressed_ability_names, ability_default):
        return False
    return name not in set(active_tool_names or ())


def render_ability_index(
    entries: List[Dict],
    starred: Optional[Iterable[str]] = None,
    order: Optional[List[str]] = None,
) -> str:
    """Build the ``# [ABILITIES]`` block listing discoverable abilities whose
    tools + how-to skill are withheld until ``load_ability``.

    ``entries`` is a list of ``{id, name, desc}`` dicts. Returns "" when empty.

    ``starred`` / ``order`` make the menu **message-aware** (the semantic router
    ranks the abilities against what the user just asked):
    - ``order`` — ability ids most-relevant-first; entries are listed in this
      order (any not in it follow, alphabetically). Absent ⇒ plain alphabetical,
      exactly as before.
    - ``starred`` — ids to flag with ★ under a "likely relevant right now" note,
      so the agent's eye lands on the ability this message probably needs. Absent
      ⇒ no stars.
    Both are optional and additive: with neither, this renders the original
    alphabetical menu, so nothing changes when routing is off/unavailable.
    """
    rows = [e for e in entries if e.get("id")]
    if not rows:
        return ""
    star_set = set(starred or ())

    lead = (
        "These abilities are available but their tools and how-to skill are "
        "withheld to keep your context lean. Call `load_ability(\"<id>\")` the "
        "moment a task needs one — it returns the ability's skill plus its tools "
        "and keeps them active for the rest of this conversation. Don't guess an "
        "ability's tools before loading it."
    )
    if star_set:
        lead += (
            " Entries marked ★ look most relevant to the current request — load "
            "one of those first if it fits."
        )
    lines: List[str] = ["# [ABILITIES]", lead]

    # Cache order is a product invariant, not a per-message ranking.  The nested
    # Simple -> Standard -> Advanced sequence lets smaller and larger agents
    # share the longest DeepSeek prefix.  `order` is retained for API
    # compatibility but intentionally no longer reshuffles this shared block;
    # message-aware recommendations are emitted later as a separate overlay.
    from app.agent.cache_profiles import ordered_ability_ids

    position = {
        ability_id: i
        for i, ability_id in enumerate(ordered_ability_ids(r["id"] for r in rows))
    }
    ordered = sorted(
        rows,
        key=lambda r: (
            position.get(r["id"], len(position)),
            (r.get("name") or r["id"]).lower(),
        ),
    )

    for e in ordered:
        nm = e.get("name") or e["id"]
        desc = (e.get("desc") or "").strip().split("\n")[0]
        mark = "★ " if e["id"] in star_set else ""
        base = f"- {mark}`{e['id']}` — {nm}"
        lines.append(f"{base}: {desc}" if desc else base)
    return "\n".join(lines).strip()


# ── Prompt index rendering ─────────────────────────────────────────────────────

def render_index(
    entries: List[Dict],
    ask_names: Optional[set] = None,
) -> str:
    """Build the ``# [TOOLS]`` system-prompt block from the agent's loaded tools.

    ``entries`` is a list of ``{name, desc, mode, active}`` dicts (one per loaded
    tool). Tools that are core/always (or already loaded) are listed as ready to
    use; discoverable-but-unloaded tools are listed under a load-on-demand
    heading that points the model at ``load_tool``. Returns "" when there are no
    tools at all.

    ``ask_names`` — tool names that are confirmation-gated in the current
    execution mode; each is tagged inline so the agent knows it will pause for
    the user (and can ask them to approve or switch to Auto) rather than
    discovering the gate only by being blocked.

    The catalog is generated solely from the real, authorized tool set. Denied
    tools are deliberately omitted rather than disclosed as blocked capabilities.
    """
    if not entries:
        return ""

    ask_set = set(ask_names or ())

    ready: List[str] = []
    loadable: List[str] = []
    for e in entries:
        name = e.get("name") or ""
        if not name:
            continue
        desc = (e.get("desc") or "").strip().split("\n")[0]
        line = f"- `{name}` — {desc}" if desc else f"- `{name}`"
        if name in ask_set:
            line += "  _(needs the user's confirmation to run in this mode)_"
        mode = e.get("mode")
        if mode == "discoverable" and not e.get("active"):
            loadable.append(line)
        else:
            ready.append(line)

    if not ready and not loadable:
        return ""

    lines: List[str] = [
        "# [TOOLS]",
        "These are the tools available to you on this agent. Tools under "
        "“Ready to use” can be called directly right now.",
    ]
    if ask_set:
        lines.append(
            "Tools marked _(needs the user's confirmation)_ are gated by the "
            "current execution mode: calling one pauses for the user. When such a "
            "tool is the right next step, briefly tell the user what you want to "
            "do and ask them to approve it — or, if they've already approved your "
            "plan, switch to Auto with `set_execution_mode(\"auto\")` and proceed."
        )
    if ready:
        lines.append("\n## Ready to use")
        lines.extend(sorted(ready))
    if loadable:
        lines.append(
            "\n## Load on demand"
            "\nThese tools are available but their full input schema is withheld "
            "to keep your context lean. Call `load_tool(\"<name>\")` the moment a "
            "task needs one — its parameters arrive immediately and it stays "
            "callable for the rest of this conversation. Don't guess a tool's "
            "arguments before loading it, and never call a tool that isn't listed "
            "here."
        )
        lines.extend(sorted(loadable))
    return "\n".join(lines).strip()
