Read an exact absolute container path or relative live-checkout file path after locating it. Use 1-based `offset` and a `limit` of 1–2000 lines to read only the relevant region. The response includes `total_lines`, `next_offset`, and `truncated`; continue at `next_offset` when present.

The output is capped at 1 MiB and a file over 32 MiB cannot be inspected with this tool. A truncated result is not evidence of the full file. The path cannot cross a symlink.
