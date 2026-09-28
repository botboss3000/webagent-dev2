"""
SQLCipher full-DB encryption — placeholder.

SQLCipher encrypts the entire SQLite database file under a single passphrase.
It is **not** tenant-isolated (all rows share the passphrase), so it is best
used as a defense-in-depth layer beneath field-level encryption.

Activation requires the optional `pysqlcipher3` dependency:

    pip install pysqlcipher3

When level == "full_db" the StorageBackend factory should branch on import
and instantiate a SQLCipher-aware LocalBackend. That branch is not yet
implemented — this module exists so the factory can refuse the level cleanly
with a clear message and so the UI can offer the option.
"""

from app.encryption.interface import EncryptionBackend

FEATURE = {
    "id": "full_db",
    "display_name": "Full-DB (SQLCipher)",
    "category": "encryption",
    "status": "experimental",
    "summary": "Whole-database encryption under one passphrase. Placeholder — not yet wired.",
    "requires": ["the optional pysqlcipher3 dependency"],
}


class SQLCipherEncryption(EncryptionBackend):
    name = "full_db"
    level = "full_db"

    def __init__(self, settings: dict | None = None) -> None:
        try:
            import pysqlcipher3  # noqa: F401
        except ImportError as e:
            raise RuntimeError(
                "SQLCipher encryption requires the `pysqlcipher3` package. "
                "Install it (`pip install pysqlcipher3`) and restart, "
                "then provision a passphrase via the encryption admin UI."
            ) from e
        # Full implementation will hook into LocalBackend on open() to apply
        # PRAGMA key='<passphrase>'. Until then, refuse to be used.
        raise NotImplementedError(
            "full_db (SQLCipher) is not yet wired into the active backend. "
            "Use 'field' for per-row encryption."
        )

    async def encrypt(self, user_id: str, plaintext: str) -> str:
        # SQLCipher operates at the file layer; rows pass through untouched.
        return plaintext

    async def decrypt(self, user_id: str, ciphertext: str) -> str:
        return ciphertext
