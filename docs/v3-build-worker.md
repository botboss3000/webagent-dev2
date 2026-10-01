# Dedicated v3 build worker

Host-side, fixed-operation service. WebAgent does not receive SSH or Docker socket access.
The worker has Docker privileges (effectively host-root); restrict its OS account, repo writers,
network callers and token holders accordingly. A token authorizes building repository code
and replacing only `webagent-dev-v3`. This is trusted administrator infrastructure, not a sandbox.

## Install on the development host

Requires Python 3.11+, Docker, this checkout at `/home/ar/webagent-dev2`, the existing
running v3 container, and several GB of free Docker storage. Uses the pinned Rust compiler
inside the v3 Dockerfile; no host Cargo installation is needed. Builds use the existing
allowlist and UI baseline check, not arbitrary uploaded source. Source must be synchronized
to this host checkout before submitting a build. Job results contain the context SHA-256.

1. Synchronize these source changes to the host. Install the service with:
   `sudo install -m 644 scripts/v3-build-worker.service /etc/systemd/system/`
2. Generate a random token (`openssl rand -hex 32`) directly into a protected host secret
   file `/etc/webagent-v3-build-worker.env` with `V3_BUILD_WORKER_TOKEN=<token>`.
   Set root ownership and mode 600. Do not print it in chat, commit it, or pass it on a command line.
3. The supplied unit binds only to the host Tailscale address `100.112.23.15:8092`.
   Enforce Tailscale ACLs and firewall rules so only the WebAgent host can connect.
   Plain HTTP is acceptable only over the encrypted private Tailscale connection; use TLS
   if the client path is not encrypted. Never proxy this service onto a public admin route.
4. `sudo systemctl daemon-reload && sudo systemctl enable --now v3-build-worker`
5. Inject `V3_BUILD_WORKER_URL=http://100.112.23.15:8092` and the same token as
   `V3_BUILD_WORKER_CLIENT_TOKEN` into the WebAgent server environment via its protected
   secret configuration, then recreate/restart that server. The token is removed from worker
   process environment before subprocesses run; it is never passed into the v3 container.

The service name is **v3-build-worker.service**. Do not restart v2/v3 just to install the worker.
Existing agent permissions are unchanged; grant this ability only to trusted administrators.

## API and WebAgent workflow

All routes require `Authorization: Bearer <token>`, never a query parameter.
- `GET /v1/status`: host identity, Docker version and active job.
- `POST /v1/jobs`: `{operation: "build", request_id: "unique-id"}` (JSON with quoted keys).
- `POST /v1/jobs`: `{operation: "deploy", request_id: "another-id", build_job_id: "successful-job-id"}`.
- `GET /v1/jobs/<id>`: saved status, bounded build output, image ID and rollback/readiness results.

Use `v3_build_status`, `v3_worker_submit`, and `v3_worker_job` from the V3 Build ability.
Submission is asynchronous (202), not a successful build/deployment. Exactly one job runs at a
 time; reuse the same request ID and body after uncertain responses. Different input with
 the same ID is rejected. No free-form commands, paths, images, environment, uploads or URLs.

Deployment copies the existing container Config and HostConfig, including environment,
mounts, port bindings, resource/security settings and restart policy, with a pinned new image ID.
Only default/bridge networking is supported. It preserves `/state` and `/workspaces` mounts.
A stopped rollback container is retained, and a failed health check restores the old container.
There can be brief v3 downtime. Rollback is container-only: it does not undo database migrations
or mutations on shared mounts; take a host-side data backup before schema-changing deployments.
Initial container provisioning remains host-only via `v3/deploy.sh` after configuration review.

Jobs persist under `/var/lib/webagent-v3-build-worker`. Restart marks active jobs interrupted;
it never silently retries a deploy. After interruption, inspect both the target and recorded
rollback container before submitting another deployment. Inspect service logs on host for
infrastructure failures; API output never includes docker inspect environment or container logs.
Image/rollback/job retention is manual; this worker does not prune or delete old backups/images.

## Verification boundary

Local mocked tests validate authentication, job semantics and rollback flow. Installing this
service, building an actual image, and checking readiness on the host are separate acceptance
steps; source completion is not a live deployment claim.
