# Storage authority and browser cache contract — Phase 1

Status: implemented behind disabled feature gates. Server SQLite remains the
default and recommended production authority.

## Authority model

An authenticated `(user_id, session_id)` pair is the ownership boundary.
`app/db/storage_authority.py` defines the common snapshot, revision, mutation,
conflict, tombstone, idempotency, and recovery types. Server SQLite implements
that contract through `ServerSQLiteTranscriptAuthority`; the IndexedDB sync
wire format uses the same fields.

Each authoritative snapshot has:

- an owner (`user_id`, `session_id`);
- a monotonically increasing authority revision;
- a canonical content hash and schema version;
- session metadata and ordered interactions;
- an optional tombstone and deletion timestamp.

Writes are compare-and-swap operations against `base_server_revision`.
`applied` and `noop` are acknowledgements. `conflict` and `rejected` must stay
dirty locally. A mutation ID is an idempotency key: an exact retry returns the
stored result, while reuse with different content is rejected.

Recovery reads the authority when a local revision/hash does not match. A
tombstone prevents an offline or stale device from resurrecting deleted data.

## Browser-authority execution boundary

`BrowserAuthorityDB` has no dynamic delegation. Every allowed operation is
enumerated. Shared agent configuration, authorization, credentials resolution,
and billing reads may reach server storage. Transcript, turn state, session-run
state, tool protocol messages, skill toggles, and interrupt state are either
ephemeral or browser-owned.

The request-scoped DB override covers helpers that call `get_db()` internally.
Non-default agent engines are rejected because they have not implemented this
boundary. Legacy turn hooks are not called with a raw server DB.

Permitted server mutations during browser authority are account/control-plane
effects that are not transcript state, such as billing ledger updates and
provider usage accounting. Tool implementations may still have intentional
external side effects. A completed browser turn has a bounded replay receipt;
an interrupted turn is not claimed to be exactly-once.

## History protocol

The browser sends a complete prior transcript on a cold turn. After successful
execution the server issues an opaque, tenant/session/revision-bound one-time
history token plus the authoritative content hash. A warm request may omit
history only with that exact token and revision.

Expired, evicted, consumed, corrupt, cross-tenant, or stale tokens fail before
agent or tool execution with `history_required`. The browser retries once with
the complete IndexedDB transcript and the same idempotency key. No cache-warm
boolean is treated as proof of history.

Server history entries are bounded to 1 MiB each, 500 entries, 32 MiB total,
and 15 minutes. Oversize histories continue correctly without a warm token.

## IndexedDB cache contract

IndexedDB v6 uses a server-issued pseudonymous cache scope in the physical
database name. Raw user IDs and credentials are not database keys. A cache row
is usable only when all of these match:

- authenticated cache scope and session ID;
- server authority marker;
- cache schema version;
- authoritative revision and non-empty content hash;
- unexpired TTL.

The current server policy advertises schema v1, a five-minute metadata TTL,
15-minute transcript TTL, and a 50 MiB quota. Expired or least-recently-used
clean server-cache entries may be evicted. Dirty data and browser-authoritative
transcripts are never quota-evicted. If only authoritative/dirty data remains,
quota exhaustion is surfaced rather than deleting it.

Full tool arguments/results, credentials, and auth tokens are not cached.
Legacy `tool_details` data remains purged and its write/read API is inert.

The persistent browser cache gate remains disabled. Existing default session
list APIs do not yet publish a complete revision/hash manifest, so hybrid cache
reads fail closed to the server even if stale rows exist.

## Sync and promotion

`/api/v1/browser/sync` and `/promote` accept at most ten mutations and 4 MiB per
request; each upsert is capped at 1 MiB and 2,000 interactions. Identity comes
only from the verified JWT.

Each session commits independently and receives an independent result. The
browser marks a session clean only when:

1. its result is `applied` or `noop`;
2. its current local revision still equals the submitted revision; and
3. its persisted mutation ID still matches.

Deletion writes a durable IndexedDB outbox tombstone before removing local
session data. The outbox row remains across reload, network failure, or quota
pressure until the server acknowledges it. Server tombstones win over stale
upserts.

