Reads and writes agent **prompt-slot documents**. It is **not** a SQL interface.

- Expecting SQL here is a common mistake. For raw SQL against the local or log
  databases, use `run_python` with the `sqlite3` module instead.
