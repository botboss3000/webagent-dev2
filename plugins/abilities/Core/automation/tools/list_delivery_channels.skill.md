**Call this first whenever you set up an automation.** It returns the external
channels genuinely wired up on this agent, and you may only name a channel it
returned. The always-available targets (this chat, a new chat, silent, file,
webhook) don't appear here because they never need enabling; external messaging
channels (Telegram, Slack, SMS, Discord) work **only** if listed. **Email is not
available yet** — there is no outbound mail channel — so if the user asks for
email, say so and offer a connected channel or save-to-file instead.
