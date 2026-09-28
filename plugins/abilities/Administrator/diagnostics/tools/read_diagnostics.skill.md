The flight recorder — the single most useful diagnostic tool. Returns recent
diagnostic records (or, with `view='tools'`, aggregated tool-execution metrics)
as a JSON string.

- **Default levels are `warning,error,critical`** — but run lifecycle and resume
  events are recorded at **`info`**. Whenever you are diagnosing run state, pass
  `levels="info,warning,error,critical"` or the evidence you need is simply absent.
- `view='tools'` is a *different table* (`tool_executions`, same `logs.db`): per-tool
  call count, failure count, success rate, avg/max duration, plus recent failures.
  Use it for "which tool is slow or failing".
- Narrow with `categories` (server, http, access, ws, loop, run, recovery, tool),
  `session_id`, `search` (substring against message/source/detail), and
  `since_minutes`. The window defaults to **120 minutes**; widen it (e.g. `1440`)
  for older incidents.
- The `detail` field carries full Python tracebacks — that is where the real cause
  of a vague tool error or HTTP 500 lives.
