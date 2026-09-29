# Production editions, drop-in features & the feature catalog

> **Status:** Phases 1–6 landed. This is the durable design + roadmap for the
> single codebase that ships as a **production** edition (only vetted features),
> a **full** edition (everything, the default), or any **fork** (a custom mix) —
> by treating every capability as a self-describing, drop-in plugin the app
> discovers at runtime and gates by edition.

This doc is the source of truth for the work. The at-a-glance index lives in
`CLAUDE.md`; the detail lives here.

---

## The problem this solves

The app ships with many capabilities that vary in maturity — some integrations,
encryption methods, payment processors, storage backends, etc. are battle-tested,
others are experimental stubs. We want:

1. A **production** release that contains/loads **only tested** features.
2. A **full / dev** build that has everything.
3. **Forks** that are any mix of the two.
4. Adding or removing a capability to be, ideally, **"copy a file into a folder"**
   (or delete it) — no central registry edits.
5. The app to be able to **discover what code is available to it** and report it.

---

## The core model: three things travel together

Every capability is a **feature**. A feature is a self-contained file (or small
folder) that carries three things:

| Part | What it is |
|------|-----------|
| **Tools** | the actions the agent can take (already how integrations/events/etc. work) |
| **Self-description header** (`FEATURE`) | id, display name, category, **status** (stable / beta / experimental), what it requires, and an optional **skill** |
| **Skill document** *(optional)* | the full how-to for the feature, injected into the agent on demand |

Drop the file in → its tools **and** its know-how appear. Delete it → both vanish.

### The `FEATURE` header

Each plugin module may expose a module-level `FEATURE` dict. All fields are
optional except an implied id (defaults to the file name):

