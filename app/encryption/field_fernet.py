"""
FieldFernet — per-tenant field-level encryption using cryptography.Fernet.

DEKs are fetched through VaultKeyManager (which itself talks to the configured
SecretsBackend). Each ciphertext is self-describing:

    enc:v1:fernet:<key_version>:<base64-ciphertext>

so the migrator can identify encrypted rows and pick the correct DEK version
for decryption even during a rotation window.
"""

from __future__ import annotations

import logging
from typing import Optional

from cryptography.fernet import InvalidToken

from app.encryption.interface import EncryptionBackend, is_ciphertext, PREFIX
from app.encryption.vault_keys import get_vault_keys

FEATURE = {
    "id": "field",
    "display_name": "Field-level (Fernet)",
    "category": "encryption",
    "status": "beta",
    "summary": "Per-tenant field encryption of sensitive columns via the vault key manager.",
    "requires": ["a configured secrets vault for key storage"],
}

logger = logging.getLogger(__name__)

ALGO = "fernet"


def encode(key_version: int, raw_ct: bytes) -> str:
    return f"{PREFIX}{ALGO}:{key_version}:{raw_ct.decode('ascii')}"


def decode(s: str) -> Optional[tuple]:
    """Return (algo, key_version, raw_ct_bytes) or None if not a recognized blob."""
    if not is_ciphertext(s):
        return None
    rest = s[len(PREFIX):]
    parts = rest.split(":", 2)
    if len(parts) != 3:
        return None
    algo, ver_str, ct = parts
    try:
        version = int(ver_str)
    except ValueError:
        return None
    return algo, version, ct.encode("ascii")


class FieldFernet(EncryptionBackend):
    name = "field"
    level = "field"

    def __init__(self, settings: Optional[dict] = None) -> None:
        self._settings = settings or {}

    async def encrypt(self, user_id: str, plaintext: str) -> str:
        if plaintext is None or plaintext == "":
            return plaintext
        if is_ciphertext(plaintext):
            # Already encrypted — don't double-wrap.
            return plaintext
        vkm = get_vault_keys()
        dek, version = await vkm.get_or_create_dek(user_id)
        token = dek.encrypt(plaintext.encode("utf-8"))
        return encode(version, token)

    async def decrypt(self, user_id: str, ciphertext: str) -> str:
        if not ciphertext:
            return ciphertext
        decoded = decode(ciphertext)
        if decoded is None:
            # Plaintext / legacy row — return as-is so reads survive a partial migration.
            return ciphertext
        algo, version, raw_ct = decoded
        if algo != ALGO:
            logger.warning("Unknown ciphertext algo '%s' for user %s", algo, user_id)
            return ciphertext

        vkm = get_vault_keys()
        dek = await vkm.get_dek(user_id, version)
        if dek is None:
            logger.error("No DEK for user %s v%d; returning ciphertext unchanged", user_id, version)
            return ciphertext
        try:
            return dek.decrypt(raw_ct).decode("utf-8")
        except InvalidToken:
            logger.error("DEK v%d for user %s could not decrypt the row", version, user_id)
            return ciphertext
