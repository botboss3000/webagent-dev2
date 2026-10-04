# WebAgent v3 development

The abilities work is in progress. Tenant-scoped administration, account-tier
ability grants and agent capability levels are deployed. Manifest presence
is not execution support. See [the port ledger](docs/ability-port-ledger.md);
readiness is promoted only after authenticated API acceptance. The eight
Administrator coding abilities are ready; Base remains partial, while other
groups retain their individual readiness decisions. The policy migration
preserves existing grants beneath prior ceilings and does not automatically
grant newly installed plugins.

The current Base source adds model-callable tools for revisioned session mode,
private cross-session literal search, manual compaction and bounded recall,
agent-administrator prompt and selectable skill edits, durable task checkpoints,
and declarative presentation components. The 2026-10-03 pinned Rust suite passed
341 tests (2 ignored), and both V3 development containers are healthy.
Authenticated API acceptance confirmed Base tool checkpoints, one persisted
assistant reply per run, and caller-owned cross-session search through the
model tool. Other new tools have focused Rust coverage but were not each
exercised in a live model run. A read-only to write mode request requires
approval and a fresh revision.


Independent Rust agent runtime, subscription provider adapter, and isolated Python tool worker.
Source lives here within the primary development checkout; v2 remains independently available.

New to this codebase? Start with the
[plain-language code tour](docs/code-tour.md), then use the sections below
for deployment and API details.

## Run

On the development server:

```sh
sh /home/ar/webagent-dev2/v3/deploy.sh build
sh /home/ar/webagent-dev2/v3/deploy.sh build-loop
sh /home/ar/webagent-dev2/v3/gate-deploy.sh build
sh /home/ar/webagent-dev2/v3/deploy.sh start
sh /home/ar/webagent-dev2/v3/gate-deploy.sh start
sh /home/ar/webagent-dev2/v3/deploy.sh start-loop
curl -fsS http://100.112.23.15:8081/health/ready
```

The existing named tunnels serve https://webagent.live/app from dev2 on host port 8080
and https://dev.webagent.live/app from v3 on host port 8081. The previous
production app container is stopped and kept as a rollback snapshot.

On first start, open `/setup.html` to choose the v3 admin username and password.
The retired test bootstrap credential is removed on the first launch of this setup flow.
Open `/app?tab=agents&agent=shared_default` to use the v2 Agents layout and configure the default agent's Model page. Connect a fresh ChatGPT subscription there; v2 tokens are never imported.
The default agent uses `gpt-6-luna` until you save another model. Choose a model supported by your account; a catalog name alone does not establish entitlement.
Agent model settings store an explicit provider ID. The supported IDs are
`openai_subscription` and `deepseek`; unsupported IDs are rejected. DeepSeek requires
an explicit model connection and its own encrypted API key, saved on Primary Models.
Choose `deepseek-chat` or `deepseek-reasoner` (or enter a supported upstream model ID).
The adapter calls the fixed `https://api.deepseek.com/chat/completions` endpoint,
streams text, and translates tool calls/results. API usage is billed by DeepSeek;
saving a key confirms storage, not account entitlement. Keys are never returned
to the browser. Each accepted run pins its transport and credential reference. Each accepted run pins its
provider and model so changing the agent setting affects only later runs.

Development UI files in `v3/ui` and the selected shared CSS, Agents partial,
fonts, and favicon are mounted read-only into the v3 container. HTML, CSS, and
JavaScript edits to those files appear after a browser refresh without rebuilding
the image or restarting the container. The shared files also serve v2, so keep
v3-specific UI changes in `v3/ui`; changing the container mount list still
requires recreation. The chat baseline lives in `v3/ui/chat/chat.html` and
`v3/ui/chat/chat.css`; `v3/ui/app.js` supplies authentication and selected-agent context to
`v3/ui/chat/core/panel.js`, which mounts the partial and owns chat rendering,
controls, and teardown. `chat/core/client.js` owns the existing v3 chat requests
and sequence-based SSE connection; it uses the shell request helper for cookie
authentication and CSRF. Remounting the panel disconnects its previous stream
and resize observer before binding the new controls.
Chat control configuration, pill layout, footer interaction, and sign-in adapter
modules live in `v3/ui/chat/controls/`.
The session selector now uses the active v2 trigger markup and adapted v2
portal/positioning/open-close lifecycle under `chat/controls/session-dropdown/`.
Its v3 data adapter supports private title/content search with continuation,
opening and refreshing sessions, persisted rename/pin, reversible recycle/restore,
and visible-message read acknowledgments. Checkbox batch recycle uses the same
revision-fenced Rust lifecycle writes: two-click confirmation, individually
processed sessions, explicit partial failures, and retained failed selections
with retry receipts. It is not an atomic batch. Unsupported management actions
are disabled, not sent to v2. This remains a partial port: hidden sessions,
custom pinned ordering, rich status/family grouping, and the remaining header
controls are not yet equivalent to v2.
V3 startup wiring selects its independent `v3/data/config/chat.json` through
an authenticated `GET /api/v3/auth/ui-config` route. `deploy.sh start` seeds the
file once from the running v2 app’s `data/config/chat_ui.json`, only when the
v3 file is absent; later starts preserve v3 edits. The existing `/state` mount
supplies `/state/config/chat.json` rather than a separate shared v2-file mount.
These source changes require container recreation to take effect. Live
configuration separation is not verified yet. Its
`chat_common` values merge with `chat_desktop` or `chat_mobile`; arrays replace
the common value. V3 currently applies supported header rows (agent name,
conversation selector, previous/next, new, minimize), active/idle composer rows
and sizes, placeholder, transcript width, and tail clearance. The composer uses the canonical v2 pill/footer markup and shared CSS.
`v3/ui/chat/controls/chat-pill-config.js` is a synchronized copy of
`ui/shared/js/chat-pill-config.js` with only its sign-in hook adapted;
`controls/footer-handle.js` reuses the v2 footer drag interaction with v3-scoped
conversation persistence. Keep these copies synchronized when the v2 reference
changes. Attachment, voice, execution-mode, model-picker, abilities, and
notification actions remain visibly unavailable until their v3 contracts are
ported; unavailable usage statistics are omitted, not fabricated. Other v2 controls
remain unsupported until their v3 features are implemented. Run and approval
cards remain visible above the composer regardless of footer layout, so
approvals stay accessible. Rust, Python worker, and dependency changes still
require an image rebuild.

