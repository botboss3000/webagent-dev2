"""P2P server — FastAPI routes for instance-to-instance sync.

Every request is authenticated via Ed25519 signature verification.  The sender's
public key must match a stored peer, and the signature must cover the full request
(method + path + timestamp + body sha256).

Endpoints:

  GET  /api/v1/p2p/status      — this instance's id, name, public key
  POST /api/v1/p2p/handshake   — exchange public keys, authorize a peer
  GET  /api/v1/p2p/manifest    — full ``data/`` file listing with sha256+mtime
  POST /api/v1/p2p/pull        — fetch changed files (base64-encoded contents)
"""

from __future__ import annotations

import base64 as _b64
import hashlib as _hl
import json
import logging
import socket
import time
from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.p2p import identity
from app.p2p import store as peer_store
from app.p2p.sync import build_manifest, snapshot_db_file

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/p2p", tags=["p2p"])

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_REPLAY_WINDOW_SEC = 90


# ── Pydantic models ────────────────────────────────────────────────────────────

class HandshakeRequest(BaseModel):
    instance_id: str = ""
    name: str = ""
    public_key: str = ""
    x25519_public_key: str = ""
    url: str = ""
    sync_options: dict = {}
    bootstrap_only: bool = False


class PullRequest(BaseModel):
    paths: List[str] = []
    snapshot_db: bool = False


class SignalingRequest(BaseModel):
    instance_id: str = ""
    public_key: str = ""
    sdp_answer: str = ""
    ice_candidates: list = []


# ── Auth helpers ───────────────────────────────────────────────────────────────

def _verify_request(request: Request, body_bytes: bytes) -> str:
    """Verify the request signature and return the peer's stored id (or raise 401)."""
    sender_id = request.headers.get("X-P2P-Instance-Id", "").strip()
    sig = request.headers.get("X-P2P-Signature", "").strip()
    ts_str = request.headers.get("X-P2P-Timestamp", "").strip()

    if not sender_id or not sig or not ts_str:
        raise HTTPException(status_code=401, detail="Missing p2p auth headers")

    # Replay protection
    try:
        ts_val = int(ts_str)
        if abs(time.time() - ts_val) > _REPLAY_WINDOW_SEC:
            raise HTTPException(status_code=401, detail="Request timestamp too old or in future")
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid timestamp")

    # Find the peer by remote instance id
    peers = peer_store.list_peers()
    peer = None
    for p in peers:
        if p.get("remote_instance_id") == sender_id:
            peer = p
            break
    if not peer:
        raise HTTPException(status_code=401, detail=f"Unknown peer instance: {sender_id}")

    # Check if this peer has been revoked
    from app.p2p.revocation import is_revoked
    pub_hex = peer.get("public_key", "")
    if is_revoked(pub_hex):
        raise HTTPException(status_code=403, detail="Peer has been revoked")

    # Verify signature
    if not identity.verify_signature(
        public_key_hex_str=pub_hex,
        method=request.method,
        path=request.url.path,
        timestamp=ts_str,
        body_bytes=body_bytes,
        signature_b64=sig,
    ):
        raise HTTPException(status_code=401, detail="Signature verification failed")

    return peer["id"]


async def _verify_async(request: Request) -> str:
    body_bytes = await request.body()
    return _verify_request(request, body_bytes)


