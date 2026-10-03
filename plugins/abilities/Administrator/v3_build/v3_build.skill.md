# V3 Build

Use for compiling/testing the repository's v3 Rust application and building its development image. Run v3_build_status first in the execution environment that will do the build. A plugin supplies structured operations, not missing infrastructure.

Native tasks require Rust 1.90, Cargo, a C compiler for bundled SQLite, dependency-download access (or a populated cache), and writable temporary/cache space. Image builds require a reachable Docker daemon and registry access; the pinned Dockerfile supplies Rust and runtime dependencies. Builds can consume substantial disk, memory, and time. Cargo and Docker execute repository build code, so grant this ability only to trusted agents/callers.

No arbitrary commands, paths, image tags, installation, Docker socket mounting, service lifecycle changes, or deployments. This ability runs on the WebAgent server, not a user's device or an SSH host. If infrastructure is missing, report the exact prerequisite; arrange a trusted build host separately rather than widening privileges. A successful image build is not deployment or a working chat verification.

# Rust v3 — port the UI, implement the feature

## Direction and boundaries

V3 is an independent Rust application in `v3/`. The v2 reference repository is in **`v3/v2_ref/`**. The goal is to adapt the **whole reference repository** to the v3 app piece by piece, including the actual v2 UI and the Rust backend contracts that make each feature work—not build a lookalike, connect v3 to the v2 backend, or treat copied markup as feature parity. V2 remains independently available.

### Reference repository as migration tracking

Treat `v3/v2_ref/` as the remaining migration inventory, not a permanent duplicate. As functions are fully adapted and verified in v3, delete those adapted functions from the corresponding reference files while retaining the not-yet-adapted parts. Once a whole file has been fully adapted and verified, delete that file from `v3/v2_ref/`. Do not remove incomplete work or unrelated reference code, and do not delete from the independently running v2 repository. The eventual goal is for `v3/v2_ref/` to contain only the bits deliberately unwanted in v3, making what remains versus what has actually moved over clear. Preserve intentionally unwanted bits there rather than deleting them as if migrated.

For **UI files**, port each file **completely**, including its markup, styles, interactions, lifecycle, and applicable supporting assets; adjust its API endpoints and request/response handling for v3 where applicable. A partial UI copy is not a completed port. Remove a UI reference file only after the complete port and its applicable v3 backend behavior have been verified.

Rust owns authentication, APIs, provider requests, policy/approval gates, run state, recovery, and storage. An isolated Python worker performs permitted filesystem and process operations. A separate promotion broker handles approved live-source changes. Keep those boundaries intact; loading an ability never bypasses policy.

Read `CLAUDE.md`, `v3/README.md`, and `v3/docs/ui-journeys.md` first. They are the current operational references; recheck source and the target deployment rather than treating this guide as a permanent capability inventory.

## Source map and current baseline

- `v3/src/main.rs`: API routing and run orchestration; `provider.rs`: subscription adapter; `store.rs`: persistence; `abilities.rs`: catalog and effective tool discovery.
- `v3/worker.py`: workspace tool execution. `v3/deploy.sh`, `Dockerfile`, and `launch.py`: build, mounts, and startup.
- `v3/ui/`: independent shell, chat partial/styles, and page modules. Pages under `main-panel/<id>/` use `page.json`; the Rust `/api/v3/pages/catalog` discovers them. Entry modules export `start(mount)`.
- `v3/v2_ref/`: v2 reference repository and remaining migration inventory; its `ui/` and `index.html` contain the UI reference implementations. Inspect the exact component's markup, styling, events, lifecycle, and API callers before adapting it; retain shared design tokens and sister-panel consistency. Port UI files completely and adjust endpoints for v3 where applicable. Remove verified adapted functions/files from this reference directory as described above, leaving pending and intentionally unwanted bits.

The documented baseline includes setup/account, Agents model/ability controls, chat, approvals/recovery, and Codebase Admin workspace tools. This is **not full v2 parity**: other Administrator abilities can be catalog-only, and v2 controls may remain unsupported. A manifest, displayed row, or saved visibility mode does not establish executable support.

## UI migration scope and lessons

