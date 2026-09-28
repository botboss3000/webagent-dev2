"""TEMPLATE — copy me to add a new integration. DO NOT edit the core to add one.

================================================================================
 WebAgent structure philosophy:  CORE app  vs.  DROP-IN plugins
================================================================================
A new capability is **a new file in a plugin folder**, never an edit to the core.
The app *discovers* plugin files at runtime — you register nothing by hand.

  • This folder (app/integrations/)  → OAuth / API integrations (this template)
  • app/events/sources/              → "when X happens" event sources
  • app/communications/plugins/      → message channels (telegram, slack, …)
  • plugins/abilities/Memory/       → connected knowledge sources
  • app/secrets/                     → secrets vaults
  • app/encryption/                  → encryption methods
  • plugins/billing/processors/      → payment processors
  • app/scheduler/providers/         → remote scheduler backends

To add an integration:
  1. Copy this file to app/integrations/<your_capability>_tools.py
  2. Fill in the FEATURE header and the TOOLS list below.
  3. That's it — it's auto-discovered. Do NOT add it to app/tools/loader.py,
     app/tools/core_tools.py, app/main.py, or any central registry / if-elif.

To REMOVE a capability: delete its file. Nothing else references it.

Every plugin declares a FEATURE header so the feature catalog + editions know
about it (App Config → Features; docs/claude/production-editions.md). `status`
is the production gate: stable / beta / experimental. A file with no FEATURE is
treated as `unknown` and excluded from non-`full` editions — so always add one.
================================================================================
"""

import json

# ── The self-description header (read by the feature catalog + edition gating) ──
FEATURE = {
    "id": "example",                       # stable machine id (also the catalog id)
    "display_name": "Example Integration",
    "category": "integration",
    "status": "experimental",              # stable | beta | experimental
    "summary": "One line describing what this integration does.",
    "requires": ["whatever credentials/setup it needs"],
    # ── Optional: bundle a skill that teaches the agent how to use these tools.
    # When this ability is enabled for an agent, the skill joins its # [SKILLS]
    # catalog (load-on-demand by handle). Mint the handle ONCE and freeze it.
    # "skill_mode": "selectable",          # selectable | always_on
    # "skill_handle": "example_ab12cd34",  # minted once, never regenerated
    # "skill": "## Using the Example integration\n- step one...\n- step two...",
}


# ── Tool handler(s) ─────────────────────────────────────────────────────────
# Handlers are async and are called as handler(user_id=..., agent_id=..., **args).
async def example_do_something(user_id: str = "", agent_id: str = "", text: str = "") -> str:
    # ... call the provider's API here (use app/integrations/oauth_helper.py for
    # OAuth token handling), then return a JSON string result.
    return json.dumps({"status": "ok", "echo": text})


# ── The tool registry the loader auto-discovers ─────────────────────────────
# Each entry: name, provider (must match the agent_connections.connection_type
# gate; omit for always-on), handler, parameters (JSON Schema), stages, and the
# destructive flag (writes/sends → requires confirmation).
TOOLS = [
    {
        "name": "example_do_something",
        "provider": "example",
        "handler": example_do_something,
        "parameters": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "Some input."}},
            "required": ["text"],
        },
        "stages": ["execute_tools"],
        "destructive": False,
    },
]
