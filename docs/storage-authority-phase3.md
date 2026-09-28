# Storage authority and browser cache contract — Phase 3

Status: implementation complete; production activation remains blocked on the
live provider/browser canary described below. Both browser feature gates remain
disabled in `.env.example`.

## Implemented outcome

### Postgres and Supabase manifests

Postgres bootstrap now installs a row trigger that advances the authority
revision and marks the materialized transcript hash dirty after every
interaction insert, update, or delete. The first manifest read rebuilds the
canonical hash and warm validations read one `session_manifests` row.

Supabase ships the same trigger in
`supabase/migrations/20260730000000_session_manifest_maintenance.sql`.
The Postgres schema fingerprint includes the trigger definition, so an upgrade
cannot incorrectly skip its installation.

### Provider-native tool idempotency and reconciliation

The durable tool reservation now stores only a non-secret provider,
operation, and deterministic provider resource ID. A lost lease remains
`uncertain` unless the adapter explicitly registered a provider-native
deduplication primitive.

The calendar adapters now use:

- a deterministic Google Calendar event `id`;
- a deterministic Microsoft Graph event `transactionId`.

Those providers can safely receive the same create request after a lost worker
lease. Other side-effecting tools retain the Phase 2 fail-closed behavior.
Stripe's documented `Idempotency-Key` helper is available for the future Stripe
integration; Stripe is still marked `coming_soon` in this repository.

Provider references:

- https://developers.google.com/workspace/calendar/api/guides/create-events
- https://learn.microsoft.com/en-us/graph/api/resources/event
- https://docs.stripe.com/api/idempotent_requests

### Device/session management and purge acknowledgement

Manage Account now lists signed-in device sessions, their last-seen time,
revocation state, and browser-purge acknowledgement. A user can revoke another
device or sign out and purge the current one.

Revocation still advances immediately. A revoked or expired but validly signed
device token is accepted only by the purge-status and purge-ack endpoints. It
cannot authorize normal application requests. A reachable device clears its
tenant-scoped IndexedDB and acknowledges the purge; an offline device remains
revoked and performs the same handshake when it next runs the client.

### Export/delete policy enforcement

Administrator `export_enabled` and `delete_enabled` policy now gates migration
export, self/account deletion, user-data deletion, and permanent session
deletion. Moving a session to the recycling bin is not treated as permanent
deletion.

Manage Account can download one JSON export containing:

- server-authority sessions, interactions, manifests, summaries, memory/chunks,
  attachment metadata and bytes, browser sessions, skills, data sources,
  automations, profile, and the non-secret account fields;
- every IndexedDB store on the current device, with Blob values base64 encoded.

Account deletion erases server-authority transcripts/manifests, memory/chunks,
attachment objects and metadata, webhooks, browser sessions, automation/event
records, billing/user-owned rows, profile, account, and durable turn/tool
receipts. It also erases browser-sync receipts, short-lived process replay
caches, the user's local sync sidecar and its exact pre-encryption backup
siblings, and that user from the legacy account JSON backup. Provider-managed
database backups remain subject to the operator's configured retention policy.
The client then purges its browser-authority/cache stores and acknowledges
completion.

### Canary and rollback

Validated IndexedDB caching has two rollout controls in addition to the
existing main feature gate:

- `WEBAGENT_BROWSER_CACHE_CANARY_PERCENT` selects a stable percentage of users;
- `WEBAGENT_BROWSER_CACHE_ROLLBACK=true` fails closed immediately.

An operator can also use a runtime marker without changing environment or
routing configuration:

```powershell
python scripts/browser_cache_rollback.py activate
python scripts/browser_cache_rollback.py status
python scripts/browser_cache_rollback.py clear
```

Browser authority is not included in this canary and remains a separate
security decision.

## Verification

The Phase 0–3 contract suite contains 55 passing tests (plus 6 subtests).
Phase 3 adds provider
trigger contract checks, provider-native retry/reconciliation checks,
pseudonymous receipt erasure, signed-device purge acknowledgement, lifecycle
export/delete coverage, policy guards, and deterministic canary rollback.

A follow-up implementation audit also added regression coverage and fixes for:

- Postgres manifest reads that must not poison a transaction with a SQLite probe;
- revision-CAS publication so a concurrent interaction cannot make a stale hash
  look clean;
- old- and new-session invalidation when a SQLite interaction changes sessions;
- Google duplicate-event reconciliation after a lost worker lease;
- per-device remember-token binding and retention of offline purge directives;
- purge acknowledgement only after the exact tenant IndexedDB was cleared;
- live rollback enforcement on every manifest validation;
- lifecycle export completeness and credential-field redaction.

`docs/storage-authority-phase3-benchmark.json` records a local 5,000-row,
5.12 MB transcript workload:

- cold manifest rebuild: 538.824 ms;
- warm manifest p50/p95: 0.301/0.664 ms;
- manifest transfer: 182 bytes;
- estimated transfer reduction: 99.9964%.

This local run is not a production/provider claim.

## Required production activation gate

Run the isolated Postgres workload over the actual encrypted network path:

```powershell
$env:WEBAGENT_PHASE3_PROVIDER_DSN = '<dedicated test DSN>'
python scripts/benchmark_storage_phase3.py --provider --rows 50000 --payload-bytes 2048
```

The harness creates a unique temporary schema and drops it in `finally`.
Do not point it at an account where schema creation/drop is outside the
operator-approved test scope.

