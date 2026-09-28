"""COPY ME to add a new agent ability.  ← read this whole file first.

────────────────────────────────────────────────────────────────────────────
THE PHILOSOPHY (why this folder exists)
────────────────────────────────────────────────────────────────────────────
WebAgent is a small **core** plus many **drop-in plugins**. A new capability is
a NEW FILE (or pair of files) in a plugin folder — never an edit to the core.
The app discovers everything at runtime, so you register nothing by hand. This
is what lets a production build ship only the tested features (see
docs/claude/production-editions.md).

An **ability** is a host-side capability you can grant an agent (Codebase Admin,
Web Access, Terminal Control, …). Each ability lives in a GROUP FOLDER under
``plugins/abilities/``. When you drop it in, it automatically appears in:
  • the agent's tool gate          (app/tools/loader.py reads the .json tools list)
  • the feature catalog / editions (reads the .json status field)
  • the connections API            (app/api/agents.py)
  • the admin **Agent Settings** panel AND the per-agent **Abilities tab**
    (both fetch GET /api/v1/abilities/catalog and render generically)

So you do **NOT** edit any of those files. Do NOT add this ability to a list,
registry, or if/elif anywhere. Drop the files → it works. Delete them → it's
gone. That is the entire contract.

────────────────────────────────────────────────────────────────────────────
HOW TO ADD ONE
────────────────────────────────────────────────────────────────────────────
  1. Pick or create a GROUP FOLDER under plugins/abilities/ — e.g. Core/,
     Administrator/, or a brand-new folder like MyGroup/. The folder name IS
     the group (case and spaces are preserved for the UI; the internal id is
     lowercased with underscores). If the folder doesn't have a _group.json,
     one will be created with defaults.
  2. Create an ability SUBFOLDER inside the group folder, named after the
     ability (the folder name becomes the ability id):
         plugins/abilities/<Group>/<ability_id>/
     Inside that folder, create the ability files:
       <ability_id>.json   — descriptor (display name, icon, colour, tools, kind)
       <ability_id>.py     — runtime (build_tools, handlers, schemas)
     You may add any number of support files alongside them — the ability owns
     its entire subfolder.
     Copy the .json and .py skeleton from this template file. The .json carries
     everything the UI and catalog need; the .py carries only runtime code.
  3. The .py file should:
       • define a module-level ``build_tools(*, user_id, session_id, agent_id,
         agent_template_id, enabled_providers=None, **ctx) -> {name: handler}``,
       • define a module-level ``TOOL_SCHEMAS`` dict ({name: json-schema}) and,
         if any tool writes/deletes, a ``DESTRUCTIVE`` set of those names,
       • optionally ``start_background()`` / ``stop_background()`` for a
         long-lived service.
     Core discovers all of this GENERICALLY — the one block in app/tools/loader.py
     (search "Self-contained ability tools") calls your build_tools for every
     enabled ability and reads TOOL_SCHEMAS/DESTRUCTIVE AFTER the call, so you wire
     NOTHING in app/. Keep heavy imports LAZY (inside build_tools).
  4. That's it. (Files whose name starts with "_", like this one, are skipped.
     Subfolders named __pycache__ are skipped.)

  ── SKILLS (optional) ─────────────────────────────────────────────────────
  Ship a ``<id>.skill.md`` next to the descriptor to give the agent a how-to for
  this ability. Write the high-level guidance FIRST (what the capability is for,
  when to reach for it, how the tools fit together) — that becomes the ability's
  skill. Then add one ``## tool:<tool_name>`` section per tool whose guidance is
  specialised: those are delivered to the agent exactly when that tool activates,
  so the ability-level skill stays short and each tool gets its detail in time.
  Use the exact tool names from the descriptor's ``tools`` list. A skill with no
  ``## tool:`` heading is unchanged single-blob behaviour, so this is opt-in.
  Authoring guideline: plugins/abilities/README.md

────────────────────────────────────────────────────────────────────────────
ABILITY SUBFOLDER STRUCTURE
────────────────────────────────────────────────────────────────────────────

Every ability lives in its own subfolder inside the group folder. The subfolder
name IS the ability id. Inside it you can put whatever files the ability needs —
the scanner only cares that ``<id>.json`` (the descriptor) is present, and that
``<id>.py`` (the entry module) exists for runtime abilities.

Example (a complex ability with support modules):

  plugins/abilities/MyGroup/my_ability/
    ├── my_ability.json       ← descriptor (required)
    ├── my_ability.py         ← entry module (required for runtime)
    ├── my_ability.skill.md   ← skill document (optional)
    ├── helpers.py            ← support module
    ├── constants.py          ← support module
    └── __pycache__/          ← auto-generated, ignored

The group folder itself should contain only ``_group.json`` and ability
subfolders — no loose `.py` or `.json` files.

────────────────────────────────────────────────────────────────────────────
ABILITY .JSON DESCRIPTOR FORMAT
────────────────────────────────────────────────────────────────────────────
The .json file carries everything the UI needs. Copy-paste the skeleton:

{
  "display_name": "My Ability",
  "kind": "ability",          // ability | channel | oauth | credential | placeholder
  "icon": "puzzle",           // Lucide icon name (https://lucide.dev/icons)
  "color": "#7aa2f7",         // accent colour
  "description": "One-line description for the row.",
  "note": null,               // optional config note shown in the row
  "simple": true,             // true = toggle directly; false = config panel first
  "placeholder": false,       // true = grey "Coming Soon" row, no toggle
  "order": 1,                  // sort position within group (ties resolve alphabetically)
  "tools": ["my_tool_a"],     // tool NAMES this ability unlocks
  "tool_metadata": {          // optional — per-tool loop metadata for the admin
    "my_tool_a": {            // Tools view + loop visualizer. Tools without an
      "stages": ["execute_tools"],   // entry default to execute_tools stage,
      "destructive": false           // non-destructive. (Runtime confirmation
    }                                // gating still comes from DESTRUCTIVE in
  },                                 // the .py, not from here.)
  "config": {                 // optional — only when simple=false
    "settings": []
  }
}

────────────────────────────────────────────────────────────────────────────
HOW GROUPS WORK  (groups are emergent — there is no master list to edit)
────────────────────────────────────────────────────────────────────────────
The FOLDER NAME IS the group. Put your ability file in an existing folder to
join that group, or create a new folder to create a new group. That's it.

To style a group, add a ``_group.json`` in the folder:

{
  "name": "My Group",
  "icon": "layers",
  "color": "#9aa5ce",
  "desc": "Description shown under the group header.",
  "order": 100
}

If _group.json is missing, the group gets auto-styled from the folder name
(title-cased, neutral icon/colour) and sorted alphabetically after all
numbered groups. A folder with only _group.json and no abilities renders as
an empty placeholder group in the UI.

────────────────────────────────────────────────────────────────────────────
ABILITY KINDS
────────────────────────────────────────────────────────────────────────────
  "ability"      — Tool-gating ability. Needs a .py with build_tools.
  "channel"      — Messaging channel (e.g. Telegram). Has send/receive runtime.
  "oauth"        — OAuth credential provider. Configure → toggle on.
  "credential"   — Raw credential (scraper key, browser cookies).
  "placeholder"  — Grey "Coming Soon" row. No toggle, no .py needed.
"""

