# Repository Access

Call `repository_access` before answering which GitHub repository this installation can access or which Git operations this agent can use. Default `verify_remote=true` performs a bounded metadata GET only. `false` reports configuration without contacting GitHub.

The credential is read server-side from the encrypted app DB vault entry `deploy_github_token`; never request, print, or copy the token. This tool requires an installation administrator and does not grant any new permissions.

Distinguish credential presence from verified read access, reported GitHub repository roles from token write scopes/branch protections, and the Source Control selection from the built-in repository targeted by agent Git Control. Missing tools are unavailable; listed permissions still obey execution-mode and confirmation gates. Unknown push permission is not permission denied or granted.

No clone, fetch, download, commit, or push is performed. Failed metadata checks must be reported as unverified, not global inability to use GitHub.
