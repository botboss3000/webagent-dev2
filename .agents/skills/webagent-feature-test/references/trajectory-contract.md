# Trajectory contract

Use this logical shape. YAML or JSON are both acceptable.

```yaml
id: guest-signup-free-chat
title: Anonymous visitor signs up and starts a free chat
target:
  baseUrl: http://localhost:8080
  build: current-working-tree
profile:
  identity: guest
  browserState: isolated-cold
  viewport: desktop
preconditions:
  - registration mode allows self-service accounts
trajectory:
  - open the public app
  - choose sign up
  - create a free account
  - enter the default chat
  - send "hi"
  - observe the response
checkpoints:
  - id: usable-public-page
    expected: public page is visibly interactive
  - id: account-created
    expected: authenticated free account is active
  - id: message-accepted
    expected: "hi" appears once as the user's message
  - id: response-complete
    expected: one assistant response completes without an uncaught error
measure:
  - navigation-to-usable
  - interaction-to-feedback
  - request latency
  - server persistence
  - console and HTTP errors
  - visual checkpoints
  - reload persistence
variants:
  - warm-cache
  - returning-account
exploration:
  seed: generated-before-run
  maxActions: 0
```

## Required run output

```json
{
  "runId": "run-...",
  "trajectoryId": "guest-signup-free-chat",
  "seed": "...",
  "environment": {},
  "startedAt": "ISO-8601",
  "finishedAt": "ISO-8601",
  "result": "pass|fail|blocked|not_run",
  "cases": [
    {
      "id": "TC-1",
      "result": "pass|fail|blocked|not_run",
      "steps": [
        {
          "id": "TC-1-S1",
          "action": "...",
          "expected": "...",
          "actual": "...",
          "result": "pass|fail|blocked|not_run",
          "timing": {"valueMs": 0, "boundary": "..."},
          "evidence": [],
          "issues": []
        }
      ]
    }
  ],
  "metrics": {},
  "artifacts": [],
  "limitations": [],
  "reproduction": {}
}
```

## Measurement rules

- `navigation-to-usable` ends when the primary intended control is visible and enabled, not merely at DOMContentLoaded.
- `interaction-to-feedback` ends at the first meaningful visible acknowledgement.
- `request latency` comes from network or server timing, not the agent tool-call duration.
- `server persistence` requires an authoritative server/API/database observation.
- `sync delay` begins at authoritative write acknowledgement and ends when the other client displays the new state.
- State whether a browser was genuinely isolated-cold, cache-cleared, warm, or unknown. A new tab alone is not a cold profile.
