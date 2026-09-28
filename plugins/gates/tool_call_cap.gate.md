---
{
  "id": "tool_call_cap",
  "display_name": "Tool-call cap",
  "description": "Stop the turn once it has made this many tool calls.",
  "phase": "pre_tool",
  "status": "stable",
  "order": 40,
  "view": {"key": "max_tool_calls_per_turn", "kind": "int", "param": "limit", "default": 0},
  "params": {"limit": {"type": "int", "default": 0, "min": 0, "max": 500, "label": "Max tool calls per turn (0 = off)", "hint": "How many tool calls a single turn may make before the run stops it. 0 leaves the cap off, which is the shipped default."}}
}
---
Keep the number of tool calls per turn within the configured budget; bundle reads and avoid redundant calls.
