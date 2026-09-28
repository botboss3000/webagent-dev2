# Development and production layout

The server keeps two complete, independent application checkouts:

- /home/ar/webagent is production and serves webagent.live on host port 8080.
- /home/ar/webagent-dev is development and serves dev.webagent.live on host
  port 8081.
- /home/ar/webagent-state is production runtime state.
- /home/ar/webagent-dev-state is development runtime state.

The development checkout is the complete development repository. There is no
nested app/app directory and no separate source overlay.

## Release selection

The file dev-sync.json is the release policy. It separates files into four
groups:

- dev_only_roots lists the areas where selectively excluded development tools
  are allowed to live.
- dev_only_paths lists the exact folders within those roots that stay only in
  development.
- state_paths lists runtime data that must never be copied into production.
- tooling_paths keeps the release tooling itself out of the production repo.

To make another folder development-only, add its exact repo-relative path to
dev_only_paths. It must be inside one of dev_only_roots. Removing a path from
dev_only_paths makes that folder eligible for the next production release.

The current development-only selections cover TUI, the Codex engine, the
Administrator code index, and selected administrative UI tools.

## Configuration

Tracked baseline configuration is intentionally eligible for release. Defaults
under app/defaults and tracked baseline files under data/config can therefore
be reviewed, committed, and copied to production.

Runtime agent data, databases, user data, uploads, engine state, temporary
captures, authentication data, and
secrets remain in the sibling state directories. The sync command considers
only Git-tracked files, so ignored or untracked runtime files are never copied.

The development container keeps its complete existing state mount. This is
deliberate: runtime configuration and authentication files are required by the
running application, even though they are not release content.

## Preview a release on the server

Run this from /home/ar/webagent-dev:

    ./scripts/sync-production

Preview is the default. It reports the number of tracked files selected,
excluded files, additions, updates, removals, and the total production changes.
Add --verbose to list every path.

## Apply a release

Before applying:

1. Commit or intentionally discard all development changes.
2. Confirm /home/ar/webagent is clean.
3. Review the preview and the exclusions in dev-sync.json.

Then run:

    ./scripts/sync-production --apply

The command:

- copies only selected Git-tracked files from development;
- propagates deletions for included files;
- preserves the production .git directory;
- preserves excluded folders already present in production;
- refuses a dirty production checkout;
- refuses a dirty development checkout unless
  --allow-dirty-source is explicitly supplied;
- verifies that the destination matches the selected source after copying.

It does not stage, commit, push, rebuild, restart, or deploy containers.

The launcher uses a temporary container because the host does not provide
Python and the long-running development container does not mount the production
checkout. Preview mounts production read-only; --apply makes that mount writable.

## Complete a production release

After the copy:

1. Review git status and git diff in /home/ar/webagent.
2. Run production-focused tests.
3. Confirm the local production health endpoint.
4. Commit the reviewed production changes.
5. Push the production repository.
6. Rebuild or restart only when the changed files require it.
7. Confirm https://webagent.live/app.

Ordinary bind-mounted Python and browser-source changes may reload without an
image rebuild. Dependency, interpreter, operating-system package, or Dockerfile
changes require deliberate container rebuild and recreation with the current
mounts, ports, user, health check, environment, and secrets preserved.
