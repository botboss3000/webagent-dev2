"""HttpTransport — Ed25519-signed + ECDH-encrypted HTTP requests to a peer's URL.

Wraps the existing httpx pattern from worker.py/sync.py into the Transport interface,
adding ECDH payload encryption on top of Ed25519 request signing.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Dict

import httpx

from app.p2p import identity
from app.p2p.transport import Transport
from app.p2p.transport.crypto import (
    derive_session_key_from_x25519_pub,
    encrypt_payload,
    decrypt_payload,
)

logger = logging.getLogger(__name__)

HTTP_TIMEOUT = 60.0


class HttpTransport(Transport):
    """HTTP-based transport with ECDH-encrypted payloads."""

    def __init__(self) -> None:
        self._session_keys: Dict[str, bytes] = {}  # peer_id -> AES-256-GCM key

    async def connect(self, peer: Dict) -> bool:
        """Derive session key from the peer's X25519 public key."""
        try:
            x25519_pub = peer.get("x25519_public_key", "")
            if x25519_pub:
                key = derive_session_key_from_x25519_pub(
                    identity.private_key_bytes(),
                    x25519_pub,
                )
                self._session_keys[peer["id"]] = key
            return True
        except Exception as e:
            logger.warning("HttpTransport connect failed for %s: %s", peer.get("id"), e)
            return False

    async def send(self, peer_id: str, method: str, path: str, body: bytes) -> bytes:
        """Send a signed + encrypted request to a peer."""
        from app.p2p import store as peer_store
        peer = peer_store.get_peer(peer_id)
        if not peer:
            raise ValueError(f"Unknown peer: {peer_id}")

        url = peer["url"].rstrip("/")
        key = self._session_keys.get(peer_id)

        # Encrypt body if we have a session key
        if key and body:
            encrypted_body = encrypt_payload(key, body)
            body_to_send = base64.b64encode(encrypted_body)
            headers = {"X-P2P-Encrypted": "1"}
        else:
            body_to_send = body
            headers = {}

        # Sign the request (signature covers the encrypted body)
        body_bytes = body_to_send if isinstance(body_to_send, bytes) else body_to_send.encode()
        sig, ts = identity.sign_request(method, path, body_bytes)

        headers.update({
            "Content-Type": "application/json",
            "X-P2P-Instance-Id": identity.instance_id(),
            "X-P2P-Signature": sig,
            "X-P2P-Timestamp": ts,
        })

        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, verify=True) as client:
            resp = await client.request(
                method=method,
                url=f"{url}{path}",
                content=body_bytes,
                headers=headers,
            )
            if resp.status_code != 200:
                raise IOError(f"HTTP {resp.status_code} from {url}{path}: {resp.text[:200]}")

            response_bytes = resp.content
            # Decrypt if encrypted
            if resp.headers.get("X-P2P-Encrypted") == "1" and key:
                response_bytes = decrypt_payload(key, response_bytes)

            return response_bytes

    async def disconnect(self, peer_id: str) -> None:
        self._session_keys.pop(peer_id, None)

    async def is_connected(self, peer_id: str) -> bool:
        # HTTP is connectionless — "connected" means we have a session key
        return peer_id in self._session_keys
