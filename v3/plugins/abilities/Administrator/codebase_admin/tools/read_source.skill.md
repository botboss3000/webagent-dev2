Reads file contents from disk, with line-range slicing for large files.

- Use `offset` (1-indexed) and `limit` — **not** `start`/`end`.
- Locate the region with `search_source` first, then read a targeted window, rather
  than paging through a whole large file.