## Retention, disclosure, export, and deletion

Browser-authoritative transcripts are device-local, visible to anyone with
access to that browser profile, and subject to browser backup, storage eviction,
private-browsing, and device-management policy. This disclosure must be shown
before production enablement.

Logout purges the active tenant database before credentials change. Explicit
local clear purges every v6 store, including sync outbox and legacy tool detail
rows. Server-side deletion creates a tombstone; tombstone and idempotency
receipt retention must exceed the maximum supported offline period before
production enablement. Sync receipts default to 30 days through
`WEBAGENT_BROWSER_SYNC_RECEIPT_DAYS`; tombstones are deliberately not pruned
until Phase 2 adds device revocation epochs.

Export must read a single authority snapshot and include its revision, hash,
schema version, tombstone state, session metadata, and interactions. Browser
export must not silently merge a stale server copy. Account deletion must purge
server data, browser data on every reachable signed-in device, cached history,
and replay receipts. Cross-device browser purge needs a revocation epoch in
Phase 2.

## Telemetry and administrator controls

Telemetry may record mode, cache hit/miss reason, schema/revision mismatch,
payload byte counts, latency, quota status, SQLite operation counts, conflict
status, and pseudonymous scope. It must not record transcript content, history
tokens, idempotency keys, tool arguments/results, credentials, raw user IDs, or
content hashes.

Administrators currently control:

- `WEBAGENT_ENABLE_BROWSER_AUTHORITY` (default false);
- `WEBAGENT_ENABLE_BROWSER_SESSION_CACHE` (default false);
- fail-closed storage routing through the admin-only atomic routing endpoint;
- browser chat rate/window limits.

Production enablement also requires administrator-visible TTL/quota/tombstone
retention controls and telemetry status. Until those are externalized and the
Phase 2 criteria below pass, both browser feature flags remain false.

## Benchmark

Command:

```text
python scripts/benchmark_storage_phase1.py --turns 100 --chars 500 --iterations 25
```

Fixture: 100 turns, 200 interactions, 500 content characters each, 25
iterations. These are storage/protocol microbenchmarks and exclude network and
model latency.

| Mode | CPU/warm | SQLite reads/writes | IDB bytes | first render | warm turn | warm bytes |
|---|---:|---:|---:|---:|---:|---:|
| Server SQLite, no persistent browser cache | 4.942 ms | 200 / 2 | 0 | 5.530 ms | 4.942 ms | 108,681 |
| Server SQLite, validated IndexedDB cache | 1.268 ms | 0 / 2 | 108,681 | 1.603 ms | 1.268 ms | 133 |
| Browser authority | 1.235 ms | 0 / 0 | 108,681 | 1.086 ms | 1.235 ms | 133 |

The benchmark supports keeping a validated persistent cache as a production
goal; it does not justify enabling either gate by itself.

## Phase 2 handoff

Keep both browser gates disabled.

1. Add authoritative revision/hash manifests to the default server SQLite
   session and incremental-message APIs, then run real end-to-end hybrid cache
   tests instead of fail-closed misses.
2. Add durable, cross-process turn idempotency reservations. Define recovery
   for server crashes and disconnected clients around externally side-effecting
   tools; do not claim exactly-once until tools participate.
3. Audit every built-in and plugin tool for captured raw DB handles. Require
   authority-aware tool contexts or explicitly classify non-transcript writes.
4. Add a browser revocation epoch and device registry so logout/account deletion
   can invalidate other devices and stale caches.
5. Externalize TTL, quota, tombstone/receipt retention, export/delete policy,
   and telemetry controls in the admin UI.
6. Run full multi-process, multi-device, encrypted-store, and provider-backed
   load tests. Capture p50/p95 first-render and warm-turn latency, CPU, SQLite
   reads/writes, IndexedDB growth, transfer bytes, conflict rates, and recovery
   outcomes.
7. Only after the above passes, canary validated IndexedDB cache first. Browser
   authority is a separate later decision with an explicit security review and
   rollback plan.
