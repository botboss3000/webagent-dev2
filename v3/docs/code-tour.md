# A plain-language tour of WebAgent v3

Agent ability access follows `src/api/ability_admin.rs` into the selected agent's
metadata and membership reference. `src/access/capabilities.rs` owns revisioned
capability levels; `src/access/policy.rs` meets them with installation rules,
account-tier ability/tool grants and agent base policy. The same result filters
model guidance and schemas and is checked again before execution. A listed
manifest remains metadata until compiled registration and readiness exist.
Partial ports expose only registered tools and keep unsupported declarations
unavailable. Core native adapters live under `plugins/abilities/Core/base/`.
Startup-resume installation configuration lives in `src/services.rs`, with its
revisioned administrator API in `src/api/service_admin.rs`; startup recovery
checks it before requeuing safe interrupted work. Uncertain writes still require
reconciliation regardless of that setting.
See [ability port ledger](ability-port-ledger.md) for explicit remaining gaps.


This is a map for someone reading v3 code for the first time. The
[README](../README.md) has deployment commands and the detailed contracts.

A **run** is one attempt to answer a user's message. It can be queued,
working, waiting for approval, or finished.

## Tenant agents and model roles

<!-- TENANT-MODEL-ROUTE: resolve authority, then pin the role before dispatch. -->
The agent directory fixes each owner. A separate membership capability appoints
an agent administrator for model and member administration, without changing
tool or ability authority. Owners appoint administrators; delegated administrators
assign existing accounts as members or visitors. Installation administrators
retain oversight without becoming owners. See `agent_authority` and
`assign_agent_membership` in [storage/access.rs](../src/storage/access.rs).
Initial owner creation advances access revisions while preserving the creator's
sign-in, allowing immediate configuration and retry. Later membership edits
rotate sign-in generation and stop affected work.

[api/model_admin.rs](../src/api/model_admin.rs) resolves the selected agent before
returning a filtered catalog or accepting changes. Only a connection owner can
edit its credentials or sharing grants. Both the connection and model entry
must grant an agent access. Rust mints credential references; browser input
cannot select another owner's stored secret.

[models/selection.rs](../src/models/selection.rs) separates roster membership,
capabilities and six explicit role appointments. Standard routes chat; System
routes compaction. Image In describes uploaded images, and Voice handles transcription and
ephemeral conversation routing. Premium and Image Out remain configurable
without a separate workflow. Each role has independent ordered backups. A permitted
custom choice selects one roster entry without changing Standard. The chat footer
reads permitted choices through api/chat_controls.rs. Its revisioned preference
and retry receipt live in the owning user session; admission rechecks the choice
and pins it for dispatch. The loop consumes a newer composer revision at the next model-call boundary without another user message, preserving the current call and separate System plan. The public run view exposes only the dispatched model ID.
Usage accounting in src/usage.rs accepts only bounded provider-reported numeric fields. Store::index_run updates session totals by the difference from the previous encrypted run view in the same transaction; repeated saves do not double-count. GET /sessions/{id}/usage returns a safe numeric summary and latest context projection. Context bytes and provider tokens remain separate, and unreported cost/cache values stay unknown.
Execution-mode changes from the footer supply the current revision; moving from
read-only to write-capable requires explicit browser confirmation. Model-tool
mode changes still require their separate Rust approval gate. Continue creates
a fresh ordinary user turn and never retries a stopped run implicitly.

[api/chat.rs](../src/api/chat.rs) pins both role plans and their revisions.
[app.rs](../src/app.rs) and [agent_loop/run.rs](../src/agent_loop/run.rs) recheck
membership, account tier, roster, grants and credential references at dispatch
and recovery. Role changes affect new runs; revocation can stop a pinned run.
Failover never crosses roles. Recovery cannot invent a plan for an old run that
never pinned one; start a new message after configuring Standard.