V3 main panel pages live in v3/ui/main-panel/<id> with a page.json descriptor.
The Rust GET /api/v3/pages/catalog route discovers these folders and returns
pages by order and label. The shell builds desktop tabs, mobile navigation,
and content mounts from the catalog. A descriptor provides id, label, optional
order, and local html, css, and entry assets; an entry module exports start(mount).
Agents currently adapts the shared v2 partial in the v3 shell. Manage Account
is a catalog page and the header avatar opens it. Adding a page takes effect
after a browser refresh; backend catalog code changes require a v3 rebuild.

## Session metadata foundation

`src/storage/session.rs` defines the typed metadata stored inside each encrypted,
user-owned `session:<id>` document. It follows v2's title, pinned flag, lifecycle
status and timestamps, without copying v2's plaintext schema or importing v2 data.
V3 adds `metadata_revision`, `metadata_schema_version`, `last_active_at`,
`read_through_seq`, and nullable `recycled_at`; status is `active` or `recycled`.
The read marker is a durable sequence cursor, not v2's wall-clock `read_at`.

On first use of each user database, an atomic, idempotent migration fills only
missing fields, using the latest public chat timestamp for last activity, with
creation time as the fallback. A legacy document without a creation timestamp
uses zero to mean unknown; it is not made newly active by the migration. New
records get a creation timestamp when absent. Partial session-document writes
preserve omitted metadata fields. Agent binding, checkpoint state, public chat,
run state and sequence numbers are retained. Migration completion is encrypted
in that same user database, and an invalid record rolls back the whole backfill.

Phases one and two are verified in disposable Rust databases and the authenticated
Axum router. `GET /api/v3/sessions` returns safe active-session summaries ordered
by pinned first, last-active descending, and session ID ascending as a stable
tie-breaker, plus `server_time` (Unix seconds). Snapshots expose the same metadata;
checkpoint input and runtime state are not included in session summaries.

`PATCH /api/v3/sessions/{id}` accepts `client_request_id` (1-128 bytes),
`expected_revision` (the current `metadata_revision`), and `title` and/or `pinned`.
For example: `{"client_request_id":"rename-1","expected_revision":0,"title":"Work","pinned":true}`.
Titles are trimmed, nonempty, at most 200 UTF-8 bytes, and contain no control
characters. Unknown fields and malformed types are rejected. The route retains
browser authentication, CSRF, user ownership and live agent-access checks; it
is not added to the machine messaging API. Missing sessions return 404; recycled
sessions, stale revisions and request-ID reuse with different input return 409.
A success returns `{"session":<safe summary>}`. Session writes, encrypted retry
receipts and `session_updated` events commit atomically after reserving the SQLite
writer. Identical retries return the original result, including its original
revision. No-op patches have receipts but do not advance revisions or add events.
Stale runner snapshots preserve newer metadata while still updating checkpoints.
Renaming and pinning never advance last activity.

Public chat inserts now advance `last_active_at` in the same transaction as
accepted user messages, working assistant publications, and final assistant
replies. Duplicate publication retries do not advance activity. Non-public run
updates, opening, renaming, and pinning do not count as activity. Activity uses
server epoch seconds and never decreases; it does not change metadata revisions.

