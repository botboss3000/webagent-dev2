# Storage authority and browser cache contract — Phase 4

Status: Phase 4 implementation landed on July 31, 2026; the production
completion gate remains closed pending the deployment checks below. Browser
session caching and browser authority remain disabled by default. This phase
does not authorize browser write authority; that decision remains separate from
cache rollout.

Implemented evidence:

- exact-origin CORS, unsafe-request origin enforcement, WebSocket origin
  enforcement, restrictive response headers, and configurable report-only or
  enforced CSP;
- authenticated, ownership-scoped chat/browser accessory routes; caller-scoped
  browser config resolution; a strict browser DTO; bounded browser session IDs;
  fail-closed access/billing policy checks; and no process-global provider
  credential pre-resolution;
- user-scoped interrupt rows with an ownership foreign key, stale-row cleanup,
  no missing-session fallback, and conservative cleanup of only proven legacy
  placeholders;
- a machine-readable 11-store inventory, tenant-scoped attachment storage,
  byte-aware quota checks, TTL cleanup, complete/redacted export, verified
  purge, partial-failure retry, and remote policy/schema epochs;
- `persistent_cache`, `memory_only`, and `disabled` policies, including
  memory-only account/token mirrors and a prohibition on IndexedDB opens;
- removal or sanitization of the audited tool-log, Markdown, filename, terminal
  error, and debug-eval sinks, with real-Chromium XSS/IndexedDB-sentinel tests.

Production release still requires an allowlisted-origin configuration, an
enforced-CSP canary with zero supported-workflow violations, the full
environment-specific browser/normal-chat parity matrix, and security/privacy
review evidence. Until those checks pass, both browser feature gates stay off.

## Objective

Bring browser chat and every browser-resident data path under the same security,
access, billing, credential, privacy, retention, export, deletion, and incident
controls as normal server-authority chat.

Phase 4 must assume that any successful same-origin script execution can read
IndexedDB. Browser-cache encryption is defense-in-depth against copied storage,
not a control against XSS running in the application origin.

## Current-state findings

### CORS and request integrity

`app/main.py` currently configures CORS with `allow_origins=["*", "null"]`,
credentials enabled, and unrestricted methods and headers. Phase 4 replaces this
with an explicit deployment allowlist. API CORS and public embed/frame origins
are separate policies; enabling an agent embed must not grant that origin general
authenticated API access.

Authentication primarily uses bearer tokens rather than ambient cookies, which
reduces classic CSRF exposure but does not remove the need for request-origin
validation. Query-string tokens, WebSocket origins, future cookie use, login,
recall, and other credential-changing endpoints require an explicit policy.

`AuthMiddleware` exists but is not registered globally. Caller identity
middleware decodes and exposes a verified identity when present; it does not
require one. Several browser-chat-adjacent `/api/v1/chat` accessory routes,
including interrupt/resume and suggestion/skill/ability/config operations, need
an explicit route-by-route authentication and object-authorization audit.

### Browser-chat parity

`app/api/browser_storage.py:browser_chat` already calls the normal agent access
and billing guards. Phase 4 must prove complete parity rather than rely on those
two calls alone. Tool discovery/visibility, ability gates, confirmation rules,
credential resolution, provider selection, rate limits, usage recording, and
error semantics must use the same policy decisions as normal chat.

The current billing helper is fail-open on checker errors, and the browser config
response returns a raw agent dictionary. Paid access must fail closed when its
policy backend is unavailable, and browser config must use an allowlisted DTO
whose fields are proven free of credentials and server-only policy state.

Additional browser-chat blockers:

- the browser config response model does not match the current tool/ability
  objects and can fail validation when they are non-empty;
- browser chat pre-resolves provider configuration with `apply_env=True`,
  writing caller configuration into process-global environment before the
  concurrency-safe loop resolves it again;
- agent config is cached by agent ID rather than caller/policy epoch, so
  user-specific prompts and revoked policy can be stale;
