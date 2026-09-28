---
{
  "id": "verify_after_edit",
  "display_name": "Verify each edit",
  "description": "After an edit, require a verify-class tool call before any other tool.",
  "phase": "pre_tool",
  "status": "stable",
  "order": 20,
  "view": {"key": "require_verify_after_edit", "kind": "bool"},
  "params": {}
}
---
After each file edit, verify it before moving on: read the file back, run a test, or check a diff. Continuing without verification is blocked.
