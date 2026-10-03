# Search this session

Search public user and assistant chat messages in the current conversation. This narrow V3 tool does not search other conversations, private model interactions, tool arguments/results or arbitrary databases.

Provide query as case-sensitive literal text (1–256 UTF-8 bytes), after_seq (default zero), and limit (default 10, maximum 20 matches). Each call scans at most 50 chat rows and returns snippets of at most 512 Unicode characters around each match. next_seq is the last scanned chat sequence; continue with that cursor while has_more is true. A page with no matches can still have more rows to scan. Regular expressions are not interpreted.

Messages retain their published IDs, roles, source run and timestamps. Snippets can omit surrounding content and must not be treated as complete evidence. Live Rust policy authorizes every read.
