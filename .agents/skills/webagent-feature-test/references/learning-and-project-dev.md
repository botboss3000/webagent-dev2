# Learning and Project Dev updates

Update the living feature test after a run when the user requests durable learning.

## Promote durable knowledge

Add only facts supported by the run, source inspection, or a user-confirmed contract:

- verified entry points and user-visible triggers;
- required identity, feature flags, account state, and test data;
- reliable selectors or semantic targets;
- measurement boundaries and known harness limitations;
- newly discovered edge cases and regression trajectories;
- code and API locations that explain the behavior;
- artifact types required to prove a future pass.

Do not encode a transient timestamp, generated credential, one-off account ID, or tool-specific locator unless it is essential to reproduction.

## GenUI run events

Publish compact, idempotent events keyed by `runId`, `caseId`, and `stepId`:

```json
{
  "runId": "run-...",
  "caseId": "TC-1",
  "stepId": "TC-1-S2",
  "status": "running|pass|fail|blocked",
  "expected": "...",
  "actual": "...",
  "metric": {"name": "interaction-to-feedback", "valueMs": 0},
  "artifactRefs": [],
  "updatedAt": "ISO-8601"
}
```

Update the UI after meaningful checkpoints rather than raw mouse movements. The page should expose current progress, actual versus expected, metrics, artifacts, coverage, reproduction seed, and the distinction between product failures and harness limitations.

## Self-improvement boundary

After the run, propose or apply narrow changes to the stored plan:

1. preserve the prior version and append the run summary;
2. update coverage from actual results;
3. add a regression case for a reproducible newly observed failure;
4. record a harness limitation as a limitation, not a product requirement;
5. bump the plan version only when the durable plan changed.

Never modify application source while running a validation-only trajectory. A separate user request can authorize fixes after results are reviewed.
