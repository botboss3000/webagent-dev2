Resolves a **direct deep link** with current-user ownership verification, without
driving the browser first. Pass a relative `path` (default `/app`) and, for a
chat, the `chat_session_id`; it returns the exact route (for example,
`/app?session=…`) and confirms the session belongs to this user.

Prefer this over clicking through the application to reach a test target, then
open the resolved route with `dev_browser_action(action="open", path="…")`.