- `id` — stable machine id (defaults to the module's file stem).
- `display_name` — friendly name for the UI.
- `category` — `integration` / `event_source` / `channel` / `connector` /
  `scheduler` / `encryption` / `payment` / `secrets` / `storage` / `tool`.
- `status` — `stable` / `beta` / `experimental`. **This is the production gate.**
  A file with no header is treated as `unknown` (and surfaced as such) so it is
  never silently assumed production-ready.
- `summary` — one line shown in the catalog.
- `requires` — list of human-readable prerequisites ("Google OAuth credentials").
- `skill` / `skill_mode` — see *Ability-bundled skills* below.

The header is **additive metadata** — the existing discovery contracts
(`TOOLS`, `source_cls`, `plugin_cls`, the hand-listed registries) are untouched,
so adding a header never changes how a feature loads.

---

## Editions

An **edition** decides which features are actually switched on.

- **full** — everything (dev / internal build). **This is the default.**
- **production** — only features marked `stable` (plus any explicitly named).
- **fork-X** — any custom mix.

The active edition is resolved from the `WEBAGENT_EDITION` env var (falling back
to `full`). The edition definitions live in `app/features/editions.json`
(committed; code has a built-in default if the file is absent).

**Hybrid cut (chosen):**

- **Runtime** — at startup the app reads the active edition and (eventually) only
  activates the features that edition allows. Flipping editions is a config
  change, not a rebuild. Dropping a file in works live.
- **Packaging (later)** — an optional build step physically strips the
  non-included files when cutting a real production artifact, so experimental
  code isn't even shipped.

> **Phase 1 does NOT gate anything.** The edition is read and *reported* only.
> Default `full` means every feature still loads exactly as today. Enforcement
> arrives in a later phase, behind the edition switch.

---

## The discovery catalog

`app/features/catalog.py` builds one **catalog** by scanning every plugin folder
and reading each feature's header. For each feature it reports: id, display name,
category, status, whether it's **drop-in** (true folder-scan) or **registry**
(needs a central edit today), whether it carries a header yet, what it requires,
and — once gating is on — whether the active edition includes it and **why not**
if excluded.

This catalog *is* the "what code is available to it" discovery the design calls
for. It's exposed admin-only at **`GET /api/v1/features`** and rendered in
**App Config → Features**.

The catalog is built **lazily and fully guarded** — a broken plugin can never
break boot or the endpoint; it just shows up as an error row.

---

## Ability-bundled skills

The app already has an on-demand **skill** system: a skill has a name, a
"when to use it" line (always shown), and a body (shown only after the agent
calls `load_skill`, unless marked always-on). See `app/agent/skills.py`.

A feature/ability can **carry its own skill**. When the ability is enabled for an
agent, its skill is folded into the agent's `# [SKILLS]` catalog automatically.
The agent always sees the "when to use" line; the moment it wants to use the
ability it loads the full body. **Default is load-on-demand**; a feature may mark
its skill always-on when the guidance is short and essential.

This co-locates the **"what" (tools)** with the **"how" (skill)** so they can
never drift apart, and means exploring an ability teaches the agent everything
about it.

**For a JSON-described ability** (`plugins/abilities/<group>/<id>.json`), the body
lives in a sibling **`<id>.skill.md`** file (read whole by `_resolve_skill_body`
in `app/abilities/__init__.py`) and the always-shown "when to use it" line is the
JSON's **`skill_summary`** field, with optional **`skill_mode`**
(`selectable` — load-on-demand, the default — or `always_on`). The catalog
builder passes those two fields plus `skill_handle` through to the entry, so
`collect_ability_skills` can surface them; without `skill_summary` the bundled
skill would show a blank summary line. Example:
`plugins/abilities/Core/automation/automation.json` + `automation.skill.md`.

## Ability enable-state — catalog-driven, stored in one file

Whether an ability is **on at the app level** is NOT hardcoded anywhere. The set
of toggleable abilities is simply every `kind:"ability"` drop-in the catalog
discovers, and each one's on/off choice is persisted (per-admin, non-secret) in
**`data/config/agent-abilities.json`** by `app/admin/ability_config.py`. So
adding an ability is a pure drop-in — the admin **Agent Tools** panel shows its
toggle and it persists with **no** edit to any core list (there used to be a
hardcoded `_ABILITY_CONFIG_KEY` map in `app/admin/integrations.py`; it is gone).

- **Default when unset — SHIP POLICY: abilities are ON by default.** A `kind:"ability"`
  drop-in with no stored admin toggle and no explicit `default_enabled` in its
  descriptor resolves to **enabled**, so a fresh install ships fully unlocked and
  any ability added later is auto-on — no hand-toggling. An ability that must ship
  OFF opts out with an explicit `"default_enabled": false`. `ability_default_enabled`
  is the single source (defaults True); `ui_catalog()`'s `enabled` field mirrors it,
  and `_ability_app_enabled` applies *stored choice ▸ this default*. (Credentialed
  abilities are still hidden by a separate secret-present gate in
  `get_admin_configured_providers` until their key/cookie is supplied, so "on by
  default" never leaks a tool that has no credentials behind it.)
- **Display order, same story:** the ability table's group order and per-ability
  order also live in this file (the **`order`** section: `{groups, abilities}`),
  not just in the drop-in JSONs. Each drop-in still declares a seed `order`
  (`_group.json` for groups, the descriptor for abilities); on first boot
  `ensure_bootstrapped` snapshots those into the file, and `ui_catalog()` then
  sorts by the file's order, falling back to the descriptor seed for anything not
  yet listed (so a freshly-dropped ability still lands where its descriptor says
  until an admin reorders it). The `GET`/`PUT /admin/integrations/abilities/order`
  endpoints read and persist it. An older file that predates the `order` section
  is backfilled from the descriptors on next boot — current order is preserved.
- **Migration:** on first boot when the file is absent, `ensure_bootstrapped`
  seeds it from the previous vault rows + the `tool_defaults` blob, so nothing an
  admin already configured resets.
- **Secrets never live here** — API keys, OAuth/BYO secrets and tokens stay in
  the encrypted vault (`data/db/vault.db`). The file holds only non-secret
  toggles, the display `order`, global per-tool defaults (`tools`), and
  non-secret config knobs.

## The same drop-in model for UI pages

The shell's **pages** — the main header tabs and the Admin Tools sidebar views —
use the identical three-layer model, but the plugin folders live under **`ui/`**,
not `plugins/`:

- **Descriptor:** `ui/<page>/page.json` (a **main** header tab) or
  `ui/admin-tools/<view>/page.json` (an **admin** sidebar view) — declares `id`,
  `kind`, default `label`/`icon`/`order`, optional `locked`, and (for main pages
  **and drop-in admin views**) the `html` partial + `entry`/`start`/`stop`, or an
  `iframe` URL. Two fields make a page **fully self-contained**: `css` (string or
  list, relative to the folder) auto-injects the page's stylesheets so it needs
  **no `<link>` in `index.html`** (omit it and a `<id>.css`/`<folder>.css` in the
  folder is auto-picked); `router` names an optional `server.py` in the folder
  that exposes a FastAPI `router`, auto-mounted as the page's **backend API** so
  it needs **no `include_router` in `app/main.py`**. (`.py` files under `ui/` are
  never served to the browser.)
- **Discovery + catalog:** `app/ui_pages/` scans both roots and serves
  `GET /api/v1/pages/catalog` (mirrors `app/abilities/` + `/abilities/catalog`).
  Catalog keys are namespaced by kind so a main page and an admin view can share
  an id (`terminal` is both).
- **Override store:** `app/admin/page_config.py` owns
  `data/config/main-panel-pages.json` + `admin-panel-pages.json` — the admin's
  per-page `order`/`label`/`icon`/`hidden` (mirrors `ability_config.py`). The
  descriptor value is only the **seed**; the file wins once set. Endpoints:
  `PUT /admin/integrations/pages/{scope}/order`,
  `POST /admin/integrations/pages/{scope}/{id}`.

So **dropping a `ui/<page>/page.json` folder adds a page — and deleting the folder
removes it — with no edit anywhere else.** Everything renders from the catalog:
`index.html` / `header-build.js` / `partial-loader.js` / `tabs.js` (main) and
`header-build.js` (`__buildAdminStrip`) / `partial-loader.js` / `ui/shared/js/files.js`
(admin). Concretely, the four things that *used* to need a manual edit per page no
longer do: the header tab/strip + content mount (catalog), the HTML partial
(catalog `html`), the **CSS** (`__ensurePageStyles` injects the descriptor `css`),
and the **backend router** (`ui_pages.discover_routers()` mounts the folder's
`server.py`). `tabs.js` no longer statically imports ANY page module — every
page's `start`/`stop` is a dynamic `import()` of its descriptor `entry`, so a
removed folder can never break the shell's load. **Admin views are true drop-ins
too:** a new `ui/admin-tools/<id>/` folder with a `page.json` (carrying `html` +
`entry`/`start`/`stop` + optional `css`) gets its strip icon built from the
catalog, its partial auto-loaded (its `<template data-slot="#admin-tools">` main +
optional `#files-sidebar` panel), and its lifecycle driven by a dynamic `import()`
of its `entry` — exactly like a main page. **Every** admin view is now a true
drop-in: the last two inline built-ins — the File Manager (Explorer) and the
Terminal — were split into `ui/admin-tools/explorer/explorer-view.js` and
`ui/admin-tools/terminal/terminal-view.js` (each owns its own tab engine and
shares no code with the other), so `partial-loader.js`'s `ADMIN_SUB_PAGES` and
`BUILTIN_ADMIN_IDS` are now empty and `ui/shared/js/files.js` is purely the
shared Admin-Tools frame that dispatches every view via its descriptor
`entry`/`start`/`stop`.
Each admin view also owns its **own collapse / switch-display control** in its panel
header (`.files-panel-collapse-btn`, auto-injected) — there is no shared strip
collapse button.

## The same drop-in model for agent engines (alternate runtimes)

The drop-in pattern also covers the agent's **runtime**, not just its tools. A
normal agent runs the LLM loop in `app/agent/loop.py`. An agent whose record
carries **`metadata.engine = "<id>"`** instead hands its WHOLE turn to a matching
**engine adapter** in `plugins/engines/` (a drop-in plugin tree, sibling of
`plugins/abilities/`). The adapter is a single async
generator that yields the loop's OWN event vocabulary (`stream` / `tool_call` /
`tool_result` / `agent_step_end` / `response` / `error`) and persists the same
`interactions` rows — so the chat UI, live streaming, and reload are unchanged;
only the brain differs.

- **One generic seam, no per-engine `if`.** Near the top of `stream_agent_events`
  (right after the agent record loads, BEFORE provider resolution) one block reads
  `metadata.engine` and calls `get_engine_stream(id)`. The registry
  (`plugins/engines/__init__.py`) **auto-discovers** any engine folder whose
  package `__init__` exposes `ENGINE_ID = "<id>"` + `async def stream(**ctx)`.
  Adding an engine = a new `plugins/engines/<id>/` folder; no edit to the loop or
  a central list.
- **Shipped engine: `claude_code`** — drives the locally-installed Claude Code CLI
  (`plugins/engines/claude_code/`). Built from the `local-claude` agent
  template (admin-level; **not** discoverable — it is deliberately kept OUT of the
  normal Add-Agent template dropdown). Runs `claude` headless in `stream-json`
  mode, prompt on **stdin** (never an argv — the Windows `.CMD` shim makes argv
  unsafe), output read on a **worker thread** (never asyncio subprocess — Windows
  SelectorEventLoop), scoped to a configured starting folder, resuming the Claude
  conversation mapped to the chat (`sessions.metadata.claude_session_id`).
- **Gating** is the existing admin line (`db.is_user_admin`) checked inside the
  adapter — same posture as every other admin power (a signed-in admin, including
  over a tunnel). Cost is **informational only** (Claude spends its own login).
- **A truly distinct agent in the UI** — Local Claude Code shares almost none of
  the normal agent's panel. It is created from the single **"New Agent" tile** by
  picking the **Claude** segment of the create card's WebAgent / Claude / Terminal
  type chooser (admin-only segments, matching the runtime gate); the lower area then
  shows an inline sign-in + settings form, and the card's "+" finalises it. It then
  opens into its **own stripped, tab-less card** (no Config / Prompts / Agent Loop
  / Abilities / Members / Monetization tabs) showing only its Claude settings. All
  of this lives in its own module `ui/main-panel/agents/js/claude-agent.js`
  (`renderClaudeCreateBody` + `renderClaudeSettings`); `view.js` builds the chooser
  (`_buildMockTypeToggle`) + accept (`_acceptEngineCreate`) and diverts a
  `claude_code` agent to `_renderClaudeAgentCard` instead of the normal card.
- **Per-agent config** lives in `metadata.claude_code` (folder / extra_flags /
  model / append_persona), edited on that distinct card and saved through the
  agent PUT's `claude_code` lane (mirrors `chat_ui`). The card's **Default chat
  mode** select (Ask/Plan/Auto) is the SHARED `metadata.default_execution_mode`
  field (same as normal agents), mapped onto `claude --permission-mode` by
  `_resolve_permission_mode` (Ask→default, Plan→plan, Auto→`--dangerously-skip-
  permissions`). It supersedes the legacy `act_freely` boolean, which is still
  read as the backward-compat fallback for agents that never picked a mode
  (`act_freely=True`⇒Auto).

## Companion `<ability>.json` — attachment-handling abilities

An ability that handles **uploaded files** ships a third package file beside its
`.py` and `.skill.md`: a companion **`<ability>.json`** (e.g.
`plugins/abilities/Core/image_vision/image_vision.json`). It declares, as pure data:

- `handles` — the mime-types / categories it routes (`"image/png"`, `"image/*"`,
  or a bare category like `"image"`);
- `worker_system` (+ optional `worker_instruction`) — the system prompt for the
  one-shot worker that reads the file, with `{context}` / `{request}` placeholders
  so the description is tailored to the conversation;
- `guidance` — the copy folded into the user turn: `switch_available` /
  `describe_only` (when the file was handled) and `not_enabled` (the
  anti-hallucination fallback when the ability is off or unconfigured).

The **attachment type-router** (`app/agent/attachment_router.py`) scans these JSONs
and maps each upload to its owning ability by mime-type — so adding a new file-type
ability (document, video, audio …) is **drop-in**: ship the `.py` + `.skill.md` +
`.json`, with **no** edit to the router or the `attachment_describe` loop node. See
[agent-loop.md](agent-loop.md) for the node's routing logic.

### Companion `<ability>.json` — per-agent settings panel (`config.settings`)

Any ability can expose **per-agent settings** as pure data in its `.json` under
`config.settings` (e.g. `plugins/abilities/Core/automation/automation.json`,
`agent_orchestration/agent_orchestration.json`). Each entry is `{ key, label, type, default, hint }`
plus `{ min, max, step }` for numbers. `type` is `number`, `boolean`, or text.
This is **declare-only**: both ability panels (per-agent Abilities tab + admin
Agent Settings) render the rows generically from
`GET /api/v1/abilities/{id}/config-schema` via the shared `_renderSettingInput`
helper in `ui/shared/js/dom-utils.js` (boolean → Enabled/Disabled dropdown,
number → number input). The per-agent panel saves to the agent's `automation`/
ability connection row as `config.ability_settings.<key>` (string values).

**Reading them at runtime:** the ability's `_load_*_config` clamps `ability_settings`
over code defaults. **Gotcha:** the panel saves every value as a STRING, so a
boolean comes back as `"true"`/`"false"` — never coerce with bare `bool(v)`
(`bool("false")` is `True`). Use a tolerant parse (see `_coerce_bool` in
`automation.py`).

**The admin tree is the global ceiling (admin = max; per-agent ⊆ admin).** The
**admin** Agent Settings tree sets app-wide maximums; a **per-agent** Abilities
tab may match them or be **stricter, never looser**. Two kinds of limit carry it:

- **Tool permissions** — strictness order **Auto < Ask < Deny**. The admin's
  per-tool global default (`/admin/integrations/tool-defaults`) is the loosest an
  agent may pick. The runtime already caps the effective permission
  (`app/tools/tool_defaults.resolve_permission` takes the stricter of agent vs
  global); the per-agent UI enforces it too — `GET /agents/{id}/tools` returns a
  `ceiling` per tool, and the permission tri (`_buildPermissionTri` in dom-utils.js)
  clamps the looser-than-ceiling side. The tri track keeps the plain 3-position
  graphic — the limit is shown in the tool-state panel, where the
  looser-than-ceiling row (e.g. Auto under an Ask ceiling) is disabled and renders
  greyed.
- **Config knobs** — a setting opts in with a `ceiling` key: `"off"`/`"on"` for
  booleans (which side binds), `"max"`/`"min"` for numbers (the admin value is the
  cap/floor). `app/admin/ability_config.effective_ability_config` is **schema-aware**:
  it clamps each per-agent value to the admin's app-level value per that rule (an
  un-annotated knob keeps the old agent-wins merge — back-compatible). Every
  runtime reader must route the per-agent `ability_settings` through
  `effective_ability_config` for the cap to bind (see `automation.py:_load_automation_config`).
  The per-agent panel reflects it: `config-schema` attaches the admin value as
  `ceiling_value`, `_renderSettingInput` emits `data-ceiling`/`data-ceiling-value`,
  and `agent-ability-table.js:_applyConfigCeiling` locks a binding boolean / caps a
  number + shows a 🔒 "set by admin" note. To give an ability a config ceiling: add
  the `ceiling` keys to its `.json` **and** route its `_load_*_config` through
  `effective_ability_config`.

