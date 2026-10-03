# Look up indexed source

Choose `action`: `file`, `line`, `symbol`, or `edge`. `line` requires `file_path` and `line_no`. Symbol and edge search use literal `query`, optionally constrained to one `file_path`. Paths are relative to `root`, default `webagent-v3`.

Results preserve stored hashes, physical line ownership and parser uncertainty. Each returned record has a live `stale` flag. Lookup never rescans or silently adopts edited source. Use `limit` and `offset`/`next_offset` for pages; if `result_cap_reached` is true, narrow the query or file filter.
