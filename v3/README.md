# WebAgent v3 development

Independent Rust agent runtime, subscription provider adapter, and isolated Python tool worker.
Source lives here within the primary development checkout; v2 remains independently available.

## Run

On the development server:

```sh
sh /home/ar/webagent-dev-v2/v3/deploy.sh build
sh /home/ar/webagent-dev-v2/v3/deploy.sh start
curl -fsS http://100.112.23.15:8081/health/ready
```

The existing named tunnels serve https://webagent.live/app from dev2 on host port 8080
and https://dev.webagent.live/app from v3 on host port 8081. The previous
production app container is stopped and kept as a rollback snapshot.

On first start, open `/setup.html` to choose the v3 admin username and password.
The retired test bootstrap credential is removed on the first launch of this setup flow.
Open `/app?tab=agents&agent=shared_default` to use the v2 Agents layout and configure the default agent's Model page. Connect a fresh ChatGPT subscription there; v2 tokens are never imported.
The default agent uses `gpt-6-luna` until you save another model. Choose a model supported by your account; a catalog name alone does not establish entitlement.

## Boundaries

The Rust application owns policies, approvals, execution state, recovery and shutoff. The first v3 UI port uses v2's Agents partial and CSS at image-build time; the independent v3 API and storage serve its chat and model settings. Other v2 tabs and agent subpages are not wired yet.
The Python worker owns actual filesystem operations and approved shell/Python processes.
Its separate UID cannot read /state or application process environments.
File tools reject symlink/path escapes. Approved scripts can access the entire v3 workspace collection
and external network; they are not a per-session or hostile-code sandbox. No host source, Docker socket,
v2 data, or production storage is mounted.

Runtime records are encrypted using a v3-only local key. Preserve the whole data directory,
including its encryption key, for recovery; encryption does not replace filesystem permissions.
Do not inspect the live session database directly. Use authenticated APIs.

## Recovery and controls

Read-only work can resume automatically after restart. Approvals survive restarts.
An interrupted write, shell or Python action is held for outcome review.
Never automatically replay unknown side effects. Review can accept a known result,
explicitly retry (subject to fresh policy/gate checks), or abandon the run.
Clearing global shutoff never resumes stopped runs automatically.
Shutoff cannot undo effects already committed outside the container.

The event log is append-only. Current run documents are checkpoints, updated transactionally
with their events. Derived schemas/context segments are reconstructed only when needed;
history and tool output are bounded. Full required model history is supplied until transport
support for server-retained context is demonstrated.

## Verification

```sh
cargo test --locked
python3 -B -m unittest discover -s tests -p 'test_*.py'
```

Build and run these commands on the development server, using the pinned Rust build image if needed.
UI journey routes and visible checkpoints are in docs/ui-journeys.md.
