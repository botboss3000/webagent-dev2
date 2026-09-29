---
{
  "id": "closing_message",
  "display_name": "Closing message",
  "description": "Coordinate independently selectable final-response requirements into one completion instruction and validation pass.",
  "phase": "completion",
  "status": "stable",
  "order": 70,
  "inject": "completion",
  "view": {"key": "require_closing_message", "kind": "bool"},
  "params": {
    "flags": {
      "type": "string",
      "multiline": true,
      "level": "app",
      "label": "Flag categories (comma-separated)",
      "hint": "App-wide flag vocabulary — one list governs every agent. Configured at the app level only; never shown on an agent's Modes page.",
      "default": "needs_input, complete, error, partial, unverified"
    },
    "cta_max_words": {
      "type": "number",
      "level": "app",
      "label": "Notification word limit",
      "hint": "The notification / call-to-action must be above 0 and at most this many words. App-wide: the end-of-run validator enforces it, so a blank field falls back to the shipped default.",
      "default": 5,
      "min": 1,
      "max": 50,
      "step": 1
    },
    "require_flag": {
      "type": "boolean",
      "label": "Require status flag",
      "hint": "Require and validate the private final-status flag. Disable it to omit the flag advisory and validation while retaining any other selected Closing Message features.",
      "default": true
    },
    "require_notification": {
      "type": "boolean",
      "label": "Require notification / next step",
      "hint": "Require and validate the private short notification / next-step summary. Disable it without affecting the flag or output controls.",
      "default": true
    },
    "output_closer_enabled": {
      "type": "boolean",
      "label": "Enforce output message",
      "hint": "Enable final user-facing message guidance and enforcement. Its output limit, format rejection, and rewrite guidance are inactive while this is off.",
      "default": true
    },
    "notification": {
      "type": "string",
      "multiline": true,
      "label": "Notification / next-step guidance",
      "hint": "Free-text guidance for the notification / next-step line. App-level text governs every agent and a per-agent value appends to it. Blank adds nothing here — the shipped floor still applies."
    },
    "format": {
      "type": "string",
      "multiline": true,
      "label": "Output format guidance",
      "hint": "Blank = the agent answers freeform. The house style belongs to the app or the agent, not the gate."
    },
    "max_tokens": {
      "type": "number",
      "label": "Maximum output tokens",
      "hint": "Maximum tokens for the final user-facing message. 0 disables the limit; an over-limit draft receives one dedicated provider-capped rewrite, then the normal Closing Message validation.",
      "default": 0,
      "min": 0,
      "max": 100000,
      "step": 1
    },
    "format_enforcement_min_tokens": {
      "type": "number",
      "label": "Enforce bullet format above tokens",
      "hint": "When the final response exceeds this estimated token count, it must contain Markdown bullet points or it is returned to the agent for a bullet rewrite. 0 disables format enforcement; responses at or below the threshold are promoted unchanged.",
      "default": 1000,
      "min": 0,
      "max": 100000,
      "step": 1
    },
    "rejection_message": {
      "type": "string",
      "multiline": true,
      "label": "Rejection message",
      "hint": "Instructions returned to the agent when its final response is too long. For example: ‘Rewrite as concise bullet points only.’",
      "default": "Rewrite the answer as concise bullet points only, keeping the important facts."
    }
  }
}
---
Give your final response to the user.
