"""Local filesystem attachment backend.

Inbound attachments land in the user's own data home:
``data/user_data/{user_id}/uploads/{uuid}.ext`` (see app/user_workspace.py),
and are served only through an authenticated, opaque content grant. The
``storage_path`` recorded on each row is ``{user_id}/uploads/{uuid}.ext``
(relative to the user-data base), so read/delete/public_url all resolve against
that base without revealing the path to the browser.

``UPLOAD_DIR`` (or ``WEBAGENT_USER_DATA_DIR``) still overrides the base dir.
"""

import logging
import os
import uuid
from pathlib import Path
from typing import Optional

from app import user_workspace as ws
from app.attachments_store.interface import AttachmentStore
from app.encryption.workspace_files import read_encrypted_file, write_encrypted_file

logger = logging.getLogger(__name__)


def _make_storage_name(filename: str) -> str:
    ext = ""
    if "." in filename:
        ext = f".{filename.rsplit('.', 1)[1].lower()}"
    return f"{uuid.uuid4().hex}{ext}"


class LocalAttachmentStore(AttachmentStore):
    name = "local"

    def __init__(self, upload_dir: Optional[str] = None):
        # Base = the per-user data home root. An explicit upload_dir or the legacy
        # UPLOAD_DIR env still wins (so a custom mount point keeps working).
        override = upload_dir or os.environ.get("UPLOAD_DIR", "").strip()
        self._dir = Path(override) if override else ws.base_dir()

    async def store(self, user_id: str, file_bytes: bytes, filename: str, mime_type: str) -> dict:
        safe_uid = ws.safe_segment(user_id)
        rel_path = f"{safe_uid}/uploads/{_make_storage_name(filename)}"
        dest = self._dir / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            await write_encrypted_file(safe_uid, dest, file_bytes)
        except OSError as e:
            logger.error("Failed to write %s: %s", dest, e)
            raise IOError(f"Cannot write upload: {e}") from e
        from app.content_access import artifact_url_for_path
        return {
            "storage_path": rel_path,
            "public_url": artifact_url_for_path(
                user_id=user_id, storage_path=rel_path,
                original_name=filename, mime_type=mime_type,
            ),
        }

    def public_url(self, storage_path: str) -> str:
        """Return an opaque grant for a contained legacy storage path."""
        from app.content_access import artifact_url_for_path
        user_id = str(storage_path or "").replace("\\", "/").split("/", 1)[0]
        return artifact_url_for_path(user_id=user_id, storage_path=storage_path)

    async def read(self, storage_path: str) -> Optional[bytes]:
        path = self._dir / storage_path
        if not path.exists():
            return None
        try:
            owner_id = str(storage_path or "").replace("\\", "/").split("/", 1)[0]
            return await read_encrypted_file(owner_id, path)
        except (OSError, ValueError) as e:
            logger.error("Failed to read %s: %s", path, e)
            return None

    async def delete(self, storage_path: str) -> bool:
        path = self._dir / storage_path
        if not path.exists():
            return False
        try:
            path.unlink()
            return True
        except OSError as e:
            logger.error("Failed to delete %s: %s", path, e)
            return False