Old primary assignments migrate to Standard with their existing backup order.
A connected default subscription is imported once; System is never silently
appointed. Clearing Standard persists an unconfigured state. Old records are
retained, but older binaries cannot enforce this tenant contract: rollback
requires a compatible whole-state backup.


## The five actors

| Actor | Everyday meaning | Start reading |
| --- | --- | --- |
| Web API | The front desk: sign-in, access checks, message admission, and chat pages. | [api/mod.rs](../src/api/mod.rs), [api/chat.rs](../src/api/chat.rs) |
| Rust loop | The worker that picks up accepted messages, calls a model, and decides what happens next. | [agent_loop/scheduler.rs](../src/agent_loop/scheduler.rs), [agent_loop/run.rs](../src/agent_loop/run.rs) |
| Storage | The durable notebook: accepted work, progress, and final replies survive restarts. | [storage/mod.rs](../src/storage/mod.rs) |
| Python tool worker | Runs installed low-level tools only after Rust permits them. | [worker.py](../worker.py), [agent_loop/tools/mod.rs](../src/agent_loop/tools/mod.rs) |
| Python gate worker | Optionally checks a proposed final reply; it cannot publish one. | [gate_worker.py](../gate_worker.py), [gates/protocol.rs](../src/gates/protocol.rs) |

`V3_PROCESS_ROLE=api` and `V3_PROCESS_ROLE=loop` start the same Rust
executable in separate containers. The `health` role checks the loop heartbeat. [main.rs](../src/main.rs) selects
that role; [app.rs](../src/app.rs) builds the shared state and starts it.

The file layout follows those responsibilities. [api/](../src/api/mod.rs) owns
HTTP handlers; [agent_loop/](../src/agent_loop/mod.rs) owns scheduling,
recovery, model turns, and publication. Single and parallel tool calls share
[tools/dispatch.rs](../src/agent_loop/tools/dispatch.rs), while
[tools/batch.rs](../src/agent_loop/tools/batch.rs) decides which calls may
overlap. [runtime_config.rs](../src/runtime_config.rs) holds concurrency and
other loop settings. [gates/](../src/gates/mod.rs) validates selected modules;
[initial/](../src/gates/initial/mod.rs) and
[closing/](../src/gates/closing/mod.rs) own their control behavior. The
optional size gate has one [descriptor](../plugins/gates/publication_size_guard_v1/gate.json)
and [handler](../plugins/gates/publication_size_guard_v1/handler.py).
[context/](../src/context/assembly.rs), [models/](../src/models/provider/mod.rs),
[access/](../src/access/mod.rs), and [storage/](../src/storage/mod.rs) group the
other Rust code. The Rust binary still uses some root module aliases so
existing storage and API contracts stay stable through this source move.

## Follow one message

1. **Sign in and check access.** Account status, role, tier, agent membership,
   and current permissions decide whether the person may proceed. The rules
   are in [access/mod.rs](../src/access/mod.rs),
   [access/policy.rs](../src/access/policy.rs), and
   [api/admin_access.rs](../src/api/admin_access.rs).
2. **Accept and save.** The API writes the user's message, a `queued` run,
   and a receipt in one transaction. Retrying the same message ID returns the
   same run. HTTP 202 means accepted, not answered.
3. **Notice the work.** The loop validates and acknowledges versioned command
   records, then scans queued runs. Queued storage is the execution authority;
   commands are a durable trigger ledger.
4. **Claim a run.** The loop takes a time-limited lease and assigns a new
   generation and revision. An old worker cannot overwrite a newer attempt.
   See `claim_run` and `save_run` in [storage/mod.rs](../src/storage/mod.rs).
