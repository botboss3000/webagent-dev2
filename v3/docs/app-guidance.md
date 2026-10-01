# Application optimization guidance

This document records findings from monitoring WebAgent v2 session
`607a9eb2-7c27-479d-9089-6c0d91505632` on 2026-09-30 and translates them
into guidance for v3. The request in that session was to add minute/hour time
intervals to the model usage chart.

These are verified v2 observations and proposed v3 requirements, not a claim
that v3 has the same defects. V3 uses a Rust runtime and an isolated Python tool
worker. Validate each recommendation against the active v3 path before changing
it. Preserve approval, encryption, ownership, ordering, and recovery guarantees.

## Observed attempt

The attempt ran from approximately 12:20:46 to 12:27:02 UTC. The agent made the
chart edits, but its application-worker restart interrupted execution before
final minute/hour verification. V2 recovery published a terminal failure.

| Measurement | Observed value |
| --- | ---: |
| Completed model calls | 32 |
| Cumulative model-call time | 342 seconds |
| Cumulative tool execution time | 17 seconds |
| Input tokens summed across calls | 1.91 million |
| Cached input tokens summed across calls | 1.02 million |
| Recorded interaction-write latency | 15 ms median; 190 ms maximum |
| Execution events | 651 |
| Session event-endpoint requests in the inspected logs | 1,324 |
| Median gap between event-endpoint requests | 82 ms |

Token totals count repeated context across requests. Model and tool durations
are their recorded execution boundaries, not browser rendering latency.
The observer generated three event-endpoint requests; snapshot/activity polling
was separate. HTTP access records did not identify individual browser tabs.

Backend preparation took approximately 574 ms. One model call took 92.6 seconds
to produce the edit command; the command itself completed in under a second.
No database-lock bottleneck was established.

## 1. Make restart workflows preserve task progress

**Evidence:** The agent called `restart_server` to activate its Python edit.
V2 then marked its consumed run failed. This was consistent with its recovery
policy, which avoids replaying side effects with uncertain outcomes.

**V2 locations:** `app/chat/repository.py` (`recover_orphaned`),
`app/chat/recovery.py`, and the restart tool's instructions.

**V3 guidance:**
- State the interruption and recovery consequences in lifecycle-tool guidance.
- Persist completed work, tool outcomes, and remaining verification before a
  planned restart.
- Resume proven read-only verification through the appropriate recovery path.
- Hold uncertain mutations for outcome review; never replay them automatically.
- Verify readiness before continuing, and recheck tool policy and identity.

**Acceptance:** A planned restart does not lose the verification plan, duplicate
a mutation, or falsely report task completion. V3 already documents read-only
restart recovery; test this workflow against that implementation.

## 2. Eliminate rapid polling for unchanged run state

**Evidence:** V2's event endpoint returned immediately whenever a run was active,
including when there were no new publications. Its client reconnected with zero
delay after success. Three bounded probes returned unchanged state in about
15 ms each. The inspected logs contained 1,324 event requests.

**V2 locations:** `app/chat/api.py` (`events`) and
`ui/chat/core/transport.js` (`follow`).

**V3 guidance:** Use a persistent stream, bounded long polling, or paced polling.
Wait for new publications or a changed run state rather than returning the same
active state immediately. Bound retry rates and cancel inactive subscriptions.
Consider sharing a subscription across tabs where practical.

**Acceptance:** During a long model call, unchanged state does not cause a tight
request loop. Reconnects replay from the committed cursor without lost,
duplicated, or reordered publications. Measure requests per minute and delivery
latency together.

## 3. Reduce tool discovery and source-navigation turns

**Evidence:** The agent loaded tools individually, guessed two nonexistent source
directories, and read overlapping file ranges repeatedly. Most model calls took
several seconds even when the resulting tool completed in milliseconds.

**V3 guidance:**
- Supply a compact current repository map and capability summary.
- Load the relevant permitted tool bundle together.
- Resolve the owning UI bundle through the source index or route registry before
  guessing paths.
- Batch independent searches and reads; keep dependent operations sequential.
- Reuse evidence already returned and request only missing ranges.
- Provide a bounded environment preflight identifying available verification
  commands and writable cache locations.

**Acceptance:** A comparable task reaches the owning UI and backend code with
fewer discovery-only model calls, without skipping required instructions,
approval checks, or verification.

## 4. Keep prompt context relevant and stable