`PUT /api/v3/sessions/{id}/read` accepts `{"through_seq":<u64>}` with browser
authentication, CSRF, user ownership and live agent-access checks. Zero is a
no-op; any nonzero cursor must identify an existing public chat message in that
session (not a global sequence gap or another session's message). Invalid
cursors return 400, missing/inaccessible sessions 404, and recycled sessions 409.
The cursor advances by maximum, making identical retries and older tabs harmless
without request IDs or metadata revision changes. Cursor writes and encrypted
`session_read` events commit atomically. Reading a snapshot does not acknowledge
anything; the browser must explicitly acknowledge rendered visible messages in
the later dropdown integration phase. A retry returns current state, not a
historical receipt.

Lists, snapshots and metadata/read mutations expose `unread_count` and
`is_unread`, counting assistant chat rows beyond the durable cursor, not global
sequence gaps or user messages. Lists project counts and metadata in one database
snapshot. Stale checkpoint writes preserve activity and read cursors even when
metadata revisions are equal. The elapsed "last active" counter remains a later
browser feature derived from these timestamps and server time.

Reversible lifecycle (phase 4, source-tested; not deployed): browser
`DELETE /api/v3/sessions/{id}` recycles and `POST /api/v3/sessions/{id}/restore`
restores. Both require `{"client_request_id":"unique-id","expected_revision":<u64>}`
and authentication, CSRF, user ownership and live agent access. Lifecycle changes,
encrypted receipts and events commit atomically. Exact retries return the original
receipt (even after a later restore); reused IDs with different input or revisions
that changed return 409. Already-in-target-state requests are no-ops with receipts.
Recycling blocks any nonterminal indexed run, including queued work, stopped runs,
approval/recovery waits and unknown statuses. Only completed/cancelled/failed runs
are terminal. No implicit cancellation or permanent purge occurs. History,
checkpoint, agent binding, pinning, activity and read cursor are retained; restore
clears `recycled_at` without counting as activity. Snapshots remain readable.
`GET /api/v3/sessions?status=recycled` lists recycled summaries with the same stable
ordering; the default/`status=active` lists active ones. Invalid status returns 400.
Recycled sessions reject new messages and nonterminal run writes/claims with 409;
storage checks the lifecycle in the write transaction, fencing independent-store
admission races. Existing accepted-message receipts remain retryable.

Private session search (phase 5, source-tested; not deployed): browser-only
`GET /api/v3/sessions/search?q=<text>&status=active&limit=20` searches only the
signed-in user's encrypted session titles and chat-visible user/assistant string
content. It never searches reasoning, checkpoints, tool records or another user's
store. Live agent access filters results; no machine-messaging search route is
exposed. Matching is case-insensitive and literal, not regex, fuzzy or stemming.
`status` is active (default) or recycled; `limit` is 1–50 (default 20). Queries
are trimmed and normalized, capped at 128 characters and 256 normalized bytes.

Results identify title/message hits, session/agent IDs, title, a plain-text snippet
of at most 240 characters and, for messages, sequence and role. **Render titles and
snippets with textContent, never innerHTML or Markdown**; snippets intentionally
remain plain strings even when source text contains markup. Search does not mark
messages read or update activity.

Each request scans at most 128 candidate records and 8 MiB of ciphertext, with a
2 MiB per-record ceiling (including session documents containing checkpoints).
Oversized records are skipped explicitly: `skipped_oversized`, cumulative
`skipped_oversized_total`, and `incomplete_due_to_size` disclose missing coverage.
`scan_complete` means traversal exhausted, not that oversized content was searched.
An empty results page can still have a continuation after scan/access filtering.
Follow `continuation` with the same query/status until absent. The encrypted,
authenticated token is user/query/status-bound, expires after 15 minutes, survives
restart and rejects tampering. Invalid bounds/tokens return 400.

Traversal is titles by session ID, then messages by sequence, not relevance order.
Initial title-key/message-sequence upper bounds exclude later appends above those
bounds, but each page reads current content/status: this is not an immutable
cross-request snapshot. Restart a search after edits for a fresh view. No plaintext
search index or result receipts are persisted. Dropdown integration remains phase 6.
The Rust changes require a rebuild and authorized development deployment before
live databases receive the migration; no deployment is performed by the tests.

## Direct messaging API

The narrowly scoped `/api/v3/admin/messaging` API accepts an administrator machine
credential without a browser cookie or CSRF token. All other admin routes retain
browser authentication. This credential can read any v3 conversation and submit
messages as the administrator; keep it private, use HTTPS, and do not embed it in
frontend code. Run policy, approvals, queue limits, provider configuration, and
global shutoff still apply. This API does not approve tools or change settings.

Set `V3_MESSAGE_API_TOKEN` in the deployment environment before `deploy.sh start`.
Use a randomly generated 32-256-character printable ASCII token (for example,
`openssl rand -hex 32`). The server retains only its SHA-256 digest. An unset or
empty token disables machine access; an invalid configured token rejects startup.
The tool worker and promotion broker do not inherit it. Rotate/remove the token
by changing the container environment and recreating v3; rebuilding alone does
not change the running container environment. Never commit the token.

Every request requires `Authorization: Bearer <token>`. Responses are JSON:

- `POST /api/v3/admin/messaging/sessions` with
  `{"client_request_id":"unique-request-id","title":"API chat"}` returns `{"id":"session-id","title":"API chat"}`.
- `POST /api/v3/admin/messaging/sessions/{id}/messages` with
  `{"client_message_id":"unique-message-id","text":"Hello"}` returns HTTP 202
  and `{"run_id":"run-id"}`. Acceptance is asynchronous, not a completed reply.
- `GET /api/v3/admin/messaging/sessions/{id}` returns `session`, `messages`,
  `runs`, and `last_seq`. Poll this endpoint to read assistant replies and the
  matching run's status/error. An approval-required run still needs the normal
  administrator approval UI; polling never grants approval.

Keep the same client ID and body when retrying an uncertain POST. Reusing an ID
with changed input returns 409; sending another message while a run is active
also returns 409. Missing/wrong/disabled credentials return 401. Credentials
never belong in URL parameters. Backend changes require an image rebuild.

## Boundaries

The Rust application owns policies, approvals, execution state, recovery and shutoff. The first v3 UI port uses v2's Agents partial and CSS at image-build time; the independent v3 API and storage serve its chat and model settings. Other v2 tabs and agent subpages are not wired yet.
The model first receives `list_abilities` and `load_ability`. Loading a catalog ability
makes its currently permitted worker schemas available for that run; execution checks the
current policy again, and writes still require approval by default. The authenticated
`GET /api/v3/abilities` endpoint exposes the visible catalog and effective tool policies.
The Administrator catalog contains ten abilities. This coding port implements Codebase Admin,
Code Index, Git Control, Terminal Control, Diagnostics, Remote Tunnel, V3 Build and
V3 Workspace. Computer Control and Program Screenshot are explicitly excluded. New
adapters passed authenticated API acceptance; tier and capability-level
grants do not expand merely because a plugin is installed. See
[Administrator coding](docs/administrator-coding.md) for configuration and contracts.
The loader rejects duplicate declarations. Unknown v2 tools remain catalog-only
and cannot be discovered by the agent, loaded, or executed. The development
container reads v3/plugins/abilities from its live v3 mount. If an ability
directory or manifest disappears, the ability and its tools immediately leave
agent discovery and execution, even for a session that loaded it earlier. Saved
agent visibility settings remain in v3's agent database; restoring the files
makes them effective again. A changed manifest or skill requires a v3 restart
to reload its contents.

`state_inspect` is a Rust-native, read-only tool for bounded current-session
metadata. It exposes a resource catalog, session title and chat cursor/count,
chat pages with 512-character content previews, and recent run status summaries
without pending tool arguments or provider payloads. Chat uses an ascending
`after` sequence cursor; chat and run limits are 1–50. The authenticated
`GET /api/v3/sessions/{id}/state-inspect` route uses the same typed resources
and enforces the current tool policy. Neither interface accepts SQL or a database path.

The `prompt_slot_read` and approval-gated `prompt_slot_write` tools manage
ordered instructions for the single `shared_default` agent. Slots live encrypted
in the agent database, use optimistic revisions to reject stale writes, and
are assembled into model instructions for newly accepted runs. Existing runs
retain their pinned instructions. The authenticated
`GET /api/v3/agents/shared_default/slots` and
`GET/PUT/DELETE /api/v3/agents/shared_default/slots/{name}` routes allow
administrator management; mutations require CSRF. V3 has no per-user slot
overrides yet, and reserves v2's special `__skills__` slot.

The approval-gated `workspace_seed` tool copies the audited v3 source snapshot into
`webagent-v3/` within one session workspace and creates a clean Git baseline. The runtime
image includes Rust 1.90 and build tools for local verification. `source_review` gives a
bounded read-only view of tracked edits and new files without changing the Git index.
The authenticated `GET /api/v3/sessions/{id}/source-review` route exposes the same review to
the administrator when policy allows it. A
`truncated` result is incomplete and must not be treated as a full patch. Edits and commits
in that workspace do not promote themselves to the server checkout.

The approval-gated `source_patch` tool applies a text-only unified diff to up to 64 files in
the seeded source after matching its `expected_review_sha256` to a complete review bound to the
Git baseline. It
rejects path escapes, Git internals, symlinks, binary patches, and unsupported modes. A
failed precheck leaves the workspace unchanged; an interrupted apply enters outcome review.

After inspecting a complete `source_review`, the agent can call
`promotion_propose` with the exact review digest and a verification summary. The
proposal freezes the patch and emits a session event; it cannot change live
source. The signed-in administrator can list proposals with
`GET /api/v3/sessions/{id}/promotions` and inspect the frozen diff with
`GET /api/v3/sessions/{id}/promotions/{request_id}/review`. The ordinary
`GET /api/v3/sessions/{id}/promotions/{request_id}` returns receipt metadata
without the patch.

To approve that proposal, the administrator sends
`POST /api/v3/sessions/{id}/promotions` with the proposal's UUID
`client_request_id`, its `expected_review_sha256`, and the usual CSRF header.
The server rechecks the current workspace review against the frozen proposal
before dispatching it to the separate broker. To dismiss a proposal, the
administrator sends `POST /api/v3/sessions/{id}/promotions/{request_id}/reject`
with the CSRF header; rejection is idempotent and cannot change live source.
An administrator may also use the promotion POST endpoint directly after a
complete review. Retrying the same request ID
returns its stored result; a dispatch interrupted by a restart becomes
`uncertain` and is never replayed. Inspect an uncertain promotion against the
live checkout before making a new request. An applied source change needs a
deliberate image rebuild and container recreation before the running application
uses it.

The container mounts only the v3 source directory at `/live-v3` for promotion.
The Rust app and tool worker cannot write that mount. The broker runs as the
checkout owner, accepts only the app's private socket, obeys global shutoff, and
limits changes to v3 source, plugins, tests, UI, docs, configuration and selected v3 root files. Runtime data and workspaces remain excluded.
UI files and runtime data are excluded from promotion.

The Python worker owns actual filesystem operations and approved shell/Python processes.
Its `file_edit` operation replaces one exact span in an existing workspace file, supports
a SHA-256 precondition, and requires approval through the Rust policy gate.
`file_write` publishes a fully written and synced same-directory temporary file;
if durability confirmation fails after publication, the result is `uncertain`.
Its separate UID cannot read /state or application process environments.
File tools reject symlink/path escapes. Approved scripts can access the entire v3 workspace collection
and external network; they are not a per-session or hostile-code sandbox. The tool worker cannot write the live v3 source mount. Only the v2 chat UI config is mounted read-only;
no Docker socket, other v2 data, or production storage is mounted.

Runtime records are encrypted using a v3-only local key under `v3/data`. The split
databases are `db/app.db` for account, provider, policy and global control,
`user_data/admin/admin.db` for sessions, runs, private interactions and public chat,
and `agent_data/shared_default/shared_default.db` for the default agent model, instruction slots, and ability settings.
The agent database uses v2-shaped agents, agent_connections, and agent_prompts
tables with a smaller v3 column set and encrypted metadata, config, and prompt content.
On first open, v3 copies its own legacy agent docs and prompt_slots values into
those tables in one transaction; the old rows remain for rollback but are no longer
used for active reads or writes.
The first split-storage start migrates encrypted legacy records, events, run
indexes, session context, and chat history from `runtime.sqlite`. Migration is
idempotent and marks completion only after every record is verified. The old
database remains on disk for rollback. Preserve the whole data directory,
including its encryption key, for recovery. Do not inspect live session databases
directly; use authenticated APIs.

The app database also has explicit `user_accounts` and `user_profiles` tables.
On startup, v3 imports its existing admin login into those tables without changing
the password hash or current sign-in generation. First-run setup creates the
account and profile together. Authentication reads the account table, while
`/api/v3/auth/me` exposes the profile and stable user ID. V3 still admits one
administrator and routes that user's sessions to `user_data/admin/admin.db`;
additional users and device records are not enabled yet. This does not copy
accounts or credentials from v2.
The header account panel can update the username and display name, choose a
1, 7, 30, or 90 day duration for new sign-ins, and list or revoke browser
sign-ins. Auth session documents and the duration policy live encrypted in the
app database. The panel does not yet manage machine/device configuration.
Password changes verify the current password, rotate the account generation,
and revoke existing sign-ins.

## Session capability mode

Each conversation starts in `write` mode. The authenticated
`GET/PUT /api/v3/sessions/{id}/mode` route reads or changes it with a
CSRF-protected `PUT {"mode":"read_only"|"write"}`. A mode change has a
monotonic revision and invalidates approvals waiting under the previous mode.
`read_only` hides mutating tool schemas and denies mutating calls at dispatch;
read tools and normal chat remain available. `write` retains the global
per-tool allow/deny/approval policy. The session-specific effective catalog is
available at `GET /api/v3/sessions/{id}/abilities`. The persistent global
shutoff still stops all runs independently of mode.

## Ability visibility and session activation

The shared default agent starts with each ability `discoverable`, preserving
the existing v3 load-on-demand behavior. An administrator can set an ability
to `visible` so its permitted tools and bounded skill guidance are provided
to the model on every turn. `GET /api/v3/agents/shared_default/abilities`
returns the catalog, per-ability visibility, and revision. Change one known
ability with CSRF-protected
`PUT /api/v3/agents/shared_default/abilities/{ability_id}/visibility`
and `{"visibility":"off"|"visible"|"discoverable","expected_revision":n}`.
`off` removes the ability from agent discovery and denies its tools even when an older
session had activated it. The agent configuration catalog keeps disabled entries
visible so they can be re-enabled. The separate skill setting uses
`PUT /api/v3/agents/shared_default/abilities/{ability_id}/skill-visibility`
with the same body and revision: `off` withholds the bundled skill,
`discoverable` returns it only through `load_ability`, and `visible` includes
it in the agent context when the ability is revealed. Tool modes use
PUT /api/v3/agents/shared_default/abilities/{ability_id}/tools/{tool_name}/mode
with the same revision; off removes the tool from schemas and execution.
Use the revision from GET; stale revisions return 409.

`GET /api/v3/sessions/{id}/abilities` returns the effective catalog and
the session's revisioned `active` and `suppressed` lists. A successful
`load_ability` activates a discoverable ability for later runs in that
session. An administrator can set a known ability to `active`,
`suppressed`, or `default` (clear the session override) with CSRF-protected
`PUT /api/v3/sessions/{id}/abilities/{ability_id}` and
`{"state":"...","expected_revision":n}`; stale revisions return 409.
Suppression wins over visibility and activation. Global tool policy and
session read-only mode still filter schemas and deny calls at dispatch.
Changes increment a revision and revoke approvals waiting under old
settings. Agent visibility and session activation are encrypted in their
respective databases.

At each model call Rust now builds a bounded # [ABILITIES] menu from the live,
caller-authorized catalog. A discoverable, unloaded ability appears by ID,
name, and short summary; its tool schemas and skill body remain withheld until
load_ability succeeds. The generated # [TOOLS] list names discovery tools and
tools callable in that call. An individually discoverable tool under a visible
ability appears under Load on demand with its owning ability ID. Off, suppressed,
pending, and denied entries are absent. The menu is rebuilt after policy and
session changes and does not become durable chat history. V2's bootstrap_tools
slot is authored and template-seeded; its live ability/tool indexes are generated.
V3 uses its pinned base instructions plus this generated index, not a copied V2
bootstrap slot that could advertise unavailable tools.
V2 also has an optional embedding-based relevance hint and auto-reveal path;
V3 keeps explicit loading and does not import that router.

## Recovery and controls

Read-only work can resume automatically after restart. Approvals survive restarts.
An interrupted write, shell or Python action is held for outcome review.
Never automatically replay unknown side effects. Review can accept a known result,
explicitly retry (subject to fresh policy/gate checks), or abandon the run.
Clearing global shutoff never resumes stopped runs automatically.
Shutoff cannot undo effects already committed outside the container.

Accepted user messages and final successful agent replies are stored in the user
database's `chat` table. The chat snapshot, SSE feed and webapp read only that table;
draft tokens, tool results, failures and cancellations stay out of the visible chat.
The separate encrypted `interactions` table retains model inputs and tool outputs for
future turns. Current run documents checkpoint recovery state. When context exceeds
256 KiB, v3 summarizes complete older turns and checkpoints the replacement while
retaining original interactions. A failed or unsafe summary pauses the run without
dropping its input. Provider requests send the compacted context in full.
Repeated context reads use a bounded 16 MiB in-memory cache and decrypt only
interactions appended after the cached cursor. Checkpoint changes invalidate the
cache; after restart, encrypted storage rebuilds it.

## Verification

```sh
cargo test --locked
python3 -B -m unittest discover -s tests -p 'test_*.py'
```

Build and run these commands on the development server, using the pinned Rust build image if needed.
UI journey routes and visible checkpoints are in docs/ui-journeys.md.

## Phase 1 workflow foundation

The Phase 0 architecture and storage contract is in [workflow-foundation.md](docs/workflow-foundation.md).

The existing in-process runner now records fixed checkpoints: admission,
attachment preparation, context assembly, model dispatch/output, pre-tool,
tool result, and publication. They are not reorderable. Native authorization,
approval revision checks, cancellation, shutoff and uncertain-write recovery
remain mandatory. Optional module lists are empty; configuring an uninstalled
module is rejected, not silently ignored. Receipts record the latest visit to each
checkpoint within private run state, with generation, policy revision, input
digest and turn/call index. They are audit metadata, not tool authorizations.
Policy/approval denials mark the pre-tool receipt rejected; a denied tool result
also records rejection while retaining its dispatch generation and policy revision.
Receipt reasons use fixed labels rather than copying arbitrary tool output.

`src/workflow.defaults.json` is embedded at compile time. Authenticated
`GET /api/v3/workflow/policy` returns effective policy/revision;
`PUT` validates JSON overrides and persists the resolved policy in the application
plane. Browser writes require the existing CSRF contract. Unknown fields,
uninstalled modules, invalid budgets and values exceeding shipped ceilings are
rejected. Policy is read at loop boundaries and rechecked before normal model
dispatch; publication checks the current limit. Tool permissions remain a separate
mandatory policy. Model selection and base instructions retain run pinning.

`context_assembly` is the pure model-call assembly entry point beside the loop.
It pins base instructions plus ordered agent prompt slots and prepares an envelope
from authorized ability/tool guidance, bootstrap and effective tool schemas,
durable transcript items, and native lifetime-scoped contributions. It delegates
input definitions and lifetime selection to `context_input`, measurements to
`context_accounting`, and safe complete-turn planning to `context_compaction`;
it returns either a compaction plan or the budget-checked envelope. The loop still
owns summary dispatch and persistence, approvals, credentials, cancellation,
failover and dispatch. Provider adapters retain their transport-specific serialization.

Each ready normal model call saves `context_assembly_report` in its encrypted run
checkpoint before dispatch. This content-free report records provider/transport,
turn/call identity, logical JSON envelope bytes, prompt/guidance content sizes,
bootstrap/tool schema sizes, overlay inclusion/expiry/deduplication, and history
message/call/result/reasoning sections. Argument and result content sizes overlap
their parent sections and are not additional totals. The logical envelope excludes
HTTP transport options and provider-specific translation; these are not tokenizer
counts or provider usage. Summary calls remain loop-owned and are not reported as
normal calls. The existing context-composition UI remains a static estimate, not
this per-call report.

Trusted producer contracts distinguish history/run/turn/call contributions;
history contributions must already have been committed by their producer. No
user/model JSON is implicitly accepted as trusted instructions. Session capability
mode is a native call-scoped overlay; ability guidance is regenerated per call.
This extraction preserves existing compaction boundaries and skill visibility;
it does not introduce token-aware budgets, independent skill persistence,
a new injection API, or v2 provider-profile parity. Phase 5 also permits
compaction after a fully completed tool exchange within one long task.
The loop summarizes that completed prefix, validates the summary, and saves
it before another model call; an incomplete exchange cannot be split.
A single oversized result without a safe bounded summary source still pauses
for review.

`publication` validates and commits accepted final output through the existing
transaction and unique terminal-run constraint. The caller's completed status and
publication receipt are replaced only after the durable commit succeeds; a storage
failure leaves the in-memory run unchanged. Oversized candidates pause for
review; empty responses retain the explicit failure behavior. Rich closing flags,
working-message promotion, classifiers and repair loops are later modules.
A first Phase 5 attachment slice accepts up to two inline `text/plain` entries on
`POST /api/v3/sessions/{id}/messages`: `{"name":"note.txt","media_type":"text/plain","text":"..."}`.
Each is limited to 16 KiB, with a 32 KiB total. Admission saves encrypted
user-owned records, the queued run, and the retry receipt atomically. Chat
shows the author's message while model context includes labeled, untrusted
attachment text. The response and run view carry IDs and digests; the owner can
read a record at `GET /api/v3/sessions/{id}/attachments/{aid}`. Binary uploads,
PDF extraction, image descriptions, and UI attachment actions remain unsupported.
Session memory can be saved explicitly after a completed run with
`POST /api/v3/sessions/{id}/memories` and
`{"source_run_id":"...","client_memory_id":"...","text":"..."}`.
The authenticated owner can list notes at `GET /api/v3/sessions/{id}/memories`
and remove one at `DELETE /api/v3/sessions/{id}/memories/{mid}`. Each session
holds at most 16 notes of 1 KiB each. The eight newest notes are frozen at
the next message admission and enter model context as labeled, untrusted user
data. No model output or tool call can write them implicitly. Notes are encrypted
in the user plane. This is session-scoped explicit memory, not automatic
post-run extraction or cross-session recall.
This is not full v2 parity or the Phase 2 MVP acceptance release. Image builds
run locked Rust tests in the pinned compiler stage before the release build.
Building does not deploy.

## Tenant agents and explicit model roles

<!-- TENANT-MODEL-CONTRACT: follow scoped authority, explicit appointments and pinned dispatch. -->
An agent owns its model roster and role assignments. Its owner can appoint agent
administrators, who manage model configuration and assign existing accounts as
members or visitors. Installation administrators retain oversight without becoming
owners. Credentials and connection sharing remain under each connection owner's
control. Membership changes invalidate access and quiesce affected runs. Creating
an agent's initial owner membership preserves the creator's sign-in so they can
configure models immediately; later membership edits still revoke sign-ins.

`GET/PUT /api/v3/models/catalog?agent_id=<id>` returns and atomically updates a
caller-scoped catalog using optimistic revisions. `/models/agents` lists permitted
agents and supports retry-safe creation. `/models/agents/{id}/members` lists or
assigns existing accounts. Browser writes require CSRF. Connection authorization
and API-key writes require connection ownership; credential references are minted
by the server and secrets are never returned.

Six roles are explicit entry-ID appointments: Standard, Premium, Image In,
Image Out, System and Voice. Standard serves chat; System serves compaction.
The other four workflows are not implemented. Capabilities validate appointments
but never choose a role. Each role has an ordered backup chain. Chat can supply
`model_entry_id` for a permitted roster entry without editing Standard.

Admission pins the chat and optional System plans, candidate IDs, connection
references and revisions. Dispatch and recovery recheck membership, account tier,
roster, grants, revocation and credential rotation. Role edits affect new runs.
Failover stays within its role and stops after response activity or uncertain
effects. Missing Standard is a configuration error; missing System pauses a run
when compaction is needed.

Legacy primary assignments migrate to Standard with their backups. A connected
legacy default subscription is imported once; cleared roles never resurrect.
System requires an explicit appointment. Old unpinned runs cannot inherit a new
selection. Preserve a compatible whole-state backup before deployment; old
binaries cannot enforce this new contract.

The Models page shows the selected agent's roster, roles, backups, effective
availability, connection sharing, and members. Nonmanagers get a read-only view.
Ability catalogs, ability tiers and the Abilities page are unchanged.


## Phase 4 loop and publication gate

The v3 API, loop, and metadata gate are separate sibling containers. The API
container runs HTTP, authentication, admission, the tool worker, and the promotion
broker. `webagent-dev-v3-loop` runs the Rust scheduler and recovers durable runs;
it has no public port. `webagent-dev-v3-gate` runs an unprivileged Python gate
with no network, credentials, state, or workspace mount. Only the Rust loop can
authorize tools or publish a terminal assistant reply.

The API and loop share `v3/runtime` for local control and worker sockets, and
`v3/data` for durable state. The loop and gate share only
`v3/gate-runtime`, mounted at `/run/v3/gate`. The gate socket is
`/run/v3/gate/gate.sock` (mode 0660); the Rust client verifies gate UID 1102
and the gate verifies loop UID 1100. Requests and responses use a versioned,
16 KiB bounded protocol with a one-second client deadline. The gate receives
the final reply's byte count and digest, never its text. Its first installed
module, `publication_size_guard_v1`, rejects replies above 8192 bytes.
Rust still enforces the native publication limit and is the only writer of
the final chat row.

Accepted messages are written with a schema-v1 loop command and an idempotency
receipt in one user-store transaction. The loop validates and acknowledges
commands; the queued run index remains the execution authority. Run transitions
produce versioned events and use a per-run revision plus lease owner, epoch,
and expiry checks. A terminal reply, completed run, and unique publication
outbox record commit together. The publication outbox records the accepted
reply; there is no separate downstream delivery consumer in this phase.

Build the images independently on the development server:

```sh
sh /home/ar/webagent-dev2/v3/deploy.sh build
sh /home/ar/webagent-dev2/v3/deploy.sh build-loop
sh /home/ar/webagent-dev2/v3/gate-deploy.sh build
```

For a new installation, start the API first so its control and worker sockets
exist, start the gate, then start the loop with `deploy.sh start-loop`.
`deploy.sh start` and `gate-deploy.sh start` each require their named
container to be absent. For an update, inspect the live image and container
settings, capture a compatible full backup of `v3/data` including its
encryption key before a migration, and retain the stopped prior container as
rollback material. Replace only the image's matching container: a gate update
does not replace the API or loop, and a loop update does not replace the API or
gate. Stop the loop before replacing its image; generation, lease-epoch, and
run-revision checks govern durable recovery when the new loop starts. Treat an interrupted external write as uncertain until reconciled.
The API readiness endpoint returns 503 while the loop heartbeat is absent or
stale, then 200 after the loop resumes. Check health and one persisted reply
after each replacement. The socket directory is transient and is not
application data or backup authority.

Gate activation and order live in `/state/config/loop-runtime.json` under
`workflow.modules`. The default is empty. For the first two native modules and
the optional size plugin, use:
`{"model_dispatch":["message_visibility_v1"],"publication":["closing_status_v1","optional:publication_size_guard_v1"]}`.
The fixed Rust checkpoints run in their normal order; each list orders modules
within that checkpoint. Unknown, duplicate, or wrong-checkpoint modules fail
validation. Only the isolated Python size plugin accepts the `optional:` prefix.

The integral module definitions and settings are single live JSON files at
`v3/data/config/gates/message_visibility_v1.json` and
`v3/data/config/gates/closing_status_v1.json` (mounted at
`/state/config/gates`). These same files are selected build/test inputs;
there is no duplicate packaged default. The visibility module adds the v2
message-visibility guidance and private `publish_main_message` control before
a model call. A validated call commits a nonterminal working chat row while
the run continues. The closing module offers private `set_closer_status`
with a configured flag and short notification. Rust stores this intent with
the final reply; missing or invalid status becomes the configured
`unverified` fallback without a repair turn. Its status does not itself
publish a reply.

The optional `publication_size_guard_v1` descriptor and handler live in
`v3/plugins/gates/publication_size_guard_v1/` and are packaged with the isolated Python gate. A
mandatory transport or protocol failure pauses publication; an optional
failure records a private skip and native Rust checks still apply. An explicit
rejection always blocks. No gate can grant tool permission or publish a
terminal reply; Rust alone commits that reply exactly once. Gate settings
have no UI yet.

## Context compaction

`src/context/compaction.rs` owns rolling checkpoint policy and validation.
`<V3_STATE_ROOT>/config/context-compaction.json` is the single operational authority.
On first startup only, a validated legacy/operator file (`V3_CONTEXT_COMPACTION_CONFIG_PATH`,
or the package `config/context-compaction.json`) is copied into the data root. Existing data
always wins and invalid JSON fails explicitly. The legacy file remains a migration template;
editing it after migration does not change runtime behavior. Both API and loop use `/state`.
Model/provider/fallback fields are not accepted.
Settings are read at each model-call boundary and held immutable through the summary call.

Defaults are a 40,000 estimated-token history trigger, 10,000-token verbatim tail,
and 2,000-token summary prompt target, using four characters per estimated token.
Estimates count serialized history characters, including the previous checkpoint, not instructions
or tool schemas. The independent full-request byte ceiling remains in loop-runtime.json and
can trigger compaction earlier. Safe complete boundaries may retain less than the tail target.
Each fold replaces the previous checkpoint plus aged history with one summary; original
interactions remain intact. Summary generation uses the run's separately pinned System role and its own
backup chain. It never falls back to Standard. The summary token target is a prompt allowance, not exact tokenizer
measurement; configured reserve and validation limits remain hard byte constraints.

Loop-runtime schema 2 removes the duplicate summary reserve. Schema 1 data is migrated
with a backup; legacy workflow storage imports discard the redundant reserve. Workflow API
writes reject compaction settings: use the compaction JSON instead. Cached per-item byte and
Unicode character measurements feed compaction planning without reserializing unchanged history.
No settings UI, recall tools, or failure-policy changes are added. Rust changes require rebuilding;
data JSON edits apply at the next call boundary without restart. Back up configuration and state
before rollout. Original interactions and checkpoint representation remain compatible.

## Recovery input deltas and shared retained-cache limits

Run metadata now references a version-1 item set in the user database's encrypted
`recovery_input` table. Saves compare the working input with existing items and encrypt/write
only new or changed items. Truncation and compaction replace the affected recovery items;
original `interactions` rows remain untouched. Metadata, item updates, revision/lease fences,
events and publication receipts commit in the same transaction. Readers reconstruct the
complete run in one transaction and reject missing ordinals, unsupported versions and failed
authentication. Existing inline-input run documents remain readable and upgrade on write.

<!-- RECOVERY-MANIFEST: sealed commitments avoid decryption without trusting a caller hint. -->
This is a materialized recovery checkpoint with item-level deltas, not an unbounded replay
journal or a periodic-checkpoint scheduler. The encrypted metadata optionally holds a v1
manifest pairing SHA-256 digests of compact JSON content and its ciphertext at each ordinal.
Saves verify every prior ciphertext against that authenticated manifest before mutation and
hash current input through a streaming JSON writer, without a full temporary JSON buffer.
Unchanged retained items need no decryption, encryption, or row rewrite. Inline and older
split records remain readable; manifest-free split records decrypt once on their next save
to establish commitments, preserving unchanged ciphertext. Malformed manifests, missing
rows, swapped ciphertext, and damaged rows fail closed; reads still decrypt and verify the
complete input. Metadata and manifests commit alongside item deltas in the same transaction.

Saves still read all retained ciphertext and serialize/hash current input: this is not yet
committed mutation tracking or constant-time metadata persistence. Terminal fencing still
hydrates a previous run. Active snapshots and provider payloads still own copies. The
verified benefit is avoiding unchanged history decryption, encryption and database rewrites;
no overall CPU, allocation, RSS or latency speedup is inferred from this change.

Each Store family shares one document LRU and one context LRU across all its user handles.
Context keys include the account identity, and invalidation affects only that account/session.
Reconfiguration resets these shared caches for existing and future handles. Runtime JSON
`document_budget_bytes` and `context_budget_bytes` therefore bound estimated retained cache
weight per process, not per user. Document caching remains disabled for cross-process runtime
authority reads. API and loop processes have separate caches; pools, live request copies,
streaming buffers and allocator overhead are outside these limits. Independent Store roots
also have independent budgets. Idle connections expire after 60 seconds. At 64 registered user handles, idle entries are pruned; active handles retain their shared synchronization gate. This is not a hard cap on active users or all process memory.

Rollout requires a rebuilt API and loop using the same format. Older binaries cannot hydrate
split run records: stop old writers before rollout and back up the state root. Do not downgrade
a populated state root without restoring its compatible backup or a deliberate export migration.
Source verification used temporary fixtures only; this change does not migrate live data by itself.

## Runtime optimization boundaries

<!-- CONTEXT-ROADMAP: keep operational estimates separate from compaction authority. -->
Run-local guidance/schema reuse keys the full freshly resolved authorization state,
catalog, policy, visibility, loaded abilities and mode. Tool execution still checks live
permissions. Activated discoverable skills are rebuilt from the catalog across compaction;
Off and suppressed states remain authoritative. Catalog refresh still requires restart.

Optional `context.model_profiles` in `config/loop-runtime.json` maps
`provider/model` to `context_tokens`, `output_reserve_tokens`, and `bytes_per_token`
(1–8). The logical-envelope ceiling is the smaller of the existing byte ceiling and
`(context_tokens - output_reserve_tokens) * bytes_per_token`. Unknown models retain
the existing ceiling. These are operator estimates, not tokenizers, entitlement claims,
or provider output controls. Compaction policy remains solely in context-compaction.json.

Subscription streaming retains at most 4096 completed fallback items and 16 MiB
serialized fallback output, with bounded diagnostic event-kind cardinality. This does
not bound HTTP-library buffers, allocator overhead or all concurrent request memory.
Public context summaries allowlist numeric logical-envelope byte totals only; they are
not billed/provider tokens. The UI displays those summaries when the deployed backend
supplies them. Source tests alone do not establish deployed UI acceptance.

Repeatable accounting measurement:
`cargo test --locked context_accounting::measurements::repeated_accounting_measurement -- --nocapture`.
This compares unchanged-history serialization with cached accounting, not end-to-end
runtime throughput, allocations or RSS. Full runtime benchmarking and coordinated
API/loop rollout remain acceptance work; do not infer those outcomes from this test.

<!-- RECOVERY-BENCHMARK: opt-in fixture never reads or changes deployed state. -->
Repeatable transactional recovery measurement (temporary encrypted SQLite state):
`cargo test --locked repeated_recovery_save_measurement -- --ignored --nocapture`.
It times 50 status-only saves of 512 retained items and a reopen/read, asserting
unchanged ciphertext and exact reconstruction. It does not claim a baseline speedup,
provider throughput, allocation count, or total-process RSS reduction.

### Dispatch allocation boundaries

DISPATCH-HISTORY-TRANSFER / DISPATCH-RELEASE-SNAPSHOTS: after durable
assembly receipts and credential resolution, normal dispatch moves the local
history allocation into the provider instead of cloning it. The separately
loaded fencing snapshot is saved and released before awaiting the provider;
response handling reloads authoritative state. Cancellation, permission and
workflow checks remain in place. Schemas still have an owned dispatch copy.

DISPATCH-DIGEST-STREAM / PROVIDER-BORROWED-BODY: fingerprints stream JSON
directly into SHA-256; subscription requests serialize borrowed fields directly
into the HTTP body without an intermediate owned JSON tree. Wire-parity tests
cover omitted tools, reasoning, escaping and complete arguments. Transport
buffers and provider-specific conversions still allocate, and recovery saves
still perform history-sized integrity work. No measured process-wide CPU or
peak-RAM reduction is claimed from these allocation removals alone.

## App access-policy configuration

V3 owns `<state-root>/config/access-policy.json` (deployed as
`v3/data/config/access-policy.json`, mounted at `/state/config/access-policy.json`).
The repository template is `v3/config/access-policy.json`:

```json
{"registration": "restricted"}
```

`restricted` creates pending accounts requiring administrator approval; `open`
activates ordinary registered accounts and enables the existing guest admission
flow. Neither grants administrator privileges or agent/tool entitlements.
The file is read on policy checks, so edits require no restart after this code
is deployed. Invalid or unreadable policy fails closed, rather than opening access.
On first startup without a file, the existing database registration mode is
migrated; otherwise the default is restricted. The file then becomes authoritative.
The existing access-mode API keeps its V2-compatible values (`admin_approval` /
`public_registered`) and writes this file atomically. Registration edits preserve
additional JSON fields for future feature/page policies; those fields do not yet
enforce restrictions. Do not put credentials or user records in this file.

## Integral ability slices

Base has a partial port: Rust-native fixed-offset time/date, finite arithmetic,
text attachment paging, literal current-session chat search and exact bundled
skill listing/loading, plus bounded current-agent prompt reads. Prompt reads
hide internal slots and never accept another agent as a target. Skill reads recheck current access and bundle validity;
loading never activates an ability or grants execution. Revoked native skill
text is masked from future model inputs while its encrypted journal is retained.
Only compiled
schemas are exposed; unsupported Base declarations stay unavailable. The
legacy account and membership migration grants only Codebase Admin and Web
Access. New ability IDs require explicit grants beneath installation policy.

The installation startup-resume service is configured through
`GET/PUT /api/v3/admin/services/boot_orphan_resume`. It stores encrypted,
revisioned installation configuration and actor audit records. The default
preserves automatic safe recovery. Disabling it stops interrupted safe work at
startup until explicitly resumed. Uncertain writes always require reconciliation;
ordinary expired-lease recovery remains active. Agent owners cannot change this
installation service merely by editing their agent's ability visibility.

Luna model calls use a conservative 119,808-byte logical input ceiling beneath
the requested 128,000 context limit, reserving 8,192 for output/protocol headroom.
Tighter installation limits and operator estimates still apply. This byte budget
does not claim an exact provider token count or set an upstream output limit.

## Administrator coding authority

The compiled registry in `src/agent_loop/tools/administrator.rs` owns the Administrator
model schemas. Rust binds each call to its authenticated user, session and agent;
model arguments never select a tenant, credential, host origin or Docker command.
Agent-owned settings use revision-checked encrypted metadata. Cautious settings
strengthen approvals; they cannot raise the installation or account-tier ceiling.
Repository connections have owner-bound, separately encrypted token references;
rotation/revocation quiesces affected resources. Public views never return tokens.

The Python worker executes isolated source/index/Git/PTY operations. `sandbox.py`
requires Landlock ABI 6 or newer and denies other workspaces, state, control sockets
and signals outside the child domain. Preview listener ports are assigned per
workspace below the ephemeral test-listener range. A trusted supervisor reaps
descendants. Seeding copies only the immutable image snapshot and fences publication.
Workspace requests keep encrypted caller-owned receipts; an interrupted mutation is
reconciled, never silently replayed.

Diagnostics browsers use short-lived current-caller cookies kept outside coding
workspaces. PNG artifacts require authenticated ownership and current access. Preview
quick tunnels expose only verified workspace terminal listeners through an authenticated
expiring proxy; they never replace the permanent development tunnel.

Frozen image builds and separately approved development deployment use a protected
host broker, not a Docker socket in the app or tool worker. Only installation
administrators can see or submit those host jobs; the same Rust policy resolver
removes their schemas for ordinary accounts. Renewable access leases stop pending effects
on revocation. Jobs retain rollback image/config exports and stopped containers;
uncertain interrupted deployment requires reconciliation. Source input changes make
a build stale even if compilation passed.
