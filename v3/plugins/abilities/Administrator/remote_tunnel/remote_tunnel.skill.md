# Remote Tunnel

Expose this machine's running app over a **public Cloudflare quick tunnel** and
report the address. This is the same "quick tunnel" the Remote Access card in
App Settings offers: an ephemeral `https://<random-words>.trycloudflare.com`
address, backed by Cloudflare, with **no account or DNS setup** required. The
address is random and changes every time a new tunnel starts.

## When to use it

Use this when the user asks to **"start a tunnel"**, **"expose the app"**,
**"make it reachable from my phone / outside"**, or **"give me the public URL"**.

## The two tools

- **`start_tunnel`** — launches the quick tunnel in the background and returns the
  live `trycloudflare.com` URL. It waits a few seconds (default 15, up to 40 via
  `wait_seconds`) for Cloudflare to announce the address. If the address hasn't
  appeared yet it returns `status: "starting"` — just call `get_tunnel_url` a few
  seconds later to fetch it.
- **`get_tunnel_url`** — re-reads the **current** tunnel: whether one is running
  and its public URL. Use it to answer "what's the URL again?" without starting a
  new tunnel (starting a new one replaces the old and mints a fresh random URL).

## How to work

1. Call `start_tunnel`. On `status: "ok"`, give the user the `public_url` verbatim.
2. If it returns `status: "starting"`, wait a moment and call `get_tunnel_url`.
3. To repeat the URL later in the conversation, call `get_tunnel_url` — do **not**
   call `start_tunnel` again unless the user wants a fresh tunnel.

## Notes & failure modes

- **One shared tunnel.** This drives the same single tunnel the App Settings card
  controls, so the agent and the UI never run two competing tunnels. Starting one
  here shows up as running on the card, and vice-versa.
- **Needs `cloudflared`.** The Cloudflare `cloudflared` helper must be installed
  and on PATH. If it isn't, `start_tunnel` returns a clear error telling the user
  to install it — a quick tunnel needs no Cloudflare account.
- **Security.** A live tunnel makes this machine reachable from the public
  internet (behind the app's own auth). Only start one when the user asks, and
  tell them the address is public.
