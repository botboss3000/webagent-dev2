# WebAgent v3 UI journeys

These routes belong to the independent v3 application and must use its development origin. Do not use the v2 route registry to navigate v3.

- First-run setup: `/setup.html`. When no v3 admin exists, verify the v2-style Welcome to WebAgent card, username and two password fields. A completed setup redirects to `/app`; revisiting `/setup.html` redirects to `/app` without exposing setup again. The v2 bootstrap-code field is omitted because v3 has independent storage.
- Agents catalog: `/app?tab=agents&agent=shared_default&agentView=config`. Verify the V2-style Config directory lists exactly Primary Models and Abilities, with matching tabs above it. Both the directory and tabs use `v3/ui/main-panel/agents/pages/catalog.json`. Open each directory link, verify its selected tab and visible panel, and reload to confirm the `agentView` route restores that page. Check the directory in both themes.
- Agents and Primary Models: `/app?tab=agents&agent=<authorized-agent-id>&agentView=model`. Models consumes the selected Agents route context; it has no local agent selector or Create agent controls. Verify compact saved rows, add/advanced controls, searchable provider dropdown, inline display-name editor, capability-gated role menu, roster ordering and per-role ordered backups (at most seven backups plus primary). Reload after saves; unlink must retain the model connection. Connection credentials and authorization remain owner-only. Standard and System runtime workflows are supported; other role workflows and unsupported reference features remain pending.
- Agents and Abilities: /app?tab=agents&agent=shared_default&agentView=abilities. Verify the grouped compact ability rows, expandable skill/tool bodies, on/off switches, and Off/Discoverable/Visible menus. Codebase Admin is callable; the other Administrator tool rows show Pending port or Unavailable in v3. Save one ability, skill, or tool mode and reload to confirm persistence.
- App catalog: `/app?tab=app&appView=config`. The App directory reuses the Agents directory renderer and shared `v3/ui/shared/page-tabs.js` interaction. Verify starring File Manager adds its tab, unstarring removes it, and favorites/order survive reload in this browser. Check keyboard Alt+Arrow reordering and 500ms hold-to-drag. Open `/app?tab=app&appView=files` and verify the explicit pending-port notice: filesystem browsing/editing, Git, and deployment APIs are not implemented or connected to V2. Check both themes. File Manager is a catalog entry, not a completed functional port.
- Account: `/app?tab=account`. Verify the header avatar opens Manage account, username and display name are editable, and password, sign-in duration, and signed-in browser controls appear. Reload preserves the account view. Saving the current duration shows a success status; changing a password signs out existing sessions.
- Chat: `/app?tab=agents&agent=shared_default`. Verify the v2 split shell and chat composer. Select the intended agent before creating a conversation; new conversation requests must carry that agent ID. Connect its own fresh ChatGPT subscription, send a bounded message, and reload the `session_id` route. Verify the conversation remains bound to its original agent even when the configuration-page agent selection changes. The default-agent connection is never inherited by a new agent.
- Existing conversation: `/app?session_id=<known-v3-session-id>`. Derive the identifier from the v3 API or conversation selector. Navigate directly; verify the selected conversation, its history, and the same query parameter after reload.
- Provider and policies: `/app?tab=agents&agent=shared_default`. The Model page shows ChatGPT subscription status and verification code. Expand **Tool policies** to inspect six policy selectors. Initiating provider authorization must show a verification link and user code; completing it requires the user's account authorization.
- Approval: open the exact conversation route for a run waiting for approval. Verify the tool name and complete arguments, then approve or deny the known development fixture. Reload while waiting to verify the gate persists.
- Recovery: open the exact conversation route for a deliberately interrupted fixture. Verify the uncertain outcome warning and Accept known result, Authorize retry, and Abandon run controls. Never retry an unknown external effect without reconciling it.
- Shutoff: activate shutoff on a development fixture. Verify send is disabled and controls remain accessible. After backend restart, verify shutoff remains active. Clearing it must leave stopped runs waiting for explicit resume.
- Themes and mobile: exercise chat and expanded control panels in both themes and a narrow viewport. Verify the v2-style mobile navigation opens Agents and Manage account, the Chat control shows the full chat panel, and the main panel fills the viewport when selected. From either full mobile panel, pull down while its scroller is at the top (or start in the top 50 pixels) and verify the fixed circular refresh indicator appears; release after half the screen height to reload, or after 85% to clear browser data and sign out before a cache-busting reload. Verify the engaged blue or red ring rotates until navigation begins; reduced-motion preferences keep it still. Verify the compact chat header places New conversation beside the conversation selector and the run cards above the composer. Verify text, borders, composer, approval arguments, and recovery actions remain legible and reachable.