Before changing `WEBAGENT_ENABLE_BROWSER_SESSION_CACHE` from `false`, capture
real multi-instance/Chromium measurements for first render, warm turn, CPU,
Postgres operations, IndexedDB growth, transfer bytes, conflict rate,
uncertain-tool rate, and rollback recovery. Perform the rollback-marker drill
while the canary is live. Start with a small stable percentage, and leave
`WEBAGENT_ENABLE_BROWSER_AUTHORITY=false`.

Until those environment-specific measurements pass, Phase 3 is implemented but
the production cache activation gate is intentionally closed.

## Next-step activation plan

The remaining work is an operational rollout, not another authority-model
change. Keep both browser feature gates disabled until every step below has a
recorded result.

### 1. Freeze and verify the release candidate

1. Resolve or explicitly waive unrelated repository-wide collection failures.
   As of July 31, 2026, `tests/test_optimizer_handoff.py` cannot import
   `_existing_closer_session`; the Phase 0-3 storage suites themselves pass
   55 tests plus 6 subtests.
2. Run the Phase 0-3 suite on the exact candidate artifact:

   ```powershell
   uv run --with pytest python -m pytest -q `
     tests/test_storage_phase0.py `
     tests/test_storage_phase1.py `
     tests/test_storage_phase1_browser.py `
     tests/test_storage_phase2.py `
     tests/test_storage_phase3.py
   ```

3. Record the commit SHA, image/version identifier, schema fingerprint, browser
   versions, and effective storage policy. Do not benchmark an uncommitted or
   different source state.

Go/no-go: all storage contract tests pass; no false manifest cache hit, tenant
scope leak, duplicate provider side effect, or purge acknowledgement without a
confirmed local deletion is accepted.

### 2. Run the isolated provider benchmark

1. Obtain a dedicated, operator-approved Postgres DSN whose role may create and
   drop a temporary schema.
2. Run at least three 50,000-row samples over the real encrypted production-like
   network path:

   ```powershell
   $env:WEBAGENT_PHASE3_PROVIDER_DSN = '<dedicated test DSN>'
   python scripts/benchmark_storage_phase3.py `
     --provider --rows 50000 --payload-bytes 2048
   ```

3. Capture cold rebuild latency, warm p50/p95, transfer bytes, query/operation
   count, CPU, errors, and schema cleanup for every sample.
4. Exercise an interaction mutation during a cold rebuild and confirm the
   revision compare-and-swap retries or fails closed; it must never publish a
   stale hash as clean.
5. With a disposable provider user, verify export completeness/redaction and
   deletion across sessions, manifests, memory/chunks, attachments, automation
   and event rows, billing/wallet rows, account fields, and local sync receipts.

Go/no-go: zero correctness/cleanup errors; at least 95% transfer reduction
against the same full-transcript workload; warm-manifest p95 no more than 25%
of the full-transcript p95; cold rebuild remains inside the existing first-render
SLO. If there is no approved first-render SLO, define it before accepting the
result.

### 3. Make rollback deployment-wide

The file marker is local to one filesystem. Before a multi-instance canary,
either:

- distribute marker activation/clear to every serving instance and verify each
  instance independently; or
- move the runtime rollback state into shared control-plane storage.

The environment rollback flag remains the deployment-wide emergency backstop.
Test that an already-running client receives `not_modified=false` on its next
manifest validation after rollback activates.

Go/no-go: every serving instance fails closed within one validation cycle, no
browser reload is required to stop cache hits, and rollback does not affect
server-authority transcript correctness.

### 4. Run the Chromium multi-instance canary

Keep:

```text
WEBAGENT_ENABLE_BROWSER_AUTHORITY=false
```

Enable validated session caching only for the canary, then ramp a stable cohort:

1. 1% for at least 24 hours;
2. 5% for at least 24 hours;
3. 25% for at least 48 hours;
4. 50% for at least 72 hours;
5. 100% only after a written review of the preceding stages.

At each stage compare canary and control for first render, warm turn, CPU,
Postgres operations, transfer bytes, IndexedDB growth, cache conflicts,
uncertain-tool outcomes, authentication refresh failures, device-purge
completion, export completeness, and support/error rate.

Go/no-go: no security or data-integrity regression; no statistically meaningful
increase in conflict, uncertain-tool, auth-refresh, or purge-failure rates;
IndexedDB remains within policy; warm-turn latency and transfer improve without
regressing the approved first-render SLO.

### 5. Drill rollback before every promotion

During live canary traffic:

```powershell
python scripts/browser_cache_rollback.py activate
python scripts/browser_cache_rollback.py status
python scripts/browser_cache_rollback.py clear
```

Confirm cache hits stop, server-authority reads continue, pending browser writes
are not promoted to authority, device purge still works with revoked/expired
signed credentials, and clearing the marker restores only eligible cohorts.
Store timestamps and per-instance evidence with the rollout record.

### 6. Close Phase 3; decide browser authority separately

Phase 3 may be marked production-active only after the provider benchmark,
provider lifecycle canary, multi-instance Chromium measurements, and rollback
drill all pass. Browser authority stays disabled. Enabling it requires a separate
security/design phase covering offline write authority, conflict semantics,
key custody, recovery, and incident rollback; cache-canary success does not
authorize that change. The proposed work is specified in
`docs/storage-authority-phase4.md`.
