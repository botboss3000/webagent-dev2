"""Admin API for P2P peer management — invite generation, peer listing, revocation."""

from __future__ import annotations

import logging
from typing import Dict, List

from fastapi import APIRouter, HTTPException

from app.p2p import identity
from app.p2p import store as peer_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/p2p", tags=["admin-p2p"])


@router.post("/invite")
async def create_invite():
    """Generate a WebRTC invite QR code for a new device.

    Creates an SDP offer + ICE candidates, encodes them into a QR code data URI,
    and returns everything the admin UI needs to display the QR.

    Admin-only endpoint.
    """
    try:
        from app.p2p.transport.webrtc_transport import WebRTCTransport
        from app.p2p.transport.signaling import encode_invite_qr
        from app.p2p.transport.crypto import _ed25519_priv_to_x25519
        from cryptography.hazmat.primitives.asymmetric import x25519
        import base64

        # Create the WebRTC transport and generate an offer
        transport = WebRTCTransport()
        sdp_offer_b64, ice_json = await transport.create_offer()

        # Read tunnel URL from config
        tunnel_url = _get_tunnel_url()

        # Get X25519 public key for the invite
        priv_bytes = identity.private_key_bytes()
        our_x25519 = x25519.X25519PrivateKey.from_private_bytes(
            _ed25519_priv_to_x25519(priv_bytes)
        )
        x25519_pub_b64 = base64.b64encode(
            our_x25519.public_key().public_bytes_raw()
        ).decode()

        # Generate QR code
        qr_data_uri = encode_invite_qr(
            instance_id=identity.instance_id(),
            ed25519_pubkey_hex=identity.public_key_hex(),
            x25519_pub_b64=x25519_pub_b64,
            tunnel_url=tunnel_url,
            sdp_offer_b64=sdp_offer_b64,
            ice_json=ice_json,
        )

        # Also generate the raw invite string (for copy-paste)
        invite_uri = _build_invite_uri(
            identity.instance_id(),
            identity.public_key_hex(),
            x25519_pub_b64,
            tunnel_url,
            sdp_offer_b64,
            ice_json,
        )

        return {
            "qr_data_uri": qr_data_uri,
            "invite_string": invite_uri,
            "instance_id": identity.instance_id(),
            "expires_in_seconds": 300,  # 5-minute window
        }
    except Exception as e:
        logger.exception("Failed to create P2P invite")
        raise HTTPException(status_code=500, detail=f"Failed to create invite: {e}")


@router.get("/peers")
async def list_peers() -> List[Dict]:
    """List all known P2P peers with their sync status."""
    peers = peer_store.list_peers()
    result = []
    for peer in peers:
        result.append({
            "id": peer.get("id", ""),
            "name": peer.get("name", ""),
            "url": peer.get("url", ""),
            "status": peer.get("status", "unknown"),
            "last_sync_at": peer.get("last_sync_at", ""),
            "last_sync_files": peer.get("last_sync_files", 0),
            "public_key": peer.get("public_key", ""),
        })
    return result


@router.delete("/peers/{peer_id}")
async def remove_peer(peer_id: str):
    """Revoke a peer and remove it from the P2P mesh.

    Revoked peers cannot reconnect — their public key is added to the revocation
    set, their peer KEK is cleared, and their peer file is moved to revoked/.
    """
    try:
        from app.p2p.revocation import revoke_peer
        was_revoked = revoke_peer(peer_id)
        if not was_revoked:
            raise HTTPException(status_code=404, detail=f"Peer {peer_id} not found")
        return {"status": "revoked", "peer_id": peer_id}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to revoke peer %s", peer_id)
        raise HTTPException(status_code=500, detail=str(e))


def _get_tunnel_url() -> str:
    """Read the Cloudflare tunnel URL from config."""
    import json
    from pathlib import Path

    tunnel_path = Path(__file__).resolve().parent.parent.parent / "data" / "config" / "tunnel_link.json"
    if tunnel_path.exists():
        try:
            with open(tunnel_path) as f:
                data = json.load(f)
                return data.get("tunnel_url", "") or data.get("url", "") or "http://localhost:8080"
        except Exception:
            pass
    return "http://localhost:8080"


def _build_invite_uri(
    instance_id: str,
    ed25519_pubkey_hex: str,
    x25519_pub_b64: str,
    tunnel_url: str,
    sdp_offer_b64: str,
    ice_json: str,
) -> str:
    """Build the wa-p2p:// invite URI string."""
    import base64

    tunnel_b64 = base64.urlsafe_b64encode(tunnel_url.encode()).decode().rstrip("=")
    ice_b64 = base64.urlsafe_b64encode(ice_json.encode()).decode().rstrip("=")

    return (
        f"wa-p2p://v1/{instance_id}/{ed25519_pubkey_hex}/{x25519_pub_b64}/"
        f"{tunnel_b64}/{sdp_offer_b64}/{ice_b64}"
    )
