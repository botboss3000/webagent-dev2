"""
Abstract attachment-store backend.

An AttachmentStore owns the persistence of file bytes for the `attachments`
table — chat images, voice recordings, dropped documents.

Implementations:
  - LocalAttachmentStore     data/user_data/{user_id}/uploads/{uuid}.ext on disk (default)
  - BrowserAttachmentStore   server records metadata only; bytes live in the
                             user's browser IndexedDB. Multi-device users
                             will not see each other's attachments.
  - S3AttachmentStore        AWS S3 (boto3, lazy-imported)
  - GcsAttachmentStore       Google Cloud Storage (google-cloud-storage, lazy)

Each backend reports a `storage_provider` string (the same as its mode name)
that gets written onto the attachment row, so per-row retrieval works even
after the admin switches the active backend.
"""

from abc import ABC, abstractmethod
from typing import Optional


class AttachmentStore(ABC):
    """Server-side bytes-persistence backend for chat attachments."""

    name: str = "unknown"

    @abstractmethod
    async def store(
        self,
        user_id: str,
        file_bytes: bytes,
        filename: str,
        mime_type: str,
    ) -> dict:
        """Persist a file. Returns dict with storage_path and public_url.

        Raises IOError on backend failure, RuntimeError if not configured.
        """
        ...

    @abstractmethod
    async def read(self, storage_path: str) -> Optional[bytes]:
        """Return the raw bytes for a previously-stored file, or None."""
        ...

    @abstractmethod
    async def delete(self, storage_path: str) -> bool:
        """Remove a stored file. Returns False if it didn't exist."""
        ...

    def public_url(self, storage_path: str) -> str:
        """Synchronous URL builder. Default mirrors the local /uploads route."""
        return f"/uploads/{storage_path}"
