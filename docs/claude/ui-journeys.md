# UI journey registry

Use these relative routes to begin a focused WebAgent UI journey in a new controlled tab.
For server Chromium, call `login_as_current_user()` with no `app_url`, take the local
origin from its returned `url`, and append the route. That origin is resolved from
`WEBAGENT_PORT` / the Overview `PORT` setting. Do not hard-code a domain or port.
All entries require the normal authenticated development fixture unless noted. Do not
add transient entity IDs or destructive actions.

## Owner bundles

- `agents` — `ui/main-panel/agents/{agents.html,agents.css,js/agents.js}`; route `/app?tab=agents`.
- Agents chat URLs retain independent tab, agent, agentView, and session query parameters; the concrete parameterized form is the `agents.page` entry under Existing parameterized subviews.
- `app` — `ui/main-panel/app/{instances.html,instances.css,instances.js}`; route `/app?tab=app` (platform admin).
- `browser` — `ui/main-panel/browser/{browser.html,browser.css,browser.js}`; route `/app?tab=browser`.
- `genui` — `ui/main-panel/genui/{genui.html,genui.css,js/genui.js}`; route `/app?tab=genui`.
- `wiki` — `ui/main-panel/wiki/{wiki.html,wiki.css,js/wiki.js}`; route `/app?tab=wiki`.
- `admin-tools` — `ui/main-panel/admin-tools/{admin-tools.html,files.css}` and `ui/shared/js/files.js`; route `/app?tab=admin-tools` (platform admin).

For every owner, verify the selected page is visible, its named primary control is
reachable, and a reload keeps the same route. Capture the final URL and one visible
assertion before reading correlated diagnostics.

## App subviews

All entries below are platform-admin routes with the same owner bundle. Use the
relative form `/app?tab=app&view=<id>`; Diagnostics appends it to the dynamic local
origin returned by `login_as_current_user()`.

- `app.settings` — Settings landing renders.
- `app.admin-panel-pages` — Admin Panel Pages renders.
- `app.admin-tools` — Admin Tools runtime view renders.
- `app.agent-tools` — Agent tools & abilities renders.
- `app.app-access` — App Access renders.
- `app.app-functions` — App Functions renders.
- `app.automation-engine` — Automation Engine renders.
- `app.browser-session-management` — Browser Session Management renders.
- `app.dashboard` — Dashboard runtime view renders.
- `app.design` — Design settings render.
- `app.entitlements` — Entitlements render.
- `app.file-manager` — File Manager runtime view renders.
- `app.main-panel-pages` — Main Panel Pages render.
- `app.models` — Models render.
- `app.overview` — Overview runtime view renders.
- `app.storage-routing` — Storage Routing renders.
- `app.templates-prompts` — Templates & Prompts render.
- `app.usage` — Usage Data runtime view renders.
- `app.user-policies` — User Policies render.

## Existing parameterized subviews

- `agents.page` — `/app?tab=agents&agent=<agent id>&agentView=<page id>`; the agent id is the
  `data-agent-id` on the agent row (e.g. `shared_default`) and the page id is one of the folders
  under `ui/main-panel/agents/pages/`: `identity`, `manager`, `model`, `modes`, `runtime`,
  `voice`, `deploy`, `abilities`, `prompts`, `members`, `automations`, `monetization`,
  `agent-loop`, `sessions`, `config`. `agentView=model` is normalised to no `agentView`
  parameter. Both parameters come from the bundle itself — `ui/main-panel/agents/js/view.js`
  calls `replaceRouteParams({ agent: agent.id, agentView: pageId })`. Verify the topic card
  renders, expand any collapsed `<details>` cards before asserting their contents, and reload
  to confirm the route sticks.
- `wiki.article` — `/app?tab=wiki&article=<known-non-destructive-slug>`; use a known fixture article and verify its title.
- `admin-tools.view` — `/app?tab=admin-tools&view=<catalog-view-id>`; use a non-destructive catalog view, verify the strip selection and panel, then reload.

## Diagnostics workflow

For a WebAgent UI diagnosis, load Browser Control if it is not already available,
call `login_as_current_user()` without `app_url`, then call
`browser_backend(mode="server")`. Append this registry's relative route to the
returned local origin and navigate directly there. The headed server Chromium is the
ready journey target: do not search for executables or probe filesystem paths. A
backend-status call is the only availability preflight. Verify the final URL and
visible checkpoint, perform only the entry’s minimal reversible steps, capture
browser evidence, read correlated Diagnostics records, and close the leased browser tab.

## Diagnostics direct loading

Use `dev_browser_action(action="open", path=<one of the routes above>)`; it authenticates server Chromium directly on that route. For a current-user chat session, use `dev_browser_action(action="open", chat_session_id=<session id>)` to load `/app?session=…` without intermediary navigation.
