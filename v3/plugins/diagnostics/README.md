# V3 operational diagnostics (initial backend module)

Optional Rust application plugin, not an agent ability or the V2 journey recorder.
Build-time discovery uses this directory's `mod.rs`; `src/diagnostics.rs` provides
no-op hooks when the directory is absent. Remove the directory and rebuild to
remove the module and endpoint; retained evidence is not deleted. Set
`V3_DIAGNOSTICS_ENABLED=false` on both API and loop processes and recreate them
to disable capture without rebuilding. The manifest describes the package;
it is not a dynamic Rust code loader.

## Capture and storage

Model failures (including System compaction) record run/session IDs, the failing
provider/model, failure category, response-started flag, HTTP status where known,
transport flags and underlying I/O error kind where exposed, and retry/backup/
terminal decision. Both subscription Responses and API-key Chat Completions
adapters preserve transport classifications. HTTP 4xx/5xx record only matched
route templates, method and status. API internal errors record a fixed label.
No raw errors, provider bodies, prompts, tool arguments/results, headers, URLs,
query strings, credentials, or cookies enter this archive.

Each API/loop process has a nonblocking 4,096-event queue feeding a separate
`V3_STATE_ROOT/diagnostics/logs.db` SQLite WAL archive. Directory permissions are
0700 on Unix. Retention caps records at 200,000 and incrementally removes events
older than seven days during writes (1,000 expired records per write). Age cleanup
is not scheduled while idle. Archive failures never stop chat; queue drops and
write errors are process-local counters, not global totals. Startup/archive-write
failures also appear on console. Abrupt termination can lose queued events; there
is no guaranteed shutdown flush in this initial implementation.

## Read API

`GET /api/v3/admin/diagnostics?session_id=<id>&run_id=<id>&limit=100&before=<id>`
requires existing browser authentication and installation-administrator authority.
Results are newest first; limit is clamped to 1–500, and `before` is exclusive.
The read API is installation-wide and intentionally unavailable to tenant admins
and ordinary users. Disabled/uninitialized capture returns `enabled:false`.
Both processes write the same archive; returned counters describe the API process.

## Parity boundary

This closes the provider-failure visibility gap, not all V2 parity. Browser error
intake, full tracing/stack capture, live ring/feed, tool timing, comprehensive
recovery/lifecycle instrumentation, and the journey recorder UI remain unported.
No frontend is added. Do not interpret successful compilation as deployment.

Tests cover redaction, durable reopen and expiration; provider and runtime suites
cover adapter behavior and existing failure safety. Retry policy is unchanged:
partial responses and uncertain effects must never be silently replayed.
