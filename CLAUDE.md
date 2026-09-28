# Project notes for Claude

## Git / branching preferences

- Do **NOT** create or commit to worktree branches (e.g. auto-generated
  `claude/...` branches) unless we've explicitly agreed to it and the user has
  given permission for that specific piece of work.
- The default expectation is to commit straight to `main`. Only branch when the
  user asks for it.

## Git auth & line endings

- GitHub auth for CLI git is handled by a credential helper
  (`scripts/git-credential-webagent.py`, registered in the global git config)
  that resolves the app's shared vault token (`app/deploy/credentials.py`,
  service `deploy_github_token`, mirrored to `data/config/provider.json`).
  Plain `git fetch` / `pull` / `push` against github.com just works — never
  paste tokens into URLs or repo config.
- The repository stores LF (see `.gitattributes`). If a file ever shows up as
  fully re-written in a diff, check `git ls-files --eol` for `i/crlf` and fix
  with `git add --renormalize <file>` instead of committing the noise.
