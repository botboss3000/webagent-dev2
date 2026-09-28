# Automation — scheduling, reminders & event triggers

This ability lets you run work **later** or **when something happens**, instead of only right now. Two trigger types:

- **Time** — a one-shot (fire once, in N minutes or at a set time) or a recurring schedule (a cron pattern that repeats).
- **Event** — fire when an external source does something (an email arrives, a file is added). Events only work for sources an admin has actually connected.

Everything you create is owned by the current user and shows up in their **Automation tab**, where a human can pause, edit, or delete it too.

Each tool below carries its own mini-skill, delivered when that tool becomes callable — so you don't need this whole skill loaded to use one of them.

---

## Pick the right tool

| The user wants… | Use | Notes |
|---|---|---|
| "Remind me to X in/at …" | `remind_me` | One-shot text ping into THIS chat. Still confirm that's where they want it — if they want it anywhere else, use `schedule_task` with `delivery` instead. |
| "Do this task in 10 min / at 3pm" | `schedule_task` with `in_minutes` or `at` | One-shot job that runs a prompt, not just a text ping. |
| "Every morning / weekday / hour, do X" | `schedule_task` with `schedule_cron` | **Recurring — gated** (see Limits). If the gate refuses, fall back to a one-shot and tell the user. |
| "When an email/file/message arrives, do X" | `event_subscribe` | Only if the source is enabled — check first. |
| "What do I have running?" | `list_automations` / `list_event_subscriptions` | |
| Change / pause / resume / delete one | `update_automation`, `pause_automation`, `resume_automation`, `cancel_automation` | All need the `automation_id` from a list call first. |
| "Run it now" | `run_automation_now` | Fires once immediately, on top of its schedule. |

**Always list before you act on an existing automation.** The mutate tools take an `automation_id` you can only get from `list_automations`. Never guess an id.

---

## Delivery — ALWAYS ask where the result goes

An automation's result has to land somewhere, and the right place depends on the user — a 7am wake-up call is useless in a chat they aren't looking at. **So whenever you set up an automation, ask the user where they want the result delivered before you create it.** Don't silently default to the current chat.

1. **Call `list_delivery_channels` first** so you know which external channels are actually wired up on this agent.
2. **Then ask, offering the real options** — e.g. *"When this fires, where should it go — here in this chat, a brand-new chat, [any channels that came back, e.g. Telegram], saved to a file, or done silently?"* Only name channels `list_delivery_channels` returned.
3. **Set `delivery` to what they choose.** It's a list of targets; each is a channel name or an object:
   - `{mode:"here"}` — reply in the current chat.
   - `{mode:"new_session"}` — a fresh sidebar session.
   - `{mode:"headless"}` — do the work, surface nothing.
   - `{mode:"channel", channel:"telegram", recipient:"…"}` — an external comms channel (only one `list_delivery_channels` returned).
   - `{mode:"webhook", url:"…"}` or `{mode:"file", filename:"…"}`.

You can fan out to several targets at once (e.g. act silently **and** ping Telegram). The per-agent limit caps how many — extra targets are trimmed.

**What works vs. what doesn't:** the current chat, a new chat, silent, a file, and a webhook are **always** available. External messaging channels (Telegram, Slack, SMS, Discord) work **only** if `list_delivery_channels` returned them. **Email is not available yet** — there is no outbound mail channel — so if the user asks for email, tell them it isn't set up and offer an alternative (a messaging channel that *is* connected, or save-to-file). Never promise a channel that didn't come back from `list_delivery_channels`.

Whatever you pick is shown to the user on their **Automations page** under the "Output" column, so set it deliberately — the destination is visible, not hidden.

---

## Run mode — who executes it

`run_mode` is usually `inline` (you run it yourself — the default). The clone modes (`fresh_clone`, `dedicated_clone`) need the **Agent Orchestration** ability enabled on this agent; if it isn't, the tool silently falls back to `inline` and returns a `warning`. Relay that fallback to the user rather than pretending a clone ran.

---

## Recurring guardrails (per fire)

When you do create a recurring or event automation, these optional knobs keep it from running away:

- `max_per_day` — cap fires per day.
- `expires_at` — auto-stop after an ISO timestamp.
- `disable_after_failures` — auto-disable after N consecutive failures.
- `retry_max` / `retry_backoff_seconds` — retry a failed run before giving up that occurrence.

Offer these when a user sets up something open-ended ("every minute forever" deserves an expiry or a daily cap).

---

## Per-agent limits (why a call may be refused)

This ability is configured per agent. A request can be refused or trimmed by these settings — when that happens, relay the limit honestly instead of retrying:

- **Allow Recurring Schedules** — ON by default. If an admin has turned it off, `schedule_cron` is refused; use a one-shot instead.
- **Allow Event Triggers** — if off, `event_subscribe` is refused; use timed tasks.
- **Max Automations per User** — once hit, new schedule/subscribe calls are refused until the user cancels some.
- **Max Fires per Day** — caps an event trigger's daily fires (you may ask for a lower cap, never higher).
- **Max Delivery Targets** — extra `delivery` targets beyond the cap are trimmed.
- **Require Human Approval** — if on, every automation you create starts **paused**; tell the user to enable it from the Automation tab.

When a tool returns an error or a `note`/`warning`, read it and pass its meaning
to the user in plain language. Do not repeat a current-generation failure
unchanged. If a state-dependent negative is stale, retry the smallest safe status
or list probe once against the current resource generation before reporting it.
