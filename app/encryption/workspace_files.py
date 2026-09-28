"""Envelope encryption for durable files in a user's workspace.

Each workspace uses its existing server-managed tenant DEK.  Every file gets a
fresh random AES-256-GCM object key; that object key is wrapped by the workspace
DEK and stored in a small, versioned header beside the ciphertext.  Consequently
rotating a workspace DEK only requires re-wrapping object keys, not decrypting
and rewriting the file contents.

Legacy plaintext remains readable so an installation can migrate without an
all-at-once outage.  New writes through this module are always encrypted.
"""

from __future__ import annotations

import os
import struct
import uuid
from pathlib import Path

from cryptography.fernet import InvalidToken
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.exceptions import InvalidTag

from app.encryption.vault_keys import get_vault_keys

MAGIC = b"WAWSENC1"
FORMAT_VERSION = 1
NONCE_BYTES = 12
OBJECT_KEY_BYTES = 32
_HEADER = struct.Struct(">8sBIH12s")
_AAD_PREFIX = b"webagent-workspace-file-v1\x00"


class WorkspaceEncryptionError(ValueError):
    """The file is encrypted but malformed or cannot be authenticated."""


def workspace_key_id(owner_id: str) -> str:
    """Return the stable key identity used by the owner's on-disk workspace."""
    cleaned = "".join(c for c in str(owner_id or "") if c.isalnum() or c in "-_")
    if not cleaned:
        raise WorkspaceEncryptionError("workspace owner is required")
    return cleaned


def is_encrypted(payload: bytes) -> bool:
    return len(payload) >= len(MAGIC) and payload.startswith(MAGIC)


def _aad(owner_id: str) -> bytes:
    return _AAD_PREFIX + workspace_key_id(owner_id).encode("utf-8")


async def encrypt_bytes(owner_id: str, plaintext: bytes) -> bytes:
    """Encrypt bytes using a new object key wrapped by the workspace DEK."""
    tenant = workspace_key_id(owner_id)
    dek, key_version = await get_vault_keys().get_or_create_dek(tenant)
    object_key = AESGCM.generate_key(bit_length=OBJECT_KEY_BYTES * 8)
    wrapped_key = dek.encrypt(object_key)
    if len(wrapped_key) > 0xFFFF:
        raise WorkspaceEncryptionError("wrapped object key is too large")
    nonce = os.urandom(NONCE_BYTES)
    ciphertext = AESGCM(object_key).encrypt(nonce, plaintext, _aad(tenant))
    return _HEADER.pack(
        MAGIC, FORMAT_VERSION, int(key_version), len(wrapped_key), nonce
    ) + wrapped_key + ciphertext


async def decrypt_bytes(owner_id: str, payload: bytes) -> bytes:
    """Decrypt a workspace payload; return legacy plaintext unchanged."""
    if not is_encrypted(payload):
        return payload
    if len(payload) < _HEADER.size:
        raise WorkspaceEncryptionError("encrypted workspace file is truncated")
    magic, format_version, key_version, wrapped_len, nonce = _HEADER.unpack_from(payload)
    if magic != MAGIC or format_version != FORMAT_VERSION:
        raise WorkspaceEncryptionError("unsupported workspace encryption format")
    wrapped_end = _HEADER.size + wrapped_len
    if wrapped_len == 0 or len(payload) <= wrapped_end:
        raise WorkspaceEncryptionError("encrypted workspace file is truncated")
    tenant = workspace_key_id(owner_id)
    dek = await get_vault_keys().get_dek(tenant, key_version)
    if dek is None:
        raise WorkspaceEncryptionError(
            f"workspace DEK v{key_version} is unavailable for {tenant}"
        )
    try:
        object_key = dek.decrypt(payload[_HEADER.size:wrapped_end])
        return AESGCM(object_key).decrypt(
            nonce, payload[wrapped_end:], _aad(tenant)
        )
    except (InvalidToken, InvalidTag, ValueError) as exc:
        raise WorkspaceEncryptionError(
            "encrypted workspace file failed authentication"
        ) from exc


def _atomic_replace(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


async def write_encrypted_file(owner_id: str, path: Path, plaintext: bytes) -> None:
    """Encrypt and atomically replace ``path`` with the resulting envelope."""
    _atomic_replace(Path(path), await encrypt_bytes(owner_id, plaintext))


async def read_encrypted_file(owner_id: str, path: Path) -> bytes:
    """Read and decrypt ``path``, accepting a legacy plaintext file."""
    return await decrypt_bytes(owner_id, Path(path).read_bytes())


async def encrypt_file_in_place(owner_id: str, path: Path) -> bool:
    """Encrypt a legacy plaintext file in place. Return true when changed."""
    target = Path(path)
    payload = target.read_bytes()
    if is_encrypted(payload):
        return False
    _atomic_replace(target, await encrypt_bytes(owner_id, payload))
    return True


async def rewrap_file_key(owner_id: str, path: Path) -> bool:
    """Re-wrap an encrypted file's object key with the active workspace DEK.

    File ciphertext is untouched.  Returns false when the file is plaintext or
    already uses the active workspace DEK.
    """
    target = Path(path)
    payload = target.read_bytes()
    if not is_encrypted(payload):
        return False
    if len(payload) < _HEADER.size:
        raise WorkspaceEncryptionError("encrypted workspace file is truncated")
    magic, version, old_key_version, wrapped_len, nonce = _HEADER.unpack_from(payload)
    if magic != MAGIC or version != FORMAT_VERSION:
        raise WorkspaceEncryptionError("unsupported workspace encryption format")
    wrapped_end = _HEADER.size + wrapped_len
    tenant = workspace_key_id(owner_id)
    manager = get_vault_keys()
    old_dek = await manager.get_dek(tenant, old_key_version)
    if old_dek is None:
        raise WorkspaceEncryptionError(f"workspace DEK v{old_key_version} is unavailable")
    active_dek, active_version = await manager.get_or_create_dek(tenant)
    if active_version == old_key_version:
        return False
    try:
        object_key = old_dek.decrypt(payload[_HEADER.size:wrapped_end])
    except InvalidToken as exc:
        raise WorkspaceEncryptionError("wrapped object key failed authentication") from exc
    new_wrapped = active_dek.encrypt(object_key)
    updated = _HEADER.pack(MAGIC, FORMAT_VERSION, active_version, len(new_wrapped), nonce)
    _atomic_replace(target, updated + new_wrapped + payload[wrapped_end:])
    return True
