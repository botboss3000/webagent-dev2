Upgrades onto the configured **premium** model — the row an admin assigned the
*Premium* role to in App Config → Models.

- It resolves through the same role-slot mechanism the chat footer picker uses, with
  the legacy `high_effort_targets` resolver as a fallback.
- A premium-role model qualifies even when it is NOT enabled as an everyday brain (the
  "premium-only" pattern) — that is the normal way a dedicated premium model is set up.
- **No-op if you're already on one.** If none is configured, the tool says so — relay
  that an admin can assign the Premium role to a capable model; don't pretend you
  switched.
