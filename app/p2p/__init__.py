"""P2P mirror — instance-to-instance data sync with Ed25519 mutual auth.

Two WebAgent instances authenticate each other via Ed25519 public-key exchange,
then a background worker keeps their ``data/`` folders in sync by periodically
comparing file manifests and pulling changed files.  No third party: every request
is cryptographically signed and timestamp-verified.
"""

from app.p2p.identity import (
    instance_id,
    public_key_hex,
    sign_request,
    verify_signature,
)
from app.p2p.store import (
    list_peers,
    get_peer,
    add_peer,
    remove_peer,
    get_sync_state,
    set_sync_state,
)
from app.p2p.sync import build_manifest, diff_manifest, pull_files
from app.p2p.worker import P2PWorker, start_worker, stop_worker, kick

__all__ = [
    "instance_id",
    "public_key_hex",
    "sign_request",
    "verify_signature",
    "list_peers",
    "get_peer",
    "add_peer",
    "remove_peer",
    "get_sync_state",
    "set_sync_state",
    "build_manifest",
    "diff_manifest",
    "pull_files",
    "P2PWorker",
    "start_worker",
    "stop_worker",
    "kick",
]
