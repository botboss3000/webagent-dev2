# v3_build_image

Builds webagent-dev-v3:local from the pinned v3/Dockerfile and deployment allowlist only. Validates shared UI hashes before submitting the context. Requires Docker daemon access already provisioned on this server. It may replace that development image tag but never starts/stops/recreates containers. This does not deploy. Inspect exit_code/timed_out and then separately verify deployment if requested.
