# Reproducible installs, Docker, and remote SSH deployment plan

## Goal

Make every supported install consume the same Python 3.14 dependency lock, add a
first-class Docker Compose installation, and let an administrator install either
the native or containerized stack onto an existing Linux machine over SSH.

## Current state

- `pyproject.toml` plus `uv.lock` is the only reproducible dependency graph.
- Windows already uses `uv sync`, but it does not pass `--frozen`.
- Docker, macOS, Linux, and Termux still install hand-maintained requirements
  files. Those files have drifted: `requirements.txt` contains `supabase` while
  the project manifest does not, omits `pypdf`, and `req_no_playwright.txt`
  duplicates/conflicts on `httpx`.
- The current Dockerfile installs from `requirements.txt`; there is no Compose
  application stack or durable app-data volume definition.
- Direct remote installation already exists. `SSHVMProvider` connects with
  Paramiko, supports key or password authentication and sudo, stores named host
  profiles in the vault, uploads the common native bootstrap, and streams its
  log. It should be extended rather than duplicated.
- The SSH client currently accepts an unknown host key automatically. A
  production installer needs explicit host-key trust and a visible fingerprint.

## Target experience

The New Deployment page exposes two related routes:

1. **Docker / Compose on this machine** generates an idempotent local command.
2. **Existing server (SSH)** connects from WebAgent and offers an install mode:
   **Native systemd** (the existing behavior) or **Docker Compose**.

Native remains the default for saved SSH profiles so existing behavior does not
silently change. Both modes reuse the repository, branch, private-repository
token, embedded configuration, domain, and administrator-password controls.

## Architecture

### 1. One dependency source

1. Keep runtime dependencies in `pyproject.toml` and exact resolutions in
   `uv.lock`.
2. Move Playwright from the base dependency list to a `browser` extra. Install
   scripts that need browser automation select `--extra browser`; lightweight
   and server installs omit it. Keep `encryption` and `embeddings-local` as
   explicit extras.
3. Audit `supabase` usage. Remove it from the legacy requirements export if it
   is unused; otherwise add it to the authoritative project manifest.
4. Generate compatibility exports from the lock instead of editing them:
   - `requirements.txt`: core plus `browser`.
   - `req_no_playwright.txt`: core only.
   The exact `uv export --frozen` commands live in a script and CI regenerates
   then fails on a diff.
5. Change every uv-based install to `uv sync --frozen --python 3.14` with its
   declared extras. Change legacy pip-only platforms to consume the generated,
   locked export.
6. Remove or lock the unconstrained bootstrap fallback package set. A checkout
   missing its manifest/lock should fail with a clear recovery message instead
   of constructing a different environment.

### 2. Container artifacts

Add a production `compose.yaml` and make the Dockerfile install directly from
`pyproject.toml` and `uv.lock` with `uv sync --frozen --no-dev`.

The Compose stack should provide:

- `webagent`, built from the checked-out revision and running as a non-root user;
- a persistent app-data volume or bind mount for `data/` and other durable local
  state, with an initialization step that fixes ownership safely;
- port selection through `WEBAGENT_PORT`, defaulting to 8080;
- a liveness check on `/health` and readiness gate on `/health/ready`;
- `restart: unless-stopped`, graceful shutdown, and a single worker by default;
- an optional Postgres/pgvector profile, not an automatically enabled database;
- optional Caddy/HTTPS configuration when a domain is supplied;
- `.env` and bootstrap configuration mounted or injected at runtime, never
  copied into the image;
- an explicit browser-enabled image/profile rather than silently installing a
  Playwright package without its browser and OS libraries.

Do not mount the host Docker socket into WebAgent. Local installation should be
a generated command, and remote installation should execute Docker commands over
the existing SSH connection.

### 3. Shared install recipes

Split recipe generation from transport:

