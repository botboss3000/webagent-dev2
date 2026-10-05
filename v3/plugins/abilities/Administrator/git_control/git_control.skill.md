# Git Control — V3
Use the current session workspace repository, default webagent-v3. Start with
git_tool operation status for HEAD and review_sha256. Every mutation requires
expected_head. No shell command, host path, credential, or remote URL arguments.
Exact paths are required for staging, resetting and commit_and_push.
resolve_conflict requires an unmerged path, expected_sha256 and ours/theirs/both;
both concatenates sides without inferring a semantic merge. Review before staging.
commit_and_push requires paths, message, expected_head and expected_review_sha256.
It refuses unselected staged paths and suspected credentials. Push is ordinary,
never force; pull is fast-forward only. Existing configured external remotes need
trusted runtime credential transport. Workspace-local remotes stay in the session.
Interrupted mutations may have uncertain effects: inspect local and remote state
before retry. Passive repository notices use deduplicated snapshots, never changes.
Readiness remains pending until registration, configuration and API acceptance.
