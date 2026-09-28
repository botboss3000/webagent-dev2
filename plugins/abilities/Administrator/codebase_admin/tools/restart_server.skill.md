Restarts the web agent server so edited `.py` files take effect. It calls
`app.relauncher.trigger_restart()` in-process — no HTTP request, no token.

- **This tool is the only correct way to restart.** Do not POST `/api/v1/restart`
  yourself: it is admin-gated and answers a tokenless localhost caller with 401.
  Do not kill the process with `run_command`/`run_python` either — the tree is
  `tini → uvicorn --workers N`, so killing it takes the server down for good
  instead of restarting it.
- **Use it freely — no user permission or confirmation is required.** Call it whenever
  you have made a change that only takes effect in a restarted server, then verify the
  change. Do not stop to ask for approval first.
- **Expect to be interrupted, then verify.** The relauncher signals the uvicorn
  supervisor and replaces workers one at a time; the container and the supervisor stay
  up and the session normally resumes in a fresh worker. Send any reply you owe the user
  *before* calling, since the turn is cut while the workers roll. Success returns
  `Server restarting: …` — treat that as success, not as a gate refusal.
- `Server restart unavailable: …` means no launcher matched this topology;
  `Could not request server restart: …` means the relauncher raised. Report either
  result rather than retrying blindly or falling back to killing the process.
