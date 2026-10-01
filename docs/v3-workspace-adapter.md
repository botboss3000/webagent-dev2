# Private v3 workspace adapter

This is a trusted-administrator development service, separate from browser login,
v3 application accounts, the tool worker, and the privileged Docker build worker.
It implements isolated source preparation. It cannot apply live source or deploy.
A frozen adapter proposal is NOT automatically a v3 session promotion proposal.
No v3 schema, provider credentials, accounts or runtime data are imported.

## Delivered tools

The drop-in `v3_workspace` Administrator ability provides:
- `v3_workspace_status`: connection and fixed-operation status.
- `v3_workspace_action`: seed, read, review, patch, check, test or propose.
- `v3_workspace_receipt`: read an original durable operation receipt.

All calls require a signed-in platform administrator whose ID matches the
server-configured mapping. Action is mutating/approval-gated, including its read
variants; use the separate status and receipt tools for read-only inspection.
This implementation does not enable the ability or widen any existing agent.

## Install (trusted operator)

1. Synchronize `scripts/v3_workspace_worker.py`, the service unit, and the new
   ability to the development host. The service imports the reviewed v3 worker
   helpers from this checkout; keep that checkout operator-controlled.
2. Create the unprivileged `webagent-v3-workspace` service account. Give it read
   access to the checkout and write access only to its external state directory.
   Install Rust 1.90, Cargo, Git, a C compiler and dependency-download access on
   this host. Cargo absence produces a scoped blocked result. Do not grant Docker.
3. Generate a separate 32–256 character non-space ASCII credential directly into
   `/etc/webagent-v3-workspace-worker.env` as `V3_WORKSPACE_WORKER_TOKEN=...`.
   Keep the file root-owned mode 600; never print it, send it to chat, or commit it.
4. Install the unit under `/etc/systemd/system/`, check the actual checkout path
   and private bind address, then enable/start `v3-workspace-worker.service`.
   The supplied address is the development host's private Tailscale address.
   Enforce private-network ACLs/firewalls. Use TLS unless transport is encrypted
   and confined to the trusted private network. Never publish this endpoint.
5. Configure the WebAgent server through protected environment configuration:
   `V3_WORKSPACE_WORKER_URL=http://100.112.23.15:8093`,
   `V3_WORKSPACE_WORKER_CLIENT_TOKEN=<same new credential>`, and
   `V3_WORKSPACE_WORKER_CALLER_ID=<exact signed-in administrator ID>`.
   Restart/recreate as required to load environment and the new drop-in catalog.
   Grant the new ability only to a trusted development agent through normal admin
   configuration. Do not change the currently running agent's permissions.

## Protocol and workflow

Every request uses `Authorization: Bearer <credential>`; no redirects or query
credentials. GET `/v1/status`, GET `/v1/receipts/<request_id>`, and POST `/v1/actions`.
An action has exactly `{operation, workspace_id, request_id, arguments}`. Workspace
IDs are canonical UUIDs; request IDs are 1–80 alphanumeric/underscore/hyphen chars.
The state directory must be outside the checkout. A new workspace imports only
allowlisted v3 source roots/files and creates an independent clean Git baseline.

1. Status; seed with `{}`.
2. Read `{path, offset?, limit?}`; paths are relative to the seeded v3 root.
3. Review with `{}`. Require `truncated=false` and a non-null `review_sha256`.
4. Patch `{patch, expected_review_sha256}` using a text-only unified diff.
5. Fresh review; check/test `{expected_review_sha256, timeout_seconds?}`.
   These run only fixed `cargo check/test --locked` commands and execute trusted
   repository code. Check both status and `source_changed`; verification of an
   earlier digest does not verify later changes.
6. Fresh complete review; propose `{expected_review_sha256, verification_summary}`.
   This freezes a patch under the external state directory and marks it unapplied.

Every operation, including read/review, has a receipt. Same ID/input returns the
original receipt; use a new ID for a fresh observation. Changed retries are
rejected. After a transport timeout, inspect the receipt and current source.
Restarted running receipts become uncertain and are not re-executed automatically.
Precheck/dispatch failures are conservatively marked uncertain; inspect before
another mutation. Service state/control/shutoff disables new actions. This is
separate from v3's own global shutoff: there is no claim of inherited runtime policy.

## Promotion/deployment boundary

The first version intentionally has no apply endpoint. A reviewed proposal needs
an explicitly authorized import/approval step into the existing promotion broker
(or trusted operator application), preserving its path allowlist and stale-source
checks. This integration is not delivered here and must not be implied by a
`proposed` result. Build/deploy use the existing dedicated build service only after
live source has been deliberately promoted. Deployment requires explicit approval.

## Security and verification

This is trusted-admin infrastructure, not a hostile-code sandbox. Cargo tests and
build scripts execute repository code under the service account. They are not given
the service credential in environment, but share the service UID and can access its
state; same-UID process isolation is not a security guarantee. Only trusted reviewed
source should run here. Strong adversarial isolation requires a separate build
executor identity/container and separate control-state access before enabling
untrusted authors. Systemd limits host writes, but do not claim per-workspace isolation.

Focused mocked/local tests: `python -B -m pytest -q -p no:cacheprovider
tests/test_v3_workspace_adapter.py`. Tests use temporary source and state; they
prove contracts, not installed-host connectivity, compiled v3 correctness or a
live promotion. No UI paths are changed by this adapter implementation.

## Reuse existing browser login (read-only fallback)
Without V3_WORKSPACE_WORKER_URL, the tool calls the existing login_as_current_user_v3 bridge. No second workspace credential is required for this fallback. The bridge must already be configured. Verified identity and origin are checked; only allowlisted GET endpoints are fetched; the controlled browser is closed afterward.
Supported: status, source review for an existing v3 session, and that session's promotion receipt. In this transport workspace_id identifies the v3 session; receipt lookup also requires it. Unsupported: seed, file read, patch, Cargo check/test and propose. V3 currently provides no direct HTTP endpoints for those operations. The bridge does not bypass v3 tool approvals. A configured worker never silently switches transports on failure.
