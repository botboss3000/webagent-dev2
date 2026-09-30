# WebAgent v3 UI journeys

These routes belong to the independent v3 application and must use its development origin. Do not use the v2 route registry to navigate v3.

- First-run setup: `/setup.html`. When no v3 admin exists, verify the v2-style Welcome to WebAgent card, username and two password fields. A completed setup redirects to `/app`; revisiting `/setup.html` redirects to `/app` without exposing setup again. The v2 bootstrap-code field is omitted because v3 has independent storage.
- Agents and Model: `/app?tab=agents&agent=shared_default`. Authenticate as the v3 development admin when prompted. Verify the v2 Agents carousel, selected WebAgent card, Model tab, ChatGPT Subscription connection row, and model field. Save a model ID and reload to confirm persistence.
- Chat: `/app?tab=agents&agent=shared_default`. Verify the v2 split shell and chat composer. Connect a fresh ChatGPT subscription, create a conversation, send a message, and reload its `session_id` route to confirm transcript recovery.
- Existing conversation: `/app?session_id=<known-v3-session-id>`. Derive the identifier from the v3 API or conversation selector. Navigate directly; verify the selected conversation, its history, and the same query parameter after reload.
- Provider and policies: `/app?tab=agents&agent=shared_default`. The Model page shows ChatGPT subscription status and verification code. Expand **Tool policies** to inspect six policy selectors. Initiating provider authorization must show a verification link and user code; completing it requires the user's account authorization.
- Approval: open the exact conversation route for a run waiting for approval. Verify the tool name and complete arguments, then approve or deny the known development fixture. Reload while waiting to verify the gate persists.
- Recovery: open the exact conversation route for a deliberately interrupted fixture. Verify the uncertain outcome warning and Accept known result, Authorize retry, and Abandon run controls. Never retry an unknown external effect without reconciling it.
- Shutoff: activate shutoff on a development fixture. Verify send is disabled and controls remain accessible. After backend restart, verify shutoff remains active. Clearing it must leave stopped runs waiting for explicit resume.
- Themes and mobile: exercise chat and expanded control panels in both themes and a narrow viewport. Verify text, borders, composer, approval arguments, and recovery actions remain legible and reachable.

Capture the final URL and visible checkpoint. Browser mutations must remain scoped to v3 development fixtures; do not access production or import v2 session data.
