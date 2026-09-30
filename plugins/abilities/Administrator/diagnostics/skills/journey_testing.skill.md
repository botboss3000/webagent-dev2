---
id: journey_testing
title: Journey Testing
when_to_use: Use when validating a user-facing WebAgent change in the browser — resolving the target route, exercising the workflow, and asserting the exact control, content, or state the user named.
default_mode: visible
minimum_access: registered
---

# Journey Testing

Use this skill to **validate** a user-facing change in the browser — resolve the
target route, exercise the workflow, and assert the exact control, content, or
state the user named. This is the journey contract only.

Guidance for **building or changing** UI source lives in the Codebase Admin
ability's `ui_theming` (palette, borders, surfaces) and `ui_toggle_list` skills.
Load either when applicable to author the change; use this skill to prove it.

## Required journey

After the final UI source change, navigate to the affected route in the
authenticated development browser. Exercise the changed workflow and assert the
exact control, content, or state the user named. Source inspection and unit tests
support the result but do not replace the browser journey. Report the route,
actions, visible assertion, browser scope, and result; if a browser check fails,
report the failed scope and exact limitation rather than claiming completion.

### Resolve the route first — never click through the app

Find the target's URL before opening a browser; navigating by clicking tabs is a
measured waste. Three steps:

1. **Resolve the route from the registry** `docs/claude/ui-journeys.md` — each
   entry gives the stable route, owner bundle, and checks. If the target is not
   listed, derive it from the bundle's own JS before opening anything:
   `grep -n "replaceRouteParams({\|searchParams.get(" ui/<bundle>/**/js/*.js`.
2. **Open it directly, pre-authenticated** — pass the route to
   `dev_browser_action` with `action="open"` and `path="/app?tab=…"`. App subviews
   are `/app?tab=app&view=<id>`; for a chat, use
   `dev_browser_url(path="/app", chat_session_id="…")` (or open `/app?session=…`
   directly). Do not hand-navigate to the page and then hunt for the control.
3. **Assert the named control**, then reload once to prove the route re-opens in
   the same state.

## Structural layout checks

When a requirement specifies a relationship such as below, inside, or separate
from a header or toggle, use a DOM assertion that proves that relationship. Do not
substitute a visual approximation or geometry-only measurement.

## Chat UI journey testing and requirements learning

This section governs chat UI work only: loading and streaming, compaction,
session changes, interruptions, and their recovery states. Other UI work still
follows the build conventions in the Codebase Admin UI skills, but does not inherit
these chat-specific journey requirements automatically.

### Journey surface and workload

- Run the final journey in the same attached Chrome browser family and profile
  the user is using, but in a new ChatGPT-controlled tab. Do not take over,
  navigate, reload, or close the user's active tab.
- Test `https://dev.webagent.live/app`. Source, container, API, and log checks
  remain on the development server. Never substitute production or a laptop
  source checkout.
- A separate tab is not a cold profile. Label cache state accurately. If
  attached Chrome control is unavailable, mark the journey `blocked`; do not
  silently substitute another browser for final acceptance.
- Exercise the dedicated Test WebAgent through the visible chat UI with a small,
  relevant, reversible task. Require exactly one bounded sub-agent delegation
  when the tested flow exposes orchestration, followed by a short synthesized
  parent response.
- The ChatGPT browser controller performs the actions and owns every assertion.
  The Test WebAgent supplies realistic workload and visible orchestration
  evidence; it never decides whether the journey passed.
- Keep the Test WebAgent and its sub-agent away from source edits, deployments,
  service lifecycle actions, secrets, account changes, destructive data
  operations, and unrelated exploration.

### Render Recorder

The Render Recorder is available for ultra-fine UI logging when a chat defect is
intermittent, visually glitchy, timing-sensitive, or difficult to explain from
screenshots alone. Use it for precise troubleshooting of render order, streaming
updates, duplicated or disappearing elements, scroll movement, session
transitions, control flicker, and layout shifts.

- Start recording before the first reproduction action and keep it active
  through the final, error, interruption, reload, or recovery state.
- When the user requests a full-session investigation, remove the time/data
  cutoff and preserve recording across session and agent switches.
- Correlate recorder timestamps and render events with the journey's run, case,
  and step IDs. Use the recording to explain what changed and in what order.
- Recorder evidence supplements visible Chrome assertions; it does not replace
  the real user journey or independently establish a pass.

### Loading and streaming

- After a user sends a message, show meaningful progress before tool activity or
  response content.
- Maintain one activity group for each uninterrupted run segment. Its tool,
  message/update, mode, response, and compaction counters must reflect the
  events that actually occurred and must not split into duplicate groups. Each
  collapsed counter must equal the semantic rows revealed by opening that
  activity group and update whenever those rows change; it must never report
  zero tools while tool rows are present. Completed historical groups must be
  projected from the same durable interaction sequence as their lazy-loaded
  details, so a reload cannot show a different group or counter from the DB.