### Companion `credentials` block — an ability that needs secrets (the common process)

An ability that needs credentials (an API key, pasted cookies, a token) **declares
them as data** in its `.json` instead of hand-rolling a save endpoint + config
card. The block is:

```
"credentials": {
  "scope": "admin" | "user" | "agent",   // who owns the secret
  "requires": ["api_key"],               // keys that must be set to count "configured"
  "fields": [
    {"key": "provider", "label": "Provider", "type": "select", "options": [...]},
    {"key": "api_key",  "label": "API Key",  "type": "password", "secret": true},
    {"key": "endpoint", "label": "Endpoint", "type": "text"}
  ]
}
```

One generic module — **`app/abilities/credentials.py`** — maps `(ability, scope,
user, agent)` to a vault row and does all I/O on the encrypted vault
(`auth_elements`): **secret** fields go to `secret_ref` (JSON when more than one),
non-secret fields to `config`. Scope resolution mirrors how the app already stores
secrets — `admin` → `user_id=admin`; `user` → the caller; `agent` →
caller + `label="agent:<id>"`. Three generic endpoints serve it (on the abilities
router in `app/api/agents.py`): **`GET`/`POST`/`DELETE
/api/v1/abilities/{id}/credentials`** — the GET returns the declared fields, the
non-secret values, and a `{secret_key: bool}` "is set" map, but **never the secret
values**; a blank secret field on save means "leave it unchanged".

**Gating is scope-aware:** `get_admin_configured_providers()` in
`app/admin/integrations.py` only **hides an ability until its secrets exist for
`scope:"admin"` abilities** — one global secret (e.g. the scraper's API key) that
nothing a per-agent user could supply, so it's pointless to show the ability to
agents until the admin fills it in the admin table. **`scope:"user"` / `scope:"agent"`
credential abilities stay visible even when empty** — their secret is entered by the
agent's own user *inside the per-agent panel*, so hiding-until-configured would be a
chicken-and-egg (nowhere to enter it). Those abilities load through the normal
`enabled_providers` path on app-enable alone; their tools simply return
`not_configured` at run time until the user supplies the secret.

**The form is shared by both panels.** `buildCredentialsSection` lives in
`ui/shared/js/dom-utils.js` and is rendered **inline in the ability's expand panel**
by **both** the admin ability table (`admin-ability-table.js`) **and** the per-agent
Abilities tab (`agent-ability-table.js`) — the `SISTER-PANEL` contract, one
implementation. The backend decides `can_edit` from scope (admin-scope → admin only;
user/agent-scope → the caller's own), so the admin table owns global secrets while a
user pastes their own per-user secret in their agent. The descriptor's
`has_credentials` flag — surfaced by `ui_catalog()` **and** on each connection row
(`connection_rows()`) — tells each panel when to render the form. The ability's `.py`
reads its creds at run time via `credentials.read_credentials(<id>, user_id=…,
agent_id=…)`.

**Consumers:** `web_scraper` (admin scope, requires an API key) under
`plugins/abilities/Web/`, and **`browser_control`** (user scope) — whose optional
pasted-cookie box is a *fallback* for its `web_session_*` cookie-replay tools, which
prefer the user's live in-app browser login. (The credentials block carries no
`requires`, so it never gates the ability — per the scope-aware rule above,
user-scope creds stay visible/optional.) A back-compat fallback in
`read_credentials` reads legacy vault keys (`scraper_config` for the scraper,
`browser_session` for cookies — mapped for both the retired `browser_cookies` id and
`browser_control`) when no new-style row exists, so creds entered before the refactor
keep working. *(The standalone "Browser Cookies" ability was folded into Browser
Control — the cookie-replay tools are useless without a browser to log in with.)*
**Declare-only** — a new credentialed ability is a pure drop-in: ship the `.py` +
`.json` (with a `credentials` block) and storage, UI, endpoints, and gating all come
for free.

### Optional `tool_metadata` block — loop-stage info for the admin Tools view

An ability's `.json` may carry a top-level **`tool_metadata`** map giving per-tool
loop metadata for `/admin/tools` and the loop visualizer:
`"tool_metadata": {"my_tool": {"stages": ["guardrails", "execute_tools"], "destructive": true}}`.
It is aggregated by `tool_metadata()` in `app/abilities/__init__.py` and merged
into the loader's `BUILTIN_TOOL_METADATA` at import (explicit entries override the
loader's legacy literals; tools with no entry anywhere default to the
`execute_tools` stage, non-destructive). **Declare-only** — a new ability's tools
show up in the admin Tools panel with no loader edit. Note this is display/stage
metadata only; what actually gates confirmation at run time is the ability's
`DESTRUCTIVE` set in its `.py`.

