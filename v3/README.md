# WebAgent v3 development

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
`v3/ui/chat/chat.css`; `v3/ui/app.js` mounts the partial before binding controls.
V3 reads the existing `data/config/chat_ui.json` through an authenticated
`GET /api/v3/auth/ui-config` route. The deployment mounts only that file
read-only; edits to it affect both v2 and v3 after a browser refresh. Its
`chat_common` values merge with `chat_desktop` or `chat_mobile`; arrays replace
the common value. V3 currently applies supported header rows (agent name,
conversation selector, previous/next, new, minimize), active/idle composer rows
and sizes, placeholder, transcript width, and tail clearance. The composer uses the canonical v2 pill/footer markup and shared CSS.
`v3/ui/chat/chat-pill-config.js` is a synchronized copy of
`ui/shared/js/chat-pill-config.js` with only its sign-in hook adapted;
`footer-handle.js` reuses the v2 footer drag interaction with v3-scoped
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
The Administrator catalog includes the eight v2 ability manifests and their skill files
under v3/plugins/abilities/Administrator/. Only Codebase Admin currently has executable
v3 worker tools; the other ability rows show their declared tools as unavailable.
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
limits changes to backend source, abilities, tests, and selected v3 root files.
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
control. Membership changes invalidate access and quiesce affected runs.

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
