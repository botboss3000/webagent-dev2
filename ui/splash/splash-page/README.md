# Guided setup landing page

The splash plugin is the first-visit setup experience served at `/`. The app
itself remains at `/app`.

The page now guides a visitor through:

1. WebAgent's problem/solution introduction.
2. Choosing a product tour, agent-creation flow, or instance-creation flow.
3. Describing the agent with rotating, path-specific examples.
4. Selecting from the live agent-abilities catalog (all selected by default).
5. Choosing private/anonymous access and the embed-widget default.
6. Reviewing the agent-prompt and GenUI workstreams before continuing.

The product tour is the default path. Agent creation opens the guided builder,
while instance creation switches to a compact deployment-target chooser.

## Handoff

`Continue with the default agent` writes the assembled build brief to the
existing `webagent.chatDraft.v1` composer-draft key and opens `/app`. The normal
chat boot restores it into the default agent's composer, where the user can
review and send it. This deliberately uses the existing draft recovery path so
the setup never races authentication or session creation.

The self-host flow stores the chosen target in
`sessionStorage["webagent.onboarding.deployTarget"]` before opening the app. It
is ready for the Instances deployment page to consume when deep-link navigation
is added.

## First-visit controls

The server reads the `wa_seen_splash` cookie. The final checkbox makes that
cookie persistent for one year and mirrors it in
`localStorage["webagent.splashSeen.v1"]`; skipping creates a session cookie.
The existing app-wide splash setting and account preference remain unchanged.

## Files

- `splash-page.html` — the setup stages and controls.
- `splash-page.css` — responsive, design-token-based presentation.
- `js/splash-landing.js` — state, catalog loading, progress, branching and handoff.
- `js/splash-page.js` — app-shell preference API.

To retest, clear `wa_seen_splash` and `webagent.splashSeen.v1`, then load `/`.