### Agent self-management is fenced — an agent can't widen its own box

An agent with the **Agent Management** ability may configure *other* agents it
owns, but the tool layer **refuses self-targeting** of the mutating tools
(`update_agent`, `set_agent_tool`, `set_agent_ability`, `edit_agent_prompt`,
`manage_agent_skills`) — enforced in `_wrap_with_limits` in
`plugins/abilities/Core/agent_management.py` by comparing the target to the
caller's own `agent_id`. This stops an agent from re-enabling an ability or
widening a tool its admin switched off. Human admins are unaffected (they act via
the REST/UI endpoints, which carry no caller-agent identity).

Spawned **clones never exceed their master**: `create_clone_agent`
(`app/db/local.py`) clamps granted abilities to the master's enabled set and
unions the master's tool-deny list — a DB-level ceiling that holds even if a
spawn caller passes an over-broad list. The orchestration/automation spawn paths
also clamp in their handlers; the DB clamp is the backstop.

### Per-ability caller access — the "Available to" gate (per-agent, runtime-enforced)

Independent of *whether* an ability is enabled, an agent can mark each enabled
ability with the **caller-access level** required to trigger it — a ladder of
three rungs, strictest last:

| Level (`available_to`) | Who may trigger the ability |
|------------------------|-----------------------------|
| `everyone` (default)   | anyone, including not-signed-in anonymous (`anon_*`) guests |
| `registered`           | signed-in (non-anonymous) accounts only |
| `admin`                | admin users only |

- **Storage — per-agent, no migration.** The map lives in `agents.metadata`
  under **`ability_access`** (`{ability_id: level}`), exactly mirroring the
  `ability_modes` visibility map. The default is `everyone`, and the writer
  (`db.set_agent_ability_access`) stores **only genuine restrictions** (an
  `everyone` choice clears the key), so the map is empty for every existing agent
  and nothing changes until a level is set. Vocabulary + helpers
  (`normalize_access` / `resolve_ability_access` / `ACCESS_RANK`) sit beside the
  visibility ones in `app/tools/tool_modes.py`.
- **Enforcement — a real boundary at tool-assembly time, not a prompt hint.** The
  chat/run paths call `load_tools(..., gate_caller_access=True)`; that one flag
  routes the agent's enabled set through
  `app/agent/ability_access.filter_abilities_for_caller`, which ranks the **live
  caller** (admin > registered > everyone, via `caller_access_rank` — same tier
  logic as `chat._enforce_agent_access_policy`) and drops any ability whose rung
  the caller doesn't meet **before its tools are materialized**. So the gated
  ability's tools never enter the tool dict, the `# [TOOLS]` / `# [ABILITIES]`
  index, or the callable set — the agent genuinely cannot use them. The matching
  ability-bundled skill is also stripped from the turn by
  `append_skills_section` (it threads the caller id), so the prompt never
  advertises tools the caller doesn't have.
- **Opt-in flag, so previews stay honest.** `gate_caller_access` defaults **off**;
  only the genuine runtime loads (the agent loop + the chat tool-count builds)
  pass `True`. The config/preview endpoints (`GET /agents/{id}/tools`, the
  schema-preview) leave it off so they always show the agent's **full configured**
  set regardless of who is viewing — the gate keys off the *chatter*, never the
  *viewer*.
- **Fail-safe directions.** Caller ranking fails **down** (any error → the lowest,
  anonymous rung) so a glitch can never escalate privileges; the filter fails
  **open** (any error reading the per-agent map → no restriction, == the default
  state) so a transient read error never bricks an agent's tools.
- **UI + API.** Set per-ability on the per-agent **Abilities** tab via the
  **Available to** dropdown (`agent-ability-table.js`), saved by
  `PUT /api/v1/agents/{agent_id}/abilities/{ability_id}/access`. This is the
  finer-grained, per-ability sibling of the agent-wide `user_mode` access policy
  (anonymous / register / authorized).

### Skill ids (collision-proof + stable)

- **Agent-authored skills** keep using the **name** as their identity. The save
  path rejects a duplicate name on the same agent, so a random suffix is
  unnecessary. Loaded/active tracking stays name-based — unchanged behavior.
- **Ability-contributed skills** get a unique handle minted **once** and stored
  with the ability — e.g. `automation_fj27enfiu287`. It is **random for
  uniqueness but frozen for stability**: never regenerated, so a loaded
  ability-skill still matches itself across restarts and conversation replay.
- The two live in separate id spaces (friendly names vs minted handles), so they
  can never collide; two abilities that both ship an "Email" skill simply get two
  different handles. The agent loads its own skills by name and ability skills by
  handle, so the call handle itself disambiguates; the catalog shows each skill's
  exact handle plus a friendly display name (with a source tag to break ties).

> Skill *wiring* (merging the two sources into the prompt, minting handles,
> teaching `load_skill` to find ability skills) is Phase 1/2 work. Phase 1 only
> reserves the `skill` / `skill_mode` fields in the header so the format is ready.

---

## What stays permanently core (never editioned out)

Making these optional would just be a way to brick the app:

- **Agent-loop essentials** — the tool-discovery tools (`load_tool`,
  `list_skills`, `load_skill`), `get_time` / `get_date` / `calculate` /
  `read_attachment` / `register_user`, the DB-query tool, and memory.
- **SQLite local storage** — the universal fallback every other backend falls
  back to.
- **Inline-DB secrets** — the bootstrap vault that unlocks every other vault.
- **"None" encryption** pass-through and **billing-absent = free** — already the
  defaults.

Everything else — including **web search** — becomes a demotable add-on.

### Surfacing the always-on core tools — the `virtual` "Base" row

Those permanently-core tools are wired into every agent by the loader and belong
to **no** ability, so historically they never appeared in the ability tables. To
make them visible + permission-manageable, a descriptor may set
**`"virtual": true`** — a **display-only** ability that *lists* tools it does not
own. The canonical one is **Core ▸ Base** (`plugins/abilities/Core/base/base.json`),
which lists `load_tool` / `load_ability` / `list_skills` / `load_skill` /
`set_execution_mode` / `get_time` / `get_date` / `calculate` / `read_attachment` /
`memory` / `session_search` / `read_own_prompt` / `edit_own_prompt` /
`save_own_skill` / `remove_own_skill` / `register_user` and the webhook tools.

A virtual ability:

- has **no `.py` runtime** and is **NOT** coerced to a "Coming Soon" placeholder
  despite that (the placeholder rule skips `virtual`);
- **may still ship a bundled skill.** `collect_ability_skills` (in
  `app/agent/ability_skills.py`) has a dedicated pass that surfaces a `virtual`
  ability's `<id>.skill.md` for **every** agent — independent of
  `gather_enabled_providers`, since a virtual ability owns no `agent_connections`
  row and never appears in the enabled set. Base uses this to ship the
  **self-improvement guide** (`base.skill.md`, handle `self_improvement`,
  `selectable`) that teaches the agent when to save a skill vs a memory vs a
  prompt edit;
- is deliberately **excluded from `tools_map()` / `ABILITY_TOOLS`** — so it gates
  nothing and the runtime can never withhold these always-on tools via ability
  gating (`tool_hidden_by_ability`). The panels group its tools by a SEPARATE
  display-only lookup, `abilities.virtual_ability_for_tool()`, used only to LABEL
  a tool's owning row in `GET /agents/{id}/tools` — never for gating;
- should pair with **`locked_on: true` + `protected: true`** so it reads as the
  always-on, undeletable foundation it is.