- Each newly submitted user message is the current transcript anchor. Keep it
  landed with its top 62px below the transcript viewport top while activity and
  the final closer arrive; never auto-scroll to the final response. Add whatever
  leading transcript clearance is necessary for the first user message to reach
  the same anchor without negative scrolling: at least 62px, and never less than
  the complete visible header stack plus 8px. An uncapped transcript-tail clearance
  is required to make that landing reachable rather than allowing the composer,
  footer, or normal tail padding to clamp it.
  Re-land the active anchor after keyboard, visual-viewport, composer, or footer
  layout changes. Those layout changes and focus/blur events are not reader
  scroll intent and must never release the anchor.
- Release the current-turn anchor only after an intentional downward scroll that
  begins inside the transcript. Once released, preserve the reader's chosen
  position except for the soft user-message snap described below. Wheel, touch,
  pointer, and transcript keyboard input may establish scroll intent; browser
  reflow or programmatic scrolling may not.
- Treat every rendered user message as a soft scroll anchor at the same 62px
  landing position. After an intentional transcript scroll in either direction
  settles, if the nearest user message's landing target is within 100px above or
  below the current position, pull smoothly to that exact target. Outside that
  +/-100px release zone, do not snap or constrain normal scrolling. Honor reduced
  motion by settling immediately instead of animating.
- Keep the current preview and progress note in a stable order. The preview must
  retain the complete finalized message without text truncation. A fixed visual
  height may scroll internally, but ellipsis, line clamping, slicing, or partial
  persistence must never discard preview content.
- Preserve chronological transcript order and scroll anchoring while previews
  grow or shrink. Streaming must not make bubbles jump, reorder, duplicate, or
  oscillate vertically.
- Keep Stop continuously visible for the active run without flicker, and clear
  it on every terminal path.
- End every run with exactly one closer. If closer generation fails, use the
  last meaningful working-agent response; if neither exists, show a loud,
  explicit user-facing failure rather than ending silently.

### Compaction

- Show `Compacting...` as a standard tool-style progress element in both the
  footer and transcript until the result arrives.
- Show one concise transient notice using the same values and compact-number
  formatting as the footer, keep it visible for two seconds, then remove it.
- Store the detailed result durably inside the run's collapsed activity group
  and increment the compaction counter exactly once.
- On reload, restore only the durable detail. Do not restore transient progress
  or the two-second notice.
- Manual, agent-tool, automatic, and supported engine-hook compactions share
  this presentation. No-op and error notices remain standalone and do not count
  as successful compactions.

### Session changes

- Hide the complete old session shell—identity, header, transcript, footer,
  menus, and actions—before revealing any target-session content.
- Apply the target agent/session's header and footer configuration while hidden,
  then reveal the correct shell and transcript atomically. Old identity and new
  content must never be visible together.
- Reuse compatible configuration and transcript caches for same-agent switches;
  allow cold transcript loading only after the correct target shell is visible.
- Measure target identity, configuration, and transcript phases independently.
  Do not call a switch fast based on tool-call wall time.
- When the Render Recorder is part of the test, keep it active for the full
  session without a time/data cutoff and across session or agent switches.

### Interruptions and recovery

- Preserve this visible order: original user message, current activity
  group/preview, interrupting user message, resumed activity group/preview, and
  one final closer.
- Do not duplicate an activity group within the same uninterrupted segment or
  lose already-rendered progress when ownership transfers.
- Typed `Continue` and the Continue button must produce the same canonical
  user approval. Automatic or watchdog recovery must not impersonate approval.
- Error, cancellation, empty-completion, reload, and reconnect paths must clear
  stale controls, preserve durable transcript content, and produce an explicit
  terminal outcome.

### Recognize new requirements

At the start of a chat UI task, extract the user's explicit observable
requirements and compare them with this section. During implementation and
journey testing, watch for additional user statements that define a reusable
chat UI invariant rather than a one-off preference.

When a durable requirement is new:

1. Finish the current in-scope work unless the decision is required to proceed.
2. Present the candidate requirement in concrete, testable wording and say which
   part of the user's request introduced it.
3. Ask once whether the user wants it added to this UI guideline.
4. Do not edit the guideline until the user explicitly agrees. A direct request
   to add or update the guideline already counts as agreement.
5. Treat observed bugs, implementation details, and harness limitations as run
   findings—not permanent requirements—unless the user confirms the rule.

Batch closely related candidates into one concise question. Do not repeatedly
ask about a requirement the user has already accepted or declined in the
current conversation.

### Evidence

Record action, expected result, actual result, and `pass`, `fail`,
`blocked`, or `not_run` for each required checkpoint. Verify the relevant
chat states in both themes, exercise pointer and keyboard behavior, inspect
focus and accessible state, and check for clipping, overlap, layout shift,
console errors, failed requests, and reload recovery. Use measured DOM geometry
when animation or visual noise makes screenshots unreliable. A journey passes
only when every required checkpoint passes.

