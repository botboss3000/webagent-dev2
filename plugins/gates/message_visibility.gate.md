---
{
  "id": "message_visibility",
  "display_name": "Message visibility",
  "description": "Let the working agent publish an optional normal standalone assistant bubble without another provider completion.",
  "phase": "prompt",
  "inject": "prompt",
  "status": "stable",
  "order": 65,
  "default_enabled": true,
  "mode_scoped": false,
  "append_message": true,
  "view": {"key": "require_message_visibility", "kind": "bool"},
  "params": {
    "guidance": {
      "type": "string",
      "multiline": true,
      "label": "Additional visibility guidance",
      "hint": "Optional guidance that helps this agent choose when a working message should be visible. It adds to the fixed protocol below and cannot override it."
    }
  }
}
---
Ordinary substantive non-final assistant text stays inside the current activity group automatically. Only when you intentionally need a user-visible working update, call `publish_main_message` with the complete clean text to show as a normal standalone assistant bubble while the run continues. Do not use that call for the final response. The call never asks for approval or pauses the run; continue work normally after it. Do not emit visibility markers or other control text in assistant prose.
