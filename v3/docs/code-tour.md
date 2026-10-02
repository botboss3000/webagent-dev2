# A plain-language tour of WebAgent v3

This is a map for someone reading v3 code for the first time. The
[README](../README.md) has deployment commands and the detailed contracts.

A **run** is one attempt to answer a user's message. It can be queued,
working, waiting for approval, or finished.

## The five actors

| Actor | Everyday meaning | Start reading |
| --- | --- | --- |
| Web API | The front desk: sign-in, access checks, message admission, and chat pages. | [main.rs](../src/main.rs), [user_api.rs](../src/user_api.rs) |
| Rust loop | The worker that picks up accepted messages, calls a model, and decides what happens next. | [main.rs](../src/main.rs): `scheduler` and `execute_run` |
| Storage | The durable notebook: accepted work, progress, and final replies survive restarts. | [store.rs](../src/store.rs) |
| Python tool worker | Runs installed low-level tools only after Rust permits them. | [worker.py](../worker.py), [tools.rs](../src/tools.rs) |
| Python gate worker | Optionally checks a proposed final reply; it cannot publish one. | [gate_worker.py](../gate_worker.py), [gate_protocol.rs](../src/gate_protocol.rs) |

`V3_PROCESS_ROLE=api` and `V3_PROCESS_ROLE=loop` start the same Rust
executable in separate containers. The `health` role checks the loop heartbeat.

## Follow one message

1. **Sign in and check access.** Account status, role, tier, agent membership,
   and current permissions decide whether the person may proceed. The rules
   are in [access.rs](../src/access.rs),
   [access_policy.rs](../src/access_policy.rs), and
   [phase3_api.rs](../src/phase3_api.rs).
2. **Accept and save.** The API writes the user's message, a `queued` run,
   and a receipt in one transaction. Retrying the same message ID returns the
   same run. HTTP 202 means accepted, not answered.
3. **Notice the work.** The loop validates and acknowledges versioned command
   records, then scans queued runs. Queued storage is the execution authority;
   commands are a durable trigger ledger.
4. **Claim a run.** The loop takes a time-limited lease and assigns a new
   generation and revision. An old worker cannot overwrite a newer attempt.
   See `claim_run` and `save_run` in [store.rs](../src/store.rs).
5. **Prepare and call the model.** The loop assembles history, instructions,
   and available tools. [workflow.rs](../src/workflow.rs) names the fixed
   checkpoints; [context_input.rs](../src/context_input.rs) defines borrowed inputs
   and selects temporary contributions, [context_accounting.rs](../src/context_accounting.rs)
   measures append-only history, and [context_assembly.rs](../src/context_assembly.rs)
   combines and validates the request, [context_compaction.rs](../src/context_compaction.rs) plans and validates
   history compaction, and [provider.rs](../src/provider.rs) sends it.
6. **Handle tool requests, if any.** Rust checks today's access and tool policy
   again. Some actions need human approval. Only a permitted action reaches
   the Python tool worker. Showing a tool to the model does not permit its use.
7. **Check a final reply.** Rust applies its own output rules. If enabled,
   `publication_size_guard_v1` receives bounded metadata such as the reply's
   byte count, run identifiers, and digests, never its text. It can pass or
   reject, but cannot publish.
8. **Publish once.** Rust commits the completed run, one assistant message,
   and a publication record together. That record has no separate delivery
   consumer today. See [publication.rs](../src/publication.rs).

Clients read messages and run status through the API; they do not open database
files directly.

The session-scoped progress endpoint projects saved run state and bounded
checkpoint decisions. Receipts are shown in fixed name order, not execution
order. See [task_progress.rs](../src/task_progress.rs). This
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

[store.rs](../src/store.rs), [access_store.rs](../src/access_store.rs), and
[agent_storage.rs](../src/agent_storage.rs) implement these boundaries.

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