5. **Prepare and call the model.** The loop assembles history, instructions,
   and available tools. Configured model-dispatch gates add trusted, call-scoped
   guidance and private control schemas; this guidance is not durable chat text.
   Before each call, Rust filters the installed ability catalog and tool schemas
   through live agent, account-tier, membership, installation, and session rules.
   It generates a compact # [ABILITIES] menu for discoverable abilities and a
   # [TOOLS] list for tools callable now. load_ability reveals a selected ability's
   skill and tools for the session; the menu itself grants nothing. This mirrors
   V2's generated indexes without copying its authored bootstrap_tools slot.
   The integral gate JSON under `v3/data/config/gates` is both the build input
   and live configuration. Optional modules live under `v3/plugins/gates`. [workflow.rs](../src/workflow.rs) names the fixed
   checkpoints; [context/input.rs](../src/context/input.rs) defines borrowed inputs
   and selects temporary contributions, [context/accounting.rs](../src/context/accounting.rs)
   measures append-only history, and [context/assembly.rs](../src/context/assembly.rs)
   combines and validates the request, [context/compaction.rs](../src/context/compaction.rs) plans and validates
   history compaction, and [models/provider/mod.rs](../src/models/provider/mod.rs) sends it.
   Agent identity stores optional message-format and closing-notification guidance
   in encrypted metadata. The Identity API revision-fences edits. Message format
   is call-scoped on every model call, even with the working-message route off;
   notification guidance accompanies the server-owned closing protocol.
6. **Handle tool requests, if any.** Rust checks today's access and tool policy
   again. Some actions need human approval. Eligible independent calls can run
   together under each tool's concurrency setting; rejected calls get their own
   result. Only a permitted low-level action reaches the Python tool worker.
   The private `publish_main_message` and `set_closer_status` controls stay in
   Rust. A valid working update becomes a nonterminal assistant chat row in the
   same transaction as its tool result. Showing a tool to the model does not
   permit its use.
7. **Check a final reply.** Rust applies its own output rules. The configured
   closer records a status flag and short notification intent; missing or
   malformed status becomes `unverified` without another model turn. If enabled,
   `publication_size_guard_v1` receives bounded metadata such as the reply's
   byte count, run identifiers, and digests, never its text. It can pass or
   reject, but cannot publish.
8. **Publish once.** Rust commits the completed run, one final assistant
   message, and a publication record with the notification intent together.
   That record has no separate delivery consumer today. See [agent_loop/publication.rs](../src/agent_loop/publication.rs).

Clients read messages and run status through the API; they do not open database
files directly.

The session-scoped progress endpoint projects saved run state and bounded
checkpoint decisions. Receipts are shown in fixed name order, not execution
order. See [api/progress.rs](../src/api/progress.rs). This
content-free view has no completion percentage or proof of goal fulfillment.
Its compaction count is the run's saved count of completed summaries; missing
counts mean zero, while invalid or excessive stored counts appear as unknown.

## Where information belongs

- **Application storage:** accounts, roles, tiers, agent catalog, and audit.
- **Agent storage:** one agent's settings, abilities, connections, and policies.
- **User storage:** that person's chats, runs, approvals, and receipts.
- **Secrets:** keys and credentials stay behind controlled interfaces. The
  Python gate receives neither.
- **Transient sockets:** `v3/runtime` connects web and loop;
  `v3/gate-runtime` connects loop and gate. They are not chat backups.

[storage/mod.rs](../src/storage/mod.rs), [storage/access.rs](../src/storage/access.rs), and
[storage/agent.rs](../src/storage/agent.rs) implement these boundaries.

## Access words

| Word | Plain meaning |
| --- | --- |
| Status | Whether an account is pending, active, suspended, or disabled. |
| Role | Whether the person is an ordinary user or administrator. |
| Tier | Which models, limits, and capabilities the account receives. |
| Relationship | Whether the person owns, belongs to, or may call an agent. |
| Visibility | Whether the model can see or discover a tool. |
| Permission | Whether an action is denied, needs approval, or can run automatically. |
| Availability | Whether the tool has an installed implementation. |

These checks are separate. Visible never means permitted. A saved approval is
rechecked if access or policy changes.

