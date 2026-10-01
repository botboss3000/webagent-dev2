---
id: v3_migration
title: Rust v3 migration guide
when_to_use: Use when investigating or extending the Rust v3 app, porting actual v2 UI components, or implementing v3 backend support for a v2 feature.
default_mode: discoverable
minimum_access: registered
---

# Rust v3 — port the UI, implement the feature

## Direction and boundaries

V3 is an independent Rust application in `v3/`, built incrementally from v2's working features. The goal is to move the **actual v2 UI** over and implement the Rust backend contracts that make each feature work—not build a lookalike, connect v3 to the v2 backend, or treat copied markup as feature parity. V2 remains independently available.

Rust owns authentication, APIs, provider requests, policy/approval gates, run state, recovery, and storage. An isolated Python worker performs permitted filesystem and process operations. A separate promotion broker handles approved live-source changes. Keep those boundaries intact; loading an ability never bypasses policy.

Read `CLAUDE.md`, `v3/README.md`, and `v3/docs/ui-journeys.md` first. They are the current operational references; recheck source and the target deployment rather than treating this guide as a permanent capability inventory.

## Source map and current baseline

- `v3/src/main.rs`: API routing and run orchestration; `provider.rs`: subscription adapter; `store.rs`: persistence; `abilities.rs`: catalog and effective tool discovery.
- `v3/worker.py`: workspace tool execution. `v3/deploy.sh`, `Dockerfile`, and `launch.py`: build, mounts, and startup.
- `v3/ui/`: independent shell, chat partial/styles, and page modules. Pages under `main-panel/<id>/` use `page.json`; the Rust `/api/v3/pages/catalog` discovers them. Entry modules export `start(mount)`.
- Root `ui/` and `index.html`: v2 reference implementations. V3 already reuses selected v2 CSS/assets and the Agents partial. Inspect the exact component's markup, styling, events, lifecycle, and API callers before adapting it; retain shared design tokens and sister-panel consistency.

The documented baseline includes setup/account, Agents model/ability controls, chat, approvals/recovery, and Codebase Admin workspace tools. This is **not full v2 parity**: other Administrator abilities can be catalog-only, and v2 controls may remain unsupported. A manifest, displayed row, or saved visibility mode does not establish executable support.

## Port one complete feature at a time

1. Trace the v2 control through UI events, API request/response shapes, authorization, persistence, and runtime effects. State exactly what will be reused and any intentional v3 differences.
2. Define and implement the matching `/api/v3` contract in Rust, including validation, ownership, CSRF for browser writes, policy/approval checks, durable state, and explicit errors. Preserve run pinning, cancellation, shutoff, and recovery semantics where applicable. Reject unsupported operations instead of returning fake success.
3. Adapt the real component in `v3/ui`; keep v3-only behavior out of shared v2 files. Mark pending features honestly until the backend and execution path exist. Do not import v2 accounts, sessions, credentials, or provider tokens.
4. Test the Rust contract and worker boundary, then exercise the authenticated v3 control end-to-end. Verify persistence after reload, failures, both themes, and mobile layout. Resolve routes from the **v3** journey registry, not v2's. Capture a final screenshot and visible assertion; API tests alone do not prove a UI port.

## Deployment and workspace discipline

The README records v3 development at `https://dev.webagent.live/app` on host port 8081, separate from v2 on 8080; confirm the active target before acting. Mounted UI edits generally need a browser refresh. Rust/worker/dependency edits require an image rebuild and container recreation; mount or environment changes require recreation. Skill/manifest changes require a v3 restart to reload. Do not restart v2 to validate v3.

Within v3, use `workspace_seed`, edit the isolated snapshot, verify with bounded `cargo check` / `cargo test --locked`, inspect a complete `source_review`, and use `promotion_propose`. Workspace edits are not live deployment. Follow the broker's allowed paths; UI promotion is currently excluded. Use authenticated APIs for live state, never shell/Python to open v3's encrypted application databases or keys.

## Investigation lesson — historical, not a live diagnosis

Session `403498b7-6822-44e9-adae-a2fd9ce32600` found provider HTTP/stream errors discarded in `provider.rs` and collapsed into generic retry/failure text in the runner. It did **not** establish the underlying chat failure or account/model entitlement. Preserve sanitized actionable errors; do not leak tokens or provider payloads.

That session added `/api/v3/admin/messaging` create/send/read routes in source but reported no deployment or compiled-test verification. Machine access is disabled without `V3_MESSAGE_API_TOKEN`; it is narrowly scoped and does not approve tools or unlock settings. HTTP 202 means a run was accepted, not that chat succeeded: poll its status/reply. Check current code, configuration, tests, and live evidence before claiming this API—or any migrated feature—works.
