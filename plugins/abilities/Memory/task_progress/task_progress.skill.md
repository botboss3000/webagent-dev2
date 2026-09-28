# Task Progress — durable task authoring

Task Progress keeps one durable, evidence-backed record of the work underway. It
is not a substitute for the live task card: the card remains authoritative for
the current task, its checkpoints, evidence, blockers, and completion state.

## Author tasks for an observable outcome

- Name each task as a concrete outcome: **verb + deliverable + scope**. Prefer
  “Verify the Modes-page Task Progress gate persists the configured budget” over
  “work on Task Progress.”
- Keep one task to one verifiable outcome. Split tasks only when their success
  evidence, dependencies, owner, approval requirement, or delivery boundary
  differs. Do not manufacture tiny checklist items merely to appear active.
- Before starting, state what completion means and what evidence will establish
  it: a test result, a browser-visible assertion, an inspected diff, an API
  response, or user confirmation as appropriate.

## Choose priority and order deliberately

- The user’s explicit priority always wins.
- Otherwise order work by dependency first, then safety or approval gates, then
  risk, then the smallest valuable reversible unit.
- Keep a single active task. Queue later work rather than claiming multiple
  simultaneous active outcomes.
- Surface a real blocker with its exact missing fact or required approval. Do not
  hide it by marking the task complete or by replacing it with vague progress.

## Complete only from evidence

Use Task Progress to record material plan changes, meaningful progress, and
completion. When self-completion is allowed, complete only when the durable
record contains evidence that directly supports the stated outcome. A plausible
narrative, an intention to test, or an unverified implementation is not enough.

If evidence is incomplete, keep the task active, identify the narrowest next
action, and state the limitation plainly. Do not let historical evidence crowd
out the active task: Task Progress preserves the active task’s relevant tool
evidence first and retains completed-task evidence only within its configured
carryover budget.
