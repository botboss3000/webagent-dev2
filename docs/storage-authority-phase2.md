# Storage authority and browser cache contract — Phase 2

Status: implemented on top of the Phase 0/1 baseline. Server SQLite remains the
default authority. `WEBAGENT_ENABLE_BROWSER_AUTHORITY=false` and
`WEBAGENT_ENABLE_BROWSER_SESSION_CACHE=false` remain the production defaults.

## User-visible outcome

Validated hybrid caching now has a real server manifest protocol. Cached
transcripts are used only after an authenticated server response confirms the
exact authority revision and content hash, and IndexedDB also verifies its own
cached payload hash before rendering. A corrupt, stale, expired, wrong-schema,
or incomplete cache is replaced from server SQLite.

The first manifest read after a transcript mutation rebuilds its canonical hash.
SQLite triggers advance the revision and mark the materialized manifest dirty;
subsequent validations read one manifest row rather than the transcript.

Logout advances a per-user revocation epoch. Existing JWTs on every device stop
validating, remember-login state is cleared, and a later login receives a new
pseudonymous IndexedDB scope. A remote device that is offline cannot be
physically erased, but it cannot authenticate or reuse its stale scope after it
reconnects. Account deletion and administrator revocation advance the same epoch
before removing/locking the account.

## Side-effect recovery

Side-effecting tools are wrapped in a durable SQLite reservation keyed by user
turn and normalized tool intent. The reservation is cross-process:

- a concurrent duplicate is refused while the lease is live;
- a completed duplicate reuses the stored result;
- a lost lease during an external side effect becomes `uncertain`;
- an `uncertain` call is never automatically replayed.

This is an at-most-once safety boundary, not an exactly-once claim. Exactly-once
requires the external provider to accept WebAgent's tool idempotency key.
`app.tools.execution_context.current_tool_context()` exposes that key and the
active authority mode so provider adapters can participate in the next phase.

## Tool database-handle audit

`scripts/audit_tool_db_handles.py` scans built-in tools, integrations, and
ability plugins for `_get_conn()`, `get_raw_client()`, and direct SQLite access.
Every current occurrence is classified in
`docs/storage-tool-db-audit.json`; module-level handle capture and unclassified
new files fail the audit. Browser authority continues to expose no raw DB
handle, so server-transcript/control-plane paths fail closed.

## Administrator controls and disclosure

The Settings → Data Settings storage panel now exposes:

- metadata and transcript TTL;
- device cache quota;
- tombstone and sync-receipt retention;
- durable turn-reservation retention;
- export/delete policy;
- storage telemetry and payload redaction status.

Saving policy never enables either browser feature. Env-locked deployments use
the matching `WEBAGENT_BROWSER_*` variables. Telemetry policy permits only
mode, reason/status, byte counts, timings, quota state, and pseudonymous scope;
transcript content, tokens, credentials, tool arguments/results, raw user IDs,
idempotency keys, and content hashes remain excluded.

Device-local data remains visible to anyone who can access that browser profile
and is subject to browser backup, private-mode, device-management, and eviction
behavior. Logout purges the current device when reachable; revocation prevents
unreachable devices from reconnecting, but cannot remotely overwrite offline
browser files.

## Verification and benchmark

Phase 2 adds Python contract tests for manifest changes, split per-user SQLite,
multi-process reservation contention, uncertain side-effect recovery, JWT
revocation, policy persistence, tool context, and the raw-handle audit. The real
Chromium suite proves a revision-validated cache hit and corrupt-payload
fallback.

`scripts/benchmark_storage_phase2.py` records cold/rebuild hash cost, warm
manifest p50/p95, transfer reduction, and durable reservation p50/p95 in
`docs/storage-authority-phase2-benchmark.json`. It is a local microbenchmark,
not a provider or model benchmark.

## Phase 3 handoff

Keep both browser gates disabled.

1. Add provider-native manifest maintenance for Postgres/Supabase and run
   encrypted split-store migrations under production-sized transcripts.
2. Pass `ToolExecutionContext.idempotency_key` into every externally
   side-effecting provider API that supports idempotency, and add provider
   reconciliation for `uncertain` calls.
3. Add a user-facing device/session management panel and a reachable-device
   purge acknowledgement protocol; retain the new epoch as the offline backstop.
4. Enforce and test export/delete policy across server authority, browser
   authority, attachments, memory, replay receipts, and backups.
5. Run multi-instance provider-backed load/canary tests with real network
   latency and encryption. Capture p50/p95 first render and warm turn, CPU,
   SQLite/Postgres operations, IndexedDB growth, transfer bytes, conflict rate,
   uncertain-tool rate, and rollback recovery.
6. Canary validated IndexedDB caching only after those tests and an operator
   rollback drill. Browser authority remains a separate security decision.
