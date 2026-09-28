# `app/defaults/agents/` — agent template files (bundled seeds)

Each `*.json` file in this folder is one **agent template**: the starting
configuration a new agent is cloned from. `default.json` is the canonical,
full-schema reference — it lists every field the agent-configuration page can
set. The other files are intentionally sparse and rely on defaults.

These are **bundled, read-only seed files that always ship** (so the app boots
with a working default agent even when no `data/` folder is present). The seeder
resolves its source via `app/util/paths.agents_seed_dir()`: it prefers a
`data/agents/` folder if one exists (a deployment override), and otherwise falls
back to this bundled folder.

## How these files are applied

- **Seeded on boot, and on admin re-seed.** At startup the app scans this folder
  and upserts each file into the `agent_templates` table (plus per-slot prompt
  rows). When a user gets a new agent, that template row is cloned into a real
  `agents` row.
- **Manifest-gated + non-destructive.** The seeder hashes the files; if nothing
  changed it does nothing. Bump a file's `version` (integer) to make existing
  installs pick up your edit. Prompt slots an **admin has hand-edited** in the UI
  are protected and won't be overwritten unless a force re-seed is run.
- **Override-or-default, and missing never breaks.** Every field is read
  defensively. **If a key is present it overrides the default; if it's absent the
  built-in default applies.** A partial file is valid — you only need to write
  the fields that differ. Unknown/extra keys are ignored.

## Field reference

### Identity & visibility
| Field | Type | Default | Meaning |
|---|---|---|---|
| `id` | string | — (**required**) | Stable template id (`"default"`, `"codebase-engineer"`, …). |
| `version` | int | `1` | Bump to trigger re-seed of this file's prompt slots. |
| `name` | string | `id` | Display name. |
| `description` | string | `""` | One-line summary. |
| `icon` | string | `""` | Lucide icon name. |
| `can_be_default` | bool | `true` | May be a user's default agent. |
| `is_system` | bool | `false` | Built-in system agent. |
| `is_pipeline` | bool | `false` | Part of a multi-agent pipeline. |
| `is_admin_agent` | bool | `false` | Admin-only agent. |
| `access_level` | string | `"all"` | Who may use it. |
| `discoverable` | bool | `false` | Appears in the "New Agent" template list. |

### Model & generation
| Field | Type | Default | Meaning |
|---|---|---|---|
| `model` | string\|null | `null` | Model id, or `null` to use the platform default. |
| `provider` | string\|null | `null` | Provider, or `null` for default. |
| `temperature` | float | `0.0` | Sampling temperature. |
| `max_tokens` | int | `8096` | Max output tokens per turn. |

### Limits (guardrails)
| Field | Type | Default | Meaning |
|---|---|---|---|
| `max_turn_count` | int | `0` | Max turns; `0` = unlimited. |
| `max_wall_seconds` | int\|null | `null` | Wall-clock cap per run; `null` = none. |
| `max_identical_tool_calls` | int | `0` | Loop-breaker for repeated identical calls; `0` = off. |
| `max_stall_strikes` | int | `0` | Stall-guard strikes before stopping; `0` = off. |

### Trigger & loop
| Field | Type | Default | Meaning |
|---|---|---|---|
| `trigger_type` | string | `"user_input"` | `user_input` \| `slash_command` \| `tool_call` \| `schedule` \| `webhook` \| `background`. |
| `trigger_key` | string\|null | `null` | Command/event key for the trigger type. |
| `loop_logic` | array | `[]` | List of `{ "node": "<name>", "enabled": true\|false }`. Empty = every loop step on. Only list the steps you want to turn **off**. |

### Tools — availability & permission (the per-tool knobs)
These are the two per-tool columns from the Tools panel. **Omit a tool entirely
to accept its default** (sent + auto). Only list the tools you want to change.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `tool_modes` | object | `{}` | `{ "<tool>": "always" \| "discoverable" }`. `always` = full schema sent every turn (the UI "Sent"). `discoverable` = name only until the agent calls `load_tool` (the UI "Discover"). A small set of **core** tools is always sent and can't be changed. |
| `tool_permissions` | object | `{}` | `{ "<tool>": "auto" \| "ask" \| "deny" }`. `auto` = runs unattended. `ask` = requires confirmation first. `deny` = the agent can't use it at all. |

> **Important:** `tool_permissions` sets the **policy** for this agent. It does
> **not** change a tool's built-in danger label — whether a tool is inherently
> destructive / confirmation-worthy stays defined in code and is authoritative.
> You can require confirmation or forbid a tool here; you cannot mark a dangerous
> tool as safe.

### Abilities & connections
| Field | Type | Default | Meaning |
|---|---|---|---|
| `metadata.pre_enabled_connections` | array | `[]` | Ability/connection ids switched **on** at creation (creates the `agent_connections` rows). This is the operative list. Supports **wildcards** (see below). |
| `abilities` | array | (ignored) | A readable mirror for authors; the seeder acts on `metadata.pre_enabled_connections`, so keep the two in sync. |

**Wildcards** — instead of listing every id, `pre_enabled_connections` may contain:

| Token | Expands to |
|---|---|
| `"*"` (or `"all"`) | **every** ability, all groups — this is how the shipped `default` agent enables all abilities, and it auto-includes any ability added later |
| `"Core/*"` / `"group:core"` / `"Core:*"` | every ability in that **group** (folder under `plugins/abilities/`); group matched by folder name or normalized id, case-insensitive |

Wildcards expand to concrete ids at creation time. Plain ids and integration
connection-types (`gmail`, …) still work and can be mixed with wildcards, e.g.
`["Core/*", "web_access"]`. An unknown group name expands to nothing (logged),
never an error. Only real, non-placeholder abilities are seeded — coming-soon
and display-only (Core ▸ Base) entries are skipped.

### Prompts (slots)
Each becomes an editable prompt slot. All default to `""`.

| Field | Slot | Notes |
|---|---|---|
| `system_prompt` | system | Locked base prompt. |
| `agent_prompt` | agent | Agent identity/context. |
| `user_prompt` | user | User profile/context. |
| `skills_prompt` | skills | Skills/how-to context. |
| `tasks_prompt` | tasks | Standard workflows. |
| `misc_prompt` | misc | Anything else. |
| `automation_prompt` | automation | Plain-English scheduled tasks (parsed into schedule/event rows on save). |
| `bootstrap_tools_prompt` | bootstrap_tools | The always-available tools index. (Legacy `bootstrap_tools` key still accepted.) |
| `skills` | `__skills__` | Array of on-demand skill packs. |

### Not template-seeded
A few per-agent runtime settings are **not** taken from these files — they're set
later in the UI per agent: `user_mode`, per-agent `allowed_tools` edits beyond
what `tool_permissions` derives, member/authorized-user lists, and sort order.

## Worked example — a locked-down assistant

```json
{
  "id": "support-bot",
  "version": 1,
  "name": "Support Bot",
  "description": "Answers questions; can read but not change anything.",
  "model": null,
  "temperature": 0.2,
  "max_turn_count": 40,
  "max_wall_seconds": 300,
  "tool_modes": {
    "browser_action": "discoverable",
    "generate_image": "discoverable"
  },
  "tool_permissions": {
    "commit_and_push": "deny",
    "run_command": "deny",
    "write_source": "ask"
  },
  "metadata": {
    "pre_enabled_connections": ["web_access", "diagnostics"]
  },
  "system_prompt": "You are a read-only support assistant…"
}
```

Everything not mentioned (other tools, the other limits, the other prompts)
falls back to its default. That's the whole point: write only what differs.