- Keep `build_install_script()` as the native Ubuntu/Debian systemd recipe.
- Add `build_docker_install_script()` for Docker Engine/Compose validation,
  checkout/update, secret-file placement, `docker compose up -d --build`, and
  readiness polling.
- Add one small Docker manual provider that emits the local command and uses the
  same repository/config bundle inputs as the Windows/macOS/Termux commands.
- Add `install_mode` to `SSHVMProvider` and dispatch to the native or Docker
  recipe. Reuse its SFTP upload, privilege escalation, progress stream, saved
  host profiles, credential vault, deployment record, and reconnect behavior.
- Installing Docker Engine on the remote host must be a separate, explicit
  checkbox. When selected, use the vendor apt repository with supported distro
  and architecture checks; otherwise fail with the exact prerequisite command.

Remote files containing secrets must be mode `0600`. Tokens and passwords must
not appear in command arguments or progress events. Consume/remove the one-time
bootstrap bundle after successful first boot.

### 4. SSH trust and lifecycle safety

Before expanding the SSH install path:

- Replace unconditional `AutoAddPolicy` with known-host verification.
- On first contact, show the server key type and SHA-256 fingerprint and require
  explicit trust; persist the trusted key with the saved host profile.
- Reject a changed key before sending credentials or scripts.
- Keep connect/auth/sudo/preflight separate from mutation so **Test connection**
  remains read-only.
- Give each remote install a job id and durable log path so the UI can reconnect
  after a browser or server restart.
- Make both recipes idempotent. Re-running updates the checkout/image and keeps
  data. **Tear down** stops/removes services but preserves data by default; data
  deletion is a separate confirmation.

## Delivery phases

### Phase A — dependency reproducibility

- Normalize project dependencies and extras.
- Add the export/check script and regenerate both requirements files.
- Switch Windows/bootstrap/Docker to frozen uv installs and pip-only paths to
  frozen exports.
- Add CI checks for `uv lock --check`, clean exports, Python 3.14 sync, and
  `pip check`.

### Phase B — container runtime

- Refactor the Dockerfile to the lock-based build.
- Add Compose, persistent volumes, health/readiness behavior, and optional
  database/browser/TLS profiles.
- Add `deploy/docker-setup.sh` as the idempotent local/remote entry point.

### Phase C — deployment integration

- Add the manual Docker target and UI panel.
- Add `install_mode` and Docker preflight/dispatch to `SSHVMProvider`.
- Add host-key fingerprint enrollment and mismatch rejection.
- Stream Docker build/start/readiness progress through the existing NDJSON
  deployment flow and record the selected mode.

### Phase D — verification and documentation

- Update installation and deployment docs and remove claims that Docker/SSH are
  future work.
- Add upgrade, recovery, backup, log, stop, and uninstall instructions for both
  install modes.
- Run the acceptance matrix below in CI where practical and on one disposable
  Ubuntu VM for the SSH end-to-end path.

## Acceptance matrix

- A clean Python 3.14 frozen sync succeeds on Windows and Linux.
- Generated requirements files exactly match `uv.lock`; CI detects drift.
- The image builds without resolving dependencies beyond the lock.
- Compose reaches `/health/ready`, survives restart, and retains app data.
- Core and browser-enabled Compose profiles behave as documented.
- SSH key, passphrase-protected key, password, root, passwordless sudo, and
  password sudo paths have coverage.
- First-contact host-key enrollment works; a changed host key is blocked.
- Native and Docker SSH installs stream progress, tolerate reconnect, and are
  idempotent on rerun.
- Private-repository and embedded-config installs expose no secrets in image
  layers, process arguments, logs, deployment records, or API responses.
- Tear-down preserves data unless the administrator separately confirms data
  deletion.

## Recommended implementation order

Complete Phase A first. Build Phase B entirely from the frozen dependency model,
then add the two thin transports in Phase C. This avoids encoding today’s
requirements-file drift into the new Docker and SSH installation paths.