- tools are not explicitly marked/filterable by supported authority mode;
- the current wallet post-charge path calls a credit helper with a negative
  amount that the helper ignores, so a usage record is not proof of settlement;
- browser interrupts are process-local and cannot reach a turn on another
  worker;
- caller-supplied browser session IDs lack a bounded format contract.

### Browser data

Browser data currently spans two IndexedDB databases and 11 stores.

The tenant-scoped `webagent_session_db_<scope>` database has:

- `sessions`;
- `interactions`;
- `agent_config`;
- `session_runs`;
- `memories`;
- `genui_pages`;
- `genui_html`;
- `user_files`;
- `sync_outbox`;
- `tool_details`.

The separate global `webagent-attachments` database has:

- `attachments`, containing raw Blob data, MIME type, filename, size, and
  creation time.

The attachment database is currently not tenant-scoped, included in browser
export, quota-accounted, or cleared by logout/revocation/account deletion.
Device purge can therefore acknowledge after clearing the session database while
attachment bytes remain. Phase 4 must close this gap before purge acknowledgement
is considered complete.

Configured cache TTL/LRU behavior currently applies only to validated
server-cache session/transcript rows. Other stores are effectively indefinite;
configured tombstone retention is not enforced in the browser outbox; and the
current JSON size estimator excludes the attachment database and undercounts
Blob bytes.

Phase 4 maintains a versioned inventory for every store and any future store.
Inventory and lifecycle checks are release-blocking: adding a store without
classification, retention, export, erasure, purge, and invalidation behavior
must fail CI.

### CSP and XSS

The embed page has a `frame-ancestors` response policy, but there is no
application-wide CSP protecting the main UI and API-delivered HTML. Because the
UI contains legacy inline scripts/styles and dynamic HTML, CSP must begin in
report-only mode and be tightened with measured remediation rather than enabled
with broad `unsafe-inline`/`unsafe-eval` exceptions that provide little value.

Known high-risk sinks include unsanitized tool-log/SSE HTML, attachment Markdown
and filenames, terminal error strings, and generated HTML. Debug tooling also
uses `eval`. User-controlled screenshots/user-data are mounted on the
credentialed application origin; active HTML/SVG must instead be forced to
download, sandboxed, or served from an isolated cookieless origin.

### Interrupt data pollution

Commit `ccf0149` changed `session_interrupts.session_id` to remove its session
foreign key and added an existing-schema fallback that creates:

```sql
INSERT OR IGNORE INTO sessions (id, user_id, ...)
VALUES (?, '', ...);
```

That fallback creates empty-owner placeholder sessions. It pollutes authority
data, is not ownership-safe, and can collide with browser-authority session
identifiers. Phase 4 removes the fallback rather than normalizing it.

## Workstream 1 — CORS, authorization, and CSRF

### Configuration

Add a deployment setting such as `WEBAGENT_ALLOWED_ORIGINS` with these rules:

- secure default: same-origin only;
- production rejects `*` and `null` when authenticated API access is enabled;
- origins are exact normalized scheme/host/port tuples;
- no path, wildcard subdomain, suffix, substring, or reflected-origin matching;
- public embed origins remain in the per-agent embed policy and do not enter the
  API allowlist automatically;
- environment-locked deployments cannot weaken the allowlist through runtime UI.

Restrict CORS methods and headers to the endpoints that need them. Send
`Vary: Origin` on allowlisted responses. Enable credentialed CORS only if an
approved endpoint actually uses secure cookies.

### Request integrity

- Reject disallowed `Origin` values on authenticated unsafe methods even when
  the request does not require a CORS preflight.
- Inventory every `/api/v1/chat` and `/api/v1/browser` route and require
  authentication plus object/session ownership by default. Maintain a small,
  reviewed public/embed exception list.
- Validate WebSocket `Origin` before accepting authentication or allocating
  browser/session resources.
- Remove bearer tokens from query strings where browser APIs support an
  `Authorization` header. Where a query credential is unavoidable, use a
  one-time, narrowly scoped, short-lived ticket and prevent it from reaching
  logs/referrers.
