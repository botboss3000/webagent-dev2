# Diagnostics — private V3 development browser

Rust owns current-caller authentication, live ability policy, approvals and sanitized run diagnostics. No tool accepts another caller, an arbitrary application origin, an authentication token, a database path or JavaScript.

Use `read_diagnostics` for the native bounded current-session/run diagnostic view. It does not open encrypted databases or expose raw model text, tool arguments, credential records or unrestricted installation logs.

## Authenticated browser

`login_as_current_user` opens a private Chromium profile for the caller's current chat session and verifies `/api/v3/auth/me`. Authentication uses a short-lived HttpOnly `v3_session` cookie minted by Rust and passed separately from model arguments. Tokens, cookies and headers never enter tool results. A new authorization generation closes the old profile; expired, idle or revoked profiles cannot be reused.

The browser origin is fixed to `https://dev.webagent.live`. The route is supplied as an exact relative `path`; only implemented app routes and bounded current-session read APIs are accepted. Selected agent and conversation IDs must match Rust's current agent/session. External, production, file and data origins are unavailable.

`dev_browser_action` supports only `navigate`, `read`, `click`, `type`, `screenshot` and `close`. There is no `evaluate`, arbitrary script, page HTML export or filesystem upload. Read the current page before each click or type. DOM changes, navigation, screenshots or previous input withdraw the observation and require another read. Private credential/password targets cannot be typed or clicked.

Reads return bounded sanitized visible text with `source_kind: untrusted_browser_observation`. Literal text input is bounded and never executed as script. Rust still decides whether a mutation requires approval.

## Images and recording

Screenshots are actual bounded viewport PNG files stored privately outside the coding workspace. Inputs, textareas, secret-marked nodes and exact authentication-cookie text are masked before capture. Results contain an opaque artifact ID, digest, dimensions and an authenticated current-session API download path; never a host path.

`dev_browser_record` has `start`, `status` and `stop`. It records at most 30 PNG frames over 30 seconds. The result is explicitly `bounded_png_frames`, not a video. Stop recording and close the browser after use. A screenshot quota or lease failure stops capture.

After uncertain input or timeout, inspect the current page before another action. Neither restart nor recovery replays clicks, typing or navigation mutations. Profiles are discarded rather than restored; the immutable interaction journal remains original evidence.

Use API validation when requested. This ability does not require a browser journey or force a screenshot for an API-only task.
