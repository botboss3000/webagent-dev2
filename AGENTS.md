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
