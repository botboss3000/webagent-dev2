# Prompt slot write

An administrator must approve this tool. Use `action: put` with `slot_name`, `content`, and `expected_revision`; use revision 0 only to create a new slot. For an existing slot, first read its current revision and preserve metadata unless changing it intentionally. Optional `order_index`, `lock`, and `merge_mode` (`replace` or `append`) set slot metadata. Use `action: delete` with `slot_name` and the current positive `expected_revision`. A stale revision is rejected. Edits apply to newly accepted runs; do not use prompt slots to store credentials or to bypass tool policy.