- **Port supported functions by default.** Preserve v2 behavior unless checked evidence shows it is dead, stale, deliberately unwanted, or already replaced by an equivalent v3 implementation. A simpler implementation is not proof of equivalence. Trace callers, consumers, events, assets, and API dependencies first; verify existing equivalents and fill gaps rather than duplicate them.
- **Keep UI migration separate from new feature development.** During a UI-porting task, leave a function completely unported in `v3/v2_ref/` if it requires new backend endpoints, contracts, services, or other development outside that task. Report the missing dependency; do not add unused UI fragments or expand scope. For example, browser-error reporting remains pending until a supported v3 diagnostics intake exists. The feature-development steps below apply only when that broader work is in scope.
- **Respect v3 Content Security Policy.** Move v2 inline scripts into external scripts when required, preserving execution order. Do not weaken CSP to make copied code work. Use CSP-compatible verification rather than harnesses requiring `eval` or dynamic code construction.
- **Verify behavior and lifecycle, not appearance alone.** For startup/animation ports, assert readiness gates, event order and counts, timers, reduced-motion behavior, and cleanup. For carousels, verify overflow, edge controls, active-tab visibility, scrolling, and spring-back. A screenshot alone cannot detect duplicate lifecycle events.
- **Clean migrated remnants as part of completion.** After verification, remove the adapted function and exclusively owned wiring from the reference: obsolete listeners, observer callbacks, dangling calls, empty script tags, and inaccurate comments. Preserve shared and unported logic. Search for removed symbols afterward. Partial function migration does not justify deleting the containing UI file.
- **Separate simulated checks from device confirmation.** Synthetic gesture tests establish handler behavior, not physical-device behavior; report that limitation. Record explicit user confirmation on a real device as additional acceptance evidence, not as a new change request.

## Port one complete feature at a time

1. Trace the v2 control through UI events, API request/response shapes, authorization, persistence, and runtime effects. State exactly what will be reused and any intentional v3 differences.
2. Define and implement the matching `/api/v3` contract in Rust, including validation, ownership, CSRF for browser writes, policy/approval checks, durable state, and explicit errors. Preserve run pinning, cancellation, shutoff, and recovery semantics where applicable. Reject unsupported operations instead of returning fake success.
3. Port the complete real UI files from `v3/v2_ref/` into `v3/ui`, including markup, styles, interactions, lifecycle, and supporting assets; adjust endpoints and request/response handling for v3 where applicable. Keep v3-only behavior out of shared v2 files. Mark pending features honestly until the backend and execution path exist. Do not import v2 accounts, sessions, credentials, or provider tokens.
4. Test the Rust contract and worker boundary, then exercise the authenticated v3 control end-to-end. Verify persistence after reload, failures, both themes, and mobile layout. Resolve routes from the **v3** journey registry, not v2's. Capture a final screenshot and visible assertion; API tests alone do not prove a UI port.
5. After verification, delete the fully adapted functions or whole files from `v3/v2_ref/` only. Retain pending portions and intentionally unwanted bits so the reference directory accurately tracks the remaining migration inventory.

## Deployment and workspace discipline

The README records v3 development at `https://dev.webagent.live/app` on host port 8081, separate from v2 on 8080; confirm the active target before acting. Mounted UI edits generally need a browser refresh. Rust/worker/dependency edits require an image rebuild and container recreation; mount or environment changes require recreation. Skill/manifest changes require a v3 restart to reload. Do not restart v2 to validate v3.

### Repository development (external WebAgent / Codebase Admin)

The `v3/` directory is part of the development checkout. An authorized repository-development agent may edit it directly using the normal checked-out source tools; browser login, an isolated v3 runtime session, and promotion tools are not prerequisites for source development. Preserve unrelated changes and follow Codebase Admin checkout coordination.

Before overlapping edits or compilation, inspect the current diff and other ongoing v3 tasks/sessions and active build jobs. Identify shared files and coordinate ownership rather than overwriting another task's work. Do not start competing builds or deploy a snapshot while another task is midway through shared-file edits. Serialize shared build/deploy jobs or use an explicitly isolated build snapshot/target directory; record which source revision/diff was verified. If another task changes a dependency of the result, revalidate before claiming completion. Do not block unrelated source edits merely because another task exists.

Direct source edits are not deployment. Run bounded verification, review the complete diff, and keep image rebuild/container recreation separate under the existing deployment authorization rules.

### Tools executed by the v3 runtime agent

