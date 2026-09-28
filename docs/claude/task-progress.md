# Durable task progress and isolated supervision

The runtime owns an engine-independent task contract in
`app/agent/task_state.py`. The scoped application database stores
`agent_task_state`; all access goes through the application's normal scoped
backend. This is additive state. Existing transcripts, task-reduction receipts,
and legacy Codex checkpoints remain available. Never inspect encrypted session
database files directly during development; use application APIs.

## Authority and progress

Authenticated intake preserves original user messages as immutable requirements.
The working agent can add finer acceptance criteria linked to an original
requirement, but cannot replace or weaken existing requirements. The Scout is
an advisory proposal, not an authority to change user scope.

`get_task_checkpoint` reads the current revision without invalidating it.
`update_task_checkpoint` is an internal Core ▸ Base tool available in Ask,
Plan, and Auto. It records dependency-checked steps, findings, decisions,
evidence links, per-requirement answer claims, and the concrete next action.
A small Ask task needs only one step; multi-part requests should introduce
linked criteria. Plan Overview remains the durable planning document, while
Task Progress is the only checklist. In Plan mode, planning-phase steps describe
investigation and saving the overview; detailed execution-phase steps preserve
the future implementation work and may remain pending. On Plan-to-Auto transition,
the first dependency-eligible execution step becomes active. Task Progress is
rendered directly from canonical state, not edited separately.

`present_plan` replaces the execution-phase slice with the submitted detailed
checklist while preserving statuses for matching IDs. Dependencies are optional
and enforced only when declared. Existing steps without a phase default to
planning. A legacy `plan-checklist` component is imported once as execution
steps and then retired.

`present_plan` (and its sibling `present_chat_component`) is a **Core ▸ Base**
tool wired by `app/tools/loader.py` with the always-on core — NOT part of the
optional Visualizer ability it once shipped in. Plan mode's completion contract
requires it, so it is locked on (Tier-1): no ability toggle, global deny, or
`allowed_tools` block can withhold it. Otherwise an agent whose Visualizer was
switched off could be put into Plan mode and then fail the turn with
`plan_document_tool_unavailable`, discarding the plan it had already written.
`update_task_checkpoint` and `get_task_checkpoint` are the read/write half of the
same machinery and live in the same place, locked on identically — they were
relocated out of the locked-on Context Control ability, where their permanence
was incidental rather than guaranteed.

The task system itself is OPTIONAL: it exists only when the agent's
`metadata.manager.task_progress.enabled` is on. Plan mode is offered to every
agent, so the two must not be coupled. With a checkpoint, `present_plan` also
merges the execution checklist into canonical Task Progress; with checkpoints
disabled it saves the standalone Plan Overview and still returns `status: "ok"`,
which is what satisfies the loop's `require_plan_document` completion gate. Plan
mode therefore completes whether or not an agent has task progress switched on.

Checkpoint revisions reject stale updates. Runtime ownership checks prevent an
old initializer, reviewer, or MCP process from reclaiming a replaced turn.
The native working loop requires an initial request-linked checklist and active
step before work. It then admits at most three work attempts between checkpoints,
including calls refused by validation or later gates. Each checkpoint includes a
20–1200-character agent-authored summary of results, their implication for the
active step, and the next action. The summary is persisted as a normal progress
message and also appears in Task Progress. Prose alone does not reset the cadence.
Plan dependencies are optional: independent steps can run in parallel or out of
list order; only explicit depends_on edges enforce prerequisites.

Required checkpoint requests expose only update_task_checkpoint. Runtime
admission independently rejects other work even if a provider ignores tool_choice;
two unsuccessful required-checkpoint cycles stop with an explicit failure.
Checkpoint updates run alone, preventing sibling work from invalidating their
revision. In the native loop the runtime pins expected_revision to the exact
snapshot supplied for that inference, rather than trusting the model to copy a
number from history; a state change after that snapshot still fails CAS.
Checkpoint-only rewrites are bounded to three between work attempts, and two
failed checkpoint cycles stop as checkpoint_failed rather than automatically
restarting the same failure. Checkpoint summaries use normal sequenced DB events
before their progress event so reconnect and incremental history retain them.
A final checkpoint after the latest work is still required. MCP uses
the same initial-plan and three-outcome admission rule. Isolated
contract workers and anonymous plain-chat callers retain their existing
capability ceilings; browser-owned storage does not use the server SQL record.

