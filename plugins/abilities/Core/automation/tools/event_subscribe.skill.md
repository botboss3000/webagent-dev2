Creates an event trigger from a source that `list_event_sources` confirmed is
enabled.

- A source that is discovered but **not** enabled makes this refuse with
  `source_not_enabled` — tell the user real-time push isn't available for that
  source and **stop**. Do not create the subscription anyway.
- Ask where the result should go (see **Delivery**) — an email that lands at 2am
  won't be read in the chat the user is sitting in now, so confirm the
  destination rather than defaulting.
- The recurring guardrails and the event-trigger limit apply.
