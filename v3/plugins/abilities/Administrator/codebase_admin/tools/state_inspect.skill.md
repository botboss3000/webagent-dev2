Read bounded state for this chat session through the Rust runtime. Call `catalog` to discover resources.

`session` returns the session title, creation time, public chat count, and latest chat cursor. `chat` returns public message previews in ascending sequence order. Set `after` to the last returned `next_seq` to continue; default is 0. `limit` defaults to 20 and must be 1–50. Check `has_more` before paging again. Chat content previews stop at 512 characters and carry a `truncated` flag.

`runs` returns up to 50 recent run summaries with ID, status, error preview, uncertainty, turns, and update time. It excludes pending tool arguments and provider payloads. `catalog` and `session` take no cursor or limit; `runs` takes only `limit`.

The runtime fixes the current session. This tool has no SQL, key, filesystem path, or arbitrary session selector. It obeys current policy and global shutoff; do not use shell or Python to inspect encrypted application databases.
