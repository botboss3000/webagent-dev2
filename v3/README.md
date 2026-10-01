# WebAgent v3 development

Independent Rust agent runtime, subscription provider adapter, and isolated Python tool worker.
Source lives here within the primary development checkout; v2 remains independently available.

## Run

On the development server:

```sh
sh /home/ar/webagent-dev2/v3/deploy.sh build
sh /home/ar/webagent-dev2/v3/deploy.sh start
curl -fsS http://100.112.23.15:8081/health/ready
```

The existing named tunnels serve https://webagent.live/app from dev2 on host port 8080
and https://dev.webagent.live/app from v3 on host port 8081. The previous
production app container is stopped and kept as a rollback snapshot.

On first start, open `/setup.html` to choose the v3 admin username and password.
The retired test bootstrap credential is removed on the first launch of this setup flow.
Open `/app?tab=agents&agent=shared_default` to use the v2 Agents layout and configure the default agent's Model page. Connect a fresh ChatGPT subscription there; v2 tokens are never imported.
The default agent uses `gpt-6-luna` until you save another model. Choose a model supported by your account; a catalog name alone does not establish entitlement.
Agent model settings store an explicit provider ID. The current supported ID is
`openai_subscription`; unsupported IDs are rejected. Each accepted run pins its
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
and sizes, placeholder, transcript width, and tail clearance. Other v2 controls
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
