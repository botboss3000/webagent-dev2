# V3 Build bridge from V2

Use this ability only for an authorized V2 administrator updating the V3 development app. The Python bridge reaches V3's protected native broker with its own authenticated identity; it shares the durable task registry with V3 and Codex. Read the checkout-root `AGENTS.md` and `v3/README.md` first.

1. Call `v3_build_work_status`, then `v3_build_work(action="begin")` with a distinct stable task ID. Begin creates an isolated V3 source snapshot. The receipt supplies its workspace path and initial digest. Other tasks may edit or release in parallel.
2. Edit and review in V3 Workspace. Its proposal is an isolated artifact, not an applied release. Its seed can lag the latest release; use `v3_source_read` to inspect the broker task snapshot and rebase the diff if needed. Pass the reviewed text diff to `v3_source_apply` with the open task ID, a stable request ID, and the task's current `workspace_inputs_sha256` from `v3_build_work_status`. The broker applies only to that task snapshot after path, digest and patch checks. Inspect `v3_source_receipt` after an uncertain response; do not submit a new ID until reconciled.

Use `v3_source_search` for bounded literal search and `v3_source_read_batch` for up to four source ranges in the same open task. Run `v3_cargo_task` with the task's current source digest for fixed locked check or test commands. Recheck the digest after verification; a stale result does not qualify for finish.
3. Verify the matching task source with focused checks. Refresh `v3_build_work_status` and capture that task's settled workspace digest. `v3_build_finish` freezes it, queues its own serialized V3 development deployment, and keeps the original tool call open until the broker reports a terminal result and both containers are checked. This action requires its own approval and live deployment authority.
4. Wait for the `v3_build_finish` tool result; do not narrate repeated unchanged status or start another build. The tool performs read-only status checks internally. A terminal completion requires `verified_healthy=true` for both V3 containers. If the connection breaks or the one-hour tool wait expires, use the task ID and assigned job ID as one durable checkpoint; then call `v3_build_wait` to reconcile the existing job. Never submit a duplicate build or retry an uncertain deployment.

Perform source review, checks, and the completion wait directly in this session. Do not delegate the work to Codex or Claude agents.

Candidate integration rejects overlapping file changes against the latest healthy release. Use a new registered task to revise a failed or conflicting change; finished task IDs are single use. `abandon` applies only to an open task. The deployment containers use versioned code, abilities, configuration and UI mounts. After both containers are healthy, the broker copies changed release files into the shared checkout if no conflicting checkout edit occurred. Direct File Manager checkout edits do not enter the release queue. Production is not a target.

`login_as_current_user_v3` remains a separate development browser bridge. Login success does not validate a feature or grant V3 tool approvals.