**Evidence:** The first model request contained approximately 43,000 input tokens;
later requests reached about 74,000. Diagnostics reported a system prompt of
roughly 167,000 characters. Existing v2 changes defaulted more ability skills to
always-on, a plausible contributor rather than a proven sole cause.

**V2 locations:** `app/agent/ability_skills.py` and the prompt assembly path.

**V3 guidance:** Keep essential operating rules visible and load task-specific
instructions on demand. Avoid duplicate instructions. Measure context by
component: core rules, skills, schemas, history, attachments, and tool output.
Preserve stable prefixes where supported and measure actual cache hits.
Bound large tool results and summarize complete older turns safely.

V3 already documents a 256 KiB serialized-context compaction threshold. Do not
treat compaction as a substitute for controlling the initial prompt and schemas.

**Acceptance:** Compare initial and peak tokens, cache-hit ratio, model latency,
turn count, and task correctness on the same workload. Cached tokens alone do
not establish efficient execution.

## 5. Make tool failures structured and actionable

**Evidence:** Two `search_source` results said that the path did not exist while
activity recorded `error: false`. Commands encountered missing `rg`, missing
Node, and a Python bytecode-cache permission error while their overall exit
status was zero.

**V3 guidance:** Return a consistent structured outcome with success, error
type, recoverability, exit status, and relevant stderr. Distinguish invocation
success from command or domain-operation success. Preserve failures of required
subcommands instead of allowing the last successful command to mask them.
Use an accurate capability summary to choose working alternatives.

**Acceptance:** Missing paths, unavailable executables, permission failures, and
nonzero command exits appear as failures in agent context and diagnostics.
Guidance leads to a corrected call or an explicit limitation, not unchanged
retries or false verification claims.

## 6. Reduce storage work without weakening durability

**Evidence:** V2 journaled non-stream execution events individually. This attempt
produced 651 events, alongside interaction writes. Recorded interaction writes
were relatively fast; the stronger measured concern was excessive request and
transaction volume.

**V2 locations:** `app/chat/runtime.py` (`Execution.observe`),
`app/chat/repository.py` (`journal`), and `app/db/local.py`.

**V3 guidance:**
- Batch low-priority diagnostics where bounded buffering is acceptable.
- Keep durable tool boundaries, approvals, publications, and recovery checkpoints
  atomic and promptly committed.
- Reuse connections safely and avoid repeating expensive initialization on hot
  paths when measured evidence supports it.
- Keep blocking storage work away from the async request/runtime loop.
- Measure connection setup, query time, transaction time, lock waits, encryption,
  checkpointing, and queue delay separately.
- Validate indexes against active v3 query patterns before adding them.

**Acceptance:** Reduce transaction and connection volume while preserving
crash recovery, ownership checks, append-only event semantics, and publication
ordering. Inspect session state only through authenticated APIs, never by
opening session database files directly.

## 7. Make the browser cache incremental and bounded

**Evidence:** V2's controlled-chat cache read, merged, and rewrote a session object
for each saved update. Cache persistence completed before the update was emitted
to subscribers. Large-history rewrite costs and quota-failure behavior are
source-backed concerns, not measured failures in this attempt.

**V2 locations:** `ui/chat/core/client.js` (`putState`),
`ui/chat/core/transport.js` (`save`), and `ui/chat/core/state.js`.

**V3 guidance:** Store publications separately from cursor and run metadata, and
update them atomically. Define retention, quota handling, and cache-unavailable
behavior explicitly. Preserve instance/user scoping, revision validation,
terminal-state monotonicity, and multi-tab consistency. Keep private execution
diagnostics outside the persistent public transcript cache.

**Acceptance:** Measure cache reads/writes and rendering with a long transcript.
Test quota exhaustion, interrupted transactions, offline reopening, identity
changes, and concurrent tabs. Persistence failures must be visible and must not
silently make committed server publications disappear from the user's view.

## Evidence limits and implementation order

No attached browser surface was available to the observer. IndexedDB timings,
browser memory, rendering latency, and cold/warm cache behavior were not measured.
The agent's own browser observations were available through its activity API,
but they do not establish browser-cache performance.

Prioritize restart-aware completion and excess polling, then discovery guidance
and prompt size. Correct tool failure reporting before trusting failure-rate
metrics. Optimize storage batching and browser cache layout using fresh v3
measurements.

This document authorizes no deployment, lifecycle operation, or production
change. It is a guidance record; implementation requires a separate task.