## Restarts and unfamiliar terms

The API returns **503 readiness** when the loop heartbeat is stale. It can
still save an accepted message while the loop is down. The returning loop
resumes queued work. It holds an interrupted external write for review when
the outcome is unknown, and it will not publish a second final reply.

A **lease** is temporary ownership of a run. A **generation** identifies one
execution attempt. A **revision** changes with each saved run update. Together
they keep stale processes and API requests from overwriting newer progress.
An **outbox** is a durable record written with the action it describes.
A **receipt** remembers the result of a request so a retry cannot make a second
run. A **transaction** means related records are saved together or not at all.
A **digest** is a one-way fingerprint used to compare inputs without sending
raw content to the gate. A **checkpoint** is a named place where the loop
records what it checked and decided.

## Current limits

The final-reply size guard is the only installed Python gate module.
The context planner can now summarize a completed tool exchange during one
long task when earlier-turn compaction cannot make room. Rust persists the
validated summary before calling the model again; the public chat and tool history remain durable. A background user-store job
removes reasoning from the aged private interaction and completed-run recovery
records after the checkpoint commits. Small inline text attachments are accepted and stored in the
user plane with the message; their text enters context as untrusted source data.
A completed run can supply the source for an explicit session memory note.
The authenticated API saves the bounded note in the user plane. At the next
message admission, Rust freezes the eight newest notes; loop hydration puts
them beside the newest user turn as labeled source data, never instructions.
List and delete are session-scoped. Automatic memory extraction, binary files,
images, requirement evidence, and delegation remain later roadmap work.
An ability catalog entry does not imply that every v2 tool runs in v3.

Authenticated run diagnostics also expose a bounded numeric context summary from
the durable assembly report. These values measure serialized JSON bytes, not
provider tokens; transcript text, instructions and tool contents remain private.

Luna calls have a conservative 119,808-byte logical input ceiling beneath the
requested 128,000 context limit. The runtime reserves 8,192 for output/protocol
headroom and never lets an operator estimate expand that ceiling. This is a
conservative byte budget, not an exact provider token count or output control.

Bundled skill reads resolve current caller, agent, tier, capability level and
visibility before returning exact catalog assets. Loading never grants execution.
Live bundle validity invalidates the run cache when a manifest or skill changes;
revoked native skill text is masked from future model inputs while its encrypted
journal remains intact. Compacted history remains past evidence, not permission.

Base read_own_prompt resolves the current caller-owned session before the agent
and rechecks live access and bundle validity around bounded prompt-slot reads.
Internal slots are omitted; locked slots and nonmanagers are reported read-only.
The separate edit_own_prompt, save_own_skill and remove_own_skill tools now
target that agent, require manager authority and compare-and-swap revisions,
and save idempotency receipts. Self-authored skills remain selectable only;
loading one never grants execution.

Base's cross-session search reads only caller-owned sessions and rechecks agent
admission. Similarity is lexical. Manual compaction runs only at the next safe
model boundary; the reasoning-filtered source segment and checkpoint save atomically in encrypted
user storage. A hash-only cleanup marker lets the loop retire older reasoning
in a separate atomic transaction. Recall pages that source after masking revocable guidance. Task
checkpoints and declarative components use run-generation fences and revisions.
The session snapshot projects components as display-only data; the chat panel
renders them as text on reload. A model-generated choice, form or approval
display does not submit an answer or authorize an action.

## Administrator coding path

`agent_loop/tools/administrator.rs` and its compiled schema JSON bind current-session
diagnostics, browser authority, workspace receipts and host jobs. `api/repository.rs`
owns tenant repository URLs and minted encrypted credentials; `api/ability_admin.rs`
owns CAS settings. `administrator_services.rs` renews host authorization leases and
polls opt-in content-free repository notices without publishing assistant replies.
`api/administrator.rs` checks authenticated session ownership for private PNG artifacts
and notices.

