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