- Set a restrictive `Referrer-Policy` and redact query values in HTTP/access
  diagnostics.
- Keep bearer-token APIs non-cookie-authenticated. If cookies are introduced,
  require `Secure`, `HttpOnly`, and an appropriate `SameSite` setting plus a
  synchronizer or signed double-submit CSRF token on unsafe requests.
- Apply rate limits to login, recall, token refresh, browser chat, sync,
  promotion, interrupt, purge status/ack, export, and deletion.

### Acceptance tests

- configured same-origin and allowlisted-origin requests succeed;
- unconfigured, `"null"`, suffix-confusion, mixed-case, default-port, and
  user-info origins fail;
- an allowed embed origin cannot call authenticated API endpoints unless it is
  independently API-allowlisted;
- unsafe cross-origin simple requests fail before mutation;
- preflight methods/headers are minimal;
- WebSockets reject disallowed origins;
- no long-lived credential appears in URL, access log, error telemetry, or
  referrer output.

## Workstream 2 — Browser-chat policy parity

Extract or reuse one policy assembly path shared by normal and browser chat. The
result must carry:

- authenticated caller and tenant scope;
- agent visibility/access result;
- billing/subscription result;
- effective model/provider;
- effective visible and executable tools;
- ability and integration gates;
- destructive-tool confirmation requirements;
- user-scoped credential handles;
- rate/usage budget;
- storage-authority mode.

Do not pass an empty tool allowlist as `None` if `None` means unrestricted.
Credential resolution must remain request/user scoped; browser chat must not
publish one user's provider secrets into process-global environment state.
`BrowserAuthorityDB` may suppress transcript writes, but it must not bypass
control-plane authorization, billing, usage, credential, or tool-receipt writes.
Configured paid agents fail closed with a service/billing error when the billing
policy backend is unavailable.

Return an allowlisted browser agent/config DTO. Tests place credential/API-key
sentinels in every plausible agent, provider, tool, ability, and integration
field and prove those sentinels never reach the config response, SSE, IndexedDB,
export, or telemetry.

Remove the browser pre-resolution path that uses `apply_env=True`. The shared
loop's coroutine/request-scoped provider resolution is the only credential path;
resolution failure stops the turn before any model/tool call, and tests prove
concurrent users never mutate or consume each other's environment/credentials.

Make agent caching caller- and policy-aware. Cache only demonstrably public,
non-policy configuration, or key entries by caller/tenant and a policy epoch.
Access, tool, ability, credential, billing, and revocation changes invalidate
warm entries immediately. Failure to establish authoritative access fails
closed.

Add explicit tool metadata for supported authority modes. A
browser-incompatible tool is absent from config, prompt schemas, and execution;
a fabricated call for a hidden/denied/incompatible tool is rejected before its
handler runs.

Replace best-effort negative `credit()` settlement with an atomic debit/settle
operation tied to the durable turn/reference. Paid turns, retries, replays, and
disconnects charge exactly once; insufficient balance is denied; free/exempt
flows remain available; configured paid agents fail closed when billing storage
is unavailable.

Validate browser session IDs as bounded opaque identifiers before using them in
maps, receipts, billing, or persistence. Remove the unused request model override
or validate it against caller-visible model choices before it can influence
execution.

### Parity test matrix

Run each scenario through normal chat and browser chat and compare policy
decisions before any model/provider call:

- private, public, unlisted, disabled, and missing agent;
- owner, permitted user, anonymous/guest, wrong tenant, and revoked user;
- free, paid, exhausted, exempt, and failed billing state;
- no tools, explicit tool allowlist, hidden tool, disabled ability, disconnected
  integration, destructive confirmation, and unauthorized credential;
- model override allowed/denied and non-default engine;
- rate-limit and usage-accounting success/failure.

Go/no-go: browser chat cannot see or execute any tool, credential, model, or
agent that normal chat would deny, and it records the same billable usage and
side-effect receipts.