Mutation intent is recorded before execution. Failed or interrupted outcomes
remain uncertain. Reconciliation requires fresh successful observations and
remains an agent claim until reviewed. Work revisions conservatively invalidate
evidence after later mutations. All mandatory criteria and steps must be covered;
agent claims alone never set the task to complete. Only a current, explicit
passing review with no structural gaps accepts completion. Missing, failed, or
inconclusive review leaves needs_work. Handoff capsules project canonical status
and next action when it exists.

## Shared context allocation

Context Control reserves instructions, current requirements/checkpoint, tool
schemas, and output headroom before allocating optional history. Compaction
summaries carry the webagent_history_summary marker; authoritative state carries
webagent_task_state and is never repeatedly rewritten by summarization.

Bounded tool-evidence policies apply to the current run too. Evidence referenced
by the active task may promote an earlier-task exchange within the same budget.
Unselected exchanges become retrieval receipts; raw stored history is preserved.
Final selection keeps tool-call/result groups together.

Mandatory state and the latest user message are never sliced to fit. An oversized
required packet raises ContextBudgetExceeded with an instruction to split the
step or increase its budget. Token accounting is an estimate, not the provider's
exact tokenizer. No smaller global context default is rolled out by this change.

Codex wrapper mode accounts for the current prompt, selected persona, WebAgent
MCP schemas, and an explicit allowance for CLI-owned instructions/tools. Resumed
native Codex threads remain CLI-managed; packet sizing does not claim to bound
their complete provider context. CLI-owned built-in tools also remain outside
the MCP operation tracker; their claims still require completion review.

## Secondary manager boundary

The manager receives allowlisted structured supervision packets: original
requirements, labeled agent plans/claims, dependency and evidence references,
qualified observations, unresolved findings, and the four most recent public
assistant progress updates (at most 800 characters each). Updates are untrusted
agent claims; the packet excludes raw tool calls, arguments, outputs and reasoning.
It does not receive the complete working transcript. Proposals
are represented by an operation category with scope unverified when the runtime
cannot classify exact scope.

Edit-review v3 workers are tool-free. Old reviewer sessions that may contain
raw context are replaced. Reservation identifiers used to fence pending calls
remain internal and are excluded from worker requests. Final acceptance and positive manager approvals must match the exact canonical
state revision. Negative advisory manager findings instead match turn, contract, work, and checkpoint revisions, atomically
when persisted: ordinary observations or public prose do not starve an in-flight
review; changed work, plans or user scope invalidate it.

The watchdog default checks every four iterations, within the existing review
budgets; explicit per-agent overrides remain respected. Negative findings become
durable obligations. Background opinions cannot retroactively block an executed
operation; pre-action checks enforce actionable planning and existing permissions.

Isolation reduces prompt-injection exposure; it is not immunity. Agent claims
and summaries can carry malicious text and remain untrusted data. Runtime tool
reports are observations, not independent verification. A content-isolated
manager can identify missing/stale evidence and contradictions but must return
inconclusive when the facts or operation scope cannot be established. Separate
bounded evidence reviewers retain their explicit verification role.

## Verification

Focused suites cover state reload with no working history, budgets at several
small sizes, persistent multi-part criteria, stale generation/revision rejection,
dependency validation, mutation freshness, uncertain-operation reconciliation,
allowlist exclusion, bounded evidence retention, close-review failure paths, and
MCP generation/operation tracking.

Runtime deployment caveat: inspect the actual development container command.
As observed on 2026-09-08 it uses four Uvicorn workers without --reload. Source
edits and container pytest runs do not update those already-loaded workers; a
deliberate development restart is required before claiming live verification.

Use the development container:
`docker exec webagent-dev python -m pytest -q tests/test_task_state.py tests/test_task_context_budget.py tests/test_manager_isolation.py tests/test_mcp_task_lifecycle.py`

A final real chat journey must use the attached Chrome profile and development
Test WebAgent under the journey_testing skill. Unit and integration tests do not establish
live model quality or browser rendering. Compare real task completion, omissions,
repeated work, false manager blocks, latency, and total tokens before lowering
production or development defaults.
