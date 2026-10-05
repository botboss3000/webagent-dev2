# Read index scan progress

Use `action="list"` (default) for retained scan batches or `action="get"` with an exact `batch_id`. The default root is the current session's `webagent-v3`. Responses include requested/indexed file counts, timestamps, status, error class and completed revision.

A completed batch proves that derived rows committed at that scan, not that source remains fresh. Run coverage afterward. Failed scans retain their error receipt and the prior index. Up to 100 recent batches are retained; `limit`, `offset` and `next_offset` page the result.
