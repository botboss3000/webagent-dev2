# Codebase Admin — Execute, Verify, Finish

Load this skill before reading, changing, debugging, or extending the WebAgent repository.

The guidance here is mostly the **operating contract** — how to work in this repo — and it
spans every tool, so it stays at this level. Each tool below additionally carries its own
mini-skill, delivered when that tool becomes callable.

## Operating contract

- Treat requests to fix, implement, change, investigate, or verify as work to complete in the active execution mode. Do not replace requested work with a proposal.
- Give a plan only when it materially helps: one sentence or at most three bullets, once. Start the first inspection or implementation immediately afterward.
- Never repeat, reword, or elaborate a plan while waiting. If blocked, state the exact failed action, missing fact, or approval needed and the smallest next action.
- Prefer checked evidence to narration. Do not claim a cause, test result, or completion without verifying it.
- Stop when the requested result is complete. Do not extend scope with speculative refactors or optional work.

## Work loop

1. Read `CLAUDE.md`, then only the routed guide relevant to the area being changed.
2. Locate the narrowest target with `search_source`, `search_comments`, a symbol lookup, or a targeted read. Do not scan whole trees or large files without a reason.
3. Form one testable hypothesis. Batch independent reads and searches.
4. Make the smallest coherent change that solves the request and preserves surrounding conventions and unrelated user changes.
5. Verify proportionally: inspect the diff, then run the narrowest relevant import, test, lint, API check, or UI check.
6. Report outcome, files changed, verification, and any genuine limitation concisely.

## Progress and recovery

- On a failed tool call, read the error and retry once only when the correction is clear. After two failed attempts, change approach or report the blocker; never loop.
- If two searches do not narrow the target, inspect the nearest owning module or ask one focused question.
- Trace caller, callee, data flow, and relevant tests before changing a multi-file path. Avoid speculative rewrites.
- For a diagnosis, report: observed evidence → likely cause → confidence → next action.

## Safety and approval

- Read/search/list/status/diff/log and non-mutating verification commands are inspection. Run them without asking for approval.
- Classify an operation by its effect, not its name. A command is not destructive merely because it is a command.
- When a persistent mutation requires approval under the current mode or tool policy, describe the concrete effect once and wait. When approved, execute it rather than replanning it.
- Never delete, overwrite, or reset files merely because backups may exist. Verify the exact target and need first.
- Bind-mounted workspaces may show a numeric file owner or group different from the runtime user. That is expected and is not evidence of a permission problem. Judge access by effective writability (`test -w`, `os.access(..., os.W_OK)`, or the result of the normal targeted edit), not by `ls`/`stat` ownership alone. When the target is writable, use the ordinary source-editing tool without announcing the UID/GID difference, switching to an atomic-replacement workaround, or changing ownership or mode. Report a permission blocker only after the intended write returns a permission error and an effective-access check also fails.

## WebAgent conventions

- New integrations, channels, abilities, schedulers, and similar capabilities are drop-in files under `plugins/` with the repository’s `FEATURE` metadata pattern; do not add central registries or dispatch branches for them.
- Use `search_comments` as an index, then read surrounding source before relying on a hit.
- For UI work, load the relevant UI skill when needed: `ui_theming` (palette, borders,
  surfaces) or `ui_toggle_list` (ability tables / config pages). Support both themes
  with design-system variables, and retain only comments that accurately describe
  current behavior.
- For validating a UI change in the browser, load the Diagnostics ability's
  `journey_testing` skill (resolve the route first; see its "Resolve the route
  first" section).
- For chat loading/streaming, compaction, session changes, interruptions, or
  recovery, follow that skill's chat UI journey and requirements-learning
  section. Also use the trajectory and evidence contracts in
  `.agents/skills/webagent-feature-test/SKILL.md`.
- **User-named reference implementation is binding.** When the user says to "look at," "use," "match," "mirror," or otherwise names another page, component, flow, or file as the reference, inspect its concrete markup, styles, behavior, and lifecycle before proposing or editing the target. Treat the reference as an acceptance input, not high-level inspiration.
- Before the first target edit, state the specific reference elements or behaviors being reused and identify any deliberate divergence required by the user's request. Do not substitute a broad search result, a visual impression, or a similarly named surface for the named reference.
- For a UI change based on a reference, verify the completed target against the named reference's relevant behavior and the user's requested differences. If the reference cannot be inspected, stop and report that blocker; do not implement from memory or a generic approximation.
- Search relevant consistency markers when applicable: `SISTER-PANEL`, `RENAME-FIELD PATTERN`, `CAROUSEL-WIRING PATTERN`, and `PREVENTIVE-COMMENTS`.
- Reuse `ui/shared/js/dom-utils.js` utilities instead of copying helpers. For intentional duplication, document and synchronize every marked copy.
- Every frontend diagnostic log must start with `DEBUG-TAG:` and be removed when no longer useful.
- **Chat footer / widget / panel edits are config-first.** When the user wants to edit the chat footer, chat widget, or chat panel, open `data/config/chat_ui.json` FIRST and make the change there — that file governs those surfaces (header, footer, chat pill, sizing/layout), and hard-coded CSS/JS only provides structural defaults. Only touch hard code when the requested change genuinely cannot be expressed in `chat_ui.json`.
- **`plugins/engines/` is special and off-limits by default.** It hosts the Claude Code and Codex CLI engine adapters (`claude_code/`, `codex/`, plus `terminal_chat/`) and is NOT part of the normal WebAgent repo. Unless the user explicitly asks to work on the codex or Claude agents, do not read, search, or modify anything under `plugins/engines/`.

## Repository map

- Backend and agent loop: `app/`; API routes: `app/api/`; prompt/agent logic: `app/agent/`; storage: `app/db/`.
- Plugin abilities and their tools: `plugins/abilities/`.
- Chat UI: `ui/chat/`; shared UI: `ui/shared/`; admin tools: `ui/admin-tools/`.
- Default agent templates: `app/defaults/agents/`.

## Completion

- Review the final diff; run the relevant verification; do not claim success on an unrun or failing check.
- Update repository documentation only when the change affects documented behavior, structure, configuration, or usage.
- Do not create scratch reports or helper artifacts in the repository.
