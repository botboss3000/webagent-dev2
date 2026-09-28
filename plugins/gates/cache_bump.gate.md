---
{
  "id": "cache_bump",
  "display_name": "Final cache-generation bump",
  "description": "Require the sw.js CACHE bump to be the run's final source edit.",
  "phase": "completion",
  "status": "stable",
  "order": 60,
  "view": {"key": "require_final_service_worker_cache_bump", "kind": "bool"},
  "params": {}
}
---
After any repository change, increment the numeric CACHE suffix in sw.js. Make that sw.js bump the final source edit; if a source file changes afterward, increment it again. Before completion, verify the final changed-path event includes sw.js and report the new generation.
