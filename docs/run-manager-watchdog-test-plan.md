# Run Manager watchdog feature-test plan

This plan follows the repository's `webagent-feature-test` trajectory contract.
It separates deterministic loop safety from LLM judgment and requires evidence
from the user surface, event stream, persistence layer, and background-usage
records. An API response alone is not sufficient for a journey pass.

## Run contract

```yaml
id: run-manager-watchdog-supervision
title: Agent is redirected or safely stopped when work stops making progress
target:
  baseUrl: http://localhost:8080
  build: current-working-tree
profile:
  identity: isolated-admin-test-user
  browserState: isolated-disposable
  viewport: desktop
preconditions:
  - run_manager app function is enabled
  - a disposable agent has metadata.manager.watchdog enabled
  - the agent has max_identical_tool_calls=3 and max_stall_strikes=3
  - the test provider can return deterministic Manager verdicts
  - manager.max_checks=9, max_checks_by_kind uses plan=1/edit=4/watchdog=3/commit=1, and max_blocks=3
  - safe fixture tools can return constant success, controlled failure, and delayed results
trajectory:
  - open the agent chat
  - start a controlled task that repeats a safe tool call
  - observe the repeated call being blocked mechanically
  - observe a watchdog health event and Manager review
  - observe corrective feedback reach the active run
  - continue until the agent changes approach or reaches the configured safe stop
checkpoints:
  - id: loop-detected
    expected: the repeated call is not executed and stall_guard_loop is emitted
  - id: watchdog-triggered
    expected: one bounded Manager check receives structured run-health evidence
  - id: correction-delivered
    expected: actionable feedback is persisted and reaches the active run before its next inference
  - id: behavior-corrected
    expected: the agent changes approach, asks for input, or stops at the configured strike limit
  - id: reload-persistent
    expected: the Manager note and final run state remain correct after reload
measure:
  - interaction-to-feedback
  - watchdog-trigger-to-verdict
  - verdict-to-next-inference
  - server persistence
  - background Manager call count
  - console and HTTP errors
  - visual checkpoints
  - reload persistence
variants:
  - warm-cache
  - contract_chk-disabled-manager_chk-enabled
  - run_manager-app-function-disabled
exploration:
  seed: watchdog-20260821-a17f
  maxActions: 12
```

Planned run ID: `run-watchdog-20260821-001`. A real execution must record its
actual start and finish timestamps and must not reuse this ID for a rerun.

## Test cases

### TC-1 — Identical call loop

1. Configure the disposable agent with `watchdog.on_stall=true`,
   `cooldown_turns=2`, and a deterministic Manager `off_track` verdict.
   Expected: the saved configuration is returned by the authoritative agent API.
2. Cause three requests for the same safe tool with canonical-equivalent arguments.
   Expected: the third request is blocked before execution with
   `error_type=loop_blocked` and pipeline reason `identical`.
3. Inspect the Manager request capture.
   Expected: it contains `reason=identical_call`, tool name, signature count,
   total count, turn, and stall strikes; it does not contain raw tool output.
4. Observe the next model inference and reload the chat.
   Expected: the corrective Manager message is present in the active inference,
   exactly one durable `source=system:manager` row exists, and reload preserves it.

### TC-2 — Exact two-step oscillation without false positives

1. Request canonical calls `A/B/A/B/A/B` using two safe fixture operations.
   Expected: the sixth request is blocked with reason `oscillation` and evidence
   identifying the two tool names.
2. Repeat with the same tool-name sequence but different canonical arguments.
   Expected: no oscillation block occurs; legitimate multi-file exploration continues.

### TC-3 — Same-result and tool-budget loops

1. Make a safe fixture tool return the same normalized result three times, then
   request it again. Expected: the next call is blocked with reason `no_progress`.
2. Exceed a per-tool budget. Expected: the excess request is not executed and
   the Manager evidence reports `tool_budget` with the configured total.

### TC-4 — Sliding-window failures and escalation

1. Configure `on_errors=3`, `error_window=4`, and return outcomes
   `failure, success, failure, failure`. Expected: the fourth outcome emits one
   `watchdog_trigger` for `tool_error_cluster`; a success does not erase the
   sliding-window signal.
2. Produce further qualifying failures inside the cooldown. Expected: mechanical
   strikes continue to accumulate, but overlapping Manager calls do not.
3. Return three actionable blocking verdicts with `max_blocks=3`. Expected:
   the third block stops execution cleanly with a Manager escalation explanation.
4. Reach `max_stall_strikes`. Expected: execution stops cleanly, no further tool
   runs occur, and the user receives the safe-stop explanation.
5. Repeat using validation, contract, guardrail, and Manager-blocked failures.
   Expected: each failure class participates in the same health accounting.

### TC-5 — Background feedback reaches the current run

1. Delay an advisory Manager verdict until the main agent has begun its current
   tool execution, then complete it before the next inference.
2. Capture the messages sent for that next inference.
   Expected: one `[MANAGER WATCHDOG — OFF_TRACK]` system message appears before
   the model call, while the durable assistant-side note remains available for reload.
