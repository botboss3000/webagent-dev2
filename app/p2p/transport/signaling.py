"""P2P signaling — QR code generation and SDP answer return path.

The QR carries a wa-p2p:// URI containing the SDP offer + ICE candidates.
The answer is returned via a one-shot HTTP POST to the primary's tunnel URL.
"""

from __future__ import annotations

import base64
import io
import json
import logging
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# qrcode is already in pyproject.toml dependencies


def encode_invite_qr(
    instance_id: str,
    ed25519_pubkey_hex: str,
    x25519_pub_b64: str,
    tunnel_url: str,
    sdp_offer_b64: str,
    ice_json: str,
) -> str:
    """Generate a data URI of a QR code containing the WebRTC invite.
    
    Format: wa-p2p://v1/<instance_id>/<ed25519_pub>/<x25519_pub>/<tunnel_b64>/<sdp_b64>/<ice_b64>
    All base64 payloads are URL-safe encoded (alphabet without '/' or '+', padding
    stripped) so they can live as URI path components.
    """
    import qrcode

    tunnel_b64 = base64.urlsafe_b64encode(tunnel_url.encode()).decode().rstrip("=")
    ice_b64 = base64.urlsafe_b64encode(ice_json.encode()).decode().rstrip("=")
    x25519_b64 = base64.urlsafe_b64encode(base64.b64decode(x25519_pub_b64)).decode().rstrip("=")
    sdp_b64 = base64.urlsafe_b64encode(base64.b64decode(sdp_offer_b64)).decode().rstrip("=")

    uri = (
        f"wa-p2p://v1/{instance_id}/{ed25519_pubkey_hex}/{x25519_b64}/"
        f"{tunnel_b64}/{sdp_b64}/{ice_b64}"
    )

    img = qrcode.make(uri, error_correction=qrcode.constants.ERROR_CORRECT_M)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return f"data:image/png;base64,{base64.b64encode(buf.read()).decode()}"


def decode_invite_qr(qr_text: str) -> Optional[Dict]:
    """Parse a wa-p2p:// URI back into its components.
    
    Returns None if the format is invalid.
    """
    if not qr_text.startswith("wa-p2p://"):
        return None

    try:
        parts = qr_text[len("wa-p2p://"):].split("/")
        if len(parts) != 7 or parts[0] != "v1":
            return None

        tunnel_url = base64.urlsafe_b64decode(parts[4] + "===").decode()
        ice_json = base64.urlsafe_b64decode(parts[6] + "===").decode()
        # URL-safe components round-trip back to standard base64 so downstream
        # consumers (derive_session_key_from_x25519_pub, SDP decode) work unchanged.
        x25519_pub_b64 = base64.b64encode(base64.urlsafe_b64decode(parts[3] + "===")).decode()
        sdp_offer_b64 = base64.b64encode(base64.urlsafe_b64decode(parts[5] + "===")).decode()

        return {
            "version": parts[0],
            "instance_id": parts[1],
            "ed25519_public_key": parts[2],
            "x25519_public_key": x25519_pub_b64,
            "tunnel_url": tunnel_url,
            "sdp_offer_b64": sdp_offer_b64,
            "ice_candidates": json.loads(ice_json),
        }
    except Exception as e:
        logger.warning("Failed to decode invite QR: %s", e)
        return None


async def send_answer(
    tunnel_url: str,
    sdp_answer_b64: str,
    answer_ice_json: str,
    instance_id: str,
    ed25519_public_key_hex: str,
) -> bool:
    """Send the SDP answer to the primary peer via its tunnel URL.
    
    This is the ONLY data that flows through Cloudflare — ~1KB, once.
    Returns True if the answer was accepted.
    """
    import httpx

    payload = {
        "instance_id": instance_id,
        "public_key": ed25519_public_key_hex,
        "sdp_answer": sdp_answer_b64,
        "ice_candidates": json.loads(answer_ice_json),
    }

    try:
        async with httpx.AsyncClient(timeout=15.0, verify=True) as client:
            resp = await client.post(
                f"{tunnel_url.rstrip('/')}/api/v1/p2p/signaling",
                json=payload,
            )
            if resp.status_code == 200:
                data = resp.json()
                logger.info("Signaling answer accepted by %s", tunnel_url)
                return True
            else:
                logger.warning("Signaling answer rejected: HTTP %d — %s", resp.status_code, resp.text[:200])
                return False
    except Exception as e:
        logger.warning("Failed to send signaling answer to %s: %s", tunnel_url, e)
        return False


# In-memory store for pending signaling answers (keyed by remote instance_id)
_pending_answers: Dict[str, Dict] = {}


def _store_pending_answer(instance_id: str, sdp_answer: str, ice_candidates: list, public_key: str) -> None:
    """Store a pending SDP answer from a connecting peer.
    
    Called by the signaling endpoint. The WebRTC transport picks it up
    when completing the connection.
    """
    _pending_answers[instance_id] = {
        "sdp_answer": sdp_answer,
        "ice_candidates": ice_candidates,
        "public_key": public_key,
        "received_at": __import__('time').time(),
    }


def get_pending_answer(instance_id: str) -> Optional[Dict]:
    """Retrieve and consume a pending SDP answer."""
    return _pending_answers.pop(instance_id, None)