Tier-1 tools render with an **"always on"** permission pill (no editable
Auto/Ask/Deny) in both tables — the shared `_TOOL_ALWAYS_ON` set in
`ui/shared/js/dom-utils.js` mirrors `loader.TIER_1_ALWAYS_ON`; only the
genuinely-gateable Base tools (`memory`, `session_search`, `read_own_prompt`,
`edit_own_prompt`, `save_own_skill`, `remove_own_skill`, the webhook tools)
expose a permission control.

**The self-improvement toolset** (all four under Base, self-targeted — they can
only ever act on the running agent):
- `read_own_prompt` / `edit_own_prompt` — read and improve the agent's OWN prompt
  (slots). Admin-LOCKED slots are read-only to the edit tool, so safety/identity
  sections stay protected.
- `save_own_skill` / `remove_own_skill` — the agent teaches **itself** reusable
  how-to "skill-type memories": it writes into its own (admin-base) skill list,
  so a saved skill shows up in `list_skills` and loads on demand via `load_skill`.
  This is the un-fenced, self-only counterpart to `agent_management`'s
  `manage_agent_skills` (which manages OTHER agents).
- The bundled `self_improvement` guide skill teaches the decision rule: a reusable
  **skill** for how-to / procedures, the `memory` tool for **facts**, and
  `edit_own_prompt` for durable changes to standing behavior — otherwise save
  nothing (ordinary replies aren't worth storing).

This whole toolset is the deliberate inverse of `agent_management`'s prompt/skill
tools, which manage OTHER agents and refuse self-targeting; see "Agent
self-management is fenced".

---

## Billing: agent tier (core) + platform tier (optional, strippable)

Billing is split into two tiers so a **public / agent-only** edition can ship
monetization with **no trace** of the marketplace/platform layer:

- **Agent tier (always present)** — `plugins/billing/`. An agent admin prices
  their agent; the end user pays; the agent keeps 100%. Holds pricing,
  wallet/credits, the access gate, usage, the payment processors, and the
  `/api/v1/billing` agent+user routes. Its core charge engine knows nothing
  about platform fees.
- **Platform tier (optional)** — `plugins/admin/billing/`. The marketplace
  operator's cut of each charge, app-wide policy/ceilings, payout (Connect)
  onboarding, the platform-admin routes + UI, and the platform ledger.

The two connect through a **neutral billing-extension seam**
(`plugins/billing/extensions.py`): the core looks up an optional extension and,
finding none, every hook is a no-op (no fee, no platform config). The platform
package registers itself via a generic `plugins/<group>/billing/` folder scan, so
the core never names it. The dependency only ever points **platform → agent** —
deleting the platform package leaves the agent tier fully working with no
dangling reference. The platform admin UI is a drop-in view
(`ui/admin-tools/billing/`) and its schema self-installs / lives in
`migrations/021_billing_platform.sql`; the core billing schema carries no platform
columns or tables.

**Cutting the public edition:** the `public` edition in `editions.json` lists the
platform paths under `exclude_paths`; `scripts/build_edition.py public <out>`
physically removes them (code + UI + migration) and sanitizes the output manifest
so the artifact never names what was stripped. Verify with a grep over the output.

---

## Dual-repo production mirror (the "Release to production" button)

Separate from cutting an *edition artifact* by hand, the running app can publish
a **trimmed copy of itself** to a second GitHub repo from the **Git Control**
page. The problem it solves: git can't push "everything except folder X" — every
commit carries the whole tree — so **production is its own repo** (own folder,
own remote, own history), *regenerated on demand* from the dev tree minus the
folders **and files** an admin marked dev-only. It reuses the editions trim
philosophy but is driven entirely from the UI, with no rebuild.

- **Engine:** `app/production_mirror.py` owns the shared config store
  (`data/config/production-mirror.json` — production folder, remote URL, exclude
  list, last-release record; gitignored, no secrets) and the async
  `release_events()` generator. A release: lists the dev project's real files via
  `git ls-files --cached --others --exclude-standard` (so `.gitignore` is honoured
  automatically — runtime DBs, secrets and caches can never leak), drops the
  excluded paths (folders or files), mirrors what's left into the production repo (**wiping all but
  its `.git` first** so deletions propagate), **secret-scans the staged diff**
  (same patterns as the dev commit path), then commits + pushes.
- **First-setup is non-destructive.** If the production remote already has
  history, the first release **clones** it so the trimmed tree commits *on top*
  (a fast-forward, nothing overwritten); only a genuinely empty remote gets a
  fresh `git init`. Auth: the shared `github_token` from `provider.json` is
  embedded into the push URL **in memory only** (never written to the mirror's
  `.git/config`), so it works without a credential helper and leaks nothing.
- **Endpoints** (admin-gated, on the existing Git router `app/api/github.py`):
  `GET/POST /api/v1/github/production/config`, `GET/POST …/production/exclude`
  (the exclude list — *shared* with the File Explorer), `POST …/production/exclude-bulk`
  (apply many add/remove changes atomically — drives the folder check-all/uncheck-all),
  `GET …/production/status`, `POST …/production/release` (NDJSON stream, like the
  ⭐ commit-and-push).
- **UI, two surfaces sharing one exclude list:** the **File Explorer**
  (`ui/admin-tools/explorer/explorer-view.js`) has an **eye toggle** that flips the tree into
  *production-preview* mode — a checkbox on every folder **and file** (ticked =
  ships, untick = dev-only; a path under an excluded folder shows unticked +
  locked), repainting in place via `refreshProdMarks()` with no tree reload.
  **Folder checkboxes are tri-state:** a native *indeterminate* dash means "some
  items inside are dev-only" (`prodFolderState`/`prodExcludedDescendants`). Clicking
  a partial (or empty) folder **includes its whole subtree** — clears the folder's
  mark and every dev-only descendant in one atomic `…/exclude-bulk` call
  (`prodIncludeSubtree`); clicking a full folder **excludes the whole folder**
  (`prodExcludeSubtree`). So the cycle is: partial → click → all-on → click → all-off.
  Right-click *Exclude / Include from production* does the same (folder entry tracks
  the tri-state), and an excluded path carries a "Dev" badge. The backend already
  accepts individual file paths (`set_excluded`/`set_excluded_bulk`/`_is_under_excluded`/
  `_norm_rel`), so file-level exclusion needed no engine change. The **Git page**
  exposes a single streaming **Release to production** button in the sync row (next
  to Merge — `ui/shared/js/files-git.js`, `renderReleaseButton` +
  `doReleaseToProduction`); the old standalone Production section (repo URL / folder
  inline edits, status badge, release-note field) was removed — the button alone
  drives the trim → copy → commit → push, auto-writing the release note. The prod
  repo URL + folder are now set from the File Explorer's production settings (still
  `POST /api/v1/github/production/config`). This is an admin-tools extension, **not**
  a new ability — it lives in the Git page + File Explorer's own modules, wired to
  nothing in the agent core.

## Multi-repo Git Control page (the repo selector)

The same **Git Control** page can manage **more than this app's own repo**. A
dropdown at the very top picks **which repo the page operates on** — its Changes
list, commit graph, branches and commit/push/pull all re-point to the selected
repo. This is the companion to the production mirror: the mirror *publishes a
trimmed copy*, this lets you *directly view and commit to* any other local repo.

- **Engine:** `app/git_repos.py` — a small, self-contained registry (mirrors
  `production_mirror.py`; no import from `github.py`, avoids a cycle). The **built-in
  WebAgent entry** is *synthesized on the fly* (project root + live `origin` + the
  shared `github_token`) — never persisted, can't be edited/removed — so selecting
  it behaves byte-for-byte like before. **Added repos** are stored in the gitignored
  `data/config/git-repos.json` with their own `folder`/`remote_url`/`token`
  (per-repo key, *local file* storage, exactly like the shared one — not the vault).
  `list_repos()` strips the raw key to a `has_token` flag; `resolve_active()` returns
  `(folder, token)`; `validate_folder()` confirms a folder is a git work tree and
  returns its branch/origin to pre-fill the Add form.