Capture the final URL and visible checkpoint. Browser mutations must remain scoped to v3 development fixtures; do not access production or import v2 session data.

## Selection acceptance status

The selection table module was imported and exercised in a detached DOM against a mocked API (primary assignment, separate backup records, reorder, unlink without delete, agent switch). This is not live acceptance. The combined Rust snapshot must pass its build/tests and be explicitly authorized for development deployment before the authenticated journey above. Capture both themes and mobile, verify reload persistence and failure feedback, and share a final screenshot. Do not authorize a provider account on behalf of the user or copy v2 credentials.

- DeepSeek: `/app?tab=agents&agent=shared_default&agentView=model`. Open Add model connection, choose DeepSeek, verify `deepseek-chat` and `deepseek-reasoner` suggestions, create a disposable connection, and expand it. Verify DeepSeek provider metadata and masked API-key control, with no ChatGPT authorization button. Check invalid-key feedback and persisted connection after reload, both themes and narrow layout. Key-save confirmation means encrypted storage only; a paid live-provider request requires a user-supplied key and separate bounded workload.

## Models reference UI adaptation — partial acceptance, 2026-10-03

Source changes: `ui/main-panel/agents/pages/model-table.js`,
`ui/shared/model-row-ordering.js` and `ui/app.css` (legacy form styles scoped
away from the reference-style mount). No Rust changes or service recreation were
performed for this adaptation. Unrelated checkout edits were preserved.

Authenticated server-browser checks at `https://dev.webagent.live` used the
existing disposable agent `agent-523bff26b77d25c5c61b2bb2f47f2498`.
Model creation, Standard assignment, inline rename, keyboard roster ordering,
backup linking and unlinking were exercised; rename, order, role and linked
backup survived reload. Unlink retained the backup model and connection.
Empty DeepSeek key submission visibly returned
“Invalid API key: use 8–512 printable non-space characters”. No credentials
were supplied and no provider workload was executed.
The final dropdown edit was checked after navigation: open and Escape-close
both succeeded on an authenticated page at `agentView=model&verify=lifecycle-final`.

Synthetic pointer ordering also survived reload at `verify=pointer-persist`;
this does not establish physical touch-device behavior.

This is not full reference-file parity or complete acceptance.
Detached browser checks additionally verified view-only role disabling, hidden
add controls, absent owner-only credentials, mount clearing on destroy, and a
delayed reload resolving after destroy without repopulating the mount. These
mocked checks are not live cross-account authorization acceptance.

Mobile layout, both-theme visual comparison,
live read-only/ownership regression journeys remain to be verified.
A read-only authenticated session API check confirmed the open conversation
`f4eaf62b-a242-440c-8bb0-f946101a2db5` remained bound to `shared_default`
while Models configured the disposable agent. This is a pinning regression
observation, not a provider execution test. Reference files are retained: discovery, usage/pricing,
account defaults, credential library, extra providers/endpoints and prompt
injections remain unported. Do not remove partially adapted reference files.

Final CSS navigation at `verify=css-final` displayed expanded model details
with no horizontal overflow at the desktop viewport. Post-change light-theme
roster assertion at `verify=closeout-2` also had no horizontal overflow.
Screenshots were captured; full visual reference comparison and mobile
acceptance remain open.


