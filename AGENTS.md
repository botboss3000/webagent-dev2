# Project notes for Codex

## Git / branching preferences

- Do **NOT** create or commit to worktree branches (e.g. auto-generated
  `Codex/...` branches) unless we've explicitly agreed to it and the user has
  given permission for that specific piece of work.
- The default expectation is to commit straight to `main`. Only branch when the
  user asks for it.

## UI work

- Before any UI update, optimization, or journey test, load the matching ability
  skill(s). To validate a change in the browser, the Diagnostics ability's
  `journey_testing` skill
  (`plugins/abilities/Administrator/diagnostics/skills/journey_testing.skill.md`).
  To build or change UI, use the applicable Codebase Admin UI skills
  (`ui_theming`, `ui_toggle_list`).
- For UI investigation, diagnosis, or change planning, use the Code Index
  ability (`plugins/abilities/Administrator/code_index/code_index.skill.md`)
  first. Confirm its coverage report has no gaps or stale relevant files; use
  its symbol, line, and static-edge lookups to identify affected UI code. Re-scan
  changed UI files before relying on the index again. The index guides source
  review; it does not replace direct source inspection or browser validation.
- For an end-to-end chat UI journey, also load
  `.agents/skills/webagent-feature-test/SKILL.md` and follow its trajectory and
  evidence contracts.

## V3 breadcrumb comments

- When changing v3 behavior, leave brief, plain-language breadcrumbs at the
  boundaries a new reader must follow: request admission, storage ownership,
  model context, policy and approval decisions, tool dispatch, recovery, and
  publication. Explain what happens and why the boundary exists.
- Put a comment beside a non-obvious invariant, failure path, or handoff between
  Rust and a worker. Name the authority that makes the decision and what the
  other component is allowed to do. Do not use comments to restate each line.
- Keep comments current when changing the code they describe. Update
  `v3/docs/code-tour.md` when a new actor, durable record, or major execution
  step changes the layman's route through the system.
- A reader should be able to trace a feature from its API through the loop,
  storage, and worker without assuming that a catalog entry means the feature
  executes. Document unsupported or deliberately deferred parts explicitly.

## V3 compilation and build coordination

- Treat a v3 Rust compilation as a checkout of its inputs. Before running
  `cargo check`, `cargo test`, `cargo build`, or another Cargo command against
  the live checkout, identify the inputs it will read (including `v3/src`,
  `v3/Cargo.toml`, `v3/Cargo.lock`, and any build scripts or generated source).
  Check the current Git status and relevant diffs, and coordinate with other
  active agents or people to confirm that no one is editing those inputs during
  the command. A clean or unchanged Git status alone does not prove inactivity.
- Coordinate use of the shared `v3/target` directory too. Do not overlap Cargo
  commands that write the same build output; wait for the other build to finish
  or use a separate target directory when appropriate.
- For `v3/deploy.sh build` or another image build, include its whole build
  context in the checkout: Rust inputs, worker and launch code, ability files,
  v3 UI assets, and the selected shared UI assets listed by the build script.
  Confirm that writers of those inputs have paused until the build has captured
  them. UI editing may continue during a Cargo-only check when those UI files
  are not inputs to that command. Live runtime data and session files may keep
  changing; they are not build inputs and must not be frozen or inspected for
  this check.
- Record the input state immediately before and after compilation, including
  relevant untracked files. If an input changed while the command ran, mark the
  result stale and repeat the check after the inputs are stable. When finished,
  tell the other workers the checkout is released and report what command ran,
  which inputs were checked, and whether its result is current.

## Shared V3 build coordination

For a V3 self-update task that uses automatic validation and deployment, Codex
and WebAgent share the protected host build broker. Codex runs
`python3 -B /home/ar/webagent-dev2/v3/plugins/abilities/Administrator/v3_build/buildctl.py`
through SSH on the development server. Use `status` to inspect open work;
`begin <stable-task-id> <summary>` before editing; `inputs` after source
review and checks; and `finish <task-id> --expected <inputs_sha256>` when
the task is ready. The last registered task to finish queues one tested
build and V3 development deployment. Poll `job <job-id>` for the terminal
result. An abandoned task can use `abandon <task-id>`; it does not deploy.
The same broker registry and build lock apply to V3 agent tools and Codex.

Only use `finish` when automatic V3 development deployment is authorized for
the task. A source digest detects edits outside the registry, but source writers
must still coordinate before starting a build. The CLI cannot choose a host
command, container name, image tag, or production target.

## V3 reducing build and validation time

- Work from narrow checks to expensive builds: run a quick, appropriately
  scoped compiler check after Rust control-flow edits, then affected tests and
  mounted-asset browser checks. Capture settled inputs before the full image
  build, then perform live backend acceptance after an authorized deployment.
  Keep the required build-script tests and final unchanged-input verification.
- Avoid starting full image builds while known backend edits or integrations
  are still pending. Coordinate the final source snapshot with other writers
  first, including configuration-dependent backend changes. An early build is
  provisional when its inputs may still change.
- Validate bind-mounted UI assets against the current development runtime
  before building where its APIs support the journey. Clearly distinguish UI
  checks from backend behavior that requires the new runtime; do not claim
  acceptance for undeployed endpoints.
- Before repeating a full image build, identify exactly what changed and
  whether it affects the runtime binary or packaged deployment artifacts.
  A change only to a confirmed bind-mounted UI asset normally needs a refresh
  and focused UI checks, not Rust recompilation. Still capture current inputs
  and reconcile packaged assets with the intended deployment.
- During UI review, trace visibility from configuration through all applicable
  CSS; check that completed-run data survives polling and reloads; and test
  focus/blur/click order in both empty and populated composers. Include keyboard
  activation and conversation switching where relevant.
- When compilation fails, inspect the first actionable diagnostic and the
  surrounding scope before rerunning broader checks. Check renamed variables,
  task handles, and cleanup paths after changing async control flow.
- When a concurrency test fails intermittently, preserve the failure evidence
  and distinguish a product defect, test isolation problem, or environment
  contention. Use a bounded isolated reproduction before a justified full
  rerun. A passing rerun does not erase the original failure; report it and
  track recurring failures separately without weakening valid assertions.
- Record start/end times for focused checks, full tests, release compilation,
  image assembly, and browser acceptance when assessing delays. Separate
  machine build time, troubleshooting, source coordination, and browser work;
  report overlaps rather than adding concurrent intervals together.
