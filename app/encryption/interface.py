"""
Abstract encryption-backend interface.

An EncryptionBackend transparently encrypts and decrypts sensitive strings
on behalf of the StorageBackend decorator (see app/db/interface.py).

Tenant-scoped: every method takes a `user_id` so per-tenant DEKs can be
used. Implementations that ignore the tenant (e.g. NoEncryption) still
accept the argument.

Ciphertext format produced by encrypting backends:
    "enc:v1:<algo>:<key_version>:<base64-ciphertext>"

The prefix is self-describing so the migration loop can tell encrypted rows
from plaintext rows and pick the correct key version for decryption.
"""

from abc import ABC, abstractmethod
from typing import Optional


PREFIX = "enc:v1:"


def is_ciphertext(s: Optional[str]) -> bool:
    """True if s is a WebAgent-formatted ciphertext blob."""
    return bool(s) and s.startswith(PREFIX)


class EncryptionBackend(ABC):
    """Async encrypt/decrypt over per-tenant data."""

    name: str = "unknown"
    level: str = "none"

    @abstractmethod
    async def encrypt(self, user_id: str, plaintext: str) -> str:
        """Encrypt plaintext for a tenant. Returns the ciphertext string (prefixed)."""
        ...

    @abstractmethod
    async def decrypt(self, user_id: str, ciphertext: str) -> str:
        """Decrypt a ciphertext string. If the input is not encrypted, return as-is."""
        ...

    async def health(self) -> dict:
        """
        Sanity check. Default implementation tries a round-trip with a synthetic
        user. Specific backends may override.
        Returns {"ok": bool, "level": str, "message": str}.
        """
        try:
            sample = "encryption-health-probe"
            ct = await self.encrypt("_health_probe", sample)
            pt = await self.decrypt("_health_probe", ct)
            ok = pt == sample
            return {
                "ok": ok,
                "level": self.level,
                "message": f"{self.name} round-trip {'ok' if ok else 'mismatch'}",
            }
        except Exception as e:
            return {"ok": False, "level": self.level, "message": str(e)}
