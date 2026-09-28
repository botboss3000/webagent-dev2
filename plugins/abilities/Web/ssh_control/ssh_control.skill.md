# SSH Control

Use SSH Control only for devices external to the WebAgent server. The runtime
rejects localhost, loopback, link-local, and addresses assigned to this host.
Never try to bypass that boundary through aliases, DNS, tunnels, or another
tool; Administrator abilities are the proper route for changing WebAgent itself.

## Connecting a device

1. Call `ssh_list_connections` before asking for credentials; the requested
   device may already be saved for this user and agent.
2. If it is not saved, call `ssh_request_connection` with a short human name.
   A secure browser card collects the host, login, private key/password, and
   optional sudo password. Secret values go directly to the encrypted vault and
   are never returned to you.
3. Stop and ask the user to complete the card. They must inspect and trust the
   server's SHA-256 host-key fingerprint before the profile is saved.
4. After the user confirms completion, call `ssh_list_connections` again and
   use the returned opaque `connection_id`.

If a user pastes a password or private key into chat, do not repeat it or place
it in a tool call. Explain that chat is not the secure credential path, ask them
to rotate the exposed credential, and open the secure card instead.

## Commands

- Use `ssh_run_command` for bounded, non-interactive commands. It returns stdout,
  stderr, exit code, timeout, duration, and truncation state.
- Set `elevated=true` only when the task requires POSIX sudo. The runtime uses a
  stored sudo password when present and otherwise tries passwordless `sudo -n`.
- Do not claim success unless the exit code and output support it. Report stderr,
  timeouts, truncation, authentication failures, and host-key changes plainly.
- SSH Control is Auto by default, but normal per-tool Deny/Ask/Auto policy still
  applies. Do not evade a Deny or an approval request.

## Background jobs

- Use `ssh_start_job` only when the command will outlive a normal tool call.
- Poll with `ssh_poll_job`, carrying its returned `next_cursor` forward so output
  is not repeated. `wait_seconds` may long-poll briefly instead of busy polling.
- Jobs exist only while this WebAgent process is running. A restart loses their
  tracking state. `ssh_cancel_job` closes the SSH channel, but cannot guarantee
  that deliberately detached grandchildren on the remote host stop.
- Use the existing Automation ability for recurring monitoring and alerts. A
  durable automation should run a fresh `ssh_run_command` check each time; do not
  build a permanent monitor out of an in-memory SSH job.

V1 does not provide interactive shells, SFTP, forwarding, jump hosts, SSH-agent
credentials, certificates, or hardware-backed keys.
