# Repository Access — V3

Use `repository_access(verify_remote=true)` before reporting which repositories this agent can access. False reports configuration without network access. The current session selects the agent; another tenant cannot be selected. Connections are HTTPS grants configured by this agent's administrators. Tokens remain in the encrypted credential plane and never enter output.

A bounded metadata GET verifies GitHub repository visibility where supported. Report other providers, absent credentials, and failed metadata reads as unverified. Reported GitHub repository roles do not establish token write scope, branch protection permission, or permission to execute Git tools. This tool never clones, fetches, commits, or pushes. Use typed Git Control under its live policy for those workflows.
