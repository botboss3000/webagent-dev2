# Scan current-session source

Actions: `discover`, `scan`, `scan_file`. Default `root` is `webagent-v3`; optional `scope` is relative to it. `scan_file` requires one exact relative `file_path`. No traversal, absolute paths, symlinks or runtime directories are allowed.

`discover` reports indexed/stale state without writing source or refreshing the index. `scan` atomically replaces records in its scope and removes disappeared files. `scan_file` replaces only one file. Optional `expected_revision` rejects concurrent/stale index edits; optional `batch_id` must be unique. Source changes during scanning reject the update. Read `code_index_progress` after an error.

Results are paginated metadata. Follow `next_offset` via lookup/discovery for large results. The successful scan returns the new revision, batch ID, file counts and removal counts. Bounds fail explicitly; an error never means source was changed.