async def _authenticated_json(request: Request) -> tuple[str, dict]:
    """Verify a signed request and decode its optionally encrypted JSON body.

    HttpTransport signs the bytes it actually sends.  Verification must therefore
    happen before base64/AES-GCM decoding.
    """
    body_bytes = await request.body()
    peer_id = _verify_request(request, body_bytes)
    plain = body_bytes
    if request.headers.get("X-P2P-Encrypted") == "1":
        peer = peer_store.get_peer(peer_id) or {}
        peer_x25519 = str(peer.get("x25519_public_key") or "")
        if not peer_x25519:
            raise HTTPException(status_code=400, detail="Peer encryption key is missing")
        try:
            from app.p2p.transport.crypto import (
                decrypt_payload,
                derive_session_key_from_x25519_pub,
            )
            session_key = derive_session_key_from_x25519_pub(
                identity.private_key_bytes(), peer_x25519
            )
            plain = decrypt_payload(session_key, _b64.b64decode(body_bytes))
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Could not decrypt P2P payload") from exc
    try:
        decoded = json.loads(plain.decode("utf-8") or "{}")
    except (UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Invalid P2P JSON payload") from exc
    if not isinstance(decoded, dict):
        raise HTTPException(status_code=400, detail="P2P payload must be an object")
    return peer_id, decoded


# ── Routes ─────────────────────────────────────────────────────────────────────

# ── Signaling Route (bootstrap — no signature required) ───────────────────────────

@router.post("/signaling")
async def p2p_signaling(body: SignalingRequest):
    """Receive an SDP answer from a connecting peer.
    
    Bootstrap endpoint — no Ed25519 signature required. The DTLS handshake
    that follows provides mutual authentication via SDP fingerprints.
    Rate-limited to prevent abuse.
    """
    if not body.sdp_answer:
        raise HTTPException(status_code=400, detail="Missing sdp_answer")
    if not body.instance_id:
        raise HTTPException(status_code=400, detail="Missing instance_id")
    if not body.public_key or len(body.public_key) != 64:
        raise HTTPException(status_code=400, detail="Invalid public_key")

    try:
        bytes.fromhex(body.public_key)
    except ValueError:
        raise HTTPException(status_code=400, detail="public_key is not valid hex")

    if body.instance_id == identity.instance_id() or body.public_key == identity.public_key_hex():
        raise HTTPException(
            status_code=409,
            detail="Cannot pair an instance with a cloned copy of its own P2P identity",
        )

    # Store the pending answer for the WebRTC transport to pick up
    # (the transport layer polls for pending answers)
    from app.p2p.transport.signaling import _store_pending_answer
    _store_pending_answer(body.instance_id, body.sdp_answer, body.ice_candidates, body.public_key)

    return {
        "status": "accepted",
        "instance_id": identity.instance_id(),
        "public_key": identity.public_key_hex(),
        "x25519_public_key": _get_x25519_public_key_b64(),
        "name": _hostname(),
    }


@router.get("/status")
async def p2p_status():
    """Return this instance's identity — the first step in a peering handshake."""
    return {
        "instance_id": identity.instance_id(),
        "public_key": identity.public_key_hex(),
        "name": _hostname(),
        "protocol_version": 1,
        "capabilities": {"scoped_bootstrap": True},
    }


@router.post("/handshake")
async def p2p_handshake(body: HandshakeRequest):
    """Accept a peer handshake.  No signature required — bootstrap endpoint."""
    if not body.public_key or len(body.public_key) != 64:
        raise HTTPException(status_code=400, detail="Invalid public_key (64 hex chars required)")

    try:
        bytes.fromhex(body.public_key)
    except ValueError:
        raise HTTPException(status_code=400, detail="public_key is not valid hex")

    url = body.url or ""
    name = body.name or body.instance_id or "peer"

    stored = peer_store.add_peer(
        url=url,
        name=name,
        public_key_hex=body.public_key,
        remote_instance_id=body.instance_id,
        x25519_public_key=body.x25519_public_key,
        sync_options=body.sync_options,
        bootstrap_only=body.bootstrap_only,
    )

    return {
        "peer_id": stored["id"],
        "instance_id": identity.instance_id(),
        "public_key": identity.public_key_hex(),
        "x25519_public_key": _get_x25519_public_key_b64(),
        "name": _hostname(),
    }


@router.post("/bootstrap/apply")
async def p2p_bootstrap_apply(request: Request):
    """Apply a one-time, category-scoped bootstrap from an authenticated peer.

    Unlike recurring manifest sync this endpoint never replaces app.db.  Secret
    values arrive inside the encrypted transport payload and are re-encrypted by
    the target's own storage facade.
    """
    _peer_id, payload = await _authenticated_json(request)
    try:
        from app.p2p.bootstrap import apply_payload
        return await apply_payload(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("P2P bootstrap apply failed")
        raise HTTPException(status_code=500, detail="Could not apply P2P bootstrap") from exc


@router.get("/manifest")
async def p2p_manifest(request: Request):
    """Return a full classified manifest of this instance's data/ folder.

    Requires valid p2p auth headers.
    """
    await _verify_async(request)
    from app.p2p.manifest import build_classified_manifest
    classified = build_classified_manifest()
    return {
        "instance_id": identity.instance_id(),
        "classified": classified,
        "full_count": len(classified["full"]),
        "row_count": len(classified["row"]),
    }


@router.post("/pull")
async def p2p_pull(request: Request, body: PullRequest):
    """Fetch file contents from this instance.

    ``snapshot_db`` = True snapshots .db files via ``sqlite3 .backup`` so the
    sender never transmits a half-written database.
    """
    await _verify_async(request)

    if not body.paths:
        return {"files": []}

    files = []
    for relpath in body.paths:
        try:
            if relpath.endswith(".db") and body.snapshot_db:
                content = snapshot_db_file(relpath)
            else:
                full = _PROJECT_ROOT / relpath
                if not full.exists():
                    logger.warning("Pull requested missing file: %s", relpath)
                    continue
                content = full.read_bytes()

            files.append({
                "path": relpath,
                "content_b64": _b64.b64encode(content).decode(),
                "sha256": _hl.sha256(content).hexdigest(),
                "size": len(content),
            })
        except Exception as e:
            logger.warning("Pull error for %s: %s", relpath, e)

    return {"files": files}


# ── Vault row-sync models ────────────────────────────────────────────────────────

class VaultManifestRequest(BaseModel):
    vault_schema: str = ""  # e.g. "vault_app" — empty means all three


class VaultPullRequest(BaseModel):
    vault_schema: str = ""
    rows: list = []  # list of {user_id, service, label} keys to fetch


class VaultPushRequest(BaseModel):
    vault_schema: str = ""
    rows: list = []  # list of full row dicts to apply


# ── Vault row-sync routes ────────────────────────────────────────────────────────

@router.post("/vault/manifest")
async def p2p_vault_manifest(request: Request, body: VaultManifestRequest):
    """Return a row-level manifest for one or all vault schemas.

    Requires valid p2p auth headers.
    """
    await _verify_async(request)
    from app.p2p.vault_sync import build_vault_row_manifest

    schemas = [body.vault_schema] if body.vault_schema else ["vault_app", "vault_agent", "vault_user"]
    result = {}
    for schema in schemas:
        try:
            result[schema] = build_vault_row_manifest(schema)
        except Exception as e:
            logger.warning("Vault manifest for %s failed: %s", schema, e)
            result[schema] = []

    return {
        "instance_id": identity.instance_id(),
        "manifests": result,
    }


@router.post("/vault/pull")
async def p2p_vault_pull(request: Request, body: VaultPullRequest):
    """Fetch specific vault rows by (user_id, service, label) keys.

    Requires valid p2p auth headers.
    """
    await _verify_async(request)
    from app.p2p.vault_sync import build_vault_row_manifest

    if not body.vault_schema or not body.rows:
        return {"rows": []}

    # Build full manifest, then filter to requested keys
    full_manifest = build_vault_row_manifest(body.vault_schema)
    requested_keys = {(r["user_id"], r["service"], r["label"]) for r in body.rows}
    matching = [r for r in full_manifest if (r["user_id"], r["service"], r["label"]) in requested_keys]

    return {"rows": matching}


@router.post("/vault/push")
async def p2p_vault_push(request: Request, body: VaultPushRequest):
    """Receive vault rows from a peer and apply them.

    Requires valid p2p auth headers.
    """
    await _verify_async(request)
    from app.p2p.vault_sync import apply_vault_rows

    if not body.vault_schema or not body.rows:
        raise HTTPException(status_code=400, detail="Missing vault_schema or rows")

    result = apply_vault_rows(body.vault_schema, body.rows, direction="push")
    return result


def _hostname() -> str:
    try:
        return socket.gethostname() or "webagent"
    except Exception:
        return "webagent"


def _get_x25519_public_key_b64() -> str:
    """Return our X25519 public key (derived from Ed25519 seed) as base64."""
    try:
        from app.p2p.transport.crypto import local_x25519_public_key_b64
        return local_x25519_public_key_b64()
    except Exception:
        return ""
