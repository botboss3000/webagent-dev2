# A plain-language tour of WebAgent v3

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

[api/model_admin.rs](../src/api/model_admin.rs) resolves the selected agent before
returning a filtered catalog or accepting changes. Only a connection owner can
edit its credentials or sharing grants. Both the connection and model entry
must grant an agent access. Rust mints credential references; browser input
cannot select another owner's stored secret.

[models/selection.rs](../src/models/selection.rs) separates roster membership,
capabilities and six explicit role appointments. Standard routes chat; System
routes compaction. Premium, Image In, Image Out and Voice remain configurable
but have no workflow. Each role has independent ordered backups. A permitted
custom choice selects one roster entry without changing Standard.

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
   The integral gate JSON under `v3/data/config/gates` is both the build input
   and live configuration. Optional modules live under `v3/plugins/gates`. [workflow.rs](../src/workflow.rs) names the fixed
   checkpoints; [context/input.rs](../src/context/input.rs) defines borrowed inputs
   and selects temporary contributions, [context/accounting.rs](../src/context/accounting.rs)
   measures append-only history, and [context/assembly.rs](../src/context/assembly.rs)
   combines and validates the request, [context/compaction.rs](../src/context/compaction.rs) plans and validates
   history compaction, and [models/provider/mod.rs](../src/models/provider/mod.rs) sends it.
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
validated summary before calling the model again; the original run history
remains durable. Small inline text attachments are accepted and stored in the
user plane with the message; their text enters context as untrusted source data.
A completed run can supply the source for an explicit session memory note.
The authenticated API saves the bounded note in the user plane. At the next
message admission, Rust freezes the eight newest notes; loop hydration puts
them beside the newest user turn as labeled source data, never instructions.
List and delete are session-scoped. Automatic memory extraction, binary files,
images, requirement evidence, and delegation remain later roadmap work.
An ability catalog entry does not imply that every v2 tool runs in v3.
