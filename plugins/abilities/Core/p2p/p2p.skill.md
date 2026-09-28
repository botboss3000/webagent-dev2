# P2P Mirror

## When to use

Load this skill when the user asks to set up instance-to-instance data mirroring,
add a sync peer, list configured peers, remove a peer, or trigger a manual sync.

## Tools

- **add_peer(url, name)** — Handshake with a remote WebAgent instance and
  configure it as a sync peer. Requires the remote instance's base URL (e.g.
  `https://my-other-instance.example.com`). The two instances exchange Ed25519
  public keys. Once added, the background P2P worker keeps the `data/` folder
  in sync automatically.
- **remove_peer(peer_id)** — Remove a peer (use `list_peers` to find its id).
- **list_peers()** — Show all peers with their sync status (online/offline,
  last sync time).
- **sync_now(peer_id)** — Trigger an immediate sync instead of waiting for the
  periodic worker cycle.

## How sync works

The **P2P worker** runs in the background on every instance. Every 60 seconds:

1. Fetches the peer's **manifest** (a listing of every file under `data/` with
   sha256 hashes).
2. Compares it to the local manifest.
3. Pulls any changed or new files (base64-encoded + checksum-verified).
4. Database files are atomically snapshotted via `sqlite3 .backup` before transfer.

Every request is signed with the instance's Ed25519 private key and verified by
the peer. Timestamps prevent replay attacks.

## Peer config storage

Peers are stored in `data/config/p2p/peers/<id>.json`. The instance's own keypair
is in `data/config/p2p/keypair.json` (generated once at first boot).
