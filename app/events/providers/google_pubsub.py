"""Google Cloud Pub/Sub push verification + payload decoding.

Gmail, Google Calendar, and Google Drive all push change notifications via
Pub/Sub. Pub/Sub signs each push as an OIDC JWT in the ``Authorization:
Bearer ...`` header; the JWT's audience equals the push subscription's
configured audience. We verify against Google's published JWKS.

Environment:
  - ``EVENTS_PUBSUB_AUDIENCE`` — the audience string set when the Pub/Sub
    push subscription was created. Typically the public webhook URL (e.g.
    ``https://your.host/api/v1/events/gmail``). Required.
  - ``EVENTS_PUBSUB_SA_EMAIL`` — optional. If set, the JWT ``email`` claim
    must match this service-account address (defence in depth).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from typing import Any, Dict, Optional, Tuple

import httpx
from fastapi import Request

logger = logging.getLogger(__name__)

_GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
_JWKS_CACHE: Dict[str, Any] = {"fetched_at": 0, "keys": {}}
_JWKS_TTL = 3600


async def _get_jwks() -> Dict[str, Any]:
    now = time.time()
    if _JWKS_CACHE["keys"] and (now - _JWKS_CACHE["fetched_at"]) < _JWKS_TTL:
        return _JWKS_CACHE["keys"]
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(_GOOGLE_JWKS_URL)
            r.raise_for_status()
            data = r.json()
        keys = {k["kid"]: k for k in data.get("keys", [])}
        _JWKS_CACHE["keys"] = keys
        _JWKS_CACHE["fetched_at"] = now
        return keys
    except Exception as e:
        logger.warning("Could not refresh Google JWKS: %s", e)
        return _JWKS_CACHE["keys"]


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


async def verify_pubsub_jwt(request: Request, expected_audience: Optional[str] = None) -> bool:
    """Verify a Pub/Sub push request's OIDC JWT. Returns True on success."""
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    if not auth or not auth.lower().startswith("bearer "):
        return False
    token = auth.split(None, 1)[1].strip()
    parts = token.split(".")
    if len(parts) != 3:
        return False

    audience = expected_audience or os.environ.get("EVENTS_PUBSUB_AUDIENCE", "")
    if not audience:
        logger.warning("EVENTS_PUBSUB_AUDIENCE not set; refusing to verify Pub/Sub JWT")
        return False

    try:
        header = json.loads(_b64url_decode(parts[0]))
        payload = json.loads(_b64url_decode(parts[1]))
    except Exception:
        return False

    if payload.get("iss") not in ("https://accounts.google.com", "accounts.google.com"):
        return False
    if payload.get("aud") != audience:
        logger.warning("Pub/Sub JWT aud mismatch: got %r want %r", payload.get("aud"), audience)
        return False
    if int(payload.get("exp", 0)) < int(time.time()) - 60:
        return False

    expected_sa = os.environ.get("EVENTS_PUBSUB_SA_EMAIL", "")
    if expected_sa and payload.get("email") != expected_sa:
        logger.warning("Pub/Sub JWT email mismatch: got %r want %r",
                       payload.get("email"), expected_sa)
        return False

    try:
        from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers
        from cryptography.hazmat.primitives.asymmetric.padding import PKCS1v15
        from cryptography.hazmat.primitives.hashes import SHA256
        from cryptography.hazmat.primitives.serialization import load_der_public_key  # noqa: F401
        from cryptography.hazmat.backends import default_backend  # noqa: F401
    except ImportError:
        logger.warning("cryptography not installed; cannot verify Pub/Sub JWT signature")
        return False

    keys = await _get_jwks()
    kid = header.get("kid")
    jwk = keys.get(kid)
    if not jwk:
        # Refresh once in case of key rotation.
        _JWKS_CACHE["fetched_at"] = 0
        keys = await _get_jwks()
        jwk = keys.get(kid)
    if not jwk:
        return False

    try:
        n = int.from_bytes(_b64url_decode(jwk["n"]), "big")
        e = int.from_bytes(_b64url_decode(jwk["e"]), "big")
        pub = RSAPublicNumbers(e, n).public_key()
        signing_input = f"{parts[0]}.{parts[1]}".encode("ascii")
        signature = _b64url_decode(parts[2])
        pub.verify(signature, signing_input, PKCS1v15(), SHA256())
        return True
    except Exception as e:
        logger.warning("Pub/Sub JWT signature verification failed: %s", e)
        return False


async def decode_pubsub_envelope(request: Request) -> Tuple[Dict[str, Any], bytes]:
    """Pull (attributes, data_bytes) out of a Pub/Sub push request body."""
    body = await request.json()
    msg = body.get("message") or {}
    attrs = msg.get("attributes") or {}
    data_b64 = msg.get("data") or ""
    try:
        data = _b64url_decode(data_b64) if data_b64 else b""
    except Exception:
        data = b""
    return attrs, data
