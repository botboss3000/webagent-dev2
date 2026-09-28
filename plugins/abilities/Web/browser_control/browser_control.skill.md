# Browser Control — drive a stateful shared tab

Use this ability for JavaScript-rendered pages, persistent logins, forms, screenshots, or an external provider without a direct integration. For a public one-shot HTTP response, use `http_request` instead. The browser session is shared with the user’s Web view and preserves its own cookies.

## Choose the least powerful path

- Use `browser_action` for a page workflow: navigate, inspect, act, then inspect again.
- Use `web_session_status` first, then `web_session_fetch` or `web_session_graphql`, only when a logged-in cookie-replay request is the right fit. These actions act as the user and may change remote state.
- Use `browser_popup` only to hand a page to the user; it is not a substitute for the agent reading a page.
- Use `browser_backend()` with no mode to inspect backend availability. Prefer headless; use `server` for headed server rendering and `local` only for a user-device login, CAPTCHA, 2FA, explicit takeover, or explicit user request.

## Reliable loop

1. `navigate` to the page.
2. Read with `get_text(selector="body")`, `get_html`, or `screenshot` before deciding what to click. `get_text` requires a selector.
3. Make one action per call (`click`, `type`, or `evaluate`), then re-read the resulting state. Wait only when the page needs time to settle.
4. Confirm the final state with text, URL/title, or a screenshot.
5. Close with `browser_action(action="close")` when no live handoff is underway. Closing saves state; leave it open only for an explicit user handoff, 2FA, or imminent continuation, and say so.

## Testing WebAgent itself

For a journey against this WebAgent application, do not treat an unauthenticated
headless page as a product result. Load Diagnostics' `login_as_current_user()`,
call it without an `app_url`, and require its verified, stored, and active user
IDs to agree. Check `browser_backend()` availability, then use
`browser_backend(mode="server")` for the headed server Chromium. Append the
relative route from `docs/claude/ui-journeys.md` to the returned local origin,
exercise the named visible control, capture an assertion, and close the session.
If Diagnostics or its login tool is unavailable, report that prerequisite as
blocked. `mode="local"` is only for a connected user-device browser, login/2FA/
CAPTCHA, explicit takeover, or an explicit user request — never use it merely
because a headless session is unauthenticated.

Omit `browser_session_id` to use the agent’s shared tab. A private or unshared tab is intentionally inaccessible; do not work around that gate.

## Credentials, cookies, and privacy

- First check whether a credential or session is available. `vault_login` reads saved credentials server-side and returns only an outcome; the agent never sees the email or password.
- `vault_login` is for supported single-step forms. If it reports 2FA, a challenge, or an unsupported multi-step login, move to the user’s real browser when available and ask the user to complete the login themselves. **Do not attempt to type vaulted credentials manually:** those values are never exposed to the agent.
- Use `cookie_allowlist` only when the user explicitly asks to remember or forget a domain. Provide a registrable domain, not a URL.
- Cookie-replay requests carry the user’s session and can have side effects. Check the method, URL, and requested effect before issuing one; never report a mutation as completed without its response evidence.

Respect site terms, user intent, confirmation gates, and the difference between a backend-scoped observation and global browser availability.
