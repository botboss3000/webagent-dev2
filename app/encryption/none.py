"""
No-op encryption backend. Pass-through; the default level.
"""

from app.encryption.interface import EncryptionBackend

FEATURE = {
    "id": "none",
    "display_name": "No encryption",
    "category": "encryption",
    "status": "stable",
    "summary": "Pass-through — data stored as-is. The default.",
}


class NoEncryption(EncryptionBackend):
    name = "none"
    level = "none"

    async def encrypt(self, user_id: str, plaintext: str) -> str:
        return plaintext

    async def decrypt(self, user_id: str, ciphertext: str) -> str:
        return ciphertext

    async def health(self) -> dict:
        return {"ok": True, "level": self.level, "message": "encryption disabled"}
