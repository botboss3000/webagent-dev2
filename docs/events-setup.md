# Event triggers — one-time setup

The event-trigger subsystem lets a user say things like *"when an email
arrives from any airline, summarize it and message me on Telegram"* and
have the agent fire **in real time** when the matching email shows up.

Most external services (Gmail, Outlook, Drive, etc.) push notifications
to a public HTTPS URL on the WebAgent server. This document covers the
one-time infrastructure setup each provider needs **before** event
subscriptions start working.

> Migration **`016_event_triggers.sql`** must already be applied (SQLite
> installs apply it automatically on startup; Supabase / external Postgres
> installs must run the SQL once from the migrations folder).

---

## Concepts

| Term | Meaning |
|---|---|
| **Event source** | One external service that can emit events (gmail, slack, ...). Plugin under `app/events/sources/`. |
| **Event type** | What happened: `message_received`, `event_added`, `file_modified`, `mention`, ... |
| **Subscription** | A row in `agent_event_subscriptions` saying "when SOURCE emits EVENT_TYPE for OWNER, fire AGENT with PROMPT, optionally deliver via CHANNEL." |
| **Push** | Provider POSTs to WebAgent the moment something happens (real-time). |
| **Poll** | Background loop checks the provider on a cadence (≥5 s). |
| **Renewal** | Many push providers expire subscriptions after a few days; the renewer loop refreshes them automatically. |

---

## What each source needs

### 1. Gmail — push via Cloud Pub/Sub (or 60s poll fallback)

> **Poll fallback is automatic.** If `EVENTS_GMAIL_PUBSUB_TOPIC` is not set,
> Gmail subscriptions still work — the background poller calls
> `users.history.list` every 60 seconds and emits the same
> `message_received` events. Setup is then *only* the user's Google OAuth
> connection; no GCP work needed. To upgrade to real push (no 60s lag, lower
> request volume), do the one-time GCP setup below and set the env vars.

Required env vars (push mode only — omit all three for poll fallback):

| Variable | Purpose |
|---|---|
| `EVENTS_GMAIL_PUBSUB_TOPIC` | Full topic name, e.g. `projects/webagent-495517/topics/webagent-gmail-watch` |
| `EVENTS_PUBSUB_AUDIENCE`    | Audience to validate JWTs against; must match the audience you set on the Pub/Sub push subscription (typically the public webhook URL, e.g. `https://your.host/api/v1/events/gmail`) |
| `EVENTS_PUBSUB_SA_EMAIL`    | *(optional)* extra check — the service account address Pub/Sub uses to authenticate pushes |

Once-only GCP setup (in the `webagent-495517` project):

1. **Create a Pub/Sub topic** named (e.g.) `webagent-gmail-watch`.
2. **Grant Gmail permission to publish to it.** Add the principal
   `gmail-api-push@system.gserviceaccount.com` with role `roles/pubsub.publisher`
   on the topic.
3. **Create a Pub/Sub push subscription** on that topic:
   - **Endpoint URL:** `https://<your-host>/api/v1/events/gmail`
   - **Enable authentication** → select a service account in the project
     (any SA with the basic Pub/Sub publisher role) and set the **audience**
     to the same URL (`https://<your-host>/api/v1/events/gmail`).
   - **Ack deadline** ~30 s.
4. Set the three env vars above, restart WebAgent.
5. The first user who saves a "when an email arrives ..." automation
   triggers a `users.watch` call on their Gmail; the daily renewer re-runs
   it before Gmail's 7-day TTL.

### 2. Outlook Mail / Outlook Calendar / OneDrive — Microsoft Graph

Required env var:

| Variable | Purpose |
|---|---|
| `EVENTS_GRAPH_NOTIFICATION_BASE` | Public base URL Graph will POST to, e.g. `https://your.host`. |

No one-time provider setup beyond the existing Microsoft OAuth app — Graph
creates and validates subscriptions per-user when the user adds an event
trigger. The first POST from Graph is a *validation handshake* that echoes
a token; the source plugins handle that automatically.

Each Graph mail / calendar / drive subscription has a TTL of roughly
**3 days**; the renewer loop refreshes them every hour.

### 3. Google Calendar — push via Calendar channels

Required env var:

| Variable | Purpose |
|---|---|
| `EVENTS_GCAL_NOTIFICATION_BASE` | Public base URL Calendar will POST to, e.g. `https://your.host`. |

Calendar uses the older "channels" mechanism (not Pub/Sub). Channels are
authenticated via a per-channel `token` we generate, and verified in
`verify_webhook`. TTL ~7 days; renewer rebuilds the channel on each
refresh (Google does not allow PATCHing a channel in place).