## Workstream 3 — IndexedDB data inventory

Create a machine-readable inventory checked into the repository. Each store
entry includes:

- purpose and schema version;
- authority classification: authority, cache, draft, receipt, or derived;
- data categories and examples;
- identifiers and tenant key;
- sensitivity and whether credentials/secrets are forbidden;
- writer and reader code paths;
- server sync/promotion destination;
- retention/TTL and size/quota limits;
- export representation, including Blob/base64 handling;
- erasure, logout purge, remote invalidation, and schema-migration behavior;
- encryption status and threat-model limitations.

Initial classification:

| Store | Expected classification | Required lifecycle |
|---|---|---|
| `sessions` | authority only when separately approved; otherwise cache | transcript/session TTL, export, purge, invalidation |
| `interactions` | authority only when separately approved; otherwise cache | transcript TTL, export, tombstone-safe purge |
| `agent_config` | derived cache | short TTL, version/hash invalidation, no secrets |
| `session_runs` | ephemeral coordination cache | short TTL, clear at terminal/recovery |
| `memories` | sensitive user data | explicit authority rule, export, erasure, logout purge |
| `genui_pages` | generated user content | TTL/authority rule, export, erasure |
| `genui_html` | active HTML/XSS-sensitive content | sanitization/trusted rendering, short TTL, purge |
| `user_files` | user content and Blobs | byte quota, export, erasure, MIME-safe rendering |
| `sync_outbox` | pending mutations/receipts | retry TTL, conflict handling, never silently discard |
| `tool_details` | potentially sensitive tool output | payload redaction, short TTL, export decision, purge |
| `attachments` (`webagent-attachments`) | sensitive user Blob data in a legacy global DB | tenant migration, byte-accurate quota, export, purge, object-URL revocation |

CI must compare `objectStoreNames` created by the current schema with the
inventory and fail on missing or extra entries.

Prefer folding browser attachments into the tenant-scoped database. If a
separate database remains necessary, name and key it by the same authenticated
tenant scope. Treat the legacy global database as migration/purge-only until it
is empty.

## Workstream 4 — Retention, export, erasure, and invalidation

### Retention and TTL

- Enforce policy on read and through bounded background cleanup.
- Distinguish metadata, transcript, run-state, generated HTML, files, pending
  sync, receipts, and tool-detail retention.
- Do not expire a pending mutation, unresolved conflict, or purge directive
  merely because ordinary cache TTL elapsed.
- Record last cleanup, rows/bytes removed, quota status, and errors without
  recording payloads.
- Account quota with actual Blob sizes across every browser database and
  `navigator.storage.estimate()`. Reserve capacity before writes and never evict
  authority or unsynchronized data silently.

### Export

- Export every inventoried store in one versioned JSON envelope.
- Take one consistent read-only transaction per database and record the database
  and schema versions used.
- Encode Blob/ArrayBuffer values as base64 with MIME type, size, and digest.
- Include unresolved conflicts and pending sync operations.
- Redact credentials, bearer tokens, provider secrets, signing material, and
  internal capability tokens.
- Fail or explicitly mark the export incomplete if a required store/value cannot
  be read; never silently return an empty collection for an error.

### Erasure and logout

- Account/session deletion completes server tombstones/erasure before local
  authority data is acknowledged as deleted.
- Logout behavior is policy-controlled: purge sensitive browser data by default;
  a retained cache requires explicit administrator policy and must not retain
  credentials.
- Remote device revocation keeps its purge directive until that exact tenant
  database is demonstrably cleared and acknowledged.
- Purge covers IndexedDB, local/session storage account mirrors, in-memory
  history/replay caches, relevant Cache Storage entries, and service-worker
  state containing user data.
- Purge revokes attachment object URLs and clears both current tenant databases
  plus any matching legacy attachment data.
- Partial failure remains visible and retryable.

Use one browser lifecycle coordinator for export and purge. A purge
acknowledgement includes the policy/schema/invalidation epoch plus the expected
and completed database/store set. The server accepts it only when every required
tenant store was cleared and verified; an attachment-only or partially failed
device must not acknowledge success.