Inside v3's own restricted runtime, use `workspace_seed`, edit the isolated snapshot, verify with bounded `cargo check` / `cargo test --locked`, inspect a complete `source_review`, and use `promotion_propose`. Workspace edits are not live deployment. Follow the broker's allowed paths; UI promotion is currently excluded. These runtime restrictions do not redefine the external repository-development workflow above.

For either workflow, use authenticated APIs for live state; never use shell/Python to open v3's encrypted application databases or keys.

## Investigation lesson — historical, not a live diagnosis

Session `403498b7-6822-44e9-adae-a2fd9ce32600` found provider HTTP/stream errors discarded in `provider.rs` and collapsed into generic retry/failure text in the runner. It did **not** establish the underlying chat failure or account/model entitlement. Preserve sanitized actionable errors; do not leak tokens or provider payloads.

That session added `/api/v3/admin/messaging` create/send/read routes in source but reported no deployment or compiled-test verification. Machine access is disabled without `V3_MESSAGE_API_TOKEN`; it is narrowly scoped and does not approve tools or unlock settings. HTTP 202 means a run was accepted, not that chat succeeded: poll its status/reply. Check current code, configuration, tests, and live evidence before claiming this API—or any migrated feature—works.

## Dedicated worker
When V3_BUILD_WORKER_URL is configured, status probes the host worker. Use v3_worker_submit for build, then poll v3_worker_job. Deploy only the successful build_job_id after explicit deployment authorization. Keep identical request_id/body on uncertain retries. Never substitute local Docker when remote configuration fails. See docs/v3-build-worker.md for host installation; do not expose the worker publicly or store tokens in chat/source.

## Authenticated v3 browser validation

`login_as_current_user_v3` belongs to **V3 Build**, not Diagnostics. It runs on the v2 WebAgent server to authenticate its owned/shared server browser against the independent v3 development app. It is not a tool implemented inside the v3 isolated worker and does not provide cross-version SSO or import v2 credentials. Browser Control must also be enabled to exercise the resulting browser session.

A trusted administrator must configure `V3_BROWSER_LOGIN_TOKEN` (a separate secret, 32–256 printable non-space ASCII characters) and `V3_BROWSER_LOGIN_CALLER_ID` (the exact signed-in v2 administrator ID) on both the WebAgent tool server and the v3 server. Configure `V3_DIAGNOSTICS_ORIGIN` on the tool server; it defaults to `https://dev.webagent.live`, accepts an HTTPS origin or loopback HTTP, and cannot be selected by tool input. Deploy the Rust `/api/v3/diagnostics/browser-login` endpoint and recreate services after environment changes. Never put these secrets in chat, source, page JavaScript, or the isolated worker. The bridge is disabled when its credential/mapping is absent, and is separate from `V3_MESSAGE_API_TOKEN` and the build-worker credential.

1. Resolve the affected route from `v3/docs/ui-journeys.md` or the v3 page module. Call `login_as_current_user_v3(path="/app?tab=account")`, replacing the path with that relative route. Omit `browser_session_id` unless explicitly targeting an owned/shared tab. The tool checks the run-bound caller and platform-admin role, selects the server-backed browser, installs a ten-minute HttpOnly session cookie, navigates, and verifies `/api/v3/auth/me`.
2. Require `status="ok"`, `application="v3"`, `backend="server"`, and `verified.http_status=200`, `verified.user_id="admin"`, `verified.is_admin=true`. This is an explicit mapping to v3's existing independent administrator, not an assertion that v2 and v3 user IDs match. No token, password, cookie value, or CSRF credential is returned. A failed or blocked result is not a verified login; report the exact configuration/deployment limitation.
3. Load Browser Control's `browser_action` and use the returned `browser_session_id` for reading and acting on the v3 page. **Do not use v2's `dev_browser_action` or `dev_browser_url` facade:** those are bound to v2's localhost origin and identity contract. Navigate → read → act → assert the requested visible state, then reload to check persistence. Check relevant themes/layouts and failures. Capture a final screenshot and share it with the user. Close the controlled browser when finished unless a live handoff is required.
4. Record the actual origin, route, actions, visible assertion, screenshot, and pass/fail/blocked result. Login success alone, source tests, or a successful image build do not prove an app workflow works. An HTTP 401/403/404 or identity mismatch invalidates that journey; re-establish authentication before drawing capability conclusions. Re-authenticate if the ten-minute session expires.
