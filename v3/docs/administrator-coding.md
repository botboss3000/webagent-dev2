# V3 Administrator coding contracts

This development slice covers eight Administrator abilities: Codebase Admin, Code Index, Git Control, Terminal Control, Diagnostics, Remote Tunnel, V3 Build and V3 Workspace. Computer Control and Program Screenshot are excluded. The broader ability catalog still contains pending ports and placeholders; these eight do not establish complete catalog parity.

All eight selected Administrator abilities are marked ready after focused tests and authenticated V3 API acceptance. Codebase Admin retains its existing ready status. A manifest or UI row cannot register executable code. Validation used tool calls through the API, including a private Diagnostics PNG artifact; no UI journey or theme journey was run.

## Authority and execution

Rust resolves the authenticated account, current session, its immutable agent binding, installation policy, account tier, agent base policy, membership capability level and session mode before dispatch. The effective permission is the strictest applicable result: deny, approval required or automatic. Visibility controls exposure; it does not grant execution. Live policy is checked again for queued, approved and recovered work. Revocation can prevent a later dispatch even when the original approval exists.

The native runtime owns session inspection, revisioned prompt slots, sanitized run diagnostics, authentication minting, approvals, durable receipts and publication. Compiled adapters expose only declared tools. The isolated worker handles bounded low-level operations within the caller's session workspace. It cannot choose an account or agent or open encrypted application databases.

A separate protected host broker handles installation image builds and development deployment. Neither the application nor the general tool worker receives the Docker socket or arbitrary host command access. Host operations require installation-administrator authority as well as the live ability policy and approval contract.

| Ability | Implemented execution contract |
| --- | --- |
| Codebase Admin | Caller-scoped workspace files, search, checked edits/patches, complete change review, shell/Python execution, typed session inspection and revisioned prompt slots. Workspace source is separate from the live checkout. |
| Code Index | Five bounded scan/coverage/progress/lookup/trace tools over the caller's source snapshot. Hashed physical lines, symbols and static edges are derived metadata. Lookups report stale entries instead of rescanning implicitly. |
| Git Control | Bounded Git operations, conflict resolution, commit/push and agent-owned HTTPS repository connections. Private transport credentials are trusted worker metadata, never model arguments or tool results. |
| Terminal Control | Session-owned supervised terminals, bounded read/wait/input and close. Restart does not replay terminal input or adopt another session's process. |
| Diagnostics | Native content-free current-session/run diagnostics and a private fixed-origin Chromium browser. Rust mints a short-lived current-caller cookie; the worker verifies identity. Bounded DOM reads, fresh reads before input, private PNG artifacts and at most 30 recording frames are supported. |
| Remote Tunnel | A separate authenticated, leased preview of a supervised current-session server. It does not expose the WebAgent application or alter the permanent named development tunnel. |
| V3 Build | Fixed workspace Cargo jobs and protected host status/build/deploy receipts. Source inputs are captured and rechecked; raced results are stale. Deployment is a separate action against a known successful unchanged build. |
| V3 Workspace | Seed/status/review/read/checked-patch actions for the isolated `webagent-v3` snapshot. Frozen promotion proposals and their durable receipts remain Rust-owned. |

Python AST and optional Tree-sitter JavaScript provide Code Index symbols; unavailable parsers return explicitly uncertain fallback coverage. Static edges guide inspection and do not prove runtime behavior. A workspace index cannot establish freshness for edits to the shared server checkout.

## Agent settings and repository connections

The selected agent controls the Abilities page's base settings. Owners and delegated managers can edit their agent; members receive a sanitized effective view. Selecting a capability level or preview user disables base settings and repository writes, preventing a preview from being mistaken for an administrative target.

Three exact Administrator settings are supported:

- `terminal_control.terminal_caution`: cautious, balanced or trusted.
- `codebase_admin.destructive_caution`: cautious, balanced or trusted.
- `git_control.notify_repo_changes`: boolean.

