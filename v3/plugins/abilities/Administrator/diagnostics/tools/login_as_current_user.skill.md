Mints a session **as the signed-in user server-side, with no password anywhere** —
for diagnosing the running WebAgent app in dev when there is no active browser login.

It calls the same `create_access_token(username, user_id)` the login endpoint
uses and injects the JWT into the page's `localStorage.auth_token` (the key the
whole UI reads), then reloads and verifies via `/api/v1/auth/me`. Only the outcome
is returned — `username`, `user_id`, `url`, `verified` — never the token.

From then on, drive the app as the user with `browser_action`. With no `app_url`
it resolves this app's own local dev origin from `WEBAGENT_PORT` / the configured
hub port; pass an explicit origin only for a deliberately different host.

**Confirm-gated (destructive):** in Ask/Plan mode it pauses for approval before
minting a session as the user. Require `verified` to be true — and the stored and
active user ids to agree — before treating an empty state as evidence.