# ─────────────────────────────────────────────────────────────────────────────
# .PY SKELETON — copy this into your <ability_id>.py file and fill in.
# ─────────────────────────────────────────────────────────────────────────────

TOOL_SCHEMAS: dict = {}      # {tool_name: json-schema}; filled inside build_tools
DESTRUCTIVE: set = set()     # names of tools that write/delete (need confirmation)

def build_tools(*, user_id: str = "", session_id: str = "", agent_id: str = "",
                agent_template_id: str = "", enabled_providers=None, **_ctx):
    """Return {tool_name: handler} for this ability. Import handlers LAZILY here.
    Return {} to inject nothing this call (e.g. when a broader ability wins)."""
    async def my_tool_a(arg: str):
        """One-line description the agent sees."""
        return "…"

    TOOL_SCHEMAS.clear()
    TOOL_SCHEMAS.update({
        "my_tool_a": {"type": "object",
                      "properties": {"arg": {"type": "string"}},
                      "required": ["arg"]},
    })
    # DESTRUCTIVE.update({"my_tool_a"})   # only if it writes/deletes
    return {"my_tool_a": my_tool_a}

# If your handlers come from a core factory that also exports its schema/destructive
# constants, populate TOOL_SCHEMAS/DESTRUCTIVE from those INSIDE build_tools (so they
# never drift), e.g.:
#     from app.tools.my_factory import build_x, X_SCHEMAS, X_DESTRUCTIVE
#     handlers = build_x(user_id)
#     TOOL_SCHEMAS.clear(); TOOL_SCHEMAS.update(X_SCHEMAS)
#     DESTRUCTIVE.clear(); DESTRUCTIVE.update(X_DESTRUCTIVE)
#     return handlers
