# terminal_open

Launch `command` as a bounded shell string in a genuine PTY in this session workspace. Optional `cols` (20–300, default 100) and `rows` (5–100, default 30) set its dimensions. Returns an opaque `terminal_id`; read it before sending input. No host credentials or application environment enter the child. Requires execution permission and approval when policy requires it.
