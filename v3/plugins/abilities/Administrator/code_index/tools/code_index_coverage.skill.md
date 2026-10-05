# Check coverage and freshness

Actions: `report` (default), `gaps`, `stale`. The current-session repository root defaults to `webagent-v3`. `report` returns physical-line totals, gaps, parser state, stale count, unindexed count and paginated details. Require zero gaps, zero stale count and zero unindexed count before relying on relevant source.

`gaps` reports missing line ledger entries; `stale` reports changed, deleted or unreadable files. No action rescans. Follow each detail page's `next_offset`; completed scan batches do not replace this freshness check.