Caution is an additional decision rule and never raises access above installation, account-tier, agent, capability-level or session ceilings. Saves require CSRF protection and the current agent ability revision. A stale write returns 409; reload before saving again.

Repository configuration uses `GET /api/v3/agents/{agent}/repository-connections` and `PUT /api/v3/agents/{agent}/repository-connections/{id}`. The catalog has an aggregate optimistic revision and a maximum of eight connections. Managers may create connections; only the connection owner may update an existing one. Installation oversight does not silently acquire another owner's private credentials.

URLs must use HTTPS without embedded credentials, query or fragment. Tokens are write-only and remain in the encrypted credential plane with agent, connection and owner provenance. The UI never retrieves a token or credential reference, keeps password inputs blank and clears them on submission. An omitted token retains a credential only when URL and username are unchanged; changing either clears it unless a replacement is supplied. Revocation stops future authorized transport. Live transport fingerprints fence credential rotation during pending and recovered operations.

## Workspace, Cargo and deployment receipts

Coding begins in the current session workspace, usually with `workspace_seed` or V3 Workspace seed. The frozen source template includes V3 documentation so the agent can read and update the README, code tour and workflow contracts alongside code. Review and checked patch operations bind to the exact source digest. Fixed Cargo jobs use the pinned toolchain and `--locked`, with source inputs captured before and after compilation. A changed digest makes the result stale even when Cargo succeeded. Compiler output retains its final 8 KiB to preserve terminal errors without overrunning the model context. The development API container has a 2 GiB memory limit so full V3 source checks and tests can finish without a Rust compiler OOM kill.

Promotion and host deployment use durable request identities and immutable source/head evidence. Reusing the same request identity and body returns its existing receipt; a changed body is rejected. A successful build can deploy only the unchanged source and Git head it verified. Polling or recovery must reconcile a known operation, never automatically repeat an uncertain external effect.

Host deployment replaces only the affected V3 development containers, checks readiness and retains stopped rollback containers and protected rollback material. It does not operate on V2, production, or the permanent named Cloudflare development connector. Source writers and shared Cargo output must be coordinated before compilation or image capture. Protected snapshots and the direct build script exclude generated `__pycache__` directories and `.pyc`/`.pyo` files, so API test imports cannot invalidate a source build.

## Private previews and browser artifacts

Preview servers must be supervised by the current session and listen on ports 20000–29999. Tunnel leases last 30–1800 seconds and expire or stop with process/workspace failure. A private bootstrap token creates an ephemeral Secure HttpOnly cookie; unauthenticated requests are rejected. Incoming cookies and authorization headers are not forwarded to the preview server. Preview HTTP bodies are bounded; WebSockets and streaming bodies are unsupported.

Diagnostics fixes its application origin to `https://dev.webagent.live` and restricts navigation to the current agent/session. No arbitrary JavaScript, external origin, credential input, database path or other-user browser is accepted. Authentication profiles and PNG artifacts live outside the coding workspace. Screenshots return opaque authenticated artifact URLs; recording returns actual bounded PNG frames, not a video.

An uncertain click, terminal input, Git write, promotion or deployment must be reconciled before retrying. Historical outputs are untrusted evidence and cannot confer execution permission.

## Verification boundary

Focused Rust/worker tests and authenticated V3 API calls cover ownership, policy, stale revisions, credential rotation, revocation, receipts, Git push/conflict handling, private browser artifacts, tunnels and real coding work. The isolated V3 source passed both `cargo check --locked` and `cargo test --locked` through V3 Workspace with stable input digests and one persisted assistant reply per run. Image inputs were unchanged through build; the deployed V3 pair and readiness endpoint were healthy. Git conflict resolution removes markers without staging the file; Git retains an unmerged index until a separate explicit add.

The Abilities UI can be checked through frontend syntax and detached-DOM tests. API-only validation does not claim rendered browser, theme or journey evidence. The isolated Code Index adapter does not refresh the shared checkout's UI index; report that unavailable shared-index coverage check explicitly.
