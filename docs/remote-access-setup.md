# Remote Access — setup guide

The **Remote Access** card (Settings → **Data Settings**, below the Database
card) lets a phone or any
other device reach this WebAgent server. It offers several methods through one
card; turn on whichever you want. All settings are admin-only.

> **Security first.** Exposing this machine also exposes its admin shell/file
> tools. Keep sign-in enabled, keep the powerful tools admin-only, and prefer a
> private method (Tailscale) when you don't need a public link. The signpost and
> the tunnels are *plumbing* — your WebAgent login is still the real gate.

## The methods

| Method | App-managed? | Address | Reachable by | Best for |
|--------|--------------|---------|--------------|----------|
| **Same network** | n/a (always shown) | `http://<lan-ip>:<port>` | devices on your Wi-Fi | quick local use |
| **ngrok** | yes (start/stop) | reserved domain = stable; else changes | anyone with the link | a managed tunnel |
| **Cloudflare** | yes (start/stop) | fixed hostname on your domain | anyone with the link | a stable public address |
| **Tailscale** | status only | fixed private name/IP | only your enrolled devices | private, most secure |
| **Port-forward** | status only | your own public address | the whole internet | full DIY control |

Only **same-network** and **ngrok** can be fully automated by the app
(ngrok exposes a local control API the app reads). Cloudflare is supervised but
needs a one-time Cloudflare setup; Tailscale and port-forward are status +
guidance because the app can't install them for you.

## One-time setup per method

### Same network
Nothing to set up. The card shows the address + a QR code. Only real LAN
addresses a phone on the same Wi-Fi can reach are listed — virtual-adapter
addresses (WSL, Hyper-V's Default Switch, Docker, VirtualBox/VMware host-only
networks) are filtered out so the list isn't cluttered with addresses that only
exist inside this PC. Note it's plain `http://`, so secure-context browser
features (Google sign-in, clipboard, voice) won't work over it — use a tunnel
for those.

### ngrok
1. Install ngrok and create a free ngrok account.
2. In the card, paste your **authtoken** and click **Save token** (the app runs
   ngrok's one-time `add-authtoken` for you; WebAgent never stores the token).
3. (Optional, for a stable address) buy a reserved domain in ngrok and put it in
   the **Reserved domain** field.
4. Pick **ngrok** as the method and click **Start**.

### Cloudflare named tunnel
1. In Cloudflare, create a **named tunnel** and map a hostname (e.g.
   `pc.webagent.live`) to `http://localhost:<port>`. Install `cloudflared` and
   its credentials on this PC (`cloudflared tunnel login` / `tunnel create`).
2. In the card, enter the **tunnel name** and the **hostname**, pick
   **Cloudflare**, and click **Start**.
3. Tick **Quick tunnel** instead if you just want a throwaway
   `trycloudflare.com` address with no Cloudflare account.

### Tailscale
1. Install Tailscale on this PC **and** your phone; sign both into the same
   account.
2. Pick **Tailscale** and click **Start** — the card shows the device's private
   address and registers it with the signpost.

### Port-forward (manual)
1. Forward an external port on your router to this PC's `<lan-ip>:<port>`, set up
   dynamic-DNS + HTTPS as needed.
2. Enter the resulting **public address**, pick **Port-forward**, and click
   **Test** to confirm it answers.

## The phone bookmark (auto-reconnect)

When the address can change (e.g. free ngrok), the **signpost** keeps a fixed
bookmark working:

- This PC reports its current address to a **signpost server** (default
  `https://webagent.live`) whenever it changes.
- Your phone saves **one** link — `https://<signpost>/go/<your-key>` — shown in
  the card with a QR. Opening it always forwards to the PC's live address.

> A stable address (reserved ngrok domain, Cloudflare named tunnel, or
> Tailscale) avoids re-login on the phone, because the browser keeps your
> session per web address. With a *changing* address you skip the manual step
> but the new address is a fresh origin, so you'll sign in again.

## Hosting the signpost for other people

The signpost directory is **multi-tenant**: it stores one entry per rendezvous
key. Set a WebAgent install's signpost **role** to **Server** (or **Both**) and
it will host the `where-is-my-PC` lookup + `/go/<key>` redirect for many users —
each user's PC reports under its own key, secured by a per-key push token bound
on first use. The directory only holds addresses; it never sees agent traffic.

## What gets written (runtime, gitignored)

- `remote_access.json` — this install's config + its rendezvous key / push token.
- `remote_access_pointers.json` — the signpost directory (when serving).

Both are per-machine and must stay out of git (already in `.gitignore`).
