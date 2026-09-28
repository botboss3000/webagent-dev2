---
{
  "id": "error_strike",
  "display_name": "Tool-error strike",
  "description": "Fire a strike once this many tool calls fail in a row.",
  "phase": "pre_tool",
  "status": "stable",
  "order": 30,
  "view": {"key": "max_tool_errors", "kind": "int", "param": "threshold", "default": 0},
  "params": {"threshold": {"type": "int", "default": 0, "min": 0, "max": 20, "label": "Consecutive tool errors before a strike (0 = off)", "hint": "Counts tool calls that fail in a row, so one success resets the count. 0 leaves the strike off, which is the shipped default — the gate does nothing until a value is set."}}
}
---
When a tool call fails, read the error and its fresh-evidence metadata before deciding whether to retry. Retry only when corrected parameters or a clearly different safe probe address the failure. If a negative capability result is stale, revalidate it once with the smallest focused probe against the current scope and generation; do not repeat an unchanged current-generation failure. If tool calls keep failing, stop retrying the same approach: change tactics, ask a clarifying question, or report the blocker.
