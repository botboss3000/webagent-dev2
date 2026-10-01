# Login as the trusted v3 development administrator

Use `login_as_current_user_v3` from V3 Build before validating the independent v3 app. Load V3 Build's authenticated-browser playbook for setup and safeguards. This is a privileged session-creating action, confirm-gated in Ask/Plan; it does not bypass ownership or permission checks.

Provide a relative `path` resolved from `v3/docs/ui-journeys.md` (default `/app?tab=account`). Omit `browser_session_id` for the run's shared tab. Origin and caller mapping are server-configured; neither accepts a tool override.

Require `status=ok`, v3 application, server backend, and verified HTTP 200 with `user_id=admin` and `is_admin=true`. Use Browser Control `browser_action` with the returned browser-session ID to read, exercise, reload, and visibly assert the target. Capture and share the final screenshot. Do not use the v2 development-browser facade. Close the browser after testing. Authentication expires after ten minutes.

Missing bridge configuration/deployment is a blocker, not proof the UI is broken. Never request, print, log, or store the bridge secret or session cookie; do not substitute v2 tokens or the messaging API token.
