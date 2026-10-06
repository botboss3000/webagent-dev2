# Parallel source work and serial V3 releases

Codex, the V3 native agent, and the V2 Python bridge share one protected host
broker. Each caller begins a stable task ID in isolated mode. The broker copies
the current healthy release's allowlisted source into a private task workspace;
the task receipt provides its path and source digest. Writers edit their own
snapshots, so an unfinished task does not delay another task's build.

Apply reviewed text patches with a stable request ID and the task's current
`workspace_inputs_sha256`. The broker checks allowed paths, regular files,
source digest, and `git apply --check` before changing that task snapshot. A
V2 Workspace proposal is an artifact; its seed may lag a newer release, so use
`v3_source_read` to inspect and rebase against the broker task workspace.

After focused checks, finish with the settled task digest. Finish freezes a
copy of the task source and assigns a durable job ID immediately. One job at a
time compares that copy with the latest healthy release. Changes to different
files combine in finish order. A file changed by both releases is a conflict:
the later job fails before building and needs a new task based on the current
release. Finished task IDs are single use.

The admitted job builds both images from one frozen context. `v3/Dockerfile`
runs locked Rust tests and a release build in one tested builder stage. Buildx
reuses Cargo registry and target caches across the API and loop image targets.
The broker then mounts code, configuration, abilities, and UI assets from the
job's versioned release directory into the replacement containers. The
broker copies a successful release's changed files into the shared source
checkout after both containers become healthy. It checks that those checkout
files still match the previous release or the new release; a conflict holds
the queue for reconciliation. Direct checkout edits are not part of the queue
and do not change the mounted release assets.
WebAgent workers apply reviewed patches to their registered task workspace;
the writable checkout is not an automatic release source.

Every replacement retains stopped prior containers and private rollback image
material. A broker restart can resume a job that was still queued; running or
externally uncertain work is not replayed. If a deployment's outcome is
uncertain, reconcile it before another release. The broker checks both
container health states and its frozen source digest before recording success.

Workers poll the durable job record about every 15 seconds while active, with
a 30 minute bound. A timeout or disconnect is still pending and is resumed
from the same task or job ID. Polling does not wake an idle agent. Automatic
development deployment requires the caller's authorized finish action;
production is outside this workflow.
