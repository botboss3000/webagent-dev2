"""Browser (IndexedDB) attachment backend — server side.

When this backend is active, the frontend stores file bytes in the user's
browser via IndexedDB and POSTs only the *metadata* to /api/v1/upload (the
multipart bytes field is omitted). The server records the row with a
synthetic storage_path of `indexeddb://{attachment_id}` so the existing
attachments table shape doesn't change.

Reads return None — the server never has the bytes. Tools that depend on
server-side bytes (e.g. read_attachment, agent vision) will not work when
this backend is active. That's the documented tradeoff.
"""

import logging
from typing import Optional

from app.attachments_store.interface import AttachmentStore

logger = logging.getLogger(__name__)


class BrowserAttachmentStore(AttachmentStore):
    name = "browser"

    async def store(self, user_id: str, file_bytes: bytes, filename: str, mime_type: str) -> dict:
        # The /api/v1/upload endpoint detects this backend and skips calling
        # store() entirely (it registers a metadata-only row instead). If we
        # *are* called, treat it as an out-of-band server upload and just
        # return a placeholder — bytes are lost.
        logger.warning(
            "BrowserAttachmentStore.store() called with %d bytes — bytes are not persisted server-side.",
            len(file_bytes),
        )
        return {"storage_path": "indexeddb://server-orphan", "public_url": ""}

    async def read(self, storage_path: str) -> Optional[bytes]:
        return None

    async def delete(self, storage_path: str) -> bool:
        return False

    def public_url(self, storage_path: str) -> str:
        return ""