3. Return `on_track`. Expected: no corrective message is injected; only diagnostic
   verdict metadata is stamped on the anchor interaction.

### TC-6 — Gate correctness

1. Enable blocking `plan_gate` and `edit_gate`; attempt two separate edits.
   Expected: plan gate runs once and edit gate runs for each edit.
2. Repeat in Plan/Ask mode after explicit user confirmation.
   Expected: Manager gates still run after the confirmation guard.
3. Configure blocking `commit_gate` and return `block`.
   Expected: `commit_and_push` is not executed. The Manager request contains
   structured JSON with the proposed `commit`, `changed_paths`/change inventory,
   and a verification summary, but no raw tool output. The verdict only claims
   what that evidence supports; it does not claim an independently inspected diff.
4. Configure async commit gate.
   Expected: commit execution is not delayed, and actionable feedback appears later.

### TC-7 — Independence, budgets, and failure behavior

1. Disable `contract_chk` while leaving `manager_chk` enabled.
   Expected: mechanical contract rules are skipped but Manager supervision remains active.
2. Disable `manager_chk` while leaving `contract_chk` enabled.
   Expected: mechanical rules remain active and no Manager call is made.
3. Use the default per-kind caps and trigger two plan checks, five edit checks,
   four watchdog checks, and two commit checks. Expected: at most 1/4/3/1 calls
   respectively, with at most 9 calls overall; exhaustion in one kind does not
   consume another kind's reservation.
4. Override `max_checks` below the sum of per-kind caps. Expected: the overall
   hard ceiling wins even when individual kinds still have room.
5. Return missing/empty `reason`, or an actionable verdict with missing/empty
   `feedback`/`suggestion`. Expected: it is rejected as inconclusive and no
   malformed corrective note is injected.
6. Make both Manager attempts consume the configured operation timeout.
   Expected: their combined wall time is bounded by one 60-second total budget,
   the working action fails open, and the run remains responsive.
7. Turn off the `run_manager` app function during a run.
   Expected: later checks make no Manager model calls without affecting mechanical safety.

### TC-8 — User-visible and persistence regression

1. Run TC-1 from the actual chat surface while recording screenshots at loop
   detection, Manager correction, and safe completion/stop.
2. Capture browser console warnings, failed HTTP requests, websocket duplication,
   server diagnostics, and authoritative interaction rows.
3. Reload and reopen the session.
   Expected: messages appear once, in session sequence order; no stale running
   indicator remains; the final state agrees across UI, API, and database.

## Evidence and timing rules

- Browser evidence: screenshots plus visible message/pipeline state at meaningful checkpoints.
- Event evidence: websocket or streamed pipeline events, preserving reason, severity, and strikes.
- Persistence evidence: authoritative interaction rows, their `source`, metadata, parent, and session sequence.
- Usage evidence: background usage entries labelled `manager:<kind>`; use these to prove `max_checks`.
- `interaction-to-feedback`: user submit acknowledgement to first visible activity, measured in the browser.
- `watchdog-trigger-to-verdict`: server timestamp of the health trigger to completed Manager verdict.
- `verdict-to-next-inference`: completed verdict to the next provider request containing the correction.
- Tool-call wall time must never be reported as browser or application request latency.

## Evaluation

- `pass`: every required checkpoint passes at every named layer.
- `fail`: product behavior contradicts an expected result with usable evidence.
- `blocked`: credentials, deterministic provider control, fixture tools, or an evidence channel are unavailable.
- `not_run`: the case was intentionally excluded from that run.

Continue past non-blocking case failures. Stop if testing would touch a normal
browser profile, a non-disposable account, real source-control remotes, or an
unisolated production session.

## Observed run — `run-manager-mini-loops-20260821-002`

- Seed: `manager-mini-loops-20260821-b41e`
- Build: current working tree on 2026-08-21
- Overall result: `blocked`. The three deterministic journeys passed, but the
  required disposable live-chat, persistence, usage-ledger, console/HTTP, and
  reload evidence channels were unavailable. No product failure was observed.
- TC-1 gate/config/verdict journey: `pass` — 11/11 tests, 0.386 s.
- TC-2 watchdog health journey: `pass` — 3/3 tests, 1.795 s.
- TC-3 cap/timeout journey: `pass` — 3/3 tests, 3.388 s.
- Focused regression: `pass` — 41/41 tests in 6.34 s; 30 existing FastAPI
  deprecation warnings were retained.
- TC-4 blocking escalation journey: `not_run` — needs a disposable agent plus a
  deterministic Manager provider.
- TC-5 active feedback/persistence journey: `blocked` — do not infer delivery,
  uniqueness, usage booking, or reload persistence from source inspection.
- Raw JUnit artifacts are kept outside the GenUI data bag under
  `temp/feature-test/run-manager-mini-loops-20260821-002/`.

Project Dev now has a dedicated `Manager Parallel Mini-Loops` card. Its living
plan records the four trigger lanes, gate modes, verdict contracts, default
budgets, observed case results, source locations, and the blocked live journeys.
