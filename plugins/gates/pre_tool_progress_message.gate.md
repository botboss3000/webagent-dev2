---
{
  "id": "pre_tool_progress_message",
  "display_name": "Pre-edit progress message",
  "description": "Recommended for Auto mode: send a deliberate, tool-free progress message describing the work about to be done before the first edit, then continue automatically.",
  "phase": "pre_tool",
  "inject": "pre_tool",
  "status": "stable",
  "order": 10,
  "view": {"key": "require_pre_tool_progress_message", "kind": "bool"},
  "params": {
    "format": {
      "type": "string",
      "multiline": true,
      "label": "Pre-edit progress message instructions",
      "hint": "Optional instructions for this agent's deliberate pre-edit message. They guide its content; the separate tool-free message boundary is enforced.",
      "default": "In Auto mode, before your first edit, send the user a concise pre-edit progress message: say what you found and the concrete work you are about to do. Write plain prose with no heading. Do not call it a plan, request approval, or wait for a response. Send this message alone with no tool calls, then immediately proceed with the edit in the same run."
    }
  }
}
---
Before editing, send the user one deliberate, tool-free progress message describing the work you are about to do. Do not call any tools in that response. Then continue automatically.