## Actual reference Models renderer — 2026-10-03

`v3/ui/main-panel/agents/pages/model-table.js` now mounts the actual reference
`ui/shared/js/model-table.js` and standalone model analytics component from
`v3/v2_ref`, copied into `v3/ui/shared/models/`. It does not use the former
reconstructed v3 renderer. Reference app3 styles and required helper functions
are included locally. The v3-only adapter translates scoped catalog records and
role appointments; no v2 API fallback is allowed. Unknown/shared records stay
in catalog snapshots, writes retain revision fences, row removal only unlinks
from this agent, and existing per-role backup candidates are retained.

Pending UI remains present with `NEED BACKEND CONTRACT`: entitled catalog,
discovery/provider presets, custom transports/base URLs, usage/analytics/reset,
metadata/reference pricing, account defaults, named credential libraries,
role prompt injections, and reference alias groups. Discovery is not simulated;
supported providers allow manually entered upstream model IDs. V3's existing
per-role backups are not equivalent to reference alias groups: their state is
preserved, but alias-group editing stays blocked. Subscription reset/library
operations also remain pending. Device authorization requires the explicit
issue-code action; provider selection alone must not initiate it. Credential
writes and authorization are connection-owner-only. Reference files remain in
v2_ref because these backend-dependent features have not completed migration.

Observed authenticated journey on https://dev.webagent.live:
`/app?tab=agents&agent=agent-523bff26b77d25c5c61b2bb2f47f2498&agentView=model`
(the existing Models UI port verification fixture). After the final source edit,
reloaded, opened the original roles menu, assigned System, reloaded again and
asserted Standard and System were displayed on the first saved row. The original
inline rename editor had saved `Actual reference port verified`, also retained
after reload. Expanded details showed adjacent pending markers for pricing,
account defaults and aliases; directives were individually labelled. Resource
entries showed zero /api/v1 or /admin/settings requests. A final screenshot was
captured at 18:14:46 UTC. Default-agent credentials and provider workloads were
not changed or executed.

Mock adapter checks (not live authorization tests): existing backup candidates
survived a roster save; a freshly loaded read-only adapter rejected the write
with zero PUT calls. Browser module loading and git diff --check passed. Node
was absent, so no Node CLI syntax/test command ran. Provider authorization/key
rotation, physical drag gestures, mobile layout and both-theme regression checks
are not established by this journey and remain follow-up verification. No Rust,
schema, service restart or image deployment was performed by this UI port.

## Session dropdown batch recycle — 2026-10-03

Route: `/app?session_id=<disposable-v3-session-id>` on the independent development
origin. Open the conversation selector, enable checkbox mode, and select empty
fixtures using a row click, row Enter/Space, or the checkbox button. Selection
must leave the active conversation and read cursors unchanged. The first
Recycle selected click arms confirmation for three seconds; the second starts
the batch. Busy controls prevent duplicate submission. Each selected conversation
uses the existing cookie/CSRF, ownership and revision-fenced Rust DELETE contract.
A batch is not atomic: successful rows leave the selection; failed rows stay
selected with explicit feedback and their unchanged request receipts for retry.
Closing the dropdown clears checkbox mode when no batch is running. Teardown
must stop starting further writes and suppress late panel callbacks; it cannot
undo a write already accepted by Rust.

Live server-browser acceptance selected two newly created empty fixtures with
row click and Enter, preserved the active conversation, verified the first
confirmation click made no lifecycle change, and then observed both fixtures
recycled with read cursors zero and the notice “Recycled 2 of 2 selected
sessions.” Isolated real-adapter/mock-client checks verified partial-failure
selection retention, no duplicate retry of successful rows, and reuse of the
failed request receipt. These isolated checks are not live authorization tests.
Full dropdown port acceptance remains open for hidden-session and custom-order
contracts, family grouping, both-theme/narrow-layout regression, and active
container configuration provenance. Reference functions remain pending in
`v3/v2_ref` because their containing UI port is incomplete.
