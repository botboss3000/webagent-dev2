**The mandatory development-browser tool for any WebAgent UI task.** Use
`dev_browser_action(action="open", path="/app?tab=…")` as the first browser
call. It creates the current run's isolated browser session, signs it in as the
current user, switches to server-backed Chromium, and verifies identity after
the handoff. Its result carries local origin and opaque session metadata only —
never a token.

After opening, use the same tool for `navigate`, `click`, `type`, `get_text`,
`get_html`, `screenshot`, `wait`, `evaluate`, `title`, `url`, and `close`. For a
chat, pass `chat_session_id` to `action="open"` so that session is preloaded.

Do not use `run_python`, shell commands, Playwright APIs, Chromium/Chrome
executables, generic Browser Control, local/device browsers, or an
authenticated headless browser to discover, launch, or drive a WebAgent UI
journey. This tool is the single supported browser surface and fails closed
with an actionable blocker. Use an exact registry route from
`docs/claude/ui-journeys.md`, and always close with
`dev_browser_action(action="close")` on every terminal path.