### Remote invalidation

- Revision/schema/policy changes invalidate affected entries.
- Emergency rollback forces cache misses immediately on every instance.
- Agent config, tool visibility, ability, credential, billing, and user
  revocation changes have their own invalidation events; transcript revision
  alone is insufficient.
- Offline devices fail closed on reconnect before serving invalidated sensitive
  data.

### Browser lifecycle acceptance tests

- Enumerate `indexedDB.databases()` and every `objectStoreNames` in real
  Chromium; fail for an unregistered database/store.
- Seed unique values and real Blobs in all 11 stores for two tenants. Verify
  byte-for-byte export, then logout, revoke, expire auth, delete the account, and
  prove every target value is gone while the other tenant remains intact.
- Cover attachment-only devices, the legacy global attachment database, open-tab
  blocked deletion, and an injected per-store failure. Prove no purge
  acknowledgement occurs on partial failure and retry eventually succeeds.
- Exercise consistent export during concurrent writes and explicit
  partial/incomplete reporting.
- Test TTL at read, startup, and periodic sweep boundaries. Pending outbox,
  tombstone, receipt, and purge records remain until their stronger durability
  conditions are met.
- Fill quota with actual large Blobs across both databases. Accounting is
  byte-accurate, writes fail before partial persistence, and authority/dirty
  state is never silently evicted.
- Run remote invalidation across two tabs, two devices, online/offline/reconnect,
  policy/epoch changes, and BroadcastChannel failure fallback.

## Workstream 5 — Memory-only sensitive-deployment mode

Add an administrator policy with at least:

- `persistent_cache`: validated IndexedDB caching permitted;
- `memory_only`: browser state exists only for the active page/process;
- `disabled`: browser session caching/authority code paths unavailable.

In `memory_only` mode:

- do not open or create the tenant IndexedDB;
- do not open or create the attachment IndexedDB;
- do not persist transcript, files, generated HTML, memories, tool details,
  sync data, or agent configuration;
- do not persist remember credentials or authenticated account mirrors in
  `localStorage`;
- clear previously created tenant databases when policy changes to memory-only,
  with visible acknowledgement/failure;
- expose the effective mode in the admin UI, routing response, diagnostics, and
  account disclosure/export.

Static application assets may remain browser-cacheable only when they contain no
tenant/user data.

Real-Chromium acceptance tests spy on/fail `indexedDB.open`, exercise
chat/attachment workflows for the tab lifetime, reload, and prove no WebAgent
tenant database, transcript, attachment, account mirror, remember credential,
or user-data Cache Storage entry was created or retained.

## Workstream 6 — CSP and XSS regression program

1. Inventory inline scripts, inline styles, dynamic script creation,
   `innerHTML`/`outerHTML`, `insertAdjacentHTML`, `eval`/`Function`, template
   injection, generated UI HTML, Markdown rendering, SVG, and Blob URL sinks.
   Replace raw tool/SSE/attachment/filename/error sinks with one reviewed
   sanitizer/renderer or `textContent`.
2. Add CSP in report-only mode with per-response nonces or hashes:
   `default-src 'self'`, narrowly scoped `connect-src`, `img-src`, `media-src`,
   `worker-src`, `frame-src`, `object-src 'none'`, and `base-uri 'none'`.
3. Remove violations and third-party origins one by one. Do not treat
   `unsafe-inline` or `unsafe-eval` as the finished policy.
4. Enforce CSP after the violation budget reaches zero for supported workflows.
5. Add Trusted Types in report-only mode where Chromium coverage is useful, then
   enforce it for high-risk sinks.
6. Remove or isolate debug `eval`; the enforced application policy must not
   contain `unsafe-eval`.
7. Move active user-controlled HTML/SVG off the credentialed app origin or serve
   it with download/sandbox/nosniff controls that prevent same-origin execution.

