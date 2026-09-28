---
{
  "id": "file_checkout",
  "display_name": "File checkout",
  "description": "Fingerprint research reads and require an exclusive session checkout before repository writes in write-capable modes.",
  "phase": "pre_tool",
  "status": "stable",
  "order": 5,
  "view": {"key": "require_file_checkout", "kind": "bool"},
  "params": {}
}
---
Repository file reads are fingerprinted. Before editing, call checkout_files for every path; use checked-out source tools, validate, then call checkin_files. Completion is blocked while this session owns active checkouts.
