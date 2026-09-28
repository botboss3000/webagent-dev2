Runs Python and returns stdout — the dependable way to execute code in this
environment, where the shell may lack common binaries.

- Use it for anything the shell can't handle: structured data work, JSON/SQLite
  queries, and logic that would be clumsy as a one-liner.
- It runs as a subprocess, so it does **not** share the agent process's memory — it
  cannot observe or mutate the running server's in-process state.