### 4. Google Drive — push via Drive channels

Required env var:

| Variable | Purpose |
|---|---|
| `EVENTS_GDRIVE_NOTIFICATION_BASE` | Public base URL Drive will POST to. |

Same channels mechanism as Calendar. Per-subscription cursor lives in
`agent_event_subscriptions.external_metadata.page_token`.

### 5. Dropbox — single app-level webhook

Required env var:

| Variable | Purpose |
|---|---|
| `EVENTS_DROPBOX_WEBHOOK_BASE` | Public base URL Dropbox will POST to, e.g. `https://your.host`. |

In the Dropbox app console, add a webhook URI of
`https://<your-host>/api/v1/events/dropbox`. Dropbox does a one-time GET
verification (echoes a `challenge` query param) — the source plugin
handles that automatically. Subsequent POSTs carry a list of account ids
that have changes; the source's per-user cursor walks the delta.

### 6. Shopify — per-shop webhooks

Required env var:

| Variable | Purpose |
|---|---|
| `EVENTS_SHOPIFY_WEBHOOK_BASE` | Public base URL Shopify will POST to. |

Webhooks are created per-shop via the Shopify Admin API when the first
event trigger is added by a user with a connected shop. Signatures are
verified via HMAC-SHA256 against the Shopify app's shared secret.

### 7. Comms channels (Telegram / Slack / Discord / SMS / WhatsApp)

These already have webhooks (under `/api/v1/webhooks/{plugin}`) wired by
the existing communications layer. The event subsystem just **bridges**
them — `app/communications/processor.py` emits a normalized
`message_received` event on every inbound channel message, and the
router fans it out to any matching event subscriptions.

No additional env vars; if the channel works for chat today, it works as
an event source today.

### 8. Polling sources (Twitter / Reddit)

No env vars. The poll loop runs every 15 seconds and calls each enabled
poll subscription according to its `default_poll_interval_seconds` (≥60s
for Twitter and Reddit to respect rate limits).

---

## How it shows up to the user

In the agent's **Automation** tab, the user writes English. The LLM
parser classifies each line as either a **schedule task** (cron) or an
**event subscription**, and writes both into the right table. Example
file:

```
# Schedule
Every weekday at 9am, send me a Telegram summary of yesterday's calendar.

# Events
When an email arrives from any airline, summarize the message.
When someone @-mentions me in Slack, draft a one-line reply for me to send.
When a new file is added to my Google Drive folder "Travel", extract dates.
```

On save:

- Line 1 → `agent_automations` row, cron `0 9 * * 1-5`, channel `telegram`.
- Line 2 → `agent_event_subscriptions` row, source `gmail`, event_type
  `message_received`, filter `{"query": "from:airlines"}`. The Gmail
  source plugin then calls `users.watch` for the user. No channel was
  specified, so the agent will **ask the user where to deliver** when
  the trigger fires.
- Line 3 → source `slack`, event_type `mention`. No provider setup —
  bridged from the existing Slack webhook.
- Line 4 → source `google_drive`, event_type `file_added`, filter
  `{"parent_id": "<folder-id>"}`. Drive watch registered on save.

---

## Observability

| Place | What you see |
|---|---|
| `GET /api/v1/events/sources` | Manifest of enabled sources (LLM-parser-facing). |
| `GET /api/v1/events/subscriptions?agent_id=...` | List subscriptions for an agent. |
| Table `agent_event_subscriptions` | Each row has `last_event_at`, `last_status`, `last_error`, `fire_count`, `external_expiration_at`. |
| Table `event_deliveries` | Append-only audit / dedup log; one row per delivery attempt. |
| Logs | `app.events.*` loggers (router / poller / renewer / per-source). |

---

## Adding a new event source

1. Drop a new file under `app/events/sources/`, e.g. `pagerduty_source.py`.
2. Subclass `EventSource`. Implement at minimum:
   - `name`, `event_types`, `supports_push` (or `supports_poll`)
   - `register_subscription` (push: register watch; poll: seed cursor)
   - For push: `verify_webhook`, `handle_webhook` (decode + normalize)
   - For poll: `poll`
3. Expose the class as `source_cls = MySource`.
4. Restart. The manager auto-discovers it; the parser starts offering it
   to the LLM; the intake route `POST /api/v1/events/mysource` is live.

No changes to the router, the executor, the parser, the poller, or the
renewer are needed for a new source — they all talk through `EventSource`.
