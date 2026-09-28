"""
KMS-wrapped encryption — placeholder.

Same as FieldFernet but the KEK is held in a cloud KMS (GCP KMS or AWS KMS).
The KEK never leaves the KMS; DEK wrap/unwrap calls hit the KMS Encrypt /
Decrypt APIs and DEKs are cached in-process with a short TTL.

For now this class refuses construction with a clear message so the UI can
expose the option without claiming functionality that isn't wired yet.
Building it out is a follow-up: implement GCPKMSKey / AWSKMSKey key sources
and inject them into VaultKeyManager in place of the local KEK.
"""

from app.encryption.interface import EncryptionBackend

FEATURE = {
    "id": "kms",
    "display_name": "KMS-wrapped (Fernet)",
    "category": "encryption",
    "status": "experimental",
    "summary": "Field encryption with the key-encryption-key held in a cloud KMS. Placeholder — not yet wired.",
    "requires": ["GCP KMS or AWS KMS"],
}


class KMSWrappedFernet(EncryptionBackend):
    name = "kms"
    level = "kms"

    def __init__(self, settings: dict | None = None) -> None:
        raise NotImplementedError(
            "kms encryption is not yet wired. Use 'field' with a strong "
            "secrets-vault provider (os_keyring / gcp_secret_manager / "
            "aws_secrets_manager) instead."
        )

    async def encrypt(self, user_id: str, plaintext: str) -> str:
        return plaintext

    async def decrypt(self, user_id: str, ciphertext: str) -> str:
        return ciphertext
