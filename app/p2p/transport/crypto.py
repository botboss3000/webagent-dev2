"""ECDH payload encryption for P2P transport — shared by HttpTransport and WebRTCTransport.

Derives session keys from Ed25519 keypairs (converted to X25519 per RFC 8032),
then encrypts payloads with AES-256-GCM. This provides end-to-end confidentiality
on top of the Ed25519 signature authentication.
"""

from __future__ import annotations

import base64
import hashlib
import os
from typing import Tuple

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import x25519
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.p2p import identity


def local_x25519_public_key_b64() -> str:
    """This instance's X25519 public key, derived from its Ed25519 seed."""
    private = x25519.X25519PrivateKey.from_private_bytes(
        _ed25519_priv_to_x25519(identity.private_key_bytes())
    )
    return base64.b64encode(private.public_key().public_bytes_raw()).decode()


def _ed25519_priv_to_x25519(ed25519_priv_bytes: bytes) -> bytes:
    """Convert an Ed25519 private key (raw 32 bytes) to an X25519 private key."""
    import hashlib
    # SHA-512 the seed, take first 32 bytes, clamp per RFC 8032
    h = hashlib.sha512(ed25519_priv_bytes).digest()[:32]
    # Clamp
    h = bytearray(h)
    h[0] &= 248
    h[31] &= 127
    h[31] |= 64
    return bytes(h)


def derive_session_key(ed25519_priv_bytes: bytes, peer_public_key_hex: str) -> bytes:
    """Derive a shared AES-256-GCM session key from our Ed25519 private key
    and the peer's Ed25519 public key (hex-encoded raw 32 bytes).
    
    Converts both to X25519, performs ECDH, then HKDF-SHA256 to derive a 32-byte key.
    """
    our_x25519 = x25519.X25519PrivateKey.from_private_bytes(
        _ed25519_priv_to_x25519(ed25519_priv_bytes)
    )
    peer_pub_bytes = bytes.fromhex(peer_public_key_hex)
    # Convert peer Ed25519 pub to X25519 pub (RFC 8032)
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    ed_pub = Ed25519PublicKey.from_public_bytes(peer_pub_bytes)
    peer_x25519_bytes = _ed25519_pub_to_x25519(peer_pub_bytes)
    peer_x25519 = x25519.X25519PublicKey.from_public_bytes(peer_x25519_bytes)
    
    shared = our_x25519.exchange(peer_x25519)
    
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=b"webagent-p2p-session-key-v1",
    )
    return hkdf.derive(shared)


def _ed25519_pub_to_x25519(ed_pub_bytes: bytes) -> bytes:
    """Convert Ed25519 public key bytes to X25519 public key bytes.
    
    Not implemented — we exchange X25519 public keys during the handshake instead.
    Use ``derive_session_key_from_x25519_pub()`` with the peer's X25519 public key.
    """
    raise NotImplementedError(
        "Use derive_session_key_from_x25519_pub() with the peer's X25519 public key "
        "from the handshake."
    )


def derive_session_key_from_x25519_pub(ed25519_priv_bytes: bytes, peer_x25519_pub_b64: str) -> bytes:
    """Derive session key using our Ed25519 private key and the peer's X25519 public key
    (base64-encoded raw 32 bytes, exchanged during handshake)."""
    our_x25519 = x25519.X25519PrivateKey.from_private_bytes(
        _ed25519_priv_to_x25519(ed25519_priv_bytes)
    )
    peer_pub_bytes = base64.b64decode(peer_x25519_pub_b64)
    peer_x25519 = x25519.X25519PublicKey.from_public_bytes(peer_pub_bytes)
    
    shared = our_x25519.exchange(peer_x25519)
    
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=b"webagent-p2p-session-key-v1",
    )
    return hkdf.derive(shared)


def encrypt_payload(key: bytes, plaintext: bytes) -> bytes:
    """AES-256-GCM encrypt. Returns nonce (12 bytes) + ciphertext + tag."""
    nonce = os.urandom(12)
    aesgcm = AESGCM(key)
    ciphertext = aesgcm.encrypt(nonce, plaintext, None)
    return nonce + ciphertext


def decrypt_payload(key: bytes, ciphertext: bytes) -> bytes:
    """AES-256-GCM decrypt. Expects nonce (12 bytes) + ciphertext + tag."""
    nonce = ciphertext[:12]
    ct = ciphertext[12:]
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(nonce, ct, None)


# ── KEK exchange (for cross-peer Fernet decryption) ──────────────────────────────

def encrypt_kek_for_peer(
    session_key: bytes,
    kek_bytes: bytes,
) -> bytes:
    """Encrypt our KEK for transmission to a peer, under the ECDH session key.
    
    The KEK is a Fernet-compatible base64 string; we encode it to bytes
    and encrypt with AES-256-GCM.
    
    Returns: nonce (12 bytes) + ciphertext + tag, for the peer to decrypt
              with decrypt_payload + the same session key.
    """
    return encrypt_payload(session_key, kek_bytes)


def decrypt_peer_kek(
    session_key: bytes,
    encrypted_kek: bytes,
) -> bytes:
    """Decrypt a peer's KEK received during handshake."""
    return decrypt_payload(session_key, encrypted_kek)
