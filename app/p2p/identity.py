"""P2P identity — a per-instance Ed25519 keypair for mutual authentication.

Generated once and persisted in ``data/config/p2p/runtime/keypair.json``.  Every outbound
P2P request is signed with this key; every inbound request is verified against the
peer's stored public key.  A timestamp nonce in each signature prevents replay.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Dict, Optional, Tuple

from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives import serialization

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_CONFIG_DIR = _PROJECT_ROOT / "data" / "config" / "p2p"
_KEYPAIR_PATH = _CONFIG_DIR / "runtime" / "keypair.json"

_KEYPAIR: Optional[Dict[str, str]] = None    # {private: b64, public: b64, id: str}
_INSTANCE_ID: Optional[str] = None


def _ensure_keypair() -> Dict[str, str]:
    """Load or generate the instance's Ed25519 keypair.  Idempotent — called once
    per process then cached."""
    global _KEYPAIR, _INSTANCE_ID
    if _KEYPAIR:
        return _KEYPAIR

    _KEYPAIR_PATH.parent.mkdir(parents=True, exist_ok=True)

    if _KEYPAIR_PATH.exists():
        try:
            _KEYPAIR = json.loads(_KEYPAIR_PATH.read_text(encoding="utf-8"))
            if _KEYPAIR.get("private") and _KEYPAIR.get("public"):
                _INSTANCE_ID = _KEYPAIR.get("id", uuid.uuid4().hex[:12])
                return _KEYPAIR
        except Exception:
            logger.warning("Corrupt p2p keypair, regenerating", exc_info=True)

    # Generate new keypair
    private_key = ed25519.Ed25519PrivateKey.generate()
    public_key = private_key.public_key()

    priv_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_bytes = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )

    _INSTANCE_ID = uuid.uuid4().hex[:12]
    _KEYPAIR = {
        "id": _INSTANCE_ID,
        "private": base64.b64encode(priv_bytes).decode(),
        "public": base64.b64encode(pub_bytes).decode(),
    }

    _KEYPAIR_PATH.write_text(json.dumps(_KEYPAIR, indent=2), encoding="utf-8")
    logger.info("Generated new p2p Ed25519 keypair (id=%s)", _INSTANCE_ID)
    return _KEYPAIR


def instance_id() -> str:
    """This instance's stable P2P id."""
    kp = _ensure_keypair()
    return kp["id"]


def private_key_bytes() -> bytes:
    """This instance's Ed25519 private key (raw 32 bytes)."""
    kp = _ensure_keypair()
    return base64.b64decode(kp["private"])


def public_key_hex() -> str:
    """This instance's public key as a hex string (for display / handshake)."""
    kp = _ensure_keypair()
    return base64.b64decode(kp["public"]).hex()


def public_key_pem() -> str:
    """This instance's public key as a PEM string (for config display)."""
    priv = ed25519.Ed25519PrivateKey.from_private_bytes(private_key_bytes())
    pub = priv.public_key()
    return pub.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()


def sign_request(method: str, path: str, body_bytes: bytes = b"") -> Tuple[str, str]:
    """Sign an outbound request.  Returns (signature_b64, timestamp_iso).

    The signature covers: ``method:path:timestamp:body_sha256``.  Timestamp is
    included in the ``X-P2P-Signature`` header and verified on receipt.
    """
    import hashlib

    ts = str(int(time.time()))
    payload = f"{method}:{path}:{ts}:{hashlib.sha256(body_bytes).hexdigest()}"
    priv = ed25519.Ed25519PrivateKey.from_private_bytes(private_key_bytes())
    sig = priv.sign(payload.encode())
    return base64.b64encode(sig).decode(), ts


def verify_signature(
    public_key_hex_str: str,
    method: str,
    path: str,
    timestamp: str,
    body_bytes: bytes,
    signature_b64: str,
) -> bool:
    """Verify an inbound request signature.  Returns True if valid, False otherwise."""
    import hashlib

    try:
        pub_bytes = bytes.fromhex(public_key_hex_str)
        pub_key = ed25519.Ed25519PublicKey.from_public_bytes(pub_bytes)
        sig = base64.b64decode(signature_b64)
    except Exception:
        return False

    payload = f"{method}:{path}:{timestamp}:{hashlib.sha256(body_bytes).hexdigest()}"
    try:
        pub_key.verify(sig, payload.encode())
        return True
    except Exception:
        return False


def sign_payload(payload_bytes: bytes) -> Tuple[str, str]:
    """Convenience: sign POST body for a peer request.  Wraps ``sign_request``
    with the default method/path filled in by the caller."""
    return sign_request("POST", "", payload_bytes)
