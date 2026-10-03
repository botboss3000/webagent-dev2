# Git Control

Version control only — status, diff, commit, push, branch, pull — without shell access. On by default.

Each tool has its own personal skill under `tools/`, delivered when that tool becomes callable.

## Repository access discovery

Before answering which GitHub repository is accessible or which Git functions are available, call `repository_access`. It reads the shared GitHub credential server-side from the app DB vault and optionally verifies repository metadata without downloading contents. Never treat a missing standalone GitHub credential block as absence of Source Control authentication. Distinguish configured credentials, verified repository access, loaded tools, and execution-mode permission; never expose tokens. Agent Git Control targets the built-in project, not the repository currently selected in File Manager.

### Authenticated repository downloads

- Use the verified repository identity, not an assumed name: this task's checkout resolved to `botboss3000/webagent-dev2`, not `botboss3000/webagent-dev`. Recheck current access rather than treating that historical result as permanent.
- Before cloning, inspect the exact destination. Preserve existing content; do not overwrite or delete a non-empty destination without explicit authorization. A shallow clone is a latest snapshot, not full history—label it accordingly.
- CLI authentication depends on the current environment's Git credential-helper registration. A clone error such as `could not read Username` does not establish that the app vault lacks credentials. Inspect the helper configuration and the existing `scripts/git-credential-webagent.py` integration before concluding access is unavailable.
- When the shared vault credential exists but this environment has no registered helper, use the existing vault-backed helper via a command-scoped Git `credential.helper` setting for the authorized operation. Do not change global Git configuration merely to perform one clone. Never embed tokens in command arguments, remote URLs, repository configuration, output, or skill text; resolve them server-side from the app DB vault.
- After cloning, verify the sanitized remote identity, checked-out revision, and working-tree status. Report the destination and whether history is shallow. Download authorization does not authorize committing, pushing, deploying, or changing unrelated development work.
