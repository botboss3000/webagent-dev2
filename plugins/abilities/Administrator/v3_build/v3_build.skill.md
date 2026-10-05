# V3 Build bridge from V2

Use this ability only when a V2 agent must inspect or submit a fixed V3 development build. V3 source lives in `/home/ar/webagent-dev2/v3`; the V3 runtime has its own workspace and build abilities. Read the checkout-root `AGENTS.md` and `v3/README.md` for the current build, source, and deployment boundaries. Do not use the older `/home/ar/webagent-dev` checkout or production resources.

Call `v3_build_status` to obtain the frozen allowlisted `inputs_sha256`. `v3_worker_submit(operation="build")` submits a pinned, locked test and image job; `v3_worker_job` polls its durable result. A successful build is not deployment. An authorized `v3_worker_submit(operation="deploy")` must cite this session's completed unchanged build job and the same source digest. It briefly interrupts V3, preserves stopped rollback containers, and checks both replacements' health. Reuse a stable request ID after an uncertain response; inspect the existing receipt before any new request.

V2's extra direct image and Cargo entrypoints were redundant with the fixed worker submission and isolated workspace checks. The older build worker restart remains an operator recovery action outside this exposed agent workflow. Source work in the automatic V3 deployment workflow should register with V3's `v3_build_work` coordinator or its Codex CLI before editing; V2's bridge does not supply that coordination itself.

`login_as_current_user_v3` maps the signed-in V2 administrator to the independent V3 development browser for a bounded journey. It needs the configured bridge and Browser Control ability. Verify the V3 identity, exact route, and visible result; login success alone is not feature acceptance. This bridge does not import V2 credentials or grant V3 tool approvals.
