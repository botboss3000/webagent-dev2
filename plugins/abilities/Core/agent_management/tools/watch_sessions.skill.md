Arm, retarget, or disarm automatic session monitoring for THIS agent. A watch triggers the agent once whenever one of the user's sessions **settles** — a turn that was running stops. It never fires mid-run, so a long run yields one report at the end rather than one per step.

- `action="start"` arms it and points the updates at a session. The default target is the session you are speaking in, so say it from the chat the user wants notified. Arming again from another chat simply MOVES that pointer — there is one watch row per agent — so previous chats go quiet on their own. Never go hunting for stale watches to switch off.
- `action="stop"` disarms. The row is kept, so a later `start` restores it.
- `action="status"` reports the armed state, the notify session, the fire count and the last error. Read it before telling the user monitoring is on.
- `outcome="incomplete"` narrows the watch to turns that stopped involuntarily or errored; leave it blank to hear about every settle.

Two preconditions, both worth stating plainly if unmet: the agent needs the Automation ability enabled (otherwise nothing is delivered), and the ability-level `allow_session_watch` setting must be on. `start` warns about the first.

When a session event arrives, read the injected payload before reaching for other tools — it already carries the session title, status, stop cause, and the last user and agent messages. Use `session_search` only if that isn't enough. Report the session, what happened, and whether it looks finished; act on the session itself only when the user has asked you to.

## Completion checklist

Report the agent’s role, enabled abilities, non-default tool permissions, loaded/available skills, model and guardrail limits, trigger, and credential handoff. Re-read the final agent state after material configuration work. If a requested capability needs an ability that is not enabled, explain that dependency rather than pretending its tools exist.