Static worker dispatch loads only installed Administrator adapters. Code Index keeps
its derived bounded index inside the isolated workspace; Git uses no-follow directory
descriptors and exact HEAD/review fences; PTYs retain observation sequences and
bounded output. Seeding and fixed Cargo tasks run under `sandbox.py`; approved shell, Python and terminal commands can reach the live development checkout mounted at /live-checkout, including v3 and v2 source;
trusted supervisors cancel descendants. Diagnostics profiles and preview authentication
remain outside the shell-readable workspace. The preview proxy rechecks that the
listener belongs to the same workspace terminal before forwarding.

`plugins/abilities/Administrator/v3_build/host.py` is installed as a protected host
service, with a root-created Unix listener and narrow frozen-input snapshot channel.
Generated Python bytecode caches are excluded from both protected and direct image
build inputs.
The app and worker do not receive host Docker privileges. Fixed development jobs
are caller-owned, idempotent and revision fenced. Revocation cancels a pending job;
after uncertain effects it retains receipts and rollback material for reconciliation.
V3 Build's work tool records per-session source claims in the protected broker's
durable coordination registry. A final close with the current full-context digest
submits one combined build/test/deploy job; other open claims block submission and
the broker checks the digest and claims again before stopping a container. Rust
checks live installation and tool authority and renews the job's short lease.
The broker also accepts the development checkout owner over the same Unix socket
for only work coordination and read-only status/job polling. It derives Codex's
identity from the Unix peer, never a client-supplied V3 user or credential.
An unregistered external editor is outside the registry, so the digest remains
the final source-race fence.
Computer Control and Program Screenshot remain excluded and unavailable. The
eight selected Administrator abilities passed authenticated API acceptance: real
workspace editing, Code Index, Git push and conflict handling, supervised
terminals, private browser artifacts, leased previews and protected image
builds. Fixed V3 Workspace Cargo check and test passed on a stable source
snapshot. Cargo tool output keeps the final 8 KiB so diagnostics remain visible
within the model context budget. The development API container needs 2 GiB of
memory for full V3 source compilation. No UI journey was run. Broader ability
groups remain pending as documented in the port ledger.

## Composer media and voice

api/uploads.rs keeps upload bytes in encrypted caller-owned documents. Pending
files can be removed with an atomic quota update; admission makes the run link
immutable. Public chat metadata is separate from model-facing excerpts.
Core/base/session_adapter.rs reads text, native image descriptions or honest
metadata, never raw binary content or another conversation's uploads.

api/voice.rs owns a bounded transient voice lane per caller and conversation.
PCM16 frames use cookie/CSRF-authenticated HTTP requests with monotonically
numbered, replay-safe frames. Rust VAD ends a turn after speech and silence;
periodic partial and final transcription use the agent's pinned Voice role.
MediaRecorder is the browser fallback. Audio, quick questions, answers and
unconfirmed proposals are memory-only and expire; restart discards them.
Confirmed work enters the existing send path with a durable receipt. Target-run
and mode revisions fence changes; cancellation preserves uncertain-write recovery.
Spoken confirmations never approve a pending tool action.

Spoken replies use the browser by default. The optional installation speech
service requires V3_VOICE_TTS_API_KEY; V3_VOICE_TTS_MODEL accepts tts-1,
tts-1-hd or gpt-4o-mini-tts. Its endpoint is fixed to OpenAI, and it never reads
v2 credentials. Agent Voice transcription needs an audio-capable API connection;
a ChatGPT subscription connection alone cannot transcribe microphone recordings.

The focused acceptance tests are api/composer_tests.rs, provider media tests,
and tests/chat-controls.test.cjs / tests/composer-media.test.cjs. These use
isolated stores and local providers. Browser and live-provider acceptance require
deployment of both matching V3 API and loop images; fixture success alone does
not establish live microphone/provider acceptance.