- **Re-pointing is one chokepoint.** `github.py` keeps a module global
  `_ACTIVE_REPO` (default = project root) that `_run_git` uses as its cwd. Each UI
  endpoint calls `_use_active_repo()` at its top (resolves the saved selection →
  sets `_ACTIVE_REPO` + token); the commit/push core inherits whatever the caller
  set. **Agents are deliberately decoupled:** the **Git Control** ability calls
  `_pin_to_project_root()` first, so agent git always acts on *this* app's repo and
  never follows whatever the user has selected on the page. Same single-process
  global convention as the existing `_TOKEN_CACHE`.
- **Endpoints** (admin-gated, on the existing Git router): `GET …/github/repos`,
  `GET …/repos/validate?path=`, `POST …/repos` (add), `POST …/repos/select`,
  `POST …/repos/{id}` (edit; blank token keeps the key), `DELETE …/repos/{id}`
  (the built-in can't be removed). Route order: `/repos/select` precedes
  `/repos/{repo_id}` so "select" isn't swallowed as an id. **New routes → needs a
  `:8080` restart** to register (same "Not Found until restart" pattern as the
  production endpoints).
- **UI:** `ui/shared/js/files-git.js` — `renderRepoSelector()`/`renderRepoForm()`
  + `wireRepoSelector()` and the `selectRepo`/`openRepoForm`/`submitRepoForm`/
  `removeRepoEntry` actions; the **Browse…** folder picker reuses
  `GET /api/v1/files/tree` (`loadFolderBrowser`). The Production section and the
  shared-key row hide for non-default repos (`_isActiveBuiltin()`). Styles in
  `ui/main-panel/admin-tools/files.css` (`.fg-repo-*`, theme tokens, dark + light).
  Another admin-tools extension — **not** an ability, wired to nothing in the core.

## Current modularity scorecard (what Phase 2 fixes)

| Subsystem | Folder | Today |
|-----------|--------|-------|
| OAuth integrations | `app/integrations/` | ✅ drop-in (folder scan) |
| Event sources | `app/events/sources/` | ✅ drop-in (folder scan) |
| Communication channels | `app/communications/plugins/` | ✅ drop-in (folder scan) |
| Scheduler providers | `app/scheduler/providers/` | ✅ drop-in (drop a file with a `PROVIDER` dict) |
| Data connectors | `app/connectors/` | ✅ drop-in (auto-discovered; DB `type` CHECK relaxed) |
| Encryption methods | `app/encryption/` | ✅ drop-in (auto-discovered by `FEATURE.id`) |
| Payment processors | `plugins/billing/processors/` | ✅ drop-in (drop a file with `processor_cls`) |
| Secrets vaults | `app/secrets/` | ✅ drop-in (auto-discovered by `cls.name`) |
| Storage backends | `app/db/` | ⚠️ mode switch (irreducible-fallback heavy; left as-is) |
| Built-in tools (web search…) | abilities | ✅ catalogued + editionable (web search via `web_access`) |
| **Agent abilities** | **`plugins/abilities/`** | ✅ **drop-in + self-contained tools** (one FOLDER per ability — `plugins/abilities/<Group>/<id>/` holding `<id>.json` descriptor, `<id>.py` runtime with `build_tools()` handlers + `TOOL_SCHEMAS`/`DESTRUCTIVE` + any background service (`start_background`/`stop_background`) + an optional FastAPI **`router`** (auto-mounted), optional `<id>.skill.md`, and any support files; both ability panels + the loader's generic discovery + the catalog all read it — no per-ability wiring in core) |

> **The root `plugins/` tree.** As of the abilities refactor the canonical home
> for drop-in capability files is a top-level **`plugins/`** folder (sibling to
> `app/`), so "plugin vs core" is structural, not just convention. **Abilities
> live there now** (`plugins/abilities/`). The other subsystems above still sit
> under `app/` and are migrating into `plugins/` one at a time (each move keeps
> its core manager/base class in `app/` and lifts only the drop-in leaf files
> out). Until a subsystem is migrated, add its plugins in the `app/…` folder
> shown above.

**Done:** Phase 2 converted the registry subsystems to folder-scan discovery
(additive — the irreducible fallbacks stay hard-wired) and relaxed the connector
DB-schema coupling; ability-bundled skills are wired (see above). Phase 3 made
the built-in abilities (incl. web search via `web_access`) catalogued and
editionable. Phase 4 wired **edition gating** at every load site and added the
packaging step. Storage selection was intentionally left on its mode-switch
(every backend falls back to it, so folder-scan there buys little and risks the
fallback) — new storage backends still register via discovery for the catalog.

---

## Per-agent discovery default (`discovery_default`) — discoverable by default

Visibility (`visible` vs `discoverable`) is normally set **per ability / per tool**.
Independent of those per-item maps, an agent may carry a single **default
visibility** in `agents.metadata` under **`discovery_default`**
(`AGENT_DISCOVERY_DEFAULT_KEY` in `app/tools/tool_modes.py`). It is the fallback
applied to any ability that has **no explicit per-ability choice**:

- **absent ⇒ `visible`** — every pre-existing agent behaves exactly as before
  (all enabled abilities' tools shipped each turn).
- **`discoverable`** — every un-chosen ability is withheld behind the compact
  `# [ABILITIES]` menu; its tools **and** bundled skill arrive only when the
  model calls `load_ability("<id>")`, then stay active for the conversation. This
  collapses a large tool surface (e.g. 100+ schemas) down to a short ability list
  plus the always-on core/Base tools, slashing the per-turn input the model must
  prefill (the dominant chat-latency + cost driver on a big agent).

**Resolution order** is *explicit per-ability choice ▸ `discovery_default` ▸
`visible`*, so an admin/agent can still force a specific ability `visible` or
`discoverable` regardless of the default. It threads through
`resolve_ability_mode(id, modes, default)` and the `ability_default=` kwarg on
`ability_is_revealed` / `tool_hidden_by_ability`; the runtime reads it once via
`db.get_agent_discovery_default(agent_id)` in the agent loop, the prompt builder
(ability-skill gate), the live chat Abilities panel, and the per-agent Abilities
tab (so the dropdown shows the TRUE effective mode). Skills already default to
`discoverable`, so they need no separate flag.

**Who gets it on by default:** the **default WebAgent agent**
(`create_agent_for_user`) and **every Agent-Manager-created agent**
(`create_custom_agent`) are seeded `discovery_default="discoverable"` via the
shared `_with_discovery_default()` helper (which never overrides an explicit
template choice). Existing default agents (`is_user_default=1`) are **backfilled
once at boot** in `_initialize` so the in-place default agent benefits without a
re-create. Setter: `db.set_agent_discovery_default(agent_id, mode)`.

## App-wide per-tool defaults (resolution order)

Independent of editions, two per-tool dimensions resolve through the same
three-level order — **agent override ▸ app-wide global default ▸ built-in
default** — for both **permission** (`auto`/`ask`/`deny`) and **visibility**
(`always`/`discoverable`):

- The **global default** is an admin-set DEFAULT every agent inherits unless
  that agent has its own per-tool override. It lives in one `tool_defaults`
  `auth_elements` row under the admin user (`get_global_tool_defaults()` in
  `app/admin/integrations.py`); see the four `/admin/integrations/tool-defaults`
  endpoints.
- Permission has **no per-agent override map** today (it's encoded across the
  agent's `allowed_tools` block-list = deny and `safety_policy.destructive_tools`
  = ask), so the v1 semantics are **union/additive**: a global can ADD a deny or
  ask, but an agent's own lists always win for tools it names, and a global can
  never relax (force `auto` on) a tool the agent already gated. The default blob
  is empty, so nothing changes until an admin sets a value. **Tier-1 always-on
  tools can never be denied.**
- Visibility threads through `resolve_mode()`/`is_sent()` in
  `app/tools/tool_modes.py` (each takes an optional `global_defaults` arg);
  permission is resolved for display by `resolve_permission()` in the sibling
  `app/tools/tool_defaults.py`. The agent loop fetches the blob once per run and
  applies it to the `# [TOOLS]` index, the sent-schema filter, the deny merge
  into `load_tools`, and the ask-set.

## Phases (all landed)

1. ✅ **Foundation** — header format, discovery catalog, edition manifest
   (default `full`), `GET /api/v1/features`, the App Config Features report,
   headers on the 3 drop-in subsystems.
2. ✅ **Unify Tier-2 to drop-in** — secrets/encryption/payments/scheduler/
   connectors now auto-discover (additive; fallbacks intact); connector DB CHECK
   relaxed; ability-bundled skills wired with minted handles.
3. ✅ **Demote built-in tools to add-ons** — abilities (incl. web search via
   `web_access`) are catalogued and editionable.
4. ✅ **Cut the production edition** — edition gating wired at every load site
   (default `full` = no-op) + `scripts/build_edition.py` packaging step.
5. ✅ **Abilities become drop-in + root `plugins/` tree** — each host ability is
   now one self-describing file in `plugins/abilities/` (declaring the tools it
   gates + its render metadata). `app/abilities/` is the core manager; the loader
   map, the feature catalog, `app/api/agents.py`'s connection rows, and **both**
   ability panels (`ui/js/app-config.js`, `ui/js/agents.js`) all read it via
   `GET /api/v1/abilities/catalog`. No per-ability constants remain in those
   files.
6. ✅ **Abilities ship their own tools (fully self-contained)** — every
   tool-bearing ability now carries its handlers in its own file via a
   `build_tools(*, user_id, session_id, agent_id, agent_template_id,
   enabled_providers=None, **ctx)` hook, plus module-level `TOOL_SCHEMAS` and
   `DESTRUCTIVE`. The loader has ONE generic discovery block (search
   "Self-contained ability tools") that calls every enabled ability's
   `build_tools` and reads its schemas/destructive set afterward; all the old
   per-ability `if "<id>" in enabled_providers:` wiring blocks were removed.
   Most tool-bearing abilities now carry their handlers **fully inside the
   ability file** — true self-contained drop-ins with no core factory:
   `image_generation` / `app_control` / `agent_management` / `automation` /
   `agent_orchestration` (the former `delegate_to_agent` /
   `list_delegatable_agents`) under `plugins/abilities/Core/`, `diagnostics` /
   `terminal_control` under `Administrator/`, the Wiki Context ability
   (`plugins/abilities/Memory/wiki_context.py`), and `web_access`
   (`web_search` / `get_weather` / `maps_geocode`) under `Web/`. A few still
   lean on a shared library rather than a core factory — e.g. the Codebase
   Admin / Git Control / UI Admin abilities wrap the admin adapter
   (`plugins/admin/adapter.py`); the ability file just wraps it and mirrors its
   constants. There are **no** surviving `app/tools/*_tools.py` factory files
   behind these abilities — `app/tools/` now holds only host infrastructure
   (`loader`, `registry`, `tracker`, `tool_modes`, `tool_defaults`,
   `core_tools` (trimmed to always-on host tools), `browser`,
   `read_attachment`, `optimizer_tools`). Adding/removing a
   tool-bearing ability now needs **zero** loader edits. **Next:** migrate the
   remaining `app/…` plugin subsystems into `plugins/` (managers/base classes
   stay in `app/`; only leaf files move).
7. ✅ **Behavioural abilities (no tools) ship hooks, not handlers** — an ability
   can carry `"tools": []` and instead expose a module-level hook that core
   dispatches generically, so the *behaviour* is swappable by toggling abilities
   with no core edits. Two such seams exist today, both resolved by reading the
   agent's enabled `agent_connections` (`section='ability'`) rows and checking a
   module attribute (never a hardcoded ability id):
   - **`TURN_HOOK`** — `async hook(db, user_id, session_id, emit)` fired after
     every chat turn (`turn_hooks_for_agent` in `app/abilities/`, dispatched from
     `app/chat/execution.py`). Example: **Session Namer**.
   - **`CONTEXT_STRATEGY` (context-management, pick-one)** — an ability that sets
     `CONTEXT_STRATEGY = True` and exposes `CONTEXT_SETTINGS(db, agent_id)` /
     `CONTEXT_COMPACT(db, user_id, session_id, settings)` /
     `CONTEXT_STATUS(messages, settings)` (+ optional `CONTEXT_ASSEMBLE(...)` to
     fully override history assembly). `context_strategy_for_agent` returns the
     **single** enabled strategy (lowest `order` wins, warns on >1 — strategies
     are mutually exclusive, unlike additive tools); the agent loop's pre-call
     gauge + memory-guard compaction and `session_history.build_openai_history_from_session`
     call it instead of importing a compactor by name. Default implementation:
     **Context Control** (`plugins/abilities/Memory/context_control/`), whose engine
     stays in `app/agent/context_control.py` + `agent/compaction.py`. Beyond the
     strategy hooks it also ships one ordinary agent-facing tool via `build_tools`,
     **`compact_context`** (deliberate self-compaction — `maybe_compact(..., force=True)`),
     plus a bundled `context_control.skill.md` on when to use it; a strategy ability
     can carry tools and the two seams coexist. A
     sliding-window or retrieval-recall strategy drops in by shipping the same
     contract and is enabled *instead* of Context Control. Its settings come from
     `config.ability_settings` on the `agent_connections` row, exactly like a
     tool-bearing ability's config panel.
   - **`locked_on` (safety-device flag, descriptor-level).** A behavioural ability
     may declare `"locked_on": true` (Context Control does) to become an
     **always-on safety device**: its toggle is fixed ON in **both** ability panels
     (the admin Agent-Settings table and the per-agent Abilities tab) — clicking it
     reports "Cannot be deactivated" instead of saving — and it is forced enabled at
     every gate: the UI catalog (`ui_catalog` sets `enabled: true`), the per-agent
     connections API (`get_agent_connections` forces the row's `enabled`), the
     strategy resolver (`context_strategy_for_agent` unions in any `locked_on`
     strategy even with no per-agent row), and `get_context_settings` (treats it
     enabled despite a missing/disabled row). Both the admin disable endpoint
     (`DELETE /admin/integrations/abilities/{id}`) and the per-agent connection
     upsert refuse to turn it off. Pair it with `"protected": true` so the folder
     can't be deleted from the UI either. Only the on/off is fixed — the ability's
     `config.settings` knobs stay freely editable. Check via
     `abilities.ability_is_locked_on(id)`.

### Infrastructure abilities — a long-lived service + its own HTTP API

An ability need not gate an agent tool at all. Two generic seams let a whole
**observability / infrastructure** subsystem live as a drop-in ability (`tools: []`),
so a production edition strips it by deleting the folder — no core edit:

- **`start_background` / `stop_background` (background service).** An ability module
  that defines these async hooks gets its service discovered by
  `abilities.background_service_hooks()` and started by core's **`ability-background`**
  leader service (`app/main.py`) — the drop-in policy explicitly forbids adding a
  per-ability startup line to `main.py`. (Leader-gated, so in multi-instance only the
  leader pre-warms it; single-process wins leadership instantly, so behaviour is
  unchanged.)
- **`router` (self-mounted HTTP API).** An ability module that exposes a module-level
  FastAPI `router` (or a `ROUTERS` list) gets it discovered by
  `abilities.ability_routers()` and included by core's generic **ability-router** mount
  loop (`app/main.py`, the HTTP twin of the service loop) — no `include_router` line per
  ability. Router discovery is independent of the app-enable toggle (mirrors the
  always-mounted page/service hooks); an excluded edition strips the folder instead.

**Shipped consumer: the Render Recorder ability**
(`plugins/abilities/Administrator/render_recorder/`), the browser-side flight recorder
(sibling of the always-core server-side `diagnostics`). Its one file holds the engine,
its `/api/v1/recordings` API (browser intake + admin read/clear + lease issue), and its
service hooks. It ships **off by default** (`default_enabled:false`), and capture is
started only by an admin opening the header switch in one specific browser tab — the
ability toggle is the presence/edition switch, that in-tab switch is the live on/off for
that document only. Delete the folder → the
capture service, the API routes, and the feature all vanish with no edit to `main.py`.

## App functions — background services, not agent abilities (`app_function`)

Some catalog entries are **not** things an agent chooses to use — they run
**automatically for the app itself**: the **Session Namer** (auto-titles a chat
after its opening turns), **Context Control**'s automatic compaction train, and
the **Render Recorder** flight recorder. These share a trait: no agent decides to
invoke them, so listing them in the two *ability* tables (admin Agent Tools + the
per-agent Abilities tab) is misleading. A descriptor opts out with
**`"app_function": true`**.

- **It is a UI-classification flag ONLY — runtime is untouched.** `_load()` reads
  the flag; `ui_catalog()` then lifts every `app_function` OUT of its `abilities`
  map and its group's `members`, into a separate top-level **`app_functions`**
  list. Both ability tables render from `groups`/`abilities`, so they simply stop
  showing these rows — no per-table edit. Everything the *runtime* keys off still
  reads the full `_load()` catalog, so tool-building, `context_strategy_for_agent`,
  `turn_hooks_for_agent`, `background_service_hooks()` and `ability_routers()` all
  resolve exactly as before. An `app_function` keeps its `locked_on` /
  `context_strategy` / `build_tools` / service+router hooks and behaves identically.
- **The App Functions table** lives in **App Settings** (not Agent Settings):
  a `.ac-category-group` shell (`#ac-app-functions-list`) in
  `ui/main-panel/app/settings/app-settings/app-settings.html`, rendered by
  `app-functions.js` from the catalog's `app_functions` list. Each entry is an
  expandable `.ac-row` with an on/off toggle and (for a non-`simple` function) a
  settings body. It **reuses the same endpoints and shared helpers** as the admin
  ability table — on/off via `POST/DELETE /admin/integrations/abilities/{id}`,
  config via `GET/PUT /api/v1/abilities/{id}/config` (+ `/config-schema`), rows via
  `_buildSettingsRowsHtml` — so an app function's toggle + knobs persist to
  `data/config/agent-abilities.json` exactly like an ability's. A drop-in that sets
  `app_function:true` appears here with **no** edit to the panel or the HTML.
- **Session Namer** got a descriptor (`session_titler.json`, `app_function:true`,
  ships ON) so it has a real on/off for the first time. Its turn hook is no longer
  dispatched unconditionally — `turn_hooks_for_agent` gates it on
  `ability_app_enabled("session_titler")` (the app-level resolver: `locked_on` →
  on, else stored admin choice ▸ descriptor default, fail-**on**).
- **Context Control was split** (its three agent tools ≠ its background service).
  The descriptor is now `app_function:true` with **`tools: []`**, so it leaves the
  ability tables; the automatic compaction train (its `CONTEXT_STRATEGY` +
  `locked_on` flags) still resolves and runs. Its three agent-facing tools —
  `compact_context` / `recall_compacted` / `search_this_session` — are now listed
  under **Core ▸ Base** (`base.json` `tools`), i.e. **always-on for every agent**:
  because the descriptor no longer gates them (`tools_map()['context_control']` is
  empty), `ability_for_tool()` returns None and `virtual_ability_for_tool()` labels
  them Base, while `context_control.build_tools()` still *creates* them at runtime
  (the loader's locked-on union runs it regardless of any per-agent row). Its
  compaction knobs (token limit, threshold, self-compaction posture, …) now render
  as **app-level** settings in the App Functions table, read via
  `effective_ability_config` (admin app-level base with no per-agent override).

### Descriptor-only app functions — a toggle for a wired-in core service

An `app_function` does **not** need an ability runtime. A descriptor with **no
`<id>.py`** is normally coerced to a "coming soon" placeholder; an `app_function`
(like a `virtual` row) is **exempt** from that coercion (`_load()`), so a
bare `<id>.json` stays a real, toggleable row. This is how a **wired-in core
singleton** — one hardcoded in `app/main.py`'s startup, not a drop-in — gets an
admin on/off without becoming a plugin: ship a descriptor, then gate the service's
**start site** on `ability_app_enabled(<id>)`.

These live under **`plugins/abilities/System/`** (a pure-app-function group, so
`ui_catalog()` suppresses its otherwise-empty category card from the two ability
tables). Two classes ship there:

- **Behavioural (checked live).** *Task Grouping* — `app/admin/tasks.py` gates the
  LLM tie-breaker on `ability_app_enabled("task_grouping")` and falls back to the
  keyword rule when off (no model calls). *Diagnostics Recorder* — the flight
  recorder in `app/agent/diagnostics.py` ANDs its `enabled` flag with
  `ability_app_enabled("diagnostics_recorder")` at construction (its advanced knobs
  — ring size, retention, persist level — stay JSON-only in `app-settings.json`).
- **Boot-time core singletons (apply after a restart).** `app/main.py` gates each
  start site on `_appfn_on(<id>)` (a fail-**on** wrapper over `ability_app_enabled`):
  **Scheduler** (`scheduler`), **Event Runtime** (`event_runtime`), **Run Watchdog**
  (`watchdog`), **Resume Runs at Startup** (`boot_orphan_resume`), **Hybrid Database
  Sync** (`hybrid_sync`), **Multi-Device Worker** (`device_worker`), **Remote Access
  Tunnel** (`remote_access`). All wire up once at boot, so a toggle change takes
  effect on the next restart — each descriptor's `description` says so. Turning one
  off simply skips its `_leader.register(...)` (or start call); nothing else changes.

These are descriptor-only on purpose: they gate `has_runtime=False`, so
`tools_map()`/`background_service_hooks()`/`ability_routers()` all skip them, and
`_seedable_ability_ids()` excludes every `app_function` (an app-level service is
never a per-agent connection). Adding another such toggle is: drop a `<id>.json`
under `System/` with `app_function:true` + `default_enabled:true`, then gate the
one start site on `ability_app_enabled("<id>")`. No panel, HTML, or core-registry
edit.

## Promoting a feature / cutting a build

- **Promote a feature:** edit one word — its `FEATURE['status']` from
  `experimental`/`beta` → `stable` (or name it in an edition's `include` list in
  `app/features/editions.json`). Add a `FEATURE` header to any still-`unknown`
  plugin so it's never assumed production-ready by accident.
- **Run as an edition:** set `WEBAGENT_EDITION=production` (or `beta`, or a fork
  name). The running app then loads only that edition's features; `full` (default)
  loads everything. Watch the result in **App Config → Features**.
- **Cut a code-absent artifact:** `python -m scripts.build_edition production
  ../webagent-production` copies the repo and physically removes the drop-in
  plugin files the edition excludes (registry subsystems + abilities are
  runtime-gated, so their files stay but don't load).

### Gating chokepoints (where the edition is enforced)
- **Integrations** — `inject_integration_tools` skips excluded modules.
- **Channels / Event sources** — the managers' `get_enabled_plugins()` /
  `enabled()` filter on each plugin's stashed `FEATURE`.
- **Abilities** — the loader filters `enabled_providers` through
  `gating.ability_enabled` (one chokepoint gates every ability's tools).
All are **fail-open** (unknown → kept) and **no-op for `full`**.

---

## Key files (Phase 1)

- `app/features/descriptor.py` — the `FEATURE` shape + normalization + reading a
  header off a module.
- `app/features/editions.py` — edition definitions + active-edition resolution.
- `app/features/editions.json` — committed edition manifest.
- `app/features/catalog.py` — scans the plugin folders, builds the catalog.
- `app/api/features.py` — `GET /api/v1/features` (admin-only).
- `ui/main-panel/app/settings/features/features.html` (container) + `app-config/nav.js` (`_renderFeaturesCatalog`) — the
  Features report panel.
