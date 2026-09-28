---
{
  "id": "browser_journey",
  "display_name": "Browser journey for UI changes",
  "description": "Require a fresh browser journey proving a UI change on the changed route.",
  "phase": "completion",
  "status": "stable",
  "order": 50,
  "view": {"key": "require_browser_journey_for_ui_change", "kind": "bool"},
  "params": {}
}
---
Treat a UI-facing source mutation as invalidating earlier browser proof, so completion requires a fresh post-change journey: navigate to the affected interface, exercise the changed workflow or state, and capture a user-visible assertion with get_text, get_html, or screenshot — using browser_action or the development browser (dev_browser_action), whichever surface this session has. Unit tests, diffs, source reads, and isolated screenshots without navigation do not by themselves prove the user journey. When the user's concern names visible controls, content, or a visible state, the journey must create or use the necessary safe fixture and assert that exact user-visible condition; a geometry-only assertion, or a journey with no target control present, does not verify the user's stated requirement and must be reported as unverified rather than complete. A failed switch to another browser backend is scoped to that attempted backend and cannot waive the journey on the backend that remains selected; only a fresh failed browser action against the active backend may be disclosed as the unverified blocker, and a failed action is never passing evidence. If headless evidence cannot inspect the required state, probe the local backend when appropriate; a local-device failure is not a browser failure, so revalidate the headless Playwright backend and then use a safe development fixture or another scoped method. Report exactly which scope lacks the required data or capability, and review persisted ui_verification evidence when auditing or closing a run.
