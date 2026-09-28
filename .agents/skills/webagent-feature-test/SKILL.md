---
name: webagent-feature-test
description: Plan and run evidence-backed end-to-end feature journeys for WebAgent or another local app, including browser, API, persistence, performance, visual, cache, synchronization, gesture, and exploratory testing. Use when a user asks to validate a complete user experience or update Project Dev with durable QA knowledge; do not use for implementation or bug fixing unless separately requested.
---

# WebAgent feature testing

Turn the user's trajectory and the feature requirements into a reproducible test run. Validate the complete experience; an API success alone does not prove a browser journey.

## Before the run

1. Read [references/trajectory-contract.md](references/trajectory-contract.md).
2. Identify the target environment, identity profile, initial state, destructive or account-changing steps, and evidence channels actually available.
3. Refine the trajectory into test cases whose steps each contain an action and observable expected result. Ask only about unknowns that materially change the test.
4. Obtain any action-time confirmation required by the active browser or tool policy. Never clear or modify a person's normal browser profile to simulate a cold start; use an isolated disposable test profile.

## Execute

- Assign a run ID, case IDs, step IDs, and a reproduction seed before acting.
- Prefer the real user surface. Supplement it with API, Python, database, or log inspection when those observations prove persistence, authorization, synchronization, or failure cause.
- Record actual duration and the measurement boundary. Do not label tool-call wall time as application latency.
- Capture visual state at meaningful checkpoints when the selected surface supports it. If screenshots, network traces, browser performance entries, or another requested evidence channel are unavailable, record that channel as `blocked`; do not infer a pass.
- Continue past non-blocking failures. Stop when a required action needs confirmation, credentials or external coordination, or when continuing would corrupt the result.
- For gestures, record the source and target, path or semantic gesture, duration, release point, visible feedback, resulting state, and persistence after reload.

## Chat UI journeys

For WebAgent chat loading/streaming, compaction, session changes, interruptions,
or recovery behavior, load the Diagnostics ability's `journey_testing` skill and apply
its "Chat UI journey testing and requirements learning" section. Use the user's
attached
Chrome browser in a separate ChatGPT-controlled tab and preserve the user's
active tab and browser state.

Exercise the dedicated Test WebAgent through the visible chat UI with one small,
reversible task and one bounded sub-agent delegation when orchestration is
exposed. The browser controller owns actions, assertions, and the verdict.

Use the Render Recorder for ultra-fine logging when reproducing intermittent
glitchiness, render-order races, control flicker, scroll movement, or precise
timing defects. Start it before the reproduction and keep it through the
terminal/recovery state; recorder output supplements rather than replaces
visible Chrome assertions.

Before reporting completion, compare the user's chat UI requirements with the
guideline. If the request introduced a reusable requirement that is not already
covered, propose concrete testable wording and ask whether it should be added.
Do not promote it to the guideline without explicit user agreement, and do not
confuse an observed bug or harness limitation with a durable product rule.

## Evaluate

- A journey passes only when every required checkpoint passes across the required layers.
- Use `pass`, `fail`, `blocked`, or `not_run`. Never collapse blocked into failed or passed.
- Distinguish product failures from harness limitations and environment failures.
- Preserve console warnings, HTTP failures, stale-state flashes, duplicate requests, missing accessibility state, and timing-budget violations even when the user eventually completes the journey.

## Learn and publish

Read [references/learning-and-project-dev.md](references/learning-and-project-dev.md) when the user wants a living plan or Project Dev updates.

Improve the durable plan only from observed behavior, repository evidence, or user-confirmed requirements. Add newly discovered triggers, code locations, environment prerequisites, measurement limitations, and regression cases. Never turn one incidental failure into a universal product requirement.

Keep raw screenshots, traces, and logs outside the GenUI data bag. Publish small artifact references, metrics, step results, requirement coverage, the seed, and reproduction instructions. Prefer the host's GenUI data/update tools; otherwise use the repository's documented Project Dev adapter. Do not invent a write path.

The workflow is host-neutral: Codex and WebAgent may use different browser or shell tools, but both must consume and emit the same trajectory and run-result contracts.