Regression payloads must cover stored and reflected content in messages, tool
results, Markdown, filenames, attachment metadata, memories, generated UI,
agent configuration, error text, and imported/exported browser data.

Tests must demonstrate that injected scripts/event handlers/URLs do not execute.
Also include a sentinel IndexedDB value and verify attempted XSS cannot exfiltrate
it under the enforced policy. This test validates XSS prevention; encryption is
not credited as the reason the read failed.

## Workstream 7 — Browser-cache encryption

Treat encryption only as defense-in-depth against copied browser storage or
casual disk/profile inspection.

Before adding it, document:

- threat model and explicitly excluded same-origin XSS threat;
- Web Crypto algorithm, authenticated context, schema/version binding, and key
  rotation;
- key storage/wrapping, recovery, logout, revocation, export, and deletion;
- behavior when keys or ciphertext are corrupt or unavailable;
- performance and quota impact.

Never store provider credentials in browser cache merely because encryption is
enabled. Product/admin copy must not imply that encryption protects data from a
successful same-origin script execution.

## Workstream 8 — Interrupt fallback cleanup

Remove the empty-user placeholder session fallback from `LocalBackend.set_interrupt`.

Use separate behavior for the two authority modes:

- server-authority interrupt: require authenticated user scope, verify the
  session exists and is owned/participated by that user, then write an
  ownership-scoped interrupt record;
- browser-authority interrupt: address the in-memory active-turn registry by
  `(user_id, session_id)` and return `not_found` when no matching turn exists;
  do not create a server session or orphan interrupt row.

Migrate/clean existing pollution:

- identify `sessions.user_id=''` rows attributable to the fallback;
- delete only rows proven to be empty placeholders with no legitimate child
  data;
- report ambiguous rows for operator review;
- remove orphan `session_interrupts`;
- restore an enforceable ownership relationship in the schema, either through a
  valid session foreign key or explicit `user_id` plus composite ownership
  validation.

### Acceptance tests

- interrupting a missing session creates zero rows;
- one user cannot interrupt another user's server or browser turn;
- a browser interrupt never writes a server session;
- an owner-scoped control-plane interrupt reaches the correct turn across
  workers/restart without creating a transcript session;
- stale interrupts expire under an explicit TTL;
- cleanup is idempotent and never deletes a legitimate session.

## Delivery sequence

1. Land the IndexedDB inventory and security-policy configuration schema.
2. Restrict CORS and add Origin/WebSocket request-integrity tests.
3. Enforce authentication/object authorization across the chat/browser route
   matrix, then extract browser/normal chat policy parity and land matrix tests.
4. Remove the interrupt placeholder fallback and clean proven pollution.
5. Implement enforceable retention, complete export/erasure, and remote
   invalidation for every inventoried store.
6. Add and test `memory_only`/`disabled` modes.
7. Deploy CSP/Trusted Types report-only, remediate violations, then enforce CSP.
8. Complete security/privacy review and update operator/user disclosure.

## Phase 4 completion gate

Phase 4 is complete only when:

- production CORS has no wildcard or `"null"` authenticated origin;
- unsafe requests and WebSockets enforce configured origins;
- all non-public chat/browser routes require an authenticated caller and enforce
  tenant/session ownership;
- browser chat passes the normal-chat policy parity matrix;
- the IndexedDB inventory exactly matches the implemented schema;
- the legacy global attachment database is migrated or empty and cannot retain
  bytes after a successful purge acknowledgement;
- every store has enforced TTL, export, erasure, logout, and invalidation rules;
- memory-only mode leaves no tenant IndexedDB or persistent credential/account
  mirror;
- CSP is enforced for the supported UI without broad bypass directives;
- XSS regressions cannot execute or read the IndexedDB sentinel;
- browser encryption claims explicitly exclude same-origin XSS;
- interrupting a missing/browser-owned session cannot create an empty-user
  server session;
- security, privacy, and compliance evidence is attached to the release record.

Until that gate passes, keep:

```text
WEBAGENT_ENABLE_BROWSER_AUTHORITY=false
```
